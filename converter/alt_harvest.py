# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recover author-written ``/Alt`` from a source PDF's structure tree.

Docling reconstructs document structure from visual layout alone and never
opens the input's ``/StructTreeRoot``.  Any alt text an author already wrote is
therefore discarded at ingestion and later replaced by VLM output — or, when AI
is disabled, by a literal ``"Figure"`` placeholder.  This module reads that
authored alt text back out so it can be carried through the pipeline.

Author-written alt always wins over machine-generated alt.

The module is layered so the geometry is testable without PDF I/O:

``extract_source_alt``
    Walk a source PDF's structure tree and return its Figure/Formula alt text
    with page numbers and bounding boxes, in Docling's coordinate space.
``match_harvest_to_elements``
    Pure-geometry pairing of harvested entries against Docling struct elements.
``apply_harvest``
    Write matched alt into the struct JSON on disk and the extracted HTML.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path, PurePath
from typing import Any, Dict, List, Optional, Sequence

import pikepdf

logger = logging.getLogger(__name__)

# Alt text that carries no information.  Superset of the inline list used by
# ``audit/checks/image_checks.py`` — this is the canonical home.
GENERIC_PATTERNS = frozenset({
    "image", "picture", "photo", "graphic", "icon", "diagram",
    "figure", "chart", "logo", "img", "screenshot", "illustration",
    "a picture", "an image", "a photo", "a graphic",
})

# Placeholders this pipeline has itself emitted in the past.  Users re-upload
# our output, so rejecting these is load-bearing: without it a second pass
# harvests "Figure" straight back in and the fix defeats itself.
PIPELINE_PLACEHOLDER_RE = re.compile(
    r"""^(?:
          Figure
        | Formula
        | Figure\ on\ page\ \d+
        | Figure:\ .+\ \(page\ \d+\)
        | Unrecognized\ formula\ \(page\ \d+\)
    )$""",
    re.VERBOSE,
)

_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif",
                     ".tiff", ".svg", ".webp")

# Office object names that ride along as /Alt when a Word/PowerPoint document
# is exported to PDF ("Picture 8", "Text Box 9").  These are the authoring
# tool's internal shape names, not descriptions, and roughly one /Alt string
# in seven in a sample of real-world PDFs.  Harvesting them would be worse than useless: a non-generic alt
# also stops the VLM from generating a real description for that image.
_OFFICE_SHAPE_RE = re.compile(
    r"""^(?:text\ ?box|textbox|picture|rectangle|line|oval|group|image
        |chart|diagram|freeform|straight\ connector|elbow\ connector
        |connector|arrow|content\ placeholder|placeholder|shape|graphic
        |object|table|title|subtitle|footer|header|slide|frame|canvas
        |smart\ ?art|word\ ?art|flowchart|star|cloud|callout|autoshape
        )[\ _-]*\d+$""",
    re.IGNORECASE | re.VERBOSE,
)

# Camera filenames and PDF-tool artifacts: "_DSC1902", "P437TB1#y1".
# About one /Alt in nine in the same sample.  Deliberately narrow — genuine short alt such as
# "Skull" or "Books" must survive.
_OPAQUE_CODE_RE = re.compile(
    r"^(?:_\S+|\S*[#$%&*]\S*|(?:[A-Za-z]+\d+){2,}[A-Za-z0-9]*)$"
)


# Office appends its own signature to auto-generated alt ("A close up of a
# sign  Description automatically generated").  The description itself is
# worth keeping, but the signature is tool boilerplate a screen reader would
# read aloud, so trim it.
_OFFICE_BOILERPLATE_RE = re.compile(
    r"\s*Description\s+automatically\s+generated"
    r"(?:\s+with\s+\w+\s+confidence)?\s*\.?\s*$",
    re.IGNORECASE,
)


def strip_tool_boilerplate(alt: str) -> str:
    """Remove an authoring tool's own signature from an /Alt string."""
    return _OFFICE_BOILERPLATE_RE.sub("", alt or "").strip()


