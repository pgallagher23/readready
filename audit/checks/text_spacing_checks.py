# SPDX-License-Identifier: AGPL-3.0-or-later
"""Text spacing accessibility checks."""

import re

from audit.base_check import AccessibilityCheck


class TextSpacingCheck(AccessibilityCheck):
    """Check that text spacing can be adjusted without loss (WCAG 1.4.12)."""

    TEXT_ELEMENTS = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "span", "div", "li", "td", "th", "a", "label"]

    def check(self) -> None:
        self._check_fixed_line_height()
        self._check_overflow_with_fixed_height()
        self._check_important_spacing()

    def _check_fixed_line_height(self) -> None:
        found_issue = False
        elements = self.soup.find_all(self.TEXT_ELEMENTS, attrs={"style": True})

        for element in elements:
            style = element.get("style", "")
            match = re.search(r"line-height\s*:\s*(\d+(?:\.\d+)?)\s*(px|pt)\b", style)
            if match:
                text = self.get_element_text(element)
                if text:
                    found_issue = True
                    size = match.group(1)
                    unit = match.group(2)
                    self.add_issue(
                        "fixed-line-height",
                        "1.4.12",
                        "minor",
                        element=element,
                        description=f"Line height uses absolute unit ({size}{unit}) which may prevent text spacing adjustment. Use unitless values instead.",
                        status="needs_remediation",
                    )

        if not found_issue:
            self.add_issue(
                "compliant-text-spacing",
                "1.4.12",
                "minor",
                description="No fixed line heights detected that would prevent text spacing adjustment",
                status="compliant",
            )

    def _check_overflow_with_fixed_height(self) -> None:
        elements = self.soup.find_all(attrs={"style": True})

        for element in elements:
            style = element.get("style", "")
            has_overflow_hidden = re.search(r"overflow\s*:\s*hidden", style)
            has_fixed_height = re.search(r"(?<!max-)height\s*:\s*\d+\s*(px|pt|em|rem)\b", style)

            if has_overflow_hidden and has_fixed_height:
                text = self.get_element_text(element)
                if text and len(text) > 10:
                    self.add_issue(
                        "text-spacing-overflow-risk",
                        "1.4.12",
                        "major",
                        element=element,
                        description="Element has overflow:hidden with fixed height which may clip text when spacing is increased",
                        status="needs_remediation",
                    )

    def _check_important_spacing(self) -> None:
        style_blocks = self.soup.find_all("style")
        spacing_props = ["line-height", "letter-spacing", "word-spacing"]

        for style_block in style_blocks:
            content = style_block.string or ""
            for prop in spacing_props:
                if re.search(rf"{prop}\s*:[^;]*!important", content):
                    self.add_issue(
                        "text-spacing-important-override",
                        "1.4.12",
                        "minor",
                        element=style_block,
                        description=f"Stylesheet uses !important on {prop} which prevents user text spacing adjustment",
                        status="needs_remediation",
                    )
