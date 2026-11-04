# SPDX-License-Identifier: AGPL-3.0-or-later
"""Link-related accessibility checks."""

import re
from typing import List, Dict

from audit.base_check import AccessibilityCheck


class LinkTextCheck(AccessibilityCheck):
    """Check for proper link text (WCAG 2.4.4)."""

    def check(self) -> None:
        links = self.soup.find_all("a")

        links_by_text: Dict[str, List[str]] = {}

        generic_link_texts = [
            "click here", "click", "here", "read more", "more",
            "learn more", "details", "link", "this link", "this page",
            "this", "go", "go to", "view", "view more", "see more",
            "see details", "continue", "continue reading",
        ]

        for link in links:
            if link.get("href", "").startswith("#") and not link.get("href", "").strip("#"):
                continue

            text = self.get_element_text(link)

            if not text:
                img = link.find("img")
                if img and img.get("alt"):
                    self.add_issue(
                        "compliant-image-link",
                        "2.4.4",
                        "info",
                        element=link,
                        description=f"Link with image has appropriate alt text: \"{img.get('alt')}\"",
                        status="compliant",
                    )
                    continue

                self.add_issue(
                    "empty-link-text",
                    "2.4.4",
                    "critical",
                    element=link,
                    description="Link has no text content",
                )
                continue

            text_lower = text.lower()
            # aria-label provides an accessible name override (WCAG 2.4.4)
            aria_label = link.get("aria-label", "").strip()
            if text_lower in generic_link_texts:
                if aria_label and aria_label.lower() not in generic_link_texts:
                    self.add_issue(
                        "compliant-link-text",
                        "2.4.4",
                        "info",
                        element=link,
                        description=f"Generic visible text '{text}' overridden by aria-label: '{aria_label}'",
                        status="compliant",
                    )
                else:
                    self.add_issue(
                        "generic-link-text",
                        "2.4.4",
                        "major",
                        element=link,
                        description=f"Generic link text: '{text}' is not descriptive",
                    )
            elif self._is_url(text):
                if aria_label and not self._is_url(aria_label):
                    self.add_issue(
                        "compliant-link-text",
                        "2.4.4",
                        "info",
                        element=link,
                        description=f"URL visible text overridden by aria-label: '{aria_label}'",
                        status="compliant",
                    )
                else:
                    self.add_issue(
                        "url-as-link-text",
                        "2.4.4",
                        "minor",
                        element=link,
                        description=f"URL used as link text: '{text}'",
                    )
            else:
                self.add_issue(
                    "compliant-link-text",
                    "2.4.4",
                    "info",
                    element=link,
                    description=f"Link has descriptive text: '{text}'",
                    status="compliant",
                )

            href = link.get("href", "")
            if text_lower not in links_by_text:
                links_by_text[text_lower] = []
            links_by_text[text_lower].append(href)

        for text, hrefs in links_by_text.items():
            if len(hrefs) > 1 and len(set(hrefs)) > 1:
                for link in self.soup.find_all(
                    "a", text=re.compile(f"^{re.escape(text)}$", re.IGNORECASE)
                ):
                    self.add_issue(
                        "duplicate-link-text-different-url",
                        "2.4.9",
                        "major",
                        element=link,
                        description=f"Multiple links with text '{text}' go to different destinations",
                    )

    def _is_url(self, text: str) -> bool:
        # A URL used as link text is a single token. A multi-word phrase that
        # merely mentions a domain (e.g. an aria-label "Link to example.edu
        # website") is descriptive, not a raw URL — internal whitespace rules it
        # out. This keeps bare-URL link text flagged while letting descriptive
        # aria-label overrides count as compliant (WCAG 2.4.4).
        t = (text or "").strip()
        if not t or re.search(r"\s", t):
            return False
        url_patterns = [
            r"^https?://", r"^www\.", r"\.com(/|$)", r"\.org(/|$)",
            r"\.net(/|$)", r"\.edu(/|$)", r"\.gov(/|$)", r"\.io(/|$)",
        ]
        return any(re.search(pattern, t, re.IGNORECASE) for pattern in url_patterns)


class NewWindowLinkCheck(AccessibilityCheck):
    """Check for links that open in new windows (WCAG 3.2.5)."""

    def check(self) -> None:
        new_window_links = self.soup.find_all("a", attrs={"target": "_blank"})
        new_window_links.extend(self.soup.find_all("a", attrs={"rel": "external"}))

        for link in new_window_links:
            text = self.get_element_text(link)
            has_warning = False
            warning_method = ""

            warning_phrases = ["new window", "new tab", "opens in new", "external"]
            if any(phrase in text.lower() for phrase in warning_phrases):
                has_warning = True
                warning_method = "text content"

            aria_label = link.get("aria-label", "")
            if any(phrase in aria_label.lower() for phrase in warning_phrases):
                has_warning = True
                warning_method = "aria-label"

            title = link.get("title", "")
            if any(phrase in title.lower() for phrase in warning_phrases):
                has_warning = True
                warning_method = "title attribute"

            sr_elements = link.find_all(
                ["span", "div"],
                class_=lambda c: c
                and any(
                    sr_class in c
                    for sr_class in ["sr-only", "visually-hidden", "screen-reader-text"]
                ),
            )
            for sr_elem in sr_elements:
                sr_text = sr_elem.get_text(strip=True)
                if any(phrase in sr_text.lower() for phrase in warning_phrases):
                    has_warning = True
                    warning_method = "screen reader text"

            if has_warning:
                self.add_issue(
                    "compliant-new-window-link",
                    "3.2.5",
                    "info",
                    element=link,
                    description=f"Link opens in new window with warning via {warning_method}: '{text}'",
                    status="compliant",
                )
            else:
                self.add_issue(
                    "new-window-link-no-warning",
                    "3.2.5",
                    "minor",
                    element=link,
                    description=f"Link opens in new window without warning: '{text}'",
                )
