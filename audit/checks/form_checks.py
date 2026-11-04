# SPDX-License-Identifier: AGPL-3.0-or-later
# Portions derived from Content Accessibility Utility on AWS
# (https://github.com/awslabs/content-accessibility-utility-on-aws),
# Copyright 2025 Amazon.com, Inc. or its affiliates, Apache-2.0.
# Modified by the ReadReady project; see NOTICE.
"""Form-related accessibility checks."""

from typing import List, Dict
from bs4 import Tag

from audit.base_check import AccessibilityCheck


class FormLabelCheck(AccessibilityCheck):
    """Check for proper form labels (WCAG 1.3.1, 3.3.2)."""

    def check(self) -> None:
        self._found_issue = False
        self._check_inputs()
        if not self._found_issue:
            self.add_issue(
                "compliant-form-labels",
                "3.3.2",
                "minor",
                description="Form controls have appropriate labels and instructions",
                status="compliant",
            )

    def _check_inputs(self) -> None:
        for input_elem in self.soup.find_all("input"):
            input_type = input_elem.get("type", "").lower()
            if input_type in ["hidden", "submit", "reset", "button"]:
                continue

            if not input_elem.has_attr("name"):
                self._found_issue = True
                self.add_issue(
                    "form-control-missing-name",
                    "1.3.1",
                    "major",
                    element=input_elem,
                    description=f"Form control ({input_type}) missing name attribute",
                )

            if not self._has_associated_label(input_elem):
                self._found_issue = True
                self.add_issue(
                    "form-control-missing-label",
                    "1.3.1",
                    "critical",
                    element=input_elem,
                    description=f"Form control ({input_type}) has no associated label",
                )

        for select_elem in self.soup.find_all("select"):
            if not select_elem.has_attr("name"):
                self._found_issue = True
                self.add_issue(
                    "form-control-missing-name",
                    "1.3.1",
                    "major",
                    element=select_elem,
                    description="Select control missing name attribute",
                )
            if not self._has_associated_label(select_elem):
                self._found_issue = True
                self.add_issue(
                    "form-control-missing-label",
                    "1.3.1",
                    "critical",
                    element=select_elem,
                    description="Select control has no associated label",
                )

        for textarea_elem in self.soup.find_all("textarea"):
            if not textarea_elem.has_attr("name"):
                self._found_issue = True
                self.add_issue(
                    "form-control-missing-name",
                    "1.3.1",
                    "major",
                    element=textarea_elem,
                    description="Textarea control missing name attribute",
                )
            if not self._has_associated_label(textarea_elem):
                self._found_issue = True
                self.add_issue(
                    "form-control-missing-label",
                    "1.3.1",
                    "critical",
                    element=textarea_elem,
                    description="Textarea control has no associated label",
                )

        for label_elem in self.soup.find_all("label"):
            label_text = self.get_element_text(label_elem)
            if not label_text:
                self._found_issue = True
                self.add_issue(
                    "form-label-empty",
                    "3.3.2",
                    "major",
                    element=label_elem,
                    description="Label element has no text content",
                )

    def _has_associated_label(self, form_control: Tag) -> bool:
        if form_control.has_attr("id"):
            matching_label = self.soup.find("label", attrs={"for": form_control["id"]})
            if matching_label:
                return True

        parent_label = form_control.find_parent("label")
        if parent_label:
            return True

        if form_control.has_attr("aria-labelledby"):
            label_ids = form_control["aria-labelledby"].split()
            for label_id in label_ids:
                label_elem = self.soup.find(id=label_id)
                if label_elem and self.get_element_text(label_elem):
                    return True

        if form_control.has_attr("aria-label") and form_control["aria-label"].strip():
            return True

        if form_control.has_attr("title") and form_control["title"].strip():
            return True

        return False


