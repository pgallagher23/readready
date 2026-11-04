# SPDX-License-Identifier: AGPL-3.0-or-later
"""Issue context must not carry the whole document's text.

``html_snippet`` was capped at 500 chars but ``text_content`` stored
``get_text()`` in full. A document-level issue on <html>/<body> (language,
no-h1, title) therefore carried the ENTIRE document text, and the auditor
stores every issue four times (issues / by_page / by_principle / by_status).
On prod a 31-page, 28-issue document produced a 5.05MB Celery result (97% of
it this field), which is what filled Redis on 2026-09-29. Every reader uses
at most the first 160 chars.

Run: python -m pytest tests/test_context_collector_size.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup

from audit.context_collector import ContextCollector, MAX_TEXT_CONTENT_CHARS


def test_document_level_context_is_capped():
    body_text = "word " * 50_000
    soup = BeautifulSoup(f"<html lang='en'><body><p>{body_text}</p></body></html>",
                         "html.parser")
    ctx = ContextCollector(soup.find("html")).collect()
    assert len(ctx["text_content"]) == MAX_TEXT_CONTENT_CHARS
    assert len(ctx["html_snippet"]) <= 500


def test_cap_keeps_every_consumers_prefix():
    # Readers slice up to [:160]; the cap must never cut into that.
    assert MAX_TEXT_CONTENT_CHARS >= 160


def test_short_text_is_unchanged():
    soup = BeautifulSoup("<h2>Methods and Materials</h2>", "html.parser")
    ctx = ContextCollector(soup.find("h2")).collect()
    assert ctx["text_content"] == "Methods and Materials"


def test_inline_image_data_uri_is_capped():
    # Docling inlines figures as data: URIs; each image issue copied ~140KB of
    # base64 into context.attributes (and its parent's) four times over.
    data_uri = "data:image/png;base64," + "A" * 200_000
    soup = BeautifulSoup(
        f'<figure data-uri="{data_uri}"><img src="{data_uri}" alt="" class="x y"></figure>',
        "html.parser")
    ctx = ContextCollector(soup.find("img")).collect()
    assert ctx["attributes"]["src"].startswith("data:image/png;base64,")
    assert len(ctx["attributes"]["src"]) == MAX_TEXT_CONTENT_CHARS
    assert ctx["attributes"]["alt"] == ""
    assert ctx["attributes"]["class"] == ["x", "y"]
    assert len(ctx["parent"]["attributes"]["data-uri"]) == MAX_TEXT_CONTENT_CHARS


def test_capping_does_not_mutate_the_document():
    data_uri = "data:image/png;base64," + "A" * 5_000
    soup = BeautifulSoup(f'<img src="{data_uri}">', "html.parser")
    ContextCollector(soup.find("img")).collect()
    assert soup.find("img")["src"] == data_uri
