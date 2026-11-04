# SPDX-License-Identifier: AGPL-3.0-or-later
"""TOC/LoF/LoT/Index table remediation.

Converts tables classified as navigational (TOC, List of Figures, etc.)
from <table> markup to semantic <nav>/<ol> markup. This removes them
from table validation scope entirely, fixing "tables should have headers"
and "table regularity" failures in Acrobat/PAC.
"""

import logging
import re
from typing import Dict

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

# Labels for aria-label on the <nav> element
_NAV_LABELS = {
    "toc": "Table of Contents",
    "lof": "List of Figures",
    "lot": "List of Tables",
    "index": "Index",
}

# Page number pattern
_PAGE_NUM = re.compile(r"^\s*(\d{1,4}|[ivxlcdm]+|[IVXLCDM]+)\s*$")


def _extract_toc_entries(table: Tag) -> list:
    """Extract (text, page_number) pairs from a TOC-like table.

    Returns a list of dicts with 'text' and 'page' keys.
    """
    entries = []
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        # Try to identify page number in last cell
        last_text = cells[-1].get_text(strip=True)
        page_num = None
        content_cells = cells

        if _PAGE_NUM.match(last_text) and len(cells) > 1:
            page_num = last_text
            content_cells = cells[:-1]

        # Join remaining cells as entry text, cleaning leader dots
        text_parts = []
        for cell in content_cells:
            cell_text = cell.get_text(strip=True)
            # Remove leader dots
            cell_text = re.sub(r"[.·]{3,}", "", cell_text)
            cell_text = re.sub(r"(\.\s+){3,}", "", cell_text)
            if cell_text:
                text_parts.append(cell_text)

        entry_text = " ".join(text_parts).strip()
        if entry_text:
            entries.append({"text": entry_text, "page": page_num})

    return entries


def _convert_table_to_nav(soup: BeautifulSoup, table: Tag, table_type: str):
    """Replace a TOC/LoF/LoT/Index table with a <nav>/<ol> structure."""
    entries = _extract_toc_entries(table)
    if not entries:
        logger.warning("No entries extracted from %s table, skipping", table_type)
        return

    label = _NAV_LABELS.get(table_type, "Navigation")

    # Build the <nav> element
    nav = soup.new_tag("nav", attrs={"aria-label": label})
    ol = soup.new_tag("ol")
    nav.append(ol)

    for entry in entries:
        li = soup.new_tag("li")
        # Entry text
        li.append(entry["text"])
        # Page number as a separate span
        if entry["page"]:
            page_span = soup.new_tag("span", attrs={"class": "page-num"})
            page_span.string = f" {entry['page']}"
            li.append(page_span)
        ol.append(li)

    # Preserve the data-table-type attribute for downstream identification
    nav["data-table-type"] = table_type

    # Replace the table with the nav
    table.replace_with(nav)
    logger.info(
        "Converted %s table to <nav>/<ol> with %d entries",
        table_type, len(entries),
    )


def convert_toc_tables(soup: BeautifulSoup) -> Dict[str, int]:
    """Convert all TOC/LoF/LoT/Index tables to semantic <nav>/<ol> markup.

    Should be called early in the remediation pipeline, before table
    remediation runs.

    Returns:
        Dict of counts by table type.
    """
    stats = {"toc": 0, "lof": 0, "lot": 0, "index": 0}

    # Collect tables first to avoid modifying during iteration
    toc_tables = []
    for table in soup.find_all("table"):
        table_type = table.get("data-table-type")
        if table_type and table_type in stats:
            toc_tables.append((table, table_type))

    for table, table_type in toc_tables:
        _convert_table_to_nav(soup, table, table_type)
        stats[table_type] += 1

    total = sum(stats.values())
    if total:
        logger.info(
            "TOC remediation: converted %d navigational tables "
            "(toc=%d, lof=%d, lot=%d, index=%d)",
            total, stats["toc"], stats["lof"], stats["lot"], stats["index"],
        )
    return stats
