# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for audit/standards.py — scoring and verdict logic.

These functions are the single source of truth for WCAG compliance scores
and the pass/needs_review verdict.  Drift here silently changes pass rates
across the entire pipeline.

Run:  python -m pytest tests/test_audit_standards.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inspect

from audit.standards import (
    AAA_CRITERIA,
    VERDICT_DEFAULTS,
    WCAG_CRITERIA,
    WCAG_PRINCIPLES,
    build_verdict_inputs,
    compute_compliance_score,
    compute_wcag_verdict,
    emr_passes,
    get_criterion_info,
    get_principle_for_criterion,
    resolve_verdict_config,
    summarize_pipeline_result,
    verdict_from_sr,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _issue(crit: str, status: str = "needs_remediation", severity: str = "major") -> dict:
    return {
        "wcag_criterion": crit,
        "remediation_status": status,
        "severity": severity,
    }


def _audit_with_score(score: float) -> dict:
    """Build minimal post_audit dict with a given overall_score."""
    return {"compliance": {"overall_score": score}}


# ===================================================================
# get_criterion_info / get_principle_for_criterion
# ===================================================================

class TestCriterionLookup:

    def test_known_criterion(self):
        info = get_criterion_info("1.1.1")
        assert info["name"] == "Non-text Content"
        assert info["level"] == "A"

    def test_unknown_criterion_returns_default(self):
        info = get_criterion_info("99.99.99")
        assert info["name"] == "Unknown Criterion"

    def test_principle_for_known_criterion(self):
        assert get_principle_for_criterion("1.1.1") == "Perceivable"
        assert get_principle_for_criterion("2.4.4") == "Operable"
        assert get_principle_for_criterion("3.1.1") == "Understandable"
        assert get_principle_for_criterion("4.1.1") == "Robust"

    def test_principle_for_unknown_criterion(self):
        assert get_principle_for_criterion("99.99.99") == "Unknown"


# ===================================================================
# compute_compliance_score
# ===================================================================

class TestComputeComplianceScore:

    def test_no_issues_perfect_score(self):
        """No issues at all → every criterion passes, 100% score."""
        result = compute_compliance_score([])
        assert result["overall_score"] == 100.0
        assert all(d["passed"] for d in result["by_criterion"].values())

    def test_all_criteria_present_in_output(self):
        """Every criterion from WCAG_CRITERIA should appear in by_criterion."""
        result = compute_compliance_score([])
        for cid in WCAG_CRITERIA:
            assert cid in result["by_criterion"], f"Missing criterion {cid}"

    def test_single_failing_criterion(self):
        """One failing issue in 1.1.1 → that criterion fails, score < 100."""
        issues = [_issue("1.1.1")]
        result = compute_compliance_score(issues)
        assert result["by_criterion"]["1.1.1"]["passed"] is False
        assert result["by_criterion"]["1.1.1"]["failing"] == 1
        assert result["overall_score"] < 100.0

    def test_remediated_issue_does_not_fail(self):
        """An auto_remediated issue should NOT count as failing."""
        issues = [_issue("1.1.1", status="auto_remediated")]
        result = compute_compliance_score(issues)
        assert result["by_criterion"]["1.1.1"]["passed"] is True

    def test_compliant_issue_does_not_fail(self):
        """Compliant status should not count as failing."""
        issues = [_issue("1.1.1", status="compliant")]
        result = compute_compliance_score(issues)
        assert result["by_criterion"]["1.1.1"]["passed"] is True

    def test_mixed_statuses_on_same_criterion(self):
        """One failing + one compliant on same criterion → criterion fails."""
        issues = [
            _issue("1.1.1", status="needs_remediation"),
            _issue("1.1.1", status="compliant"),
        ]
        result = compute_compliance_score(issues)
        assert result["by_criterion"]["1.1.1"]["passed"] is False
        assert result["by_criterion"]["1.1.1"]["total"] == 2
        assert result["by_criterion"]["1.1.1"]["failing"] == 1

    def test_multiple_criteria_failing(self):
        """Failures across multiple criteria."""
        issues = [
            _issue("1.1.1"),
            _issue("1.3.1"),
            _issue("2.4.4"),
        ]
        result = compute_compliance_score(issues)
        assert result["by_criterion"]["1.1.1"]["passed"] is False
        assert result["by_criterion"]["1.3.1"]["passed"] is False
        assert result["by_criterion"]["2.4.4"]["passed"] is False
        # Unaffected criterion should still pass
        assert result["by_criterion"]["3.1.1"]["passed"] is True

    def test_score_calculation(self):
        """Score = passing_AA_criteria / total_AA_criteria * 100."""
        # Fail exactly one A/AA criterion
        issues = [_issue("1.1.1")]
        result = compute_compliance_score(issues)
        total_aa = len([c for c in WCAG_CRITERIA if c not in AAA_CRITERIA])
        expected = (total_aa - 1) / total_aa * 100
        assert abs(result["overall_score"] - round(expected, 1)) < 0.2

    def test_aaa_criteria_excluded_from_overall_score(self):
        """AAA criteria (2.4.9, 3.2.5) should NOT affect the overall AA score."""
        issues = [_issue("2.4.9")]  # AAA criterion
        result = compute_compliance_score(issues)
        # Overall score should still be 100 — AAA failure doesn't count
        assert result["overall_score"] == 100.0
        # But it should still appear in by_criterion
        assert result["by_criterion"]["2.4.9"]["passed"] is False

    def test_aaa_criteria_excluded_from_principle_score(self):
        """AAA criteria should not affect principle-level scores."""
        issues = [_issue("2.4.9")]  # AAA, Operable principle
        result = compute_compliance_score(issues)
        assert result["by_principle"]["Operable"]["score"] == 100.0

    def test_by_principle_structure(self):
        """Each principle should have score, total, passing keys."""
        result = compute_compliance_score([])
        for principle in WCAG_PRINCIPLES:
            assert principle in result["by_principle"]
            p = result["by_principle"][principle]
            assert "score" in p
            assert "total" in p
            assert "passing" in p
            assert p["score"] == 100.0

    def test_by_severity_counts(self):
        """Severity counts should only include needs_remediation issues."""
        issues = [
            _issue("1.1.1", severity="critical"),
            _issue("1.3.1", severity="major"),
            _issue("2.4.4", severity="minor"),
            _issue("3.1.1", status="compliant", severity="critical"),  # should NOT count
        ]
        result = compute_compliance_score(issues)
        assert result["by_severity"]["critical"] == 1
        assert result["by_severity"]["major"] == 1
        assert result["by_severity"]["minor"] == 1
        assert result["by_severity"]["info"] == 0

    def test_unknown_criterion_included(self):
        """Issues with unknown criterion IDs should be tracked."""
        issues = [_issue("99.99.99")]
        result = compute_compliance_score(issues)
        assert "99.99.99" in result["by_criterion"]
        assert result["by_criterion"]["99.99.99"]["passed"] is False

    def test_missing_status_key_treated_as_passing(self):
        """Issue dict without remediation_status should NOT count as failing.

        Only explicit 'needs_remediation' status increments the failing count.
        """
        issues = [{"wcag_criterion": "1.1.1", "severity": "major"}]
        result = compute_compliance_score(issues)
        assert result["by_criterion"]["1.1.1"]["passed"] is True
        assert result["by_criterion"]["1.1.1"]["total"] == 1
        assert result["by_criterion"]["1.1.1"]["failing"] == 0

    def test_informational_issue_does_not_fail_its_criterion(self):
        issues = [{
            "type": "no-h1", "wcag_criterion": "1.3.1", "severity": "info",
            "remediation_status": "informational",
        }]
        score = compute_compliance_score(issues)
        assert score["by_criterion"]["1.3.1"]["passed"] is True
        assert score["overall_score"] == 100.0

    def test_principle_score_reflects_failures(self):
        """A failure in a Perceivable criterion should lower that principle's score."""
        issues = [_issue("1.1.1")]
        result = compute_compliance_score(issues)
        assert result["by_principle"]["Perceivable"]["score"] < 100.0
        # Other principles should be unaffected
        assert result["by_principle"]["Operable"]["score"] == 100.0
        assert result["by_principle"]["Understandable"]["score"] == 100.0
        assert result["by_principle"]["Robust"]["score"] == 100.0


# ===================================================================
# compute_wcag_verdict
# ===================================================================

class TestComputeWcagVerdict:
    """Tests for the two-state verdict function.

    Default thresholds: min_wcag_score=100, min_coverage=0.80,
    min_element_match_rate=85, reject_hollow_pages=True.
    """

    def test_perfect_metrics_pass(self):
        """All metrics above thresholds → pass."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
        )
        assert result == "pass"

    def test_low_wcag_score_needs_review(self):
        result = compute_wcag_verdict(
            _audit_with_score(95.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
        )
        assert result == "needs_review"

    def test_low_coverage_needs_review(self):
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.50,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
        )
        assert result == "needs_review"

    def test_low_element_match_rate_needs_review(self):
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=50.0,
            hollow_pages=[],
            sr_quality="good",
        )
        assert result == "needs_review"

    def test_none_element_match_rate_needs_review(self):
        """EMR=None (overlay/fallback mode) always → needs_review."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=None,
            hollow_pages=[],
            sr_quality="good",
        )
        assert result == "needs_review"

    def test_hollow_pages_needs_review(self):
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[1, 3],
            sr_quality="good",
        )
        assert result == "needs_review"

    def test_unknown_sr_quality_needs_review(self):
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="unknown",
        )
        assert result == "needs_review"

    def test_low_reading_order_tau_is_advisory_by_default(self):
        """Low tau does not block: it is a best-practice signal, not a failure.

        A tau disagreement is not a WCAG failure — 1.3.2 is audited separately
        by ReadingOrderCheck, and a manual review of flagged documents found
        none that actually read wrong. Detection still runs and still reports.
        """
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
            reading_order_min_tau=0.20,
        )
        assert result == "pass"

    def test_low_reading_order_tau_blocks_when_advisory_disabled(self):
        """The kill-switch restores the blocking gate."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
            reading_order_min_tau=0.20,
            reading_order_advisory=False,
        )
        assert result == "needs_review"

    def test_none_reading_order_tau_does_not_block(self):
        """No eligible page scored (min_tau=None) → gate skipped, still passes."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
            reading_order_min_tau=None,
        )
        assert result == "pass"

    def test_good_reading_order_tau_passes(self):
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
            reading_order_min_tau=0.95,
        )
        assert result == "pass"

    def test_reading_order_threshold_is_configurable(self):
        """A tau between a relaxed and default threshold passes when relaxed."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
            reading_order_min_tau=0.45,
            min_reading_order_tau=0.40,
        )
        assert result == "pass"

    def test_reject_hollow_pages_disabled(self):
        """With reject_hollow_pages=False, hollow pages should not block pass."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[1, 3, 5],
            sr_quality="good",
            reject_hollow_pages=False,
        )
        assert result == "pass"

    def test_custom_thresholds_relaxed(self):
        """Relaxed thresholds should allow lower metrics to pass."""
        result = compute_wcag_verdict(
            _audit_with_score(90.0),
            avg_coverage=0.70,
            element_match_rate=60.0,
            hollow_pages=[],
            sr_quality="fair",
            min_wcag_score=85.0,
            min_coverage=0.60,
            min_element_match_rate=50.0,
        )
        assert result == "pass"

    def test_custom_thresholds_strict(self):
        """Strict thresholds should reject otherwise-passing metrics."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
            min_coverage=0.99,  # stricter than default
        )
        assert result == "needs_review"

    # --- Coverage-corroborated EMR relaxation (floor=70 by default) ---

    def test_emr_below_hard_but_corroborated_passes(self):
        """EMR in 70-85 with corroborating coverage → pass (relaxed to floor)."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=75.0,
            hollow_pages=[],
            sr_quality="good",
        )
        assert result == "pass"

    def test_emr_below_floor_stays_review_even_with_high_coverage(self):
        """Collapsed EMR (<floor) needs review even at coverage 1.0."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=1.0,
            element_match_rate=40.0,
            hollow_pages=[],
            sr_quality="good",
        )
        assert result == "needs_review"

    def test_emr_at_floor_boundary_corroborated_passes(self):
        """EMR exactly at the floor with corroborating coverage → pass."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.85,
            element_match_rate=70.0,
            hollow_pages=[],
            sr_quality="good",
        )
        assert result == "pass"

    def test_emr_just_below_floor_corroborated_needs_review(self):
        """Just below the floor is not rescued by coverage."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=69.9,
            hollow_pages=[],
            sr_quality="good",
        )
        assert result == "needs_review"

    def test_emr_in_band_but_coverage_below_min_not_rescued(self):
        """Coverage below min disables relaxation (and fails coverage gate)."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.79,  # < default min_coverage 0.80
            element_match_rate=75.0,
            hollow_pages=[],
            sr_quality="good",
        )
        assert result == "needs_review"

    def test_emr_above_hard_threshold_always_passes(self):
        """EMR >= hard threshold passes regardless of the floor."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=1.0,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
        )
        assert result == "pass"

    def test_relaxation_disabled_when_floor_equals_hard(self):
        """Setting the floor equal to the hard threshold disables relaxation."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=75.0,
            hollow_pages=[],
            sr_quality="good",
            min_element_match_rate_when_covered=85,
        )
        assert result == "needs_review"


