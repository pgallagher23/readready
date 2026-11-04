# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keyboard accessibility remediation strategies (WCAG 2.1.1).

Fixes interactive elements that are not keyboard accessible.
"""

import logging
import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from remediate.element_resolver import get_id_index, resolve_issue_element

logger = logging.getLogger(__name__)


def remediate_interactive_tabindex_negative(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Remove tabindex='-1' from interactive elements so they are
    reachable via keyboard."""
    ctx = issue.get("context", {})
    if not isinstance(ctx, dict):
        return None

    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    if element.get("tabindex") == "-1":
        del element["tabindex"]
        return f"Removed tabindex='-1' from <{element.name}>"

    return None


def remediate_onclick_not_keyboard_accessible(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Make elements with onclick keyboard accessible by adding role,
    tabindex, and a keydown handler."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    if not element.get("onclick"):
        return None

    # Add button role if no role present
    if not element.get("role"):
        element["role"] = "button"

    # Make focusable
    if not element.has_attr("tabindex"):
        element["tabindex"] = "0"

    # Add keyboard handler that triggers onclick on Enter/Space
    if not element.get("onkeydown"):
        element["onkeydown"] = (
            "if(event.key==='Enter'||event.key===' ')"
            "{event.preventDefault();this.click();}"
        )

    tag = element.name
    return f"Added role='button', tabindex='0', and onkeydown to <{tag}>"


def remediate_onclick_missing_keyboard_handler(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add keyboard handler to focusable elements that have onclick
    but no onkeydown."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    if element.get("onkeydown"):
        return None

    element["onkeydown"] = (
        "if(event.key==='Enter'||event.key===' ')"
        "{event.preventDefault();this.click();}"
    )
    return f"Added onkeydown handler to <{element.name}>"


def remediate_interactive_missing_role(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add role='button' to elements acting as controls (onclick +
    tabindex) but missing a role."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    if element.get("role"):
        return None

    element["role"] = "button"
    return f"Added role='button' to <{element.name}>"


def flag_potential_keyboard_trap(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Flag a potential keyboard trap for manual review.

    Keyboard traps cannot be reliably detected or fixed automatically —
    they require human testing with actual keyboard navigation.  This
    strategy marks the issue for manual review without modifying the DOM.
    """
    element = _find_issue_element(soup, issue)
    tag = element.name if element is not None else "unknown"
    issue["remediation_status"] = "requires_manual_review"
    issue["remediation_source"] = "local_pipeline"
    return f"Flagged <{tag}> for manual keyboard-trap review (no automatic fix possible)"


def flag_container_may_trap_focus(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Flag a container that may trap keyboard focus for manual review.

    Containers with tabindex and scripted focus management may trap
    users — this requires manual testing to confirm and fix.
    """
    element = _find_issue_element(soup, issue)
    tag = element.name if element is not None else "unknown"
    issue["remediation_status"] = "requires_manual_review"
    issue["remediation_source"] = "local_pipeline"
    return f"Flagged <{tag}> container for manual focus-trap review (no automatic fix possible)"


def remediate_mouse_only_handler(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add keyboard equivalents for mouse-only event handlers."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    # Map mouse handlers to keyboard equivalents
    mouse_to_key = {
        "onmousedown": "onkeydown",
        "onmouseup": "onkeyup",
        "ondblclick": "onkeydown",
    }

    added = []
    for mouse_handler, key_handler in mouse_to_key.items():
        if element.get(mouse_handler) and not element.get(key_handler):
            handler_code = element[mouse_handler]
            element[key_handler] = (
                f"if(event.key==='Enter'||event.key===' ')"
                f"{{event.preventDefault();{handler_code}}}"
            )
            added.append(key_handler)

    # Make focusable if not already
    if added and not element.has_attr("tabindex"):
        natively_focusable = {"a", "button", "input", "select", "textarea"}
        if element.name not in natively_focusable:
            element["tabindex"] = "0"
            added.append("tabindex='0'")

    if added:
        return f"Added {', '.join(added)} to <{element.name}>"
    return None


# ── Helpers ──────────────────────────────────────────────────────────

def _find_issue_element(soup: BeautifulSoup, issue: Dict[str, Any]):
    """Locate the element referenced by an audit issue."""
    ctx = issue.get("context", {})
    if not isinstance(ctx, dict):
        return None

    # Try CSS path from location
    resolved = resolve_issue_element(soup, issue, id_index=get_id_index(soup))
    if resolved is not None:
        return resolved

    # Fallback: use element tag + attributes from context
    tag = ctx.get("element_name")
    if not tag:
        return None

    attrs = ctx.get("attributes", {})
    if not isinstance(attrs, dict):
        attrs = {}

    # Try finding by tag + distinguishing attributes
    for attr in ("id", "name", "onclick", "onmousedown", "role"):
        if attr in attrs:
            found = soup.find(tag, attrs={attr: attrs[attr]})
            if found:
                return found

    return None
