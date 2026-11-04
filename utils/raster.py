# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bound the size of page rasters.

A PDF page may be up to 200 x 200 inches; rendering one at 150 dpi would
allocate a 900-megapixel pixmap. Every render site passes its requested DPI
through :func:`safe_dpi`, which lowers it just enough to keep the pixmap
within ``MAX_RENDER_PIXELS``.
"""
import math

# 40 Mpx is ~120 MB of RGB: comfortably above any real page at 300 dpi
# (US Letter is 8.4 Mpx, A0 is 130 Mpx at 300 dpi but 33 Mpx at 150 dpi).
MAX_RENDER_PIXELS = 40_000_000


def safe_dpi(width_pt: float, height_pt: float, dpi: float,
             max_pixels: int = MAX_RENDER_PIXELS) -> float:
    """Return ``dpi`` lowered so a ``width_pt`` x ``height_pt`` raster fits ``max_pixels``."""
    area_in2 = (float(width_pt) / 72.0) * (float(height_pt) / 72.0)
    if area_in2 <= 0 or dpi <= 0:
        return dpi
    if area_in2 * dpi * dpi <= max_pixels:
        return dpi
    return math.sqrt(max_pixels / area_in2)