def _is_opaque_code(text: str) -> bool:
    return (" " not in text and len(text) <= 16
            and any(c.isdigit() for c in text)
            and bool(_OPAQUE_CODE_RE.match(text)))

# Structure-tree walk safety limits.  Real-world trees contain /K cycles.
_MAX_DEPTH = 100
_MAX_NODES = 200_000

# A box thinner than this in either axis carries no usable geometry.
_MIN_BBOX_EXTENT = 1.0


def is_our_placeholder(alt: str) -> bool:
    """True if ``alt`` is a placeholder this pipeline emitted."""
    return bool(PIPELINE_PLACEHOLDER_RE.match((alt or "").strip()))


def is_generic_alt(alt: Optional[str]) -> bool:
    """True if ``alt`` provides no information a screen reader could use."""
    cleaned = (alt or "").strip().lower().rstrip(".")
    if not cleaned:
        return True
    if cleaned in GENERIC_PATTERNS:
        return True
    if is_our_placeholder(alt or ""):
        return True
    if any(cleaned.endswith(ext) for ext in _IMAGE_EXTENSIONS):
        return True
    stripped = (alt or "").strip()
    if _OFFICE_SHAPE_RE.match(stripped):
        return True
    if _is_opaque_code(stripped):
        return True
    return len(cleaned) < 5


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _rotate_bbox_to_display(
    bbox: Dict[str, float], rotation: int, raw_w: float, raw_h: float,
) -> Dict[str, float]:
    """Map a bbox from content-stream space into Docling's display space.

    Docling reports bounding boxes after the page's ``/Rotate`` has been
    applied; ``/A /BBox`` values are in pre-rotation content-stream space.
    Mirrors the corner transform in ``output_gen/content_matcher.py``.
    """
    rotation = rotation % 360
    if rotation == 0:
        return bbox

    corners = [(bbox["l"], bbox["b"]), (bbox["r"], bbox["t"])]
    if rotation == 90:
        moved = [(y, raw_w - x) for x, y in corners]
    elif rotation == 180:
        moved = [(raw_w - x, raw_h - y) for x, y in corners]
    elif rotation == 270:
        moved = [(raw_h - y, x) for x, y in corners]
    else:
        return bbox

    xs = [p[0] for p in moved]
    ys = [p[1] for p in moved]
    return {"l": min(xs), "b": min(ys), "r": max(xs), "t": max(ys)}


def _normalize_bbox(
    raw: Optional[Sequence[Any]], page_geom: Dict[str, Any],
) -> Optional[Dict[str, float]]:
    """Normalize a raw ``/BBox`` into Docling's coordinate space.

    Returns None when the box is degenerate — a sliver carries no geometry
    worth matching on, and treating it as bbox-less lets the ordinal fallback
    have a turn instead.
    """
    if not raw or len(raw) < 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw[:4])
    except (TypeError, ValueError):
        return None

    # Docling's page space is 0-origin; the MediaBox may not be.
    x0 -= page_geom["origin_x"]
    x1 -= page_geom["origin_x"]
    y0 -= page_geom["origin_y"]
    y1 -= page_geom["origin_y"]

    bbox = {"l": min(x0, x1), "b": min(y0, y1),
            "r": max(x0, x1), "t": max(y0, y1)}

    if (bbox["r"] - bbox["l"] <= _MIN_BBOX_EXTENT
            or bbox["t"] - bbox["b"] <= _MIN_BBOX_EXTENT):
        return None

    return _rotate_bbox_to_display(
        bbox, page_geom["rotation"], page_geom["raw_w"], page_geom["raw_h"],
    )


