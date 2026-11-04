# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for output_gen/report_page.py's best-practice advisories section."""


def test_report_page_lists_informational_issues_as_best_practice():
    from output_gen.report_page import _build_advisories_html
    html = _build_advisories_html([
        {"type": "no-h1", "remediation_status": "informational",
         "description": "Document has no main heading (h1)."},
        {"type": "x", "remediation_status": "needs_remediation",
         "description": "ignored"},
    ])
    assert "Best-practice" in html and "no main heading" in html
    assert "ignored" not in html
    assert _build_advisories_html([]) == ""


def test_report_page_surfaces_an_advisory_gate_finding():
    """The appended report is a written artifact the user may forward. On a
    document with a flagged reading order it claimed "no verification issues"
    and omitted the finding entirely, because the advisories section only ever
    saw post_audit issues and never the verdict-gate reasons."""
    from audit.standards import resolve_verdict_config
    from output_gen.report_page import _build_report_html

    clean = {"compliance": {"overall_score": 100.0, "by_criterion": {}},
             "summary": {"total_issues": 0, "compliant": 24,
                         "severity_counts": {}}, "issues": []}
    sr = {"sr_quality": "good", "avg_coverage": 0.99,
          "element_match_rate": 99.0, "reading_order_min_tau": 0.30,
          "reading_order_failing_pages": [3, 4], "hollow_pages": []}
    html = _build_report_html(
        clean, "Doc", sr_data=sr, verdict_config=resolve_verdict_config({}))
    low = html.lower()
    assert "best-practice" in low
    assert "reading order" in low


def test_report_page_clean_doc_has_no_advisories_section():
    from audit.standards import resolve_verdict_config
    from output_gen.report_page import _build_report_html

    clean = {"compliance": {"overall_score": 100.0, "by_criterion": {}},
             "summary": {"total_issues": 0, "compliant": 24,
                         "severity_counts": {}}, "issues": []}
    html = _build_report_html(
        clean, "Doc", sr_data={}, verdict_config=resolve_verdict_config({}))
    assert "best-practice" not in html.lower()
