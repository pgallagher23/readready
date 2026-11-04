# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for cover page Figure treatment in pdf_tagger."""
from output_gen.pdf_tagger import _apply_cover_page_figure_treatment


def _make_enriched(elements, overlay_pages, page_images=None, page_width=612, page_height=792):
    """Helper to build a minimal enriched structure."""
    if page_images is None:
        page_images = [{"index": 0, "x": 0, "y": 0, "width": 612, "height": 792}]
    cs_analysis = {
        "page_analyses": [
            {"page": 1, "width": page_width, "height": page_height, "images": page_images, "text_runs": []},
            {"page": 2, "width": 612, "height": 792, "images": [], "text_runs": [{"text": "hello", "render_mode": 0}]},
        ]
    }
    enriched = {
        "elements": elements,
        "overlay_pages": overlay_pages,
    }
    return enriched, cs_analysis


class TestCoverPageDetection:
    def test_triggers_when_page1_all_overlay(self):
        """Page 1 in overlay_pages + all elements have 0 content runs → triggers."""
        elements = [
            {"id": "e1", "tag": "P", "page_no": 1, "text": "Title", "bbox": {"l": 50, "b": 600, "r": 500, "t": 650}, "content_run_count": 0},
            {"id": "e2", "tag": "P", "page_no": 1, "text": "Subtitle", "bbox": {"l": 50, "b": 500, "r": 400, "t": 530}, "content_run_count": 0},
            {"id": "e3", "tag": "P", "page_no": 2, "text": "Body", "bbox": {"l": 50, "b": 700, "r": 500, "t": 730}, "content_run_count": 5},
        ]
        enriched, cs = _make_enriched(elements, overlay_pages=[1])
        result = _apply_cover_page_figure_treatment(enriched, cs)

        page1_elems = [e for e in result["elements"] if e.get("page_no") == 1]
        assert len(page1_elems) == 1
        assert page1_elems[0]["tag"] == "Figure"
        assert page1_elems[0]["alt_text"] == "Cover page: Title. Subtitle"
        assert page1_elems[0]["image_run_indices"] == [{"page": 1, "image_index": 0}]
        page2_elems = [e for e in result["elements"] if e.get("page_no") == 2]
        assert len(page2_elems) == 1
        assert page2_elems[0]["tag"] == "P"
        assert 1 not in result["overlay_pages"]

    def test_no_trigger_when_page1_has_content_runs(self):
        """Page 1 has content runs → no trigger."""
        elements = [
            {"id": "e1", "tag": "H1", "page_no": 1, "text": "Title", "bbox": {"l": 50, "b": 600, "r": 500, "t": 650}, "content_run_count": 3},
        ]
        enriched, cs = _make_enriched(elements, overlay_pages=[])
        result = _apply_cover_page_figure_treatment(enriched, cs)
        assert result["elements"] == elements

    def test_no_trigger_when_page1_not_overlay(self):
        """Page 1 not in overlay_pages → no trigger even with 0 runs."""
        elements = [
            {"id": "e1", "tag": "H1", "page_no": 1, "text": "Title", "bbox": {"l": 50, "b": 600, "r": 500, "t": 650}, "content_run_count": 0},
        ]
        enriched, cs = _make_enriched(elements, overlay_pages=[2])
        result = _apply_cover_page_figure_treatment(enriched, cs)
        assert result["elements"] == elements

    def test_no_trigger_mixed_overlay_and_content(self):
        """Page 1 has mix of overlay + content-matched elements → no trigger."""
        elements = [
            {"id": "e1", "tag": "H1", "page_no": 1, "text": "Title", "bbox": {"l": 50, "b": 600, "r": 500, "t": 650}, "content_run_count": 2},
            {"id": "e2", "tag": "P", "page_no": 1, "text": "Sub", "bbox": {"l": 50, "b": 500, "r": 400, "t": 530}, "content_run_count": 0},
        ]
        enriched, cs = _make_enriched(elements, overlay_pages=[1])
        result = _apply_cover_page_figure_treatment(enriched, cs)
        page1_elems = [e for e in result["elements"] if e.get("page_no") == 1]
        assert len(page1_elems) == 2

    def test_no_trigger_when_no_page1_elements(self):
        """No elements on page 1 → no trigger (blank page)."""
        elements = [
            {"id": "e1", "tag": "P", "page_no": 2, "text": "Body", "bbox": {"l": 50, "b": 700, "r": 500, "t": 730}, "content_run_count": 3},
        ]
        enriched, cs = _make_enriched(elements, overlay_pages=[1])
        result = _apply_cover_page_figure_treatment(enriched, cs)
        assert result["elements"] == elements

    def test_no_trigger_when_page1_has_heading(self):
        """Guard 2: a heading on page 1 means real OCR'd structure → no collapse."""
        elements = [
            {"id": "e1", "tag": "H1", "page_no": 1, "text": "Title", "bbox": {"l": 50, "b": 600, "r": 500, "t": 650}, "content_run_count": 0},
            {"id": "e2", "tag": "P", "page_no": 2, "text": "Body", "bbox": {"l": 50, "b": 700, "r": 500, "t": 730}, "content_run_count": 5},
        ]
        enriched, cs = _make_enriched(elements, overlay_pages=[1])
        result = _apply_cover_page_figure_treatment(enriched, cs)
        assert result["elements"] == elements
        assert 1 in result["overlay_pages"]

    def test_no_trigger_many_substantive_paragraphs(self):
        """Guard 2: 3+ substantive text elements on page 1 → no collapse."""
        elements = [
            {"id": f"e{i}", "tag": "P", "page_no": 1, "text": f"Paragraph {i}", "bbox": {"l": 50, "b": 600 - i * 40, "r": 500, "t": 630 - i * 40}, "content_run_count": 0}
            for i in range(3)
        ] + [
            {"id": "e9", "tag": "P", "page_no": 2, "text": "Body", "bbox": {"l": 50, "b": 700, "r": 500, "t": 730}, "content_run_count": 5},
        ]
        enriched, cs = _make_enriched(elements, overlay_pages=[1])
        result = _apply_cover_page_figure_treatment(enriched, cs)
        assert result["elements"] == elements

    def test_no_trigger_single_page_document(self):
        """Guard 1: a single-page document has no cover → no collapse."""
        elements = [
            {"id": "e1", "tag": "P", "page_no": 1, "text": "Flyer", "bbox": {"l": 50, "b": 600, "r": 500, "t": 650}, "content_run_count": 0},
        ]
        enriched, cs = _make_enriched(elements, overlay_pages=[1])
        cs["page_analyses"] = cs["page_analyses"][:1]
        result = _apply_cover_page_figure_treatment(enriched, cs)
        assert result["elements"] == elements

    def test_alt_text_empty_elements(self):
        """Elements with no text produce generic alt."""
        elements = [
            {"id": "e1", "tag": "Figure", "page_no": 1, "text": "", "bbox": {"l": 0, "b": 0, "r": 612, "t": 792}, "content_run_count": 0},
        ]
        enriched, cs = _make_enriched(elements, overlay_pages=[1])
        result = _apply_cover_page_figure_treatment(enriched, cs)
        page1 = [e for e in result["elements"] if e.get("page_no") == 1]
        assert page1[0]["alt_text"] == "Cover page"

    def test_no_images_on_page1(self):
        """Page 1 has no images in cs_analysis → still triggers but no image_run_indices."""
        elements = [
            {"id": "e1", "tag": "P", "page_no": 1, "text": "Title", "bbox": {"l": 50, "b": 600, "r": 500, "t": 650}, "content_run_count": 0},
        ]
        enriched, cs = _make_enriched(elements, overlay_pages=[1], page_images=[])
        result = _apply_cover_page_figure_treatment(enriched, cs)
        page1 = [e for e in result["elements"] if e.get("page_no") == 1]
        assert len(page1) == 1
        assert page1[0]["tag"] == "Figure"
        assert page1[0].get("image_run_indices", []) == []

    def test_bbox_uses_actual_page_dimensions(self):
        """A4 page should get A4 dimensions in bbox, not hardcoded US Letter."""
        elements = [
            {"id": "e1", "tag": "P", "page_no": 1, "text": "Title", "bbox": {"l": 50, "b": 600, "r": 500, "t": 650}, "content_run_count": 0},
        ]
        enriched, cs = _make_enriched(elements, overlay_pages=[1], page_width=595, page_height=841)
        result = _apply_cover_page_figure_treatment(enriched, cs)
        page1 = [e for e in result["elements"] if e.get("page_no") == 1]
        assert page1[0]["bbox"] == {"l": 0, "b": 0, "r": 595, "t": 841}
