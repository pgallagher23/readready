# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for whitespace-only page detection in content_matcher."""
import pytest
from output_gen.content_matcher import _is_whitespace_only_page


def test_all_space_runs_detected():
    """Page with only space characters should be whitespace-only."""
    runs = [
        {"text": " ", "render_mode": 0},
        {"text": " ", "render_mode": 0},
        {"text": " ", "render_mode": 0},
    ]
    assert _is_whitespace_only_page(runs) is True


def test_empty_runs_detected():
    """Page with empty text runs should be whitespace-only."""
    runs = [
        {"text": "", "render_mode": 0},
        {"text": "  ", "render_mode": 0},
    ]
    assert _is_whitespace_only_page(runs) is True


def test_no_runs_detected():
    """Page with zero text runs should be whitespace-only."""
    assert _is_whitespace_only_page([]) is True


def test_real_text_not_detected():
    """Page with actual text should NOT be whitespace-only."""
    runs = [
        {"text": "Hello", "render_mode": 0},
        {"text": " ", "render_mode": 0},
    ]
    assert _is_whitespace_only_page(runs) is False


def test_invisible_text_ignored():
    """Invisible text (render_mode=3) should be excluded from check."""
    runs = [
        {"text": "Hidden OCR text", "render_mode": 3},
        {"text": " ", "render_mode": 0},
    ]
    assert _is_whitespace_only_page(runs) is True


def test_image_match_suppressed_on_whitespace_page():
    """On whitespace-only pages with OCR elements, images should not match."""
    docling_structure = {
        "metadata": {"num_pages": 1},
        "pages": [{"page_no": 1, "width": 612, "height": 792}],
        "elements": [
            {
                "id": "elem_0",
                "tag": "Figure",
                "page_no": 1,
                "text": "",
                "alt_text": "Example University letterhead",
                "bbox": {"l": 40, "b": 324, "r": 572, "t": 735},
            },
            {
                "id": "elem_1",
                "tag": "H1",
                "page_no": 1,
                "text": "The Graduate College",
                "bbox": {"l": 216, "b": 677, "r": 351, "t": 691},
            },
        ],
    }
    cs_analysis = {
        "page_analyses": [
            {
                "page": 1,
                "width": 612,
                "height": 792,
                "rotation": 0,
                "text_runs": [
                    {"index": 0, "text": " ", "render_mode": 0, "x": 72, "y": 307,
                     "width": 1, "height": 11, "end_x": 73, "font": "R8", "size": 11},
                ],
                "images": [
                    {"index": 0, "x": 40.56, "y": 324.24,
                     "width": 531.36, "height": 410.64,
                     "bits_per_component": 8},
                ],
                "text_run_count": 1,
                "image_count": 1,
                "xobject_count": 0,
            }
        ],
    }

    from output_gen.content_matcher import match_elements_to_content

    enriched = match_elements_to_content(docling_structure, cs_analysis)

    # Figure should NOT have image_run_indices (suppressed)
    fig = next(e for e in enriched["elements"] if e["tag"] == "Figure")
    assert fig.get("image_run_indices") is None, (
        "Image should not be matched to Figure on whitespace-only page"
    )

    # Page 1 should be classified as overlay
    assert 1 in enriched.get("overlay_pages", []), (
        "Whitespace-only page with OCR elements should be overlay"
    )


def test_list_no_whitespace_run_match():
    """List element on whitespace-only page should not match space runs."""
    docling_structure = {
        "metadata": {"num_pages": 1},
        "pages": [{"page_no": 1, "width": 612, "height": 792}],
        "elements": [
            {
                "id": "elem_0",
                "tag": "Figure",
                "page_no": 1,
                "text": "",
                "alt_text": "Example University letterhead",
                "bbox": {"l": 40, "b": 324, "r": 572, "t": 735},
            },
            {
                "id": "elem_1",
                "tag": "H1",
                "page_no": 1,
                "text": "The Graduate College",
                "bbox": {"l": 216, "b": 677, "r": 351, "t": 691},
            },
            {
                "id": "elem_2",
                "tag": "L",
                "page_no": 1,
                "text": "",
                "bbox": {"l": 74, "b": 327, "r": 543, "t": 554},
                "children": [
                    {
                        "tag": "LI",
                        "text": "Please type all information directly.",
                        "bbox": {"l": 75, "b": 543, "r": 539, "t": 554},
                        "page_no": 1,
                    },
                    {
                        "tag": "LI",
                        "text": "Gather signatures from the following:",
                        "bbox": {"l": 75, "b": 516, "r": 270, "t": 527},
                        "page_no": 1,
                    },
                ],
            },
        ],
    }
    cs_analysis = {
        "page_analyses": [
            {
                "page": 1,
                "width": 612,
                "height": 792,
                "rotation": 0,
                "text_runs": [
                    {"index": 0, "text": " ", "render_mode": 0, "x": 72, "y": 307,
                     "width": 1, "height": 11, "end_x": 73, "font": "R8", "size": 11},
                    {"index": 1, "text": " ", "render_mode": 0, "x": 72, "y": 282,
                     "width": 1, "height": 11, "end_x": 73, "font": "R8", "size": 11},
                ],
                "images": [
                    {"index": 0, "x": 40.56, "y": 324.24,
                     "width": 531.36, "height": 410.64,
                     "bits_per_component": 8},
                ],
                "text_run_count": 2,
                "image_count": 1,
                "xobject_count": 0,
            }
        ],
    }

    from output_gen.content_matcher import match_elements_to_content

    enriched = match_elements_to_content(docling_structure, cs_analysis)

    # List should NOT have content_run_count (whitespace runs suppressed)
    lst = next(e for e in enriched["elements"] if e["tag"] == "L")
    assert lst.get("content_run_count", 0) == 0, (
        f"List should have 0 content runs on whitespace page, got {lst.get('content_run_count')}"
    )

    # Page 1 should be overlay
    assert 1 in enriched.get("overlay_pages", [])
