# SPDX-License-Identifier: AGPL-3.0-or-later
"""Extract PDF structure tree for screen reader preview.

Reads the actual tagged PDF's StructTreeRoot and returns a nested hierarchy
suitable for rendering as an interactive tree widget in the web UI.

This lets users verify what assistive technology will actually consume,
rather than relying on the intermediate HTML representation.

Usage:
    from output_gen.screen_reader_preview import extract_structure_tree

    tree = extract_structure_tree("output_accessible.pdf")
    # tree is a nested dict: {"tag": "Document", "children": [...], ...}
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def extract_structure_tree(pdf_path: str) -> Dict[str, Any]:
    """Extract structure tree from tagged PDF for screen reader preview.

    Args:
        pdf_path: Path to the tagged PDF.

    Returns:
        Dict with:
            pages: list[dict] — per-page structure, each with:
                page: int — 1-based page number
                elements: list[dict] — top-level elements on that page
            total_pages: int — total number of pages in the PDF
        Each element node has:
            tag: str — structure element type (Document, H1, P, Table, etc.)
            text: str | None — /ActualText content
            alt: str | None — /Alt description (figures)
            page: int | None — 1-based page number from /Pg reference
            children: list[dict] — child nodes
        On error, returns {"error": "message"}.
    """
    try:
        import fitz
    except ImportError:
        return {"error": "PyMuPDF not installed"}

    if not Path(pdf_path).exists():
        return {"error": f"PDF not found: {pdf_path}"}

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return {"error": f"Cannot open PDF: {e}"}

    try:
        # Build page xref → page number map
        page_xref_map: Dict[int, int] = {}
        for i in range(len(doc)):
            page_xref_map[doc[i].xref] = i + 1

        cat_str = doc.xref_object(doc.pdf_catalog(), compressed=False)

        str_root_m = re.search(r"/StructTreeRoot\s+(\d+)\s+0\s+R", cat_str)
        if not str_root_m:
            return {"error": "PDF has no structure tree (not tagged)"}

        sr_str = doc.xref_object(int(str_root_m.group(1)), compressed=False)
        doc_xref = _find_document_element(doc, sr_str)
        if doc_xref is None:
            return {"error": "Cannot locate Document structure element"}

        tree = _walk_tree(doc, doc_xref, page_xref_map)
        _flatten_passthrough_containers(tree)

        # Group top-level elements by page
        pages_dict: Dict[int, List[Dict]] = {}
        for child in tree.get("children", []):
            pg = child.get("page") or _infer_page(child) or 1
            pages_dict.setdefault(pg, []).append(child)

        pages_list = [
            {"page": pg, "elements": elems}
            for pg, elems in sorted(pages_dict.items())
        ]

        return {
            "pages": pages_list,
            "total_pages": len(doc),
        }
    except Exception as e:
        logger.exception("Error extracting structure tree from %s", pdf_path)
        return {"error": f"Extraction failed: {e}"}
    finally:
        doc.close()


def _infer_page(node: Dict[str, Any]) -> Optional[int]:
    """Infer page number from a node's first child with a page reference."""
    for child in node.get("children", []):
        pg = child.get("page")
        if pg:
            return pg
        deep = _infer_page(child)
        if deep:
            return deep
    return None


def structure_tree_to_json(tree: Dict[str, Any]) -> str:
    """Serialize tree to compact JSON string for frontend consumption."""
    return json.dumps(tree, ensure_ascii=False, separators=(",", ":"))


# Tags that are pure grouping wrappers with no semantic value of their own.
# When these appear as direct children of Document, their children are
# promoted up so the user sees meaningful elements (H1, Table, etc.) first.
_PASSTHROUGH_TAGS = frozenset({"Sect", "Div", "Part", "Art"})


def _flatten_passthrough_containers(tree: Dict[str, Any]) -> None:
    """Flatten passthrough containers that are direct children of Document.

    Replaces e.g. Document > [Sect > [H1, Table], Sect > [H1, P]]
    with         Document > [H1, Table, H1, P]
    so the tree opens to semantically meaningful elements.
    """
    children = tree.get("children")
    if not children:
        return

    new_children: List[Dict[str, Any]] = []
    for child in children:
        tag = child.get("tag", "")
        # Flatten passthrough containers that have no text/alt of their own
        if tag in _PASSTHROUGH_TAGS and not child.get("text") and not child.get("alt"):
            new_children.extend(child.get("children", []))
        else:
            new_children.append(child)
    tree["children"] = new_children


# ======================================================================
# Internal helpers — parsing logic adapted from struct_tree_validator.py
# ======================================================================


