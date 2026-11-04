# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rasterising a page must never allocate an unbounded pixmap.

A few-KB PDF can declare 300 pages of 200 x 200 inches; at 150 dpi that is
900 megapixels per page. Every render site clamps its DPI so the pixmap
stays under a fixed pixel budget.
"""
import ast
from pathlib import Path

import pytest

from utils.raster import MAX_RENDER_PIXELS, safe_dpi

ROOT = Path(__file__).resolve().parent.parent
LETTER = (612.0, 792.0)


def test_normal_page_keeps_requested_dpi():
    assert safe_dpi(*LETTER, 150) == 150
    assert safe_dpi(*LETTER, 300) == 300


def test_huge_page_is_clamped_to_the_pixel_budget():
    w = h = 14400.0  # 200 in, the PDF maximum
    dpi = safe_dpi(w, h, 150)
    assert dpi < 150
    pixels = (w * dpi / 72) * (h * dpi / 72)
    assert pixels <= MAX_RENDER_PIXELS * 1.001


def test_degenerate_rect_does_not_divide_by_zero():
    assert safe_dpi(0, 0, 150) == 150


RENDER_SITES = [
    "converter/fallback_text_extractor.py",
    "output_gen/content_matcher.py",
    "vlm/table_caption_generator.py",
    "converter/docling_converter.py",
    "audit/checks/color_contrast_checks.py",
    "app.py",
]


@pytest.mark.parametrize("rel", RENDER_SITES)
def test_every_render_site_uses_safe_dpi(rel):
    src = (ROOT / rel).read_text()
    tree = ast.parse(src)
    renders = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
               and getattr(n.func, "attr", "") in ("get_pixmap", "get_textpage_ocr")]
    assert renders, f"{rel} no longer renders pages; drop it from RENDER_SITES"
    assert "safe_dpi(" in src, f"{rel} renders pages without clamping the DPI"
