# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sync the remediated document title into the structure JSON.

HTML remediation (remediate_descriptive_title) rewrites a filename-like
<title> into a human-readable one, but the tagger reads metadata.title from
the structure JSON, which was never updated. Several documents shipped a
junk PDF title (a URL, an OCR fragment, a stray date, a word fragment) while
the HTML already held the correct one.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from output_gen.pdf_tagger import _norm_title, _sync_title_from_html


def _write(tmp_path, html_title, json_title, *, metadata=True):
    html = tmp_path / "doc.html"
    html.write_text(
        f"<html><head><title>{html_title}</title></head><body></body></html>",
        encoding="utf-8",
    )
    struct = {"elements": []}
    if metadata:
        struct["metadata"] = {"title": json_title, "num_pages": 1}
    jpath = tmp_path / "structure.json"
    jpath.write_text(json.dumps(struct), encoding="utf-8")
    return str(jpath), str(html)


def _title_of(jpath):
    with open(jpath, encoding="utf-8") as f:
        return (json.load(f).get("metadata") or {}).get("title")


def test_norm_title_collapses_whitespace_and_nbsp():
    assert _norm_title("  Behavioral\t\r \xa0Health  ") == "Behavioral Health"
    assert _norm_title("Behavioral\t\n \xa0Health") == "Behavioral Health"
    assert _norm_title(None) == ""


def test_syncs_when_titles_differ(tmp_path):
    """The real defect: HTML has the good title, JSON has the junk one."""
    jpath, html = _write(tmp_path, "Src Sample Photo Form",
                         "example.edu/rec/facilities...otopre-approvalform.pdf")
    assert _sync_title_from_html(jpath, html) == "Src Sample Photo Form"
    assert _title_of(jpath) == "Src Sample Photo Form"


def test_no_op_when_titles_match(tmp_path):
    jpath, html = _write(tmp_path, "Annual Report 2024", "Annual Report 2024")
    assert _sync_title_from_html(jpath, html) is None
    assert _title_of(jpath) == "Annual Report 2024"


def test_no_op_when_titles_differ_only_by_whitespace(tmp_path):
    jpath, html = _write(tmp_path, "Behavioral\t\n \xa0Health",
                         "Behavioral\t\r \xa0Health")
    assert _sync_title_from_html(jpath, html) is None


def test_no_op_on_entity_only_difference(tmp_path):
    """BeautifulSoup unescapes; '&amp;' must not read as a real divergence."""
    jpath, html = _write(tmp_path, "Fish &amp; Wildlife", "Fish & Wildlife")
    assert _sync_title_from_html(jpath, html) is None


@pytest.mark.parametrize("bad", ["", "   ", "Untitled Document"])
def test_no_op_on_empty_or_placeholder_html_title(tmp_path, bad):
    jpath, html = _write(tmp_path, bad, "Real Existing Title")
    assert _sync_title_from_html(jpath, html) is None
    assert _title_of(jpath) == "Real Existing Title"


def test_creates_metadata_block_when_absent(tmp_path):
    jpath, html = _write(tmp_path, "Recovered Title", None, metadata=False)
    assert _sync_title_from_html(jpath, html) == "Recovered Title"
    assert _title_of(jpath) == "Recovered Title"


def test_missing_html_returns_none_without_raising(tmp_path):
    jpath, _ = _write(tmp_path, "x", "y")
    assert _sync_title_from_html(jpath, str(tmp_path / "nope.html")) is None


def test_unparseable_json_returns_none_without_raising(tmp_path):
    _, html = _write(tmp_path, "Good Title", "junk")
    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    assert _sync_title_from_html(str(bad), html) is None


def test_is_idempotent(tmp_path):
    """Second run finds them equal and no-ops — matters for --skip-docling."""
    jpath, html = _write(tmp_path, "Src Sample Photo Form", "junk.pdf")
    assert _sync_title_from_html(jpath, html) == "Src Sample Photo Form"
    assert _sync_title_from_html(jpath, html) is None
    assert _title_of(jpath) == "Src Sample Photo Form"


def test_preserves_other_structure_content(tmp_path):
    """The rewrite must not drop elements or sibling metadata keys."""
    jpath, html = _write(tmp_path, "New Title", "old")
    assert _sync_title_from_html(jpath, html) == "New Title"
    with open(jpath, encoding="utf-8") as f:
        data = json.load(f)
    assert data["elements"] == []
    assert data["metadata"]["num_pages"] == 1


def test_accepts_a_directory_of_html(tmp_path):
    """html_path is a file on both call paths, but the table sync tolerates
    a directory and this must behave the same way."""
    d = tmp_path / "extracted_html"
    d.mkdir()
    (d / "doc.html").write_text(
        "<html><head><title>From Directory</title></head></html>",
        encoding="utf-8")
    jpath = tmp_path / "structure.json"
    jpath.write_text(json.dumps({"metadata": {"title": "junk_stem"}}),
                     encoding="utf-8")
    assert _sync_title_from_html(str(jpath), str(d)) == "From Directory"


def test_tag_pdf_exposes_synced_title_in_its_result():
    """tag_pdf must surface the synced title so pipeline_core can adopt it.

    Without this, the PDF bookmarks and the WCAG validation report built
    from result["title"] would carry the old junk title while the PDF
    metadata carried the corrected one.
    """
    import inspect

    from output_gen import pdf_tagger

    src = inspect.getsource(pdf_tagger.tag_pdf)
    assert "_sync_title_from_html(" in src, "tag_pdf must call the title sync"
    assert "document_title" in src, (
        "tag_pdf must expose the synced title as result['document_title']"
    )


def test_pipeline_core_adopts_synced_title_at_every_call_site():
    """Both tag_pdf call sites must adopt the synced title.

    The PDF bookmarks and the WCAG validation report are built after
    tagging from result["title"]; if a site is missed, they carry the old
    junk title while the PDF's own metadata carries the corrected one.
    """
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "pipeline_core.py"), encoding="utf-8").read()

    unpack_sites = src.count('result["pdf_path"] = pdf_tag_result["path"]')
    adopt_sites = len(re.findall(r'pdf_tag_result\.get\("document_title"\)', src))
    assert unpack_sites == 2, f"expected 2 tag_pdf unpack sites, found {unpack_sites}"
    assert adopt_sites == unpack_sites, (
        f"{adopt_sites} of {unpack_sites} call sites adopt the synced title"
    )
