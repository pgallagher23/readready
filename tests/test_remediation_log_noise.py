# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manifest-log noise control for benign per-issue remediation skips.

Some audit issues are intentionally not fixed by a per-issue strategy in the
RemediationManager:

* ``filename-like-title`` / ``filename-like-heading`` are resolved earlier by
  the structural title pre-pass (``remediator._apply_structural_fixes``), so the
  manager has no strategy registered for them.
* ``table-missing-scope`` is moot for the output PDF because the iText tagger
  writes ``/Scope`` on every TH structure element.
* the landmark / skip-link issues are added by a sibling landmark fix (the
  strategies chain) or are not applicable to a linear converted document.

These previously logged a scary ``WARNING`` ("no remediation strategy
available") or an ``INFO`` ("strategy could not apply a fix") for every
occurrence — 1500+ lines in a 1000-PDF manifest — without signalling a real
defect. The authoritative compliance state still comes from the
post-remediation audit, so quieting them to ``DEBUG`` hides nothing.
"""

import logging

from bs4 import BeautifulSoup

from remediate.remediation_manager import RemediationManager


def _issue(itype, severity="major", wcag="2.4.2"):
    return {
        "type": itype,
        "severity": severity,
        "wcag_criterion": wcag,
        "description": f"desc for {itype}",
    }


def test_benign_no_strategy_skip_is_debug_not_warning(caplog):
    """filename-like-title has no per-issue strategy (pre-pass handles it);
    its skip must be DEBUG, never WARNING."""
    soup = BeautifulSoup("<html><body><p>x</p></body></html>", "html.parser")
    manager = RemediationManager(soup)
    assert "filename-like-title" not in manager.strategies  # guard the premise

    with caplog.at_level(logging.DEBUG, logger="remediate.remediation_manager"):
        manager.remediate_issues([_issue("filename-like-title")])

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    debugs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert not any("filename-like-title" in m for m in warnings)
    assert any("filename-like-title" in m for m in debugs)
    # still counted as skipped — stats/report semantics unchanged
    assert manager.stats["skipped"] == 1


def test_unknown_type_still_warns(caplog):
    """A genuinely unhandled, non-benign issue type must still WARN so real
    gaps stay visible."""
    soup = BeautifulSoup("<html><body><p>x</p></body></html>", "html.parser")
    manager = RemediationManager(soup)

    with caplog.at_level(logging.DEBUG, logger="remediate.remediation_manager"):
        manager.remediate_issues([_issue("totally-unknown-issue-xyz")])

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("totally-unknown-issue-xyz" in m for m in warnings)


def test_benign_could_not_apply_skip_is_debug(caplog):
    """table-missing-scope has a strategy that may return None; when it does,
    the skip is benign (tagger sets /Scope) and must be DEBUG, not INFO."""
    soup = BeautifulSoup("<html><body><p>x</p></body></html>", "html.parser")
    manager = RemediationManager(soup)
    manager.strategies["table-missing-scope"] = lambda *a, **k: None  # force skip

    with caplog.at_level(logging.DEBUG, logger="remediate.remediation_manager"):
        manager.remediate_issues([_issue("table-missing-scope", wcag="1.3.1")])

    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    debugs = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert not any("table-missing-scope" in m for m in infos)
    assert any("table-missing-scope" in m for m in debugs)


def test_nonbenign_could_not_apply_still_info(caplog):
    """A non-benign strategy that returns None must still log at INFO so the
    skip remains visible in the manifest log."""
    soup = BeautifulSoup("<html><body><p>x</p></body></html>", "html.parser")
    manager = RemediationManager(soup)
    manager.strategies["broken-aria-reference"] = lambda *a, **k: None

    with caplog.at_level(logging.DEBUG, logger="remediate.remediation_manager"):
        manager.remediate_issues([_issue("broken-aria-reference", wcag="4.1.2")])

    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("broken-aria-reference" in m for m in infos)
