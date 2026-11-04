# SPDX-License-Identifier: AGPL-3.0-or-later
"""Text spacing remediation strategies (WCAG 1.4.12).

Fixes fixed line-heights, !important overrides on spacing properties,
and overflow:hidden containers that would clip text when spacing changes.
"""

import logging
import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from remediate.element_resolver import get_id_index, resolve_issue_element

logger = logging.getLogger(__name__)


def remediate_fixed_line_height(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Convert fixed line-height (px/pt) to a unitless relative value."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    style = element.get("style", "")
    match = re.search(
        r"line-height\s*:\s*(\d+(?:\.\d+)?)\s*(px|pt)\b", style
    )
    if not match:
        return None

    size = float(match.group(1))
    unit = match.group(2)

    # Determine font-size to compute a relative ratio
    font_match = re.search(
        r"font-size\s*:\s*(\d+(?:\.\d+)?)\s*(px|pt|em|rem)\b", style
    )
    if font_match:
        font_size = float(font_match.group(1))
        font_unit = font_match.group(2)
        # Normalise to px for ratio calculation
        if font_unit == "pt":
            font_size *= 1.333
        if unit == "pt":
            size *= 1.333
        ratio = round(size / font_size, 2) if font_size > 0 else 1.5
    else:
        # No font-size in inline style — assume 16px default
        if unit == "pt":
            size *= 1.333
        ratio = round(size / 16, 2)

    # Ensure minimum 1.5 ratio per WCAG
    ratio = max(ratio, 1.5)

    new_style = re.sub(
        r"line-height\s*:\s*\d+(?:\.\d+)?\s*(?:px|pt)\b\s*;?\s*",
        f"line-height: {ratio}; ",
        style,
    ).strip()

    element["style"] = new_style
    return f"Converted line-height to unitless {ratio} on <{element.name}>"


def remediate_text_spacing_important_override(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Remove !important from text spacing properties in stylesheets."""
    spacing_props = ["line-height", "letter-spacing", "word-spacing"]
    fixed = 0

    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or ""
        if not css_text:
            continue

        new_css = css_text
        for prop in spacing_props:
            new_css = re.sub(
                rf"({prop}\s*:[^;]*?)\s*!important",
                r"\1",
                new_css,
            )
        if new_css != css_text:
            style_tag.string = new_css
            fixed += 1

    if fixed:
        return f"Removed !important from text spacing in {fixed} stylesheet(s)"
    return None


def remediate_text_spacing_overflow_risk(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Change overflow:hidden to overflow:auto on text containers with
    fixed height, so text isn't clipped when spacing increases."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    style = element.get("style", "")
    if "overflow" not in style:
        return None

    new_style = re.sub(
        r"overflow\s*:\s*hidden",
        "overflow: auto",
        style,
    )
    if new_style != style:
        element["style"] = new_style
        return f"Changed overflow:hidden to overflow:auto on <{element.name}>"
    return None


# ── Helpers ──────────────────────────────────────────────────────────

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

    for attr in ("id", "style"):
        if attr in attrs:
            found = soup.find(tag, attrs={attr: attrs[attr]})
            if found:
                return found

    return None
