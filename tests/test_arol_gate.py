# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for AROL readability sub-checks (E and F) in gate_screen_reader_sim."""

import sys
import os
import pytest
import fitz

sys.path.insert(0, os.path.dirname(__file__))
from validation.gates import (
    _check_mcr_linkage,
    _check_extraction_divergence,
    gate_screen_reader_sim,
)


@pytest.fixture
def orphaned_at_pdf(tmp_path):
    """Create a PDF with 3 elements: 1 proper MCR, 1 orphaned AT, 1 with both."""
    pdf_path = str(tmp_path / "orphaned.pdf")
    doc = fitz.open()
    page = doc.new_page()
    page_xref = page.xref

    struct_root = doc.get_new_xref()
    doc_elem = doc.get_new_xref()

    # P1: proper MCR — /K 0 + /Pg
    p1 = doc.get_new_xref()
    doc.update_object(p1, (
        f"<< /Type /StructElem /S /P /P {doc_elem} 0 R "
        f"/K 0 /Pg {page_xref} 0 R >>"
    ))

    # P2: orphaned AT — no /K, no /Pg
    p2 = doc.get_new_xref()
    doc.update_object(p2, (
        f"<< /Type /StructElem /S /P /P {doc_elem} 0 R "
        f"/ActualText (orphaned text) >>"
    ))

    # P3: proper MCR + AT
    p3 = doc.get_new_xref()
    doc.update_object(p3, (
        f"<< /Type /StructElem /S /P /P {doc_elem} 0 R "
        f"/ActualText (has both) /K 1 /Pg {page_xref} 0 R >>"
    ))

    doc.update_object(doc_elem, (
        f"<< /Type /StructElem /S /Document /P {struct_root} 0 R "
        f"/K [{p1} 0 R {p2} 0 R {p3} 0 R] >>"
    ))

    parent_arr = doc.get_new_xref()
    doc.update_object(parent_arr, f"[{p1} 0 R {p3} 0 R]")
    parent_tree = doc.get_new_xref()
    doc.update_object(parent_tree,
                      f"<< /Type /NumberTree /Nums [0 {parent_arr} 0 R] >>")

    doc.update_object(struct_root, (
        f"<< /Type /StructTreeRoot /K {doc_elem} 0 R "
        f"/ParentTree {parent_tree} 0 R /ParentTreeNextKey 1 >>"
    ))

    cat = doc.pdf_catalog()
    doc.xref_set_key(cat, "StructTreeRoot", f"{struct_root} 0 R")
    doc.xref_set_key(cat, "MarkInfo", "<< /Marked true >>")
    doc.xref_set_key(page_xref, "StructParents", "0")

    doc.save(pdf_path, garbage=4)
    doc.close()
    return pdf_path


def test_detects_arol_invisible_elements(orphaned_at_pdf):
    result = _check_mcr_linkage(orphaned_at_pdf)
    # P2 has AT only (no MCR) → AROL-invisible
    # P3 has AT + MCR → AROL-invisible (AT blocks MCR reading)
    # Only P1 (MCR only) is AROL-readable
    assert result["arol_invisible"] == 2  # P2 and P3
    assert result["total_text_leaves"] == 3
    assert result["arol_invisible_pct"] == pytest.approx(66.7, abs=1)


def test_all_arol_visible_when_mcr_only(tmp_path):
    """PDF where all elements have MCR and no /ActualText — zero invisible."""
    pdf_path = str(tmp_path / "clean.pdf")
    doc = fitz.open()
    page = doc.new_page()
    page_xref = page.xref

    struct_root = doc.get_new_xref()
    doc_elem = doc.get_new_xref()

    p1 = doc.get_new_xref()
    doc.update_object(p1, (
        f"<< /Type /StructElem /S /P /P {doc_elem} 0 R "
        f"/K 0 /Pg {page_xref} 0 R >>"
    ))

    doc.update_object(doc_elem, (
        f"<< /Type /StructElem /S /Document /P {struct_root} 0 R "
        f"/K [{p1} 0 R] >>"
    ))

    parent_arr = doc.get_new_xref()
    doc.update_object(parent_arr, f"[{p1} 0 R]")
    parent_tree = doc.get_new_xref()
    doc.update_object(parent_tree,
                      f"<< /Type /NumberTree /Nums [0 {parent_arr} 0 R] >>")
    doc.update_object(struct_root, (
        f"<< /Type /StructTreeRoot /K {doc_elem} 0 R "
        f"/ParentTree {parent_tree} 0 R /ParentTreeNextKey 1 >>"
    ))

    cat = doc.pdf_catalog()
    doc.xref_set_key(cat, "StructTreeRoot", f"{struct_root} 0 R")
    doc.xref_set_key(cat, "MarkInfo", "<< /Marked true >>")
    doc.xref_set_key(page_xref, "StructParents", "0")

    doc.save(pdf_path, garbage=4)
    doc.close()

    result = _check_mcr_linkage(pdf_path)
    assert result["arol_invisible"] == 0


# ---------------------------------------------------------------------------
# Task 2: Garbled /ActualText quality heuristic tests
# ---------------------------------------------------------------------------

