# SPDX-License-Identifier: AGPL-3.0-or-later
# Portions derived from Content Accessibility Utility on AWS
# (https://github.com/awslabs/content-accessibility-utility-on-aws),
# Copyright 2025 Amazon.com, Inc. or its affiliates, Apache-2.0.
# Modified by the ReadReady project; see NOTICE.
"""Table remediation strategies.

Fixes table headers, scope attributes, captions, thead, and tbody.
Uses heuristics with optional LLM fallback for ambiguous tables.
"""

import json
import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from remediate.element_resolver import get_id_index, resolve_issue_element

logger = logging.getLogger(__name__)


def _clean_css_path(path: str) -> str:
    """Clean audit-generated CSS paths for use with select_one().

    The auditor produces paths like '[document] > html > body > table'
    where '[document]' is not a valid CSS selector.
    """
    path = re.sub(r"\[document\]\s*>\s*", "", path)
    return path.strip()


def _try_css_path(soup: BeautifulSoup, path: str):
    """Try to resolve a CSS path, with fallbacks for DOM mutations.

    Earlier remediation phases (e.g. adding <main>) can insert wrapper
    elements that invalidate audit-generated CSS paths.  We try the
    original path first, then progressively relax ancestor constraints.
    """
    cleaned = _clean_css_path(path)
    # Attempt 1: exact path
    try:
        result = soup.select_one(cleaned)
        if result:
            return result
    except Exception:
        pass

    # Attempt 2: strip intermediate wrappers (main, section, article, div)
    # that may have been inserted between body and the content
    relaxed = re.sub(
        r"(body)\s*>\s*(?:main|section|article)\s*>",
        r"\1 ",
        cleaned,
    )
    if relaxed != cleaned:
        try:
            result = soup.select_one(relaxed)
            if result:
                return result
        except Exception:
            pass

    # Attempt 2b: relax body > child to body descendant — handles case
    # where DOM gained a wrapper (e.g. <main>) that the audit path lacks
    descendant = re.sub(r"body\s*>", "body ", cleaned)
    if descendant != cleaned:
        try:
            result = soup.select_one(descendant)
            if result:
                return result
        except Exception:
            pass

    # Attempt 3: use just the table selector portion (e.g. "table:nth-of-type(5)")
    table_match = re.search(r"(table(?::nth-of-type\(\d+\))?)", cleaned)
    if table_match:
        try:
            result = soup.select_one(table_match.group(1))
            if result:
                return result
        except Exception:
            pass

    return None


def get_table_from_issue(soup: BeautifulSoup, issue: Dict[str, Any]):
    """Find the specific table referenced by an issue.

    Uses a layered fallback strategy with content verification.
    Earlier remediation phases can shift table indices and invalidate CSS
    paths, so every candidate is verified against the issue's text_content
    when available.
    """
    context = issue.get("context", {})
    location = issue.get("location", {})
    expected_text = ""
    if isinstance(context, dict):
        expected_text = (context.get("text_content") or "")[:120]

    def _resolve_table(el):
        """Extract table from a CSS-selected element."""
        if not el:
            return None
        if el.name == "table":
            return el
        return el.find_parent("table")

    def _content_matches(table):
        """Check if table content matches the expected text signature."""
        if not expected_text:
            return True  # No signature to verify against
        # Window must be large enough to accommodate captions/preamble
        # that push the header text further into the text stream.
        window = max(500, len(expected_text) + 400)
        return expected_text in table.get_text(strip=True)[:window]

    # Fast path: a stamped id names the element directly, so there is no index
    # to shift and no sibling counting. Content verification still applies --
    # the id could name an element inside a different table if the audit path
    # was generated before a structural change.
    #
    # get_id_index(soup) is called here (rather than trusting the resolver's
    # bare getattr) so this function is self-sufficient: whether or not a
    # caller has already primed the cache (the production path via
    # RemediationManager.remediate_issues does), the first call here builds
    # the index once and every call after reuses the cached dict from the
    # soup object, so per-call cost stays a hash lookup instead of a scan.
    _id_index = get_id_index(soup)
    _by_id = resolve_issue_element(soup, issue, id_index=_id_index)
    if _by_id is not None:
        _table = _by_id if _by_id.name == "table" else _by_id.find_parent("table")
        if _table is not None and _content_matches(_table):
            return _table

    # Collect candidates from CSS paths (context path, then location path)
    css_candidates = []
    if isinstance(context, dict):
        path = context.get("path") or context.get("element_path", "")
        if path:
            t = _resolve_table(_try_css_path(soup, path))
            if t:
                css_candidates.append(t)

    if isinstance(location, dict):
        path = location.get("path", "")
        if path and "table" in path.lower():
            t = _resolve_table(_try_css_path(soup, path))
            if t and t not in css_candidates:
                css_candidates.append(t)

    # Accept first CSS candidate whose content matches
    for candidate in css_candidates:
        if _content_matches(candidate):
            return candidate

    # --- Position-based fallback (with nearby search) ---
    if isinstance(context, dict):
        position = context.get("position", {})
        element_name = context.get("element_name", "")
        index = position.get("index", -1)

        if isinstance(index, int) and index >= 0:
            if element_name == "table":
                tables = soup.find_all("table")
                if index < len(tables):
                    if _content_matches(tables[index]):
                        return tables[index]
                    # Index may have shifted — search nearby
                    for offset in [-1, 1, -2, 2, -3, 3]:
                        adj = index + offset
                        if 0 <= adj < len(tables):
                            if _content_matches(tables[adj]):
                                return tables[adj]
            elif element_name == "th":
                all_th = soup.find_all("th")
                if index < len(all_th):
                    parent_table = all_th[index].find_parent("table")
                    if parent_table:
                        return parent_table

    # --- Content-matching fallback (scan all tables) ---
    if expected_text:
        for table in soup.find_all("table"):
            window = max(500, len(expected_text) + 400)
            if expected_text in table.get_text(strip=True)[:window]:
                return table

    # --- Accept unverified CSS candidate if nothing else matched ---
    if css_candidates:
        return css_candidates[0]

    # --- Last resort: first table ---
    return soup.find("table")


def infer_scope_from_position(table, th) -> str:
    """Infer scope (col or row) based on cell position."""
    row = th.parent
    if not row or row.name != "tr":
        return "col"

    rows = table.find_all("tr")
    row_index = rows.index(row) if row in rows else -1
    cells = row.find_all(["th", "td"])
    cell_index = cells.index(th) if th in cells else -1

    # Rows in thead -> column headers
    thead = table.find("thead")
    if thead and row in thead.find_all("tr"):
        return "col"

    # First row -> column headers
    if row_index == 0:
        return "col"
    # First column -> row headers
    if cell_index == 0:
        return "row"

    # Second cell in a row where a rowspan th occupies cell 0 -> row header
    if cell_index == 1:
        first_cell = cells[0] if cells else None
        if first_cell and first_cell.name == "th" and int(first_cell.get("rowspan", 1) or 1) > 1:
            return "row"

    # Check role hints
    if th.get("role") == "rowheader" or any(
        "rowhead" in c.lower() for c in th.get("class", [])
    ):
        return "row"
    if th.get("role") == "columnheader" or any(
        "colhead" in c.lower() for c in th.get("class", [])
    ):
        return "col"

    return "col"


