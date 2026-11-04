# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inventory installed TrueType/OpenType fonts."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from fontTools.ttLib import TTCollection, TTFont

from utils.font_dir import resolve_font_dir


FONT_EXTENSIONS = {".ttf", ".otf", ".ttc", ".otc"}


_SYSTEM_FONT_DIRS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
    str(Path.home() / ".local/share/fonts"),
    str(Path.home() / ".fonts"),
)


def default_font_dirs() -> tuple[str, ...]:
    return (str(resolve_font_dir()),) + _SYSTEM_FONT_DIRS


@dataclass(frozen=True)
class FontInfo:
    """Metadata for one font face."""

    family: str
    subfamily: str
    full_name: str
    postscript_name: str
    path: str
    format: str
    font_number: int = 0

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def scan_font_inventory(
    font_dirs: Optional[Sequence[str]] = None,
    *,
    include_system_fonts: bool = True,
) -> List[FontInfo]:
    """Scan font directories and return normalized font face metadata."""
    dirs = list(font_dirs or [])
    if include_system_fonts:
        dirs.extend(default_font_dirs())

    seen_paths: set[Path] = set()
    fonts: List[FontInfo] = []
    for path in _iter_font_files(dirs):
        resolved = path.resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        fonts.extend(_read_font_faces(resolved))

    fonts.sort(key=lambda item: (item.family.lower(), item.subfamily.lower(), item.path))
    return fonts


def font_inventory_by_family(fonts: Iterable[FontInfo]) -> Dict[str, List[FontInfo]]:
    """Group fonts by normalized family name."""
    grouped: Dict[str, List[FontInfo]] = {}
    for font in fonts:
        grouped.setdefault(normalize_font_key(font.family), []).append(font)
    return grouped


def normalize_font_key(name: str) -> str:
    """Normalize a font name for matching."""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _iter_font_files(font_dirs: Sequence[str]) -> Iterable[Path]:
    for raw_dir in font_dirs:
        if not raw_dir:
            continue
        root = Path(os.path.expanduser(raw_dir))
        if not root.exists():
            continue
        if root.is_file() and root.suffix.lower() in FONT_EXTENSIONS:
            yield root
            continue
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in FONT_EXTENSIONS:
                yield path


def _read_font_faces(path: Path) -> List[FontInfo]:
    suffix = path.suffix.lower()
    try:
        if suffix in {".ttc", ".otc"}:
            collection = TTCollection(str(path), lazy=True)
            try:
                return [
                    _font_info_from_ttfont(font, path, index)
                    for index, font in enumerate(collection.fonts)
                ]
            finally:
                for font in collection.fonts:
                    font.close()
        font = TTFont(str(path), lazy=True)
        try:
            return [_font_info_from_ttfont(font, path, 0)]
        finally:
            font.close()
    except Exception:
        return []


def _font_info_from_ttfont(font: TTFont, path: Path, font_number: int) -> FontInfo:
    family = _name(font, 1) or path.stem
    subfamily = _name(font, 2) or "Regular"
    full_name = _name(font, 4) or f"{family} {subfamily}".strip()
    postscript_name = _name(font, 6) or ""
    return FontInfo(
        family=family,
        subfamily=subfamily,
        full_name=full_name,
        postscript_name=postscript_name,
        path=str(path),
        format=path.suffix.lower().lstrip("."),
        font_number=font_number,
    )


def _name(font: TTFont, name_id: int) -> str:
    name_table = font.get("name")
    if not name_table:
        return ""
    for record in name_table.names:
        if record.nameID != name_id:
            continue
        try:
            value = record.toUnicode().strip()
        except Exception:
            continue
        if value:
            return value
    return ""
