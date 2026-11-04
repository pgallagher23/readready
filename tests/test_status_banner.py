# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the unified status banner + Improvement Guide when a document
is ``needs_review`` solely because of a document-level metric (low average
text coverage) that the per-page SR check does not flag as an issue.

Regression for the coverage-only needs-review case: WCAG 24/24, screen
reader quality "good", but avg_coverage 0.73 < verdict gate 0.80. Before the
fix the amber banner never said "Needs Review", showed no reason, and the
Improvement Guide returned empty (so the tab disappeared).
"""

import app
from audit.standards import compute_wcag_verdict


def _coverage_only_case():
    """post_audit + pipeline_result mirroring the coverage-only reproduction."""
    post_audit = {
        "compliance": {
            "overall_score": 100.0,
            "by_criterion": {
                "1.1.1": {"passed": True, "total": 1, "failing": 0},
                "1.3.1": {"passed": True, "total": 1, "failing": 0},
            },
        },
        "summary": {"total_issues": 0, "needs_remediation": 0},
        "issues": [],
    }
    result = {
        "sr_quality": {
            "sr_quality": "good",
            "avg_coverage": 0.73,
            "element_match_rate": 100.0,
            "hollow_pages": [],
            "tagger_mode": "content-preserving",
            "scanned_pages": [],
            "total_pages": 2,
            "ocr_pages_passing_pct": None,
        },
        "warnings": [],
        "source_quality_warnings": {},
    }
    vc = {
        "min_wcag_score": 100.0,
        "min_coverage": 0.80,
        "min_element_match_rate": 85,
        "reject_hollow_pages": True,
    }
    return post_audit, result, vc


def test_coverage_only_is_needs_review():
    """Sanity: the verdict itself is needs_review for this case."""
    post_audit, result, vc = _coverage_only_case()
    sr = result["sr_quality"]
    verdict = compute_wcag_verdict(
        post_audit,
        avg_coverage=sr["avg_coverage"],
        element_match_rate=sr["element_match_rate"],
        hollow_pages=sr["hollow_pages"],
        sr_quality=sr["sr_quality"],
        min_wcag_score=vc["min_wcag_score"],
        min_coverage=vc["min_coverage"],
        min_element_match_rate=vc["min_element_match_rate"],
        reject_hollow_pages=vc["reject_hollow_pages"],
        has_scanned_pages=False,
        has_native_pages=True,
    )
    assert verdict == "needs_review"


def test_banner_states_needs_review_and_coverage_reason():
    """The amber banner must name the verdict and explain the coverage gate."""
    post_audit, result, vc = _coverage_only_case()
    html = app._build_unified_status_html(
        100.0, 0.0, post_audit, result, verdict_config=vc, structure={},
    )
    assert "Needs Review" in html, "banner must display the verdict word"
    # The blocking reason (text coverage below threshold) must be visible.
    assert "73%" in html and "80%" in html, (
        "banner must surface the coverage value and threshold"
    )


def test_reading_order_reason_links_to_content_editor():
    """The reading-order gate line points the user to the Content Editor (with
    an in-app link) rather than only to a screen-reader manual check."""
    post_audit, result, vc = _coverage_only_case()  # already needs_review (amber)
    result["sr_quality"]["reading_order_min_tau"] = 0.30  # below the gate
    result["sr_quality"]["reading_order_failing_pages"] = [2, 3, 4, 5]
    html = app._build_unified_status_html(
        100.0, 0.0, post_audit, result, verdict_config=vc, structure={},
    )
    # New wording: reading order can be changed in the editor.
    assert "review and change it in the" in html
    assert "verify with a screen reader during manual review" not in html
    # Clickable in-app link to the Content Editor sub-tab.
    assert "window._gotoContentEditor(this)" in html
    assert ">Content Editor</a>" in html
    # The failing page range is still surfaced.
    assert "2-5" in html or "2–5" in html


def test_improvement_guide_renders_for_coverage_only():
    """The Improvement Guide must not be empty when coverage is the only blocker."""
    post_audit, result, vc = _coverage_only_case()
    guide = app.build_remediation_guide_html(
        result, "dummy.pdf", {}, verdict_config=vc,
    )
    assert guide.strip(), "guide must render (so the tab appears) for needs_review"
    assert "Coverage" in guide, "guide must explain the low-coverage reason"


def test_passing_document_has_no_guide():
    """Control: a genuinely passing doc still produces no guide (tab hidden)."""
    post_audit, result, vc = _coverage_only_case()
    result["sr_quality"]["avg_coverage"] = 0.95  # now above the gate
    guide = app.build_remediation_guide_html(
        result, "dummy.pdf", {}, verdict_config=vc,
    )
    assert guide == "", "passing document should produce an empty guide"


def test_fair_banner_shows_pages_not_quality_grade():
    """A 'fair' document surfaces page-specific actions, not the old
    'Screen Reader Quality: Fair' grade."""
    post_audit, result, vc = _coverage_only_case()
    sr = result["sr_quality"]
    sr["sr_quality"] = "fair"
    sr["element_match_rate"] = 84.5
    sr["avg_coverage"] = 0.95  # coverage fine; the concern is reading order
    sr["reading_order_min_tau"] = 0.30  # below the gate
    sr["reading_order_failing_pages"] = [16, 18, 19]
    html = app._build_unified_status_html(
        100.0, 0.0, post_audit, result, verdict_config=vc, structure={},
    )
    assert "Screen Reader Quality: Fair" not in html
    assert "Screen Reader Quality" not in html
    assert "16" in html and "18" in html  # the flagged pages are named


def test_poor_banner_shows_unusable_headline():
    """A 'poor' document keeps a clear 'may not be usable' headline."""
    post_audit, result, vc = _coverage_only_case()
    sr = result["sr_quality"]
    sr["sr_quality"] = "poor"
    sr["element_match_rate"] = 40.0
    html = app._build_unified_status_html(
        100.0, 0.0, post_audit, result, verdict_config=vc, structure={},
    )
    assert "may not be usable by a screen reader" in html


def test_green_banner_still_surfaces_advisory_reading_order():
    """A doc that passes ONLY because reading order is advisory must still say so.

    Demoting the gate turns these documents green; if the finding rendered only
    on the amber banner it would become invisible on exactly the documents the
    demotion affects.
    """
    post_audit, result, vc = _coverage_only_case()
    sr = result["sr_quality"]
    sr["sr_quality"] = "good"
    sr["avg_coverage"] = 0.95
    sr["element_match_rate"] = 95.0
    sr["reading_order_min_tau"] = 0.30
    sr["reading_order_failing_pages"] = [16, 18, 19]
    html = app._build_unified_status_html(
        100.0, 0.0, post_audit, result, verdict_config=vc, structure={},
    )
    assert "Accessible" in html, "reading order alone must no longer fail the doc"
    assert "Reading order may not match the visual layout" in html
    assert "16" in html and "18" in html


def test_reading_order_line_is_not_duplicated_on_amber():
    """The advisory pill and the amber detail block must not both render it."""
    post_audit, result, vc = _coverage_only_case()
    sr = result["sr_quality"]
    sr["avg_coverage"] = 0.40           # forces amber for coverage
    sr["reading_order_min_tau"] = 0.30
    sr["reading_order_failing_pages"] = [7]
    html = app._build_unified_status_html(
        100.0, 0.0, post_audit, result, verdict_config=vc, structure={},
    )
    assert "Needs Review" in html
    assert html.count("Reading order may not match the visual layout") == 1
