# SPDX-License-Identifier: AGPL-3.0-or-later
# Portions derived from Content Accessibility Utility on AWS
# (https://github.com/awslabs/content-accessibility-utility-on-aws),
# Copyright 2025 Amazon.com, Inc. or its affiliates, Apache-2.0.
# Modified by the ReadReady project; see NOTICE.
"""Color contrast remediation strategies.

WCAG 1.4.3 — Fix insufficient text contrast (4.5:1 / 3:1 for large text).
WCAG 1.4.11 — Fix insufficient non-text contrast (3:1 for UI components).

Uses WCAG relative luminance formula and HSL-based color adjustment to
meet target ratios while preserving hue.
"""

import logging
import re
from typing import Any, Dict, Optional, Tuple

from bs4 import BeautifulSoup

from remediate.element_resolver import get_id_index, resolve_issue_element
from utils.color_math import (
    adjust_border_for_contrast,
    adjust_color_for_contrast,
    contrast_ratio,
    parse_color,
    rgb_to_hex,
)

logger = logging.getLogger(__name__)

# Regex for extracting CSS color values
_COLOR_RE = re.compile(
    r"(?<!background-)color\s*:\s*"
    r"(#[0-9a-fA-F]{3,8}|rgb\([^)]+\)|rgba\([^)]+\)|[a-zA-Z]+)"
)
_BG_COLOR_RE = re.compile(
    r"background-color\s*:\s*"
    r"(#[0-9a-fA-F]{3,8}|rgb\([^)]+\)|rgba\([^)]+\)|[a-zA-Z]+)"
)
_BORDER_COLOR_RE = re.compile(
    r"border(?:-color)?\s*:\s*[^;]*?"
    r"(#[0-9a-fA-F]{3,8}|rgb\([^)]+\)|rgba\([^)]+\)|[a-zA-Z]+)"
)