def _find_document_element(doc, sr_str: str) -> Optional[int]:
    """Find the Document structure element xref from StructTreeRoot."""
    # /K [ N 0 R ]
    k_arr = re.search(r"/K\s*\[\s*(\d+)\s+0\s+R", sr_str)
    if k_arr:
        return int(k_arr.group(1))
    # /K N 0 R
    k_single = re.search(r"/K\s+(\d+)\s+0\s+R", sr_str)
    if k_single:
        return int(k_single.group(1))
    return None


def _walk_tree(
    doc, xref: int, page_xref_map: Optional[Dict[int, int]] = None,
) -> Dict[str, Any]:
    """Recursively walk the structure tree, returning nested dicts."""
    try:
        obj_str = doc.xref_object(xref, compressed=False)
    except Exception:
        return {"tag": "Unknown", "text": None, "alt": None, "children": []}

    # Tag name
    s_m = re.search(r"/S\s+/(\w+)", obj_str)
    tag = s_m.group(1) if s_m else "Unknown"

    # Page reference
    page = None
    if page_xref_map:
        pg_m = re.search(r"/Pg\s+(\d+)\s+0\s+R", obj_str)
        if pg_m:
            page = page_xref_map.get(int(pg_m.group(1)))

    # Text and alt
    text = _get_actual_text(doc, xref, obj_str)
    alt = _get_alt_text(obj_str)

    # Child structure elements
    child_xrefs = _get_child_struct_elems(doc, obj_str)
    children = [_walk_tree(doc, cx, page_xref_map) for cx in child_xrefs]

    node: Dict[str, Any] = {"tag": tag}
    if page:
        node["page"] = page
    if text:
        node["text"] = text
    if alt:
        node["alt"] = alt
    if children:
        node["children"] = children

    return node


def _get_actual_text(doc, xref: int, obj_str: str) -> Optional[str]:
    """Extract /ActualText from a structure element."""
    # Hex-encoded /ActualText (UTF-16BE with BOM)
    hex_m = re.search(r"/ActualText\s*<([0-9a-fA-F\s]+)>", obj_str, re.DOTALL)
    if hex_m:
        h = hex_m.group(1).replace("\n", "").replace(" ", "")
        try:
            bs = bytes.fromhex(h)
            if bs[:2] == b"\xfe\xff":
                text = bs[2:].decode("utf-16-be", errors="replace")
                if text.strip():
                    return text
            else:
                text = bs.decode("utf-8", errors="replace")
                if text.strip():
                    return text
        except ValueError:
            pass

    # Literal /ActualText in parentheses
    lit_m = re.search(r"/ActualText\s*\(", obj_str)
    if lit_m:
        start = lit_m.end()
        depth = 1
        i = start
        while i < len(obj_str) and depth > 0:
            c = obj_str[i]
            if c == "(" and (i == 0 or obj_str[i - 1] != "\\"):
                depth += 1
            elif c == ")" and (i == 0 or obj_str[i - 1] != "\\"):
                depth -= 1
            i += 1
        text = obj_str[start : i - 1]
        if text.strip():
            return text

    return None


def _get_alt_text(obj_str: str) -> Optional[str]:
    """Extract /Alt from a structure element (used for Figures)."""
    # Hex /Alt
    alt_hex = re.search(r"/Alt\s*<([0-9a-fA-F\s]+)>", obj_str, re.DOTALL)
    if alt_hex:
        h = alt_hex.group(1).replace("\n", "").replace(" ", "")
        try:
            bs = bytes.fromhex(h)
            if bs[:2] == b"\xfe\xff":
                text = bs[2:].decode("utf-16-be", errors="replace")
                if text.strip():
                    return text
        except ValueError:
            pass

    # Literal /Alt
    alt_m = re.search(r"/Alt\s*\(", obj_str)
    if alt_m:
        start = alt_m.end()
        depth = 1
        i = start
        while i < len(obj_str) and depth > 0:
            c = obj_str[i]
            if c == "(" and (i == 0 or obj_str[i - 1] != "\\"):
                depth += 1
            elif c == ")" and (i == 0 or obj_str[i - 1] != "\\"):
                depth -= 1
            i += 1
        text = obj_str[start : i - 1]
        if text.strip():
            return text

    return None


def _get_child_struct_elems(doc, obj_str: str) -> List[int]:
    """Extract child structure element xrefs from /K."""
    child_xrefs = []

    k_arr = re.search(r"/K\s*\[([^\]]+)\]", obj_str)
    if k_arr:
        for r in re.findall(r"(\d+)\s+0\s+R", k_arr.group(1)):
            try:
                co = doc.xref_object(int(r), compressed=False)
                if "/Type /StructElem" in co:
                    child_xrefs.append(int(r))
            except Exception:
                pass
    else:
        k_s = re.search(r"/K\s+(\d+)\s+0\s+R", obj_str)
        if k_s:
            try:
                co = doc.xref_object(int(k_s.group(1)), compressed=False)
                if "/Type /StructElem" in co:
                    child_xrefs.append(int(k_s.group(1)))
            except Exception:
                pass

    return child_xrefs


