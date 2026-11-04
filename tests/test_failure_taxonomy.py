# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for logging_util.failure_taxonomy.

Pure-function tests: build a synthetic outcome record, assert which
failure-reason codes classify_failure_reasons() emits. No I/O.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logging_util.failure_taxonomy import (  # noqa: E402
    FailureReason,
    classify_failure_reasons,
    summarize_post_audit_issue_types,
    VALIDATOR_CHECK_TO_REASON,
    POST_AUDIT_TYPE_TO_REASON,
    GATE_TO_REASON,
)


def _codes(record):
    return [r["code"] for r in classify_failure_reasons(record)]


# ── clean pass ──────────────────────────────────────────────────────────────

def test_clean_pass_returns_none_sentinel():
    record = {"status": "passed", "verdict": "pass", "phase_status": {}}
    reasons = classify_failure_reasons(record)
    assert [r["code"] for r in reasons] == [FailureReason.none.value]
    assert reasons[0]["severity"] == "info"


# ── phase / error reasons ───────────────────────────────────────────────────

def test_docling_phase_failure():
    record = {
        "status": "error", "verdict": None,
        "phase_status": {"docling": {"status": "failed", "error": "boom"}},
    }
    assert FailureReason.docling_conversion_failed.value in _codes(record)


def test_pre_audit_phase_failure():
    record = {"status": "error", "verdict": None,
              "phase_status": {"wcag_pre_audit": {"status": "failed", "error": "x"}}}
    assert FailureReason.pre_audit_failed.value in _codes(record)


def test_remediation_phase_failure():
    record = {"status": "error", "verdict": None,
              "phase_status": {"remediation": {"status": "failed", "error": "x"}}}
    assert FailureReason.remediation_failed.value in _codes(record)


def test_post_audit_phase_failure():
    record = {"status": "error", "verdict": None,
              "phase_status": {"wcag_post_audit": {"status": "failed", "error": "x"}}}
    assert FailureReason.post_audit_failed.value in _codes(record)


def test_tagger_crash_from_status():
    record = {"status": "tagger_failed", "verdict": "needs_review", "phase_status": {}}
    assert FailureReason.tagger_crashed.value in _codes(record)


def test_phase_recorded_reason_is_preferred():
    # phase_guard may stamp a specific taxonomy reason on the phase entry
    record = {"status": "error", "verdict": None, "phase_status": {
        "tagging": {"status": "failed", "error": "oom", "reason": "oom_or_memory_error"}}}
    assert FailureReason.oom_or_memory_error.value in _codes(record)


def test_encrypted_skip():
    record = {"status": "skipped", "verdict": None,
              "error": "Encrypted PDF (user password required)", "phase_status": {}}
    assert FailureReason.input_encrypted_user_password.value in _codes(record)


def test_not_a_pdf_skip():
    # An HTML/error-page file with a .pdf extension (web-scrape artifact).
    record = {"status": "skipped", "verdict": None,
              "error": "Not a valid PDF file (bad magic bytes): 502.pdf",
              "phase_status": {}}
    codes = _codes(record)
    assert FailureReason.input_not_a_pdf.value in codes
    assert FailureReason.unknown_error.value not in codes


# ── screen-reader-derived reasons (only when not a clean pass) ───────────────

def test_hollow_pages():
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "sr_quality": {"hollow_pages": [2, 5]}}
    reasons = classify_failure_reasons(record)
    hp = [r for r in reasons if r["code"] == FailureReason.hollow_pages_unrecovered.value]
    assert hp, "expected hollow_pages_unrecovered"
    assert "2" in hp[0]["detail"] and "5" in hp[0]["detail"]


def test_figures_missing_alt():
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "sr_quality": {"figures_total": 10, "figures_missing_alt": 3}}
    assert FailureReason.figures_missing_alt.value in _codes(record)


def test_low_coverage_below_threshold():
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "sr_quality": {"avg_coverage": 0.40}}
    assert FailureReason.text_coverage_low.value in _codes(record)


def test_ocr_quality_poor():
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "sr_quality": {"scanned_pages": [1, 2, 3], "ocr_pages_passing_pct": 0.5}}
    assert FailureReason.ocr_quality_poor.value in _codes(record)


