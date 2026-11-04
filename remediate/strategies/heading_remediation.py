# SPDX-License-Identifier: AGPL-3.0-or-later
# Portions derived from Content Accessibility Utility on AWS
# (https://github.com/awslabs/content-accessibility-utility-on-aws),
# Copyright 2025 Amazon.com, Inc. or its affiliates, Apache-2.0.
# Modified by the ReadReady project; see NOTICE.
"""Heading remediation strategies.

Fixes missing h1, missing headings, skipped levels, and empty content.
"""

import logging
import re
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def promote_document_h1(
    soup: BeautifulSoup, options: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """Retag existing content as the document h1, or return None.

    Population A (document has h2-h6): retag the earliest title-matching
    sub-heading within the first HEADING_CANDIDATE_WINDOW headings; if none
    matches, the earliest sub-heading in that same window with usable text.
    Population B (zero
    headings): window ladder over leading body text blocks — title match,
    then first usable text. Retag-only by construction: no text is created.
    """
    from output_gen.heading_detection import (
        HEADING_CANDIDATE_WINDOW,
        is_usable_heading_text,
        normalize_text,
    )
    from output_gen.heading_inference import close_html_heading_level_gaps

    if soup.find("h1"):
        return None

    conv = (options or {}).get("conversion", {}) or {}
    if not conv.get("detect_document_heading", True):
        return None
    promote_first_text = bool(conv.get("heading_promote_first_text", True))
    promote_subheading = bool(conv.get("heading_promote_subheading", True))

    title_el = soup.find("title")
    title = normalize_text(title_el.get_text() if title_el else "")
    title_ok = bool(title) and is_usable_heading_text(title)

    def _retag(el) -> str:
        el.name = "h1"
        # Promoting out of an h2/h3 document empties the h2 rung, which the
        # audit reads as a skipped level — a 1.3.1 failure created by the fix.
        close_html_heading_level_gaps(soup)
        return normalize_text(el.get_text(" ", strip=True))

    subheads = soup.find_all(re.compile(r"^h[2-6]$"))
    if subheads:
        if not promote_subheading:
            return None
        if title_ok:
            for h in subheads[:HEADING_CANDIDATE_WINDOW]:
                if normalize_text(h.get_text(" ", strip=True)).casefold() == title.casefold():
                    return _retag(h)
        for h in subheads[:HEADING_CANDIDATE_WINDOW]:
            text = normalize_text(h.get_text(" ", strip=True))
            if is_usable_heading_text(text):
                return _retag(h)
        return None

    body = soup.find("body") or soup
    candidates = []
    for el in body.find_all(["p", "div"]):
        if el.find_parent(["header", "footer", "nav"]):
            continue
        if el.find(["p", "div"]):  # container: its leaf blocks come later
            continue
        text = normalize_text(el.get_text(" ", strip=True))
        if text:
            candidates.append((el, text))
            if len(candidates) >= HEADING_CANDIDATE_WINDOW:
                break

    if title_ok:
        for el, text in candidates:
            if text.casefold() == title.casefold():
                return _retag(el)
    if promote_first_text:
        for el, text in candidates:
            if is_usable_heading_text(text):
                return _retag(el)
    return None


def remediate_missing_h1(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Promote existing content to h1. Never inserts text."""
    promoted = promote_document_h1(soup, args[0] if args else None)
    if promoted:
        return f"Promoted existing text to h1: {promoted}"
    return None


def remediate_missing_headings(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Convert bold paragraphs or short divs to headings."""
    converted = 0

    # Find bold paragraphs that look like headings
    for p in soup.find_all("p"):
        text = p.get_text(strip=True)
        if not text or len(text) > 100:
            continue

        # Check if entire paragraph is bold
        b_tag = p.find(["b", "strong"])
        if b_tag and b_tag.get_text(strip=True) == text:
            # Determine heading level from context
            prev_heading = p.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
            if prev_heading:
                level = int(prev_heading.name[1])
                new_level = min(level + 1, 6)
            else:
                new_level = 2 if soup.find("h1") else 1

            p.name = f"h{new_level}"
            # Remove the bold wrapper
            if b_tag:
                b_tag.unwrap()
            converted += 1

    if converted:
        return f"Converted {converted} bold paragraphs to headings"

    promoted = promote_document_h1(soup, args[0] if args else None)
    if promoted:
        return f"Promoted paragraph to h1: {promoted}"
    return None


def remediate_skipped_heading_level(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Fix skipped heading levels by inserting intermediate headings."""
    headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    if not headings:
        return None

    fixed = 0
    prev_level = 0
    for heading in headings:
        level = int(heading.name[1])
        if prev_level > 0 and level > prev_level + 1:
            # Insert intermediate heading(s)
            for intermediate in range(prev_level + 1, level):
                new_heading = soup.new_tag(f"h{intermediate}")
                new_heading.string = heading.get_text(strip=True)
                new_heading["class"] = ["sr-only"]
                heading.insert_before(new_heading)
                fixed += 1
        prev_level = level

    if fixed:
        return f"Inserted {fixed} intermediate headings to fix skipped levels"
    return None


def remediate_empty_heading_content(
    soup: BeautifulSoup, issue: Dict[str, Any], *args
) -> Optional[str]:
    """Fill empty headings with text derived from context."""
    fixed = 0
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if heading.get_text(strip=True):
            continue

        # Try to derive text from context
        next_p = heading.find_next("p")
        if next_p and next_p.get_text(strip=True):
            text = next_p.get_text(strip=True)[:60]
            heading.string = text
            fixed += 1
        else:
            # Remove empty heading to avoid confusing screen readers
            heading.decompose()
            fixed += 1

    if fixed:
        return f"Fixed {fixed} empty headings"
    return None


def _looks_like_filename(text: str) -> bool:
    """True if ``text`` looks like a filename slug rather than a real title.

    Filenames have no whitespace and use underscores/hyphens as word
    separators. Underscores are the strongest signal (real titles essentially
    never contain them); a no-space string equal to the source stem is also
    treated as a filename by the caller.
    """
    if not text:
        return False
    if re.search(r"\s", text):
        return False
    return "_" in text


def _is_id_token(token: str) -> bool:
    """True if ``token`` looks like a hash/uuid/id fragment, not a real word.

    Heuristic: a hexadecimal run (length >= 4) containing at least one digit —
    e.g. ``a1b2c3d4``, ``2b852a72``. Restricting to hex avoids dropping real
    words that merely contain a digit (e.g. ``November11event``).
    """
    t = token.lower()
    return (
        len(token) >= 4
        and all(c in "0123456789abcdef" for c in t)
        and any(c.isdigit() for c in token)
    )


# Trailing tokens that are file-format noise, not part of a title.
_FORMAT_TOKENS = {"pdf", "html", "htm", "doc", "docx", "txt"}


def _humanize_filename_stem(stem: str) -> Optional[str]:
    """Derive a human-readable title from a source filename stem.

    URL-decodes the stem (uploaded names often carry ``%20``), strips leading id-like
    tokens (hash/uuid prefixes) and trailing format tokens, splits camelCase and
    long concatenated runs (``wordninja`` when available), and title-cases the
    result — preserving acronyms. Returns ``None`` when nothing descriptive
    remains (e.g. an id-only stem) so callers never fabricate a meaningless title.

    Example: ``a1b2c3d4_annualreportform`` -> ``Annual Report Form``.
    """
    from urllib.parse import unquote

    s = unquote(stem or "").strip()
    s = re.sub(r"\.[A-Za-z0-9]{1,4}$", "", s)        # drop a trailing extension
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)        # split camelCase boundaries

    tokens = [t for t in re.split(r"[_\-\s.]+", s) if t]

    # Drop leading id-like (hash) tokens; stop at the first real word.
    while tokens and _is_id_token(tokens[0]):
        tokens.pop(0)
    # Drop trailing file-format tokens (e.g. a "-pdf" suffix).
    while tokens and tokens[-1].lower() in _FORMAT_TOKENS:
        tokens.pop()
    if not tokens:
        return None

    try:
        import wordninja

        split = wordninja.split
    except ImportError:
        split = None

    words = []
    for token in tokens:
        # Only break up long concatenated runs; leave acronyms ("NASA"), already
        # cased words, and short tokens ("src") intact so wordninja can't shred
        # them into "W Mu" / "S rc".
        if split and len(token) >= 10:
            parts = split(token.lower()) or [token]
        else:
            parts = [token]
        words.extend(parts)

    if not words:
        return None
    # Capitalize each word but preserve all-caps acronyms.
    return " ".join(w if w.isupper() else w[:1].upper() + w[1:] for w in words)


def _first_heading_text(
    soup: BeautifulSoup, exclude_stem: str = ""
) -> Optional[str]:
    """Return the text of the first heading that does not look like a filename slug.

    ``exclude_stem`` is an optional source-stem string; headings whose text
    exactly matches it are also skipped (handles hyphen-only stems such as
    ``doc-2024`` that contain no underscore but are still filename slugs).
    """
    body = soup.find("body") or soup
    for h in body.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        text = h.get_text(strip=True)
        if not text:
            continue
        if _looks_like_filename(text):
            continue
        if exclude_stem and text == exclude_stem:
            continue
        return text
    return None


def remediate_descriptive_title(
    soup: BeautifulSoup,
    struct_data: Optional[Dict[str, Any]] = None,
    source_stem: str = "",
) -> Optional[str]:
    """Replace a filename-like <title>/<h1> with a descriptive document title."""
    title_el = soup.find("title")
    current = title_el.get_text(strip=True) if title_el else ""

    is_filename = _looks_like_filename(current) or (
        current and source_stem and current == source_stem
    )
    if not is_filename:
        return None

    # Source 1: Docling metadata title, when it is prose (has a space).
    new_title: Optional[str] = None
    meta_title = ((struct_data or {}).get("metadata") or {}).get("title") or ""
    meta_title = meta_title.strip()
    if meta_title and " " in meta_title and not _looks_like_filename(meta_title):
        new_title = meta_title
    # Source 2: first descriptive content heading.
    if not new_title:
        new_title = _first_heading_text(soup, exclude_stem=source_stem)
    # Source 3: last resort — humanize the source filename stem itself.
    if not new_title:
        new_title = _humanize_filename_stem(source_stem)

    if not new_title:
        return None

    if title_el:
        title_el.string = new_title
    h1 = soup.find("h1")
    if h1:
        h1_text = h1.get_text(strip=True)
        h1_is_filename = _looks_like_filename(h1_text) or (
            h1_text and source_stem and h1_text == source_stem
        )
        if h1_is_filename:
            h1.string = new_title
    return f"Set descriptive document title: {new_title}"


_SECTION_ANCHOR = re.compile(
    r"^(Level|Part|Section|Chapter|Unit|Module|Phase|Step)\s+([IVXLC]+|\d+)\b",
    re.IGNORECASE,
)


def remediate_flat_heading_nesting(soup: BeautifulSoup) -> Optional[str]:
    """Demote flat sub-section headings under recognizable section anchors.

    Conservative: only acts when >=2 headings at the same level match a
    section-anchor word (Level/Part/Section/Chapter/Unit/Module/Phase/Step).
    Non-anchor headings at that level appearing after the first anchor are
    demoted one level. Headings before the first anchor (the title block) and
    at other levels are left untouched. No anchor pattern => no-op.
    """
    headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    if not headings:
        return None

    # Find the heading level that carries the anchors.
    anchor_levels = [
        int(h.name[1])
        for h in headings
        if _SECTION_ANCHOR.match(h.get_text(strip=True))
    ]
    if len(anchor_levels) < 2:
        return None
    section_level = min(anchor_levels)  # if anchors span tiers, act only on the outermost

    demoted = 0
    seen_first_anchor = False
    for h in headings:
        level = int(h.name[1])
        if level != section_level:
            continue
        is_anchor = bool(_SECTION_ANCHOR.match(h.get_text(strip=True)))
        if is_anchor:
            seen_first_anchor = True
            continue
        if seen_first_anchor:
            new_level = min(section_level + 1, 6)
            if new_level != level:
                h.name = f"h{new_level}"
                demoted += 1

    if demoted:
        return f"Demoted {demoted} sub-section heading(s) under section anchors"
    return None
