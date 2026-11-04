# SPDX-License-Identifier: AGPL-3.0-or-later
# Portions derived from Content Accessibility Utility on AWS
# (https://github.com/awslabs/content-accessibility-utility-on-aws),
# Copyright 2025 Amazon.com, Inc. or its affiliates, Apache-2.0.
# Modified by the ReadReady project; see NOTICE.
"""Document structure remediation strategies.

Fixes missing document title and language attributes.
"""

import logging
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def remediate_missing_document_title(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add a document title derived from h1 or meta description."""
    head = soup.find("head")
    if not head:
        head = soup.new_tag("head")
        html = soup.find("html")
        if html:
            html.insert(0, head)
        else:
            return None

    existing_title = head.find("title")
    if existing_title and existing_title.string and existing_title.string.strip():
        return None

    # Derive title from h1
    title_text = "Untitled Document"
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title_text = h1.get_text(strip=True)
    else:
        # Try meta description
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            title_text = meta_desc["content"][:80]

    if existing_title:
        existing_title.string = title_text
    else:
        title_tag = soup.new_tag("title")
        title_tag.string = title_text
        head.append(title_tag)

    return f"Added document title: {title_text}"


def remediate_missing_language(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add lang attribute to html element."""
    html_tag = soup.find("html")
    if not html_tag:
        return None

    if html_tag.get("lang"):
        return None

    # Default to English; could be enhanced with langdetect
    html_tag["lang"] = "en"
    return "Added language attribute: lang='en'"