def test_top_level_coverage_and_hollow_fallback_for_manifest_records():
    # manifest records carry avg_coverage/hollow_pages at top level (no sr_quality dict)
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "avg_coverage": 0.45, "hollow_pages": [3, 9]}
    codes = _codes(record)
    assert FailureReason.text_coverage_low.value in codes
    assert FailureReason.hollow_pages_unrecovered.value in codes


def test_passing_doc_does_not_emit_sr_deficiencies():
    # A clean pass must never surface coverage/hollow noise.
    record = {"status": "passed", "verdict": "pass", "phase_status": {},
              "sr_quality": {"avg_coverage": 0.40, "hollow_pages": [2]}}
    assert _codes(record) == [FailureReason.none.value]


# ── element match rate (EMR gate) ────────────────────────────────────────────

def test_element_match_rate_low_below_floor():
    # EMR below the corroboration floor (70) is flagged even with high coverage —
    # that band signals genuine structural collapse, not benign attribution noise.
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "sr_quality": {"avg_coverage": 1.0, "element_match_rate": 50.0,
                             "total_pages": 1, "scanned_pages": []}}
    codes = _codes(record)
    assert FailureReason.element_match_rate_low.value in codes
    assert FailureReason.unknown_error.value not in codes


def test_element_match_rate_corroborated_suppressed():
    # EMR in 70-85 with corroborating coverage (>= min_coverage) is benign
    # attribution noise — suppressed, mirroring compute_wcag_verdict's emr_ok.
    record = {"status": "passed", "verdict": "pass", "phase_status": {},
              "sr_quality": {"avg_coverage": 1.0, "element_match_rate": 81.8,
                             "total_pages": 1, "scanned_pages": []}}
    assert FailureReason.element_match_rate_low.value not in _codes(record)


def test_element_match_rate_low_when_coverage_below_min():
    # EMR in 70-85 but coverage below min → not corroborated → still flagged.
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "sr_quality": {"avg_coverage": 0.50, "element_match_rate": 81.8,
                             "total_pages": 1, "scanned_pages": []}}
    assert FailureReason.element_match_rate_low.value in _codes(record)


def test_element_match_rate_not_flagged_when_all_scanned():
    # No native pages → EMR gate is skipped in compute_wcag_verdict; the taxonomy
    # must mirror that and not flag element_match_rate_low.
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "sr_quality": {"avg_coverage": 1.0, "element_match_rate": 0.0,
                             "total_pages": 2, "scanned_pages": [1, 2]}}
    assert FailureReason.element_match_rate_low.value not in _codes(record)


def test_element_match_rate_low_top_level_fallback():
    # Older manifest records carry element_match_rate at the top level (no
    # sr_quality dict) — mirror the avg_coverage/hollow_pages fallback.
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "element_match_rate": 70.0}
    assert FailureReason.element_match_rate_low.value in _codes(record)


def test_element_match_rate_ok_not_flagged():
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "sr_quality": {"element_match_rate": 95.0, "total_pages": 3,
                             "scanned_pages": []}}
    assert FailureReason.element_match_rate_low.value not in _codes(record)


# ── wcag score ──────────────────────────────────────────────────────────────

def test_wcag_score_below_threshold():
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "wcag_post_score": 85.0}
    assert FailureReason.wcag_score_below_threshold.value in _codes(record)


# ── mapping tables ──────────────────────────────────────────────────────────

def test_validator_check_mapping():
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "struct_tree_issue_checks": {"low-text-coverage": 1, "blank-leaf-element": 2,
                                           "all-mcr-invisible": 1}}
    codes = _codes(record)
    assert FailureReason.text_coverage_low.value in codes
    assert FailureReason.struct_tree_blank_elements.value in codes
    assert FailureReason.mcr_all_invisible.value in codes


def test_post_audit_type_mapping():
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "post_audit_issue_types": {"pdf-not-marked": 1, "pdf-hollow-pages": 1}}
    codes = _codes(record)
    assert FailureReason.pdf_not_marked.value in codes
    assert FailureReason.hollow_pages_unrecovered.value in codes


