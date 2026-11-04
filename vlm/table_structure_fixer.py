# SPDX-License-Identifier: AGPL-3.0-or-later
"""AI-powered table structure fixer using VLM.

Fallback for tables that fail heuristic remediation for
table-irregular-headers and table-missing-headers-id checks.
Sends table structure to a text LLM, gets structural corrections
as JSON, applies them deterministically.
"""

import json
import logging
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, Tag

from vlm.base_client import BaseVLMClient
from vlm.client_factory import create_vlm_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

TABLE_STRUCTURE_SYSTEM_PROMPT = """\
You are a WCAG 2.1 AA accessibility expert specializing in HTML table structure.

WCAG 1.3.1 requirements for data tables:
- Header cells use <th>, data cells use <td>
- Column headers need scope="col", row headers need scope="row"
- The first row (or thead row) must be ALL <th> or ALL <td>, never mixed
- If any data rows use <th> in the first column (row headers), then ALL data rows must be consistent
- Complex tables (colspan/rowspan, many rows with both row and column headers) need id/headers linkage

You must respond with ONLY a valid JSON object, no markdown fences, no explanation outside the JSON."""


TABLE_STRUCTURE_PROMPT = """\
Analyze this HTML table and determine the correct header configuration.

Current table HTML (showing first rows):
{table_html}

{structure_summary}

Current issues detected:
{issues_description}

Return a JSON object with these fields:
{{
  "header_row_indices": [list of 0-based row indices that are column header rows],
  "row_header_columns": [list of 0-based column indices that serve as row headers in data rows],
  "cells_to_fix": [
    {{"row": int, "col": int, "should_be": "th" or "td", "scope": "col" or "row" or null}}
  ],
  "reasoning": "brief explanation"
}}

Rules:
- header_row_indices: rows containing column headers (usually [0], sometimes [0,1])
- row_header_columns: columns with row headers (usually [0] or [] if none)
- cells_to_fix: only cells that need to CHANGE type. Omit cells that are already correct.
- If the first row has mixed th/td, fix ALL cells in that row to be the same type.
- If a column inconsistently has th in some data rows and td in others, make them all consistent."""


SCOPE_DETECTION_PROMPT = """\
Analyze this HTML table and determine the correct scope attribute for each <th> element.

Table HTML:
{table_html}

{structure_summary}

Each <th> needs scope="col" (column header) or scope="row" (row header).

Rules:
- Headers in the first row or in <thead> are typically scope="col"
- Headers in the first column of data rows are typically scope="row"
- A header spanning multiple columns (colspan) is scope="col"
- A header spanning multiple rows (rowspan) is scope="row"
- When a table has BOTH column headers (top) and row headers (left column), assign accordingly
- Look at the cell TEXT to determine meaning: labels like "Name", "Date", "Total" in the top row \
are column headers; labels like category names in the left column are row headers

Return a JSON object:
{{
  "scopes": [
    {{"row": 0, "col": 0, "scope": "col", "text": "first few words of header"}},
    ...
  ],
  "reasoning": "brief explanation"
}}

List EVERY <th> cell with its correct scope. Use 0-based row and column indices."""


