# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for utils/editor_input.py — Content Editor input hardening.

These pin the defense-in-depth caps on user-supplied screen-reader text/alt/
caption edits: per-field length limits, per-batch size limit, and element-id
shape validation. They also assert the security-relevant property that
injection-shaped payloads survive *verbatim* as inert string data (escaping
happens later at json.dump -> iText PdfString), never interpreted.

Run:  python -m pytest tests/test_editor_input_hardening.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.editor_input import (  # noqa: E402
    DEFAULT_MAX_ALT_LENGTH,
    DEFAULT_MAX_BATCH_EDITS,
    DEFAULT_MAX_TEXT_LENGTH,
    EditorLimits,
    cap_edit_value,
    is_valid_elem_id,
    normalize_sr_edit_batch,
)

LIMITS = EditorLimits()  # module defaults

# Payloads that would be dangerous if ever interpreted as PDF syntax, shell,
# HTML, or JSON. The editor must treat them as ordinary text.
INJECTION_PAYLOADS = [
    ")",                                   # close a PDF string
    "\\",                                  # PDF escape char
    ") /JavaScript (app.alert\\(1\\)) Tj", # PDF operator/dict injection attempt
    "<script>alert(1)</script>",           # XSS
    '"; DROP TABLE x; --',                 # SQL-ish
    "$(rm -rf /)",                         # shell-ish
    '{"injected": true}',                  # JSON-ish
    "‮evil",                          # bidi override prefix
]


# --- is_valid_elem_id ------------------------------------------------------

def test_valid_elem_ids_accepted():
    for good in ["elem_0005", "elem_0005.child_Span", "elem_0005.cell_1_2",
                 "table_0", "elem_12.child_Link"]:
        assert is_valid_elem_id(good), good


def test_invalid_elem_ids_rejected():
    for bad in ["", "elem 0005", "elem/0005", "../etc/passwd",
                "<b>", "a" * 200, "id\nwithnewline", None, 123, {}]:
        assert not is_valid_elem_id(bad), repr(bad)


# --- cap_edit_value --------------------------------------------------------

def test_cap_value_under_limit_unchanged():
    assert cap_edit_value("hello", is_alt=True, limits=LIMITS) == "hello"
    assert cap_edit_value("hello", is_alt=False, limits=LIMITS) == "hello"


def test_cap_value_truncates_alt_and_text_separately():
    long = "x" * (DEFAULT_MAX_TEXT_LENGTH + 100)
    alt_capped = cap_edit_value(long, is_alt=True, limits=LIMITS)
    text_capped = cap_edit_value(long, is_alt=False, limits=LIMITS)
    assert len(alt_capped) == DEFAULT_MAX_ALT_LENGTH
    assert len(text_capped) == DEFAULT_MAX_TEXT_LENGTH
    # alt cap is the stricter of the two
    assert len(alt_capped) < len(text_capped)


def test_cap_value_coerces_non_string():
    assert cap_edit_value(123, is_alt=True, limits=LIMITS) == "123"
    assert cap_edit_value(True, is_alt=False, limits=LIMITS) == "True"


def test_injection_payloads_preserved_verbatim_when_under_cap():
    # Security property: the value is stored as-is (inert text), not parsed.
    for payload in INJECTION_PAYLOADS:
        assert cap_edit_value(payload, is_alt=False, limits=LIMITS) == payload


# --- normalize_sr_edit_batch ----------------------------------------------

def test_basic_batch_routes_to_text_key():
    batch = {"elem_0001": {"text": "Hello world"}}
    entries, stats = normalize_sr_edit_batch(batch, 0, LIMITS)
    assert entries == {"text:0:elem_0001": "Hello world"}
    assert stats["accepted"] == 1
    assert stats["dropped_invalid_id"] == 0
    assert stats["batch_truncated"] == 0


def test_alt_field_uses_alt_cap_text_field_uses_text_cap():
    big = "y" * (DEFAULT_MAX_TEXT_LENGTH + 50)
    batch = {
        "elem_0001": {"alt_text": big},
        "elem_0002": {"text": big},
    }
    entries, _ = normalize_sr_edit_batch(batch, 3, LIMITS)
    assert len(entries["text:3:elem_0001"]) == DEFAULT_MAX_ALT_LENGTH
    assert len(entries["text:3:elem_0002"]) == DEFAULT_MAX_TEXT_LENGTH


def test_injection_payloads_survive_batch_verbatim():
    batch = {f"elem_{i:04d}": {"text": p} for i, p in enumerate(INJECTION_PAYLOADS)}
    entries, stats = normalize_sr_edit_batch(batch, 0, LIMITS)
    assert stats["accepted"] == len(INJECTION_PAYLOADS)
    stored = list(entries.values())
    for payload in INJECTION_PAYLOADS:
        assert payload in stored


