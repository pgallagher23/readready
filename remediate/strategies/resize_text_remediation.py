# SPDX-License-Identifier: AGPL-3.0-or-later
"""Text resize remediation strategies (WCAG 1.4.4).

Converts fixed font sizes to relative units and fixes overflow clipping.
"""

import logging
import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from remediate.element_resolver import get_id_index, resolve_issue_element

logger = logging.getLogger(__name__)

# Base font size assumption for px→rem conversion
_BASE_PX = 16


def remediate_fixed_font_size(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Convert px/pt font-size to rem in both inline styles and
    stylesheet blocks."""
    fixed = 0

    # ── Inline styles ──
    for element in soup.find_all(attrs={"style": True}):
        style = element.get("style", "")
        new_style = _convert_font_size_in_css(style)
        if new_style != style:
            element["style"] = new_style
            fixed += 1

    # ── <style> blocks ──
    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or ""
        if not css_text:
            continue
        new_css = _convert_font_size_in_css(css_text)
        if new_css != css_text:
            style_tag.string = new_css
            fixed += 1

    if fixed:
        return f"Converted fixed font-size to rem in {fixed} location(s)"
    return None


def remediate_text_overflow_hidden(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Change overflow:hidden to overflow:auto on text containers so
    resized text isn't clipped."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    style = element.get("style", "")
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

def _convert_font_size_in_css(css: str) -> str:
    """Replace font-size: Npx/pt with font-size: Nrem."""
    def _to_rem(match):
        size = float(match.group(1))
        unit = match.group(2)
        if unit == "pt":
            size *= 1.333  # pt → px
        rem = round(size / _BASE_PX, 3)
        return f"font-size: {rem}rem"

    return re.sub(
        r"font-size\s*:\s*(\d+(?:\.\d+)?)\s*(px|pt)\b",
        _to_rem,
        css,
    )


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
