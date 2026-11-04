# SPDX-License-Identifier: AGPL-3.0-or-later
"""Block-order normalization: fix gross Docling reading-order displacements.

Docling's ``iterate_items()`` sometimes emits a page's visually-top block LAST
in element order (cross-page ListGroup continuations, late SectionHeaders), and
Pass-4 orphan recovery appends synthetic P elements at the array end.  The Java
tagger builds the structure tree in array order, so these displacements become
real screen-reader reading-order defects.

HARD CONSTRAINT: a general
geometric re-sort is FORBIDDEN — Docling's order is CORRECT on multi-column
documents and a naive spatial sort corrupts them.  This pass therefore moves
only gross, unambiguous displacements:

* per page, only blocks that rise clearly ABOVE everything already read
  (running-max rule — never mid-page swaps, never downward moves);
* never on pages that look columnar (side-by-side elements), are rotated,
  scanned/overlay, or already ambiguous (per-page move cap);
* only movable block tags (P/H1-H6/L) — Figure/Table/Caption/TOC anchor the
  running max but are never moved;
* plus a doc-level pass returning end-appended cross-page stragglers (orphan
  blocks) to their page via ``insert_elements_preserving_order``.

The pass mutates the list in place (``elements[:] = ...``) so callers holding
the ``enriched["elements"]`` alias observe the change, and renumbers
``reading_order`` by index only when something actually moved.  Idempotent by
construction: a moved block becomes the page's running max on re-scan.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Set

from output_gen.reading_order import insert_elements_preserving_order

logger = logging.getLogger(__name__)

# Gross-move threshold (~2 text lines).  Real misplaced blocks rise far more
# (100pt+ observed); adjacency jitter (caption above figure) stays below.
RISE_MIN_PT = 24.0
# Near-tie band (~half a line): a displaced block whose top is within this of
# an anchor keeps Docling's order relative to that anchor on re-insertion.
TIE_SLACK_PT = 6.0
# Columnar guard: two elements vertically overlapping by more than this
# fraction of the shorter one, while horizontally disjoint, mean side-by-side
# layout — the whole page is skipped.
V_OVERLAP_MIN_FRAC = 0.5
# Minimum horizontal gap to call a pair "disjoint columns".  A heading brushing
# an indented list sits closer than this (gaps of ~3pt are typical).
H_GAP_MIN_PT = 6.0
# Per-page displaced cap: more than max(CAP_ABS, ceil(CAP_FRAC * n)) displaced
# blocks means the layout is ambiguous (poster/form) — skip the page entirely.
CAP_ABS = 2
CAP_FRAC = 0.20
# Doc-level cap for cross-page stragglers (Step A).
STRAGGLER_CAP = 5
# Only these block tags are ever moved.  Everything with a valid bbox anchors
# the running max regardless.
MOVABLE_TAGS = frozenset({"P", "H1", "H2", "H3", "H4", "H5", "H6", "L"})


def _valid_bbox(elem: Dict[str, Any]) -> bool:
    bbox = elem.get("bbox")
    if not isinstance(bbox, dict):
        return False
    try:
        return all(
            isinstance(bbox.get(k), (int, float)) for k in ("l", "b", "r", "t")
        ) and bbox["t"] > bbox["b"]
    except (KeyError, TypeError):
        return False


def _is_columnar_page(page_elems: List[Dict[str, Any]]) -> bool:
    """True when ANY element pair is side-by-side (vertical overlap while
    horizontally disjoint) — the strictest form of column detection, chosen
    because over-skipping only costs a missed fix (the safe direction)."""
    n = len(page_elems)
    for i in range(n):
        bi = page_elems[i]["bbox"]
        for j in range(i + 1, n):
            bj = page_elems[j]["bbox"]
            overlap = min(bi["t"], bj["t"]) - max(bi["b"], bj["b"])
            if overlap <= 0:
                continue
            shorter = min(bi["t"] - bi["b"], bj["t"] - bj["b"])
            if shorter <= 0 or overlap <= V_OVERLAP_MIN_FRAC * shorter:
                continue
            # Vertical overlap is substantial — are they horizontally disjoint?
            if (bi["r"] + H_GAP_MIN_PT <= bj["l"]
                    or bj["r"] + H_GAP_MIN_PT <= bi["l"]):
                return True
    return False


def _find_displaced(page_elems: List[Dict[str, Any]]) -> List[int]:
    """Indices (into page_elems) of blocks that rise clearly above everything
    already read — the running-max rule.  Displaced blocks do not update the
    running max; every element anchors it; only MOVABLE_TAGS are candidates."""
    displaced: List[int] = []
    runmax = -math.inf
    first_top: Optional[float] = None
    for i, elem in enumerate(page_elems):
        bbox = elem["bbox"]
        top, bottom = bbox["t"], bbox["b"]
        if first_top is None:
            first_top = top
            runmax = top
            continue
        is_candidate = elem.get("tag") in MOVABLE_TAGS
        gross_rise = top >= runmax + RISE_MIN_PT
        entirely_above_first = top > runmax and bottom >= first_top
        if is_candidate and (gross_rise or entirely_above_first):
            displaced.append(i)
            continue  # displaced blocks don't anchor the running max
        runmax = max(runmax, top)
    return displaced


def _reinsert(page_elems: List[Dict[str, Any]], displaced_idx: List[int]) -> None:
    """Move displaced blocks to their geometric slot within the page sequence.

    Processed by descending top so the highest block lands first.  Each block
    is inserted before the first remaining element whose top is more than
    TIE_SLACK_PT below it (near-ties keep Docling's order); if every element
    is at-or-above, it goes to the page end; if all are below, to the front.
    """
    moving = [page_elems[i] for i in displaced_idx]
    moving_ids = {id(e) for e in moving}
    remaining = [e for e in page_elems if id(e) not in moving_ids]
    for elem in sorted(moving, key=lambda e: -e["bbox"]["t"]):
        top = elem["bbox"]["t"]
        insert_at = len(remaining)
        for k, other in enumerate(remaining):
            if other["bbox"]["t"] < top - TIE_SLACK_PT:
                insert_at = k
                break
        remaining.insert(insert_at, elem)
    page_elems[:] = remaining


def normalize_block_order(
    elements: List[Dict[str, Any]],
    skip_pages: Optional[Set[int]] = None,
) -> Dict[str, Any]:
    """Normalize gross block-order displacements in a Docling element list.

    Mutates ``elements`` in place (``elements[:] = ...``) and renumbers
    ``reading_order`` by index iff anything moved.  Returns a stats dict for
    ``matching_summary`` — see the module docstring for the algorithm and its
    guard rails.

    Args:
        elements: The enriched/docling element list (flat; nested containers
            like L carry their LI children inside the element dict).
        skip_pages: 1-based page numbers to leave untouched (overlay,
            invisible-only, rotated).
    """
    skip_pages = skip_pages or set()
    stats: Dict[str, Any] = {
        "moves": 0,
        "cross_page_stragglers": 0,
        "headings_moved": 0,
        "pages_changed": [],
        "pages_skipped": {
            "skip_list": 0, "columnar": 0, "few_elements": 0,
            "invalid_bbox": 0, "capped": 0, "step_a_aborted": 0,
        },
        "moved": [],
    }

    # ── Step A: return cross-page stragglers to their page ───────────────────
    stragglers: List[Dict[str, Any]] = []
    max_page = -math.inf
    abort_step_a = False
    for elem in elements:
        page = elem.get("page_no")
        if not isinstance(page, int):
            abort_step_a = True
            break
        if (page < max_page and elem.get("tag") in MOVABLE_TAGS
                and _valid_bbox(elem) and page not in skip_pages):
            stragglers.append(elem)
        else:
            max_page = max(max_page, page)
    if not abort_step_a and 0 < len(stragglers) <= STRAGGLER_CAP:
        straggler_ids = {id(e) for e in stragglers}
        remaining = [e for e in elements if id(e) not in straggler_ids]
        # insert_elements_preserving_order assumes each page's elements are
        # contiguous; verify remaining is page-monotonic or abort (fail-safe).
        rem_pages = [int(e["page_no"]) for e in remaining]  # all ints: verified above
        if all(rem_pages[i] <= rem_pages[i + 1] for i in range(len(rem_pages) - 1)):
            elements[:] = insert_elements_preserving_order(remaining, stragglers)
            stats["cross_page_stragglers"] = len(stragglers)
        else:
            stats["pages_skipped"]["step_a_aborted"] = 1
    elif len(stragglers) > STRAGGLER_CAP:
        stats["pages_skipped"]["step_a_aborted"] = 1

    # ── Step B: per-page running-max normalization ───────────────────────────
    # Group indices by page; each page's elements are handled as the array
    # subsequence at those indices, so other pages' positions never change.
    page_index: Dict[int, List[int]] = {}
    for i, elem in enumerate(elements):
        page = elem.get("page_no")
        if isinstance(page, int):
            page_index.setdefault(page, []).append(i)

    for page in sorted(page_index):
        idxs = page_index[page]
        if page in skip_pages:
            stats["pages_skipped"]["skip_list"] += 1
            continue
        page_elems = [elements[i] for i in idxs]
        if len(page_elems) < 3:
            stats["pages_skipped"]["few_elements"] += 1
            continue
        if not all(_valid_bbox(e) for e in page_elems):
            stats["pages_skipped"]["invalid_bbox"] += 1
            continue
        if _is_columnar_page(page_elems):
            stats["pages_skipped"]["columnar"] += 1
            continue
        displaced = _find_displaced(page_elems)
        if not displaced:
            continue
        cap = max(CAP_ABS, math.ceil(CAP_FRAC * len(page_elems)))
        if len(displaced) > cap:
            stats["pages_skipped"]["capped"] += 1
            continue

        first_top = page_elems[0]["bbox"]["t"]
        for di in displaced:
            elem = page_elems[di]
            stats["moves"] += 1
            if elem.get("tag", "").startswith("H"):
                stats["headings_moved"] += 1
            if len(stats["moved"]) < 20:
                stats["moved"].append({
                    "id": elem.get("id"), "tag": elem.get("tag"), "page": page,
                    "rise": round(elem["bbox"]["t"] - first_top, 1),
                })
        _reinsert(page_elems, displaced)
        for slot, elem in zip(idxs, page_elems):
            elements[slot] = elem
        stats["pages_changed"].append(page)

    # ── Renumber only when something actually moved ──────────────────────────
    if stats["moves"] or stats["cross_page_stragglers"]:
        for i, elem in enumerate(elements):
            elem["reading_order"] = i

    return stats