def _bbox_from_attributes(
    node: pikepdf.Dictionary, page_geom: Optional[Dict[str, Any]],
) -> Optional[Dict[str, float]]:
    """Pull a layout ``/BBox`` off a structure element's ``/A`` attributes.

    ``/A`` may be a dictionary, an array of dictionaries, or an indirect
    reference to either.
    """
    if page_geom is None:
        return None
    try:
        attrs = node.get("/A")
    except Exception:
        return None
    if attrs is None:
        return None

    candidates = list(attrs) if isinstance(attrs, pikepdf.Array) else [attrs]

    fallback = None
    for cand in candidates:
        if not isinstance(cand, pikepdf.Dictionary):
            continue
        try:
            raw = cand.get("/BBox")
        except Exception:
            continue
        if raw is None:
            continue
        owner = str(cand.get("/O", ""))
        if owner == "/Layout":
            return _normalize_bbox(list(raw), page_geom)
        if fallback is None and not owner:
            fallback = list(raw)

    return _normalize_bbox(fallback, page_geom) if fallback else None


# ---------------------------------------------------------------------------
# Structure-tree extraction
# ---------------------------------------------------------------------------

def _build_role_map(struct_root: pikepdf.Dictionary) -> Dict[str, str]:
    try:
        raw = struct_root.get("/RoleMap")
    except Exception:
        return {}
    if not isinstance(raw, pikepdf.Dictionary):
        return {}
    out = {}
    for key, value in raw.items():
        try:
            out[str(key)] = str(value)
        except Exception:
            continue
    return out


def _resolve_tag(tag: str, role_map: Dict[str, str]) -> str:
    """Follow ``/RoleMap`` to a standard tag, breaking cycles."""
    seen = {tag}
    while tag in role_map:
        tag = role_map[tag]
        if tag in seen:
            break
        seen.add(tag)
    return tag


def _text_of(node: pikepdf.Dictionary, key: str) -> Optional[str]:
    try:
        raw = node.get(key)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        text = str(raw)
    except Exception:
        return None
    # Trailing NULs are common in Acrobat-authored /Alt strings.
    text = text.replace("\x00", "").strip()
    text = strip_tool_boilerplate(text)
    return text or None


def _page_rotation(page) -> int:
    """Read ``/Rotate``, inheriting it from the page tree when absent.

    Not every pikepdf build exposes ``Page.rotation``, so read the key
    directly and walk ``/Parent`` for the inheritable case.
    """
    node = getattr(page, "obj", page)
    for _ in range(_MAX_DEPTH):
        try:
            if node is None or "/Rotate" not in node:
                node = node.get("/Parent") if node is not None else None
                continue
            return int(node["/Rotate"]) % 360
        except Exception:
            return 0
    return 0


def _page_geometry(pdf: pikepdf.Pdf) -> tuple:
    """Return (objgen -> 1-based page no, page no -> geometry)."""
    page_index: Dict[Any, int] = {}
    geometry: Dict[int, Dict[str, Any]] = {}

    for idx, page in enumerate(pdf.pages, start=1):
        try:
            page_index[page.obj.objgen] = idx
        except Exception:
            pass

        try:
            box = [float(v) for v in page.mediabox]
        except Exception:
            box = [0.0, 0.0, 612.0, 792.0]
        origin_x, origin_y = min(box[0], box[2]), min(box[1], box[3])
        raw_w = abs(box[2] - box[0])
        raw_h = abs(box[3] - box[1])

        rotation = _page_rotation(page)

        geometry[idx] = {
            "origin_x": origin_x, "origin_y": origin_y,
            "raw_w": raw_w, "raw_h": raw_h, "rotation": rotation,
        }

    return page_index, geometry


def _pg_from_kids(node: pikepdf.Dictionary) -> Optional[Any]:
    """Return a /Pg reference carried by an MCR dict inside ``node``'s /K.

    Conforming producers may put /Pg on the marked-content reference rather
    than on the structure element itself (PDF 32000-1 14.7.4.3).  Reading it
    only from the element silently loses the page for every such figure, which
    made every author description on those documents unmatchable (G-02).

    Only a dict carrying /MCID counts: /Type is optional on an MCR, so it
    cannot be required, but /MCID is mandatory — which keeps an /OBJR or a
    nested child StructElem from donating the wrong page.
    """
    try:
        kids = node.get("/K")
    except Exception:
        return None
    if kids is None:
        return None
    candidates = kids if isinstance(kids, pikepdf.Array) else [kids]
    for kid in candidates:
        if not isinstance(kid, pikepdf.Dictionary):
            continue
        try:
            if "/MCID" in kid and "/Pg" in kid:
                return kid.get("/Pg")
        except Exception:
            continue
    return None


