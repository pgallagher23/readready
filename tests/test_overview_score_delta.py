# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Overview score card must show the change from the pre-remediation audit."""


def test_delta_uses_pre_remediation_audit():
    import app
    html = app._build_overview_metrics_html({"pipeline_result": {
        "audit": {"compliance": {"overall_score": 87.5}},
        "post_audit": {"compliance": {"overall_score": 95.8}},
    }})
    assert "+8%" in html
    assert "+96%" not in html
