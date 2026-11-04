# SPDX-License-Identifier: AGPL-3.0-or-later
"""Input purpose remediation strategies (WCAG 1.3.5).

Adds autocomplete attributes to form fields with identifiable purposes.
"""

import logging
import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from remediate.element_resolver import get_id_index, resolve_issue_element

logger = logging.getLogger(__name__)

# Same mapping as the audit check — kept in sync
_NAME_TO_AUTOCOMPLETE = {
    "name": "name",
    "full-name": "name",
    "fullname": "name",
    "first-name": "given-name",
    "firstname": "given-name",
    "given-name": "given-name",
    "last-name": "family-name",
    "lastname": "family-name",
    "family-name": "family-name",
    "surname": "family-name",
    "email": "email",
    "e-mail": "email",
    "phone": "tel",
    "tel": "tel",
    "telephone": "tel",
    "mobile": "tel",
    "address": "street-address",
    "street": "street-address",
    "street-address": "street-address",
    "city": "address-level2",
    "state": "address-level1",
    "province": "address-level1",
    "zip": "postal-code",
    "zipcode": "postal-code",
    "zip-code": "postal-code",
    "postal": "postal-code",
    "postal-code": "postal-code",
    "country": "country-name",
    "username": "username",
    "user-name": "username",
    "password": "current-password",
    "new-password": "new-password",
    "cc-number": "cc-number",
    "card-number": "cc-number",
    "cc-name": "cc-name",
    "cc-exp": "cc-exp",
    "organization": "organization",
    "company": "organization",
    "org": "organization",
}

_TYPE_TO_AUTOCOMPLETE = {
    "email": "email",
    "tel": "tel",
    "url": "url",
}


def remediate_input_missing_autocomplete(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add autocomplete attribute to inputs with identifiable purposes."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    if element.get("autocomplete"):
        return None

    input_type = (element.get("type") or "text").lower()
    name = (element.get("name") or "").lower().strip()
    id_val = (element.get("id") or "").lower().strip()

    # Determine the correct autocomplete value
    autocomplete = None

    if input_type in _TYPE_TO_AUTOCOMPLETE:
        autocomplete = _TYPE_TO_AUTOCOMPLETE[input_type]

    if not autocomplete:
        for pattern, ac_val in _NAME_TO_AUTOCOMPLETE.items():
            if pattern in name or pattern in id_val:
                autocomplete = ac_val
                break

    if autocomplete:
        element["autocomplete"] = autocomplete
        identifier = name or id_val or input_type
        return f"Added autocomplete='{autocomplete}' to input '{identifier}'"
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

    for attr in ("id", "name", "type"):
        if attr in attrs:
            found = soup.find(tag, attrs={attr: attrs[attr]})
            if found:
                return found

    return None