def extract_source_alt(
    pdf_path: str, *, use_actualtext_for_formula: bool = True,
) -> Dict[str, Any]:
    """Harvest authored Figure/Formula alt text from a source PDF.

    Returns a dict with ``has_struct_tree``, ``page_count``, ``figures``,
    ``formulas``, ``counts`` and ``error``.  Each harvested entry carries
    ``alt``, ``source``, ``page_no``, ``bbox`` (Docling display space, or
    None), ``tag``, ``raw_tag``, ``order`` and ``page_order``.

    Never raises: a malformed source PDF yields an empty harvest.
    """
    empty = {
        "has_struct_tree": False, "page_count": 0,
        "figures": [], "formulas": [],
        "counts": {
            "figures_total": 0, "figures_with_alt": 0, "figures_generic": 0,
            "figures_placeholder": 0, "with_bbox": 0, "no_page": 0,
            "formulas_total": 0, "formulas_with_alt": 0,
        },
        "error": None,
    }

    try:
        pdf = pikepdf.open(pdf_path)
    except Exception as exc:
        logger.warning("Alt harvest: cannot open %s: %s", pdf_path, exc)
        return {**empty, "error": str(exc)[:200]}

    with pdf:
        try:
            struct_root = pdf.Root.get("/StructTreeRoot")
        except Exception:
            struct_root = None
        if not isinstance(struct_root, pikepdf.Dictionary):
            return {**empty, "page_count": len(pdf.pages)}

        page_index, geometry = _page_geometry(pdf)
        role_map = _build_role_map(struct_root)
        counts = dict(empty["counts"])
        figures: List[Dict[str, Any]] = []
        formulas: List[Dict[str, Any]] = []
        per_page_order: Dict[Optional[int], int] = {}
        visited = set()
        order = 0
        nodes_seen = 0

        try:
            root_kids = struct_root.get("/K")
        except Exception:
            root_kids = None

        stack = [(root_kids, None, 0)]
        while stack:
            node, inherited_pg, depth = stack.pop()
            if node is None or depth > _MAX_DEPTH:
                continue
            nodes_seen += 1
            if nodes_seen > _MAX_NODES:
                logger.warning("Alt harvest: node cap hit on %s", pdf_path)
                break

            if isinstance(node, pikepdf.Array):
                for child in reversed(list(node)):
                    stack.append((child, inherited_pg, depth + 1))
                continue

            if not isinstance(node, pikepdf.Dictionary):
                continue  # integer MCIDs and the like

            try:
                objgen = node.objgen
                if objgen != (0, 0):
                    if objgen in visited:
                        continue
                    visited.add(objgen)
            except Exception:
                pass

            own_pg = node.get("/Pg") if "/Pg" in node else None
            page_ref = own_pg if own_pg is not None else inherited_pg

            raw_tag = str(node.get("/S", "")) if "/S" in node else ""
            resolved = _resolve_tag(raw_tag, role_map) if raw_tag else ""

            kind = None
            if resolved in ("/Figure", "/Image"):
                kind = "figure"
            elif resolved == "/Formula":
                kind = "formula"

            if kind:
                order += 1
                counts["figures_total" if kind == "figure"
                       else "formulas_total"] += 1

                page_no = None
                effective_pg = (page_ref if page_ref is not None
                                else _pg_from_kids(node))
                if effective_pg is not None:
                    try:
                        page_no = page_index.get(effective_pg.objgen)
                    except Exception:
                        page_no = None
                if page_no is None:
                    counts["no_page"] += 1

                alt = _text_of(node, "/Alt")
                source = "alt"
                if not alt and kind == "formula" and use_actualtext_for_formula:
                    # For a Formula, /ActualText IS the accessible
                    # representation.  For a Figure it is the glyph run
                    # underneath the image, which would be garbage as alt.
                    alt = _text_of(node, "/ActualText")
                    source = "actualtext"

                accepted = False
                if alt:
                    if is_our_placeholder(alt):
                        if kind == "figure":
                            counts["figures_placeholder"] += 1
                    elif is_generic_alt(alt):
                        if kind == "figure":
                            counts["figures_generic"] += 1
                    else:
                        accepted = True

                if accepted:
                    page_geom = geometry.get(page_no) if page_no else None
                    bbox = _bbox_from_attributes(node, page_geom)
                    if bbox is not None:
                        counts["with_bbox"] += 1
                    per_page_order[page_no] = per_page_order.get(page_no, 0) + 1
                    entry = {
                        "alt": alt, "source": source, "page_no": page_no,
                        "bbox": bbox, "tag": resolved, "raw_tag": raw_tag,
                        "order": order, "page_order": per_page_order[page_no],
                    }
                    if kind == "figure":
                        counts["figures_with_alt"] += 1
                        figures.append(entry)
                    else:
                        counts["formulas_with_alt"] += 1
                        formulas.append(entry)

            try:
                kids = node.get("/K")
            except Exception:
                kids = None
            if kids is not None:
                stack.append((kids, page_ref, depth + 1))

        return {
            "has_struct_tree": True,
            "page_count": len(pdf.pages),
            "figures": figures,
            "formulas": formulas,
            "counts": counts,
            "error": None,
        }


