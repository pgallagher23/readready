# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for remediation strategies.

Each test creates minimal HTML, runs a single strategy function, and asserts
the expected DOM mutation.  No PDFs, no Docling, no VLM — runs in seconds.

Run:  python -m pytest tests/test_remediation_strategies.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup
from typing import Dict, Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _issue(issue_type: str = "", **kwargs) -> Dict[str, Any]:
    """Build a minimal audit-issue dict."""
    base = {
        "type": issue_type,
        "wcag_criterion": "1.3.1",
        "severity": "major",
        "description": "",
        "context": {},
        "location": {},
        "remediation_status": "needs_remediation",
    }
    base.update(kwargs)
    return base


# ===================================================================
# Document structure strategies
# ===================================================================

from remediate.strategies.document_structure_remediation import (
    remediate_missing_document_title,
    remediate_missing_language,
)


class TestMissingDocumentTitle:
    """remediate_missing_document_title — WCAG 2.4.2"""

    def test_adds_title_when_absent(self):
        soup = _soup("<html><head></head><body><h1>My Report</h1><p>text</p></body></html>")
        result = remediate_missing_document_title(soup, _issue("missing-document-title"))
        assert result is not None
        title = soup.find("title")
        assert title is not None
        assert title.get_text(strip=True) != ""

    def test_no_op_when_title_present(self):
        soup = _soup("<html><head><title>Existing Title</title></head><body><p>text</p></body></html>")
        result = remediate_missing_document_title(soup, _issue("missing-document-title"))
        assert result is None
        assert soup.find("title").get_text() == "Existing Title"


class TestMissingLanguage:
    """remediate_missing_language — WCAG 3.1.1"""

    def test_adds_lang_when_absent(self):
        soup = _soup("<html><body><p>Hello</p></body></html>")
        result = remediate_missing_language(soup, _issue("missing-language"))
        assert result is not None
        assert soup.find("html")["lang"] == "en"

    def test_no_op_when_lang_present(self):
        soup = _soup('<html lang="fr"><body><p>Bonjour</p></body></html>')
        result = remediate_missing_language(soup, _issue("missing-language"))
        assert result is None
        assert soup.find("html")["lang"] == "fr"

    def test_no_op_when_no_html_tag(self):
        soup = _soup("<p>bare fragment</p>")
        result = remediate_missing_language(soup, _issue("missing-language"))
        assert result is None


# ===================================================================
# Heading strategies
# ===================================================================

from remediate.strategies.heading_remediation import (
    remediate_missing_h1,
    remediate_missing_headings,
    remediate_skipped_heading_level,
    remediate_empty_heading_content,
)


class TestRemediateMissingH1:
    """remediate_missing_h1 — retag-only promotion, never insertion."""

    def test_promotes_title_matching_subheading(self):
        soup = BeautifulSoup(
            "<html><head><title>Mammals</title></head><body>"
            "<h2>Mammals</h2><p>Body text.</p></body></html>", "html.parser")
        result = remediate_missing_h1(soup, _issue("no-h1"))
        assert result and "Mammals" in result
        assert soup.find("h1").get_text() == "Mammals"
        assert soup.find("h2") is None  # retagged, not duplicated

    def test_promotes_first_subheading_when_title_matches_nothing(self):
        soup = BeautifulSoup(
            "<html><head><title>zzz_no_match.pdf</title></head><body>"
            "<h2>AGENDA</h2><h2>Minutes</h2></body></html>", "html.parser")
        result = remediate_missing_h1(soup, _issue("no-h1"))
        assert result and soup.find("h1").get_text() == "AGENDA"

    def test_promotes_second_subheading_when_first_is_a_bare_number(self):
        soup = BeautifulSoup(
            "<html><head><title>4</title></head><body>"
            "<h2>4</h2><h2>Terrorism</h2></body></html>", "html.parser")
        result = remediate_missing_h1(soup, _issue("no-h1"))
        assert result and soup.find("h1").get_text() == "Terrorism"

    def test_zero_heading_doc_promotes_first_usable_paragraph(self):
        soup = BeautifulSoup(
            "<html><head><title></title></head><body>"
            "<p>/gid00005/gid00032</p><p>Purple Loosestrife Summary</p>"
            "</body></html>", "html.parser")
        result = remediate_missing_h1(soup, _issue("no-h1"))
        assert result and soup.find("h1").get_text() == "Purple Loosestrife Summary"

    def test_window_skips_landmark_containers(self):
        soup = BeautifulSoup(
            "<html><body><footer><p>Document converted for accessibility "
            "compliance.</p></footer><p>Real first text</p></body></html>",
            "html.parser")
        remediate_missing_h1(soup, _issue("no-h1"))
        h1 = soup.find("h1")
        assert h1 is not None and h1.get_text() == "Real first text"

    def test_never_inserts_text(self):
        html = ("<html><head><title>Great Title</title></head><body>"
                "<footer><p>x</p></footer></body></html>")
        soup = BeautifulSoup(html, "html.parser")
        before = soup.get_text()
        result = remediate_missing_h1(soup, _issue("no-h1"))
        assert result is None            # nothing promotable on the page
        assert soup.find("h1") is None   # and nothing was written in
        assert soup.get_text() == before

    def test_noop_when_h1_exists(self):
        soup = BeautifulSoup("<html><body><h1>Here</h1></body></html>", "html.parser")
        assert remediate_missing_h1(soup, _issue("no-h1")) is None

    def test_kill_switch_via_options(self):
        soup = BeautifulSoup(
            "<html><head><title></title></head><body><p>7 April 2014</p>"
            "</body></html>", "html.parser")
        opts = {"conversion": {"heading_promote_first_text": False}}
        assert remediate_missing_h1(soup, _issue("no-h1"), opts) is None

    def test_promotion_does_not_strand_deeper_headings(self):
        """Retagging the lone h2 must not leave the h3s behind a gap.

        HeadingHierarchyCheck collects headings by level, so {h1, h3} reads as
        a skipped level and turns an otherwise-clean document into a major
        1.3.1 failure — caused by the promotion itself.
        """
        from audit.checks.heading_checks import HeadingHierarchyCheck

        soup = BeautifulSoup(
            "<html><head><title>Annual Report</title></head><body>"
            "<h2>Annual Report</h2><h3>Section A</h3><h3>Section B</h3>"
            "</body></html>", "html.parser")
        assert remediate_missing_h1(soup, _issue("no-h1")) is not None
        assert [h.name for h in soup.find_all(["h1", "h2", "h3"])] == [
            "h1", "h2", "h2"]

        found = []
        HeadingHierarchyCheck(soup, lambda t, *a, **k: found.append(t)).check()
        assert "skipped-heading-level" not in found

    def test_promotion_keeps_hierarchy_when_no_gap_is_created(self):
        """A surviving h2 means no rung is emptied — leave the ladder alone."""
        soup = BeautifulSoup(
            "<html><head><title>Annual Report</title></head><body>"
            "<h2>Annual Report</h2><h2>Second</h2><h3>Sub</h3>"
            "</body></html>", "html.parser")
        remediate_missing_h1(soup, _issue("no-h1"))
        assert [h.name for h in soup.find_all(["h1", "h2", "h3"])] == [
            "h1", "h2", "h3"]

    def test_abstention_never_relevels(self):
        """No promotion, no renumbering: an untouched document keeps its h2s
        and must not acquire an h1 by renaming."""
        soup = BeautifulSoup(
            "<html><head><title>Date: ______</title></head><body>"
            "<h2>_____</h2><h4>____</h4></body></html>", "html.parser")
        before = str(soup)
        assert remediate_missing_h1(soup, _issue("no-h1")) is None
        assert str(soup) == before

    def test_master_switch_blocks_zero_heading_promotion(self):
        """detect_document_heading=False must stop the HTML path too.

        config.yaml documents false as restoring the previous behaviour —
        "such documents get no heading". Before this gate the flag reached
        the strategy and was ignored, so the promise held only for the
        converter (structure JSON) path.
        """
        soup = BeautifulSoup(
            "<html><head><title></title></head><body>"
            "<p>Purple Loosestrife Summary</p></body></html>", "html.parser")
        opts = {"conversion": {"detect_document_heading": False}}
        assert remediate_missing_h1(soup, _issue("no-h1"), opts) is None
        assert soup.find("h1") is None

    def test_master_switch_blocks_subheading_promotion(self):
        """Population A (retag an existing h2) honours the master switch."""
        soup = BeautifulSoup(
            "<html><head><title>Annual Report</title></head><body>"
            "<h2>Annual Report</h2><p>Body</p></body></html>", "html.parser")
        opts = {"conversion": {"detect_document_heading": False}}
        assert remediate_missing_h1(soup, _issue("no-h1"), opts) is None
        assert soup.find("h1") is None
        assert soup.find("h2") is not None  # the author's heading is untouched

    def test_subheading_switch_blocks_the_h2_retag(self):
        """heading_promote_subheading=False leaves existing sub-headings alone."""
        soup = BeautifulSoup(
            "<html><head><title>Annual Report</title></head><body>"
            "<h2>Annual Report</h2><p>Body</p></body></html>", "html.parser")
        opts = {"conversion": {"heading_promote_subheading": False}}
        assert remediate_missing_h1(soup, _issue("no-h1"), opts) is None
        assert soup.find("h2") is not None

    def test_subheading_switch_leaves_zero_heading_promotion_enabled(self):
        """The Population A switch is scoped: zero-heading docs still promote."""
        soup = BeautifulSoup(
            "<html><head><title></title></head><body>"
            "<p>Purple Loosestrife Summary</p></body></html>", "html.parser")
        opts = {"conversion": {"heading_promote_subheading": False}}
        assert remediate_missing_h1(soup, _issue("no-h1"), opts) is not None
        assert soup.find("h1").get_text() == "Purple Loosestrife Summary"


