# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for audit checks.

Each test creates minimal HTML, runs a single audit check, and asserts
the expected issues are (or aren't) reported.  No PDFs, no external
services — runs in seconds.

Run:  python -m pytest tests/test_audit_checks.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


class IssueCollector:
    """Minimal callback that collects issues from a check."""

    def __init__(self):
        self.issues: List[Dict[str, Any]] = []

    def __call__(self, issue_type, wcag_criterion, severity, **kwargs):
        self.issues.append({
            "type": issue_type,
            "wcag_criterion": wcag_criterion,
            "severity": severity,
            **kwargs,
        })

    def types(self) -> List[str]:
        return [i["type"] for i in self.issues]

    def statuses(self) -> List[str]:
        return [i.get("status", "needs_remediation") for i in self.issues]

    def needs_remediation(self) -> List[Dict[str, Any]]:
        return [i for i in self.issues if i.get("status", "needs_remediation") == "needs_remediation"]

    def compliant(self) -> List[Dict[str, Any]]:
        return [i for i in self.issues if i.get("status") == "compliant"]


def _run_check(check_class, html: str) -> IssueCollector:
    """Instantiate a check on the given HTML and run it, returning collected issues."""
    soup = _soup(html)
    collector = IssueCollector()
    check = check_class(soup, collector)
    check.check()
    return collector


# ===================================================================
# Heading checks (WCAG 1.3.1, 2.4.6)
# ===================================================================

from audit.checks.heading_checks import HeadingHierarchyCheck, HeadingContentCheck, DocumentTitleCheck


class TestHeadingHierarchyCheck:

    def test_no_headings_flags_issue(self):
        c = _run_check(HeadingHierarchyCheck, "<html><body><p>text</p></body></html>")
        assert "no-headings" in c.types()

    def test_missing_h1_flagged(self):
        c = _run_check(HeadingHierarchyCheck, "<html><body><h2>Section</h2></body></html>")
        assert "no-h1" in c.types()

    def test_no_h1_is_informational(self):
        c = _run_check(HeadingHierarchyCheck,
                       "<html><body><h2>Sub</h2></body></html>")
        issue = next(i for i in c.issues if i["type"] == "no-h1")
        assert issue["severity"] == "info"
        assert issue["status"] == "informational"
        assert "no-h1" not in {i["type"] for i in c.needs_remediation()}

    def test_skipped_level_flagged(self):
        c = _run_check(HeadingHierarchyCheck, "<html><body><h1>Title</h1><h3>Sub</h3></body></html>")
        assert "skipped-heading-level" in c.types()

    def test_correct_hierarchy_compliant(self):
        c = _run_check(HeadingHierarchyCheck, "<html><body><h1>Title</h1><h2>Section</h2><h3>Sub</h3></body></html>")
        assert "compliant-heading-hierarchy" in c.types()
        assert len(c.needs_remediation()) == 0

    def test_skip_is_judged_by_level_set_not_document_order(self):
        """Headings are collected by level, so a skip means a level is absent
        document-wide — h1,h3,h2 is fine because an h2 exists somewhere.

        close_html_heading_level_gaps mirrors these semantics; if this check
        ever moves to document order, that helper stops matching it and
        promotion can strand deeper headings again.
        """
        out_of_order = _run_check(
            HeadingHierarchyCheck,
            "<html><body><h1>A</h1><h3>C</h3><h2>B</h2></body></html>")
        assert "skipped-heading-level" not in out_of_order.types()

        missing_rung = _run_check(
            HeadingHierarchyCheck,
            "<html><body><h1>A</h1><h3>B</h3><h3>C</h3></body></html>")
        assert "skipped-heading-level" in missing_rung.types()

    def test_h1_only_is_compliant(self):
        c = _run_check(HeadingHierarchyCheck, "<html><body><h1>Title</h1></body></html>")
        assert "compliant-heading-hierarchy" in c.types()

    def test_multiple_h1_no_skip_is_compliant(self):
        c = _run_check(HeadingHierarchyCheck, "<html><body><h1>A</h1><h1>B</h1></body></html>")
        assert "compliant-heading-hierarchy" in c.types()

    def test_h2_to_h4_skip_flagged(self):
        c = _run_check(HeadingHierarchyCheck, "<html><body><h1>T</h1><h2>S</h2><h4>D</h4></body></html>")
        assert "skipped-heading-level" in c.types()


class TestHeadingContentCheck:

    def test_empty_heading_flagged(self):
        c = _run_check(HeadingContentCheck, "<html><body><h2></h2></body></html>")
        assert any("empty" in t for t in c.types())

    def test_whitespace_only_heading_flagged(self):
        c = _run_check(HeadingContentCheck, "<html><body><h2>   </h2></body></html>")
        assert any("empty" in t or "whitespace" in t for t in c.types())

    def test_heading_with_text_compliant(self):
        c = _run_check(HeadingContentCheck, "<html><body><h2>Section Title</h2></body></html>")
        assert len(c.needs_remediation()) == 0


class TestDocumentTitleCheck:

    def test_missing_title_flagged(self):
        c = _run_check(DocumentTitleCheck, "<html><head></head><body><p>text</p></body></html>")
        assert any("title" in t for t in c.types())
        assert len(c.needs_remediation()) > 0

    def test_empty_title_flagged(self):
        c = _run_check(DocumentTitleCheck, "<html><head><title></title></head><body></body></html>")
        assert len(c.needs_remediation()) > 0

    def test_valid_title_compliant(self):
        c = _run_check(DocumentTitleCheck, "<html><head><title>My Report</title></head><body></body></html>")
        assert len(c.needs_remediation()) == 0


# ===================================================================
# Document structure checks (WCAG 3.1.1, 1.3.1, 2.4.1)
# ===================================================================

from audit.checks.structure_checks import DocumentLanguageCheck, MainLandmarkCheck, SkipLinkCheck


class TestDocumentLanguageCheck:

    def test_missing_lang_flagged(self):
        c = _run_check(DocumentLanguageCheck, "<html><body><p>text</p></body></html>")
        assert "missing-document-language" in c.types()

    def test_empty_lang_flagged(self):
        c = _run_check(DocumentLanguageCheck, '<html lang=""><body></body></html>')
        assert any("invalid" in t for t in c.types())

    def test_short_lang_flagged(self):
        c = _run_check(DocumentLanguageCheck, '<html lang="x"><body></body></html>')
        assert any("invalid" in t for t in c.types())

    def test_valid_lang_compliant(self):
        c = _run_check(DocumentLanguageCheck, '<html lang="en"><body></body></html>')
        assert "compliant-document-language" in c.types()

    def test_lang_fr_compliant(self):
        c = _run_check(DocumentLanguageCheck, '<html lang="fr"><body></body></html>')
        assert "compliant-document-language" in c.types()


class TestMainLandmarkCheck:

    def test_missing_main_flagged(self):
        c = _run_check(MainLandmarkCheck, "<html><body><p>text</p></body></html>")
        assert "missing-main-landmark" in c.types()

    def test_main_element_compliant(self):
        c = _run_check(MainLandmarkCheck, "<html><body><main>content</main></body></html>")
        assert "compliant-main-landmark" in c.types()

    def test_role_main_compliant(self):
        c = _run_check(MainLandmarkCheck, '<html><body><div role="main">content</div></body></html>')
        assert "compliant-main-landmark" in c.types()


class TestSkipLinkCheck:

    def test_no_skip_link_flagged(self):
        c = _run_check(SkipLinkCheck, "<html><body><nav>nav</nav><main>content</main></body></html>")
        assert any("skip" in t for t in c.types())

    def test_skip_link_present_compliant(self):
        html = '<html><body><a href="#main-content" class="skip-link">Skip to content</a><main id="main-content">text</main></body></html>'
        c = _run_check(SkipLinkCheck, html)
        compliant = [t for t in c.types() if "compliant" in t]
        assert len(compliant) > 0 or len(c.needs_remediation()) == 0


# ===================================================================
# Image checks (WCAG 1.1.1)
# ===================================================================

from audit.checks.image_checks import AltTextCheck


class TestAltTextCheck:

    def test_missing_alt_flagged(self):
        c = _run_check(AltTextCheck, '<html><body><img src="photo.jpg"></body></html>')
        assert "missing-alt-text" in c.types()

    def test_empty_alt_not_decorative_flagged(self):
        c = _run_check(AltTextCheck, '<html><body><img src="photo.jpg" alt=""></body></html>')
        assert "empty-alt-text" in c.types()

    def test_decorative_image_compliant(self):
        c = _run_check(AltTextCheck, '<html><body><img src="spacer.gif" alt="" role="presentation"></body></html>')
        assert "compliant-decorative-image" in c.types()

    def test_valid_alt_compliant(self):
        c = _run_check(AltTextCheck, '<html><body><img src="photo.jpg" alt="Team photo at retreat"></body></html>')
        assert len(c.needs_remediation()) == 0

    def test_generic_alt_flagged(self):
        c = _run_check(AltTextCheck, '<html><body><img src="chart.png" alt="image"></body></html>')
        assert any("generic" in t for t in c.types())

    def test_no_images_no_issues(self):
        c = _run_check(AltTextCheck, "<html><body><p>No images here</p></body></html>")
        assert len(c.issues) == 0

    def test_figcaption_covers_empty_alt(self):
        html = '<html><body><figure><img src="x.png" alt=""><figcaption>Chart description</figcaption></figure></body></html>'
        c = _run_check(AltTextCheck, html)
        assert "empty-alt-text" not in c.types()


# ===================================================================
# Table checks (WCAG 1.3.1)
# ===================================================================

from audit.checks.table_checks import TableHeaderCheck, TableStructureCheck, TableRegularityCheck


class TestTableHeaderCheck:

    def test_no_headers_flagged(self):
        html = "<html><body><table><tr><td>A</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></table></body></html>"
        c = _run_check(TableHeaderCheck, html)
        assert "table-missing-headers" in c.types()

    def test_has_headers_compliant(self):
        html = "<html><body><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table></body></html>"
        c = _run_check(TableHeaderCheck, html)
        assert "table-missing-headers" not in c.types()

    def test_layout_table_skipped(self):
        html = '<html><body><table role="presentation"><tr><td>layout</td></tr></table></body></html>'
        c = _run_check(TableHeaderCheck, html)
        assert "table-missing-headers" not in c.types()

    def test_toc_table_skipped(self):
        html = '<html><body><table data-table-type="toc"><tr><td>Chapter 1</td><td>3</td></tr></table></body></html>'
        c = _run_check(TableHeaderCheck, html)
        assert "table-missing-headers" not in c.types()

    def test_no_scope_flagged(self):
        html = "<html><body><table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table></body></html>"
        c = _run_check(TableHeaderCheck, html)
        assert any("scope" in t for t in c.types())

    def test_scope_present_compliant(self):
        html = '<html><body><table><tr><th scope="col">A</th><th scope="col">B</th></tr><tr><td>1</td><td>2</td></tr></table></body></html>'
        c = _run_check(TableHeaderCheck, html)
        scope_issues = [t for t in c.types() if "scope" in t and "compliant" not in t]
        assert len(scope_issues) == 0


class TestTableStructureCheck:

    def test_missing_thead_flagged(self):
        html = "<html><body><table><tr><th>A</th></tr><tr><td>1</td></tr></table></body></html>"
        c = _run_check(TableStructureCheck, html)
        assert any("thead" in t for t in c.types())

    def test_has_thead_compliant(self):
        html = "<html><body><table><thead><tr><th>A</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table></body></html>"
        c = _run_check(TableStructureCheck, html)
        thead_issues = [i for i in c.needs_remediation() if "thead" in i["type"]]
        assert len(thead_issues) == 0

    def test_missing_caption_on_complex_table_flagged(self):
        """Only complex tables (rowspan/colspan/multi-header/10+ rows) trigger missing caption.
        Note: caption check lives in TableHeaderCheck, not TableStructureCheck."""
        html = """<html><body><table>
            <tr><th colspan="2">Full Name</th><th>Age</th></tr>
            <tr><td>First</td><td>Last</td><td>30</td></tr>
        </table></body></html>"""
        c = _run_check(TableHeaderCheck, html)
        assert any("caption" in t for t in c.types())


class TestTableRegularityCheck:

    def test_regular_table_no_issues(self):
        html = """<html><body><table>
            <tr><th>A</th><th>B</th></tr>
            <tr><td>1</td><td>2</td></tr>
            <tr><td>3</td><td>4</td></tr>
        </table></body></html>"""
        c = _run_check(TableRegularityCheck, html)
        irregularity_issues = [i for i in c.needs_remediation() if "irregular" in i["type"]]
        assert len(irregularity_issues) == 0

    def test_irregular_column_count_flagged(self):
        html = """<html><body><table>
            <tr><td>A</td><td>B</td><td>C</td></tr>
            <tr><td>1</td><td>2</td></tr>
        </table></body></html>"""
        c = _run_check(TableRegularityCheck, html)
        assert any("irregular" in t for t in c.types())


# ===================================================================
# Link checks (WCAG 2.4.4)
# ===================================================================

from audit.checks.link_checks import LinkTextCheck


class TestLinkTextCheck:

    def test_empty_link_flagged(self):
        c = _run_check(LinkTextCheck, '<html><body><a href="https://example.com"></a></body></html>')
        assert "empty-link-text" in c.types()

    def test_generic_link_text_flagged(self):
        c = _run_check(LinkTextCheck, '<html><body><a href="https://example.com">click here</a></body></html>')
        assert "generic-link-text" in c.types()

    def test_descriptive_link_compliant(self):
        c = _run_check(LinkTextCheck, '<html><body><a href="/policy">Privacy Policy</a></body></html>')
        assert "empty-link-text" not in c.types()
        assert "generic-link-text" not in c.types()

    def test_image_link_with_alt_compliant(self):
        html = '<html><body><a href="/home"><img src="logo.png" alt="Home page"></a></body></html>'
        c = _run_check(LinkTextCheck, html)
        assert "empty-link-text" not in c.types()

    def test_anchor_link_skipped(self):
        """Internal fragment links (#) should not be flagged."""
        c = _run_check(LinkTextCheck, '<html><body><a href="#">anchor</a></body></html>')
        assert "empty-link-text" not in c.types()

    def test_bare_url_text_flagged(self):
        c = _run_check(LinkTextCheck, '<html><body><a href="https://x.edu">https://x.edu</a></body></html>')
        assert "url-as-link-text" in c.types()

    def test_url_text_with_descriptive_aria_label_compliant(self):
        # A descriptive aria-label that happens to contain a domain (ends in .edu)
        # must NOT be treated as "still a URL" — it's a multi-word phrase.
        html = ('<html><body><a href="https://portal.example.edu" '
                'aria-label="Link to the example.edu website">https://portal.example.edu</a></body></html>')
        c = _run_check(LinkTextCheck, html)
        assert "url-as-link-text" not in c.types()
        assert "compliant-link-text" in c.types()

    def test_is_url_precision(self):
        from audit.checks.link_checks import LinkTextCheck as LC
        chk = LC(__import__("bs4").BeautifulSoup("<a></a>", "html.parser"), lambda *a, **k: None)
        # true bare URLs (single token)
        assert chk._is_url("https://x.edu")
        assert chk._is_url("www.example.com")
        assert chk._is_url("example.org")
        # descriptive phrases that merely contain a domain are NOT URLs
        assert not chk._is_url("Link to the example.edu website")
        assert not chk._is_url("Visit www.example.com for details")


# ===================================================================
# Form checks (WCAG 1.3.1, 3.3.2)
# ===================================================================

from audit.checks.form_checks import FormLabelCheck


class TestFormLabelCheck:

    def test_input_without_label_flagged(self):
        html = '<html><body><form><input type="text" name="email"></form></body></html>'
        c = _run_check(FormLabelCheck, html)
        assert len(c.needs_remediation()) > 0

    def test_input_with_label_compliant(self):
        html = '<html><body><form><label for="email">Email</label><input type="text" id="email"></form></body></html>'
        c = _run_check(FormLabelCheck, html)
        label_issues = [i for i in c.needs_remediation() if "label" in i["type"]]
        assert len(label_issues) == 0

    def test_input_with_aria_label_compliant(self):
        html = '<html><body><form><input type="text" aria-label="Email address"></form></body></html>'
        c = _run_check(FormLabelCheck, html)
        label_issues = [i for i in c.needs_remediation() if "label" in i["type"]]
        assert len(label_issues) == 0

    def test_hidden_input_skipped(self):
        html = '<html><body><form><input type="hidden" name="csrf"></form></body></html>'
        c = _run_check(FormLabelCheck, html)
        assert len(c.needs_remediation()) == 0


# ===================================================================
# Formula checks (WCAG 1.1.1)
# ===================================================================

from audit.checks.formula_checks import FormulaAltTextCheck


class TestFormulaAltTextCheck:

    def test_role_math_without_label_flagged(self):
        """Element with role=math but no aria-label → needs remediation."""
        html = '<html><body><span role="math">E = mc²</span></body></html>'
        c = _run_check(FormulaAltTextCheck, html)
        assert "formula-missing-alt-text" in c.types()

    def test_standalone_math_without_alttext_flagged(self):
        """Standalone <math> without alttext → needs remediation."""
        html = '<html><body><math><mi>x</mi><mo>=</mo><mn>2</mn></math></body></html>'
        c = _run_check(FormulaAltTextCheck, html)
        assert "formula-missing-description" in c.types()

    def test_role_math_with_label_compliant(self):
        html = '<html><body><span role="math" aria-label="Energy equals mass times speed of light squared">E = mc²</span></body></html>'
        c = _run_check(FormulaAltTextCheck, html)
        assert "compliant-formula-alt-text" in c.types()
        assert len(c.needs_remediation()) == 0

    def test_math_with_alttext_compliant(self):
        html = '<html><body><math alttext="x equals 2"><mi>x</mi><mo>=</mo><mn>2</mn></math></body></html>'
        c = _run_check(FormulaAltTextCheck, html)
        assert "compliant-formula-alt-text" in c.types()
        assert len(c.needs_remediation()) == 0

    def test_generic_label_flagged(self):
        html = '<html><body><span role="math" aria-label="formula">E = mc²</span></body></html>'
        c = _run_check(FormulaAltTextCheck, html)
        assert "formula-generic-alt-text" in c.types()

    def test_no_formulas_no_issues(self):
        c = _run_check(FormulaAltTextCheck, "<html><body><p>No formulas</p></body></html>")
        assert len(c.issues) == 0


# ===================================================================
# Cross-check consistency tests
# ===================================================================

class TestAuditCheckConsistency:
    """Verify that checks produce expected issue shapes."""

    def test_all_issues_have_required_fields(self):
        """Every issue from every check must have type, criterion, and severity."""
        html = """<html><body>
            <h1>Title</h1><h3>Skipped</h3>
            <img src="x.png">
            <table><tr><td>A</td></tr></table>
            <a href="https://example.com"></a>
        </body></html>"""

        all_checks = [
            HeadingHierarchyCheck, HeadingContentCheck, DocumentLanguageCheck,
            MainLandmarkCheck, AltTextCheck, TableHeaderCheck, LinkTextCheck,
        ]

        soup = _soup(html)
        collector = IssueCollector()
        for check_class in all_checks:
            check = check_class(soup, collector)
            check.check()

        assert len(collector.issues) > 0, "Expected some issues from problematic HTML"
        for issue in collector.issues:
            assert "type" in issue, f"Issue missing 'type': {issue}"
            assert "wcag_criterion" in issue, f"Issue missing 'wcag_criterion': {issue}"
            assert "severity" in issue, f"Issue missing 'severity': {issue}"

    def test_clean_html_produces_no_remediation_issues(self):
        """Well-formed accessible HTML should not trigger remediation issues."""
        html = """<html lang="en">
        <head><title>Accessible Document</title></head>
        <body>
            <a href="#main" class="skip-link">Skip to content</a>
            <header><nav><a href="/">Home</a></nav></header>
            <main id="main">
                <h1>Welcome</h1>
                <h2>Section One</h2>
                <p>Some content here.</p>
                <figure>
                    <img src="chart.png" alt="Revenue chart showing Q1 growth">
                    <figcaption>Figure 1: Revenue chart</figcaption>
                </figure>
                <table>
                    <caption>Quarterly Results</caption>
                    <thead><tr><th scope="col">Quarter</th><th scope="col">Revenue</th></tr></thead>
                    <tbody><tr><td>Q1</td><td>$1M</td></tr></tbody>
                </table>
            </main>
            <footer><p>Footer info</p></footer>
        </body></html>"""

        critical_checks = [
            HeadingHierarchyCheck, DocumentLanguageCheck, MainLandmarkCheck,
            AltTextCheck, TableHeaderCheck, TableStructureCheck,
        ]

        soup = _soup(html)
        collector = IssueCollector()
        for check_class in critical_checks:
            check = check_class(soup, collector)
            check.check()

        remediation_needed = collector.needs_remediation()
        if remediation_needed:
            types = [i["type"] for i in remediation_needed]
            assert False, f"Clean HTML should not need remediation, got: {types}"


# ===================================================================
# Pipe-delimited tabular data check (WCAG 1.3.1)
# ===================================================================

from audit.checks.table_checks import PipeDelimitedDataCheck


class TestPipeDelimitedDataCheck:

    def test_flags_run_of_pipe_pairs(self):
        html = (
            "<html><body><div>"
            "<p>BUS 248|Business Communications</p>"
            "<p>ENG 111|English</p>"
            "</div></body></html>"
        )
        c = _run_check(PipeDelimitedDataCheck, html)
        assert any(i["type"] == "pipe-delimited-tabular-data" for i in c.issues)
        flagged = [i for i in c.issues if i["type"] == "pipe-delimited-tabular-data"]
        assert flagged[0]["wcag_criterion"] == "1.3.1"
        assert flagged[0]["severity"] == "major"
        assert flagged[0].get("status") == "needs_remediation"

    def test_no_flag_when_already_a_table(self):
        html = (
            "<html><body><table><tbody>"
            "<tr><td>BUS 248</td><td>Business Communications</td></tr>"
            "</tbody></table></body></html>"
        )
        c = _run_check(PipeDelimitedDataCheck, html)
        assert not any(i["type"] == "pipe-delimited-tabular-data" for i in c.issues)

    def test_no_flag_for_prose_with_incidental_pipe(self):
        html = "<html><body><p>This is prose | with a pipe in the middle here.</p></body></html>"
        c = _run_check(PipeDelimitedDataCheck, html)
        assert not any(i["type"] == "pipe-delimited-tabular-data" for i in c.issues)


# ===================================================================
# Filename-like title / heading detection (WCAG 2.4.2 / 2.4.6)
# ===================================================================

class TestFilenameTitleDetection:

    def test_filename_title_flagged(self):
        html = (
            "<html><head><title>example_cc_gs_course_guide_24-25</title></head>"
            "<body><h1>Real Heading</h1></body></html>"
        )
        c = _run_check(DocumentTitleCheck, html)
        assert any(i["type"] == "filename-like-title" for i in c.issues)
        flagged = [i for i in c.issues if i["type"] == "filename-like-title"][0]
        assert flagged["wcag_criterion"] == "2.4.2"
        assert flagged.get("status") == "needs_remediation"
        assert flagged["severity"] == "major"

    def test_descriptive_title_not_flagged(self):
        html = (
            "<html><head><title>Annual Report 2024</title></head>"
            "<body><h1>Annual Report 2024</h1></body></html>"
        )
        c = _run_check(DocumentTitleCheck, html)
        assert not any(i["type"] == "filename-like-title" for i in c.issues)

    def test_filename_h1_flagged_by_content_check(self):
        html = "<html><body><h1>course_guide_24_25</h1><p>x</p></body></html>"
        c = _run_check(HeadingContentCheck, html)
        assert any(i["type"] == "filename-like-heading" for i in c.issues)
        flagged = [i for i in c.issues if i["type"] == "filename-like-heading"][0]
        assert flagged["wcag_criterion"] == "2.4.6"
        assert flagged["severity"] == "major"


# ===================================================================
# Flat heading nesting advisory (WCAG 1.3.1, non-gating)
# ===================================================================

from audit.checks.heading_checks import FlatHeadingNestingCheck


class TestFlatHeadingNestingCheck:

    def test_flags_flat_anchors_as_informational(self):
        html = (
            "<html><body>"
            "<h2>Level I - Foundations</h2>"
            "<h2>Writing (WR)</h2>"
            "<h2>Level II - Exploration</h2>"
            "<h2>Personal Wellness (PW)</h2>"
            "</body></html>"
        )
        c = _run_check(FlatHeadingNestingCheck, html)
        flagged = [i for i in c.issues if i["type"] == "flat-heading-nesting"]
        assert len(flagged) == 1
        assert flagged[0]["wcag_criterion"] == "1.3.1"
        assert flagged[0].get("status") == "informational"
        assert flagged[0]["severity"] == "info"

    def test_no_flag_without_anchor_pattern(self):
        html = "<html><body><h2>Alpha</h2><h2>Beta</h2></body></html>"
        c = _run_check(FlatHeadingNestingCheck, html)
        assert not any(i["type"] == "flat-heading-nesting" for i in c.issues)

    def test_no_flag_when_already_nested(self):
        html = (
            "<html><body>"
            "<h2>Level I - Foundations</h2><h3>Writing (WR)</h3>"
            "<h2>Level II - Exploration</h2><h3>Personal Wellness (PW)</h3>"
            "</body></html>"
        )
        c = _run_check(FlatHeadingNestingCheck, html)
        assert not any(i["type"] == "flat-heading-nesting" for i in c.issues)


# ===================================================================
# Color contrast checks (WCAG 1.4.3) — PDF-based
#
# These exercise the PyMuPDF span/background path of ColorContrastCheck,
# which the HTML-only _run_check helper can't reach.  They guard against
# the false white-on-white positive for light text in a colored box.
# ===================================================================

import pytest

fitz = pytest.importorskip("fitz")

from audit.checks.color_contrast_checks import ColorContrastCheck


def _glyph_bbox(page):
    """Return the bbox of the first text span on a page."""
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            for s in ln["spans"]:
                return s["bbox"]
    raise AssertionError("no text span found on page")


def _run_contrast(page) -> IssueCollector:
    """Run ColorContrastCheck against a fitz page (empty HTML soup)."""
    collector = IssueCollector()
    ColorContrastCheck(_soup("<html><body></body></html>"), collector,
                       pdf_page=page).check()
    return collector


class TestColorContrastPdf:

    def test_white_text_in_opaque_colored_box_not_flagged(self):
        """White text inside a box that fully contains it → compliant."""
        doc = fitz.open()
        p = doc.new_page(width=400, height=140)
        p.draw_rect(fitz.Rect(10, 10, 390, 70), fill=(0.15, 0.3, 0.7))
        p.insert_text((30, 45), "Section Heading", color=(1, 1, 1), fontsize=16)
        c = _run_contrast(p)
        assert "insufficient-color-contrast" not in c.types()
        assert "compliant-color-contrast" in c.types()

    def test_white_text_in_tight_banner_not_flagged(self):
        """Regression: tight colored banner whose glyph bbox overflows the
        box used to be reported as #FFFFFF on #FFFFFF (1:1)."""
        doc = fitz.open()
        p = doc.new_page(width=400, height=140)
        p.insert_text((30, 45), "Section Heading", color=(1, 1, 1), fontsize=16)
        x0, y0, x1, y1 = _glyph_bbox(p)
        p.draw_rect(fitz.Rect(x0 - 2, y0 + 1, x1 + 2, y1 - 1), fill=(0.15, 0.3, 0.7))
        c = _run_contrast(p)
        assert "insufficient-color-contrast" not in c.types()

    def test_white_text_no_background_suppressed(self):
        """White text with no detectable background → undetermined →
        suppressed (visible white glyphs cannot be white-on-white)."""
        doc = fitz.open()
        p = doc.new_page(width=400, height=140)
        p.insert_text((30, 45), "Floating White", color=(1, 1, 1), fontsize=16)
        c = _run_contrast(p)
        assert "insufficient-color-contrast" not in c.types()

    def test_dark_text_no_background_not_flagged(self):
        """Dark text on undetermined background → assumed white → high
        contrast → not flagged (and no real failure suppressed)."""
        doc = fitz.open()
        p = doc.new_page(width=400, height=140)
        p.insert_text((30, 45), "Dark Text", color=(0, 0, 0), fontsize=16)
        c = _run_contrast(p)
        assert "insufficient-color-contrast" not in c.types()

    def test_true_low_contrast_still_flagged(self):
        """Light-gray text on a measured light box must STILL be flagged —
        the suppression must not over-suppress genuine failures."""
        doc = fitz.open()
        p = doc.new_page(width=400, height=140)
        p.draw_rect(fitz.Rect(10, 10, 390, 70), fill=(0.95, 0.95, 0.95))
        p.insert_text((30, 45), "Low Contrast", color=(0.7, 0.7, 0.7), fontsize=16)
        c = _run_contrast(p)
        assert "insufficient-color-contrast" in c.types()


class TestAntialiasedGrayPredicate:
    """Unit tests for the anti-aliasing-gray discriminator.

    Small glyphs sampled from a rendered pixmap can yield a neutral-gray
    'background' that is really the anti-aliased edge of dark text on a white
    page (e.g. a 7pt digit → sampled (105,105,105)).  The predicate identifies
    that signature so the sampler can substitute the true white background,
    eliminating false 1.4.3 failures while preserving genuine ones.
    """

    def _pred(self):
        from audit.checks.color_contrast_checks import _is_antialiased_gray
        return _is_antialiased_gray

    def test_black_glyph_aa_gray_detected(self):
        """Black text sampled as mid-gray → AA of black-on-white."""
        assert self._pred()((105, 105, 105), (0, 0, 0)) is True

    def test_dark_gray_glyph_aa_gray_detected(self):
        """#404040 text sampled as (167,167,167) → AA signature."""
        assert self._pred()((167, 167, 167), (64, 64, 64)) is True

    def test_white_background_not_aa(self):
        """A genuinely white sampled background is not the AA signature."""
        assert self._pred()((255, 255, 255), (0, 0, 0)) is False

    def test_near_white_not_aa(self):
        """Near-white (>= 0.98 luminance) gray text bg stays a real finding."""
        # gray #7f7f7f on near-white must remain a genuine low-contrast fail
        assert self._pred()((254, 254, 254), (127, 127, 127)) is False

    def test_colored_background_not_aa(self):
        """A saturated (non-neutral) background is a real color, not AA."""
        assert self._pred()((243, 117, 86), (255, 255, 255)) is False  # orange banner
        assert self._pred()((81, 43, 27), (126, 126, 126)) is False    # brown box

    def test_bg_darker_than_fg_not_aa(self):
        """bg must sit between fg luminance and white; darker bg is not AA."""
        assert self._pred()((50, 50, 50), (200, 200, 200)) is False