class TestEmrPasses:
    """The shared EMR-gate predicate used by both the verdict and the taxonomy."""

    def _call(self, emr, cov, **kw):
        return emr_passes(
            emr,
            avg_coverage=cov,
            has_native_pages=kw.get("has_native_pages", True),
            min_element_match_rate=kw.get("min_element_match_rate", 85),
            min_element_match_rate_when_covered=kw.get(
                "min_element_match_rate_when_covered", 70),
            min_coverage=kw.get("min_coverage", 0.80),
        )

    def test_no_native_pages_always_passes(self):
        assert self._call(0.0, 0.0, has_native_pages=False) is True

    def test_none_emr_fails(self):
        assert self._call(None, 1.0) is False

    def test_above_hard_passes(self):
        assert self._call(90.0, 0.0) is True

    def test_corroborated_in_band_passes(self):
        assert self._call(75.0, 0.95) is True

    def test_below_floor_fails_even_high_coverage(self):
        assert self._call(40.0, 1.0) is False

    def test_at_floor_boundary_corroborated_passes(self):
        assert self._call(70.0, 0.80) is True

    def test_just_below_floor_fails(self):
        assert self._call(69.9, 0.95) is False

    def test_in_band_but_low_coverage_fails(self):
        assert self._call(75.0, 0.79) is False

    def test_boundary_wcag_score_exact(self):
        """Score exactly at threshold should pass."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.85,
            element_match_rate=85.0,
            hollow_pages=[],
            sr_quality="good",
            min_wcag_score=100.0,
            min_coverage=0.85,
            min_element_match_rate=85.0,
        )
        assert result == "pass"

    def test_boundary_coverage_just_below(self):
        """Coverage just below the default threshold (0.80) should fail."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.79,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
        )
        assert result == "needs_review"

    def test_boundary_coverage_exact(self):
        """Coverage exactly at the default threshold (0.80) should pass."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.80,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
        )
        assert result == "pass"

    def test_empty_hollow_pages_not_rejected(self):
        """Empty list of hollow pages should not trigger rejection."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
        )
        assert result == "pass"

    def test_none_hollow_pages_not_rejected(self):
        """None hollow_pages (not provided) should not trigger rejection."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=None,
            sr_quality="good",
        )
        assert result == "pass"

    def test_missing_compliance_key(self):
        """Post-audit dict without compliance key should default to 0 → needs_review."""
        result = compute_wcag_verdict(
            {},
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
        )
        assert result == "needs_review"

    def test_sr_quality_fair_passes(self):
        """sr_quality='fair' is not 'unknown', so should not block pass."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="fair",
        )
        assert result == "pass"

    def test_sr_quality_poor_passes(self):
        """sr_quality='poor' is not 'unknown', so should not block pass by itself."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="poor",
        )
        assert result == "pass"

    def test_default_args_needs_review(self):
        """Default args (0 coverage, None EMR, unknown SR) → needs_review."""
        result = compute_wcag_verdict(_audit_with_score(100.0))
        assert result == "needs_review"

    def test_explicit_threshold_emr_80(self):
        """An explicit min_element_match_rate=80 lets EMR=82 pass.

        (config.yaml/VERDICT_DEFAULTS use 85; this exercises a custom
        threshold passed by a caller.)
        """
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=82.0,
            hollow_pages=[],
            sr_quality="good",
            min_element_match_rate=80.0,
        )
        assert result == "pass"

    def test_explicit_threshold_emr_80_boundary(self):
        """EMR just below an explicit threshold of 80 → needs_review.

        Coverage corroboration is disabled here (floor == threshold) to
        isolate the hard EMR boundary; the relaxation has its own tests.
        """
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=79.9,
            hollow_pages=[],
            sr_quality="good",
            min_element_match_rate=80.0,
            min_element_match_rate_when_covered=80.0,
        )
        assert result == "needs_review"


# ===================================================================
# Scanned-PDF and mixed-doc verdict gates
# ===================================================================

class TestScannedVerdict:
    """Scanned source PDFs are graded by per-page OCR quality instead
    of element_match_rate (EMR is meaningless when there are no native
    text runs to match against).  Mixed docs (some scanned, some
    native) must pass BOTH gates.

    Each case fixes overall_score=100, sr_quality="good", and
    avg_coverage>=min_coverage so the new gates are isolated.
    """

    def test_scanned_only_emr_none_does_not_block(self):
        """All pages scanned + good OCR → pass even though EMR=None."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=None,
            hollow_pages=[],
            sr_quality="good",
            has_scanned_pages=True,
            has_native_pages=False,
            ocr_pages_passing_pct=0.9,
        )
        assert result == "pass"

    def test_scanned_low_ocr_pct_needs_review(self):
        """All pages scanned + half OCR'd poorly → needs_review."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=None,
            hollow_pages=[],
            sr_quality="fair",
            has_scanned_pages=True,
            has_native_pages=False,
            ocr_pages_passing_pct=0.5,
        )
        assert result == "needs_review"

    def test_scanned_ocr_pct_none_needs_review(self):
        """All pages scanned, no OCR grades available → needs_review."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=None,
            hollow_pages=[],
            sr_quality="good",
            has_scanned_pages=True,
            has_native_pages=False,
            ocr_pages_passing_pct=None,
        )
        assert result == "needs_review"

    def test_mixed_both_gates_must_pass_emr_fails(self):
        """Mixed doc: EMR fails → needs_review even if OCR passes."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=50.0,
            hollow_pages=[],
            sr_quality="good",
            has_scanned_pages=True,
            has_native_pages=True,
            ocr_pages_passing_pct=0.95,
        )
        assert result == "needs_review"

    def test_mixed_both_gates_must_pass_ocr_fails(self):
        """Mixed doc: OCR fails → needs_review even if EMR passes."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
            has_scanned_pages=True,
            has_native_pages=True,
            ocr_pages_passing_pct=0.5,
        )
        assert result == "needs_review"

    def test_mixed_both_gates_pass(self):
        """Mixed doc: both EMR and OCR pass → pass."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
            has_scanned_pages=True,
            has_native_pages=True,
            ocr_pages_passing_pct=0.9,
        )
        assert result == "pass"

    def test_native_only_no_ocr_gate(self):
        """has_scanned_pages=False → OCR gate skipped entirely."""
        result = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
            has_scanned_pages=False,
            has_native_pages=True,
            ocr_pages_passing_pct=None,  # would normally block
        )
        assert result == "pass"

    def test_backwards_compat_default_args(self):
        """Existing callers (no new params) get the old behavior."""
        result_old_style = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
        )
        result_explicit = compute_wcag_verdict(
            _audit_with_score(100.0),
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
            has_scanned_pages=False,
            has_native_pages=True,
        )
        assert result_old_style == result_explicit == "pass"


# ===================================================================
# Integration: compute_compliance_score → compute_wcag_verdict
# ===================================================================

class TestScoreToVerdict:
    """End-to-end: build issues → compute score → compute verdict."""

    def test_clean_doc_passes(self):
        """Document with no issues should get 100 score and pass verdict."""
        score = compute_compliance_score([])
        audit = {"compliance": score}
        verdict = compute_wcag_verdict(
            audit,
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
        )
        assert score["overall_score"] == 100.0
        assert verdict == "pass"

    def test_one_failing_criterion_blocks_pass(self):
        """A single failing criterion drops score below 100 → needs_review."""
        issues = [_issue("1.1.1")]
        score = compute_compliance_score(issues)
        audit = {"compliance": score}
        verdict = compute_wcag_verdict(
            audit,
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
        )
        assert score["overall_score"] < 100.0
        assert verdict == "needs_review"

    def test_remediated_doc_passes(self):
        """All issues remediated → 100 score → pass."""
        issues = [
            _issue("1.1.1", status="auto_remediated"),
            _issue("1.3.1", status="auto_remediated"),
            _issue("2.4.4", status="compliant"),
        ]
        score = compute_compliance_score(issues)
        audit = {"compliance": score}
        verdict = compute_wcag_verdict(
            audit,
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
        )
        assert score["overall_score"] == 100.0
        assert verdict == "pass"

    def test_aaa_only_failure_still_passes(self):
        """Failing only AAA criteria should not block pass."""
        issues = [_issue("2.4.9"), _issue("3.2.5")]
        score = compute_compliance_score(issues)
        audit = {"compliance": score}
        verdict = compute_wcag_verdict(
            audit,
            avg_coverage=0.95,
            element_match_rate=90.0,
            hollow_pages=[],
            sr_quality="good",
        )
        assert score["overall_score"] == 100.0
        assert verdict == "pass"


# ===================================================================
# VERDICT_DEFAULTS / resolve_verdict_config / build_verdict_inputs
# ===================================================================

class TestVerdictDefaults:
    """The single source of truth for verdict thresholds."""

    def test_defaults_match_config_values(self):
        assert VERDICT_DEFAULTS["min_coverage"] == 0.80
        assert VERDICT_DEFAULTS["min_element_match_rate"] == 85
        assert VERDICT_DEFAULTS["min_wcag_score"] == 100.0

    def test_signature_defaults_match_VERDICT_DEFAULTS(self):
        """compute_wcag_verdict signature defaults must not drift from the dict."""
        params = inspect.signature(compute_wcag_verdict).parameters
        for key, expected in VERDICT_DEFAULTS.items():
            assert params[key].default == expected, (
                f"{key} default {params[key].default} != VERDICT_DEFAULTS {expected}"
            )


class TestResolveVerdictConfig:
    def test_none_returns_defaults(self):
        assert resolve_verdict_config(None) == VERDICT_DEFAULTS
        # returns a copy, not the module-level dict
        assert resolve_verdict_config(None) is not VERDICT_DEFAULTS

    def test_full_config_dict_reads_verdict_section(self):
        cfg = {"verdict": {"min_coverage": 0.90}, "llm": {"x": 1}}
        out = resolve_verdict_config(cfg)
        assert out["min_coverage"] == 0.90
        assert out["min_element_match_rate"] == 85  # unspecified → default

    def test_bare_verdict_dict(self):
        out = resolve_verdict_config({"min_coverage": 0.70})
        assert out["min_coverage"] == 0.70

    def test_partial_falls_back_for_unspecified(self):
        out = resolve_verdict_config({"verdict": {"min_wcag_score": 95.0}})
        assert out["min_wcag_score"] == 95.0
        assert out["min_coverage"] == VERDICT_DEFAULTS["min_coverage"]

    def test_explicit_value_wins_over_default(self):
        out = resolve_verdict_config({"verdict": {"reject_hollow_pages": False}})
        assert out["reject_hollow_pages"] is False

    def test_null_verdict_section_does_not_crash(self):
        """A present-but-null `verdict:` section (YAML empty) → defaults, no crash."""
        assert resolve_verdict_config({"verdict": None, "llm": {}}) == VERDICT_DEFAULTS

    def test_non_dict_input_returns_defaults(self):
        assert resolve_verdict_config("garbage") == VERDICT_DEFAULTS
        assert resolve_verdict_config(123) == VERDICT_DEFAULTS


class TestBuildVerdictInputs:
    def test_derives_native_and_scanned_flags(self):
        sr = {"scanned_pages": [], "total_pages": 5, "avg_coverage": 0.9}
        kw = build_verdict_inputs(_audit_with_score(100.0), sr)
        assert kw["has_scanned_pages"] is False
        assert kw["has_native_pages"] is True

    def test_all_scanned_has_no_native(self):
        sr = {"scanned_pages": [1, 2, 3], "total_pages": 3}
        kw = build_verdict_inputs(_audit_with_score(100.0), sr)
        assert kw["has_scanned_pages"] is True
        assert kw["has_native_pages"] is False

    def test_avg_coverage_none_becomes_zero(self):
        kw = build_verdict_inputs(_audit_with_score(100.0), {"avg_coverage": None})
        assert kw["avg_coverage"] == 0.0

    def test_thresholds_injected_from_config(self):
        kw = build_verdict_inputs(
            _audit_with_score(100.0), {}, {"verdict": {"min_coverage": 0.5}}
        )
        assert kw["min_coverage"] == 0.5

    def test_verdict_from_sr_matches_manual_call(self):
        sr = {
            "scanned_pages": [], "total_pages": 4, "avg_coverage": 0.95,
            "element_match_rate": 90.0, "hollow_pages": [], "sr_quality": "good",
        }
        assert verdict_from_sr(_audit_with_score(100.0), sr) == "pass"


class TestVerdictParityUIvsManifest:
    """The headline acceptance: identical inputs → identical verdict on both
    surfaces, and the OLD manifest hardcoding would have differed."""

    def _sr(self):
        # Fully-scanned document at 0.82 coverage: above config 0.80, below the
        # old bare default 0.85.
        return {
            "scanned_pages": [1, 2], "total_pages": 2, "avg_coverage": 0.82,
            "element_match_rate": None, "hollow_pages": [],
            "sr_quality": "fair", "ocr_pages_passing_pct": 0.9,
        }

    def test_ui_and_manifest_agree_given_same_config(self):
        cfg = {"verdict": {"min_coverage": 0.80, "min_element_match_rate": 85}}
        post = _audit_with_score(100.0)
        ui = verdict_from_sr(post, self._sr(), cfg)
        manifest = verdict_from_sr(post, self._sr(), cfg)
        assert ui == manifest == "pass"  # scanned doc, OCR gate passes, coverage ok

    def test_old_manifest_hardcoding_would_have_differed(self):
        """Old manifest: no config (→0.85), sr_quality='good', no scanned flags."""
        post = _audit_with_score(100.0)
        sr = self._sr()
        old = compute_wcag_verdict(
            post,
            avg_coverage=sr["avg_coverage"],   # 0.82
            element_match_rate=None,
            hollow_pages=[],
            sr_quality="good",                 # hardcoded
            # no verdict_config → min_coverage default; no scanned/native flags
        )
        new = verdict_from_sr(
            post, sr, {"verdict": {"min_coverage": 0.80}}
        )
        # Old: 0.82 < 0.85 default AND EMR gate applied (None) → needs_review.
        # New: scanned doc (EMR skipped), 0.82 >= 0.80, OCR gate passes → pass.
        assert old == "needs_review"
        assert new == "pass"
        assert old != new


# ===================================================================
# summarize_pipeline_result
# ===================================================================

class TestSummarizePipelineResult:

    def test_error_result(self):
        out = summarize_pipeline_result({"error": "PDF is encrypted"}, {})
        assert out == {
            "status": "error",
            "wcag_score": 0.0,
            "issue_count": 0,
            "issues": "PDF is encrypted",
        }

    def test_success_result_uses_verdict_and_counts_issues(self):
        result = {
            "post_audit": {
                "compliance": {"overall_score": 91.27},
                "issues": [
                    _issue("1.1.1"),
                    _issue("1.3.1", status="remediated"),
                    {"remediation_status": "needs_remediation",
                     "description": "Missing alt text"},
                ],
            },
            "sr_quality": {
                "avg_coverage": 0.95, "element_match_rate": 96.0,
                "hollow_pages": [], "sr_quality": "good",
            },
        }
        out = summarize_pipeline_result(result, {})
        assert out["status"] == verdict_from_sr(
            result["post_audit"], result["sr_quality"], {}
        )
        assert out["wcag_score"] == 91.3
        assert out["issue_count"] == 2  # remediated issue not counted
        assert "Missing alt text" in out["issues"]

    def test_missing_post_audit_defaults(self):
        out = summarize_pipeline_result({}, {})
        assert out["wcag_score"] == 0.0
        assert out["issue_count"] == 0
        assert out["issues"] == ""


class TestReadingOrderAdvisoryReporting:
    """The reading-order finding stays visible after it stops blocking."""

    def test_gate_reason_is_informational_by_default(self):
        from reporting.issue_aggregation import _verdict_gate_reasons
        reasons = _verdict_gate_reasons(
            {"reading_order_min_tau": 0.2, "reading_order_failing_pages": [3]}, {}
        )
        ro = [r for r in reasons if r["type"] == "pdf-reading-order"]
        assert len(ro) == 1, "detection must still be reported"
        assert ro[0]["remediation_status"] == "informational"
        assert ro[0]["severity"] == "info"
        assert ro[0]["failing_pages"] == [3]

    def test_gate_reason_blocks_when_advisory_disabled(self):
        from reporting.issue_aggregation import _verdict_gate_reasons
        reasons = _verdict_gate_reasons(
            {"reading_order_min_tau": 0.2, "reading_order_failing_pages": [3]},
            {"reading_order_advisory": False},
        )
        ro = [r for r in reasons if r["type"] == "pdf-reading-order"]
        assert ro[0]["remediation_status"] == "needs_remediation"
        assert ro[0]["severity"] == "major"

    def test_advisory_default_is_on(self):
        from audit.standards import resolve_verdict_config
        assert resolve_verdict_config({})["reading_order_advisory"] is True
