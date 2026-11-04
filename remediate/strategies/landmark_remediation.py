# SPDX-License-Identifier: AGPL-3.0-or-later
# Portions derived from Content Accessibility Utility on AWS
# (https://github.com/awslabs/content-accessibility-utility-on-aws),
# Copyright 2025 Amazon.com, Inc. or its affiliates, Apache-2.0.
# Modified by the ReadReady project; see NOTICE.
"""Landmark remediation strategies.

Adds missing main, nav, header, footer, and skip-link elements.
Landmarks chain: main -> nav -> header -> footer -> skip link.
"""

import logging
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def remediate_missing_skip_link(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add a skip navigation link at the top of the body."""
    body = soup.find("body")
    if not body:
        return None

    # Check if skip link already exists
    for link in body.find_all("a", limit=5):
        href = link.get("href", "")
        text = link.get_text(strip=True).lower()
        if href.startswith("#") and (
            "skip" in text or "main" in text or "content" in text
        ):
            return None

    # Ensure main content has an ID target
    main = soup.find("main") or soup.find(attrs={"role": "main"})
    if main and not main.get("id"):
        main["id"] = "main-content"

    target_id = "main-content"
    if main and main.get("id"):
        target_id = main["id"]

    # Create skip link
    skip_link = soup.new_tag("a", href=f"#{target_id}")
    skip_link["class"] = "skip-link"
    skip_link.string = "Skip to main content"

    # Add CSS for skip link
    style = soup.find("style")
    if not style:
        style = soup.new_tag("style")
        head = soup.find("head")
        if head:
            head.append(style)

    skip_css = """
.skip-link {
    position: absolute;
    top: -44px;
    left: 0;
    background: #000;
    color: #fff;
    padding: 10px 16px;
    min-height: 44px;
    min-width: 44px;
    z-index: 100;
    transition: top 0.3s;
    box-sizing: border-box;
}
.skip-link:focus {
    top: 0;
}
"""
    if style:
        if style.string:
            if ".skip-link" not in style.string:
                style.string = style.string + skip_css
        else:
            style.string = skip_css

    body.insert(0, skip_link)
    return "Added skip navigation link"


def remediate_missing_footer_landmark(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add a footer landmark if missing."""
    if soup.find("footer") or soup.find(attrs={"role": "contentinfo"}):
        return None

    body = soup.find("body")
    if not body:
        return None

    footer = soup.new_tag("footer")
    footer["role"] = "contentinfo"

    copyright_p = soup.new_tag("p")
    copyright_p.string = "Document converted for accessibility compliance."
    footer.append(copyright_p)

    body.append(footer)

    # Chain: also add skip link if missing
    remediate_missing_skip_link(soup, issue)

    return "Added footer landmark"


def remediate_missing_header_landmark(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add a header/banner landmark if missing."""
    if soup.find("header") or soup.find(attrs={"role": "banner"}):
        return None

    body = soup.find("body")
    if not body:
        return None

    header = soup.new_tag("header")
    header["role"] = "banner"

    # Move an existing h1 into the header. When there is none, the only
    # permitted way to get one is to retag content already on the page —
    # this strategy may never synthesize the string. A <title> is not page
    # content: on a document with no real title it is a humanized filename
    # ("Quarterly Budget Draft v2"), which has exactly the shape
    # is_usable_heading_text accepts while appearing nowhere in the
    # document. Announcing that to a screen-reader user as the document
    # heading is worse than having no heading, which the audit still reports.
    h1 = soup.find("h1")
    if h1 is None:
        from remediate.strategies.heading_remediation import (
            promote_document_h1,
        )

        if promote_document_h1(soup, args[0] if args else None):
            h1 = soup.find("h1")

    if h1:
        h1.extract()
        header.append(h1)
    # No promotable heading: the header landmark is still this strategy's job.

    body.insert(0, header)

    # Chain: also add footer if missing
    remediate_missing_footer_landmark(soup, issue)

    return "Added header landmark"


def remediate_missing_navigation_landmark(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add a navigation landmark if missing."""
    if soup.find("nav") or soup.find(attrs={"role": "navigation"}):
        return None

    body = soup.find("body")
    if not body:
        return None

    nav = soup.new_tag("nav")
    nav["role"] = "navigation"
    nav["aria-label"] = "Document navigation"

    # Look for existing link lists to wrap
    ul_with_links = None
    for ul in soup.find_all("ul"):
        links = ul.find_all("a")
        if len(links) >= 2:
            ul_with_links = ul
            break

    if ul_with_links:
        ul_with_links.wrap(nav)
    else:
        # Create minimal nav with document structure
        nav_list = soup.new_tag("ul")
        li = soup.new_tag("li")
        link = soup.new_tag("a", href="#main-content")
        link.string = "Main Content"
        li.append(link)
        nav_list.append(li)
        nav.append(nav_list)

        header = soup.find("header")
        if header:
            header.insert_after(nav)
        else:
            body.insert(0, nav)

    # Chain: also add header if missing
    remediate_missing_header_landmark(soup, issue, *(args[:1]))

    return "Added navigation landmark"


def remediate_missing_main_landmark(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add a main landmark and move content into it."""
    if soup.find("main") or soup.find(attrs={"role": "main"}):
        return None

    body = soup.find("body")
    if not body:
        return None

    main = soup.new_tag("main")
    main["id"] = "main-content"
    main["role"] = "main"

    # Move non-landmark children into main
    landmark_tags = {"header", "nav", "footer", "aside"}
    landmark_roles = {"banner", "navigation", "contentinfo", "complementary"}

    children_to_move = []
    for child in list(body.children):
        if hasattr(child, "name") and child.name:
            if child.name in landmark_tags:
                continue
            if child.get("role") in landmark_roles:
                continue
            if child.name == "a" and "skip-link" in " ".join(child.get("class", [])):
                continue
            if child.name == "style" or child.name == "script":
                continue
        children_to_move.append(child)

    for child in children_to_move:
        child.extract()
        main.append(child)

    # Insert main after header/nav, or at start of body
    header = soup.find("header")
    nav = soup.find("nav")
    if nav:
        nav.insert_after(main)
    elif header:
        header.insert_after(main)
    else:
        body.append(main)

    # Chain: also add navigation if missing
    remediate_missing_navigation_landmark(soup, issue, *(args[:1]))

    return "Added main landmark"
