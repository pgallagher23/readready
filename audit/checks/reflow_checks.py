# SPDX-License-Identifier: AGPL-3.0-or-later
"""Content reflow accessibility checks."""

import re

from audit.base_check import AccessibilityCheck


class ReflowCheck(AccessibilityCheck):
    """Check content reflow at 320px width (WCAG 1.4.10)."""

    def check(self) -> None:
        self._check_fixed_widths()
        self._check_horizontal_scroll()
        self._check_nowrap()

    def _check_fixed_widths(self) -> None:
        found_issue = False
        elements = self.soup.find_all(attrs={"style": True})

        for element in elements:
            style = element.get("style", "")
            match = re.search(r"(?:min-)?width\s*:\s*(\d+)\s*px", style)
            if match:
                width = int(match.group(1))
                if width > 320:
                    found_issue = True
                    self.add_issue(
                        "fixed-width-prevents-reflow",
                        "1.4.10",
                        "major",
                        element=element,
                        description=f"Element has fixed width of {width}px which prevents content reflow at 320px viewport",
                        status="needs_remediation",
                    )

        if not found_issue:
            self.add_issue(
                "compliant-reflow-width",
                "1.4.10",
                "minor",
                description="No fixed widths >320px detected that would prevent reflow",
                status="compliant",
            )

    def _check_horizontal_scroll(self) -> None:
        elements = self.soup.find_all(attrs={"style": True})

        for element in elements:
            style = element.get("style", "")
            if re.search(r"overflow-x\s*:\s*(scroll|auto)", style):
                text = self.get_element_text(element)
                if text:
                    self.add_issue(
                        "horizontal-scroll-required",
                        "1.4.10",
                        "minor",
                        element=element,
                        description="Element with text content requires horizontal scrolling",
                        status="needs_remediation",
                    )

    def _check_nowrap(self) -> None:
        text_elements = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "span", "div", "li", "td", "th"]
        elements = self.soup.find_all(text_elements, attrs={"style": True})

        for element in elements:
            style = element.get("style", "")
            if re.search(r"white-space\s*:\s*nowrap", style):
                text = self.get_element_text(element)
                if text and len(text) > 50:
                    self.add_issue(
                        "nowrap-text",
                        "1.4.10",
                        "minor",
                        element=element,
                        description="Long text content has white-space:nowrap which prevents reflow",
                        status="needs_remediation",
                    )