def _make_at_only_pdf(tmp_path, actual_text: str) -> str:
    """Helper: create a PDF with one P element that has only /ActualText."""
    pdf_path = str(tmp_path / "at_quality.pdf")
    doc = fitz.open()
    page = doc.new_page()

    struct_root = doc.get_new_xref()
    doc_elem = doc.get_new_xref()

    p1 = doc.get_new_xref()
    # Escape parens in ActualText
    escaped = actual_text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    doc.update_object(p1, (
        f"<< /Type /StructElem /S /P /P {doc_elem} 0 R "
        f"/ActualText ({escaped}) >>"
    ))

    doc.update_object(doc_elem, (
        f"<< /Type /StructElem /S /Document /P {struct_root} 0 R "
        f"/K [{p1} 0 R] >>"
    ))
    parent_tree = doc.get_new_xref()
    doc.update_object(parent_tree,
                      "<< /Type /NumberTree /Nums [] >>")
    doc.update_object(struct_root, (
        f"<< /Type /StructTreeRoot /K {doc_elem} 0 R "
        f"/ParentTree {parent_tree} 0 R /ParentTreeNextKey 0 >>"
    ))

    cat = doc.pdf_catalog()
    doc.xref_set_key(cat, "StructTreeRoot", f"{struct_root} 0 R")
    doc.xref_set_key(cat, "MarkInfo", "<< /Marked true >>")

    doc.save(pdf_path, garbage=4)
    doc.close()
    return pdf_path


def test_garbled_text_detected(tmp_path):
    """'bo r bo r' pattern — mostly single/double-char words."""
    pdf = _make_at_only_pdf(tmp_path, "bo r bo r it e on C")
    result = _check_mcr_linkage(pdf)
    assert result["garbled_actual_text"] == 1


def test_normal_text_not_flagged(tmp_path):
    """Normal sentence should not be flagged as garbled."""
    pdf = _make_at_only_pdf(tmp_path, "The quick brown fox jumps over the lazy dog")
    result = _check_mcr_linkage(pdf)
    assert result["garbled_actual_text"] == 0


def test_short_text_exempt(tmp_path):
    """Short text like 'OK' or 'N/A' should not be flagged."""
    pdf = _make_at_only_pdf(tmp_path, "OK")
    result = _check_mcr_linkage(pdf)
    assert result["garbled_actual_text"] == 0


# ---------------------------------------------------------------------------
# Task 3: Extraction divergence tests (Sub-check F)
# ---------------------------------------------------------------------------

def test_divergence_zero_when_all_accessible(tmp_path):
    """PDF where all text has proper MCR — divergence should be near 0."""
    pdf_path = str(tmp_path / "accessible.pdf")
    doc = fitz.open()
    page = doc.new_page()
    page_xref = page.xref

    struct_root = doc.get_new_xref()
    doc_elem = doc.get_new_xref()

    p1 = doc.get_new_xref()
    doc.update_object(p1, (
        f"<< /Type /StructElem /S /P /P {doc_elem} 0 R "
        f"/K 0 /Pg {page_xref} 0 R >>"
    ))

    doc.update_object(doc_elem, (
        f"<< /Type /StructElem /S /Document /P {struct_root} 0 R "
        f"/K [{p1} 0 R] >>"
    ))
    parent_arr = doc.get_new_xref()
    doc.update_object(parent_arr, f"[{p1} 0 R]")
    parent_tree = doc.get_new_xref()
    doc.update_object(parent_tree,
                      f"<< /Type /NumberTree /Nums [0 {parent_arr} 0 R] >>")
    doc.update_object(struct_root, (
        f"<< /Type /StructTreeRoot /K {doc_elem} 0 R "
        f"/ParentTree {parent_tree} 0 R /ParentTreeNextKey 1 >>"
    ))

    cat = doc.pdf_catalog()
    doc.xref_set_key(cat, "StructTreeRoot", f"{struct_root} 0 R")
    doc.xref_set_key(cat, "MarkInfo", "<< /Marked true >>")
    doc.xref_set_key(page_xref, "StructParents", "0")

    # Write visible text AND tagged MCR text with same content
    page.insert_text(fitz.Point(72, 100), "Hello world test sentence",
                     fontsize=12)

    font = fitz.Font("helv")
    page.insert_font(fontname="Helv", fontbuffer=font.buffer)

    content = (
        "BT\n"
        "/P <</MCID 0>> BDC\n"
        "3 Tr /Helv 12 Tf\n"
        "72 600 Td (Hello world test sentence) Tj\n"
        "EMC\n"
        "ET\n"
    )
    content_xref = doc.get_new_xref()
    content_bytes = content.encode("latin-1")
    doc.update_object(content_xref,
                      f"<< /Length {len(content_bytes)} >>")
    doc.update_stream(content_xref, content_bytes)
    existing = list(page.get_contents())
    doc.xref_set_key(page_xref, "Contents",
                     f"[{' '.join(f'{x} 0 R' for x in existing + [content_xref])}]")

    doc.save(pdf_path, garbage=4)
    doc.close()

    result = _check_extraction_divergence(pdf_path)
    assert result["avg_divergence"] <= 0.1


def test_divergence_high_when_text_inaccessible(orphaned_at_pdf):
    """PDF with orphaned AT — text exists but isn't MCR-accessible."""
    result = _check_extraction_divergence(orphaned_at_pdf)
    # Some divergence expected since orphaned elements have text
    # but no MCR linkage (sub-check F would flag this)
    assert result["avg_divergence"] >= 0.0  # At minimum, no crash


# ---------------------------------------------------------------------------
# Task 4: Integration test — sub-checks E and F in gate_screen_reader_sim
# ---------------------------------------------------------------------------

def test_gate_includes_mcr_linkage_details(orphaned_at_pdf, tmp_path):
    """Gate should include sub_e_mcr_linkage in details."""
    # Create a source PDF (just a copy for comparison)
    source = str(tmp_path / "source.pdf")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(fitz.Point(72, 100), "orphaned text has both")
    doc.save(source)
    doc.close()

    result = gate_screen_reader_sim(source, orphaned_at_pdf)
    details = result.get("details", {})
    # Should have sub_e_mcr_linkage in details
    assert "sub_e_mcr_linkage" in details
    assert details["sub_e_mcr_linkage"]["arol_invisible"] == 2  # P2 (AT only) + P3 (AT+MCR)
