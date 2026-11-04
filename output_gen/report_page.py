# SPDX-License-Identifier: AGPL-3.0-or-later
"""Append a WCAG 2.1 AA verification report page to the remediated PDF.

The report is generated as a semantically structured HTML document and
converted to a tagged PDF via WeasyPrint (>=62.0), which produces a proper
PDF structure tree from standard HTML semantics.  This approach satisfies
WCAG 2.1 Level AA for the report page itself:

  • 3.1.1  Language of Page      — html[lang="en"]
  • 2.4.2  Page Titled           — <title> element
  • 2.4.6  Headings and Labels   — <h1>, <h2> hierarchy
  • 1.3.1  Info and Relationships — <table> with scope attributes, <caption>
  • 2.4.4  Link Purpose          — <a> text content describes destination
  • 4.1.2  Name, Role, Value     — native HTML semantics
  • 4.1.1  Parsing               — valid HTML5 markup
  • 1.4.1  Use of Color          — badges use text AND colour
  • 1.4.3  Contrast (Minimum)    — all text/bg pairs verified ≥ 4.5:1 below

Contrast ratios (relative luminance per WCAG 1.4.3):
  #fff  on #1E3A5F (navy header)     ≈ 11.2:1  ✓
  #fff  on #1D4ED8 (section row)     ≈  6.5:1  ✓
  #fff  on #141720 (table header)    ≈ 15.0:1  ✓
  #fff  on #166534 (PASS banner)     ≈  7.2:1  ✓
  #141720 on #fff  (body text)       ≈ 15.0:1  ✓
  #47528A on #fff  (secondary text)  ≈  7.9:1  ✓
  #166534 on #DCFCE7 (PASS badge)    ≈  6.6:1  ✓
  #92400E on #FEF3C7 (REVIEW badge)  ≈  5.0:1  ✓
  #47568A on #F0F2F5 (N/A badge)     ≈  6.5:1  ✓
"""

import io
import logging
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from audit.standards import verdict_from_sr
from utils.branding import get_branding as _get_branding

logger = logging.getLogger(__name__)

# ─── WCAG criterion metadata ──────────────────────────────────────────────────

_NAMES: Dict[str, str] = {
    "1.1.1":  "Non-text Content",
    "1.3.1":  "Info and Relationships",
    "1.3.2":  "Meaningful Sequence",
    "1.3.5":  "Identify Input Purpose",
    "1.4.1":  "Use of Color",
    "1.4.3":  "Contrast (Minimum)",
    "1.4.4":  "Resize Text",
    "1.4.5":  "Images of Text",
    "1.4.10": "Reflow",
    "1.4.11": "Non-text Contrast",
    "1.4.12": "Text Spacing",
    "2.4.1":  "Bypass Blocks",
    "2.4.2":  "Page Titled",
    "2.4.4":  "Link Purpose (In Context)",
    "2.4.6":  "Headings and Labels",
    "3.1.1":  "Language of Page",
    "3.1.2":  "Language of Parts",
    "2.1.1":  "Keyboard",
    "2.4.3":  "Focus Order",
    "2.4.7":  "Focus Visible",
    "3.3.1":  "Error Identification",
    "3.3.2":  "Labels or Instructions",
    "4.1.1":  "Parsing",
    "4.1.2":  "Name, Role, Value",
}

_LEVEL: Dict[str, str] = {
    "1.1.1": "A",   "1.3.1": "A",   "1.3.2": "A",   "1.3.5": "AA",
    "1.4.1": "A",   "1.4.3": "AA",  "1.4.4": "AA",  "1.4.5": "AA",
    "1.4.10": "AA", "1.4.11": "AA", "1.4.12": "AA",
    "2.1.1": "A",   "2.4.1": "A",   "2.4.2": "A",   "2.4.3": "A",
    "2.4.4": "A",   "2.4.6": "AA",  "2.4.7": "AA",
    "3.1.1": "A",   "3.1.2": "AA",  "3.3.1": "A",
    "3.3.2": "A",   "4.1.1": "A",   "4.1.2": "A",
}

