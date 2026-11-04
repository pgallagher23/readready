# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for output_gen/heading_inference.py — font-size clustering.

The font-size lookup (PyMuPDF) is exercised by smoke runs in the converter;
these tests pin the cluster→level math so it can't silently regress.

Run:  python -m pytest tests/test_heading_inference.py -v
"""

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from output_gen.heading_inference import (
    CLUSTER_TOLERANCE_PT,
    MAX_LEVEL,
    _assign_levels,
    _bucket_sizes,
    _normalize_sequence,
    _numbering_depth,
    close_html_heading_level_gaps,
    infer_heading_levels,
    normalize_heading_levels,
)


def test_single_size_returns_h1():
    out = _bucket_sizes([12.0, 12.0, 12.0])
    assert out == {12.0: 1}


def test_two_sizes_get_two_levels_largest_first():
    out = _bucket_sizes([18.0, 12.0, 12.0, 18.0])
    assert out[18.0] == 1
    assert out[12.0] == 2


def test_within_tolerance_merges():
    # 12 and 12.5 are within 1.0 of each other -> same level
    out = _bucket_sizes([18.0, 12.5, 12.0])
    assert out[18.0] == 1
    assert out[12.5] == out[12.0] == 2


def test_six_distinct_sizes_use_all_levels():
    sizes = [30.0, 24.0, 20.0, 16.0, 12.0, 9.0]
    out = _bucket_sizes(sizes)
    assert out[30.0] == 1
    assert out[24.0] == 2
    assert out[20.0] == 3
    assert out[16.0] == 4
    assert out[12.0] == 5
    assert out[9.0] == 6


def test_more_than_max_level_clusters_get_merged():
    # 8 distinct sizes, MAX_LEVEL=6 -> must merge 2 adjacent pairs
    sizes = [40.0, 30.0, 22.0, 18.0, 14.0, 12.0, 10.0, 8.0]
    out = _bucket_sizes(sizes)
    levels = sorted(set(out.values()))
    assert len(levels) <= MAX_LEVEL
    # Largest size still maps to H1
    assert out[40.0] == 1
    # Adjacent-pair merging: the two sizes with smallest gap get same level
    # (12.0/10.0 gap=2, 14.0/12.0 gap=2, 10.0/8.0 gap=2, 22.0/18.0 gap=4 — pick smaller)
    # Just assert: at least one pair of distinct input sizes maps to same level
    pairs_sharing_level = sum(
        1 for a in out for b in out if a < b and out[a] == out[b]
    )
    assert pairs_sharing_level > 0


def test_empty_input():
    assert _bucket_sizes([]) == {}
    assert _bucket_sizes([0.0]) == {}


def test_infer_heading_levels_skips_when_few_headings():
    # Single heading element -> no-op (you can't have a hierarchy of one)
    elements = [{"tag": "H1", "level": 1, "page_no": 1,
                 "bbox": {"l": 0, "b": 0, "r": 100, "t": 20}}]
    updated = infer_heading_levels(elements, "/tmp/nonexistent.pdf")
    assert updated == 0
    assert elements[0]["tag"] == "H1"


def test_infer_heading_levels_skips_missing_pdf():
    elements = [
        {"tag": "H1", "level": 1, "page_no": 1, "bbox": {"l": 0, "b": 0, "r": 100, "t": 20}},
        {"tag": "H1", "level": 1, "page_no": 1, "bbox": {"l": 0, "b": 30, "r": 100, "t": 50}},
    ]
    updated = infer_heading_levels(elements, "/tmp/definitely-not-a-real-pdf.pdf")
    assert updated == 0  # graceful on missing PDF
    # tags unchanged
    assert all(e["tag"] == "H1" for e in elements)


def test_tolerance_constant_is_positive():
    # Ward off accidental zero/negative tolerance from a future refactor
    assert CLUSTER_TOLERANCE_PT > 0
    assert MAX_LEVEL >= 3  # PDF/UA allows up to 6; we want at least basic hierarchy


# --- numbering depth -------------------------------------------------------

def test_numbering_depth_chapter():
    assert _numbering_depth("Chapter 1 Introduction") == 1
    assert _numbering_depth("Chapter 2") == 1


def test_numbering_depth_dotted():
    assert _numbering_depth("1.1 Background") == 2
    assert _numbering_depth("2.3.1 Spatially correlated effects") == 3


def test_numbering_depth_bare_leading_integer():
    assert _numbering_depth("1 Introduction") == 1


def test_numbering_depth_trailing_dot_does_not_add_level():
    assert _numbering_depth("1. Introduction") == 1
    assert _numbering_depth("1.1. Background") == 2


def test_numbering_depth_unnumbered_returns_none():
    assert _numbering_depth("ACKNOWLEDGMENTS") is None
    assert _numbering_depth("Abstract") is None
    # leading word, trailing number is not a section number
    assert _numbering_depth("References 54") is None
    assert _numbering_depth("") is None


def test_numbering_depth_clamped_to_max_level():
    assert _numbering_depth("1.2.3.4.5.6.7 deep") == MAX_LEVEL


# --- level assignment (font size + numbering combination) ------------------

def test_assign_levels_numbering_wins_when_sizes_equal():
    # All same font size, but numbering implies a hierarchy -> numbering wins.
    headings = [
        {"size": 17.0, "text": "Chapter 1 Introduction"},
        {"size": 17.0, "text": "1.1 Background"},
        {"size": 17.0, "text": "1.1.1 Detail"},
    ]
    assert _assign_levels(headings) == [1, 2, 3]


def test_assign_levels_font_size_when_unnumbered():
    headings = [
        {"size": 24.8, "text": "Contents"},
        {"size": 17.2, "text": "Acknowledgments"},
    ]
    assert _assign_levels(headings) == [1, 2]


def test_assign_levels_mixed_numbered_and_unnumbered():
    headings = [
        {"size": 24.8, "text": "Chapter 1 Introduction"},  # numbering -> 1
        {"size": 17.2, "text": "1.1 Background"},           # numbering -> 2
        {"size": 24.8, "text": "Contents"},                 # font -> largest -> 1
    ]
    assert _assign_levels(headings) == [1, 2, 1]


def test_assign_levels_no_size_no_numbering_is_none():
    # Unknown size and no numbering -> no opinion (None = leave unchanged).
    headings = [
        {"size": None, "text": "Mystery Heading"},
        {"size": 20.0, "text": "Big Title"},
    ]
    out = _assign_levels(headings)
    assert out[0] is None
    assert out[1] == 1


def test_assign_levels_no_size_uses_numbering():
    headings = [
        {"size": None, "text": "Chapter 1"},
        {"size": None, "text": "1.1 Sub"},
    ]
    assert _assign_levels(headings) == [1, 2]


# --- full infer_heading_levels path (real PDF font lookup) -----------------

def _make_pdf_with_headings(path, headings):
    """Create a one-page PDF with the given (text, fontsize) headings stacked
    top-to-bottom. Returns element dicts (all tagged H1) whose bboxes match the
    rendered text, mirroring what the converter produces."""
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    H = page.rect.height
    y = 80
    for text, size in headings:
        page.insert_text((72, y), text, fontsize=size)
        y += size + 40
    doc.save(str(path))
    doc.close()

    # Read back exact span bboxes/sizes, build bottom-left-origin elements.
    doc = fitz.open(str(path))
    page = doc[0]
    spans = []
    for blk in page.get_text("dict")["blocks"]:
        for ln in blk.get("lines", []):
            for sp in ln["spans"]:
                spans.append(sp)
    doc.close()

    elements = []
    for i, (text, _size) in enumerate(headings):
        sp = spans[i]
        x0, y0, x1, y1 = sp["bbox"]
        elements.append({
            "tag": "H1", "level": 1, "page_no": 1, "text": text,
            "bbox": {"l": x0, "b": H - y1, "r": x1, "t": H - y0},
        })
    return elements


def test_infer_levels_font_size_path(tmp_path):
    pdf = tmp_path / "fs.pdf"
    elements = _make_pdf_with_headings(
        pdf, [("Big Section", 24.0), ("Smaller Section", 12.0)]
    )
    changed = infer_heading_levels(elements, str(pdf))
    assert elements[0]["tag"] == "H1"   # largest font
    assert elements[1]["tag"] == "H2"   # smaller font
    assert changed >= 1


def test_infer_levels_numbering_overrides_equal_font(tmp_path):
    pdf = tmp_path / "num.pdf"
    elements = _make_pdf_with_headings(
        pdf, [("Chapter 1 Intro", 14.0), ("1.1 Background", 14.0)]
    )
    infer_heading_levels(elements, str(pdf))
    # Same font size, but numbering depth makes a real hierarchy.
    assert elements[0]["tag"] == "H1"
    assert elements[1]["tag"] == "H2"


def test_infer_levels_updates_level_field(tmp_path):
    pdf = tmp_path / "lvl.pdf"
    elements = _make_pdf_with_headings(
        pdf, [("Big", 24.0), ("Med", 16.0), ("1.1 Sub", 16.0)]
    )
    infer_heading_levels(elements, str(pdf))
    # level field kept in sync with tag
    for e in elements:
        assert e["tag"] == f"H{e['level']}"


# --- sequence normalization (veraPDF 7.4.2: H1 first, no downward skips) ----

def _is_valid_sequence(levels):
    if not levels:
        return True
    if levels[0] != 1:
        return False
    for prev, cur in zip(levels, levels[1:]):
        if cur > prev + 1:   # skipped a level going deeper
            return False
    return True


def test_normalize_first_heading_forced_to_h1():
    assert _normalize_sequence([3, 5, 2]) == [1, 2, 2]


def test_normalize_no_downward_skip():
    assert _normalize_sequence([1, 3]) == [1, 2]


def test_normalize_jump_back_up_allowed():
    assert _normalize_sequence([1, 2, 3, 1]) == [1, 2, 3, 1]


def test_normalize_none_is_sibling_of_previous():
    assert _normalize_sequence([1, 2, None, 3]) == [1, 2, 2, 3]


def test_normalize_leading_none_becomes_h1():
    assert _normalize_sequence([None, 2]) == [1, 2]


def test_normalize_empty():
    assert _normalize_sequence([]) == []


def test_normalize_always_valid_for_messy_input():
    for seq in ([3, 5, 2, 1, 6], [None, 4, 4, 2, 5], [2, 2, 2], [6, 1, 5]):
        assert _is_valid_sequence(_normalize_sequence(seq)), seq


# --- element-list heading normalization (the final pre-tagging pass) ---------

def _tags(elements):
    return [e["tag"] for e in elements]


def _elems(*tags):
    """Build minimal element dicts; non-heading tags get no level."""
    out = []
    for t in tags:
        e = {"tag": t}
        if t and len(t) == 2 and t[0] == "H":
            e["level"] = int(t[1])
        out.append(e)
    return out


def test_normalize_heading_levels_fixes_skip_and_preserves_nonheadings():
    els = _elems("H1", "H3", "P", "H2", "H4")
    changed = normalize_heading_levels(els)
    assert _tags(els) == ["H1", "H2", "P", "H2", "H3"]
    assert changed == 2  # H3->H2 and H4->H3
    # level field kept in sync with tag
    assert els[1]["level"] == 2
    assert els[4]["level"] == 3
    # non-heading untouched (no level key introduced)
    assert els[2] == {"tag": "P"}


def test_normalize_heading_levels_forces_first_to_h1():
    els = _elems("H3", "P", "H4")
    normalize_heading_levels(els)
    assert _tags(els) == ["H1", "P", "H2"]


def test_normalize_heading_levels_idempotent_on_valid():
    els = _elems("H1", "H1", "H2", "H3", "P", "H2")
    changed = normalize_heading_levels(els)
    assert changed == 0
    assert _tags(els) == ["H1", "H1", "H2", "H3", "P", "H2"]


def test_normalize_heading_levels_fewer_than_two_headings_is_noop():
    els = _elems("H3", "P", "P")
    assert normalize_heading_levels(els) == 0
    assert _tags(els) == ["H3", "P", "P"]  # single heading left as-is


def test_normalize_heading_levels_real_aaup_sequence():
    # The actual structure-tree DFS heading sequence captured from the
    # AAUP_Contr1977_78 output (skips H1->H3 at positions 1 and 4).
    seq = (
        "H1 H3 H1 H1 H3 H2 H3 H4 H5 H3 H3 H3 H3 H3 H3 H3 H3 H4 H5 H5 H4 H4 "
        "H5 H5 H4 H5 H4 H4 H5 H5 H5 H5 H4 H4 H4 H4 H4 H4 H5 H4 H5 H5 H5 H5 "
        "H5 H5 H5 H5 H5 H5 H4 H5 H5 H1 H1 H1 H1 H2 H3 H4 H5 H5 H5 H5 H4 H4 "
        "H5 H5 H5 H5 H5 H4 H5 H5 H5 H4 H5 H5 H5 H5"
    ).split()
    els = _elems(*seq)
    changed = normalize_heading_levels(els)
    assert changed == 2
    levels = [int(t[1]) for t in _tags(els)]
    assert _is_valid_sequence(levels)


# ---------------------------------------------------------------------------
# close_html_heading_level_gaps — HTML-side counterpart of the JSON normalizer
# ---------------------------------------------------------------------------

def _html(*tags: str) -> "BeautifulSoup":
    from bs4 import BeautifulSoup

    body = "".join(f"<{t}>Heading {n}</{t}>" for n, t in enumerate(tags))
    return BeautifulSoup(f"<html><body>{body}</body></html>", "html.parser")


def _names(soup) -> list:
    return [h.name for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])]


def test_close_html_gaps_closes_a_gap_left_by_promotion():
    soup = _html("h1", "h3", "h3")  # what an h2->h1 retag leaves behind
    changed = close_html_heading_level_gaps(soup)
    assert _names(soup) == ["h1", "h2", "h2"]
    assert changed == 2


def test_close_html_gaps_is_noop_on_a_contiguous_ladder():
    soup = _html("h1", "h2", "h3")
    assert close_html_heading_level_gaps(soup) == 0
    assert _names(soup) == ["h1", "h2", "h3"]


def test_close_html_gaps_never_invents_an_h1():
    """Anchored at the shallowest level present, so a document whose
    promotion abstained keeps its h2s instead of having one renamed."""
    soup = _html("h2", "h3")
    assert close_html_heading_level_gaps(soup) == 0
    assert _names(soup) == ["h2", "h3"]


def test_close_html_gaps_preserves_relative_subordination():
    soup = _html("h1", "h3", "h4")
    close_html_heading_level_gaps(soup)
    assert _names(soup) == ["h1", "h2", "h3"]


def test_close_html_gaps_is_idempotent():
    soup = _html("h1", "h3", "h4")
    close_html_heading_level_gaps(soup)
    first = _names(soup)
    assert close_html_heading_level_gaps(soup) == 0
    assert _names(soup) == first


def test_close_html_gaps_noop_on_a_single_heading():
    soup = _html("h3")
    assert close_html_heading_level_gaps(soup) == 0
    assert _names(soup) == ["h3"]
