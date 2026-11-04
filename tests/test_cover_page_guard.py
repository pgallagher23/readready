# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the cover-page-collapse guard in
``output_gen.pdf_tagger._apply_cover_page_figure_treatment``.

The cover-page-figure treatment collapses an overlay-fallback page 1 (all
elements with 0 content runs) into a single full-page Figure.  That is only
correct for a genuine scanned *cover* that OCRs to little/no structure.

It must NOT fire when the page is really a document:
  - a single-page doc has no "cover", and
  - a page Docling OCR'd into a heading / multiple paragraphs has real
    structure that the collapse would destroy.

Real-world trigger: a one-page image-only flyer
(a colloquium announcement) whose real text lives only as
``/ActualText`` in pre-existing tags.  Docling OCRs it into an H1 + paragraphs
+ figures; the un-guarded collapse crushed those into one "Cover page" Figure,
producing false ``moderate-text-coverage`` and SR ``poor`` warnings.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from output_gen.pdf_tagger import _apply_cover_page_figure_treatment  # noqa: E402


def _elem(eid, tag, text="", page_no=1):
    return {
        "id": eid,
        "tag": tag,
        "text": text,
        "page_no": page_no,
        "content_run_count": 0,  # overlay fallback: nothing matched
    }


def _cs_analysis(pages):
    return {
        "pages": pages,
        "page_analyses": [
            {"page": i + 1, "width": 612.0, "height": 792.0, "images": []}
            for i in range(pages)
        ],
    }


def _is_collapsed(enriched):
    elems = enriched.get("elements", [])
    return any(e.get("match_method") == "cover_page_figure" for e in elems)


def test_single_page_flyer_with_heading_not_collapsed():
    """One-page flyer OCR'd into H1 + paragraphs must keep its structure."""
    enriched = {
        "overlay_pages": [1],
        "elements": [
            _elem("e1", "Figure"),
            _elem("e2", "H1", "Example Lecture: Current Work and Future Directions"),
            _elem("e3", "P", "Jane Example, Ph.D."),
            _elem("e4", "P", "Friday, Oct 6, 2000 3 p.m. Room 100"),
            _elem("e5", "P", "Department of Psychology"),
        ],
    }
    result = _apply_cover_page_figure_treatment(enriched, _cs_analysis(pages=1))
    assert not _is_collapsed(result), "single-page doc must not be collapsed to a cover Figure"
    # All original elements preserved.
    ids = {e.get("id") for e in result["elements"]}
    assert {"e1", "e2", "e3", "e4", "e5"} <= ids
    # Page 1 stays on the overlay path (not removed as it is on collapse).
    assert 1 in result.get("overlay_pages", [])


def test_multipage_with_structure_not_collapsed():
    """A multi-page doc whose page 1 OCR'd to a heading also keeps structure."""
    enriched = {
        "overlay_pages": [1],
        "elements": [
            _elem("e1", "H1", "Annual Report 2017"),
            _elem("e2", "P", "Executive summary of the year."),
            _elem("e3", "P", "Second paragraph of real content."),
            _elem("e9", "P", "Body text on page two.", page_no=2),
        ],
    }
    result = _apply_cover_page_figure_treatment(enriched, _cs_analysis(pages=3))
    assert not _is_collapsed(result), "page with a heading must not be collapsed"


def test_genuine_scanned_cover_still_collapses():
    """A bare scanned cover (multi-page, no heading, <=1 short text) still collapses."""
    enriched = {
        "overlay_pages": [1],
        "elements": [
            _elem("e1", "Figure"),
            _elem("e2", "P", "x"),  # trivial OCR noise, not substantive
            _elem("e9", "P", "Real body content on page two.", page_no=2),
        ],
    }
    result = _apply_cover_page_figure_treatment(enriched, _cs_analysis(pages=3))
    assert _is_collapsed(result), "genuine scanned cover should still collapse to one Figure"
    page1 = [e for e in result["elements"] if e.get("page_no") == 1]
    assert len(page1) == 1 and page1[0]["tag"] == "Figure"
    # Page 1 removed from overlay_pages on collapse (image-wrapping path).
    assert 1 not in result.get("overlay_pages", [])
