# SPDX-License-Identifier: AGPL-3.0-or-later
"""Branding configuration: product name, colors, logo, landing page.

Single source of truth for everything an institution customizes. Read via
``get_branding()``; nothing else in the codebase hard-codes a brand color.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_COLORS: Dict[str, str] = {
    "primary": "#1f2937",
    "primary_dark": "#111827",
    "accent": "#2563eb",
    "neutral": "#9ca3af",
    "warm_bg": "#f8fafc",
}
_KNOWN_KEYS = {
    "product_name", "header_title", "organization", "logo", "logo_alt",
    "colors", "landing_page",
}


class BrandingError(ValueError):
    """Raised for an invalid ``branding`` section; message names the key."""


@dataclass(frozen=True)
class Branding:
    product_name: str = "ReadReady"
    header_title: str = "ReadReady"
    organization: str = ""
    logo: Optional[Path] = None
    logo_alt: str = ""
    colors: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLORS))
    landing_page: Optional[Path] = None

    @property
    def primary(self) -> str:
        return self.colors["primary"]

    @property
    def primary_dark(self) -> str:
        return self.colors["primary_dark"]

    @property
    def accent(self) -> str:
        return self.colors["accent"]

    @property
    def neutral(self) -> str:
        return self.colors["neutral"]

    @property
    def warm_bg(self) -> str:
        return self.colors["warm_bg"]


def branding_from_config(section: Optional[dict], root: Path = REPO_ROOT) -> Branding:
    section = dict(section or {})
    unknown = set(section) - _KNOWN_KEYS
    if unknown:
        raise BrandingError(f"branding: unknown key(s) {sorted(unknown)}")
    branding_dir = root / "static" / "branding"

    colors = dict(DEFAULT_COLORS)
    for key, value in (section.get("colors") or {}).items():
        if key not in DEFAULT_COLORS:
            raise BrandingError(f"branding.colors.{key}: unknown color key")
        if not isinstance(value, str) or not _HEX.match(value):
            raise BrandingError(f"branding.colors.{key}: expected #RRGGBB, got {value!r}")
        colors[key] = value

    logo: Optional[Path] = None
    if section.get("logo"):
        candidate = branding_dir / section["logo"]
        logo = candidate if candidate.is_file() else None

    landing: Optional[Path] = None
    if section.get("landing_page"):
        lp = Path(section["landing_page"])
        lp = lp if lp.is_absolute() else root / lp
        landing = lp if lp.is_file() else None

    return Branding(
        product_name=str(section.get("product_name") or "ReadReady"),
        header_title=str(section.get("header_title") or "ReadReady"),
        organization=str(section.get("organization") or ""),
        logo=logo,
        logo_alt=str(section.get("logo_alt") or ""),
        colors=colors,
        landing_page=landing,
    )


_cached: Optional[Branding] = None


def get_branding() -> Branding:
    """Branding from config.yaml, loaded once per process."""
    global _cached
    if _cached is None:
        from utils.config_loader import get_section
        _cached = branding_from_config(get_section("branding"))
    return _cached
