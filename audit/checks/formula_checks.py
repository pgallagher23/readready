# SPDX-License-Identifier: AGPL-3.0-or-later
"""Formula accessibility checks (WCAG 1.1.1 — Non-text Content)."""

import re

from audit.base_check import AccessibilityCheck


class FormulaAltTextCheck(AccessibilityCheck):
    """Check that formula elements have meaningful alternative text.

    Formulas are non-text content under WCAG 1.1.1. They require either:
    - An aria-label attribute with a description
    - A role="math" attribute with aria-label
    - MathML content with an alttext attribute
    - Or at minimum, non-generic alt text in the PDF structure

    This check audits the HTML representation. The iText tagger
    separately ensures /Alt is set on Formula structure elements.
    """

    # Generic alt text patterns that indicate the formula was not described
    GENERIC_PATTERNS = [
        re.compile(r"^mathematical formula", re.IGNORECASE),
        re.compile(r"^formula on page", re.IGNORECASE),
        re.compile(r"^equation$", re.IGNORECASE),
        re.compile(r"^formula$", re.IGNORECASE),
        re.compile(r"^math$", re.IGNORECASE),
    ]

    def check(self) -> None:
        # Check elements with role="math"
        math_role_elems = self.soup.find_all(attrs={"role": "math"})
        for elem in math_role_elems:
            aria_label = elem.get("aria-label", "").strip()
            math_child = elem.find("math")

            if math_child:
                # Has MathML — check for alttext
                alttext = math_child.get("alttext", "").strip()
                if alttext and not self._is_generic(alttext):
                    self.add_issue(
                        "compliant-formula-alt-text",
                        "1.1.1",
                        "critical",
                        element=elem,
                        description="Formula has MathML with descriptive alttext",
                        status="compliant",
                    )
                elif aria_label and not self._is_generic(aria_label):
                    self.add_issue(
                        "compliant-formula-alt-text",
                        "1.1.1",
                        "critical",
                        element=elem,
                        description="Formula has aria-label description",
                        status="compliant",
                    )
                else:
                    self.add_issue(
                        "formula-missing-description",
                        "1.1.1",
                        "major",
                        element=elem,
                        description="Formula has MathML but missing text description",
                        status="needs_remediation",
                    )
            elif aria_label:
                if self._is_generic(aria_label):
                    self.add_issue(
                        "formula-generic-alt-text",
                        "1.1.1",
                        "major",
                        element=elem,
                        description=f"Formula has generic description: '{aria_label[:60]}'",
                        status="needs_remediation",
                    )
                else:
                    self.add_issue(
                        "compliant-formula-alt-text",
                        "1.1.1",
                        "critical",
                        element=elem,
                        description="Formula has descriptive aria-label",
                        status="compliant",
                    )
            else:
                self.add_issue(
                    "formula-missing-alt-text",
                    "1.1.1",
                    "critical",
                    element=elem,
                    description="Formula element missing description (no aria-label or MathML alttext)",
                    status="needs_remediation",
                )

        # Check <math> elements not inside role="math" containers
        standalone_math = self.soup.find_all("math")
        for math_elem in standalone_math:
            parent = math_elem.parent
            if parent and parent.get("role") == "math":
                continue  # Already checked above

            alttext = math_elem.get("alttext", "").strip()
            if alttext and not self._is_generic(alttext):
                self.add_issue(
                    "compliant-formula-alt-text",
                    "1.1.1",
                    "critical",
                    element=math_elem,
                    description="Standalone MathML element has descriptive alttext",
                    status="compliant",
                )
            else:
                self.add_issue(
                    "formula-missing-description",
                    "1.1.1",
                    "major",
                    element=math_elem,
                    description="MathML element missing alttext attribute",
                    status="needs_remediation",
                )

    def _is_generic(self, text: str) -> bool:
        """Check if text matches a known generic formula placeholder."""
        for pattern in self.GENERIC_PATTERNS:
            if pattern.match(text):
                return True
        return False
