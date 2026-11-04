# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-session editor state cap must evict the least recently edited entries."""
import app


def _fill(n):
    return {f"text:0:elem_{i:05d}": {"text": str(i)} for i in range(n)}


def test_re_editing_an_old_entry_makes_it_newest():
    cap = app._MAX_EDITOR_STATE_ENTRIES
    edits = _fill(cap)
    edits = app._merge_editor_entries(edits, {"text:0:elem_00005": {"text": "changed"}})
    edits = app._merge_editor_entries(edits, {"text:0:new": {"text": "n"}})

    assert len(edits) == cap
    assert edits["text:0:elem_00005"] == {"text": "changed"}
    assert "text:0:new" in edits
    assert "text:0:elem_00000" not in edits, "the untouched oldest entry is the one evicted"


def test_caption_edit_refreshes_position():
    cap = app._MAX_EDITOR_STATE_ENTRIES
    edits = _fill(cap)
    edits = app._on_caption_edit(edits, "text:0:elem_00005", "changed")
    edits = app._on_caption_edit(edits, "img:0:new", "alt")
    assert edits["text:0:elem_00005"] == "changed"
    assert "text:0:elem_00000" not in edits
