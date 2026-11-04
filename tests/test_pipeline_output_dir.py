# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-document output directory must always be a child of the session dir.

A dotted upload name such as ``...pdf`` has the stem ``..``; deriving the
output directory from it and then cleaning it would delete the session
directory's parent.
"""
from pathlib import Path

import pytest

from pipeline_core import prepare_pdf_output_dir, safe_pdf_stem


@pytest.mark.parametrize(
    "name,expected",
    [
        ("report.pdf", "report"),
        ("...pdf", "document"),
        ("..pdf", "document"),
        (".pdf", "pdf"),
        ("..evil.pdf", "evil"),
        ("my report (v2).pdf", "my_report_v2"),
        ("a/b.pdf", "b"),
    ],
)
def test_safe_pdf_stem(name, expected):
    assert safe_pdf_stem(Path(name)) == expected


def test_dotted_name_never_cleans_outside_session_dir(tmp_path):
    session = tmp_path / "session"
    session.mkdir()
    sibling = tmp_path / "other_session" / "keep.txt"
    sibling.parent.mkdir()
    sibling.write_text("x")

    out = prepare_pdf_output_dir(Path("...pdf"), session, clean_output=True)

    assert sibling.exists()
    assert out.parent == session
    assert out.is_dir()


def test_clean_output_removes_only_the_document_dir(tmp_path):
    session = tmp_path / "session"
    stale = session / "report"
    stale.mkdir(parents=True)
    (stale / "old.html").write_text("stale")
    (session / "unrelated.txt").write_text("keep")

    out = prepare_pdf_output_dir(Path("report.pdf"), session, clean_output=True)

    assert out == stale
    assert not (stale / "old.html").exists()
    assert (session / "unrelated.txt").exists()
