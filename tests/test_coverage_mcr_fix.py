# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the MCR-aware text-coverage fix in
``output_gen/struct_tree_validator._check_text_coverage``.

Content-preserving tagging carries body text in marked-content references
(MCR), not ``/ActualText``.  The coverage metric must not flag such a PDF as
"low text coverage" just because the body words live in MCR rather than
``/ActualText`` — especially on form-heavy PDFs where AcroForm ``Form``
elements carry ``/ActualText`` field labels that used to defeat the
``get_text()`` rescue.

See plan: false "low text coverage" on content-preserving (MCR) tagged PDFs.
"""

import os
import sys

import fitz  # PyMuPDF
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from output_gen.struct_tree_validator import _check_text_coverage  # noqa: E402

# 30 distinct, easy-to-tokenize body words — what a screen reader should read.
BODY_TEXT = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet "
    "kilo lima mike november oscar papa quebec romeo sierra tango "
    "uniform victor whiskey xray yankee zulu apple banana cherry plum"
)

# AcroForm field labels — the /ActualText that used to defeat the rescue.
FORM_LABELS = [
    "Text field: Year", "Checkbox: Spring", "Text field: Name",
    "Text field: Instrument", "Checkbox: Freshman", "Text field: Instructor",
]


def _make_pdf(path: str, text: str) -> None:
    """Render ``text`` onto a single page so get_text() returns it."""
    doc = fitz.open()
    page = doc.new_page()
    rect = fitz.Rect(36, 36, page.rect.width - 36, page.rect.height - 36)
    page.insert_textbox(rect, text, fontsize=11)
    doc.save(path)
    doc.close()


def _mcr_leaf(xref: int, tag: str = "P") -> dict:
    """A content-preserving body leaf: MCR content, no /ActualText."""
    return {"tag": tag, "text": None, "has_mcr": True, "has_objr": False,
            "children": 0, "depth": 2, "path": f"/Document/{tag}", "xref": xref}


def _label_leaf(xref: int, tag: str, text: str) -> dict:
    """A /ActualText-bearing label leaf (Form field caption / Figure alt)."""
    return {"tag": tag, "text": text, "has_mcr": False, "has_objr": False,
            "children": 0, "depth": 2, "path": f"/Document/{tag}", "xref": xref}


def _checks(issues):
    return {i["check"] for i in issues}


def test_mcr_body_with_form_labels_not_flagged_low(tmp_path):
    """Body in MCR + many Form-field /ActualText labels must NOT read as low
    coverage — the form labels must not defeat the get_text() rescue."""
    src = str(tmp_path / "source.pdf")
    tag = str(tmp_path / "tagged.pdf")
    _make_pdf(src, BODY_TEXT)
    _make_pdf(tag, BODY_TEXT)  # tagged page renders the same visible body text

    elems = [_mcr_leaf(100 + i) for i in range(10)]          # 10 MCR body <P>
    elems += [_label_leaf(200 + i, "Form", lbl)              # 6 form-field labels
              for i, lbl in enumerate(FORM_LABELS)]
    elems.append(_label_leaf(300, "Figure", "Example University logo"))

    tdoc = fitz.open(tag)
    try:
        issues = _check_text_coverage(tdoc, src, elems, warn_threshold=80)
    finally:
        tdoc.close()

    checks = _checks(issues)
    assert "low-text-coverage" not in checks, (
        f"false positive: body text present via MCR but flagged low; issues={issues}")
    assert "moderate-text-coverage" not in checks, (
        f"body fully present but flagged moderate; issues={issues}")
    assert "text-coverage" in checks, (
        f"expected info-level full-coverage result; issues={issues}")


def test_mcr_body_genuinely_missing_still_flagged(tmp_path):
    """Guard: when the body text is genuinely absent from the tagged PDF, the
    MCR-aware fallback must still report low coverage (no blanket suppression)."""
    src = str(tmp_path / "source.pdf")
    tag = str(tmp_path / "tagged.pdf")
    _make_pdf(src, BODY_TEXT)
    _make_pdf(tag, "lorem ipsum dolor sit amet consectetur")  # unrelated → body lost

    elems = [_mcr_leaf(100 + i) for i in range(10)]  # MCR leaves, no /ActualText

    tdoc = fitz.open(tag)
    try:
        issues = _check_text_coverage(tdoc, src, elems, warn_threshold=80)
    finally:
        tdoc.close()

    assert "low-text-coverage" in _checks(issues), (
        f"genuine coverage gap must still be flagged; issues={issues}")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
