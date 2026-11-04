#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# scripts/import_fonts.py
"""Import user-owned fonts into the ReadReady fonts directory.

    python scripts/import_fonts.py /mnt/c/Windows/Fonts [/Library/Fonts ...] [--dest fonts] [--dry-run]
    python scripts/import_fonts.py --check [--dest fonts]

Copies .ttf/.otf/.ttc/.otc files, never overwrites, de-duplicates by content,
writes manifest.json into the destination, and prints which metric-matched
families the tagger can now embed. You must own a license for every font you import.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.font_dir import resolve_font_dir  # noqa: E402

EXTS = {".ttf", ".otf", ".ttc", ".otc"}
MACOS_FONT_DIRS = ("/Library/Fonts", "/System/Library/Fonts", "/System/Library/AssetsV2")
STYLE_KEYS = ("regular", "bold", "italic", "bold_italic")
APPLE_SUBDIR = "apple"
MANIFEST_NAME = "manifest.json"
# Mirrors mapToWindowsFontName in PdfAccessibilityTagger.java. Keep in sync.
METRIC_FAMILIES = {
    "Times New Roman": {"regular": "times.ttf", "bold": "timesbd.ttf", "italic": "timesi.ttf", "bold_italic": "timesbi.ttf"},
    "Arial": {"regular": "arial.ttf", "bold": "arialbd.ttf", "italic": "ariali.ttf", "bold_italic": "arialbi.ttf"},
    "Arial Narrow": {"regular": "ARIALN.TTF", "bold": "ARIALNB.TTF", "italic": "ARIALNI.TTF", "bold_italic": "ARIALNBI.TTF"},
    "Arial Black": {"regular": "ariblk.ttf"},
    "Courier New": {"regular": "cour.ttf", "bold": "courbd.ttf", "italic": "couri.ttf", "bold_italic": "courbi.ttf"},
    "Tahoma": {"regular": "tahoma.ttf", "bold": "tahomabd.ttf"},
    "Verdana": {"regular": "verdana.ttf", "bold": "verdanab.ttf", "italic": "verdanai.ttf", "bold_italic": "verdanaz.ttf"},
    "Calibri": {"regular": "calibri.ttf", "bold": "calibrib.ttf", "italic": "calibrii.ttf", "bold_italic": "calibriz.ttf"},
    "Georgia": {"regular": "georgia.ttf", "bold": "georgiab.ttf", "italic": "georgiai.ttf", "bold_italic": "georgiaz.ttf"},
    "Trebuchet MS": {"regular": "trebuc.ttf", "bold": "trebucbd.ttf", "italic": "trebucit.ttf", "bold_italic": "trebucbi.ttf"},
    "Cambria": {"bold": "cambriab.ttf", "italic": "cambriai.ttf"},
    "Book Antiqua": {"regular": "BKANT.TTF"},
    "Symbol": {"regular": "symbol.ttf"},
    "Wingdings": {"regular": "wingding.ttf"},
}


@dataclass
class ImportReport:
    copied: list = field(default_factory=list)
    skipped_duplicate: list = field(default_factory=list)
    skipped_existing: list = field(default_factory=list)


def _font_names(path: Path) -> tuple[str, str]:
    """(family, subfamily) via fontTools; ('', '') on failure."""
    try:
        from fontTools.ttLib import TTCollection, TTFont
        f = TTCollection(path).fonts[0] if path.suffix.lower() in {".ttc", ".otc"} else TTFont(path)
        name = f["name"]
        return (name.getDebugName(1) or "", name.getDebugName(2) or "")
    except Exception:
        return ("", "")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_manifest(dest: Path) -> tuple[list[dict], int]:
    """Return (live entries, dropped count). Entries whose file is gone or whose record
    lacks file/sha256 are dropped. An unreadable or malformed manifest warns on stderr
    and counts as empty."""
    path = dest / MANIFEST_NAME
    if not path.is_file():
        return [], 0
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        print(f"warning: ignoring unreadable {path}: {exc}", file=sys.stderr)
        return [], 0
    if not isinstance(raw, list):
        print(f"warning: ignoring {path}: expected a JSON list", file=sys.stderr)
        return [], 0
    live: list[dict] = []
    for m in raw:
        if not isinstance(m, dict) or not isinstance(m.get("file"), str) or not isinstance(m.get("sha256"), str):
            continue
        if (dest / m["file"]).is_file():
            live.append(m)
    return live, len(raw) - len(live)


def import_fonts(sources: list[Path], dest: Path, dry_run: bool = False) -> ImportReport:
    rep = ImportReport()
    manifest, dropped = _load_manifest(dest)
    seen: dict[str, Path] = {m["sha256"]: dest / m["file"] for m in manifest}
    for src in sources:
        for p in sorted(x for x in Path(src).rglob("*") if x.is_file() and x.suffix.lower() in EXTS):
            digest = _sha(p)
            if digest in seen:
                rep.skipped_duplicate.append(p)
                continue
            from_macos = any(seg in p.as_posix() for seg in MACOS_FONT_DIRS)
            target = (dest / APPLE_SUBDIR / p.name) if from_macos else (dest / p.name)
            if target.exists():
                rep.skipped_existing.append(p)
                continue
            family, sub = _font_names(p)
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, target)
            seen[digest] = target
            rep.copied.append(target)
            manifest.append({"file": target.relative_to(dest).as_posix(), "family": family,
                             "subfamily": sub, "source": str(p), "sha256": digest,
                             "imported": date.today().isoformat()})
    if not dry_run and (rep.copied or dropped):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2))
    return rep


def coverage_table(dest: Path) -> list[tuple[str, dict[str, bool]]]:
    """Top-level files only: the tagger's Windows-name lookup reads nothing else."""
    present = {p.name.lower() for p in dest.iterdir() if p.is_file()} if dest.is_dir() else set()
    rows = []
    for family, faces in METRIC_FAMILIES.items():
        rows.append((family, {k: (faces[k].lower() in present) if k in faces else False
                              for k in STYLE_KEYS}))
    return rows


def _print_coverage(dest: Path) -> None:
    print(f"\nMetric-matched families in {dest}:")
    for family, styles in coverage_table(dest):
        marks = "  ".join(f"{k}={'yes' if v else 'no '}" for k, v in styles.items())
        print(f"  {family:16} {marks}")
    print("Missing faces fall back to Liberation / TeX Gyre / Noto (output is still fully tagged).")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="*", type=Path)
    ap.add_argument("--dest", type=Path, default=resolve_font_dir())
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true", help="only print the coverage table")
    a = ap.parse_args(argv)
    if not a.check:
        if not a.sources:
            ap.error("give at least one source directory (or --check)")
        rep = import_fonts(a.sources, a.dest, a.dry_run)
        print(f"copied {len(rep.copied)}, duplicates {len(rep.skipped_duplicate)}, existing {len(rep.skipped_existing)}")
    _print_coverage(a.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
