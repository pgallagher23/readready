# SPDX-License-Identifier: AGPL-3.0-or-later
"""An empty VLM completion must not be written as alt text (G-29).

Run: python -m pytest tests/test_vlm_alt_guard.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup

from vlm.alt_text_generator import _apply_vlm_results


def _one_image_item():
    soup = BeautifulSoup('<img src="a.png" alt="">', "html.parser")
    tag = soup.find("img")
    return soup, [{"src": "a.png", "tag": tag, "img_file": "/tmp/a.png",
                   "page_number": 1}]


def test_empty_completion_is_skipped_not_counted_as_generated():
    soup, items = _one_image_item()
    updated, skipped, records, _alts = _apply_vlm_results(
        items, {"a.png": {"alt_text": "", "confidence": 90}}
    )
    assert updated == 0, "an empty completion is not a generated description"
    assert skipped == 1
    assert records[0]["outcome"] == "skipped"
    assert records[0]["skip_reason"]
    assert soup.find("img")["alt"] == "", "must not write an empty alt as success"


def test_too_short_completion_is_skipped():
    soup, items = _one_image_item()
    updated, skipped, records, _alts = _apply_vlm_results(
        items, {"a.png": {"alt_text": "ok", "confidence": 90}}
    )
    assert updated == 0 and skipped == 1
    assert records[0]["outcome"] == "skipped"


def test_real_description_is_still_written():
    soup, items = _one_image_item()
    updated, skipped, records, _alts = _apply_vlm_results(
        items, {"a.png": {"alt_text": "A red barn beside a silo", "confidence": 90}}
    )
    assert updated == 1 and skipped == 0
    assert records[0]["outcome"] == "generated"
    assert soup.find("img")["alt"] == "A red barn beside a silo"


def test_decorative_is_still_honoured():
    soup, items = _one_image_item()
    updated, skipped, records, _alts = _apply_vlm_results(
        items, {"a.png": {"alt_text": "decorative", "confidence": 90}}
    )
    assert records[0]["outcome"] == "decorative"
    assert soup.find("img").get("role") == "presentation"


def test_none_completion_does_not_crash():
    """result.get("alt_text", "") returns None when the key is present-but-null.

    The pre-existing alt_text.lower() call would raise AttributeError on that;
    the guard must absorb it rather than let a per-image null kill the phase.
    """
    _soup, items = _one_image_item()
    updated, skipped, records, _alts = _apply_vlm_results(
        items, {"a.png": {"alt_text": None, "confidence": 90}}
    )
    assert updated == 0 and skipped == 1
    assert records[0]["outcome"] == "skipped"


def test_skipped_early_seeds_counters_and_records():
    """skipped_early carries images rejected before the VLM ran."""
    _soup, items = _one_image_item()
    early = [{"src": "gone.png", "file_path": None, "alt_text": None,
              "outcome": "skipped", "skip_reason": "Image file not found on disk",
              "page_number": 2}]
    updated, skipped, records, _alts = _apply_vlm_results(
        items, {"a.png": {"alt_text": "A red barn beside a silo"}}, early
    )
    assert updated == 1
    assert skipped == 1, "the early skip must still be counted"
    assert records[0]["src"] == "gone.png"
    assert len(records) == 2
