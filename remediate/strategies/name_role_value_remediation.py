# SPDX-License-Identifier: AGPL-3.0-or-later
"""Name, Role, Value remediation strategies (WCAG 4.1.2).

Fixes invalid ARIA roles, broken references, missing accessible names,
and missing required ARIA attributes on custom widgets.
"""

import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from remediate.element_resolver import get_id_index, resolve_issue_element

logger = logging.getLogger(__name__)


def remediate_invalid_aria_role(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Remove invalid ARIA role attribute."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    role = element.get("role", "")
    if role:
        del element["role"]
        return f"Removed invalid role='{role}' from <{element.name}>"
    return None


def remediate_aria_labelledby_missing_target(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Remove broken aria-labelledby references."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    ref_ids = element.get("aria-labelledby", "").strip().split()
    valid_ids = [rid for rid in ref_ids if soup.find(id=rid)]

    if valid_ids:
        element["aria-labelledby"] = " ".join(valid_ids)
        removed = set(ref_ids) - set(valid_ids)
        return (
            f"Removed broken aria-labelledby ref(s): "
            f"{', '.join(removed)}"
        )
    else:
        del element["aria-labelledby"]
        return "Removed aria-labelledby with no valid targets"


def remediate_aria_labelledby_empty_target(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Remove aria-labelledby references to empty elements."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    ref_ids = element.get("aria-labelledby", "").strip().split()
    valid_ids = []
    for rid in ref_ids:
        target = soup.find(id=rid)
        if target and target.get_text(strip=True):
            valid_ids.append(rid)

    if valid_ids:
        element["aria-labelledby"] = " ".join(valid_ids)
    else:
        del element["aria-labelledby"]

    return "Removed aria-labelledby ref(s) to empty elements"


def remediate_aria_describedby_missing_target(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Remove broken aria-describedby references."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    ref_ids = element.get("aria-describedby", "").strip().split()
    valid_ids = [rid for rid in ref_ids if soup.find(id=rid)]

    if valid_ids:
        element["aria-describedby"] = " ".join(valid_ids)
        removed = set(ref_ids) - set(valid_ids)
        return (
            f"Removed broken aria-describedby ref(s): "
            f"{', '.join(removed)}"
        )
    else:
        del element["aria-describedby"]
        return "Removed aria-describedby with no valid targets"


def remediate_button_missing_name(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add an accessible name to buttons that lack one."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    # Check if button now has text (may have been fixed by other remediation)
    if element.get_text(strip=True):
        return None
    if element.get("aria-label"):
        return None

    # Try to derive a name from context
    name = None

    # Check for value attribute (input type=submit/button)
    if element.get("value"):
        name = element["value"]

    # Check for title
    if not name and element.get("title"):
        name = element["title"]

    # Check for nearby label text
    if not name:
        label = element.find_parent("label")
        if label:
            name = label.get_text(strip=True)

    # Check for form submit context
    if not name:
        form = element.find_parent("form")
        if form:
            name = "Submit"
        else:
            name = "Button"

    element["aria-label"] = name
    return f"Added aria-label='{name}' to button"


def remediate_input_image_missing_alt(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add alt attribute to input type='image'."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    if element.get("alt"):
        return None

    # Derive alt from value, title, or src
    alt = element.get("value") or element.get("title")
    if not alt:
        src = element.get("src", "")
        if src:
            # Extract filename without extension
            parsed = urlparse(src)
            filename = parsed.path.rsplit("/", 1)[-1]
            alt = filename.rsplit(".", 1)[0].replace("-", " ").replace(
                "_", " "
            ).strip().capitalize()
    if not alt:
        alt = "Submit"

    element["alt"] = alt
    return f"Added alt='{alt}' to image input"


def remediate_iframe_missing_title(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add title attribute to iframes."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    if element.get("title") or element.get("aria-label"):
        return None

    # Derive title from src
    title = "Embedded content"
    src = element.get("src", "")
    if src:
        parsed = urlparse(src)
        hostname = parsed.hostname or ""
        path = parsed.path or ""

        if hostname:
            title = f"Content from {hostname}"
        elif path:
            filename = path.rsplit("/", 1)[-1]
            title = filename.rsplit(".", 1)[0].replace("-", " ").replace(
                "_", " "
            ).strip().capitalize() or "Embedded content"

    element["title"] = title
    return f"Added title='{title}' to iframe"


def remediate_custom_widget_missing_aria_attrs(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add missing required ARIA attributes with sensible defaults."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    role = element.get("role", "").strip().lower()

    # Default values for required ARIA attributes
    defaults = {
        "checkbox": {"aria-checked": "false"},
        "switch": {"aria-checked": "false"},
        "radio": {"aria-checked": "false"},
        "menuitemcheckbox": {"aria-checked": "false"},
        "menuitemradio": {"aria-checked": "false"},
        "combobox": {"aria-expanded": "false"},
        "option": {"aria-selected": "false"},
        "slider": {"aria-valuenow": "0"},
        "spinbutton": {"aria-valuenow": "0"},
        "scrollbar": {"aria-valuenow": "0"},
    }

    if role not in defaults:
        return None

    added = []
    for attr, default_val in defaults[role].items():
        if not element.has_attr(attr):
            element[attr] = default_val
            added.append(f"{attr}='{default_val}'")

    # scrollbar also requires aria-controls
    if role == "scrollbar" and not element.get("aria-controls"):
        # Can't auto-determine the target — add placeholder
        element["aria-controls"] = ""
        added.append("aria-controls (needs manual target)")

    if added:
        return f"Added {', '.join(added)} to role='{role}'"
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

    for attr in ("id", "role", "src", "aria-labelledby",
                 "aria-describedby", "aria-invalid"):
        if attr in attrs:
            found = soup.find(tag, attrs={attr: attrs[attr]})
            if found:
                return found

    # Last resort: find by tag name (may match wrong element)
    return None
