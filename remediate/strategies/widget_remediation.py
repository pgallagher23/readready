# SPDX-License-Identifier: AGPL-3.0-or-later
"""Widget and fieldset remediation strategies.

WCAG 1.3.1 — Fix fieldsets missing legend elements.
WCAG 4.1.2 — Add accessible names to custom widgets.
"""

import logging
import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from remediate.element_resolver import get_id_index, resolve_issue_element

logger = logging.getLogger(__name__)


def remediate_fieldset_missing_legend(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add or populate legend elements in fieldsets that lack them."""
    element = _find_issue_element(soup, issue)

    if element is not None and element.name == "fieldset":
        fieldsets = [element]
    else:
        fieldsets = soup.find_all("fieldset")

    fixed = 0
    for fieldset in fieldsets:
        legend = fieldset.find("legend")
        if legend and legend.get_text(strip=True):
            continue

        # Derive legend text from context
        legend_text = _derive_fieldset_legend(fieldset)

        if legend is None:
            legend = soup.new_tag("legend")
            fieldset.insert(0, legend)

        legend.string = legend_text
        fixed += 1

    if fixed:
        return f"Added legend to {fixed} fieldset(s)"
    return None


def remediate_custom_widget_missing_name(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add an accessible name (aria-label) to custom widgets that lack one."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    # Already has a name
    if element.get("aria-label") or element.get("aria-labelledby"):
        return None

    name = _derive_widget_name(soup, element)
    element["aria-label"] = name
    role = element.get("role", element.name)
    return f"Added aria-label='{name}' to {role} widget"


def remediate_duplicate_link_text_different_url(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Disambiguate links that share the same text but point to different
    URLs by adding aria-label with destination context."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    if element.get("aria-label"):
        return None

    text = element.get_text(strip=True)
    href = element.get("href", "")

    # Build a descriptive label from the URL
    label = _url_context_label(text, href)
    if label and label != text:
        element["aria-label"] = label
        return f"Added aria-label='{label}' to disambiguate link"
    return None


def remediate_color_only_status(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add a text prefix to status elements that may convey meaning
    through color alone."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    classes = element.get("class", [])
    if isinstance(classes, str):
        classes = classes.split()

    # Determine the status type from class names
    prefix = _status_prefix_from_classes(classes)

    # Add role if missing
    if not element.get("role"):
        if prefix in ("Error:", "Warning:"):
            element["role"] = "alert"
        else:
            element["role"] = "status"

    # Prepend text prefix if not already present
    text = element.get_text(strip=True)
    if text and not any(text.lower().startswith(p.lower().rstrip(":"))
                        for p in ("Error:", "Warning:", "Success:", "Note:", "Info:")):
        prefix_span = soup.new_tag("span")
        prefix_span["class"] = "sr-only"
        prefix_span.string = prefix + " "
        element.insert(0, prefix_span)
        return f"Added '{prefix}' prefix and role to status element"

    # Just add role if text already has prefix
    return f"Added role='{element.get('role')}' to status element"


# ── Helpers ──────────────────────────────────────────────────────────

def _derive_fieldset_legend(fieldset) -> str:
    """Derive legend text from the fieldset's contents."""
    # Try to get text from the first label in the fieldset
    labels = fieldset.find_all("label")
    if labels:
        # Look for common prefix among labels
        texts = [l.get_text(strip=True) for l in labels if l.get_text(strip=True)]
        if texts:
            # If labels share a common word, use it
            words_sets = [set(t.lower().split()) for t in texts]
            if len(words_sets) >= 2:
                common = words_sets[0]
                for ws in words_sets[1:]:
                    common &= ws
                # Remove generic words
                common -= {"the", "a", "an", "is", "or", "and", "for"}
                if common:
                    return " ".join(w.capitalize() for w in sorted(common))

    # Try input names
    inputs = fieldset.find_all("input")
    if inputs:
        name = inputs[0].get("name", "")
        if name:
            return " ".join(
                w.capitalize() for w in re.split(r"[_\-]", name)
            )

    return "Options"


def _derive_widget_name(soup, element) -> str:
    """Derive an accessible name for a widget from context."""
    # 1. Child text content
    text = element.get_text(strip=True)
    if text and len(text) <= 50:
        return text

    # 2. Title attribute
    if element.get("title"):
        return element["title"]

    # 3. Look for a preceding label or heading
    prev = element.find_previous(["label", "h1", "h2", "h3", "h4", "h5", "h6"])
    if prev:
        label_text = prev.get_text(strip=True)
        if label_text:
            return label_text

    # 4. Look for parent with text
    parent = element.parent
    if parent and parent.name not in ("body", "html", "[document]"):
        parent_text = parent.get_text(strip=True)
        if parent_text and len(parent_text) <= 50:
            return parent_text

    # 5. Derive from role
    role = element.get("role", "")
    if role:
        return role.replace("-", " ").capitalize()

    return element.name.capitalize()


def _url_context_label(text: str, href: str) -> Optional[str]:
    """Build a descriptive label from link text + URL destination."""
    if not href:
        return None

    from urllib.parse import urlparse
    try:
        parsed = urlparse(href)
    except Exception:
        return None

    # Extract meaningful destination info
    hostname = parsed.hostname or ""
    if hostname.startswith("www."):
        hostname = hostname[4:]

    path = parsed.path or ""
    path_segments = [
        s for s in path.strip("/").split("/")
        if s and s not in ("index.html", "index.htm")
    ]

    context = ""
    if path_segments:
        segment = path_segments[-1]
        segment = segment.rsplit(".", 1)[0]
        context = segment.replace("-", " ").replace("_", " ").strip()
    elif hostname:
        context = hostname

    if context:
        return f"{text} ({context})"
    return None


def _status_prefix_from_classes(classes: list) -> str:
    """Determine appropriate text prefix from CSS class names."""
    class_str = " ".join(c.lower() for c in classes)

    if any(w in class_str for w in ("error", "danger", "invalid")):
        return "Error:"
    if any(w in class_str for w in ("warning", "caution")):
        return "Warning:"
    if any(w in class_str for w in ("success", "valid")):
        return "Success:"
    if any(w in class_str for w in ("info", "notice")):
        return "Note:"
    if "alert" in class_str:
        return "Alert:"
    return "Note:"


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

    for attr in ("id", "role", "class", "href", "name"):
        if attr in attrs:
            found = soup.find(tag, attrs={attr: attrs[attr]})
            if found:
                return found

    return None
