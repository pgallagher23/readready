# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for output_gen/block_order.py — block-order normalization.

Fixture style mirrors test_heading_inference.py: synthetic element lists,
in-place mutation asserts, no PDFs. Coordinates are bottom-left-origin PDF
points ({l, b, r, t}); page_no is 1-based.
"""

import copy

import pytest

from output_gen.block_order import normalize_block_order


def _el(tag, page, t, b, l=72.0, r=350.0, ro=0, **kw):
    """Minimal element dict in docling_structure shape."""
    e = {
        "id": kw.pop("id", f"elem_{ro:04d}"),
        "type": kw.pop("type", "TextItem"),
        "tag": tag,
        "level": None,
        "page_no": page,
        "bbox": {"l": l, "b": b, "r": r, "t": t},
        "reading_order": ro,
        "text": kw.pop("text", f"{tag} text at {t}"),
    }
    e.update(kw)
    return e


def _tags(elements):
    return [e["tag"] for e in elements]


def _tops(elements):
    return [e["bbox"]["t"] for e in elements]


# ── 1. The rotated-contract rotation shape ────────────────────────────────────────────

def _contract_page():
    """Realistic shape of a contract page 23 (docling JSON)."""
    return [
        _el("H2", 23, t=403.91, b=395.27, l=72.0, r=163.05, ro=140),
        _el("P", 23, t=383.39, b=340.0, ro=141),
        _el("P", 23, t=316.67, b=250.0, ro=142),
        _el("L", 23, t=528.35, b=416.22, l=165.72, r=362.52, ro=143,
            type="ListGroup", text="", children=[{"tag": "LI"}]),
    ]


def test_rotation_shape_moves_list_to_front():
    elements = _contract_page()
    stats = normalize_block_order(elements)
    assert _tags(elements) == ["L", "H2", "P", "P"]
    assert stats["moves"] == 1
    # reading_order renumbered by index
    assert [e["reading_order"] for e in elements] == [0, 1, 2, 3]
    # nested children travel with the moved element
    assert elements[0]["children"] == [{"tag": "LI"}]


# ── 2. Multi-column guard ─────────────────────────────────────────────────────

def test_multicolumn_page_untouched():
    # Two side-by-side columns (gap 20pt >= 6), fully overlapping vertically,
    # plus a would-be displaced block at the end. Columnar -> whole page no-op.
    elements = [
        _el("P", 1, t=700, b=400, l=72, r=290, ro=0),
        _el("P", 1, t=700, b=400, l=310, r=540, ro=1),
        _el("P", 1, t=380, b=300, l=72, r=290, ro=2),
        _el("P", 1, t=750, b=710, ro=3),  # top block ordered last
    ]
    before = copy.deepcopy(elements)
    stats = normalize_block_order(elements)
    assert elements == before
    assert stats["moves"] == 0
    assert stats["pages_skipped"]["columnar"] == 1


# ── 3. Per-page cap ───────────────────────────────────────────────────────────

def test_cap_skips_ambiguous_page():
    # 6 anchors descending + 4 displaced -> 4 > max(2, ceil(0.2*10)=2) -> skip.
    elements = [_el("P", 1, t=700 - i * 50, b=680 - i * 50, ro=i) for i in range(6)]
    elements += [
        _el("P", 1, t=750 + i * 10, b=740 + i * 10, ro=6 + i) for i in range(4)
    ]
    before = copy.deepcopy(elements)
    stats = normalize_block_order(elements)
    assert elements == before
    assert stats["moves"] == 0
    assert stats["pages_skipped"]["capped"] == 1


# ── 4. Small rise is not displacement ────────────────────────────────────────

def test_small_rise_not_moved():
    elements = [
        _el("P", 1, t=700, b=680, ro=0),
        _el("P", 1, t=650, b=630, ro=1),
        _el("P", 1, t=600, b=580, ro=2),
        _el("P", 1, t=708, b=660, ro=3),  # rise 8 < 24, bottom below first top
    ]
    before = copy.deepcopy(elements)
    stats = normalize_block_order(elements)
    assert elements == before
    assert stats["moves"] == 0


# ── 5. Entirely-above clause catches short blocks ────────────────────────────

def test_entirely_above_first_moved():
    # One-line block wholly above the current first element (rise < 24).
    elements = [
        _el("P", 1, t=700, b=680, ro=0),
        _el("P", 1, t=650, b=630, ro=1),
        _el("P", 1, t=600, b=580, ro=2),
        _el("H2", 1, t=712, b=702, ro=3),  # rise 12, but b=702 >= first.t=700
    ]
    stats = normalize_block_order(elements)
    assert _tags(elements) == ["H2", "P", "P", "P"]
    assert stats["moves"] == 1


# ── 6. Footnote-last stays ────────────────────────────────────────────────────

def test_footnote_last_not_moved():
    elements = [
        _el("P", 1, t=700, b=680, ro=0),
        _el("P", 1, t=650, b=630, ro=1),
        _el("P", 1, t=600, b=580, ro=2),
        _el("P", 1, t=100, b=80, ro=3),  # bottom-of-page footnote
    ]
    before = copy.deepcopy(elements)
    stats = normalize_block_order(elements)
    assert elements == before
    assert stats["moves"] == 0


# ── 7. Figures never move but anchor runmax ──────────────────────────────────

def test_displaced_figure_not_moved():
    elements = [
        _el("P", 1, t=700, b=680, ro=0),
        _el("P", 1, t=650, b=630, ro=1),
        _el("P", 1, t=600, b=580, ro=2),
        _el("Figure", 1, t=760, b=720, ro=3),  # displaced but not movable
    ]
    before = copy.deepcopy(elements)
    stats = normalize_block_order(elements)
    assert elements == before
    assert stats["moves"] == 0


def test_full_height_first_figure_suppresses_page():
    # Figure anchors runmax at page top; later top-block shows no rise.
    elements = [
        _el("Figure", 1, t=750, b=60, ro=0),
        _el("P", 1, t=600, b=580, ro=1),
        _el("P", 1, t=500, b=480, ro=2),
        _el("P", 1, t=730, b=700, ro=3),  # t=730 < runmax 750 -> not displaced
    ]
    before = copy.deepcopy(elements)
    stats = normalize_block_order(elements)
    assert elements == before
    assert stats["moves"] == 0


# ── 8. Degenerate pages skipped safely ───────────────────────────────────────

def test_fewer_than_3_elements_skipped():
    elements = [
        _el("P", 1, t=400, b=380, ro=0),
        _el("P", 1, t=700, b=680, ro=1),  # out of order but only 2 elements
    ]
    before = copy.deepcopy(elements)
    stats = normalize_block_order(elements)
    assert elements == before
    assert stats["moves"] == 0
    assert stats["pages_skipped"]["few_elements"] == 1


def test_invalid_bbox_page_skipped():
    elements = [
        _el("P", 1, t=700, b=680, ro=0),
        _el("P", 1, t=650, b=630, ro=1),
        _el("P", 1, t=760, b=720, ro=2),
    ]
    elements[1]["bbox"] = {}
    before = copy.deepcopy(elements)
    stats = normalize_block_order(elements)
    assert elements == before
    assert stats["moves"] == 0
    assert stats["pages_skipped"]["invalid_bbox"] == 1


# ── 9. skip_pages respected ───────────────────────────────────────────────────

def test_skip_pages_respected():
    elements = _contract_page()
    before = copy.deepcopy(elements)
    stats = normalize_block_order(elements, skip_pages={23})
    assert elements == before
    assert stats["moves"] == 0
    assert stats["pages_skipped"]["skip_list"] == 1


# ── 10. Idempotency ───────────────────────────────────────────────────────────

def test_idempotent_on_fixed_page():
    elements = _contract_page()
    normalize_block_order(elements)
    after_first = copy.deepcopy(elements)
    stats2 = normalize_block_order(elements)
    assert elements == after_first
    assert stats2["moves"] == 0
    assert stats2["cross_page_stragglers"] == 0


# ── 11/12. Cross-page stragglers (Step A) ────────────────────────────────────

def _three_page_doc():
    out = []
    ro = 0
    for pg in (1, 2, 3):
        for t in (700, 600, 400):
            out.append(_el("P", pg, t=t, b=t - 30, ro=ro))
            ro += 1
    return out


def test_orphan_at_doc_end_returns_to_page():
    elements = _three_page_doc()
    orphan = _el("P", 2, t=500, b=470, ro=99, id="orphan_0001")
    elements.append(orphan)
    stats = normalize_block_order(elements)
    assert stats["cross_page_stragglers"] == 1
    # Orphan sits within page 2, between t=600 and t=400
    page2 = [e for e in elements if e["page_no"] == 2]
    assert _tops(page2) == [700, 600, 500, 400]
    # Whole array back to page-monotonic
    pages = [e["page_no"] for e in elements]
    assert pages == sorted(pages)
    # reading_order renumbered by index
    assert [e["reading_order"] for e in elements] == list(range(len(elements)))


def test_straggler_cap_disables_step_a():
    elements = _three_page_doc()
    for i in range(6):  # 6 > STRAGGLER_CAP (5)
        elements.append(_el("P", 1, t=500 - i * 10, b=480 - i * 10, ro=90 + i))
    before = copy.deepcopy(elements)
    stats = normalize_block_order(elements)
    assert stats["cross_page_stragglers"] == 0
    # Step A untouched; the appended page-1 elements stay at the end
    assert [e["page_no"] for e in elements] == [e["page_no"] for e in before]


# ── 13. Tie-slack insertion keeps Docling order among near-ties ──────────────

def test_tie_slack_preserves_docling_order():
    # X is entirely above first (b=701 >= t=700) so it IS displaced, but its
    # top is within TIE_SLACK of the first element -> lands AFTER it.
    elements = [
        _el("P", 1, t=700, b=680, ro=0),
        _el("P", 1, t=650, b=630, ro=1),
        _el("P", 1, t=600, b=580, ro=2),
        _el("H2", 1, t=705, b=701, ro=3),
    ]
    stats = normalize_block_order(elements)
    assert stats["moves"] == 1
    assert _tags(elements) == ["P", "H2", "P", "P"]
    assert _tops(elements) == [700, 705, 650, 600]


# ── 14. No-change inputs are untouched (no renumber) ─────────────────────────

def test_no_change_means_no_renumber():
    elements = [
        _el("P", 1, t=700, b=680, ro=5),
        _el("P", 1, t=650, b=630, ro=7),
        _el("P", 1, t=600, b=580, ro=9),
    ]
    before = copy.deepcopy(elements)
    stats = normalize_block_order(elements)
    assert elements == before  # original reading_order values preserved
    assert stats["moves"] == 0
    assert stats["cross_page_stragglers"] == 0
