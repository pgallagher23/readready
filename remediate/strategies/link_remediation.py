# SPDX-License-Identifier: AGPL-3.0-or-later
# Portions derived from Content Accessibility Utility on AWS
# (https://github.com/awslabs/content-accessibility-utility-on-aws),
# Copyright 2025 Amazon.com, Inc. or its affiliates, Apache-2.0.
# Modified by the ReadReady project; see NOTICE.
"""Link remediation strategies.

Fixes empty link text, generic link text, URLs as text, and new-window warnings.
"""

import logging
import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def remediate_empty_link_text(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add descriptive text to empty links based on their URL."""
    context = issue.get("context", {})
    if not isinstance(context, dict):
        context = {}

    # Try to find the link
    element_html = context.get("html_snippet", "")
    href_match = re.search(r'href="([^"]*)"', element_html) if element_html else None

    if not href_match:
        # Fallback: find first empty link
        for link in soup.find_all("a"):
            if not link.get_text(strip=True) and link.get("href"):
                href = link["href"]
                if href.startswith("http"):
                    domain_match = re.search(r"https?://(?:www\.)?([^/]+)", href)
                    if domain_match:
                        link.string = f"Link to {domain_match.group(1)}"
                        return f"Added text: Link to {domain_match.group(1)}"
                link.string = f"Link to {href}"
                return f"Added text: Link to {href}"
        return None

    href = href_match.group(1)
    links = soup.find_all("a", href=href)
    for link in links:
        if not link.get_text(strip=True):
            if href.startswith("http"):
                domain_match = re.search(r"https?://(?:www\.)?([^/]+)", href)
                if domain_match:
                    link.string = f"Link to {domain_match.group(1)}"
                    return f"Added text: Link to {domain_match.group(1)}"
            link.string = f"Link to {href}"
            return f"Added text: Link to {href}"

    return None


def remediate_generic_link_text(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Replace generic link text like 'click here' with descriptive text."""
    generic_texts = {"click here", "here", "read more", "more", "learn more", "details", "link"}

    for link in soup.find_all("a"):
        text = link.get_text(strip=True).lower()
        if text not in generic_texts:
            continue

        href = link.get("href", "")

        # Try URL-based description
        if href.startswith("http"):
            domain_match = re.search(r"https?://(?:www\.)?([^/]+)", href)
            if domain_match:
                link.string = f"Visit {domain_match.group(1)} website"
                return f"Replaced '{text}' with domain-based description"

        # Try context-based description
        parent = link.parent
        if parent and parent.name != "body":
            parent_text = parent.get_text(strip=True)
            link_text = link.get_text(strip=True)
            context_text = parent_text.replace(link_text, "").strip()
            if context_text:
                preview = context_text[:30].strip()
                if len(context_text) > 30:
                    preview += "..."
                link.string = f"More about {preview}"
                return f"Replaced '{text}' with context description"

        # Default improvements
        replacements = {
            "click here": "View details",
            "read more": "Read more about this topic",
            "learn more": "Learn more about this topic",
        }
        link.string = replacements.get(text, "View related information")
        return f"Replaced generic link text '{text}'"

    return None


def remediate_new_window_link_no_warning(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add screen reader warning to links that open in new windows."""
    for link in soup.find_all("a", target="_blank"):
        text = link.get_text(strip=True)
        if "new window" in text.lower() or "new tab" in text.lower():
            continue

        sr_span = soup.new_tag("span")
        sr_span["class"] = "sr-only"
        sr_span.string = " (opens in new window)"
        link.append(sr_span)

        if not link.get("title"):
            link["title"] = f"{text} (opens in new window)"

        return "Added new-window warning to link"

    return None
