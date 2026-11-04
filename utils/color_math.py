# SPDX-License-Identifier: AGPL-3.0-or-later
"""WCAG color contrast math utilities.

Provides color parsing, WCAG relative luminance, contrast ratio
calculation, and color adjustment to meet target contrast ratios.
"""

import math
import re
from typing import Optional, Tuple

# CSS named colors (subset — the 17 standard + common web colors)
_NAMED_COLORS = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "green": (0, 128, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "cyan": (0, 255, 255),
    "magenta": (255, 0, 255),
    "silver": (192, 192, 192),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
    "maroon": (128, 0, 0),
    "olive": (128, 128, 0),
    "lime": (0, 255, 0),
    "aqua": (0, 255, 255),
    "teal": (0, 128, 128),
    "navy": (0, 0, 128),
    "fuchsia": (255, 0, 255),
    "purple": (128, 0, 128),
    "orange": (255, 165, 0),
    "brown": (165, 42, 42),
    "pink": (255, 192, 203),
    "coral": (255, 127, 80),
    "tomato": (255, 99, 71),
    "gold": (255, 215, 0),
    "wheat": (245, 222, 179),
    "lightgray": (211, 211, 211),
    "lightgrey": (211, 211, 211),
    "darkgray": (169, 169, 169),
    "darkgrey": (169, 169, 169),
    "dimgray": (105, 105, 105),
    "dimgrey": (105, 105, 105),
    "lightblue": (173, 216, 230),
    "darkblue": (0, 0, 139),
    "lightgreen": (144, 238, 144),
    "darkgreen": (0, 100, 0),
    "darkred": (139, 0, 0),
    "indianred": (205, 92, 92),
    "crimson": (220, 20, 60),
    "ivory": (255, 255, 240),
    "beige": (245, 245, 220),
    "linen": (250, 240, 230),
    "whitesmoke": (245, 245, 245),
    "ghostwhite": (248, 248, 255),
    "aliceblue": (240, 248, 255),
    "lavender": (230, 230, 250),
    "mistyrose": (255, 228, 225),
    "honeydew": (240, 255, 240),
    "mintcream": (245, 255, 250),
    "azure": (240, 255, 255),
    "snow": (255, 250, 250),
    "seashell": (255, 245, 238),
    "floralwhite": (255, 250, 240),
    "transparent": (255, 255, 255),  # treat as white for contrast
    "inherit": None,
    "initial": None,
    "currentcolor": None,
}


def parse_color(css_color: str) -> Optional[Tuple[int, int, int]]:
    """Parse a CSS color value to an (R, G, B) tuple.

    Supports: #hex, rgb(), rgba(), and named colors.
    Returns None if the color cannot be parsed.
    """
    css_color = css_color.strip().lower()

    # Named colors
    if css_color in _NAMED_COLORS:
        return _NAMED_COLORS[css_color]

    # Hex: #RGB or #RRGGBB
    if css_color.startswith("#"):
        hex_val = css_color[1:]
        if len(hex_val) == 3:
            r = int(hex_val[0] * 2, 16)
            g = int(hex_val[1] * 2, 16)
            b = int(hex_val[2] * 2, 16)
            return (r, g, b)
        if len(hex_val) == 6:
            r = int(hex_val[0:2], 16)
            g = int(hex_val[2:4], 16)
            b = int(hex_val[4:6], 16)
            return (r, g, b)
        if len(hex_val) == 8:  # #RRGGBBAA — ignore alpha
            r = int(hex_val[0:2], 16)
            g = int(hex_val[2:4], 16)
            b = int(hex_val[4:6], 16)
            return (r, g, b)
        return None

    # rgb(R, G, B) or rgba(R, G, B, A)
    m = re.match(
        r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*[\d.]+\s*)?\)",
        css_color,
    )
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))

    return None


def rgb_to_hex(r: int, g: int, b: int) -> str:
    """Convert RGB integers to a #RRGGBB hex string."""
    return f"#{r:02X}{g:02X}{b:02X}"


