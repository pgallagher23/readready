# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the filename-fallback title cleanup in _extract_title.

Batch-processed filenames are URL-encoded and carry an 8-hex-digit prefix
(e.g. ``e5f6a7b8_Example%20Investigation%20Process%20Flowchart%2010-25-2017``).
When the fallback fires, the dc:title must not be that junk verbatim.

Run:  python -m pytest tests/test_title_fallback.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from converter.docling_converter import _title_from_filename


def test_percent_decoding_and_hash_prefix_strip():
    assert (
        _title_from_filename(
            "e5f6a7b8_Example%20Investigation%20Process%20Flowchart%2010-25-2017")
        == "Example Investigation Process Flowchart 10 25 2017"
    )


def test_hash_prefix_with_hyphen_separator():
    assert _title_from_filename("f6a7b8c9-Student%20Conduct") == "Student Conduct"


def test_plain_filename_untouched_except_separators():
    assert _title_from_filename("Annual_Report-2024") == "Annual Report 2024"


def test_eight_hex_word_not_a_prefix_is_kept():
    # No separator after the hex run -> not a hash prefix.
    assert _title_from_filename("deadbeef") == "deadbeef"


def test_nine_hex_chars_not_stripped():
    # Prefix strip is exactly 8 hex chars + separator.
    assert _title_from_filename("123456789_Title") == "123456789 Title"


def test_result_never_empty():
    # Stem that cleans to nothing falls back to the raw stem.
    assert _title_from_filename("deadbeef_") == "deadbeef_"


def test_whitespace_collapsed():
    assert _title_from_filename("a__b--c") == "a b c"


def test_yyyymmdd_date_prefix_not_stripped():
    # All-digit 8-char run is valid hex but is a date, not a hash prefix
    # -- stripping it would lose the only distinguishing token (I5).
    assert _title_from_filename("20240426-agenda") == "20240426 agenda"


def test_yyyymmdd_date_prefix_not_stripped_underscore():
    assert _title_from_filename("20190329-ebminutes") == "20190329 ebminutes"
