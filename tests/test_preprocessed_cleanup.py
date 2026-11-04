# SPDX-License-Identifier: AGPL-3.0-or-later
"""The font-fixed working copy must not be left beside the input PDF."""
from pathlib import Path

from output_gen import pdf_tagger


def _run(tmp_path, monkeypatch, single_jvm_result):
    src = tmp_path / "input.pdf"
    src.write_bytes(b"%PDF-1.7\n")
    copy = tmp_path / "input_preprocessed.pdf"

    def fake_preprocess(pdf_path, **_):
        copy.write_bytes(b"%PDF-1.7\n")
        return str(copy)

    monkeypatch.setattr(pdf_tagger, "_preprocess_broken_fonts", fake_preprocess)
    monkeypatch.setattr(pdf_tagger, "_run_single_jvm_content_preserving",
                        lambda *a, **k: single_jvm_result)
    return copy, pdf_tagger._run_itext_tagger(
        str(src), str(tmp_path / "s.json"), str(tmp_path / "out.pdf"), "en",
    )


def test_copy_removed_when_single_jvm_succeeds(tmp_path, monkeypatch):
    expected = {"path": "out.pdf"}
    copy, result = _run(tmp_path, monkeypatch, expected)
    assert result == expected
    assert not Path(copy).exists()
