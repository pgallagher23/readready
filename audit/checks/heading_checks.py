# SPDX-License-Identifier: AGPL-3.0-or-later
# Portions derived from Content Accessibility Utility on AWS
# (https://github.com/awslabs/content-accessibility-utility-on-aws),
# Copyright 2025 Amazon.com, Inc. or its affiliates, Apache-2.0.
# Modified by the ReadReady project; see NOTICE.
"""Heading structure accessibility checks."""

import re

from audit.base_check import AccessibilityCheck


def _looks_like_filename(text: str) -> bool:
    """A no-whitespace string containing an underscore reads as a filename slug."""
    if not text or re.search(r"\s", text):
        return False
    return "_" in text


class HeadingHierarchyCheck(AccessibilityCheck):
    """Check for proper heading hierarchy (WCAG 1.3.1)."""

    def check(self) -> None:
        headings = []
        for i in range(1, 7):
            for h in self.soup.find_all(f"h{i}"):
                headings.append((i, h))

        if not headings:
            self.add_issue(
                "no-headings",
                "1.3.1",
                "major",
                element=self.soup.find("body"),
                description="Document has no heading elements",
                status="needs_remediation",
            )
            return

        has_h1 = any(level == 1 for level, _ in headings)
        if not has_h1:
            self.add_issue(
                "no-h1",
                "1.3.1",
                "info",
                element=self.soup.find("body"),
                description=(
                    "Document has no main heading (h1). Best practice, not a "
                    "WCAG failure: add one if any text names the document."
                ),
                status="informational",
            )

        prev_level = 0
        has_skipped_levels = False

        for level, heading in headings:
            if prev_level > 0 and level > prev_level + 1:
                has_skipped_levels = True
                self.add_issue(
                    "skipped-heading-level",
                    "1.3.1",
                    "major",
                    element=heading,
                    description=f"Heading level skipped from H{prev_level} to H{level}",
                    status="needs_remediation",
                )
            prev_level = level

        if has_h1 and not has_skipped_levels:
            self.add_issue(
                "compliant-heading-hierarchy",
                "1.3.1",
                "major",
                element=headings[0][1],
                description="Document has proper heading hierarchy",
                status="compliant",
            )


class HeadingContentCheck(AccessibilityCheck):
    """Check for proper heading content (WCAG 2.4.6)."""

    def check(self) -> None:
        headings = []
        for i in range(1, 7):
            for h in self.soup.find_all(f"h{i}"):
                headings.append(h)

        if not headings:
            return

        has_issues = False

        for heading in headings:
            text = self.get_element_text(heading)

            if not text:
                has_issues = True
                self.add_issue(
                    "empty-heading",
                    "2.4.6",
                    "major",
                    element=heading,
                    description="Heading has no text content",
                    status="needs_remediation",
                )
            elif text.lower() in ["heading", "title", "subtitle", "header"]:
                has_issues = True
                self.add_issue(
                    "generic-heading",
                    "2.4.6",
                    "minor",
                    element=heading,
                    description=f"Heading has generic text: '{text}'",
                    status="needs_remediation",
                )
            elif _looks_like_filename(text):
                has_issues = True
                self.add_issue(
                    "filename-like-heading",
                    "2.4.6",
                    "major",
                    element=heading,
                    description=f"Heading looks like a filename: '{text}'",
                    status="needs_remediation",
                )

        if not has_issues:
            self.add_issue(
                "compliant-heading-content",
                "2.4.6",
                "major",
                element=headings[0],
                description="Document has proper heading content",
                status="compliant",
            )


class DocumentTitleCheck(AccessibilityCheck):
    """Check for document title (WCAG 2.4.2)."""

    def check(self) -> None:
        title = self.soup.find("title")

        if not title:
            self.add_issue(
                "missing-title",
                "2.4.2",
                "major",
                element=self.soup.find("head"),
                description="Document missing title element",
                status="needs_remediation",
            )
            return

        title_text = self.get_element_text(title)

        if not title_text:
            self.add_issue(
                "empty-title",
                "2.4.2",
                "major",
                element=title,
                description="Document has empty title element",
                status="needs_remediation",
            )
        elif title_text.lower() in [
            "untitled",
            "document",
            "page",
            "new page",
            "title",
        ]:
            self.add_issue(
                "generic-title",
                "2.4.2",
                "minor",
                element=title,
                description=f"Document has generic title: '{title_text}'",
                status="needs_remediation",
            )
        elif _looks_like_filename(title_text):
            self.add_issue(
                "filename-like-title",
                "2.4.2",
                "major",
                element=title,
                description=f"Title looks like a filename, not a description: '{title_text}'",
                status="needs_remediation",
            )
        else:
            self.add_issue(
                "compliant-document-title",
                "2.4.2",
                "major",
                element=title,
                description="Document has proper title",
                status="compliant",
            )


_SECTION_ANCHOR = re.compile(
    r"^(Level|Part|Section|Chapter|Unit|Module|Phase|Step)\s+([IVXLC]+|\d+)\b",
    re.IGNORECASE,
)


class FlatHeadingNestingCheck(AccessibilityCheck):
    """Advisory: sub-sections sharing the section-anchor heading level (1.3.1).

    Non-gating (informational). Flat same-level headings are not a documented
    WCAG failure, but when >=2 section anchors (Level/Part/...) share a level
    with non-anchor headings, the document hierarchy is likely under-described.
    The remediator re-nests these when it can; this surfaces the rest.
    """

    def check(self) -> None:
        headings = []
        for i in range(1, 7):
            for h in self.soup.find_all(f"h{i}"):
                headings.append((i, h))
        if not headings:
            return

        anchor_levels = [
            lvl for lvl, h in headings if _SECTION_ANCHOR.match(h.get_text(strip=True))
        ]
        if len(anchor_levels) < 2:
            return
        section_level = min(anchor_levels)

        # Any non-anchor heading at the section level => flat nesting.
        flat = any(
            lvl == section_level and not _SECTION_ANCHOR.match(h.get_text(strip=True))
            for lvl, h in headings
        )
        if flat:
            self.add_issue(
                "flat-heading-nesting",
                "1.3.1",
                "info",
                element=headings[0][1],
                description=(
                    "Sub-section headings share the section-anchor level; "
                    "consider nesting them one level deeper"
                ),
                status="informational",
            )
