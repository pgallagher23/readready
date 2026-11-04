# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for TOC continuation detection in toc_detector."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup
from utils.toc_detector import classify_table, TYPE_TOC, TYPE_DATA


def _make_html_with_continuation(junk_paragraphs=True, dots_in_continuation=True):
    """Build HTML simulating a TOC table followed by junk <p> then continuation table."""
    toc_table = '<table data-table-type="toc"><tr><td>Article I</td><td>Recognition</td><td>1-2</td></tr></table>'
    junk = ""
    if junk_paragraphs:
        junk = "<p>|</p><p>t</p><p>;</p><p>|</p>"
    dot_cell = ". . . . . 64" if dots_in_continuation else "64"
    continuation = f'<table><tr><td>Article XXXI</td><td>Faculty Research Fund{dot_cell}</td><td>64</td></tr>'
    continuation += '<tr><td>Article XXXII</td><td>Travel Fund</td><td>64-65</td></tr>'
    continuation += '<tr><td>Article XXXIII</td><td>Retirement</td><td>65</td></tr></table>'

    html = f"<html><body><h2>CONTENTS</h2>{toc_table}{junk}{continuation}</body></html>"
    return BeautifulSoup(html, "html.parser")


def test_continuation_detected_with_junk_paragraphs():
    """A table after junk single-char <p> elements should still detect preceding TOC."""
    soup = _make_html_with_continuation(junk_paragraphs=True, dots_in_continuation=True)
    tables = soup.find_all("table")
    # Table 0 is already classified as TOC (via data-table-type attr)
    # Table 1 should be classified as TOC continuation
    result = classify_table(tables[1])
    assert result == TYPE_TOC, f"Expected 'toc', got '{result}'"


def test_continuation_detected_without_max_cols_gate():
    """A continuation table with many columns (Docling misdetection) should still classify as TOC."""
    # Build a table with 10+ columns per row (simulates Docling misdetection)
    toc_table = '<table data-table-type="toc"><tr><td>Article I</td><td>Intro</td><td>1</td></tr></table>'
    # 10-column continuation table with leader dots
    wide_row = "<tr>" + "<td>Article</td><td>XXXI</td>" + "<td></td>" * 6 + "<td>. . . 64</td><td>65</td></tr>"
    continuation = f"<table>{wide_row}{wide_row}{wide_row}</table>"
    html = f"<html><body>{toc_table}{continuation}</body></html>"
    soup = BeautifulSoup(html, "html.parser")

    tables = soup.find_all("table")
    result = classify_table(tables[1])
    assert result == TYPE_TOC, f"Expected 'toc', got '{result}'"


def test_no_false_positive_without_preceding_toc():
    """A table with dots but no preceding TOC should NOT be classified as TOC continuation."""
    html = '<html><body><table><tr><td>Item . . . . 5</td></tr><tr><td>Item . . . . 6</td></tr></table></body></html>'
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table")
    result = classify_table(tables[0])
    # Should be data (no preceding TOC, no heading match, low page ratio with 1 col)
    assert result == TYPE_DATA, f"Expected 'data', got '{result}'"


def test_real_paragraph_blocks_continuation():
    """A substantial <p> (not junk) between tables should block continuation detection."""
    toc_table = '<table data-table-type="toc"><tr><td>Article I</td><td>Intro</td><td>1</td></tr></table>'
    blocker = "<p>This is a substantial paragraph that separates the tables and indicates they are not related.</p>"
    other_table = "<table><tr><td>Data</td><td>Values</td></tr></table>"
    html = f"<html><body>{toc_table}{blocker}{other_table}</body></html>"
    soup = BeautifulSoup(html, "html.parser")

    tables = soup.find_all("table")
    result = classify_table(tables[1])
    assert result == TYPE_DATA, f"Expected 'data', got '{result}'"


def _table_under(heading):
    rows = "".join(f"<tr><td>Sample {i}</td><td>{v}</td></tr>"
                   for i, v in enumerate(["12", "45", "7", "33"], 1))
    html = f"<html><body><h2>{heading}</h2><table>{rows}</table></body></html>"
    return BeautifulSoup(html, "html.parser").find("table")


def test_numbered_chapter_heading_does_not_make_data_table_a_toc():
    """'Chapter 3' is a section title, not the bare 'CHAPTER' column word of a TOC."""
    assert classify_table(_table_under("Chapter 3")) == TYPE_DATA
    assert classify_table(_table_under("Appendix 2")) == TYPE_DATA


def test_bare_chapter_heading_still_marks_toc():
    assert classify_table(_table_under("CHAPTER")) == TYPE_TOC