_WCAG_BASE = "https://www.w3.org/TR/WCAG21/"
_WCAG_URL: Dict[str, str] = {
    "1.1.1":  _WCAG_BASE + "#non-text-content",
    "1.3.1":  _WCAG_BASE + "#info-and-relationships",
    "1.3.2":  _WCAG_BASE + "#meaningful-sequence",
    "1.3.5":  _WCAG_BASE + "#identify-input-purpose",
    "1.4.1":  _WCAG_BASE + "#use-of-color",
    "1.4.3":  _WCAG_BASE + "#contrast-minimum",
    "1.4.4":  _WCAG_BASE + "#resize-text",
    "1.4.5":  _WCAG_BASE + "#images-of-text",
    "1.4.10": _WCAG_BASE + "#reflow",
    "1.4.11": _WCAG_BASE + "#non-text-contrast",
    "1.4.12": _WCAG_BASE + "#text-spacing",
    "2.1.1":  _WCAG_BASE + "#keyboard",
    "2.4.1":  _WCAG_BASE + "#bypass-blocks",
    "2.4.2":  _WCAG_BASE + "#page-titled",
    "2.4.3":  _WCAG_BASE + "#focus-order",
    "2.4.4":  _WCAG_BASE + "#link-purpose-in-context",
    "2.4.6":  _WCAG_BASE + "#headings-and-labels",
    "2.4.7":  _WCAG_BASE + "#focus-visible",
    "3.1.1":  _WCAG_BASE + "#language-of-page",
    "3.1.2":  _WCAG_BASE + "#language-of-parts",
    "3.3.1":  _WCAG_BASE + "#error-identification",
    "3.3.2":  _WCAG_BASE + "#labels-or-instructions",
    "4.1.1":  _WCAG_BASE + "#parsing",
    "4.1.2":  _WCAG_BASE + "#name-role-value",
}

# (section_label, [(wcag_id, one-line description), …])
_SECTIONS: List[Tuple[str, List[Tuple[str, str]]]] = [
    ("Document", [
        ("2.4.2",  "Document has a descriptive title"),
        ("3.1.1",  "Primary language is specified"),
        ("3.1.2",  "Language of text phrases is specified"),
        ("1.3.2",  "Correct reading sequence can be determined"),
        ("2.4.1",  "Mechanism available to bypass repeated content"),
    ]),
    ("Page Content", [
        ("1.4.3",  "Text contrast ratio is at least 4.5:1"),
        ("1.4.1",  "Color not sole means of conveying information"),
        ("1.4.4",  "Text resizable to 200% without loss of content"),
        ("1.4.10", "Content reflows at 320 CSS pixels width"),
        ("1.4.12", "Text spacing adjustable without content loss"),
        ("4.1.1",  "No markup errors; element IDs are unique"),
    ]),
    ("Alternate Text", [
        ("1.1.1",  "All non-text content has a text alternative"),
    ]),
    ("Images", [
        ("1.4.5",  "Real text used instead of images of text"),
        ("1.4.11", "UI component graphics have 3:1 contrast ratio"),
    ]),
    ("Tables", [
        ("1.3.1",  "Table structure is programmatically determinable"),
    ]),
    ("Headings", [
        ("2.4.6",  "Headings describe topic or purpose"),
    ]),
    ("Links", [
        ("2.4.4",  "Link purpose determinable from text or context"),
    ]),
    ("Keyboard &amp; Focus", [
        ("2.1.1",  "All functionality available from a keyboard"),
        ("2.4.3",  "Focus order preserves meaning and operability"),
        ("2.4.7",  "Keyboard focus indicator is visible"),
    ]),
    ("Forms", [
        ("4.1.2",  "UI components have programmatic name and role"),
        ("3.3.1",  "Input errors are identified and described in text"),
        ("3.3.2",  "Form inputs have labels or instructions"),
        ("1.3.5",  "Form field purpose is programmatically determined"),
    ]),
]

