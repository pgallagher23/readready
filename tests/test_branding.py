# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_branding.py
import pytest
from pathlib import Path

from utils.branding import Branding, BrandingError, branding_from_config, DEFAULT_COLORS


def test_defaults_when_section_empty(tmp_path):
    b = branding_from_config({}, tmp_path)
    assert b.product_name == "ReadReady"
    assert b.header_title == "ReadReady"
    assert b.logo is None
    assert b.colors == DEFAULT_COLORS
    assert b.primary == "#1f2937"


def test_colors_merge_over_defaults(tmp_path):
    b = branding_from_config({"colors": {"primary": "#532E1F"}}, tmp_path)
    assert b.primary == "#532E1F"
    assert b.accent == DEFAULT_COLORS["accent"]


def test_bad_hex_names_key(tmp_path):
    with pytest.raises(BrandingError, match="branding.colors.accent"):
        branding_from_config({"colors": {"accent": "gold"}}, tmp_path)


def test_unknown_color_key_rejected(tmp_path):
    with pytest.raises(BrandingError, match="branding.colors.primry"):
        branding_from_config({"colors": {"primry": "#000000"}}, tmp_path)


def test_logo_resolved_under_static_branding(tmp_path):
    (tmp_path / "static" / "branding").mkdir(parents=True)
    (tmp_path / "static" / "branding" / "logo.png").write_bytes(b"\x89PNG")
    b = branding_from_config({"logo": "logo.png", "logo_alt": "Example U"}, tmp_path)
    assert b.logo == tmp_path / "static" / "branding" / "logo.png"
    assert b.logo_alt == "Example U"


def test_missing_logo_is_none_not_error(tmp_path):
    b = branding_from_config({"logo": "nope.png"}, tmp_path)
    assert b.logo is None


def test_header_page_settings_are_gone(tmp_path):
    """About/Contact pages were removed; their settings are rejected, not ignored."""
    for key in ("pages_dir", "contact_email"):
        with pytest.raises(BrandingError, match=key):
            branding_from_config({key: "x"}, tmp_path)
    assert not hasattr(branding_from_config({}, tmp_path), "nav_pages")
