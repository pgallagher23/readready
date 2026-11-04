# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: invisible-only ("Tr=3 sandwich scan") pages must stay
invisible after content-preserving tagging.

Modelled on a real scanned PDF: a scanned page sets "3 Tr" (invisible
text rendering mode) ONCE, right after the page's single BT, and every
subsequent word inherits that invisibility per PDF graphics-state rules -- a
classic OCR "sandwich" scan. The content-preserving tagger splits that single
content stream into one BDC/Artifact scope per matched structure element.

Two distinct properties are asserted here, and they are NOT equally strong:

* Visibility (test_no_text_visible_when_rendered): the page must render with
  no painted glyphs. Tr is graphics state and BDC/EMC does not scope it, so
  the source's single "3 Tr" persists across every later scope on its own.
  An A/B of the real Brandon file against the pre-Tr-forcing JAR confirmed
  this: both builds rendered pixel-identically to the source on all 18
  genuine invisible-only pages. This assertion therefore guards against
  regressions (e.g. a future change that drops or overrides the source Tr),
  not against a bug the forcing was needed to cure.

* Scope self-containment (test_every_marked_content_scope_stays_invisible):
  every text-painting scope must state Tr=3 locally rather than depend on
  state set in an earlier, now-closed scope. Only the forcing satisfies this;
  it fails against the pre-forcing JAR. The forcing is gated on the source's
  own prevailing Tr already being 3, so it can never hide visible content.

