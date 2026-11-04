# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration test: AI table captions are emitted as a Caption struct
element (first child of Table) with invisible MCR text that screen
readers can extract — not just a /Summary attribute.

Runs the real iTeX JAR in overlay mode on a synthetic 1-page PDF +
hand-crafted structure JSON, then walks the output struct tree.

Run:  python -m pytest tests/test_table_caption_struct.py -v
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz  # PyMuPDF
from output_gen.struct_tree_validator import _walk_struct_tree

REPO = Path(__file__).resolve().parent.parent
JAR = REPO / "output_gen" / "itext_tagger.jar"
CAPTION_TEXT = "Summary of quarterly revenue by region and product line ZZX9."


def _find_struct_tree_root(doc) -> int:
    cat = doc.xref_object(doc.pdf_catalog(), compressed=False)
    m = re.search(r"/StructTreeRoot\s+(\d+)\s+0\s+R", cat)
    assert m, "no StructTreeRoot in output catalog"
    return int(m.group(1))


def _make_input_pdf(path: Path) -> None:
    doc = fitz.open()
    doc.new_page(width=612, height=792)  # US Letter
    doc.save(str(path))
    doc.close()


def _make_structure_json(path: Path) -> None:
    table = {
        "type": "TableItem",
        "tag": "Table",
        "page_no": 1,
        "bbox": {"l": 72, "b": 600, "r": 540, "t": 720},
        "caption": CAPTION_TEXT,
        "cells": [
            {"row": 0, "col": 0, "text": "Region", "is_header": True,
             "is_col_header": True},
            {"row": 0, "col": 1, "text": "Revenue", "is_header": True,
             "is_col_header": True},
            {"row": 1, "col": 0, "text": "North", "is_header": False},
            {"row": 1, "col": 1, "text": "100", "is_header": False},
        ],
    }
    structure = {"elements": [table]}
    path.write_text(json.dumps(structure), encoding="utf-8")


@pytest.mark.skipif(not JAR.exists(), reason="itext_tagger.jar not built")
def test_table_caption_emitted_as_struct_element():
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        in_pdf = tdp / "in.pdf"
        struct = tdp / "struct.json"
        out_pdf = tdp / "out.pdf"
        _make_input_pdf(in_pdf)
        _make_structure_json(struct)

        proc = subprocess.run(
            ["java", "-jar", str(JAR),
             "--input", str(in_pdf),
             "--structure", str(struct),
             "--output", str(out_pdf),
             "--lang", "en",
             "--tag-mode", "overlay"],
            capture_output=True, text=True, timeout=120,
        )
        assert out_pdf.exists(), (
            f"tagger did not produce output.\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )

        doc = fitz.open(str(out_pdf))
        try:
            results = []
            _walk_struct_tree(doc, _find_struct_tree_root(doc), results)

            tags = [r["tag"] for r in results]
            assert "Table" in tags, f"no Table in struct tree: {tags}"

            captions = [r for r in results if r["tag"] == "Caption"]
            assert captions, f"no Caption struct element emitted; tags={tags}"

            # Caption must be a child of Table and carry marked content
            cap = captions[0]
            assert "/Table/Caption" in cap["path"], (
                f"Caption not under Table; path={cap['path']}"
            )
            assert cap["has_mcr"], "Caption has no marked content (MCR)"

            # Caption must be the FIRST child of Table (reading order matters)
            table_idx = next(i for i, r in enumerate(results)
                             if r["tag"] == "Table")
            cap_idx = next(i for i, r in enumerate(results)
                           if r["tag"] == "Caption")
            assert cap_idx == table_idx + 1, (
                f"Caption must be first child of Table in DFS walk order; "
                f"table_idx={table_idx}, cap_idx={cap_idx}"
            )

            # Regression: table data cells still present
            assert "TH" in tags, f"table headers lost; tags={tags}"
            assert "TD" in tags, f"table data cells lost; tags={tags}"

            # Caption text is extractable from the page (invisible Tr=3 text
            # is still in the content stream → fitz extracts it)
            page_text = doc[0].get_text()
            assert CAPTION_TEXT[:30] in page_text, (
                "caption text not found in page content stream"
            )

            # /Summary on the Table must be the generic constant, NOT the
            # caption text (the description now lives in the Caption element)
            table_elem = next(r for r in results if r["tag"] == "Table")
            t_obj = doc.xref_object(table_elem["xref"], compressed=False)
            assert "Data table" in t_obj, (
                "/Summary should be the generic 'Data table'"
            )
            assert CAPTION_TEXT[:20] not in t_obj, (
                "/Summary must not carry the caption text"
            )
        finally:
            doc.close()
