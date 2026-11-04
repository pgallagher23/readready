# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve an audit issue to the element it refers to.

Audit paths are CSS selectors. Where the element carries a stamped id the path
is a direct reference (``th[data-a11y-id="a11y-e00042"]``) and resolution is a
dict lookup; where it does not, the path is positional
(``table:nth-of-type(35) > tbody > tr``) and resolution falls back to
soupsieve, whose ``:nth-of-type`` cost grows with the element's position in
the document.

The positional path is a permanent fallback, not a deprecated one: documents
converted before stamping existed, elements created during remediation, and any
tag outside STAMPED_TAGS all still arrive here with positional paths.
"""
import logging
import re
from typing import Any, Dict, Optional

import soupsieve

from converter.element_stamper import A11Y_ID_ATTR

logger = logging.getLogger(__name__)

_INDEX_ATTR = "_a11y_id_index"

# Matches a trailing 'tag[data-a11y-id="..."]' segment. Anchored to the END
# of the path on purpose: _get_element_path stops at the first ancestor
# carrying a stamped id, so that segment can appear on an ancestor, where it
# names the wrong element.
_TRAILING_ID = re.compile(
    r'(?:^|>)\s*[A-Za-z][\w-]*\[data-a11y-id="([^"]+)"\]\s*$')


def _clean_css_path(path: str) -> str:
    """Strip the auditor's '[document] > ' prefix, which is not valid CSS."""
    return re.sub(r"\[document\]\s*>\s*", "", path or "").strip()


def build_id_index(soup: Any) -> Dict[str, Any]:
    """Map every stamped data-a11y-id in the document to its element. One pass."""
    return {
        el[A11Y_ID_ATTR]: el
        for el in soup.find_all(attrs={A11Y_ID_ATTR: True})
        if el.get(A11Y_ID_ATTR)
    }


def get_id_index(soup: Any) -> Dict[str, Any]:
    """Return the cached id index for this soup, building it on first use."""
    index = getattr(soup, _INDEX_ATTR, None)
    if index is None:
        index = build_id_index(soup)
        setattr(soup, _INDEX_ATTR, index)
    return index


def invalidate_id_index(soup: Any) -> None:
    """Drop the cached index. Call after inserting or removing id'd elements."""
    setattr(soup, _INDEX_ATTR, None)


def resolve_issue_element(
    soup: Any, issue: Dict[str, Any], id_index: Optional[Dict[str, Any]] = None
) -> Optional[Any]:
    """Return the element an issue refers to, or None.

    Order: trailing id via the index, then trailing id via a scan, then the
    positional CSS path.
    """
    loc = issue.get("location") or {}
    path = loc.get("path", "") if isinstance(loc, dict) else ""
    if not path or path == "Unknown path":
        return None

    cleaned = _clean_css_path(path)

    match = _TRAILING_ID.search(cleaned)
    if match:
        key = match.group(1)
        index = id_index if id_index is not None else getattr(soup, _INDEX_ATTR, None)
        if index is not None:
            element = index.get(key)
            # An element a strategy removed is still in the index but no longer
            # in the tree. Checking .parent is O(1) and makes the index
            # self-correcting, so callers never have to invalidate it: a stale
            # entry degrades to the scan below instead of returning a detached
            # element.
            if element is not None and element.parent is not None:
                return element
        # No index, index miss, or a stale entry: scan rather than trust an
        # absence. Elements inserted after the index was built land here.
        element = soup.find(attrs={A11Y_ID_ATTR: key})
        if element is not None:
            return element

    try:
        matches = soup.select(cleaned)
        if matches:
            return matches[0]
    except soupsieve.util.SelectorSyntaxError:
        # Expected: garbage/malformed paths (empty audit context, corrupted
        # location data) produce selectors soupsieve can't parse. Quiet by
        # design -- this is not a bug, just an unresolvable issue.
        logger.debug("Unresolvable CSS path: %s", cleaned)
    except Exception:
        # Anything else (e.g. a real bug in how a path was generated) should
        # be visible in production, not silently swallowed alongside expected
        # misses.
        logger.warning("Unexpected error resolving CSS path: %s", cleaned, exc_info=True)
    return None
