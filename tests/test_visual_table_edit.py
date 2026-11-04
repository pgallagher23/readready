# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for in-place table cell editing in the Content Editor Visual view.

Tables in the Visual overlay used to render as a single non-editable box that
routed the user to the Reading view. They now render one editable box per cell
(at the cell's bbox), reusing the exact ``{elem_id}.cell_{r}_{c}`` edit key the
Reading view already emits — so the existing save path handles them unchanged.

A table whose cells lack usable bboxes falls back to the old routing box, and
``TOC`` keeps routing. A config kill-switch restores the old routing behaviour.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = pytest.importorskip("app")  # skips if Gradio/app import unavailable


@pytest.fixture(autouse=True)
def _fake_page_image(monkeypatch):
    """The overlay only emits its positioned boxes when a page image is
    available. Stub the renderer so the box HTML is exercised without a PDF."""
    monkeypatch.setattr(
        app, "_render_full_page_image",
        lambda *a, **k: "data:image/png;base64,stub",
    )


def _table_cells(with_bbox=True):
    """A 2x2 table: one header row (row 0) + one body row (row 1)."""
    def bb(l, b, r, t):
        return {"l": l, "b": b, "r": r, "t": t} if with_bbox else None
    return [
        {"row": 0, "col": 0, "text": "Region", "is_header": True,
         "is_col_header": True, "bbox": bb(72, 700, 300, 720)},
        {"row": 0, "col": 1, "text": "Revenue", "is_header": True,
         "is_col_header": True, "bbox": bb(300, 700, 520, 720)},
        {"row": 1, "col": 0, "text": "North", "is_header": False,
         "bbox": bb(72, 675, 300, 695)},
        {"row": 1, "col": 1, "text": "42", "is_header": False,
         "bbox": bb(300, 675, 520, 695)},
    ]


def _struct(tmp_path, *, tag="Table", with_bbox=True):
    data = {
        "pages": {"1": {"width": 612, "height": 792}},
        "elements": [
            {"id": "elem_tbl", "page_no": 1, "tag": tag,
             "reading_order": 1,
             "bbox": {"l": 72, "b": 660, "r": 520, "t": 720},
             "cells": _table_cells(with_bbox=with_bbox)},
        ],
    }
    p = tmp_path / "structure.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def _overlay(struct_path):
    return app._structure_json_to_overlay_html(
        struct_path, 1, "", {"1": {"width": 612, "height": 792}}, sink_id="s"
    )


def test_table_cells_render_as_editable_spans(tmp_path):
    html = _overlay(_struct(tmp_path))
    # every cell is an editable span keyed exactly like the Reading view
    for key in ("elem_tbl.cell_0_0", "elem_tbl.cell_0_1",
                "elem_tbl.cell_1_0", "elem_tbl.cell_1_1"):
        assert f'data-elem-id="{key}"' in html, key
    assert "sr-edit-text" in html
    # header cells carry the header class; body cells do not
    assert "vis-box-cell-header" in html
    assert "vis-box-cell" in html
    # the old routing affordance must be gone for a positioned table
    assert "_visOpenTable" not in html


def test_cell_key_matches_reading_view(tmp_path):
    """Guard against key drift between the Visual and Reading views: both must
    address the same cell with the same key so the save path applies the edit."""
    struct_path = _struct(tmp_path)
    struct = json.loads(open(struct_path, encoding="utf-8").read())
    tbl = struct["elements"][0]
    prose = app._prose_table(tbl, editable=True)
    visual = _overlay(struct_path)
    # the key _prose_table emits for the top-left body cell must appear in both
    assert 'data-elem-id="elem_tbl.cell_1_0"' in prose
    assert 'data-elem-id="elem_tbl.cell_1_0"' in visual


def test_table_without_cell_bboxes_falls_back_to_routing(tmp_path):
    html = _overlay(_struct(tmp_path, with_bbox=False))
    # no positioned cell → keep the old single routing box, no orphaned table
    assert "_visOpenTable" in html
    assert "vis-box-cell" not in html


def test_toc_still_routes(tmp_path):
    html = _overlay(_struct(tmp_path, tag="TOC"))
    assert "_visOpenTable" in html
    assert "vis-box-cell" not in html


def test_kill_switch_restores_routing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        app, "_load_config",
        lambda: {"editor": {"visual_table_edit_enabled": False}},
    )
    html = _overlay(_struct(tmp_path))
    assert "_visOpenTable" in html
    assert "vis-box-cell" not in html
