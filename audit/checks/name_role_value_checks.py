# SPDX-License-Identifier: AGPL-3.0-or-later
"""Name, Role, Value accessibility checks (WCAG 4.1.2).

Ensures that for all UI components, the name and role can be
programmatically determined, states/properties/values can be
programmatically set, and notification of changes is available.
"""

import re

from audit.base_check import AccessibilityCheck

# Valid ARIA roles per WAI-ARIA 1.1 specification
VALID_ARIA_ROLES = {
    # Widget roles
    "alert", "alertdialog", "button", "checkbox", "dialog", "gridcell",
    "link", "log", "marquee", "menuitem", "menuitemcheckbox",
    "menuitemradio", "option", "progressbar", "radio", "scrollbar",
    "searchbox", "slider", "spinbutton", "status", "switch", "tab",
    "tabpanel", "textbox", "timer", "tooltip", "treeitem",
    # Composite widget roles
    "combobox", "grid", "listbox", "menu", "menubar", "radiogroup",
    "tablist", "tree", "treegrid",
    # Document structure roles
    "application", "article", "cell", "columnheader", "definition",
    "directory", "document", "feed", "figure", "group", "heading",
    "img", "list", "listitem", "math", "none", "note", "presentation",
    "row", "rowgroup", "rowheader", "separator", "table", "term",
    "toolbar",
    # Landmark roles
    "banner", "complementary", "contentinfo", "form", "main",
    "navigation", "region", "search",
}

# ARIA roles that require an accessible name
ROLES_REQUIRING_NAME = {
    "button", "checkbox", "combobox", "gridcell", "link", "listbox",
    "menu", "menubar", "menuitem", "menuitemcheckbox", "menuitemradio",
    "option", "progressbar", "radio", "radiogroup", "scrollbar",
    "searchbox", "slider", "spinbutton", "switch", "tab", "tabpanel",
    "textbox", "treeitem",
    # Landmark roles that need a name when multiple exist
    "form", "region",
}

# Required ARIA attributes per role
REQUIRED_ARIA_ATTRS = {
    "checkbox": ["aria-checked"],
    "combobox": ["aria-expanded"],
    "heading": ["aria-level"],
    "menuitemcheckbox": ["aria-checked"],
    "menuitemradio": ["aria-checked"],
    "option": ["aria-selected"],
    "radio": ["aria-checked"],
    "scrollbar": ["aria-controls", "aria-valuenow"],
    "slider": ["aria-valuenow"],
    "spinbutton": ["aria-valuenow"],
    "switch": ["aria-checked"],
}