# ======================================================================
# JSON-based preview — reads enriched structure JSON instead of PDF
# ======================================================================


def build_sr_data_from_structure_json(
    structure_json_path: str,
) -> Dict[str, Any]:
    """Build screen reader preview data from the enriched structure JSON.

    This replaces PDF-based extraction (which relied on /ActualText, now
    removed from structure elements). The enriched JSON is the source of
    truth for what text gets written into the tagged PDF.

    Returns the same page-grouped format as extract_structure_tree().
    """
    path = Path(structure_json_path)
    if not path.exists():
        return {"error": f"Structure JSON not found: {structure_json_path}"}

    try:
        structure = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"Failed to read structure JSON: {e}"}

    elements = structure.get("elements", [])
    pages_info = structure.get("pages", {})

    # Determine total pages from pages_info keys or max page_no
    if pages_info:
        total_pages = max(int(k) for k in pages_info.keys()) if pages_info else 1
    elif elements:
        total_pages = max(e.get("page_no", 1) for e in elements)
    else:
        total_pages = 0

    # Convert each element to an SR preview node, grouped by page
    pages_dict: Dict[int, List[Dict]] = {}
    for elem in elements:
        node = _elem_to_sr_node(elem)
        if node:
            pg = elem.get("page_no", 1) or 1
            pages_dict.setdefault(pg, []).append(node)

    pages_list = [
        {"page": pg, "elements": elems}
        for pg, elems in sorted(pages_dict.items())
    ]

    return {
        "pages": pages_list,
        "total_pages": total_pages,
    }


def _elem_to_sr_node(elem: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert an enriched structure JSON element to an SR preview node."""
    tag = elem.get("tag", "")
    if not tag:
        return None

    node: Dict[str, Any] = {"tag": tag}

    elem_id = elem.get("id")
    if elem_id:
        node["id"] = elem_id

    page = elem.get("page_no")
    if page:
        node["page"] = page

    # Text: use alt_text for Figures/Formulas, text for everything else
    alt_text = elem.get("alt_text")
    if alt_text:
        node["alt"] = alt_text

    text = elem.get("text")
    if text:
        node["text"] = text

    # Build children from table cells or explicit children
    children: List[Dict[str, Any]] = []

    # Table cells → TR > TH/TD hierarchy
    cells = elem.get("cells")
    if cells and tag == "Table":
        children = _cells_to_table_children(cells, elem_id or "")

    # Explicit children (Link, Span, etc.)
    for child in elem.get("children", []):
        child_node = _child_to_sr_node(child, elem_id or "")
        if child_node:
            children.append(child_node)

    if children:
        node["children"] = children

    return node


def _child_to_sr_node(
    child: Dict[str, Any], parent_id: str,
) -> Optional[Dict[str, Any]]:
    """Convert a child element (Link, Span, etc.) to an SR preview node."""
    tag = child.get("tag", "")
    if not tag:
        return None

    node: Dict[str, Any] = {"tag": tag}

    child_id = child.get("id", f"{parent_id}.child_{tag}")
    node["id"] = child_id

    text = child.get("text")
    if text:
        node["text"] = text

    alt = child.get("alt_text")
    if alt:
        node["alt"] = alt

    return node


def _cells_to_table_children(
    cells: List[Dict[str, Any]], table_id: str,
) -> List[Dict[str, Any]]:
    """Convert flat cells array to TR > TH/TD hierarchy for SR preview."""
    # Group cells by row
    rows: Dict[int, List[Dict]] = {}
    for cell in cells:
        r = cell.get("row", 0)
        rows.setdefault(r, []).append(cell)

    tr_nodes: List[Dict[str, Any]] = []
    for row_idx in sorted(rows.keys()):
        row_cells = sorted(rows[row_idx], key=lambda c: c.get("col", 0))
        td_nodes: List[Dict[str, Any]] = []
        for cell in row_cells:
            cell_tag = "TH" if cell.get("is_header") else "TD"
            cell_id = f"{table_id}.cell_{cell.get('row', row_idx)}_{cell.get('col', 0)}"
            cell_node: Dict[str, Any] = {
                "tag": cell_tag,
                "id": cell_id,
            }
            cell_text = cell.get("text")
            if cell_text:
                cell_node["text"] = cell_text
            td_nodes.append(cell_node)

        tr_node: Dict[str, Any] = {"tag": "TR", "children": td_nodes}
        tr_nodes.append(tr_node)

    return tr_nodes
