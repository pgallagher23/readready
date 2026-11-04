# SPDX-License-Identifier: AGPL-3.0-or-later
"""Color use and link text remediation strategies.

WCAG 1.4.1 — Ensure links have non-color distinction.
WCAG 2.4.4 — Fix URL-as-link-text.
"""

import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from remediate.element_resolver import get_id_index, resolve_issue_element

logger = logging.getLogger(__name__)


# ── 1.4.1 Color Use ────────────────────────────────────────────────

def remediate_color_only_link_distinction(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add text-decoration:underline to links that removed it, so they
    are not distinguished from surrounding text by color alone."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    style = element.get("style", "")

    # Replace text-decoration:none with text-decoration:underline
    new_style = re.sub(
        r"text-decoration\s*:\s*none\s*;?\s*",
        "text-decoration: underline; ",
        style,
    ).strip()

    if new_style != style:
        element["style"] = new_style
        return f"Restored text-decoration:underline on link"

    # If no text-decoration was set at all, add underline
    if "text-decoration" not in style:
        if style:
            element["style"] = style.rstrip(";") + "; text-decoration: underline"
        else:
            element["style"] = "text-decoration: underline"
        return "Added text-decoration:underline to link"

    return None


# ── 2.4.4 Link Purpose ────────────────────────────────────────────

def remediate_url_as_link_text(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """When a URL is used as link text, add an aria-label with a more
    descriptive version derived from the URL's domain and path."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    # Already has an accessible name override
    if element.get("aria-label"):
        return None

    text = element.get_text(strip=True)
    href = element.get("href", text)

    label = _url_to_label(href)
    if label and label != text:
        element["aria-label"] = label
        return f"Added aria-label='{label}' to URL link"
    return None


# ── Helpers ──────────────────────────────────────────────────────────

def _url_to_label(url: str) -> Optional[str]:
    """Convert a URL to a human-readable label."""
    if not url:
        return None

    # mailto: / bare email address → describe as an email link.
    if url.startswith("mailto:") or ("@" in url and "/" not in url):
        addr = url.split(":", 1)[1] if url.startswith("mailto:") else url
        addr = addr.split("?", 1)[0].strip()
        return f"Email {addr}" if addr else None

    try:
        parsed = urlparse(url)
    except Exception:
        return None

    hostname = parsed.hostname or ""
    path = parsed.path or ""

    # Scheme-less URL ("www.mi.gov/x") — urlparse puts it all in path; re-parse
    # with a synthetic scheme so the domain/path split works.
    if not hostname and "." in url and "//" not in url:
        try:
            parsed = urlparse("http://" + url.lstrip("/"))
            hostname = parsed.hostname or ""
            path = parsed.path or ""
        except Exception:
            pass

    # Strip www. prefix
    if hostname.startswith("www."):
        hostname = hostname[4:]

    if not hostname:
        return None

    # Build label from domain + meaningful path segment
    parts = [hostname]
    path_segments = [
        s for s in path.strip("/").split("/") if s and s != "index.html"
    ]
    if path_segments:
        # Take the last meaningful segment
        segment = path_segments[-1]
        # Clean up the segment
        segment = segment.rsplit(".", 1)[0]  # remove extension
        segment = segment.replace("-", " ").replace("_", " ").strip()
        if segment:
            parts.append(segment)

    label = " — ".join(parts)
    # A bare domain ("portal.example.edu") would read as a raw URL; phrase it
    # as a clear sentence so it describes purpose and is not itself URL-shaped
    # (WCAG 2.4.4 — see audit.checks.link_checks.LinkTextCheck).
    if len(parts) == 1:
        return f"Link to {label} website"
    return f"Link to {label}"


def _find_issue_element(soup: BeautifulSoup, issue: Dict[str, Any]):
    """Locate the element referenced by an audit issue."""
    ctx = issue.get("context", {})
    if not isinstance(ctx, dict):
        return None

    resolved = resolve_issue_element(soup, issue, id_index=get_id_index(soup))
    if resolved is not None:
        return resolved

    tag = ctx.get("element_name")
    if not tag:
        return None

    attrs = ctx.get("attributes", {})
    if not isinstance(attrs, dict):
        attrs = {}

    for attr in ("id", "href"):
        if attr in attrs:
            found = soup.find(tag, attrs={attr: attrs[attr]})
            if found:
                return found

    return None
