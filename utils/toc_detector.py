# SPDX-License-Identifier: AGPL-3.0-or-later
"""Detect Table of Contents, List of Figures/Tables, and Index tables.

Docling converts these navigational structures as regular HTML <table>
elements. This module classifies tables so the pipeline can tag them
with proper PDF structure elements (TOC/TOCI) instead of Table/TR/TD,
and convert the HTML to semantic <nav>/<ol> markup.
"""

import re
import logging
from typing import Optional

from bs4 import Tag

logger = logging.getLogger(__name__)

# Classification labels
TYPE_TOC = "toc"
TYPE_LOF = "lof"      # List of Figures
TYPE_LOT = "lot"      # List of Tables
TYPE_INDEX = "index"
TYPE_DATA = "data"     # Regular data table

# Heading patterns that signal navigational tables.
# Applied against OCR-normalized text (spaces removed, lowercased).
_TOC_HEADINGS_NORM = {"tableofcontents", "contents"}
_LOF_HEADINGS_NORM = {"listoffigures", "listoffigure"}
_LOT_HEADINGS_NORM = {"listoftables", "listoftable"}
_INDEX_HEADINGS_NORM = {"index"}

# Headings that signal a TOC continuation (e.g., "CHAPTER" between TOC pages)
_TOC_CONTINUATION_HEADINGS = {"chapter", "appendix", "appendices"}

# Section-number patterns in first cells that indicate TOC content
# e.g., "2.1 Materials", "3.2.1 Methods", "A.1 Appendix"
_SECTION_NUM = re.compile(
    r"^(?:\d+\.[\d.]*\s|[A-Z]\.\d|[ivxlcdm]+\.\s)",
    re.IGNORECASE,
)

# Fuzzy match threshold: what fraction of the canonical word's characters
# must appear (in order) in the OCR text. Handles missing characters.
_FUZZY_THRESHOLD = 0.75


def _ocr_normalize(text: str) -> str:
    """Normalize text for OCR-tolerant matching.

    Removes all whitespace and non-alphabetic characters, lowercases.
    'LIST OF FI URES' → 'listoffiures'
    """
    return re.sub(r"[^a-z]", "", text.lower())


def _heading_key(text: str) -> str:
    """Like :func:`_ocr_normalize` but keeps digits, so "Chapter 3" stays
    distinct from the bare "CHAPTER" column word of a multi-page TOC."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _fuzzy_heading_match(heading_text: str, canonical_set: set) -> bool:
    """Check if heading text fuzzy-matches any canonical form.

    Uses subsequence matching to handle OCR character deletion:
    'listoffiures' matches 'listoffigures' because all characters
    in the OCR text appear in order within the canonical.
    """
    norm = _ocr_normalize(heading_text)
    if not norm:
        return False

    for canonical in canonical_set:
        # Exact match (after normalization)
        if norm == canonical:
            return True
        # Check if the normalized OCR text is a subsequence of canonical
        # (OCR drops characters) OR canonical is a subsequence of OCR text
        # (OCR adds noise)
        if _is_near_subsequence(norm, canonical):
            return True
    return False


def _is_near_subsequence(short: str, long_: str) -> bool:
    """Check if short is approximately a subsequence of long_.

    Returns True if at least FUZZY_THRESHOLD of short's characters
    appear in order within long_, allowing for missing/extra chars.
    """
    if not short or not long_:
        return False
    # Ensure we compare the shorter against the longer
    if len(short) > len(long_):
        short, long_ = long_, short
    # Guard: short string must be a meaningful fraction of long string.
    # Very short canonicals (e.g., "contents" = 8 chars) will spuriously
    # match any sufficiently long heading as a subsequence.  Require
    # the shorter string to be at least 40% of the longer string's
    # length to avoid false positives.
    if len(short) < len(long_) * 0.4:
        return False
    matched = 0
    j = 0
    for ch in short:
        while j < len(long_):
            if long_[j] == ch:
                matched += 1
                j += 1
                break
            j += 1
    return matched >= len(short) * _FUZZY_THRESHOLD

# Page number pattern: 1-4 digits optionally with roman numerals
_PAGE_NUM = re.compile(r"^\s*(\d{1,4}|[ivxlcdm]+|[IVXLCDM]+)\s*$")

# Leader dots pattern (e.g., "Introduction . . . . 5" or "Introduction.....5")
_LEADER_DOTS = re.compile(r"[.·]{3,}|(\.\s+){3,}")

# Continued-page patterns (e.g., "Table of Contents—continued")
_CONTINUED = re.compile(r"continued|cont['.]?d", re.IGNORECASE)


def _get_preceding_heading(table: Tag) -> Optional[str]:
    """Find the nearest preceding heading element's text."""
    for sib in table.previous_siblings:
        if isinstance(sib, Tag):
            if sib.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                return sib.get_text(strip=True)
            # Also check if the sibling contains a heading
            heading = sib.find(["h1", "h2", "h3", "h4", "h5", "h6"])
            if heading:
                return heading.get_text(strip=True)
            # Skip over navigational tables and <nav> elements — they
            # shouldn't block us from reaching the heading above them
            if sib.name == "nav":
                continue
            if sib.name == "table" and sib.get("data-table-type") in (
                TYPE_TOC, TYPE_LOF, TYPE_LOT, TYPE_INDEX,
            ):
                continue
            # Stop after first non-heading block element (don't look too far)
            if sib.name in ("p", "div", "table", "ul", "ol", "section"):
                break
    # Check parent's preceding siblings (table might be inside a div/section)
    if table.parent and table.parent.name not in ("body", "html", "[document]"):
        return _get_preceding_heading(table.parent)
    return None


