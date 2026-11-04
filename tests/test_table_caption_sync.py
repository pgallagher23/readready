# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for robust HTML-table <-> struct-JSON table matching and the
caption/header sync functions in output_gen.pdf_tagger.

Pure-Python: no JAR, VLM, or network needed.

Run:  python -m pytest tests/test_table_caption_sync.py -v
"""

import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup

from output_gen.pdf_tagger import (
    _table_fingerprint,
    _match_html_tables_to_struct,
    _sync_table_captions_from_html,
    _sync_table_headers_from_html,
)


def _html_table(rows, caption=None):
    """Build a bs4 <table> element. rows = list of list of cell text."""
    parts = ["<table>"]
    if caption:
        parts.append(f"<caption>{caption}</caption>")
    for r in rows:
        parts.append("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>")
    parts.append("</table>")
    return BeautifulSoup("".join(parts), "html.parser").find("table")


def _struct_table(rows):
    """Build a struct-JSON TableItem dict. rows = list of list of cell text."""
    cells = []
    for ri, row in enumerate(rows):
        for ci, txt in enumerate(row):
            cells.append({"row": ri, "col": ci, "text": txt})
    return {"type": "TableItem", "tag": "Table", "cells": cells}


# == _table_fingerprint ==

def test_fingerprint_normalizes_case_and_whitespace():
    fp = _table_fingerprint(["  Region ", "REGION", "re  gion"])
    assert fp == {"region"}


# == _match_html_tables_to_struct ==

def test_match_equal_counts_matching_text():
    html = [_html_table([["Region", "Revenue"], ["North", "100"]]),
            _html_table([["Year", "Sales"], ["2020", "5"]])]
    struct = [_struct_table([["Region", "Revenue"], ["North", "100"]]),
              _struct_table([["Year", "Sales"], ["2020", "5"]])]
    assert _match_html_tables_to_struct(html, struct) == {0: 0, 1: 1}


def test_match_equal_counts_diverging_text_uses_position():
    # Struct cell text shares NOTHING with any HTML table (the original
    # silent-drop trigger). Positional fallback must still map diagonally.
    html = [_html_table([["Region", "Revenue"]]),
            _html_table([["Year", "Sales"]])]
    struct = [_struct_table([["XXX", "YYY"]]),
              _struct_table([["ZZZ", "WWW"]])]
    assert _match_html_tables_to_struct(html, struct) == {0: 0, 1: 1}


def test_match_reorder_keeps_positional_and_warns(caplog):
    html = [_html_table([["Region", "Revenue"]]),
            _html_table([["Year", "Sales"]])]
    struct = [_struct_table([["Year", "Sales"]]),       # text matches html[1]
              _struct_table([["Region", "Revenue"]])]   # text matches html[0]
    with caplog.at_level(logging.WARNING):
        m = _match_html_tables_to_struct(html, struct)
    assert m == {0: 0, 1: 1}                            # positional kept
    assert "keeping positional match" in caplog.text


def test_match_unequal_counts_greedy(caplog):
    html = [_html_table([["A", "B"]]),
            _html_table([["C", "D"]]),
            _html_table([["E", "F"]])]
    struct = [_struct_table([["C", "D"]]),   # matches html[1]
              _struct_table([["E", "F"]])]    # matches html[2]
    with caplog.at_level(logging.WARNING):
        m = _match_html_tables_to_struct(html, struct)
    assert m == {0: 1, 1: 2}
    assert "count mismatch" in caplog.text


def test_match_empty_inputs():
    assert _match_html_tables_to_struct([], []) == {}
    assert _match_html_tables_to_struct([_html_table([["A"]])], []) == {}


# == _sync_table_captions_from_html (integration) ==

def test_sync_caption_written_despite_text_divergence(tmp_path):
    html = tmp_path / "doc.html"
    html.write_text(
        "<html><body>"
        "<table><caption>Quarterly revenue by region</caption>"
        "<tr><td>Region</td><td>Revenue</td></tr></table>"
        "</body></html>"
    )
    struct_path = tmp_path / "struct.json"
    # Struct cell text DIFFERS from HTML (OCR/remediation divergence) — the
    # old matcher dropped the caption here.
    struct = {"elements": [{
        "type": "TableItem", "tag": "Table",
        "cells": [{"row": 0, "col": 0, "text": "REGION_X"},
                  {"row": 0, "col": 1, "text": "REVENUE_Y"}],
    }]}
    struct_path.write_text(json.dumps(struct))

    _sync_table_captions_from_html(str(struct_path), str(html))

    out = json.loads(struct_path.read_text())
    assert out["elements"][0]["caption"] == "Quarterly revenue by region"


def test_sync_caption_unmatched_is_logged(tmp_path, caplog):
    html = tmp_path / "doc.html"
    html.write_text(
        "<html><body>"
        "<table><caption>Orphan caption ABC</caption>"
        "<tr><td>X</td></tr></table>"
        "</body></html>"
    )
    struct_path = tmp_path / "struct.json"
    struct_path.write_text(json.dumps({"elements": []}))  # no struct tables

    with caplog.at_level(logging.WARNING):
        _sync_table_captions_from_html(str(struct_path), str(html))

    assert "Orphan caption ABC" in caplog.text


# == _sync_table_headers_from_html (integration) ==

def test_sync_headers_despite_text_divergence(tmp_path):
    html = tmp_path / "doc.html"
    html.write_text(
        "<html><body><table>"
        "<tr><th scope='col'>Region</th><th scope='col'>Revenue</th></tr>"
        "<tr><td>North</td><td>100</td></tr>"
        "</table></body></html>"
    )
    struct_path = tmp_path / "struct.json"
    # Struct cell text DIFFERS from HTML — old matcher skipped this table.
    struct = {"elements": [{
        "type": "TableItem", "tag": "Table",
        "cells": [
            {"row": 0, "col": 0, "text": "AAA", "is_header": False},
            {"row": 0, "col": 1, "text": "BBB", "is_header": False},
            {"row": 1, "col": 0, "text": "ccc"},
            {"row": 1, "col": 1, "text": "ddd"},
        ],
    }]}
    struct_path.write_text(json.dumps(struct))

    _sync_table_headers_from_html(str(struct_path), str(html))

    cells = json.loads(struct_path.read_text())["elements"][0]["cells"]
    assert cells[0]["is_header"] is True
    assert cells[0]["is_col_header"] is True
    assert cells[1]["is_col_header"] is True


def test_sync_caption_unmatched_in_unequal_counts_is_logged(tmp_path, caplog):
    # Two captioned HTML tables but only one struct table → one HTML
    # caption cannot be matched and must be logged (not silently dropped).
    html = tmp_path / "doc.html"
    html.write_text(
        "<html><body>"
        "<table><caption>Matched caption</caption>"
        "<tr><td>Region</td><td>Revenue</td></tr></table>"
        "<table><caption>Orphan caption XYZ</caption>"
        "<tr><td>Year</td><td>Sales</td></tr></table>"
        "</body></html>"
    )
    struct_path = tmp_path / "struct.json"
    struct = {"elements": [{
        "type": "TableItem", "tag": "Table",
        "cells": [{"row": 0, "col": 0, "text": "Region"},
                  {"row": 0, "col": 1, "text": "Revenue"}],
    }]}
    struct_path.write_text(json.dumps(struct))

    with caplog.at_level(logging.WARNING):
        _sync_table_captions_from_html(str(struct_path), str(html))

    out = json.loads(struct_path.read_text())
    # The matched table got its caption...
    assert out["elements"][0]["caption"] == "Matched caption"
    # ...and the unmatched HTML caption was logged, not silently dropped.
    assert "Orphan caption XYZ" in caplog.text
