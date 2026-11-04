# SPDX-License-Identifier: AGPL-3.0-or-later
"""Images whose VLM call failed must be offered for manual alt text (G-22).

Run: python -m pytest tests/test_editor_records_failed_images.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.editor_records import ensure_image_editor_records

EDITABLE = {"generated", "harvested", "needs_manual_alt_text", "decorative"}


def _run(record, img="/tmp/a.png", struct=None):
    """ensure_image_editor_records mutates alt_block in place and returns None."""
    alt_block = {"image_records": [record]}
    ensure_image_editor_records(alt_block, [img], struct)
    return alt_block["image_records"]


def test_skipped_image_becomes_needs_manual_alt_text():
    out = _run({
        "src": "a.png", "file_path": "/tmp/a.png", "alt_text": None,
        "outcome": "skipped",
        "skip_reason": "VLM generation failed: timeout", "page_number": 1,
    })
    rec = next(r for r in out if str(r["src"]).endswith("a.png"))
    assert rec["outcome"] in EDITABLE, (
        "a failed image must be offered to the user, not filtered out of the UI"
    )
    assert rec["outcome"] == "needs_manual_alt_text"
    assert rec.get("skip_reason"), "the reason it failed must survive for the user"


def test_successful_records_are_untouched():
    out = _run({
        "src": "b.png", "file_path": "/tmp/b.png", "alt_text": "A red barn",
        "outcome": "generated", "skip_reason": None, "page_number": 1,
    }, img="/tmp/b.png")
    rec = next(r for r in out if str(r["src"]).endswith("b.png"))
    assert rec["outcome"] == "generated"
    assert rec["alt_text"] == "A red barn"


def test_decorative_is_not_promoted_to_needs_manual():
    out = _run({
        "src": "c.png", "file_path": "/tmp/c.png", "alt_text": "",
        "outcome": "decorative", "skip_reason": None, "page_number": 1,
    }, img="/tmp/c.png")
    rec = next(r for r in out if str(r["src"]).endswith("c.png"))
    assert rec["outcome"] == "decorative", "an intentional decision is not a failure"


def test_harvested_alt_wins_over_promotion():
    """A skipped image that carries author alt is 'harvested', not a failure.

    The authored-alt loop runs first and fills alt_text; the promotion must not
    then overwrite that outcome, or the user is told to write a description
    the author already wrote.
    """
    struct = {"elements": [
        {"image_ref": "/tmp/d.png", "alt_source": "source_pdf",
         "alt_text": "State of Michigan Coat of Arms"},
    ]}
    out = _run({
        "src": "d.png", "file_path": "/tmp/d.png", "alt_text": None,
        "outcome": "skipped", "skip_reason": "VLM generation failed: timeout",
        "page_number": 1,
    }, img="/tmp/d.png", struct=struct)
    rec = next(r for r in out if str(r["src"]).endswith("d.png"))
    assert rec["outcome"] == "harvested"
    assert rec["alt_text"] == "State of Michigan Coat of Arms"
