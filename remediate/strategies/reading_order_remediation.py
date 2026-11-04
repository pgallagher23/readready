# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading order remediation strategies (WCAG 1.3.2).

Fixes positive tabindex and CSS reordering that disrupts reading sequence.
"""

import logging
import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from remediate.element_resolver import get_id_index, resolve_issue_element

logger = logging.getLogger(__name__)


def remediate_reading_order_tabindex(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Reset positive tabindex values to 0 to restore natural DOM order."""
    element = _find_issue_element(soup, issue)
    if element is not None:
        try:
            val = int(element.get("tabindex", "0"))
        except (ValueError, TypeError):
            return None
        if val > 0:
            element["tabindex"] = "0"
            return f"Changed tabindex='{val}' to '0' on <{element.name}>"
        return None

    # Fallback: fix all positive tabindex values
    fixed = 0
    for elem in soup.find_all(attrs={"tabindex": True}):
        try:
            val = int(elem.get("tabindex", "0"))
        except (ValueError, TypeError):
            continue
        if val > 0:
            elem["tabindex"] = "0"
            fixed += 1
    if fixed:
        return f"Reset {fixed} positive tabindex value(s) to 0"
    return None


def remediate_reading_order_css_reorder(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Remove CSS properties that reorder visual presentation away from
    DOM order (order, flex-direction reverse, direction:rtl)."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    style = element.get("style", "")
    original = style

    # Remove CSS order property (careful not to match border, z-order)
    style = re.sub(r"(?:^|(?<=[\s;]))order\s*:\s*-?\d+\s*;?\s*", "", style)
    # Remove flex-direction reverse
    style = re.sub(
        r"flex-direction\s*:\s*\w+-reverse\s*;?\s*", "", style
    )
    # Remove direction:rtl override
    style = re.sub(r"direction\s*:\s*rtl\s*;?\s*", "", style)

    style = style.strip()
    if style != original:
        if style:
            element["style"] = style
        else:
            del element["style"]
        return f"Removed CSS reorder properties from <{element.name}>"
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

    for attr in ("id", "style", "tabindex"):
        if attr in attrs:
            found = soup.find(tag, attrs={attr: attrs[attr]})
            if found:
                return found

    return None