def remediate_insufficient_color_contrast(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Fix text color contrast by adjusting the foreground color to
    meet WCAG 4.5:1 (or 3:1 for large text)."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    style = element.get("style", "")

    # Extract current colors
    fg_rgb = _extract_fg_color(style)
    bg_rgb = _extract_bg_color(element)

    if fg_rgb is None or bg_rgb is None:
        return _apply_fallback_contrast(element, style)

    target = 3.0 if _is_large_text(element) else 4.5

    current = contrast_ratio(fg_rgb, bg_rgb)
    if current >= target:
        return None

    new_fg = adjust_color_for_contrast(fg_rgb, bg_rgb, target)
    new_hex = rgb_to_hex(*new_fg)
    new_ratio = contrast_ratio(new_fg, bg_rgb)

    if _COLOR_RE.search(style):
        new_style = _COLOR_RE.sub(f"color: {new_hex}", style)
    else:
        new_style = style.rstrip(";").rstrip() + f"; color: {new_hex}"

    element["style"] = new_style
    return (
        f"Adjusted text color to {new_hex} "
        f"(contrast {new_ratio:.1f}:1, was {current:.1f}:1)"
    )


def remediate_potential_color_contrast_issue(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Handle elements where colors couldn't be fully determined.
    Attempt to parse whatever color info is available and fix if needed."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    style = element.get("style", "")
    fg_rgb = _extract_fg_color(style)
    bg_rgb = _extract_bg_color(element)

    if fg_rgb is None and bg_rgb is None:
        return None

    if fg_rgb is None:
        fg_rgb = (0, 0, 0)
    if bg_rgb is None:
        bg_rgb = (255, 255, 255)

    target = 3.0 if _is_large_text(element) else 4.5
    current = contrast_ratio(fg_rgb, bg_rgb)

    if current >= target:
        return None

    new_fg = adjust_color_for_contrast(fg_rgb, bg_rgb, target)
    new_hex = rgb_to_hex(*new_fg)

    if _COLOR_RE.search(style):
        new_style = _COLOR_RE.sub(f"color: {new_hex}", style)
    else:
        new_style = style.rstrip(";").rstrip() + f"; color: {new_hex}"

    element["style"] = new_style
    new_ratio = contrast_ratio(new_fg, bg_rgb)
    return f"Set text color to {new_hex} (contrast {new_ratio:.1f}:1)"


def remediate_insufficient_non_text_contrast(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Fix non-text contrast (UI components, borders, SVGs) to meet
    the 3:1 minimum ratio."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    if element.name == "svg":
        return _fix_svg_contrast(element)

    style = element.get("style", "")
    border_match = _BORDER_COLOR_RE.search(style)
    bg_rgb = _extract_bg_color(element)

    if border_match and bg_rgb:
        border_rgb = parse_color(border_match.group(1))
        if border_rgb:
            current = contrast_ratio(border_rgb, bg_rgb)
            if current >= 3.0:
                return None

            new_border = adjust_border_for_contrast(border_rgb, bg_rgb, 3.0)
            new_hex = rgb_to_hex(*new_border)
            new_ratio = contrast_ratio(new_border, bg_rgb)

            new_style = style.replace(border_match.group(1), new_hex)
            element["style"] = new_style
            return (
                f"Adjusted border color to {new_hex} "
                f"(contrast {new_ratio:.1f}:1, was {current:.1f}:1)"
            )

    # Fallback: ensure a visible border
    if bg_rgb:
        from utils.color_math import relative_luminance
        bg_lum = relative_luminance(bg_rgb)
        border_color = "#333333" if bg_lum > 0.5 else "#CCCCCC"
        if style:
            element["style"] = style.rstrip(";") + f"; border: 1px solid {border_color}"
        else:
            element["style"] = f"border: 1px solid {border_color}"
        return f"Added visible border ({border_color}) for non-text contrast"

    return None


# ── Helpers ──────────────────────────────────────────────────────────

def _extract_fg_color(style: str) -> Optional[Tuple[int, int, int]]:
    """Extract foreground text color from inline style."""
    match = _COLOR_RE.search(style)
    if match:
        return parse_color(match.group(1))
    return None


def _extract_bg_color(element) -> Optional[Tuple[int, int, int]]:
    """Extract background color, walking up ancestors if needed."""
    el = element
    while el and getattr(el, "name", None) not in (None, "[document]"):
        style = el.get("style", "") if hasattr(el, "get") else ""
        match = _BG_COLOR_RE.search(style)
        if match:
            rgb = parse_color(match.group(1))
            if rgb:
                return rgb
        el = getattr(el, "parent", None)
    return (255, 255, 255)


def _is_large_text(element) -> bool:
    """Determine if element contains large text (18pt+ or 14pt+ bold)."""
    tag = element.name if hasattr(element, "name") else ""
    if tag in ("h1", "h2", "h3"):
        return True
    style = element.get("style", "") if hasattr(element, "get") else ""
    size_match = re.search(r"font-size:\s*(\d+(?:\.\d+)?)\s*(px|pt|em|rem)", style)
    if size_match:
        size = float(size_match.group(1))
        unit = size_match.group(2)
        if unit == "pt":
            size *= 1.333
        elif unit in ("em", "rem"):
            size *= 16
        if size >= 24:
            return True
        if size >= 18.67:
            weight_match = re.search(r"font-weight:\s*(bold|[7-9]00)", style)
            if weight_match or tag in ("b", "strong"):
                return True
    return False


def _fix_svg_contrast(svg_element) -> Optional[str]:
    """Fix contrast for SVG graphical elements."""
    fill = svg_element.get("fill", "")
    stroke = svg_element.get("stroke", "")
    color_attr = "fill" if fill and fill != "none" else "stroke"
    color_val = fill if color_attr == "fill" else stroke

    if not color_val or color_val in ("none", "currentColor"):
        return None

    fg_rgb = parse_color(color_val)
    bg_rgb = _extract_bg_color(svg_element)

    if fg_rgb is None or bg_rgb is None:
        return None

    current = contrast_ratio(fg_rgb, bg_rgb)
    if current >= 3.0:
        return None

    new_fg = adjust_border_for_contrast(fg_rgb, bg_rgb, 3.0)
    new_hex = rgb_to_hex(*new_fg)
    svg_element[color_attr] = new_hex
    new_ratio = contrast_ratio(new_fg, bg_rgb)
    return (
        f"Adjusted SVG {color_attr} to {new_hex} "
        f"(contrast {new_ratio:.1f}:1, was {current:.1f}:1)"
    )


def _apply_fallback_contrast(element, style: str) -> Optional[str]:
    """Apply a safe fallback when colors can't be parsed."""
    bg_rgb = _extract_bg_color(element)
    if bg_rgb:
        from utils.color_math import relative_luminance
        bg_lum = relative_luminance(bg_rgb)
        new_color = "#000000" if bg_lum > 0.5 else "#FFFFFF"
    else:
        new_color = "#000000"

    if _COLOR_RE.search(style):
        new_style = _COLOR_RE.sub(f"color: {new_color}", style)
    else:
        new_style = style.rstrip(";").rstrip() + f"; color: {new_color}"

    element["style"] = new_style
    return f"Applied fallback text color {new_color} for contrast"


def _find_issue_element(soup: BeautifulSoup, issue: Dict[str, Any]):
    """Locate the element referenced by an audit issue."""
    ctx = issue.get("context", {})
    if not isinstance(ctx, dict):
        return None

    resolved = resolve_issue_element(soup, issue, id_index=get_id_index(soup))
    if resolved is not None:
        return resolved

    tag = ctx.get("element_name")
    if not tag:
        return None

    attrs = ctx.get("attributes", {})
    if not isinstance(attrs, dict):
        attrs = {}

    for attr in ("id", "style", "class", "fill", "stroke"):
        if attr in attrs:
            found = soup.find(tag, attrs={attr: attrs[attr]})
            if found:
                return found

    return None
