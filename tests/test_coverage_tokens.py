# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the shared text-coverage tokenizer.

Regression for a course-guide false low-coverage warning:
the source PDF uses pipe separators between course code and title
("ENG 111|English"), parenthesized abbreviations ("(GS)"), and dashes
("Design I: 2-D"). PyMuPDF extracts those as joined tokens (``111|english``)
while the structure tree's /ActualText separates them (``111``, ``english``).
A whitespace-only tokenizer counts the joined source tokens as "missing" even
though the content is fully present and correctly tagged.

``coverage_tokens`` must split on non-alphanumeric boundaries so both sides
tokenize identically, and is the single source of truth shared by
``struct_tree_validator._check_text_coverage`` and
``validation_gates.quick_screen_reader_check``.
"""

from output_gen.struct_tree_validator import coverage_tokens


def _coverage(source: str, tagged: str) -> float:
    src = coverage_tokens(source)
    tag = coverage_tokens(tagged)
    return len(src & tag) / len(src) if src else 1.0


def test_splits_on_pipe_and_punctuation():
    assert coverage_tokens("ENG 111|English (GS)") == {"eng", "111", "english", "gs"}


def test_splits_on_internal_dash():
    assert coverage_tokens("Design I: 2-D") == {"design", "i", "2", "d"}


def test_pipe_joined_source_fully_covered_by_separated_tagged():
    """The crux: pipe-joined source must score 100% against separated tagged."""
    source = "BUS 248|Business Communications\nENG 111|English"
    tagged = "BUS 248 Business Communications ENG 111 English"
    assert _coverage(source, tagged) == 1.0


def test_parenthesized_abbreviation_matches_bare():
    assert _coverage("General Studies (GS)", "General Studies GS") == 1.0


def test_dehyphenates_linebreak():
    # "geogra-\nphy" in source should match "geography" in tagged.
    assert _coverage("the geogra-\nphy class", "the geography class") == 1.0


def test_genuinely_missing_content_still_counts_as_missing():
    """Real dropped words must still register — the fix must not mask gaps."""
    source = "alpha bravo charlie delta echo"
    tagged = "alpha bravo charlie"  # delta + echo genuinely absent
    assert _coverage(source, tagged) == 0.6