# ---------------------------------------------------------------------------
# Matching harvested alt against Docling struct elements
# ---------------------------------------------------------------------------

def _bbox_area(bbox: Optional[Dict[str, float]]) -> float:
    if not bbox:
        return 0.0
    return max(0.0, bbox["r"] - bbox["l"]) * max(0.0, bbox["t"] - bbox["b"])


def _intersection_area(a: Dict[str, float], b: Dict[str, float]) -> float:
    dx = min(a["r"], b["r"]) - max(a["l"], b["l"])
    dy = min(a["t"], b["t"]) - max(a["b"], b["b"])
    if dx <= 0 or dy <= 0:
        return 0.0
    return dx * dy


def _centroid_distance(a: Dict[str, float], b: Dict[str, float]) -> float:
    acx, acy = (a["l"] + a["r"]) / 2.0, (a["b"] + a["t"]) / 2.0
    bcx, bcy = (b["l"] + b["r"]) / 2.0, (b["b"] + b["t"]) / 2.0
    return ((acx - bcx) ** 2 + (acy - bcy) ** 2) ** 0.5


def _is_candidate(element: Dict[str, Any], kind: str) -> bool:
    """Targets eligible to receive harvested alt.

    A Figure must be backed by an extracted image: Docling reclassifies some
    columnar-text tables as Figure (``image_ref=None``) and pre-fills their
    ``alt_text`` from the table's own text.  Requiring ``image_ref`` keeps
    harvested alt away from those.  The empty-``alt_text`` guard makes the
    whole operation idempotent and never overwrites existing work.
    """
    if element.get("tag") != kind:
        return False
    if (element.get("alt_text") or "").strip():
        return False
    if kind == "Figure" and not element.get("image_ref"):
        return False
    return True