Run:  python -m pytest tests/test_invisible_only_page_visibility.py -v
"""

import json
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAR = os.path.join(ROOT, "output_gen", "itext_tagger.jar")

pytestmark = pytest.mark.skipif(
    shutil.which("java") is None or not os.path.exists(JAR),
    reason="needs java + itext_tagger.jar",
)

RUN_TEXTS = [
    "Title Heading Text",
    "First paragraph sentence about elephants and rivers.",
    "Second paragraph sentence about mountains and rivers.",
]
# (x, y) of each Td move, in encounter order -- used both to build the raw
# content stream and to give each element a matching bbox for the
# containment check in ContentPreservingTagger.
RUN_XY = [(72, 700), (72, 660), (72, 640)]


@pytest.fixture(scope="module")
def tagged(tmp_path_factory):
    fitz = pytest.importorskip("fitz")
    tmp = tmp_path_factory.mktemp("invis_only")
    src = str(tmp / "src.pdf")

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    font = fitz.Font("helv")
    page.insert_font(fontname="Helv", fontbuffer=font.buffer)

    # Whole-page invisible OCR text layer: "3 Tr" set ONCE, before the
    # first word, inherited by every later Tj per PDF graphics-state rules
    # (matches the real Brandon PDF's single-BT-per-page structure).
    lines = ["BT", "/Helv 12 Tf", "3 Tr", "72 700 Td",
             f"({RUN_TEXTS[0]}) Tj"]
    prev_x, prev_y = RUN_XY[0]
    for text, (x, y) in zip(RUN_TEXTS[1:], RUN_XY[1:]):
        lines.append(f"{x - prev_x} {y - prev_y} Td")
        lines.append(f"({text}) Tj")
        prev_x, prev_y = x, y
    lines.append("ET")
    content_bytes = ("\n".join(lines) + "\n").encode("latin-1")

    content_xref = doc.get_new_xref()
    doc.update_object(content_xref, f"<< /Length {len(content_bytes)} >>")
    doc.update_stream(content_xref, content_bytes)
    page_xref = page.xref
    existing = [x for x in page.get_contents() if x != content_xref]
    doc.xref_set_key(page_xref, "Contents",
                      f"[{' '.join(f'{x} 0 R' for x in existing + [content_xref])}]")
    doc.save(src, garbage=4)
    doc.close()

    tags = ["H1", "P", "P"]
    elements = []
    for i, (text, (x, y), tag) in enumerate(zip(RUN_TEXTS, RUN_XY, tags)):
        elements.append({
            "id": f"elem_{i:04d}", "type": "TextItem", "tag": tag,
            "level": 1 if tag == "H1" else None, "text": text,
            "page_no": 1,
            "bbox": {"l": x - 2, "b": y - 3, "r": x + 300, "t": y + 14},
            "reading_order": i, "alt_text": None, "image_ref": None,
            "content_run_indices": [
                {"page": 1, "run_index": i, "text": text, "x": x, "y": y}
            ],
        })

    structure = {
        "metadata": {"source_pdf": "src.pdf", "num_pages": 1,
                     "language": "en", "title": "Invisible-only page"},
        "pages": {"1": {"width": 612, "height": 792}},
        "elements": elements,
        "unmatched_runs": [],
        "invisible_only_pages": [1],
        "overlay_pages": [],
    }
    sjson = str(tmp / "enriched.json")
    with open(sjson, "w") as f:
        json.dump(structure, f)

    out = str(tmp / "tagged.pdf")
    proc = subprocess.run(
        ["java", "-jar", JAR, "--tag-mode", "content-preserving",
         "--input", src, "--structure", sjson, "--output", out,
         "--lang", "en-US", "--title", "Invisible-only page"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert os.path.exists(out)
    return out


def _page1_content(pdf_path):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(pdf_path)
    content = b""
    page = doc[0]
    for xref in page.get_contents():
        content += doc.xref_stream(xref)
    doc.close()
    return content


# Marked-content block: "/Tag <<...MCID N...>> BDC ... EMC" or
# "/Artifact BDC ... EMC" -- either way, everything opened between BDC/EMC.
_MC_BLOCK_RE = re.compile(rb"BDC(.*?)EMC", re.DOTALL)


def test_every_marked_content_scope_stays_invisible(tagged):
    """Every BDC/EMC scope that paints one of our OCR words must locally
    set Tr=3 -- it must not rely on inheriting rendering mode from an
    earlier, now-closed scope.  (Inheritance does in fact work, since Tr
    is graphics state; this asserts the stronger self-containment
    invariant, which survives a lost text-state replay or any later
    per-MCID extraction of the stream.)"""
    content = _page1_content(tagged)
    blocks = _MC_BLOCK_RE.findall(content)
    text_blocks = [b for b in blocks if b"Tj" in b or b"TJ" in b]
    assert len(text_blocks) >= 3, (
        f"expected >=3 text-painting marked-content scopes, got "
        f"{len(text_blocks)}: {content!r}")
    for i, block in enumerate(text_blocks):
        assert b"3 Tr" in block, (
            f"marked-content scope #{i} paints text but never sets "
            f"Tr=3 locally -- it depends on rendering mode inherited from "
            f"an earlier, now-closed scope:\n{block!r}")


def test_no_text_visible_when_rendered(tagged):
    """Rendering the tagged page should not paint any of the OCR words --
    they must stay invisible, exactly as in the source scan."""
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(tagged)
    pix = doc[0].get_pixmap(dpi=72)
    doc.close()
    # An all-invisible page renders as a blank (all-white) raster --
    # any painted glyph would show up as non-white pixels.
    samples = pix.samples
    assert all(b == 255 for b in samples), (
        "page rendered with non-white pixels -- invisible OCR text is "
        "painting visibly")


def test_text_still_recoverable_via_mcr(tagged):
    """The invisible text must still be extractable (screen readers /
    copy-paste read MCR content regardless of Tr) -- the fix must not
    have accidentally stripped it instead of hiding it."""
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(tagged)
    text = doc[0].get_text()
    doc.close()
    for expected in RUN_TEXTS:
        assert expected in text, f"{expected!r} not recoverable from MCR"


# ---------------------------------------------------------------------------
# Negative fixture: a page LISTED in invisible_only_pages that ALSO contains
# genuinely visible text -- modelled on page 19 of a real scanned PDF
# (20 visible + 12 invisible text operators on one nominally "invisible-only"
# page). This pins the `sourceTr == 3` guard on the forced-invisibility write
# in ContentPreservingTagger.write()/handleTextOperator(): without the guard,
# the forced "3 Tr" fires unconditionally at every new marked-content scope
# on such a page and silently blanks any genuinely visible run it opens on.
# Measured on the real file: ~2% pixel delta unguarded, 0% guarded.
#
# The visible run here deliberately never has its own "Tr" operator -- it
# relies on the PDF-spec default text rendering mode (Tr=0), exactly like
# most real content-stream generators that only ever emit "Tr" to turn
# invisibility ON. This matters for discrimination: had the visible run its
# own explicit "0 Tr" immediately before it, write()'s buffered text-state
# replay (the `case "Tr":` branch feeding pendingTextStateOps) would replay
# that "0 Tr" right after any forced "3 Tr" and silently restore visibility
# even with the guard deleted -- masking exactly the bug this test exists to
# catch. The invisible OCR run instead carries its own explicit "3 Tr".
# ---------------------------------------------------------------------------

MIXED_VISIBLE_TEXT = "Visible paragraph text is genuinely painted."
MIXED_INVISIBLE_TEXT = "Invisible OCR body text recovered by Tesseract."
MIXED_VISIBLE_XY = (72, 700)
MIXED_INVISIBLE_XY = (72, 660)


@pytest.fixture(scope="module")
def tagged_mixed(tmp_path_factory):
    fitz = pytest.importorskip("fitz")
    tmp = tmp_path_factory.mktemp("invis_only_mixed")
    src = str(tmp / "src.pdf")

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    font = fitz.Font("helv")
    page.insert_font(fontname="Helv", fontbuffer=font.buffer)

    lines = [
        "BT",
        "/Helv 12 Tf",
        f"{MIXED_VISIBLE_XY[0]} {MIXED_VISIBLE_XY[1]} Td",
        f"({MIXED_VISIBLE_TEXT}) Tj",
        f"{MIXED_INVISIBLE_XY[0] - MIXED_VISIBLE_XY[0]} "
        f"{MIXED_INVISIBLE_XY[1] - MIXED_VISIBLE_XY[1]} Td",
        "3 Tr",
        f"({MIXED_INVISIBLE_TEXT}) Tj",
        "ET",
    ]
    content_bytes = ("\n".join(lines) + "\n").encode("latin-1")

    content_xref = doc.get_new_xref()
    doc.update_object(content_xref, f"<< /Length {len(content_bytes)} >>")
    doc.update_stream(content_xref, content_bytes)
    page_xref = page.xref
    existing = [x for x in page.get_contents() if x != content_xref]
    doc.xref_set_key(page_xref, "Contents",
                      f"[{' '.join(f'{x} 0 R' for x in existing + [content_xref])}]")
    doc.save(src, garbage=4)
    doc.close()

    elements = [
        {
            "id": "elem_0000", "type": "TextItem", "tag": "P", "level": None,
            "text": MIXED_VISIBLE_TEXT, "page_no": 1,
            "bbox": {"l": MIXED_VISIBLE_XY[0] - 2, "b": MIXED_VISIBLE_XY[1] - 3,
                     "r": MIXED_VISIBLE_XY[0] + 320, "t": MIXED_VISIBLE_XY[1] + 14},
            "reading_order": 0, "alt_text": None, "image_ref": None,
            "content_run_indices": [
                {"page": 1, "run_index": 0, "text": MIXED_VISIBLE_TEXT,
                 "x": MIXED_VISIBLE_XY[0], "y": MIXED_VISIBLE_XY[1]}
            ],
        },
        {
            "id": "elem_0001", "type": "TextItem", "tag": "P", "level": None,
            "text": MIXED_INVISIBLE_TEXT, "page_no": 1,
            "bbox": {"l": MIXED_INVISIBLE_XY[0] - 2, "b": MIXED_INVISIBLE_XY[1] - 3,
                     "r": MIXED_INVISIBLE_XY[0] + 320, "t": MIXED_INVISIBLE_XY[1] + 14},
            "reading_order": 1, "alt_text": None, "image_ref": None,
            "content_run_indices": [
                {"page": 1, "run_index": 1, "text": MIXED_INVISIBLE_TEXT,
                 "x": MIXED_INVISIBLE_XY[0], "y": MIXED_INVISIBLE_XY[1]}
            ],
        },
    ]

    structure = {
        "metadata": {"source_pdf": "src.pdf", "num_pages": 1,
                     "language": "en", "title": "Mixed visible/invisible page"},
        "pages": {"1": {"width": 612, "height": 792}},
        "elements": elements,
        "unmatched_runs": [],
        "invisible_only_pages": [1],
        "overlay_pages": [],
    }
    sjson = str(tmp / "enriched.json")
    with open(sjson, "w") as f:
        json.dump(structure, f)

    out = str(tmp / "tagged.pdf")
    proc = subprocess.run(
        ["java", "-jar", JAR, "--tag-mode", "content-preserving",
         "--input", src, "--structure", sjson, "--output", out,
         "--lang", "en-US", "--title", "Mixed visible/invisible page"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert os.path.exists(out)
    return out


def test_visible_run_scope_has_no_forced_invisibility(tagged_mixed):
    """The marked-content scope painting the genuinely visible run must not
    contain a forced '3 Tr' -- the sourceTr==3 guard in
    ContentPreservingTagger.write() must stop the invisible-only forcing
    from leaking onto a scope the source itself never marked invisible."""
    content = _page1_content(tagged_mixed)
    blocks = _MC_BLOCK_RE.findall(content)
    visible_blocks = [
        b for b in blocks if MIXED_VISIBLE_TEXT.encode("latin-1") in b]
    assert len(visible_blocks) == 1, (
        f"expected exactly one marked-content scope painting the visible "
        f"run, got {len(visible_blocks)}: {content!r}")
    assert b"3 Tr" not in visible_blocks[0], (
        "visible run's marked-content scope contains a forced '3 Tr' -- "
        "the sourceTr==3 guard failed to stop the invisible-only forcing "
        "from leaking onto genuinely visible content:\n"
        f"{visible_blocks[0]!r}")


def test_visible_text_still_paints_on_misclassified_invisible_only_page(
        tagged_mixed):
    """Rendering the tagged page must still show ink -- the ONLY content
    that can legitimately paint on this page is the visible run (the OCR
    run is genuinely Tr=3 with or without the guard), so any painted pixel
    proves the guard didn't just avoid the literal '3 Tr' bytes but that
    the glyphs actually render (behaviour, not implementation)."""
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(tagged_mixed)
    pix = doc[0].get_pixmap(dpi=72)
    doc.close()
    samples = pix.samples
    assert any(b != 255 for b in samples), (
        "page rendered entirely blank -- the genuinely visible run was "
        "silently blanked (forced invisibility leaked from the OCR "
        "layer's scope onto the visible run's scope)")
