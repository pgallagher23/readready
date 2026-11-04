# SPDX-License-Identifier: AGPL-3.0-or-later
"""Focus order and focus visibility remediation strategies.

WCAG 2.4.3 Focus Order — fix positive tabindex and CSS order.
WCAG 2.4.7 Focus Visible — remove outline suppression or add replacement.
"""

import logging
import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from remediate.element_resolver import get_id_index, resolve_issue_element

logger = logging.getLogger(__name__)


# ── 2.4.3 Focus Order ───────────────────────────────────────────────

def remediate_positive_tabindex(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Replace positive tabindex with tabindex='0' to restore natural
    DOM focus order."""
    element = _find_issue_element(soup, issue)
    if element is None:
        # Fallback: fix all positive tabindex values in the document
        fixed = 0
        for elem in soup.find_all(attrs={"tabindex": True}):
            try:
                val = int(elem.get("tabindex", "0"))
            except (ValueError, TypeError):
                continue
            if val > 0:
                elem["tabindex"] = "0"
                fixed += 1
        if fixed:
            return f"Reset {fixed} positive tabindex values to 0"
        return None

    try:
        val = int(element.get("tabindex", "0"))
    except (ValueError, TypeError):
        return None

    if val > 0:
        element["tabindex"] = "0"
        return f"Changed tabindex='{val}' to '0' on <{element.name}>"
    return None


def remediate_stylesheet_css_order(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Remove CSS 'order' property from <style> blocks."""
    fixed = 0
    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or ""
        if not css_text:
            continue

        # Remove order declarations (but not border, z-order, etc.)
        new_css = re.sub(
            r"([\s;{])order\s*:\s*-?\d+\s*;?",
            r"\1",
            css_text,
        )
        if new_css != css_text:
            style_tag.string = new_css
            fixed += 1

    if fixed:
        return f"Removed CSS 'order' property from {fixed} stylesheet(s)"
    return None


def remediate_css_order_focus_mismatch(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Remove CSS 'order' from inline styles."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    style = element.get("style", "")
    new_style = re.sub(r"order\s*:\s*-?\d+\s*;?\s*", "", style).strip()

    if new_style != style:
        if new_style:
            element["style"] = new_style
        else:
            del element["style"]
        return f"Removed CSS 'order' from <{element.name}> inline style"
    return None


def remediate_flex_reverse_focus_mismatch(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Remove flex-direction reverse from inline styles."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    style = element.get("style", "")
    new_style = re.sub(
        r"flex-direction\s*:\s*\w+-reverse\s*;?\s*", "", style
    ).strip()

    if new_style != style:
        if new_style:
            element["style"] = new_style
        else:
            del element["style"]
        return f"Removed flex-direction reverse from <{element.name}>"
    return None


def remediate_stylesheet_flex_reverse(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Remove flex-direction reverse from <style> blocks."""
    fixed = 0
    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or ""
        if not css_text:
            continue

        new_css = re.sub(
            r"flex-direction\s*:\s*\w+-reverse\s*;?\s*",
            "",
            css_text,
        )
        if new_css != css_text:
            style_tag.string = new_css
            fixed += 1

    if fixed:
        return f"Removed flex-direction reverse from {fixed} stylesheet(s)"
    return None


# ── 2.4.7 Focus Visible ─────────────────────────────────────────────

def remediate_focus_outline_removed(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Fix inline outline:none by removing the declaration or adding
    a box-shadow replacement."""
    element = _find_issue_element(soup, issue)
    if element is None:
        return None

    style = element.get("style", "")
    # Replace outline:none with a visible focus indicator
    new_style = re.sub(
        r"outline\s*:\s*(none|0(?:px)?)\s*;?\s*",
        "",
        style,
    ).strip()

    if new_style != style:
        if new_style:
            element["style"] = new_style
        else:
            del element["style"]
        return f"Removed outline suppression from <{element.name}>"
    return None


def remediate_stylesheet_focus_outline_removed(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Fix :focus rules in <style> blocks that suppress outline by
    adding a box-shadow replacement."""
    fixed = 0
    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or ""
        if not css_text:
            continue

        new_css = _fix_focus_outline_in_css(css_text)
        if new_css != css_text:
            style_tag.string = new_css
            fixed += 1

    if fixed:
        return f"Fixed focus outline suppression in {fixed} stylesheet(s)"
    return None


def remediate_blanket_outline_removal(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Remove blanket '* { outline: none }' rules from stylesheets."""
    fixed = 0
    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or ""
        if not css_text:
            continue

        # Remove * { outline: none/0 } rules
        new_css = re.sub(
            r"\*\s*\{[^}]*outline\s*:\s*(none|0(?:px)?)\s*;?\s*[^}]*\}",
            "",
            css_text,
            flags=re.I,
        )
        if new_css != css_text:
            style_tag.string = new_css
            fixed += 1

    if fixed:
        return f"Removed blanket outline suppression from {fixed} stylesheet(s)"
    return None


def remediate_element_focus_outline_removed(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Fix element-specific :focus outline suppression in stylesheets."""
    # This is handled by the same logic as stylesheet_focus_outline_removed
    return remediate_stylesheet_focus_outline_removed(soup, issue, *args)


# ── Helpers ──────────────────────────────────────────────────────────

def _fix_focus_outline_in_css(css_text: str) -> str:
    """In CSS text, replace outline:none in :focus rules with a
    box-shadow replacement indicator."""

    def _replace_focus_block(match):
        selector = match.group(1)
        declarations = match.group(2)

        # Check if there's already a replacement
        has_replacement = bool(re.search(
            r"(box-shadow|border\s*:|border-color|text-decoration)\s*:",
            declarations, re.I,
        ))
        if has_replacement:
            return match.group(0)

        # Replace outline:none with box-shadow focus indicator
        new_decls = re.sub(
            r"outline\s*:\s*(none|0(?:px)?)\s*;?",
            "outline: 2px solid #4A90D9;",
            declarations,
            flags=re.I,
        )
        return f"{selector}:focus {{{new_decls}}}"

    return re.sub(
        r"([^{}]+?)(:focus\s*)\{([^}]*)\}",
        lambda m: _replace_focus_block_wrapper(m),
        css_text,
        flags=re.I,
    )


def _replace_focus_block_wrapper(match):
    """Wrapper for regex sub callback."""
    selector = match.group(1).rstrip()
    declarations = match.group(3)

    has_outline_removal = bool(re.search(
        r"outline\s*:\s*(none|0(?:px)?)", declarations, re.I
    ))
    if not has_outline_removal:
        return match.group(0)

    has_replacement = bool(re.search(
        r"(box-shadow|border\s*:|border-color|text-decoration)\s*:",
        declarations, re.I,
    ))
    if has_replacement:
        return match.group(0)

    # Replace outline:none with visible outline
    new_decls = re.sub(
        r"outline\s*:\s*(none|0(?:px)?)\s*;?",
        "outline: 2px solid #4A90D9;",
        declarations,
        flags=re.I,
    )
    return f"{selector}:focus {{{new_decls}}}"


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

    for attr in ("id", "style", "tabindex"):
        if attr in attrs:
            found = soup.find(tag, attrs={attr: attrs[attr]})
            if found:
                return found

    return None
