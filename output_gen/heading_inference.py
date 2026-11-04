# SPDX-License-Identifier: AGPL-3.0-or-later
"""Infer heading levels (H1–H6) from source-PDF font sizes and numbering.

Docling tags every heading in many PDFs as ``level=1``, producing a flat run
of ``H1`` elements (52 in one dissertation tested). A flat heading
structure defeats screen-reader heading navigation. This module reassigns
levels using two signals:

1. **Font size** — cluster the heading font sizes pulled from the source PDF;
   the largest cluster becomes H1, the next H2, and so on (``_bucket_sizes``).
2. **Numbering depth** — a leading ``Chapter N`` or ``N`` is depth 1, ``N.N`` is
   depth 2, ``N.N.N`` is depth 3, etc. (``_numbering_depth``).

When a heading carries a numbering prefix, its depth is authoritative; otherwise
the font-size level is used. The pass only *re-levels* elements already tagged
as headings — it never adds or removes headings.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Font sizes within this many points of each other count as the same level.
CLUSTER_TOLERANCE_PT = 1.0

# PDF/UA allows headings H1–H6.
MAX_LEVEL = 6

HEADING_TAGS = {f"H{i}" for i in range(1, MAX_LEVEL + 1)}


def _bucket_sizes(sizes: List[float]) -> Dict[float, int]:
    """Map each font size to a heading level (largest size → level 1).

    Sizes within ``CLUSTER_TOLERANCE_PT`` of each other share a level. If more
    than ``MAX_LEVEL`` distinct clusters remain, the adjacent clusters with the
    smallest size gaps are merged until at most ``MAX_LEVEL`` levels exist.
    """
    vals = sorted({float(s) for s in sizes if s and s > 0}, reverse=True)
    if not vals:
        return {}

    # Cluster nearby sizes (descending). Each cluster's first element is its
    # representative (its maximum), so clusters stay within tolerance of their
    # representative rather than chaining arbitrarily far.
    clusters: List[List[float]] = []
    for s in vals:
        if clusters and abs(clusters[-1][0] - s) <= CLUSTER_TOLERANCE_PT:
            clusters[-1].append(s)
        else:
            clusters.append([s])

    # Merge adjacent clusters with the smallest gaps until within MAX_LEVEL.
    while len(clusters) > MAX_LEVEL:
        # Find the adjacent pair (i, i+1) with the smallest representative gap.
        best_i = 0
        best_gap = None
        for i in range(len(clusters) - 1):
            gap = clusters[i][0] - clusters[i + 1][0]
            if best_gap is None or gap < best_gap:
                best_gap = gap
                best_i = i
        clusters[best_i].extend(clusters[best_i + 1])
        del clusters[best_i + 1]

    mapping: Dict[float, int] = {}
    for level, cluster in enumerate(clusters, start=1):
        for size in cluster:
            mapping[size] = level
    return mapping


# Leading section numbering: an optional structural word (Chapter/Section/…)
# followed by a dotted integer sequence, with an optional trailing dot.
_NUMBERING_RE = re.compile(
    r"^\s*(?:chapter|section|appendix|part)?\s*(\d+(?:\.\d+)*)\.?(?:\s|$)",
    re.IGNORECASE,
)


def _numbering_depth(text: str) -> Optional[int]:
    """Heading depth from leading numbering, or None when unnumbered.

    ``Chapter 1`` / ``1`` → 1, ``1.1`` → 2, ``1.2.3`` → 3 (clamped to
    ``MAX_LEVEL``). A leading word with no number (``Abstract``) or a trailing
    number (``References 54``) is not numbering and returns None.
    """
    if not text:
        return None
    m = _NUMBERING_RE.match(text)
    if not m:
        return None
    depth = len(m.group(1).split("."))
    return min(depth, MAX_LEVEL)


def _assign_levels(headings: List[dict]) -> List[Optional[int]]:
    """Assign a heading level to each heading dict (``size`` + ``text``).

    Numbering depth is authoritative when present; otherwise the font-size
    cluster level is used. Returns None for a heading with neither signal
    (unknown font size and no numbering) so the caller leaves it unchanged.
    """
    font_map = _bucket_sizes([h["size"] for h in headings if h.get("size")])

    levels: List[Optional[int]] = []
    for h in headings:
        depth = _numbering_depth(h.get("text") or "")
        if depth is not None:
            levels.append(depth)
            continue
        size = h.get("size")
        if size is not None and float(size) in font_map:
            levels.append(font_map[float(size)])
        else:
            levels.append(None)
    return levels


def _normalize_sequence(desired: List[Optional[int]]) -> List[int]:
    """Force a heading-level list into a PDF/UA 7.4.2-valid sequence.

    Rules: the first heading is H1, and no heading may go more than one level
    deeper than its predecessor (jumps back up to any shallower level are
    allowed). A None ("no opinion") becomes a sibling of the previous heading
    (H1 if it is first).
    """
    out: List[int] = []
    prev = 0
    for d in desired:
        if d is None:
            level = prev if prev >= 1 else 1
        else:
            level = min(d, prev + 1)
        level = max(level, 1)
        out.append(level)
        prev = level
    return out


def normalize_heading_levels(elements: List[dict]) -> int:
    """Force the final heading sequence into a PDF/UA 7.4.2-valid one.

    Runs over the merged, reading-ordered element list (after cover-page
    treatment + gap-recovery injection/reorder) so the levels the tagger emits
    into StructTreeRoot never skip (H1->H3). Headings are taken in list order
    (the order the tagger linearizes into the structure tree). Mutates ``tag``
    and ``level`` in place and returns the number of headings changed.

    Idempotent: a valid sequence is a fixed point of ``_normalize_sequence``.
    Unlike :func:`infer_heading_levels`, this needs no PDF — it only re-levels
    the existing ``Hn`` tags, so it is the last safety net before tagging
    regardless of which stage injected or reordered a heading.
    """
    headings = [e for e in elements if e.get("tag") in HEADING_TAGS]
    if len(headings) < 2:
        return 0
    fixed = _normalize_sequence([int(e["tag"][1]) for e in headings])
    changed = 0
    for elem, level in zip(headings, fixed):
        new_tag = f"H{level}"
        if elem.get("tag") != new_tag:
            elem["tag"] = new_tag
            elem["level"] = level
            changed += 1
    return changed


_HTML_HEADING_RE = re.compile(r"^h[1-6]$")


def close_html_heading_level_gaps(soup) -> int:
    """Renumber HTML headings so the levels present form a gap-free ladder.

    Mirrors ``HeadingHierarchyCheck``, which collects headings by level rather
    than by document position, so it reports skipped-heading-level when a level
    between two present levels is absent anywhere in the document. Retagging one
    heading to h1 can empty a rung: an h2 promoted out of an h2/h3 document
    leaves {h1, h3}.

    The remap is anchored at the shallowest level present rather than at 1, so a
    document that was never promoted keeps its h2s and can never acquire an h1
    by renaming — promotion stays the only thing that creates one.

    Returns the number of headings changed. Idempotent.
    """
    headings = soup.find_all(_HTML_HEADING_RE)
    if len(headings) < 2:
        return 0

    remap: Dict[int, int] = {}
    prev: Optional[int] = None
    for level in sorted({int(h.name[1]) for h in headings}):
        prev = level if prev is None else min(level, prev + 1)
        remap[level] = prev

    changed = 0
    for h in headings:
        current = int(h.name[1])
        if remap[current] != current:
            h.name = f"h{remap[current]}"
            changed += 1
    return changed


def infer_heading_levels(elements: List[dict], pdf_path: str) -> int:
    """Re-level heading elements in place. Returns the number changed.

    No-ops (returns 0, leaves ``elements`` untouched) when there are fewer than
    two headings or the source PDF cannot be opened.
    """
    headings = [e for e in elements if e.get("tag") in HEADING_TAGS]
    if len(headings) < 2:
        return 0

    try:
        import fitz
        doc = fitz.open(pdf_path)
    except Exception as e:
        logger.debug("Heading inference skipped (cannot open PDF): %s", e)
        return 0

    try:
        page_cache: Dict[int, dict] = {}

        def _font_size(elem: dict) -> Optional[float]:
            page_no = elem.get("page_no")
            bbox = elem.get("bbox")
            if not page_no or not bbox or not (1 <= page_no <= doc.page_count):
                return None
            page = doc[page_no - 1]
            if page_no not in page_cache:
                page_cache[page_no] = page.get_text("dict")
            height = page.rect.height
            # Element bbox is bottom-left origin; convert to fitz top-left.
            rect = fitz.Rect(bbox["l"], height - bbox["t"],
                             bbox["r"], height - bbox["b"])
            best: Optional[float] = None
            for block in page_cache[page_no].get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if fitz.Rect(span["bbox"]).intersects(rect):
                            size = span.get("size")
                            if size and (best is None or size > best):
                                best = size
            return best

        sized = [{"size": _font_size(h), "text": h.get("text") or ""}
                 for h in headings]
        levels = _normalize_sequence(_assign_levels(sized))

        changed = 0
        for heading, level in zip(headings, levels):
            new_tag = f"H{level}"
            if heading.get("tag") != new_tag:
                heading["tag"] = new_tag
                heading["level"] = level
                changed += 1
        return changed
    finally:
        doc.close()
