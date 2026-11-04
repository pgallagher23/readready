# SPDX-License-Identifier: AGPL-3.0-or-later
"""Error identification remediation strategies (WCAG 3.3.1).

Adds error message containers, pattern descriptions, and live regions
to forms so that input errors can be identified and described.
"""

import logging
import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from remediate.element_resolver import get_id_index, resolve_issue_element

logger = logging.getLogger(__name__)


def remediate_required_field_no_error_mechanism(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add an error message container linked via aria-describedby to
    required form fields."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    # Skip if already has error mechanism
    if element.get("aria-errormessage") or element.get("aria-describedby"):
        return None

    # Generate a unique error ID
    control_id = element.get("id") or element.get("name") or "field"
    error_id = f"{control_id}-error"

    # Ensure uniqueness
    counter = 1
    while soup.find(id=error_id):
        error_id = f"{control_id}-error-{counter}"
        counter += 1

    # Create error container
    error_span = soup.new_tag("span")
    error_span["id"] = error_id
    error_span["class"] = "field-error"
    error_span["aria-live"] = "polite"
    error_span.string = ""

    # Link the control to the error container
    element["aria-describedby"] = error_id

    # Insert error container after the element
    element.insert_after(error_span)

    return f"Added error container '{error_id}' for required field"


def remediate_pattern_input_no_description(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add a title attribute describing the expected pattern for
    inputs with pattern validation."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    if element.get("title"):
        return None

    pattern = element.get("pattern", "")
    if not pattern:
        return None

    # Generate a human-readable description from common patterns
    description = _pattern_to_description(pattern)
    element["title"] = description

    return f"Added title='{description}' for pattern-validated input"


def remediate_type_validated_input_no_description(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add a placeholder hint for type-validated inputs that lack
    any format description."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    if element.get("placeholder") or element.get("title"):
        return None

    input_type = element.get("type", "").lower()
    hints = {
        "email": "name@example.com",
        "url": "https://example.com",
        "tel": "123-456-7890",
        "number": "Enter a number",
        "date": "YYYY-MM-DD",
        "time": "HH:MM",
        "datetime-local": "YYYY-MM-DDTHH:MM",
    }

    hint = hints.get(input_type)
    if hint:
        element["placeholder"] = hint
        return f"Added placeholder='{hint}' for type='{input_type}' input"

    return None


def remediate_aria_invalid_no_error_message(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add an error container for elements marked aria-invalid='true'
    but lacking an associated error message."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    if element.get("aria-errormessage") or element.get("aria-describedby"):
        return None

    control_id = element.get("id") or element.get("name") or "field"
    error_id = f"{control_id}-error"

    counter = 1
    while soup.find(id=error_id):
        error_id = f"{control_id}-error-{counter}"
        counter += 1

    error_span = soup.new_tag("span")
    error_span["id"] = error_id
    error_span["class"] = "field-error"
    error_span["role"] = "alert"
    error_span.string = ""

    element["aria-errormessage"] = error_id
    element.insert_after(error_span)

    return f"Added error message container '{error_id}' for aria-invalid element"


def remediate_aria_errormessage_missing_target(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Create the missing target element for aria-errormessage."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    error_id = element.get("aria-errormessage", "").strip()
    if not error_id:
        return None

    # Check if target now exists (may have been created by another fix)
    if soup.find(id=error_id):
        return None

    error_span = soup.new_tag("span")
    error_span["id"] = error_id
    error_span["class"] = "field-error"
    error_span["role"] = "alert"
    error_span.string = ""

    element.insert_after(error_span)
    return f"Created missing error message target '{error_id}'"


def remediate_form_no_error_live_region(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add an error live region inside forms that have required fields
    but no alert/aria-live container."""
    # Check if one already exists
    if soup.find(attrs={"role": "alert"}) or soup.find(
        attrs={"aria-live": "assertive"}
    ):
        return None

    forms = soup.find_all("form")
    if not forms:
        return None

    added = 0
    for form in forms:
        required = form.find_all(attrs={"required": True})
        required_aria = form.find_all(attrs={"aria-required": "true"})
        if not required and not required_aria:
            continue

        # Check if this form already has an alert region
        if form.find(attrs={"role": "alert"}) or form.find(
            attrs={"aria-live": "assertive"}
        ):
            continue

        error_div = soup.new_tag("div")
        error_div["role"] = "alert"
        error_div["aria-live"] = "assertive"
        error_div["class"] = "form-errors"
        error_div.string = ""

        # Insert at the top of the form
        form.insert(0, error_div)
        added += 1

    if added:
        return f"Added error live region to {added} form(s)"
    return None


# ── Helpers ──────────────────────────────────────────────────────────

def _pattern_to_description(pattern: str) -> str:
    """Convert a regex pattern to a human-readable description."""
    descriptions = {
        r"[0-9]{5}": "Enter a 5-digit number (e.g., 49008)",
        r"[0-9]{5}(-[0-9]{4})?": "Enter a ZIP code (e.g., 49008 or 49008-1234)",
        r"[A-Za-z]{2,}": "Enter at least 2 letters",
        r"[A-Z]{2}": "Enter exactly 2 uppercase letters",
        r"[A-Z]{3}": "Enter exactly 3 uppercase letters",
        r"\d{3}-\d{3}-\d{4}": "Enter a phone number (e.g., 123-456-7890)",
        r"\d{3}-\d{2}-\d{4}": "Enter a SSN (e.g., 123-45-6789)",
    }

    if pattern in descriptions:
        return descriptions[pattern]

    # Generate a generic description
    return f"Enter a value matching the format: {pattern}"


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

    for attr in ("id", "name", "pattern", "type", "aria-invalid"):
        if attr in attrs:
            found = soup.find(tag, attrs={attr: attrs[attr]})
            if found:
                return found

    return None
