#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gradio web interface for ReadReady."""

from utils.raster import safe_dpi
import base64
import collections
import io
import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from html import escape
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import quote

from dotenv import load_dotenv
load_dotenv()

# Gradio's usage analytics and version check call out to api.gradio.app and
# huggingface.co. A self-hosted service must not report usage to a third party.
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")

try:
    import gradio as gr
    import bs4  # noqa: F401
    import docling  # noqa: F401
except ModuleNotFoundError as _e:
    print(
        f"ERROR: Missing dependency ({_e}). "
        "Run inside the project virtualenv:\n"
        "  source .venv/bin/activate && python app.py\n"
        "Or install first:  pip install -r requirements.txt",
        file=sys.stderr,
    )
    sys.exit(1)

from audit.standards import resolve_verdict_config, verdict_from_sr
from output_gen.changes_report import (
    _DOCUMENT_WIDE_TYPES,
    _embed_image,
    _issue_label,
    _page_label,
)
from output_gen.report_page import _NAMES, _LEVEL, _WCAG_URL
from pipeline_core import rerun_output_phases, run_pipeline
from utils.config_loader import load_config
from utils.editor_input import (
    EditorLimits,
    cap_edit_value,
    iter_list_edit_targets,
    normalize_sr_edit_batch,
    normalize_sr_order_batch,
    parse_order_edits,
)
from logging_util.outcome_recorder import (
    OutcomeWriter,
    build_outcome_record,
    new_run_id,
    run_file_log,
    update_latest_symlink,
)
from reporting.constants import (
    _SEV_BADGE, _SEV_DEFAULT, _SR_DEFAULT_SEVERITY, _CE_LINK,
    _ACROBAT_REMEDIATION_INFO, _FIX_HINTS, _ISSUE_DISPLAY_NAMES,
    _md_to_html, _load_remediation_tips, _get_remediation_info,
)
from reporting.issue_location import (
    _get_issue_location, _match_issues_to_bboxes,
    _render_issue_thumbnail, _render_coverage_thumbnail,
)
from reporting.issue_aggregation import (
    _aggregate_issues_by_type, _sr_is_poor, _sr_action_items,
    _verdict_gate_reasons,
)
from reporting.remediation_guide import build_remediation_guide_html

