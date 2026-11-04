# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for output_gen/content_matcher.py — assignment correctness."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from output_gen.content_matcher import match_elements_to_content, _build_summary


def _page_analysis(page_num, runs):
    """Minimal page_analyses entry for match_elements_to_content."""
    return {
        "page": page_num,
        "width": 612,
        "height": 792,
        "rotation": 0,
        "invisible_only": False,
        "has_crop_box": False,
        "has_existing_mcids": False,
        "existing_mcid_count": 0,
        "text_runs": runs,
        "text_run_count": len(runs),
        "image_count": 0,
        "images": [],
        "xobject_count": 0,
        "total_chars": sum(r.get("char_count", len(r.get("text", ""))) for r in runs),
        "fonts_used": [],
    }


def _cs(pages_runs):
    """Build a cs_analysis dict from {page_num: [runs]}."""
    return {
        "file": "test.pdf",
        "pages": len(pages_runs),
        "page_analyses": [_page_analysis(p, r) for p, r in sorted(pages_runs.items())],
        "summary": {
            "total_text_runs": sum(len(r) for r in pages_runs.values()),
            "total_images": 0,
        },
    }


def test_whitespace_only_run_not_assigned_to_element():
    """Whitespace-only runs should be artifacts, not part of any element."""
    structure = {
        "elements": [
            {
                "id": "p0",
                "tag": "P",
                "page_no": 1,
                "text": "Hello world",
                "bbox": {"l": 50, "b": 100, "r": 200, "t": 120},
            }
        ]
    }
    cs = _cs({1: [
        {"index": 0, "text": "Hello world", "x": 55, "y": 110,
         "end_x": 150, "render_mode": 0, "char_count": 11},
        {"index": 1, "text": " ", "x": 150, "y": 110,
         "end_x": 155, "render_mode": 0, "char_count": 1},
    ]})

    enriched = match_elements_to_content(structure, cs)
    runs = enriched["elements"][0].get("content_run_indices", [])
    run_indices = {r["run_index"] for r in runs}

    assert 0 in run_indices, "real text run should be matched"
    assert 1 not in run_indices, \
        "whitespace-only run should NOT be matched to any element"


def test_run_below_table_bbox_not_claimed_by_cell():
    """Run outside the parent Table's bbox must NOT be claimed by any cell.

    Reproduces a failure where run 212 ("* Will be
    verified...") at y=253 was claimed by first-table cell r8 c0
    (Docling text "*Applicant has not previously...") because the
    matcher's parent-bbox grace of 15pt let runs below the table edge
    bleed into cell tolerance windows.

    Cell is placed at the table's bottom row (y=263-283) so the
    cell.b - vert_dn expansion would dip below table.b=260 without
    the parent-bbox clamp. With the 15pt grace the clamp resolves to
    table.b - 15 = 245, so the run at y=253 falls inside the cell
    tolerance window. With the 3pt grace the clamp resolves to 257,
    correctly excluding the run.
    """
    structure = {
        "elements": [
            {
                "id": "tbl",
                "tag": "Table",
                "page_no": 1,
                "bbox": {"l": 50, "b": 260, "r": 500, "t": 600},
                "cells": [
                    {
                        "row": 8, "col": 0, "row_span": 1, "col_span": 1,
                        "text": "*Foo bar",
                        "is_header": False,
                        "is_col_header": False,
                        "is_row_header": False,
                        "bbox": {"l": 50, "b": 263, "r": 250, "t": 283},
                    },
                ],
            },
            {
                "id": "p_below",
                "tag": "P",
                "page_no": 1,
                "text": "*Foo baz",
                "bbox": {"l": 50, "b": 240, "r": 250, "t": 258},
            },
        ]
    }
    cs = _cs({1: [
        {"index": 0, "text": "*Foo baz", "x": 55, "y": 253,
         "end_x": 150, "render_mode": 0, "char_count": 8},
    ]})

    enriched = match_elements_to_content(structure, cs)
    tbl = next(e for e in enriched["elements"] if e["id"] == "tbl")
    cell_runs = {r["run_index"]
                 for r in tbl["cells"][0].get("content_run_indices", [])}

    assert 0 not in cell_runs, \
        "run outside table bbox (y=253 < table.b=260) must not be claimed by cell"


# ---------------------------------------------------------------------------
# EMR denominator: text-less Figures are excluded; other empty elements are not
# ---------------------------------------------------------------------------

def _matched_p(text="hello world"):
    return {"tag": "P", "text": text, "content_run_count": 1,
            "content_run_indices": [{"run_index": 0}]}


def test_textless_figure_excluded_from_emr_denominator():
    """A doc fully matched except a trailing text-less Figure scores 100."""
    elements = [
        _matched_p(),
        {"tag": "Figure", "text": "", "content_run_count": 0,
         "alt_text": "a chart", "image_ref": "img0.png"},
    ]
    summary = _build_summary(elements, unmatched=[], page_runs={})
    assert summary["total_elements"] == 2
    assert summary["matched_elements"] == 1
    assert summary["figure_only_excluded"] == 1
    # denominator drops the text-less figure → 1/1 = 100, not 1/2 = 50
    assert summary["element_match_rate"] == 100.0


