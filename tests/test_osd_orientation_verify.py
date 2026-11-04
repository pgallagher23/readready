# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for OCR-verified OSD orientation in converter/docling_converter.py.

Docling's Tesseract CLI model runs OSD on every OCR region and applies the
proposed rotation unconditionally, ignoring the orientation confidence.
Tesseract OSD can confidently mis-report ``Rotate: 180`` on an upright scan,
so Docling flips a good page upside-down before OCR and the whole page comes
out as gibberish.

`_verified_osd_orientation` defeats this by OCR'ing BOTH the original and the
OSD-proposed rotation and keeping whichever yields more real dictionary words.
For an upright English page it must return 0 (do NOT rotate); for a genuinely
upside-down page it must return the proposed angle (DO rotate back).

The fixture is page 1 of the shipped ``examples/sample.pdf`` rendered to PNG.

Run:  python -m pytest tests/test_osd_orientation_verify.py -v
"""

import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from converter.docling_converter import _verified_osd_orientation

FIXTURE = os.path.join(ROOT, "examples", "sample.pdf")

needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="needs the tesseract binary"
)


@pytest.fixture(scope="module")
def upright_png(tmp_path_factory):
    """Render page 1 of the sample PDF (upright English text) to PNG."""
    fitz = pytest.importorskip("fitz")
    out = tmp_path_factory.mktemp("osd") / "upright.png"
    doc = fitz.open(FIXTURE)
    doc[0].get_pixmap(dpi=216).save(str(out))
    doc.close()
    return str(out)


@needs_tesseract
def test_upright_page_with_false_180_is_not_rotated(upright_png):
    # A (wrong) proposal of 180 on an upright page must be rejected.
    angle = _verified_osd_orientation(
        upright_png, 180, tesseract_cmd="tesseract", lang_codes=["eng"]
    )
    assert angle == 0


@needs_tesseract
def test_genuinely_upside_down_page_is_rotated_back(upright_png, tmp_path):
    # Flip the upright page to make a truly upside-down scan. OSD would
    # propose 180; verification must accept it (rotating back wins).
    from PIL import Image

    flipped = tmp_path / "flipped.png"
    Image.open(upright_png).rotate(180, expand=True).save(str(flipped))

    angle = _verified_osd_orientation(
        str(flipped), 180, tesseract_cmd="tesseract", lang_codes=["eng"]
    )
    assert angle == 180


def test_zero_proposal_is_passthrough(upright_png):
    # When OSD proposes no rotation there is nothing to verify (no OCR runs).
    angle = _verified_osd_orientation(
        upright_png, 0, tesseract_cmd="tesseract", lang_codes=["eng"]
    )
    assert angle == 0