def test_malformed_ids_dropped_and_counted():
    batch = {
        "elem_0001": {"text": "ok"},
        "../escape": {"text": "nope"},
        "has space": {"text": "nope"},
        "<script>": {"text": "nope"},
    }
    entries, stats = normalize_sr_edit_batch(batch, 0, LIMITS)
    assert entries == {"text:0:elem_0001": "ok"}
    assert stats["accepted"] == 1
    assert stats["dropped_invalid_id"] == 3


def test_batch_size_capped():
    limits = EditorLimits(max_batch_edits=10)
    batch = {f"elem_{i:05d}": {"text": "v"} for i in range(25)}
    entries, stats = normalize_sr_edit_batch(batch, 0, limits)
    assert stats["accepted"] == 10
    assert stats["batch_truncated"] == 15
    assert len(entries) == 10


def test_none_value_skipped():
    batch = {"elem_0001": {"text": None}, "elem_0002": {"text": "kept"}}
    entries, stats = normalize_sr_edit_batch(batch, 0, LIMITS)
    assert entries == {"text:0:elem_0002": "kept"}
    assert stats["accepted"] == 1


def test_non_dict_batch_returns_empty():
    for junk in [None, "string", 42, ["list"]]:
        entries, stats = normalize_sr_edit_batch(junk, 0, LIMITS)
        assert entries == {}
        assert stats["accepted"] == 0


def test_malformed_entries_skipped_without_error():
    batch = {
        "elem_0001": "not a dict",   # fields must be a dict
        "elem_0002": {},             # empty fields
        "elem_0003": {"text": "ok"},
    }
    entries, stats = normalize_sr_edit_batch(batch, 0, LIMITS)
    assert entries == {"text:0:elem_0003": "ok"}
    assert stats["accepted"] == 1


# --- EditorLimits.from_config ---------------------------------------------

def test_from_config_defaults_when_empty():
    lim = EditorLimits.from_config(None)
    assert lim.max_alt_length == DEFAULT_MAX_ALT_LENGTH
    assert lim.max_text_length == DEFAULT_MAX_TEXT_LENGTH
    assert lim.max_batch_edits == DEFAULT_MAX_BATCH_EDITS


def test_from_config_overrides_applied():
    lim = EditorLimits.from_config(
        {"max_alt_length": 100, "max_text_length": 200, "max_batch_edits": 5}
    )
    assert (lim.max_alt_length, lim.max_text_length, lim.max_batch_edits) == (100, 200, 5)


def test_from_config_rejects_bad_and_nonpositive_values():
    lim = EditorLimits.from_config(
        {"max_alt_length": "oops", "max_text_length": 0, "max_batch_edits": -5}
    )
    assert lim.max_alt_length == DEFAULT_MAX_ALT_LENGTH
    assert lim.max_text_length == DEFAULT_MAX_TEXT_LENGTH
    assert lim.max_batch_edits == DEFAULT_MAX_BATCH_EDITS


from utils.editor_input import (  # noqa: E402
    normalize_sr_order_batch,
    parse_order_edits,
)


# --- normalize_sr_order_batch ---------------------------------------------

def test_order_batch_routes_to_order_key():
    batch = {"3": ["elem_2", "elem_1"]}
    entries, stats = normalize_sr_order_batch(batch, 0, LIMITS)
    assert entries == {"order:0:3": ["elem_2", "elem_1"]}
    assert stats["accepted"] == 1


def test_order_batch_drops_invalid_ids_keeps_rest():
    batch = {"1": ["elem_1", "bad id!", "elem_2"]}
    entries, stats = normalize_sr_order_batch(batch, 2, LIMITS)
    assert entries == {"order:2:1": ["elem_1", "elem_2"]}
    assert stats["dropped_invalid_id"] == 1


def test_order_batch_drops_invalid_page():
    batch = {"notapage": ["elem_1"], "-1": ["elem_2"]}
    entries, stats = normalize_sr_order_batch(batch, 0, LIMITS)
    assert entries == {}
    assert stats["dropped_invalid_page"] == 2


def test_order_batch_empty_id_list_not_emitted():
    entries, stats = normalize_sr_order_batch({"1": []}, 0, LIMITS)
    assert entries == {}


def test_order_batch_non_dict_returns_empty():
    for junk in (None, [], "x", 5):
        entries, stats = normalize_sr_order_batch(junk, 0, LIMITS)
        assert entries == {} and stats["accepted"] == 0


# --- parse_order_edits -----------------------------------------------------

def test_parse_order_edits_extracts_for_doc():
    caption_edits = {
        "order:0:3": ["elem_2", "elem_1"],
        "text:0:elem_9": "hi",
        "order:1:4": ["elem_5"],  # different doc
    }
    assert parse_order_edits(caption_edits, 0) == {3: ["elem_2", "elem_1"]}


def test_parse_order_edits_ignores_malformed_and_nonlist():
    caption_edits = {"order:0:x": ["elem_1"], "order:0:2": "notalist"}
    assert parse_order_edits(caption_edits, 0) == {}


def test_parse_order_edits_empty():
    assert parse_order_edits({}, 0) == {}
    assert parse_order_edits(None, 0) == {}
