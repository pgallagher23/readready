# SPDX-License-Identifier: AGPL-3.0-or-later
"""Images of text accessibility checks."""

import re

from audit.base_check import AccessibilityCheck


class ImagesOfTextCheck(AccessibilityCheck):
    """Check for images of text that should be real text (WCAG 1.4.5)."""

    TEXT_FILENAME_PATTERNS = [
        r"text", r"title", r"heading", r"header_text", r"banner_text",
        r"button_text", r"label", r"caption_text", r"logo_text",
        r"wordmark", r"tagline", r"slogan",
    ]

    def check(self) -> None:
        images = self.soup.find_all("img")
        if not images:
            return

        found_issue = False
        for img in images:
            src = img.get("src", "")
            alt = img.get("alt", "")

            if self._filename_suggests_text(src):
                found_issue = True
                self.add_issue(
                    "possible-image-of-text",
                    "1.4.5",
                    "minor",
                    element=img,
                    description=f"Image filename suggests it may contain text: '{src.split('/')[-1]}'. Consider using real text instead.",
                    status="needs_remediation",
                )
                continue

            if alt and len(alt) > 250:
                found_issue = True
                self.add_issue(
                    "possible-image-of-text",
                    "1.4.5",
                    "minor",
                    element=img,
                    description=f"Image has very long alt text ({len(alt)} chars) which may indicate it contains significant text content that should be real text",
                    status="needs_remediation",
                )

        if not found_issue:
            self.add_issue(
                "compliant-no-images-of-text",
                "1.4.5",
                "minor",
                description="No obvious images of text detected",
                status="compliant",
            )

    def _filename_suggests_text(self, src: str) -> bool:
        if not src:
            return False
        filename = src.split("/")[-1].lower()
        for pattern in self.TEXT_FILENAME_PATTERNS:
            if re.search(pattern, filename):
                return True
        return False
