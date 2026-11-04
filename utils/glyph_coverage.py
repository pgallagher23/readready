# SPDX-License-Identifier: AGPL-3.0-or-later
"""Glyph coverage checks for TrueType/OpenType fonts.

These helpers are intentionally standalone. They do not modify PDFs or HTML;
they answer whether a candidate font can represent the Unicode text that the
pipeline may need to write into an accessible output PDF.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Optional, Set

from fontTools.ttLib import TTFont


@dataclass(frozen=True)
class MissingGlyph:
    """A character that is not covered by a font cmap."""

    character: str
    codepoint: int
    name: str


@dataclass(frozen=True)
class GlyphCoverage:
    """Coverage result for one font and text sample."""

    font_path: str
    text_length: int
    unique_codepoints: int
    covered_codepoints: int
    missing: List[MissingGlyph]

    @property
    def missing_count(self) -> int:
        return len(self.missing)

    @property
    def coverage_ratio(self) -> float:
        if self.unique_codepoints == 0:
            return 1.0
        return self.covered_codepoints / self.unique_codepoints

    @property
    def fully_covered(self) -> bool:
        return not self.missing


def supported_codepoints(font_path: str, *, font_number: int = 0) -> Set[int]:
    """Return Unicode codepoints advertised by a font's cmap tables."""
    return set(_cached_supported_codepoints(str(Path(font_path)), font_number))


def check_glyph_coverage(
    font_path: str,
    text: str,
    *,
    font_number: int = 0,
    ignore_whitespace: bool = True,
) -> GlyphCoverage:
    """Check whether a font covers every unique character in ``text``."""
    codepoints = _text_codepoints(text, ignore_whitespace=ignore_whitespace)
    supported = supported_codepoints(font_path, font_number=font_number)
    missing = [
        MissingGlyph(
            character=chr(cp),
            codepoint=cp,
            name=f"U+{cp:04X}",
        )
        for cp in sorted(codepoints - supported)
    ]
    return GlyphCoverage(
        font_path=str(font_path),
        text_length=len(text),
        unique_codepoints=len(codepoints),
        covered_codepoints=len(codepoints) - len(missing),
        missing=missing,
    )


def find_missing_glyphs(
    font_path: str,
    text: str,
    *,
    font_number: int = 0,
    ignore_whitespace: bool = True,
) -> List[MissingGlyph]:
    """Return only the missing glyph records for a text sample."""
    return check_glyph_coverage(
        font_path,
        text,
        font_number=font_number,
        ignore_whitespace=ignore_whitespace,
    ).missing


def supports_text(
    font_path: str,
    text: str,
    *,
    font_number: int = 0,
    min_coverage: float = 1.0,
) -> bool:
    """Return true when ``font_path`` reaches the requested text coverage."""
    coverage = check_glyph_coverage(font_path, text, font_number=font_number)
    return coverage.coverage_ratio >= min_coverage


def format_missing_glyphs(
    missing: Iterable[MissingGlyph],
    *,
    limit: int = 20,
) -> str:
    """Format missing glyphs for logs or reports."""
    items = list(missing)
    shown = items[:limit]
    parts = [f"{item.name} {item.character!r}" for item in shown]
    if len(items) > limit:
        parts.append(f"... {len(items) - limit} more")
    return ", ".join(parts)


def _text_codepoints(text: str, *, ignore_whitespace: bool) -> Set[int]:
    codepoints = set()
    for char in text or "":
        if ignore_whitespace and char.isspace():
            continue
        if ord(char) < 32:
            continue
        codepoints.add(ord(char))
    return codepoints


@lru_cache(maxsize=512)
def _cached_supported_codepoints(font_path: str, font_number: int) -> frozenset[int]:
    path = Path(font_path)
    if not path.exists():
        raise FileNotFoundError(f"Font not found: {font_path}")

    font = TTFont(str(path), fontNumber=font_number, lazy=True)
    try:
        codepoints: Set[int] = set()
        for table in font["cmap"].tables:
            if table.isUnicode():
                codepoints.update(table.cmap.keys())
        return frozenset(codepoints)
    finally:
        font.close()