def test_gate_mapping_from_failed_gates_list():
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "failed_gates": ["visual_diff"]}
    assert FailureReason.gate_failed_visual_diff.value in _codes(record)


def test_gate_mapping_from_gates_dict():
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "gates": {"all_passed": False, "gates": [
                  {"gate": "screen_reader_sim", "passed": False},
                  {"gate": "visual_diff", "passed": True}]}}
    codes = _codes(record)
    assert FailureReason.gate_failed_screen_reader_sim.value in codes
    assert FailureReason.gate_failed_visual_diff.value not in codes


# ── font / tounicode ────────────────────────────────────────────────────────

def test_font_substituted_lossy():
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "font_substitution": {"summary": {"substituted": 1, "with_residual_missing": 1}}}
    assert FailureReason.font_substituted_lossy.value in _codes(record)


def test_tounicode_unmappable():
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "source_quality_warnings": {"tounicode_cmaps_failed": 2}}
    assert FailureReason.tounicode_unmappable.value in _codes(record)


# ── infra ───────────────────────────────────────────────────────────────────

def test_oom_from_error_detail():
    record = {"status": "error", "verdict": None, "phase_status": {},
              "error_detail": {"type": "MemoryError", "phase": "tagging"}}
    assert FailureReason.oom_or_memory_error.value in _codes(record)


def test_unknown_error_fallback():
    record = {"status": "error", "verdict": None, "phase_status": {},
              "error": "something weird", "error_detail": {"type": "RuntimeError"}}
    assert FailureReason.unknown_error.value in _codes(record)


def test_unmapped_post_audit_type_becomes_unknown_with_raw_code():
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "post_audit_issue_types": {"pdf-some-future-check": 1}}
    reasons = classify_failure_reasons(record)
    unk = [r for r in reasons if r["code"] == FailureReason.unknown_error.value]
    assert unk and "pdf-some-future-check" in unk[0]["detail"]


# ── drift guards / purity ───────────────────────────────────────────────────

def test_lookup_tables_cover_all_known_codes():
    valid = {r.value for r in FailureReason}
    for table in (VALIDATOR_CHECK_TO_REASON, POST_AUDIT_TYPE_TO_REASON, GATE_TO_REASON):
        for src, code in table.items():
            val = code.value if isinstance(code, FailureReason) else code
            assert val in valid, f"{src} maps to unknown reason {code}"


