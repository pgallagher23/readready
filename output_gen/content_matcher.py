#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 2: Element-to-Content Matching.

Matches Docling structure elements (with bounding boxes) to content stream
text runs (from Phase 1 analysis), producing an enriched structure JSON
that tells the Phase 3 tagger which content stream operators belong to
each element.

Usage:
    from output_gen.content_matcher import match_elements_to_content
    enriched = match_elements_to_content(docling_structure, cs_analysis)

The enriched structure adds `content_run_indices` to each element,
listing which text runs from the content stream belong to it.
"""

from utils.raster import safe_dpi
import copy
import json
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Parent-table grace for cell-bbox expansion. The cell's tolerance window
# is clamped to the parent Table's bbox ± this constant. Was 15pt; a 15pt
# grace let runs from paragraphs immediately above/below the table bleed
# into cell tolerance windows. 3pt is enough to
# absorb Docling bbox rounding without crossing element boundaries.
TABLE_BBOX_GRACE_PT = 3.0


def _transform_run_coords(
    run_cx: float, run_cy: float,
    page_no: int, page_rotations: Dict,
) -> Tuple[float, float]:
    """Transform content stream coords to visual space for rotated pages.

    Docling reports bboxes in post-rotation visual space, but content stream
    coordinates are pre-rotation.  This applies the same rotation as the page's
    /Rotate value to convert run centroids into visual space.
    """
    if page_no in page_rotations:
        rot, raw_w, raw_h = page_rotations[page_no]
        if rot == 90:
            return run_cy, raw_w - run_cx
        elif rot == 180:
            return raw_w - run_cx, raw_h - run_cy
        elif rot == 270:
            return raw_h - run_cy, run_cx
    return run_cx, run_cy


def _match_pass(
    elements: List[Dict],
    page_runs: Dict[int, List[Dict]],
    page_rotations: Dict,
    tolerance: float,
    assigned: Dict[Tuple[int, int], str],
    locked: set,
    elem_by_id: Dict[str, Dict],
    run_lookup: Optional[Dict[Tuple[int, int], Dict]] = None,
    figure_peer_ids: Optional[set] = None,
    whitespace_only_pages: Optional[set] = None,
) -> None:
    """Run one matching pass: assign text runs to elements within tolerance.

    Args:
        elements: Structure elements (modified in place).
        page_runs: Page number -> list of text runs.
        page_rotations: Page number -> (rotation, raw_width, raw_height).
        tolerance: Bbox expansion in points.
        assigned: (page, run_index) -> element_id mapping (modified in place).
        locked: Set of (page, run_index) keys that cannot be reassigned.
        elem_by_id: Element id -> element dict for O(1) lookup.
        run_lookup: (page, run_index) -> run dict for O(1) text retrieval.
        figure_peer_ids: Pre-computed set of id(elem) for Figures with text peers.
        whitespace_only_pages: Pages where all visible runs are whitespace — skip matching.
    """
    for elem in elements:
        # Skip elements on whitespace-only pages — these get overlay MCRs
        if whitespace_only_pages and elem.get("page_no") in whitespace_only_pages:
            continue
        # Skip Figures that overlap with another non-Figure element on
        # the same page.  When a Figure and a Table/P/H share the same
        # bbox, letting the Figure claim text runs starves the semantic
        # element.  Figures that are the *only* element covering a page
        # region are still allowed to match (e.g., infographic PDFs).
        if elem.get("tag") == "Figure" and (
                id(elem) in figure_peer_ids if figure_peer_ids is not None
                else _figure_has_text_peer(elem, elements)):
            if "content_run_indices" not in elem:
                elem["content_run_indices"] = []
                elem["content_run_count"] = 0
                elem["matched_text_preview"] = ""
            continue

        page_no = elem.get("page_no")
        bbox = elem.get("bbox")
        if page_no is None or bbox is None:
            if "content_run_indices" not in elem:
                elem["content_run_indices"] = []
                elem["content_run_count"] = 0
                elem["matched_text_preview"] = ""
            continue

        runs = page_runs.get(page_no, [])
        matched_indices = []
        matched_texts = []

        el = bbox["l"] - tolerance
        eb = bbox["b"] - tolerance
        er = bbox["r"] + tolerance
        et = bbox["t"] + tolerance

        for run in runs:
            if not run.get("text", "").strip():
                continue
            run_idx = run["index"]
            key = (page_no, run_idx)

            # Skip runs locked by a previous pass
            if key in locked:
                continue

            # Text run centroid transformed to visual space
            run_cx = (run["x"] + run["end_x"]) / 2
            run_cy = run["y"]  # baseline y is the primary coordinate
            run_cx, run_cy = _transform_run_coords(
                run_cx, run_cy, page_no, page_rotations)

            # Determine effective centroid for containment check.
            # Primary: whole-run centroid.
            # Fallback: interpolated position for wide runs.
            effective_cx = None
            if el <= run_cx <= er and eb <= run_cy <= et:
                effective_cx = run_cx
            elif run.get("width", 0) > 100 and eb <= run_cy <= et:
                interp_cx = _interpolate_substring_position(
                    elem.get("text", ""), run)
                if interp_cx is not None and el <= interp_cx <= er:
                    effective_cx = interp_cx

            if effective_cx is None:
                continue

            # Check if already assigned to another element
            if key in assigned:
                existing_elem_id = assigned[key]
                existing_elem = elem_by_id.get(existing_elem_id)
                if existing_elem:
                    # Never steal from a Table with cells — cell
                    # sub-matching needs the parent's runs to
                    # redistribute to TD/TH.
                    if (existing_elem.get("tag") == "Table"
                            and existing_elem.get("cells")):
                        continue

                    # Conflict resolution via scoring: inside-flag,
                    # semantic priority, text similarity, then bbox area.
                    existing_score = _conflict_score(
                        effective_cx, run_cy,
                        existing_elem.get("bbox", {}),
                        existing_elem.get("tag", ""),
                        existing_elem.get("text", ""),
                        run.get("text", ""))
                    current_score = _conflict_score(
                        effective_cx, run_cy, bbox,
                        elem.get("tag", ""),
                        elem.get("text", ""),
                        run.get("text", ""))
                    if current_score <= existing_score:
                        continue  # existing wins or ties
                    # Steal from existing (current scores higher)
                    _remove_run_from_element(
                        existing_elem, page_no, run_idx
                    )

            matched_indices.append((*key, effective_cx, run_cy))
            matched_texts.append(run.get("text", ""))
            assigned[key] = elem.get("id", "?")

        # Merge with any runs already matched in a previous pass
        existing = elem.get("content_run_indices", [])
        existing_keys = {(r["page"], r["run_index"]) for r in existing}
        new_entries = []
        for p, r, cx, cy in matched_indices:
            if (p, r) in existing_keys:
                continue
            run_text = ""
            _r = run_lookup.get((p, r)) if run_lookup else None
            if _r is not None:
                run_text = _r.get("text", "") or ""
            else:
                for _r2 in page_runs.get(p, []):
                    if _r2["index"] == r:
                        run_text = _r2.get("text", "") or ""
                        break
            new_entries.append({
                "page": p,
                "run_index": r,
                "x": round(cx, 2),
                "y": round(cy, 2),
                "text": run_text,
            })
        elem["content_run_indices"] = existing + new_entries
        elem["content_run_count"] = len(elem["content_run_indices"])
        all_texts = []
        for ri in elem["content_run_indices"]:
            _r = run_lookup.get((ri["page"], ri["run_index"])) if run_lookup else None
            if _r:
                all_texts.append(_r.get("text", ""))
            else:
                for _r2 in page_runs.get(ri["page"], []):
                    if _r2["index"] == ri["run_index"]:
                        all_texts.append(_r2.get("text", ""))
                        break
        elem["matched_text_preview"] = " ".join(all_texts)[:120]


def _distribute_toc_runs(
    elements: List[Dict],
    page_runs: Dict[int, List[Dict]],
    assigned: Dict,
):
    """Distribute TOC-level content runs to individual TOCI children.

    Uses anchor-based text matching: finds each TOCI's "anchor run" by
    matching the first word(s) of the TOCI text against content runs,
    then builds y-bands from anchor positions.  Writes
    ``content_run_indices`` on each TOCI child and clears the parent
    TOC's runs.

    This replaces the Java tagger's spatial bbox matching, which fails
    because TOCI bboxes (PyMuPDF coords) and content run y-values
    (content-stream baseline coords) use different coordinate systems.
    """
    for elem in elements:
        if elem.get("tag") != "TOC" or not elem.get("children"):
            continue
        parent_runs = elem.get("content_run_indices", [])
        if not parent_runs:
            continue

        children = [c for c in elem["children"] if c.get("tag") == "TOCI"]
        if not children:
            continue

        parent_page = elem.get("page_no")
        toc_bbox = elem.get("bbox", {})

        # Build run lookup for TOC's page
        all_runs = page_runs.get(parent_page, [])
        run_by_idx = {r["index"]: r for r in all_runs}

        # Resolve parent run entries to full run dicts
        toc_run_indices = set()
        for ri in parent_runs:
            toc_run_indices.add(ri["run_index"])

        # --- Step 1: Find anchor run for each TOCI ---
        used_indices: set = set()
        anchors: List[Optional[float]] = []  # y-position per TOCI

        # Count how many TOCIs share the same first word — if >1,
        # we need two-word disambiguation for that word (not just "article").
        first_word_counts: Dict[str, int] = {}
        for child in children:
            words = child.get("text", "").lower().split()
            if words:
                first_word_counts[words[0]] = first_word_counts.get(words[0], 0) + 1

        for child in children:
            words = child.get("text", "").lower().split()
            if not words:
                anchors.append(None)
                continue

            first_word = words[0]
            needs_second_word = (
                len(words) > 1 and first_word_counts.get(first_word, 0) > 1
            )
            anchor_run = None

            for run in all_runs:
                idx = run["index"]
                if idx in used_indices or idx not in toc_run_indices:
                    continue
                run_text = run.get("text", "").strip().lower()
                # Match run text against the TOCI's first word.
                # Use exact equality to avoid "NO." false-matching "no",
                # but also accept run_text that starts with first_word
                # followed by a non-alpha character (e.g. "article." for
                # TOC entries that include trailing punctuation in one run).
                if run_text == first_word:
                    pass  # exact match
                elif (run_text.startswith(first_word)
                      and not run_text[len(first_word)].isalpha()):
                    # Trailing non-alpha (space/dot/comma) — word-boundary OK.
                    # But reject if the trailing char is "." (abbreviation),
                    # since that's a different token (e.g. "NO." ≠ "no").
                    if run_text[len(first_word)] == ".":
                        continue
                else:
                    continue
                # Two-word disambiguation when multiple TOCIs share first word
                if needs_second_word:
                    next_idx = idx + 1
                    next_run = run_by_idx.get(next_idx)
                    if next_run:
                        next_text = next_run.get("text", "").strip().lower()
                        if next_text != words[1]:
                            continue
                    else:
                        continue
                anchor_run = run
                break

            if anchor_run is not None:
                used_indices.add(anchor_run["index"])
                anchors.append(anchor_run["y"])
            else:
                anchors.append(None)

        # --- Step 2: Build y-bands from anchored positions ---
        # Interpolate positions for unanchored TOCIs from their neighbors.
        anchored = [(i, y) for i, y in enumerate(anchors) if y is not None]
        if not anchored:
            # No anchors found — leave runs on parent for Java fallback
            continue

        # Fill in unanchored positions by interpolation
        for i in range(len(anchors)):
            if anchors[i] is not None:
                continue
            # Find nearest anchored neighbor above (lower index = higher y)
            above_idx, above_y = None, None
            for ai, ay in anchored:
                if ai < i:
                    above_idx, above_y = ai, ay
                else:
                    break
            # Find nearest anchored neighbor below (higher index = lower y)
            below_idx, below_y = None, None
            for ai, ay in anchored:
                if ai > i:
                    below_idx, below_y = ai, ay
                    break
            if above_y is not None and below_y is not None:
                span = below_idx - above_idx
                frac = (i - above_idx) / span
                anchors[i] = above_y + frac * (below_y - above_y)
            elif above_y is not None:
                if len(anchored) >= 2:
                    avg_step = (anchored[0][1] - anchored[-1][1]) / (len(anchored) - 1)
                    anchors[i] = above_y - avg_step * (i - above_idx)
                else:
                    anchors[i] = above_y - 10.0 * (i - above_idx)
            elif below_y is not None:
                if len(anchored) >= 2:
                    avg_step = (anchored[0][1] - anchored[-1][1]) / (len(anchored) - 1)
                    anchors[i] = below_y + avg_step * (below_idx - i)
                else:
                    anchors[i] = below_y + 10.0 * (below_idx - i)

        # Rebuild anchored list with interpolated positions
        anchored = [(i, y) for i, y in enumerate(anchors) if y is not None]

        toc_top = toc_bbox.get("t", 9999)
        toc_bot = toc_bbox.get("b", 0)

        # Each TOCI owns from midpoint-above to midpoint-below
        bands: List[Tuple[int, float, float]] = []  # (child_idx, upper, lower)
        for j, (ci, ay) in enumerate(anchored):
            upper = toc_top if j == 0 else (anchored[j - 1][1] + ay) / 2.0
            lower = toc_bot if j == len(anchored) - 1 else (ay + anchored[j + 1][1]) / 2.0
            bands.append((ci, upper, lower))

        # --- Step 3: Assign runs to bands ---
        child_runs: Dict[int, List[Dict]] = {i: [] for i in range(len(children))}

        for ri in parent_runs:
            run = run_by_idx.get(ri["run_index"])
            if not run:
                continue
            ry = run["y"]
            assigned_ci = None
            for ci, upper, lower in bands:
                if ry <= upper and ry > lower:
                    assigned_ci = ci
                    break
            if assigned_ci is not None:
                child_runs[assigned_ci].append(ri)
            # Runs that don't match any band are dropped (Artifact)

        # --- Step 4: Write per-TOCI content_run_indices ---
        for i, child in enumerate(children):
            child["content_run_indices"] = child_runs.get(i, [])

        # Mark all distributed runs as assigned to suppress gap recovery.
        toc_id = elem.get("id", "TOC")
        for i, child in enumerate(children):
            for ri in child.get("content_run_indices", []):
                key = (ri["page"], ri["run_index"])
                assigned[key] = toc_id

        # Clear parent TOC runs (now distributed to children)
        elem["content_run_indices"] = []
        elem["content_run_count"] = 0

        anchored_count = len(anchored)
        total_distributed = sum(len(v) for v in child_runs.values())
        logger.info(
            "  TOC page %d: distributed %d runs to %d/%d TOCI children",
            parent_page, total_distributed, anchored_count, len(children),
        )


def _extract_ocr_word_positions(
    source_pdf_path: str,
    overlay_pages: List[int],
) -> Dict[str, List[Dict[str, Any]]]:
    """Extract word-level OCR positions for overlay (scanned) pages.

    Runs PyMuPDF's Tesseract OCR on each overlay page and returns per-page
    word positions in **Docling display space** (post-rotation visual
    coordinates, bottom-left origin) — the same coordinate system used by
    element bboxes.  The Java tagger converts these to content-stream
    coordinates via ``displayToContent()``.

    Returns:
        ``{"page_no": [{"t": text, "x": float, "y": float, "fs": float}, ...]}``
    """
    import fitz  # PyMuPDF

    doc = fitz.open(source_pdf_path)
    result: Dict[str, List[Dict[str, Any]]] = {}

    for page_no in overlay_pages:
        if page_no < 1 or page_no > doc.page_count:
            continue
        page = doc[page_no - 1]

        try:
            tp = page.get_textpage_ocr(
                language="eng", dpi=safe_dpi(page.rect.width, page.rect.height, 150), full=True)
        except Exception as exc:
            logger.warning("  OCR failed on overlay page %d: %s", page_no, exc)
            continue

        page_dict = page.get_text("dict", textpage=tp, sort=True)
        rotation = page.rotation % 360
        rect_h = page.rect.height  # de-rotated page height

        words: List[Dict[str, Any]] = []
        for blk in page_dict.get("blocks", []):
            if blk.get("type", 0) != 0:
                continue
            for line in blk.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if not text:
                        continue
                    sb = span["bbox"]  # (x0, y0, x1, y1) fitz TL coords
                    font_size = span.get("size", 11.0)

                    # Convert fitz de-rotated TL coords to Docling display
                    # BL coords.  The mapping depends on page /Rotate because
                    # fitz and Docling use different de-rotation conventions.
                    if rotation == 270:
                        # fitz axes are swapped relative to Docling display
                        dx = round(sb[1], 2)   # fitz y → display x
                        dy = round(sb[0], 2)   # fitz x → display y (BL)
                    elif rotation == 90:
                        dx = round(rect_h - sb[1], 2)
                        dy = round(page.rect.width - sb[0], 2)
                    elif rotation == 180:
                        dx = round(page.rect.width - sb[0], 2)
                        dy = round(sb[1], 2)
                    else:
                        # /Rotate 0 or absent: standard TL → BL flip
                        dx = round(sb[0], 2)
                        dy = round(rect_h - sb[3], 2)

                    words.append({
                        "t": text,
                        "x": dx,
                        "y": dy,
                        "fs": round(font_size, 1),
                    })

        if words:
            result[str(page_no)] = words

    doc.close()
    return result


def _is_whitespace_only_page(text_runs: List[Dict]) -> bool:
    """Return True if all visible text runs on a page are whitespace-only.

    Invisible text (render_mode=3) is excluded from the check since it's
    typically OCR overlay text that gets stripped by the tagger.
    """
    for run in text_runs:
        if run.get("render_mode") == 3:
            continue  # skip invisible text
        text = run.get("text", "")
        if text.strip():
            return False  # found visible non-whitespace text
    return True


# --- False-Figure split clustering ----------------------------------------
# Groups a reclassified figure's matched runs into visual text blocks:
# baselines -> lines, large x-gaps -> side-by-side fragments, vertically
# adjacent overlapping fragments -> blocks.  Pure geometry; no graph
# inference (flowchart arrow order is out of scope).

_SAME_LINE_TOL = 2.5      # pt: baselines closer than this are one line
_DEFAULT_LINE_PITCH = 14.0
_MAX_LINE_PITCH = 30.0    # pt: cap — 2 lines a box-gap apart must not
                          # inflate pitch and glue vertically distant boxes
_MIN_FRAGMENT_GAP = 18.0  # pt: floor for the intra-line split threshold
_BLOCK_MERGE_FACTOR = 1.8  # merge lines when gap < factor * pitch


def _run_end_x(r: Dict) -> float:
    """end_x with fallbacks: x+width, then x + 5pt/char, then x+20."""
    if r.get("end_x") is not None:
        return float(r["end_x"])
    if r.get("width") is not None:
        return float(r["x"]) + float(r["width"])
    return float(r["x"]) + 5.0 * max(1, int(r.get("char_count", 4)))


def _transform_run_for_display(
    r: Dict, page_no: int, page_rotations: Dict,
) -> Dict:
    """Shallow-copy a run with x/y/end_x mapped into Docling's display
    coordinate space, so split-clustering geometry lines up with element
    bboxes (which are always in post-rotation visual space).

    Fixes two independent defects in the raw content-stream fields:
      - page-level /Rotate: both endpoints are pushed through the same
        rotation transform used elsewhere in this module (_match_pass),
        instead of being read as if the page were unrotated.
      - reversed text-matrix runs (x > end_x, e.g. vertical axis labels
        drawn RTL): l/r are derived as min/max over BOTH transformed
        endpoints rather than assuming x is always the left edge.
    """
    raw_end_x = _run_end_x(r)
    x0, y0 = _transform_run_coords(
        float(r["x"]), float(r["y"]), page_no, page_rotations)
    x1, y1 = _transform_run_coords(
        raw_end_x, float(r["y"]), page_no, page_rotations)
    disp = dict(r)
    disp["x"] = min(x0, x1)
    disp["end_x"] = max(x0, x1)
    disp["y"] = (y0 + y1) / 2.0
    return disp


def _group_lines(runs: List[Dict]) -> List[List[Dict]]:
    """Group runs into baseline lines, top-to-bottom (descending y)."""
    lines: List[List[Dict]] = []
    for r in sorted(runs, key=lambda r: (-float(r["y"]), float(r["x"]))):
        if lines and abs(float(lines[-1][0]["y"]) - float(r["y"])) <= _SAME_LINE_TOL:
            lines[-1].append(r)
        else:
            lines.append([r])
    return lines


def _estimate_line_pitch(runs: List[Dict]) -> float:
    """Median gap between distinct consecutive baselines (fallback 14pt).

    Capped at _MAX_LINE_PITCH: with few lines the median can BE a
    box-separation gap (e.g. two boxes 200pt apart), and an uncapped
    pitch would merge vertically distant blocks.
    """
    ys = sorted({round(float(r["y"]), 1) for r in runs}, reverse=True)
    gaps = [ys[i] - ys[i + 1] for i in range(len(ys) - 1)
            if ys[i] - ys[i + 1] > _SAME_LINE_TOL]
    if not gaps:
        return _DEFAULT_LINE_PITCH
    gaps.sort()
    return min(gaps[len(gaps) // 2], _MAX_LINE_PITCH)


def _split_line_fragments(line: List[Dict]) -> List[List[Dict]]:
    """Split one baseline line at large horizontal gaps.

    The adaptive part of the threshold uses WORD-SCALE gaps only
    (<= _MIN_FRAGMENT_GAP): a line holding just two boxes has box
    separations as its only gaps, and a median over those would set the
    tolerance above the very gaps we need to split at.
    """
    frags: List[List[Dict]] = []
    ordered = sorted(line, key=lambda r: float(r["x"]))
    gaps = [float(b["x"]) - _run_end_x(a)
            for a, b in zip(ordered, ordered[1:])]
    word_gaps = sorted(g for g in gaps if 0 < g <= _MIN_FRAGMENT_GAP)
    med = word_gaps[len(word_gaps) // 2] if word_gaps else 0.0
    gap_tol = max(_MIN_FRAGMENT_GAP, 2.0 * med)
    for i, r in enumerate(ordered):
        if i > 0 and gaps[i - 1] > gap_tol:
            frags.append([r])
        elif frags:
            frags[-1].append(r)
        else:
            frags.append([r])
    return frags


def _cluster_runs_into_blocks(runs: List[Dict]) -> List[List[Dict]]:
    """Cluster runs into visual blocks; see module comment above."""
    if not runs:
        return []
    pitch = _estimate_line_pitch(runs)
    lines = _group_lines(runs)
    # Fragments carry (line_idx, y, x_min, x_max, runs)
    frags = []
    for li, line in enumerate(lines):
        for f in _split_line_fragments(line):
            frags.append({
                "line": li, "y": float(f[0]["y"]),
                "x0": min(float(r["x"]) for r in f),
                "x1": max(_run_end_x(r) for r in f),
                "runs": f,
            })
    # Union-find over fragments: merge vertically adjacent overlapping ones.
    parent = list(range(len(frags)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)

    for i, a in enumerate(frags):
        for j, b in enumerate(frags):
            if b["line"] <= a["line"] or j == i:
                continue
            if b["line"] - a["line"] > 3:  # only look a few lines down
                continue
            v_gap = a["y"] - b["y"]
            h_overlap = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
            if 0 < v_gap <= _BLOCK_MERGE_FACTOR * pitch and h_overlap > 0:
                union(i, j)
    groups: Dict[int, List[Dict]] = {}
    for i, f in enumerate(frags):
        groups.setdefault(find(i), []).extend(f["runs"])
    # Order blocks top-to-bottom, then left-to-right.  Row-quantize the
    # vertical key (1.5x pitch buckets) so side-by-side boxes whose tops
    # differ by a point or two still read left-to-right (verified on the
    # Institutional flowchart: "Disclosure to the institution" x=207 t=860 must precede
    # "Disclosure to External Source" x=379 t=861).
    q = 1.5 * max(pitch, 1.0)
    blocks = list(groups.values())
    blocks.sort(key=lambda blk: (
        -round(max(float(r["y"]) for r in blk) / q),
        min(float(r["x"]) for r in blk),
    ))
    return blocks


def _block_text(block: List[Dict]) -> str:
    """Reading-order text: lines top-to-bottom, runs left-to-right."""
    parts = []
    for line in _group_lines(block):
        for r in sorted(line, key=lambda r: float(r["x"])):
            t = (r.get("text") or "").strip()
            if t:
                parts.append(t)
    return " ".join(parts)


def _block_bbox(block: List[Dict], line_pitch: float) -> Dict[str, float]:
    ys = [float(r["y"]) for r in block]
    return {
        "l": round(min(float(r["x"]) for r in block), 2),
        "b": round(min(ys) - 0.2 * line_pitch, 2),
        "r": round(max(_run_end_x(r) for r in block), 2),
        "t": round(max(ys) + 0.8 * line_pitch, 2),
    }


def match_elements_to_content(
    docling_structure: Dict[str, Any],
    cs_analysis: Dict[str, Any],
    tolerance: float = 55.0,
    tight_tolerance: float = 15.0,
    wide_tolerance: float = 55.0,
    source_pdf_path: Optional[str] = None,
    normalize_block_order: bool = True,
    split_reclassified: bool = True,
) -> Dict[str, Any]:
    """Match Docling elements to content stream text runs by bbox overlap.

    Uses a two-pass strategy:
      Pass 1 (tight tolerance): High-confidence matches locked against stealing.
      Pass 2 (wide tolerance): Sweep remaining unmatched runs.

    Args:
        docling_structure: Docling structure JSON (with elements[].bbox).
        cs_analysis: Content stream analysis JSON from Phase 1.
        tolerance: Legacy parameter (unused when tight/wide are provided).
        tight_tolerance: Pass 1 bbox expansion in points (default 15).
        wide_tolerance: Pass 2 bbox expansion in points (default 55).
        split_reclassified: Split text-only false-Figures into per-block
            P elements instead of one Div (config
            output.split_reclassified_figures).  Default True.

    Returns:
        Enriched copy of docling_structure with added fields per element:
        - content_run_indices: list of (page, run_index) tuples
        - content_run_count: number of matched runs
        - matched_text_preview: first 80 chars of matched content
        Also adds `unmatched_runs` at the top level for artifact candidates.
    """
    enriched = copy.deepcopy(docling_structure)
    elements = enriched.get("elements", [])

    # Build page-indexed lookup of text runs and rotation info
    page_runs = {}  # page_no -> list of runs
    page_rotations = {}  # page_no -> (rotation, raw_width, raw_height)
    invisible_only_pages = set()
    for pa in cs_analysis.get("page_analyses", []):
        page_num = pa["page"]
        runs = pa.get("text_runs", [])
        rot = pa.get("rotation", 0)
        if rot:
            page_rotations[page_num] = (rot, pa["width"], pa["height"])
        # Filter out invisible text (render_mode 3) — these are OCR overlays
        # or the current invisible MCR text we want to replace
        visible_runs = [r for r in runs if r.get("render_mode", 0) != 3]

        if visible_runs:
            page_runs[page_num] = visible_runs
        elif runs:
            # All text on this page is invisible (render_mode 3).
            # This happens on pages with scanned content + OCR text behind
            # images (e.g. code screenshots, image-rendered PDFs).
            # Fall back to using the invisible runs — they're the only
            # extractable text and better than leaving the page untagged.
            page_runs[page_num] = runs
            invisible_only_pages.add(page_num)
        else:
            page_runs[page_num] = []

    if invisible_only_pages:
        logger.info(
            "  %d page(s) have only invisible text (render_mode=3), "
            "using as fallback: %s",
            len(invisible_only_pages),
            sorted(invisible_only_pages)[:20],
        )

    if page_rotations:
        logger.info(
            "  %d page(s) have /Rotate, transforming coordinates: %s",
            len(page_rotations),
            {p: r[0] for p in sorted(page_rotations) for r in [page_rotations[p]]},
        )

    # Log CropBox-normalized pages (coordinates already adjusted by
    # the Java analyzer — no further transforms needed here)
    crop_pages = {
        pa["page"]: (pa.get("crop_offset_x", 0), pa.get("crop_offset_y", 0))
        for pa in cs_analysis.get("page_analyses", [])
        if pa.get("has_crop_box")
    }
    if crop_pages:
        logger.info(
            "  %d page(s) have CropBox (coords pre-normalized by analyzer): %s",
            len(crop_pages),
            {p: crop_pages[p] for p in sorted(crop_pages)},
        )
        # Log out-of-bounds run counts for diagnostics
        for pa in cs_analysis.get("page_analyses", []):
            oob = pa.get("out_of_bounds_runs", 0)
            if oob > 0:
                logger.info(
                    "    Page %d: %d/%d runs outside CropBox (will be Artifact)",
                    pa["page"], oob, pa.get("text_run_count", 0),
                )

    # O(1) run lookup by (page, run_index) — replaces inner linear scans
    run_lookup: Dict[Tuple[int, int], Dict] = {}
    for _pg, _runs in page_runs.items():
        for _r in _runs:
            run_lookup[(_pg, _r["index"])] = _r

    # --- Two-pass matching ---
    assigned = {}   # (page, run_index) -> element_id
    locked = set()  # runs from Pass 1 that can't be stolen

    # O(1) element lookup for conflict resolution
    elem_by_id = {e.get("id"): e for e in elements if e.get("id")}

    # Pre-compute which Figures have text peers (avoids O(n²) repeated scans)
    figure_peer_ids = {
        id(e) for e in elements
        if e.get("tag") == "Figure" and _find_text_peer(e, elements) is not None
    }

    # Pages where all visible text runs are whitespace (spaces, empty strings).
    # These are typically scanned pages where an Image XObject provides the
    # visual content and Docling OCR extracts the semantic text.
    # On these pages, suppress image-to-element matching so the Java tagger
    # uses the overlay path (Artifact-wrap image + overlay MCRs for text).
    whitespace_only_pages: set = set()
    for pa in cs_analysis.get("page_analyses", []):
        page_no = pa["page"]
        runs = pa.get("text_runs", [])
        images = pa.get("images", [])
        if not images:
            continue  # no images to suppress
        if not _is_whitespace_only_page(runs):
            continue  # has real text — normal matching
        # Check if any Docling element on this page has OCR text
        has_ocr_text = any(
            e.get("page_no") == page_no
            and (e.get("text", "").strip() or (e.get("alt_text") or "").strip())
            and e.get("tag") != "Figure"
            for e in elements
        )
        if has_ocr_text:
            whitespace_only_pages.add(page_no)
            logger.info(
                "  Page %d: whitespace-only text runs with OCR elements "
                "— suppressing image matching for overlay path",
                page_no,
            )

    # Pass 1: Tight tolerance — high-confidence matches
    _match_pass(elements, page_runs, page_rotations,
                tight_tolerance, assigned, locked, elem_by_id, run_lookup,
                figure_peer_ids, whitespace_only_pages)

    # Lock all Pass 1 assignments
    locked = set(assigned.keys())
    pass_1_count = len(locked)

    # Pass 2: Wide tolerance — sweep remaining unmatched runs
    _match_pass(elements, page_runs, page_rotations,
                wide_tolerance, assigned, locked, elem_by_id, run_lookup,
                figure_peer_ids, whitespace_only_pages)

    # Pass 3: Text-based recovery for unmatched elements
    pass_3_stats = _recovery_pass_3(
        elements, page_runs, page_rotations,
        assigned, locked, elem_by_id, wide_tolerance,
        figure_peer_ids, run_lookup, whitespace_only_pages,
    )
    if pass_3_stats.get("total_recovered", 0) > 0:
        logger.info(
            "  Pass 3 recovery: %d elements recovered "
            "(%d reclaimed, %d proximity)",
            pass_3_stats["total_recovered"],
            pass_3_stats["strategy_c_reclaimed"],
            pass_3_stats["strategy_b_proximity"],
        )

    # --- Cell-level matching for Table elements ---
    # For each Table with cells[], assign text runs to individual cells
    # so the tagger can create TR > TH/TD hierarchy.
    for elem in elements:
        if elem.get("tag") != "Table" or not elem.get("cells"):
            continue
        parent_page = elem.get("page_no")

        # --- Adaptive vertical tolerance (computed once per table) ---
        # Dense tables have row spacing of 4-8pt.  A fixed 55pt vertical
        # tolerance spans 2-3 rows in each direction, causing every cell
        # to claim runs from neighboring rows (91% duplicate assignments
        # measured on a 10-column 8pt-row table).
        #
        # Per-cell vertical tolerance = full gap to each adjacent row (floor
        # 5pt for bbox imprecision, cap at wide_tolerance).  Horizontal
        # expansion stays at wide_tolerance (columns are spaced much
        # further apart).  cell_claimed below enforces single ownership.
        row_ranges: Dict[int, List[float]] = {}
        for rc in elem["cells"]:
            rcb = rc.get("bbox")
            if not rcb:
                continue
            rr = rc["row"]
            if rr not in row_ranges:
                row_ranges[rr] = [rcb["b"], rcb["t"]]
            else:
                row_ranges[rr][0] = min(row_ranges[rr][0], rcb["b"])
                row_ranges[rr][1] = max(row_ranges[rr][1], rcb["t"])

        sorted_rows = sorted(row_ranges.keys())
        # Map row index → gap to adjacent rows for per-cell tolerance.
        # Gaps may be negative when rowspan cells overlap vertically.
        row_gap_above: Dict[int, float] = {}
        row_gap_below: Dict[int, float] = {}
        for ri_idx, ri_r in enumerate(sorted_rows):
            if ri_idx > 0:
                ri_prev = sorted_rows[ri_idx - 1]
                row_gap_above[ri_r] = row_ranges[ri_prev][0] - row_ranges[ri_r][1]
            if ri_idx < len(sorted_rows) - 1:
                ri_next = sorted_rows[ri_idx + 1]
                row_gap_below[ri_r] = row_ranges[ri_r][0] - row_ranges[ri_next][1]

        # cell_claimed maps run key → (cell_runs_list, inside_actual, dist,
        # claimant_row).  Prevents the same run landing in multiple cells when
        # rows are clearly separated.  For overlapping rows (broken Docling
        # segmentation) duplicates are allowed — the Java tagger uses
        # /ActualText to override, so they are harmless.
        cell_claimed: Dict[Tuple[int, int], Tuple[list, bool, float, int]] = {}

        for cell in elem["cells"]:
            cb = cell.get("bbox")
            if not cb:
                cell["content_run_indices"] = []
                continue

            # Table cells are always on the same page as their parent
            # Table — Docling doesn't produce cross-page cells.
            cell_page = cell.get("page_no", parent_page)

            runs = page_runs.get(cell_page, [])
            if not runs:
                cell["content_run_indices"] = []
                continue

            # Clamp cell bbox expansion to parent Table bbox + TABLE_BBOX_GRACE_PT.
            # Without clamping, cell tolerance reaches far outside the Table,
            # stealing runs from neighboring P/H1 elements (validated: 6.43%
            # of cell-run assignments were outside parent bbox).
            # Per-cell vertical tolerance: half the gap to each adjacent row,
            # floored at 10% of cell height (minimum spatial buffer for
            # bbox imprecision), capped at wide_tolerance.
            # Horizontal tolerance stays at wide_tolerance (columns are
            # much further apart than rows).
            cell_row = cell.get("row", -1)
            # Per-cell vertical tolerance: full gap to each adjacent row,
            # floored at 5pt to absorb Docling bbox imprecision.  Negative
            # gaps (overlapping rowspan cells) fall back to the floor.
            vert_floor = 5.0
            gap_above = row_gap_above.get(cell_row, wide_tolerance)
            gap_below = row_gap_below.get(cell_row, wide_tolerance)
            vert_up = min(wide_tolerance, max(vert_floor, gap_above if gap_above > 0 else vert_floor))
            vert_dn = min(wide_tolerance, max(vert_floor, gap_below if gap_below > 0 else vert_floor))
            pb = elem.get("bbox", {})
            cl = max(cb["l"] - wide_tolerance, pb.get("l", -9999) - TABLE_BBOX_GRACE_PT)
            cbot = max(cb["b"] - vert_dn, pb.get("b", -9999) - TABLE_BBOX_GRACE_PT)
            cr = min(cb["r"] + wide_tolerance, pb.get("r", 9999) + TABLE_BBOX_GRACE_PT)
            ct = min(cb["t"] + vert_up, pb.get("t", 9999) + TABLE_BBOX_GRACE_PT)
            cell_runs = []

            table_id = elem.get("id", "?")
            for run in runs:
                if not run.get("text", "").strip():
                    continue
                run_idx = run["index"]
                key = (cell_page, run_idx)
                owner = assigned.get(key)
                run_cx = (run["x"] + run["end_x"]) / 2
                run_cy = run["y"]
                run_cx, run_cy = _transform_run_coords(
                    run_cx, run_cy, cell_page, page_rotations)

                # Is the run centroid inside this cell's *actual* bbox?
                inside_actual = (
                    cb["l"] <= run_cx <= cb["r"]
                    and cb["b"] <= run_cy <= cb["t"]
                )

                if owner is not None and owner != table_id:
                    # Run belongs to another element. Only steal if:
                    # 1. Run centroid is inside the actual cell bbox
                    #    (zero tolerance — highest confidence).
                    # 2. Owner is not another Table.
                    # 3. Run is NOT inside the owner's actual bbox —
                    #    if it is, the owner has a legitimate claim
                    #    and we shouldn't steal. We only reclaim runs
                    #    that the owner grabbed via tolerance expansion.
                    if not inside_actual:
                        continue  # outside actual cell, don't steal
                    owner_elem = elem_by_id.get(owner)
                    if owner_elem and owner_elem.get("tag") == "Table":
                        continue  # don't steal between tables
                    owner_bbox = owner_elem.get("bbox") if owner_elem else None
                    if owner_bbox and (owner_bbox["l"] <= run_cx <= owner_bbox["r"]
                                      and owner_bbox["b"] <= run_cy <= owner_bbox["t"]):
                        continue  # run is inside owner's actual bbox
                    if key in cell_claimed:
                        # Only block if the two competing cells are from
                        # rows that don't overlap.
                        prior_row = cell_claimed[key][3]
                        if cell_row != prior_row:
                            gap_between = row_gap_above.get(
                                max(cell_row, prior_row), 1)
                            if gap_between > 0:
                                continue  # rows clearly separated, already owned
                    cur_dist = _run_to_cell_dist(run_cx, run_cy, cb)
                    cell_runs.append({"page": cell_page, "run_index": run_idx})
                    if owner_elem:
                        _remove_run_from_element(owner_elem, cell_page, run_idx)
                    assigned[key] = table_id
                    cell_claimed[key] = (cell_runs, inside_actual, cur_dist, cell_row)
                elif owner is None or owner == table_id:
                    # Unassigned or already table's — use adaptive tolerance.
                    if cl <= run_cx <= cr and cbot <= run_cy <= ct:
                        cur_dist = _run_to_cell_dist(run_cx, run_cy, cb)

                        if key not in cell_claimed:
                            # Not yet claimed by any cell.
                            cell_runs.append({"page": cell_page, "run_index": run_idx})
                            assigned[key] = table_id
                            cell_claimed[key] = (cell_runs, inside_actual, cur_dist, cell_row)
                        else:
                            # Run claimed by a sibling cell.
                            prior_list, prior_actual, prior_dist, prior_row = cell_claimed[key]
                            # Rows overlap when their bboxes touch or intersect —
                            # allow duplicates (Java tagger uses /ActualText).
                            rows_overlap = (
                                cell_row != prior_row
                                and row_gap_above.get(max(cell_row, prior_row), 1) <= 0
                            )
                            if rows_overlap:
                                cell_runs.append({"page": cell_page, "run_index": run_idx})
                                assigned[key] = table_id
                            else:
                                # Separated rows: transfer ownership to stronger
                                # claimant — inside-actual bbox beats tolerance;
                                # otherwise nearest centroid wins.
                                if inside_actual and not prior_actual:
                                    take_over = True
                                elif not inside_actual and not prior_actual:
                                    take_over = cur_dist < prior_dist
                                else:
                                    take_over = False
                                if take_over:
                                    run_entry = {"page": cell_page, "run_index": run_idx}
                                    prior_list.pop(prior_list.index(run_entry))
                                    cell_runs.append(run_entry)
                                    cell_claimed[key] = (cell_runs, inside_actual, cur_dist, cell_row)

            cell["content_run_indices"] = cell_runs

        # Remove cell-assigned runs from parent Table element to prevent
        # the Java tagger from tagging the same operator twice.
        cell_run_keys = set()
        for cell in elem.get("cells", []):
            for ri in cell.get("content_run_indices", []):
                cell_run_keys.add((ri["page"], ri["run_index"]))
        if cell_run_keys:
            elem["content_run_indices"] = [
                ri for ri in elem.get("content_run_indices", [])
                if (ri["page"], ri["run_index"]) not in cell_run_keys
            ]
            elem["content_run_count"] = len(elem["content_run_indices"])

    # --- LI-level matching for List elements ---
    # For each L with children[].tag=="LI" and bbox, assign text runs
    # to individual list items for L > LI > LBody hierarchy.
    for elem in elements:
        if elem.get("tag") != "L" or not elem.get("children"):
            continue
        parent_page = elem.get("page_no")

        for child in elem["children"]:
            if child.get("tag") != "LI":
                continue
            cb = child.get("bbox")
            if not cb:
                child["content_run_indices"] = []
                continue

            # Use explicit page_no if set (from converter), otherwise
            # default to parent page. Don't infer via run overlap —
            # adjacent pages have identical coordinate systems and
            # the inference picks the wrong page 25%+ of the time.
            child_page = child.get("page_no", parent_page)

            runs = page_runs.get(child_page, [])
            if not runs:
                child["content_run_indices"] = []
                continue

            # Use tight tolerance for LI sub-matching — LI bboxes are
            # precise (8-9pt tall, 2-3pt gaps) and wide tolerance causes
            # massive run overlap between adjacent list items.
            _li_tol = 2.0
            # Clamp LI bbox expansion to parent L bbox + TABLE_BBOX_GRACE_PT,
            # but only for same-page children.  Cross-page children have
            # coordinates independent of the parent's (now page-clipped)
            # bbox and must not be clamped. The same rationale as the table-
            # cell clamp applies: runs from paragraphs immediately above/below
            # the list must not bleed into LI tolerance windows.
            if child_page == parent_page:
                pb = elem.get("bbox", {})
                cl = max(cb["l"] - _li_tol, pb.get("l", -9999) - TABLE_BBOX_GRACE_PT)
                cbot = max(cb["b"] - _li_tol, pb.get("b", -9999) - TABLE_BBOX_GRACE_PT)
                cr = min(cb["r"] + _li_tol, pb.get("r", 9999) + TABLE_BBOX_GRACE_PT)
                ct = min(cb["t"] + _li_tol, pb.get("t", 9999) + TABLE_BBOX_GRACE_PT)
            else:
                cl = cb["l"] - _li_tol
                cbot = cb["b"] - _li_tol
                cr = cb["r"] + _li_tol
                ct = cb["t"] + _li_tol
            li_runs = []

            list_id = elem.get("id", "?")
            for run in runs:
                if not run.get("text", "").strip():
                    continue
                run_idx = run["index"]
                key = (child_page, run_idx)
                owner = assigned.get(key)
                # Allow unassigned runs and steal from non-List elements.
                # LI bboxes are tight (2pt tolerance), so a match here
                # is highly specific — more than a nearby P or H element.
                if owner is not None and owner != list_id:
                    owner_elem = elem_by_id.get(owner)
                    if owner_elem and owner_elem.get("tag") == "L":
                        if child_page == parent_page:
                            continue  # same-page: don't steal between lists
                        # cross-page: allow — this LI legitimately belongs here
                run_cx = (run["x"] + run["end_x"]) / 2
                run_cy = run["y"]
                run_cx, run_cy = _transform_run_coords(
                    run_cx, run_cy, child_page, page_rotations)
                if cl <= run_cx <= cr and cbot <= run_cy <= ct:
                    li_runs.append({"page": child_page, "run_index": run_idx})
                    if owner is not None and owner != list_id:
                        owner_e = elem_by_id.get(owner)
                        if owner_e:
                            _remove_run_from_element(
                                owner_e, child_page, run_idx
                            )
                    assigned[key] = list_id

            child["content_run_indices"] = li_runs
            child["content_run_count"] = len(li_runs)

        # Remove child-assigned runs from parent L element to prevent
        # the Java tagger from tagging the same operator twice (once for
        # L, once for LI), which causes text duplication in screen readers.
        child_run_keys = set()
        for child in elem.get("children", []):
            for ri in child.get("content_run_indices", []):
                child_run_keys.add((ri["page"], ri["run_index"]))
        if child_run_keys:
            elem["content_run_indices"] = [
                ri for ri in elem.get("content_run_indices", [])
                if (ri["page"], ri["run_index"]) not in child_run_keys
            ]
            elem["content_run_count"] = len(elem["content_run_indices"])

    # --- Image matching for Figure elements ---
    # Match content stream images to Figure element bboxes.
    page_images = {}
    for pa in cs_analysis.get("page_analyses", []):
        imgs = pa.get("images", [])
        if imgs:
            page_images[pa["page"]] = imgs

    for elem in elements:
        if elem.get("tag") != "Figure":
            continue
        page_no = elem.get("page_no")
        bbox = elem.get("bbox")
        if page_no is None or bbox is None:
            continue
        if page_no in whitespace_only_pages:
            continue  # overlay path handles these scanned pages
        imgs = page_images.get(page_no, [])
        if not imgs:
            continue

        el = bbox["l"] - wide_tolerance
        eb = bbox["b"] - wide_tolerance
        er = bbox["r"] + wide_tolerance
        et = bbox["t"] + wide_tolerance

        best_img = None
        best_overlap = 0
        for img in imgs:
            # Image bbox from content stream analysis
            ix = img["x"]
            iy = img["y"]
            iw = img.get("width", 0)
            ih = img.get("height", 0)

            # Transform image coordinates if page is rotated
            if page_no in page_rotations:
                rot, raw_w, raw_h = page_rotations[page_no]
                if rot == 90:
                    ix, iy, iw, ih = iy, raw_w - ix - iw, ih, iw
                elif rot == 180:
                    ix, iy = raw_w - ix - iw, raw_h - iy - ih
                elif rot == 270:
                    ix, iy, iw, ih = raw_h - iy - ih, ix, ih, iw
            # Compute overlap area between Figure bbox and image bbox
            ox1 = max(el, ix)
            oy1 = max(eb, iy)
            ox2 = min(er, ix + iw)
            oy2 = min(et, iy + ih)
            if ox2 > ox1 and oy2 > oy1:
                overlap = (ox2 - ox1) * (oy2 - oy1)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_img = img

        if best_img is not None:
            elem["image_run_indices"] = [
                {"page": page_no, "image_index": best_img["index"]}
            ]

    # --- Form XObject fallback matching (veraPDF 7.20 t2) ---
    # For elements with content_run_count==0, check if their bbox overlaps
    # a Form XObject invocation.  This allows the tagger to wrap the Do
    # operator in a structure element instead of Artifact.
    page_xobjects: Dict[int, List[Dict]] = {}
    for pa in cs_analysis.get("page_analyses", []):
        xobjs = pa.get("xobject_invocations", [])
        if xobjs:
            page_xobjects[pa["page"]] = xobjs

    if page_xobjects:
        xobj_matched = 0
        xobj_tol = 15.0  # tight tolerance — XObject BBox may be imprecise
        for elem in elements:
            if elem.get("content_run_count", 0) > 0:
                continue  # already matched to text runs
            if elem.get("image_run_indices"):
                continue  # already matched to an image
            page_no = elem.get("page_no")
            bbox = elem.get("bbox")
            if page_no is None or bbox is None:
                continue
            xobjs = page_xobjects.get(page_no, [])
            if not xobjs:
                continue

            el = bbox["l"] - xobj_tol
            eb = bbox["b"] - xobj_tol
            er = bbox["r"] + xobj_tol
            et = bbox["t"] + xobj_tol

            matched_xobjs = []
            for xobj in xobjs:
                if "x" not in xobj or "y" not in xobj:
                    continue  # no bbox recorded
                xw = xobj.get("width", 0)
                xh = xobj.get("height", 0)
                # Check overlap between element bbox and XObject bbox
                ox1 = max(el, xobj["x"])
                oy1 = max(eb, xobj["y"])
                ox2 = min(er, xobj["x"] + xw)
                oy2 = min(et, xobj["y"] + xh)
                if ox2 > ox1 and oy2 > oy1:
                    matched_xobjs.append({
                        "page": page_no,
                        "xobject_index": xobj["index"],
                    })

            if matched_xobjs:
                elem["xobject_content_indices"] = matched_xobjs
                xobj_matched += 1

        if xobj_matched:
            logger.info(
                "  Matched %d element(s) to Form XObject invocations "
                "(7.20 t2 compliance)",
                xobj_matched,
            )

    # --- Figure-as-text reclassification ---
    # Docling classifies some text-heavy pages (flowcharts, forms,
    # checklists) as a single Figure with empty text.  Screen readers
    # read /Alt on Figures and skip MCR children, making the text
    # inaccessible.  Reclassify to Div so the tagger sets /ActualText.
    split_count = 0
    div_count = 0
    split_plans = []  # (index_in_elements, [replacement elements])
    for idx, elem in enumerate(elements):
        if elem.get("tag") != "Figure":
            continue
        text_runs = elem.get("content_run_count", 0)
        has_text = bool(elem.get("text", "").strip())
        has_images = bool(elem.get("image_run_indices"))
        # Risk 1 mitigation for editable SR preview: if a user has set
        # alt_text on a Figure (e.g. via the SR tab inline editor), keep
        # it as a Figure even when its text is empty. Otherwise the
        # reclassify would clobber the user's alt by switching the
        # element away from Figure.
        has_user_alt = bool((elem.get("alt_text") or "").strip())
        if not (text_runs > 5 and not has_text and not has_images
                and not has_user_alt):
            continue

        # Resolve full run records for the element's matched runs.
        full_runs = []
        for ri in elem.get("content_run_indices", []):
            _r = run_lookup.get((ri["page"], ri["run_index"]))
            if _r is None:
                for _r2 in page_runs.get(ri["page"], []):
                    if _r2["index"] == ri["run_index"]:
                        _r = _r2
                        break
            if _r is not None and (_r.get("text") or "").strip():
                full_runs.append((ri, _r))

        if split_reclassified and full_runs:
            # Cluster in DISPLAY space (matches element bboxes), not raw
            # content-stream space: transform both endpoints of every run
            # through the page's /Rotate, and let l/r fall out as
            # min/max over both endpoints (also fixes RTL text-matrix
            # runs whose x > end_x, e.g. vertical axis labels).
            elem_page_no = elem.get("page_no")
            disp_runs = [
                _transform_run_for_display(r, elem_page_no, page_rotations)
                for _, r in full_runs
            ]
            ref_by_id = {
                id(dr): ri
                for (ri, _r), dr in zip(full_runs, disp_runs)
            }
            pitch = _estimate_line_pitch(disp_runs)
            blocks = _cluster_runs_into_blocks(disp_runs)
            block_bboxes = [_block_bbox(block, pitch) for block in blocks]
            degenerate = next(
                (bb for bb in block_bboxes
                 if bb["r"] <= bb["l"] or bb["t"] <= bb["b"]),
                None,
            )
            if degenerate is not None:
                logger.info(
                    "Reclassified figure %s (page %s): split produced a "
                    "degenerate block bbox %s (r<=l or t<=b) — falling "
                    "back to legacy single-Div path",
                    elem.get("id"), elem_page_no, degenerate,
                )
            else:
                new_elems = []
                for j, (block, bbox) in enumerate(zip(blocks, block_bboxes)):
                    text = _block_text(block)
                    if not text.strip():
                        continue
                    refs = [ref_by_id[id(r)] for r in block]
                    new_elems.append({
                        "id": f"{elem['id']}_s{j}",
                        "type": "reclassified_figure_split",
                        "tag": "P",
                        "level": None,
                        "text": text,
                        "page_no": elem.get("page_no"),
                        "bbox": bbox,
                        "reading_order": elem.get("reading_order"),
                        "alt_text": None,
                        "image_ref": None,
                        "content_run_indices": refs,
                        "content_run_count": len(refs),
                        "matched_text_preview": text[:80],
                        "match_confidence": "high",
                        "match_method": "reclassified_split",
                    })
                if new_elems:
                    split_plans.append((idx, new_elems))
                    split_count += 1
                    continue

        # Legacy path (split disabled, no resolvable runs, or the split
        # geometry degenerated): one Div.
        elem["tag"] = "Div"
        elem["type"] = "reclassified_figure"
        elem["text"] = " ".join(
            (r.get("text") or "").strip() for _, r in full_runs) if full_runs else ""
        div_count += 1

    # Splice split replacements (reverse order keeps earlier indices valid),
    # then renumber reading_order so it stays strictly increasing.
    if split_plans:
        for idx, new_elems in reversed(split_plans):
            elements[idx:idx + 1] = new_elems
        for i, e in enumerate(elements):
            e["reading_order"] = i
        logger.info(
            "Split %d reclassified figure(s) into %d P block(s)",
            len(split_plans),
            sum(len(n) for _, n in split_plans))
    if div_count:
        logger.info(
            "Reclassified %d Figure(s) to Div (text-heavy, no image)",
            div_count)

    # --- TOCI-level matching for TOC elements ---
    # Distribute TOC-level content runs to individual TOCI children
    # using anchor-based text matching (coordinate-system-agnostic).
    _distribute_toc_runs(elements, page_runs, assigned)

    # Collect unmatched runs (artifact candidates)
    unmatched = []
    for page_no, runs in page_runs.items():
        for run in runs:
            key = (page_no, run["index"])
            if key not in assigned:
                unmatched.append({
                    "page": page_no,
                    "run_index": run["index"],
                    "text": run.get("text", "")[:80],
                    "x": run.get("x"),
                    "y": run.get("y"),
                    "font": run.get("font"),
                    "font_type": run.get("font_type"),
                })

    enriched["unmatched_runs"] = unmatched
    enriched["invisible_only_pages"] = sorted(invisible_only_pages)

    # Detect scanned/image-only pages that need overlay fallback.
    # These pages have 0 visible text runs but Docling found elements via OCR.
    page_elements_set: set = set()
    for elem in elements:
        pn = elem.get("page_no")
        if pn is not None:
            page_elements_set.add(pn)

    overlay_pages = []
    for pa in cs_analysis.get("page_analyses", []):
        page_num = pa["page"]
        visible_runs = [
            r for r in pa.get("text_runs", [])
            if r.get("render_mode", 0) != 3
        ]
        if len(visible_runs) == 0 and page_num in page_elements_set:
            overlay_pages.append(page_num)

    # Whitespace-only pages with OCR elements also need the overlay path.
    # (len(visible_runs)==0 above misses these — their runs exist but are
    # all spaces/empty, so they aren't caught as "0 text runs".)
    for wp in whitespace_only_pages:
        if wp not in overlay_pages:
            overlay_pages.append(wp)
            logger.info("  Page %d: added to overlay_pages (whitespace-only)", wp)

    enriched["overlay_pages"] = sorted(overlay_pages)
    if overlay_pages:
        logger.info(
            "  %d page(s) need overlay fallback (0 text runs, OCR elements): %s",
            len(overlay_pages),
            sorted(overlay_pages)[:20],
        )

    # --- Layer 2: Text validation — confidence scoring ---
    _validate_matches(elements, page_runs, run_lookup)

    # --- OCR noise filtering (2.6d) ---
    # Remove tiny unmatched P elements that are OCR artifacts from
    # background images — no content stream evidence they're real text.
    pre_filter_count = len(elements)
    elements[:] = [
        e for e in elements
        if not (
            e.get("tag") == "P"
            and len((e.get("text") or "").strip()) < 3
            and _bbox_area(e.get("bbox", {})) < 500
            and e.get("content_run_count", 0) == 0
        )
    ]
    ocr_noise_filtered = pre_filter_count - len(elements)
    if ocr_noise_filtered:
        logger.info(
            "  Filtered %d OCR noise P element(s) "
            "(short text, tiny bbox, no content match)",
            ocr_noise_filtered,
        )

    # --- 2.6e: Duplicate element deduplication ---
    # When Docling creates a Figure at the same bbox as a semantic element
    # (Table/P/H/L/TOC), remove the Figure.  The Figure is a visual
    # detection artifact with no text or alt_text to preserve.
    # Pre-pass: transfer alt_text from duplicate Figures to their peers
    for e in elements:
        if (e.get("tag") == "Figure"
                and e.get("content_run_count", 0) == 0
                and not (e.get("text") or "").strip()
                and e.get("alt_text")):
            if id(e) in figure_peer_ids:
                peer = _find_text_peer(e, elements)
                if peer and not peer.get("alt_text"):
                    peer["alt_text"] = e["alt_text"]
    pre_dedup = len(elements)
    elements[:] = [
        e for e in elements
        if not (
            e.get("tag") == "Figure"
            and e.get("content_run_count", 0) == 0
            and not (e.get("text") or "").strip()
            and not (e.get("alt_text") or "").strip()
            and id(e) in figure_peer_ids
        )
    ]
    dedup_removed = pre_dedup - len(elements)
    if dedup_removed:
        logger.info(
            "  Dedup: removed %d duplicate Figure element(s) "
            "(overlapping semantic peer, no text/alt)",
            dedup_removed,
        )

    total_runs = sum(len(runs) for runs in page_runs.values())
    summary = _build_summary(elements, unmatched, page_runs)
    summary["ocr_noise_filtered"] = ocr_noise_filtered
    summary["confidence_breakdown"] = {
        "high": sum(1 for e in elements if e.get("match_confidence") == "high"),
        "medium": sum(1 for e in elements if e.get("match_confidence") == "medium"),
        "low": sum(1 for e in elements if e.get("match_confidence") == "low"),
        "none": sum(1 for e in elements if e.get("match_confidence") == "none"),
    }
    summary["two_pass"] = {
        "tight_tolerance": tight_tolerance,
        "wide_tolerance": wide_tolerance,
        "pass_1_locked": pass_1_count,
        "pass_1_rate": round(pass_1_count / max(total_runs, 1) * 100, 1),
    }
    summary["pass_3"] = pass_3_stats

    # Page-level accounting (diagnostic)
    page_deficits = _page_level_accounting(elements, page_runs, assigned)

    # --- Pass 4: Recover orphaned run blocks on deficit pages ---
    orphan_stats = _recover_orphaned_run_blocks(
        elements, page_runs, assigned, page_deficits, run_lookup
    )
    if orphan_stats["recovered"]:
        # Recompute deficits after recovery
        page_deficits = _page_level_accounting(elements, page_runs, assigned)

    summary["page_deficits"] = page_deficits
    summary["pages_with_deficit"] = len(page_deficits)
    summary["pass_4_orphan_recovery"] = orphan_stats

    # --- Block-order normalization (kill-switch: output.normalize_block_order) ---
    # Fix gross Docling reading-order displacements (visually-top blocks emitted
    # last, end-appended Pass-4 orphans).  Runs after orphan recovery so those
    # synthetic elements are repositioned too; skips overlay/invisible/rotated
    # pages where bbox "t" is not a reliable visual top.  The Java tagger builds
    # the tag tree in array order, so this is what fixes the screen-reader order.
    if normalize_block_order:
        from output_gen.block_order import normalize_block_order as _nbo
        _skip = (set(overlay_pages) | set(invisible_only_pages)
                 | set(page_rotations))
        bo_stats = _nbo(elements, skip_pages=_skip)
        if bo_stats["moves"] or bo_stats["cross_page_stragglers"]:
            logger.info(
                "  Block-order normalization: %d move(s), %d straggler(s), "
                "pages %s",
                bo_stats["moves"], bo_stats["cross_page_stragglers"],
                bo_stats["pages_changed"][:10],
            )
        if bo_stats.get("headings_moved"):
            # A moved heading can re-introduce an H-level skip; production
            # re-normalizes later (pdf_tagger), but the manifest path never
            # does — keep the sequence PDF/UA 7.4.2-valid here for both.
            from output_gen.heading_inference import normalize_heading_levels
            normalize_heading_levels(elements)
        summary["block_order_normalization"] = bo_stats

    enriched["matching_summary"] = summary

    return enriched


def _bbox_area(bbox: Dict) -> float:
    """Calculate bbox area."""
    if not bbox:
        return float("inf")
    w = abs(bbox.get("r", 0) - bbox.get("l", 0))
    h = abs(bbox.get("t", 0) - bbox.get("b", 0))
    return w * h


def _run_to_cell_dist(run_cx: float, run_cy: float, cell_bbox: Dict) -> float:
    """Euclidean distance from a run centroid to a cell's bbox centroid.

    Used for tie-breaking when multiple cells claim the same run.
    """
    cx = (cell_bbox["l"] + cell_bbox["r"]) / 2
    cy = (cell_bbox["b"] + cell_bbox["t"]) / 2
    return math.sqrt((run_cx - cx) ** 2 + (run_cy - cy) ** 2)


def _edge_distance(px: float, py: float, bbox: Dict) -> float:
    """Distance from point (px, py) to nearest edge of bbox.

    Returns 0 if the point is inside the bbox.
    """
    if not bbox:
        return float("inf")
    dx = max(bbox.get("l", 0) - px, 0, px - bbox.get("r", 0))
    dy = max(bbox.get("b", 0) - py, 0, py - bbox.get("t", 0))
    return math.sqrt(dx * dx + dy * dy)


def _find_text_peer(
    fig_elem: Dict, elements: List[Dict]
) -> Optional[Dict]:
    """Find a non-Figure element that overlaps substantially with a Figure.

    Returns the first non-Figure element that covers >50% of the Figure's
    area, or None.  A small P overlapping a corner won't match a large
    Figure — the peer must cover most of the Figure's own area.
    """
    fig_bbox = fig_elem.get("bbox")
    fig_page = fig_elem.get("page_no")
    if not fig_bbox or fig_page is None:
        return None

    fig_area = _bbox_area(fig_bbox)
    if fig_area <= 0:
        return None

    for other in elements:
        if other is fig_elem or other.get("tag") == "Figure":
            continue
        if other.get("page_no") != fig_page:
            continue
        ob = other.get("bbox")
        if not ob:
            continue
        # Compute overlap area
        ol = max(fig_bbox["l"], ob["l"])
        ob_ = max(fig_bbox["b"], ob["b"])
        or_ = min(fig_bbox["r"], ob["r"])
        ot = min(fig_bbox["t"], ob["t"])
        if or_ > ol and ot > ob_:
            overlap = (or_ - ol) * (ot - ob_)
            # Peer must cover >50% of the Figure's area
            if overlap / fig_area > 0.5:
                return other
    return None


def _figure_has_text_peer(
    fig_elem: Dict, elements: List[Dict]
) -> bool:
    """Check if a Figure element overlaps substantially with a non-Figure.

    Returns True when a non-Figure element covers most of the Figure's
    area — meaning the Figure is a visual duplicate of a Table/P/H and
    should yield text runs.
    """
    return _find_text_peer(fig_elem, elements) is not None


# Semantic priority for conflict resolution.  Higher = more likely to
# be the correct owner of a text run.  All text-bearing elements get
# equal priority (3) — tiebreak by inside-flag then bbox area.  Only
# containers that yield to children (L, TOC) and Figure are lower.
_SEMANTIC_PRIORITY = {
    "Figure": 0, "Formula": 1,
    "L": 1, "TOC": 1,
    # Everything else (P, H1-H6, Span, Table, LI, etc.) defaults to 3
}


def _interpolate_substring_position(
    elem_text: str, run: Dict,
) -> Optional[float]:
    """Interpolate x-position of element text within a wide run.

    Uses linear character-based interpolation: each character occupies
    an equal fraction of the run's width.  For proportional fonts this
    introduces up to ~18pt error, well within the 55pt matching tolerance.

    Args:
        elem_text: The Docling element's text.
        run: Content stream text run dict with keys: text, x, width.

    Returns:
        Interpolated centroid x-coordinate, or None if the element text
        is not found in the run text.
    """
    if not elem_text or not run.get("text"):
        return None

    elem_clean = elem_text.lower().strip()
    run_text = run["text"]
    run_lower = run_text.lower()
    run_len = len(run_text)
    width = run.get("width", 0)

    if run_len == 0 or width <= 0:
        return None

    # Strategy 1: full element text as substring
    pos = run_lower.find(elem_clean)
    match_len = len(elem_clean)

    if pos < 0:
        # Strategy 2: first two significant words
        words = [w for w in elem_clean.split() if len(w) > 2][:2]
        if not words:
            return None
        for w in words:
            pos = run_lower.find(w)
            if pos >= 0:
                match_len = len(w)
                break
        if pos < 0:
            return None

    # Linear interpolation: char position → x coordinate
    interp_start = run["x"] + (pos / run_len) * width
    interp_end = run["x"] + ((pos + match_len) / run_len) * width
    return (interp_start + interp_end) / 2


def _conflict_score(
    run_cx: float,
    run_cy: float,
    elem_bbox: Dict,
    elem_tag: str,
    elem_text: str = "",
    run_text: str = "",
) -> Tuple[int, int, float, float]:
    """Score for conflict resolution.  Higher tuple wins.

    Returns (inside_flag, semantic_priority, text_similarity, -area):
      - inside_flag: 1 if run is inside actual bbox, 0 if tolerance zone
      - semantic_priority: tag-based priority
      - text_similarity: word overlap between run text and element text
      - -area: negative area (smaller bbox = higher score)
    """
    edge = _edge_distance(run_cx, run_cy, elem_bbox)
    inside = 1 if edge == 0 else 0
    priority = _SEMANTIC_PRIORITY.get(elem_tag, 3)
    area = _bbox_area(elem_bbox)

    run_words = _word_set(run_text)
    elem_words = _word_set(elem_text)
    similarity = (
        len(run_words & elem_words) / len(run_words)
        if run_words else 0.0
    )

    return (inside, priority, similarity, -area)




def _remove_run_from_element(
    elem: Dict, page: int, run_index: int
) -> None:
    """Remove a run assignment from an element."""
    runs = elem.get("content_run_indices", [])
    elem["content_run_indices"] = [
        r for r in runs
        if not (r["page"] == page and r["run_index"] == run_index)
    ]
    elem["content_run_count"] = len(elem["content_run_indices"])


def _build_summary(
    elements: List[Dict],
    unmatched: List[Dict],
    page_runs: Dict,
) -> Dict[str, Any]:
    """Build matching quality summary."""
    total_elements = len(elements)
    # Count elements matched to text runs OR Form XObject invocations.
    # XObject-matched elements have xobject_content_indices set and are
    # handled by the tagger (Do operator wrapped in structure elements).
    def _elem_has_child_runs(e: Dict) -> bool:
        """True if a Table or L element has runs in its cells/children."""
        tag = e.get("tag")
        if tag == "Table":
            return any(c.get("content_run_indices") for c in e.get("cells", []))
        if tag == "L":
            return any(ch.get("content_run_indices")
                       for ch in e.get("children", []))
        return False

    def _is_matched(e: Dict) -> bool:
        return bool(
            e.get("content_run_count", 0) > 0
            or e.get("xobject_content_indices")
            or e.get("image_run_indices")
            or _elem_has_child_runs(e)
        )

    def _is_textless_unmatched_figure(e: Dict) -> bool:
        """A Figure with no text and no matched content.

        Such elements carry accessibility via alt text + image_ref, not
        content-stream text runs, so counting them as misses in a *text-run
        attribution* rate is a category error — they are excluded from the
        EMR denominator.  Scoped strictly to Figures: empty P/Table/L/Formula
        elements stay counted, because their emptiness is a real signal.
        """
        return (
            e.get("tag") == "Figure"
            and not (e.get("text") or "").strip()
            and not _is_matched(e)
        )

    matched_elements = sum(1 for e in elements if _is_matched(e))
    xobject_matched = sum(
        1 for e in elements
        if e.get("content_run_count", 0) == 0
        and not e.get("image_run_indices")
        and not _elem_has_child_runs(e)
        and e.get("xobject_content_indices")
    )
    unmatched_elements = total_elements - matched_elements

    # Exclude text-less unmatched Figures from the rate denominator (they are
    # accessible via alt text, not text runs).  total_elements /
    # unmatched_elements are still reported in full for diagnostics.
    figure_only_excluded = sum(
        1 for e in elements if _is_textless_unmatched_figure(e)
    )
    rate_denominator = total_elements - figure_only_excluded

    total_runs = sum(len(runs) for runs in page_runs.values())
    matched_runs = total_runs - len(unmatched)

    return {
        "total_elements": total_elements,
        "matched_elements": matched_elements,
        "xobject_matched_elements": xobject_matched,
        "unmatched_elements": unmatched_elements,
        "figure_only_excluded": figure_only_excluded,
        "element_match_rate": round(
            matched_elements / max(rate_denominator, 1) * 100, 1
        ),
        "total_content_runs": total_runs,
        "matched_runs": matched_runs,
        "unmatched_runs": len(unmatched),
        "run_match_rate": round(
            matched_runs / max(total_runs, 1) * 100, 1
        ),
    }


def _normalize(text: str) -> str:
    """Normalize text for comparison: whitespace, case, common substitutions."""
    if not text:
        return ""
    t = text.strip().lower()
    t = " ".join(t.split())  # collapse whitespace
    t = t.replace("\u2014", "-")   # em-dash
    t = t.replace("\u2013", "-")   # en-dash
    t = t.replace("\u201c", '"')   # smart open quote
    t = t.replace("\u201d", '"')   # smart close quote
    t = t.replace("\u2019", "'")   # smart apostrophe
    t = t.replace("\ufb01", "fi")  # fi ligature
    t = t.replace("\ufb00", "ff")  # ff ligature
    t = t.replace("\ufb01", "fi")  # fi ligature (duplicate-safe)
    t = t.replace("\ufb02", "fl")  # fl ligature (duplicate-safe)
    t = t.replace("\ufb03", "ffi") # ffi ligature
    t = t.replace("\ufb04", "ffl") # ffl ligature
    t = t.replace("\u2018", "'")   # smart open single quote
    t = t.replace("\ufffd", "")    # Unicode replacement char
    return t


def _word_set(text: str) -> set:
    """Tokenize text into a word set for overlap scoring.

    Strips table formatting (pipes, dash sequences) before splitting.
    """
    if not text:
        return set()
    t = _normalize(text)
    # Strip table formatting
    t = t.replace("|", " ")
    # Remove dash sequences (table separators like "---")
    import re
    t = re.sub(r"-{2,}", " ", t)
    words = set(t.split())
    # Remove very short tokens that cause false matches
    return {w for w in words if len(w) > 1}


def _recovery_pass_3(
    elements: List[Dict],
    page_runs: Dict[int, List[Dict]],
    page_rotations: Dict,
    assigned: Dict,
    locked: set,
    elem_by_id: Dict[str, Dict],
    wide_tolerance: float,
    figure_peer_ids: Optional[set] = None,
    run_lookup: Optional[Dict] = None,
    whitespace_only_pages: Optional[set] = None,
) -> Dict:
    """Pass 3: Text-based recovery for unmatched elements.

    Runs after Pass 1 (tight) and Pass 2 (wide) bbox matching.
    Recovers elements with content_run_count==0 using text matching,
    Figure run reclamation, and proximity matching.

    Returns stats dict with per-strategy counts.
    """
    spatial_cap = wide_tolerance * 2  # 110pt

    stats = {
        "strategy_c_reclaimed": 0,
        "strategy_b_proximity": 0,
        "skipped_no_runs": 0,
        "skipped_no_text": 0,
    }

    # Collect unmatched elements with text (candidates for recovery)
    candidates = []
    for elem in elements:
        if elem.get("content_run_count", 0) > 0:
            continue
        # Skip elements whose children already have runs (false positives)
        if any(c.get("content_run_indices") for c in elem.get("children", [])):
            continue
        text = (elem.get("text") or "").strip()
        page = elem.get("page_no")
        if page is None:
            continue
        if whitespace_only_pages and page in whitespace_only_pages:
            continue  # overlay MCRs handle these pages
        runs = page_runs.get(page, [])
        if not runs:
            stats["skipped_no_runs"] += 1
            continue
        if not text:
            stats["skipped_no_text"] += 1
            # Still a candidate for Strategy B (proximity)
            if elem.get("bbox"):
                candidates.append(elem)
            continue
        candidates.append(elem)

    if not candidates:
        stats["total_recovered"] = 0
        return stats

    # Build per-page Figure index for Strategy C
    page_figures: Dict[int, List[Dict]] = {}
    for elem in elements:
        if elem.get("tag") == "Figure" and elem.get("content_run_count", 0) > 0:
            pg = elem.get("page_no")
            if pg is not None:
                page_figures.setdefault(pg, []).append(elem)

    def _assign_runs_to_elem(elem: Dict, run_keys: list, method: str,
                             confidence: str) -> int:
        """Assign runs to element, update assigned dict, return count."""
        if not run_keys:
            return 0
        existing = elem.get("content_run_indices", [])
        existing_keys = {(r["page"], r["run_index"]) for r in existing}
        new_entries = [
            {"page": p, "run_index": ri}
            for p, ri in run_keys if (p, ri) not in existing_keys
        ]
        if not new_entries:
            return 0
        elem["content_run_indices"] = existing + new_entries
        elem["content_run_count"] = len(elem["content_run_indices"])

        # Update matched text preview
        all_texts = []
        for ri in elem["content_run_indices"]:
            _r = run_lookup.get((ri["page"], ri["run_index"])) if run_lookup else None
            if _r:
                all_texts.append(_r.get("text", ""))
            else:
                for _r2 in page_runs.get(ri["page"], []):
                    if _r2["index"] == ri["run_index"]:
                        all_texts.append(_r2.get("text", ""))
                        break
        elem["matched_text_preview"] = " ".join(all_texts)[:120]
        elem["match_confidence"] = confidence
        elem["match_method"] = method

        for p, ri in run_keys:
            assigned[(p, ri)] = elem.get("id", "?")
        return len(new_entries)

    def _run_centroid(run: Dict) -> tuple:
        cx = (run["x"] + run.get("end_x", run["x"])) / 2
        cy = run["y"]
        return cx, cy

    def _dist_to_bbox(cx: float, cy: float, bbox: Dict) -> float:
        if not bbox:
            return float("inf")
        dx = max(bbox.get("l", 0) - cx, 0, cx - bbox.get("r", 0))
        dy = max(bbox.get("b", 0) - cy, 0, cy - bbox.get("t", 0))
        return math.sqrt(dx * dx + dy * dy)

    # ── Strategy C: Run Reclamation from Figures ─────────────────────
    still_unmatched = []
    for elem in candidates:
        text = (elem.get("text") or "").strip()
        if not text or elem.get("tag") == "Figure":
            still_unmatched.append(elem)
            continue

        page = elem.get("page_no")
        bbox = elem.get("bbox")
        figures = page_figures.get(page, [])
        if not figures or not bbox:
            still_unmatched.append(elem)
            continue

        elem_words = _word_set(text)
        if not elem_words:
            still_unmatched.append(elem)
            continue

        reclaimed = []
        for fig in figures:
            # Never reclaim from Figures with actual images
            if fig.get("image_run_indices"):
                continue
            has_text_peer = id(fig) in figure_peer_ids
            threshold = 0.3 if has_text_peer else 0.5

            # Collect Figure's run texts
            fig_run_texts = []
            fig_run_keys = []
            for ri in fig.get("content_run_indices", []):
                key = (ri["page"], ri["run_index"])
                if key in locked:
                    continue
                _r = run_lookup.get((ri["page"], ri["run_index"]))
                if _r:
                    fig_run_texts.append(_r.get("text", ""))
                    fig_run_keys.append((key, _r))
                else:
                    for _r2 in page_runs.get(ri["page"], []):
                        if _r2["index"] == ri["run_index"]:
                            fig_run_texts.append(_r2.get("text", ""))
                            fig_run_keys.append((key, _r2))
                            break

            if not fig_run_keys:
                continue

            fig_words = _word_set(" ".join(fig_run_texts))
            overlap = len(elem_words & fig_words) / len(elem_words) if elem_words else 0
            if overlap < threshold:
                continue

            # Reclaim runs that are closer to elem than to Figure
            fig_bbox = fig.get("bbox", {})
            for key, run in fig_run_keys:
                cx, cy = _run_centroid(run)
                dist_elem = _dist_to_bbox(cx, cy, bbox)
                dist_fig = _dist_to_bbox(cx, cy, fig_bbox)
                if dist_elem <= spatial_cap and dist_elem <= dist_fig:
                    _remove_run_from_element(fig, key[0], key[1])
                    reclaimed.append(key)

        if reclaimed:
            _assign_runs_to_elem(elem, reclaimed, "pass3_reclaimed", "medium")
            stats["strategy_c_reclaimed"] += 1
        else:
            still_unmatched.append(elem)

    candidates = still_unmatched

    # ── Strategy C2: Text-based reclamation from any element ─────────
    # For unmatched elements whose text appears in assigned runs owned
    # by other elements, reclaim runs where the unmatched element's text
    # is a BETTER match than the current owner's text.
    still_unmatched = []
    for elem in candidates:
        text = (elem.get("text") or "").strip()
        if not text or len(text) < 6 or elem.get("tag") == "Figure":
            still_unmatched.append(elem)
            continue

        page = elem.get("page_no")
        bbox = elem.get("bbox")
        if not bbox:
            still_unmatched.append(elem)
            continue

        elem_norm = _normalize(text)[:15]
        elem_words = _word_set(text)
        if not elem_norm or len(elem_norm) < 4 or len(elem_words) < 2:
            still_unmatched.append(elem)
            continue

        reclaimed = []
        for run in page_runs.get(page, []):
            if not run.get("text", "").strip():
                continue
            key = (page, run["index"])
            if key in locked:
                continue
            # Run must be within spatial cap of unmatched element
            cx, cy = _run_centroid(run)
            if _dist_to_bbox(cx, cy, bbox) > spatial_cap:
                continue
            run_text = _normalize(run.get("text", ""))
            if not run_text:
                continue
            # Check if unmatched element's prefix appears in this run
            if elem_norm not in run_text and run_text[:10] not in elem_norm:
                # Fall back to word overlap
                run_words = _word_set(run.get("text", ""))
                if not run_words or len(elem_words & run_words) / len(elem_words) < 0.3:
                    continue

            # This run matches our element. Is it assigned to someone else?
            if key not in assigned:
                reclaimed.append(key)
                continue

            owner_id = assigned[key]
            owner = elem_by_id.get(owner_id)
            if not owner:
                reclaimed.append(key)
                continue

            # Don't steal from Tables with cells (cell sub-matching needs these)
            if owner.get("tag") == "Table" and owner.get("cells"):
                continue

            # Compare: does the run text match our element better than the owner?
            owner_text = _normalize(owner.get("text", ""))
            owner_words = _word_set(owner.get("text", ""))
            run_words = _word_set(run.get("text", ""))

            if not run_words:
                continue

            elem_overlap = len(elem_words & run_words) / len(elem_words) if elem_words else 0
            owner_overlap = len(owner_words & run_words) / len(owner_words) if owner_words else 0

            # Only reclaim if our element is a significantly better match
            if elem_overlap > owner_overlap + 0.2:
                # Also check spatial: we must be closer
                dist_elem = _dist_to_bbox(cx, cy, bbox)
                owner_bbox = owner.get("bbox", {})
                dist_owner = _dist_to_bbox(cx, cy, owner_bbox) if owner_bbox else float("inf")
                if dist_elem <= dist_owner:
                    _remove_run_from_element(owner, page, run["index"])
                    reclaimed.append(key)

        if reclaimed:
            _assign_runs_to_elem(elem, reclaimed, "pass3_reclaimed_text", "medium")
            stats["strategy_c_reclaimed"] += 1
        else:
            still_unmatched.append(elem)

    candidates = still_unmatched

    # ── Strategy B: Nearest-Neighbor Proximity ───────────────────────
    for elem in candidates:
        page = elem.get("page_no")
        bbox = elem.get("bbox")
        if not bbox:
            continue

        text = (elem.get("text") or "").strip()
        # Figure suppression
        if elem.get("tag") == "Figure" and id(elem) in figure_peer_ids:
            continue

        nearby = []
        for run in page_runs.get(page, []):
            if not run.get("text", "").strip():
                continue
            key = (page, run["index"])
            if key in assigned:
                continue
            cx, cy = _run_centroid(run)
            dist = _dist_to_bbox(cx, cy, bbox)
            if wide_tolerance < dist <= 80:
                nearby.append((dist, run))
        nearby.sort(key=lambda x: x[0])

        if not nearby:
            continue

        # For elements without text, require exactly one nearby run
        if not text and len(nearby) > 1:
            continue

        # Cap at 3 runs
        matched_keys = []
        for dist, run in nearby[:3]:
            # If element has text, validate overlap
            if text:
                run_words = _word_set(run.get("text", ""))
                elem_words = _word_set(text)
                if elem_words and run_words and not (run_words & elem_words):
                    continue  # no word overlap = skip
            matched_keys.append((page, run["index"]))

        if matched_keys:
            confidence = "low"
            method = "pass3_proximity"
            if text:
                method = "pass3_proximity_validated"
            _assign_runs_to_elem(elem, matched_keys, method, confidence)
            stats["strategy_b_proximity"] += 1

    stats["total_recovered"] = (
        stats["strategy_c_reclaimed"]
        + stats["strategy_b_proximity"]
    )

    return stats


def _recover_orphaned_run_blocks(
    elements: List[Dict],
    page_runs: Dict[int, List[Dict]],
    assigned: Dict,
    page_deficits: List[Dict],
    run_lookup: Optional[Dict] = None,
) -> Dict:
    """Pass 4: Recover contiguous blocks of unassigned runs on deficit pages.

    Detects pages where Docling mis-segmented an element (e.g. merged a
    sidebar caption with body text from a different page region).  The
    element's bbox is far from the actual body text, so Passes 1-3 can't
    reach those runs.  This pass groups contiguous unassigned runs into
    blocks, verifies the block text appears in an existing element's text
    (confirming the merge), and creates a synthetic P element with a
    bbox derived from the run positions.

    Only fires on pages with >20% text deficit to avoid false positives
    on well-matched pages.
    """
    stats: Dict[str, Any] = {"recovered": 0, "elements_created": 0, "pages_fixed": 0}

    if not page_deficits:
        return stats

    deficit_pages = {d["page"] for d in page_deficits}

    # Find max existing element ID to generate unique synthetic IDs
    max_id = 0
    for elem in elements:
        eid = elem.get("id", "")
        if eid.startswith("elem_"):
            try:
                max_id = max(max_id, int(eid.split("_")[1]))
            except (ValueError, IndexError):
                pass
        elif eid.startswith("gap_"):
            try:
                max_id = max(max_id, int(eid.split("_")[1]) + 10000)
            except (ValueError, IndexError):
                pass
    synth_counter = max_id + 1

    for pg in sorted(deficit_pages):
        runs = page_runs.get(pg, [])
        if not runs:
            continue

        # Collect unassigned runs, sorted by y descending (top of page first)
        unassigned = [
            r for r in runs if (pg, r["index"]) not in assigned
        ]
        if len(unassigned) < 2:
            continue

        unassigned.sort(key=lambda r: (-r["y"], r["x"]))

        # Group into contiguous blocks: runs within 15pt vertical gap
        blocks: List[List[Dict]] = []
        current_block: List[Dict] = [unassigned[0]]
        for r in unassigned[1:]:
            prev_y = current_block[-1]["y"]
            if abs(prev_y - r["y"]) <= 15:
                current_block.append(r)
            else:
                blocks.append(current_block)
                current_block = [r]
        blocks.append(current_block)

        # Only consider blocks with ≥2 runs (single stray runs are noise)
        blocks = [b for b in blocks if len(b) >= 2]
        if not blocks:
            continue

        # For each block, check if its text appears in an existing element
        # on this page whose bbox is far from the block's position
        page_elems = [e for e in elements if e.get("page_no") == pg]

        for block in blocks:
            block_text_parts = []
            for r in block:
                rt = r.get("text", "")
                if rt:
                    block_text_parts.append(rt)
            if not block_text_parts:
                continue

            block_text = " ".join(block_text_parts)
            block_norm = _normalize(block_text)
            if len(block_norm) < 10:
                continue  # too short to verify

            # Compute block bbox from run positions
            block_ys = [r["y"] for r in block]
            block_xs = [r["x"] for r in block]
            block_end_xs = [r.get("end_x", r["x"] + 50) for r in block]
            block_bbox = {
                "l": min(block_xs) - 5,
                "b": min(block_ys) - 5,
                "r": max(block_end_xs) + 5,
                "t": max(block_ys) + 15,  # approximate line height
            }

            # Check: does any element on this page contain this text
            # but have a bbox far from the block?
            source_elem = None
            best_overlap = 0.0
            block_words = _word_set(block_text)
            if not block_words or len(block_words) < 3:
                continue

            for elem in page_elems:
                elem_text = (elem.get("text") or "").strip()
                if not elem_text or len(elem_text) < len(block_text):
                    continue

                elem_words = _word_set(elem_text)
                if not elem_words:
                    continue

                overlap = len(block_words & elem_words) / len(block_words)
                if overlap < 0.5:
                    continue

                # Check spatial distance: block must be far from element bbox
                elem_bbox = elem.get("bbox", {})
                if not elem_bbox:
                    continue

                # Distance from block centroid to element bbox
                bcx = (block_bbox["l"] + block_bbox["r"]) / 2
                bcy = (block_bbox["b"] + block_bbox["t"]) / 2
                dx = max(elem_bbox.get("l", 0) - bcx, 0,
                         bcx - elem_bbox.get("r", 0))
                dy = max(elem_bbox.get("b", 0) - bcy, 0,
                         bcy - elem_bbox.get("t", 0))
                dist = math.sqrt(dx * dx + dy * dy)

                # Only recover if block is >150pt from element (far mismatch)
                if dist > 150 and overlap > best_overlap:
                    best_overlap = overlap
                    source_elem = elem

            if not source_elem:
                continue

            # Verify: don't create if another element already covers this bbox
            already_covered = False
            for elem in page_elems:
                if elem is source_elem:
                    continue
                eb = elem.get("bbox", {})
                if not eb:
                    continue
                # Check if element bbox overlaps with block bbox
                if (eb.get("l", 0) <= block_bbox["r"]
                        and eb.get("r", 0) >= block_bbox["l"]
                        and eb.get("b", 0) <= block_bbox["t"]
                        and eb.get("t", 0) >= block_bbox["b"]):
                    # Check if the overlapping element has matched runs
                    if elem.get("content_run_count", 0) > 0:
                        already_covered = True
                        break

            if already_covered:
                continue

            # Create synthetic P element
            run_indices = [{"page": pg, "run_index": r["index"]} for r in block]
            synth_id = f"orphan_{synth_counter:04d}"
            synth_counter += 1

            synth_elem = {
                "id": synth_id,
                "type": "TextItem",
                "tag": "P",
                "level": None,
                "text": block_text,
                "page_no": pg,
                "bbox": block_bbox,
                "reading_order": 0,
                "alt_text": None,
                "image_ref": None,
                "source": "orphan_recovery",
                "content_run_indices": run_indices,
                "content_run_count": len(run_indices),
                "matched_text_preview": block_text[:80],
                "match_confidence": "orphan_recovered",
                "match_method": "pass4_orphan_block",
            }

            elements.append(synth_elem)
            for ri in run_indices:
                assigned[(ri["page"], ri["run_index"])] = synth_id

            stats["recovered"] += len(run_indices)
            stats["elements_created"] += 1

            logger.info(
                "  Pass 4 orphan recovery: page %d — created %s "
                "(%d runs, text: %.40s…)",
                pg, synth_id, len(run_indices), block_text,
            )

        if stats["elements_created"]:
            stats["pages_fixed"] += 1

    return stats


def _page_level_accounting(
    elements: List[Dict],
    page_runs: Dict[int, List[Dict]],
    assigned: Dict,
) -> List[Dict]:
    """Compute per-page text deficit between expected and matched content.

    Returns list of deficit dicts for pages with >20% deficit.
    """
    # Aggregate per page
    page_expected: Dict[int, int] = {}
    page_elem_ids: Dict[int, List[str]] = {}
    page_elem_texts: Dict[int, List[str]] = {}
    for elem in elements:
        pg = elem.get("page_no")
        if pg is None:
            continue
        text = (elem.get("text") or "").strip()
        page_expected[pg] = page_expected.get(pg, 0) + len(text)
        if elem.get("content_run_count", 0) == 0 and text:
            page_elem_ids.setdefault(pg, []).append(elem.get("id", "?"))
            # Store a short text snippet for UI display
            snippet = text[:80] + ("…" if len(text) > 80 else "")
            page_elem_texts.setdefault(pg, []).append(snippet)

    page_matched: Dict[int, int] = {}
    for pg, runs in page_runs.items():
        matched_chars = 0
        for run in runs:
            if (pg, run["index"]) in assigned:
                matched_chars += len(run.get("text", ""))
        page_matched[pg] = matched_chars

    deficits = []
    for pg in sorted(page_expected):
        expected = page_expected[pg]
        if expected < 50:
            continue
        matched = page_matched.get(pg, 0)
        deficit_pct = max(0, (1 - matched / max(expected, 1))) * 100
        if deficit_pct > 20:
            unassigned = sum(
                1 for r in page_runs.get(pg, [])
                if (pg, r["index"]) not in assigned
            )
            deficits.append({
                "page": pg,
                "expected_chars": expected,
                "matched_chars": matched,
                "deficit_pct": round(deficit_pct, 1),
                "unmatched_elements": page_elem_ids.get(pg, []),
                "unmatched_texts": page_elem_texts.get(pg, []),
                "unassigned_runs": unassigned,
            })

    if deficits:
        logger.warning(
            "  Page-level text deficits (>20%%): %d page(s) — %s",
            len(deficits),
            [(d["page"], f"{d['deficit_pct']}%") for d in deficits[:10]],
        )

    return deficits


def _validate_matches(
    elements: List[Dict],
    page_runs: Dict[int, List[Dict]],
    run_lookup: Optional[Dict] = None,
) -> None:
    """Layer 2: Validate bbox matches with text comparison.

    Adds match_confidence and match_method to each element.
    """
    for elem in elements:
        runs = elem.get("content_run_indices", [])

        if not runs:
            if elem.get("xobject_content_indices"):
                elem["match_confidence"] = "medium"
                elem["match_method"] = "xobject_matched"
            else:
                elem["match_confidence"] = "none"
                elem["match_method"] = "unmatched"
            continue

        # Tags where text validation isn't possible
        if elem.get("tag") in ("Figure", "Formula", "Table", "L"):
            elem["match_confidence"] = "medium"
            elem["match_method"] = "bbox_no_text_validation"
            continue

        elem_text = _normalize(elem.get("text", ""))
        if not elem_text:
            elem["match_confidence"] = "medium"
            elem["match_method"] = "bbox_empty_text"
            continue

        # Concatenate matched run texts
        concat_parts = []
        for ri in runs:
            page_no = ri["page"]
            run_idx = ri["run_index"]
            _r = run_lookup.get((page_no, run_idx))
            if _r:
                concat_parts.append(_r.get("text", ""))
            else:
                for _r2 in page_runs.get(page_no, []):
                    if _r2["index"] == run_idx:
                        concat_parts.append(_r2.get("text", ""))
                        break
        run_text = _normalize(" ".join(concat_parts))

        if not run_text:
            elem["match_confidence"] = "low"
            elem["match_method"] = "bbox_no_run_text"
            continue

        # Bidirectional substring check (first 20 chars)
        if elem_text[:20] in run_text or run_text[:20] in elem_text:
            elem["match_confidence"] = "high"
            elem["match_method"] = "bbox_text_validated"
        else:
            elem["match_confidence"] = "medium"
            elem["match_method"] = "bbox_only"


def print_matching_report(enriched: Dict[str, Any]) -> None:
    """Print a human-readable matching report."""
    summary = enriched.get("matching_summary", {})

    print(f"\n{'='*70}")
    print("ELEMENT-TO-CONTENT MATCHING REPORT")
    print(f"{'='*70}")
    _excluded = summary.get('figure_only_excluded', 0)
    _denom = summary.get('total_elements', 0) - _excluded
    print(f"  Elements: {summary.get('matched_elements', 0)}"
          f"/{_denom} matched "
          f"({summary.get('element_match_rate', 0)}%)"
          + (f" [{_excluded} text-less figures excluded]" if _excluded else ""))
    print(f"  Content runs: {summary.get('matched_runs', 0)}"
          f"/{summary.get('total_content_runs', 0)} matched "
          f"({summary.get('run_match_rate', 0)}%)")
    print(f"  Unmatched runs (artifact candidates): "
          f"{summary.get('unmatched_runs', 0)}")

    # Per-element detail
    elements = enriched.get("elements", [])
    current_page = None
    for elem in elements:
        page = elem.get("page_no")
        if page != current_page:
            current_page = page
            print(f"\n  --- Page {page} ---")

        count = elem.get("content_run_count", 0)
        tag = elem.get("tag", "?")
        text = (elem.get("text") or "")[:50]
        matched_preview = elem.get("matched_text_preview", "")[:50]
        status = "+" if count > 0 else "X"

        print(f"    [{status}] {tag:8s} runs={count:3d} "
              f"docling=\"{text}\"")
        if count > 0 and matched_preview:
            print(f"              stream=\"{matched_preview}\"")

    # Unmatched runs summary by page
    unmatched = enriched.get("unmatched_runs", [])
    if unmatched:
        print(f"\n  Unmatched content runs (first 10):")
        for run in unmatched[:10]:
            print(f"    Page {run['page']:3d} idx={run['run_index']:3d} "
                  f"({run.get('x', 0):6.1f},{run.get('y', 0):6.1f}) "
                  f"\"{run['text'][:40]}\"")
        if len(unmatched) > 10:
            print(f"    ... and {len(unmatched) - 10} more")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Phase 2: Match Docling elements to content stream runs"
    )
    parser.add_argument("structure", help="Docling structure JSON path")
    parser.add_argument("analysis", help="Content stream analysis JSON path")
    parser.add_argument("-o", "--output", help="Write enriched JSON to file")
    parser.add_argument("--tolerance", type=float, default=5.0,
                       help="Bbox matching tolerance in points (default: 5.0)")
    args = parser.parse_args()

    with open(args.structure, "r") as f:
        structure = json.load(f)
    with open(args.analysis, "r") as f:
        analysis = json.load(f)

    enriched = match_elements_to_content(structure, analysis,
                                          tolerance=args.tolerance)
    print_matching_report(enriched)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(enriched, f, indent=2)
        print(f"\nEnriched structure written to: {args.output}")