def _get_preceding_nav_type(table: Tag) -> Optional[str]:
    """Check if this table immediately follows a classified navigational table or nav.

    This detects continuation tables — e.g., a TOC that spans multiple
    pages may produce multiple consecutive tables.
    """
    for sib in table.previous_siblings:
        if isinstance(sib, Tag):
            # Skip headings between continuation tables
            if sib.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                continue
            # Found a navigational table or converted nav — inherit its type
            if sib.name == "nav":
                nav_type = sib.get("data-table-type")
                if nav_type in (TYPE_TOC, TYPE_LOF, TYPE_LOT, TYPE_INDEX):
                    return nav_type
            if sib.name == "table":
                table_type = sib.get("data-table-type")
                if table_type in (TYPE_TOC, TYPE_LOF, TYPE_LOT, TYPE_INDEX):
                    return table_type
                # Hit a non-navigational table — stop
                break
            # Skip junk <p> elements (single-char OCR artifacts at page boundaries)
            if sib.name == "p" and len(sib.get_text(strip=True)) <= 2:
                continue
            # Any other block element means this isn't a continuation
            if sib.name in ("p", "div", "ul", "ol", "section"):
                break
    return None


def _get_page_number_ratio(table: Tag) -> float:
    """What fraction of rows have a page-number-like value in the last cell."""
    rows = table.find_all("tr")
    if not rows:
        return 0.0

    page_num_count = 0
    for row in rows:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        last_text = cells[-1].get_text(strip=True)
        if _PAGE_NUM.match(last_text):
            page_num_count += 1

    return page_num_count / len(rows) if rows else 0.0


def _has_leader_dots(table: Tag) -> bool:
    """Check if any cell in the table contains leader dots."""
    for cell in table.find_all(["td", "th"]):
        text = cell.get_text()
        if _LEADER_DOTS.search(text):
            return True
    return False


def _get_column_count_range(table: Tag) -> tuple:
    """Get (min, max) column counts across rows."""
    rows = table.find_all("tr")
    if not rows:
        return (0, 0)
    counts = []
    for row in rows:
        cells = row.find_all(["td", "th"])
        col_count = 0
        for cell in cells:
            colspan = int(cell.get("colspan", 1))
            col_count += colspan
        counts.append(col_count)
    return (min(counts), max(counts)) if counts else (0, 0)