class TestSkippedHeadingLevel:
    """remediate_skipped_heading_level — WCAG 1.3.1"""

    def test_inserts_intermediate_heading(self):
        soup = _soup("<html><body><h1>Title</h1><h3>Subsection</h3></body></html>")
        result = remediate_skipped_heading_level(soup, _issue("skipped-heading-level"))
        assert result is not None
        # Should have inserted an h2 between h1 and h3
        headings = [h.name for h in soup.find_all(["h1", "h2", "h3"])]
        assert headings == ["h1", "h2", "h3"]

    def test_no_op_when_hierarchy_correct(self):
        soup = _soup("<html><body><h1>Title</h1><h2>Section</h2><h3>Sub</h3></body></html>")
        result = remediate_skipped_heading_level(soup, _issue("skipped-heading-level"))
        assert result is None

    def test_multiple_gaps(self):
        soup = _soup("<html><body><h1>Title</h1><h4>Deep</h4></body></html>")
        result = remediate_skipped_heading_level(soup, _issue("skipped-heading-level"))
        assert result is not None
        headings = [h.name for h in soup.find_all(["h1", "h2", "h3", "h4"])]
        assert headings == ["h1", "h2", "h3", "h4"]

    def test_inserted_headings_are_sr_only(self):
        soup = _soup("<html><body><h1>Title</h1><h3>Sub</h3></body></html>")
        remediate_skipped_heading_level(soup, _issue("skipped-heading-level"))
        h2 = soup.find("h2")
        assert "sr-only" in (h2.get("class") or [])


class TestEmptyHeadingContent:
    """remediate_empty_heading_content — WCAG 2.4.6"""

    def test_fills_empty_heading(self):
        soup = _soup("<html><body><h2></h2><p>Following paragraph</p></body></html>")
        result = remediate_empty_heading_content(soup, _issue("empty-heading-content"))
        h2 = soup.find("h2")
        assert h2 is not None
        # Should have some text now (either from sibling context or fallback)
        assert h2.get_text(strip=True) != ""

    def test_no_op_when_heading_has_text(self):
        soup = _soup("<html><body><h2>Real Heading</h2></body></html>")
        result = remediate_empty_heading_content(soup, _issue("empty-heading-content"))
        assert result is None