logger = logging.getLogger("pdf_accessibility")
PROJECT_ROOT = Path(__file__).resolve().parent
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Suppress HTTP library debug logs (they dump full request bodies
# including base64-encoded images, bloating log files).
for _noisy in ("urllib3", "requests", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

def _sanitize_for_json(obj):
    """Recursively replace non-JSON-safe values (NaN, Inf, bytes, Path, set).

    Python's json.dumps outputs literal NaN/Infinity for special floats,
    which JavaScript's JSON.parse rejects. This ensures Gradio session
    state can always be serialized safely.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, set):
        return list(obj)
    return obj


# ── Warning → plain-language message mapping ─────────────────────────────
# Each key is a substring matched case-insensitively against raw warning
# strings from pipeline_core (see _build_amber_banner). Keys should be
# lowercase. Values: (category, severity, user_message).
# severity: "error" (red) or "warning" (amber)
_WARNING_MESSAGES = {
    # ── Document Limits (amber) ──
    "images were processed for alt text": (
        "Document Limits", "warning",
        "Some images exceeded the processing limit and may need manual alt text — review them in the Alt Text & Captions tab.",
    ),
    "tables were processed for captions": (
        "Document Limits", "warning",
        "Some tables exceeded the processing limit and may need manual captions — review them in the Alt Text & Captions tab.",
    ),
    "formulas were processed": (
        "Document Limits", "warning",
        "Some formulas exceeded the processing limit and use basic descriptions — review them in the Alt Text & Captions tab.",
    ),
    # ── Output Quality (amber) ──
    "weasyprint re-render": (
        "Output Quality", "warning",
        "PDF was regenerated from HTML — layout may differ and screen reader support is limited.",
    ),
    # ── Output Quality / Advisory (amber) — rendering risks, not blocking ──
    "substitution fonts were injected": (
        "Output Quality", "warning",
        "Substitution fonts were injected — the PDF may render incorrectly in Acrobat. "
        "Use the HTML version or obtain a newer source document.",
    ),
    "unicode mapping could not be generated": (
        "Output Quality", "warning",
        "Some fonts could not be mapped to Unicode — text may be incorrect in screen readers, manual verification required.",
    ),
    # ── Accessibility (red) — the PDF is not conformant as delivered ──
    "figures have no alt text": (
        "Accessibility", "error",
        "Figures are missing alt text, so this PDF is not accessible as delivered. "
        "Add descriptions in the Alt Text & Captions tab before publishing.",
    ),
    # ── Errors (red) — no tagged PDF was produced ──
    "pdf tagging failed": (
        "Error", "error",
        "Tagged PDF could not be generated — the accessible HTML version is still available.",
    ),
    "pdf tagging skipped": (
        "Error", "error",
        "PDF tagging skipped (structure extraction failed) — HTML version is still available.",
    ),
}


def _render_issue_html(issue: dict, show_wcag: bool = True) -> str:
    """Render a single issue as an HTML card with severity badge, optional WCAG info, and fix hint."""
    sev = issue.get("severity", "minor")
    badge_style = _SEV_BADGE.get(sev, _SEV_DEFAULT)
    raw_type = issue.get("type", "")
    display_name = _ISSUE_DISPLAY_NAMES.get(raw_type, raw_type)
    itype = escape(display_name)
    desc = escape(issue.get("description", ""))
    fix = _FIX_HINTS.get(raw_type, "")

    wcag_html = ""
    if show_wcag:
        crit_id = issue.get("wcag_criterion", "")
        crit_name = _NAMES.get(crit_id, "")
        crit_level = _LEVEL.get(crit_id, "")
        crit_url = _WCAG_URL.get(crit_id, "")
        if crit_id:
            label = f'{crit_id} {crit_name}' if crit_name else crit_id
            if crit_level:
                label += f' (Level {crit_level})'
            if crit_url:
                wcag_html = (
                    f'<a href="{crit_url}" target="_blank" rel="noopener" '
                    f'style="color:#475569;text-decoration:underline;font-size:0.6875rem;">'
                    f'{escape(label)}</a>'
                )
            else:
                wcag_html = f'<span style="color:#475569;font-size:0.6875rem;">{escape(label)}</span>'

    fix_html = ""
    if fix:
        fix_html = (
            f'<div style="font-size:0.6875rem;color:#14532d;margin-top:2px;padding-left:4px;">'
            f'Fix: {escape(fix)}</div>'
        )

    return (
        f'<div style="margin-bottom:10px;padding-bottom:8px;'
        f'border-bottom:1px solid #f3f4f6;">'
        f'<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px;">'
        f'<span style="padding:1px 7px;border-radius:3px;font-size:0.625rem;'
        f'font-weight:700;text-transform:uppercase;white-space:nowrap;{badge_style}">'
        f'{escape(sev)}</span>'
        f'{wcag_html}'
        f'</div>'
        f'<div style="font-size:0.75rem;color:#1e293b;font-weight:600;">{itype}</div>'
        f'<div style="font-size:0.75rem;color:#374151;padding-left:4px;">{desc}</div>'
        f'{fix_html}'
        f'</div>'
    )


def _render_sr_issue_html(issue: dict, sr_metrics_html: str = "") -> str:
    """Render an SR quality issue — no severity pill, metrics below the title."""
    raw_type = issue.get("type", "")
    display_name = _ISSUE_DISPLAY_NAMES.get(raw_type, raw_type)
    itype = escape(display_name)
    desc = escape(issue.get("description", ""))
    fix = _FIX_HINTS.get(raw_type, "")

    fix_html = ""
    if fix:
        fix_html = (
            f'<div style="font-size:0.6875rem;color:#14532d;margin-top:2px;padding-left:4px;">'
            f'Fix: {escape(fix)}</div>'
        )

    return (
        f'<div style="margin-bottom:10px;padding-bottom:8px;'
        f'border-bottom:1px solid #f3f4f6;">'
        f'<div style="font-size:0.75rem;color:#1e293b;font-weight:600;">{itype}</div>'
        f'{sr_metrics_html}'
        f'<div style="font-size:0.75rem;color:#374151;padding-left:4px;">{desc}</div>'
        f'{fix_html}'
        f'</div>'
    )


# Informational pipeline metrics that are too noisy for user-faced reports.
_SUPPRESSED_ISSUE_TYPES: set[str] = set()

def _collapse_page_ranges(pages: list[int]) -> str:
    """Collapse a list of page numbers into range notation.

    Example: [1, 2, 3, 5, 8, 9, 10] → "pages 1-3, 5, 8-10"
    """
    if not pages:
        return ""
    nums = sorted(set(pages))
    ranges: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
        else:
            ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = n
    ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
    label = "page" if len(nums) == 1 else "pages"
    return f"{label} {', '.join(ranges)}"


def _render_aggregated_issue_line(entry: dict) -> str:
    """Render a single aggregated issue type as a compact HTML line.

    ``display_html`` (optional) is trusted, developer-authored markup used
    verbatim — e.g. an in-app link. When absent, ``display_name`` is escaped.
    """
    sev = entry["severity"]
    badge_style = _SEV_BADGE.get(sev, _SEV_DEFAULT)
    display = entry.get("display_html") or escape(entry["display_name"])
    pages_str = ""
    if not entry["is_document_wide"] and entry["pages"]:
        pages_str = (
            f' <span style="font-size:0.75rem;color:#4b5563;">'
            f'({_collapse_page_ranges(entry["pages"])})</span>'
        )
    return (
        f'<div style="display:flex;align-items:center;gap:6px;margin-bottom:5px;">'
        f'<span style="padding:1px 7px;border-radius:3px;font-size:0.625rem;'
        f'font-weight:700;text-transform:uppercase;white-space:nowrap;{badge_style}">'
        f'{escape(sev)}</span>'
        f'<span style="font-size:0.75rem;color:#1e293b;font-weight:600;">{display}</span>'
        f'{pages_str}'
        f'</div>'
    )




def _build_unified_status_html(
    post_score: float,
    pre_score: float,
    post_audit: dict,
    pipeline_result: dict,
    verdict_config: dict = None,
    structure: dict = None,
) -> str:
    """Build a single unified status banner combining WCAG, SR quality, and pipeline notes.

    Two states:
      Green  — ``compute_wcag_verdict`` returns "pass" → "Accessible"
      Amber  — anything else → "Needs Review"
      (Red "Failed" is handled by _build_error_html for pipeline-level errors.)

    All text/background colour pairs meet WCAG 2.1 AA (>= 4.5:1 contrast).
    Status is conveyed by text AND colour — not colour alone.
    """
    font = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"

    # ── Partition issues ──────────────────────────────────────────────
    all_issues = post_audit.get("issues", [])
    needs_rem = [
        i for i in all_issues
        if i.get("remediation_status") == "needs_remediation"
        and i.get("type", "") not in _SUPPRESSED_ISSUE_TYPES
    ]
    wcag_issues = [i for i in needs_rem if not i.get("type", "").startswith("pdf-")]
    sr_issues = [i for i in needs_rem if i.get("type", "").startswith("pdf-")]

    # ── WCAG criteria counts (for metrics display) ────────────────────
    by_criterion = post_audit.get("compliance", {}).get("by_criterion", {})
    aa_crit = {c for c, d in by_criterion.items() if _LEVEL.get(c) in ("A", "AA")}
    wcag_failing = set()
    for issue in all_issues:
        if (issue.get("remediation_status") == "needs_remediation"
                and not issue.get("type", "").startswith("pdf-")):
            crit = issue.get("wcag_criterion", "")
            if crit and _LEVEL.get(crit) in ("A", "AA"):
                wcag_failing.add(crit)
    wcag_tested = len(aa_crit)
    wcag_passing = wcag_tested - len(wcag_failing)

    # ── SR data ───────────────────────────────────────────────────────
    sr_data = pipeline_result.get("sr_quality", {}) or {}
    sr_quality = sr_data.get("sr_quality", "unknown") if sr_data else "unknown"
    avg_cov = sr_data.get("avg_coverage", 0) if sr_data else 0
    sr_tagger_mode = sr_data.get("tagger_mode", "") if sr_data else ""
    sr_emr = sr_data.get("element_match_rate") if sr_data else None
    hollow = sr_data.get("hollow_pages", []) if sr_data else []
    fig_total = sr_data.get("figures_total", 0) if sr_data else 0
    fig_missing = sr_data.get("figures_missing_alt", 0) if sr_data else 0

    # ── Pipeline warnings ─────────────────────────────────────────────
    pipeline_warnings = pipeline_result.get("warnings", [])

    # ── Score delta ───────────────────────────────────────────────────
    delta = post_score - pre_score
    delta_str = f"+{delta:.1f}" if delta >= 0 else f"{delta:.1f}"

    # ── Determine unified state ───────────────────────────────────────
    vc = verdict_config or {}
    unified_verdict = verdict_from_sr(post_audit, sr_data, verdict_config)
    is_green = (unified_verdict == "pass")

    # ── Pipeline advisory pills (shared by both banners) ─────────────
    # Built once so the same wording appears regardless of verdict.
    # Substitution fonts, Unicode mapping, and other rendering risks
    # are advisory (amber) — not blocking accessibility failures — so
    # they no longer contradict a green verdict with a standalone red
    # alert.  Details (counts + font names) live in the WCAG Report
    # tab's Processing Notes section.
    warning_pills = _build_warning_pills(pipeline_warnings)

    # Document-level findings that report but no longer fail the document
    # (reading order, since VERDICT_DEFAULTS ``reading_order_advisory``).  They
    # ride the shared pill list so they appear on a GREEN banner too — a
    # non-blocking finding that only rendered on amber would be invisible on
    # exactly the documents the demotion turns green.
    gate_reasons = _verdict_gate_reasons(sr_data, vc)
    warning_pills = warning_pills + [
        _render_gate_reason_line(r) for r in gate_reasons
        if r.get("remediation_status") == "informational"
    ]

    if is_green:
        banner = _build_green_banner(
            font, wcag_passing, wcag_tested, delta_str,
            sr_tagger_mode, sr_emr, avg_cov,
            pre_score, post_score, pipeline_result,
            warning_pills=warning_pills,
        )
    else:
        banner = _build_amber_banner(
            font, wcag_passing, wcag_tested, "needs_review", delta_str,
            wcag_issues, sr_issues, sr_quality, sr_tagger_mode, sr_emr,
            avg_cov, hollow, fig_total, fig_missing, sr_data,
            pipeline_warnings, warning_pills=warning_pills,
            structure=structure, gate_reasons=gate_reasons,
        )

    disclaimer = (
        '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;'
        'padding:10px 14px;margin-top:10px;font-size:0.75rem;color:#475569;line-height:1.6;">'
        '<b>Review Required:</b> '
        'Automated remediation is not a substitute for manual verification. '
        'Please review all alt text for accuracy, confirm the reading order matches '
        'the intended document flow before publishing or distributing this document.'
        '</div>'
    )

    return banner + disclaimer


def _render_gate_reason_line(r: dict) -> str:
    """Render one document-level verdict-gate reason as an issue line.

    Shared by the amber banner's detail block and the advisory pill list, so a
    finding reads identically whether it blocks the verdict or merely reports.
    """
    detail_html = None
    if r.get("type") == "pdf-low-text-coverage":
        detail = (
            f"Text coverage {r['coverage_pct']}% — below the "
            f"{r['threshold_pct']}% threshold required to pass"
        )
    elif r.get("type") == "pdf-reading-order":
        pages = _collapse_page_ranges(r.get("failing_pages", []))
        where = f" on {pages}" if pages else ""
        detail = (
            f"Reading order may not match the visual layout{where} — "
            f"you can review and change it in the Content Editor"
        )
        ce_link = (
            '<a href="#" onclick="return window._gotoContentEditor(this)" '
            'style="color:#1d4ed8;font-weight:700;text-decoration:underline;'
            'cursor:pointer;">Content Editor</a>'
        )
        detail_html = (
            f"Reading order may not match the visual layout{escape(where)} — "
            f"you can review and change it in the {ce_link}"
        )
    else:
        detail = _ISSUE_DISPLAY_NAMES.get(r.get("type", ""), r.get("type", ""))
    return _render_aggregated_issue_line({
        "severity": r.get("severity", "major"),
        "display_name": detail,
        "display_html": detail_html,
        "pages": [],
        "is_document_wide": True,
    })


def _build_warning_pills(pipeline_warnings: list) -> list[str]:
    """Match pipeline warnings against ``_WARNING_MESSAGES`` and render each
    match as an aggregated-issue-line HTML pill.

    Shared by both the green and amber banners so the same advisory pills
    appear regardless of verdict. Returns a list of HTML strings, in match
    order (first-match-wins per warning string).
    """
    pills: list[str] = []
    for warn_str in pipeline_warnings:
        wl = warn_str.lower()
        for pattern, (_category, severity, message) in _WARNING_MESSAGES.items():
            if pattern in wl:
                badge_sev = "critical" if severity == "error" else "major"
                pills.append(_render_aggregated_issue_line({
                    "severity": badge_sev,
                    "display_name": message,
                    "pages": [],
                    "is_document_wide": True,
                }))
                break
    return pills


def _build_green_banner(
    font: str,
    wcag_passing: int = 0,
    wcag_tested: int = 0,
    delta_str: str = "",
    sr_tagger_mode: str = "",
    sr_emr=None,
    avg_cov: float = 0,
    pre_score: float = 0,
    post_score: float = 0,
    pipeline_result: dict = None,
    warning_pills: list = None,
) -> str:
    """Build the green "Accessible" banner — WCAG compliant + screen reader verified.

    Advisory pipeline warnings (e.g. substitution fonts) are rendered as
    pills inside the banner so a green verdict can still surface known
    rendering risks without a contradictory red banner alongside it.
    """
    bg = "#14532d"
    text = "#ffffff"
    sep = "rgba(255,255,255,0.28)"

    pills_html = ""
    if warning_pills:
        pills_html = (
            f'<div style="width:100%;height:1px;background:{sep};margin:10px 0 8px;"></div>'
            f'<div style="background:#ffffff;border-radius:6px;padding:10px 12px;">'
            + "\n".join(warning_pills)
            + '</div>'
        )

    return (
        f'<div style="{font}">'
        f'<div style="border-radius:8px;overflow:hidden;border:1px solid #e5e7eb;'
        f'background:{bg};padding:16px 20px;">'
        f'<div style="font-size:0.6875rem;text-transform:uppercase;letter-spacing:1px;'
        f'color:{text};opacity:0.92;">Document Status</div>'
        f'<div style="font-size:1.375rem;font-weight:700;color:{text};margin-top:2px;">'
        f'Accessible</div>'
        f'<div style="font-size:0.8125rem;color:{text};margin-top:2px;">'
        f'WCAG 2.1 AA Compliant &middot; Screen Reader Verified</div>'
        f'{pills_html}'
        f'</div></div>'
    )


def _count_critical_issues(
    wcag_issues: list,
    hollow: list,
    sr_quality: str,
    fig_missing: int,
    pipeline_warnings: list,
) -> int:
    """Count all critical issues across WCAG audit, SR quality, and pipeline warnings.

    Used by both the amber banner subline and the Overview tab metric card
    so the counts are always consistent.
    """
    n = sum(1 for i in wcag_issues if i.get("severity") == "critical")
    if hollow:
        n += 1
    if sr_quality == "poor":
        n += 1
    if fig_missing > 0:
        n += 1
    # Pipeline warnings with "error" severity are rendered as critical badges
    for warn_str in pipeline_warnings:
        wl = warn_str.lower()
        for pattern, (_cat, severity, _msg) in _WARNING_MESSAGES.items():
            if severity == "error" and pattern in wl:
                n += 1
                break
    return n


def _build_amber_banner(
    font: str,
    wcag_passing: int,
    wcag_tested: int,
    wcag_verdict: str,
    delta_str: str,
    wcag_issues: list,
    sr_issues: list,
    sr_quality: str,
    sr_tagger_mode: str,
    sr_emr,
    avg_cov: float,
    hollow: list,
    fig_total: int,
    fig_missing: int,
    sr_data: dict,
    pipeline_warnings: list,
    warning_pills: list = None,
    structure: dict = None,
    gate_reasons: list = None,
) -> str:
    """Build the "Needs Review" banner — WCAG issues or SR quality concerns.

    ``warning_pills`` are pre-rendered HTML strings (one per matched pipeline
    warning) produced by :func:`_build_warning_pills`. They are appended to
    the issue list inside the banner so the same advisory pills appear
    regardless of verdict.
    """
    bg = "#e0f2f1"     # soft teal background
    text = "#1a3636"   # dark teal text
    sep = "rgba(26,54,54,0.20)"

    n_wcag = len(wcag_issues)
    n_sr = len(sr_issues)

    # Pages affected by WCAG issues (enrich from structure when available)
    wcag_pages = set()
    for i in wcag_issues:
        pg = (i.get("location") or {}).get("page_number")
        if (not pg or pg <= 0) and structure and structure.get("elements"):
            pg, _ = _get_issue_location(i, structure)
        if pg and pg > 0:
            wcag_pages.add(pg)
    n_wcag_pages = len(wcag_pages)

    # ── Detail section (aggregated by issue type with page numbers) ────
    detail_parts: list[str] = []

    aggregated = _aggregate_issues_by_type(wcag_issues, sr_issues, hollow, sr_data=sr_data, structure=structure)

    if aggregated:
        VISIBLE = 5
        for entry in aggregated[:VISIBLE]:
            detail_parts.append(_render_aggregated_issue_line(entry))
        if len(aggregated) > VISIBLE:
            hidden_lines = "\n".join(
                _render_aggregated_issue_line(e) for e in aggregated[VISIBLE:]
            )
            n_more = len(aggregated) - VISIBLE
            detail_parts.append(
                f'<details style="margin-top:4px;">'
                f'<summary style="cursor:pointer;font-size:0.75rem;color:#4b5563;'
                f'padding:4px 0;">Show {n_more} more issue type{"s" if n_more != 1 else ""}</summary>'
                f'<div style="padding-top:6px;">{hidden_lines}</div>'
                f'</details>'
            )

    # Pipeline warning pills (pre-rendered by _build_warning_pills,
    # shared with the green banner so advisory wording is consistent).
    if warning_pills:
        detail_parts.extend(warning_pills)

    # Document-level verdict gates that aren't represented as per-page issues
    # (e.g. average text coverage in the 50–80% dead band). Without these the
    # banner would have an empty detail section and never explain the review.
    # Informational reasons are already in ``warning_pills`` (rendered above for
    # both banners); rendering them here too would duplicate the line.
    for r in (gate_reasons or []):
        if r.get("remediation_status") == "informational":
            continue
        detail_parts.append(_render_gate_reason_line(r))

    # Screen-reader concerns — rendered as page-specific action items rather
    # than a vague "Screen Reader Quality: Fair" grade. Broadly-broken output
    # ("poor") keeps a single headline; everything else is just the page list.
    # Skip concerns already shown by the verdict-gate lines above (reading order,
    # coverage) so they don't appear twice.
    _gate_types = {r.get("type") for r in (gate_reasons or [])}
    _item_gate = {
        "reading_order": "pdf-reading-order",
        "low_coverage": "pdf-low-text-coverage",
        "page_deficit": "pdf-low-text-coverage",
        "emr": "pdf-low-text-coverage",
    }
    sr_items = [
        it for it in _sr_action_items(sr_data)
        if _item_gate.get(it["key"]) not in _gate_types
    ]
    # Broadly-broken output keeps a headline even when its specific concerns are
    # already covered by the gate lines above (so it never renders silently).
    if _sr_is_poor(sr_data):
        detail_parts.append(_render_aggregated_issue_line({
            "severity": "critical",
            "display_name": (
                "This PDF may not be usable by a screen reader — "
                "review the pages below"
            ),
            "pages": [],
            "is_document_wide": True,
        }))
    for item in sr_items:
        detail_parts.append(_render_aggregated_issue_line({
            "severity": item["severity"],
            "display_name": item["text"],
            "display_html": item.get("html"),
            "pages": item["pages"],
            "is_document_wide": not item["pages"],
        }))

    detail_html = "\n".join(detail_parts)

    # SR critical issues line — count only SR/pipeline critical issues (not WCAG audit)
    n_sr_critical = _count_critical_issues(
        [], hollow, sr_quality, fig_missing, pipeline_warnings,
    )
    critical_line = ""
    if n_sr_critical > 0:
        critical_line = (
            f'<div style="font-size:1.375rem;font-weight:700;color:{text};margin-top:2px;">'
            f'Screen Reader &mdash; {n_sr_critical} Critical'
            f' Issue{"s" if n_sr_critical != 1 else ""} Found</div>'
        )

    return (
        f'<div style="{font}">'
        f'<div style="border-radius:8px;overflow:hidden;border:1px solid #b2dfdb;">'
        # Banner header
        f'<div style="background:{bg};padding:16px 20px;">'
        f'<div style="font-size:0.6875rem;text-transform:uppercase;letter-spacing:1px;'
        f'color:{text};opacity:0.92;">Document Status</div>'
        f'<div style="font-size:1.375rem;font-weight:700;color:{text};margin-top:2px;">'
        f'Needs Review</div>'
        f'<div style="font-size:0.8125rem;color:{text};margin-top:2px;">'
        f'WCAG 2.1 AA &mdash; {wcag_passing} of {wcag_tested} Criteria Passing</div>'
        f'{critical_line}'
        f'<div style="width:100%;height:1px;background:{sep};margin:8px 0;"></div>'
        f'</div>'
        # Detail section
        f'<div style="padding:12px 16px;background:#f5fafa;border-top:1px solid #b2dfdb;">'
        f'{detail_html}</div>'
        f'</div></div>'
    )


def _build_integrated_report_html(
    post_audit: dict,
    sr_quality_data: dict,
    pdf_path: str,
    structure: dict,
) -> str:
    """Build an integrated issues report: severity bar + doc-wide + page-by-page.

    Only pages with issues are shown.  Every problematic page gets a screenshot.
    Passing WCAG criteria are not shown.  Returns empty string when no issues.
    """
    # Filter to actionable issues, excluding suppressed types and pdf-* issues
    # (pdf-* issues are shown in the SR Quality status box, not here).
    all_issues = post_audit.get("issues", [])
    issues = [
        i for i in all_issues
        if i.get("remediation_status") == "needs_remediation"
        and i.get("type", "") not in _SUPPRESSED_ISSUE_TYPES
        and not i.get("type", "").startswith("pdf-")
    ]
    hollow_pages = (sr_quality_data or {}).get("hollow_pages", [])

    if not issues and not hollow_pages:
        return ""

    # Split document-wide (HTML audit) vs page-specific
    doc_wide = [i for i in issues if i.get("type", "") in _DOCUMENT_WIDE_TYPES]
    page_specific = [i for i in issues if i.get("type", "") not in _DOCUMENT_WIDE_TYPES]

    # Build page info for thumbnails
    pages_info = {}
    for elem in structure.get("elements", []):
        pno = elem.get("page_no")
        if pno and "height" in elem:
            pages_info.setdefault(str(pno), {}).setdefault("height", elem["height"])
    for pno_str, pdata in structure.get("pages", {}).items():
        pages_info.setdefault(pno_str, {}).update(pdata)

    # Group page-specific issues by page
    _SEV_ORDER = {"critical": 0, "major": 1, "minor": 2}
    page_issues: dict[int, list] = {}
    # Issues whose page can't be determined — bucketed separately so they are
    # NOT falsely piled onto page 1 (a long-standing mis-attribution).
    unresolved_issues: list = []
    has_elems = bool(structure.get("elements"))

    for issue in page_specific:
        loc = issue.get("location") or {}
        page_no = loc.get("page_number")
        bbox = None
        if has_elems:
            struct_page, struct_bbox = _get_issue_location(issue, structure)
            if struct_bbox is not None:
                bbox = struct_bbox
            # page_number == 0 means "unresolved" (HTML audits set it that way),
            # same as None — accept the structure-resolved page even without a
            # bbox, so e.g. formula issues land on their real page not page 1.
            if (page_no is None or page_no == 0) and struct_page and struct_page > 0:
                page_no = struct_page
        sev = issue.get("severity", "minor")
        if page_no is None or page_no == 0:
            # Unknown page — keep page-less rather than defaulting to page 1.
            unresolved_issues.append((_SEV_ORDER.get(sev, 2), issue, None))
            continue
        page_issues.setdefault(page_no, []).append(
            (_SEV_ORDER.get(sev, 2), issue, bbox)
        )

    sorted_pages = sorted(page_issues.keys())

    sections = []

    # ── Document-wide issues ──────────────────────────────────────────
    if doc_wide:
        dw_items = "".join(_render_issue_html(i) for i in doc_wide)
        sections.append(
            f'<div style="border:1px solid #e5e7eb;border-radius:6px;padding:14px;'
            f'margin-bottom:12px;background:#f9fafb;">'
            f'<div style="font-size:0.875rem;font-weight:700;color:{BRAND_PRIMARY};'
            f'margin-bottom:10px;">Document-Wide Issues ({len(doc_wide)})</div>'
            f'{dw_items}'
            f'</div>'
        )

    # ── Page-by-page cards ────────────────────────────────────────────
    for page_no in sorted_pages:
        issue_list = page_issues[page_no]
        issue_list.sort(key=lambda x: x[0])
        count = len(issue_list)

        # Screenshot — use first issue's bbox for annotation, full page otherwise
        thumb_html = ""
        if pdf_path:
            first_bbox = issue_list[0][2] if issue_list else None
            b64 = _render_issue_thumbnail(
                pdf_path, page_no, first_bbox, pages_info,
                dpi=100 if first_bbox else 60,
                annotate=bool(first_bbox),
            )
            if b64:
                thumb_html = (
                    f'<img src="data:image/png;base64,{b64}" '
                    f'style="max-width:100%;max-height:200px;border:1px solid #e2e8f0;'
                    f'border-radius:4px;display:block;margin-bottom:10px;" '
                    f'alt="Page {page_no} screenshot"/>'
                )

        # Render first 3 issues inline, rest in expander
        visible_html = "".join(_render_issue_html(iss) for _, iss, _ in issue_list[:3])
        overflow_html = ""
        if count > 3:
            hidden = "".join(_render_issue_html(iss) for _, iss, _ in issue_list[3:])
            overflow_html = (
                f'<details style="margin-top:4px;">'
                f'<summary style="cursor:pointer;font-size:0.75rem;color:#4b5563;'
                f'padding:4px 0;">Show {count - 3} more issue{"s" if count - 3 != 1 else ""}</summary>'
                f'<div style="padding-top:6px;">{hidden}</div>'
                f'</details>'
            )

        sections.append(
            f'<div style="border:1px solid #e5e7eb;border-radius:6px;padding:14px;'
            f'margin-bottom:10px;background:#ffffff;">'
            f'<div style="font-size:0.875rem;font-weight:700;color:{BRAND_PRIMARY};'
            f'margin-bottom:10px;">Page {page_no} &mdash; '
            f'{count} issue{"s" if count != 1 else ""}</div>'
            f'{thumb_html}'
            f'{visible_html}{overflow_html}'
            f'</div>'
        )

    # ── Issues with no identifiable page ──────────────────────────────
    # Rendered page-agnostically so they surface without being mis-attributed
    # to page 1 (which is often unaffected).
    if unresolved_issues:
        unresolved_issues.sort(key=lambda x: x[0])
        count = len(unresolved_issues)
        u_visible = "".join(_render_issue_html(iss) for _, iss, _ in unresolved_issues[:3])
        u_overflow = ""
        if count > 3:
            u_hidden = "".join(_render_issue_html(iss) for _, iss, _ in unresolved_issues[3:])
            u_overflow = (
                f'<details style="margin-top:4px;">'
                f'<summary style="cursor:pointer;font-size:0.75rem;color:#4b5563;'
                f'padding:4px 0;">Show {count - 3} more issue{"s" if count - 3 != 1 else ""}</summary>'
                f'<div style="padding-top:6px;">{u_hidden}</div>'
                f'</details>'
            )
        sections.append(
            f'<div style="border:1px solid #e5e7eb;border-radius:6px;padding:14px;'
            f'margin-bottom:10px;background:#ffffff;">'
            f'<div style="font-size:0.875rem;font-weight:700;color:{BRAND_PRIMARY};'
            f'margin-bottom:10px;">Page not identified &mdash; '
            f'{count} issue{"s" if count != 1 else ""}</div>'
            f'{u_visible}{u_overflow}'
            f'</div>'
        )

    # ── Hollow pages ──────────────────────────────────────────────────
    if hollow_pages:
        pages_str = ", ".join(str(p) for p in hollow_pages[:15])
        suffix = f" (+{len(hollow_pages) - 15} more)" if len(hollow_pages) > 15 else ""
        sections.append(
            f'<div style="border:1px solid #fca5a5;border-radius:6px;padding:12px 14px;'
            f'background:#fef2f2;margin-bottom:10px;">'
            f'<div style="font-size:0.8125rem;font-weight:600;color:#991b1b;margin-bottom:4px;">'
            f'Hollow Pages</div>'
            f'<div style="font-size:0.75rem;color:#78350f;">'
            f'Pages {pages_str}{suffix} have visible content but no tagged text. '
            f'Screen readers will skip these pages entirely.</div>'
            f'</div>'
        )

    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;'
        'margin-bottom:14px;">'
        + "".join(sections)
        + '</div>'
    )


def _build_sr_structure_html(pdf_path: str) -> str:
    """Build an inline screen reader structure summary from the tagged PDF.

    Pre-computes a compact view of the first ~30 semantic elements from the
    PDF's structure tree.  Includes a link to open the full side-panel preview.
    Returns empty string on any failure.
    """
    if not pdf_path or not Path(pdf_path).exists():
        return ""

    try:
        from output_gen.screen_reader_preview import extract_structure_tree
    except ImportError:
        return ""

    try:
        tree = extract_structure_tree(pdf_path)
    except Exception:
        return ""
    if not tree or not isinstance(tree, dict) or tree.get("error"):
        return ""

    # Walk the tree and collect the first ~30 leaf/semantic elements
    _MAX_ELEMENTS = 30
    _TAG_COLORS = {
        "H1": "#7c3aed", "H2": "#7c3aed", "H3": "#7c3aed",
        "H4": "#7c3aed", "H5": "#7c3aed", "H6": "#7c3aed",
        "P": "#1e293b", "Span": "#3f4f63",
        "Table": "#0369a1", "TR": "#0369a1", "TH": "#0369a1", "TD": "#0369a1",
        "Figure": "#78350f", "Formula": "#78350f",
        "Link": "#0891b2", "L": "#334155", "LI": "#334155",
    }
    _CONTAINERS = {"Sect", "Div", "Part", "Art", "Document"}
    lines: list[str] = []

    def _walk(node: dict, depth: int) -> None:
        if len(lines) >= _MAX_ELEMENTS:
            return
        tag = node.get("tag", "")
        text = node.get("text")
        alt = node.get("alt")
        children = node.get("children", [])

        indent = "&nbsp;" * (depth * 4)
        tag_color = _TAG_COLORS.get(tag, "#475569")

        # For tables, summarize instead of listing every cell
        if tag == "Table" and children:
            rows = [c for c in children if c.get("tag") in ("TR", "THead", "TBody", "TFoot")]
            tr_count = 0
            for r in rows:
                if r.get("tag") == "TR":
                    tr_count += 1
                else:
                    tr_count += len([c for c in r.get("children", []) if c.get("tag") == "TR"])
            # Count columns from first TR
            first_tr = None
            for r in children:
                if r.get("tag") == "TR":
                    first_tr = r
                    break
                for c in r.get("children", []):
                    if c.get("tag") == "TR":
                        first_tr = c
                        break
                if first_tr:
                    break
            col_count = len(first_tr.get("children", [])) if first_tr else "?"
            lines.append(
                f'{indent}<span style="color:{tag_color};font-weight:600;">{escape(tag)}</span>'
                f' <span style="color:#4b5563;font-size:0.6875rem;">'
                f'{tr_count} rows &times; {col_count} cols</span>'
            )
            return  # Don't recurse into table children

        # Build content preview
        content = ""
        if alt:
            disp = escape(alt[:80]) + ("..." if len(alt) > 80 else "")
            content = f' <span style="color:#78350f;font-size:0.6875rem;">[Alt: {disp}]</span>'
        elif text:
            disp = escape(text[:80]) + ("..." if len(text) > 80 else "")
            content = f' <span style="color:#4b5563;font-size:0.6875rem;">"{disp}"</span>'

        if tag in _CONTAINERS and not text and not alt:
            # Show Document root with lang
            if tag == "Document":
                lines.append(
                    f'{indent}<span style="color:{tag_color};font-weight:600;">{escape(tag)}</span>'
                )
            for child in children:
                _walk(child, depth)
            return

        lines.append(
            f'{indent}<span style="color:{tag_color};font-weight:600;">{escape(tag)}</span>'
            f'{content}'
        )

        for child in children:
            _walk(child, depth + 1)

    try:
        _walk(tree, 0)
    except Exception:
        return ""

    truncated = len(lines) >= _MAX_ELEMENTS
    elements_html = "<br>".join(lines)

    footer = ""
    if truncated:
        footer = (
            '<div style="font-size:0.75rem;color:#4b5563;margin-top:8px;font-style:italic;">'
            f'Showing first {_MAX_ELEMENTS} elements.'
            '</div>'
        )

    # The "Open Full Preview" link clicks the hidden Gradio SR preview button,
    # which triggers the event chain to extract the structure tree and open the panel.
    preview_link = (
        '<div style="margin-top:8px;">'
        '<button onclick="'
        "var btns=document.querySelectorAll('button[id*=sr-preview-btn]');"
        "var btn=btns[btns.length-1]||btns[0];"  # last rendered = current tab
        "if(btn){btn.click();}"
        '" style="background:none;border:1px solid #d1d5db;border-radius:4px;'
        'padding:4px 12px;font-size:0.75rem;color:#374151;cursor:pointer;">'
        'Open Full Preview</button>'
        '</div>'
    )

    return (
        '<details style="font-family:\'SF Mono\',Menlo,Consolas,monospace;'
        'background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;'
        'padding:14px 18px;margin-bottom:14px;">'
        '<summary style="cursor:pointer;font-size:0.875rem;font-weight:700;'
        f'color:{BRAND_PRIMARY};padding:2px 0;font-family:-apple-system,BlinkMacSystemFont,'
        "'Segoe UI',sans-serif;\">Screen Reader Structure</summary>"
        f'<div style="padding-top:10px;font-size:0.75rem;line-height:1.8;">'
        f'{elements_html}'
        f'{footer}'
        f'{preview_link}'
        f'</div>'
        '</details>'
    )


class _LogBuffer:
    """Thread-safe ring buffer that captures log records for a single session."""

    def __init__(self, maxlen=150):
        self._buf: collections.deque = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self.active = False
        self.status = ""
        self.doc_count = 0        # total docs in this batch
        self.doc_index = 0        # 1-based index of doc currently processing
        self.start_time: float | None = None  # time.time() when batch started
        self.last_active_time: float = time.time()  # tracks when buffer was last active

    def append(self, line: str):
        with self._lock:
            self._buf.append(line)

    def get_lines(self):
        with self._lock:
            return list(self._buf)

    def clear(self):
        with self._lock:
            self._buf.clear()
        self.status = ""
        self.doc_count = 0
        self.doc_index = 0
        self.start_time = None

    def set_status(self, text: str):
        self.status = text


class _LogRouter(logging.Handler):
    """Global logging handler that routes records to per-session _LogBuffers.

    Uses threading.local() so each worker thread can be associated with a
    specific session's buffer during pipeline execution.
    """

    def __init__(self):
        super().__init__()
        self._local = threading.local()
        self._buffers: dict[str, _LogBuffer] = {}
        self._lock = threading.Lock()

    def get_or_create_buffer(self, session_id: str) -> _LogBuffer:
        with self._lock:
            if session_id not in self._buffers:
                self._buffers[session_id] = _LogBuffer()
            return self._buffers[session_id]

    def get_buffer(self, session_id: str) -> _LogBuffer | None:
        with self._lock:
            return self._buffers.get(session_id)

    def remove_buffer(self, session_id: str):
        with self._lock:
            self._buffers.pop(session_id, None)

    def prune_stale_buffers(self, max_age_seconds: int = 14400) -> int:
        """Remove inactive buffers older than *max_age_seconds*. Returns count removed."""
        now = time.time()
        with self._lock:
            stale = [
                sid for sid, buf in self._buffers.items()
                if not buf.active and (now - buf.last_active_time) > max_age_seconds
            ]
            for sid in stale:
                del self._buffers[sid]
        return len(stale)

    def bind_thread(self, session_id: str):
        """Associate the current thread with a session's log buffer."""
        self._local.session_id = session_id

    def unbind_thread(self):
        """Disassociate the current thread from any session."""
        self._local.session_id = None

    def active_count(self) -> int:
        """Return the number of sessions with active (running) log buffers."""
        with self._lock:
            return sum(1 for buf in self._buffers.values() if buf.active)

    def active_stats(self) -> dict:
        """Return aggregate stats across all active sessions."""
        with self._lock:
            active_bufs = [b for b in self._buffers.values() if b.active]
        total_users = len(active_bufs)
        total_docs = sum(b.doc_count for b in active_bufs)
        docs_done = sum(max(0, b.doc_index - 1) for b in active_bufs)
        docs_remaining = total_docs - docs_done
        return {
            "active_users": total_users,
            "total_docs": total_docs,
            "docs_remaining": docs_remaining,
        }

    def emit(self, record):
        sid = getattr(self._local, "session_id", None)
        if sid is None:
            return
        buf = self.get_buffer(sid)
        if buf and buf.active:
            buf.append(self.format(record))


_log_router = _LogRouter()
_log_router.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_log_router)


def _load_config() -> dict:
    """Load base config.yaml with fixed pipeline defaults.

    VLM settings (Ollama URL, model) are read from config.yaml only.
    All audit checks are always enabled. OCR is auto-detected.
    PDF generation is always on with itext engine.
    """
    try:
        config = load_config()
    except FileNotFoundError:
        config = {}

    config.setdefault("output", {})
    config["output"]["generate_pdf"] = True
    config["output"]["generate_report"] = True
    config["output"]["pdf_engine"] = "itext"

    config.setdefault("conversion", {})
    config["conversion"]["extract_images"] = True
    config["conversion"]["image_format"] = "png"
    config["conversion"]["do_ocr"] = True

    return config


def _file_mtime(path: str) -> float:
    """Return file mtime in seconds since epoch, or 0.0 if unreadable."""
    if not path:
        return 0.0
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def _cached_first_page_thumb(doc: dict, pdf_path: str) -> str:
    """Cache the Overview thumbnail on the doc dict, keyed by pdf mtime."""
    key = (pdf_path, _file_mtime(pdf_path))
    cached = doc.get("_first_thumb_cache")
    if cached and cached.get("key") == key:
        return cached["uri"]
    uri = _render_first_page_thumbnail(pdf_path, width_px=260) if pdf_path else ""
    doc["_first_thumb_cache"] = {"key": key, "uri": uri}
    return uri


def _cached_editor_html(
    doc: dict, sr_json: str, sr_pdf_path: str, sr_struct_path: str,
) -> tuple:
    """Cache the Content Editor's prose + tree HTML on the doc dict.

    Invalidated when either the structure JSON or the tagged PDF mtime
    changes — which happens automatically when _update_pdf_handler
    re-runs the pipeline post-stages.
    """
    key = (
        sr_struct_path, _file_mtime(sr_struct_path),
        sr_pdf_path, _file_mtime(sr_pdf_path),
        hash(sr_json) if sr_json else 0,
    )
    cached = doc.get("_editor_html_cache")
    if cached and cached.get("key") == key:
        return cached["prose"], cached["tree"]
    prose = _structure_json_to_prose_html(sr_struct_path, editable=True)
    tree = _sr_json_to_html(
        sr_json, pdf_path=sr_pdf_path,
        structure_json_path=sr_struct_path,
        editable=True, sink_id="",
    )
    doc["_editor_html_cache"] = {"key": key, "prose": prose, "tree": tree}
    return prose, tree


def _cached_embed_image(doc: dict, file_path: str) -> str:
    """Cache base64-encoded image data URIs on the doc dict, per-path mtime.

    Used by the Alt Text & Captions tab so we only re-encode an image when
    its file content changes — not on every @gr.render trigger.
    """
    if not file_path:
        return ""
    cache = doc.setdefault("_img_uri_cache", {})
    key = (file_path, _file_mtime(file_path))
    cached = cache.get(file_path)
    if cached and cached.get("key") == key:
        return cached["uri"]
    uri = _embed_image(file_path) or ""
    cache[file_path] = {"key": key, "uri": uri}
    return uri


def _file_url(file_path: str) -> str:
    """Return a Gradio file-serving URL for an on-disk image artifact.

    Used instead of inlining images as base64 in the @gr.render payload.
    Inlined data URIs force the browser to download every image before it
    can paint the page (they all "shoot out" at once); a file URL lets the
    browser fetch each image lazily/asynchronously after the page paints.

    The file's directory must be registered via gr.set_static_paths() — done
    once per session when the output dir is created (see process_uploads).
    Returns "" on failure so callers fall back to the no-thumbnail layout.
    """
    if not file_path:
        return ""
    try:
        abs_path = str(Path(file_path).resolve())
    except Exception:
        return ""
    return "/gradio_api/file=" + quote(abs_path, safe="/")


def _render_first_page_thumbnail(pdf_path: str, width_px: int = 260) -> str:
    """Render page 0 of *pdf_path* as a base64 PNG data URI for inline <img>.

    Used on the Overview tab so users can visually identify a document at a
    glance.  Returns an empty string on any failure (caller should fall back
    to the no-thumbnail layout).
    """
    try:
        import fitz
        doc = fitz.open(pdf_path)
        if len(doc) == 0:
            doc.close()
            return ""
        page = doc[0]
        page_w = page.rect.width or 1.0
        zoom = safe_dpi(page.rect.width, page.rect.height,
                        72.0 * float(width_px) / page_w) / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        doc.close()
        b64 = base64.b64encode(pix.tobytes("png")).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception:
        return ""


def _render_full_page_image(
    pdf_path: str, page_no: int, struct_path: str, dpi: int = 150,
) -> str:
    """Rasterize one PDF page to a PNG on disk and return its file URL.

    Used by the Visual Content Editor as the background the editable boxes
    sit on. The PNG is written under the structure JSON's output dir (already
    registered with gr.set_static_paths and serving image_ref images), so the
    file URL resolves. Returns "" on failure.
    """
    if not pdf_path or page_no < 1:
        return ""
    try:
        out_dir = Path(struct_path).parent / "_visedit"
        out_dir.mkdir(parents=True, exist_ok=True)
        png_path = out_dir / f"page_{page_no}_{dpi}.png"
        # Regenerate only when missing or stale vs the source PDF.
        if not png_path.exists() or _file_mtime(str(png_path)) < _file_mtime(pdf_path):
            import fitz
            doc = fitz.open(pdf_path)
            if page_no < 1 or page_no > len(doc):
                doc.close()
                return ""
            page = doc[page_no - 1]
            zoom = safe_dpi(page.rect.width, page.rect.height, dpi) / 72.0
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            pix.save(str(png_path))
            doc.close()
        return _file_url(str(png_path))
    except Exception:
        return ""


def _compute_untagged_regions(
    pdf_path: str, page_no: int, page_elems: list[dict], pages_info: dict,
) -> list[dict]:
    """Return PDF-coordinate bboxes of text blocks on this page that overlap
    no tagged element — i.e. coverage gaps a screen reader would skip.

    Read-only "awareness" helper for the Visual editor. Reuses gap_recovery's
    block extractor and overlap test. This is an approximation of coverage
    gaps (gap_recovery drops <2-word blocks and merges differently than live
    recovery), not the authoritative recovery set.
    """
    try:
        import fitz
        from output_gen.gap_recovery import _extract_pymupdf_blocks, _bbox_overlap
        doc = fitz.open(pdf_path)
        if page_no < 1 or page_no > len(doc):
            doc.close()
            return []
        page = doc[page_no - 1]
        ph = pages_info.get(str(page_no), {}).get("height", page.rect.height)
        blocks = _extract_pymupdf_blocks(page, ph)
        doc.close()
        tagged = [
            e["bbox"] for e in page_elems
            if e.get("bbox") and all(k in e["bbox"] for k in ("l", "t", "r", "b"))
        ]
        out = []
        for b in blocks:
            bb = b.get("bbox")
            if not bb:
                continue
            if any(_bbox_overlap(bb, t, 15.0) for t in tagged):
                continue
            out.append(bb)
        return out
    except Exception:
        return []


def _build_changes_report_html(
    result: dict,
    post_audit: dict,
) -> str:
    """Build Document Changes Report HTML for the Gradio results tab.

    Shows what the pipeline changed: summary stats, changes table,
    image review thumbnails, and table before/after snapshots.
    """
    rem = result.get("remediation", {})
    change_records = list(rem.get("change_records", []))
    table_changes = rem.get("table_changes", [])
    image_records = result.get("alt_text", {}).get("image_records", [])
    fd_res = result.get("formula_descriptions", {})

    has_pipeline_data = bool(
        result.get("stats") or result.get("audit")
        or result.get("fallback_recovery") or result.get("ocr_recovery")
        or result.get("formula_descriptions", {}).get("descriptions")
        or result.get("phase_status")
    )
    if not change_records and not table_changes and not image_records and not has_pipeline_data:
        return ""

    # Reclassify skipped/failed records that were resolved by batch sweep
    still_failing_types: set = set()
    if post_audit:
        for issue in post_audit.get("issues", []):
            if issue.get("remediation_status") == "needs_remediation":
                still_failing_types.add(issue.get("type", ""))

    change_records = [
        {**r,
         "outcome": (
             "fixed" if r.get("outcome", "skipped") in ("skipped", "failed")
             and r.get("type", "") not in still_failing_types
             else r.get("outcome", "skipped")
         ),
         "detail": (
             "Remediated"
             if r.get("outcome", "skipped") in ("skipped", "failed")
             and r.get("type", "") not in still_failing_types
             else r.get("detail", "")
         )}
        for r in change_records
    ]

    # Add CSS injection entry
    change_records.append({
        "outcome": "fixed", "type": "css-injection",
        "wcag_criterion": "1.4.3", "severity": "minor",
        "description": "Accessible CSS injected",
        "detail": "Accessible CSS stylesheet injected for contrast, spacing, and focus indicators",
    })

    # Add alt text generation entries to the changes table
    _gen_count = sum(1 for r in image_records if r.get("outcome") == "generated")
    _dec_count = sum(1 for r in image_records if r.get("outcome") == "decorative")
    for img_rec in image_records:
        outcome = img_rec.get("outcome", "")
        if outcome == "generated":
            change_records.append({
                "outcome": "fixed", "type": "alt-text-generated",
                "wcag_criterion": "1.1.1", "severity": "critical",
                "description": "Alt text generated by AI",
                "detail": f"AI-generated descriptions for {_gen_count} image{'s' if _gen_count != 1 else ''} — review in the Alt Text & Captions tab",
                "page_number": img_rec.get("page_number"),
            })
        elif outcome == "decorative":
            change_records.append({
                "outcome": "fixed", "type": "decorative-image-marked",
                "wcag_criterion": "1.1.1", "severity": "critical",
                "description": "Image marked as decorative",
                "detail": f"{_dec_count} image{'s' if _dec_count != 1 else ''} marked with empty alt text",
                "page_number": img_rec.get("page_number"),
            })

    # Add formula description entries to the changes table
    fd_descs_cr = fd_res.get("descriptions", {})
    if fd_descs_cr:
        _struct_path_cr = result.get("structure_json_path")
        _struct_elems_cr: list = []
        if _struct_path_cr and Path(_struct_path_cr).exists():
            try:
                _struct_elems_cr = json.loads(
                    Path(_struct_path_cr).read_text(encoding="utf-8")
                ).get("elements", [])
            except Exception:
                pass
        for elem_id, desc_info in fd_descs_cr.items():
            page_no = None
            for elem in _struct_elems_cr:
                if elem.get("id") == elem_id:
                    page_no = elem.get("page_no")
                    break
            change_records.append({
                "outcome": "fixed", "type": "formula-description-generated",
                "wcag_criterion": "1.1.1", "severity": "critical",
                "description": "Formula description generated by AI",
                "detail": (desc_info.get("description") or "")[:80],
                "page_number": page_no,
            })

    fixed_count = sum(1 for r in change_records if r.get("outcome") == "fixed")
    skipped_count = sum(1 for r in change_records if r.get("outcome") in ("skipped", "failed"))
    image_count = len(image_records)
    table_count = len(table_changes)

    # ── Inline styles (brand) ─────────────────────────────────────────
    box_base = (
        "flex:1;min-width:120px;border-radius:6px;padding:14px 16px;"
        "text-align:center;"
    )
    count_style = "font-size:1.75rem;font-weight:700;line-height:1.2;"
    label_style = "font-size:0.75rem;color:#334155;margin-top:2px;"

    # Summary boxes
    summary_html = (
        '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px;">'
        f'<div style="{box_base}background:#dcfce7;border:1px solid #86efac;">'
        f'<div style="{count_style}color:#0f4f28;">{fixed_count}</div>'
        f'<div style="{label_style}">Changes Applied</div></div>'
        f'<div style="{box_base}background:#fef2f2;border:1px solid #fca5a5;">'
        f'<div style="{count_style}color:#991b1b;">{skipped_count}</div>'
        f'<div style="{label_style}">Unresolved Items</div></div>'
        f'<div style="{box_base}background:#eff6ff;border:1px solid #93c5fd;">'
        f'<div style="{count_style}color:#1e3a8a;">{image_count}</div>'
        f'<div style="{label_style}">Images Reviewed</div></div>'
        f'<div style="{box_base}background:#f0f4f8;border:1px solid #d1d5db;">'
        f'<div style="{count_style}color:#374151;">{table_count}</div>'
        f'<div style="{label_style}">Tables Improved</div></div>'
        '</div>'
    )

    # ── Changes table ─────────────────────────────────────────────────────
    # Group change records by (type, outcome)
    by_type: dict = {}
    for rec in change_records:
        itype = rec.get("type", "")
        outcome = rec.get("outcome", "skipped")
        key = (itype, outcome)
        if key not in by_type:
            by_type[key] = {
                "type": itype, "outcome": outcome,
                "wcag_criterion": rec.get("wcag_criterion", ""),
                "detail": rec.get("detail") or rec.get("description") or "",
                "pages": [],
                "is_document_wide": itype in _DOCUMENT_WIDE_TYPES,
            }
        page_num = rec.get("page_number")
        if page_num and page_num not in by_type[key]["pages"]:
            by_type[key]["pages"].append(page_num)

    # Add table changes as grouped entries
    for tc in table_changes:
        cap = tc.get("caption", "") or ""
        itype = f"table-fix:{cap}" if cap else "table-fix"
        detail = "; ".join(tc.get("changes", []))
        key = (itype, "fixed")
        if key not in by_type:
            by_type[key] = {
                "type": itype, "outcome": "fixed",
                "wcag_criterion": "1.3.1", "detail": detail,
                "pages": [], "is_document_wide": False,
                "is_table": True, "caption": cap,
            }
        page_num = tc.get("page_number")
        if page_num and page_num not in by_type[key]["pages"]:
            by_type[key]["pages"].append(page_num)

    for entry in by_type.values():
        entry["pages"].sort()

    outcome_order = {"fixed": 0, "skipped": 1, "failed": 2}
    sorted_entries = sorted(
        by_type.values(),
        key=lambda e: (outcome_order.get(e["outcome"], 9), e["type"]),
    )

    # Badge styles
    _BADGE_STYLES = {
        "fixed": f"background:#dcfce7;color:#0f4f28;border:1px solid #86efac;",
        "skipped": f"background:#fef2f2;color:#991b1b;border:1px solid #fca5a5;",
        "failed": f"background:#fef3c7;color:#713507;border:1px solid #fcd34d;",
    }
    _BADGE_LABELS = {"fixed": "Fixed", "skipped": "Skipped", "failed": "Failed"}

    def _badge_html(outcome: str) -> str:
        style = _BADGE_STYLES.get(outcome, _BADGE_STYLES["skipped"])
        lbl = _BADGE_LABELS.get(outcome, outcome.capitalize())
        return (
            f'<span style="{style}padding:2px 8px;border-radius:3px;'
            f'font-size:0.6875rem;font-weight:600;">{lbl}</span>'
        )

    th_style = (
        f"padding:8px 10px;text-align:left;color:#fff;font-weight:600;"
        f"font-size:0.75rem;"
    )
    td_style = "padding:7px 10px;font-size:0.75rem;color:#374151;border-bottom:1px solid #e5e7eb;"

    changes_rows = []
    for entry in sorted_entries:
        itype = entry["type"]
        is_table = entry.get("is_table", False)
        label = f"Table: {entry.get('caption', '')}" if is_table and entry.get("caption") else (
            "Data table" if is_table else _issue_label(itype)
        )
        wcag = entry["wcag_criterion"]
        detail = entry["detail"]
        outcome = entry["outcome"]
        pages = entry["pages"]

        if pages:
            pages_str = ", ".join(str(p) for p in pages)
            if entry["is_document_wide"]:
                pages_str += " (document-wide)"
        elif entry["is_document_wide"]:
            pages_str = "All pages"
        else:
            pages_str = "\u2014"

        changes_rows.append(
            f'<tr>'
            f'<td style="{td_style}">{escape(label)}</td>'
            f'<td style="{td_style}font-family:monospace;font-size:0.6875rem;">{escape(wcag)}</td>'
            f'<td style="{td_style}">{escape(detail)}</td>'
            f'<td style="{td_style}">{escape(pages_str)}</td>'
            f'<td style="{td_style}">{_badge_html(outcome)}</td>'
            f'</tr>'
        )

    changes_table_html = (
        '<table style="width:100%;border-collapse:collapse;font-size:0.75rem;" role="table">'
        f'<thead><tr style="background:{BRAND_PRIMARY};">'
        f'<th scope="col" style="{th_style}width:22%;">Change</th>'
        f'<th scope="col" style="{th_style}width:60px;">WCAG</th>'
        f'<th scope="col" style="{th_style}">Detail</th>'
        f'<th scope="col" style="{th_style}width:90px;">Pages</th>'
        f'<th scope="col" style="{th_style}width:76px;">Status</th>'
        f'</tr></thead><tbody>'
        + "\n".join(changes_rows)
        + '</tbody></table>'
    )

    # ── Document Profile section ─────────────────────────────────────────
    profile_html = ""
    doc_stats = result.get("stats", {})
    if doc_stats:
        _prof_box = (
            "flex:1;min-width:80px;border-radius:6px;padding:10px 12px;"
            "text-align:center;background:#f0f4f8;border:1px solid #d1d5db;"
        )
        _prof_count = "font-size:1.375rem;font-weight:700;color:#1e3a5f;line-height:1.2;"
        _prof_label = "font-size:0.6875rem;color:#334155;margin-top:2px;"

        doc_lang = result.get("language", "")
        src_file = result.get("source_pdf_path", "")
        if src_file:
            from pathlib import Path as _P
            src_file = _P(src_file).name

        meta_chips = ""
        if src_file:
            meta_chips += (
                f'<span style="display:inline-block;background:#f8f9fc;border:1px solid #d9dbe0;'
                f'border-radius:3px;padding:2px 8px;font-size:0.6875rem;color:#141720;margin-right:6px;">'
                f'Source: <strong>{escape(src_file)}</strong></span>'
            )
        if doc_lang:
            meta_chips += (
                f'<span style="display:inline-block;background:#f8f9fc;border:1px solid #d9dbe0;'
                f'border-radius:3px;padding:2px 8px;font-size:0.6875rem;color:#141720;">'
                f'Language: <strong>{escape(doc_lang)}</strong></span>'
            )

        profile_html = (
            f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px;">'
            f'<div style="{_prof_box}"><div style="{_prof_count}">{doc_stats.get("pages", 0)}</div>'
            f'<div style="{_prof_label}">Pages</div></div>'
            f'<div style="{_prof_box}"><div style="{_prof_count}">{doc_stats.get("figures", 0)}</div>'
            f'<div style="{_prof_label}">Figures</div></div>'
            f'<div style="{_prof_box}"><div style="{_prof_count}">{doc_stats.get("tables", 0)}</div>'
            f'<div style="{_prof_label}">Tables</div></div>'
            f'<div style="{_prof_box}"><div style="{_prof_count}">{doc_stats.get("formulas", 0)}</div>'
            f'<div style="{_prof_label}">Formulas</div></div>'
            f'</div>'
            f'<div style="margin-bottom:14px;">{meta_chips}</div>'
        )

    # ── Improvement Summary section ──────────────────────────────────────
    delta_html = ""
    pre_audit = result.get("audit", {})
    if pre_audit and post_audit:
        pre_score = pre_audit.get("compliance", {}).get("overall_score", 0.0)
        post_score = post_audit.get("compliance", {}).get("overall_score", 0.0)
        pre_issues = pre_audit.get("summary", {}).get("needs_remediation", 0)
        post_issues = post_audit.get("summary", {}).get("needs_remediation", 0)
        resolved = max(0, pre_issues - post_issues)
        delta_val = post_score - pre_score

        if delta_val > 0:
            delta_msg = f"Compliance score improved by {delta_val:.1f} percentage points"
        elif delta_val == 0:
            delta_msg = "Compliance score unchanged"
        else:
            delta_msg = f"Compliance score changed by {delta_val:.1f} percentage points"

        delta_html = (
            f'<h4 style="font-size:0.875rem;font-weight:600;color:{BRAND_PRIMARY};margin:0 0 8px;">'
            f'Improvement Summary</h4>'
            f'<div style="display:flex;border-radius:6px;overflow:hidden;'
            f'background:#1e3a5f;margin-bottom:14px;">'
            f'<div style="flex:1;padding:12px 16px;">'
            f'<div style="font-size:1.375rem;font-weight:700;color:#ffffff;">{pre_score:.1f}% &rarr; {post_score:.1f}%</div>'
            f'<div style="font-size:0.75rem;color:#ffffffee;">{escape(delta_msg)}</div>'
            f'</div>'
            f'<div style="width:1px;background:rgba(255,255,255,0.3);margin:8px 0;"></div>'
            f'<div style="min-width:140px;padding:12px 16px;">'
            f'<div style="font-size:1rem;font-weight:700;color:#ffffff;">{pre_issues} &rarr; {post_issues}</div>'
            f'<div style="font-size:0.75rem;color:#ffffffee;">Issues requiring attention</div>'
            f'<div style="font-size:0.75rem;color:#ffffffee;">{resolved} issue{"s" if resolved != 1 else ""} resolved</div>'
            f'</div></div>'
        )

    # ── Pipeline Processing Summary section ──────────────────────────────
    pipeline_html = ""
    _chips: list = []
    _chip_s = (
        "display:inline-block;background:#f8f9fc;border:1px solid #d9dbe0;"
        "border-radius:3px;padding:3px 8px;font-size:0.6875rem;color:#141720;"
        "margin:0 6px 6px 0;"
    )

    if doc_stats.get("pages") is not None:
        _chips.append(f'Conversion: <strong>{doc_stats["pages"]} pages</strong> processed')

    fb = result.get("fallback_recovery", {})
    if fb.get("pages_recovered", 0) > 0:
        _chips.append(
            f'Text recovery: <strong>{fb["pages_recovered"]} pages</strong> '
            f'rescued ({fb.get("fallback_elements_added", 0)} elements)'
        )

    ocr = result.get("ocr_recovery", {})
    if ocr.get("pages_recovered", 0) > 0:
        _chips.append(
            f'OCR recovery: <strong>{ocr["pages_recovered"]} pages</strong> '
            f'scanned ({ocr.get("fallback_elements_added", 0)} elements)'
        )
    elif ocr.get("ocr_skipped_reason"):
        _chips.append(f'OCR: <strong>skipped</strong> ({escape(ocr["ocr_skipped_reason"])})')

    alt_res = result.get("alt_text", {})
    if alt_res.get("updated_count", 0) > 0:
        _chips.append(f'Alt text: <strong>{alt_res["updated_count"]} images</strong> captioned by AI')

    tc_res = result.get("table_captions", {})
    if tc_res.get("updated_count", 0) > 0:
        _chips.append(f'Table captions: <strong>{tc_res["updated_count"]} tables</strong> captioned by AI')

    fd_count = fd_res.get("updated_count", 0)
    if fd_count > 0:
        _chips.append(f'Formulas: <strong>{fd_count}</strong> described by AI')

    rem_stats = rem.get("stats", {})
    if rem_stats.get("remediated", 0) > 0:
        _chips.append(f'HTML remediation: <strong>{rem_stats["remediated"]} issues</strong> fixed')

    ps = result.get("phase_status", {})
    tag_info = ps.get("tagging", {})
    if tag_info.get("status") == "success" and tag_info.get("mode"):
        _chips.append(f'PDF tagging: <strong>{escape(tag_info["mode"])}</strong> mode')

    stripped = result.get("stripped_text_pages", [])
    if stripped:
        _chips.append(
            f'OCR stripping: <strong>{len(stripped)} pages</strong> cleaned'
        )

    if _chips:
        chips_html = "".join(f'<span style="{_chip_s}">{c}</span>' for c in _chips)
        pipeline_html = (
            f'<h4 style="font-size:0.875rem;font-weight:600;color:{BRAND_PRIMARY};margin:0 0 8px;">'
            f'Pipeline Processing Summary</h4>'
            f'<div style="margin-bottom:14px;">{chips_html}</div>'
        )

    # ── Processing Notes section (only lossy font issues) ─────────────
    _note_s = (
        "display:block;background:#f8f9fc;border:1px solid #d9dbe0;"
        "border-radius:3px;padding:8px 10px;font-size:0.75rem;color:#141720;"
        "margin-bottom:6px;"
    )
    _pill_base_note = (
        "display:inline-block;font-size:0.6875rem;font-weight:600;"
        "padding:2px 8px;border-radius:9999px;margin-right:6px;vertical-align:middle;"
    )
    # Advisory pill (amber) — matches the banner pill severity.
    # Substitution-font / Unicode-mapping issues are rendering risks, not
    # blocking accessibility failures, so they no longer get the red
    # "Critical" badge here either.
    _pill_advisory_note = _pill_base_note + "background:#fef3c7;color:#7c2d12;border:1px solid #fdba74;"

    notes_parts: list = []
    sqw = result.get("source_quality_warnings", {})
    if sqw.get("substitution_fonts_injected", 0) > 0:
        sub_names = sqw.get("substitution_font_names", [])
        names_str = ", ".join(sub_names[:20])
        suffix = f" \u2026 ({len(sub_names)} total)" if len(sub_names) > 20 else ""
        names_detail = (
            f'<br><span style="font-size:0.6875rem;font-family:monospace;color:#3f4f63;">'
            f'{escape(names_str)}{suffix}</span>'
        ) if names_str else ""
        notes_parts.append(
            f'<div style="{_note_s}border-left:3px solid #f59e0b;">'
            f'<span style="{_pill_advisory_note}">Advisory</span>'
            f'Substitution fonts injected for {sqw["substitution_fonts_injected"]} font(s) '
            f'— the PDF may render incorrectly in Acrobat. '
            f'Use the HTML version or obtain a newer source document.'
            f'{names_detail}'
            f'</div>'
        )
    if sqw.get("tounicode_cmaps_failed", 0) > 0:
        notes_parts.append(
            f'<div style="{_note_s}border-left:3px solid #f59e0b;">'
            f'<span style="{_pill_advisory_note}">Advisory</span>'
            f'{sqw["tounicode_cmaps_failed"]} font(s) could not be mapped to Unicode '
            f'— may be incorrect in screen readers, manual verification required.'
            f'</div>'
        )

    notes_html = ""
    if notes_parts:
        notes_html = (
            f'<h4 style="font-size:0.875rem;font-weight:600;color:{BRAND_PRIMARY};margin:16px 0 6px;">'
            f'Processing Notes</h4>'
            + "".join(notes_parts)
        )

    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;">'
        + profile_html
        + delta_html
        + pipeline_html
        + summary_html
        + changes_table_html
        + notes_html
        + '</div>'
    )


