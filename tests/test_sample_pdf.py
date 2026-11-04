# SPDX-License-Identifier: AGPL-3.0-or-later
import re
import fitz
from pathlib import Path
SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample.pdf"

def test_sample_exists_small_untagged():
    assert SAMPLE.is_file() and SAMPLE.stat().st_size < 200_000
    doc = fitz.open(SAMPLE)
    assert doc.page_count == 2
    assert "Sample Report" in doc[0].get_text()
    assert doc.pdf_catalog() and "/StructTreeRoot" not in doc.xref_object(doc.pdf_catalog())

def test_sample_fonts_are_real_not_stubs():
    doc = fitz.open(SAMPLE)
    font_xrefs = []
    for xref in range(1, doc.xref_length()):
        obj = doc.xref_object(xref) or ""
        font_xrefs += [int(m) for m in re.findall(r"/FontFile\d?\s+(\d+)\s+0\s+R", obj)]
    assert font_xrefs, "expected embedded font programs"
    for xref in font_xrefs:
        assert len(doc.xref_stream_raw(xref)) > 1000, f"font stream {xref} is a stub"

def test_sample_page1_text_renders_above_image():
    doc = fitz.open(SAMPLE)
    page = doc[0]
    image_top = min(info["bbox"][1] for info in page.get_image_info())
    # Text words above the image only; table borders and the image itself are excluded.
    words = [fitz.Rect(w[:4]) for w in page.get_text("words") if w[3] <= image_top]
    assert words, "expected text above the image"
    dark = 0
    for rect in words:
        pix = page.get_pixmap(dpi=50, clip=rect)
        for i in range(0, len(pix.samples), pix.n):
            if min(pix.samples[i:i + 3]) < 160:
                dark += 1
    assert dark > 50, "page 1 text above the image did not render"
