# SPDX-License-Identifier: AGPL-3.0-or-later
from pathlib import Path
STATIC = Path(__file__).resolve().parent.parent / "static"

def test_static_contains_only_loaded_files():
    allowed = {"landing_page.html", "remediation_tips.yaml", "branding"}
    assert {p.name for p in STATIC.iterdir()} == allowed
    assert {p.name for p in (STATIC / "branding").iterdir()} == {"README.md"}

def test_landing_page_is_short_and_generic(private_terms):
    text = STATIC.joinpath("landing_page.html").read_text().lower()
    assert len(text) < 6000
    assert not [t for t in private_terms if t in text]
