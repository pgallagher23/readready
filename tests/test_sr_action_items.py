# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the screen-reader action-item builder (app._sr_action_items).

The UI no longer shows a "Screen Reader Quality: Fair" grade; instead it turns
the sr_quality dict into a list of page-specific, actionable items. This helper
is the single source of truth for all display surfaces.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

app = pytest.importorskip("app")


def _items_by_key(sr):
    return {it["key"]: it for it in app._sr_action_items(sr)}


def test_clean_document_yields_no_items():
    sr = {
        "sr_quality": "good", "element_match_rate": 95.0, "avg_coverage": 0.98,
        "hollow_pages": [], "low_coverage_pages": [], "page_deficits": [],
        "per_page_ocr_quality": [], "reading_order_failing_pages": [],
        "figures_missing_alt": 0, "scanned_pages": [], "total_pages": 10,
    }
    assert app._sr_action_items(sr) == []


def test_hollow_pages_item_is_critical_with_pages():
    it = _items_by_key({"hollow_pages": [3, 5]})["hollow"]
    assert it["severity"] == "critical"
    assert it["pages"] == [3, 5]


def test_low_coverage_lists_pages_and_percentages():
    it = _items_by_key({"low_coverage_pages": [
        {"page": 4, "coverage": 0.47}, {"page": 7, "coverage": 0.61}]})["low_coverage"]
    assert it["severity"] == "major"
    assert it["pages"] == [4, 7]
    assert "47" in it["text"] and "61" in it["text"]


def test_page_deficit_item_names_pages():
    it = _items_by_key({"page_deficits": [
        {"page": 4, "deficit_pct": 53.0}, {"page": 7, "deficit_pct": 40.0}]})["page_deficit"]
    assert it["severity"] == "major"
    assert it["pages"] == [4, 7]
    assert "link" in it["text"].lower()


def test_ocr_poor_pages_only():
    it = _items_by_key({"per_page_ocr_quality": [
        {"page": 6, "label": "poor"}, {"page": 8, "label": "good"}]})["ocr"]
    assert it["pages"] == [6]


def test_reading_order_item_carries_content_editor_link():
    it = _items_by_key({"reading_order_failing_pages": [16, 18, 19]})["reading_order"]
    assert it["pages"] == [16, 18, 19]
    assert it["html"] and "_gotoContentEditor" in it["html"]


def test_figures_missing_alt_is_page_less_count():
    it = _items_by_key({"figures_missing_alt": 3})["figures"]
    assert it["pages"] == []
    assert "3" in it["text"]


def test_emr_fallback_when_no_page_deficits():
    it = _items_by_key({
        "element_match_rate": 84.5, "page_deficits": [],
        "scanned_pages": [], "total_pages": 72,
    })["emr"]
    assert it["pages"] == []
    assert "84" in it["text"]


def test_emr_suppressed_when_page_deficits_present():
    keys = _items_by_key({
        "element_match_rate": 84.5,
        "page_deficits": [{"page": 4, "deficit_pct": 53.0}],
        "total_pages": 72,
    })
    assert "emr" not in keys
    assert "page_deficit" in keys


def test_emr_suppressed_for_fully_scanned_doc():
    keys = _items_by_key({
        "element_match_rate": 40.0, "page_deficits": [],
        "scanned_pages": [1, 2, 3], "total_pages": 3,
    })
    assert "emr" not in keys


def test_sr_is_poor_reflects_categorical():
    assert app._sr_is_poor({"sr_quality": "poor"}) is True
    assert app._sr_is_poor({"sr_quality": "fair"}) is False
    assert app._sr_is_poor({}) is False
