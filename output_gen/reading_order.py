# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading order utilities for preserving Docling's column-aware element ordering.

When gap recovery or fallback text extraction adds new elements, they must be
inserted into the existing element list WITHOUT re-sorting existing elements.
Docling's AI model produces column-aware reading order that a naive spatial
sort (page_no, -bbox.t) destroys for multi-column documents.
"""

import logging
from typing import Dict, List

logger = logging.getLogger(__name__)


def insert_elements_preserving_order(
    existing: List[Dict],
    new_elements: List[Dict],
) -> List[Dict]:
    """Insert new elements into the existing list, preserving Docling's order.

    For pages with NO existing elements (fallback/OCR):
        New elements are inserted at the page boundary (after the last element
        of the previous page, before the first of the next page).

    For pages WITH existing elements (gap recovery):
        Each new element is inserted after the nearest existing element on the
        same page whose bbox.t >= the new element's bbox.t (i.e., the element
        just above or at the same vertical level). If no existing element is
        above, the new element goes before the first element on the page.

    Args:
        existing: Current element list in Docling reading order.
        new_elements: Elements to insert (gap recovery, fallback, or OCR).

    Returns:
        Combined list with new elements interleaved, existing order preserved.
    """
    if not new_elements:
        return existing

    # Group new elements by page
    new_by_page: Dict[int, List[Dict]] = {}
    for elem in new_elements:
        pn = elem.get("page_no", 0)
        new_by_page.setdefault(pn, []).append(elem)

    # Sort new elements within each page top-to-bottom.
    # Safe because these come from PyMuPDF (spatial extraction), not Docling.
    for pn_elems in new_by_page.values():
        pn_elems.sort(key=lambda e: -e.get("bbox", {}).get("t", 0))

    # Build index: page_no -> (first_idx, last_idx) in existing list
    page_ranges: Dict[int, List[int]] = {}
    for i, elem in enumerate(existing):
        pn = elem.get("page_no", 0)
        if pn not in page_ranges:
            page_ranges[pn] = [i, i]
        else:
            page_ranges[pn][1] = i

    # Build result by copying existing and inserting new elements.
    # Process pages in reverse order so earlier insertions don't shift
    # indices for later ones.
    result = list(existing)

    for pn in sorted(new_by_page.keys(), reverse=True):
        new_page_elems = new_by_page[pn]

        if pn in page_ranges:
            # Page HAS existing elements -> interleave by vertical proximity
            _insert_into_existing_page(result, new_page_elems, page_ranges[pn])
        else:
            # Page has NO existing elements -> insert at page boundary
            insert_at = _find_page_boundary(page_ranges, pn, len(result))
            for j, new_elem in enumerate(new_page_elems):
                result.insert(insert_at + j, new_elem)

    return result


def _insert_into_existing_page(
    result: List[Dict],
    new_elems: List[Dict],
    page_range: List[int],
) -> None:
    """Insert new elements among existing elements on the same page.

    Modifies *result* in place. Processes new elements from bottom to top
    (reverse of the already-sorted top-to-bottom order) so that insertions
    at higher indices don't shift lower-index targets.
    """
    start_idx, end_idx = page_range

    for new_elem in reversed(new_elems):
        new_t = new_elem.get("bbox", {}).get("t", 0)

        # Find the last existing element on this page with bbox.t >= new_t
        best_idx = None
        for i in range(end_idx, start_idx - 1, -1):
            ex_t = result[i].get("bbox", {}).get("t", 0)
            if ex_t >= new_t:
                best_idx = i
                break

        if best_idx is not None:
            # Insert after the found element
            result.insert(best_idx + 1, new_elem)
        else:
            # New element is above all existing elements — insert before first
            result.insert(start_idx, new_elem)

        # Expand range to account for the insertion
        end_idx += 1


def _find_page_boundary(
    page_ranges: Dict[int, List[int]],
    target_page: int,
    list_len: int,
) -> int:
    """Find the insertion index for a page with no existing elements.

    Returns the index after the last element of the highest page < target_page,
    or before the first element of the lowest page > target_page, or the end
    of the list if target_page is beyond all existing pages.
    """
    prev_end = -1
    for pn in sorted(page_ranges.keys()):
        if pn < target_page:
            prev_end = page_ranges[pn][1]
        elif pn > target_page:
            # Insert before this page's first element
            return page_ranges[pn][0] if prev_end < 0 else prev_end + 1
        # pn == target_page shouldn't happen (caller checks)

    # target_page is beyond all existing pages
    return prev_end + 1 if prev_end >= 0 else list_len