def _build_column_grid(table):
    """Build a grid mapping (row_index, col_index) to cell elements.

    Accounts for colspan and rowspan so that each cell occupies the
    correct set of grid positions.

    Returns (grid_dict, num_rows, num_cols).
    """
    rows = table.find_all("tr")
    if not rows:
        return {}, 0, 0

    grid = {}  # (row_idx, col_idx) -> cell element
    num_rows = len(rows)
    num_cols = 0

    for row_idx, row in enumerate(rows):
        col_idx = 0
        for cell in row.find_all(["th", "td"]):
            # Skip columns already occupied by rowspan from above
            while (row_idx, col_idx) in grid:
                col_idx += 1

            colspan = int(cell.get("colspan", 1) or 1)
            rowspan = int(cell.get("rowspan", 1) or 1)

            for r in range(rowspan):
                for c in range(colspan):
                    if (row_idx + r) < num_rows:
                        grid[(row_idx + r, col_idx + c)] = cell

            col_idx += colspan
        num_cols = max(num_cols, col_idx)

    return grid, num_rows, num_cols


def _get_header_row_indices(table, rows):
    """Identify which rows are header rows."""
    thead = table.find("thead")
    indices = set()
    for row_idx, row in enumerate(rows):
        if thead and row.parent == thead:
            indices.add(row_idx)
        elif row_idx == 0 and row.find("th"):
            indices.add(row_idx)
    return indices


def _normalize_table_headers(table):
    """Normalize irregular table header structure.

    Ensures the table has consistent th/td usage:
    0. Stray th: when < 10% of cells are th and no header row exists,
       convert all th to td (they are misclassified by Docling).
    1. First row: all cells become th (column headers) if any are th.
    2. First column of data rows: cells become th (row headers) when the
       table has a header row and the first column contains label text.

    Returns list of fix descriptions.
    """
    rows = table.find_all("tr")
    if len(rows) < 2:
        return []

    fixes = []

    # Case 0: detect stray <th> elements (very low ratio, no header row)
    all_cells = table.find_all(["th", "td"])
    all_th = [c for c in all_cells if c.name == "th"]
    total_cells = len(all_cells)
    th_count = len(all_th)

    if total_cells > 0 and th_count > 0:
        th_ratio = th_count / total_cells
        first_row_cells_check = rows[0].find_all(["th", "td"])
        first_row_all_th = (
            all(c.name == "th" for c in first_row_cells_check)
            if first_row_cells_check
            else False
        )
        thead = table.find("thead")
        has_thead_header = thead is not None and thead.find("th") is not None

        if (
            th_ratio < 0.10
            and not first_row_all_th
            and not has_thead_header
            and th_count <= 3
        ):
            converted = 0
            for th in all_th:
                th.name = "td"
                if th.get("scope"):
                    del th["scope"]
                if th.get("id"):
                    del th["id"]
                converted += 1
            if converted:
                fixes.append(
                    f"converted {converted} stray th to td "
                    f"(ratio {th_ratio:.1%})"
                )
            return fixes  # No further header normalization needed

    first_row = rows[0]
    first_row_cells = first_row.find_all(["th", "td"])

    # Case 1: first row has mixed th/td — convert all to th
    first_row_th = [c for c in first_row_cells if c.name == "th"]
    first_row_td = [c for c in first_row_cells if c.name == "td"]

    if first_row_th and first_row_td:
        converted = 0
        for td in first_row_td:
            td.name = "th"
            if not td.get("scope"):
                td["scope"] = "col"
            converted += 1
        if converted:
            fixes.append(f"converted {converted} first-row cells to th")

    # Determine if first row is now a header row (all th)
    first_row_cells = first_row.find_all(["th", "td"])
    has_header_row = all(c.name == "th" for c in first_row_cells) if first_row_cells else False

    # Case 2: the row-label ("stub") column should be th (row headers).
    #
    # The stub is normally column 0, but when a header spans the first columns
    # (colspan), the data rows carry an empty leading cell and the real labels
    # sit in column 1 (a common model-comparison layout). We therefore
    # locate the stub via the colspan/rowspan-aware grid rather than the DOM
    # first cell, skipping any leading column that is empty across all data
    # rows. The search is capped to the first two columns and never selects the
    # last column, so a value column is not mistaken for a stub and at least one
    # data column always remains (single-column tables stay unconverted).
    grid, num_rows, num_cols = _build_column_grid(table)
    header_indices = _get_header_row_indices(table, rows)
    data_row_idxs = [i for i in range(num_rows) if i not in header_indices]

    if has_header_row and data_row_idxs and num_cols > 1:
        stub_col = None
        for col in range(min(2, num_cols - 1)):
            cells_in_col = []
            seen = set()
            for r in data_row_idxs:
                cell = grid.get((r, col))
                if cell is None or id(cell) in seen:
                    continue
                seen.add(id(cell))
                cells_in_col.append(cell)
            if not cells_in_col:
                continue
            non_empty = sum(1 for c in cells_in_col if c.get_text(strip=True))
            if non_empty > len(cells_in_col) / 2:
                stub_col = col
                break  # first label-bearing column wins

        if stub_col is not None:
            stub_cells = []
            seen = set()
            for r in data_row_idxs:
                cell = grid.get((r, stub_col))
                if cell is None or id(cell) in seen:
                    continue
                seen.add(id(cell))
                stub_cells.append(cell)

            stub_th = sum(1 for c in stub_cells if c.name == "th")
            stub_td = sum(1 for c in stub_cells if c.name == "td")
            td_texts = [
                c.get_text(strip=True) for c in stub_cells if c.name == "td"
            ]

            # Convert if: some rows already have th (normalize the rest), or all
            # are td but most contain non-empty text (label-like content).
            should_convert = False
            if stub_th > 0 and stub_td > 0:
                should_convert = True  # Mixed → normalize
            elif stub_td > 0 and stub_th == 0:
                if len([t for t in td_texts if t]) > len(td_texts) / 2:
                    should_convert = True

            if should_convert:
                converted = 0
                for cell in stub_cells:
                    if cell.name == "td":
                        cell.name = "th"
                        if not cell.get("scope"):
                            cell["scope"] = "row"
                        converted += 1
                if converted:
                    fixes.append(
                        f"converted {converted} stub-column cells to row headers"
                    )

        # First-column DOM consistency. A group-label column with rowspan
        # leaves the continuation rows' first DOM cell as the secondary label,
        # and Docling sometimes marks the stub on only some rows. If the first
        # DOM cell is mixed th/td across data rows, normalize the laggards to
        # th(scope=row) so the table is not flagged table-irregular-headers.
        data_dom_rows = [rows[i] for i in data_row_idxs]
        first_cells = [
            r.find_all(["th", "td"])[0]
            for r in data_dom_rows
            if r.find_all(["th", "td"])
        ]
        n_first_th = sum(1 for c in first_cells if c.name == "th")
        if first_cells and 0 < n_first_th < len(first_cells):
            converted = 0
            for cell in first_cells:
                if cell.name == "td":
                    cell.name = "th"
                    if not cell.get("scope"):
                        cell["scope"] = "row"
                    converted += 1
            if converted:
                fixes.append(
                    f"normalized {converted} first-column cells for consistency"
                )

    # Ensure all th have scope
    for th in table.find_all("th"):
        if not th.get("scope"):
            th["scope"] = infer_scope_from_position(table, th)

    return fixes