def _build_table_previews(html_path: str, caption_keys: list) -> dict:
    """Build mini HTML previews of tables for the caption editor.

    Args:
        html_path: Path to the pipeline's remediated HTML file.
        caption_keys: List of table keys like "table_0", "table_1" that have captions.

    Returns:
        Dict mapping "table_N" -> preview HTML string (first 4 rows).
    """
    from bs4 import BeautifulSoup as BS

    previews = {}
    try:
        html_content = Path(html_path).read_text(encoding="utf-8")
        soup = BS(html_content, "html.parser")
        tables = soup.find_all("table")
    except Exception:
        return previews

    max_preview_rows = 4
    cell_style = (
        "padding:4px 8px;border:1px solid #d1d5db;font-size:0.6875rem;"
        "color:#374151;max-width:200px;word-break:break-word;"
    )
    th_style = cell_style + "background:#f0f4f8;font-weight:600;"
    table_style = (
        "width:100%;border-collapse:collapse;font-size:0.6875rem;"
        "margin:6px 0 8px;table-layout:fixed;"
    )

    for tbl_key in caption_keys:
        idx = int(tbl_key.replace("table_", ""))
        if idx >= len(tables):
            continue
        table = tables[idx]
        rows = table.find_all("tr")
        if not rows:
            continue

        preview_rows = rows[:max_preview_rows]
        row_html_parts = []
        for row in preview_rows:
            cells = row.find_all(["th", "td"])
            cell_parts = []
            for cell in cells:
                tag_name = cell.name
                text = cell.get_text(strip=True)[:80]
                style = th_style if tag_name == "th" else cell_style
                cs = cell.get("colspan")
                rs = cell.get("rowspan")
                attrs = f' style="{style}"'
                if cs and cs != "1":
                    attrs += f' colspan="{cs}"'
                if rs and rs != "1":
                    attrs += f' rowspan="{rs}"'
                cell_parts.append(f"<{tag_name}{attrs}>{escape(text)}</{tag_name}>")
            row_html_parts.append("<tr>" + "".join(cell_parts) + "</tr>")

        total_rows = len(rows)
        truncated = ""
        if total_rows > max_preview_rows:
            truncated = (
                f'<div style="font-size:0.6875rem;color:#4b5563;font-style:italic;'
                f'margin-top:2px;">'
                f'Showing {max_preview_rows} of {total_rows} rows</div>'
            )

        previews[tbl_key] = (
            f'<table style="{table_style}" role="presentation">'
            + "".join(row_html_parts)
            + "</table>"
            + truncated
        )

    return previews


def _build_report_parts(result: dict, pdf_path: Path, verdict_config: dict = None) -> dict:
    """Build HTML report parts from pipeline result.

    Returns dict with keys: status_html, report_html, changes_report_html.
    """
    post_audit_result = result.get("post_audit", {})
    pre_audit_result = result.get("audit", {})

    structure = {}
    structure_json_path = result.get("structure_json_path")
    if structure_json_path and Path(structure_json_path).exists():
        try:
            structure = json.loads(Path(structure_json_path).read_text(encoding="utf-8"))
        except Exception:
            pass

    pre_score = pre_audit_result.get("compliance", {}).get("overall_score", 0.0)
    post_score = post_audit_result.get("compliance", {}).get("overall_score", 0.0)

    status_html = _build_unified_status_html(
        post_score, pre_score, post_audit_result, result,
        verdict_config=verdict_config,
        structure=structure,
    )

    # Integrated report is now absorbed into the status banner.
    report_html = ""

    changes_report_html = _build_changes_report_html(result, post_audit_result)

    remediation_guide_html = build_remediation_guide_html(
        result, str(pdf_path), structure, verdict_config=verdict_config,
    )

    return {
        "status_html": status_html,
        "report_html": report_html,
        "changes_report_html": changes_report_html,
        "remediation_guide_html": remediation_guide_html,
    }


def _run_single_doc_pipeline(
    pdf_path: Path,
    output_dir: Path,
    config: dict,
    progress_fn,
) -> tuple:
    """Run full pipeline on one PDF and build Gradio HTML report.

    Returns:
        (report_parts, download_files, result) — report HTML parts dict,
        list of output file paths, and full pipeline result dict.
    """
    run_id = new_run_id()
    runs_root = PROJECT_ROOT / "logs" / "runs"
    run_dir = runs_root / run_id
    writer = OutcomeWriter(run_dir, run_id)
    with run_file_log(run_dir):
        try:
            result = run_pipeline(
                pdf_path=str(pdf_path),
                output_dir=str(output_dir),
                config=config,
                progress_fn=progress_fn,
                clean_output=True,
            )
        except Exception as exc:
            writer.emit_error_event(pdf_path=str(pdf_path), phase="pipeline",
                                    message=str(exc), exc=exc)
            writer.write_record(build_outcome_record(
                source="production", run_id=run_id, pdf_path=str(pdf_path),
                exc=exc, error_phase="pipeline", config=config))
            update_latest_symlink(runs_root, run_id)
            raise

    _rec = build_outcome_record(source="production", run_id=run_id,
                                pdf_path=str(pdf_path), result=result, config=config)
    writer.write_record(_rec)
    writer.emit_phase_errors(_rec)
    update_latest_symlink(runs_root, run_id)

    # Early return for pipeline-level errors (encrypted PDF, conversion failure)
    if "error" in result:
        report_parts = {
            "status_html": _build_error_html(pdf_path.name, result["error"]),
            "report_html": "",
            "changes_report_html": "",
            "remediation_guide_html": "",
        }
        return report_parts, [], result

    progress_fn(0.95, "Building report...")

    verdict_config = config.get("verdict", {})
    report_parts = _build_report_parts(result, pdf_path, verdict_config=verdict_config)

    download_files = result.get("download_files", [])

    progress_fn(1.0, "Done")
    return report_parts, download_files, result


def _build_error_html(filename: str, error_msg: str) -> str:
    """Return styled error banner HTML for a failed document.

    Matches the unified banner design (red state).  Provides a
    user-friendly reason and action hint where possible.
    """
    font = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
    bg = "#991b1b"
    text = "#ffffff"
    sep = "rgba(255,255,255,0.28)"
    msg_lower = error_msg.lower()

    # Map known failure patterns to user-friendly reason + action.
    # Each entry: (patterns, reason, action) where patterns is a tuple of
    # case-insensitive substring aliases. First entry whose any() alias is
    # in msg_lower wins. Keep the broad-pattern "java" entry last so more
    # specific Java errors (jar not found, itext tagger) can claim first.
    _ERROR_HINTS = [
        (
            ("encrypt",),
            "Encrypted PDF — cannot modify structure",
            "Remove encryption or password protection and re-process.",
        ),
        (
            ("not a valid pdf",),
            "Invalid file format — not a PDF document",
            "Ensure the uploaded file is a valid PDF.",
        ),
        (
            ("cannot read",),
            "File not readable",
            "Check file permissions and try uploading again.",
        ),
        (
            ("timed out", "timeout"),
            "Processing timed out",
            "The document may be too large or complex. Try with fewer pages.",
        ),
        (
            ("conversion failed",),
            "PDF conversion failed — the document could not be parsed",
            "The PDF may be corrupted or use unsupported features. "
            "Try re-saving it from the original application.",
        ),
        (
            ("itext tagger",),
            "PDF tagging failed",
            "The document structure could not be written. "
            "Try re-processing or contact support if the issue persists.",
        ),
        (
            ("jar not found",),
            "Tagging engine unavailable",
            "The iText tagger is not installed. Contact your administrator.",
        ),
        (
            # Java runtime missing — match the actual error markers,
            # not a bare "java" (which over-matches stack traces and
            # any path containing /java/).
            (
                "no such file or directory: 'java'",
                "command not found: java",
                "java: command not found",
            ),
            "Java runtime unavailable",
            "Java 11+ is required for PDF tagging. Contact your administrator.",
        ),
    ]

    reason = error_msg
    action = ""
    matched_hint = False
    for patterns, hint_reason, hint_action in _ERROR_HINTS:
        if any(p in msg_lower for p in patterns):
            reason = hint_reason
            action = hint_action
            matched_hint = True
            break

    action_html = ""
    if action:
        action_html = (
            f'<div style="font-size:0.75rem;color:{text};margin-top:2px;">'
            f'Action: {escape(action)}</div>'
        )

    # Subtitle: when we have a specific matched reason, just show the
    # filename for context. The "Reason:" line below already carries
    # the failure framing — repeating "— document could not be made
    # accessible" is noise. When no hint matched and the reason is the
    # raw exception text, keep the generic framing so the user still
    # sees a clear "this failed" signal.
    if matched_hint:
        subtitle = escape(filename)
    else:
        subtitle = f'{escape(filename)} — document could not be made accessible'

    return (
        f'<div style="{font}">'
        f'<div style="border-radius:8px;overflow:hidden;border:1px solid #e5e7eb;'
        f'background:{bg};padding:16px 20px;">'
        f'<div style="font-size:0.6875rem;text-transform:uppercase;letter-spacing:1px;'
        f'color:{text};opacity:0.92;">Document Status</div>'
        f'<div style="font-size:1.375rem;font-weight:700;color:{text};margin-top:2px;">'
        f'Failed</div>'
        f'<div style="font-size:0.8125rem;color:{text};margin-top:2px;">'
        f'{subtitle}</div>'
        f'<div style="width:100%;height:1px;background:{sep};margin:8px 0;"></div>'
        f'<div style="font-size:0.75rem;color:{text};">Reason: {escape(reason)}</div>'
        f'{action_html}'
        f'<div style="font-size:0.6875rem;color:{text};opacity:0.92;margin-top:6px;">'
        f'If the interface is unresponsive, refresh your browser to start a new session.</div>'
        f'</div></div>'
    )


def _create_bulk_zip(file_paths: list, output_dir: Path) -> Optional[str]:
    """Create a zip archive of all output files.

    Returns the path to the zip file, or None if no files to zip.
    """
    if not file_paths:
        return None
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    zip_path = output_dir / f"accessible_pdfs_{timestamp}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath in file_paths:
            p = Path(fpath)
            if p.exists():
                zf.write(p, arcname=p.name)
    return str(zip_path)


def _filter_downloads(
    file_paths: list,
    include_html: bool,
    include_html_pdf: bool,
    include_report: bool,
) -> list:
    """Filter download files based on user output options."""
    filtered = []
    for fpath in file_paths:
        name = Path(fpath).name.lower()
        is_report = "_validation_report" in name or "_verification_report" in name or "_accessibility_report" in name or "_compliance_report" in name or "_document_changes_report" in name
        is_html_pdf = "_html_accessible.pdf" in name
        if is_html_pdf and not include_html_pdf:
            continue
        if is_report and not include_report:
            continue
        if name.endswith(".html") and not include_html and not is_report:
            continue
        filtered.append(fpath)
    return filtered


def _refilter_downloads(
    session: dict,
    include_html: bool,
    include_html_pdf: bool,
    include_report: bool,
) -> tuple:
    """Re-filter downloads when a user toggles a Download Options checkbox."""
    unfiltered = session.get("all_downloads_unfiltered", [])
    if not unfiltered:
        return session, gr.update(), gr.update()

    filtered = _filter_downloads(unfiltered, include_html, include_html_pdf, include_report)
    session["all_downloads"] = filtered
    session["include_html"] = include_html
    session["include_html_pdf"] = include_html_pdf
    session["include_report"] = include_report

    output_dir = Path(session.get("output_dir", ""))
    zip_path = _create_bulk_zip(filtered, output_dir)
    session["zip_path"] = zip_path

    if filtered:
        dl_update = gr.update(value=filtered)
    else:
        dl_update = gr.update(value=None)

    if zip_path and Path(zip_path).exists() and len(filtered) > 1:
        zip_update = gr.update(value=zip_path)
    else:
        zip_update = gr.update(value=None)

    return session, dl_update, zip_update


# Session output dirs already registered with Gradio's static-file server, so
# we only call set_static_paths() once per dir (it appends without dedupe).
_REGISTERED_STATIC_DIRS: set = set()


def _register_static_dir(output_dir: Path) -> None:
    """Let Gradio serve this session's extracted images via /gradio_api/file=.

    Images are then fetched lazily by the browser instead of being inlined as
    base64 in the render payload. Idempotent per process.
    """
    key = str(output_dir)
    if key in _REGISTERED_STATIC_DIRS:
        return
    try:
        gr.set_static_paths([key])
        _REGISTERED_STATIC_DIRS.add(key)
    except Exception as e:
        logger.debug("set_static_paths failed for %s: %s", key, e)


class _TaskFailed(RuntimeError):
    """A queued task ran and failed (or timed out). Never retried in-process."""


def _revoke_quietly(handle) -> None:
    try:
        handle.revoke()
    except Exception as e:
        logger.warning("Could not revoke queued task: %s", e)


def _poll_celery(handle, deadline_s: float, on_progress,
                 queue_deadline_s: Optional[float] = None) -> dict:
    """Wait for a Celery task, forwarding progress; raise _TaskFailed on failure.

    ``deadline_s`` limits the task's run time and starts only once a worker
    picks the task up, so time spent waiting in the queue does not count.
    ``queue_deadline_s`` (None = no limit) caps the queue wait separately.
    Either limit revokes the task before raising.
    """
    start = time.monotonic()
    run_start = None
    while not handle.is_ready():
        now = time.monotonic()
        if handle.is_queued():
            if queue_deadline_s is not None and now - start > queue_deadline_s:
                _revoke_quietly(handle)
                raise _TaskFailed(
                    "The processing queue is busy; try again later "
                    f"(waited {queue_deadline_s:.0f}s)."
                )
        else:
            if run_start is None:
                run_start = now
            if now - run_start > deadline_s:
                _revoke_quietly(handle)
                raise _TaskFailed(f"task exceeded the {deadline_s:.0f}s deadline")
        on_progress(*handle.get_progress())
        time.sleep(1)
    if handle.failed():
        raise _TaskFailed(handle.get_error())
    return handle.get_result()


def _task_error_parts(pdf_path: Path, message: str):
    """(report_parts, download_files, pipeline_result) for a failed task."""
    report_parts = {
        "status_html": _build_error_html(pdf_path.name, message),
        "report_html": "",
        "changes_report_html": "",
        "remediation_guide_html": "",
    }
    return report_parts, [], {"error": message}


def _stage_upload(pdf_path: Path, output_dir: Path, filename: str) -> Path:
    """Copy an upload into the session directory and return the copy's path.

    Gradio removes its upload cache after ``delete_cache`` seconds, while the
    session directory lives for hours and Update PDF re-reads the source PDF
    from it. Running on a copy gives the source the session's lifetime. A
    path already inside the session directory is returned unchanged.
    """
    staged = Path(output_dir) / filename
    if staged.resolve() != Path(pdf_path).resolve():
        shutil.copy2(pdf_path, staged)
    return staged


def _process_via_celery(pdf_path: Path, filename: str, output_dir: Path,
                        config: dict, doc_progress, log_buf):
    """Run one document on a Celery worker and poll it to completion.

    Returns ``(report_parts, download_files, pipeline_result)``, or None when
    dispatch failed before the task was submitted (the caller then runs the
    document in-process). Once submitted, a failure is reported as an error
    entry and never re-run in the web process, which has no memory or time
    limits.
    """
    submitted = False
    try:
        from tasks import task_runner
        # The worker reads from the shared volume, not the app container's
        # Gradio cache.
        shared_pdf = _stage_upload(pdf_path, output_dir, filename)
        handle = task_runner.submit_task(str(shared_pdf), str(output_dir), config)
        submitted = True
        _seen_phases = [0]

        def _on_progress(frac, phase, phase_log, _dp=doc_progress, _seen=_seen_phases):
            _dp(frac, phase)
            # Append any new phases from the worker's accumulated log
            for new_phase in phase_log[_seen[0]:]:
                log_buf.append(new_phase)
            _seen[0] = len(phase_log)

        task_time_limit = config.get("queue", {}).get("task_time_limit", 600)
        pipeline_result = _poll_celery(
            handle, task_time_limit + 60, _on_progress,
            queue_deadline_s=task_time_limit * 3,
        )
        # Build report parts in app process (needs Gradio HTML helpers)
        if "error" in pipeline_result:
            report_parts, download_files, _ = _task_error_parts(
                pdf_path, pipeline_result["error"])
        else:
            doc_progress(0.95, "Building report...")
            verdict_config = config.get("verdict", {})
            report_parts = _build_report_parts(pipeline_result, pdf_path, verdict_config=verdict_config)
            download_files = pipeline_result.get("download_files", [])
            doc_progress(1.0, "Done")
        return report_parts, download_files, pipeline_result
    except _TaskFailed as task_err:
        # The task ran on a worker and failed or timed out.
        logger.warning("Celery task failed for %s: %s", filename, task_err)
        return _task_error_parts(pdf_path, str(task_err))
    except Exception as celery_err:
        if not submitted:
            logger.warning(
                "Celery dispatch failed for %s (%s), falling back to sync",
                filename, celery_err,
            )
            return None
        logger.exception("Celery task for %s failed after submission", filename)
        return _task_error_parts(
            pdf_path, f"Lost track of the queued task: {celery_err}")


def _shared_output_root(env) -> Path:
    """Directory shared with Celery workers (the compose ``output-data`` volume)."""
    return Path(env.get("READREADY_SHARED_DIR") or "/app/output")


def _check_page_limit(pdf_paths, max_pages: int) -> None:
    """Reject any upload with more than ``max_pages`` pages before processing."""
    import pymupdf
    for p in pdf_paths:
        try:
            with pymupdf.open(p) as doc:
                n = doc.page_count
        except Exception:
            continue  # unreadable PDFs are reported by the pipeline itself
        if n > max_pages:
            raise gr.Error(
                f"{Path(p).name} has {n} pages; the limit is {max_pages}. "
                "Split the document or use the command-line tool."
            )


def process_batch(
    pdf_files,
    include_html: bool,
    include_html_pdf: bool,
    include_report: bool,
    session: dict,
    progress=gr.Progress(track_tqdm=False),
):
    """Process multiple PDFs sequentially, returning per-document results.

    Args:
        pdf_files: List of file paths from gr.File(file_count="multiple").
        include_html: Whether to include HTML files in downloads.
        include_html_pdf: Whether to include HTML-as-PDF in downloads.
        include_report: Whether to include compliance/changes reports in downloads.
        session: gr.State dict with session-level data.
        progress: Gradio progress tracker.

    Returns:
        (download_files, session) — filtered downloads and updated session state.
    """
    if not pdf_files:
        raise gr.Error("Please upload at least one PDF file.")

    config = _load_config()
    max_docs = config.get("ui", {}).get("max_upload_documents", 3)

    if len(pdf_files) > max_docs:
        raise gr.Error(
            f"Maximum {max_docs} documents allowed. You uploaded {len(pdf_files)}. "
            f"Upload fewer documents and try again."
        )

    # Guard against excessive total batch size
    max_file_mb = config.get("ui", {}).get("max_file_size_mb", 25)
    max_batch_mb = max_docs * max_file_mb
    total_size_mb = sum(Path(f).stat().st_size for f in pdf_files) / (1024 * 1024)
    if total_size_mb > max_batch_mb:
        raise gr.Error(
            f"Total batch size ({total_size_mb:.0f} MB) exceeds limit "
            f"({max_batch_mb} MB). Upload fewer or smaller files."
        )
    _check_page_limit(pdf_files, config.get("ui", {}).get("max_pages", 300))

    # Clear previous results so gr.render gets a clean slate
    session["results"] = []
    session["zip_path"] = None
    session["all_downloads"] = []

    # Per-session output directory — use shared volume when Celery is available
    # so the worker container can write output that the app container can read.
    from tasks.task_runner import is_celery_available
    _use_celery = is_celery_available()

    if not session.get("output_dir") or not Path(session["output_dir"]).exists():
        if _use_celery:
            _shared_dir = _shared_output_root(os.environ) / f"session_{uuid.uuid4().hex}"
            _shared_dir.mkdir(parents=True, exist_ok=True)
            session["output_dir"] = str(_shared_dir)
        else:
            session["output_dir"] = str(Path(tempfile.mkdtemp(prefix="pdf_a11y_")))
    output_dir = Path(session["output_dir"])
    _register_static_dir(output_dir)

    # Per-session log buffer
    if not session.get("session_id"):
        session["session_id"] = uuid.uuid4().hex
    sid = session["session_id"]
    log_buf = _log_router.get_or_create_buffer(sid)
    log_buf.clear()
    log_buf.active = True
    log_buf.doc_count = len(pdf_files)
    log_buf.start_time = time.time()
    _log_router.bind_thread(sid)

    n = len(pdf_files)
    all_results = []
    all_downloads = []

    try:
        for doc_idx, pdf_file in enumerate(pdf_files, start=1):
            log_buf.doc_index = doc_idx
            pdf_path = Path(pdf_file)
            filename = pdf_path.name

            def doc_progress(frac, desc="", _idx=doc_idx):
                global_frac = (_idx - 1 + frac) / n
                status_text = f"Document {_idx} of {n}: {desc}"
                log_buf.set_status(status_text)
                progress(global_frac, desc=status_text)

            try:
                if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
                    raise ValueError(f"Invalid PDF file: {filename}")
                with open(pdf_path, "rb") as _f:
                    if _f.read(5) != b"%PDF-":
                        raise ValueError(
                            f"Not a valid PDF (file content does not match): {filename}"
                        )

                celery_parts = None
                if _use_celery:
                    celery_parts = _process_via_celery(
                        pdf_path, filename, output_dir, config, doc_progress, log_buf)
                if celery_parts is not None:
                    report_parts, download_files, pipeline_result = celery_parts
                else:
                    # Sync path — unchanged, preserves progress_fn + live logs.
                    # Also used as fallback when Celery dispatch fails.
                    report_parts, download_files, pipeline_result = _run_single_doc_pipeline(
                        pdf_path=_stage_upload(pdf_path, output_dir, filename),
                        output_dir=output_dir,
                        config=config,
                        progress_fn=doc_progress,
                    )

                # Extract caption data for editing
                image_records = pipeline_result.get("alt_text", {}).get("image_records", [])
                table_captions = pipeline_result.get("table_captions", {})
                formula_descriptions = pipeline_result.get("formula_descriptions", {})

                status = "error" if "error" in pipeline_result else "success"
                doc_entry = {
                    "filename": filename,
                    "status": status,
                    "status_html": report_parts["status_html"],
                    "report_html": report_parts.get("report_html", ""),
                    "changes_report_html": report_parts["changes_report_html"],
                    "remediation_guide_html": report_parts.get("remediation_guide_html", ""),
                    "image_records": image_records,
                    "table_captions": table_captions,
                    "formula_descriptions": formula_descriptions,
                    "table_previews": _build_table_previews(
                        pipeline_result.get("html_path", ""),
                        list(table_captions.get("captions", {}).keys()),
                    ),
                    "pipeline_result": _sanitize_for_json(pipeline_result),
                }
                # Pre-compute WCAG report and SR preview for inline tabs
                _precompute_tab_data(doc_entry, len(all_results))
                all_results.append(doc_entry)
                all_downloads.extend(download_files)

            except Exception as e:
                logger.exception("Failed to process %s", filename)
                all_results.append({
                    "filename": filename,
                    "status": "error",
                    "status_html": _build_error_html(filename, str(e)),
                    "report_html": "",
                    "changes_report_html": "",
                    "remediation_guide_html": "",
                    "image_records": [],
                    "table_captions": {},
                    "formula_descriptions": {},
                    "table_previews": {},
                    "pipeline_result": {},
                })
    finally:
        log_buf.active = False
        log_buf.last_active_time = time.time()
        _log_router.unbind_thread()

    # Filter downloads based on user options
    session["all_downloads_unfiltered"] = list(all_downloads)
    all_downloads = _filter_downloads(all_downloads, include_html, include_html_pdf, include_report)

    # Create bulk zip of all output files
    zip_path = _create_bulk_zip(all_downloads, output_dir)

    session["results"] = all_results
    session["zip_path"] = zip_path
    session["all_downloads"] = all_downloads
    session["include_html"] = include_html
    session["include_html_pdf"] = include_html_pdf
    session["include_report"] = include_report
    progress(1.0, desc="Done!")

    # Build gr.update() for download_output and zip_output so all DOM
    # changes (files visible, zip visible, tabs rendered) arrive in one
    # atomic Gradio event — prevents @gr.render race with .then() chain.
    if all_downloads:
        download_update = gr.update(value=all_downloads)
    else:
        download_update = gr.update(value=None)

    if zip_path and Path(zip_path).exists() and len(all_downloads) > 1:
        zip_update = gr.update(value=zip_path)
    else:
        zip_update = gr.update(value=None)

    # Hide landing page and update session.  render_version is bumped
    # in a separate .then() step so session_state and render_version
    # never change in the same Gradio response (avoids double-render).
    return download_update, zip_update, gr.update(visible=False), session


# --- Branding (from config.yaml `branding:`; see utils/branding.py) ---
from utils.branding import get_branding
_BRAND = get_branding()
BRAND_PRIMARY      = _BRAND.primary
BRAND_ACCENT       = _BRAND.accent
BRAND_NEUTRAL      = _BRAND.neutral
BRAND_WARM_BG      = _BRAND.warm_bg
BRAND_PRIMARY_DARK = _BRAND.primary_dark


def _mix(hex_color: str, other: str, t: float) -> str:
    """Linear blend of two #RRGGBB colors; t=0 → hex_color, t=1 → other."""
    a = [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def _rgba(hex_color: str, alpha: float) -> str:
    """#RRGGBB -> 'rgba(r, g, b, alpha)'."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r}, {g}, {b}, {alpha})"


def _make_palette(base_hex: str, name: str) -> gr.themes.utils.colors.Color:
    """Build an 11-stop Gradio palette around one brand color."""
    return gr.themes.utils.colors.Color(
        c50=_mix(base_hex, "#ffffff", 0.92), c100=_mix(base_hex, "#ffffff", 0.80),
        c200=_mix(base_hex, "#ffffff", 0.60), c300=_mix(base_hex, "#ffffff", 0.40),
        c400=_mix(base_hex, "#ffffff", 0.20), c500=base_hex,
        c600=_mix(base_hex, "#000000", 0.20), c700=_mix(base_hex, "#000000", 0.40),
        c800=_mix(base_hex, "#000000", 0.60), c900=_mix(base_hex, "#000000", 0.75),
        c950=_mix(base_hex, "#000000", 0.90), name=name,
    )


# ── Landing page content (loaded from static/landing_page.html) ──────────────
# Edit static/landing_page.html directly to update the landing page text.
# Re-read on each render so edits are picked up without restarting the server.
_LANDING_PAGE_PATH = _BRAND.landing_page or (Path(__file__).parent / "static" / "landing_page.html")


def _load_landing_page_html() -> str:
    """Read the landing page HTML from disk (live-reload on every call)."""
    try:
        return _LANDING_PAGE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""

# ── Gradio theme ──────────────────────────────────────────────────────────────
_brand_primary_palette = _make_palette(BRAND_PRIMARY, "brand_primary")
_brand_accent_palette = _make_palette(BRAND_ACCENT, "brand_accent")
_brand_neutral_palette = _make_palette(BRAND_NEUTRAL, "brand_neutral")

# Browser-tab / <title> text, e.g. "ReadReady – Example University".
_PAGE_TITLE = " \u2013 ".join(x for x in (_BRAND.product_name, _BRAND.organization) if x)
# Same text escaped for embedding inside a single-quoted JS string literal.
_PAGE_TITLE_JS = json.dumps(_PAGE_TITLE)[1:-1].replace("'", "\\'")

app_theme = gr.themes.Default(
    primary_hue=_brand_primary_palette,
    secondary_hue=_brand_accent_palette,
    neutral_hue=_brand_neutral_palette,
).set(
    # Light mode values
    body_background_fill=BRAND_WARM_BG,
    body_text_color="#1f2937",
    block_background_fill="#ffffff",
    button_primary_background_fill=BRAND_PRIMARY,
    button_primary_background_fill_hover=BRAND_PRIMARY_DARK,
    button_primary_text_color="#ffffff",
    button_primary_text_color_hover="#ffffff",
    button_primary_border_color=BRAND_PRIMARY,
    button_primary_border_color_hover=BRAND_PRIMARY_DARK,
    color_accent=BRAND_PRIMARY,
    color_accent_soft=BRAND_WARM_BG,
    link_text_color=BRAND_PRIMARY,
    link_text_color_hover=BRAND_PRIMARY_DARK,
    link_text_color_active=BRAND_PRIMARY_DARK,
    link_text_color_visited=BRAND_PRIMARY_DARK,
    input_background_fill="#ffffff",
    input_border_color=BRAND_NEUTRAL,
    input_border_color_focus=BRAND_PRIMARY,
    block_border_color=BRAND_NEUTRAL,
    block_label_text_color=BRAND_PRIMARY,
    block_title_text_color=BRAND_PRIMARY,
    loader_color=BRAND_ACCENT,
    # Force dark mode to use identical light-mode values
    body_background_fill_dark=BRAND_WARM_BG,
    body_text_color_dark="#1f2937",
    block_background_fill_dark="#ffffff",
    button_primary_background_fill_dark=BRAND_PRIMARY,
    button_primary_background_fill_hover_dark=BRAND_PRIMARY_DARK,
    button_primary_text_color_dark="#ffffff",
    button_primary_text_color_hover_dark="#ffffff",
    button_primary_border_color_dark=BRAND_PRIMARY,
    button_primary_border_color_hover_dark=BRAND_PRIMARY_DARK,
    color_accent_soft_dark=BRAND_WARM_BG,
    link_text_color_dark=BRAND_PRIMARY,
    link_text_color_hover_dark=BRAND_PRIMARY_DARK,
    link_text_color_active_dark=BRAND_PRIMARY_DARK,
    link_text_color_visited_dark=BRAND_PRIMARY_DARK,
    input_background_fill_dark="#ffffff",
    input_border_color_dark=BRAND_NEUTRAL,
    input_border_color_focus_dark=BRAND_PRIMARY,
    block_border_color_dark=BRAND_NEUTRAL,
    block_label_text_color_dark=BRAND_PRIMARY,
    block_title_text_color_dark=BRAND_PRIMARY,
    loader_color_dark=BRAND_ACCENT,
    border_color_primary_dark=BRAND_NEUTRAL,
    background_fill_primary_dark="#ffffff",
    background_fill_secondary_dark=BRAND_WARM_BG,
    shadow_spread_dark="3px",
)

# ── Custom CSS ────────────────────────────────────────────────────────────────
APP_CSS = f"""
/* Container + content alignment */
.gradio-container {{
    max-width: 100% !important;
}}
/* Match header padding so content aligns with header text */
#main-content-area {{
    padding: 8px 20px 0 24px !important;
}}

/* Force light mode only — disable dark mode */
@media (prefers-color-scheme: dark) {{
    :root, html, body {{
        color-scheme: light !important;
    }}
}}
:root, html, body {{
    color-scheme: light !important;
}}
html[data-theme="dark"] {{
    --body-background-fill: {BRAND_WARM_BG} !important;
}}

/* Progress status bar (normal flow, above log) */
#progress-status-bar {{
    background: {BRAND_PRIMARY};
    color: #ffffff;
    font-weight: 600;
    font-size: 0.875rem;
    padding: 10px 16px;
    border-radius: 6px 6px 0 0;
    display: none;
    align-items: center;
    gap: 10px;
    box-shadow: 0 2px 8px {_rgba(BRAND_PRIMARY, 0.15)};
    box-sizing: border-box;
    margin-top: 8px;
}}
#progress-status-bar.active {{
    display: flex;
}}
#progress-status-bar .spinner {{
    display: inline-block;
    width: 16px; height: 16px;
    border: 2.5px solid rgba(255,255,255,0.3);
    border-top-color: {BRAND_ACCENT};
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    flex-shrink: 0;
}}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}

/* Progress bar wrapper — strip Gradio chrome */
#progress-bar-wrap {{
    padding: 0 !important;
    margin: 0 !important;
    border: none !important;
    background: transparent !important;
    min-height: 0 !important;
}}
#progress-bar-wrap:not(:has(.active)) {{
    display: none !important;
}}
#progress-bar-wrap > div {{
    padding: 0 !important;
    margin: 0 !important;
}}

/* Hide Gradio native progress overlay — we use #progress-status-bar instead */
.wrap.generating,
.progress-level,
.progress-level-inner {{
    display: none !important;
}}
/* WCAG 1.4.3: prevent Gradio from greying out content during processing */
.pending {{
    opacity: 1 !important;
}}

/* Live processing log (normal flow element) */
#log-overlay {{
    padding: 0 !important;
    margin: 0 !important;
    border: none !important;
    background: transparent !important;
    min-height: 0 !important;
}}
#log-overlay:not(:has(.log-lines)) {{
    display: none !important;
}}
#log-overlay > div {{
    padding: 0 !important;
    margin: 0 !important;
}}
.log-lines {{
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 0.6875rem;
    line-height: 1.5;
    color: {_rgba(BRAND_PRIMARY, 1)};
    background: {_rgba(BRAND_WARM_BG, 0.95)};
    padding: 12px;
    margin: 0;
    max-height: 260px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
    border-radius: 0 0 8px 8px;
    border: 1px solid rgba(180, 174, 167, 0.3);
    border-top: none;
    box-sizing: border-box;
}}
.log-lines.log-waiting {{
    color: {_rgba(BRAND_PRIMARY, 0.7)};
    font-style: italic;
    animation: pulse-wait 2s ease-in-out infinite;
}}
@keyframes pulse-wait {{
    0%, 100% {{ opacity: 0.6; }}
    50% {{ opacity: 1; }}
}}

/* ── Document tab wrapping ────────────────────────────────────────────
   Gradio 6.x measures tab buttons in a visually-hidden container and
   moves overflow tabs into a separate Svelte store (DOM removal, not
   CSS hiding).  Fix: make the hidden measurement container full-width
   with flex-wrap so wrapped buttons report small right-offsets and the
   overflow algorithm keeps ALL tabs in visible_tabs.                  */
