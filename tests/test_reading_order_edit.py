# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the pure reading-order reorder core."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from output_gen.reading_order_edit import (  # noqa: E402
    reconcile_page_order,
    apply_sr_order_edits,
)


def _write(tmp_path, struct):
    p = tmp_path / "structure.json"
    p.write_text(json.dumps(struct), encoding="utf-8")
    return str(p)


def _ids(path, page_no):
    struct = json.loads(open(path, encoding="utf-8").read())
    return [e["id"] for e in struct["elements"] if e.get("page_no") == page_no]


# --- reconcile_page_order --------------------------------------------------

def test_reconcile_simple_permutation():
    assert reconcile_page_order(["a", "b", "c"], ["c", "a", "b"]) == ["c", "a", "b"]


def test_reconcile_drops_unknown_and_appends_unnamed():
    # 'z' is unknown -> dropped; 'c' not named -> kept, appended in place.
    assert reconcile_page_order(["a", "b", "c"], ["b", "z", "a"]) == ["b", "a", "c"]


def test_reconcile_ignores_duplicate_requested_ids():
    assert reconcile_page_order(["a", "b"], ["b", "b", "a"]) == ["b", "a"]


def test_reconcile_empty_request_keeps_current():
    assert reconcile_page_order(["a", "b"], []) == ["a", "b"]


# --- apply_sr_order_edits --------------------------------------------------

def test_apply_permutes_target_page(tmp_path):
    path = _write(tmp_path, {"elements": [
        {"id": "a", "page_no": 1}, {"id": "b", "page_no": 1}, {"id": "c", "page_no": 1},
    ]})
    changed = apply_sr_order_edits(path, {1: ["c", "a", "b"]})
    assert changed == [1]
    assert _ids(path, 1) == ["c", "a", "b"]


def test_apply_leaves_other_pages_untouched_even_when_interleaved(tmp_path):
    # Page 1 and page 2 elements are interleaved in the array.
    path = _write(tmp_path, {"elements": [
        {"id": "a", "page_no": 1}, {"id": "x", "page_no": 2},
        {"id": "b", "page_no": 1}, {"id": "y", "page_no": 2},
    ]})
    apply_sr_order_edits(path, {1: ["b", "a"]})
    assert _ids(path, 1) == ["b", "a"]
    assert _ids(path, 2) == ["x", "y"]  # page 2 unchanged


def test_apply_moves_container_with_children(tmp_path):
    path = _write(tmp_path, {"elements": [
        {"id": "p", "page_no": 1, "tag": "P"},
        {"id": "L", "page_no": 1, "tag": "L", "children": [{"tag": "LI", "text": "one"}]},
    ]})
    apply_sr_order_edits(path, {1: ["L", "p"]})
    struct = json.loads(open(path, encoding="utf-8").read())
    moved = struct["elements"][0]
    assert moved["id"] == "L" and moved["children"][0]["text"] == "one"  # children rode along


def test_apply_noop_returns_empty_and_does_not_rewrite(tmp_path):
    path = _write(tmp_path, {"elements": [
        {"id": "a", "page_no": 1}, {"id": "b", "page_no": 1},
    ]})
    before = open(path, encoding="utf-8").read()
    changed = apply_sr_order_edits(path, {1: ["a", "b"]})  # same order
    assert changed == []
    assert open(path, encoding="utf-8").read() == before


def test_apply_single_block_page_is_noop(tmp_path):
    path = _write(tmp_path, {"elements": [{"id": "a", "page_no": 1}]})
    assert apply_sr_order_edits(path, {1: ["a"]}) == []


def test_apply_unknown_page_ignored(tmp_path):
    path = _write(tmp_path, {"elements": [{"id": "a", "page_no": 1}]})
    assert apply_sr_order_edits(path, {99: ["a"]}) == []


def test_apply_empty_edits_is_noop(tmp_path):
    path = _write(tmp_path, {"elements": [{"id": "a", "page_no": 1}]})
    assert apply_sr_order_edits(path, {}) == []


def test_apply_skips_page_with_nonunique_ids(tmp_path):
    # Two blocks with the same id on one page: refuse to reorder (no corruption).
    path = _write(tmp_path, {"elements": [
        {"id": "dup", "page_no": 1}, {"id": "dup", "page_no": 1}, {"id": "c", "page_no": 1},
    ]})
    before = open(path, encoding="utf-8").read()
    assert apply_sr_order_edits(path, {1: ["c", "dup"]}) == []
    assert open(path, encoding="utf-8").read() == before


# --- _apply_structure_order_edits seam (pipeline_core) --------------------

def test_apply_structure_order_edits_seam(tmp_path):
    from pipeline_core import _apply_structure_order_edits
    path = _write(tmp_path, {"elements": [
        {"id": "a", "page_no": 1}, {"id": "b", "page_no": 1},
    ]})
    result = {"structure_json_path": path}
    changed = _apply_structure_order_edits(result, {1: ["b", "a"]})
    assert changed == [1]
    assert _ids(path, 1) == ["b", "a"]


def test_apply_structure_order_edits_no_path_is_safe(tmp_path):
    from pipeline_core import _apply_structure_order_edits
    assert _apply_structure_order_edits({}, {1: ["b", "a"]}) == []
