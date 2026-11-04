# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTML parsing and validation checks."""

import re
from collections import Counter

from audit.base_check import AccessibilityCheck


class HTMLParsingCheck(AccessibilityCheck):
    """Check for valid HTML parsing (WCAG 4.1.1)."""

    BLOCK_ELEMENTS = frozenset([
        "address", "article", "aside", "blockquote", "details", "dialog",
        "dd", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer",
        "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr",
        "li", "main", "nav", "ol", "p", "pre", "section", "table", "ul",
    ])

    def __init__(self, soup, add_issue_callback, raw_html=""):
        super().__init__(soup, add_issue_callback)
        self.raw_html = raw_html

    def check(self) -> None:
        self._check_duplicate_ids()
        self._check_nesting()

    def _check_duplicate_ids(self) -> None:
        elements_with_id = self.soup.find_all(attrs={"id": True})
        id_counts = Counter(el.get("id") for el in elements_with_id)
        found_issue = False

        for id_value, count in id_counts.items():
            if count > 1:
                found_issue = True
                elements = self.soup.find_all(attrs={"id": id_value})
                for element in elements[1:]:
                    self.add_issue(
                        "duplicate-id",
                        "4.1.1",
                        "major",
                        element=element,
                        description=f"Duplicate id='{id_value}' found ({count} occurrences). IDs must be unique.",
                        status="needs_remediation",
                    )

        if not found_issue:
            self.add_issue(
                "compliant-unique-ids",
                "4.1.1",
                "minor",
                description="All element IDs are unique",
                status="compliant",
            )

    def _check_nesting(self) -> None:
        found_issue = False

        # Check <p> containing block elements
        paragraphs = self.soup.find_all("p")
        for p in paragraphs:
            for child in p.descendants:
                if hasattr(child, "name") and child.name in self.BLOCK_ELEMENTS:
                    found_issue = True
                    self.add_issue(
                        "invalid-nesting",
                        "4.1.1",
                        "major",
                        element=p,
                        description=f"<p> element contains block-level <{child.name}> which is invalid nesting",
                        status="needs_remediation",
                    )
                    break

        # Check <a> inside <a>
        links = self.soup.find_all("a")
        for link in links:
            nested_links = link.find_all("a")
            if nested_links:
                found_issue = True
                self.add_issue(
                    "invalid-nesting",
                    "4.1.1",
                    "major",
                    element=link,
                    description="<a> element contains nested <a> which is invalid nesting",
                    status="needs_remediation",
                )

        # Check <button> inside <button>
        buttons = self.soup.find_all("button")
        for button in buttons:
            nested_buttons = button.find_all("button")
            if nested_buttons:
                found_issue = True
                self.add_issue(
                    "invalid-nesting",
                    "4.1.1",
                    "major",
                    element=button,
                    description="<button> element contains nested <button> which is invalid nesting",
                    status="needs_remediation",
                )

        if not found_issue:
            self.add_issue(
                "compliant-html-nesting",
                "4.1.1",
                "minor",
                description="No invalid HTML nesting detected",
                status="compliant",
            )
