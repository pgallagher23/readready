# SPDX-License-Identifier: AGPL-3.0-or-later
"""A working pipeline must never be indistinguishable from a dead one.

Benign skips log at DEBUG, so a document whose issues are overwhelmingly
benign emits nothing at INFO for minutes while pegged at 100% CPU. Observed:
1788 of 2855 issues silent, 400 consecutive seconds of dead air.
"""
import logging

from bs4 import BeautifulSoup

from remediate.remediation_manager import RemediationManager


# ── mid-run silence ──────────────────────────────────────────────────


def _benign_issues(n):
    """n issues of a type that is registered but logs its skip at DEBUG."""
    return [
        {
            "type": "table-missing-scope",
            "severity": "minor",
            "wcag_criterion": "1.3.1",
            "description": f"issue {i}",
            "remediation_status": "needs_remediation",
        }
        for i in range(n)
    ]


def test_benign_skips_emit_an_info_progress_line(caplog):
    """A long run of DEBUG-suppressed skips must still report progress at INFO.

    Without this, the remediation loop can run for minutes emitting nothing,
    which is what made a healthy pipeline look hung.
    """
    mgr = RemediationManager(BeautifulSoup("<html><body></body></html>", "html.parser"))
    # Force every issue down the benign-skip path: strategy present, returns None.
    mgr.strategies["table-missing-scope"] = lambda _soup, _issue, *_a: None

    with caplog.at_level(logging.INFO, logger="remediate.remediation_manager"):
        mgr.remediate_issues(_benign_issues(250))

    info_lines = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert info_lines, (
        "250 benign skips produced no INFO output — a silent pipeline is "
        "indistinguishable from a hung one"
    )


def test_progress_line_reports_how_many_were_skipped(caplog):
    """The progress line must carry the count, not just prove liveness."""
    mgr = RemediationManager(BeautifulSoup("<html><body></body></html>", "html.parser"))
    mgr.strategies["table-missing-scope"] = lambda _soup, _issue, *_a: None

    with caplog.at_level(logging.INFO, logger="remediate.remediation_manager"):
        mgr.remediate_issues(_benign_issues(250))

    text = " ".join(r.getMessage() for r in caplog.records)
    assert "250" in text or "200" in text, (
        f"no skip count in INFO output: {text[:300]}"
    )


def test_short_runs_stay_quiet(caplog):
    """A handful of benign skips must not add noise — the fix is for long runs."""
    mgr = RemediationManager(BeautifulSoup("<html><body></body></html>", "html.parser"))
    mgr.strategies["table-missing-scope"] = lambda _soup, _issue, *_a: None

    with caplog.at_level(logging.INFO, logger="remediate.remediation_manager"):
        mgr.remediate_issues(_benign_issues(3))

    progress = [r for r in caplog.records
                if r.levelno >= logging.INFO and "skipped" in r.getMessage().lower()]
    assert not progress, (
        f"3 benign skips should stay quiet, got: {[r.getMessage() for r in progress]}"
    )