def test_matched_figure_not_excluded():
    """A Figure matched via image stays counted (not 'text-less unmatched')."""
    elements = [
        _matched_p(),
        {"tag": "Figure", "text": "", "content_run_count": 0,
         "image_run_indices": [3]},
    ]
    summary = _build_summary(elements, unmatched=[], page_runs={})
    assert summary["figure_only_excluded"] == 0
    assert summary["matched_elements"] == 2
    assert summary["element_match_rate"] == 100.0


def test_empty_non_figure_elements_still_count_as_miss():
    """Empty P/Table/L/Formula are NOT excluded — emptiness is a real signal."""
    for tag in ("P", "Table", "L", "Formula"):
        elements = [_matched_p(), {"tag": tag, "text": "",
                                   "content_run_count": 0}]
        summary = _build_summary(elements, unmatched=[], page_runs={})
        assert summary["figure_only_excluded"] == 0, tag
        assert summary["matched_elements"] == 1, tag
        # empty element still in denominator → 1/2 = 50
        assert summary["element_match_rate"] == 50.0, tag


# ── Block-order normalization integration ────────────────────────────────────

def _displaced_structure():
    """Three stacked paragraphs + a displaced top block ordered last."""
    return {
        "elements": [
            {"id": "e0", "tag": "P", "page_no": 1, "text": "middle one",
             "bbox": {"l": 72, "b": 640, "r": 350, "t": 660},
             "reading_order": 0},
            {"id": "e1", "tag": "P", "page_no": 1, "text": "middle two",
             "bbox": {"l": 72, "b": 590, "r": 350, "t": 610},
             "reading_order": 1},
            {"id": "e2", "tag": "P", "page_no": 1, "text": "bottom text",
             "bbox": {"l": 72, "b": 540, "r": 350, "t": 560},
             "reading_order": 2},
            {"id": "e3", "tag": "P", "page_no": 1, "text": "top block",
             "bbox": {"l": 72, "b": 700, "r": 350, "t": 720},
             "reading_order": 3},
        ]
    }


def _displaced_runs():
    return _cs({1: [
        {"index": 0, "text": "middle one", "x": 75, "y": 650,
         "end_x": 200, "render_mode": 0, "char_count": 10},
        {"index": 1, "text": "middle two", "x": 75, "y": 600,
         "end_x": 200, "render_mode": 0, "char_count": 10},
        {"index": 2, "text": "bottom text", "x": 75, "y": 550,
         "end_x": 200, "render_mode": 0, "char_count": 11},
        {"index": 3, "text": "top block", "x": 75, "y": 710,
         "end_x": 200, "render_mode": 0, "char_count": 9},
    ]})


def test_matcher_normalizes_displaced_top_block():
    """The tail pass moves the displaced top block to the front, with
    run assignments identical to the flag-off run."""
    on = match_elements_to_content(_displaced_structure(), _displaced_runs())
    off = match_elements_to_content(
        _displaced_structure(), _displaced_runs(), normalize_block_order=False
    )

    assert [e["id"] for e in on["elements"]] == ["e3", "e0", "e1", "e2"]
    assert on["matching_summary"]["block_order_normalization"]["moves"] == 1
    # Matching itself is unaffected: same run set per element id
    runs_on = {e["id"]: sorted(r["run_index"] for r in e.get("content_run_indices", []))
               for e in on["elements"]}
    runs_off = {e["id"]: sorted(r["run_index"] for r in e.get("content_run_indices", []))
                for e in off["elements"]}
    assert runs_on == runs_off


def test_matcher_block_order_kill_switch():
    enriched = match_elements_to_content(
        _displaced_structure(), _displaced_runs(), normalize_block_order=False
    )
    assert [e["id"] for e in enriched["elements"]] == ["e0", "e1", "e2", "e3"]
    assert "block_order_normalization" not in enriched["matching_summary"]


def test_heading_move_renormalizes_levels():
    """A moved heading triggers normalize_heading_levels so the manifest path
    (which never reaches pdf_tagger's own call) stays 7.4.2-valid."""
    structure = _displaced_structure()
    structure["elements"][3]["tag"] = "H3"   # displaced top block is a heading
    structure["elements"][0]["tag"] = "H2"   # followed by an H2
    enriched = match_elements_to_content(structure, _displaced_runs())

    tags = [e["tag"] for e in enriched["elements"]]
    assert [e["id"] for e in enriched["elements"]] == ["e3", "e0", "e1", "e2"]
    # H3-first sequence re-leveled to start at H1 with no skips
    assert tags[0] == "H1"
    assert tags[1] == "H2"
    assert enriched["matching_summary"]["block_order_normalization"]["headings_moved"] == 1