def match_harvest_to_elements(
    harvested: List[Dict[str, Any]],
    elements: List[Dict[str, Any]],
    kind: str,
    *,
    min_iou: float = 0.30,
    min_containment: float = 0.70,
    ordinal_fallback: bool = True,
) -> Dict[str, Any]:
    """Pair harvested source alt against Docling struct elements.

    Matching is page-scoped and one-to-one.  Geometry decides first; a
    conservative ordinal fallback only runs where geometry could not have had
    an opinion.  Pure function over plain dicts — no PDF or file I/O.
    """
    candidates: Dict[Optional[int], List[Dict[str, Any]]] = {}
    for element in elements:
        if _is_candidate(element, kind):
            candidates.setdefault(element.get("page_no"), []).append(element)

    by_page: Dict[Optional[int], List[Dict[str, Any]]] = {}
    for entry in harvested:
        by_page.setdefault(entry.get("page_no"), []).append(entry)

    matches: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    geometry_hits = 0
    ordinal_hits = 0

    for page_no, page_sources in sorted(
        by_page.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)
    ):
        page_targets = candidates.get(page_no, [])
        if not page_targets:
            unmatched.extend(page_sources)
            continue

        claimed_src, claimed_tgt = set(), set()

        # --- Geometry: score every pair, then take the best available first.
        scored = []
        for si, source in enumerate(page_sources):
            s_bbox = source.get("bbox")
            if not s_bbox:
                continue
            for ti, target in enumerate(page_targets):
                t_bbox = target.get("bbox")
                if not t_bbox:
                    continue
                inter = _intersection_area(s_bbox, t_bbox)
                if inter <= 0:
                    continue
                s_area, t_area = _bbox_area(s_bbox), _bbox_area(t_bbox)
                union = s_area + t_area - inter
                iou = inter / union if union > 0 else 0.0
                smaller = min(s_area, t_area)
                containment = inter / smaller if smaller > 0 else 0.0
                if iou < min_iou and containment < min_containment:
                    continue
                scored.append((
                    -max(iou, containment),
                    _centroid_distance(s_bbox, t_bbox),
                    source.get("page_order", 0),
                    target.get("reading_order", 0),
                    si, ti, max(iou, containment),
                ))

        for _, _, _, _, si, ti, score in sorted(scored):
            if si in claimed_src or ti in claimed_tgt:
                continue
            claimed_src.add(si)
            claimed_tgt.add(ti)
            matches.append({
                "elem_id": page_targets[ti]["id"],
                "alt": page_sources[si]["alt"],
                "source": page_sources[si].get("source", "alt"),
                "page_no": page_no,
                "score": round(score, 4),
                "method": "geometry",
            })
            geometry_hits += 1

        # --- Ordinal fallback, only where geometry could not have decided.
        residual_src = [i for i in range(len(page_sources))
                        if i not in claimed_src]
        residual_tgt = [i for i in range(len(page_targets))
                        if i not in claimed_tgt]

        if ordinal_fallback and residual_src and residual_tgt:
            # A source that HAS a bbox and still failed geometry was actively
            # rejected — never override that with position.
            blind_src = [i for i in residual_src if not page_sources[i].get("bbox")]
            eligible = (
                len(residual_src) == 1 and len(residual_tgt) == 1
                and not page_sources[residual_src[0]].get("bbox")
            ) or (
                len(residual_src) == len(residual_tgt)
                and len(blind_src) == len(residual_src)
            )
            if eligible:
                ordered_src = sorted(
                    residual_src,
                    key=lambda i: (page_sources[i].get("page_order", 0),
                                   page_sources[i].get("order", 0)))
                ordered_tgt = sorted(
                    residual_tgt,
                    key=lambda i: (page_targets[i].get("reading_order", 0),
                                   page_targets[i].get("id", "")))
                for si, ti in zip(ordered_src, ordered_tgt):
                    claimed_src.add(si)
                    claimed_tgt.add(ti)
                    matches.append({
                        "elem_id": page_targets[ti]["id"],
                        "alt": page_sources[si]["alt"],
                        "source": page_sources[si].get("source", "alt"),
                        "page_no": page_no,
                        "score": None,
                        "method": "ordinal",
                    })
                    ordinal_hits += 1

        unmatched.extend(page_sources[i] for i in range(len(page_sources))
                         if i not in claimed_src)

    return {
        "matches": matches,
        "unmatched_source": [
            {"page_no": u.get("page_no"), "chars": len(u.get("alt") or ""),
             "preview": (u.get("alt") or "")[:80]}
            for u in unmatched
        ],
        "stats": {
            "source_total": len(harvested),
            "candidates": sum(len(v) for v in candidates.values()),
            "matched": len(matches),
            "matched_geometry": geometry_hits,
            "matched_ordinal": ordinal_hits,
            "unmatched": len(unmatched),
        },
    }


