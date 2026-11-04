# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run-match corroboration for the EMR verdict gate (native-only).

Regression for low-EMR / high-coverage false positives. A *fully-native*
document whose element_match_rate is below the coverage floor (70) should still
pass when text coverage AND a high share of content-stream runs both corroborate
that the content is present and attributed — the low EMR is benign Docling
over-segmentation, not missing content (e.g. a document with 305/305 runs matched, 0
unmatched runs, yet 115/251 elements counted "unmatched"; the unmatched text was
verified present in the output PDF).

Scanned/OCR-dominated docs (Population B, e.g. 13/14 pages scanned) must
NOT be rescued: EMR is the wrong signal there and "100% coverage" is unreliable.
"""

import pytest

from audit.standards import (
    VERDICT_DEFAULTS,
    compute_wcag_verdict,
    emr_passes,
)
from logging_util.failure_taxonomy import classify_failure_reasons

# Canonical thresholds (mirror VERDICT_DEFAULTS so the tests pin the contract).
HARD = 85          # min_element_match_rate
FLOOR = 70         # min_element_match_rate_when_covered
RUN_FLOOR = 80     # min_run_match_rate_when_covered
MIN_COV = 0.80     # min_coverage


def _emr(emr, *, cov, run=None, scanned=False, native=True):
    return emr_passes(
        emr,
        avg_coverage=cov,
        has_native_pages=native,
        has_scanned_pages=scanned,
        run_match_rate=run,
        min_element_match_rate=HARD,
        min_element_match_rate_when_covered=FLOOR,
        min_run_match_rate_when_covered=RUN_FLOOR,
        min_coverage=MIN_COV,
    )


# ── emr_passes: the run-match corroboration path (native only) ───────────────

def test_native_high_run_match_rescues_low_emr():
    """Native, EMR 54, coverage 1.0, run_match 100 -> passes."""
    assert _emr(54.2, cov=1.0, run=100.0, scanned=False) is True


def test_native_run_match_at_floor_rescues():
    """Native, EMR 58.6, run_match 81.5 (>= 80 floor) -> passes."""
    assert _emr(58.6, cov=1.0, run=81.5, scanned=False) is True


def test_native_low_run_match_not_rescued():
    """Native, EMR 56, run_match 41.3 (< floor) -> needs review."""
    assert _emr(56.2, cov=1.0, run=41.3, scanned=False) is False


def test_scanned_high_run_match_not_rescued():
    """Population B: scanned, EMR 10, run 95 -> NOT rescued."""
    assert _emr(10.5, cov=1.0, run=95.2, scanned=True) is False


def test_run_match_rescue_requires_coverage():
    """Low coverage blocks the run-match path even with high run_match."""
    assert _emr(54.0, cov=0.5, run=100.0, scanned=False) is False


# ── emr_passes: existing paths must be unchanged ─────────────────────────────

def test_hard_threshold_unchanged():
    """EMR >= 85 passes regardless of run_match (None)."""
    assert _emr(90.0, cov=0.0, run=None, scanned=False) is True


def test_coverage_floor_unchanged_when_no_run_match():
    """EMR in [70,85) with coverage and no run_match -> coverage-floor pass."""
    assert _emr(75.0, cov=1.0, run=None, scanned=False) is True


def test_below_coverage_floor_without_run_match_still_fails():
    """EMR 65 < floor 70, coverage high, no run_match -> needs review."""
    assert _emr(65.0, cov=1.0, run=None, scanned=False) is False


def test_none_emr_still_fails():
    """None EMR (content-preserving did not run) -> needs review."""
    assert _emr(None, cov=1.0, run=100.0, scanned=False) is False


def test_no_native_pages_skips_gate():
    """Fully-scanned source: EMR gate skipped (passes)."""
    assert _emr(0.0, cov=1.0, run=0.0, scanned=True, native=False) is True


# ── compute_wcag_verdict end-to-end ──────────────────────────────────────────

def _post(score=100.0):
    return {"compliance": {"overall_score": score}}


def test_verdict_native_oversegmented_passes():
    """Native end-to-end: native, EMR 54, run 100 -> pass."""
    v = compute_wcag_verdict(
        _post(),
        avg_coverage=0.99,
        element_match_rate=54.2,
        run_match_rate=100.0,
        sr_quality="fair",
        has_scanned_pages=False,
        has_native_pages=True,
        min_run_match_rate_when_covered=RUN_FLOOR,
    )
    assert v == "pass"


def test_verdict_scanned_oversegmented_still_needs_review():
    """Scanned end-to-end: 13/14 scanned, EMR 10, run 95 -> needs_review."""
    v = compute_wcag_verdict(
        _post(),
        avg_coverage=1.0,
        element_match_rate=10.5,
        run_match_rate=95.2,
        sr_quality="poor",
        has_scanned_pages=True,
        has_native_pages=True,
        ocr_pages_passing_pct=0.0,
        min_run_match_rate_when_covered=RUN_FLOOR,
    )
    assert v == "needs_review"


def test_default_run_floor_present():
    """Config default exists so resolve_verdict_config can thread it."""
    assert VERDICT_DEFAULTS["min_run_match_rate_when_covered"] == RUN_FLOOR


# ── failure taxonomy stays consistent with the verdict ───────────────────────

def test_taxonomy_no_emr_reason_when_run_match_rescues():
    """Rescued native doc: taxonomy must NOT report element_match_rate_low."""
    record = {
        "status": "passed",
        "verdict": "pass",
        "sr_quality": {
            "sr_quality": "fair",
            "avg_coverage": 0.99,
            "element_match_rate": 54.2,
            "run_match_rate": 100.0,
            "scanned_pages": [],
            "total_pages": 34,
        },
        "avg_coverage": 0.99,
        "element_match_rate": 54.2,
    }
    codes = {r["code"] for r in classify_failure_reasons(record)}
    assert "element_match_rate_low" not in codes


def test_taxonomy_reports_emr_reason_when_run_match_low():
    """Borderline native doc (run match 41): taxonomy keeps element_match_rate_low."""
    record = {
        "status": "gates_failed",
        "verdict": "needs_review",
        "sr_quality": {
            "sr_quality": "fair",
            "avg_coverage": 1.0,
            "element_match_rate": 56.2,
            "run_match_rate": 41.3,
            "scanned_pages": [],
            "total_pages": 2,
        },
        "avg_coverage": 1.0,
        "element_match_rate": 56.2,
    }
    codes = {r["code"] for r in classify_failure_reasons(record)}
    assert "element_match_rate_low" in codes
