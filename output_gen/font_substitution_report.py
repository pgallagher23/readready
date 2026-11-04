# SPDX-License-Identifier: AGPL-3.0-or-later
"""Diagnostic report for broken-font substitutions.

When the tagger replaces a broken ``.notdef`` font (e.g. ``NotDefSpecial``)
with a coverage-resolved fallback, this report records what was requested,
what was selected, and which glyphs (if any) the fallback could not cover.

It consumes :class:`utils.font_resolver.FontResolution` objects directly and
emits a JSON + Markdown pair alongside the tagged PDF.  It is a debug artifact
only — it does not gate the verdict.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class FontSubstitutionRecord:
    """One requested-font → selected-font substitution decision."""

    requested: str
    selected: Optional[str]
    selected_path: Optional[str]
    substituted: bool
    reason: str
    coverage_ratio: float
    missing_glyphs: List[str] = field(default_factory=list)
    scope: str = "document"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested": self.requested,
            "selected": self.selected,
            "selected_path": self.selected_path,
            "substituted": self.substituted,
            "reason": self.reason,
            "coverage_ratio": round(self.coverage_ratio, 4),
            "fully_covered": not self.missing_glyphs,
            "missing_glyphs": self.missing_glyphs,
        }


class FontSubstitutionReport:
    """Accumulates :class:`FontResolution`s and writes JSON + Markdown."""

    def __init__(self) -> None:
        self.records: List[FontSubstitutionRecord] = []

    def add(self, resolution: Any, *, scope: str = "document") -> None:
        """Record a ``FontResolution`` (from ``resolve_font``)."""
        sel = getattr(resolution, "selected", None)
        cov = getattr(resolution, "coverage", None)
        missing = []
        coverage_ratio = 1.0
        if cov is not None:
            coverage_ratio = cov.coverage_ratio
            missing = [f"{m.name} {m.character!r}" for m in cov.missing]
        self.records.append(FontSubstitutionRecord(
            requested=getattr(resolution, "requested_family", ""),
            selected=sel.full_name if sel is not None else None,
            selected_path=sel.path if sel is not None else None,
            substituted=bool(getattr(resolution, "substituted", False)),
            reason=getattr(resolution, "reason", ""),
            coverage_ratio=coverage_ratio,
            missing_glyphs=missing,
            scope=scope,
        ))

    def has_substitutions(self) -> bool:
        return bool(self.records)

    def has_residual_missing(self) -> bool:
        return any(r.missing_glyphs for r in self.records)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "font_substitutions": [r.to_dict() for r in self.records],
            "summary": {
                "total": len(self.records),
                "substituted": sum(1 for r in self.records if r.substituted),
                "with_residual_missing": sum(1 for r in self.records if r.missing_glyphs),
            },
        }

    def _to_markdown(self) -> str:
        lines = ["# Font Substitution Report", ""]
        if not self.records:
            lines.append("_No broken fonts required substitution._")
            return "\n".join(lines) + "\n"
        lines += [
            "| Requested | Selected | Substituted | Reason | Coverage | Missing glyphs |",
            "|---|---|---|---|---|---|",
        ]
        for r in self.records:
            missing = ", ".join(r.missing_glyphs[:10]) if r.missing_glyphs else "—"
            if len(r.missing_glyphs) > 10:
                missing += f" … (+{len(r.missing_glyphs) - 10})"
            lines.append(
                f"| {r.requested or '—'} | {r.selected or '—'} | "
                f"{'yes' if r.substituted else 'no'} | {r.reason} | "
                f"{r.coverage_ratio * 100:.1f}% | {missing} |"
            )
        return "\n".join(lines) + "\n"

    def write(self, output_dir, stem: str) -> Tuple[Path, Path]:
        """Write ``<stem>_font_substitution.{json,md}`` into ``output_dir``.

        Returns the (json_path, md_path) pair.
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        json_path = out / f"{stem}_font_substitution.json"
        md_path = out / f"{stem}_font_substitution.md"
        json_path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        md_path.write_text(self._to_markdown(), encoding="utf-8")
        return json_path, md_path
