# SPDX-License-Identifier: AGPL-3.0-or-later
"""Content reflow remediation strategies (WCAG 1.4.10).

Converts fixed widths to max-width, removes nowrap, and fixes
horizontal scroll requirements.
"""

import logging
import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from remediate.element_resolver import get_id_index, resolve_issue_element

logger = logging.getLogger(__name__)


def remediate_fixed_width_prevents_reflow(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Replace fixed width >320px with max-width so content can reflow."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    style = element.get("style", "")

    def _width_to_max_width(match):
        prefix = match.group(1)  # "width" or "min-width"
        value = match.group(2)
        px = int(value)
        if px > 320:
            return f"max-width: {value}px; width: 100%"
        return match.group(0)

    new_style = re.sub(
        r"(?:min-)?width\s*:\s*(\d+)\s*px",
        lambda m: f"max-width: {m.group(1)}px; width: 100%"
        if int(m.group(1)) > 320
        else m.group(0),
        style,
    )
    if new_style != style:
        element["style"] = new_style
        return f"Replaced fixed width with max-width on <{element.name}>"
    return None


def remediate_nowrap_text(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Remove white-space:nowrap from text elements."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    style = element.get("style", "")
    new_style = re.sub(
        r"white-space\s*:\s*nowrap\s*;?\s*", "", style
    ).strip()

    if new_style != style:
        if new_style:
            element["style"] = new_style
        else:
            del element["style"]
        return f"Removed white-space:nowrap from <{element.name}>"
    return None


def remediate_horizontal_scroll_required(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Remove overflow-x:scroll/auto from text containers to prevent
    forced horizontal scrolling."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    style = element.get("style", "")
    new_style = re.sub(
        r"overflow-x\s*:\s*(scroll|auto)\s*;?\s*", "", style
    ).strip()

    if new_style != style:
        if new_style:
            element["style"] = new_style
        else:
            del element["style"]
        return f"Removed overflow-x from <{element.name}>"
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
