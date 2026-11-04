# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path
import utils.font_dir as fd

def test_default_is_repo_fonts(monkeypatch):
    monkeypatch.delenv("READREADY_FONT_DIR", raising=False)
    assert fd.resolve_font_dir() == fd.REPO_ROOT / "fonts"

def test_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("READREADY_FONT_DIR", str(tmp_path))
    assert fd.resolve_font_dir() == tmp_path

def test_missing_dir_is_returned_not_raised(monkeypatch, tmp_path):
    monkeypatch.setenv("READREADY_FONT_DIR", str(tmp_path / "nope"))
    assert fd.resolve_font_dir() == tmp_path / "nope"

def test_jvm_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("READREADY_FONT_DIR", str(tmp_path))
    assert fd.jvm_font_flag() == f"-Dreadready.fontDir={tmp_path}"

def test_font_inventory_uses_resolver(monkeypatch, tmp_path):
    monkeypatch.setenv("READREADY_FONT_DIR", str(tmp_path))
    from utils import font_inventory
    assert str(tmp_path) in font_inventory.default_font_dirs()
