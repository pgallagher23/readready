#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Post-process a tagged PDF to fix/rebuild the ParentTree.

Rebuilds the ParentTree by scanning all structure elements for MCID
references, then mapping them to pages via /Pg. This ensures every
MCID in the content streams has a ParentTree entry.

Usage:
    python fix_parent_tree.py input.pdf output.pdf
"""

import re
import sys
from collections import defaultdict
from typing import Dict, Optional, Set

import pikepdf


def fix_parent_tree(input_path: str, output_path: str) -> dict:
    """Rebuild ParentTree from structure elements and content streams."""
    pdf = pikepdf.open(input_path, allow_overwriting_input=(
        input_path == output_path))

    struct_tree_root = pdf.Root.get("/StructTreeRoot")
    if struct_tree_root is None:
        pdf.save(output_path)
        pdf.close()
        return {"error": "No StructTreeRoot"}

    # --- Step 1: Build page objgen -> page index mapping ---
    # Use (objgen) tuples as keys since Python object identity
    # doesn't work with pikepdf's proxy objects
    page_objgen_to_idx = {}
    for i, page in enumerate(pdf.pages):
        try:
            page_objgen_to_idx[page.objgen] = i
        except Exception:
            pass

    # --- Step 2: Scan ALL objects for StructElems with MCIDs ---
    # Map: page_index -> {mcid -> SE_object}
    page_mcid_to_se: Dict[int, Dict[int, object]] = defaultdict(dict)
    se_count = 0

    for obj in pdf.objects:
        try:
            if not isinstance(obj, pikepdf.Dictionary):
                continue
            if obj.get("/Type") != pikepdf.Name.StructElem:
                continue

            se_count += 1
            pg = obj.get("/Pg")
            k = obj.get("/K")
            if k is None:
                continue

            # Resolve /Pg to page index
            pg_idx = _resolve_page_idx(pg, page_objgen_to_idx)

            # Extract MCIDs from /K
            _extract_mcids(obj, k, pg_idx, page_objgen_to_idx,
                            page_mcid_to_se)

        except Exception:
            pass

    # --- Step 3: Get MCIDs from content streams for validation ---
    content_mcids: Dict[int, Set[int]] = {}
    for i, page in enumerate(pdf.pages):
        try:
            raw = _get_page_content(page)
            if not raw:
                continue
            mcids = {int(m.group(1))
                     for m in re.finditer(rb'/MCID\s+(\d+)', raw)}
            if mcids:
                content_mcids[i] = mcids
        except Exception:
            pass

    # --- Step 4: Build new ParentTree ---
    nums = pikepdf.Array()
    total_mapped = 0
    total_missing = 0
    pages_fixed = 0

    for i, page in enumerate(pdf.pages):
        sp_obj = page.get("/StructParents")
        if sp_obj is None:
            continue
        sp_val = int(sp_obj)

        # Merge MCIDs from structure tree and content stream
        all_mcids = set(page_mcid_to_se.get(i, {}).keys())
        all_mcids.update(content_mcids.get(i, set()))

        if not all_mcids:
            continue

        max_mcid = max(all_mcids)
        se_map = page_mcid_to_se.get(i, {})

        # Build array indexed by MCID
        se_array = pikepdf.Array()
        for m in range(max_mcid + 1):
            se = se_map.get(m)
            if se is not None:
                se_array.append(se)
                total_mapped += 1
            else:
                se_array.append(pikepdf.Object.parse(b"null"))
                if m in content_mcids.get(i, set()):
                    total_missing += 1

        nums.append(sp_val)
        nums.append(se_array)
        pages_fixed += 1

    # Replace ParentTree
    parent_tree = pikepdf.Dictionary({"/Nums": nums})
    struct_tree_root["/ParentTree"] = pdf.make_indirect(parent_tree)
    struct_tree_root["/ParentTreeNextKey"] = pages_fixed

    pdf.save(output_path)
    pdf.close()

    return {
        "struct_elements": se_count,
        "pages_with_mcids": len(content_mcids),
        "pages_fixed": pages_fixed,
        "total_mapped": total_mapped,
        "total_missing": total_missing,
    }


def _resolve_page_idx(pg_ref, page_objgen_to_idx) -> Optional[int]:
    """Resolve a /Pg reference to a page index."""
    if pg_ref is None:
        return None
    try:
        # Use objgen tuple for comparison
        objgen = pg_ref.objgen
        if objgen in page_objgen_to_idx:
            return page_objgen_to_idx[objgen]
    except Exception:
        pass
    return None


def _extract_mcids(se_obj, k, pg_idx, page_objgen_to_idx, result):
    """Extract MCIDs from a /K entry and add to result mapping."""
    # /K can be: bare integer, MCR dict, array of these

    # Bare integer
    try:
        mcid = int(k)
        if pg_idx is not None:
            result[pg_idx][mcid] = se_obj
        return
    except (TypeError, ValueError):
        pass

    # Dictionary (MCR)
    if isinstance(k, pikepdf.Dictionary):
        k_type = k.get("/Type")
        if k_type == pikepdf.Name("/MCR"):
            mcid = k.get("/MCID")
            mcr_pg = k.get("/Pg")
            if mcid is not None:
                idx = _resolve_page_idx(mcr_pg, page_objgen_to_idx)
                if idx is None:
                    idx = pg_idx
                if idx is not None:
                    result[idx][int(mcid)] = se_obj
        return

    # Array
    if isinstance(k, pikepdf.Array):
        for item in k:
            try:
                # Bare integer in array
                mcid = int(item)
                if pg_idx is not None:
                    result[pg_idx][mcid] = se_obj
                continue
            except (TypeError, ValueError):
                pass

            # Dictionary in array
            if isinstance(item, pikepdf.Dictionary):
                item_type = item.get("/Type")
                if item_type == pikepdf.Name("/MCR"):
                    mcid = item.get("/MCID")
                    mcr_pg = item.get("/Pg")
                    if mcid is not None:
                        idx = _resolve_page_idx(mcr_pg, page_objgen_to_idx)
                        if idx is None:
                            idx = pg_idx
                        if idx is not None:
                            result[idx][int(mcid)] = se_obj


def _get_page_content(page) -> Optional[bytes]:
    """Get content stream bytes for a page."""
    contents = page.get("/Contents")
    if contents is None:
        return None
    try:
        if isinstance(contents, pikepdf.Array):
            parts = []
            for ref in contents:
                try:
                    parts.append(ref.read_bytes())
                except Exception:
                    pass
            return b"\n".join(parts) if parts else None
        return contents.read_bytes()
    except Exception:
        return None


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python fix_parent_tree.py input.pdf output.pdf")
        sys.exit(1)

    result = fix_parent_tree(sys.argv[1], sys.argv[2])
    print("ParentTree fix results:")
    for k, v in result.items():
        print(f"  {k}: {v}")