# ─── Stylesheet ───────────────────────────────────────────────────────────────

_CSS = """
@page {
    size: A4;
    margin: 0;
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
    line-height: 1.35;
}

/* ── Page header ─────────────────────────────────────────────────────────── */
header {
    background: #1E3A5F;
    color: #ffffff;
    padding: 8mm 13mm 5mm;
}

.org-name {
    font-size: 7.5pt;
    color: #A6C4F0;
    margin-bottom: 2pt;
}

header h1 {
    font-size: 19pt;
    font-weight: bold;
    color: #ffffff;
    margin-bottom: 2pt;
}

.standard {
    font-size: 8.5pt;
    color: #A6C4F0;
}

/* ── Main content ────────────────────────────────────────────────────────── */
main {
    padding: 6pt 13mm 0;
}

/* ── Document metadata strip ─────────────────────────────────────────────── */
.meta {
    border-bottom: 0.5pt solid #D9DBE0;
    padding-bottom: 6pt;
    margin-bottom: 6pt;
}

.meta p {
    font-size: 8.5pt;
    color: #141720;
    margin-bottom: 2pt;
}

.meta strong {
    color: #47528A;
}

/* ── Section headings ────────────────────────────────────────────────────── */
section {
    margin-bottom: 6pt;
}

section h2 {
    font-size: 10.5pt;
    font-weight: bold;
    color: #141720;
    margin-bottom: 4pt;
}

/* ── Verdict banner ──────────────────────────────────────────────────────── */
.verdict {
    display: flex;
    flex-direction: row;
    border-radius: 2pt;
    overflow: hidden;
    margin-bottom: 5pt;
}

.verdict-pass   { background: #166534; }
.verdict-review { background: #e0f2f1; }

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
.verdict-review .verdict-label { font-size: 16pt; color: #1a3636; }

.verdict-msg {
    font-size: 8.5pt;
    color: #ffffff;
}
.verdict-review .verdict-msg { color: #1a3636; }

.verdict-divider {
    width: 0.6pt;
    background: rgba(255, 255, 255, 0.4);
    margin: 6pt 0;
}
.verdict-review .verdict-divider { background: rgba(26, 54, 54, 0.20); }

.verdict-right {
    min-width: 140pt;
    padding: 7pt 12pt;
    font-size: 8pt;
    color: #ffffff;
}
.verdict-review .verdict-right { color: #1a3636; }
.verdict-review .score { color: #1a3636; }

.score {
    font-size: 13pt;
    font-weight: bold;
    margin-bottom: 2pt;
}

/* ── Checker message and count grid ──────────────────────────────────────── */
.checker-msg {
    font-size: 8.5pt;
    color: #141720;
    margin-bottom: 4pt;
}

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

/* ── Legend ──────────────────────────────────────────────────────────────── */
.legend {
    font-size: 7.5pt;
    color: #47528A;
    margin-bottom: 5pt;
}

/* ── Criteria table ──────────────────────────────────────────────────────── */
table {
    width: 100%;
    border-collapse: collapse;
    font-size: 8.5pt;
}

caption {
    caption-side: top;
    text-align: left;
    font-size: 7pt;
    color: #47528A;
    margin-bottom: 2pt;
}

thead th {
    background: #141720;
    color: #ffffff;
    font-size: 8pt;
    font-weight: bold;
    padding: 3pt 5pt;
    text-align: left;
}

thead th:first-child  { width: 34%; }
thead th:nth-child(2) { width: 13%; }
thead th:last-child   { width: 53%; }

tbody td,
tbody th {
    padding: 2.5pt 5pt;
    vertical-align: middle;
}

tr.alt-row { background: #EEF2FE; }

tr.section-header {
    background: #1D4ED8;
    page-break-after: avoid;
}

tr.section-header th {
    color: #ffffff;
    font-size: 8.5pt;
    font-weight: bold;
    padding: 3pt 5pt;
}

.rule-name {
    font-size: 8pt;
}

.rule-name a {
    color: #141720;
    text-decoration: underline;
}

.status-cell {
    white-space: nowrap;
}

/* ── Status badges — text AND colour (WCAG 1.4.1) ────────────────────────── */
.badge {
    display: inline-block;
    padding: 1pt 5pt;
    border-radius: 2pt;
    font-size: 7.5pt;
    font-weight: bold;
}

.badge.pass   { background: #DCFCE7; color: #166534; }
.badge.review { background: #FEF3C7; color: #92400E; }
.badge.fail   { background: #FEF3C7; color: #92400E; }
.badge.na     { background: #F0F2F5; color: #47568A; }

/* ── Inline issue rows ───────────────────────────────────────────────────── */
tr.issue-row td {
    font-size: 7pt;
    color: #47528A;
    padding: 1pt 5pt 1pt 16pt;
}

.sev-critical { color: #92400E; }
.sev-major    { color: #B45309; }
.sev-minor    { color: #CA8A04; }

/* Screen-reader-only class (accessible hidden text) */
.sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    border: 0;
}

/* ── Page footer ─────────────────────────────────────────────────────────── */
footer {
    padding: 4pt 13mm;
    border-top: 0.5pt solid #D9DBE0;
    margin-top: 8pt;
    font-size: 7.5pt;
    color: #47528A;
}
"""

