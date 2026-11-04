# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for output_gen/heading_detection.py.

Each rejection case below is a representative string for the gate it
exercises, so loosening a gate breaks a test that names the pattern it
would regress.

Run:  python -m pytest tests/test_heading_detection.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from output_gen.heading_detection import (
    is_usable_heading_text,
    normalize_text,
)


def test_normalize_collapses_whitespace():
    assert normalize_text("  BULK\xa0 MAILING\n SAMPLE ") == "BULK MAILING SAMPLE"


def test_normalize_applies_nfkc_compatibility_folding():
    """Pins NFKC, not just whitespace collapsing.

    ``\\s`` already matches NBSP, so an NBSP input cannot tell the two apart.
    Fullwidth forms and ligatures can: only NFKC maps them. Title matching
    compares a PDF-extracted string against a Docling title, and the two
    routes disagree on exactly these compatibility forms.
    """
    assert normalize_text("ＡＢ Ｒｅｐｏｒｔ") == "AB Report"
    assert normalize_text("ﬁle ﬂow") == "file flow"


def test_normalize_handles_none():
    assert normalize_text(None) == ""


def test_accepts_a_real_heading():
    # An all-caps mailing-permit heading that a checker flagged
    assert is_usable_heading_text("BULK MAILING SAMPLE PERMIT NUMBERS")


def test_accepts_a_mixed_case_heading():
    assert is_usable_heading_text("Student Learning Outcomes Assessment Report")


def test_rejects_empty_and_none():
    assert not is_usable_heading_text("")
    assert not is_usable_heading_text(None)
    assert not is_usable_heading_text("   ")


# ── policy: promote anything real; reject only junk ─────────────────────────

def test_accepts_a_single_word_title():
    assert is_usable_heading_text("Mammals")

def test_accepts_a_date_stamp():
    assert is_usable_heading_text("MAR 1 1 2010")
    assert is_usable_heading_text("7 April 2014")

def test_accepts_a_long_thesis_title():
    t = ("SAMPLE THESIS TITLE: COMPARING OUTCOME DATA BEFORE AND "
         "AFTER THE IMPLEMENTATION OF A NEW POLICY IN EXAMPLE STATE")
    assert is_usable_heading_text(t)

def test_accepts_a_trailing_colon_heading():
    assert is_usable_heading_text("Faculty Senate:")

def test_rejects_over_250_chars():
    assert not is_usable_heading_text("word " * 60)

def test_rejects_fill_in_blank_line():
    assert not is_usable_heading_text("Date: _____________________")

def test_rejects_pipeline_boilerplate():
    assert not is_usable_heading_text(
        "Document converted for accessibility compliance."
    )

def test_rejects_generic_heading_words():
    for w in ("Heading", "Title", "subtitle", "HEADER"):
        assert not is_usable_heading_text(w)

def test_rejects_gid_glyph_garbage():
    assert not is_usable_heading_text("/gid00005/gid00032/gid00043")
    assert not is_usable_heading_text("gid00014 gid00016 gid00015")

def test_rejects_replacement_chars():
    assert not is_usable_heading_text("Bud�et Calend�r")

def test_rejects_no_alpha_token():
    assert not is_usable_heading_text("12345 67 890")

def test_rejects_majority_pua():
    # 3 PUA chars + a 2-letter alpha token ("ab"): the alpha-token gate
    # passes (it only needs 2 consecutive letters), so this is rejected by
    # the PUA-majority arithmetic itself (non-space=5, pua*2=6 > 5), not by
    # the earlier alpha-token check.
    assert not is_usable_heading_text("ab")


def test_rejects_salutation():
    # Letter-style salutation from a scanned award letter
    assert not is_usable_heading_text("Dear Dr. Example,")
    # Isolate the _SALUTATION_RE gate (without trailing comma)
    assert not is_usable_heading_text("Dear Colleagues and Friends")


def test_rejects_filename_slug():
    # Single-token slug rejected by the underscore-slug gate
    assert not is_usable_heading_text("srcSamplePhotoForm_final")


def test_rejects_percent_encoded():
    # Old filename stems before the _title_from_filename fix; still possible from /Info
    assert not is_usable_heading_text("Office%20Expanded%20Compliance%20Matrix")


def test_rejects_multiword_percent_encoded():
    # Multi-word string with percent escape that is not standing in for a space
    # This exercises the _PERCENT_ESCAPE_RE gate directly (not via word-count)
    assert not is_usable_heading_text("Faculty Handbook %2F Appendix B")


def test_rejects_authoring_tool_artifact():
    # A sample audition-form title — reads as prose, passes every other gate
    assert not is_usable_heading_text("Microsoft Word - Sample Audition Form checklist.doc")


def test_rejects_trailing_file_extension():
    assert not is_usable_heading_text("Annual Assessment Report.pdf")


def test_rejects_fabricated_placeholder():
    """document_structure_remediation writes "Untitled Document" into <title>
    when a document has none, and DocumentTitleCheck runs before the heading
    strategies — so the placeholder is in place before they look. It clears
    every shape gate (two words, all alphabetic), hence the explicit set."""
    assert not is_usable_heading_text("Untitled Document")
    assert not is_usable_heading_text("untitled document")
    assert not is_usable_heading_text("  UNTITLED   DOCUMENT  ")


from output_gen.heading_detection import (
    detect_document_heading,
    promote_document_heading,
)


def _els(*texts, tag="P"):
    return [{"tag": tag, "page_no": 1, "text": t} for t in texts]


def test_promotes_on_title_match_anywhere_in_window():
    els = _els("MAR 1 1 2010", "Mammals", "Body text starts here.")
    d = detect_document_heading(els, title="Mammals")
    assert d == {"index": 1, "signal": "title-match", "text": "Mammals"}


def test_promotes_first_usable_text_without_title():
    els = _els("7 April 2014", "Committee agenda follows.")
    d = detect_document_heading(els, title="")
    assert d["signal"] == "first-text" and d["index"] == 0


def test_first_text_skips_junk_to_next_candidate():
    els = _els("/gid00005/gid00032", "Purple Loosestrife Summary")
    d = detect_document_heading(els, title="")
    assert d["index"] == 1


def test_kill_switch_disables_first_text_rung():
    els = _els("7 April 2014", "Committee agenda follows.")
    assert detect_document_heading(els, title="", promote_first_text=False) is None


def test_title_match_beats_first_text():
    els = _els("A letterhead line", "Annual Report 2024")
    d = detect_document_heading(els, title="Annual Report 2024")
    assert d["signal"] == "title-match" and d["index"] == 1


def test_abstains_when_document_already_has_headings():
    els = [{"tag": "H2", "page_no": 1, "text": "Existing"},
           {"tag": "P", "page_no": 1, "text": "Body"}]
    assert detect_document_heading(els, title="Existing") is None


def test_candidates_limited_to_window():
    els = _els(*["/gid00001x"] * 5, "Real Title")  # 5 junk fill the window
    assert detect_document_heading(els, title="Real Title") is None


def test_promote_document_heading_retags_in_place():
    els = _els("Mammals", "Body.")
    rec = promote_document_heading(els, title="Mammals")
    assert rec["signal"] == "title-match"
    assert els[0]["tag"] == "H1" and els[0]["level"] == 1
    assert els[0]["type"] == "SectionHeaderItem"


def test_promote_document_heading_kill_switch():
    els = _els("Mammals", "Body.")
    assert promote_document_heading(els, title="Mammals", enabled=False) is None
    assert els[0]["tag"] == "P"
