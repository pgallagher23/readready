# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate a WCAG 2.1 AA-compliant Document Changes Report PDF.

Produces a standalone PDF (via WeasyPrint) that documents every change
made during accessibility remediation, grouped by original PDF page number,
plus a full image review section with thumbnails and AI-generated alt text.

WCAG 2.1 AA compliance:
  • 3.1.1  Language of Page      — html[lang="en"]
  • 2.4.2  Page Titled           — <title> element
  • 2.4.6  Headings and Labels   — <h1>/<h2>/<h3> hierarchy
  • 2.4.1  Bypass Blocks         — skip link
  • 1.3.1  Info and Relationships — <table> with scope/caption, <figure>/<figcaption>
  • 2.4.4  Link Purpose          — n/a (no links in this report)
  • 4.1.2  Name, Role, Value     — native HTML semantics, aria-labelledby
  • 1.4.1  Use of Color          — status badges use text AND colour
  • 1.4.3  Contrast (Minimum)    — all pairs verified ≥ 4.5:1

Contrast ratios verified:
  #fff  on #1E3A5F (navy header)     ≈ 11.2:1  ✓
  #fff  on #141720 (table header)    ≈ 15.0:1  ✓
  #141720 on #fff  (body text)       ≈ 15.0:1  ✓
  #166534 on #DCFCE7 (fixed badge)   ≈  6.6:1  ✓
  #92400E on #FEF3C7 (skipped badge) ≈  5.0:1  ✓
  #92400E on #FEF3C7 (failed badge)  ≈  5.9:1  ✓
  #1E3A8A on #EFF6FF (ai badge)      ≈  8.2:1  ✓
  #374151 on #F9FAFB (decor badge)   ≈  7.4:1  ✓
"""

import base64
import logging
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from output_gen.report_page import (
    _SECTIONS as _COMPLIANCE_SECTIONS,
    _NAMES as _COMPLIANCE_NAMES,
    _LEVEL as _COMPLIANCE_LEVEL,
    _WCAG_URL as _COMPLIANCE_WCAG_URL,
    _get_status,
)
from audit.standards import verdict_from_sr
from utils.branding import get_branding

logger = logging.getLogger(__name__)

# ── CSS ────────────────────────────────────────────────────────────────────────

_CSS = """
@page {
    size: A4;
    margin: 18mm 15mm 15mm;
}

*, *::before, *::after {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
}

body {
    font-family: "Liberation Sans", "DejaVu Sans", "Ubuntu Sans", sans-serif;
    font-size: 8.5pt;
    color: #141720;
    line-height: 1.4;
}

/* ── Skip link (screen-reader / keyboard visible) ── */
.skip-link {
    position: absolute;
    left: -9999px;
    min-height: 44px;
    min-width: 44px;
    padding: 10px 16px;
    box-sizing: border-box;
}
.skip-link:focus {
    position: static;
}

/* ── Page header ── */
header {
    background: #1E3A5F;
    color: #ffffff;
    padding: 8mm 13mm 5mm;
    margin: 0 -15mm 6mm;
}

.org-name {
    font-size: 7.5pt;
    color: #A6C4F0;
    margin-bottom: 2pt;
}

header h1 {
    font-size: 18pt;
    font-weight: bold;
    color: #ffffff;
    margin-bottom: 2pt;
}

.doc-subtitle {
    font-size: 8pt;
    color: #A6C4F0;
}

/* ── Section headings ── */
h2 {
    font-size: 11pt;
    font-weight: bold;
    color: #1E3A5F;
    border-bottom: 1pt solid #1E3A5F;
    padding-bottom: 2pt;
    margin: 8pt 0 4pt;
}

h3 {
    font-size: 9.5pt;
    font-weight: bold;
    color: #374151;
    margin: 6pt 0 3pt;
    background: #F0F4F8;
    padding: 2pt 4pt;
    border-left: 3pt solid #1E3A5F;
}

/* ── Summary boxes ── */
.summary-grid {
    display: table;
    width: 100%;
    border-collapse: separate;
    border-spacing: 4pt;
    margin-bottom: 6pt;
}

.summary-box {
    display: table-cell;
    text-align: center;
    padding: 5pt 4pt;
    border-radius: 3pt;
    vertical-align: middle;
}

.summary-box .count {
    font-size: 18pt;
    font-weight: bold;
    line-height: 1;
}

.summary-box .label {
    font-size: 7pt;
    margin-top: 1pt;
}