.tab-container.visually-hidden {{
    width: 100% !important;
    height: auto !important;
    flex-wrap: wrap !important;
}}
.tab-wrapper {{
    height: auto !important;
}}
.tab-container {{
    overflow: visible !important;
    height: auto !important;
}}
div[role="tablist"] {{
    flex-wrap: wrap !important;
    overflow: visible !important;
    height: auto !important;
}}
/* WCAG 2.5.8 — minimum 44px touch target for all tabs */
div[role="tablist"] > button {{
    min-height: 44px !important;
    min-width: 44px !important;
    padding: 8px 16px !important;
    flex-shrink: 0 !important;
    box-sizing: border-box !important;
}}
/* Hide overflow menu — empty with measurement fix, but belt-and-suspenders */
.overflow-menu, .overflow-dropdown {{
    display: none !important;
}}
/* Bold tab label (used for Alt Text & Captions) */
.bold-tab > button {{
    font-weight: 700 !important;
}}
/* Improvement Guide tab — gold highlight to draw attention */
.how-to-fix-tab > button {{
    font-weight: 700 !important;
    background: {BRAND_ACCENT} !important;
    color: {BRAND_PRIMARY} !important;
    border-radius: 6px 6px 0 0 !important;
    border: 1px solid {BRAND_ACCENT} !important;
    border-bottom: none !important;
    padding: 8px 16px !important;
}}
.how-to-fix-tab > button:hover {{
    background: {_mix(BRAND_ACCENT, '#000000', 0.13)} !important;
}}
.how-to-fix-tab > button[aria-selected="true"] {{
    background: {BRAND_ACCENT} !important;
    color: {BRAND_PRIMARY} !important;
    box-shadow: 0 -2px 0 0 {BRAND_PRIMARY} inset;
}}

/* Branded header */
.rr-header {{
    background: {BRAND_PRIMARY};
    color: #fff;
    padding: 14px 20px 14px 24px;
    display: flex;
    align-items: center;
    gap: 14px;
    border-bottom: 3px solid {BRAND_ACCENT};
    position: sticky;
    top: 0;
    z-index: 90;
    margin-bottom: 0;
}}
/* Queue status badge — overlaps header top-right without affecting flow */
#queue-status-badge {{
    position: sticky;
    top: 0;
    z-index: 91;
    height: 0;
    overflow: visible;
    text-align: right;
    padding-right: 24px;
    pointer-events: none;
    /* Pull badge into the header without collapsing space below */
    margin-top: -48px;
    margin-bottom: 48px;
}}
#queue-status-badge * {{
    pointer-events: auto;
}}
.rr-header-wordmark {{
    font-size: 0.8125rem;
    font-weight: 700;
    color: {BRAND_ACCENT};
    letter-spacing: 0.5px;
    text-transform: uppercase;
    border-right: 1px solid rgba(255,255,255,0.3);
    padding-right: 14px;
    margin-right: 4px;
    white-space: nowrap;
}}
.rr-header-title {{
    font-size: 1.1875rem;
    font-weight: 700;
    letter-spacing: 0.2px;
    line-height: 1.2;
    color: #ffffff;
    margin: 0;
    padding: 0;
}}
.rr-header-subtitle {{
    font-size: 0.75rem;
    color: rgba(255,255,255,0.88);
    margin-top: 3px;
}}

/* Button reinforcement — WCAG 2.5.5 AAA target size (min 44×44px).
   Checkboxes get 44×44 touch target via padding.
   Sidebar links get min-height for touch.
   Zero-size Gradio utility buttons (API, Settings) are excluded — not visible. */
button.primary,
button.secondary,
button.lg,
button.sm {{
    min-height: 44px !important;
    min-width: 44px !important;
}}
/* Visually hide Gradio's File component — backend binding stays active */
#pdf-upload {{
    position: absolute !important;
    width: 1px !important;
    height: 1px !important;
    overflow: hidden !important;
    clip: rect(0,0,0,0) !important;
    white-space: nowrap !important;
    border: 0 !important;
    padding: 0 !important;
}}
/* Custom upload area — replaces Gradio's upload box */
#custom-upload-actions .custom-upload-box {{
    background: #ffffff;
    border: 2px dashed #d1d5db;
    border-radius: 8px;
    padding: 24px 16px;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
}}
#custom-upload-actions .custom-upload-box:hover {{
    border-color: {BRAND_PRIMARY};
    background: {_mix(BRAND_PRIMARY, '#ffffff', 0.97)};
}}
#custom-upload-actions .custom-upload-box-label {{
    font-size: 0.9375rem;
    font-weight: 600;
    color: {BRAND_PRIMARY};
    margin-bottom: 4px;
}}
#custom-upload-actions .custom-upload-box-hint {{
    font-size: 0.75rem;
    color: #4b5563;
}}
.custom-upload-progress {{
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    font-size: 0.8125rem;
    font-weight: 600;
    color: {BRAND_PRIMARY};
    padding: 10px 14px;
    margin-bottom: 8px;
    background: {_mix(BRAND_PRIMARY, '#ffffff', 0.97)};
    border: 1px solid {_mix(BRAND_PRIMARY, '#ffffff', 0.76)};
    border-radius: 6px;
}}
.custom-upload-bar {{
    width: 100%;
    height: 6px;
    background: {_mix(BRAND_PRIMARY, '#ffffff', 0.76)};
    border-radius: 3px;
    overflow: hidden;
}}
.custom-upload-bar-fill {{
    height: 100%;
    width: 0%;
    background: {BRAND_PRIMARY};
    border-radius: 3px;
    transition: width 0.3s ease;
}}
.custom-upload-spinner {{
    display: inline-block;
    width: 14px;
    height: 14px;
    border: 2.5px solid {_mix(BRAND_PRIMARY, '#ffffff', 0.76)};
    border-top-color: {BRAND_PRIMARY};
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    flex-shrink: 0;
}}
#custom-upload-actions .custom-upload-btn {{
    background: none;
    border: none;
    cursor: pointer;
    padding: 0;
    width: 100%;
}}
#custom-upload-actions .custom-upload-btn:focus-visible {{
    outline: 2px solid {BRAND_PRIMARY};
    outline-offset: 2px;
    border-radius: 8px;
}}
/* File list */
#custom-upload-actions .custom-file-list {{
    margin-top: 8px;
    border: 1px solid #e5e7eb;
    border-radius: 6px;
    background: #ffffff;
    overflow: hidden;
}}
#custom-upload-actions .custom-file-item {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 4px 8px;
    font-size: 0.8125rem;
    color: #1f2937;
    border-bottom: 1px solid #e5e7eb;
}}
#custom-upload-actions .custom-file-item:last-child {{
    border-bottom: none;
}}
#custom-upload-actions .custom-file-name {{
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
    min-width: 0;
}}
#custom-upload-actions .custom-file-remove {{
    background: none;
    border: 1px solid #d1d5db;
    border-radius: 4px;
    min-width: 44px;
    min-height: 44px;
    cursor: pointer;
    color: #374151;
    font-size: 0.875rem;
    flex-shrink: 0;
    margin-left: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
}}
#custom-upload-actions .custom-file-remove:hover {{
    background: #fee2e2;
    border-color: #fca5a5;
    color: #991b1b;
}}
#custom-upload-actions .custom-file-remove:focus-visible {{
    outline: 2px solid {BRAND_PRIMARY};
    outline-offset: 2px;
}}
#custom-upload-actions .custom-action-btns {{
    display: flex;
    gap: 8px;
    margin-top: 8px;
}}
#custom-upload-actions .custom-action-btn {{
    padding: 0 14px;
    font-size: 0.8125rem;
    font-weight: 600;
    border-radius: 6px;
    cursor: pointer;
    height: 44px;
    min-width: 44px;
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    border: 1px solid {BRAND_PRIMARY};
    background: transparent;
    color: {BRAND_PRIMARY};
    box-sizing: border-box;
}}
#custom-upload-actions .custom-action-btn:hover {{
    background: {BRAND_PRIMARY};
    color: #ffffff;
}}
#custom-upload-actions .custom-action-btn:focus-visible {{
    outline: 2px solid {BRAND_PRIMARY};
    outline-offset: 2px;
}}
/* WCAG 1.4.6 AAA: Gradio .info-text uses #b4aea7 (2.19:1) — override */
.info-text {{
    color: #4b5563 !important;
}}
/* Checkbox touch targets — enlarge the label wrapper (Gradio wraps
   checkbox + label in a label element) for WCAG 2.5.5 AAA (44×44px). */
label:has(> input[type="checkbox"]) {{
    min-height: 44px !important;
    display: flex !important;
    align-items: center !important;
    gap: 8px !important;
}}
button.primary {{
    background: {BRAND_PRIMARY} !important;
    border-color: {BRAND_PRIMARY} !important;
    color: #ffffff !important;
}}
button.primary:hover {{
    background: {BRAND_PRIMARY_DARK} !important;
    border-color: {BRAND_PRIMARY_DARK} !important;
}}

/* Tab selected indicator */
button.selected {{
    color: {BRAND_PRIMARY} !important;
}}
button.selected::after {{
    background-color: {BRAND_PRIMARY} !important;
}}


/* Hide Gradio default footer */
footer.svelte-zxu34v {{
    display: none !important;
}}

/* Hide Gradio default download panels — replaced by custom accessible UI */
#output-files, #zip-output {{
    position: absolute !important;
    width: 1px !important;
    height: 1px !important;
    overflow: hidden !important;
    clip: rect(0,0,0,0) !important;
    white-space: nowrap !important;
    border: 0 !important;
    padding: 0 !important;
}}
/* Collapse Gradio wrapper divs around hidden download components */
div.block:has(> #output-files),
div.block:has(> #zip-output) {{
    display: none !important;
}}
div.block:has(#custom-download-area[style*="display: none"]),
div.block:has(#custom-download-area:empty) {{
    display: none !important;
}}
/* Custom download area */
#custom-download-area {{
    margin-top: 12px;
    background: #f8f5f2;
    border: 1px solid #e0d6cf;
    border-left: 4px solid {BRAND_PRIMARY};
    border-radius: 10px;
    padding: 16px;
}}
#custom-download-area .custom-dl-list {{
    border: 1px solid #e0d6cf;
    border-radius: 8px;
    background: #ffffff;
    overflow: hidden;
}}
#custom-download-area .custom-dl-item {{
    display: flex;
    align-items: center;
    min-height: 44px;
    border-bottom: 1px solid #e5e7eb;
}}
#custom-download-area .custom-dl-item:last-child {{
    border-bottom: none;
}}
#custom-download-area .custom-dl-link {{
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    min-height: 44px;
    padding: 8px 14px;
    color: {BRAND_PRIMARY};
    text-decoration: none;
    font-size: 0.9375rem;
    font-weight: 600;
    transition: background 0.12s;
}}
#custom-download-area .custom-dl-link:hover {{
    background: {_mix(BRAND_PRIMARY, '#ffffff', 0.97)};
}}
#custom-download-area .custom-dl-link:focus-visible {{
    outline: 2px solid {BRAND_PRIMARY};
    outline-offset: -2px;
    border-radius: 4px;
}}
#custom-download-area .custom-dl-icon {{
    flex-shrink: 0;
    font-size: 1.125rem;
}}
#custom-download-area .custom-dl-name {{
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}}
#custom-download-area .custom-dl-zip {{
    margin-top: 8px;
}}
#custom-download-area .custom-dl-zip .custom-dl-link {{
    font-weight: 700;
    border: 2px solid {BRAND_PRIMARY};
    border-radius: 8px;
    justify-content: center;
    text-decoration: none;
}}
#custom-download-area .custom-dl-zip .custom-dl-link:hover {{
    background: {BRAND_PRIMARY};
    color: #fff;
}}
#custom-download-area .custom-dl-heading {{
    font-size: 0.8125rem;
    font-weight: 700;
    color: #1a1a2e;
    margin: 0 0 6px;
    letter-spacing: 0.02em;
}}

/* Alt text / caption editor textboxes — ensure scrollbar on overflow */
.alt-text-editor textarea {{
    overflow-y: auto !important;
    resize: vertical !important;
    max-height: 200px;
}}

/* ── Inner sub-tabs (per-document) ── */
.doc-subtabs .tab-nav {{
    border-bottom: 2px solid #e5e7eb;
}}
.doc-subtabs .tab-nav button {{
    min-height: 44px;
    min-width: 44px;
    padding: 8px 16px;
    font-size: 0.8125rem;
    font-weight: 600;
    color: #4b5563;
    border: none;
    background: transparent;
    cursor: pointer;
    border-bottom: 2px solid transparent;
    margin-bottom: -2px;
    transition: color 0.15s, border-color 0.15s;
}}
.doc-subtabs .tab-nav button:hover {{
    color: {BRAND_PRIMARY};
}}
.doc-subtabs .tab-nav button.selected {{
    color: {BRAND_PRIMARY};
    border-bottom-color: {BRAND_PRIMARY};
}}
/* Outer file tabs: truncate long filenames so the tab bar doesn't wrap and
   collide with the inner subtabs. Scoped to the outer .result-tabs nav only
   (the inner .doc-subtabs nav lives inside a tabpanel, not a direct child). */
.result-tabs > .tab-wrapper .tab-nav button {{
    max-width: 24ch;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}}
/* WCAG 2.2 SC 2.4.7+2.4.11: Focus visible, not obscured */
[role="tab"]:focus-visible {{
    outline: 3px solid #1E3A5F;
    outline-offset: 2px;
    z-index: 1;
}}

/* SR tree container */
.sr-tree-container {{
    font-size: 0.8125rem;
    line-height: 1.5;
}}

/* Tree node styling */
.sr-node > details {{
    margin-left: 16px;
    border-left: 1px solid #e5e7eb;
    padding-left: 8px;
}}
.sr-node > details[open] > summary {{
    margin-bottom: 2px;
}}
.sr-node > details > summary {{
    cursor: pointer;
    padding: 2px 4px;
    border-radius: 3px;
    list-style: none;
    display: flex;
    align-items: flex-start;
    gap: 6px;
}}
.sr-node > details > summary::-webkit-details-marker {{
    display: none;
}}
.sr-node > details > summary .sr-arrow {{
    display: inline-block;
    font-size: 0.5625rem;
    color: #4b5563;
    margin-top: 4px;
    flex-shrink: 0;
    transition: transform 0.15s ease;
}}
.sr-node > details[open] > summary .sr-arrow {{
    transform: rotate(90deg);
}}
.sr-node > details > summary:hover {{
    background: #f9fafb;
}}
/* Leaf nodes (no children) — no arrow */
.sr-leaf {{
    margin-left: 16px;
    border-left: 1px solid #e5e7eb;
    padding: 2px 4px 2px 8px;
    display: flex;
    align-items: flex-start;
    gap: 6px;
}}