def test_classify_is_pure_no_io(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("classify must not perform I/O")
    monkeypatch.setattr("builtins.open", _boom)
    record = {"status": "gates_failed", "verdict": "needs_review", "phase_status": {},
              "sr_quality": {"hollow_pages": [1]}}
    assert FailureReason.hollow_pages_unrecovered.value in _codes(record)


def test_summarize_post_audit_issue_types_counts_needs_remediation_only():
    post_audit = {"issues": [
        {"type": "missing-alt-text", "remediation_status": "needs_remediation"},
        {"type": "missing-alt-text", "remediation_status": "needs_remediation"},
        {"type": "pdf-not-marked", "remediation_status": "needs_remediation"},
        {"type": "heading-order", "remediation_status": "auto_remediated"},  # resolved → skip
    ]}
    counts = summarize_post_audit_issue_types(post_audit)
    assert counts == {"missing-alt-text": 2, "pdf-not-marked": 1}


def test_summarize_post_audit_advisories_counts_informational_only():
    from logging_util.failure_taxonomy import summarize_post_audit_advisories
    post = {"issues": [
        {"type": "no-h1", "remediation_status": "informational"},
        {"type": "no-h1", "remediation_status": "needs_remediation"},
        {"type": "flat-heading-nesting", "remediation_status": "informational"},
    ]}
    assert summarize_post_audit_advisories(post) == {
        "no-h1": 1, "flat-heading-nesting": 1,
    }
    assert summarize_post_audit_advisories(None) == {}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ── reading-order gate ────────────────────────────────────────────────────────

def test_reading_order_low_fires_below_threshold():
    record = {"status": "needs_review",
              "sr_quality": {"reading_order_min_tau": 0.30,
                             "reading_order_failing_pages": [3, 7]}}
    assert FailureReason.reading_order_low.value in _codes(record)


def test_reading_order_none_does_not_fire():
    # No eligible page scored (min_tau None) → gate skipped, no reason.
    record = {"status": "passed",
              "sr_quality": {"reading_order_min_tau": None}}
    assert FailureReason.reading_order_low.value not in _codes(record)


def test_reading_order_good_does_not_fire():
    record = {"status": "passed",
              "sr_quality": {"reading_order_min_tau": 0.95}}
    assert FailureReason.reading_order_low.value not in _codes(record)


# ── docling structure-yield tripwire ────────────────────────────────────────
#
# Gates check structure validity, never yield. A fully_scanned document for
# which Docling produced almost no elements (vectorized-scan class: no text,
# no bitmaps -> OCR silently skipped) must flag loudly even when everything
# downstream "passed". Validated against 645 historical conversions: fires on
# exactly one (Essentials of Psychiatric Diagnosis, 33 elements / 241 pages).

def _yield_record(**over):
    rec = {
        "status": "needs_review", "verdict": "needs_review", "phase_status": {},
        "text_classification": "fully_scanned",
        "docling_elements": 33, "docling_pages": 241,
        "docling_pages_with_text": 0,
    }
    rec.update(over)
    return rec


def test_docling_yield_low_fires_on_mostly_textless_mixed():
    """A vector scan plus one texty cover page classifies 'mixed' — the
    tripwire must not be disarmed by that single page (validated: 0 false
    positives over 645 historical conversions)."""
    reasons = classify_failure_reasons(
        _yield_record(status="passed", verdict="pass",
                      text_classification="mixed",
                      docling_elements=34, docling_pages_with_text=1))
    codes = [r["code"] for r in reasons]
    assert FailureReason.docling_yield_low.value in codes


def test_docling_yield_balanced_mixed_is_silent():
    reasons = classify_failure_reasons(
        _yield_record(status="passed", verdict="pass",
                      text_classification="mixed",
                      docling_elements=3, docling_pages=20,
                      docling_pages_with_text=10))
    assert [r["code"] for r in reasons] == [FailureReason.none.value]


def test_docling_yield_low_fires_on_vector_scan_profile():
    reasons = classify_failure_reasons(_yield_record())
    codes = [r["code"] for r in reasons]
    assert FailureReason.docling_yield_low.value in codes


def test_docling_yield_low_overrides_clean_pass():
    """The tripwire exists for what the rest of the system misses."""
    reasons = classify_failure_reasons(
        _yield_record(status="passed", verdict="pass"))
    codes = [r["code"] for r in reasons]
    assert FailureReason.docling_yield_low.value in codes
    assert FailureReason.none.value not in codes


def test_docling_yield_healthy_scan_is_silent():
    reasons = classify_failure_reasons(
        _yield_record(status="passed", verdict="pass",
                      docling_elements=120, docling_pages=25))
    assert [r["code"] for r in reasons] == [FailureReason.none.value]


def test_docling_yield_text_only_is_silent():
    reasons = classify_failure_reasons(
        _yield_record(status="passed", verdict="pass",
                      text_classification="text_only",
                      docling_elements=2, docling_pages=10))
    assert [r["code"] for r in reasons] == [FailureReason.none.value]


def test_docling_yield_tiny_doc_is_exempt():
    """pages >= 3 required — a 2-page near-empty scan is not the failure class."""
    reasons = classify_failure_reasons(
        _yield_record(status="passed", verdict="pass",
                      docling_elements=1, docling_pages=2))
    assert [r["code"] for r in reasons] == [FailureReason.none.value]


def test_docling_yield_missing_fields_is_silent():
    rec = {"status": "passed", "verdict": "pass", "phase_status": {}}
    reasons = classify_failure_reasons(rec)
    assert [r["code"] for r in reasons] == [FailureReason.none.value]


def test_structure_yield_gate_maps_to_reason():
    rec = _yield_record(docling_elements=None, docling_pages=None,
                        failed_gates=["structure_yield"])
    codes = [r["code"] for r in classify_failure_reasons(rec)]
    assert FailureReason.docling_yield_low.value in codes
