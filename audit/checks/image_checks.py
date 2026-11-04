# SPDX-License-Identifier: AGPL-3.0-or-later
# Portions derived from Content Accessibility Utility on AWS
# (https://github.com/awslabs/content-accessibility-utility-on-aws),
# Copyright 2025 Amazon.com, Inc. or its affiliates, Apache-2.0.
# Modified by the ReadReady project; see NOTICE.
"""Image accessibility checks."""

from audit.base_check import AccessibilityCheck
from converter.alt_harvest import is_generic_alt


class AltTextCheck(AccessibilityCheck):
    """Check for proper alt text on images (WCAG 1.1.1)."""

    def check(self) -> None:
        images = self.soup.find_all("img")

        if not images:
            return

        for img in images:
            if not img.has_attr("alt"):
                self.add_issue(
                    "missing-alt-text",
                    "1.1.1",
                    "critical",
                    element=img,
                    description="Image missing alt text",
                    status="needs_remediation",
                )
            else:
                alt_text = img["alt"].strip()
                if not alt_text:
                    if (
                        img.get("role") == "presentation"
                        or img.get("aria-hidden") == "true"
                    ):
                        self.add_issue(
                            "compliant-decorative-image",
                            "1.1.1",
                            "minor",
                            element=img,
                            description="Decorative image properly marked",
                            status="compliant",
                        )
                    elif self._has_figcaption(img):
                        self.add_issue(
                            "compliant-alt-text",
                            "1.1.1",
                            "minor",
                            element=img,
                            description="Image alt text provided by parent figure caption",
                            status="compliant",
                        )
                    else:
                        self.add_issue(
                            "empty-alt-text",
                            "1.1.1",
                            "major",
                            element=img,
                            description="Image has empty alt text but is not marked as decorative",
                            status="needs_remediation",
                        )
                elif is_generic_alt(alt_text):
                    self.add_issue(
                        "generic-alt-text",
                        "1.1.1",
                        "major",
                        element=img,
                        description=f"Image has generic alt text: '{alt_text}'",
                        status="needs_remediation",
                    )
                else:
                    self.add_issue(
                        "compliant-alt-text",
                        "1.1.1",
                        "critical",
                        element=img,
                        description="Image has proper alt text",
                        status="compliant",
                    )

    @staticmethod
    def _has_figcaption(img) -> bool:
        """Check if img is inside a <figure> with a non-empty <figcaption>."""
        figure = img.find_parent("figure")
        if not figure:
            return False
        figcaption = figure.find("figcaption")
        return bool(figcaption and figcaption.get_text(strip=True))


class FigureStructureCheck(AccessibilityCheck):
    """Check for proper figure structure (WCAG 1.1.1)."""

    def check(self) -> None:
        figures = self.soup.find_all("figure")

        if not figures:
            return

        for figure in figures:
            caption = figure.find("figcaption")

            if not caption:
                self.add_issue(
                    "missing-figure-caption",
                    "1.1.1",
                    "major",
                    element=figure,
                    description="Figure missing caption",
                    status="needs_remediation",
                )
            else:
                caption_text = self.get_element_text(caption)
                if not caption_text:
                    self.add_issue(
                        "empty-figure-caption",
                        "1.1.1",
                        "major",
                        element=figure,
                        description="Figure has empty caption",
                        status="needs_remediation",
                    )
                else:
                    self.add_issue(
                        "compliant-figure-structure",
                        "1.1.1",
                        "major",
                        element=figure,
                        description="Figure has proper structure and caption",
                        status="compliant",
                    )
