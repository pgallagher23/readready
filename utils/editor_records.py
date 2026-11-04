# SPDX-License-Identifier: AGPL-3.0-or-later
"""Editor record reconciliation for the Alt Text & Captions tab.

The VLM identifies images by their HTML ``src`` — a relative path such as
``../images/figure_1.png`` — while the pipeline tracks extracted files by
absolute path.  Any code that decides "this image has no record yet" must
compare basenames, never raw src strings: comparing them verbatim can never
match, which duplicated every image in the editor and falsely flipped it
into "AI processing was disabled" mode.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional


def _record_basename(src: Any) -> str:
    """Reduce a record's src to a comparable image basename.

    Data URIs carry no filename — they reduce to "" so they can neither
    match nor block a real file.
    """
    text = str(src or "")
    if not text or text.startswith("data:"):
        return ""
    return Path(text).name


def ensure_image_editor_records(
    alt_block: Dict[str, Any],
    image_files: List[str],
    struct_data: Optional[Dict[str, Any]],
) -> None:
    """Guarantee exactly one editor record per extracted image.

    Every image needs an editor record, whatever the VLM did.  The VLM only
    returns records for images it processed, so an image that carries
    harvested alt — or that the VLM skipped or failed on — would otherwise
    be invisible in the Alt Text & Captions tab.  Mutates ``alt_block``
    in place.
    """
    records = alt_block.setdefault("image_records", [])

    harvested_alt: Dict[str, str] = {}
    for elem in (struct_data or {}).get("elements", []):
        ref = elem.get("image_ref")
        if ref and elem.get("alt_source") == "source_pdf":
            harvested_alt[Path(ref).name] = elem.get("alt_text") or ""

    # Show the author's own words in the editor rather than a blank box,
    # which would invite the user to overwrite alt text they already wrote.
    for rec in records:
        authored = harvested_alt.get(_record_basename(rec.get("src")), "")
        if authored and not (rec.get("alt_text") or "").strip():
            rec["alt_text"] = authored
            rec["outcome"] = "harvested"

    # A per-image VLM failure leaves outcome "skipped", which the Alt Text tab
    # filters out — so the images that most need a human are the ones the UI
    # hides.  Promote them to needs_manual_alt_text, keeping skip_reason so the
    # user can see why (G-22).  "decorative" is a deliberate decision, not a
    # failure, and is left alone.  This runs AFTER the authored-alt loop above,
    # so a skipped image carrying harvested alt stays "harvested".
    for rec in records:
        if rec.get("outcome") == "skipped" and not (rec.get("alt_text") or "").strip():
            rec["outcome"] = "needs_manual_alt_text"

    known = {_record_basename(r.get("src")) for r in records} - {""}
    for img in image_files:
        name = Path(img).name
        if name in known:
            continue
        authored = harvested_alt.get(name, "")
        records.append({
            "src": name, "file_path": img,
            "alt_text": authored,
            "outcome": "harvested" if authored else "needs_manual_alt_text",
            "skip_reason": None,
        })