class TestPromoteTitleParagraph:
    """remediate_missing_headings promotes an existing <p> when it can."""

    def test_promotes_paragraph_matching_the_title(self):
        html = (
            "<html><head><title>Bulk Mailing Sample Permit Numbers</title></head>"
            "<body><p>Bulk Mailing Sample Permit Numbers</p>"
            "<p>ABA DENIED ACADEMIC SKILL CENTER 80583</p></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        msg = remediate_missing_headings(soup, {})
        assert msg is not None
        assert soup.h1 is not None
        assert soup.h1.get_text() == "Bulk Mailing Sample Permit Numbers"
        # promoted in place — the paragraph is gone, not duplicated
        assert len(soup.find_all("p")) == 1

    def test_ignores_case_and_whitespace_when_matching(self):
        html = (
            "<html><head><title>Bulk  Mailing Ghost Permit Numbers</title></head>"
            "<body><p>BULK MAILING SAMPLE PERMIT NUMBERS</p></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        assert remediate_missing_headings(soup, {}) is not None
        assert soup.h1.get_text() == "BULK MAILING SAMPLE PERMIT NUMBERS"

    def test_promotes_first_usable_paragraph_when_title_matches_nothing(self):
        """No title match, but promote_document_h1's second rung (generous
        promotion of the first usable-text block) still fires."""
        html = (
            "<html><head><title>Some Document Title</title></head>"
            "<body><p>Entirely unrelated body copy goes here.</p></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        assert remediate_missing_headings(soup, {}) is not None
        assert soup.find("h1").get_text() == "Entirely unrelated body copy goes here."

    def test_no_op_when_the_title_is_junk(self):
        html = (
            "<html><head><title>Date: _____________________</title></head>"
            "<body><p>Date: _____________________</p></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        assert remediate_missing_headings(soup, {}) is None
        assert soup.find("h1") is None

    def test_bold_conversion_still_wins_when_available(self):
        html = (
            "<html><head><title>Some Document Title</title></head>"
            "<body><p><strong>Section One</strong></p></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        msg = remediate_missing_headings(soup, {})
        assert "bold paragraphs" in msg

    def test_promotes_a_title_match_within_the_candidate_window(self):
        """The window ladder (HEADING_CANDIDATE_WINDOW) scans the first 5
        leading blocks for a title match, not only the first paragraph — so
        a match at position 2 (here, after a "From:" field) is still
        promoted."""
        html = (
            "<html><head><title>Example University Faculty Senate Example University Faculty Senate</title></head>"
            "<body>"
            "<p>From:</p>"
            "<p>Example University Faculty Senate Example University Faculty Senate</p>"
            "<p>Sent: Monday, December 6, 2021</p>"
            "</body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        assert remediate_missing_headings(soup, {}) is not None
        assert soup.find("h1").get_text() == "Example University Faculty Senate Example University Faculty Senate"
        assert len(soup.find_all("p")) == 2


# ===================================================================
# Landmark strategies
# ===================================================================

from remediate.strategies.landmark_remediation import (
    remediate_missing_main_landmark,
    remediate_missing_header_landmark,
    remediate_missing_skip_link,
)


class TestMissingMainLandmark:
    """remediate_missing_main_landmark — WCAG 1.3.1"""

    def test_wraps_body_content_in_main(self):
        soup = _soup("<html><body><p>content</p><p>more</p></body></html>")
        result = remediate_missing_main_landmark(soup, _issue("missing-main-landmark"))
        assert result is not None
        main = soup.find("main")
        assert main is not None
        assert main.find("p") is not None

    def test_no_op_when_main_exists(self):
        soup = _soup("<html><body><main><p>content</p></main></body></html>")
        result = remediate_missing_main_landmark(soup, _issue("missing-main-landmark"))
        assert result is None

    def test_no_op_when_role_main_exists(self):
        soup = _soup('<html><body><div role="main"><p>content</p></div></body></html>')
        result = remediate_missing_main_landmark(soup, _issue("missing-main-landmark"))
        assert result is None

    def test_preserves_existing_landmarks(self):
        html = "<html><body><header>Banner</header><p>content</p><footer>Info</footer></body></html>"
        soup = _soup(html)
        remediate_missing_main_landmark(soup, _issue("missing-main-landmark"))
        # header and footer should NOT be inside main
        main = soup.find("main")
        assert main is not None
        assert main.find("header") is None
        assert main.find("footer") is None
        # but they should still exist
        assert soup.find("header") is not None
        assert soup.find("footer") is not None


class TestMissingSkipLink:
    """remediate_missing_skip_link — WCAG 2.4.1"""

    def test_adds_skip_link(self):
        soup = _soup('<html><body><main id="main-content"><p>text</p></main></body></html>')
        result = remediate_missing_skip_link(soup, _issue("missing-skip-link"))
        assert result is not None
        skip = soup.find("a", class_="skip-link")
        assert skip is not None
        assert "main" in skip.get("href", "")


# ===================================================================
# Figure / alt text strategies
# ===================================================================

from remediate.strategies.figure_remediation import (
    remediate_alt_text,
    remediate_improper_figure_structure,
)


class TestAltText:
    """remediate_alt_text — WCAG 1.1.1"""

    def test_adds_alt_to_bare_img(self):
        """An img with no alt gets an empty alt; nothing is invented and the issue stays open."""
        html = '<html><body><img src="chart.png"></body></html>'
        soup = _soup(html)
        issue = _issue("missing-alt-text", context={"html_snippet": '<img src="chart.png">'})
        result = remediate_alt_text(soup, issue)
        assert result is None
        assert soup.find("img")["alt"] == ""

    def test_skips_when_alt_already_set(self):
        html = '<html><body><img src="photo.jpg" alt="Team photo from retreat"></body></html>'
        soup = _soup(html)
        issue = _issue("missing-alt-text", context={"html_snippet": '<img src="photo.jpg">'})
        result = remediate_alt_text(soup, issue)
        # Should not overwrite existing meaningful alt
        assert soup.find("img")["alt"] == "Team photo from retreat"

    def test_empties_generic_alt(self):
        html = '<html><body><img src="data.png" alt="image"></body></html>'
        soup = _soup(html)
        issue = _issue("generic-alt-text", context={"html_snippet": '<img src="data.png">'})
        result = remediate_alt_text(soup, issue)
        assert result is None
        assert soup.find("img")["alt"] == ""


class TestImproperFigureStructure:
    """remediate_improper_figure_structure — WCAG 1.1.1"""

    def test_wraps_bare_img_in_figure(self):
        html = '<html><body><img src="chart.png" alt="Chart"></body></html>'
        soup = _soup(html)
        issue = _issue("improper-figure-structure", context={"html_snippet": '<img src="chart.png">'})
        result = remediate_improper_figure_structure(soup, issue)
        assert result is not None
        assert soup.find("figure") is not None


# ===================================================================
# Link strategies
# ===================================================================

from remediate.strategies.link_remediation import (
    remediate_empty_link_text,
    remediate_generic_link_text,
)
from remediate.strategies.color_link_remediation import (
    remediate_url_as_link_text,
    _url_to_label,
)


class TestUrlAsLinkText:
    """remediate_url_as_link_text — WCAG 2.4.4 (url-as-link-text)"""

    def test_label_is_not_url_shaped(self):
        # Regression: the generated aria-label must not itself look like a URL,
        # or LinkTextCheck._is_url re-flags it (url-as-link-text gap).
        from audit.checks.link_checks import LinkTextCheck
        chk = LinkTextCheck(_soup("<a></a>"), lambda *a, **k: None)
        label = _url_to_label("https://portal.example.edu")
        assert label and not chk._is_url(label), f"label still URL-shaped: {label!r}"

    def test_label_for_schemeless_url(self):
        # www.mi.gov/... has no scheme — urlparse would put it all in path.
        label = _url_to_label("www.mi.gov/campussexualassault")
        assert label and "mi.gov" in label

    def test_label_for_mailto(self):
        label = _url_to_label("mailto:faculty-senate@example.edu")
        assert label and "faculty-senate@example.edu" in label
        from audit.checks.link_checks import LinkTextCheck
        chk = LinkTextCheck(_soup("<a></a>"), lambda *a, **k: None)
        assert not chk._is_url(label)

    def test_remediation_clears_url_as_link_text(self):
        from audit.checks.link_checks import LinkTextCheck
        html = ('<html><body><a href="https://portal.example.edu">'
                'https://portal.example.edu</a></body></html>')
        soup = _soup(html)
        issue = _issue("url-as-link-text", wcag_criterion="2.4.4",
                       context={"element_name": "a",
                                "attributes": {"href": "https://portal.example.edu"}})
        assert remediate_url_as_link_text(soup, issue) is not None
        # Re-audit the remediated DOM: must no longer be url-as-link-text.
        types = []
        chk = LinkTextCheck(soup, lambda t, *a, **k: types.append(t))
        chk.check()
        assert "url-as-link-text" not in types
        assert "compliant-link-text" in types


class TestEmptyLinkText:
    """remediate_empty_link_text — WCAG 2.4.4"""

    def test_adds_text_from_url(self):
        html = '<html><body><a href="https://example.com/reports"></a></body></html>'
        soup = _soup(html)
        issue = _issue("empty-link-text", context={"html_snippet": '<a href="https://example.com/reports"></a>'})
        result = remediate_empty_link_text(soup, issue)
        link = soup.find("a")
        assert link.get_text(strip=True) != ""


class TestGenericLinkText:
    """remediate_generic_link_text — WCAG 2.4.4"""

    def test_replaces_click_here(self):
        html = '<html><body><a href="https://example.com/policy">click here</a></body></html>'
        soup = _soup(html)
        issue = _issue("generic-link-text", context={"html_snippet": '<a href="https://example.com/policy">click here</a>'})
        result = remediate_generic_link_text(soup, issue)
        assert result is not None
        link = soup.find("a")
        assert link.get_text(strip=True).lower() != "click here"


# ===================================================================
# Table strategies
# ===================================================================

from remediate.strategies.table_remediation import (
    remediate_table_missing_headers,
    remediate_table_missing_scope,
    remediate_table_missing_thead,
    remediate_table_missing_caption,
)


class TestTableMissingHeaders:
    """remediate_table_missing_headers — WCAG 1.3.1"""

    def test_promotes_first_row_to_headers(self):
        html = """<html><body>
        <table>
            <tr><td>Name</td><td>Age</td><td>City</td></tr>
            <tr><td>Alice</td><td>30</td><td>NYC</td></tr>
        </table>
        </body></html>"""
        soup = _soup(html)
        issue = _issue("table-missing-headers", context={
            "position": {"table_index": 0}
        })
        result = remediate_table_missing_headers(soup, issue)
        assert result is not None
        assert len(soup.find_all("th")) > 0


class TestTableMissingScope:
    """remediate_table_missing_scope — WCAG 1.3.1"""

    def test_adds_scope_to_th_cells(self):
        html = """<html><body>
        <table>
            <tr><th>Name</th><th>Age</th></tr>
            <tr><td>Alice</td><td>30</td></tr>
        </table>
        </body></html>"""
        soup = _soup(html)
        issue = _issue("table-missing-scope", context={
            "position": {"table_index": 0}
        })
        result = remediate_table_missing_scope(soup, issue)
        assert result is not None
        for th in soup.find_all("th"):
            assert th.get("scope") is not None


class TestTableMissingThead:
    """remediate_table_missing_thead — WCAG 1.3.1"""

    def test_wraps_header_row_in_thead(self):
        html = """<html><body>
        <table>
            <tr><th>Name</th><th>Age</th></tr>
            <tr><td>Alice</td><td>30</td></tr>
            <tr><td>Bob</td><td>25</td></tr>
        </table>
        </body></html>"""
        soup = _soup(html)
        issue = _issue("table-missing-thead", context={
            "position": {"table_index": 0}
        })
        result = remediate_table_missing_thead(soup, issue)
        assert result is not None
        assert soup.find("thead") is not None


class TestTableMissingCaption:
    """remediate_table_missing_caption — WCAG 1.3.1"""

    def test_adds_caption_element(self):
        html = """<html><body>
        <table>
            <tr><th>Name</th><th>Age</th></tr>
            <tr><td>Alice</td><td>30</td></tr>
        </table>
        </body></html>"""
        soup = _soup(html)
        issue = _issue("table-missing-caption", context={
            "position": {"table_index": 0}
        })
        result = remediate_table_missing_caption(soup, issue)
        assert result is not None
        assert soup.find("caption") is not None


# ===================================================================
# Table: complex / edge cases (historically frequent regressions)
# ===================================================================

class TestTableWithColspanRowspan:
    """Table remediation with spanning cells — regression-prone area."""

    def test_scope_on_colspan_header(self):
        html = """<html><body>
        <table>
            <tr><th colspan="2">Full Name</th><th>Age</th></tr>
            <tr><td>First</td><td>Last</td><td>30</td></tr>
        </table>
        </body></html>"""
        soup = _soup(html)
        issue = _issue("table-missing-scope", context={"position": {"table_index": 0}})
        result = remediate_table_missing_scope(soup, issue)
        assert result is not None
        th = soup.find("th", attrs={"colspan": "2"})
        assert th.get("scope") is not None

    def test_rowspan_header_scope(self):
        html = """<html><body>
        <table>
            <tr><th rowspan="2">Category</th><th>Q1</th><th>Q2</th></tr>
            <tr><td>10</td><td>20</td></tr>
        </table>
        </body></html>"""
        soup = _soup(html)
        issue = _issue("table-missing-scope", context={"position": {"table_index": 0}})
        result = remediate_table_missing_scope(soup, issue)
        assert result is not None
        for th in soup.find_all("th"):
            assert th.get("scope") is not None


class TestTableAllHeaderRows:
    """Table where ALL rows contain th — triggers THead/TBody edge case."""

    def test_all_header_rows_still_get_scope(self):
        html = """<html><body>
        <table>
            <tr><th>A</th><th>B</th></tr>
            <tr><th>C</th><th>D</th></tr>
        </table>
        </body></html>"""
        soup = _soup(html)
        issue = _issue("table-missing-scope", context={"position": {"table_index": 0}})
        result = remediate_table_missing_scope(soup, issue)
        assert result is not None
        for th in soup.find_all("th"):
            assert th.get("scope") is not None


# ===================================================================
# Parsing strategies
# ===================================================================

from remediate.strategies.parsing_remediation import (
    remediate_duplicate_id,
)


class TestDuplicateId:
    """remediate_duplicate_id — WCAG 4.1.1"""

    def test_deduplicates_ids(self):
        html = '<html><body><div id="foo">A</div><div id="foo">B</div></body></html>'
        soup = _soup(html)
        result = remediate_duplicate_id(soup, _issue("duplicate-id"))
        assert result is not None
        ids = [el["id"] for el in soup.find_all(attrs={"id": True})]
        assert len(ids) == len(set(ids)), f"IDs still duplicated: {ids}"


# ===================================================================
# H1 injection gate tests
# ===================================================================

class TestH1InjectionGate:
    """Neither injector may invent text — a junk/absent title never blocks
    promotion of *real* page content; only junk page content does."""

    def test_landmark_promotes_real_content_when_title_is_a_form_label(self):
        html = (
            "<html><head><title>Date: _____________________</title></head>"
            "<body><p>Some content</p></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        msg = remediate_missing_header_landmark(soup, {})
        assert msg is not None                      # landmark still added
        assert soup.find("header") is not None
        # junk title never becomes the heading, but real page content does
        # (generous fallback rung), retagged rather than invented
        assert soup.header.h1.get_text() == "Some content"

    def test_landmark_never_promotes_title_text_that_is_not_on_the_page(self):
        """The measured failure: a humanized filename has exactly the shape
        is_usable_heading_text accepts, so the gate passed it and the
        landmark announced text appearing nowhere in the document. The title
        text itself must never be written in — only real page content may be
        retagged, and here that's the leading paragraph."""
        html = (
            "<html><head><title>Src Sample Photo Form</title></head>"
            "<body><p>Some content</p></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        msg = remediate_missing_header_landmark(soup, {})
        assert msg is not None                      # landmark still added
        assert soup.find("header") is not None
        assert soup.header.h1.get_text() == "Some content"  # real content, retagged
        assert "Src Sample Photo Form" not in soup.body.get_text()

    def test_landmark_promotes_a_leading_paragraph_that_is_the_title(self):
        """The one permitted route to a new h1: retag page content."""
        html = (
            "<html><head><title>Annual Assessment Report</title></head>"
            "<body><p>Annual Assessment Report</p><p>Some content</p></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        remediate_missing_header_landmark(soup, {})
        assert soup.header.h1.get_text() == "Annual Assessment Report"
        # promoted, not duplicated: the paragraph is gone
        assert len(soup.find_all("h1")) == 1
        assert soup.body.get_text().count("Annual Assessment Report") == 1

    def test_landmark_never_promotes_the_untitled_placeholder_itself(self):
        """A document with no <title> gets "Untitled Document" fabricated by
        DocumentTitleCheck's strategy, which runs first. That placeholder
        string must never become the heading text — but real page content
        still gets promoted via the generous fallback rung."""
        from remediate.strategies.document_structure_remediation import (
            remediate_missing_document_title,
        )

        html = "<html><head></head><body><p>Some content</p></body></html>"
        soup = BeautifulSoup(html, "html.parser")
        remediate_missing_document_title(soup, {})
        assert soup.find("title").get_text() == "Untitled Document"

        remediate_missing_header_landmark(soup, {})
        assert soup.header.h1.get_text() == "Some content"
        assert "Untitled Document" not in soup.header.h1.get_text()
        # h1 already exists now, so a direct call is a no-op, not a re-promotion
        assert remediate_missing_h1(soup, {}) is None
        assert len(soup.find_all("h1")) == 1

    def test_landmark_moves_an_existing_h1_regardless_of_title(self):
        html = (
            "<html><head><title>Date: _____________________</title></head>"
            "<body><h1>Real Document Heading</h1><p>Body</p></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        remediate_missing_header_landmark(soup, {})
        assert soup.header.h1.get_text() == "Real Document Heading"
        assert len(soup.find_all("h1")) == 1

    def test_missing_h1_falls_back_to_first_subheading_on_a_junk_title(self):
        """A junk title never becomes the heading, but Population A's
        fallback rung (first sub-heading with usable text) still fires
        independent of title validity."""
        html = (
            "<html><head><title>a1b2c3d4 Office%20Expanded%20Matrix</title></head>"
            "<body><h2>A section</h2></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        result = remediate_missing_h1(soup, {})
        assert result is not None
        assert soup.find("h1").get_text() == "A section"

    def test_missing_h1_falls_back_to_first_subheading_when_title_matches_nothing(self):
        html = (
            "<html><head><title>Annual Assessment Report</title></head>"
            "<body><h2>A section</h2></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        assert remediate_missing_h1(soup, {}) is not None
        assert soup.find("h1").get_text() == "A section"

    def test_falls_back_to_first_subheading_on_a_placeholder_title(self):
        """A fabricated placeholder title is rejected by
        is_usable_heading_text and never becomes the heading text, but the
        fallback rung still promotes the first usable sub-heading."""
        html = (
            "<html><head><title>untitled document</title></head>"
            "<body><h2>A section</h2></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        result = remediate_missing_h1(soup, {})
        assert result is not None
        assert soup.find("h1").get_text() == "A section"

    def test_landmark_promotes_real_content_for_a_single_word_title_that_matches_nothing(self):
        html = (
            "<html><head><title>Syllabus</title></head>"
            "<body><p>Body</p></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        assert remediate_missing_header_landmark(soup, {}) is not None
        assert soup.find("header") is not None
        assert soup.header.h1.get_text() == "Body"

    def test_landmark_kill_switch_blocks_zero_heading_promotion(self):
        """The landmark path must thread options through to
        promote_document_h1 — heading_promote_first_text=False must block
        the zero-heading fallback rung here exactly as it does when called
        via remediate_missing_h1 directly."""
        html = (
            "<html><head><title>Date: _____________________</title></head>"
            "<body><p>Some content</p></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        opts = {"conversion": {"heading_promote_first_text": False}}
        remediate_missing_header_landmark(soup, {}, opts)
        assert soup.find("h1") is None
        assert soup.find("header") is not None  # landmark still added

        # Same doc, no options (default True): the fallback rung fires.
        soup2 = BeautifulSoup(html, "html.parser")
        remediate_missing_header_landmark(soup2, {})
        assert soup2.find("h1") is not None
        assert soup2.header.h1.get_text() == "Some content"


class TestManagerKillSwitchThroughLandmarkChain:
    """The kill switch must hold no matter which landmark issue type the
    manager dispatches first. missing-main-landmark is the common case:
    RemediationManager must forward its options down the whole chain
    (main -> nav -> header) into promote_document_h1, not just for a
    directly-dispatched missing-header-landmark issue."""

    def test_missing_main_landmark_respects_kill_switch(self):
        from remediate.remediation_manager import RemediationManager

        html = (
            "<html><head><title>Date: _____________________</title></head>"
            "<body><p>Some content</p></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        options = {"conversion": {"heading_promote_first_text": False}}
        manager = RemediationManager(soup, options=options)
        manager.remediate_issue(_issue("missing-main-landmark"))
        assert soup.find("h1") is None
        assert soup.find("main") is not None  # landmark chain still ran

        # Same doc, no options: the fallback rung fires and an h1 appears.
        soup2 = BeautifulSoup(html, "html.parser")
        manager2 = RemediationManager(soup2, options=None)
        manager2.remediate_issue(_issue("missing-main-landmark"))
        assert soup2.find("h1") is not None
        assert soup2.find("h1").get_text() == "Some content"

    def test_missing_main_landmark_respects_master_switch(self):
        """The master switch must survive the same main -> nav -> header hop."""
        from remediate.remediation_manager import RemediationManager

        html = (
            "<html><head><title>Annual Report</title></head>"
            "<body><h2>Annual Report</h2><p>Some content</p></body></html>"
        )
        soup = BeautifulSoup(html, "html.parser")
        options = {"conversion": {"detect_document_heading": False}}
        manager = RemediationManager(soup, options=options)
        manager.remediate_issue(_issue("missing-main-landmark"))
        assert soup.find("h1") is None
        assert soup.find("h2") is not None
        assert soup.find("main") is not None  # landmark chain still ran


def test_heading_remediation_runs_before_landmarks():
    """Pin the execution order the design depends on.

    Non-table issues keep audit order (remediation_manager._sort_issues_for_
    remediation), and the auditor registers HeadingHierarchyCheck before
    LandmarksCheck — so a promoted h1 exists by the time the landmark strategy
    looks for one. If this ever flips, the landmark manufactures the h1 first.
    """
    import inspect
    from audit import auditor as auditor_mod

    src = inspect.getsource(auditor_mod)
    assert src.index("HeadingHierarchyCheck(") < src.index("DocumentTitleCheck(")
    assert src.index("DocumentTitleCheck(") < src.index("LandmarksCheck(")


# ===================================================================
# Cross-strategy interaction tests
# ===================================================================

class TestStrategyOrdering:
    """Verify that strategies compose correctly when run in sequence."""

    def test_main_landmark_then_h1_both_present(self):
        """After adding <main> and then h1, both elements should exist in the document."""
        # 2+ word title required to clear the plausibility gate
        soup = _soup("<html><head><title>Main Report</title></head><body><p>text</p></body></html>")
        remediate_missing_main_landmark(soup, _issue("missing-main-landmark"))
        remediate_missing_h1(soup, _issue("missing-h1"))
        assert soup.find("main") is not None
        assert soup.find("h1") is not None

    def test_heading_fix_then_scope_fix_no_crash(self):
        """Heading and table strategies should not interfere with each other."""
        html = """<html><body>
        <h1>Title</h1><h3>Sub</h3>
        <table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>
        </body></html>"""
        soup = _soup(html)
        remediate_skipped_heading_level(soup, _issue("skipped-heading-level"))
        remediate_table_missing_scope(soup, _issue("table-missing-scope", context={"position": {"table_index": 0}}))
        # Should not crash, and both fixes should be present
        assert soup.find("h2") is not None
        for th in soup.find_all("th"):
            assert th.get("scope") is not None


# ===================================================================
# Pipe-delimited table strategy (WCAG 1.3.1)
# ===================================================================

from remediate.strategies.pipe_table_remediation import (
    convert_pipe_delimited_paragraphs,
)


class TestConvertPipeDelimitedParagraphs:

    def test_code_run_becomes_definition_list_without_invented_headers(self):
        html = (
            "<div>"
            "<p>BUS 248|Business Communications</p>"
            "<p>ENG 111|English</p>"
            "</div>"
        )
        soup = _soup(html)
        convert_pipe_delimited_paragraphs(soup)
        assert soup.find("table") is None, "column labels would have to be guessed"
        assert soup.find("p") is None
        dl = soup.find("dl")
        assert [dt.get_text(strip=True) for dt in dl.find_all("dt")] == ["BUS 248", "ENG 111"]
        assert [dd.get_text(strip=True) for dd in dl.find_all("dd")] == [
            "Business Communications", "English"]

    def test_non_course_codes_get_no_course_labels(self):
        html = "<div><p>ISO 9001|Quality management</p><p>RFC 822|Email format</p></div>"
        soup = _soup(html)
        convert_pipe_delimited_paragraphs(soup)
        assert "Course" not in str(soup)
        assert soup.find("dl") is not None

    def test_single_code_paragraph_becomes_definition_list(self):
        html = "<div><p>SPE 121|Speech Communication</p></div>"
        soup = _soup(html)
        convert_pipe_delimited_paragraphs(soup)
        assert soup.find("dl") is not None
        assert soup.find("p") is None

    def test_generic_pipe_pairs_become_definition_list(self):
        html = (
            "<div>"
            "<p>Name|Ada Lovelace</p>"
            "<p>Role|Mathematician</p>"
            "</div>"
        )
        soup = _soup(html)
        convert_pipe_delimited_paragraphs(soup)
        assert soup.find("table") is None
        dl = soup.find("dl")
        assert dl is not None
        assert [dt.get_text(strip=True) for dt in dl.find_all("dt")] == ["Name", "Role"]
        assert [dd.get_text(strip=True) for dd in dl.find_all("dd")] == [
            "Ada Lovelace", "Mathematician"]

    def test_single_generic_pipe_paragraph_left_untouched(self):
        # One non-course pipe pair is not enough signal — leave it.
        html = "<div><p>See section A|details follow below here</p></div>"
        soup = _soup(html)
        convert_pipe_delimited_paragraphs(soup)
        assert soup.find("table") is None
        assert soup.find("dl") is None
        assert soup.find("p") is not None

    def test_multi_pipe_and_prose_left_untouched(self):
        html = (
            "<div>"
            "<p>a|b|c</p>"
            "<p>This sentence has a | pipe but is prose, not a pair at all.</p>"
            "</div>"
        )
        soup = _soup(html)
        convert_pipe_delimited_paragraphs(soup)
        assert soup.find("table") is None
        assert soup.find("dl") is None
        assert len(soup.find_all("p")) == 2

    def test_heading_splits_run_into_two_lists(self):
        html = (
            "<div>"
            "<p>BUS 248|Business Communications</p>"
            "<p>ENG 111|English</p>"
            "<h3>Next Category</h3>"
            "<p>MTH 121|College Algebra</p>"
            "<p>MTH 122|Plane Trigonometry</p>"
            "</div>"
        )
        soup = _soup(html)
        convert_pipe_delimited_paragraphs(soup)
        assert len(soup.find_all("dl")) == 2
        assert soup.find("h3") is not None
        assert soup.find("p") is None


# ===================================================================
# Descriptive title strategy (WCAG 2.4.6 / 2.4.2)
# ===================================================================

from remediate.strategies.heading_remediation import (
    remediate_descriptive_title,
    _humanize_filename_stem,
)


class TestRemediateDescriptiveTitle:

    def test_filename_title_replaced_from_metadata(self):
        html = (
            "<html><head><title>example_cc_gs_course_guide_24-25</title></head>"
            "<body><header><h1>example_cc_gs_course_guide_24-25</h1></header></body></html>"
        )
        soup = _soup(html)
        struct = {"metadata": {"title": "Alpena Community College Course Guide"}}
        remediate_descriptive_title(soup, struct, "example_cc_gs_course_guide_24-25")
        assert soup.title.get_text(strip=True) == "Alpena Community College Course Guide"
        assert soup.h1.get_text(strip=True) == "Alpena Community College Course Guide"

    def test_descriptive_title_left_untouched(self):
        html = (
            "<html><head><title>Annual Financial Report 2024</title></head>"
            "<body><h1>Annual Financial Report 2024</h1></body></html>"
        )
        soup = _soup(html)
        remediate_descriptive_title(soup, {"metadata": {}}, "annual_report")
        assert soup.title.get_text(strip=True) == "Annual Financial Report 2024"

    def test_falls_back_to_first_heading_when_metadata_blank(self):
        html = (
            "<html><head><title>doc_123</title></head>"
            "<body><h1>doc_123</h1><h2>Quarterly Earnings Overview</h2></body></html>"
        )
        soup = _soup(html)
        remediate_descriptive_title(soup, {"metadata": {}}, "doc_123")
        assert soup.title.get_text(strip=True) == "Quarterly Earnings Overview"
        assert soup.h1.get_text(strip=True) == "Quarterly Earnings Overview"

    def test_id_only_stem_leaves_title_unchanged(self):
        # No metadata, no descriptive heading, and the stem is purely an id
        # token -> nothing to humanize, so the title stays flagged honestly.
        html = "<html><head><title>a1b2c3d4</title></head><body><h1>a1b2c3d4</h1></body></html>"
        soup = _soup(html)
        remediate_descriptive_title(soup, {"metadata": {}}, "a1b2c3d4")
        assert soup.title.get_text(strip=True) == "a1b2c3d4"

    def test_falls_back_to_humanized_filename(self):
        # No metadata, no descriptive heading -> derive a title from the stem,
        # stripping the hash prefix and splitting the concatenated words.
        html = (
            "<html><head><title>a1b2c3d4_samplereleaseform</title></head>"
            "<body><h1>a1b2c3d4_samplereleaseform</h1></body></html>"
        )
        soup = _soup(html)
        remediate_descriptive_title(soup, {"metadata": {}}, "a1b2c3d4_samplereleaseform")
        assert soup.title.get_text(strip=True) == "Sample Release Form"
        assert soup.h1.get_text(strip=True) == "Sample Release Form"

    def test_hyphen_only_stem_replaced_from_heading(self):
        html = (
            "<html><head><title>doc-2024</title></head>"
            "<body><h1>doc-2024</h1><h2>Annual Report 2024</h2></body></html>"
        )
        soup = _soup(html)
        remediate_descriptive_title(soup, {"metadata": {}}, "doc-2024")
        assert soup.title.get_text(strip=True) == "Annual Report 2024"
        assert soup.h1.get_text(strip=True) == "Annual Report 2024"


class TestHumanizeFilenameStem:

    def test_hash_prefix_and_concatenated_words(self):
        assert (
            _humanize_filename_stem("a1b2c3d4_samplereleaseform")
            == "Sample Release Form"
        )

    def test_underscore_separated(self):
        assert _humanize_filename_stem("sample_release_form") == "Sample Release Form"

    def test_id_only_stem_returns_none(self):
        assert _humanize_filename_stem("a1b2c3d4") is None

    def test_empty_stem_returns_none(self):
        assert _humanize_filename_stem("") is None

    def test_trailing_whitespace_tolerated(self):
        assert (
            _humanize_filename_stem("a1b2c3d4_samplereleaseform ")
            == "Sample Release Form"
        )

    def test_url_encoded_spaces_decoded(self):
        # Real-world filenames are URL-encoded; %20 must become a space, not "20".
        out = _humanize_filename_stem(
            "b2c3d4e5_Example%20College%20General%20Studies%20Graphic%20model.1-1-20")
        assert out
        assert "%" not in out and "20 " not in out
        assert "Example College General Studies Graphic Model" in out

    def test_digit_bearing_word_not_dropped_as_id(self):
        # "November11event" is a real word token, not a hash — must not be dropped.
        out = _humanize_filename_stem("c3d4e5f6_November11event-pdf")
        assert out and out != "Pdf"
        assert "November" in out and "Event" in out
        assert "Pdf" not in out.split()          # trailing format token stripped

    def test_camelcase_split_not_mangled(self):
        # camelCase should split on case boundaries; short tokens must not be
        # shredded by wordninja ("src" must stay "Src", not "S Rc").
        out = _humanize_filename_stem("d4e5f6a7_srcSamplePhotoForm")
        assert out == "Src Sample Photo Form"

    def test_acronym_not_shredded(self):
        assert _humanize_filename_stem("doc_NASA_report") == "Doc NASA Report"

    def test_wordninja_absent_degrades_to_humanize_only(self, monkeypatch):
        # Simulate wordninja being unavailable: the concatenated token is kept
        # whole (title-cased) rather than crashing the pipeline.
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "wordninja":
                raise ImportError("simulated missing wordninja")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert (
            _humanize_filename_stem("a1b2c3d4_samplereleaseform")
            == "Samplereleaseform"
        )


# ===================================================================
# Flat heading nesting strategy (WCAG 1.3.1)
# ===================================================================

from remediate.strategies.heading_remediation import remediate_flat_heading_nesting


class TestRemediateFlatHeadingNesting:

    def test_anchors_demote_following_siblings(self):
        html = (
            "<body>"
            "<h2>Level I - Foundations</h2>"
            "<h2>Writing (WR)</h2>"
            "<h2>Quantitative Literacy (QL)</h2>"
            "<h2>Level II - Exploration</h2>"
            "<h2>Personal Wellness (PW)</h2>"
            "</body>"
        )
        soup = _soup(html)
        result = remediate_flat_heading_nesting(soup)
        assert result is not None
        levels = [(h.name, h.get_text(strip=True)) for h in soup.find_all(["h2", "h3"])]
        assert levels == [
            ("h2", "Level I - Foundations"),
            ("h3", "Writing (WR)"),
            ("h3", "Quantitative Literacy (QL)"),
            ("h2", "Level II - Exploration"),
            ("h3", "Personal Wellness (PW)"),
        ]

    def test_headings_before_first_anchor_untouched(self):
        html = (
            "<body>"
            "<h2>Course Guide Overview</h2>"
            "<h2>Catalog Years 2020</h2>"
            "<h2>Level I - Foundations</h2>"
            "<h2>Writing (WR)</h2>"
            "<h2>Level II - Exploration</h2>"
            "<h2>Personal Wellness (PW)</h2>"
            "</body>"
        )
        soup = _soup(html)
        remediate_flat_heading_nesting(soup)
        names = [h.name for h in soup.find_all(["h2", "h3"])]
        assert names == ["h2", "h2", "h2", "h3", "h2", "h3"]

    def test_no_anchor_pattern_is_noop(self):
        html = "<body><h2>Alpha</h2><h2>Beta</h2><h2>Gamma</h2></body>"
        soup = _soup(html)
        result = remediate_flat_heading_nesting(soup)
        assert result is None
        assert [h.name for h in soup.find_all(["h2", "h3"])] == ["h2", "h2", "h2"]

    def test_single_anchor_is_noop(self):
        html = "<body><h2>Level I - Foundations</h2><h2>Writing (WR)</h2></body>"
        soup = _soup(html)
        result = remediate_flat_heading_nesting(soup)
        assert result is None
        assert [h.name for h in soup.find_all(["h2", "h3"])] == ["h2", "h2"]


# ===================================================================
# remediate_html integration of structural strategies
# ===================================================================

from remediate.remediator import remediate_html


class TestStructuralStrategiesWiredIn:

    def test_remediate_html_applies_all_three_structural_fixes(self):
        html = (
            "<html><head><title>course_guide_24_25</title></head>"
            "<body><header><h1>course_guide_24_25</h1></header><main>"
            "<h2>Level I - Foundations</h2>"
            "<h2>Writing (WR)</h2>"
            "<p>BUS 248|Business Communications</p>"
            "<p>ENG 111|English</p>"
            "<h2>Level II - Exploration</h2>"
            "<h2>Personal Wellness (PW)</h2>"
            "<p>SPE 121|Speech Communication</p>"
            "</main></body></html>"
        )
        struct = {"metadata": {"title": "Example College General Studies Course Guide"}}
        result = remediate_html(html_content=html, issues=[], options={}, struct_data=struct)
        out = _soup(result["html"])
        # Title fixed
        assert out.title.get_text(strip=True) == "Example College General Studies Course Guide"
        # Pipe runs rebuilt (one definition list per category)
        assert len(out.find_all("dl")) == 2
        assert out.find("p", string=lambda s: s and "|" in s) is None
        # Headings nested
        assert out.find("h3", string="Writing (WR)") is not None

    def test_config_flags_disable_structural_fixes(self):
        html = (
            "<html><head><title>doc_1</title></head><body><main>"
            "<p>BUS 248|Business Communications</p>"
            "<p>ENG 111|English</p>"
            "</main></body></html>"
        )
        options = {
            "convert_pipe_tables": False,
            "fix_document_title": False,
            "fix_heading_nesting": False,
        }
        result = remediate_html(html_content=html, issues=[], options=options, struct_data={})
        out = _soup(result["html"])
        assert out.find("table") is None
        assert out.title.get_text(strip=True) == "doc_1"

    def test_directory_mode_persists_structural_fixes_without_issues(self, tmp_path):
        page = tmp_path / "page_1.html"
        page.write_text(
            "<html><head><title>doc_1</title></head><body><main>"
            "<p>BUS 248|Business Communications</p>"
            "<p>ENG 111|English</p>"
            "</main></body></html>",
            encoding="utf-8",
        )
        # No issues passed → exercises the early-skip path in _remediate_directory.
        remediate_html(html_path=str(tmp_path), issues=[], options={},
                       struct_data={"metadata": {"title": "Course Catalog"}})
        out = _soup(page.read_text(encoding="utf-8"))
        assert out.find("dl") is not None
        assert out.title.get_text(strip=True) == "Course Catalog"
