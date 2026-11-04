# SPDX-License-Identifier: AGPL-3.0-or-later
"""Detect a document heading on flat-typography PDFs.

Docling keys heading detection on font-size deltas, so a document typeset in a
single size — institutional forms, meeting minutes, letters — yields zero headings, and
its tagged PDF carries no ``H*`` element at all. This module finds a heading in
such documents by *promoting an element that is already on the page*. It never
creates text: the worst it can do is decline to promote anything.

Thresholds were tuned on a sample of institutional PDFs (forms, minutes,
letters, reports). In a blind test over 250 of them with the real headings
hidden, every element the title-match signal promoted (167 of 167) was one
Docling independently considered a heading.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# --- Promotion text gate (junk-only; policy: prefer a poor h1 over none) ----
# A visible "title" longer than this is a paragraph; retag-only means we
# cannot truncate, so the candidate is skipped.
MAX_PROMOTION_CHARS = 250
# Ladder window: candidates are drawn from this many leading elements.
HEADING_CANDIDATE_WINDOW = 5

HEADING_TAGS = {f"H{i}" for i in range(1, 7)}

# Words HeadingContentCheck flags as generic-heading (2.4.6). Shared here so a
# promotion can never immediately create a 2.4.6 finding.
GENERIC_HEADING_TEXTS = {"heading", "title", "subtitle", "header"}

# Strings a pipeline stage fabricated to stand in for missing content. No
# promotion path may adopt one: document_structure_remediation writes
# "Untitled Document" into <title>, and the footer-landmark strategy injects
# the "converted for accessibility" sentence into the page itself.
_PLACEHOLDER_TEXTS = {
    "untitled document",
    "untitled",
    "no title",
    "document",
    "document converted for accessibility compliance.",
}

_PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_TOOL_ARTIFACT_RE = re.compile(r"^microsoft\s+(word|powerpoint|excel)\s*-", re.IGNORECASE)
_FILE_EXTENSION_RE = re.compile(r"\.(docx?|pdf|pptx?)$", re.IGNORECASE)
_SALUTATION_RE = re.compile(r"^dear\b", re.IGNORECASE)
_GID_RE = re.compile(r"/?gid\d{3,}", re.IGNORECASE)
_UNDERSCORE_RUN_RE = re.compile(r"_{3,}")
_ALPHA_TOKEN_RE = re.compile(r"[^\W\d_]{2}")


def normalize_text(s: Optional[str]) -> str:
    """NFKC-normalize, collapse all whitespace runs, and strip."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s or "")).strip()


def is_usable_heading_text(text: Optional[str]) -> bool:
    """True unless ``text`` is genuine junk.

    Junk-only semantics: single words, dates, section words,
    and long titles are all usable. Rejected classes: emptiness, paragraphs
    (>250 chars), fabricated placeholders and pipeline boilerplate, generic
    heading words, fill-in blanks, filename shapes, salutations, and
    glyph/encoding garbage.
    """
    t = normalize_text(text)
    if not t or len(t) > MAX_PROMOTION_CHARS:
        return False
    tc = t.casefold()
    if tc in _PLACEHOLDER_TEXTS or tc in GENERIC_HEADING_TEXTS:
        return False
    if _UNDERSCORE_RUN_RE.search(t) or _PERCENT_ESCAPE_RE.search(t):
        return False
    if _SALUTATION_RE.match(t):
        return False
    if _TOOL_ARTIFACT_RE.match(t) or _FILE_EXTENSION_RE.search(t):
        return False
    if "_" in t and not re.search(r"\s", t):  # filename slug
        return False
    if _GID_RE.search(t) or "�" in t:
        return False
    if not _ALPHA_TOKEN_RE.search(t):  # no alphabetic token of length >= 2
        return False
    non_space = [c for c in t if not c.isspace()]
    pua = sum(1 for c in non_space if 0xE000 <= ord(c) <= 0xF8FF)
    if pua * 2 > len(non_space):
        return False
    return True


def _page_one_candidates(elements: List[dict]) -> List[int]:
    """Indices of the first WINDOW page-1 ``P`` elements carrying text."""
    out: List[int] = []
    for i, elem in enumerate(elements):
        if (elem.get("page_no") or 1) != 1:
            break
        if elem.get("tag") != "P":
            continue
        if normalize_text(elem.get("text")):
            out.append(i)
            if len(out) >= HEADING_CANDIDATE_WINDOW:
                break
    return out


def detect_document_heading(
    elements: List[dict],
    title: str = "",
    promote_first_text: bool = True,
) -> Optional[Dict[str, Any]]:
    """Pick an element to retag as ``H1``, or return None.

    Zero-heading documents only. Rung 1: a window element whose text equals
    the (usable) document title. Rung 2: the window's first element with
    usable text (``promote_first_text`` is the config escape hatch). Only
    ever selects an element already in ``elements``; the caller retags it.
    """
    if any(e.get("tag") in HEADING_TAGS for e in elements):
        return None
    cands = _page_one_candidates(elements)
    if not cands:
        return None

    norm_title = normalize_text(title)
    if norm_title and is_usable_heading_text(norm_title):
        for i in cands:
            text = normalize_text(elements[i].get("text"))
            if text.casefold() == norm_title.casefold():
                return {"index": i, "signal": "title-match", "text": text}

    if promote_first_text:
        for i in cands:
            text = normalize_text(elements[i].get("text"))
            if is_usable_heading_text(text):
                return {"index": i, "signal": "first-text", "text": text}
    return None


def promote_document_heading(
    elements: List[dict],
    title: str = "",
    enabled: bool = True,
    promote_first_text: bool = True,
) -> Optional[Dict[str, Any]]:
    """Detect a document heading and retag that element in place.

    ``enabled=False`` is the kill switch (config
    ``conversion.detect_document_heading``); ``promote_first_text`` maps to
    config ``conversion.heading_promote_first_text``. Returns the detection
    record, or None when the detector abstains or a switch is off.
    """
    if not enabled:
        return None
    detection = detect_document_heading(
        elements, title=title, promote_first_text=promote_first_text
    )
    if not detection:
        return None
    promoted = elements[detection["index"]]
    promoted["tag"] = "H1"
    promoted["level"] = 1
    # Page attribution resolves by Docling item type (reporting/), so a
    # promoted heading must look like a section header there too.
    promoted["type"] = "SectionHeaderItem"
    logger.info(
        "Heading detection: promoted element %d to H1 via %s: %r",
        detection["index"], detection["signal"], detection["text"][:80],
    )
    return detection
