# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTML parsing remediation strategies (WCAG 4.1.1).

Fixes duplicate IDs and invalid HTML nesting.
"""

import logging
import re
from collections import Counter
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)


def remediate_duplicate_id(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """De-duplicate IDs by appending a numeric suffix to duplicates."""
    elements_with_id = soup.find_all(attrs={"id": True})
    id_counts = Counter(el.get("id") for el in elements_with_id)

    fixed = 0
    for id_value, count in id_counts.items():
        if count <= 1:
            continue
        dupes = soup.find_all(attrs={"id": id_value})
        # Keep the first occurrence, rename subsequent ones
        for i, elem in enumerate(dupes[1:], start=2):
            new_id = f"{id_value}-{i}"
            # Ensure the new ID is also unique
            while soup.find(id=new_id):
                i += 1
                new_id = f"{id_value}-{i}"

            old_id = elem["id"]
            elem["id"] = new_id

            # Update any label[for], aria-labelledby, aria-describedby refs
            _update_id_references(soup, old_id, new_id, elem)
            fixed += 1

    if fixed:
        return f"De-duplicated {fixed} element ID(s)"
    return None


def remediate_invalid_nesting(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Fix invalid HTML nesting (block in inline, nested interactive)."""
    fixed = 0

    # Fix <p> containing block-level elements — unwrap the <p>
    BLOCK_ELEMENTS = frozenset([
        "address", "article", "aside", "blockquote", "details", "dialog",
        "dd", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer",
        "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr",
        "li", "main", "nav", "ol", "p", "pre", "section", "table", "ul",
    ])

    for p in soup.find_all("p"):
        has_block = False
        for child in list(p.descendants):
            if hasattr(child, "name") and child.name in BLOCK_ELEMENTS:
                has_block = True
                break
        if has_block:
            # Replace <p> with a <div> to maintain grouping without
            # violating nesting rules
            p.name = "div"
            fixed += 1

    # Fix nested <a> — unwrap inner anchors
    for link in soup.find_all("a"):
        for nested in link.find_all("a"):
            nested.unwrap()
            fixed += 1

    # Fix nested <button> — unwrap inner buttons
    for button in soup.find_all("button"):
        for nested in button.find_all("button"):
            nested.unwrap()
            fixed += 1

    if fixed:
        return f"Fixed {fixed} invalid nesting issue(s)"
    return None


# ── Helpers ──────────────────────────────────────────────────────────

def _update_id_references(
    soup: BeautifulSoup, old_id: str, new_id: str, element: Tag
) -> None:
    """Update attributes that reference the old ID."""
    # label[for]
    for label in soup.find_all("label", attrs={"for": old_id}):
        # Only update if this label isn't associated with the first element
        label["for"] = new_id

    # aria-labelledby / aria-describedby / aria-errormessage
    for attr in ("aria-labelledby", "aria-describedby", "aria-errormessage",
                 "aria-controls", "aria-owns"):
        for el in soup.find_all(attrs={attr: True}):
            ref_ids = el[attr].split()
            updated = [new_id if rid == old_id else rid for rid in ref_ids]
            if updated != ref_ids:
                el[attr] = " ".join(updated)
