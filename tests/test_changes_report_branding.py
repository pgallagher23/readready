# SPDX-License-Identifier: AGPL-3.0-or-later
"""The changes report header must come from branding, never a hard-coded institution."""
from types import SimpleNamespace

from output_gen import changes_report


def _render(tmp_path, monkeypatch, organization, product_name):
    monkeypatch.setattr(
        changes_report, "get_branding",
        lambda: SimpleNamespace(organization=organization, product_name=product_name),
    )
    out = tmp_path / "report.html"
    assert changes_report.generate_changes_report_html(str(out), "Sample Doc")
    return out.read_text(encoding="utf-8")


def test_header_uses_configured_branding(tmp_path, monkeypatch, private_terms):
    html = _render(tmp_path, monkeypatch, "Example University", "ACME Checker")
    assert not [t for t in private_terms if t in html.lower()]
    assert '<p class="org-name">Example University · ACME Checker</p>' in html


def test_header_without_organization_and_escaped(tmp_path, monkeypatch, private_terms):
    html = _render(tmp_path, monkeypatch, "", "Tools <&> Co")
    assert not [t for t in private_terms if t in html.lower()]
    assert '<p class="org-name">Tools &lt;&amp;&gt; Co</p>' in html
