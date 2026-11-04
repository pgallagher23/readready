# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for converter/docling_converter.py pure-logic functions.

Covers table grid normalization, columnar-text detection, and text/link
splitting — the functions whose bugs cascade into tagger column-count
failures and remediation mismatches.

Run:  python -m pytest tests/test_docling_converter.py -v
"""

import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from converter.docling_converter import (
    _is_columnar_text_table,
    _normalize_table_grid,
    _split_text_into_spans,
    _split_text_with_links,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cell(row, col, text="", row_span=1, col_span=1,
          is_header=False, is_col_header=False, is_row_header=False):
    return {
        "row": row, "col": col, "text": text,
        "row_span": row_span, "col_span": col_span,
        "is_header": is_header,
        "is_col_header": is_col_header,
        "is_row_header": is_row_header,
    }


def _coverage(cells):
    """Build set of (row, col) positions covered by cells and their spans."""
    covered = set()
    for c in cells:
        for dr in range(c.get("row_span", 1) or 1):
            for dc in range(c.get("col_span", 1) or 1):
                covered.add((c["row"] + dr, c["col"] + dc))
    return covered


# ===================================================================
# _normalize_table_grid
# ===================================================================

class TestNormalizeTableGrid:

    def test_complete_grid_no_change(self):
        """A full 2x2 grid should not add any cells."""
        cells = [
            _cell(0, 0, "A"), _cell(0, 1, "B"),
            _cell(1, 0, "C"), _cell(1, 1, "D"),
        ]
        result = _normalize_table_grid(cells)
        assert len(result) == 4

    def test_fills_missing_cell(self):
        """A 2x2 grid with one cell missing should get a placeholder."""
        cells = [
            _cell(0, 0, "A"), _cell(0, 1, "B"),
            _cell(1, 0, "C"),
            # (1, 1) is missing
        ]
        result = _normalize_table_grid(cells)
        assert len(result) == 4
        added = [c for c in result if c["text"] == "" and c["row"] == 1 and c["col"] == 1]
        assert len(added) == 1
        assert added[0]["row_span"] == 1
        assert added[0]["col_span"] == 1

    def test_respects_colspan_coverage(self):
        """Cells covered by colspan should NOT get placeholders."""
        cells = [
            _cell(0, 0, "Wide", col_span=3),
            _cell(1, 0, "A"), _cell(1, 1, "B"), _cell(1, 2, "C"),
        ]
        result = _normalize_table_grid(cells)
        # (0,0), (0,1), (0,2) covered by colspan; (1,0-2) explicit
        assert len(result) == 4  # no placeholders needed

    def test_respects_rowspan_coverage(self):
        """Cells covered by rowspan should NOT get placeholders."""
        cells = [
            _cell(0, 0, "Tall", row_span=2), _cell(0, 1, "B"),
            # (1, 0) covered by rowspan
            _cell(1, 1, "D"),
        ]
        result = _normalize_table_grid(cells)
        assert len(result) == 3  # no placeholders needed
        coverage = _coverage(result)
        assert (1, 0) in coverage  # covered by rowspan

    def test_combined_rowspan_colspan(self):
        """2x2 block via colspan+rowspan should cover all positions."""
        cells = [
            _cell(0, 0, "Big", row_span=2, col_span=2), _cell(0, 2, "C"),
            # (1, 0) and (1, 1) covered by span
            _cell(1, 2, "F"),
        ]
        result = _normalize_table_grid(cells)
        assert len(result) == 3
        coverage = _coverage(result)
        for pos in [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)]:
            assert pos in coverage

    def test_clips_overlapping_colspan(self):
        """Overlapping colspans should be clipped before gap-filling."""
        # Cell at col=0 with col_span=3, next cell at col=2 → overlap at col 2
        cells = [
            _cell(0, 0, "A", col_span=3), _cell(0, 2, "B"),
            _cell(1, 0, "C"), _cell(1, 1, "D"), _cell(1, 2, "E"),
        ]
        result = _normalize_table_grid(cells)
        # After clipping, cell A should have col_span=2 (clipped from 3)
        cell_a = [c for c in result if c["text"] == "A"][0]
        assert cell_a["col_span"] == 2

    def test_empty_input(self):
        assert _normalize_table_grid([]) == []

    def test_single_cell(self):
        cells = [_cell(0, 0, "Only")]
        result = _normalize_table_grid(cells)
        assert len(result) == 1

    def test_sparse_grid_fills_all_gaps(self):
        """A 3x3 grid with only diagonal cells should fill 6 gaps."""
        cells = [
            _cell(0, 0, "A"),
            _cell(1, 1, "B"),
            _cell(2, 2, "C"),
        ]
        result = _normalize_table_grid(cells)
        assert len(result) == 9  # 3 original + 6 placeholders
        coverage = _coverage(result)
        for r in range(3):
            for c in range(3):
                assert (r, c) in coverage

    def test_placeholder_cells_have_correct_shape(self):
        """All placeholder cells should have expected keys and default values."""
        cells = [_cell(0, 0, "A"), _cell(1, 1, "B")]
        result = _normalize_table_grid(cells)
        placeholders = [c for c in result if c["text"] == ""]
        for p in placeholders:
            assert p["row_span"] == 1
            assert p["col_span"] == 1
            assert p["is_header"] is False
            assert p["is_col_header"] is False
            assert p["is_row_header"] is False

    def test_mutates_input_list_in_place(self):
        """_normalize_table_grid appends placeholders to the original list."""
        cells = [_cell(0, 0, "A"), _cell(1, 1, "B")]
        result = _normalize_table_grid(cells)
        assert result is cells  # same object, mutated in place
        assert len(cells) == 4  # 2 original + 2 placeholders

    def test_duplicate_cells_at_same_position(self):
        """Two cells at the same (row, col) — both should remain (no dedup)."""
        cells = [
            _cell(0, 0, "A"), _cell(0, 0, "B"),  # duplicate position
            _cell(0, 1, "C"),
            _cell(1, 0, "D"), _cell(1, 1, "E"),
        ]
        result = _normalize_table_grid(cells)
        # Both A and B should still be in the list
        texts = [c["text"] for c in result]
        assert "A" in texts
        assert "B" in texts


# ===================================================================
# _is_columnar_text_table
# ===================================================================

class TestIsColumnarTextTable:

    def _make_columnar_cells(self, n_rows=15, multi_numeric=True):
        """Build cells that mimic columnar numeric data."""
        cells = []
        for r in range(n_rows):
            if multi_numeric:
                text = f"{r * 1.5:.1f} {r * 2.3:.1f} {r * 0.8:.1f}"
            else:
                text = f"Label {r}"
            cells.append(_cell(r, 0, text))
            cells.append(_cell(r, 1, f"Data {r}"))
        return cells

    def test_columnar_numeric_detected(self):
        """Table with >70% multi-numeric first-col cells → True."""
        cells = self._make_columnar_cells(n_rows=15)
        assert _is_columnar_text_table(cells) is True

    def test_non_numeric_not_detected(self):
        """Table with text labels in first column → False."""
        cells = self._make_columnar_cells(n_rows=15, multi_numeric=False)
        assert _is_columnar_text_table(cells) is False

    def test_too_few_cells_returns_false(self):
        """< 20 cells → always False."""
        cells = [_cell(r, 0, "1.0 2.0") for r in range(9)]
        assert _is_columnar_text_table(cells) is False

    def test_too_few_rows_returns_false(self):
        """< 10 rows → always False."""
        cells = []
        for r in range(8):
            cells.extend([_cell(r, c, "1.0 2.0") for c in range(3)])
        assert _is_columnar_text_table(cells) is False

    def test_header_cells_returns_false(self):
        """Any header cell → False (real tables have headers)."""
        cells = self._make_columnar_cells(n_rows=15)
        cells[0]["is_header"] = True
        assert _is_columnar_text_table(cells) is False

    def test_col_header_returns_false(self):
        cells = self._make_columnar_cells(n_rows=15)
        cells[0]["is_col_header"] = True
        assert _is_columnar_text_table(cells) is False

    def test_empty_input(self):
        assert _is_columnar_text_table([]) is False

    def test_threshold_boundary(self):
        """Exactly 70% multi-numeric should NOT trigger (> 0.7 required)."""
        cells = []
        for r in range(10):
            if r < 7:  # 7/10 = 70% = not > 0.7
                cells.append(_cell(r, 0, "1.0 2.0 3.0"))
            else:
                cells.append(_cell(r, 0, "Label text"))
            cells.append(_cell(r, 1, "data"))
        assert _is_columnar_text_table(cells) is False

    def test_scientific_notation_negative_exp(self):
        """Scientific notation with negative exponent (1.5E-3) matches the regex."""
        cells = []
        for r in range(15):
            # Regex: ^-?\.?\d[\d.]*(?:[Ee]-?\d+)?$  — only -? not [+-]?
            cells.append(_cell(r, 0, f"1.5E-{r + 1} 2.3e-{r + 1}"))
            cells.append(_cell(r, 1, "data"))
        assert _is_columnar_text_table(cells) is True


# ===================================================================
# _split_text_into_spans / _split_text_with_links
# ===================================================================

class TestSplitTextIntoSpans:

    def test_multi_sentence_split(self):
        text = "First sentence here. Second sentence follows. Third one too."
        result = _split_text_into_spans(text)
        assert len(result) == 3
        assert all(r["tag"] == "Span" for r in result)

    def test_single_sentence_returns_empty(self):
        """Single sentence without URLs → no split needed."""
        result = _split_text_into_spans("Just one sentence here.")
        assert result == []

    def test_short_text_returns_empty(self):
        """Text < 10 chars → no split."""
        assert _split_text_into_spans("Hi.") == []
        assert _split_text_into_spans("") == []

    def test_url_detection_single_sentence(self):
        """Single sentence with URL → split into Span + Link + Span."""
        text = "Visit https://example.com for more info."
        result = _split_text_into_spans(text)
        assert len(result) == 3
        assert result[0] == {"tag": "Span", "text": "Visit"}
        assert result[1] == {"tag": "Link", "text": "https://example.com"}
        assert result[2] == {"tag": "Span", "text": "for more info."}

    def test_url_in_multi_sentence(self):
        """URL embedded in multi-sentence text."""
        text = "First sentence. Visit https://example.com for details. Last sentence."
        result = _split_text_into_spans(text)
        tags = [r["tag"] for r in result]
        assert tags.count("Link") == 1
        assert tags.count("Span") >= 2
        # Verify the Link is the correct URL
        links = [r for r in result if r["tag"] == "Link"]
        assert links[0]["text"] == "https://example.com"

    def test_none_input(self):
        assert _split_text_into_spans(None) == []

    def test_whitespace_only(self):
        assert _split_text_into_spans("      ") == []


class TestSplitTextWithLinks:

    URL_PATTERN = re.compile(r'(https?://[^\s<>"\')\]]+|www\.[^\s<>"\')\]]+)')

    def test_text_with_url(self):
        result = _split_text_with_links("Check https://example.com now", self.URL_PATTERN)
        assert len(result) == 3  # "Check" + URL + "now"
        assert result[0] == {"tag": "Span", "text": "Check"}
        assert result[1] == {"tag": "Link", "text": "https://example.com"}
        assert result[2] == {"tag": "Span", "text": "now"}

    def test_url_only(self):
        result = _split_text_with_links("https://example.com", self.URL_PATTERN)
        assert len(result) == 1
        assert result[0]["tag"] == "Link"

    def test_text_starting_with_url(self):
        result = _split_text_with_links("https://example.com is great", self.URL_PATTERN)
        assert result[0]["tag"] == "Link"
        assert result[1]["tag"] == "Span"

    def test_multiple_urls(self):
        result = _split_text_with_links(
            "See https://a.com and https://b.com for details",
            self.URL_PATTERN
        )
        links = [r for r in result if r["tag"] == "Link"]
        assert len(links) == 2

    def test_no_urls(self):
        result = _split_text_with_links("Plain text here", self.URL_PATTERN)
        assert len(result) == 1
        assert result[0]["tag"] == "Span"

    def test_www_url(self):
        result = _split_text_with_links("Visit www.example.com today", self.URL_PATTERN)
        links = [r for r in result if r["tag"] == "Link"]
        assert len(links) == 1
        assert "www.example.com" in links[0]["text"]

    def test_adjacent_urls_no_text_between(self):
        """Two URLs with only whitespace between them → two Links, no Span."""
        result = _split_text_with_links("https://a.com https://b.com", self.URL_PATTERN)
        tags = [r["tag"] for r in result]
        assert tags.count("Link") == 2
        # No meaningful text between the URLs
        spans = [r for r in result if r["tag"] == "Span"]
        for s in spans:
            assert s["text"].strip() == "" or len(s["text"].strip()) > 0
