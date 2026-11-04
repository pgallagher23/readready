# SPDX-License-Identifier: AGPL-3.0-or-later
"""Input hardening for the Content Editor (screen-reader text/alt/caption edits).

The Content Editor lets users edit the structure text, alt text, and captions
that get written into the tagged PDF's /ActualText and /Alt strings. Those edit
values are already *rendered* safely (HTML-escaped in the UI) and *serialized*
safely (json.dump -> iText ``PdfString(..., UNICODE_BIG)``), so they cannot
inject executable PDF content or XSS the operator.

These helpers add the one thing that chain does not provide: bounds. They are
defense-in-depth caps on user-supplied edit values — per-field length and
per-batch count — so an oversized edit cannot bloat the structure JSON, the
output PDF, or process memory. They are deliberately pure (no Gradio / config
imports) so they can be unit-tested without building the web UI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Defaults — overridable via config.yaml `editor:` section.
DEFAULT_MAX_ALT_LENGTH = 2000     # alt text / captions: short by WCAG convention
DEFAULT_MAX_TEXT_LENGTH = 50000   # body text (P, H1-H6, TD, ...): may be long
DEFAULT_MAX_BATCH_EDITS = 5000    # element edits accepted per batch payload

# Element ids produced by the structure builder look like "elem_0005",
# "elem_0005.child_Span", "elem_0005.cell_1_2", or table keys like "table_0".
# Accept word chars plus dot/colon/hyphen, bounded in length. This is a shape
# check (reject junk), not an existence check — unknown-but-well-formed ids are
# harmlessly ignored downstream when no element matches.
_ELEM_ID_RE = re.compile(r"^[\w.:\-]{1,128}$")


@dataclass(frozen=True)
class EditorLimits:
    """Resolved Content Editor input caps."""

    max_alt_length: int = DEFAULT_MAX_ALT_LENGTH
    max_text_length: int = DEFAULT_MAX_TEXT_LENGTH
    max_batch_edits: int = DEFAULT_MAX_BATCH_EDITS

    @classmethod
    def from_config(cls, cfg: dict | None) -> "EditorLimits":
        """Build limits from a config ``editor:`` mapping, falling back to
        the module defaults for missing or non-positive values."""
        cfg = cfg or {}

        def _pos_int(key: str, default: int) -> int:
            try:
                val = int(cfg.get(key, default))
            except (TypeError, ValueError):
                return default
            return val if val > 0 else default

        return cls(
            max_alt_length=_pos_int("max_alt_length", DEFAULT_MAX_ALT_LENGTH),
            max_text_length=_pos_int("max_text_length", DEFAULT_MAX_TEXT_LENGTH),
            max_batch_edits=_pos_int("max_batch_edits", DEFAULT_MAX_BATCH_EDITS),
        )


def is_valid_elem_id(elem_id) -> bool:
    """True if *elem_id* is a well-formed structure element id."""
    return isinstance(elem_id, str) and _ELEM_ID_RE.match(elem_id) is not None


# ── List edit keys ──────────────────────────────────────────────────────────
# List items (L > LI > LBody/Lbl) have ``id: null`` in the structure JSON, so a
# tag-only synthesized key collides across items (all LIs share one key) and
# edits overwrite each other. These two helpers are the SINGLE source of truth
# for per-item save keys: the renderer emits them and the save path resolves
# them, so the two cannot drift. Keys are positional (``{top}.LI{i}.LBody{j}``)
# and stable regardless of item content.


def list_child_key(top_id: str, *segments) -> str:
    """Build a list descendant's save key from its path.

    *segments* is a sequence of ``(tag, index)`` pairs from the top-level list
    element to the leaf, where *index* is the position within same-tag siblings.
    Example: ``list_child_key("elem_15", ("LI", 3), ("LBody", 0))`` ->
    ``"elem_15.LI3.LBody0"``. This is the one place a segment is formatted.
    """
    return top_id + "".join(f".{tag}{idx}" for tag, idx in segments)


def iter_list_edit_targets(list_elem, top_id):
    """Yield ``(key, leaf_node, li_node, kind)`` for every editable list leaf.

    Walks the ``L`` element's ``LI`` children (the only children the iText
    tagger reads); within each LI, every text-bearing ``LBody``/``Lbl``/other
    child is a leaf, a nested ``L`` recurses (threading the LI prefix as the new
    top id), and an LI with no leaf children but its own text is a flat leaf.

    - ``key`` — the positional save key (see :func:`list_child_key`).
    - ``leaf_node`` — the dict whose ``["text"]`` a save sets (the tagger reads
      ``LBody.text``, ``Lbl.text`` as the label, or ``LI.text`` when flat).
    - ``li_node`` — the owning ``LI`` (carries the bbox the overlay positions by).
    - ``kind`` — ``"lbody" | "lbl" | "other" | "flat"``.

    Indices count *all* same-tag siblings (including empty ones) so a blank item
    never shifts another item's key; empty leaves are skipped, not emitted.
    """
    children = (list_elem or {}).get("children") or []
    li_index = 0
    for child in children:
        if (child.get("tag") or "") != "LI":
            continue  # tagger only reads LI children of L; skip bare nested L / strays
        li_node = child
        li_seg = ("LI", li_index)
        li_index += 1
        emitted = False
        tag_counts: dict[str, int] = {}
        for gk in (li_node.get("children") or []):
            gtag = gk.get("tag") or ""
            if gtag == "L":
                nested_top = list_child_key(top_id, li_seg)
                for target in iter_list_edit_targets(gk, nested_top):
                    emitted = True
                    yield target
                continue
            j = tag_counts.get(gtag, 0)
            tag_counts[gtag] = j + 1  # positional index (reserve even when empty)
            if not (gk.get("text") or "").strip():
                continue
            kind = "lbody" if gtag == "LBody" else "lbl" if gtag == "Lbl" else "other"
            yield (list_child_key(top_id, li_seg, (gtag, j)), gk, li_node, kind)
            emitted = True
        if not emitted and (li_node.get("text") or "").strip():
            yield (list_child_key(top_id, li_seg), li_node, li_node, "flat")


def cap_edit_value(value, *, is_alt: bool, limits: EditorLimits) -> str:
    """Coerce *value* to ``str`` and truncate to the configured cap.

    Alt text / captions use ``max_alt_length``; body text uses
    ``max_text_length``.
    """
    text = value if isinstance(value, str) else str(value)
    limit = limits.max_alt_length if is_alt else limits.max_text_length
    if len(text) > limit:
        text = text[:limit]
    return text


def normalize_sr_edit_batch(batch, doc_idx, limits: EditorLimits):
    """Validate and cap a raw screen-reader edit batch.

    *batch* is the ``{elem_id: {field: value}}`` mapping the browser bridge
    writes into the hidden Gradio textbox. Returns ``(entries, stats)`` where:

    - ``entries`` maps ``"text:{doc_idx}:{elem_id}"`` (the key format the
      re-tag handler already parses) to the capped string value.
    - ``stats`` reports ``accepted``, ``dropped_invalid_id``, and
      ``batch_truncated`` counts for logging.

    The field name in each entry (``text`` vs ``alt_text``) selects which
    length cap applies.
    """
    entries: dict[str, str] = {}
    accepted = 0
    dropped_invalid_id = 0
    batch_truncated = 0

    if not isinstance(batch, dict):
        return entries, {
            "accepted": 0,
            "dropped_invalid_id": 0,
            "batch_truncated": 0,
        }

    for elem_id, fields in batch.items():
        if accepted >= limits.max_batch_edits:
            batch_truncated += 1
            continue
        if not isinstance(elem_id, str) or not isinstance(fields, dict) or not fields:
            continue
        if not is_valid_elem_id(elem_id):
            dropped_invalid_id += 1
            continue
        field_name = next(iter(fields.keys()))
        value = fields[field_name]
        if value is None:
            continue
        is_alt = field_name == "alt_text"
        entries[f"text:{doc_idx}:{elem_id}"] = cap_edit_value(
            value, is_alt=is_alt, limits=limits
        )
        accepted += 1

    return entries, {
        "accepted": accepted,
        "dropped_invalid_id": dropped_invalid_id,
        "batch_truncated": batch_truncated,
    }


def normalize_sr_order_batch(batch, doc_idx, limits: EditorLimits):
    """Validate a raw reading-order batch into re-tag handler keys.

    *batch* is the ``{page: [elem_id, ...]}`` mapping the order bridge writes
    into its hidden textbox (page is a JSON object key, i.e. a string). Returns
    ``(entries, stats)`` where *entries* maps ``"order:{doc_idx}:{page}"`` to a
    cleaned id list (ids failing the shape check are dropped). Never raises.
    """
    entries: dict[str, list] = {}
    accepted = 0
    dropped_invalid_id = 0
    dropped_invalid_page = 0

    if not isinstance(batch, dict):
        return entries, {"accepted": 0, "dropped_invalid_id": 0,
                         "dropped_invalid_page": 0}

    for page, ids in batch.items():
        try:
            page_no = int(page)
        except (TypeError, ValueError):
            dropped_invalid_page += 1
            continue
        if page_no < 0 or not isinstance(ids, list):
            dropped_invalid_page += 1
            continue
        clean = []
        for eid in ids[: limits.max_batch_edits]:
            if is_valid_elem_id(eid):
                clean.append(eid)
            else:
                dropped_invalid_id += 1
        if clean:
            entries[f"order:{doc_idx}:{page_no}"] = clean
            accepted += 1

    return entries, {"accepted": accepted,
                     "dropped_invalid_id": dropped_invalid_id,
                     "dropped_invalid_page": dropped_invalid_page}


def parse_order_edits(caption_edits, doc_idx):
    """Extract ``{page_no: [elem_id, ...]}`` for *doc_idx* from caption_edits.

    caption_edits mixes namespaces (``text:``, ``img:``, ``order:`` ...). Order
    entries are keyed ``order:{doc_idx}:{page}`` with a list value. Foreign
    doc_idx and malformed keys are ignored.
    """
    order_edits: dict[int, list] = {}
    for key, value in (caption_edits or {}).items():
        parts = key.split(":")
        if len(parts) != 3 or parts[0] != "order":
            continue
        try:
            if int(parts[1]) != doc_idx:
                continue
            page_no = int(parts[2])
        except (TypeError, ValueError):
            continue
        if isinstance(value, list):
            order_edits[page_no] = value
    return order_edits
