# SPDX-License-Identifier: AGPL-3.0-or-later
"""Re-extract TOC entries from multi-column Table-of-Contents pages.

Docling's table cell detection often assigns wrong row/column indices to
cells in multi-column TOC layouts (e.g. Article No | Title | Page).  When
``_export_structure_json`` concatenates cells by row, the TOCI entries come
out garbled — rows merge, entries split, and text from adjacent columns
gets interleaved.

This module detects multi-column TOCs and re-extracts clean TOCI entries
using PyMuPDF's spatially-accurate text line extraction, with column-aware
nearest-neighbor row matching.
"""

import logging
import re
from collections import Counter
from typing import Any, Callable, Dict, List, Optional

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

# Minimum lines per column cluster to count as a "real" column
_MIN_LINES_PER_COL = 3

# Gap (in points) between x0 clusters to separate columns
_COL_GAP_THRESHOLD = 25

# Y-tolerance for primary nearest-neighbor matching (left → mid)
_PRIMARY_Y_TOL = 8.0

# Y-gap limit for continuation lines within the same mid-column entry
_CONTINUATION_Y_GAP = 13.0

# X-offset from typical mid-column x0 to classify a line as "indented"
_INDENT_OFFSET = 8.0

# Y-tolerance for right-column (page number) matching
_RIGHT_Y_TOL = 15.0

# Leader-dot pattern to strip from assembled entries
_LEADER_DOTS_RE = re.compile(r"[.\xb7]{2,}")

# Multiple whitespace
_MULTI_WS_RE = re.compile(r"\s{2,}")


def _bbox_to_fitz_rect(
    bbox_dict: Dict[str, float], page_height: float
) -> "fitz.Rect":
    """Convert a bottom-left-origin bbox dict to a fitz Rect (top-left origin)."""
    return fitz.Rect(
        bbox_dict["l"],
        page_height - bbox_dict["t"],
        bbox_dict["r"],
        page_height - bbox_dict["b"],
    )


def _extract_lines(
    page: "fitz.Page", clip: "fitz.Rect"
) -> List[Dict[str, Any]]:
    """Extract text lines within *clip* as dicts with x0, yc, text."""
    text_data = page.get_text("dict", clip=clip, sort=True)
    lines: List[Dict[str, Any]] = []
    for block in text_data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            text = " ".join(s["text"] for s in line["spans"]).strip()
            if not text:
                continue
            bbox = line["bbox"]
            lines.append(
                {
                    "x0": bbox[0],
                    "y0": bbox[1],
                    "y1": bbox[3],
                    "yc": (bbox[1] + bbox[3]) / 2.0,
                    "text": text,
                }
            )
    return lines


def _cluster_x_values(lines: List[Dict[str, Any]]) -> List[List[int]]:
    """Cluster rounded x0 values into column groups.

    Returns a list of clusters, each a sorted list of unique rounded x0
    values.  Two values belong to the same cluster when their gap is
    ≤ ``_COL_GAP_THRESHOLD``.
    """
    x0_sorted = sorted(set(round(ln["x0"]) for ln in lines))
    if not x0_sorted:
        return []

    clusters: List[List[int]] = [[x0_sorted[0]]]
    for x in x0_sorted[1:]:
        if x - clusters[-1][-1] <= _COL_GAP_THRESHOLD:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    return clusters


# ── public API ────────────────────────────────────────────────────────


def is_multicolumn_toc(
    pdf_path,
    page_no: int,
    bbox_dict: Dict[str, float],
    page_height: float,
) -> bool:
    """Return True if the TOC region contains 2+ significant text columns.

    A "significant" column has at least ``_MIN_LINES_PER_COL`` text lines.

    Requires 3+ columns (e.g. article-number | title | page-number).
    Two-column layouts (long caption | page-number) work fine with
    Docling's row concatenation and re-extraction can fragment entries.
    """
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return False

    try:
        page = doc[page_no - 1]
        clip = _bbox_to_fitz_rect(bbox_dict, page_height)
        lines = _extract_lines(page, clip)
        if len(lines) < 2 * _MIN_LINES_PER_COL:
            return False

        clusters = _cluster_x_values(lines)

        # Count lines per cluster
        significant = 0
        for cluster in clusters:
            cmin, cmax = min(cluster), max(cluster)
            count = sum(
                1
                for ln in lines
                if cmin - 3 <= round(ln["x0"]) <= cmax + 3
            )
            if count >= _MIN_LINES_PER_COL:
                significant += 1

        return significant >= 3
    finally:
        doc.close()