# Screen-specific CSS overrides for standalone HTML viewing.
# Removes fixed A4 page sizing and adds responsive layout.
_SCREEN_CSS = """

/* ── Screen overrides ─────────────────────────────────────────────────────── */
@page { size: auto; margin: 0; }

body {
    max-width: 960px;
    margin: 0 auto;
    font-size: 10pt;
    padding-bottom: 2em;
}

header { border-radius: 0 0 4pt 4pt; }

main { padding: 12pt 16pt 0; }

table { font-size: 9pt; }

.verdict { flex-wrap: wrap; }

.count-box { font-size: 9pt; }
"""

# ─── Public API ───────────────────────────────────────────────────────────────


def append_audit_report_page(
    pdf_path: str,
    post_audit: Dict[str, Any],
    doc_title: str = "",
    sr_quality: str = "unknown",
    sr_data: Optional[Dict[str, Any]] = None,
    verdict_config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Append a WCAG 2.1 AA verification report page to the end of the PDF.

    Args:
        pdf_path:        Path to the accessible PDF (modified in-place).
        post_audit:      Post-remediation audit result dict.
        doc_title:       Document title for the report header.
        sr_quality:      Screen reader quality verdict (good/fair/poor/unknown).
        sr_data:         Full SR quality dict from validation gates.
        verdict_config:  Verdict thresholds from config.yaml.

    Returns:
        True on success, False on failure (original PDF is untouched on error).
    """
    pdf_bytes = _render_report_pdf(
        post_audit, doc_title, sr_quality=sr_quality,
        sr_data=sr_data, verdict_config=verdict_config,
    )
    if pdf_bytes is None:
        return False

    try:
        import fitz
    except ImportError:
        logger.warning(
            "PyMuPDF (fitz) not installed — cannot append report page. "
            "Install with: pip install pymupdf"
        )
        return False

    try:
        main_doc   = fitz.open(pdf_path)
        report_doc = fitz.open("pdf", pdf_bytes)
        main_doc.insert_pdf(report_doc)
        report_doc.close()

        buf = io.BytesIO()
        main_doc.save(buf, garbage=2, deflate=True)
        main_doc.close()

        buf.seek(0)
        Path(pdf_path).write_bytes(buf.read())
        logger.info("Verification report page appended to %s", Path(pdf_path).name)
        return True

    except Exception as e:
        logger.error("Failed to append verification report page: %s", e)
        return False


def generate_standalone_report_pdf(
    output_path: str,
    post_audit: Dict[str, Any],
    doc_title: str = "",
    sr_quality: str = "unknown",
    sr_data: Optional[Dict[str, Any]] = None,
    verdict_config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Generate a standalone single-page verification report PDF.

    Produces the same content as the page appended by append_audit_report_page,
    but as an independent PDF file.

    Args:
        output_path:     Destination path for the standalone PDF.
        post_audit:      Post-remediation audit result dict.
        doc_title:       Document title for the report header.
        sr_quality:      Screen reader quality verdict (good/fair/poor/unknown).
        sr_data:         Full SR quality dict from validation gates.
        verdict_config:  Verdict thresholds from config.yaml.

    Returns:
        True on success, False on failure.
    """
    pdf_bytes = _render_report_pdf(
        post_audit, doc_title, sr_quality=sr_quality,
        sr_data=sr_data, verdict_config=verdict_config,
    )
    if pdf_bytes is None:
        return False

    try:
        Path(output_path).write_bytes(pdf_bytes)
        logger.info(
            "Standalone verification report written to %s",
            Path(output_path).name,
        )
        return True
    except Exception as e:
        logger.error("Failed to write standalone verification report: %s", e)
        return False


def generate_standalone_report_html(
    output_path: str,
    post_audit: Dict[str, Any],
    doc_title: str = "",
    sr_quality: str = "unknown",
    sr_data: Optional[Dict[str, Any]] = None,
    verdict_config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Generate a standalone WCAG 2.1 AA-compliant verification report as HTML.

    Produces the same content as the PDF report but as a self-contained HTML
    file suitable for viewing in a web browser.

    Args:
        output_path:     Destination path for the HTML file.
        post_audit:      Post-remediation audit result dict.
        doc_title:       Document title for the report header.
        sr_quality:      Screen reader quality verdict (good/fair/poor/unknown).
        sr_data:         Full SR quality dict from validation gates.
        verdict_config:  Verdict thresholds from config.yaml.

    Returns:
        True on success, False on failure.
    """
    try:
        html_str = _build_report_html(
            post_audit, doc_title, for_screen=True, sr_quality=sr_quality,
            sr_data=sr_data, verdict_config=verdict_config,
        )
        Path(output_path).write_text(html_str, encoding="utf-8")
        logger.info(
            "Standalone verification report (HTML) written to %s",
            Path(output_path).name,
        )
        return True
    except Exception as e:
        logger.error("Failed to write standalone HTML verification report: %s", e)
        return False


# ─── PDF rendering ────────────────────────────────────────────────────────────


def _render_report_pdf(
    post_audit: Dict[str, Any],
    doc_title: str,
    sr_quality: str = "unknown",
    sr_data: Optional[Dict[str, Any]] = None,
    verdict_config: Optional[Dict[str, Any]] = None,
) -> Optional[bytes]:
    """Build the HTML report string and convert to PDF bytes via WeasyPrint.

    Returns:
        PDF bytes on success, None on failure.
    """
    try:
        from weasyprint import HTML
    except ImportError:
        logger.error(
            "WeasyPrint is not installed — cannot generate verification report. "
            "Install with: pip install weasyprint"
        )
        return None

    try:
        html_str = _build_report_html(
            post_audit, doc_title, sr_quality=sr_quality,
            sr_data=sr_data, verdict_config=verdict_config,
        )
        # Workaround: WeasyPrint 68's HarfBuzz font subsetting has a bug
        # where the second hb_subset_or_fail pass (meant to trim trailing
        # empty glyphs) produces an empty 12-byte TrueType stub, making
        # all text invisible in PDF viewers. Fix by monkeypatching the
        # subset method to use only fonttools (which works correctly).
        _patch_weasyprint_font_subsetting()
        # pdf_tags=True enables WeasyPrint's tagged PDF output:
        # - Generates a PDF StructTreeRoot with H1/H2/Table/TH/TD/Link etc.
        # - Adds ViewerPreferences.DisplayDocTitle = true
        # - Adds MarkInfo.Marked = true
        # - Preserves lang="en" from <html> as /Lang on the catalog
        return HTML(string=html_str).write_pdf(pdf_tags=True)
    except Exception as e:
        logger.error("Failed to render verification report PDF: %s", e)
        return None


_wp_font_patched = False


def _patch_weasyprint_font_subsetting():
    """Force WeasyPrint to use fonttools for font subsetting.

    WeasyPrint 68.x's HarfBuzz subsetting path has a bug: the second
    hb_subset_or_fail call (to drop trailing empty glyphs) produces an
    empty 12-byte TrueType file, causing invisible text in PDF viewers.
    This monkeypatch forces the fonttools path which works correctly.
    """
    global _wp_font_patched
    if _wp_font_patched:
        return
    try:
        # Must patch the local binding in weasyprint.pdf.fonts, not the
        # source module (weasyprint.text.ffi).  fonts.py does:
        #   from ..text.ffi import harfbuzz_subset
        # which creates a module-level name that won't see changes to ffi.
        import weasyprint.pdf.fonts as wpfonts
        if getattr(wpfonts, 'harfbuzz_subset', None) is not None:
            wpfonts.harfbuzz_subset = None
            logger.debug(
                "Disabled WeasyPrint HarfBuzz font subsetting "
                "(using fonttools instead)"
            )
    except Exception:
        pass  # If patching fails, proceed anyway
    _wp_font_patched = True


# ─── HTML generation ──────────────────────────────────────────────────────────


def _build_report_html(
    post_audit: Dict[str, Any],
    doc_title: str,
    for_screen: bool = False,
    sr_quality: str = "unknown",
    sr_data: Optional[Dict[str, Any]] = None,
    verdict_config: Optional[Dict[str, Any]] = None,
) -> str:
    """Return a complete, semantically structured HTML verification report."""
    title    = doc_title or "WCAG 2.1 AA Verification Report"
    date_str = datetime.now().strftime("%d %B %Y, %H:%M")
    org_line = " \u00b7 ".join(
        x for x in (_get_branding().organization, _get_branding().product_name) if x
    )

    compliance   = post_audit.get("compliance", {})
    score        = compliance.get("overall_score", 0.0)
    by_criterion = compliance.get("by_criterion", {})
    summary      = post_audit.get("summary", {})

    # Exclude AAA criteria (2.4.9, 3.2.5) from summary counts — they are
    # informational only and not part of AA conformance.
    _aa_crit      = {c: d for c, d in by_criterion.items() if _LEVEL.get(c) in ("A", "AA")}
    total_tested  = len(_aa_crit)
    n_passing     = sum(1 for d in _aa_crit.values() if d.get("passed", True))
    n_failing     = total_tested - n_passing
    needs_rem     = summary.get("needs_remediation", 0)
    compliant_cnt = summary.get("compliant", 0)
    sev           = summary.get("severity_counts", {})

    # Extract SR metrics for unified verdict.  Preserve the historical
    # fallback to the ``sr_quality`` parameter when sr_data lacks the key.
    verdict = verdict_from_sr(
        post_audit, sr_data, verdict_config, sr_quality_fallback=sr_quality,
    )
    if verdict == "pass":
        verdict_cls = "verdict-pass"
        verdict_lbl = "COMPLIANT"
        status_msg = "The document conforms to WCAG 2.1 Level AA."
        checker_msg = (
            "The checker found no verification issues. The document appears "
            "to conform to WCAG 2.1 Level AA."
        )
    else:
        verdict_cls = "verdict-review"
        verdict_lbl = (
            f"WCAG 2.1 AA — {n_passing} of {total_tested} Criteria Passing"
        )
        status_msg = (
            f"{n_failing} criteri{'on needs' if n_failing == 1 else 'a need'}"
            f" review to ensure compliance."
        ) if n_failing > 0 else "Manual review recommended."
        checker_msg = (
            "The checker identified areas for improvement. "
            "See the detailed report below for guidance."
        )

    # Summary statistics boxes
    counts = [
        ("Passed",            compliant_cnt),
        ("Failed",            needs_rem),
        ("Criteria assessed", total_tested),
    ]
    crit_c = sev.get("critical", 0)
    maj_c  = sev.get("major",    0)
    min_c  = sev.get("minor",    0)
    if crit_c or maj_c or min_c:
        counts += [
            ("Critical issues", crit_c),
            ("Major issues",    maj_c),
            ("Minor issues",    min_c),
        ]

    counts_html = " ".join(
        f'<span class="count-box">{escape(label)}: <strong>{val}</strong></span>'
        for label, val in counts
    )

    filename_row = (
        f'<p><strong>Filename:</strong> {escape(title)}</p>'
        if doc_title else ""
    )

    table_rows = _build_table_rows(by_criterion, post_audit.get("issues", []))
    # Verdict-gate findings that report without gating (reading order) are not
    # post_audit issues, so they reached no section of this page: a document
    # with a flagged reading order was told "no verification issues found".
    # This page is a written artifact the reader may forward, so it has to
    # carry the finding rather than assert a clean bill of health.
    from reporting.issue_aggregation import _verdict_gate_reasons

    advisories_section = _build_advisories_html(
        list(post_audit.get("issues", []))
        + [
            r for r in _verdict_gate_reasons(sr_data or {}, verdict_config or {})
            if r.get("remediation_status") == "informational"
        ]
    )

    viewport_meta = (
        '\n  <meta name="viewport" content="width=device-width, initial-scale=1">'
        if for_screen else ""
    )
    screen_css = _SCREEN_CSS if for_screen else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">{viewport_meta}
  <title>{escape(title)} &#x2014; WCAG 2.1 AA Verification Report</title>
  <style>{_CSS}{screen_css}</style>
</head>
<body>
  <header role="banner">
    <p class="org-name">{escape(org_line)}</p>
    <h1>WCAG 2.1 AA Verification Report</h1>
  </header>

  <main>
    <div class="meta" role="region" aria-label="Document information">
      {filename_row}
      <p><strong>Report date:</strong> {escape(date_str)}</p>
    </div>

    <section aria-labelledby="summary-heading">
      <h2 id="summary-heading">Summary</h2>

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

      <p class="checker-msg">{escape(checker_msg)}</p>

      <div class="count-grid" aria-label="Summary statistics">
        {counts_html}
      </div>
    </section>

    <section aria-labelledby="detailed-heading">
      <h2 id="detailed-heading">Detailed Report</h2>
      <p class="legend">
        Status:
        <span class="badge pass">COMPLIANT</span> All checks passing&#x2003;
        <span class="badge review">NEEDS REVIEW</span> Issues found, manual verification needed&#x2003;
        <span class="badge na">N/A</span> Not evaluated in this document
      </p>

      <table>
        <caption>WCAG 2.1 AA verification check results by category</caption>
        <thead>
          <tr>
            <th scope="col">Rule Name</th>
            <th scope="col">Status</th>
            <th scope="col">Description</th>
          </tr>
        </thead>
        <tbody>
          {table_rows}
        </tbody>
      </table>
    </section>
    {advisories_section}
  </main>

  <footer role="contentinfo">
    <p>Generated by {escape(_get_branding().product_name)} &#xb7; WCAG 2.1 Level AA</p>
  </footer>
</body>
</html>"""


def _build_advisories_html(issues: list) -> str:
    """A 'Best-practice suggestions' section for informational findings.

    Non-gating by definition: these issues do not affect the WCAG verdict,
    so they render as suggestions, never with failure styling.
    """
    advisories = [
        i for i in issues
        if i.get("remediation_status") == "informational"
    ]
    if not advisories:
        return ""
    # Gate-derived advisories carry a type but no prose, so fall back to the
    # same display name every other surface shows rather than printing a raw
    # slug at the reader.
    from reporting.constants import _ISSUE_DISPLAY_NAMES

    def _label(i: dict) -> str:
        itype = i.get("type") or ""
        return (
            i.get("description")
            or _ISSUE_DISPLAY_NAMES.get(itype, itype)
        )

    items = "".join(f"<li>{escape(_label(i))}</li>" for i in advisories)
    return (
        '<section aria-labelledby="bp-heading">'
        '<h2 id="bp-heading">Best-practice suggestions</h2>'
        "<p>These items do not affect WCAG 2.1 AA conformance.</p>"
        f"<ul>{items}</ul></section>"
    )


def _build_table_rows(by_criterion: dict, issues: list) -> str:
    """Build the <tbody> rows HTML for the detailed criteria table."""
    # Group needs-remediation issues by criterion ID
    issues_by_crit: Dict[str, list] = {}
    for iss in issues:
        if iss.get("remediation_status") == "needs_remediation":
            cid = iss.get("wcag_criterion", "")
            issues_by_crit.setdefault(cid, []).append(iss)

    rows: List[str] = []
    data_row_idx = 0  # tracks data rows only, for alternating background

    for section_name, criteria in _SECTIONS:
        rows.append(
            f'<tr class="section-header">'
            f'<th colspan="3" scope="rowgroup">{escape(section_name)}</th>'
            f'</tr>'
        )

        for crit_id, description in criteria:
            status      = _get_status(crit_id, by_criterion)
            level_tag   = _LEVEL.get(crit_id, "A")
            name        = _NAMES.get(crit_id, crit_id)
            uri         = _WCAG_URL.get(crit_id, "")
            link_text   = f"{crit_id} {level_tag}: {name}"

            # Unify fail → review: red is reserved for pipeline errors only
            if status == "fail":
                status = "review"
            badge_label = {
                "pass": "COMPLIANT", "review": "NEEDS REVIEW",
            }.get(status, "N/A")

            rule_cell = (
                f'<a href="{escape(uri)}">'
                f'{escape(link_text)}'
                f'</a>'
                if uri else escape(link_text)
            )

            alt_class = " alt-row" if data_row_idx % 2 == 0 else ""
            rows.append(
                f'<tr class="data-row{alt_class}">'
                f'<td class="rule-name">{rule_cell}</td>'
                f'<td class="status-cell"><span class="badge {status}">{badge_label}</span></td>'
                f'<td>{escape(description)}</td>'
                f'</tr>'
            )
            data_row_idx += 1

            # Inline issue details for FAIL/REVIEW rows
            if status in ("fail", "review"):
                for iss in issues_by_crit.get(crit_id, []):
                    sev_val  = iss.get("severity", "minor")
                    itype    = iss.get("type", "")
                    desc_i   = iss.get("description", "")
                    loc      = iss.get("location") or {}
                    page_num = loc.get("page_number", "")
                    page_sfx = f" (p.{page_num})" if page_num else ""
                    issue_text = f"{sev_val.upper()}: {itype} \u2014 {desc_i}{page_sfx}"

                    rows.append(
                        f'<tr class="issue-row">'
                        f'<td colspan="3">'
                        f'<span class="sev-{sev_val}" aria-hidden="true">&#x25cf;</span>'
                        f'<span class="sr-only">{escape(sev_val.capitalize())} issue:</span>'
                        f' {escape(issue_text)}'
                        f'</td>'
                        f'</tr>'
                    )

    return "\n          ".join(rows)


# ─── Utility helpers ──────────────────────────────────────────────────────────


def _get_status(crit_id: str, by_criterion: dict) -> str:
    """Return ``'pass'``, ``'fail'``, or ``'na'`` for a criterion."""
    if crit_id not in by_criterion:
        return "na"
    return "pass" if by_criterion[crit_id].get("passed", False) else "fail"