# ---------------------------------------------------------------------------
# Applying the harvest to the struct JSON and the extracted HTML
# ---------------------------------------------------------------------------

def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """Write beside the target then replace, so a crash cannot truncate it."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _apply_to_html(
    html_path: str,
    img_alt: Dict[str, str],
    formula_alt: Dict[int, str],
) -> Dict[str, int]:
    """Write harvested alt into the extracted HTML.

    Writing alt here (before Phase 5) is what makes author text win: the VLM's
    ``_find_images_needing_alt`` skips any image that already has non-generic
    alt, so no VLM call is made and nothing overwrites the author.
    """
    counts = {"html_imgs_updated": 0, "html_formulas_updated": 0}
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        logger.warning("Alt harvest: BeautifulSoup unavailable: %s", exc)
        return counts

    try:
        with open(html_path, encoding="utf-8") as handle:
            soup = BeautifulSoup(handle.read(), "html.parser")
    except Exception as exc:
        logger.warning("Alt harvest: cannot read HTML %s: %s", html_path, exc)
        return counts

    for img in soup.find_all("img"):
        src = img.get("src") or ""
        alt = img_alt.get(PurePath(src).name)
        if not alt:
            continue
        # Never clobber alt a human already wrote.
        if not is_generic_alt(img.get("alt")):
            continue
        img["alt"] = alt
        counts["html_imgs_updated"] += 1

    for seq_index, alt in formula_alt.items():
        node = soup.find(attrs={"data-formula-index": str(seq_index)})
        if node is None:
            continue
        if not is_generic_alt(node.get("aria-label")):
            continue
        node["aria-label"] = alt
        counts["html_formulas_updated"] += 1

    if counts["html_imgs_updated"] or counts["html_formulas_updated"]:
        try:
            with open(html_path, "w", encoding="utf-8") as handle:
                handle.write(str(soup))
        except Exception as exc:
            logger.warning("Alt harvest: cannot write HTML %s: %s",
                           html_path, exc)
            return {"html_imgs_updated": 0, "html_formulas_updated": 0}

    return counts


def apply_harvest(
    structure_json_path: str,
    source_pdf_path: str,
    *,
    html_path: Optional[str] = None,
    min_iou: float = 0.30,
    min_containment: float = 0.70,
    ordinal_fallback: bool = True,
) -> Dict[str, Any]:
    """Harvest authored alt from the source PDF into this document's artifacts.

    Writes the struct JSON **on disk** — the pipeline re-reads it from disk at
    Phase 8 and hands Phase 11 the file path, so an in-memory mutation alone
    would be silently discarded.

    Never raises: any failure yields a no-op result with ``skipped_reason``.
    """
    stats: Dict[str, Any] = {
        "has_struct_tree": False, "source_figures": 0,
        "source_figures_with_alt": 0, "matched_figures": 0,
        "matched_formulas": 0, "matched_geometry": 0, "matched_ordinal": 0,
        "unmatched_source_alt": [], "html_imgs_updated": 0,
        "html_formulas_updated": 0, "mean_alt_chars": 0,
        "skipped_reason": None,
    }

    struct_path = Path(structure_json_path)
    try:
        with open(struct_path, encoding="utf-8") as handle:
            structure = json.load(handle)
    except Exception as exc:
        logger.warning("Alt harvest: cannot read %s: %s", struct_path, exc)
        stats["skipped_reason"] = "struct_json_unreadable"
        return stats

    harvest = extract_source_alt(source_pdf_path)
    stats["has_struct_tree"] = harvest["has_struct_tree"]
    stats["source_figures"] = harvest["counts"]["figures_total"]
    stats["source_figures_with_alt"] = harvest["counts"]["figures_with_alt"]

    if not harvest["has_struct_tree"]:
        stats["skipped_reason"] = "no_struct_tree"
        return stats
    if not harvest["figures"] and not harvest["formulas"]:
        stats["skipped_reason"] = "no_authored_alt"
        return stats

    # Docling converts a rewrite of the same PDF, so page counts must agree.
    # Divergence means an assumption broke; matching by page would be wrong.
    struct_pages = len(structure.get("pages") or {})
    if struct_pages and struct_pages != harvest["page_count"]:
        logger.warning(
            "Alt harvest: page count mismatch (struct=%d, source=%d) — skipping",
            struct_pages, harvest["page_count"])
        stats["skipped_reason"] = "page_count_mismatch"
        return stats

    elements = structure.get("elements", [])
    by_id = {e.get("id"): e for e in elements}

    fig_result = match_harvest_to_elements(
        harvest["figures"], elements, "Figure", min_iou=min_iou,
        min_containment=min_containment, ordinal_fallback=ordinal_fallback)
    formula_result = match_harvest_to_elements(
        harvest["formulas"], elements, "Formula", min_iou=min_iou,
        min_containment=min_containment, ordinal_fallback=ordinal_fallback)

    # Formula HTML is addressed by position among FormulaItem elements, the
    # same enumeration vlm/formula_processor.py uses for data-formula-index.
    formula_seq = {
        e.get("id"): i for i, e in enumerate(
            e for e in elements
            if e.get("type") == "FormulaItem" and e.get("tag") == "Formula")
    }

    img_alt: Dict[str, str] = {}
    formula_alt: Dict[int, str] = {}
    alt_lengths: List[int] = []

    for match in fig_result["matches"] + formula_result["matches"]:
        element = by_id.get(match["elem_id"])
        if element is None:
            continue
        element["alt_text"] = match["alt"]
        # Read by vlm/formula_processor.py, which would otherwise overwrite it.
        element["alt_source"] = "source_pdf"
        alt_lengths.append(len(match["alt"]))
        image_ref = element.get("image_ref")
        if image_ref:
            img_alt[PurePath(str(image_ref)).name] = match["alt"]
        elif element.get("id") in formula_seq:
            formula_alt[formula_seq[element["id"]]] = match["alt"]

    stats["matched_figures"] = len(fig_result["matches"])
    stats["matched_formulas"] = len(formula_result["matches"])
    stats["matched_geometry"] = (fig_result["stats"]["matched_geometry"]
                                 + formula_result["stats"]["matched_geometry"])
    stats["matched_ordinal"] = (fig_result["stats"]["matched_ordinal"]
                                + formula_result["stats"]["matched_ordinal"])
    stats["unmatched_source_alt"] = (fig_result["unmatched_source"]
                                     + formula_result["unmatched_source"])
    stats["mean_alt_chars"] = (
        int(sum(alt_lengths) / len(alt_lengths)) if alt_lengths else 0)

    if (stats.get("source_figures_with_alt")
            and stats.get("matched_figures") == 0
            and stats.get("skipped_reason") is None):
        # Alt text existed and none of it was applied.  Never leave this
        # silent — it is the production incident's exact signature (G-02).
        stats["skipped_reason"] = "no_alt_applied"
        logger.warning(
            "Alt harvest: %d source figures carried alt text but none could be "
            "matched to a page — author descriptions were NOT applied",
            stats.get("source_figures_with_alt"),
        )

    if not alt_lengths:
        return stats

    try:
        _write_json_atomic(struct_path, structure)
    except Exception as exc:
        logger.warning("Alt harvest: cannot write %s: %s", struct_path, exc)
        stats["skipped_reason"] = "struct_json_unwritable"
        stats["matched_figures"] = 0
        stats["matched_formulas"] = 0
        return stats

    if html_path:
        stats.update(_apply_to_html(html_path, img_alt, formula_alt))

    for miss in stats["unmatched_source_alt"]:
        logger.warning(
            "Alt harvest: source alt on page %s had no matching figure (%d chars)",
            miss.get("page_no"), miss.get("chars", 0))

    return stats
