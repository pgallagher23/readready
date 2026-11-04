# SPDX-License-Identifier: AGPL-3.0-or-later
"""Error identification accessibility checks (WCAG 3.3.1).

3.3.1 Error Identification: If an input error is automatically detected,
the item in error is identified and the error is described to the user
in text.
"""

from audit.base_check import AccessibilityCheck


class ErrorIdentificationCheck(AccessibilityCheck):
    """Check that form error identification mechanisms are in place
    (WCAG 3.3.1)."""

    def check(self) -> None:
        self._found_issue = False
        self._check_required_fields_error_support()
        self._check_pattern_validated_fields()
        self._check_aria_invalid_usage()
        self._check_error_message_association()
        if not self._found_issue:
            self.add_issue(
                "compliant-error-identification",
                "3.3.1",
                "minor",
                description="Error identification mechanisms are properly implemented",
                status="compliant",
            )

    def _check_required_fields_error_support(self) -> None:
        """Check that required fields have mechanisms to identify
        errors when left empty."""
        form_controls = self.soup.find_all(["input", "select", "textarea"])

        for control in form_controls:
            if control.name == "input":
                input_type = control.get("type", "text").lower()
                if input_type in ("hidden", "submit", "reset", "button",
                                  "image"):
                    continue

            is_required = (
                control.has_attr("required")
                or control.get("aria-required") == "true"
            )
            if not is_required:
                continue

            # Check for error identification support:
            # 1. aria-errormessage pointing to an element
            # 2. aria-describedby that could contain error text
            # 3. A sibling/nearby error element pattern
            has_error_mechanism = (
                control.has_attr("aria-errormessage")
                or control.has_attr("aria-describedby")
                or self._has_nearby_error_container(control)
            )

            if not has_error_mechanism:
                self._found_issue = True
                self.add_issue(
                    "required-field-no-error-mechanism",
                    "3.3.1",
                    "major",
                    element=control,
                    description=(
                        f"Required {control.name} field has no error "
                        f"identification mechanism (add aria-errormessage "
                        f"or aria-describedby to reference an error "
                        f"container)"
                    ),
                )

    def _check_pattern_validated_fields(self) -> None:
        """Check that inputs with pattern validation have error
        descriptions."""
        inputs = self.soup.find_all("input", attrs={"pattern": True})
        for inp in inputs:
            if not inp.get("title") and not inp.has_attr("aria-describedby"):
                self._found_issue = True
                self.add_issue(
                    "pattern-input-no-description",
                    "3.3.1",
                    "major",
                    element=inp,
                    description=(
                        "Input with pattern validation has no error "
                        "description (add title attribute describing the "
                        "expected format, or use aria-describedby)"
                    ),
                )

        # Also check type-validated inputs that have specific formats
        type_validated = self.soup.find_all(
            "input",
            attrs={"type": lambda t: t and t.lower() in (
                "email", "url", "number", "tel", "date",
                "datetime-local", "time", "week", "month",
            )},
        )
        for inp in type_validated:
            input_type = inp.get("type", "").lower()
            has_description = (
                inp.has_attr("aria-describedby")
                or inp.has_attr("aria-errormessage")
                or inp.get("title")
                or inp.get("placeholder")
            )
            # Only flag if also required — optional type-validated
            # fields with browser defaults are acceptable
            is_required = (
                inp.has_attr("required")
                or inp.get("aria-required") == "true"
            )
            if is_required and not has_description:
                self._found_issue = True
                self.add_issue(
                    "type-validated-input-no-description",
                    "3.3.1",
                    "minor",
                    element=inp,
                    description=(
                        f"Required input type='{input_type}' has no format "
                        f"hint (add placeholder, title, or aria-describedby "
                        f"to describe expected format)"
                    ),
                )

    def _check_aria_invalid_usage(self) -> None:
        """Verify that aria-invalid is used correctly when present."""
        elements = self.soup.find_all(attrs={"aria-invalid": "true"})
        for elem in elements:
            # When aria-invalid="true", there should be an error
            # message associated with the element
            has_error_msg = (
                elem.has_attr("aria-errormessage")
                or elem.has_attr("aria-describedby")
            )
            if not has_error_msg:
                self._found_issue = True
                self.add_issue(
                    "aria-invalid-no-error-message",
                    "3.3.1",
                    "major",
                    element=elem,
                    description=(
                        f"Element <{elem.name}> has aria-invalid='true' but "
                        f"no associated error message (add aria-errormessage "
                        f"or aria-describedby)"
                    ),
                )

            # Verify aria-errormessage target exists
            if elem.has_attr("aria-errormessage"):
                err_id = elem["aria-errormessage"].strip()
                target = self.soup.find(id=err_id)
                if not target:
                    self._found_issue = True
                    self.add_issue(
                        "aria-errormessage-missing-target",
                        "3.3.1",
                        "critical",
                        element=elem,
                        description=(
                            f"aria-errormessage references non-existent "
                            f"id '{err_id}'"
                        ),
                    )

    def _check_error_message_association(self) -> None:
        """Check elements with role='alert' or aria-live='assertive'
        that may contain error messages are properly structured."""
        alerts = self.soup.find_all(attrs={"role": "alert"})
        live_regions = self.soup.find_all(
            attrs={"aria-live": "assertive"}
        )
        error_containers = list(set(alerts + live_regions))

        if not error_containers:
            # Check if any forms exist — if forms exist but no error
            # containers, that's a potential issue
            forms = self.soup.find_all("form")
            required_fields = self.soup.find_all(
                attrs={"required": True}
            )
            required_aria = self.soup.find_all(
                attrs={"aria-required": "true"}
            )

            if forms and (required_fields or required_aria):
                self._found_issue = True
                self.add_issue(
                    "form-no-error-live-region",
                    "3.3.1",
                    "minor",
                    element=forms[0],
                    description=(
                        "Form with required fields has no error live "
                        "region (role='alert' or aria-live='assertive') "
                        "for announcing errors to assistive technology"
                    ),
                )

    def _has_nearby_error_container(self, control) -> bool:
        """Check if a form control has a nearby element that could
        serve as an error container."""
        # Check siblings
        parent = control.parent
        if parent:
            for sibling in parent.children:
                if not hasattr(sibling, "get"):
                    continue
                classes = sibling.get("class", [])
                if isinstance(classes, str):
                    classes = classes.split()
                role = sibling.get("role", "")

                # Common error container patterns
                if any(
                    c in " ".join(classes).lower()
                    for c in ("error", "invalid", "help-block",
                              "form-error", "field-error", "validation")
                ):
                    return True
                if role == "alert":
                    return True
                if sibling.get("aria-live") in ("assertive", "polite"):
                    return True

        return False
