# SPDX-License-Identifier: AGPL-3.0-or-later
"""Figure structure and alt text remediation strategies.

Wraps standalone images in proper <figure> elements with <figcaption>,
and adds captions to existing figures only when the document itself
provides one. Alt text is never invented here: without an AI description
an image keeps an empty alt and the user is prompted to add one.
"""

import logging
import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def remediate_improper_figure_structure(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Wrap standalone images in figure elements with figcaption."""
    wrapped = 0
    for img in soup.find_all("img"):
        if img.find_parent("figure"):
            continue

        # Skip decorative images
        if img.get("role") in ("presentation", "none"):
            continue

        figure = soup.new_tag("figure")
        figure["role"] = "group"

        caption_text, caption_p = _find_source_caption(img)

        # Wrap image in figure
        img.wrap(figure)

        if caption_text:
            _attach_caption(soup, figure, caption_text, caption_p)
            current_alt = (img.get("alt") or "").strip()
            if not current_alt or _caption_makes_alt_redundant(caption_text, current_alt):
                # The figcaption carries the description; a duplicate img alt
                # becomes a nested /Alt that is never read in the PDF.
                figure["aria-label"] = caption_text[:200]
                img["alt"] = ""
            # Otherwise the img alt is a real description the caption does not
            # repeat (e.g. a bare "Figure 3"): keep it, and do not let the
            # caption become the figure's accessible name over it (G-15).

        wrapped += 1

    if wrapped:
        return f"Wrapped {wrapped} image(s) in figure elements"
    return None


def _find_source_caption(img):
    """Caption the document itself provides for ``img``: ``(text, p)``.

    Only real caption sources count: a "Figure N"/"Fig." paragraph that is
    the immediate next element sibling of the image (or of its wrapping
    figure), or the image's title attribute. ``p`` is that paragraph, or
    None when the caption came from the title. Alt text and the nearest
    heading are not captions; promoting them invents visible structure.
    Returns ``("", None)`` when there is no caption source.
    """
    anchor = img.find_parent("figure") or img
    next_el = anchor.find_next_sibling()
    if next_el is not None and next_el.name == "p":
        p_text = next_el.get_text(strip=True)
        if p_text.startswith(("Figure", "Fig.")):
            return p_text, next_el
    if img.get("title") and img["title"].strip():
        return img["title"].strip()[:200], None
    return "", None


def _attach_caption(soup, figure, caption_text, caption_p) -> None:
    """Append a figcaption to ``figure``.

    A caption paragraph is moved into the figcaption (not copied), so the
    caption is read once rather than twice.
    """
    figcaption = soup.new_tag("figcaption")
    if caption_p is not None:
        for child in list(caption_p.contents):
            figcaption.append(child.extract())
        caption_p.decompose()
    else:
        figcaption.string = caption_text
    figure.append(figcaption)


def remediate_missing_figure_caption(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Add figcaption to the specific figure referenced by the audit issue."""
    figure = _find_figure_from_issue(soup, issue)
    if not figure:
        return None

    if figure.find("figcaption"):
        return None  # already has caption

    img = figure.find("img")
    caption_text, caption_p = _find_source_caption(img) if img else ("", None)

    if not caption_text:
        return None

    _attach_caption(soup, figure, caption_text, caption_p)
    current_alt = (img.get("alt") or "").strip()
    if not figure.get("aria-label") and (
        not current_alt or _caption_makes_alt_redundant(caption_text, current_alt)
    ):
        figure["aria-label"] = caption_text[:200]

    label = caption_text[:60] + ("..." if len(caption_text) > 60 else "")
    return f"Added figcaption: \"{label}\""


def remediate_alt_text(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Ensure an image without usable alt carries an empty alt.

    Descriptions come only from the AI model or from the user; nothing is
    invented here. Returns None so the audit issue stays open.
    """
    img = _find_image_from_issue(soup, issue)
    if not img:
        return None

    current_alt = (img.get("alt") or "").strip()
    generic_alts = {"image", "diagram", "photo", "picture", "graphic", "icon"}
    if current_alt and current_alt.lower() not in generic_alts:
        return None  # the author's alt text stands

    # No description is invented locally. An empty alt is delivered and the
    # issue stays open, so the user is prompted to add one in the editor.
    img["alt"] = ""
    return None


def _find_image_from_issue(
    soup: BeautifulSoup, issue: Dict[str, Any]
) -> Optional[Any]:
    """Locate the specific <img> referenced by an audit issue."""
    context = issue.get("context", {})
    if not isinstance(context, dict):
        context = {}

    # Try HTML snippet to match
    snippet = context.get("html_snippet", "")
    if snippet:
        src_match = re.search(r'src=["\']([^"\']+)["\']', snippet)
        if src_match:
            src_val = src_match.group(1)
            img = soup.find("img", src=src_val)
            if img:
                return img

    # Try location path
    location = issue.get("location", {})
    if isinstance(location, dict):
        path = location.get("path", "")
        if path:
            # Clean [document] prefix that breaks CSS selectors
            clean_path = re.sub(r"\[document\]\s*>\s*", "", path)
            try:
                result = soup.select_one(clean_path)
                if result and result.name == "img":
                    return result
                if result:
                    img = result.find("img")
                    if img:
                        return img
            except Exception:
                pass

    # Try matching by position index from context
    position = context.get("position", {})
    if isinstance(position, dict) and "index" in position:
        idx = position["index"]
        imgs = soup.find_all("img")
        if 0 <= idx < len(imgs):
            return imgs[idx]

    # Fallback: first image without proper alt text
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        if not alt or alt.lower() in {"image", "diagram", "photo", "picture", "graphic", "icon"}:
            return img

    return None


def _find_figure_from_issue(
    soup: BeautifulSoup, issue: Dict[str, Any]
) -> Optional[Any]:
    """Locate the specific <figure> referenced by an audit issue."""
    context = issue.get("context", {})
    if not isinstance(context, dict):
        context = {}

    # 1. Match via img src extracted from snippet — most reliable
    snippet = context.get("html_snippet", "")
    if snippet:
        src_match = re.search(r'src=["\']([^"\']+)["\']', snippet)
        if src_match:
            img = soup.find("img", src=src_match.group(1))
            if img:
                figure = img.find_parent("figure")
                if figure:
                    return figure

    # 2. CSS path from location
    location = issue.get("location", {})
    if isinstance(location, dict):
        path = location.get("path", "")
        if path:
            clean_path = re.sub(r"\[document\]\s*>\s*", "", path)
            try:
                result = soup.select_one(clean_path)
                if result and result.name == "figure":
                    return result
            except Exception:
                pass
            # Relax body > child to body descendant for <main> wrapper
            descendant = re.sub(r"body\s*>", "body ", clean_path)
            if descendant != clean_path:
                try:
                    result = soup.select_one(descendant)
                    if result and result.name == "figure":
                        return result
                except Exception:
                    pass

    # 3. Position index
    position = context.get("position", {})
    if isinstance(position, dict) and "index" in position:
        idx = position["index"]
        figures = soup.find_all("figure")
        if 0 <= idx < len(figures):
            return figures[idx]

    return None


_BARE_FIGURE_NUMBER_RE = re.compile(
    r"^(figure|fig\.?|table|chart|image|plate)\s*\d*[.:]?$", re.I
)


def _caption_makes_alt_redundant(caption: str, alt: str) -> bool:
    """True only when the figcaption already conveys the alt's information.

    A bare figure number ("Figure 3"), a source line, or any caption shorter
    than the alt does NOT make the alt redundant — clearing it in those cases
    destroys the image's only accessible description (G-15).
    """
    cap = " ".join(caption.lower().split())
    a = " ".join(alt.lower().split())
    if not cap:
        return False
    if _BARE_FIGURE_NUMBER_RE.match(cap):
        return False
    if cap == a or a in cap:
        return True
    if len(cap) < len(a):
        return False
    alt_words = set(a.split())
    if not alt_words:
        return False
    return len(alt_words & set(cap.split())) / len(alt_words) >= 0.8


def clear_redundant_img_alt(soup: BeautifulSoup) -> Optional[str]:
    """Clear img alt text when the parent figure already has a figcaption.

    If a <figure> has a non-empty <figcaption>, the figcaption is the
    accessible description for the whole figure.  Any alt text on the
    <img> inside becomes redundant and maps to a nested /Alt in the PDF
    structure tree — causing Acrobat's "Alternate text that will never
    be read" failure.
    """
    cleared = 0
    for figure in soup.find_all("figure"):
        figcaption = figure.find("figcaption")
        if not figcaption or not figcaption.get_text(strip=True):
            continue
        img = figure.find("img")
        alt = (img.get("alt") or "").strip() if img else ""
        if not img or not alt:
            continue
        if not _caption_makes_alt_redundant(figcaption.get_text(strip=True), alt):
            # The caption does not carry the alt's information.  Clearing alt
            # here would destroy the image's only description (G-15).
            continue
        img["alt"] = ""
        img.attrs.pop("aria-label", None)
        cleared += 1
    if cleared:
        return f"Cleared redundant alt text on {cleared} image(s) inside captioned figures"
    return None
