# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the page-overlay renderer (app.py).

The standalone Visual view was removed; this renderer now backs the
Reading-order view's preview pane. Covers the load-bearing coordinate
transform and the overlay HTML emitted for each element kind, including that
editable boxes reuse the same sr-edit-text/sr-edit-alt spans the prose view
emits (so the existing JS edit bridge + save path handle them unchanged).
"""
import json
import re

import pytest

import app


def test_vis_pct_rect_matches_manual_flip():
    # bbox bottom-left origin; page 612x792 (US Letter)
    style = app._vis_pct_rect({"l": 100, "b": 200, "r": 300, "t": 400}, 612, 792)
    # left = 100*100/612, top = 100*(792-400)/792, w = 100*200/612, h = 100*200/792
    assert "left:16.340%" in style
    assert "top:49.495%" in style
    assert "width:32.680%" in style
    assert "height:25.253%" in style


def test_vis_pct_rect_guards_zero_dims():
    assert app._vis_pct_rect({"l": 1, "b": 1, "r": 2, "t": 2}, 0, 0) == "left:0;top:0;width:0;height:0;"


def _pct(s, key):
    m = re.search(rf"{key}:([\d.]+)%", s)
    return float(m.group(1)) if m else None


@pytest.fixture
def doc_with_pdf(tmp_path):
    """A real 1-page 612x792 PDF + matching structure JSON."""
    import fitz

    pdf_path = tmp_path / "doc.pdf"
    d = fitz.open()
    d.new_page(width=612, height=792)
    d.save(str(pdf_path))
    d.close()

    struct = {
        "pages": {"1": {"width": 612.0, "height": 792.0}},
        "elements": [
            {"id": "elem_0001", "tag": "P", "page_no": 1,
             "bbox": {"l": 50, "b": 700, "r": 560, "t": 740},
             "reading_order": 0, "text": "Hello paragraph"},
            {"id": "elem_0002", "tag": "Figure", "page_no": 1,
             "bbox": {"l": 50, "b": 500, "r": 300, "t": 680},
             "reading_order": 1, "alt_text": "A chart", "image_ref": ""},
            {"id": "elem_0003", "tag": "Table", "page_no": 1,
             "bbox": {"l": 50, "b": 300, "r": 560, "t": 480},
             "reading_order": 2, "cells": [{"row": 0, "col": 0, "text": "x"}]},
            {"id": "elem_0004", "tag": "L", "page_no": 1,
             "bbox": {"l": 50, "b": 150, "r": 300, "t": 280},
             "reading_order": 3,
             "children": [
                 {"tag": "LI", "text": "item one", "page_no": 1,
                  "bbox": {"l": 50, "b": 250, "r": 300, "t": 280},
                  "children": [{"tag": "LBody", "text": "item one"}]},
                 {"tag": "LI", "text": "item two", "page_no": 1,
                  "bbox": {"l": 50, "b": 210, "r": 300, "t": 240},
                  "children": [{"tag": "LBody", "text": "item two"}]},
             ]},
            {"id": "elem_0005", "tag": "P", "page_no": 2,  # other page — excluded
             "bbox": {"l": 50, "b": 700, "r": 560, "t": 740}, "reading_order": 4, "text": "p2"},
        ],
    }
    struct_path = tmp_path / "docling_structure.json"
    struct_path.write_text(json.dumps(struct), encoding="utf-8")
    return str(pdf_path), str(struct_path)


def test_overlay_html_structure_and_boxes(doc_with_pdf):
    pdf_path, struct_path = doc_with_pdf
    html = app._structure_json_to_overlay_html(
        struct_path, 1, pdf_path, {}, sink_id="sink-xyz",
    )

    # Wrapper carries the sink anchor + page metadata the JS bridge/nav read.
    assert 'data-sr-sink-id="sink-xyz"' in html
    assert 'data-page="1"' in html
    assert 'data-page-count="1"' in html
    assert "vis-page-img" in html  # real PDF -> image rendered

    # P box: editable text span, same markup as prose view.
    assert 'class="vis-box vis-box-text"' in html
    assert 'class="sr-edit-text"' in html
    assert 'data-elem-id="elem_0001"' in html
    assert 'data-field="text"' in html

    # Figure box: edits alt_text.
    assert "vis-box-fig" in html
    assert 'class="sr-edit-alt"' in html
    assert 'data-field="alt_text"' in html

    # Table still routes to Reading view; carries target id.
    assert "vis-box-table" in html
    assert 'data-target-id="elem_0003"' in html
    assert "window._visOpenTable(this)" in html

    # List items are now EDITABLE boxes (one per LI), with unique positional
    # keys targeting the LBody leaf — NOT a single routing box.
    assert "vis-box-list" in html
    assert 'data-elem-id="elem_0004.LI0.LBody0"' in html
    assert 'data-elem-id="elem_0004.LI1.LBody0"' in html

    # Reading-order badge present.
    assert '<span class="vis-seq">0</span>' in html

    # The Visual standalone view (and its Image/Text/Both nav) was removed; the
    # overlay now renders bare for embedding in the Reading-order preview.
    assert "vis-mode-toggle" not in html
    assert "vis-nav" not in html

    # Page-2 element excluded from page-1 overlay.
    assert "p2" not in html


def test_overlay_box_position_is_correct(doc_with_pdf):
    pdf_path, struct_path = doc_with_pdf
    html = app._structure_json_to_overlay_html(struct_path, 1, pdf_path, {}, sink_id="s")
    # Find the P box style (top should be 100*(792-740)/792 = 6.566%)
    m = re.search(r'vis-box vis-box-text"\s+style="([^"]+)"', html)
    assert m, "P box style not found"
    style = m.group(1)
    assert abs(_pct(style, "left") - (100 * 50 / 612)) < 0.01
    assert abs(_pct(style, "top") - (100 * (792 - 740) / 792)) < 0.01
    assert abs(_pct(style, "width") - (100 * 510 / 612)) < 0.01
    assert abs(_pct(style, "height") - (100 * 40 / 792)) < 0.01


def test_overlay_missing_struct_is_graceful():
    html = app._structure_json_to_overlay_html("/no/such.json", 1, "/no/such.pdf", {}, sink_id="s")
    assert "vis-empty" in html


# ── Editable-list save keys (utils.editor_input) ────────────────────────────
from utils.editor_input import (  # noqa: E402
    list_child_key,
    iter_list_edit_targets,
    is_valid_elem_id,
)


def _L(top_id, *li_specs):
    """Build an L element. Each spec is a str (flat LI text) or a list of
    (tag, text) grandchildren."""
    children = []
    for spec in li_specs:
        if isinstance(spec, str):
            children.append({"tag": "LI", "text": spec, "children": []})
        else:
            gks = [{"tag": t, "text": tx} for t, tx in spec]
            head = next((g["text"] for g in gks if g["tag"] == "LBody"), "")
            children.append({"tag": "LI", "text": head, "children": gks})
    return {"tag": "L", "id": top_id, "children": children}


def test_list_child_key_format():
    assert list_child_key("elem_15", ("LI", 3), ("LBody", 0)) == "elem_15.LI3.LBody0"
    assert list_child_key("e", ("LI", 0)) == "e.LI0"
    assert list_child_key("e") == "e"


def test_iter_list_edit_targets_no_collision():
    # THE regression lock: two items must yield two distinct keys -> two nodes.
    L = _L("elem_0015", [("LBody", "AAA")], [("LBody", "BBB")])
    targets = list(iter_list_edit_targets(L, "elem_0015"))
    keys = [t[0] for t in targets]
    assert keys == ["elem_0015.LI0.LBody0", "elem_0015.LI1.LBody0"]
    assert len(set(keys)) == 2  # no collision
    leaves = [t[1] for t in targets]
    assert leaves[0] is not leaves[1]
    assert leaves[0]["text"] == "AAA" and leaves[1]["text"] == "BBB"
    assert all(t[3] == "lbody" for t in targets)
    assert targets[0][2] is L["children"][0]  # li_node is the owning LI


def test_iter_list_edit_targets_flat_li():
    L = _L("elem_0001", "item one", "item two")
    targets = list(iter_list_edit_targets(L, "elem_0001"))
    assert [t[0] for t in targets] == ["elem_0001.LI0", "elem_0001.LI1"]
    assert all(t[3] == "flat" for t in targets)
    assert targets[0][1] is L["children"][0]  # leaf is the LI itself


def test_iter_list_edit_targets_lbl_and_lbody():
    L = {"tag": "L", "id": "elem_0002", "children": [
        {"tag": "LI", "text": "x", "children": [
            {"tag": "Lbl", "text": "1."}, {"tag": "LBody", "text": "first"}]}]}
    by_kind = {t[3]: t[0] for t in iter_list_edit_targets(L, "elem_0002")}
    assert by_kind["lbl"] == "elem_0002.LI0.Lbl0"
    assert by_kind["lbody"] == "elem_0002.LI0.LBody0"


def test_iter_list_edit_targets_nested():
    nested = {"tag": "L", "id": None, "children": [
        {"tag": "LI", "text": "n1", "children": [{"tag": "LBody", "text": "n1"}]}]}
    L = {"tag": "L", "id": "elem_0009", "children": [
        {"tag": "LI", "text": "outer", "children": [nested]}]}
    assert [t[0] for t in iter_list_edit_targets(L, "elem_0009")] == [
        "elem_0009.LI0.LI0.LBody0"]


def test_empty_item_does_not_shift_keys():
    L = {"tag": "L", "id": "elem_0003", "children": [
        {"tag": "LI", "text": "a", "children": [{"tag": "LBody", "text": "a"}]},
        {"tag": "LI", "text": "", "children": [{"tag": "LBody", "text": ""}]},  # blank
        {"tag": "LI", "text": "c", "children": [{"tag": "LBody", "text": "c"}]}]}
    # blank middle item is skipped but LI indices stay positional (LI2, not LI1).
    assert [t[0] for t in iter_list_edit_targets(L, "elem_0003")] == [
        "elem_0003.LI0.LBody0", "elem_0003.LI2.LBody0"]


def test_list_keys_pass_validation():
    L = _L("elem_0015", [("Lbl", "1."), ("LBody", "AAA")], [("LBody", "BBB")])
    for key, *_ in iter_list_edit_targets(L, "elem_0015"):
        assert is_valid_elem_id(key)


def test_save_applies_both_list_items_no_collision():
    # Mirrors the pipeline_core save block: two different items must both
    # persist (the collision bug was last-write-wins).
    L = _L("elem_0004", [("LBody", "old one")], [("LBody", "old two")])
    text_edits = {"elem_0004.LI0.LBody0": "NEW ONE", "elem_0004.LI1.LBody0": "NEW TWO"}
    for key, leaf, li, kind in iter_list_edit_targets(L, "elem_0004"):
        if key in text_edits:
            leaf["text"] = text_edits[key]
            if kind == "lbody":
                li["text"] = text_edits[key]
    assert L["children"][0]["children"][0]["text"] == "NEW ONE"
    assert L["children"][1]["children"][0]["text"] == "NEW TWO"  # not overwritten
    assert L["children"][0]["text"] == "NEW ONE"  # LI synced for non-tagger readers


def test_prose_and_overlay_emit_matching_list_keys(doc_with_pdf):
    # Render keys (prose + overlay) must be the SAME keys the save path resolves.
    _pdf, struct_path = doc_with_pdf
    prose = app._structure_json_to_prose_html(struct_path, editable=True)
    overlay = app._structure_json_to_overlay_html(struct_path, 1, _pdf, {}, sink_id="s")
    for key in ("elem_0004.LI0.LBody0", "elem_0004.LI1.LBody0"):
        assert f'data-elem-id="{key}"' in prose, f"prose missing {key}"
        assert f'data-elem-id="{key}"' in overlay, f"overlay missing {key}"