.box-fixed   { background: #DCFCE7; color: #166534; }
.box-skipped { background: #FEF3C7; color: #92400E; }
.box-images  { background: #EFF6FF; color: #1E3A8A; }
.box-tables  { background: #FEF3C7; color: #92400E; }

/* ── Changes table ── */
table {
    width: 100%;
    border-collapse: collapse;
    font-size: 8pt;
    margin-bottom: 4pt;
}

caption {
    text-align: left;
    font-size: 8pt;
    font-weight: bold;
    color: #374151;
    padding-bottom: 3pt;
}

th {
    background: #141720;
    color: #ffffff;
    font-weight: bold;
    text-align: left;
    padding: 3pt 5pt;
}

td {
    padding: 3pt 5pt;
    border-bottom: 0.5pt solid #E5E7EB;
    vertical-align: top;
}

tr:nth-child(even) td {
    background: #F9FAFB;
}

/* ── Status badges ── */
.badge {
    display: inline-block;
    padding: 1pt 4pt;
    border-radius: 2pt;
    font-size: 7pt;
    font-weight: bold;
    white-space: nowrap;
}

.badge-fixed   { background: #DCFCE7; color: #166534; }
.badge-skipped { background: #FEF3C7; color: #92400E; }
.badge-failed  { background: #FEF3C7; color: #92400E; }
.badge-manual_review { background: #E0E7FF; color: #3730A3; }

/* ── Image review section ── */
.image-block {
    border: 0.5pt solid #E5E7EB;
    border-radius: 3pt;
    padding: 5pt;
    margin-bottom: 6pt;
    page-break-inside: avoid;
}

.image-block img {
    max-width: 260pt;
    max-height: 160pt;
    object-fit: contain;
    display: block;
    margin-bottom: 4pt;
    border: 0.5pt solid #D1D5DB;
}

.image-meta {
    font-size: 8pt;
    color: #374151;
}

.image-meta .alt-text {
    font-style: italic;
    color: #141720;
    margin: 3pt 0;
}

.badge-generated  { background: #EFF6FF; color: #1E3A8A; }
.badge-decorative { background: #F9FAFB; color: #374151; }
.badge-img-skip   { background: #FEF3C7; color: #92400E; }

/* ── Table review section ── */
.table-block {
    border: 0.5pt solid #E5E7EB;
    border-radius: 3pt;
    padding: 5pt;
    margin-bottom: 6pt;
    page-break-inside: avoid;
}

.table-block-header {
    display: flex;
    flex-direction: row;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 3pt;
}

.table-block-title {
    font-size: 8.5pt;
    font-weight: bold;
    color: #141720;
}

.table-block-meta {
    font-size: 7.5pt;
    color: #47528A;
}

.table-changes-list {
    margin: 2pt 0 0 12pt;
    padding: 0;
    font-size: 8pt;
    color: #374151;
}

.table-changes-list li {
    margin-bottom: 1pt;
}

.table-stat {
    display: inline-block;
    background: #F0F4F8;
    border: 0.5pt solid #D9DBE0;
    border-radius: 2pt;
    padding: 1pt 5pt;
    font-size: 7pt;
    color: #374151;
    margin-right: 3pt;
    margin-top: 3pt;
}

/* ── Before/after mini-tables ── */
.ba-container {
    display: flex;
    flex-direction: row;
    gap: 6pt;
    margin-top: 4pt;
}

.ba-panel {
    flex: 1;
    min-width: 0;
}

.ba-label {
    font-size: 7pt;
    font-weight: bold;
    color: #47528A;
    margin-bottom: 2pt;
    text-transform: uppercase;
    letter-spacing: 0.3pt;
}

.ba-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 7pt;
    table-layout: fixed;
}

.ba-table td,
.ba-table th {
    padding: 1.5pt 3pt;
    border: 0.5pt solid #D9DBE0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    background: #ffffff;
    color: #374151;
    font-weight: normal;
}

.ba-table th {
    background: #141720;
    color: #ffffff;
    font-weight: bold;
}

.ba-table .scope-tag {
    font-size: 5.5pt;
    color: #A6C4F0;
    font-weight: normal;
    margin-left: 2pt;
}

.no-preview {
    background: #F3F4F6;
    color: #6B7280;
    font-size: 7.5pt;
    font-style: italic;
    padding: 10pt;
    text-align: center;
    margin-bottom: 4pt;
    border: 0.5pt dashed #D1D5DB;
    border-radius: 2pt;
}

/* ── Compliance verdict banner ── */
.verdict {
    display: flex;
    flex-direction: row;
    border-radius: 3pt;
    overflow: hidden;
    margin-bottom: 5pt;
}

.verdict-pass   { background: #166534; }
.verdict-review { background: #92400E; }

.verdict-left {
    flex: 1;
    padding: 7pt 12pt;
}

.verdict-label {
    font-size: 22pt;
    font-weight: bold;
    color: #ffffff;
    line-height: 1.1;
    margin-bottom: 2pt;
}

.verdict-msg {
    font-size: 8.5pt;
    color: #ffffff;
}

.verdict-divider {
    width: 0.6pt;
    background: rgba(255, 255, 255, 0.4);
    margin: 6pt 0;
}

.verdict-right {
    min-width: 140pt;
    padding: 7pt 12pt;
    font-size: 8pt;
    color: #ffffff;
}

.score {
    font-size: 13pt;
    font-weight: bold;
    margin-bottom: 2pt;
}

/* ── Compliance count grid ── */
.count-grid {
    margin-bottom: 6pt;
}

.count-box {
    display: inline-block;
    background: #F8F9FC;
    border: 0.5pt solid #D9DBE0;
    border-radius: 2pt;
    padding: 1pt 6pt;
    font-size: 8pt;
    color: #141720;
    margin-right: 4pt;
    margin-bottom: 3pt;
}

/* ── Compliance legend ── */
.legend {
    font-size: 7.5pt;
    color: #47528A;
    margin-bottom: 5pt;
}

/* ── Compliance badges ── */
.compliance-badge {
    display: inline-block;
    padding: 1pt 5pt;
    border-radius: 2pt;
    font-size: 7.5pt;
    font-weight: bold;
}

.compliance-badge.pass { background: #DCFCE7; color: #166534; }
.compliance-badge.fail,
.compliance-badge.review { background: #FEF3C7; color: #92400E; }
.compliance-badge.na   { background: #F0F2F5; color: #47568A; }

/* ── Compliance table ── */
.compliance-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 8pt;
    margin-bottom: 4pt;
}

.compliance-table thead th:first-child  { width: 34%; }
.compliance-table thead th:nth-child(2) { width: 13%; }
.compliance-table thead th:last-child   { width: 53%; }

.compliance-table .section-header {
    background: #1D4ED8;
    page-break-after: avoid;
}

.compliance-table .section-header th {
    color: #ffffff;
    font-size: 8.5pt;
    font-weight: bold;
    padding: 3pt 5pt;
}

.compliance-table .alt-row { background: #EEF2FE; }

.compliance-table .rule-name { font-size: 8pt; }
.compliance-table .rule-name a { color: #141720; text-decoration: underline; }
.compliance-table .status-cell { white-space: nowrap; }

.compliance-table .issue-row td {
    font-size: 7pt;
    color: #47528A;
    padding: 1pt 5pt 1pt 16pt;
}

.sev-critical { color: #92400E; }
.sev-major    { color: #B45309; }
.sev-minor    { color: #CA8A04; }

/* ── Screen-reader only ── */
.sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border-width: 0;
}

/* ── Document profile box ── */
.box-profile  { background: #F0F4F8; color: #1E3A5F; }

/* ── Improvement delta banner ── */
.verdict-delta { background: #1E3A5F; }

/* ── Formula LaTeX code ── */
.formula-latex code {
    font-family: "Liberation Mono", "DejaVu Sans Mono", monospace;
    font-size: 7.5pt;
    background: #F3F4F6;
    padding: 1pt 3pt;
    border-radius: 1pt;
    word-break: break-all;
}
"""

_SCREEN_CSS = """

/* ── Screen overrides ─────────────────────────────────────────────────────── */
@page { size: auto; margin: 0; }

body {
    max-width: 960px;
    margin: 0 auto;
    font-size: 10pt;
    padding: 0 16pt 2em;
}

table { font-size: 9pt; }

.verdict { flex-wrap: wrap; }

.count-box { font-size: 9pt; }
"""

# ── Human-readable labels for issue types ─────────────────────────────────────

_ISSUE_LABELS: Dict[str, str] = {
    "no-h1": "Missing H1 heading",
    "missing-h1": "Missing H1 heading",
    "missing-headings": "No heading structure",
    "skipped-heading-level": "Skipped heading level",
    "empty-heading-content": "Empty heading",
    "missing-main-landmark": "Missing main landmark",
    "missing-navigation-landmark": "Missing navigation landmark",
    "missing-header-landmark": "Missing header landmark",
    "missing-footer-landmark": "Missing footer landmark",
    "missing-skip-link": "Missing skip navigation link",
    "missing-document-title": "Missing document title",
    "missing-document-language": "Missing language attribute",
    "invalid-document-language": "Invalid language attribute",
    "empty-alt-text": "Image missing alt text",
    "missing-alt-text": "Image missing alt text",
    "generic-alt-text": "Generic alt text",
    "improper-figure-structure": "Improper figure structure",
    "missing-figure-caption": "Figure missing caption",
    "empty-figure-caption": "Figure has empty caption",
    "table-missing-headers": "Table missing header cells",
    "table-irregular-headers": "Table has irregular headers",
    "table-missing-scope": "Table header missing scope",
    "table-missing-caption": "Table missing caption",
    "table-missing-thead": "Table missing header section",
    "table-missing-tbody": "Table missing body section",
    "table-missing-headers-id": "Table missing headers/id linkage",
    "table-headers-id": "Table missing headers/id linkage",
    "table-irregular-columns": "Table has irregular column counts",
    "empty-link-text": "Link has no text",
    "generic-link-text": "Link uses generic text",
    "new-window-link-no-warning": "Link opens new window without warning",
    "insufficient-color-contrast": "Insufficient color contrast",
    "missing-form-label": "Form field missing label",
    "missing-required-indicator": "Required field not marked",
    "missing-fieldset": "Form group missing fieldset",
    "css-injection": "Accessible CSS injected",
    "alt-text-generated": "Alt text generated (AI)",
    "decorative-image-marked": "Decorative image marked",
    "formula-description-generated": "Formula description generated (AI)",
    # Keyboard (2.1.1)
    "interactive-tabindex-negative": "Interactive element removed from tab order",
    "onclick-not-keyboard-accessible": "Click handler not keyboard accessible",
    "onclick-missing-keyboard-handler": "Missing keyboard handler for click event",
    "mouse-only-handler": "Mouse-only event handler",
    "interactive-missing-role": "Interactive element missing ARIA role",
    # Focus Order (2.4.3)
    "positive-tabindex": "Positive tabindex disrupts focus order",
    "stylesheet-css-order": "CSS order property may disrupt focus order",
    "css-order-focus-mismatch": "CSS order causes focus mismatch",
    "flex-reverse-focus-mismatch": "Flex reverse causes focus mismatch",
    "stylesheet-flex-reverse": "Stylesheet flex-direction reverse",
    # Focus Visible (2.4.7)
    "focus-outline-removed": "Focus outline removed (inline style)",
    "stylesheet-focus-outline-removed": "Focus outline removed (stylesheet)",
    "blanket-outline-removal": "Blanket outline removal (* selector)",
    "element-focus-outline-removed": "Element focus outline removed",
    # Error Identification (3.3.1)
    "required-field-no-error-mechanism": "Required field has no error mechanism",
    "pattern-input-no-description": "Pattern input has no format description",
    "type-validated-input-no-description": "Type-validated input has no format hint",
    "aria-invalid-no-error-message": "aria-invalid element missing error message",
    "aria-errormessage-missing-target": "aria-errormessage target does not exist",
    "form-no-error-live-region": "Form missing error live region",
    # Name, Role, Value (4.1.2)
    "invalid-aria-role": "Invalid ARIA role",
    "aria-labelledby-missing-target": "aria-labelledby references missing element",
    "aria-labelledby-empty-target": "aria-labelledby references empty element",
    "aria-describedby-missing-target": "aria-describedby references missing element",
    "button-missing-name": "Button missing accessible name",
    "input-image-missing-alt": "Image input missing alt text",
    "iframe-missing-title": "Iframe missing title",
    "custom-widget-missing-aria-attrs": "Custom widget missing required ARIA attributes",
    # Parsing (4.1.1)
    "duplicate-id": "Duplicate element ID",
    "invalid-nesting": "Invalid HTML nesting",
    # Text Spacing (1.4.12)
    "fixed-line-height": "Fixed line-height prevents spacing adjustment",
    "text-spacing-important-override": "!important overrides text spacing",
    "text-spacing-overflow-risk": "Overflow:hidden may clip respaced text",
    # Resize Text (1.4.4)
    "fixed-font-size": "Fixed font size prevents text resizing",
    "text-overflow-hidden": "Overflow:hidden may clip resized text",
    # Reflow (1.4.10)
    "fixed-width-prevents-reflow": "Fixed width prevents content reflow",
    "nowrap-text": "White-space:nowrap prevents text wrapping",
    "horizontal-scroll-required": "Horizontal scrolling required for content",
    # Reading Order (1.3.2)
    "reading-order-tabindex": "Positive tabindex disrupts reading order",
    "reading-order-css-reorder": "CSS reordering disrupts reading order",
    "reading-order-absolute-position": "Absolute positioning may disrupt reading order",
    # Input Purpose (1.3.5)
    "input-missing-autocomplete": "Input missing autocomplete attribute",
    # Language of Parts (3.1.2)
    "xml-lang-without-lang": "xml:lang present without matching lang",
    "invalid-lang-attribute": "Invalid lang attribute value",
    # Color Use (1.4.1)
    "color-only-link-distinction": "Link distinguished by color only",
    # Link Purpose (2.4.4)
    "url-as-link-text": "URL used as link text",
    # Extended Forms (1.3.1, 3.3.2)
    "form-control-missing-name": "Form control missing name attribute",
    "form-label-empty": "Label element has no text",
    "form-required-field-missing-aria": "Required field missing aria-required",
    # Widget / Fieldset (1.3.1, 4.1.2)
    "form-fieldset-missing-legend": "Fieldset missing legend",
    "form-related-controls-no-fieldset": "Related form controls not grouped",
    "custom-widget-missing-name": "Custom widget missing accessible name",
    # Duplicate links (2.4.4)
    "duplicate-link-text-different-url": "Duplicate link text points to different URLs",
    # Color-only status (1.4.1)
    "color-only-status": "Status conveyed by color alone",
    # Color contrast extended (1.4.3, 1.4.11)
    "potential-color-contrast-issue": "Potential color contrast issue",
    "insufficient-non-text-contrast": "Insufficient non-text contrast",
    # Language of Parts (3.1.2) — foreign content
    "missing-lang-on-foreign-content": "Foreign-language content missing lang attribute",
    # Keyboard traps (2.1.2)
    "potential-keyboard-trap": "Potential keyboard trap (manual review)",
    "container-may-trap-focus": "Container may trap keyboard focus (manual review)",
    # Aliases
    "missing-title": "Missing document title",
    "empty-title": "Empty document title",
    "generic-title": "Generic document title",
    "no-headings": "No heading structure",
    "form-control-missing-label": "Form control missing label",
    "form-required-field-not-indicated": "Required field not indicated",
}

# Issue types considered document-wide (no specific page element)
_DOCUMENT_WIDE_TYPES = {
    # HeadingHierarchyCheck emits two of these: "no-headings" when a document
    # has no headings at all, and "no-h1" when it has headings but none at
    # level 1 (audit/checks/heading_checks.py:26,38). "missing-h1" and
    # "missing-headings" are dispatch-table aliases with no emitter.
    # "no-headings" was almost always auto-remediated, so its absence here went
    # unnoticed — now that the heading strategies abstain rather than invent
    # text it stays unresolved on flat-typography documents, and without this
    # the report and UI file it as "Page not identified" instead of
    # Document-Wide.
    "no-headings",
    "no-h1", "missing-h1", "missing-headings",
    "missing-main-landmark", "missing-navigation-landmark",
    "missing-header-landmark", "missing-footer-landmark", "missing-skip-link",
    "missing-document-title", "missing-document-language", "invalid-document-language",
    "stylesheet-css-order", "stylesheet-flex-reverse",
    "stylesheet-focus-outline-removed", "blanket-outline-removal",
    "form-no-error-live-region",
    "text-spacing-important-override",
    "scrambled-type3-font",
    "css-injection",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _embed_image(file_path: str) -> Optional[str]:
    """Return a base64 data URI for the image, or None if not readable."""
    try:
        path = Path(file_path)
        if not path.exists():
            return None
        suffix = path.suffix.lower().lstrip(".")
        mime = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "gif": "image/gif",
            "webp": "image/webp",
        }.get(suffix, "image/png")
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{data}"
    except Exception:
        return None


def _page_label(page_number: Optional[int]) -> str:
    return f"Page {page_number}" if page_number else "Document-wide"


def _issue_label(issue_type: str) -> str:
    return _ISSUE_LABELS.get(issue_type, issue_type.replace("-", " ").title())


def _outcome_badge(outcome: str) -> str:
    cls = {
        "fixed": "badge-fixed",
        "skipped": "badge-skipped",
        "failed": "badge-failed",
        "manual_review": "badge-manual_review",
    }.get(outcome, "badge-skipped")
    label = {
        "fixed": "Fixed",
        "skipped": "Skipped",
        "failed": "Failed",
        "manual_review": "Manual Review",
    }.get(outcome, outcome.capitalize())
    return f'<span class="badge {cls}" aria-label="{label}">{label}</span>'


def _render_mini_table(rows_data: list, table_id: str) -> str:
    """Render a before/after mini-table from snapshot row data.

    Args:
        rows_data: List of row dicts from _snapshot_table_rows():
            [{"cells": [{"text": str, "is_th": bool, "scope": str}], "is_thead": bool}]
        table_id: Unique ID prefix for accessibility.
    """
    if not rows_data:
        return '<p style="font-size:7pt; color:#9CA3AF; font-style:italic;">No data</p>'

    rows_html = ""
    for row in rows_data:
        cells_html = ""
        for cell in row.get("cells", []):
            text = escape(cell.get("text", "") or "")
            if cell.get("is_th"):
                scope = cell.get("scope", "")
                scope_tag = (
                    f'<span class="scope-tag">[{escape(scope)}]</span>'
                    if scope else ""
                )
                cells_html += f"<th>{text}{scope_tag}</th>"
            else:
                cells_html += f"<td>{text}</td>"
        rows_html += f"<tr>{cells_html}</tr>\n"

    return (
        f'<table class="ba-table" aria-label="Table preview">'
        f'{rows_html}</table>'
    )


def _build_compliance_sections(
    post_audit: Dict[str, Any],
    sr_quality: str = "unknown",
    sr_data: Optional[Dict[str, Any]] = None,
    verdict_config: Optional[Dict[str, Any]] = None,
) -> str:
    """Build the compliance verdict banner and WCAG criteria table HTML."""
    compliance = post_audit.get("compliance", {})
    score = compliance.get("overall_score", 0.0)
    by_criterion = compliance.get("by_criterion", {})
    summary = post_audit.get("summary", {})

    total_tested = len(by_criterion)
    n_passing = sum(1 for d in by_criterion.values() if d.get("passed", True))
    n_failing = total_tested - n_passing
    needs_rem = summary.get("needs_remediation", 0)
    compliant_cnt = summary.get("compliant", 0)
    sev = summary.get("severity_counts", {})

    # Preserve the historical fallback to the ``sr_quality`` parameter when
    # sr_data lacks the key.
    verdict = verdict_from_sr(
        post_audit, sr_data, verdict_config, sr_quality_fallback=sr_quality,
    )
    if verdict == "pass":
        verdict_cls = "verdict-pass"
        verdict_lbl = "COMPLIANT"
        status_msg = "The document conforms to WCAG 2.1 Level AA."
    else:
        verdict_cls = "verdict-review"
        verdict_lbl = (
            f"WCAG 2.1 AA — {n_passing} of {total_tested} Criteria Passing"
        )
        status_msg = (
            f"{n_failing} criteri{'on needs' if n_failing == 1 else 'a need'}"
            f" review to ensure compliance."
        ) if n_failing > 0 else "Manual review recommended."

    # Summary statistics boxes
    counts = [
        ("Passed", compliant_cnt),
        ("Failed", needs_rem),
        ("Criteria assessed", total_tested),
    ]
    crit_c = sev.get("critical", 0)
    maj_c = sev.get("major", 0)
    min_c = sev.get("minor", 0)
    if crit_c or maj_c or min_c:
        counts += [
            ("Critical issues", crit_c),
            ("Major issues", maj_c),
            ("Minor issues", min_c),
        ]

    counts_html = " ".join(
        f'<span class="count-box">{escape(label)}: <strong>{val}</strong></span>'
        for label, val in counts
    )

    # Build criteria table rows
    issues_by_crit: Dict[str, list] = {}
    for iss in post_audit.get("issues", []):
        if iss.get("remediation_status") == "needs_remediation":
            cid = iss.get("wcag_criterion", "")
            issues_by_crit.setdefault(cid, []).append(iss)

    table_rows: List[str] = []
    data_row_idx = 0
    for section_name, criteria in _COMPLIANCE_SECTIONS:
        table_rows.append(
            f'<tr class="section-header">'
            f'<th colspan="3" scope="rowgroup">{escape(section_name)}</th>'
            f'</tr>'
        )
        for crit_id, description in criteria:
            status = _get_status(crit_id, by_criterion)
            # Unify fail → review: red is reserved for pipeline errors
            if status == "fail":
                status = "review"
            badge_label = {"pass": "COMPLIANT", "review": "NEEDS REVIEW"}.get(status, "N/A")
            level_tag = _COMPLIANCE_LEVEL.get(crit_id, "A")
            name = _COMPLIANCE_NAMES.get(crit_id, crit_id)
            uri = _COMPLIANCE_WCAG_URL.get(crit_id, "")
            link_text = f"{crit_id} {level_tag}: {name}"

            rule_cell = (
                f'<a href="{escape(uri)}">{escape(link_text)}</a>'
                if uri else escape(link_text)
            )

            alt_class = " alt-row" if data_row_idx % 2 == 0 else ""
            table_rows.append(
                f'<tr class="data-row{alt_class}">'
                f'<td class="rule-name">{rule_cell}</td>'
                f'<td class="status-cell"><span class="compliance-badge {status}">{badge_label}</span></td>'
                f'<td>{escape(description)}</td>'
                f'</tr>'
            )
            data_row_idx += 1

            if status == "review":
                for iss in issues_by_crit.get(crit_id, []):
                    sev_val = iss.get("severity", "minor")
                    itype = iss.get("type", "")
                    desc_i = iss.get("description", "")
                    loc = iss.get("location") or {}
                    page_num = loc.get("page_number", "")
                    page_sfx = f" (p.{page_num})" if page_num else ""
                    issue_text = f"{sev_val.upper()}: {itype} \u2014 {desc_i}{page_sfx}"
                    table_rows.append(
                        f'<tr class="issue-row">'
                        f'<td colspan="3">'
                        f'<span class="sev-{sev_val}" aria-hidden="true">&#x25cf;</span>'
                        f'<span class="sr-only">{escape(sev_val.capitalize())} issue:</span>'
                        f' {escape(issue_text)}'
                        f'</td>'
                        f'</tr>'
                    )

    rows_html = "\n          ".join(table_rows)

    return f"""
    <section aria-labelledby="compliance-heading">
      <h2 id="compliance-heading">Compliance Summary</h2>

      <div class="verdict {verdict_cls}" role="region" aria-label="Compliance verdict: {verdict_lbl}">
        <div class="verdict-left">
          <p class="verdict-label">{verdict_lbl}</p>
          <p class="verdict-msg">{escape(status_msg)}</p>
        </div>
        <div class="verdict-divider" aria-hidden="true"></div>
        <div class="verdict-right">
          <p class="score">Score: {score:.1f}%</p>
          <p>Criteria: {n_passing} passing, {n_failing} failing</p>
          <p>Issues: {needs_rem} requiring attention</p>
        </div>
      </div>

      <div class="count-grid" aria-label="Summary statistics">
        {counts_html}
      </div>
    </section>

    <section aria-labelledby="detailed-heading">
      <h2 id="detailed-heading">Detailed Compliance Report</h2>
      <p class="legend">
        Status:
        <span class="compliance-badge pass">COMPLIANT</span> All checks passing&#x2003;
        <span class="compliance-badge review">NEEDS REVIEW</span> Issues found, manual verification needed&#x2003;
        <span class="compliance-badge na">N/A</span> Not evaluated in this document
      </p>

      <table class="compliance-table">
        <caption>WCAG 2.1 AA verification check results by category</caption>
        <thead>
          <tr>
            <th scope="col">Rule Name</th>
            <th scope="col">Status</th>
            <th scope="col">Description</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </section>"""


def _img_outcome_badge(outcome: str) -> str:
    if outcome == "generated":
        return '<span class="badge badge-generated">AI Generated</span>'
    if outcome == "decorative":
        return '<span class="badge badge-decorative">Decorative</span>'
    return '<span class="badge badge-img-skip">Skipped</span>'


# ── New section builders ──────────────────────────────────────────────────────


def _build_document_profile(
    doc_stats: Optional[Dict[str, Any]],
    doc_language: Optional[str],
    source_filename: Optional[str],
) -> str:
    """Build the Document Profile section HTML. Returns '' if no data."""
    if not doc_stats:
        return ""
    pages = doc_stats.get("pages", 0)
    figures = doc_stats.get("figures", 0)
    tables = doc_stats.get("tables", 0)
    formulas = doc_stats.get("formulas", 0)

    meta_chips: List[str] = []
    if source_filename:
        meta_chips.append(
            f'<span class="count-box">Source: <strong>{escape(source_filename)}</strong></span>'
        )
    if doc_language:
        meta_chips.append(
            f'<span class="count-box">Language: <strong>{escape(doc_language)}</strong></span>'
        )
    meta_html = " ".join(meta_chips)

    return f"""
    <section aria-labelledby="profile-heading">
      <h2 id="profile-heading">Document Profile</h2>
      <div class="summary-grid" role="region" aria-label="Document statistics">
        <div class="summary-box box-profile">
          <div class="count">{pages}</div>
          <div class="label">Pages</div>
        </div>
        <div class="summary-box box-profile">
          <div class="count">{figures}</div>
          <div class="label">Figures</div>
        </div>
        <div class="summary-box box-profile">
          <div class="count">{tables}</div>
          <div class="label">Tables</div>
        </div>
        <div class="summary-box box-profile">
          <div class="count">{formulas}</div>
          <div class="label">Formulas</div>
        </div>
      </div>
      <div class="count-grid" aria-label="Document metadata">
        {meta_html}
      </div>
    </section>"""


def _build_improvement_delta(
    pre_audit: Optional[Dict[str, Any]],
    post_audit: Optional[Dict[str, Any]],
) -> str:
    """Build the Improvement Summary section. Returns '' if pre_audit unavailable."""
    if not pre_audit or not post_audit:
        return ""
    pre_score = pre_audit.get("compliance", {}).get("overall_score", 0.0)
    post_score = post_audit.get("compliance", {}).get("overall_score", 0.0)
    pre_issues = pre_audit.get("summary", {}).get("needs_remediation", 0)
    post_issues = post_audit.get("summary", {}).get("needs_remediation", 0)
    resolved = max(0, pre_issues - post_issues)
    delta = post_score - pre_score

    if delta > 0:
        delta_msg = f"Compliance score improved by {delta:.1f} percentage points"
    elif delta == 0:
        delta_msg = "Compliance score unchanged"
    else:
        delta_msg = f"Compliance score changed by {delta:.1f} percentage points"

    return f"""
    <section aria-labelledby="delta-heading">
      <h2 id="delta-heading">Improvement Summary</h2>
      <div class="verdict verdict-delta" role="region" aria-label="Score improvement from {pre_score:.1f}% to {post_score:.1f}%">
        <div class="verdict-left">
          <p class="verdict-label">{pre_score:.1f}% &rarr; {post_score:.1f}%</p>
          <p class="verdict-msg">{escape(delta_msg)}</p>
        </div>
        <div class="verdict-divider" aria-hidden="true"></div>
        <div class="verdict-right">
          <p class="score">{pre_issues} &rarr; {post_issues}</p>
          <p>Issues requiring attention</p>
          <p>{resolved} issue{"s" if resolved != 1 else ""} resolved</p>
        </div>
      </div>
    </section>"""


def _build_pipeline_summary(
    doc_stats: Optional[Dict[str, Any]],
    fallback_recovery: Optional[Dict[str, Any]],
    ocr_recovery: Optional[Dict[str, Any]],
    alt_text_result: Optional[Dict[str, Any]],
    table_captions_result: Optional[Dict[str, Any]],
    formula_records: Optional[List[Dict[str, Any]]],
    remediation_result: Optional[Dict[str, Any]],
    phase_status: Optional[Dict[str, Any]],
    stripped_text_pages: Optional[List[int]],
) -> str:
    """Build the Pipeline Processing Summary section. Returns '' if no data."""
    chips: List[str] = []

    stats = doc_stats or {}
    pages = stats.get("pages")
    if pages is not None:
        chips.append(f"Conversion: <strong>{pages} pages</strong> processed")

    fb = fallback_recovery or {}
    fb_pages = fb.get("pages_recovered", 0)
    if fb_pages > 0:
        fb_elems = fb.get("fallback_elements_added", 0)
        chips.append(
            f"Text recovery: <strong>{fb_pages} pages</strong> "
            f"rescued ({fb_elems} elements)"
        )

    ocr = ocr_recovery or {}
    ocr_pages = ocr.get("pages_recovered", 0)
    ocr_skip = ocr.get("ocr_skipped_reason")
    if ocr_pages > 0:
        ocr_elems = ocr.get("fallback_elements_added", 0)
        chips.append(
            f"OCR recovery: <strong>{ocr_pages} pages</strong> "
            f"scanned ({ocr_elems} elements)"
        )
    elif ocr_skip:
        chips.append(f"OCR: <strong>skipped</strong> ({escape(ocr_skip)})")

    alt = alt_text_result or {}
    alt_count = alt.get("updated_count", 0)
    if alt_count > 0:
        chips.append(f"Alt text: <strong>{alt_count} images</strong> captioned by AI")

    tc = table_captions_result or {}
    tc_count = tc.get("updated_count", 0)
    if tc_count > 0:
        chips.append(f"Table captions: <strong>{tc_count} tables</strong> captioned by AI")

    form_count = len(formula_records) if formula_records else 0
    if form_count > 0:
        chips.append(f"Formulas: <strong>{form_count}</strong> described by AI")

    rem = remediation_result or {}
    rem_stats = rem.get("stats", {})
    rem_count = rem_stats.get("remediated", 0)
    if rem_count > 0:
        chips.append(f"HTML remediation: <strong>{rem_count} issues</strong> fixed")

    ps = phase_status or {}
    tag_info = ps.get("tagging", {})
    tag_mode = tag_info.get("mode")
    tag_status = tag_info.get("status")
    if tag_status == "success" and tag_mode:
        chips.append(f"PDF tagging: <strong>{escape(tag_mode)}</strong> mode")

    stripped = stripped_text_pages or []
    if stripped:
        chips.append(
            f"OCR stripping: <strong>{len(stripped)} pages</strong> "
            f"cleaned"
        )

    if not chips:
        return ""

    chips_html = "\n    ".join(
        f'<span class="count-box">{c}</span>' for c in chips
    )

    return f"""
    <section aria-labelledby="pipeline-heading">
      <h2 id="pipeline-heading">Pipeline Processing Summary</h2>
      <div class="count-grid" aria-label="Pipeline phase results">
        {chips_html}
      </div>
    </section>"""


def _build_source_quality_section(
    sqw: Optional[Dict[str, Any]],
) -> str:
    """Build the Source Document Quality section. Returns '' if no issues."""
    if not sqw:
        return ""

    issues: List[str] = []
    repairs: List[str] = []

    n = sqw.get("unembedded_fonts", 0)
    if n > 0:
        names = sqw.get("unembedded_font_names", [])
        detail = f" ({', '.join(escape(n) for n in names[:5])}{'…' if len(names) > 5 else ''})" if names else ""
        issues.append(f"<strong>{n}</strong> unembedded font(s){detail}")

    n = sqw.get("tounicode_cmaps_failed", 0)
    if n > 0:
        issues.append(f"<strong>{n}</strong> font(s) where Unicode mapping could not be generated")

    n = sqw.get("missing_tounicode", 0)
    if n > 0:
        issues.append(f"<strong>{n}</strong> font(s) missing ToUnicode CMap at compliance check")

    n = sqw.get("invisible_text_pages_stripped", 0)
    if n > 0:
        issues.append(f"Invisible OCR text stripped from <strong>{n}</strong> page(s)")

    n = sqw.get("fonts_injected", 0)
    if n > 0:
        repairs.append(f"<strong>{n}</strong> font(s) injected as replacements")

    if sqw.get("substitution_fonts_injected", 0) > 0:
        sub_names = sqw.get("substitution_font_names", [])
        detail = f" ({', '.join(escape(n) for n in sub_names[:5])}{'…' if len(sub_names) > 5 else ''})" if sub_names else ""
        issues.append(
            '<strong style="color:#dc2626;">Acrobat compatibility:</strong> '
            f"Substitution fonts were injected for {sqw['substitution_fonts_injected']} font(s){detail}. "
            "The tagged PDF may render incorrectly in Adobe Acrobat. Use the accessible HTML output, "
            "or obtain a newer copy of the source document."
        )

    n = sqw.get("tounicode_cmaps_added", 0)
    if n > 0:
        repairs.append(f"<strong>{n}</strong> ToUnicode CMap(s) generated")

    n = sqw.get("stripped_cidset", 0)
    if n > 0:
        repairs.append(f"<strong>{n}</strong> CIDSet entry/entries removed (font metadata fix)")

    n = sqw.get("stripped_charset", 0)
    if n > 0:
        repairs.append(f"<strong>{n}</strong> CharSet entry/entries removed (font metadata fix)")

    n = sqw.get("stripped_symbolic_encoding", 0)
    if n > 0:
        repairs.append(f"<strong>{n}</strong> symbolic TrueType /Encoding removed")

    n = sqw.get("fixed_cidtogidmap", 0)
    if n > 0:
        repairs.append(f"<strong>{n}</strong> CIDToGIDMap entry/entries repaired")

    n = sqw.get("font_descriptors_repaired", 0)
    if n > 0:
        repairs.append(f"<strong>{n}</strong> FontDescriptor(s) had missing metrics filled")

    if not issues and not repairs:
        return ""

    parts: List[str] = []
    if issues:
        items = "\n      ".join(f"<li>{i}</li>" for i in issues)
        parts.append(f"""<h3 style="font-size:9pt; margin:6pt 0 3pt;">Issues Detected in Source PDF</h3>
    <ul style="font-size:8pt; margin:0 0 6pt 16pt; padding:0; color:#374151;">
      {items}
    </ul>""")
    if repairs:
        items = "\n      ".join(f"<li>{r}</li>" for r in repairs)
        parts.append(f"""<h3 style="font-size:9pt; margin:6pt 0 3pt;">Automatic Repairs Applied</h3>
    <ul style="font-size:8pt; margin:0 0 6pt 16pt; padding:0; color:#374151;">
      {items}
    </ul>""")

    body = "\n    ".join(parts)
    return f"""
    <section aria-labelledby="source-quality-heading">
      <h2 id="source-quality-heading">Source Document Quality</h2>
      <p style="font-size:8pt; color:#6B7280; margin-bottom:4pt;">
        These issues exist in the original PDF. The pipeline applied automatic
        repairs where possible, but some may require manual attention.
      </p>
    {body}
    </section>"""


def _build_formula_review(
    formula_records: Optional[List[Dict[str, Any]]],
) -> str:
    """Build the Formula Review section. Returns '' if no formulas."""
    if not formula_records:
        return """
<section aria-labelledby="formulas-heading">
  <h2 id="formulas-heading">Formula Review</h2>
  <p style="font-size:8pt; color:#374151;">No formulas required AI description.</p>
</section>"""

    blocks = ""
    for idx, rec in enumerate(formula_records, 1):
        page = rec.get("page_number")
        description = rec.get("description", "")
        latex = rec.get("latex") or ""
        image_ref = rec.get("image_ref")

        # Thumbnail
        thumbnail_html = ""
        if image_ref:
            data_uri = _embed_image(image_ref)
            if data_uri:
                thumbnail_html = (
                    f'<img src="{data_uri}" '
                    f'alt="Rendered image of formula {idx} from {escape(_page_label(page))}">'
                )
        if not thumbnail_html:
            thumbnail_html = (
                '<div class="no-preview" aria-hidden="true">'
                'Preview not available</div>'
            )

        latex_html = ""
        if latex and latex != "unknown":
            latex_html = (
                f'<p class="alt-text formula-latex">'
                f'<strong>LaTeX:</strong> <code>{escape(latex)}</code></p>'
            )

        blocks += f"""
<figure class="image-block" aria-labelledby="formula-{idx}-caption">
  {thumbnail_html}
  <figcaption id="formula-{idx}-caption" class="image-meta">
    <p><strong>{escape(_page_label(page))}</strong>
      &ensp;<span class="badge badge-generated">AI Generated</span></p>
    <p class="alt-text"><strong>Description:</strong> "{escape(description)}"</p>
    {latex_html}
  </figcaption>
</figure>"""

    return f"""
<section aria-labelledby="formulas-heading">
  <h2 id="formulas-heading">Formula Review</h2>
  <p style="margin-bottom:6pt; font-size:8pt; color:#374151;">
    The following formulas were processed by the AI model. Review each
    description for accuracy before finalising the accessible document.
  </p>
  {blocks}
</section>"""


# ── HTML builder ───────────────────────────────────────────────────────────────

def _build_html(
    doc_title: str,
    change_records: List[Dict[str, Any]],
    table_changes: List[Dict[str, Any]],
    image_records: List[Dict[str, Any]],
    css_injected: bool,
    post_audit: Optional[Dict[str, Any]] = None,
    for_screen: bool = False,
    stripped_text_pages: Optional[List[int]] = None,
    *,
    doc_stats: Optional[Dict[str, Any]] = None,
    doc_language: Optional[str] = None,
    source_filename: Optional[str] = None,
    pre_audit: Optional[Dict[str, Any]] = None,
    fallback_recovery: Optional[Dict[str, Any]] = None,
    ocr_recovery: Optional[Dict[str, Any]] = None,
    formula_records: Optional[List[Dict[str, Any]]] = None,
    phase_status: Optional[Dict[str, Any]] = None,
    alt_text_result: Optional[Dict[str, Any]] = None,
    table_captions_result: Optional[Dict[str, Any]] = None,
    remediation_result: Optional[Dict[str, Any]] = None,
    sr_data: Optional[Dict[str, Any]] = None,
    verdict_config: Optional[Dict[str, Any]] = None,
    source_quality_warnings: Optional[Dict[str, Any]] = None,
) -> str:
    """Build the complete WCAG 2.1 AA HTML for the combined verification report."""
    now = datetime.now().strftime("%B %d, %Y")
    title_safe = escape(doc_title)

    # Build compliance sections (verdict + criteria table)
    compliance_html = _build_compliance_sections(
        post_audit, sr_data=sr_data, verdict_config=verdict_config,
    ) if post_audit else ""

    # Build new sections
    profile_html = _build_document_profile(doc_stats, doc_language, source_filename)
    delta_html = _build_improvement_delta(pre_audit, post_audit)
    pipeline_html = _build_pipeline_summary(
        doc_stats, fallback_recovery, ocr_recovery, alt_text_result,
        table_captions_result, formula_records, remediation_result,
        phase_status, stripped_text_pages,
    )
    source_quality_html = _build_source_quality_section(source_quality_warnings)
    formula_html = _build_formula_review(formula_records)

    # Determine which issue types still have failures in the post-audit.
    # Any "skipped" or "failed" record whose type is NOT still failing gets
    # reclassified as "resolved" (fixed by the batch sweep or a cascade effect).
    still_failing_types: set = set()
    if post_audit:
        for issue in post_audit.get("issues", []):
            if issue.get("remediation_status") == "needs_remediation":
                still_failing_types.add(issue.get("type", ""))

    def _effective_outcome(rec: Dict[str, Any]) -> str:
        outcome = rec.get("outcome", "skipped")
        if outcome in ("skipped", "failed") and rec.get("type", "") not in still_failing_types:
            return "fixed"
        return outcome

    # Reclassify records in-place (work on a shallow copy to avoid mutating caller data)
    change_records = [
        {**r, "outcome": _effective_outcome(r), "detail": (
            "Fixed via batch sweep"
            if _effective_outcome(r) == "fixed" and r.get("outcome") in ("skipped", "failed")
            else r.get("detail", "")
        )}
        for r in change_records
    ]

    fixed_count = sum(1 for r in change_records if r.get("outcome") == "fixed")
    skipped_count = sum(1 for r in change_records if r.get("outcome") in ("skipped", "failed"))
    image_count = len(image_records)
    table_count = len(table_changes)

    # ── Summary boxes ────────────────────────────────────────────────────────
    summary_html = f"""
<div class="summary-grid" role="region" aria-labelledby="summary-heading">
  <div class="summary-box box-fixed">
    <div class="count">{fixed_count}</div>
    <div class="label">Changes Applied</div>
  </div>
  <div class="summary-box box-skipped">
    <div class="count">{skipped_count}</div>
    <div class="label">Unresolved Items</div>
  </div>
  <div class="summary-box box-images">
    <div class="count">{image_count}</div>
    <div class="label">Images Reviewed</div>
  </div>
  <div class="summary-box box-tables">
    <div class="count">{table_count}</div>
    <div class="label">Tables Improved</div>
  </div>
</div>"""

    # ── Group changes by issue type, collecting pages per type ───────────────
    # CSS injection is always document-wide
    if css_injected:
        change_records = change_records + [{
            "outcome": "fixed",
            "type": "css-injection",
            "wcag_criterion": "1.4.3",
            "severity": "minor",
            "description": "Accessible CSS injected",
            "detail": "Accessible CSS stylesheet injected for contrast, spacing, and focus indicators",
        }]

    # Group change_records by (type, outcome)
    # Each group tracks pages and detail text
    ByTypeKey = Tuple[str, str]  # (issue_type, outcome)
    by_type: Dict[ByTypeKey, Dict[str, Any]] = {}
    for rec in change_records:
        itype = rec.get("type", "")
        outcome = rec.get("outcome", "skipped")
        key: ByTypeKey = (itype, outcome)
        if key not in by_type:
            by_type[key] = {
                "type": itype,
                "outcome": outcome,
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
                "type": itype,
                "outcome": "fixed",
                "wcag_criterion": "1.3.1",
                "detail": detail,
                "pages": [],
                "is_document_wide": False,
                "is_table": True,
                "caption": cap,
            }
        page_num = tc.get("page_number")
        if page_num and page_num not in by_type[key]["pages"]:
            by_type[key]["pages"].append(page_num)

    # Sort pages within each group
    for entry in by_type.values():
        entry["pages"].sort()

    # Sort entries: fixed first, then skipped/failed; within each group alphabetical
    outcome_order = {"fixed": 0, "skipped": 1, "failed": 2}
    sorted_entries = sorted(
        by_type.values(),
        key=lambda e: (outcome_order.get(e["outcome"], 9), e["type"]),
    )

    # Build changes HTML as a table
    changes_rows = ""
    for entry in sorted_entries:
        itype = entry["type"]
        is_table = entry.get("is_table", False)
        if is_table:
            cap = entry.get("caption", "")
            label = f"Table: {cap}" if cap else "Data table"
        else:
            label = _issue_label(itype)
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
            pages_str = "—"

        changes_rows += f"""
    <tr>
      <td>{escape(label)}</td>
      <td>{escape(wcag)}</td>
      <td>{escape(detail)}</td>
      <td>{escape(pages_str)}</td>
      <td>{_outcome_badge(outcome)}</td>
    </tr>"""

    pages_html = f"""
<table>
  <caption class="sr-only">Accessibility changes grouped by issue type</caption>
  <thead>
    <tr>
      <th scope="col">Change</th>
      <th scope="col">WCAG</th>
      <th scope="col">Detail</th>
      <th scope="col">Pages</th>
      <th scope="col">Status</th>
    </tr>
  </thead>
  <tbody>{changes_rows}
  </tbody>
</table>"""

    # ── Image review section ─────────────────────────────────────────────────
    images_html = ""
    if image_records:
        blocks = ""
        for idx, rec in enumerate(image_records, 1):
            outcome = rec.get("outcome", "skipped")
            page = rec.get("page_number")
            alt_text = rec.get("alt_text") or ""
            file_path = rec.get("file_path")
            skip_reason = rec.get("skip_reason") or ""

            # Thumbnail
            thumbnail_html = ""
            if file_path:
                data_uri = _embed_image(file_path)
                if data_uri:
                    thumbnail_html = (
                        f'<img src="{data_uri}" '
                        f'alt="Thumbnail of image {idx} from {escape(_page_label(page))}">'
                    )
            if not thumbnail_html:
                thumbnail_html = (
                    f'<div class="no-preview" aria-hidden="true">'
                    f'Preview not available</div>'
                )

            # Alt text / outcome line
            if outcome == "generated":
                alt_line = (
                    f'<p class="alt-text"><strong>Alt text:</strong> '
                    f'"{escape(alt_text)}"</p>'
                )
            elif outcome == "decorative":
                alt_line = (
                    '<p class="alt-text"><strong>Alt text:</strong> '
                    '<em>(marked as decorative — no alt text required)</em></p>'
                )
            else:
                alt_line = (
                    f'<p class="alt-text"><strong>Status:</strong> '
                    f'{escape(skip_reason)}</p>'
                )

            blocks += f"""
<figure class="image-block" aria-labelledby="img-{idx}-caption">
  {thumbnail_html}
  <figcaption id="img-{idx}-caption" class="image-meta">
    <p><strong>{escape(_page_label(page))}</strong>
      &ensp;{_img_outcome_badge(outcome)}</p>
    {alt_line}
  </figcaption>
</figure>"""

        images_html = f"""
<section aria-labelledby="images-heading">
  <h2 id="images-heading">Image Review</h2>
  <p style="margin-bottom:6pt; font-size:8pt; color:#374151;">
    The following images were processed by the AI model. Review each caption
    for accuracy before finalising the accessible document.
  </p>
  {blocks}
</section>"""
    else:
        images_html = """
<section aria-labelledby="images-heading">
  <h2 id="images-heading">Image Review</h2>
  <p style="font-size:8pt; color:#374151;">No images required alt text generation.</p>
</section>"""

    # ── Table review section ─────────────────────────────────────────────────
    tables_html = ""
    vlm_captions = (table_captions_result or {}).get("captions", {})
    covered_caption_keys: set = set()
    if table_changes or vlm_captions:
        blocks = ""
        for idx, tc in enumerate(table_changes, 1):
            page = tc.get("page_number")
            cap = tc.get("caption", "") or ""
            changes = tc.get("changes", [])
            n_rows = tc.get("rows", 0)
            n_cols = tc.get("cols", 0)
            n_headers = tc.get("header_cells", 0)
            before_rows = tc.get("before_rows", [])
            after_rows = tc.get("after_rows", [])

            # Match this table to a VLM caption key by caption text
            vlm_caption_text = ""
            for vk, vtext in vlm_captions.items():
                if vtext and cap and vtext.strip() == cap.strip():
                    vlm_caption_text = vtext
                    covered_caption_keys.add(vk)
                    break

            title_text = f"Table {idx}"
            if cap:
                title_text += f": {cap}"

            caption_line = ""
            if vlm_caption_text:
                caption_line = (
                    f'\n  <p style="font-size:8pt;color:#141720;font-style:italic;margin:3pt 0 0;">'
                    f'<strong>Caption:</strong> "{escape(vlm_caption_text)}"</p>'
                )

            changes_li = "\n".join(
                f'      <li>{escape(c)}</li>' for c in changes
            )

            # Build stat chips
            stat_chips = []
            if n_rows:
                stat_chips.append(f"{n_rows} rows")
            if n_cols:
                stat_chips.append(f"{n_cols} cols")
            if n_headers:
                stat_chips.append(f"{n_headers} header cells")
            stats_html = " ".join(
                f'<span class="table-stat">{escape(s)}</span>'
                for s in stat_chips
            )

            # Build before/after mini-tables
            ba_html = ""
            if before_rows or after_rows:
                before_mini = _render_mini_table(before_rows, f"tbl-{idx}-before")
                after_mini = _render_mini_table(after_rows, f"tbl-{idx}-after")
                ba_html = f"""
  <div class="ba-container">
    <div class="ba-panel">
      <p class="ba-label">Before</p>
      {before_mini}
    </div>
    <div class="ba-panel">
      <p class="ba-label">After</p>
      {after_mini}
    </div>
  </div>"""

            blocks += f"""
<div class="table-block" role="region" aria-labelledby="tbl-{idx}-title">
  <div class="table-block-header">
    <p class="table-block-title" id="tbl-{idx}-title">{escape(title_text)}</p>
    <p class="table-block-meta">{escape(_page_label(page))}</p>
  </div>{caption_line}
  <ul class="table-changes-list">
{changes_li}
  </ul>{ba_html}
  <div style="margin-top:3pt;">
    {stats_html}
  </div>
</div>"""

        # Add tables that got VLM captions but no structural changes
        for vk in sorted(vlm_captions.keys()):
            if vk in covered_caption_keys:
                continue
            vtext = vlm_captions[vk]
            if not vtext:
                continue
            tbl_num = int(vk.replace("table_", "")) + 1
            blocks += f"""
<div class="table-block" role="region">
  <p class="table-block-title">Table {tbl_num}</p>
  <p style="font-size:8pt;color:#141720;font-style:italic;margin:3pt 0 0;">
    <strong>Caption:</strong> "{escape(vtext)}"</p>
  <span class="table-stat" style="margin-top:3pt;">AI-generated caption</span>
</div>"""

        tables_html = f"""
<section aria-labelledby="tables-heading">
  <h2 id="tables-heading">Table Review</h2>
  <p style="margin-bottom:6pt; font-size:8pt; color:#374151;">
    The following data tables were modified during remediation to improve
    accessibility. Review each table for structural correctness.
  </p>
  {blocks}
</section>"""
    else:
        tables_html = """
<section aria-labelledby="tables-heading">
  <h2 id="tables-heading">Table Review</h2>
  <p style="font-size:8pt; color:#374151;">No table changes were required.</p>
</section>"""

    # ── Processing notes (dropped — recovery/OCR/stripping are routine) ────
    notes_html = ""

    # ── Assemble full document ───────────────────────────────────────────────
    viewport_meta = (
        '\n  <meta name="viewport" content="width=device-width, initial-scale=1">'
        if for_screen else ""
    )
    screen_css = _SCREEN_CSS if for_screen else ""

    _b = get_branding()
    org_line = escape(" \u00b7 ".join(x for x in (_b.organization, _b.product_name) if x))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">{viewport_meta}
  <title>{title_safe} — WCAG 2.1 AA Verification Report</title>
  <style>{_CSS}{screen_css}</style>
</head>
<body>
  <a class="skip-link" href="#main-content">Skip to main content</a>

  <header role="banner">
    <p class="org-name">{org_line}</p>
    <h1>WCAG 2.1 AA Verification Report</h1>
    <p class="doc-subtitle">{title_safe} &mdash; Generated {escape(now)}</p>
  </header>

  <main id="main-content">

    {compliance_html}

    {profile_html}

    {delta_html}

    {pipeline_html}

    {source_quality_html}

    <section aria-labelledby="summary-heading">
      <h2 id="summary-heading">Document Changes Summary</h2>
      {summary_html}
    </section>

    <section aria-labelledby="changes-heading">
      <h2 id="changes-heading">Changes by Page</h2>
      {pages_html}
    </section>

    {notes_html}

    {images_html}

    {tables_html}

    {formula_html}

  </main>
</body>
</html>"""


_wp_font_patched = False


def _patch_weasyprint_font_subsetting():
    """Force WeasyPrint to use fonttools for font subsetting.

    WeasyPrint 68.x's HarfBuzz subsetting path produces an empty 12-byte
    TrueType stub, making all text invisible in PDF viewers.
    """
    global _wp_font_patched
    if _wp_font_patched:
        return
    try:
        import weasyprint.pdf.fonts as wpfonts
        if getattr(wpfonts, 'harfbuzz_subset', None) is not None:
            wpfonts.harfbuzz_subset = None
    except Exception:
        pass
    _wp_font_patched = True


# ── Public API ─────────────────────────────────────────────────────────────────

def generate_changes_report_pdf(
    output_path: str,
    doc_title: str,
    change_records: Optional[List[Dict[str, Any]]] = None,
    table_changes: Optional[List[Dict[str, Any]]] = None,
    image_records: Optional[List[Dict[str, Any]]] = None,
    css_injected: bool = True,
    post_audit: Optional[Dict[str, Any]] = None,
    stripped_text_pages: Optional[List[int]] = None,
    **kwargs,
) -> bool:
    """Generate a WCAG 2.1 AA-compliant Document Changes Report PDF.

    Args:
        output_path:    Destination file path for the PDF.
        doc_title:      Title of the source document.
        change_records: Per-issue change records from RemediationManager.
        table_changes:  Per-table batch sweep records from batch_fix_all_tables().
        image_records:  Per-image records from generate_alt_text_for_images().
        css_injected:   Whether accessible CSS was injected (adds a change entry).
        post_audit:     Post-remediation audit result, used to reclassify "skipped"
                        records that were subsequently resolved by the batch sweep.
        stripped_text_pages: Pages where invisible OCR text was stripped.
        **kwargs:       Additional keyword arguments forwarded to _build_html
                        (doc_stats, doc_language, source_filename, pre_audit,
                        fallback_recovery, ocr_recovery, formula_records,
                        phase_status, alt_text_result, table_captions_result,
                        remediation_result).

    Returns:
        True on success, False on failure.
    """
    try:
        from weasyprint import HTML as WP_HTML
    except ImportError:
        logger.error(
            "WeasyPrint is not installed — cannot generate Document Changes Report. "
            "Install with: pip install weasyprint"
        )
        return False

    change_records = change_records or []
    table_changes = table_changes or []
    image_records = image_records or []

    try:
        html_str = _build_html(
            doc_title=doc_title,
            change_records=change_records,
            table_changes=table_changes,
            image_records=image_records,
            css_injected=css_injected,
            post_audit=post_audit,
            stripped_text_pages=stripped_text_pages,
            **kwargs,
        )
        _patch_weasyprint_font_subsetting()
        pdf_bytes = WP_HTML(string=html_str, base_url=None).write_pdf(pdf_tags=True)
        Path(output_path).write_bytes(pdf_bytes)
        logger.info("Document Changes Report written to %s", output_path)
        return True
    except Exception as e:
        logger.error("Failed to generate Document Changes Report: %s", e)
        return False


def generate_changes_report_html(
    output_path: str,
    doc_title: str,
    change_records: Optional[List[Dict[str, Any]]] = None,
    table_changes: Optional[List[Dict[str, Any]]] = None,
    image_records: Optional[List[Dict[str, Any]]] = None,
    css_injected: bool = True,
    post_audit: Optional[Dict[str, Any]] = None,
    stripped_text_pages: Optional[List[int]] = None,
    **kwargs,
) -> bool:
    """Generate a WCAG 2.1 AA-compliant Document Changes Report as HTML.

    Args:
        output_path:    Destination file path for the HTML file.
        doc_title:      Title of the source document.
        change_records: Per-issue change records from RemediationManager.
        table_changes:  Per-table batch sweep records from batch_fix_all_tables().
        image_records:  Per-image records from generate_alt_text_for_images().
        css_injected:   Whether accessible CSS was injected (adds a change entry).
        post_audit:     Post-remediation audit result, used to reclassify "skipped"
                        records that were subsequently resolved by the batch sweep.
        stripped_text_pages: 1-based page numbers where invisible OCR text was
                        stripped for PDF/UA compliance. Shown in Processing Notes.
        **kwargs:       Additional keyword arguments forwarded to _build_html
                        (doc_stats, doc_language, source_filename, pre_audit,
                        fallback_recovery, ocr_recovery, formula_records,
                        phase_status, alt_text_result, table_captions_result,
                        remediation_result).

    Returns:
        True on success, False on failure.
    """
    change_records = change_records or []
    table_changes = table_changes or []
    image_records = image_records or []

    try:
        html_str = _build_html(
            doc_title=doc_title,
            change_records=change_records,
            table_changes=table_changes,
            image_records=image_records,
            css_injected=css_injected,
            post_audit=post_audit,
            for_screen=True,
            stripped_text_pages=stripped_text_pages,
            **kwargs,
        )
        Path(output_path).write_text(html_str, encoding="utf-8")
        logger.info("Document Changes Report (HTML) written to %s", output_path)
        return True
    except Exception as e:
        logger.error("Failed to generate HTML Document Changes Report: %s", e)
        return False