def relative_luminance(rgb: Tuple[int, int, int]) -> float:
    """Calculate WCAG 2.1 relative luminance from an RGB tuple."""
    def _linearize(channel: int) -> float:
        s = channel / 255.0
        if s <= 0.03928:
            return s / 12.92
        return ((s + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * _linearize(r) + 0.7152 * _linearize(g) + 0.0722 * _linearize(b)


def contrast_ratio(rgb1: Tuple[int, int, int], rgb2: Tuple[int, int, int]) -> float:
    """Calculate the WCAG contrast ratio between two RGB colors."""
    l1 = relative_luminance(rgb1)
    l2 = relative_luminance(rgb2)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def adjust_color_for_contrast(
    fg: Tuple[int, int, int],
    bg: Tuple[int, int, int],
    target_ratio: float = 4.5,
) -> Tuple[int, int, int]:
    """Adjust the foreground color to meet the target contrast ratio
    against the background, preserving hue as much as possible.

    Strategy: darken or lighten the foreground in HSL space until the
    target ratio is met. If the current ratio is already sufficient,
    returns the foreground unchanged.
    """
    current = contrast_ratio(fg, bg)
    if current >= target_ratio:
        return fg

    bg_lum = relative_luminance(bg)

    # Decide direction: if bg is light, darken fg; if bg is dark, lighten fg
    h, s, l = _rgb_to_hsl(fg)

    if bg_lum > 0.5:
        # Light background — try darkening first
        result = _search_lightness(h, s, l, bg, target_ratio, direction=-1)
        if result is None:
            # Darkening wasn't enough — try lightening
            result = _search_lightness(h, s, l, bg, target_ratio, direction=1)
    else:
        # Dark background — try lightening first
        result = _search_lightness(h, s, l, bg, target_ratio, direction=1)
        if result is None:
            result = _search_lightness(h, s, l, bg, target_ratio, direction=-1)

    if result is not None:
        return result

    # Absolute fallback: black or white
    if bg_lum > 0.5:
        return (0, 0, 0)
    return (255, 255, 255)


def adjust_border_for_contrast(
    border: Tuple[int, int, int],
    bg: Tuple[int, int, int],
    target_ratio: float = 3.0,
) -> Tuple[int, int, int]:
    """Adjust a border/UI component color to meet the 3:1 non-text
    contrast ratio against the background."""
    return adjust_color_for_contrast(border, bg, target_ratio)


# ── Internal HSL helpers ─────────────────────────────────────────────

def _rgb_to_hsl(rgb: Tuple[int, int, int]) -> Tuple[float, float, float]:
    """Convert RGB (0-255) to HSL (h: 0-360, s: 0-1, l: 0-1)."""
    r, g, b = rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0
    mx = max(r, g, b)
    mn = min(r, g, b)
    l = (mx + mn) / 2.0

    if mx == mn:
        h = s = 0.0
    else:
        d = mx - mn
        s = d / (2.0 - mx - mn) if l > 0.5 else d / (mx + mn)
        if mx == r:
            h = (g - b) / d + (6.0 if g < b else 0.0)
        elif mx == g:
            h = (b - r) / d + 2.0
        else:
            h = (r - g) / d + 4.0
        h *= 60.0

    return (h, s, l)


def _hsl_to_rgb(h: float, s: float, l: float) -> Tuple[int, int, int]:
    """Convert HSL (h: 0-360, s: 0-1, l: 0-1) to RGB (0-255)."""
    if s == 0:
        v = int(round(l * 255))
        return (v, v, v)

    def _hue_to_rgb(p: float, q: float, t: float) -> float:
        if t < 0:
            t += 1.0
        if t > 1:
            t -= 1.0
        if t < 1 / 6:
            return p + (q - p) * 6.0 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6.0
        return p

    q = l * (1 + s) if l < 0.5 else l + s - l * s
    p = 2 * l - q
    hk = h / 360.0

    r = _hue_to_rgb(p, q, hk + 1 / 3)
    g = _hue_to_rgb(p, q, hk)
    b = _hue_to_rgb(p, q, hk - 1 / 3)

    return (
        int(round(r * 255)),
        int(round(g * 255)),
        int(round(b * 255)),
    )


def _search_lightness(
    h: float, s: float, l: float,
    bg: Tuple[int, int, int],
    target_ratio: float,
    direction: int,
) -> Optional[Tuple[int, int, int]]:
    """Binary search for a lightness value that meets the target
    contrast ratio against bg. direction: -1 = darken, +1 = lighten."""
    lo = l if direction == -1 else l
    hi = l if direction == 1 else l

    if direction == -1:
        lo = 0.0
    else:
        hi = 1.0

    # Binary search (30 iterations gives high precision)
    best = None
    for _ in range(30):
        mid = (lo + hi) / 2.0
        candidate = _hsl_to_rgb(h, s, mid)
        ratio = contrast_ratio(candidate, bg)
        if ratio >= target_ratio:
            best = candidate  # Record last known good result
            if direction == -1:
                lo = mid  # Try less dark
            else:
                hi = mid  # Try less light
        else:
            if direction == -1:
                hi = mid  # Need darker
            else:
                lo = mid  # Need lighter

    return best


# ── PyMuPDF color helpers ───────────────────────────────────────────

def fitz_color_to_rgb(color_int: int) -> Tuple[int, int, int]:
    """Convert a PyMuPDF packed integer text color to an (R, G, B) tuple.

    PyMuPDF's ``span["color"]`` is ``(r << 16) | (g << 8) | b``.
    """
    return ((color_int >> 16) & 0xFF, (color_int >> 8) & 0xFF, color_int & 0xFF)


def fitz_fill_to_rgb(fill: Tuple[float, ...]) -> Tuple[int, int, int]:
    """Convert a PyMuPDF drawing fill color (0.0–1.0 floats) to (R, G, B) ints.

    ``get_drawings()`` normalizes fills to a 3-float RGB tuple, but be
    defensive about grayscale (1 component) and CMYK (4 components) in case
    a fill slips through un-normalized.
    """
    n = len(fill)
    if n == 1:  # grayscale
        g = int(round(fill[0] * 255))
        return (g, g, g)
    if n >= 4:  # CMYK → RGB
        c, m, y, k = fill[0], fill[1], fill[2], fill[3]
        return (
            int(round(255 * (1 - c) * (1 - k))),
            int(round(255 * (1 - m) * (1 - k))),
            int(round(255 * (1 - y) * (1 - k))),
        )
    return (int(round(fill[0] * 255)), int(round(fill[1] * 255)), int(round(fill[2] * 255)))
