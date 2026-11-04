# SPDX-License-Identifier: AGPL-3.0-or-later
"""Text resize accessibility checks."""

import re

from audit.base_check import AccessibilityCheck


class ResizeTextCheck(AccessibilityCheck):
    """Check that text can be resized to 200% without loss (WCAG 1.4.4)."""

    TEXT_ELEMENTS = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "span", "a", "li", "td", "th", "div", "label"]

    def check(self) -> None:
        self._check_fixed_font_sizes()
        self._check_overflow_hidden()

    def _check_fixed_font_sizes(self) -> None:
        found_issue = False
        elements = self.soup.find_all(self.TEXT_ELEMENTS, attrs={"style": True})

        for element in elements:
            style = element.get("style", "")
            match = re.search(r"font-size\s*:\s*(\d+(?:\.\d+)?)\s*(px|pt)\b", style)
            if match:
                size = float(match.group(1))
                unit = match.group(2)
                found_issue = True
                self.add_issue(
                    "fixed-font-size",
                    "1.4.4",
                    "minor",
                    element=element,
                    description=f"Font size uses absolute unit ({size}{unit}) which may prevent text resizing. Use relative units (em, rem, %) instead.",
                    status="needs_remediation",
                )

        style_blocks = self.soup.find_all("style")
        for style_block in style_blocks:
            content = style_block.string or ""
            matches = re.findall(r"font-size\s*:\s*(\d+(?:\.\d+)?)\s*(px|pt)\b", content)
            for size, unit in matches:
                found_issue = True
                self.add_issue(
                    "fixed-font-size",
                    "1.4.4",
                    "minor",
                    element=style_block,
                    description=f"Stylesheet uses absolute font-size ({size}{unit}). Use relative units (em, rem, %) instead.",
                    status="needs_remediation",
                )

        if not found_issue:
            self.add_issue(
                "compliant-resize-text",
                "1.4.4",
                "minor",
                description="No fixed font sizes detected that would prevent text resizing",
                status="compliant",
            )

    def _check_overflow_hidden(self) -> None:
        elements = self.soup.find_all(attrs={"style": True})

        for element in elements:
            style = element.get("style", "")
            has_overflow_hidden = re.search(r"overflow\s*:\s*hidden", style)
            has_max_height = re.search(r"max-height\s*:\s*\d+", style)
            has_fixed_height = re.search(r"(?<!max-)height\s*:\s*\d+\s*(px|pt)\b", style)

            text = self.get_element_text(element)
            if not text:
                continue

            if has_overflow_hidden and (has_max_height or has_fixed_height):
                self.add_issue(
                    "text-overflow-hidden",
                    "1.4.4",
                    "major",
                    element=element,
                    description="Text container has overflow:hidden with fixed/max height which may clip content when text is resized",
                    status="needs_remediation",
                )