def remediate_table_headers_id(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add IDs to table headers and link data cells via headers attribute.

    Also normalizes the table header structure first to ensure consistent
    th/td usage regardless of processing order.
    """
    table = get_table_from_issue(soup, issue)
    if not table:
        return None

    # Normalize headers first — ensures consistent structure before
    # assigning IDs, regardless of whether irregular-headers ran yet
    _normalize_table_headers(table)

    headers = table.find_all("th")
    if not headers:
        return None

    # Assign globally unique IDs to all th elements.
    # Collect ALL existing IDs in the document to avoid cross-table collisions.
    used_ids = {el["id"] for el in soup.find_all(id=True)}
    for i, th in enumerate(headers):
        if not th.get("id"):
            text = th.get_text(strip=True).lower()
            text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
            base_id = f"header-{text or i}"
            header_id = base_id
            counter = 2
            while header_id in used_ids:
                header_id = f"{base_id}-{counter}"
                counter += 1
            th["id"] = header_id
            used_ids.add(header_id)

    # Build a column grid that respects colspan/rowspan
    grid, num_rows, num_cols = _build_column_grid(table)
    rows = table.find_all("tr")
    header_row_indices = _get_header_row_indices(table, rows)

    # Link each <td> to its column and row headers using grid positions
    linked = 0
    seen_cells = set()
    for row_idx in range(num_rows):
        if row_idx in header_row_indices:
            continue
        for col_idx in range(num_cols):
            cell = grid.get((row_idx, col_idx))
            if not cell or cell.name != "td" or id(cell) in seen_cells:
                continue
            seen_cells.add(id(cell))

            header_ids = []

            # Column headers: th cells at this column in header rows
            # Skip headers with empty text (spacer corner cells)
            colspan = int(cell.get("colspan", 1) or 1)
            for c in range(col_idx, col_idx + colspan):
                for hr_idx in sorted(header_row_indices):
                    h_cell = grid.get((hr_idx, c))
                    if (h_cell and h_cell.name == "th" and h_cell.get("id")
                            and h_cell.get_text(strip=True)):
                        if h_cell["id"] not in header_ids:
                            header_ids.append(h_cell["id"])

            # Row headers: th cells at columns before this cell in the
            # same row, found via the grid (handles rowspan correctly)
            for c in range(col_idx):
                rh_cell = grid.get((row_idx, c))
                if (rh_cell and rh_cell.name == "th" and rh_cell.get("id")
                        and rh_cell["id"] not in header_ids):
                    header_ids.append(rh_cell["id"])

            if header_ids:
                cell["headers"] = " ".join(header_ids)
                linked += 1

    return f"Added IDs to {len(headers)} headers and linked {linked} data cells"


def _is_title_row(row) -> bool:
    """Check if a row is a title/subtitle spanning all columns (not a header).

    A title row typically has a single cell spanning many columns, containing
    a descriptive title rather than column labels.
    """
    cells = row.find_all(["td", "th"])
    if not cells:
        return False
    # Single cell spanning all columns
    if len(cells) == 1:
        colspan = int(cells[0].get("colspan", 1) or 1)
        if colspan > 1:
            return True
    return False


def _find_header_row(rows) -> int:
    """Find the best header row index in a table.

    Skips title rows (single cells spanning all columns) and returns
    the first row with multiple distinct cells containing text that
    looks like column labels.

    Returns the row index, or 0 as fallback.
    """
    for i, row in enumerate(rows[:4]):  # Check first 4 rows
        if _is_title_row(row):
            continue
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        # Check if multiple cells have text (column-label-like)
        non_empty = sum(1 for c in cells if c.get_text(strip=True))
        if non_empty >= 2:
            return i
    return 0


def remediate_table_missing_headers(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add headers to a table that has none.

    Strategy: promote the best header row to <th>, or insert a synthetic
    header if no suitable row is found. Title rows (single cell spanning
    all columns) are skipped in favour of the first multi-cell row.
    """
    table = get_table_from_issue(soup, issue)
    if not table:
        return None

    if table.find("th"):
        return None

    rows = table.find_all("tr")
    if not rows:
        return None

    header_idx = _find_header_row(rows)
    header_row = rows[header_idx]

    # If header row is not row 0, convert preceding title rows into a caption
    if header_idx > 0 and not table.find("caption"):
        title_texts = []
        for i in range(header_idx):
            text = rows[i].get_text(strip=True)
            if text:
                title_texts.append(text)
        if title_texts:
            caption = soup.new_tag("caption")
            caption.string = " — ".join(title_texts)
            table.insert(0, caption)

    converted = 0
    for td in header_row.find_all("td"):
        td.name = "th"
        td["scope"] = "col"
        converted += 1

    if converted:
        return f"Converted {converted} cells in row {header_idx} to header cells"
    return None


def remediate_table_missing_scope(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add scope attributes to th cells based on position."""
    table = get_table_from_issue(soup, issue)

    if not table:
        # Try all tables
        tables = soup.find_all("table")
        if not tables:
            return None
        total = 0
        for t in tables:
            for th in t.find_all("th"):
                if not th.get("scope"):
                    th["scope"] = infer_scope_from_position(t, th)
                    total += 1
        return f"Added scope to {total} header cells" if total else None

    headers = table.find_all("th")
    scopeless = [th for th in headers if not th.get("scope")]
    if not scopeless:
        return None

    # Try LLM fallback for ambiguous tables
    if _has_ambiguous_scope(table) and _can_use_ai_table_analysis():
        ai_scopes = _try_ai_scope_inference(table)
        if ai_scopes:
            count = 0
            for th in scopeless:
                pos = _get_cell_position(table, th)
                if pos in ai_scopes:
                    th["scope"] = ai_scopes[pos]
                    count += 1
                else:
                    th["scope"] = infer_scope_from_position(table, th)
                    count += 1
            return f"Added scope to {count} header cells (AI-assisted)" if count else None

    # Heuristic fallback
    count = 0
    for th in scopeless:
        th["scope"] = infer_scope_from_position(table, th)
        count += 1

    return f"Added scope to {count} header cells" if count else None


def _generate_table_caption(table) -> str:
    """Generate a descriptive caption based on table content analysis.

    Examines title rows, header cells, and preceding headings to build
    a meaningful caption. Falls back to a generic description.
    """
    rows = table.find_all("tr")

    # Check for a title row (single cell spanning all columns) at the top
    for row in rows[:2]:
        cells = row.find_all(["td", "th"])
        if len(cells) == 1:
            colspan = int(cells[0].get("colspan", 1) or 1)
            if colspan > 1:
                text = cells[0].get_text(strip=True)
                if text and len(text) > 3:
                    return text[:200]

    # Try header cells to infer what the table is about
    headers = table.find_all("th")
    if headers:
        header_texts = [h.get_text(strip=True) for h in headers[:6] if h.get_text(strip=True)]
        if header_texts:
            return "Table of " + ", ".join(header_texts[:4])

    # Fall back to preceding heading
    prev_heading = table.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
    if prev_heading:
        heading_text = prev_heading.get_text(strip=True)
        if heading_text:
            return heading_text

    return "Data table"


def remediate_table_missing_caption(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add a caption to tables that lack one."""
    table = get_table_from_issue(soup, issue)
    if not table or table.find("caption"):
        return None

    # Skip tables that are effectively layout tables — adding a caption
    # would reclassify them as data tables in the post-audit.
    if _is_layout_table(table):
        return None

    caption_text = _generate_table_caption(table)

    caption = soup.new_tag("caption")
    caption.string = caption_text
    table.insert(0, caption)

    return f"Added caption: {caption_text}"


def remediate_table_missing_thead(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Wrap the first row in a thead element."""
    table = get_table_from_issue(soup, issue) or soup.find("table")
    if not table or table.find("thead"):
        return None

    rows = table.find_all("tr")
    if not rows:
        return None

    # Try LLM fallback when header row identification is ambiguous
    if _is_ambiguous_header_row(table, rows) and _can_use_ai_table_analysis():
        ai_result = _try_ai_thead_identification(table)
        if ai_result:
            header_indices, title_indices = ai_result

            # Convert title rows to caption
            if title_indices and not table.find("caption"):
                title_texts = []
                for ti in title_indices:
                    if ti < len(rows):
                        text = rows[ti].get_text(strip=True)
                        if text:
                            title_texts.append(text)
                if title_texts:
                    caption = soup.new_tag("caption")
                    caption.string = " — ".join(title_texts)
                    table.insert(0, caption)

            # Convert header row cells to th and wrap in thead
            thead = soup.new_tag("thead")
            # Process in reverse order so extraction doesn't shift indices
            header_rows_to_wrap = []
            for hi in sorted(header_indices):
                if hi < len(rows):
                    header_row = rows[hi]
                    if not header_row.find("th"):
                        for td in header_row.find_all("td"):
                            td.name = "th"
                            td["scope"] = "col"
                    header_rows_to_wrap.append(header_row)
            for hr in header_rows_to_wrap:
                hr.extract()
                thead.append(hr)

            # Insert thead after caption if present, else at start
            caption_el = table.find("caption")
            if caption_el:
                caption_el.insert_after(thead)
            else:
                table.insert(0, thead)

            return "Added thead element (AI-assisted)"

    # Heuristic fallback: promote first row
    first_row = rows[0]
    # If first row has no th, convert tds to th
    if not first_row.find("th"):
        for td in first_row.find_all("td"):
            td.name = "th"

    thead = soup.new_tag("thead")
    first_row.extract()
    thead.append(first_row)
    table.insert(0, thead)

    return "Added thead element"


def remediate_table_missing_tbody(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Wrap non-header rows in a tbody element."""
    table = get_table_from_issue(soup, issue)
    if not table or table.find("tbody"):
        return None

    tbody = soup.new_tag("tbody")
    body_rows = []
    for row in table.find_all("tr"):
        if row.parent and row.parent.name in ("thead", "tfoot"):
            continue
        body_rows.append(row)

    if not body_rows:
        return None

    for row in body_rows:
        row.extract()
        tbody.append(row)

    table.append(tbody)
    return f"Added tbody with {len(body_rows)} rows"


def remediate_table_irregular_headers(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Fix irregular header structures by normalizing th/td placement."""
    table = get_table_from_issue(soup, issue)
    if not table:
        return None

    fixes = _normalize_table_headers(table)
    if fixes:
        return "Fixed irregular headers: " + "; ".join(fixes)

    # Heuristic returned no fixes — the table may have already been
    # normalized by the batch pre-pass (see fix_all_tables ~line 1025).
    # Re-validate against the audit's own check before reporting failure.
    if _batch_validate_headers(table):
        return "Already normalized in batch pre-pass"

    # Heuristic failed — try AI fallback
    ai_result = _try_ai_table_fix(table, soup, [issue])
    if ai_result:
        return ai_result

    return remediate_table_missing_scope(soup, issue, *args)


def _batch_validate_headers(table) -> bool:
    """Quick check if a table's header structure would pass audit.

    Returns True if headers are consistent (no mixed th/td in header row,
    no inconsistent first-column th in data rows).
    """
    rows = table.find_all("tr")
    if len(rows) < 2:
        return True

    # Check header row (thead first row or row 0)
    thead = table.find("thead")
    header_rows = [r for r in rows if r.parent and r.parent.name == "thead"]
    check_row = header_rows[0] if header_rows else rows[0]
    cells = check_row.find_all(["td", "th"])
    th_count = sum(1 for c in cells if c.name == "th")
    if 0 < th_count < len(cells):
        return False

    # Check first-column consistency in data rows
    data_rows = [
        r for r in rows
        if not (r.parent and r.parent.name in ("thead", "tfoot"))
    ]
    if not data_rows:
        data_rows = rows
    first_col_th = 0
    for row in data_rows:
        cells = row.find_all(["td", "th"])
        if cells and cells[0].name == "th":
            first_col_th += 1
    if 0 < first_col_th < len(data_rows):
        return False

    return True


def _is_layout_table(table) -> bool:
    """Check if a table is likely used for layout (mirrors audit logic)."""
    if table.get("role") == "presentation":
        return True
    css_classes = table.get("class", [])
    if any(
        p in " ".join(css_classes).lower()
        for p in ("layout", "grid", "non-data")
    ):
        return True
    if not table.find("th") and not table.find("caption"):
        rows = table.find_all("tr")
        if len(rows) <= 1:
            return True
        first_row_cells = rows[0].find_all(["td", "th"]) if rows else []
        if len(first_row_cells) <= 1:
            return True
    return False


def _snapshot_table_rows(table) -> list:
    """Capture the first header-like row + first data row of a table.

    Returns a list of row dicts, each containing a list of cell dicts:
      [{"cells": [{"text": "...", "is_th": bool, "scope": "col"|"row"|""}, ...], "is_thead": bool}, ...]
    """
    rows = table.find_all("tr")
    if not rows:
        return []

    result = []
    # Find the first row that has th cells (or first row if none do)
    header_row_idx = 0
    for i, row in enumerate(rows):
        if row.find("th"):
            header_row_idx = i
            break

    # Capture header row + next data row (up to 2 rows total)
    indices = [header_row_idx]
    if header_row_idx + 1 < len(rows):
        indices.append(header_row_idx + 1)
    # If header row isn't row 0, also include row 0 (shows title row context)
    if header_row_idx > 0:
        indices = [0] + indices
    # Limit to 3 rows max
    indices = indices[:3]

    for idx in indices:
        row = rows[idx]
        is_thead = row.parent and row.parent.name == "thead"
        cells = []
        for cell in row.find_all(["td", "th"]):
            text = cell.get_text(strip=True)[:40]
            cells.append({
                "text": text,
                "is_th": cell.name == "th",
                "scope": cell.get("scope", ""),
            })
        result.append({"cells": cells, "is_thead": is_thead})

    return result


def batch_fix_all_tables(
    soup: BeautifulSoup,
    table_page_map: Optional[Dict[str, int]] = None,
) -> tuple:
    """Safety-net sweep: ensure every data table has basic accessibility.

    Runs after per-issue remediation to catch anything the per-issue
    processing missed (e.g. due to CSS path failures or ordering issues).

    All operations are idempotent — they check before modifying.

    Args:
        soup: Parsed HTML document.
        table_page_map: Optional mapping of table-text-prefix → PDF page number,
            built from docling_structure.json by the caller.  Used to annotate
            per-table change records with source page numbers.

    Returns:
        Tuple of (stats_dict, table_changes_list).
        table_changes is a list of per-table dicts for the changes report.
    """
    stats = {"scope_added": 0, "thead_added": 0, "tbody_added": 0,
             "headers_normalized": 0, "caption_added": 0,
             "stray_th_fixed": 0, "regularity_cells_added": 0,
             "empty_th_fixed": 0}
    table_changes: list = []

    tables = soup.find_all("table")
    for table in tables:
        if _is_layout_table(table):
            continue

        # Skip tables that have been classified as TOC/LoF/LoT/Index
        if table.get("data-table-type") in ("toc", "lof", "lot", "index"):
            continue

        rows = table.find_all("tr")
        if not rows:
            continue

        # Determine PDF page number for this table via the page map.
        # Match on the first 40 chars of the table's text content.
        table_page_number = None
        if table_page_map:
            table_text = table.get_text(separator=" ", strip=True)[:40]
            for prefix, pno in table_page_map.items():
                if table_text.startswith(prefix) or prefix.startswith(table_text[:20]):
                    table_page_number = pno
                    break

        # Snapshot pre-fix stats to detect changes for this table
        pre_stats = dict(stats)

        # Snapshot header row + first data row BEFORE remediation
        before_rows = _snapshot_table_rows(table)

        # 0. Fix stray th elements (very few th, no header row)
        all_cells = table.find_all(["th", "td"])
        all_th_cells = [c for c in all_cells if c.name == "th"]
        total_cells = len(all_cells)
        th_count = len(all_th_cells)

        if total_cells > 0 and th_count > 0:
            th_ratio = th_count / total_cells
            first_row_cells_check = rows[0].find_all(["th", "td"])
            first_row_all_th = (
                all(c.name == "th" for c in first_row_cells_check)
                if first_row_cells_check
                else False
            )
            thead = table.find("thead")
            has_thead_header = thead is not None and thead.find("th") is not None

            if (
                th_ratio < 0.10
                and not first_row_all_th
                and not has_thead_header
                and th_count <= 3
            ):
                for th in all_th_cells:
                    th.name = "td"
                    if th.get("scope"):
                        del th["scope"]
                    if th.get("id"):
                        del th["id"]
                    stats["stray_th_fixed"] += 1
                # Fall through to step 0.5 so header promotion runs
                # after stray th elements have been cleared.

        # 0.5. If table has no headers at all, find and promote best header row
        if not table.find("th"):
            # Try AI identification when header row is ambiguous
            ai_header_applied = False
            if _is_ambiguous_header_row(table, rows) and _can_use_ai_table_analysis():
                ai_result = _try_ai_thead_identification(table)
                if ai_result:
                    ai_header_indices, ai_title_indices = ai_result
                    # Convert title rows to caption
                    if ai_title_indices and not table.find("caption"):
                        title_texts = []
                        for ti in ai_title_indices:
                            if ti < len(rows):
                                text = rows[ti].get_text(strip=True)
                                if text:
                                    title_texts.append(text)
                        if title_texts:
                            caption = soup.new_tag("caption")
                            caption.string = " — ".join(title_texts)
                            table.insert(0, caption)
                            stats["caption_added"] += 1
                    # Promote AI-identified header rows
                    for hi in ai_header_indices:
                        if hi < len(rows):
                            for td in rows[hi].find_all("td"):
                                td.name = "th"
                                if not td.get("scope"):
                                    td["scope"] = "col"
                                stats["headers_normalized"] += 1
                    ai_header_applied = True

            if not ai_header_applied:
                # Heuristic fallback
                header_idx = _find_header_row(rows)
                header_row = rows[header_idx]
                # Convert title rows above the header into a caption
                if header_idx > 0 and not table.find("caption"):
                    title_texts = []
                    for ti in range(header_idx):
                        text = rows[ti].get_text(strip=True)
                        if text:
                            title_texts.append(text)
                    if title_texts:
                        caption = soup.new_tag("caption")
                        caption.string = " — ".join(title_texts)
                        table.insert(0, caption)
                        stats["caption_added"] += 1
                # Promote header row cells to th
                for td in header_row.find_all("td"):
                    td.name = "th"
                    if not td.get("scope"):
                        td["scope"] = "col"
                    stats["headers_normalized"] += 1

        # 1. Normalize irregular headers (mixed th/td in first row)
        # Re-fetch rows in case caption was inserted
        rows = table.find_all("tr")
        first_row = rows[0]
        first_row_cells = first_row.find_all(["th", "td"])
        first_row_th = [c for c in first_row_cells if c.name == "th"]
        first_row_td = [c for c in first_row_cells if c.name == "td"]

        if first_row_th and first_row_td:
            for td in first_row_td:
                td.name = "th"
                if not td.get("scope"):
                    td["scope"] = "col"
                stats["headers_normalized"] += 1

        # 2. Ensure all th have scope (AI fallback for ambiguous tables)
        scopeless_ths = [th for th in table.find_all("th") if not th.get("scope")]
        if scopeless_ths:
            ai_scopes = None
            if _has_ambiguous_scope(table) and _can_use_ai_table_analysis():
                ai_scopes = _try_ai_scope_inference(table)
            for th in scopeless_ths:
                if ai_scopes:
                    pos = _get_cell_position(table, th)
                    th["scope"] = ai_scopes.get(pos, infer_scope_from_position(table, th))
                else:
                    th["scope"] = infer_scope_from_position(table, th)
                stats["scope_added"] += 1

        # 3. Add thead if table has th but no thead
        # Find the first row with th cells (may not be row 0 if title rows exist)
        rows = table.find_all("tr")
        th_row = None
        for r in rows:
            if r.find("th"):
                th_row = r
                break
        if th_row and not table.find("thead"):
            thead = soup.new_tag("thead")
            th_row.extract()
            thead.append(th_row)
            # Insert thead at start (after caption if present)
            caption = table.find("caption")
            if caption:
                caption.insert_after(thead)
            else:
                table.insert(0, thead)
            stats["thead_added"] += 1

        # 4. Add tbody if rows exist outside thead/tfoot and no tbody
        if not table.find("tbody") and len(table.find_all("tr")) > 0:
            body_rows = [
                r for r in table.find_all("tr")
                if not (r.parent and r.parent.name in ("thead", "tfoot"))
            ]
            if body_rows:
                tbody = soup.new_tag("tbody")
                for row in body_rows:
                    row.extract()
                    tbody.append(row)
                table.append(tbody)
                stats["tbody_added"] += 1

        # 4.5. Fix table regularity — must run after thead/tbody
        # restructuring (step 3/4) because moving a row with rowspan
        # into thead can create column count mismatches.
        reg_added = fix_table_regularity(soup, table)
        stats["regularity_cells_added"] += reg_added

        # 5. Full header normalization (handles first-column consistency
        #    that step 1's first-row-only fix doesn't cover)
        norm_fixes = _normalize_table_headers(table)
        if norm_fixes:
            stats["headers_normalized"] += len(norm_fixes)

        # 5.5. AI fallback for tables that still have irregular headers
        #      after heuristic normalization
        rows = table.find_all("tr")
        if len(rows) >= 2:
            if not _batch_validate_headers(table):
                ai_result = _try_ai_table_fix(table, soup, [])
                if ai_result:
                    stats["headers_normalized"] += 1

        # 6. Add headers/id for complex tables that lack them
        _batch_add_headers_id(soup, table, stats)

        # 6.5. Convert empty <th> to <td> — screen readers can't use
        # headers with no discernible text content.  Must run after all
        # header normalization steps (1, 5, 5.5, 6) that may create <th>.
        removed_ids = set()
        # Pre-check: does this table use first-column row headers?
        _data_rows = [
            r for r in table.find_all("tr")
            if not (r.parent and r.parent.name in ("thead", "tfoot"))
        ]
        _fc_th = sum(
            1 for r in _data_rows
            for cells in [r.find_all(["th", "td"])]
            if cells and cells[0].name == "th"
        )
        _fc_total = sum(
            1 for r in _data_rows if r.find_all(["th", "td"])
        )
        _has_row_headers = _fc_total > 0 and _fc_th > _fc_total / 2

        for th in list(table.find_all("th")):
            if not th.get_text(strip=True):
                # Keep empty <th> in thead — removing breaks first-row
                # header consistency (table-irregular-headers audit).
                if th.parent and th.parent.parent and th.parent.parent.name == "thead":
                    continue
                # Keep empty first-column <th> in row-header tables —
                # these are continuation rows; removing breaks
                # first-column consistency.
                if _has_row_headers and th.parent and th.parent.name == "tr":
                    cells = th.parent.find_all(["th", "td"])
                    if cells and cells[0] is th:
                        continue
                old_id = th.get("id")
                if old_id:
                    removed_ids.add(old_id)
                    del th["id"]
                th.name = "td"
                if th.get("scope"):
                    del th["scope"]
                stats["empty_th_fixed"] += 1
        # Clean up dangling headers references from data cells
        if removed_ids:
            for td in table.find_all("td", attrs={"headers": True}):
                hdr_val = td["headers"]
                hdr_ids = hdr_val if isinstance(hdr_val, list) else hdr_val.split()
                cleaned = [h for h in hdr_ids if h not in removed_ids]
                if cleaned:
                    td["headers"] = " ".join(cleaned)
                else:
                    del td["headers"]

        # 7. Add caption if still missing
        if not table.find("caption"):
            caption_text = _generate_table_caption(table)
            caption = soup.new_tag("caption")
            caption.string = caption_text
            # Insert before thead if present, else at start
            thead = table.find("thead")
            if thead:
                thead.insert_before(caption)
            else:
                table.insert(0, caption)
            stats["caption_added"] += 1

        # Record per-table changes for the changes report
        delta = {k: stats[k] - pre_stats.get(k, 0) for k in stats}
        changes_made = []
        if delta.get("regularity_cells_added"):
            changes_made.append(
                f"Column regularity fixed ({delta['regularity_cells_added']} cells)"
            )
        if delta.get("stray_th_fixed"):
            changes_made.append(
                f"Stray header cells converted to data cells ({delta['stray_th_fixed']})"
            )
        if delta.get("headers_normalized"):
            changes_made.append(
                f"Headers normalised ({delta['headers_normalized']} cells)"
            )
        if delta.get("scope_added"):
            changes_made.append(f"Scope attributes added ({delta['scope_added']})")
        if delta.get("thead_added"):
            changes_made.append("Table header section added")
        if delta.get("tbody_added"):
            changes_made.append("Table body section added")
        if delta.get("caption_added"):
            changes_made.append("Table caption generated and added")
        if delta.get("headers_id_added"):
            changes_made.append(
                f"Header/cell ID linkage added ({delta['headers_id_added']} links)"
            )
        if changes_made:
            caption_el = table.find("caption")
            caption_label = (
                caption_el.get_text(strip=True)[:60] if caption_el else ""
            )
            # Count total rows/cols for context
            all_rows = table.find_all("tr")
            n_rows = len(all_rows)
            n_cols = max(
                (len(r.find_all(["td", "th"])) for r in all_rows), default=0
            )
            n_header_cells = len(table.find_all("th"))
            after_rows = _snapshot_table_rows(table)
            table_changes.append({
                "page_number": table_page_number,
                "caption": caption_label,
                "changes": changes_made,
                "delta": delta,
                "rows": n_rows,
                "cols": n_cols,
                "header_cells": n_header_cells,
                "before_rows": before_rows,
                "after_rows": after_rows,
            })

    total = sum(stats.values())
    if total:
        logger.info(
            "  Batch table fixup: %d scope, %d thead, %d tbody, "
            "%d headers normalized, %d captions, %d headers-id, "
            "%d stray th fixed, %d regularity cells added",
            stats["scope_added"], stats["thead_added"], stats["tbody_added"],
            stats["headers_normalized"], stats["caption_added"],
            stats.get("headers_id_added", 0), stats["stray_th_fixed"],
            stats["regularity_cells_added"],
        )
    return stats, table_changes


def _batch_add_headers_id(soup: BeautifulSoup, table, stats: Dict[str, int]):
    """Add headers/id to a complex table if missing.

    Only acts on tables that have multiple rows with <th> (indicating
    both column and row headers) and don't already have headers/id.
    """
    # Skip if already has headers/id linkage
    if table.find("td", attrs={"headers": True}):
        return
    if table.find("th", attrs={"id": True}):
        return

    # Check if this table is complex enough to need headers/id.
    # Act on tables with multiple header rows, tables that have both
    # row and column headers, or tables with colspan/rowspan in headers
    # (colspan makes scope alone insufficient for cell association).
    header_rows = [r for r in table.find_all("tr") if r.find("th")]
    has_row_headers = any(
        th.get("scope") == "row" for th in table.find_all("th")
    )
    has_col_headers = any(
        th.get("scope") == "col" for th in table.find_all("th")
    )
    has_spanning_headers = any(
        int(th.get("colspan", 1) or 1) > 1 or int(th.get("rowspan", 1) or 1) > 1
        for th in table.find_all("th")
    )
    if (len(header_rows) <= 1
            and not (has_row_headers and has_col_headers)
            and not has_spanning_headers):
        return

    headers = table.find_all("th")
    if not headers:
        return

    # Assign globally unique IDs
    used_ids = {el["id"] for el in soup.find_all(id=True)}
    for i, th in enumerate(headers):
        if not th.get("id"):
            text = th.get_text(strip=True).lower()
            text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
            base_id = f"header-{text or i}"
            header_id = base_id
            counter = 2
            while header_id in used_ids:
                header_id = f"{base_id}-{counter}"
                counter += 1
            th["id"] = header_id
            used_ids.add(header_id)

    # Build grid and link cells
    grid, num_rows, num_cols = _build_column_grid(table)
    rows = table.find_all("tr")
    header_row_indices = _get_header_row_indices(table, rows)

    linked = 0
    seen_cells = set()
    for row_idx in range(num_rows):
        if row_idx in header_row_indices:
            continue
        for col_idx in range(num_cols):
            cell = grid.get((row_idx, col_idx))
            if not cell or cell.name != "td" or id(cell) in seen_cells:
                continue
            seen_cells.add(id(cell))

            header_ids = []
            colspan = int(cell.get("colspan", 1) or 1)
            for c in range(col_idx, col_idx + colspan):
                for hr_idx in sorted(header_row_indices):
                    h_cell = grid.get((hr_idx, c))
                    if (h_cell and h_cell.name == "th" and h_cell.get("id")
                            and h_cell.get_text(strip=True)):
                        if h_cell["id"] not in header_ids:
                            header_ids.append(h_cell["id"])

            for c in range(col_idx):
                rh_cell = grid.get((row_idx, c))
                if (rh_cell and rh_cell.name == "th" and rh_cell.get("id")
                        and rh_cell["id"] not in header_ids):
                    header_ids.append(rh_cell["id"])

            if header_ids:
                cell["headers"] = " ".join(header_ids)
                linked += 1

    if linked:
        stats["headers_id_added"] = stats.get("headers_id_added", 0) + 1


def _compute_effective_col_counts(table):
    """Compute effective column count per row, accounting for rowspan/colspan.

    Returns list of col counts, one per row.
    """
    rows = table.find_all("tr")
    occupied = {}
    col_counts = []
    for row_idx, row in enumerate(rows):
        col_idx = 0
        for cell in row.find_all(["td", "th"]):
            while (row_idx, col_idx) in occupied:
                col_idx += 1
            colspan = int(cell.get("colspan", 1) or 1)
            rowspan = int(cell.get("rowspan", 1) or 1)
            for r in range(rowspan):
                for c in range(colspan):
                    occupied[(row_idx + r, col_idx + c)] = True
            col_idx += colspan
        while (row_idx, col_idx) in occupied:
            col_idx += 1
        col_counts.append(col_idx)
    return col_counts


def fix_table_regularity(soup: BeautifulSoup, table) -> int:
    """Ensure all rows in a table have the same effective column count.

    Handles two cases:
    1. Rows that are too wide (extra empty cells conflicting with rowspan
       from prior rows — common Docling artifact) — removes excess cells.
    2. Rows that are too short — pads with empty <td> cells.

    Uses the most common column count (mode) as the target, which avoids
    inflating the target when a few rows have spurious extra cells.

    Returns the number of cells added or removed.
    """
    rows = table.find_all("tr")
    if len(rows) < 2:
        return 0

    col_counts = _compute_effective_col_counts(table)
    if not col_counts or len(set(col_counts)) <= 1:
        return 0

    # Target is the most common effective column count (mode).
    from collections import Counter
    count_freq = Counter(col_counts)
    target_cols = count_freq.most_common(1)[0][0]

    fixed = 0

    # Pass 0: Clip overlapping colspans within rows that exceed the target.
    # When Docling misdetects a cell's colspan extent, two adjacent cells
    # can overlap (cell A's col+colspan > cell B's start col).  Reducing
    # the colspan is semantically safer than removing placeholder cells,
    # which would misalign multi-level headers.
    rows = table.find_all("tr")
    for row_idx, row in enumerate(rows):
        if col_counts[row_idx] <= target_cols:
            continue
        cells = row.find_all(["td", "th"])
        # Build occupied set from rowspans in prior rows
        occupied_p0: set = set()
        for prior_idx in range(row_idx):
            pcol = 0
            for pcell in rows[prior_idx].find_all(["td", "th"]):
                while (prior_idx, pcol) in occupied_p0:
                    pcol += 1
                pcs = int(pcell.get("colspan", 1) or 1)
                prs = int(pcell.get("rowspan", 1) or 1)
                for dr in range(prs):
                    for dc in range(pcs):
                        occupied_p0.add((prior_idx + dr, pcol + dc))
                pcol += pcs
        # Map each cell to its grid column position
        cell_positions = []
        col_idx = 0
        for cell in cells:
            while (row_idx, col_idx) in occupied_p0:
                col_idx += 1
            cs = int(cell.get("colspan", 1) or 1)
            cell_positions.append((col_idx, cell, cs))
            col_idx += cs
        # Detect and clip overlapping colspans
        clipped_any = False
        for i in range(len(cell_positions) - 1):
            curr_col, curr_cell, curr_cs = cell_positions[i]
            next_col, _, _ = cell_positions[i + 1]
            if curr_col + curr_cs > next_col:
                new_cs = max(1, next_col - curr_col)
                if new_cs == 1:
                    if "colspan" in curr_cell.attrs:
                        del curr_cell["colspan"]
                else:
                    curr_cell["colspan"] = str(new_cs)
                clipped_any = True
                fixed += 1
        if clipped_any:
            col_counts = _compute_effective_col_counts(table)
            if len(set(col_counts)) <= 1:
                return fixed

    # Pass 1: Remove excess empty cells from rows that are too wide.
    for row_idx, row in enumerate(rows):
        if col_counts[row_idx] <= target_cols:
            continue
        excess = col_counts[row_idx] - target_cols
        cells = row.find_all(["th", "td"])
        removed = 0
        # Remove empty cells from end first, then start
        for cell in reversed(list(cells)):
            if removed >= excess:
                break
            if not cell.get_text(strip=True):
                cell.decompose()
                removed += 1
                fixed += 1
        if removed < excess:
            cells = row.find_all(["th", "td"])
            for cell in list(cells):
                if removed >= excess:
                    break
                if not cell.get_text(strip=True):
                    cell.decompose()
                    removed += 1
                    fixed += 1

    # Recompute after removals
    if fixed > 0:
        col_counts = _compute_effective_col_counts(table)

    # Pass 2: Pad short rows with empty <td> cells.
    rows = table.find_all("tr")
    for row_idx, row in enumerate(rows):
        if col_counts[row_idx] < target_cols:
            deficit = target_cols - col_counts[row_idx]
            for _ in range(deficit):
                empty_td = soup.new_tag("td")
                row.append(empty_td)
                fixed += 1

    # Pass 3: If still irregular (wide rows couldn't be trimmed because cells
    # had content), fall back to padding everything up to the max column count.
    col_counts = _compute_effective_col_counts(table)
    if len(set(col_counts)) > 1:
        max_cols = max(col_counts)
        rows = table.find_all("tr")
        for row_idx, row in enumerate(rows):
            if col_counts[row_idx] < max_cols:
                deficit = max_cols - col_counts[row_idx]
                for _ in range(deficit):
                    empty_td = soup.new_tag("td")
                    row.append(empty_td)
                    fixed += 1

    return fixed


def remediate_table_irregular_columns(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Fix tables with inconsistent column counts by padding short rows."""
    table = get_table_from_issue(soup, issue)
    if not table:
        return None
    added = fix_table_regularity(soup, table)
    if added:
        return f"Padded {added} cells to fix column regularity"
    return None


@dataclass
class _TableAIState:
    """Per-document table-AI state. ``llm_config`` None means no AI."""
    llm_config: Optional[Dict[str, Any]] = None
    table_analysis: Dict[str, Any] = field(default_factory=dict)
    client: Optional[Any] = None
    checked: bool = False
    calls: int = 0


_TABLE_AI: ContextVar[Optional[_TableAIState]] = ContextVar("_TABLE_AI", default=None)

# Per-process memory of whether an LLM endpoint answered, keyed by its config.
# A bulk run over hundreds of documents must not ping (or wait out a connect
# timeout for) the same endpoint once per document.
_CONNECTIVITY: Dict[str, bool] = {}


def _connectivity_key(llm_config: Dict[str, Any]) -> str:
    return json.dumps(llm_config, sort_keys=True, default=str)


def configure_table_ai(
    llm_config: Optional[Dict[str, Any]], table_analysis: Optional[Dict[str, Any]]
) -> None:
    """Set table-AI state for the current document.

    Called by remediate_html at the start of every document with the run's
    LLM config, or None when the pipeline has no AI available.
    """
    _TABLE_AI.set(_TableAIState(llm_config=llm_config,
                                table_analysis=dict(table_analysis or {})))


def _table_ai_client() -> Optional[Any]:
    """Return the current document's connected LLM client, or None."""
    state = _TABLE_AI.get()
    if state is None or state.llm_config is None:
        return None
    if not state.checked:
        state.checked = True
        key = _connectivity_key(state.llm_config)
        if _CONNECTIVITY.get(key) is False:
            return None
        try:
            from vlm.table_structure_fixer import _get_text_client
            client = _get_text_client(state.llm_config)
            if _CONNECTIVITY.get(key) or client.check_connection():
                _CONNECTIVITY[key] = True
                state.client = client
            else:
                _CONNECTIVITY[key] = False
                logger.debug("AI table analysis: LLM unreachable")
        except Exception as e:
            # Fail closed: no client means no AI table analysis this document.
            logger.debug("AI table analysis: client unavailable (%s)", e)
    return state.client


def _try_ai_table_fix(
    table, soup: BeautifulSoup, issues: list
) -> Optional[str]:
    """Try AI-powered table structure fix if configured.

    Lazy-imports vlm.table_structure_fixer to avoid circular imports
    and to gracefully handle missing Ollama connectivity.
    Caches the client and connection check across calls.
    """
    try:
        client = _table_ai_client()
        if client is None:
            return None
        state = _TABLE_AI.get()
        from vlm.table_structure_fixer import fix_table_structure_with_ai
        issue_descriptions = [
            i.get("description", "") for i in issues if isinstance(i, dict)
        ]
        return fix_table_structure_with_ai(
            table=table,
            soup=soup,
            issues=issue_descriptions,
            llm_config=state.llm_config,
            client=client,
        )
    except Exception as e:
        logger.debug("AI table fix unavailable: %s", e)
        return None


# ---------------------------------------------------------------------------
# LLM-based table semantic analysis (scope detection + thead identification)
# ---------------------------------------------------------------------------

def _can_use_ai_table_analysis() -> bool:
    """Check if LLM table analysis is allowed and within per-document limits."""
    if _table_ai_client() is None:
        return False
    state = _TABLE_AI.get()
    if not state.table_analysis.get("enabled", False):
        return False
    return state.calls < state.table_analysis.get("max_tables_per_document", 30)


def _has_ambiguous_scope(table) -> bool:
    """Check if heuristic scope detection would be ambiguous for this table.

    Returns True when any <th> would fall through to the default "col"
    return in infer_scope_from_position, or when the table has headers
    in mixed positions suggesting both row and column headers.
    """
    thead = table.find("thead")
    rows = table.find_all("tr")
    if not rows:
        return False

    ths = table.find_all("th")
    if not ths:
        return False

    th_in_non_standard_pos = False
    rows_with_th = set()

    for th in ths:
        row = th.parent
        if not row or row.name != "tr":
            continue
        row_index = rows.index(row) if row in rows else -1
        cells = row.find_all(["th", "td"])
        cell_index = cells.index(th) if th in cells else -1

        rows_with_th.add(row_index)

        # Check if this th is in a non-standard position (would default to "col")
        in_thead = thead and row in thead.find_all("tr")
        in_row_0 = row_index == 0
        in_col_0 = cell_index == 0
        has_role = th.get("role") in ("rowheader", "columnheader") or any(
            kw in c.lower()
            for c in th.get("class", [])
            for kw in ("rowhead", "colhead")
        )

        if not in_thead and not in_row_0 and not in_col_0 and not has_role:
            th_in_non_standard_pos = True

    # Ambiguous: th in non-standard position
    if th_in_non_standard_pos:
        return True

    # Ambiguous: multiple rows contain th (multi-level headers)
    if len(rows_with_th) > 2:
        return True

    # Ambiguous: th in both row-0 AND col-0 of data rows (mixed layout)
    has_row0_th = False
    has_col0_data_th = False
    for ri, row in enumerate(rows):
        cells = row.find_all(["th", "td"])
        if ri == 0 and any(c.name == "th" for c in cells):
            has_row0_th = True
        if ri > 0 and cells and cells[0].name == "th":
            has_col0_data_th = True
    if has_row0_th and has_col0_data_th:
        return True

    return False


def _is_ambiguous_header_row(table, rows) -> bool:
    """Check if header row identification is uncertain.

    Returns True when the heuristic _find_header_row would be unreliable.
    """
    if not rows:
        return False

    # No th anywhere and no thead — completely uncertain
    if not table.find("th") and not table.find("thead"):
        return True

    first_row = rows[0]
    first_cells = first_row.find_all(["th", "td"])

    # First row has no th — heuristic will default to row 0 but we're not sure
    if not any(c.name == "th" for c in first_cells) and not table.find("thead"):
        # Check if first row looks like a title (single spanning cell)
        if len(first_cells) == 1:
            colspan = int(first_cells[0].get("colspan", 1) or 1)
            if colspan > 1:
                return True
        # Check if first row is all-numeric (data, not headers)
        texts = [c.get_text(strip=True) for c in first_cells]
        if texts and all(re.match(r'^[\d.,\-$%()]+$', t) for t in texts if t):
            return True

    return False


def _get_cell_position(table, th):
    """Get (row_idx, col_idx) for a <th> element in a table."""
    rows = table.find_all("tr")
    row = th.parent
    if not row or row.name != "tr":
        return (-1, -1)
    row_index = rows.index(row) if row in rows else -1
    cells = row.find_all(["th", "td"])
    cell_index = cells.index(th) if th in cells else -1
    return (row_index, cell_index)


def _try_ai_scope_inference(table) -> Optional[Dict]:
    """Try AI-powered scope inference for a table.

    Thin wrapper around infer_table_scopes_with_ai that handles
    lazy init, counter tracking, and error catching.

    Returns dict mapping (row_idx, col_idx) → "col"|"row", or None.
    """
    try:
        client = _table_ai_client()
        if client is None:
            return None
        state = _TABLE_AI.get()
        max_retries = state.table_analysis.get("max_retries", 2)
        from vlm.table_structure_fixer import infer_table_scopes_with_ai
        state.calls += 1
        return infer_table_scopes_with_ai(
            table=table,
            llm_config=state.llm_config,
            client=client,
            max_retries=max_retries,
        )
    except Exception as e:
        logger.debug("AI scope inference unavailable: %s", e)
        return None


def _try_ai_thead_identification(table):
    """Try AI-powered thead identification for a table.

    Thin wrapper around identify_header_rows_with_ai that handles
    lazy init, counter tracking, and error catching.

    Returns (header_row_indices, title_row_indices) or None.
    """
    try:
        client = _table_ai_client()
        if client is None:
            return None
        state = _TABLE_AI.get()
        max_retries = state.table_analysis.get("max_retries", 2)
        from vlm.table_structure_fixer import identify_header_rows_with_ai
        state.calls += 1
        return identify_header_rows_with_ai(
            table=table,
            llm_config=state.llm_config,
            client=client,
            max_retries=max_retries,
        )
    except Exception as e:
        logger.debug("AI thead identification unavailable: %s", e)
        return None
