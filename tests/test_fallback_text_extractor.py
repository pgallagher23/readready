# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for converter/fallback_text_extractor.py pure-logic functions.

Covers block merging heuristics and control character detection — the
threshold-sensitive functions that determine whether text is recovered
or discarded during fallback extraction.

Run:  python -m pytest tests/test_fallback_text_extractor.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from converter.fallback_text_extractor import (
    _control_char_ratio,
    _merge_adjacent_blocks,
    _split_text_into_spans,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _block(text, x0, y0, x1, y1):
    """Build a minimal text block dict."""
    return {"text": text, "bbox": (x0, y0, x1, y1)}


# ===================================================================
# _merge_adjacent_blocks
# ===================================================================

class TestMergeAdjacentBlocks:

    def test_empty_input(self):
        assert _merge_adjacent_blocks([]) == []

    def test_single_block_unchanged(self):
        blocks = [_block("Hello", 0, 0, 100, 10)]
        result = _merge_adjacent_blocks(blocks)
        assert len(result) == 1
        assert result[0]["text"] == "Hello"

    def test_close_overlapping_blocks_merged(self):
        """Two blocks with small gap and horizontal overlap → merged."""
        blocks = [
            _block("First line", 10, 0, 200, 12),
            _block("second line", 10, 16, 200, 28),  # gap = 16-12 = 4
        ]
        result = _merge_adjacent_blocks(blocks)
        assert len(result) == 1
        assert "First line" in result[0]["text"]
        assert "second line" in result[0]["text"]

    def test_large_gap_not_merged(self):
        """Two blocks with gap > threshold → separate."""
        blocks = [
            _block("Para 1", 0, 0, 100, 10),
            _block("Para 2", 0, 30, 100, 40),  # gap = 30-10 = 20 > 14
        ]
        result = _merge_adjacent_blocks(blocks)
        assert len(result) == 2

    def test_no_horizontal_overlap_not_merged(self):
        """Blocks side-by-side (no horizontal overlap) → separate."""
        blocks = [
            _block("Left col", 0, 0, 50, 10),
            _block("Right col", 200, 14, 300, 24),  # gap ok, but no h-overlap
        ]
        result = _merge_adjacent_blocks(blocks)
        assert len(result) == 2

    def test_zero_gap_not_merged(self):
        """Gap = 0 (touching blocks) → NOT merged (requires gap > 0)."""
        blocks = [
            _block("A", 0, 0, 100, 10),
            _block("B", 0, 10, 100, 20),  # gap = 10-10 = 0
        ]
        result = _merge_adjacent_blocks(blocks)
        assert len(result) == 2

    def test_negative_gap_not_merged(self):
        """Overlapping blocks (negative gap) → NOT merged."""
        blocks = [
            _block("A", 0, 0, 100, 15),
            _block("B", 0, 10, 100, 25),  # gap = 10-15 = -5
        ]
        result = _merge_adjacent_blocks(blocks)
        assert len(result) == 2

    def test_custom_gap_threshold(self):
        """Custom threshold should control merge distance."""
        blocks = [
            _block("A", 0, 0, 100, 10),
            _block("B", 0, 20, 100, 30),  # gap = 10
        ]
        # Default threshold 14 → merge
        result_default = _merge_adjacent_blocks(blocks)
        assert len(result_default) == 1

        # Strict threshold 5 → no merge
        blocks2 = [
            _block("A", 0, 0, 100, 10),
            _block("B", 0, 20, 100, 30),
        ]
        result_strict = _merge_adjacent_blocks(blocks2, gap_threshold=5.0)
        assert len(result_strict) == 2

    def test_merged_bbox_extends_correctly(self):
        """Merged bbox should take min x0, keep original y0, max x2, extend y3."""
        blocks = [
            _block("A", 10, 0, 200, 12),
            _block("B", 5, 16, 250, 28),  # gap = 4
        ]
        result = _merge_adjacent_blocks(blocks)
        assert len(result) == 1
        bbox = result[0]["bbox"]
        assert bbox[0] == 5    # min(10, 5)
        assert bbox[1] == 0    # original top
        assert bbox[2] == 250  # max(200, 250)
        assert bbox[3] == 28   # extended bottom

    def test_three_blocks_chain_merge(self):
        """Three consecutive close blocks should all merge into one."""
        blocks = [
            _block("Line 1", 0, 0, 200, 10),
            _block("Line 2", 0, 14, 200, 24),   # gap 4
            _block("Line 3", 0, 28, 200, 38),   # gap 4
        ]
        result = _merge_adjacent_blocks(blocks)
        assert len(result) == 1
        assert "Line 1" in result[0]["text"]
        assert "Line 3" in result[0]["text"]

    def test_partial_horizontal_overlap(self):
        """Blocks with >30% horizontal overlap should merge."""
        blocks = [
            _block("A", 0, 0, 100, 10),
            _block("B", 60, 14, 200, 24),  # gap 4, overlap = min(100,200)-max(0,60) = 40, width=200, pct=0.2
        ]
        # 40/200 = 0.2 < 0.3 → should NOT merge
        result = _merge_adjacent_blocks(blocks)
        assert len(result) == 2

    def test_sufficient_horizontal_overlap_merges(self):
        """Blocks with >30% horizontal overlap should merge."""
        blocks = [
            _block("A", 0, 0, 100, 10),
            _block("B", 20, 14, 100, 24),  # gap 4, overlap = min(100,100)-max(0,20) = 80, width=100, pct=0.8
        ]
        result = _merge_adjacent_blocks(blocks)
        assert len(result) == 1

    def test_does_not_mutate_input(self):
        """Original blocks should not be modified."""
        blocks = [
            _block("First", 0, 0, 100, 10),
            _block("Second", 0, 14, 100, 24),
        ]
        original_text_0 = blocks[0]["text"]
        original_bbox_0 = blocks[0]["bbox"]
        original_text_1 = blocks[1]["text"]
        result = _merge_adjacent_blocks(blocks)
        # Input blocks unchanged even though they were merged
        assert blocks[0]["text"] == original_text_0
        assert blocks[0]["bbox"] == original_bbox_0
        assert blocks[1]["text"] == original_text_1
        assert len(blocks) == 2  # no items added/removed from input


# ===================================================================
# _control_char_ratio
# ===================================================================

class TestControlCharRatio:

    def test_clean_text(self):
        assert _control_char_ratio("Hello World") == 0.0

    def test_all_control_chars(self):
        text = "\x01\x02\x03\x04"
        assert _control_char_ratio(text) == 1.0

    def test_mixed_control_and_text(self):
        # 2 control chars (\x01, \x02) + 2 normal chars (A, B) = 50%
        text = "\x01A\x02B"
        ratio = _control_char_ratio(text)
        assert abs(ratio - 0.5) < 0.01

    def test_empty_string(self):
        assert _control_char_ratio("") == 0.0

    def test_whitespace_only(self):
        assert _control_char_ratio("   \t\n  ") == 0.0

    def test_tab_newline_cr_excluded(self):
        """Tab (0x09), LF (0x0A), CR (0x0D) should NOT count as control chars."""
        text = "A\t\nB\r"
        assert _control_char_ratio(text) == 0.0

    def test_boundary_chars(self):
        """0x1B (ESC) is non-whitespace in Python and a control char (ord <= 0x1F).
        Note: 0x1C (FS) and 0x1D (GS) ARE whitespace in Python; 0x1E and 0x1F are NOT."""
        text = "\x1BA"  # 1 control (0x1B ESC) + 1 normal (A)
        ratio = _control_char_ratio(text)
        assert abs(ratio - 0.5) < 0.01

    def test_tex_ligature_chars(self):
        """TeX T1 control chars like 0x1B should register as control.
        Note: 0x1C is whitespace in Python so gets filtered by isspace()."""
        text = "The\x1Brst\x1Cnd"
        # non_ws: T, h, e, \x1B, r, s, t, n, d = 9 chars (\x1C filtered as whitespace)
        # control: \x1B = 1
        ratio = _control_char_ratio(text)
        assert abs(ratio - 1 / 9) < 0.02

    def test_null_byte_counted_as_control(self):
        """Null bytes (0x00) are non-whitespace control chars."""
        text = "\x00AB"  # 1 null + 2 normal
        ratio = _control_char_ratio(text)
        assert abs(ratio - 1 / 3) < 0.01


# ===================================================================
# _split_text_into_spans (fallback version — no URL handling)
# ===================================================================

class TestFallbackSplitTextIntoSpans:

    def test_multi_sentence_split(self):
        text = "First sentence here. Second sentence follows. Third one too."
        result = _split_text_into_spans(text)
        assert len(result) == 3
        assert all(r["tag"] == "Span" for r in result)

    def test_single_sentence_returns_empty(self):
        result = _split_text_into_spans("Just one sentence here.")
        assert result == []

    def test_short_text_returns_empty(self):
        assert _split_text_into_spans("Hi.") == []
        assert _split_text_into_spans("") == []

    def test_no_capital_after_period_no_split(self):
        """Split requires capital after sentence-ending punctuation."""
        result = _split_text_into_spans("end of sentence. no capital here.")
        assert result == []

    def test_question_mark_split(self):
        text = "What happened? Nobody knows. Really strange."
        result = _split_text_into_spans(text)
        assert len(result) == 3

    def test_exclamation_split(self):
        text = "Amazing! This works great. Keep going."
        result = _split_text_into_spans(text)
        assert len(result) == 3
