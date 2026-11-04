# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smoke tests for the Reading-order view HTML renderer."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = pytest.importorskip("app")  # skips if Gradio/app import unavailable


def _struct(tmp_path):
    data = {
        "pages": {"1": {"width": 612, "height": 792}},
        "elements": [
            {"id": "elem_1", "page_no": 1, "tag": "H2", "text": "Methods",
             "bbox": {"l": 10, "b": 700, "r": 200, "t": 720}},
            {"id": "elem_2", "page_no": 1, "tag": "P", "text": "We construct each scheme.",
             "bbox": {"l": 10, "b": 600, "r": 400, "t": 690}},
            {"id": "elem_3", "page_no": 1, "tag": "L",
             "children": [{"tag": "LI", "text": "one"}, {"tag": "LI", "text": "two"}],
             "bbox": {"l": 10, "b": 500, "r": 400, "t": 590}},
        ],
    }
    p = tmp_path / "structure.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_row_label_uses_text_then_container_fallback():
    assert app._ro_row_label({"tag": "P", "text": "Hello world"}) == "Hello world"
    lst = {"tag": "L", "children": [{"tag": "LI"}, {"tag": "LI"}]}
    assert "List" in app._ro_row_label(lst) and "2" in app._ro_row_label(lst)


def test_view_html_lists_each_block_in_order(tmp_path):
    html = app._reading_order_view_html(
        _struct(tmp_path), 1, "", {"failing_pages": [1], "per_page": {1: 0.32}},
        "sr-edits-sink-x", "sr-order-sink-x",
    )
    # one row per top-level block, carrying its id and reading position
    for eid in ("elem_1", "elem_2", "elem_3"):
        assert f'data-elem-id="{eid}"' in html
    assert 'data-ro-page="1"' in html
    assert "sr-order-sink-x" in html          # order sink wired into the list
    assert "List" in html                      # container fallback label
    assert "0.32" in html                      # banner shows the page tau


def test_view_html_handles_missing_struct():
    html = app._reading_order_view_html("/no/such.json", 1, "", {}, "s", "o")
    assert "Reading order" in html or "No document" in html


def test_ro_initial_page_picks_worst_flagged_page():
    """The Reading-order view should open on the worst-tau flagged page, not
    a hardcoded page 1 (which is often not flagged at all)."""
    sr_quality = {
        "reading_order_failing_pages": [16, 18, 24],
        "reading_order_per_page": {16: 0.40, 18: 0.12, 24: 0.31},
    }
    assert app._ro_initial_page(sr_quality) == 18


def test_ro_initial_page_defaults_to_one_when_nothing_flagged():
    assert app._ro_initial_page({}) == 1
    assert app._ro_initial_page({"reading_order_failing_pages": []}) == 1


def test_ro_initial_page_handles_str_keyed_per_page():
    """per_page keys become strings after a JSON save round-trip."""
    sr_quality = {
        "reading_order_failing_pages": [16, 18],
        "reading_order_per_page": {"16": 0.40, "18": 0.12},
    }
    assert app._ro_initial_page(sr_quality) == 18


def test_ro_preview_omits_visual_page_nav_but_keeps_own_pagebar(tmp_path):
    # The RO preview embeds the Visual overlay but must NOT carry the Visual
    # nav (its _visSetPage buttons target the Visual host — dead here — and
    # duplicate the RO pagebar). It must keep its own _roSetPage pagebar.
    html = app._reading_order_view_html(
        _struct(tmp_path), 1, "", {}, "sr-edits-sink-x", "sr-order-sink-x",
    )
    assert "_visSetPage" not in html, "RO preview must not include dead Visual page buttons"
    assert "vis-nav" not in html, "RO preview must not include the Visual nav bar"
    assert "_roSetPage" in html, "RO view keeps its own page nav"
    assert "vis-page-host-inner" in html, "RO preview still shows the page image/boxes"


def test_view_html_has_resizable_splitter(tmp_path):
    html = app._reading_order_view_html(
        _struct(tmp_path), 1, "", {}, "sr-edits-sink-x", "sr-order-sink-x",
    )
    # draggable divider between the list and the preview (user-adjustable ratio)
    assert 'class="ro-splitter"' in html
    assert 'role="separator"' in html


def test_overlay_has_no_visual_nav(tmp_path):
    # The standalone Visual view was removed; the overlay renders bare (no page
    # nav / mode toggle) for embedding in the Reading-order preview.
    p = _struct(tmp_path)
    html = app._structure_json_to_overlay_html(p, 1, "", {}, sink_id="s")
    assert "vis-nav" not in html
    assert "_visSetPage" not in html
    assert "vis-mode-toggle" not in html
