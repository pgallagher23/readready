# SPDX-License-Identifier: AGPL-3.0-or-later
"""Anonymous uploads are bounded by page count, not just bytes."""
import ast
from pathlib import Path

import gradio as gr
import pymupdf
import pytest

import app

ROOT = Path(__file__).resolve().parent.parent


def _pdf(path, pages):
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(path)
    return str(path)


def test_over_page_limit_is_rejected(tmp_path):
    with pytest.raises(gr.Error, match="big.pdf"):
        app._check_page_limit([_pdf(tmp_path / "big.pdf", 5)], max_pages=4)


def test_at_limit_is_accepted(tmp_path):
    app._check_page_limit([_pdf(tmp_path / "ok.pdf", 4)], max_pages=4)


def test_every_subprocess_run_in_the_converter_has_a_timeout():
    src = (ROOT / "converter" / "docling_converter.py").read_text()
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "run"
                and getattr(node.func.value, "id", "") == "subprocess"):
            assert any(k.arg == "timeout" for k in node.keywords), node.lineno


def test_config_declares_max_pages():
    import yaml
    assert yaml.safe_load((ROOT / "config.yaml").read_text())["ui"]["max_pages"] == 300
