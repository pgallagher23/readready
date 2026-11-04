# SPDX-License-Identifier: AGPL-3.0-or-later
"""Language of parts remediation strategies (WCAG 3.1.2).

Fixes xml:lang without lang, invalid lang attribute values, and
detects foreign-language content that needs a lang attribute.
"""

import logging
import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from remediate.element_resolver import get_id_index, resolve_issue_element

try:
    from langdetect import detect as _detect_lang
    from langdetect.lang_detect_exception import LangDetectException
    _HAS_LANGDETECT = True
except ImportError:
    _HAS_LANGDETECT = False

logger = logging.getLogger(__name__)

_VALID_LANG = re.compile(r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]+)*$")


def remediate_xml_lang_without_lang(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Copy xml:lang value to the lang attribute."""
    element = _find_issue_element(soup, issue)
    if element is None:
        # Fallback: fix all elements with xml:lang but no lang
        fixed = 0
        for el in soup.find_all(attrs={"xml:lang": True}):
            if not el.get("lang"):
                el["lang"] = el["xml:lang"]
                fixed += 1
        if fixed:
            return f"Copied xml:lang to lang on {fixed} element(s)"
        return None

    xml_lang = element.get("xml:lang", "")
    if xml_lang and not element.get("lang"):
        element["lang"] = xml_lang
        return f"Added lang='{xml_lang}' from xml:lang on <{element.name}>"
    return None


def remediate_invalid_lang_attribute(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Fix or remove invalid lang attribute values."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    lang = element.get("lang", "").strip()

    if not lang:
        # Empty lang — remove it or inherit from parent
        parent_lang = _get_ancestor_lang(element)
        if parent_lang:
            del element["lang"]
            return f"Removed empty lang attr (inherits '{parent_lang}' from ancestor)"
        else:
            del element["lang"]
            return "Removed empty lang attribute"

    # Try to fix common malformations
    fixed = _fix_lang_code(lang)
    if fixed and _VALID_LANG.match(fixed):
        element["lang"] = fixed
        return f"Fixed lang='{lang}' to '{fixed}' on <{element.name}>"

    # Can't fix — remove the invalid value
    del element["lang"]
    return f"Removed invalid lang='{lang}' from <{element.name}>"


def remediate_missing_lang_on_foreign_content(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Detect the language of content and add a lang attribute if it
    differs from the document language."""
    if not _HAS_LANGDETECT:
        logger.warning("langdetect not installed — skipping language detection")
        return None

    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    # Already has a lang attribute
    if element.get("lang"):
        return None

    text = element.get_text(strip=True)
    if not text or len(text) < 20:
        return None

    # Get the document language
    doc_lang = _get_document_lang(soup)

    try:
        detected = _detect_lang(text)
    except LangDetectException:
        return None

    if not detected:
        return None

    # Normalise for comparison (e.g., "en" matches "en-US")
    detected_base = detected.split("-")[0].lower()
    doc_base = doc_lang.split("-")[0].lower() if doc_lang else ""

    if detected_base == doc_base:
        return None  # Same language as document — no action needed

    element["lang"] = detected
    return f"Added lang='{detected}' to <{element.name}> (detected foreign content)"


def _get_document_lang(soup: BeautifulSoup) -> str:
    """Get the document-level language."""
    html_tag = soup.find("html")
    if html_tag:
        return html_tag.get("lang", "")
    return ""


# ── Helpers ──────────────────────────────────────────────────────────

def _fix_lang_code(lang: str) -> Optional[str]:
    """Attempt to normalise a malformed language code."""
    # Strip whitespace, quotes
    lang = lang.strip().strip("'\"").lower()

    # Common fixes
    replacements = {
        "english": "en",
        "eng": "en",
        "french": "fr",
        "fra": "fr",
        "spanish": "es",
        "spa": "es",
        "german": "de",
        "deu": "de",
        "chinese": "zh",
        "zho": "zh",
        "japanese": "ja",
        "jpn": "ja",
        "portuguese": "pt",
        "por": "pt",
        "italian": "it",
        "ita": "it",
        "russian": "ru",
        "rus": "ru",
        "arabic": "ar",
        "ara": "ar",
        "korean": "ko",
        "kor": "ko",
    }
    if lang in replacements:
        return replacements[lang]

    # If it looks like a valid code already (just needs normalisation)
    if _VALID_LANG.match(lang):
        return lang

    return None


def _get_ancestor_lang(element) -> Optional[str]:
    """Walk up the tree to find the nearest ancestor with a lang attribute."""
    parent = element.parent
    while parent:
        lang = parent.get("lang") if hasattr(parent, "get") else None
        if lang:
            return lang
        parent = getattr(parent, "parent", None)
    return None


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

    for attr in ("id", "lang", "xml:lang"):
        if attr in attrs:
            found = soup.find(tag, attrs={attr: attrs[attr]})
            if found:
                return found

    # Last resort: find by tag name (for elements like blockquote/q/cite
    # that may not have distinguishing attributes)
    if tag in ("blockquote", "q", "cite"):
        found = soup.find(tag)
        if found:
            return found

    return None
