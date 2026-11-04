# SPDX-License-Identifier: AGPL-3.0-or-later
"""Apply manual reading-order edits to the enriched structure JSON.

Reading order is the order a screen reader announces a page's content. The iText
tagger walks the structure JSON's flat ``elements`` array in order with no
re-sorting (PdfAccessibilityTagger.java), so array order IS screen-reader order.
Reordering a page therefore means permuting that page's entries in the array.
This module is the single place that does it.

Pure (no Gradio / iText / config imports) so it unit-tests in isolation.
"""

from __future__ import annotations

import json
from pathlib import Path


def reconcile_page_order(current_ids, requested_ids):
    """Return a safe ordering of *current_ids* honoring *requested_ids*.

    A pending order may be stale (an intervening re-tag changed the page's
    blocks). Rather than trust it blindly:
      * ids in both, in the requested order, come first;
      * current ids not named keep their existing relative order, appended after;
      * requested ids no longer present are dropped.

    Example: current ``["a","b","c"]``, requested ``["b","z","a"]`` ->
    ``["b","a","c"]`` (``z`` dropped, ``c`` appended).
    """
    current = list(current_ids)
    current_set = set(current)
    seen = set()
    ordered = []
    for rid in requested_ids:
        if rid in current_set and rid not in seen:
            ordered.append(rid)
            seen.add(rid)
    for cid in current:
        if cid not in seen:
            ordered.append(cid)
            seen.add(cid)
    return ordered


def apply_sr_order_edits(structure_json_path, order_edits):
    """Rewrite *structure_json_path* so each page's blocks follow the new order.

    *order_edits* maps ``page_no -> [elem_id, ...]``. A page's blocks are the
    top-level ``elements`` whose ``page_no`` matches. Lists/Tables move as one
    unit because their children are nested inside the element dict.

    Returns the sorted list of page numbers actually changed (no-ops excluded).
    Writes the file only if at least one page changed.
    """
    if not order_edits:
        return []
    path = Path(structure_json_path)
    with path.open(encoding="utf-8") as f:
        struct = json.load(f)
    elements = struct.get("elements", [])
    changed = []

    for page_no, requested_ids in order_edits.items():
        idxs = [i for i, el in enumerate(elements) if el.get("page_no") == page_no]
        if len(idxs) < 2:
            continue
        current_ids = [elements[i].get("id") for i in idxs]
        if len(set(current_ids)) != len(current_ids):
            continue  # non-unique/missing ids on this page — can't map safely; skip
        new_ids = reconcile_page_order(current_ids, requested_ids)
        if new_ids == current_ids:
            continue
        by_id = {elements[i].get("id"): elements[i] for i in idxs}
        for slot, eid in zip(idxs, new_ids):
            elements[slot] = by_id[eid]
        changed.append(page_no)

    if changed:
        with path.open("w", encoding="utf-8") as f:
            json.dump(struct, f, indent=2, ensure_ascii=False)
    return sorted(changed)
