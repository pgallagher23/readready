# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading order and meaningful sequence checks."""

import re

from audit.base_check import AccessibilityCheck


class ReadingOrderCheck(AccessibilityCheck):
    """Check for meaningful reading sequence (WCAG 1.3.2)."""

    def check(self) -> None:
        self._check_tabindex()
        self._check_css_reordering()
        self._check_absolute_positioning()

    def _check_tabindex(self) -> None:
        elements = self.soup.find_all(attrs={"tabindex": True})
        found_issue = False
        for element in elements:
            try:
                tabindex = int(element.get("tabindex", 0))
            except (ValueError, TypeError):
                continue
            if tabindex > 0:
                found_issue = True
                self.add_issue(
                    "reading-order-tabindex",
                    "1.3.2",
                    "major",
                    element=element,
                    description=f"Element has tabindex={tabindex} which overrides natural reading order",
                    status="needs_remediation",
                )
        if not found_issue:
            self.add_issue(
                "compliant-reading-order-tabindex",
                "1.3.2",
                "minor",
                description="No positive tabindex values found disrupting reading order",
                status="compliant",
            )

    def _check_css_reordering(self) -> None:
        reorder_patterns = [
            (r"(?:^|[\s;])order\s*:\s*(-?\d+)", "CSS order property"),
            (r"flex-direction\s*:\s*(row-reverse|column-reverse)", "CSS flex-direction reverse"),
            (r"direction\s*:\s*rtl", "CSS direction override"),
        ]

        elements_with_style = self.soup.find_all(attrs={"style": True})
        for element in elements_with_style:
            style = element.get("style", "")
            for pattern, label in reorder_patterns:
                if re.search(pattern, style):
                    self.add_issue(
                        "reading-order-css-reorder",
                        "1.3.2",
                        "minor",
                        element=element,
                        description=f"{label} may cause visual order to differ from DOM order",
                        status="needs_remediation",
                    )

    def _check_absolute_positioning(self) -> None:
        elements_with_style = self.soup.find_all(attrs={"style": True})
        for element in elements_with_style:
            style = element.get("style", "")
            if re.search(r"position\s*:\s*(absolute|fixed)", style):
                text = self.get_element_text(element)
                if text and len(text) > 20:
                    self.add_issue(
                        "reading-order-absolute-position",
                        "1.3.2",
                        "minor",
                        element=element,
                        description="Absolutely positioned element with significant text content may disrupt reading order",
                        status="needs_remediation",
                    )