THEAD_DETECTION_PROMPT = """\
Analyze this HTML table and determine which rows are header rows (belong in <thead>).

Table HTML (first rows shown):
{table_html}

{structure_summary}

Rules:
- Header rows contain column labels/titles, NOT data values
- Title rows (e.g., "Table 3: Revenue by Quarter") are NOT header rows — they should be <caption>
- Multi-level headers: sometimes rows 0 AND 1 are both header rows (e.g., grouped column labels)
- If a row contains units (e.g., "(in thousands)") below labels, it IS a header row
- Data rows contain actual values (numbers, names, dates, measurements, etc.)

Return a JSON object:
{{
  "header_row_indices": [0],
  "title_row_indices": [],
  "reasoning": "brief explanation"
}}

header_row_indices: 0-based indices of rows that are column headers.
title_row_indices: 0-based indices of rows that are title/caption rows (not headers, not data)."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fix_table_structure_with_ai(
    table: Tag,
    soup: BeautifulSoup,
    issues: List[str],
    llm_config: Dict[str, Any],
    client: Optional[BaseVLMClient] = None,
) -> Optional[str]:
    """Attempt AI-powered structural fix for a table.

    Args:
        table: The BeautifulSoup table element to fix.
        soup: The full document soup (for ID uniqueness).
        issues: List of issue description strings for this table.
        llm_config: LLM configuration from config.yaml.
        client: Pre-built OllamaClient (avoids reconnection per table).

    Returns:
        Description of fixes applied, or None if AI could not help.
    """
    if client is None:
        client = _get_text_client(llm_config)
        if not client.check_connection():
            logger.debug("AI table fix: Ollama unreachable")
            return None

    # Snapshot for revert
    original_html = str(table)

    # Build prompt
    table_html, structure_summary = _build_table_representation(table)
    issues_text = "\n".join(f"- {d}" for d in issues) if issues else "- Table has irregular header structure"
    prompt = TABLE_STRUCTURE_PROMPT.format(
        table_html=table_html,
        structure_summary=structure_summary,
        issues_description=issues_text,
    )

    # Call LLM (no retry by default — parse failure means prompt issue)
    max_retries = llm_config.get("table_fix_max_retries", 0)
    decision = None
    for attempt in range(1 + max_retries):
        try:
            response = client.generate(
                prompt=prompt,
                system_prompt=TABLE_STRUCTURE_SYSTEM_PROMPT,
            )
            decision = _parse_ai_response(response, table)
            if decision:
                break
            logger.debug("AI table fix: parse failed (attempt %d), response: %.200s", attempt + 1, response)
        except Exception as e:
            logger.debug("AI table fix: LLM call failed: %s", e)
            return None

    if not decision:
        return None

    logger.debug("AI table fix decision: %s", decision.get("reasoning", ""))

    # Apply fixes
    fixes = _apply_structural_fixes(table, decision)
    if not fixes:
        return None

    # Validate
    if not _validate_fix(table):
        logger.debug("AI table fix: validation failed, reverting")
        _revert_table(table, soup, original_html)
        return None

    fix_desc = "AI fix: " + "; ".join(fixes)
    logger.info("  %s", fix_desc)
    return fix_desc


def infer_table_scopes_with_ai(
    table: Tag,
    llm_config: Dict[str, Any],
    client: Optional[BaseVLMClient] = None,
    max_retries: int = 2,
) -> Optional[Dict[Tuple[int, int], str]]:
    """LLM fallback for header scope detection.

    Sends the table structure to an LLM and asks it to determine
    scope="col" or scope="row" for every <th> element.

    Args:
        table: The BeautifulSoup table element.
        llm_config: LLM configuration from config.yaml.
        client: Pre-built text client (avoids reconnection).
        max_retries: Number of retries on parse failure.

    Returns:
        Dict mapping (row_idx, col_idx) → "col"|"row" for each <th>,
        or None on failure.
    """
    if client is None:
        client = _get_text_client(llm_config)
        if not client.check_connection():
            return None

    table_html, structure_summary = _build_table_representation(table)
    prompt = SCOPE_DETECTION_PROMPT.format(
        table_html=table_html,
        structure_summary=structure_summary,
    )

    for attempt in range(1 + max_retries):
        try:
            response = client.generate(
                prompt=prompt,
                system_prompt=TABLE_STRUCTURE_SYSTEM_PROMPT,
            )
            result = _parse_scope_response(response, table)
            if result is not None:
                logger.debug("AI scope inference succeeded (attempt %d)", attempt + 1)
                return result
            logger.debug(
                "AI scope inference: parse failed (attempt %d), response: %.200s",
                attempt + 1, response,
            )
        except Exception as e:
            logger.debug("AI scope inference: LLM call failed: %s", e)
            return None

    return None


def identify_header_rows_with_ai(
    table: Tag,
    llm_config: Dict[str, Any],
    client: Optional[BaseVLMClient] = None,
    max_retries: int = 2,
) -> Optional[Tuple[List[int], List[int]]]:
    """LLM fallback for thead identification.

    Sends the table structure to an LLM and asks it to identify
    which rows are header rows and which are title rows.

    Args:
        table: The BeautifulSoup table element.
        llm_config: LLM configuration from config.yaml.
        client: Pre-built text client (avoids reconnection).
        max_retries: Number of retries on parse failure.

    Returns:
        Tuple of (header_row_indices, title_row_indices) or None on failure.
        header_row_indices: 0-based row indices that belong in <thead>.
        title_row_indices: 0-based row indices that are title/caption rows.
    """
    if client is None:
        client = _get_text_client(llm_config)
        if not client.check_connection():
            return None

    table_html, structure_summary = _build_table_representation(table)
    prompt = THEAD_DETECTION_PROMPT.format(
        table_html=table_html,
        structure_summary=structure_summary,
    )

    for attempt in range(1 + max_retries):
        try:
            response = client.generate(
                prompt=prompt,
                system_prompt=TABLE_STRUCTURE_SYSTEM_PROMPT,
            )
            result = _parse_thead_response(response, table)
            if result is not None:
                logger.debug("AI thead identification succeeded (attempt %d)", attempt + 1)
                return result
            logger.debug(
                "AI thead identification: parse failed (attempt %d), response: %.200s",
                attempt + 1, response,
            )
        except Exception as e:
            logger.debug("AI thead identification: LLM call failed: %s", e)
            return None

    return None


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def _build_table_representation(table: Tag) -> Tuple[str, str]:
    """Build a compact representation of the table for the LLM prompt.

    Returns (table_html_excerpt, structure_summary).
    """
    rows = table.find_all("tr")
    num_rows = len(rows)

    # HTML excerpt: first 3 body rows (+ last row for large tables)
    max_show = 5
    if num_rows <= max_show:
        html_excerpt = str(table)
    else:
        parts = []
        parts.append("<table>")
        caption = table.find("caption")
        if caption:
            parts.append(str(caption))
        thead = table.find("thead")
        if thead:
            parts.append(str(thead))
            body_rows = [r for r in rows if not (r.parent and r.parent.name == "thead")]
        else:
            body_rows = rows
        shown = min(3, len(body_rows))
        for r in body_rows[:shown]:
            parts.append(str(r))
        if len(body_rows) > shown + 1:
            parts.append(f"<!-- ... {len(body_rows) - shown - 1} more data rows ... -->")
        last = body_rows[-1]
        if last not in body_rows[:shown]:
            parts.append(str(last))
        parts.append("</table>")
        html_excerpt = "\n".join(parts)

    # Truncate very long excerpts
    if len(html_excerpt) > 1500:
        html_excerpt = html_excerpt[:1500] + "\n<!-- truncated -->"

    # Cell-type grid for ALL rows
    grid = _build_cell_type_grid(table)
    summary_lines = []
    num_cols = max((len(r) for r in grid), default=0)
    summary_lines.append(f"Table dimensions: {num_rows} rows x {num_cols} columns")

    # Check for colspan/rowspan
    has_span = False
    for cell in table.find_all(["th", "td"]):
        if int(cell.get("colspan", 1) or 1) > 1 or int(cell.get("rowspan", 1) or 1) > 1:
            has_span = True
            break
    if has_span:
        summary_lines.append("Table uses colspan/rowspan.")

    summary_lines.append("\nCell type grid (th/td for each cell position):")
    max_grid_rows = 8
    for i, row_types in enumerate(grid):
        if len(grid) > max_grid_rows and max_grid_rows - 2 <= i < len(grid) - 2:
            if i == max_grid_rows - 2:
                summary_lines.append(f"... ({len(grid) - max_grid_rows} rows omitted) ...")
            continue
        marker = ""
        if "th" in row_types and "td" in row_types:
            marker = "  <-- MIXED th/td"
        summary_lines.append(f"Row {i}: [{', '.join(row_types)}]{marker}")

    return html_excerpt, "\n".join(summary_lines)


def _build_cell_type_grid(table: Tag) -> List[List[str]]:
    """Build a grid showing th/td type for each cell position."""
    grid = []
    for row in table.find_all("tr"):
        row_types = []
        for cell in row.find_all(["th", "td"]):
            row_types.append(cell.name)
        grid.append(row_types)
    return grid


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_ai_response(
    response_text: str, table: Tag
) -> Optional[Dict[str, Any]]:
    """Parse and validate the JSON response from the LLM."""
    # Strip markdown fences if present
    text = response_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    # Find JSON object
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None

    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None

    # Validate structure
    rows = table.find_all("tr")
    num_rows = len(rows)
    max_cols = max(
        (len(r.find_all(["th", "td"])) for r in rows), default=0
    )

    header_rows = data.get("header_row_indices", [])
    if not isinstance(header_rows, list):
        return None
    if any(not isinstance(i, int) or i < 0 or i >= num_rows for i in header_rows):
        return None

    row_header_cols = data.get("row_header_columns", [])
    if not isinstance(row_header_cols, list):
        return None
    if any(not isinstance(i, int) or i < 0 or i >= max_cols for i in row_header_cols):
        return None

    cells_to_fix = data.get("cells_to_fix", [])
    if not isinstance(cells_to_fix, list):
        return None
    for fix in cells_to_fix:
        if not isinstance(fix, dict):
            return None
        if "row" not in fix or "col" not in fix or "should_be" not in fix:
            return None
        if fix["should_be"] not in ("th", "td"):
            return None
        if not isinstance(fix["row"], int) or fix["row"] < 0 or fix["row"] >= num_rows:
            return None
        if not isinstance(fix["col"], int) or fix["col"] < 0 or fix["col"] >= max_cols:
            return None

    return data


def _extract_json(response_text: str) -> Optional[Dict]:
    """Extract a JSON object from an LLM response, stripping markdown fences."""
    text = response_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return None


def _parse_scope_response(
    response_text: str, table: Tag
) -> Optional[Dict[Tuple[int, int], str]]:
    """Parse and validate the scope detection JSON response.

    Returns dict mapping (row_idx, col_idx) → "col"|"row", or None on failure.
    """
    data = _extract_json(response_text)
    if not data:
        return None

    scopes_list = data.get("scopes")
    if not isinstance(scopes_list, list) or not scopes_list:
        return None

    rows = table.find_all("tr")
    num_rows = len(rows)

    # Build actual th map: (row_idx, col_idx) → cell text
    th_map: Dict[Tuple[int, int], str] = {}
    for ri, row in enumerate(rows):
        for ci, cell in enumerate(row.find_all(["th", "td"])):
            if cell.name == "th":
                th_map[(ri, ci)] = cell.get_text(strip=True)[:40]

    if not th_map:
        return None

    result: Dict[Tuple[int, int], str] = {}
    matched = 0

    for entry in scopes_list:
        if not isinstance(entry, dict):
            continue
        row_idx = entry.get("row")
        col_idx = entry.get("col")
        scope = entry.get("scope")

        if not isinstance(row_idx, int) or not isinstance(col_idx, int):
            continue
        if scope not in ("col", "row"):
            continue
        if row_idx < 0 or row_idx >= num_rows:
            continue

        pos = (row_idx, col_idx)
        if pos not in th_map:
            continue

        # Fuzzy-match text as sanity check
        entry_text = str(entry.get("text", ""))[:40]
        actual_text = th_map[pos]
        if entry_text and actual_text:
            ratio = SequenceMatcher(None, entry_text.lower(), actual_text.lower()).ratio()
            if ratio >= 0.4:
                matched += 1
        else:
            matched += 1  # No text to check — accept

        result[pos] = scope

    # Reject if fewer than 50% of known th cells got a valid scope
    if len(result) < len(th_map) * 0.5:
        logger.debug(
            "AI scope inference: too few scopes returned (%d/%d)",
            len(result), len(th_map),
        )
        return None

    # Reject if fuzzy text matching failed for most entries
    if matched < len(result) * 0.5:
        logger.debug("AI scope inference: text sanity check failed (%d/%d)", matched, len(result))
        return None

    return result


def _parse_thead_response(
    response_text: str, table: Tag
) -> Optional[Tuple[List[int], List[int]]]:
    """Parse and validate the thead identification JSON response.

    Returns (header_row_indices, title_row_indices) or None on failure.
    """
    data = _extract_json(response_text)
    if not data:
        return None

    header_indices = data.get("header_row_indices")
    if not isinstance(header_indices, list) or not header_indices:
        return None

    title_indices = data.get("title_row_indices", [])
    if not isinstance(title_indices, list):
        title_indices = []

    rows = table.find_all("tr")
    num_rows = len(rows)

    # Validate header indices
    for idx in header_indices:
        if not isinstance(idx, int) or idx < 0 or idx >= num_rows:
            return None
        if idx > 4:
            # Headers shouldn't be deep in the table
            return None

    # Validate title indices
    valid_title = []
    for idx in title_indices:
        if isinstance(idx, int) and 0 <= idx < num_rows and idx <= 4:
            valid_title.append(idx)

    # Header and title rows must not overlap
    if set(header_indices) & set(valid_title):
        return None

    # Reject more than 3 header rows for tables with < 20 rows
    if len(header_indices) > 3 and num_rows < 20:
        return None

    return sorted(header_indices), sorted(valid_title)


# ---------------------------------------------------------------------------
# Fix application
# ---------------------------------------------------------------------------

def _apply_structural_fixes(
    table: Tag,
    decision: Dict[str, Any],
) -> List[str]:
    """Apply the AI's structural decisions to the table.

    Returns list of fix descriptions.
    """
    fixes = []
    rows = table.find_all("tr")

    header_row_indices = set(decision.get("header_row_indices", []))
    row_header_columns = set(decision.get("row_header_columns", []))
    cells_to_fix = decision.get("cells_to_fix", [])

    # 1. Ensure header rows have all th with scope="col"
    for idx in header_row_indices:
        if idx >= len(rows):
            continue
        row = rows[idx]
        converted = 0
        for cell in row.find_all(["td", "th"]):
            if cell.name == "td":
                cell.name = "th"
                converted += 1
            cell["scope"] = "col"
        if converted:
            fixes.append(f"row {idx}: converted {converted} cells to th (column headers)")

    # 2. Ensure row header columns have th with scope="row" in data rows
    if row_header_columns:
        data_row_indices = [i for i in range(len(rows)) if i not in header_row_indices]
        converted = 0
        for idx in data_row_indices:
            cells = rows[idx].find_all(["td", "th"])
            for col_idx in row_header_columns:
                if col_idx < len(cells):
                    cell = cells[col_idx]
                    if cell.name == "td":
                        cell.name = "th"
                        converted += 1
                    cell["scope"] = "row"
        if converted:
            fixes.append(f"converted {converted} cells in column(s) {sorted(row_header_columns)} to row headers")

    # 3. Ensure non-header cells in data rows are td (fix inconsistency)
    if row_header_columns:
        data_row_indices = [i for i in range(len(rows)) if i not in header_row_indices]
        fixed_td = 0
        for idx in data_row_indices:
            cells = rows[idx].find_all(["td", "th"])
            for col_idx, cell in enumerate(cells):
                if col_idx not in row_header_columns and cell.name == "th":
                    # Only convert if this cell wasn't in a header row
                    # and isn't explicitly kept as th by cells_to_fix
                    keep = any(
                        f["row"] == idx and f["col"] == col_idx and f["should_be"] == "th"
                        for f in cells_to_fix
                    )
                    if not keep:
                        cell.name = "td"
                        if cell.get("scope"):
                            del cell["scope"]
                        fixed_td += 1
        if fixed_td:
            fixes.append(f"converted {fixed_td} non-header cells back to td")

    # 4. Apply specific cell overrides from cells_to_fix
    specific_fixes = 0
    for fix in cells_to_fix:
        row_idx = fix["row"]
        col_idx = fix["col"]
        target = fix["should_be"]
        scope = fix.get("scope")

        if row_idx >= len(rows):
            continue
        cells = rows[row_idx].find_all(["td", "th"])
        if col_idx >= len(cells):
            continue

        cell = cells[col_idx]
        if cell.name != target:
            cell.name = target
            specific_fixes += 1

        if target == "th" and scope:
            cell["scope"] = scope
        elif target == "td" and cell.get("scope"):
            del cell["scope"]

    if specific_fixes:
        fixes.append(f"applied {specific_fixes} specific cell fixes")

    # 5. Ensure all th have scope
    for th in table.find_all("th"):
        if not th.get("scope"):
            # Infer from position
            row = th.parent
            row_idx = rows.index(row) if row in rows else -1
            if row_idx in header_row_indices:
                th["scope"] = "col"
            else:
                th["scope"] = "row"

    return fixes


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_fix(table: Tag) -> bool:
    """Check if the table would pass the audit's irregular-headers checks.

    Replicates the logic from audit/checks/table_checks.py:
    1. Header row (thead first row or row 0): no mixed th/td
    2. First column of data rows: consistent th usage
    """
    rows = table.find_all("tr")
    if len(rows) < 2:
        return True

    thead = table.find("thead")
    header_rows = [
        r for r in rows if r.parent and r.parent.name == "thead"
    ]
    data_rows = [
        r for r in rows
        if not (r.parent and r.parent.name in ("thead", "tfoot"))
    ]

    # Check 1: header row must not have mixed th/td
    check_row = header_rows[0] if header_rows else rows[0]
    check_cells = check_row.find_all(["td", "th"])
    th_count = sum(1 for c in check_cells if c.name == "th")
    if th_count > 0 and th_count < len(check_cells):
        return False

    # Check 2: first column consistency in data rows
    check_rows = data_rows if data_rows else rows
    first_col_headers = 0
    for row in check_rows:
        cells = row.find_all(["td", "th"])
        if cells and cells[0].name == "th":
            first_col_headers += 1
    if first_col_headers > 0 and first_col_headers < len(check_rows):
        return False

    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _revert_table(table: Tag, soup: BeautifulSoup, original_html: str):
    """Revert a table to its original HTML."""
    new_table = BeautifulSoup(original_html, "html.parser").find("table")
    if new_table:
        table.replace_with(new_table)


def _get_text_client(llm_config: Dict[str, Any]) -> BaseVLMClient:
    """Create a VLM client configured for text-only tasks.

    Uses text_model if configured, otherwise falls back to model.
    Lower temperature for deterministic structural decisions.
    """
    return create_vlm_client(llm_config, text_only=True)
