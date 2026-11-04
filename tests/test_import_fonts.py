# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import import_fonts as imp

def _fake_ttf(path: Path, payload: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)

def test_copies_and_writes_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(imp, "_font_names", lambda p: ("Arial", "Regular"))
    src, dest = tmp_path / "src", tmp_path / "fonts"
    _fake_ttf(src / "ARIAL.TTF", b"a" * 10)
    rep = imp.import_fonts([src], dest)
    assert (dest / "ARIAL.TTF").exists() and rep.copied == [dest / "ARIAL.TTF"]
    m = json.loads((dest / "manifest.json").read_text())
    assert m[0]["file"] == "ARIAL.TTF" and m[0]["family"] == "Arial"

def test_duplicate_content_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(imp, "_font_names", lambda p: ("Arial", "Regular"))
    s1, s2, dest = tmp_path / "s1", tmp_path / "s2", tmp_path / "fonts"
    _fake_ttf(s1 / "arial.ttf", b"same"); _fake_ttf(s2 / "Arial.ttf", b"same")
    rep = imp.import_fonts([s1, s2], dest)
    assert len(rep.copied) == 1 and len(rep.skipped_duplicate) == 1

def test_existing_file_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(imp, "_font_names", lambda p: ("Arial", "Regular"))
    src, dest = tmp_path / "src", tmp_path / "fonts"
    _fake_ttf(dest / "arial.ttf", b"old"); _fake_ttf(src / "arial.ttf", b"new")
    rep = imp.import_fonts([src], dest)
    assert (dest / "arial.ttf").read_bytes() == b"old" and rep.skipped_existing

def test_macos_source_goes_to_apple_subdir(tmp_path, monkeypatch):
    monkeypatch.setattr(imp, "_font_names", lambda p: ("Helvetica", "Regular"))
    src, dest = tmp_path / "Library" / "Fonts", tmp_path / "fonts"
    _fake_ttf(src / "Helvetica.ttc", b"h")
    imp.import_fonts([src], dest)
    assert (dest / "apple" / "Helvetica.ttc").exists()

def test_windows_ttc_stays_at_root(tmp_path, monkeypatch):
    monkeypatch.setattr(imp, "_font_names", lambda p: ("Cambria", "Regular"))
    src, dest = tmp_path / "mnt" / "c" / "Windows" / "Fonts", tmp_path / "fonts"
    _fake_ttf(src / "cambria.ttc", b"c")
    imp.import_fonts([src], dest)
    assert (dest / "cambria.ttc").exists() and not (dest / "apple").exists()

def test_dry_run_copies_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(imp, "_font_names", lambda p: ("Arial", "Regular"))
    src, dest = tmp_path / "src", tmp_path / "fonts"
    _fake_ttf(src / "arial.ttf", b"a")
    rep = imp.import_fonts([src], dest, dry_run=True)
    assert rep.copied == [dest / "arial.ttf"] and not dest.exists()

def test_coverage_table_case_insensitive(tmp_path):
    _fake_ttf(tmp_path / "ARIAL.TTF", b"a"); _fake_ttf(tmp_path / "arialbd.ttf", b"b")
    table = dict(imp.coverage_table(tmp_path))
    assert table["Arial"] == {"regular": True, "bold": True, "italic": False, "bold_italic": False}

def test_coverage_ignores_apple_subdir(tmp_path):
    # The Windows-name table resolves top-level files only; apple/ uses different snake_case names.
    _fake_ttf(tmp_path / "apple" / "Arial.ttf", b"a")
    assert dict(imp.coverage_table(tmp_path))["Arial"]["regular"] is False

def test_deleted_font_is_recopied_and_dead_entry_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(imp, "_font_names", lambda p: ("Arial", "Regular"))
    src, dest = tmp_path / "src", tmp_path / "fonts"
    _fake_ttf(src / "arial.ttf", b"a")
    imp.import_fonts([src], dest)
    (dest / "arial.ttf").unlink()
    rep = imp.import_fonts([src], dest)
    assert rep.copied == [dest / "arial.ttf"] and not rep.skipped_duplicate
    assert (dest / "arial.ttf").read_bytes() == b"a"
    m = json.loads((dest / "manifest.json").read_text())
    assert len(m) == 1 and m[0]["file"] == "arial.ttf"

def test_corrupt_manifest_warns_and_import_proceeds(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(imp, "_font_names", lambda p: ("Arial", "Regular"))
    src, dest = tmp_path / "src", tmp_path / "fonts"
    _fake_ttf(src / "arial.ttf", b"a")
    _fake_ttf(dest / "manifest.json", b"{not json")
    rep = imp.import_fonts([src], dest)
    assert rep.copied == [dest / "arial.ttf"]
    assert "manifest" in capsys.readouterr().err
    assert json.loads((dest / "manifest.json").read_text())[0]["file"] == "arial.ttf"

def test_manifest_entry_missing_sha256_is_ignored(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(imp, "_font_names", lambda p: ("Arial", "Regular"))
    src, dest = tmp_path / "src", tmp_path / "fonts"
    _fake_ttf(src / "arial.ttf", b"a")
    _fake_ttf(dest / "arial.ttf", b"a")
    (dest / "manifest.json").write_text(json.dumps([{"file": "arial.ttf"}]))
    rep = imp.import_fonts([src], dest)
    assert rep.skipped_existing and not rep.skipped_duplicate
