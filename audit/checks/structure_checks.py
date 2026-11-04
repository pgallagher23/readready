# SPDX-License-Identifier: AGPL-3.0-or-later
# Portions derived from Content Accessibility Utility on AWS
# (https://github.com/awslabs/content-accessibility-utility-on-aws),
# Copyright 2025 Amazon.com, Inc. or its affiliates, Apache-2.0.
# Modified by the ReadReady project; see NOTICE.
"""Document structure and landmark accessibility checks."""

from audit.base_check import AccessibilityCheck


class DocumentLanguageCheck(AccessibilityCheck):
    """Check for document language specification (WCAG 3.1.1)."""

    def check(self) -> None:
        html_tag = self.soup.find("html")

        if not html_tag:
            return

        if not html_tag.has_attr("lang"):
            self.add_issue(
                "missing-document-language",
                "3.1.1",
                "critical",
                element=html_tag,
                description="Document missing language attribute on HTML element",
                status="needs_remediation",
            )
            return

        lang = html_tag["lang"].strip()
        if not lang or len(lang) < 2:
            self.add_issue(
                "invalid-document-language",
                "3.1.1",
                "major",
                element=html_tag,
                description=f"Document has invalid language code: '{lang}'",
                status="needs_remediation",
            )
        else:
            self.add_issue(
                "compliant-document-language",
                "3.1.1",
                "critical",
                element=html_tag,
                description=f"Document has valid language attribute: '{lang}'",
                status="compliant",
            )


class MainLandmarkCheck(AccessibilityCheck):
    """Check for main landmark (WCAG 1.3.1, 2.4.1)."""

    def check(self) -> None:
        main_element = self.soup.find("main")
        main_role = self.soup.find(attrs={"role": "main"})

        if not main_element and not main_role:
            self.add_issue(
                "missing-main-landmark",
                "1.3.1",
                "major",
                element=self.soup.find("body"),
                description="Document missing main landmark (main element or role='main')",
                status="needs_remediation",
            )
        else:
            element = main_element if main_element else main_role
            self.add_issue(
                "compliant-main-landmark",
                "1.3.1",
                "major",
                element=element,
                description="Document has proper main landmark",
                status="compliant",
            )


class SkipLinkCheck(AccessibilityCheck):
    """Check for skip navigation link (WCAG 2.4.1)."""

    def check(self) -> None:
        links = self.soup.find_all("a", limit=5)
        has_skip_link = False
        skip_link = None

        for link in links:
            href = link.get("href", "")
            text = self.get_element_text(link).lower()

            if href.startswith("#") and (
                "skip" in text or "jump" in text or "content" in text or "main" in text
            ):
                has_skip_link = True
                skip_link = link
                break

        if not has_skip_link:
            self.add_issue(
                "missing-skip-link",
                "2.4.1",
                "major",
                element=self.soup.find("body"),
                description="Document missing skip navigation link",
                status="needs_remediation",
            )
        else:
            self.add_issue(
                "compliant-skip-link",
                "2.4.1",
                "major",
                element=skip_link,
                description="Document has proper skip navigation link",
                status="compliant",
            )


class LandmarksCheck(AccessibilityCheck):
    """Check for proper landmarks (WCAG 1.3.1, 2.4.1)."""

    def check(self) -> None:
        # Navigation
        nav_element = self.soup.find("nav")
        nav_role = self.soup.find(attrs={"role": "navigation"})
        if not nav_element and not nav_role:
            self.add_issue(
                "missing-navigation-landmark",
                "1.3.1",
                "minor",
                element=self.soup.find("body"),
                description="Document missing navigation landmark",
                status="needs_remediation",
            )
        else:
            self.add_issue(
                "compliant-navigation-landmark",
                "1.3.1",
                "minor",
                element=nav_element or nav_role,
                description="Document has proper navigation landmark",
                status="compliant",
            )

        # Header
        header_element = self.soup.find("header")
        banner_role = self.soup.find(attrs={"role": "banner"})
        if not header_element and not banner_role:
            self.add_issue(
                "missing-header-landmark",
                "1.3.1",
                "minor",
                element=self.soup.find("body"),
                description="Document missing header landmark",
                status="needs_remediation",
            )
        else:
            self.add_issue(
                "compliant-header-landmark",
                "1.3.1",
                "minor",
                element=header_element or banner_role,
                description="Document has proper header landmark",
                status="compliant",
            )

        # Footer
        footer_element = self.soup.find("footer")
        contentinfo_role = self.soup.find(attrs={"role": "contentinfo"})
        if not footer_element and not contentinfo_role:
            self.add_issue(
                "missing-footer-landmark",
                "1.3.1",
                "minor",
                element=self.soup.find("body"),
                description="Document missing footer landmark",
                status="needs_remediation",
            )
        else:
            self.add_issue(
                "compliant-footer-landmark",
                "1.3.1",
                "minor",
                element=footer_element or contentinfo_role,
                description="Document has proper footer landmark",
                status="compliant",
            )
