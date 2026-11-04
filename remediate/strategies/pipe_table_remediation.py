# SPDX-License-Identifier: AGPL-3.0-or-later
"""Convert converter-flattened pipe-delimited paragraphs into real tables.

Docling sometimes fails to detect two-column tables and emits each row as a
plain ``<p>`` with the cells joined by a literal ``|`` (e.g.
``<p>BUS 248|Business Communications</p>``). That loses the tabular
relationship (WCAG 1.3.1). This strategy detects runs of such paragraphs and
rebuilds them as a ``<dl>`` (term | description). A table would need column
headers, and the source gives none, so we don't invent them.
"""

import logging
import re

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

logger = logging.getLogger(__name__)

# A left cell that looks like a short code, e.g. "BUS 248", "ISO 9001". Enough
# signal to convert a single pair; otherwise a run needs two or more rows.
_CODE_LIKE = re.compile(r"^[A-Z]{2,4}\s?\d{2,4}[A-Z]?$")


def _pipe_pair(p: Tag) -> tuple[str, str] | None:
    """Return (left, right) if ``p`` is a clean single-pipe pair, else None."""
    text = p.get_text()
    if text.count("|") != 1:
        return None
    left, right = text.split("|", 1)
    left, right = left.strip(), right.strip()
    if not left or not right:
        return None
    return left, right


def _runs_of_pipe_pairs(parent: Tag) -> list[list[Tag]]:
    """Group consecutive ``<p>`` pipe-pair siblings into runs.

    Whitespace-only text nodes do not break a run; any other element does.
    """
    runs: list[list[Tag]] = []
    current: list[Tag] = []
    for child in parent.children:
        if isinstance(child, NavigableString):
            if child.strip():            # non-whitespace text breaks the run
                if current:
                    runs.append(current)
                    current = []
            continue                     # whitespace: keep the run open
        if isinstance(child, Tag) and child.name == "p" and _pipe_pair(child):
            current.append(child)
        else:
            if current:
                runs.append(current)
                current = []
    if current:
        runs.append(current)
    return runs


def _build_dl(soup: BeautifulSoup, rows: list[tuple[str, str]]) -> Tag:
    dl = soup.new_tag("dl")
    for left, right in rows:
        dt = soup.new_tag("dt")
        dt.string = left
        dd = soup.new_tag("dd")
        dd.string = right
        dl.append(dt)
        dl.append(dd)
    return dl


def convert_pipe_delimited_paragraphs(soup: BeautifulSoup) -> str | None:
    """Rebuild pipe-delimited paragraph runs as definition lists.

    Returns a human-readable change description, or None if nothing changed.
    """
    converted = 0
    # Collect runs across every element that has children. Snapshot the parent
    # list first because we mutate the tree as we go.
    parents = [el for el in soup.find_all(True) if el.name not in ("table", "dl")]
    for parent in parents:
        if not parent.parent and parent.name not in ("body", "html"):
            continue  # detached node from an earlier replacement
        for run in _runs_of_pipe_pairs(parent):
            rows = [pair for p in run if (pair := _pipe_pair(p)) is not None]
            all_codes = all(_CODE_LIKE.match(left) for left, _ in rows)
            # Convert when there is enough signal: >=2 rows, or a single row
            # whose left cell is a code.
            if len(rows) < 2 and not all_codes:
                continue
            run[0].insert_before(_build_dl(soup, rows))
            for p in run:
                p.decompose()
            converted += 1

    if converted:
        msg = f"Converted {converted} pipe-delimited paragraph run(s) to definition lists"
        logger.info(msg)
        return msg
    return None
