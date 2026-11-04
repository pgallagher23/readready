# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_app_branding_wiring.py
"""app.py and reporting must take every color/title from utils.branding."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_hardcoded_brand_identifiers_in_product_code(private_terms):
    # Former institutional palette; names come from the maintainer-only term list.
    pattern = re.compile(
        r"#532E1F|#F1C500|#FBF7F0"
        r"|rgba?\(\s*83\s*,\s*46\s*,\s*31|rgba?\(\s*251\s*,\s*247\s*,\s*240"
        r"|#d4a800|#fdf8f5|#e5c9b8",
        re.I,
    )
    offenders = []
    for rel in ("app.py", "reporting/constants.py",
                "reporting/remediation_guide.py", "output_gen/report_page.py"):
        for i, line in enumerate((ROOT / rel).read_text().splitlines(), 1):
            low = line.lower()
            if pattern.search(line) or any(t in low for t in private_terms):
                offenders.append(f"{rel}:{i}")
    assert not offenders, "\n".join(offenders)


def test_make_palette_shape():
    import app  # noqa: F401  (imports gradio; skip if unavailable)
    pal = app._make_palette("#1F3A5F", "x")
    assert pal.c500 == "#1F3A5F"
    assert pal.c50 != pal.c950
    assert re.fullmatch(r"#[0-9a-f]{6}", pal.c50)


def test_header_uses_branding_title(monkeypatch):
    import app
    html = app._build_header_html()
    assert app._BRAND.header_title in html
    # No About/Contact navigation: no links, no menu button.
    assert "<nav" not in html and "rr-hamburger" not in html and "/pages/" not in html


def test_no_static_pages_route():
    src = (ROOT / "app.py").read_text()
    assert 'Mount("/pages"' not in src and "StaticFiles" not in src
