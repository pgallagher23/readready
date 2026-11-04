# SPDX-License-Identifier: AGPL-3.0-or-later
"""Input purpose identification checks."""

import re

from audit.base_check import AccessibilityCheck


INPUT_PURPOSE_PATTERNS = {
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

INPUT_TYPE_AUTOCOMPLETE = {
    "email": "email",
    "tel": "tel",
    "url": "url",
}


class InputPurposeCheck(AccessibilityCheck):
    """Check form inputs have autocomplete for known purposes (WCAG 1.3.5)."""

    def check(self) -> None:
        form_controls = self.soup.find_all(["input", "select", "textarea"])
        if not form_controls:
            self.add_issue(
                "compliant-input-purpose",
                "1.3.5",
                "minor",
                description="No form inputs present — criterion not applicable",
                status="compliant",
            )
            return

        found_issue = False

        for control in form_controls:
            input_type = control.get("type", "text")
            if input_type in ("hidden", "submit", "reset", "button", "checkbox", "radio", "file", "image"):
                continue

            if control.get("autocomplete"):
                continue

            name = (control.get("name") or "").lower().strip()
            id_val = (control.get("id") or "").lower().strip()

            expected = None

            if input_type in INPUT_TYPE_AUTOCOMPLETE:
                expected = INPUT_TYPE_AUTOCOMPLETE[input_type]

            if not expected:
                for pattern, autocomplete_val in INPUT_PURPOSE_PATTERNS.items():
                    if pattern in name or pattern in id_val:
                        expected = autocomplete_val
                        break

            if expected:
                found_issue = True
                identifier = name or id_val or input_type
                self.add_issue(
                    "input-missing-autocomplete",
                    "1.3.5",
                    "minor",
                    element=control,
                    description=f"Input '{identifier}' appears to collect {expected} data but lacks autocomplete attribute. Add autocomplete=\"{expected}\".",
                    status="needs_remediation",
                )

        if not found_issue:
            self.add_issue(
                "compliant-input-purpose",
                "1.3.5",
                "minor",
                description="Form inputs with identifiable purposes have appropriate autocomplete attributes",
                status="compliant",
            )