class FormRequiredFieldCheck(AccessibilityCheck):
    """Check for proper indication of required form fields (WCAG 3.3.2)."""

    def check(self) -> None:
        form_controls = self.soup.find_all(["input", "select", "textarea"])

        for control in form_controls:
            if control.name == "input":
                input_type = control.get("type", "").lower()
                if input_type in ["hidden", "submit", "reset", "button"]:
                    continue

            is_required = (
                control.has_attr("required") or control.get("aria-required") == "true"
            )

            if is_required:
                has_visual_indication = False

                if control.has_attr("id"):
                    label = self.soup.find("label", attrs={"for": control["id"]})
                    if label:
                        label_text = self.get_element_text(label)
                        if "*" in label_text or "required" in label_text.lower():
                            has_visual_indication = True

                parent_label = control.find_parent("label")
                if parent_label:
                    label_text = self.get_element_text(parent_label)
                    if "*" in label_text or "required" in label_text.lower():
                        has_visual_indication = True

                if not control.has_attr("aria-required"):
                    self.add_issue(
                        "form-required-field-missing-aria",
                        "3.3.2",
                        "minor",
                        element=control,
                        description="Required form field missing aria-required='true' attribute",
                    )

                if not has_visual_indication:
                    self.add_issue(
                        "form-required-field-not-indicated",
                        "3.3.2",
                        "major",
                        element=control,
                        description="Required form field not visually indicated as required",
                    )


class FormFieldsetCheck(AccessibilityCheck):
    """Check for proper use of fieldset and legend (WCAG 1.3.1)."""

    def check(self) -> None:
        fieldsets = self.soup.find_all("fieldset")
        for fieldset in fieldsets:
            legend = fieldset.find("legend")
            if not legend or not self.get_element_text(legend).strip():
                self.add_issue(
                    "form-fieldset-missing-legend",
                    "1.3.1",
                    "major",
                    element=fieldset,
                    description="Fieldset missing legend element or legend has no content",
                )

        forms = self.soup.find_all("form")
        for form in forms:
            self._check_radio_groups(form)
            self._check_checkbox_groups(form)

    def _check_radio_groups(self, form: Tag) -> None:
        radio_groups: Dict[str, List[Tag]] = {}
        for radio in form.find_all("input", attrs={"type": "radio"}):
            if radio.has_attr("name"):
                name = radio["name"]
                if name not in radio_groups:
                    radio_groups[name] = []
                radio_groups[name].append(radio)

        for name, radios in radio_groups.items():
            if len(radios) > 1:
                in_fieldset = any(r.find_parent("fieldset") for r in radios)
                if not in_fieldset:
                    self.add_issue(
                        "form-related-controls-no-fieldset",
                        "1.3.1",
                        "major",
                        element=radios[0],
                        description=f"Group of {len(radios)} radio buttons should be wrapped in fieldset with legend",
                    )

    def _check_checkbox_groups(self, form: Tag) -> None:
        checkboxes = form.find_all("input", attrs={"type": "checkbox"})
        if len(checkboxes) <= 2:
            return

        checkbox_groups = []
        current_group = []

        for checkbox in checkboxes:
            if not current_group:
                current_group.append(checkbox)
            else:
                if self._share_close_parent(checkbox, current_group[-1], max_levels=3):
                    current_group.append(checkbox)
                else:
                    if len(current_group) > 1:
                        checkbox_groups.append(current_group)
                    current_group = [checkbox]

        if len(current_group) > 1:
            checkbox_groups.append(current_group)

        for group in checkbox_groups:
            in_fieldset = any(cb.find_parent("fieldset") for cb in group)
            if not in_fieldset:
                self.add_issue(
                    "form-related-controls-no-fieldset",
                    "1.3.1",
                    "minor",
                    element=group[0],
                    description=f"Group of {len(group)} checkboxes should be wrapped in fieldset with legend",
                )

    def _share_close_parent(self, elem1: Tag, elem2: Tag, max_levels: int = 3) -> bool:
        elem1_parents = []
        parent = elem1.parent
        for _ in range(max_levels):
            if parent:
                elem1_parents.append(parent)
                parent = parent.parent
            else:
                break

        parent = elem2.parent
        for _ in range(max_levels):
            if parent:
                if parent in elem1_parents:
                    return True
                parent = parent.parent
            else:
                break
        return False