/* Tag badges */
.sr-tag {{
    display: inline-block;
    font-size: 0.625rem;
    font-weight: 700;
    font-family: 'SF Mono', 'Cascadia Code', 'Consolas', monospace;
    padding: 1px 5px;
    border-radius: 3px;
    white-space: nowrap;
    flex-shrink: 0;
    margin-top: 1px;
}}
.sr-tag-heading {{ background: #dbeafe; color: #1e40af; }}
.sr-tag-text {{ background: #f3f4f6; color: #374151; }}
.sr-tag-table {{ background: #dcfce7; color: #14532d; }}
.sr-tag-figure {{ background: #f3e8ff; color: #6b21a8; }}
.sr-tag-link {{ background: #ccfbf1; color: #134e4a; }}
.sr-tag-list {{ background: #ffedd5; color: #7c2d12; }}
.sr-tag-container {{ background: #f9fafb; color: #4b5563; }}
.sr-tag-form {{ background: #fce7f3; color: #831843; }}

/* Text content */
.sr-text {{
    color: #1f2937;
    word-break: break-word;
}}
.sr-alt {{
    color: #6b21a8;
    font-style: italic;
    word-break: break-word;
}}
.sr-text-toggle {{
    color: {BRAND_PRIMARY};
    font-size: 0.6875rem;
    cursor: pointer;
    border: none;
    background: none;
    padding: 0;
    text-decoration: underline;
}}
/* Root node — no left border/indent */
.sr-tree-container > .sr-node > details,
.sr-tree-container > details > div > .sr-node > details {{
    margin-left: 0;
    border-left: none;
    padding-left: 0;
}}
/* Page-level details — hide default marker, rotate arrow when open */
.sr-page-details > summary::-webkit-details-marker {{
    display: none;
}}
.sr-page-details[open] > summary .sr-arrow {{
    transform: rotate(90deg);
}}
.sr-preview-error {{
    text-align: center;
    padding: 40px 20px;
    color: #991b1b;
}}

/* Editable SR preview — inline contenteditable affordances */
.sr-edit-text,
.sr-edit-alt {{
    display: inline;
    padding: 1px 4px;
    margin: 0 1px;
    border-radius: 3px;
    border: 1px solid transparent;
    background: rgba(0, 0, 0, 0);
    cursor: text;
    outline: none;
    word-break: break-word;
    white-space: pre-wrap;
    transition: background-color 0.12s ease, border-color 0.12s ease,
                box-shadow 0.12s ease;
}}
.sr-edit-text {{
    color: #1f2937;
}}
.sr-edit-alt {{
    color: #6b21a8;
    font-style: italic;
}}
.sr-edit-text:hover,
.sr-edit-alt:hover {{
    background: #f3f4f6;
    border-color: #d1d5db;
}}
.sr-edit-text:focus,
.sr-edit-alt:focus {{
    background: #eff6ff;
    border-color: #2563eb;
    box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.18);
}}
.sr-edit-dirty {{
    background: #fef3c7 !important;
    border-color: #f59e0b !important;
}}
.sr-edit-dirty::after {{
    content: " \\2022";
    color: #b45309;
    font-weight: 700;
}}
.sr-alt-label {{
    color: #6b21a8;
    font-style: italic;
    font-weight: 600;
}}
/* SR-edit sink: kept visible=True so Gradio mounts the wrapper with
   our elem_id (visible=False omits the id), then visually removed via
   position:absolute off-screen. NOT display:none — that would also drop
   the elem_id wrapper in some Gradio versions. */
.sr-edit-sink {{
    position: absolute !important;
    left: -10000px !important;
    top: auto !important;
    width: 1px !important;
    height: 1px !important;
    overflow: hidden !important;
    pointer-events: none !important;
}}
.sr-edit-sink * {{
    pointer-events: none !important;
}}

/* Reading / Prose view — document-style typography */
.sr-prose-view {{
    max-width: 760px;
    margin: 0 auto;
    padding: 16px 20px 28px;
    font-family: Georgia, "Times New Roman", serif;
    font-size: 1rem;
    line-height: 1.65;
    color: #1f2937;
}}
.sr-prose-view h1, .sr-prose-view h2, .sr-prose-view h3,
.sr-prose-view h4, .sr-prose-view h5, .sr-prose-view h6 {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    color: {BRAND_PRIMARY};
    line-height: 1.25;
    font-weight: 700;
}}
.sr-prose-view h1 {{ font-size: 1.875rem; margin: 1.4em 0 0.45em; }}
.sr-prose-view h2 {{ font-size: 1.5rem;   margin: 1.2em 0 0.4em; }}
.sr-prose-view h3 {{ font-size: 1.25rem;  margin: 1.05em 0 0.35em; }}
.sr-prose-view h4 {{ font-size: 1.0625rem; margin: 0.95em 0 0.3em; }}
.sr-prose-view h5, .sr-prose-view h6 {{ font-size: 1rem; margin: 0.9em 0 0.3em; }}
.sr-prose-view p {{ margin: 0.55em 0; }}
.sr-prose-view figure {{
    margin: 1.2em 0;
    padding: 10px;
    background: #fafafa;
    border: 1px solid #e5e7eb;
    border-radius: 6px;
    text-align: center;
}}
.sr-prose-view figure img {{
    max-width: 100%; height: auto; display: block; margin: 0 auto 8px;
}}
.sr-prose-view figcaption {{
    color: #4b5563; font-style: italic; font-size: 0.875rem;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
.sr-prose-view .sr-prose-img-missing {{
    padding: 24px; color: #9ca3af; font-style: italic;
    background: #fff; border: 1px dashed #d1d5db; border-radius: 4px;
    margin-bottom: 8px;
}}
.sr-prose-view .sr-prose-table-wrap {{
    margin: 1.2em 0; overflow-x: auto;
}}
.sr-prose-view table {{
    border-collapse: collapse; min-width: 100%;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 0.9375rem;
}}
.sr-prose-view caption {{
    text-align: left; padding: 4px 6px; font-style: italic;
    color: #4b5563; font-size: 0.875rem;
}}
.sr-prose-view th, .sr-prose-view td {{
    border: 1px solid #d1d5db; padding: 6px 10px; vertical-align: top;
}}
.sr-prose-view th {{
    background: #f3f4f6; font-weight: 600; text-align: left;
}}
.sr-prose-view ul, .sr-prose-view ol {{
    padding-left: 1.5em; margin: 0.55em 0;
}}
.sr-prose-view li {{ margin: 0.25em 0; }}
.sr-prose-view blockquote {{
    border-left: 3px solid #d1d5db; padding: 4px 16px;
    color: #374151; margin: 1em 0; font-style: italic;
}}
.sr-prose-view pre.sr-prose-code {{
    background: #f3f4f6; border: 1px solid #e5e7eb; border-radius: 4px;
    padding: 10px 12px; font-size: 0.875rem; overflow-x: auto;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}}
.sr-prose-view .sr-formula-latex {{
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    color: #6b21a8; font-size: 0.8125rem;
    background: #faf5ff; padding: 4px 6px; border-radius: 3px;
    margin-top: 4px; display: inline-block;
}}
.sr-prose-view .sr-prose-unknown {{
    color: #6b7280; font-style: italic; padding: 2px 4px;
    border-left: 2px solid #e5e7eb; margin: 0.4em 0;
}}
.sr-prose-view .sr-prose-toc ul {{ list-style: none; padding-left: 0; }}
.sr-prose-view .sr-prose-toc li {{
    border-bottom: 1px dotted #d1d5db; padding: 4px 0;
}}
.sr-prose-empty {{
    text-align: center; padding: 40px 20px; color: #6b7280;
}}

/* Toolbar row: holds the view toggle on the left and the explanatory
   note in the empty space to its right. */
.sr-view-toolbar {{
    display: flex; align-items: center; flex-wrap: wrap;
    gap: 8px 20px;
}}
/* Subtle note sitting to the right of the toggle pills. */
.sr-view-note {{
    display: flex; align-items: center; gap: 7px;
    margin: 8px 4px 12px; max-width: 460px;
    color: #6b7280; font-size: 0.8125rem; line-height: 1.4;
}}
.sr-view-note-icon {{
    flex: 0 0 auto; color: #9ca3af;
    position: relative; top: -0.5px;
}}

/* Segmented pill toggle: Reading <-> Structure */
.sr-view-toggle {{
    display: inline-flex; gap: 2px; padding: 4px;
    background: #f3f4f6; border-radius: 999px;
    border: 1px solid #e5e7eb; margin: 8px 4px 12px;
}}
.sr-view-toggle button {{
    border: none; background: transparent;
    padding: 6px 16px; border-radius: 999px;
    font-size: 0.8125rem; cursor: pointer; color: #4b5563;
    font-family: inherit; font-weight: 500;
    transition: background-color 0.12s, color 0.12s, box-shadow 0.12s;
}}
.sr-view-toggle button:hover {{
    color: #1f2937;
}}
.sr-view-toggle button.active {{
    background: #ffffff; color: {BRAND_PRIMARY};
    font-weight: 600;
    box-shadow: 0 1px 2px rgba(0,0,0,0.08);
}}

/* View visibility — flip via data attribute on the host wrapper */
.sr-view-host[data-view="prose"] .sr-tree-container {{ display: none; }}
.sr-view-host[data-view="tree"] .sr-prose-view {{ display: none; }}

/* Reading-order view: toggle visibility off the shell data-view attr. In order
   mode keep the host (it holds the toggle pills) but hide its prose/tree
   CONTENT, and reveal the Reading-order host. */
.sr-editor-shell[data-view="order"] .sr-view-host .sr-prose-view,
.sr-editor-shell[data-view="order"] .sr-view-host .sr-tree-container,
.sr-editor-shell[data-view="order"] .sr-view-host .sr-edit-hint {{ display: none; }}
.sr-editor-shell:not([data-view="order"]) .ro-page-host {{ display: none; }}

.ro-banner {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap;
  margin:10px 0; padding:9px 12px; border-radius:8px; background:#fffbeb; border:1px solid #fcd34d;
  font-size:0.8125rem; color:#713f12; }}
.ro-chips {{ display:flex; gap:6px; flex-wrap:wrap; }}
.ro-chip {{ font-size:0.75rem; font-weight:700; padding:3px 9px; border-radius:99px;
  background:#fde68a; color:#7c2d12; border:none; cursor:pointer; }}
.ro-chip.on {{ background:#2453ff; color:#fff; }}
.ro-pagebar {{ display:flex; align-items:center; gap:8px; margin:8px 0; font-size:0.8125rem; color:#374151; }}
.ro-nav {{ border:1px solid #d1d5db; border-radius:6px; padding:3px 9px; background:#fff; cursor:pointer; }}
.ro-cur {{ font-weight:700; }}
.ro-hint {{ margin-left:8px; color:#9aa3af; }}
.ro-work {{ display:flex; gap:0; align-items:stretch; }}
.ro-listcol {{ flex:1 1 0; min-width:180px; }}
.ro-splitter {{ flex:0 0 14px; align-self:stretch; cursor:col-resize; position:relative;
  background:transparent; touch-action:none; }}
.ro-splitter::before {{ content:""; position:absolute; top:0; bottom:0; left:6px; width:2px;
  background:#e2e5ea; border-radius:1px; transition:background .12s; }}
.ro-splitter:hover::before, .ro-splitter.ro-splitting::before {{ background:#2453ff; width:3px; left:5px; }}
.ro-pagecol {{ flex:0 0 var(--ro-preview-w, 42%); min-width:150px; max-width:70%; }}
.ro-collabel {{ font-size:0.6875rem; font-weight:700; text-transform:uppercase;
  letter-spacing:.04em; color:#6b7280; margin:0 2px 7px; }}
.ro-list {{ list-style:none; margin:0; padding:0; }}
.ro-row {{ display:flex; align-items:center; gap:9px; padding:8px 10px; margin-bottom:7px;
  border:1px solid #e2e5ea; border-radius:7px; background:#fff; cursor:grab; outline:none; }}
.ro-row:focus {{ border-color:#2453ff; box-shadow:0 0 0 2px #2453ff; }}
.ro-row.ro-dragging {{ border-style:dashed; border-color:#2453ff; background:#f5f8ff;
  box-shadow:0 6px 16px rgba(36,83,255,.22); }}
.ro-grip {{ color:#9aa3af; }}
.ro-num {{ width:20px; height:20px; border-radius:50%; background:#eef1f6; color:#384150;
  font:700 0.6875rem/20px sans-serif; text-align:center; flex:0 0 auto; }}
.ro-typ {{ font-size:0.625rem; font-weight:700; color:#6b7280; background:#f1f3f7;
  padding:2px 7px; border-radius:99px; flex:0 0 auto; }}
.ro-txt {{ color:#374151; font-size:0.8125rem; white-space:normal; overflow-wrap:anywhere; line-height:1.35; }}
.vis-box.ro-box-hot {{ outline:2px solid #f59e0b; background:rgba(245,158,11,.14); }}

.vis-empty {{ padding: 24px; color: #6b7280; font-style: italic; text-align: center; }}

.vis-page-wrap {{
    position: relative; width: 100%; max-width: 900px; margin: 0 auto;
    border: 1px solid #d1d5db; box-shadow: 0 2px 12px rgba(0,0,0,0.12); background: #fff;
}}
.vis-page-img {{ display: block; width: 100%; height: auto; user-select: none; }}
.vis-overlay {{ position: absolute; inset: 0; }}

.vis-box {{
    position: absolute; box-sizing: border-box; border: 1.5px solid;
    border-radius: 3px; min-height: 10px; min-width: 6px;
}}
.vis-box-inner {{
    width: 100%; height: 100%; overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 12px; line-height: 1.25; color: #111827;
}}
.vis-box::before {{
    content: attr(data-tag); position: absolute; top: -9px; left: -1px; z-index: 2;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 8px; font-weight: 700; letter-spacing: 0.3px;
    color: #fff; padding: 1px 5px; border-radius: 3px 3px 3px 0;
    pointer-events: none; opacity: 0; transition: opacity 0.1s; white-space: nowrap;
}}
.vis-box:hover::before, .vis-box:focus-within::before {{ opacity: 1; }}
.vis-seq {{
    position: absolute; top: -10px; right: -10px; z-index: 3;
    width: 18px; height: 18px; border-radius: 50%; background: #2453ff; color: #fff;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    font-size: 10px; font-weight: 700;
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 1px 3px rgba(0,0,0,0.3); pointer-events: none;
}}
.vis-box-text {{ border-color: rgba(38,178,77,0.65); background: rgba(38,178,77,0.06); }}
.vis-box-text::before {{ background: #1f9d4d; }}
.vis-box-empty {{ border-color: rgba(148,163,184,0.7); background: rgba(148,163,184,0.05); border-style: dashed; }}
.vis-box-empty::before {{ background: #64748b; }}
.vis-box-fig {{ border-color: rgba(36,118,230,0.7); background: rgba(36,118,230,0.07); }}
.vis-box-fig::before {{ background: #2476e6; }}
.vis-box-table {{ border-color: rgba(224,122,15,0.75); background: rgba(224,122,15,0.08); cursor: pointer; }}
.vis-box-table:hover {{ background: rgba(224,122,15,0.16); }}
.vis-box-table::before {{ background: #e07a0f; }}
/* List items are editable (like text boxes) but keep an orange accent. */
.vis-box-list {{ border-color: rgba(224,122,15,0.75); background: rgba(224,122,15,0.06); }}
.vis-box-list::before {{ background: #e07a0f; }}
.vis-box-list:hover {{ background: rgba(224,122,15,0.16); }}
.vis-box-untagged {{ border: 1.6px dashed rgba(214,69,69,0.8); background: rgba(214,69,69,0.07); z-index: 0; }}
.vis-box-untagged::before {{ background: #d64545; }}
.vis-box .sr-edit-text, .vis-box .sr-edit-alt {{
    display: block; width: 100%; min-height: 1.2em; outline: none; cursor: text; white-space: normal;
}}
/* Hide the editable OCR text by default — the page image already shows the
   original text, so overlaying the editable copy makes both hard to read.
   Reveal it only when the box is ACTIVE (clicked). opacity:0 keeps the
   contenteditable clickable/focusable so a click still activates it. */
.vis-box-text .vis-box-inner, .vis-box-fig .vis-box-inner, .vis-box-list .vis-box-inner {{ opacity: 0; }}
/* Hover: just a light highlight + tag label (the ::before), no text — so a box
   is discoverable without doubling the page text. */
.vis-box-text:hover {{ background: rgba(38,178,77,0.16); }}
.vis-box-fig:hover {{ background: rgba(36,118,230,0.16); }}
/* Active (clicked) editable box: reveal + expand to fit the WHOLE paragraph
   (NO scrollbars), opaque so it's readable, raised above neighbouring boxes.
   Scoped to text/figure boxes so route (table/list) + untagged keep their look. */
.vis-box-text:focus-within, .vis-box-fig:focus-within, .vis-box-list:focus-within {{
    height: auto !important; min-height: 1.6em; overflow: visible; z-index: 10;
    background: rgba(255,255,255,0.98); box-shadow: 0 2px 12px rgba(36,83,255,0.4);
    border-radius: 4px;
}}
.vis-box-text:focus-within .vis-box-inner, .vis-box-fig:focus-within .vis-box-inner, .vis-box-list:focus-within .vis-box-inner {{
    opacity: 1; overflow: visible; height: auto; padding: 2px 3px;
    font-size: 13px; line-height: 1.4;
}}
.vis-target-flash {{ animation: visTargetFlash 1.4s ease; }}
@keyframes visTargetFlash {{ 0%, 100% {{ background: transparent; }} 25% {{ background: rgba(224,122,15,0.25); }} }}

/* Table cells are editable in place (like list items), one box per cell, with an
   orange accent. Header cells (TH) get a stronger accent so the header row is
   scannable. Hidden until active/hover so the box doesn't double the page text. */
.vis-box-cell {{ border-color: rgba(224,122,15,0.6); background: rgba(224,122,15,0.05); }}
.vis-box-cell::before {{ background: #e07a0f; }}
.vis-box-cell:hover {{ background: rgba(224,122,15,0.16); }}
.vis-box-cell-header {{ border-color: rgba(224,122,15,0.9); background: rgba(224,122,15,0.13); }}
.vis-box-cell-header::before {{ background: #c2620a; }}
.vis-box-cell .vis-box-inner {{ opacity: 0; }}
.vis-box-cell:focus-within {{
    height: auto !important; min-height: 1.6em; overflow: visible; z-index: 10;
    background: rgba(255,255,255,0.98); box-shadow: 0 2px 12px rgba(36,83,255,0.4);
    border-radius: 4px;
}}
.vis-box-cell:focus-within .vis-box-inner {{
    opacity: 1; overflow: visible; height: auto; padding: 2px 3px;
    font-size: 13px; line-height: 1.4;
}}

/* Inline edit hint (rendered once in HTML, not via ::before — Gradio's
   double-wrapper layout duplicated the pseudo-element). */
.sr-edit-hint {{
    font-size: 0.75rem;
    color: #4b5563;
    background: #f9fafb;
    border: 1px solid #e5e7eb;
    border-radius: 6px;
    padding: 8px 10px;
    margin-bottom: 10px;
}}

"""

# ── Custom JS ─────────────────────────────────────────────────────────────────
APP_JS = r"""
(function() {
    'use strict';

    // Reading-order page load: how long to keep re-issuing the sink write while
    // waiting for Gradio to bind the field (20 x 300ms = 6s, then give up).
    var _RO_LOAD_MAX_TRIES = 20;
    var _RO_LOAD_RETRY_MS = 300;

    // Shared helper: set an <input>/<textarea> value the framework-aware way and
    // dispatch input+change so Gradio's Svelte layer registers it. Defined at this
    // OUTER scope so BOTH initSrEditBridge and initSrOrderBridge (sibling IIFEs)
    // can call it — a reorder flush in initSrOrderBridge relies on it.
    function setProgrammaticValue(el, value) {
        if (!el) return false;
        var proto = (el.tagName === 'TEXTAREA')
            ? window.HTMLTextAreaElement.prototype
            : window.HTMLInputElement.prototype;
        var descr = Object.getOwnPropertyDescriptor(proto, 'value');
        try {
            if (descr && descr.set) {
                descr.set.call(el, value);
            } else {
                el.value = value;
            }
        } catch (e) {
            el.value = value;
        }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
    }

    // --- Force light mode only ---
    // Meta tag prevents @media (prefers-color-scheme: dark) from ever matching
    var meta = document.createElement('meta');
    meta.name = 'color-scheme';
    meta.content = 'light only';
    document.head.appendChild(meta);
    document.documentElement.setAttribute('data-theme', 'light');
    document.documentElement.style.colorScheme = 'light';
    // Prevent Gradio from switching to dark mode
    new MutationObserver(function(mutations) {
        mutations.forEach(function(m) {
            if (m.attributeName === 'data-theme' &&
                document.documentElement.getAttribute('data-theme') !== 'light') {
                document.documentElement.setAttribute('data-theme', 'light');
            }
        });
    }).observe(document.documentElement, { attributes: true });

    // --- Progress status bar + log auto-scroll ---
    (function watchProgress() {
        var bar = document.getElementById('progress-status-bar');
        if (!bar) { setTimeout(watchProgress, 500); return; }
        var lastText = '';
        setInterval(function() {
            var logEl = document.querySelector('#log-overlay .log-lines');
            var txt = logEl ? (logEl.getAttribute('data-status') || '') : '';
            if (txt && txt !== lastText) {
                lastText = txt;
                bar.innerHTML = '<span class="spinner"></span>' + txt;
                bar.classList.add('active');
                if (logEl) logEl.scrollTop = logEl.scrollHeight;
            } else if (!txt && lastText) {
                lastText = '';
                bar.classList.remove('active');
                bar.innerHTML = '';
            }
        }, 300);
    })();

    // --- WCAG fix: document-title (1) ---
    // Gradio does not render a <title> in the initial HTML; inject one.
    if (!document.querySelector('title')) {
        var t = document.createElement('title');
        t.textContent = '__RR_PAGE_TITLE__';
        document.head.appendChild(t);
    }

    function init() {
        if (!document.querySelector('.gradio-container')) { setTimeout(init, 100); return; }

        // --- WCAG fix: aria-prohibited-attr (2) ---
        // Gradio puts aria-label on plain <div class="empty"> with no role.
        document.querySelectorAll('div.empty[aria-label]').forEach(function(el) {
            if (!el.getAttribute('role')) {
                el.setAttribute('role', 'status');
            }
        });

    }
    init();

    // --- WCAG fix: hidden-focusable-content (4.1.2) ---
    // Gradio wraps some buttons in aria-hidden containers or hides utility
    // buttons (API, Settings) that remain focusable. Neutralize them all.
    function fixHiddenFocusable() {
        // 1. Focusable elements inside aria-hidden containers
        document.querySelectorAll('[aria-hidden="true"] button, [aria-hidden="true"] a, [aria-hidden="true"] input').forEach(function(el) {
            // Preserve #pdf-upload elements — JS proxy needs click() to work
            if (el.closest('#pdf-upload')) return;
            el.setAttribute('tabindex', '-1');
            if (el.tagName === 'BUTTON' || el.tagName === 'INPUT') {
                el.disabled = true;
            }
        });
        // 2. Gradio hidden file inputs — display:none but still keyboard-focusable
        document.querySelectorAll('input[type="file"]').forEach(function(el) {
            if (el.style.display === 'none' || el.offsetParent === null) {
                el.setAttribute('aria-hidden', 'true');
                el.setAttribute('tabindex', '-1');
            }
        });
        // 3. Gradio utility buttons that are zero-size or display:none
        document.querySelectorAll('button.show-api, button.settings, button.record').forEach(function(el) {
            el.setAttribute('tabindex', '-1');
            el.setAttribute('aria-hidden', 'true');
            el.disabled = true;
        });
        // 3. Any button/link whose computed rect is 0×0 (invisible but in DOM).
        //    Skip buttons inside @gr.render areas — they start 0×0 during re-render
        //    and the MutationObserver would permanently disable them.
        document.querySelectorAll('button, a[href]').forEach(function(el) {
            var r = el.getBoundingClientRect();
            if (r.width === 0 && r.height === 0 && !el.disabled) {
                if (el.closest('[id^="render"]') || el.closest('.tabitem') || el.closest('#output-files') || el.closest('#zip-output') || el.closest('#pdf-upload')) return;
                el.setAttribute('tabindex', '-1');
                if (el.tagName === 'BUTTON') el.disabled = true;
            }
        });
        // 4. Hidden Gradio upload — buttons must stay clickable for JS proxy
        //    but must be removed from tab order and accessibility tree.
        document.querySelectorAll('#pdf-upload button, #pdf-upload a').forEach(function(el) {
            el.setAttribute('tabindex', '-1');
            el.setAttribute('aria-hidden', 'true');
        });
    }
    fixHiddenFocusable();

    // --- WCAG fix: screen reader announcement region (4.1.3) ---
    var a11yAnnounce = document.createElement('div');
    a11yAnnounce.id = 'a11y-announce';
    a11yAnnounce.setAttribute('role', 'status');
    a11yAnnounce.setAttribute('aria-live', 'polite');
    a11yAnnounce.setAttribute('aria-atomic', 'true');
    a11yAnnounce.style.cssText = 'position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden;';
    document.body.appendChild(a11yAnnounce);
    var _hadResults = false;

    // Re-run fixes after Gradio dynamic renders (e.g. after file upload clears).
    var observer = new MutationObserver(function() {
        document.querySelectorAll('div.empty[aria-label]:not([role])').forEach(function(el) {
            el.setAttribute('role', 'status');
        });
        fixHiddenFocusable();
        patchTabs();

        // Announce when results tabs first appear
        var resultTabs = document.querySelector('[role="tablist"]:not(.doc-subtabs [role="tablist"])');
        if (resultTabs && !_hadResults) {
            _hadResults = true;
            a11yAnnounce.textContent = 'Processing complete. Results are now available.';
            setTimeout(function() { a11yAnnounce.textContent = ''; }, 5000);
        } else if (!resultTabs) {
            _hadResults = false;
        }
    });
    observer.observe(document.body, { childList: true, subtree: true });

    // ── WCAG 2.2 AA: Accessible tab pattern ──────────────────────────
    // SC 2.1.1 Keyboard: arrow key navigation
    // SC 4.1.2 Name, Role, Value: aria-controls / aria-labelledby linkage
    var _tabUid = 0;
    function patchTabs() {
        document.querySelectorAll('[role="tablist"]').forEach(function(tablist) {
            if (tablist.dataset.a11yPatched) return;
            tablist.dataset.a11yPatched = '1';

            var tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
            // Find the Gradio tabs root (parent of tab-wrapper and tabpanels)
            var container = tablist.closest('.tabs');
            if (!container) return;
            var panels = Array.from(container.querySelectorAll(':scope > [role="tabpanel"]'));

            // Link tabs ↔ panels with aria-controls / aria-labelledby
            tabs.forEach(function(tab, idx) {
                if (!tab.id) { tab.id = 'a11y-tab-' + (++_tabUid); }
                var panel = panels[idx];
                if (panel) {
                    if (!panel.id) { panel.id = 'a11y-panel-' + _tabUid; }
                    tab.setAttribute('aria-controls', panel.id);
                    panel.setAttribute('aria-labelledby', tab.id);
                    panel.setAttribute('tabindex', '0');
                }
                // tabindex management
                if (tab.getAttribute('aria-selected') === 'true') {
                    tab.setAttribute('tabindex', '0');
                } else {
                    tab.setAttribute('tabindex', '-1');
                }
            });

            // Focus wrap: sentinel after last panel.
            // Outer tabs: redirect back to active tab (wrap within results).
            // Inner tabs (doc-subtabs): redirect to the outer tab's next sibling
            // so focus escapes the inner group and continues the outer flow.
            if (!container.querySelector('.a11y-tab-sentinel')) {
                var sentinel = document.createElement('div');
                sentinel.className = 'a11y-tab-sentinel';
                sentinel.setAttribute('tabindex', '0');
                sentinel.style.cssText = 'position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);';
                container.appendChild(sentinel);
                var isInner = !!container.closest('[role="tabpanel"]');
                sentinel.addEventListener('focus', function() {
                    if (isInner) {
                        // Escape to outer tab group: find the outer tablist and
                        // focus its active tab so the user is back at document level
                        var outerPanel = container.closest('[role="tabpanel"]');
                        var outerTabs = outerPanel ? outerPanel.closest('.tabs') : null;
                        var outerActive = outerTabs ? outerTabs.querySelector(':scope > .tab-wrapper [role="tab"][aria-selected="true"]') : null;
                        if (outerActive) {
                            outerActive.focus();
                        }
                    } else {
                        // Outer tabs: wrap back to active tab
                        var active = tablist.querySelector('[role="tab"][aria-selected="true"]');
                        if (active) active.focus();
                    }
                });
            }

            // Arrow key navigation (auto-activation)
            tablist.addEventListener('keydown', function(e) {
                var currentTabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
                var idx = currentTabs.indexOf(document.activeElement);
                if (idx < 0) return;
                var newIdx = -1;
                if (e.key === 'ArrowRight' || e.key === 'ArrowDown') {
                    newIdx = (idx + 1) % currentTabs.length;
                } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
                    newIdx = (idx - 1 + currentTabs.length) % currentTabs.length;
                } else if (e.key === 'Home') {
                    newIdx = 0;
                } else if (e.key === 'End') {
                    newIdx = currentTabs.length - 1;
                }
                if (newIdx >= 0 && newIdx !== idx) {
                    e.preventDefault();
                    currentTabs[newIdx].focus();
                    currentTabs[newIdx].click();
                }
            });
        });
    }
    patchTabs();

    // --- Global lightbox for image enlargement ---
    // Creates a single lightbox overlay on document.body, accessible from any
    // Gradio HTML component regardless of DOM scoping.
    (function() {
        var overlay = document.createElement('div');
        overlay.id = 'global-lightbox';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-label', 'Enlarged view');
        overlay.setAttribute('tabindex', '-1');
        overlay.style.cssText = 'display:none;position:fixed;top:0;left:0;width:100vw;height:100vh;' +
            'background:rgba(0,0,0,0.82);z-index:99999;cursor:zoom-out;' +
            'align-items:center;justify-content:center;';
        var img = document.createElement('img');
        img.id = 'global-lightbox-img';
        img.alt = 'Enlarged view';
        img.style.cssText = 'max-width:90vw;max-height:90vh;border-radius:8px;' +
            'box-shadow:0 4px 30px rgba(0,0,0,0.5);';
        overlay.appendChild(img);
        overlay.addEventListener('click', function() {
            overlay.style.display = 'none';
        });
        document.body.appendChild(overlay);

        window._showLightbox = function(src) {
            img.src = src;
            overlay.style.display = 'flex';
            overlay.focus();
        };
    })();

    // --- WCAG fix: lightbox keyboard accessibility (2.1.1) ---
    // Close lightbox overlay with Escape key.
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            var lb = document.getElementById('global-lightbox');
            if (lb && lb.style.display !== 'none' && lb.style.display !== '') {
                lb.style.display = 'none';
            }
        }
    });

    // --- WCAG fix: accessible custom upload UI (2.5.8, 4.1.2) ---
    // Gradio's File component is visually hidden. This JS renders an accessible
    // upload box and file list, wired to Gradio's hidden component.

    var _uploadBoxHTML = '<button type="button" class="custom-upload-btn" id="custom-upload-btn">' +
        '<div class="custom-upload-box">' +
        '<div class="custom-upload-box-label">Upload PDFs</div>' +
        '<div class="custom-upload-box-hint">Click to select files (max 3, PDF only)</div>' +
        '</div></button>';

    var _lastFileCount = 0;
    function syncUploadActions() {
        var actionsEl = document.getElementById('custom-upload-actions');
        if (!actionsEl) return;
        var rows = document.querySelectorAll('#pdf-upload .file-preview tr.file');
        // Only hide uploading indicator when file count actually changed
        if (rows.length !== _lastFileCount) {
            _lastFileCount = rows.length;
            hideUploadingIndicator();
        }

        // Toggle run button label based on file state
        var runBtnEl = document.querySelector('#run-btn');
        if (runBtnEl) {
            runBtnEl.textContent = rows.length ? 'Process PDFs' : 'Upload PDFs';
        }

        if (!rows.length) {
            // No files — show upload box
            actionsEl.innerHTML = _uploadBoxHTML;
            return;
        }

        // Files present — show file list + action buttons
        var html = '<div class="custom-file-list" role="list" aria-label="Uploaded files">';
        rows.forEach(function(row, idx) {
            var nameEl = row.querySelector('.filename');
            var name = nameEl ? nameEl.textContent.trim() : 'File ' + (idx + 1);
            var safe = name.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
            html += '<div role="listitem" class="custom-file-item">' +
                '<span class="custom-file-name">' + safe + '</span>' +
                '<button type="button" class="custom-file-remove" ' +
                'aria-label="Remove ' + safe + '" data-idx="' + idx + '">' +
                '\u2715</button></div>';
        });
        html += '</div>';
        html += '<div class="custom-action-btns">';
        html += '<button type="button" class="custom-action-btn" id="custom-add-more">Add more files</button>';
        html += '<button type="button" class="custom-action-btn" id="custom-clear-all">Clear all</button>';
        html += '</div>';
        actionsEl.innerHTML = html;
    }

    var _uploadProgressInterval = null;
    function showUploadingIndicator() {
        var el = document.getElementById('custom-upload-indicator');
        if (!el) return;
        el.innerHTML = '<div class="custom-upload-progress" role="status" aria-live="polite">' +
            '<span class="custom-upload-spinner"></span> ' +
            '<span class="custom-upload-text">Uploading files\u2026</span>' +
            '<div class="custom-upload-bar"><div class="custom-upload-bar-fill"></div></div>' +
            '</div>';
        // Poll Gradio's upload progress CSS variable
        if (_uploadProgressInterval) clearInterval(_uploadProgressInterval);
        _uploadProgressInterval = setInterval(function() {
            var pct = getComputedStyle(document.documentElement).getPropertyValue('--upload-progress-width');
            var fill = el.querySelector('.custom-upload-bar-fill');
            var text = el.querySelector('.custom-upload-text');
            if (fill && pct) {
                fill.style.width = pct;
                if (text) text.textContent = 'Uploading files\u2026 ' + pct.trim();
            }
        }, 200);
    }
    function hideUploadingIndicator() {
        var el = document.getElementById('custom-upload-indicator');
        if (el) el.innerHTML = '';
        if (_uploadProgressInterval) {
            clearInterval(_uploadProgressInterval);
            _uploadProgressInterval = null;
        }
    }

    function triggerGradioUpload() {
        var fi = document.querySelector('#pdf-upload input[type="file"]');
        if (!fi) return;
        fi.value = '';
        fi.click();
        showUploadingIndicator();
    }

    // "Upload PDFs" fallback — capture phase intercepts before Gradio
    document.addEventListener('click', function(e) {
        var btn = e.target.closest('#run-btn');
        if (btn && !document.querySelectorAll('#pdf-upload .file-preview tr.file').length) {
            e.stopImmediatePropagation();
            e.preventDefault();
            triggerGradioUpload();
        }
    }, true);

    // Wire custom buttons via event delegation
    document.addEventListener('click', function(e) {
        // Upload box or "Add more files" → open file dialog
        if (e.target.closest('#custom-upload-btn') || e.target.closest('#custom-add-more')) {
            triggerGradioUpload();
        }
        // "Clear all" → confirm then trigger Gradio's hidden Clear button
        if (e.target.closest('#custom-clear-all')) {
            if (!confirm('Remove all uploaded files?')) return;
            var btns = document.querySelectorAll('#pdf-upload .icon-button-wrapper button');
            var clearBtn = btns.length > 1 ? btns[btns.length - 1] : null;
            if (clearBtn) {
                clearBtn.disabled = false;
                clearBtn.removeAttribute('aria-hidden');
                clearBtn.removeAttribute('tabindex');
                clearBtn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
            }
            setTimeout(syncUploadActions, 300);
        }
        // Per-file remove → trigger Gradio's hidden × button
        var removeBtn = e.target.closest('.custom-file-remove');
        if (removeBtn) {
            var idx = parseInt(removeBtn.dataset.idx);
            var gradioBtns = document.querySelectorAll('#pdf-upload .label-clear-button');
            var btn = gradioBtns[idx];
            if (btn) {
                btn.disabled = false;
                btn.removeAttribute('aria-hidden');
                btn.removeAttribute('tabindex');
                btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
            } else {
                // Single file — no per-file delete button; use Clear All instead
                var allBtns = document.querySelectorAll('#pdf-upload .icon-button-wrapper button');
                var clearBtn = allBtns.length > 1 ? allBtns[allBtns.length - 1] : null;
                if (clearBtn) {
                    clearBtn.disabled = false;
                    clearBtn.removeAttribute('aria-hidden');
                    clearBtn.removeAttribute('tabindex');
                    clearBtn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                }
            }
            setTimeout(syncUploadActions, 300);
        }
    });

    // Watch for file list changes in Gradio's hidden component
    (function watchUpload() {
        var uploadEl = document.getElementById('pdf-upload');
        if (!uploadEl) { setTimeout(watchUpload, 500); return; }
        new MutationObserver(syncUploadActions)
            .observe(uploadEl, { childList: true, subtree: true });
        syncUploadActions();
    })();

    // --- Accessible custom download panel ---
    // Mirrors the custom upload pattern: Gradio's gr.Files is hidden,
    // this JS renders an accessible file list with full-row download links.
    function syncDownloadArea() {
        var area = document.getElementById('custom-download-area');
        if (!area) return;

        var rows = document.querySelectorAll('#output-files .file-preview tr.file');
        var zipLink = document.querySelector('#zip-output a.download-link');

        if (!rows.length && !zipLink) {
            area.innerHTML = '';
            area.style.display = 'none';
            return;
        }
        area.style.display = '';

        var html = '';
        if (rows.length) {
            html += '<p class="custom-dl-heading">Download Files</p>';
            html += '<div class="custom-dl-list" role="list" aria-label="Download files">';
            rows.forEach(function(row) {
                var dl = row.querySelector('a.download-link');
                if (!dl) return;
                var href = dl.getAttribute('href');
                var fname = dl.getAttribute('download') || dl.textContent.trim();
                var safe = fname.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
                html += '<div role="listitem" class="custom-dl-item">' +
                    '<a class="custom-dl-link" href="' + href + '" download="' + safe + '" aria-label="Download ' + safe + '">' +
                    '<span class="custom-dl-name">' + safe + '</span>' +
                    '<span class="custom-dl-icon" aria-hidden="true">\u2B73</span>' +
                    '</a></div>';
            });
            html += '</div>';
        }
        if (zipLink) {
            var zipHref = zipLink.getAttribute('href');
            var zipName = zipLink.getAttribute('download') || 'Download All (ZIP)';
            var safeZip = zipName.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
            html += '<div class="custom-dl-zip">' +
                '<a class="custom-dl-link" href="' + zipHref + '" download="' + safeZip + '" aria-label="Download All (ZIP)">' +
                '<span class="custom-dl-name">Download All (ZIP)</span>' +
                '<span class="custom-dl-icon" aria-hidden="true">\uD83D\uDCE6</span>' +
                '</a></div>';
        }
        area.innerHTML = html;
    }

    (function watchDownloads() {
        var outEl = document.getElementById('output-files');
        var zipEl = document.getElementById('zip-output');
        if (!outEl || !zipEl) { setTimeout(watchDownloads, 500); return; }
        var dlObs = new MutationObserver(syncDownloadArea);
        dlObs.observe(outEl, { childList: true, subtree: true });
        dlObs.observe(zipEl, { childList: true, subtree: true });
        syncDownloadArea();
    })();

    // --- Editable Screen Reader Preview: JS -> Python edit bridge ---
    //
    // Sink anchor: the Python renderer wraps the editable HTML in a div
    // with data-sr-sink-id="<sink-elem-id>". The JS bridge walks up from
    // the edited span to find that anchor (NOT Gradio's elem_classes
    // wrapper — Gradio sometimes layers two divs with the same class,
    // which is why `closest('.sr-edit-container')` resolved to the inner
    // wrapper without the elem_id in the prior attempt).
    //
    // Event dispatch: Gradio's frontend (Svelte) listens for 'input' on
    // the underlying textarea, but some setups only sync state on
    // 'change'. We dispatch BOTH, after using the native prototype value
    // setter (cheap insurance for React-based Gradio variants too).
    //
    // Enable verbose logs by setting window.__SR_EDIT_DEBUG = true in the
    // console before editing.
    (function initSrEditBridge() {
        var pending = {};      // {sinkId: {elemId: {field: value}}}
        var flushTimers = {};  // {sinkId: timeoutId}
        var FLUSH_MS = 200;

        function dbg() {
            if (window.__SR_EDIT_DEBUG) {
                try { console.log.apply(console, ['[sr-edit]'].concat([].slice.call(arguments))); } catch (e) {}
            }
        }

        // setProgrammaticValue is defined at the outer APP_JS scope (shared with
        // initSrOrderBridge) — reachable here via closure.

        function sinkInputForSinkId(sinkId) {
            if (!sinkId) return null;
            var sinkRoot = document.getElementById(sinkId);
            if (!sinkRoot) {
                dbg('sink root not found for id', sinkId);
                return null;
            }
            // Gradio wraps the underlying input in several divs. Match any
            // text-like input or textarea.
            var inp = sinkRoot.querySelector('textarea, input[type="text"], input:not([type])');
            if (!inp) dbg('sink input element not found inside', sinkId);
            return inp;
        }

        function flushSink(sinkId) {
            var sink = sinkInputForSinkId(sinkId);
            if (!sink) return;
            var payload = pending[sinkId] || {};
            var json = JSON.stringify(payload);
            dbg('flush', sinkId, json);
            setProgrammaticValue(sink, json);
        }

        function debouncedFlush(sinkId) {
            clearTimeout(flushTimers[sinkId]);
            flushTimers[sinkId] = setTimeout(function () {
                flushSink(sinkId);
            }, FLUSH_MS);
        }

        function isEditable(el) {
            return el && el.classList && (
                el.classList.contains('sr-edit-text') ||
                el.classList.contains('sr-edit-alt')
            );
        }

        function sinkIdForEditable(el) {
            var anchor = el.closest('[data-sr-sink-id]');
            return anchor ? anchor.getAttribute('data-sr-sink-id') : '';
        }

        // Capture original value on first focus so we can detect "no change".
        document.addEventListener('focusin', function (e) {
            var el = e.target;
            if (!isEditable(el)) return;
            if (el.dataset.origValue === undefined) {
                el.dataset.origValue = (el.innerText || '').trim();
            }
        }, true);

        document.addEventListener('focusout', function (e) {
            var el = e.target;
            if (!isEditable(el)) return;
            var sinkId = sinkIdForEditable(el);
            if (!sinkId) {
                dbg('no data-sr-sink-id anchor found for', el);
                return;
            }
            var elemId = el.dataset.elemId;
            var field = el.dataset.field || 'text';
            if (!elemId) {
                dbg('missing data-elem-id on', el);
                return;
            }

            var value = (el.innerText || '').trim();
            var orig = el.dataset.origValue || '';

            pending[sinkId] = pending[sinkId] || {};

            if (value === orig) {
                if (pending[sinkId][elemId]) {
                    delete pending[sinkId][elemId][field];
                    if (!Object.keys(pending[sinkId][elemId]).length) {
                        delete pending[sinkId][elemId];
                    }
                }
                el.classList.remove('sr-edit-dirty');
            } else {
                pending[sinkId][elemId] = pending[sinkId][elemId] || {};
                pending[sinkId][elemId][field] = value;
                el.classList.add('sr-edit-dirty');
            }
            dbg('focusout', sinkId, elemId, field, '->', value, 'pending=', pending[sinkId]);
            // Flush IMMEDIATELY (not debounced): when the user leaves a field by
            // clicking "Update PDF" or another control, the button's handler can
            // otherwise run before a debounced flush fires, dropping the last
            // edit. Immediate flush dispatches the sink change synchronously so
            // it is queued before the button click.
            flushSink(sinkId);
        }, true);

        // Block Enter to keep edits single-line (Shift+Enter still works).
        document.addEventListener('keydown', function (e) {
            var el = e.target;
            if (!isEditable(el)) return;
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                el.blur();
            }
        });

        // Commit the active edit the instant the user points anywhere outside
        // the focused field — another box, empty space, or the "Update PDF"
        // button. pointerdown fires before the click's default action and before
        // focus moves, so blurring here flushes the pending edit (via focusout
        // above) BEFORE the button handler runs. Fixes: (1) edit lost when going
        // straight from typing to Update PDF; (2) an expanded overlay box in the
        // Visual editor swallowing the deselect click so focusout never fired.
        document.addEventListener('pointerdown', function (e) {
            var active = document.activeElement;
            if (!isEditable(active)) return;
            if (active.contains(e.target)) return;  // click inside the field: keep editing
            active.blur();
        }, true);

        // After a successful re-tag the SR tab re-renders with fresh DOM;
        // clear bookkeeping for sinks that no longer exist.
        var GC_INTERVAL_MS = 30000;
        setInterval(function () {
            Object.keys(pending).forEach(function (sinkId) {
                if (!document.getElementById(sinkId)) {
                    delete pending[sinkId];
                    clearTimeout(flushTimers[sinkId]);
                    delete flushTimers[sinkId];
                }
            });
        }, GC_INTERVAL_MS);

        // Segmented pill toggle: Reading order / Reading / Structure. Called
        // from the inline onclick attribute emitted by the Python tab block.
        // Sets data-view on the closest .sr-view-host; CSS does the
        // hide/show. The JS edit bridge above is unaffected because
        // contenteditable spans in every view share the same
        // data-sr-sink-id anchor on the host.
        window._srSetView = function (btn, view) {
            var host = btn.closest('.sr-view-host');
            var shell = btn.closest('.sr-editor-shell');
            if (host) {
                host.setAttribute('data-view', view);
                var btns = host.querySelectorAll('.sr-view-toggle button');
                btns.forEach(function (b) {
                    var on = b.getAttribute('data-view') === view;
                    b.classList.toggle('active', on);
                    b.setAttribute('aria-pressed', on ? 'true' : 'false');
                });
            }
            if (shell) {
                shell.setAttribute('data-view', view);
                // Lazy-load the first flagged (or first) page for Reading order.
                if (view === 'order' && !shell.querySelector('.ro-view-inner')) {
                    var _fpEl = shell.querySelector('[data-ro-first-page]');
                    var _fp = _fpEl
                        ? (parseInt(_fpEl.getAttribute('data-ro-first-page'), 10) || 1)
                        : 1;
                    _roLoadPage(shell, _fp);
                }
                if (view === 'order') {
                    // Re-apply the user's saved list/preview split (persists across
                    // @gr.render rebuilds and reloads; page-nav keeps it via the shell).
                    try {
                        var _rw = window.localStorage.getItem('roPreviewW');
                        if (_rw) shell.style.setProperty('--ro-preview-w', _rw + '%');
                    } catch (err) {}
                }
            }
            dbg('view switched to', view);
        };

        // Write a page number into the shell's hidden RO-page sink; the Gradio
        // .change handler swaps in that page's Reading-order view HTML. Reuses
        // setProgrammaticValue so Svelte/Gradio both pick up the value.
        function _roLoadPage(shell, pageNo, _attempt) {
            if (!shell) return;
            var attempt = _attempt || 0;
            var sinkRoot = shell.querySelector('.ro-page-sink');
            var inp = sinkRoot && sinkRoot.querySelector(
                'textarea, input[type="text"], input:not([type])');
            if (!sinkRoot) { dbg('ro-page-sink not found'); }
            else if (!inp) { dbg('ro-page-sink input not found'); }
            else { setProgrammaticValue(inp, String(pageNo)); }
            // A sink in the DOM is NOT a sink Gradio has bound. Before Svelte
            // hydrates the field, the write is neither delivered nor retained
            // (Svelte re-renders it back to ""), so the page never loads and
            // the view sits on "Loading page…" forever. Presence is the wrong
            // readiness signal — verify the effect instead and re-issue until
            // the page actually arrives.
            if (attempt >= _RO_LOAD_MAX_TRIES) { dbg('ro page load gave up'); return; }
            setTimeout(function () {
                // Stop if the user moved to another view, or it loaded.
                if (shell.getAttribute('data-view') !== 'order') return;
                if (shell.querySelector('.ro-view-inner')) return;
                _roLoadPage(shell, pageNo, attempt + 1);
            }, _RO_LOAD_RETRY_MS);
        }
        window._roSetPage = function (btnOrEl, delta) {
            var shell = btnOrEl.closest('.sr-editor-shell');
            if (!shell) return;
            var inner = shell.querySelector('.ro-view-inner');
            var cur = inner ? (parseInt(inner.getAttribute('data-page'), 10) || 1) : 1;
            var count = inner ? (parseInt(inner.getAttribute('data-page-count'), 10) || 1) : 1;
            var next = Math.min(count, Math.max(1, cur + delta));
            if (next === cur) return;
            _roLoadPage(shell, next);
        };
        window._roSetPageNum = function (btnOrEl, pageNo) {
            var shell = btnOrEl.closest('.sr-editor-shell');
            if (shell) _roLoadPage(shell, pageNo);
        };

        // Routing box (TOC, or a table with no cell bboxes) → switch to the
        // Reading view and scroll to that element.
        window._visOpenTable = function (box) {
            var shell = box.closest('.sr-editor-shell');
            var id = (box && box.dataset) ? box.dataset.targetId : '';
            var toggleBtn = shell
                ? shell.querySelector('.sr-view-toggle button[data-view="prose"]')
                : null;
            if (toggleBtn) { window._srSetView(toggleBtn, 'prose'); }
            if (!id) return;
            setTimeout(function () {
                var target = document.getElementById('vis-target-' + id);
                if (target) {
                    target.scrollIntoView({ block: 'center', behavior: 'smooth' });
                    target.classList.add('vis-target-flash');
                    setTimeout(function () {
                        target.classList.remove('vis-target-flash');
                    }, 1600);
                }
            }, 60);
        };

        // Banner link → jump to this document's "Content Editor" sub-tab.
        // Finds the nearest doc-subtabs group and clicks its Content Editor tab
        // (which then opens on the Reading-order view via initSrDefaultView).
        window._gotoContentEditor = function (el) {
            var group = el.closest('.doc-subtabs') || el.closest('.tabs');
            var tablist = group ? group.querySelector('[role="tablist"]') : null;
            if (tablist) {
                var tabs = tablist.querySelectorAll('[role="tab"]');
                for (var i = 0; i < tabs.length; i++) {
                    if ((tabs[i].textContent || '').indexOf('Content Editor') !== -1) {
                        tabs[i].click();
                        tabs[i].scrollIntoView({ block: 'nearest' });
                        break;
                    }
                }
            }
            return false;
        };
    })();

    // --- Reading-order reorder bridge: drag + keyboard, immediate flush ---
    (function initSrOrderBridge() {
        var live = null;
        function announce(msg) {
            if (!live) {
                live = document.createElement('div');
                live.setAttribute('aria-live', 'polite');
                live.className = 'ro-live';
                live.style.cssText = 'position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);';
                document.body.appendChild(live);  // outside @gr.render subtree -> survives rebuilds
            }
            live.textContent = msg;
        }
        function sinkInput(id) {
            var root = id && document.getElementById(id);
            return root ? root.querySelector('textarea, input[type="text"], input:not([type])') : null;
        }
        function renumber(list) {
            var rows = list.querySelectorAll('.ro-row');
            rows.forEach(function (r, i) {
                var n = r.querySelector('.ro-num');
                if (n) n.textContent = String(i + 1);
                r.setAttribute('aria-label', 'Position ' + (i + 1) + ': '
                    + (r.querySelector('.ro-typ') || {}).textContent + ' '
                    + (r.querySelector('.ro-txt') || {}).textContent);
            });
        }
        function highlightBox(elemId, on) {
            var shell = document.querySelector('.sr-editor-shell[data-view="order"]');
            if (!shell) return;
            var box = shell.querySelector('.ro-page-host .vis-box[data-elem-id="' + (window.CSS && CSS.escape ? CSS.escape(elemId) : elemId) + '"]');
            if (box) box.classList.toggle('ro-box-hot', !!on);
        }
        function flush(list) {
            var sink = sinkInput(list.getAttribute('data-ro-sink-id'));
            if (!sink) return;
            var page = list.getAttribute('data-ro-page');
            var ids = [];
            list.querySelectorAll('.ro-row').forEach(function (r) {
                ids.push(r.getAttribute('data-elem-id'));
            });
            var payload = {}; payload[page] = ids;
            setProgrammaticValue(sink, JSON.stringify(payload));  // immediate, survives re-render
        }
        function move(row, dir) {
            var list = row.parentNode;
            if (dir < 0 && row.previousElementSibling) {
                list.insertBefore(row, row.previousElementSibling);
            } else if (dir > 0 && row.nextElementSibling) {
                list.insertBefore(row.nextElementSibling, row);
            } else { return; }
            renumber(list);
            flush(list);
            row.focus();
            var pos = Array.prototype.indexOf.call(list.querySelectorAll('.ro-row'), row) + 1;
            announce((row.querySelector('.ro-typ') || {}).textContent + ' moved to position ' + pos
                + ' of ' + list.querySelectorAll('.ro-row').length);
        }

        // Keyboard: Alt+Up / Alt+Down move the focused row.
        document.addEventListener('keydown', function (e) {
            var row = e.target;
            if (!row.classList || !row.classList.contains('ro-row')) return;
            if (!e.altKey) return;
            if (e.key === 'ArrowUp') { e.preventDefault(); move(row, -1); }
            else if (e.key === 'ArrowDown') { e.preventDefault(); move(row, 1); }
        });

        // Cross-highlight on hover/focus.
        document.addEventListener('mouseover', function (e) {
            var row = e.target.closest && e.target.closest('.ro-row');
            if (row) highlightBox(row.getAttribute('data-elem-id'), true);
        });
        document.addEventListener('mouseout', function (e) {
            var row = e.target.closest && e.target.closest('.ro-row');
            if (row && !row.contains(e.relatedTarget)) highlightBox(row.getAttribute('data-elem-id'), false);
        });
        document.addEventListener('focusin', function (e) {
            var row = e.target;
            if (row.classList && row.classList.contains('ro-row')) highlightBox(row.getAttribute('data-elem-id'), true);
        });
        document.addEventListener('focusout', function (e) {
            var row = e.target;
            if (row.classList && row.classList.contains('ro-row')) highlightBox(row.getAttribute('data-elem-id'), false);
        });

        // Pointer drag-and-drop.
        var dragRow = null;
        document.addEventListener('dragstart', function (e) {
            var row = e.target.closest && e.target.closest('.ro-row');
            if (!row) return;
            dragRow = row; row.classList.add('ro-dragging');
            if (e.dataTransfer) { e.dataTransfer.effectAllowed = 'move'; }
        });
        document.addEventListener('dragover', function (e) {
            if (!dragRow) return;
            var over = e.target.closest && e.target.closest('.ro-row');
            if (!over || over === dragRow || over.parentNode !== dragRow.parentNode) return;
            e.preventDefault();
            var rect = over.getBoundingClientRect();
            var after = (e.clientY - rect.top) > rect.height / 2;
            over.parentNode.insertBefore(dragRow, after ? over.nextElementSibling : over);
        });
        document.addEventListener('drop', function (e) {
            if (dragRow) e.preventDefault();
        });
        document.addEventListener('dragend', function () {
            if (!dragRow) return;
            var list = dragRow.parentNode;
            dragRow.classList.remove('ro-dragging');
            renumber(list); flush(list);
            dragRow = null;
        });

        // Resizable splitter: drag to rebalance the list vs. the page preview.
        // Width is stored as a CSS var on the persistent shell (survives page
        // nav) and in localStorage (survives rebuilds/reloads; re-applied by
        // _srSetView when the Reading-order view is opened).
        var splitDrag = null;
        document.addEventListener('pointerdown', function (e) {
            var sp = e.target.closest && e.target.closest('.ro-splitter');
            if (!sp) return;
            var work = sp.closest('.ro-work');
            var shell = sp.closest('.sr-editor-shell');
            if (!work || !shell) return;
            splitDrag = { work: work, shell: shell, sp: sp };
            sp.classList.add('ro-splitting');
            e.preventDefault();
        });
        document.addEventListener('pointermove', function (e) {
            if (!splitDrag) return;
            var r = splitDrag.work.getBoundingClientRect();
            if (r.width <= 0) return;
            var pct = (r.right - e.clientX) / r.width * 100;
            pct = Math.max(20, Math.min(70, pct));
            splitDrag.shell.style.setProperty('--ro-preview-w', pct.toFixed(1) + '%');
            try { window.localStorage.setItem('roPreviewW', pct.toFixed(1)); } catch (err) {}
        });
        document.addEventListener('pointerup', function () {
            if (!splitDrag) return;
            splitDrag.sp.classList.remove('ro-splitting');
            splitDrag = null;
        });
    })();

    // Default the Content Editor to the Reading-order view (its first pill).
    // The shell's data-view is only ever set by JS, so on first mount we
    // auto-select 'order' — which reuses _srSetView to reveal the view, lazy-
    // load its first page, and re-apply the saved split. When the Reading-order
    // pill is absent (config reading_order_enabled=false), we leave Reading as
    // the default. Idempotent per shell (guarded by a flag + the data-view attr),
    // so it never overrides a view the user has since chosen.
    (function initSrDefaultView() {
        function apply() {
            var shells = document.querySelectorAll('.sr-editor-shell');
            if (!shells || !shells.forEach) return;
            shells.forEach(function (shell) {
                if (shell.__srDefaulted || shell.getAttribute('data-view')) return;
                var orderBtn = shell.querySelector(
                    '.sr-view-toggle button[data-view="order"]');
                if (!orderBtn || typeof window._srSetView !== 'function') return;
                // The toggle button ships inside the sr-edit-container HTML,
                // which Gradio mounts BEFORE the ro-page-sink Textbox. Switching
                // to the order view in that window strands the shell on the
                // "Loading page…" placeholder forever: _roLoadPage has nowhere
                // to write the page number, and this function is one-shot, so
                // nothing retries. Wait for the sink — the MutationObserver
                // below re-runs apply() when Gradio finishes mounting it.
                var sinkRoot = shell.querySelector('.ro-page-sink');
                if (!sinkRoot || !sinkRoot.querySelector(
                        'textarea, input[type="text"], input:not([type])')) return;
                shell.__srDefaulted = true;
                window._srSetView(orderBtn, 'order');
            });
        }
        // Gradio mounts the tab lazily; retry briefly to catch first render,
        // then let the observer handle later @gr.render rebuilds.
        var tries = 0;
        var iv = setInterval(function () {
            apply();
            if (++tries >= 20) clearInterval(iv);
        }, 150);
        try {
            new MutationObserver(apply).observe(
                document.body, { childList: true, subtree: true });
        } catch (e) {}
    })();

})();
"""


def _build_header_html() -> str:
    """Sticky top bar: optional logo, header title and subtitle."""
    logo_tag = ""
    if _BRAND.logo is not None:
        b64 = base64.b64encode(_BRAND.logo.read_bytes()).decode("ascii")
        logo_tag = (
            '<span style="display:inline-flex;align-items:center;'
            'margin-right:14px;flex-shrink:0;">'
            '<img src="data:image/png;base64,' + b64 + '" '
            f'alt="{escape(_BRAND.logo_alt or _BRAND.organization or _BRAND.product_name)}" '
            'style="height:36px;width:auto;display:block;">'
            '</span>'
        )
    return (
        '<div class="rr-header" role="banner" style="position:relative;">'
        + logo_tag +
        '<div style="flex:1;">'
        f'<h1 class="rr-header-title">{escape(_BRAND.header_title)}</h1>'
        '<div class="rr-header-subtitle">'
        'Make any PDF accessible &mdash; WCAG&nbsp;2.1 AA compliant, screen&#8209;reader&nbsp;ready'
        '</div>'
        '</div>'
        '</div>'
    )




def _get_queue_status_html() -> str:
    """Return a small HTML badge showing queue status (displayed in header bar)."""
    _style_idle = (
        'display:inline-block;font-size:0.75rem;font-weight:600;'
        'padding:5px 14px;border-radius:12px;'
        'color:#14532d;background:#bbf7d0;'
    )
    _style_busy = (
        'display:inline-block;font-size:0.75rem;font-weight:600;'
        'padding:5px 14px;border-radius:12px;'
        'color:#78350f;background:#fde68a;'
    )
    stats = _log_router.active_stats()
    active_users = stats["active_users"]
    total_docs = stats["total_docs"]
    docs_remaining = stats["docs_remaining"]

    # Check Celery queue for additional queued tasks
    celery_queued = 0
    celery_active = 0
    celery_wait = 0
    try:
        from tasks.task_runner import is_celery_available
        if is_celery_available():
            try:
                from tasks.monitor import get_queue_status
                qs = get_queue_status()
                celery_queued = qs.get("queue_length", 0)
                celery_active = qs.get("active_tasks", 0)
                celery_wait = qs.get("estimated_wait_seconds", 0)
            except Exception:
                pass
    except Exception:
        pass

    total_active = active_users + celery_active
    total_queued = celery_queued

    if total_active == 0 and total_queued == 0:
        return f'<div style="{_style_idle}" role="status">Ready</div>'

    # Build informative label
    parts = []
    if total_active:
        user_word = "user" if total_active == 1 else "users"
        parts.append(f"{total_active} {user_word}")
    if total_docs:
        doc_word = "doc" if total_docs == 1 else "docs"
        parts.append(f"{total_docs} {doc_word}")
    if total_queued:
        parts.append(f"{total_queued} queued")

    # Estimate wait: ~60s per remaining doc (conservative average)
    avg_per_doc = 60
    try:
        from utils.config_loader import get_config
        cfg = get_config()
        avg_per_doc = cfg.get("queue", {}).get("avg_seconds_per_doc", 60)
    except Exception:
        pass

    wait_secs = max(celery_wait, docs_remaining * avg_per_doc)
    wait_str = ""
    if wait_secs > 0:
        if wait_secs < 60:
            wait_str = "&lt;1 min wait"
        else:
            mins = int(round(wait_secs / 60))
            wait_str = f"~{mins} min wait"

    label = "\u26a1 " + " \u00b7 ".join(parts)
    if wait_str:
        label += f" \u00b7 {wait_str}"

    return f'<div style="{_style_busy}" role="status">{label}</div>'


def _poll_log(session: dict):
    """Return live log HTML for the overlay, or empty string when idle."""
    sid = session.get("session_id") if session else None
    if not sid:
        return ""
    log_buf = _log_router.get_buffer(sid)
    if not log_buf or not log_buf.active:
        return ""
    lines = log_buf.get_lines()
    status = escape(log_buf.status) if log_buf.status else ""
    if not lines:
        # Show a waiting message while the buffer is active but processing
        # hasn't started yet (e.g., queued by Gradio or Celery).
        elapsed = int(time.time() - log_buf.start_time) if log_buf.start_time else 0
        if elapsed > 600:
            # Safety timeout: deactivate orphaned buffer after 10 minutes
            log_buf.active = False
            log_buf.last_active_time = time.time()
            return ""
        wait_msg = "Preparing to process your documents\u2026"
        if elapsed > 5:
            wait_msg = (
                "In queue \u2014 your documents will begin "
                f"processing shortly ({elapsed}s)"
            )
        return (
            f'<pre class="log-lines log-waiting" role="status" '
            f'aria-live="polite" data-status="">{escape(wait_msg)}</pre>'
        )
    escaped = "\n".join(escape(l) for l in lines)
    return f'<pre class="log-lines" role="log" data-status="{status}">{escaped}</pre>'


def _get_sr_preview_json(session: dict, doc_idx: int) -> str:
    """Build structure tree JSON from enriched structure JSON.

    Called lazily when the user clicks 'Screen Reader Preview'.
    Returns JSON string for the frontend tree renderer.

    Uses the enriched structure JSON (source of truth for tagger text)
    instead of extracting /ActualText from the PDF (removed in fc86b15).
    """
    from output_gen.screen_reader_preview import (
        build_sr_data_from_structure_json,
        structure_tree_to_json,
    )

    results = session.get("results", [])
    if doc_idx >= len(results):
        return json.dumps({"error": "Document not found."})

    doc = results[doc_idx]
    struct_path = doc.get("pipeline_result", {}).get("structure_json_path", "")
    if not struct_path or not Path(struct_path).exists():
        return json.dumps({"error": "Structure JSON not found."})

    tree = build_sr_data_from_structure_json(struct_path)
    return structure_tree_to_json(tree)




def _get_wcag_report_html(session: dict, doc_idx: int) -> str:
    """Generate full WCAG compliance report HTML for the slide-out panel.

    Called lazily when the user clicks 'View WCAG Report'.
    Returns a complete HTML document string rendered inside an iframe.
    """
    from output_gen.report_page import _build_report_html

    results = session.get("results", [])
    if doc_idx >= len(results):
        return ""

    doc = results[doc_idx]
    pipeline_result = doc.get("pipeline_result", {})
    post_audit = pipeline_result.get("post_audit", {})
    if not post_audit:
        return ""

    sr_data = pipeline_result.get("sr_quality", {}) or {}
    sr_quality = sr_data.get("sr_quality", "unknown") if sr_data else "unknown"
    doc_title = doc.get("filename", "")
    verdict_config = _load_config().get("verdict", {})
    return _build_report_html(
        post_audit, doc_title, for_screen=True, sr_quality=sr_quality,
        sr_data=sr_data, verdict_config=verdict_config,
    )


_SR_TAG_CLASSES = {
    "H1": "heading", "H2": "heading", "H3": "heading", "H4": "heading",
    "H5": "heading", "H6": "heading",
    "P": "text", "Span": "text", "LBody": "text", "Lbl": "text",
    "Caption": "text", "TOCI": "text",
    "Table": "table", "TR": "table", "TH": "table", "TD": "table",
    "THead": "table", "TBody": "table", "TFoot": "table",
    "Figure": "figure", "Formula": "figure",
    "Link": "link", "Reference": "link",
    "L": "list", "LI": "list",
    "Form": "form",
    "Document": "container", "Part": "container", "Art": "container",
    "Sect": "container", "Div": "container", "BlockQuote": "container",
    "TOC": "container",
}




# Tags whose `text` is what the screen reader reads (and what the iText
# tagger writes to /ActualText). When a parent of one of these has
# text-bearing children, the children own the MCR — editing the parent's
# text is a no-op against the tagged PDF.
_SR_EDITABLE_TEXT_TAGS = frozenset({
    "P", "H1", "H2", "H3", "H4", "H5", "H6",
    "Span", "LBody", "Lbl", "Caption", "TOCI",
    "TH", "TD",
})

# Tags whose alt_text feeds /Alt on the structure element.
_SR_EDITABLE_ALT_TAGS = frozenset({"Figure", "Formula"})


def _sr_build_node_html(node: dict, editable: bool = False,
                        parent_has_editable_children: bool = False) -> str:
    """Render a single structure tree node as HTML.

    When *editable* is True, the text/alt spans become contenteditable and
    carry data-elem-id + data-field attributes that the JS bridge reads.
    Per Risk 4 in the plan, if a node has children whose text feeds the MCR,
    the parent's own text becomes read-only and only the children stay
    editable.
    """
    tag_str = node.get("tag", "")
    tag = escape(tag_str)
    text = node.get("text")
    alt = node.get("alt")
    children = node.get("children", [])
    elem_id = node.get("id", "")
    cat_class = f"sr-tag-{_SR_TAG_CLASSES.get(tag_str, 'container')}"
    badge = f'<span class="sr-tag {cat_class}">{tag}</span>'
    id_attr = f' data-elem-id="{escape(elem_id)}"' if elem_id else ""

    # If this node has children that own editable text, the parent's own
    # text edit would not propagate to /ActualText. Render parent text as
    # read-only in that case.
    children_own_text = bool(children) and any(
        c.get("tag", "") in _SR_EDITABLE_TEXT_TAGS and c.get("text")
        for c in children
    )
    can_edit_text = (
        editable
        and elem_id
        and tag_str in _SR_EDITABLE_TEXT_TAGS
        and not children_own_text
    )
    can_edit_alt = editable and elem_id and tag_str in _SR_EDITABLE_ALT_TAGS

    content_html = ""
    if alt:
        if can_edit_alt:
            content_html = (
                f'<span class="sr-alt-label">Alt: </span>'
                f'<span class="sr-edit-alt" contenteditable="true" spellcheck="true" '
                f'data-elem-id="{escape(elem_id)}" data-field="alt_text" '
                f'role="textbox" aria-label="Edit alt text for {tag}">'
                f'{escape(alt)}</span>'
            )
        else:
            content_html = f'<span class="sr-alt">Alt: {escape(alt)}</span>'
    if text:
        if can_edit_text:
            content_html += (
                f'<span class="sr-edit-text" contenteditable="true" spellcheck="true" '
                f'data-elem-id="{escape(elem_id)}" data-field="text" '
                f'role="textbox" aria-label="Edit text for {tag}">'
                f'{escape(text)}</span>'
            )
        else:
            content_html += f'<span class="sr-text">{escape(text)}</span>'

    if children:
        inner = "".join(
            _sr_build_node_html(c, editable=editable,
                                parent_has_editable_children=children_own_text)
            for c in children
        )
        return (
            f'<div class="sr-node"{id_attr}><details open>'
            f'<summary><span class="sr-arrow">&#9654;</span>{badge}'
            f'{" " + content_html if content_html else ""}</summary>'
            f'{inner}</details></div>'
        )
    else:
        return f'<div class="sr-leaf"{id_attr}>{badge}{" " + content_html if content_html else ""}</div>'


def _sr_json_to_html(json_str: str, pdf_path: str = "",
                     structure_json_path: str = "",
                     editable: bool = False,
                     sink_id: str = "") -> str:
    """Convert SR preview JSON to server-rendered HTML tree.

    When *pdf_path* and *structure_json_path* are provided, each page
    accordion includes a coverage thumbnail (tagged regions highlighted)
    in a left column with the structure tree on the right.  The image
    is clickable to enlarge via the global lightbox.

    When *editable* is True, leaf text/alt spans become contenteditable
    and emit data-elem-id/data-field attributes for the JS edit bridge.
    *sink_id* is the elem_id of the hidden Gradio textbox sink — when
    set, it is embedded as a data attribute on the editable wrapper so
    the JS bridge can resolve sink lookup deterministically. Leave empty
    when this tree is rendered INSIDE a parent host that already owns
    the data-sr-sink-id anchor (e.g. the prose+tree view wrapper).
    """
    if not json_str:
        return '<div class="sr-preview-error">No structure data available. Try re-processing the document.</div>'
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return '<div class="sr-preview-error">Failed to parse structure data. Try re-processing the document.</div>'

    if "error" in data:
        return f'<div class="sr-preview-error">{escape(data["error"])}</div>'

    # Load structure elements for coverage thumbnails
    all_elements: list = []
    pages_info: dict = {}
    if structure_json_path and Path(structure_json_path).exists():
        try:
            structure = json.loads(Path(structure_json_path).read_text(encoding="utf-8"))
            all_elements = structure.get("elements", [])
            pages_info = structure.get("pages", {})
        except Exception:
            pass

    # Pre-render coverage thumbnails when pdf_path + elements available
    page_images: Dict[int, str] = {}
    if pdf_path and Path(pdf_path).exists() and all_elements and "pages" in data:
        for pg in data["pages"]:
            page_num = pg.get("page", 0)
            if page_num > 0:
                b64 = _render_coverage_thumbnail(
                    pdf_path, page_num, all_elements, pages_info, dpi=96,
                )
                if b64:
                    page_images[page_num] = b64

    # When editable, wrap in a known anchor div so the JS bridge can
    # resolve the sink without depending on where Gradio places the
    # outer elem_id/elem_classes. Also inline the hint banner here (once)
    # instead of using a CSS ::before that fires on every matching node.
    wrapper_open = '<div class="sr-tree-container">'
    edit_banner = ""
    if editable and sink_id:
        wrapper_open = (
            f'<div class="sr-tree-container sr-edit-anchor" '
            f'data-sr-sink-id="{escape(sink_id)}">'
        )
        edit_banner = (
            '<div class="sr-edit-hint">'
            'Click any text below to edit. Click '
            '<strong>Update PDF</strong> when done.'
            '</div>'
        )
    html_parts = [wrapper_open]
    if edit_banner:
        html_parts.append(edit_banner)

    if "pages" in data:
        html_parts.append(
            f'<div style="font-size:0.75rem;color:#4b5563;margin-bottom:10px;">'
            f'{len(data["pages"])} page(s) with structure elements</div>'
        )
        for p_idx, pg in enumerate(data["pages"]):
            page_num = pg.get("page", p_idx + 1)
            elems = pg.get("elements", [])
            open_attr = " open" if p_idx == 0 else ""
            count_label = f'{len(elems)} element{"s" if len(elems) != 1 else ""}'
            html_parts.append(
                f'<details class="sr-page-details"{open_attr} style="margin-bottom:8px;'
                f'border:1px solid #e5e7eb;border-radius:6px;overflow:hidden;">'
                f'<summary style="cursor:pointer;padding:8px 12px;background:#f9fafb;'
                f'font-size:0.8125rem;font-weight:600;color:#374151;list-style:none;">'
                f'<span class="sr-arrow" style="display:inline-block;font-size:0.625rem;'
                f'margin-right:6px;transition:transform 0.15s ease;">&#9654;</span>'
                f'Page {page_num} ({count_label})</summary>'
            )

            b64 = page_images.get(page_num)
            if b64:
                # Two-column: page image (left) | structure tree (right)
                html_parts.append(
                    f'<div style="display:flex;gap:12px;padding:6px 12px;align-items:flex-start;">'
                    f'<div style="flex:0 0 200px;min-width:140px;max-width:240px;">'
                    f'<img class="sr-page-img" '
                    f'src="data:image/png;base64,{b64}" '
                    f'alt="Page {page_num}" '
                    f'style="width:100%;border:1px solid #d1d5db;border-radius:4px;'
                    f'cursor:zoom-in;" '
                    f'onclick="window._showLightbox&&window._showLightbox(this.src);" />'
                    f'<div style="font-size:0.6875rem;color:#6b7280;margin-top:4px;line-height:1.3;">'
                    f'Highlighted areas are tagged in the final PDF</div>'
                    f'</div>'
                    f'<div style="flex:1;min-width:0;overflow-x:auto;">'
                )
                for elem in elems:
                    html_parts.append(_sr_build_node_html(elem, editable=editable))
                html_parts.append('</div></div>')
            else:
                html_parts.append('<div style="padding:6px 12px;">')
                for elem in elems:
                    html_parts.append(_sr_build_node_html(elem, editable=editable))
                html_parts.append('</div>')

            html_parts.append("</details>")
    else:
        # Legacy single-tree format
        html_parts.append(_sr_build_node_html(data, editable=editable))

    html_parts.append("</div>")
    return "".join(html_parts)


# ── Prose / Reading view ──────────────────────────────────────────────
#
# Renders the same enriched structure JSON as document-style prose
# (semantic HTML with reading typography) instead of the tag-tree
# accordions emitted by _sr_json_to_html. Same data-elem-id and
# sr-edit-text / sr-edit-alt span shape, so the JS edit bridge resolves
# either view to the same hidden sink with no JS changes.

# Map structure JSON tag → semantic HTML element for headings.
_PROSE_HEADING_TAGS = {"H1": "h1", "H2": "h2", "H3": "h3",
                       "H4": "h4", "H5": "h5", "H6": "h6"}

# Reading column is ~760px; @ 2x DPI a 600px-wide thumbnail looks sharp
# while cutting embed size 5-15x vs the source PDF render (which is
# typically ~2-3k px at the pipeline's render DPI).
_PROSE_THUMB_MAX_WIDTH = 600
_PROSE_THUMB_JPEG_QUALITY = 85


def _embed_thumbnail(file_path: str,
                     max_width: int = _PROSE_THUMB_MAX_WIDTH) -> Optional[str]:
    """Like _embed_image() but downscales via PIL before base64-encoding.

    Used by the Reading view's figure builder where the inline image
    only ever displays at ~760px column width — embedding the source
    render at full resolution would balloon the HTML payload.

    Falls back to _embed_image() (no resize) when PIL isn't available,
    when the source is already smaller than max_width, or when reading
    fails. Preserves PNG/transparency; everything else becomes JPEG.
    """
    try:
        from PIL import Image  # lazy import — only paid when first figure renders
    except ImportError:
        return _embed_image(file_path)

    try:
        path = Path(file_path)
        if not path.exists():
            return None

        with Image.open(path) as im:
            # No-op resize when source is already small enough.
            if im.width <= max_width:
                im.load()  # so the file handle can close
                full = _embed_image(file_path)
                return full

            # Preserve transparency by staying PNG; otherwise JPEG is
            # 5-10x smaller for photos and screenshots.
            has_alpha = (
                im.mode in ("RGBA", "LA")
                or (im.mode == "P" and "transparency" in im.info)
            )

            ratio = max_width / im.width
            new_size = (max_width, max(1, int(im.height * ratio)))
            im_resized = im.resize(new_size, Image.Resampling.LANCZOS)

            buf = io.BytesIO()
            if has_alpha:
                im_resized.convert("RGBA").save(buf, format="PNG", optimize=True)
                mime = "image/png"
            else:
                im_resized.convert("RGB").save(
                    buf, format="JPEG",
                    quality=_PROSE_THUMB_JPEG_QUALITY,
                    optimize=True, progressive=True,
                )
                mime = "image/jpeg"
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:{mime};base64,{b64}"
    except Exception:
        # PIL choked (unsupported format, corrupt file, etc.) — fall
        # back to raw embed so the figure still shows.
        return _embed_image(file_path)


def _prose_edit_span(elem_id: str, value: str, field: str,
                     css_class: str, label_tag: str = "") -> str:
    """Editable contenteditable span — same shape used by tree view so
    the single JS bridge handles both. label_tag is just for the
    aria-label suffix, not rendered."""
    return (
        f'<span class="{css_class}" contenteditable="true" spellcheck="true" '
        f'data-elem-id="{escape(elem_id)}" data-field="{field}" '
        f'role="textbox" aria-label="Edit {field} for {escape(label_tag or "element")}">'
        f'{escape(value)}</span>'
    )


def _prose_text_or_children(elem: dict, editable: bool) -> str:
    """Render an element's text — if children own the text (Span/Link
    with their own text), emit child spans; otherwise emit the parent's
    own editable text. Matches _sr_build_node_html's children-own-MCR
    rule so editing always targets the element that feeds /ActualText.
    """
    children = elem.get("children") or []
    elem_id = elem.get("id", "")
    text = elem.get("text") or ""
    tag = elem.get("tag", "")

    children_own_text = any(
        c.get("tag", "") in _SR_EDITABLE_TEXT_TAGS and c.get("text")
        for c in children
    )
    if children_own_text:
        parts = []
        for child in children:
            child_id = child.get("id") or f"{elem_id}.child_{child.get('tag', '')}"
            child_text = child.get("text") or ""
            child_tag = child.get("tag", "")
            if not child_text:
                continue
            if editable and child_id and child_tag in _SR_EDITABLE_TEXT_TAGS:
                parts.append(_prose_edit_span(
                    child_id, child_text, "text", "sr-edit-text", child_tag))
            else:
                parts.append(escape(child_text))
            parts.append(" ")
        return "".join(parts).rstrip()

    if not text:
        return ""
    if editable and elem_id and tag in _SR_EDITABLE_TEXT_TAGS:
        return _prose_edit_span(elem_id, text, "text", "sr-edit-text", tag)
    return escape(text)


def _prose_heading(elem: dict, editable: bool) -> str:
    h_tag = _PROSE_HEADING_TAGS.get(elem.get("tag", ""), "h2")
    inner = _prose_text_or_children(elem, editable)
    return f'<{h_tag}>{inner}</{h_tag}>'


def _prose_paragraph(elem: dict, editable: bool) -> str:
    inner = _prose_text_or_children(elem, editable)
    if not inner:
        return ""
    return f'<p>{inner}</p>'


def _prose_code(elem: dict, editable: bool) -> str:
    inner = _prose_text_or_children(elem, editable)
    return f'<pre class="sr-prose-code"><code>{inner}</code></pre>'


def _prose_blockquote(elem: dict, editable: bool) -> str:
    inner = _prose_text_or_children(elem, editable)
    return f'<blockquote>{inner}</blockquote>'


def _prose_figure(elem: dict, editable: bool) -> str:
    elem_id = elem.get("id", "")
    image_ref = elem.get("image_ref") or ""
    alt_text = elem.get("alt_text") or ""

    img_html = ""
    if image_ref:
        data_uri = _file_url(image_ref)
        if data_uri:
            img_html = (
                f'<img src="{data_uri}" '
                f'alt="{escape(alt_text or "Figure")}" '
                f'loading="lazy" decoding="async">'
            )
        else:
            img_html = (
                '<div class="sr-prose-img-missing" role="img" '
                'aria-label="Image unavailable">[image unavailable]</div>'
            )

    if editable and elem_id:
        cap_html = _prose_edit_span(
            elem_id, alt_text, "alt_text", "sr-edit-alt", "Figure")
    else:
        cap_html = escape(alt_text)
    return f'<figure>{img_html}<figcaption>{cap_html}</figcaption></figure>'


def _prose_formula(elem: dict, editable: bool) -> str:
    elem_id = elem.get("id", "")
    image_ref = elem.get("image_ref") or ""
    alt_text = elem.get("alt_text") or ""
    latex = elem.get("latex") or ""

    img_html = ""
    if image_ref:
        data_uri = _file_url(image_ref)
        if data_uri:
            img_html = (
                f'<img src="{data_uri}" '
                f'alt="{escape(alt_text or "Formula")}" '
                f'loading="lazy" decoding="async">'
            )

    if editable and elem_id:
        cap_html = _prose_edit_span(
            elem_id, alt_text, "alt_text", "sr-edit-alt", "Formula")
    else:
        cap_html = escape(alt_text)

    latex_html = (
        f'<div class="sr-formula-latex">{escape(latex)}</div>'
        if latex else ""
    )
    return f'<figure class="sr-prose-formula">{img_html}<figcaption>{cap_html}</figcaption>{latex_html}</figure>'


def _prose_table(elem: dict, editable: bool) -> str:
    """Render a Table with proper rowspan/colspan handling.

    Walks cells[] sorted by (row, col). A covered-set tracks positions
    claimed by row_span/col_span from earlier cells so we skip cells
    that overlap those positions (mirrors the safety guard in the Java
    tagger's table builder).
    """
    elem_id = elem.get("id", "")
    cells = elem.get("cells") or []
    table_caption = elem.get("caption") or ""

    # Group cells by row, sorted column ascending within each row.
    rows: Dict[int, list] = {}
    for c in cells:
        r = int(c.get("row", 0) or 0)
        rows.setdefault(r, []).append(c)
    for r in rows:
        rows[r].sort(key=lambda c: int(c.get("col", 0) or 0))

    covered = set()  # (row, col) tuples claimed by earlier spans
    has_header_row = any(
        any(cl.get("is_header") for cl in row_cells)
        for row_cells in rows.values()
    )

    anchor = f' id="vis-target-{escape(elem_id)}"' if elem_id else ""
    parts = [f'<div class="sr-prose-table-wrap"{anchor}><table>']
    if table_caption:
        parts.append(f'<caption>{escape(table_caption)}</caption>')

    sorted_rows = sorted(rows.keys())
    in_thead = False
    if has_header_row:
        parts.append('<thead>')
        in_thead = True
    body_open = False

    for r in sorted_rows:
        row_cells = rows[r]
        # Heuristic: row is part of thead if all its non-skipped cells
        # are headers. Switch to tbody on first non-all-header row.
        non_covered = [c for c in row_cells
                       if (r, int(c.get("col", 0) or 0)) not in covered]
        if not non_covered:
            continue
        all_header = all(c.get("is_header") for c in non_covered)
        if in_thead and not all_header:
            parts.append('</thead>')
            in_thead = False
            parts.append('<tbody>')
            body_open = True
        elif not in_thead and not body_open:
            parts.append('<tbody>')
            body_open = True

        parts.append('<tr>')
        for c in row_cells:
            col = int(c.get("col", 0) or 0)
            if (r, col) in covered:
                continue
            rs = int(c.get("row_span", 1) or 1)
            cs = int(c.get("col_span", 1) or 1)
            for rr in range(r, r + rs):
                for cc in range(col, col + cs):
                    if (rr, cc) != (r, col):
                        covered.add((rr, cc))
            cell_tag = "th" if c.get("is_header") else "td"
            scope_attr = ""
            if c.get("is_col_header"):
                scope_attr = ' scope="col"'
            elif c.get("is_row_header"):
                scope_attr = ' scope="row"'
            rs_attr = f' rowspan="{rs}"' if rs > 1 else ""
            cs_attr = f' colspan="{cs}"' if cs > 1 else ""

            cell_text = c.get("text") or ""
            cell_key = f"{elem_id}.cell_{r}_{col}"
            if editable and elem_id:
                cell_inner = _prose_edit_span(
                    cell_key, cell_text, "text", "sr-edit-text", cell_tag.upper())
            else:
                cell_inner = escape(cell_text)
            parts.append(
                f'<{cell_tag}{scope_attr}{rs_attr}{cs_attr}>{cell_inner}</{cell_tag}>'
            )
        parts.append('</tr>')

    if in_thead:
        parts.append('</thead>')
    if body_open:
        parts.append('</tbody>')
    parts.append('</table></div>')
    return "".join(parts)


def _prose_list_child(child: dict, parent_id: str, editable: bool,
                      keymap: "dict | None" = None) -> str:
    """Render a single LI under a parent L/TOC, recursively handling
    LBody / Lbl / nested L children. The child elem may also have
    text directly on it for flat lists.

    *keymap* maps ``id(leaf_node) -> save key`` (from ``iter_list_edit_targets``)
    so list items get unique positional keys instead of the colliding
    ``f"{parent}.child_{tag}"``. Same keys the save path resolves → no drift.
    """
    tag = child.get("tag", "")
    fallback_id = child.get("id") or f"{parent_id}.child_{tag}"
    child_id = (keymap or {}).get(id(child)) or fallback_id
    text = child.get("text") or ""
    grandchildren = child.get("children") or []

    if tag in ("L",):
        # Nested list — keymap already covers its descendants.
        return _prose_render_list(child, editable, keymap=keymap)

    if tag == "LI":
        inner_parts = []
        for gc in grandchildren:
            inner_parts.append(_prose_list_child(gc, child_id, editable, keymap=keymap))
        if not any(inner_parts) and text:
            if editable:
                inner_parts.append(_prose_edit_span(
                    child_id, text, "text", "sr-edit-text", "LI"))
            else:
                inner_parts.append(escape(text))
        return f'<li>{"".join(inner_parts)}</li>'

    if tag == "Lbl":
        # List label — rendered inline before LBody. We let CSS bullets
        # handle the visual marker; show Lbl text inline if non-empty.
        if not text:
            return ""
        if editable:
            return _prose_edit_span(
                child_id, text, "text", "sr-edit-text", "Lbl") + " "
        return f'{escape(text)} '

    if tag == "LBody":
        if editable and text:
            return _prose_edit_span(
                child_id, text, "text", "sr-edit-text", "LBody")
        return escape(text)

    # Unknown child tag — render text as plain span
    if text:
        if editable:
            return _prose_edit_span(
                child_id, text, "text", "sr-edit-text", tag or "item")
        return escape(text)
    return ""


def _prose_render_list(elem: dict, editable: bool,
                       keymap: "dict | None" = None) -> str:
    elem_id = elem.get("id", "")
    # Build the id(leaf)->key map once at the top-level list; nested calls reuse
    # it (so render keys exactly match the save path — see iter_list_edit_targets).
    if keymap is None:
        keymap = {id(leaf): key
                  for key, leaf, _li, _kind in iter_list_edit_targets(elem, elem_id)}
    children = elem.get("children") or []
    li_html_parts = []
    for child in children:
        if child.get("tag") == "LI":
            li_html_parts.append(_prose_list_child(child, elem_id, editable, keymap=keymap))
        elif child.get("tag") == "L":
            # Bare nested list — wrap as LI to keep semantic
            li_html_parts.append(
                f'<li>{_prose_render_list(child, editable, keymap=keymap)}</li>'
            )
        else:
            inner = _prose_list_child(child, elem_id, editable, keymap=keymap)
            if inner:
                li_html_parts.append(f'<li>{inner}</li>')
    anchor = f' id="vis-target-{escape(elem_id)}"' if elem_id else ""
    return f'<ul{anchor}>{"".join(li_html_parts)}</ul>'


def _prose_toc(elem: dict, editable: bool) -> str:
    elem_id = elem.get("id", "")
    children = elem.get("children") or []
    items = []
    for child in children:
        text = child.get("text") or ""
        child_id = child.get("id") or f"{elem_id}.child_{child.get('tag', '')}"
        if not text:
            continue
        if editable:
            inner = _prose_edit_span(
                child_id, text, "text", "sr-edit-text", "TOCI")
        else:
            inner = escape(text)
        items.append(f'<li>{inner}</li>')
    if not items:
        return ""
    return (
        f'<nav class="sr-prose-toc" aria-label="Table of contents">'
        f'<ul>{"".join(items)}</ul></nav>'
    )


def _prose_fallback_div(elem: dict, editable: bool) -> str:
    tag = elem.get("tag", "?")
    text = elem.get("text") or ""
    elem_id = elem.get("id", "")
    if not text and not (elem.get("children") or []):
        return ""
    if editable and elem_id:
        inner = _prose_edit_span(elem_id, text, "text", "sr-edit-text", tag)
    else:
        inner = escape(text)
    return f'<div class="sr-prose-unknown" data-tag="{escape(tag)}">{inner}</div>'


_PROSE_BUILDERS = {
    "H1": _prose_heading, "H2": _prose_heading, "H3": _prose_heading,
    "H4": _prose_heading, "H5": _prose_heading, "H6": _prose_heading,
    "P": _prose_paragraph, "Span": _prose_paragraph,
    "Caption": _prose_paragraph, "TOCI": _prose_paragraph,
    "Code": _prose_code,
    "BlockQuote": _prose_blockquote,
    "Figure": _prose_figure,
    "Formula": _prose_formula,
    "Table": _prose_table,
    "L": _prose_render_list,
    "List": _prose_render_list,
    "TOC": _prose_toc,
}


def _structure_json_to_prose_html(struct_path: str, editable: bool = True) -> str:
    """Render the enriched structure JSON as document-style prose.

    Same data-elem-id / data-field / sr-edit-text|alt spans as the tree
    view, so the JS edit bridge handles both. The caller wraps this
    output in a single host element that owns data-sr-sink-id.
    """
    if not struct_path or not Path(struct_path).exists():
        return (
            '<div class="sr-prose-view sr-prose-empty">'
            'No document content available.</div>'
        )
    try:
        struct = json.loads(Path(struct_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (
            '<div class="sr-prose-view sr-prose-empty">'
            'Could not read document structure.</div>'
        )

    elements = struct.get("elements") or []
    parts = []
    for elem in elements:
        tag = elem.get("tag", "")
        builder = _PROSE_BUILDERS.get(tag, _prose_fallback_div)
        try:
            html = builder(elem, editable)
        except Exception as e:
            logger.warning("Prose builder failed for tag=%s id=%s: %s",
                           tag, elem.get("id", ""), e)
            html = ""
        if html:
            parts.append(html)

    return (
        f'<article class="sr-prose-view">'
        f'{"".join(parts)}'
        f'</article>'
    )


def _vis_pct_rect(bbox: dict, pw: float, ph: float) -> str:
    """CSS percent-position style for a PDF bbox over a full-page image.

    PDF bbox is bottom-left origin {l,b,r,t}; the image is top-left origin.
    Percentages of page dims keep the overlay aligned at any display width
    and any render DPI (see _render_coverage_thumbnail for the same flip).
    """
    if pw <= 0 or ph <= 0:
        return "left:0;top:0;width:0;height:0;"
    left = 100.0 * bbox["l"] / pw
    top = 100.0 * (ph - bbox["t"]) / ph
    width = 100.0 * (bbox["r"] - bbox["l"]) / pw
    height = 100.0 * (bbox["t"] - bbox["b"]) / ph
    return f"left:{left:.3f}%;top:{top:.3f}%;width:{width:.3f}%;height:{height:.3f}%;"


# Tags that route to the Reading view for editing rather than inline boxes
# (their children carry text that can't be unambiguously addressed by id —
# see pipeline_core child-key collision). Tables are edited in place (per-cell
# boxes, see _vis_table_cell_boxes); TOC still shows one routing box.
_VIS_ROUTE_TAGS = frozenset({"Table", "TOC"})


def _visual_table_edit_enabled() -> bool:
    """Kill-switch for in-place table editing in the Visual view. When false,
    tables route to the Reading view (the pre-2026-07 behaviour)."""
    return bool(_load_config().get("editor", {}).get("visual_table_edit_enabled", True))


def _vis_table_cell_boxes(elem: dict, elem_id: str, pw: float, ph: float) -> list:
    """One editable Visual-overlay box per table cell, positioned at the cell's
    bbox. Each cell reuses the exact ``{elem_id}.cell_{r}_{c}`` edit key the
    Reading view emits (see _prose_table), so the existing JS bridge + save path
    apply the edit unchanged — no backend change.

    Returns [] when no cell carries a usable bbox (e.g. grid-gap cells synthesized
    by _normalize_table_grid); the caller then falls back to the routing box.
    """
    boxes = []
    for c in elem.get("cells") or []:
        bbox = c.get("bbox")
        if not bbox or not all(k in bbox for k in ("l", "t", "r", "b")):
            continue
        row = int(c.get("row", 0) or 0)
        col = int(c.get("col", 0) or 0)
        cell_key = f"{elem_id}.cell_{row}_{col}"
        is_header = bool(c.get("is_header"))
        cell_tag = "TH" if is_header else "TD"
        header_cls = " vis-box-cell-header" if is_header else ""
        inner = _prose_edit_span(
            cell_key, c.get("text") or "", "text", "sr-edit-text", cell_tag)
        boxes.append(
            f'<div class="vis-box vis-box-cell{header_cls}" '
            f'style="{_vis_pct_rect(bbox, pw, ph)}" data-tag="{cell_tag}" '
            f'data-elem-id="{escape(cell_key)}">'
            f'<div class="vis-box-inner">{inner}</div></div>'
        )
    return boxes


def _structure_json_to_overlay_html(
    struct_path: str, page_no: int, pdf_path: str,
    pages_info: dict, sink_id: str = "",
) -> str:
    """Render ONE page as the page image plus an editable box over each tagged
    element, positioned at its real bbox. Used as the preview pane of the
    Reading-order view (which supplies its own pagebar).

    Editable boxes reuse the same sr-edit spans the prose/tree views emit, so
    the existing JS edit bridge + save path handle them unchanged. The wrapper
    carries data-sr-sink-id so edits resolve to the existing hidden sink.
    """
    if not struct_path or not Path(struct_path).exists():
        return '<div class="vis-empty">No document content available.</div>'
    try:
        struct = json.loads(Path(struct_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return '<div class="vis-empty">Could not read document structure.</div>'

    pages = struct.get("pages") or pages_info or {}
    n_pages = len(pages) or 1
    page_no = max(1, min(int(page_no or 1), n_pages))
    pdims = pages.get(str(page_no)) or {}
    pw = float(pdims.get("width") or 0)
    ph = float(pdims.get("height") or 0)

    elements = struct.get("elements") or []
    page_elems = [
        e for e in elements
        if e.get("page_no") == page_no
        and e.get("bbox")
        and all(k in e["bbox"] for k in ("l", "t", "r", "b"))
    ]

    boxes = []
    for e in page_elems:
        bbox = e["bbox"]
        style = _vis_pct_rect(bbox, pw, ph)
        tag = e.get("tag", "")
        elem_id = e.get("id", "")
        seq = e.get("reading_order", "")
        seq_badge = f'<span class="vis-seq">{escape(str(seq))}</span>' if seq != "" else ""
        common = (
            f'style="{style}" data-tag="{escape(tag or "?")}" '
            f'data-seq="{escape(str(seq))}" data-elem-id="{escape(elem_id or "")}"'
        )
        if tag in _SR_EDITABLE_ALT_TAGS:
            inner = _prose_edit_span(elem_id, e.get("alt_text") or "",
                                     "alt_text", "sr-edit-alt", tag) if elem_id else ""
            boxes.append(
                f'<div class="vis-box vis-box-fig" {common}>{seq_badge}'
                f'<div class="vis-box-inner">{inner}</div></div>'
            )
        elif tag == "L":
            # Editable list: one box per LI at the LI's bbox, editing the leaf
            # the tagger reads (LBody / flat LI). Lbl edits stay in Reading view.
            # Keys come from the shared generator, so they match the save path.
            seen_li = set()
            for key, leaf, li_node, kind in iter_list_edit_targets(e, elem_id):
                if kind == "lbl" or id(li_node) in seen_li:
                    continue
                li_bbox = li_node.get("bbox")
                if not li_bbox or not all(k in li_bbox for k in ("l", "t", "r", "b")):
                    continue
                if li_node.get("page_no", page_no) != page_no:
                    continue  # multi-page list: off-page items stay editable in Reading
                seen_li.add(id(li_node))
                li_seq = li_node.get("reading_order", "")
                li_badge = (f'<span class="vis-seq">{escape(str(li_seq))}</span>'
                            if li_seq != "" else "")
                li_inner = _prose_edit_span(
                    key, leaf.get("text") or "", "text", "sr-edit-text", "LBody")
                boxes.append(
                    f'<div class="vis-box vis-box-list" '
                    f'style="{_vis_pct_rect(li_bbox, pw, ph)}" data-tag="LI" '
                    f'data-seq="{escape(str(li_seq))}">{li_badge}'
                    f'<div class="vis-box-inner">{li_inner}</div></div>'
                )
        elif tag in _VIS_ROUTE_TAGS:
            cell_boxes = (
                _vis_table_cell_boxes(e, elem_id, pw, ph)
                if tag == "Table" and elem_id and _visual_table_edit_enabled()
                else []
            )
            if cell_boxes:
                boxes.extend(cell_boxes)
            else:
                # No positioned cells (or TOC / kill-switch off): route to Reading.
                cls = "vis-box-table" if tag == "Table" else "vis-box-list"
                boxes.append(
                    f'<div class="vis-box {cls}" {common} '
                    f'data-target-id="{escape(elem_id)}" '
                    f'onclick="window._visOpenTable(this)" '
                    f'title="Open in Reading view to edit">{seq_badge}</div>'
                )
        else:
            inner = _prose_text_or_children(e, editable=True)
            cls = "vis-box-text" if inner else "vis-box-empty"
            boxes.append(
                f'<div class="vis-box {cls}" {common}>{seq_badge}'
                f'<div class="vis-box-inner">{inner}</div></div>'
            )

    # Untagged regions — read-only awareness (red dashed).
    for bb in _compute_untagged_regions(pdf_path, page_no, page_elems, pages):
        if not all(k in bb for k in ("l", "t", "r", "b")):
            continue
        style = _vis_pct_rect(bb, pw, ph)
        boxes.append(
            f'<div class="vis-box vis-box-untagged" style="{style}" '
            f'data-tag="UNTAGGED" title="Untagged content — not read by screen readers"></div>'
        )

    img_url = _render_full_page_image(pdf_path, page_no, struct_path)
    if img_url:
        page_body = (
            f'<div class="vis-page-wrap">'
            f'<img class="vis-page-img" src="{img_url}" '
            f'alt="Page {page_no}" draggable="false" loading="lazy" decoding="async">'
            f'<div class="vis-overlay">{"".join(boxes)}</div>'
            f'</div>'
        )
    else:
        page_body = '<div class="vis-empty">Page image unavailable.</div>'

    # No nav bar: the Reading-order view supplies its own pagebar around this.
    return (
        f'<div class="vis-page-host-inner" data-sr-sink-id="{escape(sink_id)}" '
        f'data-page="{page_no}" data-page-count="{n_pages}">'
        f'{page_body}</div>'
    )


def _ro_row_label(elem: dict) -> str:
    """A legible one-line label for a reading-order row.

    Text-bearing blocks show their (truncated) text; text-less containers fall
    back to a tag + child-count summary so the row is never blank.
    """
    text = (elem.get("text") or "").strip()
    if text:
        return text[:120]
    tag = elem.get("tag") or "Block"
    children = elem.get("children") or []
    if tag in ("L", "List"):
        n = sum(1 for c in children if (c.get("tag") or "") == "LI")
        return f"List · {n} item{'s' if n != 1 else ''}"
    if tag in ("Table",):
        rows = elem.get("cells") or children
        return f"Table · {len(rows)} row{'s' if len(rows) != 1 else ''}"
    if tag in ("Figure", "Formula"):
        alt = (elem.get("alt_text") or "").strip()
        return f"{tag}: {alt[:100]}" if alt else tag
    return tag


def _ro_initial_page(sr_quality: dict) -> int:
    """Return the page the Reading-order view should open on: the worst-tau
    flagged page, or 1 when nothing is flagged.

    Page 1 is frequently *not* a reading-order failure, so defaulting the view
    to it stranded users on a passing page next to a "flagged pages" banner.
    ``per_page`` keys may be ints (pre-save) or strings (after a JSON save
    round-trip), so both are consulted.
    """
    failing = list((sr_quality or {}).get("reading_order_failing_pages") or [])
    if not failing:
        return 1
    per_page = (sr_quality or {}).get("reading_order_per_page") or {}

    def _tau(p):
        return per_page.get(p, per_page.get(str(p), 1.0))

    return int(min(failing, key=_tau))


def _reading_order_view_html(struct_path, page_no, pdf_path, gate_info,
                             text_sink_id, order_sink_id):
    """Render the Reading-order view for one page: gate banner + page nav +
    draggable read-sequence list (left) + page preview (right)."""
    if not struct_path or not Path(struct_path).exists():
        return '<div class="vis-empty">No document content available.</div>'
    try:
        struct = json.loads(Path(struct_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return '<div class="vis-empty">Could not read document structure.</div>'

    pages = struct.get("pages") or {}
    n_pages = len(pages) or 1
    page_no = max(1, min(int(page_no or 1), n_pages))

    failing = list((gate_info or {}).get("failing_pages") or [])
    per_page = (gate_info or {}).get("per_page") or {}
    # Banner chips, worst-first by tau.
    chips = ""
    if failing:
        # per_page keys are ints pre-save but become strings after the JSON
        # save round-trip (_sanitize_for_json); the int+str dual lookup is required.
        ranked = sorted(failing, key=lambda p: per_page.get(p, per_page.get(str(p), 1.0)))
        chip_html = []
        for p in ranked:
            tau = per_page.get(p, per_page.get(str(p)))
            tau_txt = f" · τ{tau:.2f}" if isinstance(tau, (int, float)) else ""
            cls = "ro-chip on" if p == page_no else "ro-chip"
            chip_html.append(
                f'<button type="button" class="{cls}" '
                f'onclick="window._roSetPageNum(this, {int(p)})">p.{int(p)}{escape(tau_txt)}</button>'
            )
        chips = (
            '<div class="ro-banner" role="note">'
            '<span class="ro-banner-txt">Reading order may not match the visual '
            'layout. Flagged pages (worst first):</span>'
            f'<span class="ro-chips">{"".join(chip_html)}</span></div>'
        )

    # Left: the read-sequence list (one row per top-level block on this page).
    elements = struct.get("elements") or []
    page_elems = [e for e in elements if e.get("page_no") == page_no and e.get("id")]
    rows = []
    for pos, e in enumerate(page_elems, start=1):
        eid = e.get("id")
        tag = e.get("tag") or ""
        label = _ro_row_label(e)
        rows.append(
            f'<li class="ro-row" draggable="true" tabindex="0" role="option" '
            f'data-elem-id="{escape(eid or "")}" data-ro-page="{page_no}" '
            f'aria-label="Position {pos}: {escape(tag)} {escape(label)}">'
            f'<span class="ro-grip" aria-hidden="true">⠿</span>'
            f'<span class="ro-num">{pos}</span>'
            f'<span class="ro-typ">{escape(tag)}</span>'
            f'<span class="ro-txt">{escape(label)}</span></li>'
        )
    rows_html = (
        "".join(rows) if rows
        else "<li class='ro-empty'>No reorderable blocks on this page.</li>"
    )
    list_html = (
        f'<ol class="ro-list" data-ro-page="{page_no}" '
        f'data-ro-sink-id="{escape(order_sink_id)}" '
        f'aria-label="Reading sequence for page {page_no}">'
        f'{rows_html}'
        f'</ol>'
    )

    # Right: the page-image overlay (boxes carry data-elem-id for cross-highlight
    # and reuse the sr-edit spans, so cells/text stay editable here too).
    preview_html = _structure_json_to_overlay_html(
        struct_path, page_no, pdf_path, pages, sink_id=text_sink_id,
    )

    nav = (
        '<div class="ro-pagebar">'
        f'<button type="button" class="ro-nav" onclick="window._roSetPage(this, -1)">‹ Prev</button>'
        f'<span class="ro-cur">Page {page_no} of {n_pages}</span>'
        f'<button type="button" class="ro-nav" onclick="window._roSetPage(this, 1)">Next ›</button>'
        '<span class="ro-hint">Drag a row, or focus one and press Alt+↑/↓</span>'
        '</div>'
    )

    return (
        f'<div class="ro-view-inner" data-page="{page_no}" data-page-count="{n_pages}">'
        f'{chips}{nav}'
        f'<div class="ro-work">'
        f'<div class="ro-listcol"><div class="ro-collabel">Read sequence</div>{list_html}</div>'
        f'<div class="ro-splitter" role="separator" aria-orientation="vertical" '
        f'aria-label="Resize list and preview" title="Drag to resize"></div>'
        f'<div class="ro-pagecol"><div class="ro-collabel">Page {page_no} preview</div>{preview_html}</div>'
        f'</div></div>'
    )


def _render_reading_order_page(session, page_str, doc_idx, text_sink_id, order_sink_id):
    """Gradio handler: render the Reading-order view for one page (lazy; does
    not retrigger @gr.render)."""
    try:
        results = session.get("results", [])
        if doc_idx < 0 or doc_idx >= len(results):
            return '<div class="vis-empty">No document.</div>'
        pr = results[doc_idx].get("pipeline_result", {})
        pdf_path = pr.get("pdf_path", "")
        struct_path = pr.get("structure_json_path", "")
        sr_quality = pr.get("sr_quality", {}) or {}
        gate_info = {
            "failing_pages": sr_quality.get("reading_order_failing_pages", []),
            "per_page": sr_quality.get("reading_order_per_page", {}),
        }
        try:
            page_no = int(float(str(page_str).strip() or "1"))
        except (ValueError, TypeError):
            page_no = 1
        return _reading_order_view_html(
            struct_path, page_no, pdf_path, gate_info, text_sink_id, order_sink_id,
        )
    except Exception as e:
        logger.warning("Reading-order page render failed: %s", e)
        return '<div class="vis-empty">Could not render page.</div>'


def _build_overview_metrics_html(doc: dict) -> str:
    """Build summary metric cards for the Overview tab."""
    pr = doc.get("pipeline_result", {})
    post_audit = pr.get("post_audit", {})
    pre_audit = pr.get("audit", {})
    compliance = post_audit.get("compliance", {})
    sr = pr.get("sr_quality", {}) or {}

    # WCAG score
    post_score = compliance.get("overall_score", 0)
    pre_score = (pre_audit or {}).get("compliance", {}).get("overall_score", 0)
    delta = post_score - pre_score
    delta_str = f"+{delta:.0f}%" if delta >= 0 else f"{delta:.0f}%"

    # Criteria
    by_criterion = compliance.get("by_criterion", {})
    aa_crit = {c for c in by_criterion if _LEVEL.get(c) in ("A", "AA")}
    failing = set()
    for issue in post_audit.get("issues", []):
        if (issue.get("remediation_status") == "needs_remediation"
                and not issue.get("type", "").startswith("pdf-")):
            crit = issue.get("wcag_criterion", "")
            if crit and _LEVEL.get(crit) in ("A", "AA"):
                failing.add(crit)
    passing = len(aa_crit) - len(failing)
    total_crit = len(aa_crit)

    # SR quality — surface an action count rather than a grade. "good" reads as
    # verified, "poor" keeps the at-a-glance broken signal, and "fair" becomes
    # the number of pages the user should review (from _sr_action_items).
    sr_quality = sr.get("sr_quality", "unknown")
    sr_colors = {
        "good": ("#166534", "#DCFCE7"),
        "fair": ("#92400E", "#FEF3C7"),
        "poor": ("#991B1B", "#FEE2E2"),
    }
    sr_fg, sr_bg = sr_colors.get(sr_quality, ("#4b5563", "#f3f4f6"))
    if sr_quality == "good":
        sr_label = "Verified"
    elif sr_quality == "poor":
        sr_label = "Poor"
    elif sr_quality == "fair":
        _sr_pages = sorted({p for it in _sr_action_items(sr) for p in it["pages"]})
        sr_label = (
            f'{len(_sr_pages)} page{"s" if len(_sr_pages) != 1 else ""} to review'
            if _sr_pages else "Review needed"
        )
    else:
        sr_label = "N/A"

    # Changes
    changes = pr.get("remediation", {}).get("change_records", [])
    n_changes = sum(1 for c in changes if c.get("outcome") == "fixed")

    # Images / Tables
    img_records = pr.get("alt_text", {}).get("image_records", [])
    n_img = sum(1 for r in img_records if r.get("outcome") == "generated")
    tbl_captions = pr.get("table_captions", {}).get("captions", {})
    n_tbl = len(tbl_captions)

    font = "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"

    def _card(label: str, value: str, sub: str = "", fg: str = "#374151", bg: str = "#f9fafb") -> str:
        sub_html = f'<div style="font-size:0.6875rem;color:#374151;margin-top:2px;">{escape(sub)}</div>' if sub else ""
        return (
            f'<div style="flex:1;min-width:120px;background:{bg};border:1px solid #e5e7eb;'
            f'border-radius:8px;padding:12px 16px;text-align:center;">'
            f'<div style="font-size:1.375rem;font-weight:700;color:{fg};">{escape(value)}</div>'
            f'<div style="font-size:0.75rem;color:#374151;margin-top:4px;">{escape(label)}</div>'
            f'{sub_html}</div>'
        )

    # Count SR-specific critical issues for SR card sub-line (not WCAG audit criticals)
    hollow = sr.get("hollow_pages", [])
    fig_missing = sr.get("figures_missing_alt", 0)
    pipeline_warnings = pr.get("warnings", [])
    n_sr_critical = _count_critical_issues(
        [], hollow, sr_quality, fig_missing, pipeline_warnings,
    )
    sr_sub = (
        f'{n_sr_critical} Critical Issue{"s" if n_sr_critical != 1 else ""} Found'
        if n_sr_critical > 0 else ""
    )

    if post_score >= 100:
        score_fg, score_bg = "#166534", "#f9fafb"
    elif post_score >= 70:
        score_fg, score_bg = "#92400E", "#FEF3C7"
    else:
        score_fg, score_bg = "#991B1B", "#FEE2E2"
    cards = [
        _card("WCAG 2.1 AA Score", f"{post_score:.0f}%", delta_str, fg=score_fg, bg=score_bg),
        _card("WCAG 2.1 AA Passing Criteria", f"{passing}/{total_crit}"),
        _card("Screen Reader Verification", sr_label, sr_sub, fg=sr_fg, bg=sr_bg),
        _card("Changes Applied", str(n_changes)),
    ]
    if n_img or n_tbl:
        cards.append(_card("Alt Tags Created (AI Generated)", f"{n_img} img / {n_tbl} tbl"))

    return (
        f'<div style="{font}display:flex;gap:12px;flex-wrap:wrap;margin:12px 0;">'
        + "".join(cards)
        + "</div>"
    )


def _precompute_tab_data(doc: dict, doc_idx: int) -> None:
    """Pre-compute WCAG report HTML and SR preview JSON for inline tabs."""
    try:
        session_stub = {"results": [None] * (doc_idx + 1)}
        session_stub["results"][doc_idx] = doc
        doc["wcag_full_report_html"] = _get_wcag_report_html(session_stub, doc_idx)
    except Exception:
        logger.debug("Failed to pre-compute WCAG report for %s", doc.get("filename"))
        doc["wcag_full_report_html"] = ""
    try:
        session_stub = {"results": [None] * (doc_idx + 1)}
        session_stub["results"][doc_idx] = doc
        doc["sr_preview_json"] = _get_sr_preview_json(session_stub, doc_idx)
    except Exception:
        logger.debug("Failed to pre-compute SR preview for %s", doc.get("filename"))
        doc["sr_preview_json"] = ""





def _editor_limits() -> EditorLimits:
    """Resolve Content Editor input caps from config.yaml `editor:` section."""
    return EditorLimits.from_config(_load_config().get("editor"))


_MAX_EDITOR_STATE_ENTRIES = 2000


def _cap_editor_state(merged: dict) -> dict:
    """Keep only the newest entries so per-session editor state stays bounded."""
    if len(merged) > _MAX_EDITOR_STATE_ENTRIES:
        merged = dict(list(merged.items())[-_MAX_EDITOR_STATE_ENTRIES:])
    return merged


def _merge_editor_entries(edits: dict, new_entries: dict) -> dict:
    """Merge ``new_entries`` as the newest items, then apply the cap.

    ``dict.update`` keeps an existing key in its original position, so a
    re-edit of an old element would otherwise be the first thing evicted.
    """
    merged = dict(edits) if edits else {}
    for key, value in new_entries.items():
        merged.pop(key, None)
        merged[key] = value
    return _cap_editor_state(merged)


def _on_caption_edit(edits: dict, key: str, new_value: str) -> dict:
    """Store a caption edit keyed by 'img:doc_idx:img_idx:src', 'tbl:doc_idx:tbl_key', or 'formula:doc_idx:f_idx:elem_id'."""
    # Captions / alt / formula descriptions are short descriptive text — cap
    # them to the alt-length limit. Preserve a None (clear) as-is.
    if new_value is None:
        value = None
    else:
        value = cap_edit_value(new_value, is_alt=True, limits=_editor_limits())
    return _merge_editor_entries(edits, {key: value})


def _on_sr_edits_batch(edits: dict, doc_idx: int, payload_json: str) -> dict:
    """Merge a JSON batch of inline Screen-Reader edits into caption_edits_state.

    The JS bridge (initSrEditBridge in APP_JS) accumulates per-element edits
    and writes a JSON blob like ``{"elem_0005": {"text": "..."}, ...}`` into
    a hidden Gradio textbox. That textbox's ``.change`` fires this handler.

    Edits are stored under ``"text:{doc_idx}:{elem_id}"`` — the same key
    format that ``_update_pdf_handler`` already parses (see line ~5367).
    The backend (pipeline_core.py:1270-1273) routes the value to ``text``
    or ``alt_text`` based on the element tag, so we don't transmit the
    field name here.
    """
    if not payload_json:
        return edits
    try:
        batch = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return edits
    if not isinstance(batch, dict):
        return edits
    # Defense-in-depth: cap per-field length, bound the batch size, and drop
    # malformed element ids before merging. The values are already escaped on
    # write (json.dump -> iText PdfString), so these caps bound resource use
    # rather than prevent injection.
    limits = _editor_limits()
    new_entries, stats = normalize_sr_edit_batch(batch, doc_idx, limits)
    if stats["dropped_invalid_id"] or stats["batch_truncated"]:
        logger.warning(
            "SR edit batch (doc %s): accepted %d, dropped %d malformed ids, "
            "%d over batch cap (%d)",
            doc_idx, stats["accepted"], stats["dropped_invalid_id"],
            stats["batch_truncated"], limits.max_batch_edits,
        )
    return _merge_editor_entries(edits, new_entries)


def _on_sr_order_batch(edits: dict, doc_idx: int, payload_json: str) -> dict:
    """Merge a reading-order batch into caption_edits_state.

    The order bridge (initSrOrderBridge) writes a JSON blob like
    ``{"3": ["elem_2","elem_1"]}`` (page -> new id order) into a hidden textbox;
    its .change fires this. Stored under ``order:{doc_idx}:{page}`` so
    _update_pdf_handler routes it to order_edits.
    """
    if not payload_json:
        return edits
    try:
        batch = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return edits
    if not isinstance(batch, dict):
        return edits
    limits = _editor_limits()
    new_entries, stats = normalize_sr_order_batch(batch, doc_idx, limits)
    if stats["dropped_invalid_id"] or stats["dropped_invalid_page"]:
        logger.warning(
            "SR order batch (doc %s): accepted %d page(s), dropped %d ids, %d pages",
            doc_idx, stats["accepted"], stats["dropped_invalid_id"],
            stats["dropped_invalid_page"],
        )
    return _merge_editor_entries(edits, new_entries)


def _update_pdf_handler(
    session: dict,
    caption_edits: dict,
    doc_idx: int,
    progress=gr.Progress(track_tqdm=False),
) -> tuple:
    """Re-run output phases with edited captions for a single document.

    Returns (session, download_files, status_html, cleared_caption_edits).
    """
    results = session.get("results", [])
    if doc_idx >= len(results):
        return session, [], '<div style="color:#991b1b;">Document not found — your session may have expired. Refresh the page and re-upload.</div>', caption_edits

    doc = results[doc_idx]
    pipeline_result = doc.get("pipeline_result", {})
    if not pipeline_result:
        return session, [], '<div style="color:#991b1b;">No pipeline data available — your session may have expired. Refresh the page and re-upload.</div>', caption_edits

    # Collect edits for this document
    image_edits = {}  # src -> new_alt_text
    table_edits = {}  # "table_N" -> new_caption
    formula_edits = {}  # elem_id -> new_description
    text_edits = {}  # elem_id -> new_text
    for key, value in (caption_edits or {}).items():
        parts = key.split(":")
        if len(parts) < 2:
            continue
        try:
            key_doc_idx = int(parts[1])
        except (ValueError, IndexError):
            continue
        if parts[0] == "img" and key_doc_idx == doc_idx:
            src = ":".join(parts[3:])  # src may contain colons
            image_edits[src] = value
        elif parts[0] == "tbl" and key_doc_idx == doc_idx and len(parts) > 2:
            tbl_key = parts[2]  # e.g. "table_0"
            table_edits[tbl_key] = value
        elif parts[0] == "formula" and key_doc_idx == doc_idx and len(parts) > 3:
            elem_id = parts[3]  # element ID from structure JSON
            formula_edits[elem_id] = value
        elif parts[0] == "text" and key_doc_idx == doc_idx and len(parts) > 2:
            elem_id = ":".join(parts[2:])  # elem_id may contain dots/colons
            text_edits[elem_id] = value

    order_edits = parse_order_edits(caption_edits, doc_idx)

    # Risk 2 mitigation: when an SR-tab text edit targets a Figure, also
    # mirror it into image_edits so rerun_output_phases keeps the alt_texts
    # dict and image_records in sync (pipeline_core.py:1175-1189). Without
    # this, _merge_remediation_into_structure could pull the original VLM
    # alt back over the user's edit. image_edits is keyed by the HTML
    # <img src="..."> value, which is the basename of Docling's image_ref.
    if text_edits:
        struct_path = pipeline_result.get("structure_json_path", "")
        if struct_path and Path(struct_path).exists():
            try:
                with open(struct_path, encoding="utf-8") as f:
                    _struct = json.load(f)
                figure_id_to_src = {}
                for el in _struct.get("elements", []):
                    if el.get("tag") in ("Figure", "Formula"):
                        eid = el.get("id", "")
                        image_ref = el.get("image_ref") or ""
                        if eid and image_ref:
                            figure_id_to_src[eid] = Path(image_ref).name
                for eid, new_alt in list(text_edits.items()):
                    src = figure_id_to_src.get(eid)
                    if src and src not in image_edits:
                        image_edits[src] = new_alt
            except (OSError, json.JSONDecodeError):
                pass

    if not image_edits and not table_edits and not formula_edits and not text_edits and not order_edits:
        return session, session.get("all_downloads", []), (
            '<div style="color:#78350f;font-size:0.8125rem;padding:8px;background:#fef3c7;'
            'border-radius:6px;border:1px solid #fcd34d;margin-top:8px;">'
            'No edits detected. Edit alt text, captions, or element text, then click Update PDF.</div>'
        ), caption_edits

    progress(0.1, desc="Updating captions...")

    config = _load_config()
    try:
        updated_result = rerun_output_phases(
            pipeline_result=pipeline_result,
            config=config,
            image_edits=image_edits,
            table_edits=table_edits,
            formula_edits=formula_edits,
            text_edits=text_edits,
            order_edits=order_edits,
            progress_fn=lambda frac, desc="": progress(frac, desc=desc),
        )
    except Exception as e:
        logger.exception("Failed to update PDF for %s", doc["filename"])
        return session, session.get("all_downloads", []), (
            f'<div style="color:#991b1b;font-size:0.8125rem;padding:8px;">'
            f'Update failed: {escape(str(e))}. Try again or refresh the page.</div>'
        ), caption_edits

    # Rebuild report parts with updated result
    pdf_path = Path(pipeline_result.get("source_pdf_path", doc["filename"]))
    verdict_config = config.get("verdict", {})
    report_parts = _build_report_parts(updated_result, pdf_path, verdict_config=verdict_config)

    # Update session
    doc["pipeline_result"] = _sanitize_for_json(updated_result)
    doc["status_html"] = report_parts["status_html"]
    doc["report_html"] = report_parts.get("report_html", "")
    doc["changes_report_html"] = report_parts["changes_report_html"]
    doc["remediation_guide_html"] = report_parts.get("remediation_guide_html", "")
    doc["image_records"] = updated_result.get("alt_text", {}).get("image_records", [])
    doc["table_captions"] = updated_result.get("table_captions", {})
    doc["formula_descriptions"] = updated_result.get("formula_descriptions", {})
    doc["table_previews"] = _build_table_previews(
        updated_result.get("html_path", ""),
        list(doc["table_captions"].get("captions", {}).keys()),
    )
    # Refresh WCAG report and SR preview for inline tabs
    _precompute_tab_data(doc, doc_idx)
    results[doc_idx] = doc
    session["results"] = results

    # Rebuild download list
    include_html = session.get("include_html", False)
    include_html_pdf = session.get("include_html_pdf", False)
    include_report = session.get("include_report", False)
    all_downloads = []
    for r in results:
        dl = r.get("pipeline_result", {}).get("download_files", [])
        all_downloads.extend(dl)
    session["all_downloads_unfiltered"] = list(all_downloads)
    all_downloads = _filter_downloads(all_downloads, include_html, include_html_pdf, include_report)
    session["all_downloads"] = all_downloads

    # Recreate zip
    output_dir = Path(session.get("output_dir", ""))
    zip_path = _create_bulk_zip(all_downloads, output_dir)
    session["zip_path"] = zip_path

    status_html = (
        '<div role="status" style="color:#14532d;font-size:0.8125rem;padding:8px;background:#dcfce7;'
        'border-radius:6px;border:1px solid #86efac;margin-top:8px;">'
        'PDF updated successfully with your edits.</div>'
    )

    # Bump _render_id so @gr.render creates fresh DOM keys (prevents duplicate components)
    session["_render_id"] = session.get("_render_id", 0) + 1
    session["_needs_rerender"] = True

    # render_version is bumped in a separate .then() step
    return session, all_downloads, status_html, {}


def _conditional_render_bump(session: dict, current_render_version):
    """Bump render_version only if _update_pdf_handler actually updated the PDF."""
    if session.get("_needs_rerender", False):
        session.pop("_needs_rerender", None)
        return time.time()
    return current_render_version


# --- Gradio UI ---

with gr.Blocks(
    title=_PAGE_TITLE,
    analytics_enabled=False,
    delete_cache=(1800, 3600),
) as demo:

    # Branded sticky header
    gr.HTML(_build_header_html())
    # Queue status badge (positioned in header top-right via CSS)
    queue_status_html = gr.HTML(
        value=_get_queue_status_html(), visible=True,
        elem_id="queue-status-badge",
    )

    # Main content area
    with gr.Column(elem_id="main-content-area"):

        with gr.Row():
            # Upload + process controls
            with gr.Column(scale=1, min_width=300):
                pdf_input = gr.File(
                    label="Upload PDFs (max 3)",
                    file_types=[".pdf"],
                    file_count="multiple",
                    type="filepath",
                    elem_id="pdf-upload",
                )
                gr.HTML(
                    value=(
                        '<div id="custom-upload-indicator"></div>'
                        '<div id="custom-upload-actions">'
                        '<button type="button" class="custom-upload-btn" id="custom-upload-btn">'
                        '<div class="custom-upload-box">'
                        '<div class="custom-upload-box-label">Upload PDFs</div>'
                        '<div class="custom-upload-box-hint">Click to select files (max 3, PDF only)</div>'
                        '</div>'
                        '</button>'
                        '</div>'
                    ),
                    elem_id="custom-upload-actions-wrap",
                )
                run_btn = gr.Button("Upload PDFs", variant="primary", size="lg", elem_id="run-btn")
                gr.Markdown("**Optional Downloads (HTML)**")
                with gr.Group():
                    opt_include_html = gr.Checkbox(
                        label="Accessible HTML Version",
                        value=False,
                    )
                    # The HTML-as-PDF download is not offered in the UI; the
                    # handlers still accept the flag, so it is a constant here.
                    opt_include_html_pdf = gr.State(value=False)
                    opt_include_report = gr.Checkbox(
                        label="WCAG 2.1 AA Validation Report",
                        value=False,
                    )

            # Results
            with gr.Column(scale=3):
                download_output = gr.Files(label="Download Files", elem_id="output-files", interactive=False)
                gr.HTML(value='<div id="progress-status-bar" role="status" aria-live="polite" aria-atomic="true"></div>', elem_id="progress-bar-wrap")
                log_display = gr.HTML(value="", elem_id="log-overlay")

                zip_output = gr.File(label="Download All (ZIP)", interactive=False, elem_id="zip-output")
                gr.HTML(value='<div id="custom-download-area"></div>')
                session_state = gr.State(value={})
                caption_edits_state = gr.State(value={})
                render_version = gr.State(value=0)

                # Landing page as static component — NOT inside @gr.render
                # so it is unaffected by render triggers / websocket reconnects.
                landing_html = gr.HTML(
                    value=_load_landing_page_html(), elem_id="landing-page"
                )

                @gr.render(
                    inputs=[session_state],
                    triggers=[render_version.change],
                    trigger_mode="once",
                )
                def render_report_tabs(session: dict):
                    results = session.get("results", [])
                    if not results:
                        # Must create a component so Gradio clears old
                        # rendered nodes; bare return leaves stale DOM.
                        gr.HTML(value="", visible=False, key="empty-render")
                        return

                    # Unique prefix per render cycle — forces Gradio to
                    # create fresh DOM nodes instead of reusing stale ones.
                    rid = session.get("_render_id", 0)
                    def k(name, *parts):
                        return f"{name}-{rid}-" + "-".join(str(p) for p in parts) if parts else f"{name}-{rid}"

                    with gr.Tabs(key=k("result-tabs"), selected=k("doc", 0), elem_classes=["result-tabs"]):
                        for i, doc in enumerate(results):
                            label = doc["filename"]
                            if doc.get("status") == "error":
                                label = f"[!] {label}"
                            with gr.Tab(label=label, key=k("doc", i), id=k("doc", i)):
                                is_error = doc.get("status") == "error"

                                with gr.Tabs(key=k("doc-subtabs", i), selected=k("overview", i), elem_classes=["doc-subtabs"]):
                                    # ── Overview tab ──
                                    with gr.Tab("Overview", key=k("overview", i), id=k("overview", i)):
                                        status_html = doc.get("status_html", "")
                                        metrics_html = (
                                            _build_overview_metrics_html(doc) if not is_error else ""
                                        )
                                        pdf_path = (
                                            doc.get("pipeline_result", {}).get("source_pdf_path")
                                            or doc.get("filename", "")
                                        )
                                        thumb_uri = _cached_first_page_thumb(doc, pdf_path)
                                        if thumb_uri:
                                            safe_name = escape(Path(pdf_path).name or "document")
                                            overview_html = (
                                                '<div style="display:flex; gap:16px; '
                                                'align-items:flex-start; margin:8px 0;">'
                                                '<div style="flex:0 0 260px;">'
                                                f'<img src="{thumb_uri}" '
                                                f'alt="First page preview of {safe_name}" '
                                                'style="width:260px; height:auto; display:block; '
                                                'border:1px solid #c8c8c8; border-radius:4px; '
                                                'background:#fff;">'
                                                '</div>'
                                                '<div style="flex:1 1 auto; min-width:0;">'
                                                f'{status_html}{metrics_html}'
                                                '</div>'
                                                '</div>'
                                            )
                                            gr.HTML(value=overview_html, key=k("overview-row", i))
                                        else:
                                            gr.HTML(value=status_html, key=k("status", i))
                                            if not is_error:
                                                gr.HTML(value=metrics_html, key=k("metrics", i))
                                        report = doc.get("report_html", "")
                                        if report:
                                            gr.HTML(value=report, key=k("report", i))

                                    if not is_error:
                                        # ── How to Fix tab (only for Needs Review) ──
                                        remediation_html = doc.get("remediation_guide_html", "")
                                        if remediation_html:
                                            with gr.Tab("Improvement Guide", key=k("remediation", i), elem_classes=["how-to-fix-tab"]):
                                                gr.HTML(value=remediation_html, key=k("remediation-guide", i))

                                        # ── Content Editor tab (Reading + Structure views, editable) ──
                                        with gr.Tab("Content Editor", key=k("sr", i)):
                                            sr_json = doc.get("sr_preview_json", "")
                                            _sr_pr = doc.get("pipeline_result", {})
                                            sr_pdf_path = _sr_pr.get("pdf_path", "")
                                            sr_struct_path = _sr_pr.get("structure_json_path", "")
                                            # elem_id must be a known, stable string so the JS bridge
                                            # can resolve host -> sink. Use the render-prefix-aware key.
                                            sr_host_id = f"sr-edit-host-{rid}-{i}"
                                            sr_sink_id = f"sr-edits-sink-{rid}-{i}"
                                            _ro_enabled = bool(
                                                _load_config().get("editor", {})
                                                .get("reading_order_enabled", True)
                                            )
                                            ro_order_sink_id = f"sr-order-sink-{rid}-{i}"
                                            # Both views render from the same enriched
                                            # structure JSON. The host owns the
                                            # data-sr-sink-id anchor so the JS bridge
                                            # resolves spans from either view to the
                                            # same hidden sink.
                                            _prose_html, _tree_html = _cached_editor_html(
                                                doc, sr_json, sr_pdf_path, sr_struct_path,
                                            )
                                            # Reading order leads the pills and is the default
                                            # view on open (initSrDefaultView auto-selects it);
                                            # when disabled it drops out and Reading is default.
                                            _ro_toggle = (
                                                f'<button type="button" data-view="order" '
                                                f'aria-pressed="false" '
                                                f'onclick="window._srSetView(this, \'order\')">'
                                                f'Reading order</button>'
                                            ) if _ro_enabled else ""
                                            _both_views_html = (
                                                f'<div class="sr-view-host" data-view="prose" '
                                                f'data-sr-sink-id="{escape(sr_sink_id)}">'
                                                f'<div class="sr-view-toolbar">'
                                                f'<div class="sr-view-toggle" role="tablist" '
                                                f'aria-label="Document view">'
                                                f'{_ro_toggle}'
                                                f'<button type="button" class="active" '
                                                f'data-view="prose" aria-pressed="true" '
                                                f'onclick="window._srSetView(this, \'prose\')">'
                                                f'Reading</button>'
                                                f'<button type="button" data-view="tree" '
                                                f'aria-pressed="false" '
                                                f'onclick="window._srSetView(this, \'tree\')">'
                                                f'Structure</button>'
                                                f'</div>'
                                                f'<p class="sr-view-note">'
                                                f'<svg class="sr-view-note-icon" viewBox="0 0 16 16" '
                                                f'width="15" height="15" aria-hidden="true" '
                                                f'focusable="false"><circle cx="8" cy="8" r="7" '
                                                f'fill="none" stroke="currentColor" '
                                                f'stroke-width="1.4"/><circle cx="8" cy="4.6" '
                                                f'r="0.95" fill="currentColor"/><rect x="7.25" '
                                                f'y="6.7" width="1.5" height="5" rx="0.6" '
                                                f'fill="currentColor"/></svg>'
                                                f'<span>Edits here change the screen-reader '
                                                f'structure only — they don’t modify the '
                                                f'PDF’s visual layout.</span>'
                                                f'</p>'
                                                f'</div>'
                                                f'{_prose_html}'
                                                f'{_tree_html}'
                                                f'</div>'
                                            )
                                            sr_save_btn_top = gr.Button(
                                                "Update PDF",
                                                variant="primary",
                                                size="lg",
                                                key=k("sr-save-btn-top", i),
                                            )
                                            # Shell wraps the prose/tree host and the Reading-order
                                            # host so one data-view (set by _srSetView) toggles
                                            # between views. The Reading-order host updates lazily
                                            # per page via ro_page_sink -> _render_reading_order_page,
                                            # without retriggering @gr.render.
                                            with gr.Column(elem_classes=["sr-editor-shell"], key=k("sr-shell", i)):
                                                gr.HTML(
                                                    value=_both_views_html,
                                                    elem_classes=["sr-edit-container"],
                                                    elem_id=sr_host_id,
                                                    key=k("sr-tree", i),
                                                )
                                                if _ro_enabled:
                                                    # Open on the worst-tau flagged page (not a
                                                    # hardcoded page 1, which is often not flagged);
                                                    # the JS switch reads data-ro-first-page.
                                                    _ro_first_page = _ro_initial_page(
                                                        _sr_pr.get("sr_quality") or {}
                                                    )
                                                    ro_page_host = gr.HTML(
                                                        value=(
                                                            f'<div class="vis-empty" '
                                                            f'data-ro-first-page="{_ro_first_page}">'
                                                            f'Loading page…</div>'
                                                        ),
                                                        elem_classes=["ro-page-host"],
                                                        key=k("ro-host", i),
                                                    )
                                                    ro_page_sink = gr.Textbox(
                                                        value="", visible=True, show_label=False, lines=1,
                                                        elem_classes=["sr-edit-sink", "ro-page-sink"],
                                                        key=k("ro-page-sink", i),
                                                    )
                                                    ro_order_sink = gr.Textbox(
                                                        value="", visible=True, show_label=False, lines=1,
                                                        elem_id=ro_order_sink_id,
                                                        elem_classes=["sr-edit-sink", "ro-order-sink"],
                                                        key=k("ro-order-sink", i),
                                                    )
                                                    ro_page_sink.change(
                                                        fn=_render_reading_order_page,
                                                        inputs=[session_state, ro_page_sink, gr.State(i),
                                                                gr.State(sr_sink_id), gr.State(ro_order_sink_id)],
                                                        outputs=[ro_page_host],
                                                    )
                                                    ro_order_sink.change(
                                                        fn=_on_sr_order_batch,
                                                        inputs=[caption_edits_state, gr.State(i), ro_order_sink],
                                                        outputs=[caption_edits_state],
                                                    )
                                            # JS -> Python sink. JS writes a JSON blob and
                                            # dispatches input+change events; Gradio's .change
                                            # fires the batch handler. NOTE: visible=False made
                                            # Gradio skip mounting the elem_id wrapper (confirmed
                                            # via 'sink root not found' debug log), so we mount
                                            # it visible and CSS-hide it via sr-edit-sink class.
                                            sr_sink = gr.Textbox(
                                                value="",
                                                visible=True,
                                                show_label=False,
                                                lines=1,
                                                elem_id=sr_sink_id,
                                                elem_classes=["sr-edit-sink"],
                                                key=k("sr-sink", i),
                                            )
                                            sr_sink.change(
                                                fn=_on_sr_edits_batch,
                                                inputs=[caption_edits_state, gr.State(i), sr_sink],
                                                outputs=[caption_edits_state],
                                            )
                                            sr_save_status = gr.HTML(
                                                value="",
                                                key=k("sr-save-status", i),
                                            )
                                            sr_save_btn = gr.Button(
                                                "Update PDF",
                                                variant="primary",
                                                size="lg",
                                                key=k("sr-save-btn", i),
                                            )

                                            def _wire_sr_save(trigger_btn, _top=sr_save_btn_top, _bot=sr_save_btn, _status=sr_save_status, _idx=i):
                                                trigger_btn.click(
                                                    fn=lambda: (
                                                        gr.update(interactive=False, value="Re-tagging..."),
                                                        gr.update(interactive=False, value="Re-tagging..."),
                                                    ),
                                                    inputs=[],
                                                    outputs=[_bot, _top],
                                                ).then(
                                                    fn=_update_pdf_handler,
                                                    inputs=[session_state, caption_edits_state, gr.State(_idx)],
                                                    outputs=[session_state, download_output, _status, caption_edits_state],
                                                ).then(
                                                    fn=_conditional_render_bump,
                                                    inputs=[session_state, render_version],
                                                    outputs=[render_version],
                                                ).then(
                                                    fn=lambda: (
                                                        gr.update(interactive=True, value="Update PDF"),
                                                        gr.update(interactive=True, value="Update PDF"),
                                                    ),
                                                    inputs=[],
                                                    outputs=[_bot, _top],
                                                )

                                            _wire_sr_save(sr_save_btn)
                                            _wire_sr_save(sr_save_btn_top)

                                        # ── Alt Text & Captions tab ──
                                        image_records = doc.get("image_records", [])
                                        table_captions = doc.get("table_captions", {})
                                        editable_images = [
                                            r for r in image_records
                                            if r.get("outcome") in ("generated", "harvested", "needs_manual_alt_text")
                                        ]
                                        caption_entries = table_captions.get("captions", {})
                                        # Build formula entries from VLM descriptions
                                        _fd = doc.get("formula_descriptions", {})
                                        _fd_descs = _fd.get("descriptions", {})
                                        formula_entries: list = []
                                        if _fd_descs:
                                            _pr = doc.get("pipeline_result", {})
                                            _sp = _pr.get("structure_json_path")
                                            _sel = []
                                            if _sp and Path(_sp).exists():
                                                try:
                                                    _sel = json.loads(Path(_sp).read_text(encoding="utf-8")).get("elements", [])
                                                except Exception:
                                                    pass
                                            for eid, dinfo in _fd_descs.items():
                                                fe = {"id": eid, "description": dinfo.get("description", ""), "latex": dinfo.get("latex")}
                                                for el in _sel:
                                                    if el.get("id") == eid:
                                                        fe["page_no"] = el.get("page_no")
                                                        fe["image_ref"] = el.get("image_ref")
                                                        break
                                                formula_entries.append(fe)
                                        has_captions = bool(editable_images or caption_entries or formula_entries)

                                        if has_captions:
                                            # Manual mode when no image received an AI or harvested description.
                                            _ai_disabled = bool(editable_images) and all(
                                                r.get("outcome") == "needs_manual_alt_text"
                                                for r in editable_images
                                            )
                                            _editor_title = (
                                                "Add Alt Text Manually"
                                                if _ai_disabled else "Review AI-Generated Captions"
                                            )
                                            _editor_hint = (
                                                "No AI description was available for these images. "
                                                "Add alt text for each image below, then click "
                                                "<strong>Update PDF</strong> to regenerate."
                                                if _ai_disabled else
                                                "To ensure accuracy and compliance, review and edit below, then click "
                                                "<strong>Update PDF</strong> to regenerate."
                                            )
                                            with gr.Tab("Alt Text & Captions", key=k("captions", i), elem_classes=["bold-tab"]):
                                                gr.HTML(
                                                    value=(
                                                        '<div style="font-family:-apple-system,BlinkMacSystemFont,'
                                                        "'Segoe UI',sans-serif;background:#eff6ff;border:1px solid #93c5fd;"
                                                        'border-radius:8px;padding:14px 18px;margin:12px 0;">'
                                                        '<h3 style="font-size:0.875rem;font-weight:700;color:#1e3a8a;'
                                                        f'margin:0 0 4px;">{_editor_title}</h3>'
                                                        '<div style="font-size:0.8125rem;color:#1e40af;">'
                                                        f'{_editor_hint}</div>'
                                                        '</div>'
                                                    ),
                                                    key=k("caption-prompt", i),
                                                )
                                                update_btn_top = gr.Button(
                                                    "Update PDF", variant="primary", size="lg",
                                                    key=k("update-btn-top", i),
                                                )

                                                # Image alt text — side-by-side: thumbnail | textbox
                                                for img_idx, rec in enumerate(editable_images):
                                                    src = rec.get("src", "")
                                                    alt_text = rec.get("alt_text", "")
                                                    page = rec.get("page_number")
                                                    file_path = rec.get("file_path")
                                                    page_label = f"Page {page}" if page else ""
                                                    with gr.Row(key=k("img-row", i, img_idx)):
                                                        with gr.Column(scale=1, min_width=140, key=k("img-thumb-col", i, img_idx)):
                                                            thumb_html = ""
                                                            if file_path:
                                                                data_uri = _file_url(file_path)
                                                                if data_uri:
                                                                    thumb_html = (
                                                                        f'<img src="{data_uri}" '
                                                                        f'loading="lazy" decoding="async" '
                                                                        f'alt="Image {img_idx + 1}" '
                                                                        f'style="max-width:160px;max-height:120px;'
                                                                        f'object-fit:contain;border:1px solid #d1d5db;'
                                                                        f'border-radius:4px;">'
                                                                        f'<div style="font-size:0.6875rem;color:#4b5563;'
                                                                        f'margin-top:2px;">{escape(page_label)}</div>'
                                                                    )
                                                            if thumb_html:
                                                                gr.HTML(value=thumb_html, key=k("img-thumb", i, img_idx))
                                                        with gr.Column(scale=3, key=k("img-text-col", i, img_idx)):
                                                            tb = gr.Textbox(
                                                                value=alt_text,
                                                                label=f"Image {img_idx + 1} alt text",
                                                                lines=3, max_lines=8, interactive=True,
                                                                key=k("img-alt", i, img_idx),
                                                                elem_classes=["alt-text-editor"],
                                                            )
                                                            tb.change(
                                                                fn=_on_caption_edit,
                                                                inputs=[caption_edits_state, gr.State(f"img:{i}:{img_idx}:{src}"), tb],
                                                                outputs=[caption_edits_state],
                                                            )

                                                # Table captions
                                                if caption_entries:
                                                    table_previews = doc.get("table_previews", {})
                                                    for tbl_key in sorted(caption_entries.keys()):
                                                        caption_text = caption_entries[tbl_key]
                                                        tbl_num = int(tbl_key.replace("table_", ""))
                                                        preview_html = table_previews.get(tbl_key, "")
                                                        if preview_html:
                                                            gr.HTML(
                                                                value=(
                                                                    f'<div style="font-size:0.75rem;font-weight:600;'
                                                                    f'color:{BRAND_PRIMARY};margin:8px 0 4px;">'
                                                                    f'Table {tbl_num + 1}</div>'
                                                                    f'<div style="border:1px solid #e5e7eb;'
                                                                    f'border-radius:6px;padding:8px;margin-bottom:4px;'
                                                                    f'background:#fafafa;overflow-x:auto;">'
                                                                    f'{preview_html}</div>'
                                                                ),
                                                                key=k("tbl-preview", i, tbl_key),
                                                            )
                                                        tb = gr.Textbox(
                                                            value=caption_text,
                                                            label=f"Table {tbl_num + 1} caption",
                                                            lines=3, max_lines=12, interactive=True,
                                                            key=k("tbl-cap", i, tbl_key),
                                                            elem_classes=["alt-text-editor"],
                                                        )
                                                        tb.change(
                                                            fn=_on_caption_edit,
                                                            inputs=[caption_edits_state, gr.State(f"tbl:{i}:{tbl_key}"), tb],
                                                            outputs=[caption_edits_state],
                                                        )

                                                # Formula descriptions
                                                if formula_entries:
                                                    gr.HTML(
                                                        value=(
                                                            f'<div style="font-size:0.8125rem;font-weight:600;'
                                                            f'color:{BRAND_PRIMARY};margin:14px 0 6px;">'
                                                            f'Formula Descriptions ({len(formula_entries)})</div>'
                                                        ),
                                                        key=k("formula-header", i),
                                                    )
                                                    for f_idx, fe in enumerate(formula_entries):
                                                        f_desc = fe.get("description", "")
                                                        f_page = fe.get("page_no")
                                                        f_img = fe.get("image_ref")
                                                        f_id = fe.get("id", "")
                                                        f_page_label = f"Page {f_page}" if f_page else ""
                                                        with gr.Row(key=k("formula-row", i, f_idx)):
                                                            with gr.Column(scale=1, min_width=200, key=k("formula-thumb-col", i, f_idx)):
                                                                f_thumb = ""
                                                                if f_img:
                                                                    f_data_uri = _file_url(f_img)
                                                                    if f_data_uri:
                                                                        _btn_id = f"formula-btn-{i}-{f_idx}"
                                                                        f_thumb = (
                                                                            f'<button type="button" id="{_btn_id}" '
                                                                            f'aria-label="Enlarge formula {f_idx + 1}" '
                                                                            f'onclick="window._showLightbox(this.querySelector(\'img\').src);" '
                                                                            f'style="background:none;border:none;padding:0;cursor:zoom-in;display:block;">'
                                                                            f'<img src="{f_data_uri}" '
                                                                            f'loading="lazy" decoding="async" '
                                                                            f'alt="Formula {f_idx + 1}" '
                                                                            f'style="max-width:240px;max-height:160px;'
                                                                            f'object-fit:contain;border:1px solid #d1d5db;'
                                                                            f'border-radius:4px;'
                                                                            f'background:#fafafa;padding:4px;">'
                                                                            f'</button>'
                                                                            f'<div style="font-size:0.6875rem;color:#4b5563;'
                                                                            f'margin-top:2px;">{escape(f_page_label)}'
                                                                            f' <span style="color:#1e3a8a;font-size:0.625rem;">'
                                                                            f'click to enlarge</span></div>'
                                                                        )
                                                                if f_thumb:
                                                                    gr.HTML(value=f_thumb, key=k("formula-thumb", i, f_idx))
                                                            with gr.Column(scale=3, key=k("formula-text-col", i, f_idx)):
                                                                ftb = gr.Textbox(
                                                                    value=f_desc,
                                                                    label=f"Formula {f_idx + 1} description",
                                                                    lines=3, max_lines=8, interactive=True,
                                                                    key=k("formula-alt", i, f_idx),
                                                                    elem_classes=["alt-text-editor"],
                                                                )
                                                                ftb.change(
                                                                    fn=_on_caption_edit,
                                                                    inputs=[caption_edits_state, gr.State(f"formula:{i}:{f_idx}:{f_id}"), ftb],
                                                                    outputs=[caption_edits_state],
                                                                )

                                                update_btn = gr.Button(
                                                    "Update PDF", variant="primary", size="lg",
                                                    key=k("update-btn", i),
                                                )
                                                update_status = gr.HTML(value="", key=k("update-status", i))

                                                def _wire_update(trigger_btn, _top=update_btn_top, _bot=update_btn, _status=update_status, _idx=i):
                                                    trigger_btn.click(
                                                        fn=lambda: (
                                                            gr.update(interactive=False, value="Updating..."),
                                                            gr.update(interactive=False, value="Updating..."),
                                                        ),
                                                        inputs=[],
                                                        outputs=[_bot, _top],
                                                    ).then(
                                                        fn=_update_pdf_handler,
                                                        inputs=[session_state, caption_edits_state, gr.State(_idx)],
                                                        outputs=[session_state, download_output, _status, caption_edits_state],
                                                    ).then(
                                                        fn=_conditional_render_bump,
                                                        inputs=[session_state, render_version],
                                                        outputs=[render_version],
                                                    ).then(
                                                        fn=lambda: (
                                                            gr.update(interactive=True, value="Update PDF"),
                                                            gr.update(interactive=True, value="Update PDF"),
                                                        ),
                                                        inputs=[],
                                                        outputs=[_bot, _top],
                                                    )

                                                _wire_update(update_btn)
                                                _wire_update(update_btn_top)

                                        # ── WCAG Report tab ──
                                        with gr.Tab("WCAG Report", key=k("wcag", i)):
                                            wcag_html = doc.get("wcag_full_report_html", "")
                                            if wcag_html:
                                                # Render as iframe to isolate report CSS
                                                safe_html = wcag_html.replace("&", "&amp;").replace('"', "&quot;")
                                                gr.HTML(
                                                    value=(
                                                        f'<iframe srcdoc="{safe_html}" '
                                                        f'style="width:100%;min-height:600px;height:80vh;'
                                                        f'border:1px solid #e5e7eb;border-radius:8px;" '
                                                        f'title="WCAG Compliance Report"></iframe>'
                                                    ),
                                                    key=k("wcag-report", i),
                                                )
                                            else:
                                                gr.HTML(
                                                    value='<div style="padding:20px;color:#4b5563;">WCAG report not available. Try re-processing the document.</div>',
                                                    key=k("wcag-report", i),
                                                )

                                        # ── Changes tab ──
                                        changes_html = doc.get("changes_report_html", "")
                                        if changes_html:
                                            with gr.Tab("Document Updates", key=k("changes-tab", i)):
                                                gr.HTML(value=changes_html, key=k("changes", i))

    # Disable controls and clear old results so tabs disappear immediately
    def _clear_before_run(session):
        """Clear previous results and disable inputs while processing starts."""
        session["results"] = []
        session["_render_id"] = session.get("_render_id", 0) + 1
        # Activate log buffer immediately so _poll_log shows a waiting message
        # before process_batch starts (it may be queued by Gradio).
        if not session.get("session_id"):
            session["session_id"] = uuid.uuid4().hex
        sid = session["session_id"]
        log_buf = _log_router.get_or_create_buffer(sid)
        log_buf.clear()
        log_buf.active = True
        log_buf.start_time = time.time()
        return (
            gr.update(interactive=False),  # pdf_input
            gr.update(interactive=False),  # run_btn
            gr.update(value=None),  # download_output
            gr.update(value=None),  # zip_output
            gr.update(visible=True),  # landing_html — show during processing
            session,  # session_state — cleared results
            time.time(),  # render_version — triggers empty render
        )

    run_btn.click(
        fn=_clear_before_run,
        inputs=[session_state],
        outputs=[pdf_input, run_btn, download_output, zip_output, landing_html,
                 session_state, render_version],
    ).then(
        fn=process_batch,
        inputs=[pdf_input, opt_include_html, opt_include_html_pdf, opt_include_report, session_state],
        outputs=[download_output, zip_output, landing_html, session_state],
    ).then(
        fn=lambda: time.time(),
        inputs=[],
        outputs=[render_version],
    ).then(
        fn=lambda: (gr.update(interactive=True), gr.update(interactive=True)),
        inputs=[],
        outputs=[pdf_input, run_btn],
    ).then(
        fn=lambda: _get_queue_status_html(),
        inputs=[],
        outputs=[queue_status_html],
    )

    # Re-filter downloads when any Download Options checkbox changes
    _dl_option_inputs = [session_state, opt_include_html, opt_include_html_pdf, opt_include_report]
    _dl_option_outputs = [session_state, download_output, zip_output]
    for _cb in (opt_include_html, opt_include_report):  # opt_include_html_pdf disabled
        _cb.change(
            fn=_refilter_downloads,
            inputs=_dl_option_inputs,
            outputs=_dl_option_outputs,
        )

    # Periodic queue status update (every 10 seconds)
    timer = gr.Timer(value=10)
    timer.tick(fn=lambda: _get_queue_status_html(), outputs=[queue_status_html])

    # Live log overlay (polls every 1 second during processing)
    log_timer = gr.Timer(value=1)
    log_timer.tick(fn=_poll_log, inputs=[session_state], outputs=[log_display])

def _cleanup_temp_dirs(max_age_seconds: int = 14400, interval_seconds: int = 600) -> None:
    """Periodically delete stale session directories.

    Runs in a daemon thread. Removes directories whose modification time
    is older than *max_age_seconds*. Defaults: delete after 4 hours,
    check every 10 minutes.

    Cleans two locations:
    - /tmp/pdf_a11y_* — sync-mode temp directories
    - $READREADY_SHARED_DIR/session_* (default /app/output) — Celery-mode shared volume directories
    """
    tmp_root = Path(tempfile.gettempdir())
    shared_root = _shared_output_root(os.environ)
    while True:
        time.sleep(interval_seconds)
        try:
            now = time.time()
            # Clean sync-mode temp dirs
            for d in tmp_root.iterdir():
                if not d.is_dir() or not d.name.startswith("pdf_a11y_"):
                    continue
                age = now - d.stat().st_mtime
                if age > max_age_seconds:
                    shutil.rmtree(d, ignore_errors=True)
                    logger.info("Cleaned up stale temp dir: %s (age %.0fs)", d.name, age)
            # Clean Celery-mode shared volume dirs
            if shared_root.is_dir():
                for d in shared_root.iterdir():
                    if not d.is_dir() or not d.name.startswith("session_"):
                        continue
                    age = now - d.stat().st_mtime
                    if age > max_age_seconds:
                        shutil.rmtree(d, ignore_errors=True)
                        logger.info("Cleaned up stale session dir: %s (age %.0fs)", d.name, age)
            # Prune stale log buffers (same age threshold as temp dirs)
            pruned = _log_router.prune_stale_buffers(max_age_seconds)
            if pruned:
                logger.info("Pruned %d stale log buffer(s)", pruned)
        except Exception as e:
            logger.debug("Temp cleanup error: %s", e)


def _launch_settings(env) -> dict:
    """Bind address and optional login for the web UI, from the environment.

    Defaults to loopback. Docker sets READREADY_HOST=0.0.0.0 behind Caddy.
    """
    secret = env.get("READREADY_SECRET", "")
    server_name = env.get("READREADY_HOST", "127.0.0.1")
    if not secret and server_name not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "Web UI is bound to %s with no login: anyone who can reach it can "
            "upload and process documents. Set READREADY_SECRET to require a login.",
            server_name,
        )
    return {
        "server_name": server_name,
        "auth": (env.get("READREADY_USER") or "readready", secret) if secret else None,
    }


if __name__ == "__main__":
    _config = _load_config()
    _max_file_mb = _config.get("ui", {}).get("max_file_size_mb", 25)

    # Start background cleanup of stale temp directories
    _cleanup_thread = threading.Thread(target=_cleanup_temp_dirs, daemon=True)
    _cleanup_thread.start()

    demo.queue(default_concurrency_limit=6, max_size=50)

    _settings = _launch_settings(os.environ)
    demo.launch(
        server_name=_settings["server_name"],
        auth=_settings["auth"],
        server_port=7860,
        theme=app_theme,
        css=APP_CSS,
        js=APP_JS.replace("__RR_PAGE_TITLE__", _PAGE_TITLE_JS),
        max_file_size=f"{_max_file_mb}MB",
        head='<meta name="color-scheme" content="light only">',
        show_error=False,
        prevent_thread_lock=True,
    )
    demo.block_thread()