class NameRoleValueCheck(AccessibilityCheck):
    """Check that UI components have programmatically determinable
    name, role, and value (WCAG 4.1.2)."""

    def check(self) -> None:
        self._found_issue = False
        self._check_aria_roles()
        self._check_aria_labelledby_refs()
        self._check_aria_describedby_refs()
        self._check_custom_widgets()
        self._check_interactive_elements_name()
        if not self._found_issue:
            self.add_issue(
                "compliant-name-role-value",
                "4.1.2",
                "minor",
                description="UI components have programmatically determinable name, role, and value",
                status="compliant",
            )

    def _check_aria_roles(self) -> None:
        """Validate that all role attributes use valid ARIA role values."""
        elements = self.soup.find_all(attrs={"role": True})
        for elem in elements:
            roles = elem["role"].strip().split()
            for role in roles:
                if role.lower() not in VALID_ARIA_ROLES:
                    self._found_issue = True
                    self.add_issue(
                        "invalid-aria-role",
                        "4.1.2",
                        "critical",
                        element=elem,
                        description=(
                            f"Element <{elem.name}> has invalid ARIA role: "
                            f"'{role}'"
                        ),
                    )

    def _check_aria_labelledby_refs(self) -> None:
        """Verify that aria-labelledby references point to existing elements."""
        elements = self.soup.find_all(attrs={"aria-labelledby": True})
        for elem in elements:
            ref_ids = elem["aria-labelledby"].strip().split()
            for ref_id in ref_ids:
                target = self.soup.find(id=ref_id)
                if not target:
                    self._found_issue = True
                    self.add_issue(
                        "aria-labelledby-missing-target",
                        "4.1.2",
                        "critical",
                        element=elem,
                        description=(
                            f"aria-labelledby references non-existent id "
                            f"'{ref_id}'"
                        ),
                    )
                elif not self.get_element_text(target):
                    self._found_issue = True
                    self.add_issue(
                        "aria-labelledby-empty-target",
                        "4.1.2",
                        "major",
                        element=elem,
                        description=(
                            f"aria-labelledby references element '{ref_id}' "
                            f"which has no text content"
                        ),
                    )

    def _check_aria_describedby_refs(self) -> None:
        """Verify that aria-describedby references point to existing elements."""
        elements = self.soup.find_all(attrs={"aria-describedby": True})
        for elem in elements:
            ref_ids = elem["aria-describedby"].strip().split()
            for ref_id in ref_ids:
                target = self.soup.find(id=ref_id)
                if not target:
                    self._found_issue = True
                    self.add_issue(
                        "aria-describedby-missing-target",
                        "4.1.2",
                        "major",
                        element=elem,
                        description=(
                            f"aria-describedby references non-existent id "
                            f"'{ref_id}'"
                        ),
                    )

    def _check_custom_widgets(self) -> None:
        """Check that custom widgets with ARIA roles have required attributes."""
        elements = self.soup.find_all(attrs={"role": True})
        for elem in elements:
            role = elem.get("role", "").strip().lower()
            if role not in REQUIRED_ARIA_ATTRS:
                continue

            required_attrs = REQUIRED_ARIA_ATTRS[role]
            missing = [
                attr for attr in required_attrs
                if not elem.has_attr(attr)
            ]
            if missing:
                self._found_issue = True
                self.add_issue(
                    "custom-widget-missing-aria-attrs",
                    "4.1.2",
                    "major",
                    element=elem,
                    description=(
                        f"Element with role='{role}' is missing required "
                        f"ARIA attributes: {', '.join(missing)}"
                    ),
                )

        # Check that elements with role requiring a name have one
        for elem in elements:
            role = elem.get("role", "").strip().lower()
            if role not in ROLES_REQUIRING_NAME:
                continue

            # Native elements already provide names via content/label
            if elem.name in ("a", "button", "input", "select", "textarea"):
                continue

            if not self._has_accessible_name(elem):
                self._found_issue = True
                self.add_issue(
                    "custom-widget-missing-name",
                    "4.1.2",
                    "major",
                    element=elem,
                    description=(
                        f"Element with role='{role}' has no accessible name "
                        f"(add aria-label, aria-labelledby, or text content)"
                    ),
                )

    def _check_interactive_elements_name(self) -> None:
        """Check that native interactive elements have accessible names."""
        # Buttons without accessible names
        for btn in self.soup.find_all("button"):
            if not self._has_accessible_name(btn):
                self._found_issue = True
                self.add_issue(
                    "button-missing-name",
                    "4.1.2",
                    "critical",
                    element=btn,
                    description="Button has no accessible name",
                )

        # Input type=image without alt
        for img_input in self.soup.find_all(
            "input", attrs={"type": "image"}
        ):
            if not img_input.get("alt"):
                self._found_issue = True
                self.add_issue(
                    "input-image-missing-alt",
                    "4.1.2",
                    "critical",
                    element=img_input,
                    description="Image input missing alt attribute",
                )

        # iframes without title
        for iframe in self.soup.find_all("iframe"):
            if not iframe.get("title") and not iframe.get("aria-label"):
                self._found_issue = True
                self.add_issue(
                    "iframe-missing-title",
                    "4.1.2",
                    "major",
                    element=iframe,
                    description="iframe element missing title attribute",
                )

    def _has_accessible_name(self, elem) -> bool:
        """Check whether an element has an accessible name."""
        if elem.get("aria-label", "").strip():
            return True
        if elem.get("aria-labelledby", "").strip():
            return True
        if elem.get("title", "").strip():
            return True
        if self.get_element_text(elem):
            return True
        # Check for image with alt inside the element
        img = elem.find("img")
        if img and img.get("alt", "").strip():
            return True
        return False
