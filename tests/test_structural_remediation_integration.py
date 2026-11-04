# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: re-remediate the flattened course-guide HTML and re-audit it.

Confirms the structural fixes produce a *legitimate* WCAG pass — real tables,
a descriptive title, and nested headings — not just a green auditor score.

Run:  python -m pytest tests/test_structural_remediation_integration.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup

from remediate.remediator import remediate_html

# A trimmed but faithful reproduction of the converter-flattened output.
RAW_HTML = """<!DOCTYPE html>
<html lang="en"><head><title>example_cc_gs_course_guide_24-25</title></head>
<body><header role="banner"><h1>example_cc_gs_course_guide_24-25</h1></header>
<main id="main-content" role="main"><div class="page">
<h2>Example Community College Course Guide for Example College General Studies</h2>
<h2>Level I - Foundations</h2>
<h2>Writing (WR) - Choose one</h2>
<p>BUS 248|Business Communications</p>
<p>ENG 111|English</p>
<h2>Quantitative Literacy (QL) - Choose one</h2>
<p>MTH 121|College Algebra</p>
<h2>Level II Exploration</h2>
<h2>Personal Wellness (PW) - Choose one</h2>
<p>SPE 121|Speech Communication</p>
</div></main></body></html>"""

STRUCT = {"metadata": {
    "source_pdf": "example_cc_gs_course_guide_24-25",
    "title": "Example Community College Course Guide for Example College General Studies (GS)",
}}


def test_structural_remediation_produces_real_structure():
    result = remediate_html(html_content=RAW_HTML, issues=[], options={}, struct_data=STRUCT)
    soup = BeautifulSoup(result["html"], "html.parser")

    # 1.3.1 — no pipe-delimited paragraphs remain; each code/title run is a
    # definition list (no column labels are invented).
    assert soup.find("p", string=lambda s: s and "|" in s) is None
    assert soup.find("table") is None
    lists = soup.find_all("dl")
    assert len(lists) == 3  # WR, QL, PW categories
    assert lists[0].find("dt").get_text(strip=True) == "BUS 248"

    # 2.4.6 / 2.4.2 — descriptive title, not the filename.
    assert "Example Community College" in soup.title.get_text(strip=True)
    assert "_" not in soup.title.get_text(strip=True)
    assert "_" not in soup.h1.get_text(strip=True)

    # 1.3.1 — categories nested under Level anchors.
    assert soup.find("h3", string=lambda s: s and "Writing (WR)" in s) is not None
    assert soup.find("h2", string=lambda s: s and "Level I - Foundations" in s) is not None


def test_reaudit_reports_no_structural_failures():
    from audit.checks.table_checks import PipeDelimitedDataCheck
    from audit.checks.heading_checks import DocumentTitleCheck, HeadingContentCheck

    result = remediate_html(html_content=RAW_HTML, issues=[], options={}, struct_data=STRUCT)
    soup = BeautifulSoup(result["html"], "html.parser")

    collected = []

    def collect(issue_type, criterion, severity, **kw):
        collected.append({"type": issue_type, "status": kw.get("status", "needs_remediation")})

    for cls in (PipeDelimitedDataCheck, DocumentTitleCheck, HeadingContentCheck):
        cls(soup, collect).check()

    failing = [i for i in collected if i["status"] == "needs_remediation"]
    assert failing == [], f"unexpected remaining failures: {failing}"
