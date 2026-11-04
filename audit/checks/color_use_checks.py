# SPDX-License-Identifier: AGPL-3.0-or-later
"""Color use accessibility checks."""

import re

from audit.base_check import AccessibilityCheck


class ColorUseCheck(AccessibilityCheck):
    """Check that color is not the sole means of conveying information (WCAG 1.4.1)."""

    def check(self) -> None:
        self._check_link_distinction()
        self._check_color_only_indicators()

    def _check_link_distinction(self) -> None:
        links = self.soup.find_all("a")
        found_issue = False

        for link in links:
            href = link.get("href", "")
            if not href or href.startswith("#"):
                continue

            parent = link.parent
            if not parent or parent.name in ("nav", "header", "footer", "li"):
                continue

            if parent.name in ("p", "span", "div", "td", "th"):
                style = link.get("style", "")
                has_underline = "text-decoration" in style and "none" not in style
                has_border = "border" in style
                has_weight = "font-weight" in style

                if not has_underline and not has_border and not has_weight:
                    if "text-decoration" in style and "none" in style:
                        found_issue = True
                        self.add_issue(
                            "color-only-link-distinction",
                            "1.4.1",
                            "major",
                            element=link,
                            description="Link within text may be distinguished from surrounding text only by color (text-decoration: none)",
                            status="needs_remediation",
                        )

        if not found_issue:
            self.add_issue(
                "compliant-color-use-links",
                "1.4.1",
                "minor",
                description="Links appear to have non-color distinction from surrounding text",
                status="compliant",
            )

    def _check_color_only_indicators(self) -> None:
        indicator_classes = ["error", "success", "warning", "danger", "alert", "invalid", "valid"]

        for element in self.soup.find_all(attrs={"class": True}):
            classes = element.get("class", [])
            if isinstance(classes, str):
                classes = classes.split()

            matching = [c for c in classes if any(ind in c.lower() for ind in indicator_classes)]
            if not matching:
                continue

            text = self.get_element_text(element)
            has_icon = element.find(["svg", "i", "img"])
            has_prefix = any(
                text.lower().startswith(prefix)
                for prefix in ("error:", "warning:", "success:", "note:")
            )
            has_aria = element.get("role") in ("alert", "status") or element.get("aria-live")

            if not has_icon and not has_prefix and not has_aria:
                self.add_issue(
                    "color-only-status",
                    "1.4.1",
                    "minor",
                    element=element,
                    description=f"Element with class '{' '.join(matching)}' may convey status through color alone without non-color indicator",
                    status="needs_remediation",
                )