def classify_table(html_table: Tag) -> str:
    """Classify an HTML table as TOC, LoF, LoT, Index, or data table.

    Uses a combination of:
    - Preceding heading text
    - Table structure (column count, page numbers in last column)
    - Content patterns (leader dots, roman/arabic numerals)

    Args:
        html_table: A BeautifulSoup <table> Tag.

    Returns:
        One of: "toc", "lof", "lot", "index", "data"
    """
    # Already classified (e.g., by a previous pass)
    existing = html_table.get("data-table-type")
    if existing and existing != TYPE_DATA:
        return existing

    heading = _get_preceding_heading(html_table)
    page_ratio = _get_page_number_ratio(html_table)
    has_dots = _has_leader_dots(html_table)
    min_cols, max_cols = _get_column_count_range(html_table)
    row_count = len(html_table.find_all("tr"))

    # Score-based classification
    # Strong signal: heading text (fuzzy match handles OCR artifacts)
    if heading:
        # Check for "continued" variant (e.g., "Table of Contents—continued")
        if _fuzzy_heading_match(heading, _TOC_HEADINGS_NORM) or (
            _CONTINUED.search(heading)
            and _is_near_subsequence(
                _ocr_normalize("contents"), _ocr_normalize(heading)
            )
        ):
            html_table["data-table-type"] = TYPE_TOC
            logger.debug("Classified table as TOC (heading: %s)", heading)
            return TYPE_TOC
        if _fuzzy_heading_match(heading, _LOF_HEADINGS_NORM) or (
            _CONTINUED.search(heading)
            and _is_near_subsequence(
                _ocr_normalize("listoffigures"), _ocr_normalize(heading)
            )
        ):
            html_table["data-table-type"] = TYPE_LOF
            logger.debug("Classified table as LoF (heading: %s)", heading)
            return TYPE_LOF
        if _fuzzy_heading_match(heading, _LOT_HEADINGS_NORM) or (
            _CONTINUED.search(heading)
            and _is_near_subsequence(
                _ocr_normalize("listoftables"), _ocr_normalize(heading)
            )
        ):
            html_table["data-table-type"] = TYPE_LOT
            logger.debug("Classified table as LoT (heading: %s)", heading)
            return TYPE_LOT
        if _fuzzy_heading_match(heading, _INDEX_HEADINGS_NORM) and page_ratio > 0.3:
            html_table["data-table-type"] = TYPE_INDEX
            logger.debug("Classified table as Index (heading: %s)", heading)
            return TYPE_INDEX

    # Continuation detection: if this table immediately follows a classified
    # navigational table (possibly separated by a heading like "CHAPTER"),
    # and has similar structural characteristics, inherit that classification.
    preceding_nav = _get_preceding_nav_type(html_table)
    if preceding_nav:
        heading_norm = _heading_key(heading) if heading else ""
        is_continuation_heading = (
            not heading
            or heading_norm in _TOC_CONTINUATION_HEADINGS
            or _CONTINUED.search(heading or "")
        )
        # When we have a continuation heading, leader dots, OR page-number
        # evidence, classify as continuation. Docling's column count is
        # unreliable for multi-column TOCs, so we don't gate on max_cols.
        if is_continuation_heading or page_ratio >= 0.4 or has_dots:
            html_table["data-table-type"] = preceding_nav
            logger.debug(
                "Classified table as %s (continuation, heading: %s)",
                preceding_nav, heading,
            )
            return preceding_nav

    # Heading-based: "CHAPTER" or "APPENDIX" with high page ratio → TOC
    if heading and page_ratio >= 0.4:
        heading_norm = _heading_key(heading)
        if heading_norm in _TOC_CONTINUATION_HEADINGS:
            html_table["data-table-type"] = TYPE_TOC
            logger.debug("Classified table as TOC (chapter heading: %s)", heading)
            return TYPE_TOC

    # Medium signal: structural patterns (no heading match, but looks like TOC)
    # High page-number ratio + few columns + multiple rows = likely TOC/LoF
    if page_ratio >= 0.6 and max_cols <= 4 and row_count >= 3:
        # Check the first cell text of some rows to distinguish TOC from LoF
        first_cells_text = []
        for row in html_table.find_all("tr")[:10]:
            cells = row.find_all(["td", "th"])
            if cells:
                first_cells_text.append(cells[0].get_text(strip=True).lower())

        has_figure_refs = sum(
            1 for t in first_cells_text
            if re.match(r"^\d+\.\s", t) or "figure" in t or "fig" in t
        )
        has_chapter_refs = sum(
            1 for t in first_cells_text
            if re.match(r"^[ivxlcdm]+\.\s", t, re.IGNORECASE)
            or re.match(r"^chapter\b", t, re.IGNORECASE)
            or re.match(r"^appendix\b", t, re.IGNORECASE)
        )
        # Also check for section-numbered content (e.g., "2.1 Materials")
        has_section_nums = sum(
            1 for t in first_cells_text if _SECTION_NUM.match(t)
        )

        if has_figure_refs > len(first_cells_text) * 0.5:
            html_table["data-table-type"] = TYPE_LOF
            logger.debug("Classified table as LoF (structural pattern)")
            return TYPE_LOF

        if has_dots or has_chapter_refs > 0 or has_section_nums > len(first_cells_text) * 0.3:
            html_table["data-table-type"] = TYPE_TOC
            logger.debug("Classified table as TOC (structural pattern)")
            return TYPE_TOC

    return TYPE_DATA


def classify_tables_in_soup(soup) -> dict:
    """Classify all tables in a BeautifulSoup document.

    Returns:
        Dict mapping table index (0-based) to classification string.
    """
    results = {}
    tables = soup.find_all("table")
    for idx, table in enumerate(tables):
        classification = classify_table(table)
        results[idx] = classification
        if classification != TYPE_DATA:
            logger.info(
                "Table %d classified as %s", idx, classification
            )
    return results
