# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve requested fonts to installed fonts with glyph coverage checks."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Optional, Sequence

from utils.font_inventory import FontInfo, normalize_font_key, scan_font_inventory
from utils.glyph_coverage import GlyphCoverage, check_glyph_coverage


SUBSET_PREFIX_RE = re.compile(r"^[A-Z]{6}\+")

FALLBACK_FAMILY_ORDER = (
    "Noto Sans",
    "Noto Serif",
    "Noto Sans Symbols 2",
    "Noto Sans Symbols",
    "Noto Sans Math",
    "Noto Sans CJK",
    "Source Sans 3",
    "Open Sans",
    "DejaVu Sans",
    "Liberation Sans",
    "Arial",
    "Helvetica",
)

PDF_FONT_ALIASES = {
    "arialmt": "arial",
    "arialboldmt": "arial bold",
    "arialitalicmt": "arial italic",
    "arialbolditalicmt": "arial bold italic",
    "helvetica": "helvetica",
    "helveticabold": "helvetica bold",
    "helveticaoblique": "helvetica italic",
    "timesnewromanpsmt": "times new roman",
    "timesnewromanpsboldmt": "times new roman bold",
    "timesroman": "times new roman",
    "couriernewpsmt": "courier new",
    "courier": "courier new",
}


@dataclass(frozen=True)
class FontResolution:
    """Result of selecting a concrete font for a requested family/text."""

    requested_family: str
    normalized_request: str
    selected: Optional[FontInfo]
    coverage: Optional[GlyphCoverage]
    substituted: bool
    reason: str
    candidates_considered: int

    def to_dict(self) -> Dict[str, object]:
        data = asdict(self)
        if self.selected:
            data["selected"] = self.selected.to_dict()
        if self.coverage:
            data["coverage"] = {
                "font_path": self.coverage.font_path,
                "text_length": self.coverage.text_length,
                "unique_codepoints": self.coverage.unique_codepoints,
                "covered_codepoints": self.coverage.covered_codepoints,
                "missing_count": self.coverage.missing_count,
                "coverage_ratio": round(self.coverage.coverage_ratio, 4),
                "missing": [
                    {
                        "character": item.character,
                        "codepoint": item.codepoint,
                        "name": item.name,
                    }
                    for item in self.coverage.missing[:50]
                ],
            }
        return data


def resolve_font(
    requested_family: str,
    text: str,
    *,
    requested_style: str = "Regular",
    fonts: Optional[Sequence[FontInfo]] = None,
    font_dirs: Optional[Sequence[str]] = None,
    min_coverage: float = 1.0,
) -> FontResolution:
    """Choose the best installed font for ``requested_family`` and ``text``."""
    inventory = list(fonts) if fonts is not None else scan_font_inventory(font_dirs)
    normalized_request = normalize_requested_font(requested_family)
    preferred_style = _style_key(requested_style)

    ranked = sorted(
        inventory,
        key=lambda font: _candidate_sort_key(font, normalized_request, preferred_style),
        reverse=True,
    )

    best_font: Optional[FontInfo] = None
    best_coverage: Optional[GlyphCoverage] = None
    for font in ranked:
        try:
            coverage = check_glyph_coverage(
                font.path,
                text,
                font_number=font.font_number,
            )
        except Exception:
            continue
        if best_coverage is None or coverage.coverage_ratio > best_coverage.coverage_ratio:
            best_font = font
            best_coverage = coverage
        if coverage.coverage_ratio >= min_coverage:
            selected_key = normalize_font_key(font.family)
            substituted = selected_key != normalized_request
            reason = "exact_or_alias_match" if not substituted else "fallback_with_required_coverage"
            return FontResolution(
                requested_family=requested_family,
                normalized_request=normalized_request,
                selected=font,
                coverage=coverage,
                substituted=substituted,
                reason=reason,
                candidates_considered=len(ranked),
            )

    return FontResolution(
        requested_family=requested_family,
        normalized_request=normalized_request,
        selected=best_font,
        coverage=best_coverage,
        substituted=True,
        reason="no_font_met_minimum_coverage" if best_font else "no_usable_fonts_found",
        candidates_considered=len(ranked),
    )


def normalize_requested_font(name: str) -> str:
    """Normalize PDF/CSS font names for matching against installed faces."""
    cleaned = SUBSET_PREFIX_RE.sub("", name or "")
    cleaned = cleaned.replace("-", " ").replace("_", " ").strip()
    alias_key = normalize_font_key(cleaned)
    aliased = PDF_FONT_ALIASES.get(alias_key, cleaned)
    return normalize_font_key(aliased)


def rank_fonts_for_text(
    text: str,
    *,
    fonts: Optional[Sequence[FontInfo]] = None,
    font_dirs: Optional[Sequence[str]] = None,
    limit: int = 20,
) -> List[Dict[str, object]]:
    """Return fonts ranked by glyph coverage for diagnostics."""
    inventory = list(fonts) if fonts is not None else scan_font_inventory(font_dirs)
    ranked: List[Dict[str, object]] = []
    for font in inventory:
        try:
            coverage = check_glyph_coverage(
                font.path,
                text,
                font_number=font.font_number,
            )
        except Exception:
            continue
        ranked.append({
            "font": font.to_dict(),
            "coverage_ratio": round(coverage.coverage_ratio, 4),
            "missing_count": coverage.missing_count,
        })
    ranked.sort(key=lambda item: (item["coverage_ratio"], -item["missing_count"]), reverse=True)
    return ranked[:limit]


def _candidate_sort_key(
    font: FontInfo,
    normalized_request: str,
    preferred_style: str,
) -> tuple[int, int, int, str]:
    family_key = normalize_font_key(font.family)
    full_key = normalize_font_key(font.full_name)
    postscript_key = normalize_font_key(font.postscript_name)
    fallback_keys = [normalize_font_key(name) for name in FALLBACK_FAMILY_ORDER]

    name_score = 0
    if normalized_request and normalized_request in {family_key, full_key, postscript_key}:
        name_score = 100
    elif normalized_request and (
        normalized_request in family_key
        or family_key in normalized_request
        or normalized_request in full_key
    ):
        name_score = 80
    elif family_key in fallback_keys:
        name_score = 60 - fallback_keys.index(family_key)

    style_score = 1 if preferred_style and preferred_style in _style_key(font.subfamily) else 0
    regular_score = 1 if "regular" in _style_key(font.subfamily) else 0
    return (name_score, style_score, regular_score, font.full_name)


def _style_key(style: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (style or "").lower()))
