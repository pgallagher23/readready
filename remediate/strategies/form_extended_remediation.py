# SPDX-License-Identifier: AGPL-3.0-or-later
"""Extended form remediation strategies.

WCAG 1.3.1 — Fix form controls missing name attribute and empty labels.
WCAG 3.3.2 — Add aria-required to required fields.
"""

import logging
import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from remediate.element_resolver import get_id_index, resolve_issue_element

logger = logging.getLogger(__name__)


def remediate_form_control_missing_name(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add a name attribute to form controls that lack one, derived from
    id, aria-label, or associated label text."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    if element.get("name"):
        return None

    # Derive name from id
    name = element.get("id", "")

    # Try aria-label
    if not name:
        name = element.get("aria-label", "")

    # Try associated label
    if not name and element.get("id"):
        label = soup.find("label", attrs={"for": element["id"]})
        if label:
            name = label.get_text(strip=True)

    # Try parent label
    if not name:
        parent_label = element.find_parent("label")
        if parent_label:
            name = parent_label.get_text(strip=True)

    if not name:
        # Generate from element type
        input_type = element.get("type", element.name)
        siblings = len(list(element.find_previous_siblings(element.name)))
        name = f"{input_type}-{siblings + 1}"

    # Sanitise for use as a name attribute
    name = re.sub(r"[^a-zA-Z0-9_-]", "-", name.lower().strip())
    name = re.sub(r"-+", "-", name).strip("-")
    if not name:
        name = "field-1"

    element["name"] = name
    return f"Added name='{name}' to <{element.name}>"


def remediate_form_label_empty(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Populate empty labels with text derived from the associated
    form control."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    # Check if label now has text
    if element.get_text(strip=True):
        return None

    # Find the associated control
    for_id = element.get("for", "")
    control = soup.find(id=for_id) if for_id else None

    if control is None:
        # Check if there's a control inside the label
        control = element.find(["input", "select", "textarea"])

    label_text = "Label"
    if control:
        if control.get("placeholder"):
            label_text = control["placeholder"]
        elif control.get("name"):
            label_text = " ".join(
                word.capitalize()
                for word in re.split(r"[_\-]", control["name"])
            )
        elif control.get("type"):
            label_text = control["type"].capitalize()
        elif control.get("aria-label"):
            label_text = control["aria-label"]

    element.string = label_text
    return f"Added text '{label_text}' to empty label"


def remediate_form_required_field_missing_aria(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add aria-required='true' to required form fields."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    if element.get("aria-required") == "true":
        return None

    element["aria-required"] = "true"
    return f"Added aria-required='true' to <{element.name}>"


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

    for attr in ("id", "name", "for", "type"):
        if attr in attrs:
            found = soup.find(tag, attrs={attr: attrs[attr]})
            if found:
                return found

    return None
