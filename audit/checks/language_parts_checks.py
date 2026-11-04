# SPDX-License-Identifier: AGPL-3.0-or-later
"""Language of parts accessibility checks."""

import re

from audit.base_check import AccessibilityCheck


class LanguageOfPartsCheck(AccessibilityCheck):
    """Check language markup on content in different languages (WCAG 3.1.2)."""

    VALID_LANG_PATTERN = re.compile(r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]+)*$")

    def check(self) -> None:
        self._check_existing_lang_attributes()
        self._check_quote_elements()
        self._check_xml_lang_without_lang()

    def _check_existing_lang_attributes(self) -> None:
        html_tag = self.soup.find("html")
        elements_with_lang = self.soup.find_all(attrs={"lang": True})
        found_issue = False

        for element in elements_with_lang:
            if element == html_tag:
                continue
            lang = element.get("lang", "").strip()
            if not lang:
                found_issue = True
                self.add_issue(
                    "invalid-lang-attribute",
                    "3.1.2",
                    "major",
                    element=element,
                    description="Element has empty lang attribute",
                    status="needs_remediation",
                )
            elif not self.VALID_LANG_PATTERN.match(lang):
                found_issue = True
                self.add_issue(
                    "invalid-lang-attribute",
                    "3.1.2",
                    "major",
                    element=element,
                    description=f"Element has invalid lang attribute value: '{lang}'",
                    status="needs_remediation",
                )

        if not found_issue and elements_with_lang:
            self.add_issue(
                "compliant-language-parts",
                "3.1.2",
                "minor",
                description="All lang attributes on child elements are valid",
                status="compliant",
            )

    def _check_quote_elements(self) -> None:
        quote_elements = self.soup.find_all(["blockquote", "q", "cite"])

        for element in quote_elements:
            if not element.get("lang"):
                text = self.get_element_text(element)
                if text and len(text) > 20:
                    self.add_issue(
                        "missing-lang-on-foreign-content",
                        "3.1.2",
                        "minor",
                        element=element,
                        description=f"<{element.name}> element without lang attribute - if content is in a different language, add a lang attribute",
                        status="needs_remediation",
                    )

    def _check_xml_lang_without_lang(self) -> None:
        elements = self.soup.find_all(attrs={"xml:lang": True})

        for element in elements:
            if not element.get("lang"):
                xml_lang = element.get("xml:lang", "")
                self.add_issue(
                    "xml-lang-without-lang",
                    "3.1.2",
                    "minor",
                    element=element,
                    description=f"Element has xml:lang='{xml_lang}' but no lang attribute. Add a matching lang attribute.",
                    status="needs_remediation",
                )
