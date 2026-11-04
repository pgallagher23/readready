# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration: overlay-page container tagging through the real JAR.

Before the fix, an L element whose text lives only in children[] fails the
needsTag gate on overlay pages -> no L/LI SEs, all list text lost.  Also:
overlay-written cell/LI MCRs must not be written AGAIN by the unmatched-fill
backstop passes (the pre-existing x2 cell text bug).

Run:  python -m pytest tests/test_overlay_container_tagging.py -v
"""

import json
import os
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

LI_TEXTS = [
    "First unique item mentions kumquats and telescopes.",
    "Second unique item mentions granite and lighthouses.",
]
CELL_TEXTS = [
    ["Header alpha zebra", "Header beta yak"],
    ["Body gamma xylophone", "Body delta walrus"],
]


@pytest.fixture(scope="module")
def tagged(tmp_path_factory):
    fitz = pytest.importorskip("fitz")
    tmp = tmp_path_factory.mktemp("ovl")
    src = str(tmp / "src.pdf")
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    # Simulated scan: vector graphics only, zero text runs.
    page.draw_rect(fitz.Rect(50, 50, 560, 740), color=(0, 0, 0), width=2)
    doc.save(src)
    doc.close()

    cells = []
    for r, row in enumerate(CELL_TEXTS):
        for c, text in enumerate(row):
            cells.append({
                "row": r, "col": c, "row_span": 1, "col_span": 1,
                "text": text, "is_header": r == 0,
                "is_col_header": r == 0, "is_row_header": False,
                "bbox": {"l": 60 + 250 * c, "b": 300 - 30 * r,
                         "r": 300 + 250 * c, "t": 325 - 30 * r},
                "content_run_indices": [],
            })
    structure = {
        "metadata": {"source_pdf": "src.pdf", "num_pages": 1,
                     "language": "en", "title": "Overlay containers"},
        "pages": {"1": {"width": 612, "height": 792}},
        "elements": [
            {"id": "elem_0000", "type": "SectionHeaderItem", "tag": "H1",
             "level": 1, "text": "Scanned Minutes", "page_no": 1,
             "bbox": {"l": 60, "b": 700, "r": 400, "t": 720},
             "reading_order": 0, "alt_text": None, "image_ref": None},
            {"id": "elem_0001", "type": "ListGroup", "tag": "L",
             "level": None, "text": "", "page_no": 1,
             "bbox": {"l": 60, "b": 500, "r": 550, "t": 600},
             "reading_order": 1, "alt_text": None, "image_ref": None,
             "children": [
                 {"tag": "LI", "text": t, "page_no": 1,
                  "bbox": {"l": 60, "b": 560 - 40 * i, "r": 550,
                           "t": 590 - 40 * i},
                  "children": [{"tag": "LBody", "text": t}]}
                 for i, t in enumerate(LI_TEXTS)
             ]},
            {"id": "elem_0002", "type": "TableItem", "tag": "Table",
             "level": None, "text": "irrelevant flattened table text",
             "page_no": 1,
             "bbox": {"l": 60, "b": 250, "r": 550, "t": 330},
             "reading_order": 2, "alt_text": None, "image_ref": None,
             "cells": cells},
        ],
        "unmatched_runs": [],
        "invisible_only_pages": [],
        "overlay_pages": [1],
    }
    sjson = str(tmp / "enriched.json")
    with open(sjson, "w") as f:
        json.dump(structure, f)

    out = str(tmp / "tagged.pdf")
    proc = subprocess.run(
        ["java", "-jar", JAR, "--tag-mode", "content-preserving",
         "--input", src, "--structure", sjson, "--output", out,
         "--lang", "en-US", "--title", "Overlay containers"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert os.path.exists(out)
    return out


def _tree_roles(pdf_path):
    import collections
    pikepdf = pytest.importorskip("pikepdf")
    counts = collections.Counter()
    with pikepdf.open(pdf_path) as pdf:
        st = pdf.Root.get("/StructTreeRoot")
        assert st is not None

        def walk(node, depth=0):
            if depth > 50 or not isinstance(node, pikepdf.Dictionary):
                return
            s = node.get("/S")
            if s is not None:
                counts[str(s)] += 1
            k = node.get("/K")
            if k is None:
                return
            kids = k if isinstance(k, pikepdf.Array) else [k]
            for kid in kids:
                if isinstance(kid, pikepdf.Dictionary) and "/S" in kid:
                    walk(kid, depth + 1)

        k = st.get("/K")
        for kid in (k if isinstance(k, pikepdf.Array) else [k]):
            walk(kid)
    return counts


def test_list_ses_created_on_overlay_page(tagged):
    roles = _tree_roles(tagged)
    assert roles.get("/L", 0) == 1
    assert roles.get("/LI", 0) == 2
    assert roles.get("/LBody", 0) == 2


def test_table_ses_created_on_overlay_page(tagged):
    roles = _tree_roles(tagged)
    assert roles.get("/Table", 0) == 1
    assert roles.get("/TD", 0) + roles.get("/TH", 0) == 4


def test_li_text_present_exactly_once(tagged):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(tagged)
    text = " ".join(p.get_text() for p in doc).replace("\n", " ")
    doc.close()
    for t in LI_TEXTS:
        probe = t[:30]
        assert text.count(probe) == 1, f"{probe!r} x{text.count(probe)}"


def test_cell_text_present_exactly_once(tagged):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open(tagged)
    text = " ".join(p.get_text() for p in doc).replace("\n", " ")
    doc.close()
    for row in CELL_TEXTS:
        for t in row:
            assert text.count(t) == 1, f"{t!r} x{text.count(t)}"


# ---------------------------------------------------------------------------
# I2 (2026-08-07): hasChildContent() must only rescue containers on pages
# where overlay/fallback machinery fills them. On an ordinary page it must
# fail needsTag exactly as it did before this branch -- not resurrect a
# container that (in real docs) more often means a sibling stole its runs,
# which then gets an invisible-MCR duplicate written by the LI/cell fill
# backstop (a name read twice in a literary-anthology PDF).
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tagged_non_overlay(tmp_path_factory):
    fitz = pytest.importorskip("fitz")
    tmp = tmp_path_factory.mktemp("ovl_non")
    src = str(tmp / "src.pdf")
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    # An ordinary (non-scanned) page: real text present, drawn normally.
    page.insert_text((60, 730), "Ordinary Page Heading", fontsize=14)
    doc.save(src)
    doc.close()

    structure = {
        "metadata": {"source_pdf": "src.pdf", "num_pages": 1,
                     "language": "en", "title": "Non-overlay containers"},
        "pages": {"1": {"width": 612, "height": 792}},
        "elements": [
            {"id": "elem_0000", "type": "SectionHeaderItem", "tag": "H1",
             "level": 1, "text": "Ordinary Page Heading", "page_no": 1,
             "bbox": {"l": 60, "b": 715, "r": 400, "t": 735},
             "reading_order": 0, "alt_text": None, "image_ref": None},
            # ListGroup with empty top-level text and no matched runs of
            # its own -- on an overlay page hasChildContent() would rescue
            # it; on this ordinary page it must NOT be tagged.
            {"id": "elem_0001", "type": "ListGroup", "tag": "L",
             "level": None, "text": "", "page_no": 1,
             "bbox": {"l": 60, "b": 500, "r": 550, "t": 600},
             "reading_order": 1, "alt_text": None, "image_ref": None,
             "children": [
                 {"tag": "LI", "text": t, "page_no": 1,
                  "bbox": {"l": 60, "b": 560 - 40 * i, "r": 550,
                           "t": 590 - 40 * i},
                  "children": [{"tag": "LBody", "text": t}]}
                 for i, t in enumerate(LI_TEXTS)
             ]},
        ],
        "unmatched_runs": [],
        "invisible_only_pages": [],
        "overlay_pages": [],
    }
    sjson = str(tmp / "enriched.json")
    with open(sjson, "w") as f:
        json.dump(structure, f)

    out = str(tmp / "tagged.pdf")
    proc = subprocess.run(
        ["java", "-jar", JAR, "--tag-mode", "content-preserving",
         "--input", src, "--structure", sjson, "--output", out,
         "--lang", "en-US", "--title", "Non-overlay containers"],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert os.path.exists(out)
    return out


def test_child_content_container_not_admitted_on_non_overlay_page(
        tagged_non_overlay):
    roles = _tree_roles(tagged_non_overlay)
    assert roles.get("/L", 0) == 0, roles
    assert roles.get("/LI", 0) == 0, roles
    assert roles.get("/LBody", 0) == 0, roles
    # The heading (real content) must still be tagged -- only the
    # child-content-only container is dropped.
    assert roles.get("/H1", 0) == 1, roles
