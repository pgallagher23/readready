# SPDX-License-Identifier: AGPL-3.0-or-later
"""Force full-page OCR for fully_scanned documents.

A vectorized scan (glyphs drawn as filled paths — no bitmaps, no text) is
invisible to both of Docling's OCR triggers (programmatic text, bitmap
coverage), so OCR silently never runs and the document loses all structure.
`_classify_pdf_text` already detects the class as ``fully_scanned``; these
tests pin the wiring that turns that classification into
``force_full_page_ocr=True`` on the Docling converter.

Regression source: a 241-page vector scan of a book — 33 Docling elements,
zero headings, a third-party checker score of 54%.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from converter.docling_converter import (
    _build_converter,
    _should_force_full_page_ocr,
)


# ── decision function ────────────────────────────────────────────────

def _classification(rec="fully_scanned", chars_per_page=(0, 0)):
    return {"recommendation": rec,
            "text_per_page": {i + 1: c for i, c in enumerate(chars_per_page)},
            "total_pages": len(chars_per_page)}


class TestShouldForceFullPageOcr:
    def test_fully_scanned_forces_by_default(self):
        assert _should_force_full_page_ocr(_classification(), {}, do_ocr=True) is True

    def test_config_kill_switch_wins(self):
        opts = {"force_ocr_scanned": False}
        assert _should_force_full_page_ocr(_classification(), opts, do_ocr=True) is False

    def test_explicit_config_true_forces(self):
        opts = {"force_ocr_scanned": True}
        assert _should_force_full_page_ocr(_classification(), opts, do_ocr=True) is True

    def test_ocr_disabled_never_forces(self):
        assert _should_force_full_page_ocr(_classification(), {}, do_ocr=False) is False

    def test_text_only_never_forces(self):
        assert _should_force_full_page_ocr(
            _classification("text_only"), {}, do_ocr=True) is False

    def test_mixed_never_forces(self):
        """Forcing on mixed docs would DROP their real text cells."""
        assert _should_force_full_page_ocr(
            _classification("mixed"), {}, do_ocr=True) is False


# ── converter wiring ─────────────────────────────────────────────────

def _pdf_pipeline_options(converter):
    from docling.datamodel.base_models import InputFormat
    return converter.format_to_options[InputFormat.PDF].pipeline_options


class TestBuildConverterWiring:
    def test_force_flag_reaches_ocr_options(self):
        conv = _build_converter({}, do_ocr=True, force_full_page_ocr=True)
        assert _pdf_pipeline_options(conv).ocr_options.force_full_page_ocr is True

    def test_default_is_not_forced(self):
        conv = _build_converter({}, do_ocr=True)
        assert _pdf_pipeline_options(conv).ocr_options.force_full_page_ocr is False


class TestSparseTextGuard:
    """A doc whose every page has a little REAL text (<=50 chars, so it still
    classifies fully_scanned) must NOT be forced: under force Docling drops
    programmatic text cells, replacing correct text with OCR."""

    def _cls(self, total_chars, rec="fully_scanned"):
        return _classification(rec, (total_chars // 2, total_chars - total_chars // 2))

    def test_zero_text_doc_is_forced(self):
        assert _should_force_full_page_ocr(self._cls(0), {}, do_ocr=True) is True

    def test_sparse_real_text_blocks_forcing(self):
        # e.g. a caption deck: 45 chars/page — classified fully_scanned but
        # carries genuine text that force would destroy
        assert _should_force_full_page_ocr(self._cls(900), {}, do_ocr=True) is False

    def test_trace_text_still_forces(self):
        # a few junk chars across the whole doc is still "no text"
        assert _should_force_full_page_ocr(self._cls(60), {}, do_ocr=True) is True