def extract_toc_from_pymupdf(
    pdf_path,
    page_no: int,
    bbox_dict: Dict[str, float],
    page_height: float,
    text_translate_fn: Optional[Callable[[str], str]] = None,
) -> List[Dict[str, str]]:
    """Re-extract TOCI entries from a multi-column TOC using PyMuPDF.

    Returns a list of ``{"tag": "TOCI", "text": "..."}`` dicts suitable
    for direct use as TOC children in the structure JSON.  Returns an
    empty list if extraction fails or produces no usable entries.

    Parameters
    ----------
    pdf_path : str or Path
        Path to the source PDF.
    page_no : int
        1-based page number.
    bbox_dict : dict
        TOC element bounding box in bottom-left-origin coordinates
        (keys: l, b, r, t).
    page_height : float
        Height of the page in PDF points.
    text_translate_fn : callable, optional
        Text translation function (e.g. for Type 3 font de-PUA).
    """
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        logger.warning("toc_text_extractor: cannot open %s", pdf_path)
        return []

    try:
        page = doc[page_no - 1]
        clip = _bbox_to_fitz_rect(bbox_dict, page_height)
        lines = _extract_lines(page, clip)
        if not lines:
            return []

        # ── Step 1: detect columns ──────────────────────────────────
        clusters = _cluster_x_values(lines)
        # Build (min_x, max_x, line_count) per cluster
        col_info = []
        for cluster in clusters:
            cmin, cmax = min(cluster), max(cluster)
            count = sum(
                1
                for ln in lines
                if cmin - 3 <= round(ln["x0"]) <= cmax + 3
            )
            col_info.append((cmin, cmax, count))

        # Keep only significant columns (≥ 3 lines)
        sig_cols = [
            (lo, hi) for lo, hi, cnt in col_info if cnt >= _MIN_LINES_PER_COL
        ]
        if len(sig_cols) < 2:
            # Not actually multi-column — caller should keep Docling output
            return []

        # Assign column roles: leftmost = LEFT, rightmost = RIGHT,
        # everything in between = MID
        sig_cols.sort()
        left_max_x = sig_cols[0][1] + 3
        right_min_x = sig_cols[-1][0] - 3 if len(sig_cols) >= 3 else 9999

        left_lines = sorted(
            [ln for ln in lines if round(ln["x0"]) <= left_max_x],
            key=lambda ln: ln["yc"],
        )
        right_lines = sorted(
            [
                ln
                for ln in lines
                if len(sig_cols) >= 3 and round(ln["x0"]) >= right_min_x
            ],
            key=lambda ln: ln["yc"],
        )
        mid_lines = sorted(
            [
                ln
                for ln in lines
                if round(ln["x0"]) > left_max_x
                and (len(sig_cols) < 3 or round(ln["x0"]) < right_min_x)
            ],
            key=lambda ln: ln["yc"],
        )

        if not left_lines:
            return []

        # ── Step 2: find typical mid-column start x ────────────────
        typical_mid_x0 = 0.0
        if mid_lines:
            x0_counts = Counter(round(m["x0"]) for m in mid_lines)
            typical_mid_x0 = x0_counts.most_common(1)[0][0]

        # ── Step 3: match left → mid → right ───────────────────────
        mid_used: set = set()
        right_used: set = set()
        # Each entry: (text, fitz_y_top, fitz_y_bottom)
        toci_entries: List[tuple] = []

        for left in left_lines:
            ly = left["yc"]
            entry_y_top = left["y0"]  # fitz y of first line (top)
            entry_y_bottom = left["y1"]  # fitz y of first line (bottom)

            # Find nearest unused MID line within tolerance
            best_j = None
            best_dist = _PRIMARY_Y_TOL
            for j, m in enumerate(mid_lines):
                if j in mid_used:
                    continue
                d = abs(m["yc"] - ly)
                if d < best_dist:
                    best_dist = d
                    best_j = j

            mid_texts: List[str] = []
            last_yc = ly  # y for right-col matching

            if best_j is not None:
                mid_used.add(best_j)
                mid_texts.append(mid_lines[best_j]["text"])
                last_yc = mid_lines[best_j]["yc"]
                entry_y_bottom = max(
                    entry_y_bottom, mid_lines[best_j]["y1"]
                )

                # Consume continuation lines (indented, close in y)
                for j in range(best_j + 1, len(mid_lines)):
                    if j in mid_used:
                        continue
                    m = mid_lines[j]
                    if m["yc"] - last_yc > _CONTINUATION_Y_GAP:
                        break
                    if m["x0"] > typical_mid_x0 + _INDENT_OFFSET:
                        mid_used.add(j)
                        mid_texts.append(m["text"])
                        last_yc = m["yc"]
                        entry_y_bottom = max(entry_y_bottom, m["y1"])
                    else:
                        break  # non-indented → new entry

            # Find nearest unused RIGHT line within tolerance of last_yc
            best_r = None
            best_rd = _RIGHT_Y_TOL
            for j, r in enumerate(right_lines):
                if j in right_used:
                    continue
                d = abs(r["yc"] - last_yc)
                if d < best_rd:
                    best_rd = d
                    best_r = j

            right_text = ""
            if best_r is not None:
                right_used.add(best_r)
                right_text = right_lines[best_r]["text"]
                entry_y_bottom = max(
                    entry_y_bottom, right_lines[best_r]["y1"]
                )

            # Assemble entry
            parts = [left["text"]]
            if mid_texts:
                parts.append(" ".join(mid_texts))
            if right_text:
                parts.append(right_text)

            entry = " ".join(parts)
            # Clean up leader dots and whitespace
            entry = _LEADER_DOTS_RE.sub("", entry).strip()
            entry = _MULTI_WS_RE.sub(" ", entry)

            if text_translate_fn and entry:
                entry = text_translate_fn(entry)

            if entry:
                toci_entries.append(
                    (entry, entry_y_top, entry_y_bottom)
                )

        if not toci_entries:
            return []

        # Build TOCI dicts with bboxes in bottom-left PDF coordinates.
        # fitz y is top-left origin → PDF y = page_height - fitz_y
        toc_l = bbox_dict["l"]
        toc_r = bbox_dict["r"]
        result = []
        for text, fy_top, fy_bottom in toci_entries:
            pdf_top = round(page_height - fy_top, 2)
            pdf_bottom = round(page_height - fy_bottom, 2)
            result.append({
                "tag": "TOCI",
                "text": text,
                "bbox": {
                    "l": toc_l,
                    "b": pdf_bottom,
                    "r": toc_r,
                    "t": pdf_top,
                },
            })

        # Post-process: make bboxes non-overlapping.
        # Each TOCI "owns" the vertical band from the midpoint above it
        # to the midpoint below it (between adjacent top edges).
        # This ensures the Java spatial matcher assigns runs correctly.
        if len(result) >= 2:
            original_tops = [r["bbox"]["t"] for r in result]
            for i in range(len(result)):
                if i == 0:
                    result[i]["bbox"]["t"] = round(bbox_dict["t"], 2)
                else:
                    result[i]["bbox"]["t"] = round(
                        (original_tops[i - 1] + original_tops[i]) / 2.0, 2
                    )

                if i == len(result) - 1:
                    result[i]["bbox"]["b"] = round(bbox_dict["b"], 2)
                else:
                    result[i]["bbox"]["b"] = round(
                        (original_tops[i] + original_tops[i + 1]) / 2.0, 2
                    )

        logger.debug(
            "toc_text_extractor: page %d → %d TOCI entries",
            page_no,
            len(result),
        )
        return result

    except Exception as exc:
        logger.warning(
            "toc_text_extractor: extraction failed on page %d: %s",
            page_no,
            exc,
        )
        return []
    finally:
        doc.close()
