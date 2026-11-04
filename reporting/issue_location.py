# SPDX-License-Identifier: AGPL-3.0-or-later
"""Layer 1: issue -> (page, bbox) attribution and optional region rendering.

No dependency on other reporting modules, app.py, or gradio. PyMuPDF (fitz),
base64, and io are imported lazily inside the thumbnail renderers, exactly as
in the original app.py code.
"""
from typing import Optional

import base64


def _get_issue_location(issue: dict, structure: dict) -> tuple:
    """Map an audit issue to (page_no, bbox) using the structure JSON.

    Matches by element type and reading-order index.
    Returns (None, None) if no match is found.
    """
    issue_type = issue.get("type", "")
    context = issue.get("context") or {}
    elem_name = context.get("element_name", "") if isinstance(context, dict) else ""
    attrs = (context.get("attributes") or {}) if isinstance(context, dict) else {}
    pos = context.get("position") or {}
    idx = pos.get("index", 0) if isinstance(pos, dict) else 0

    elements = structure.get("elements", [])

    # Formula must be checked before the "alt" branch: formula issue types
    # (e.g. "formula-missing-alt-text") contain "alt" and would otherwise be
    # misrouted to FigureItem.
    if "formula" in issue_type or attrs.get("role") == "math":
        target = "FormulaItem"
        # Formula HTML carries a 0-based data-formula-index assigned in reading
        # order; it maps 1:1 to the ordered FormulaItem list in the structure.
        # position.index counts a different element set and must NOT be used here.
        fidx = attrs.get("data-formula-index")
        if fidx is not None:
            try:
                idx = int(fidx)
            except (TypeError, ValueError):
                pass
    elif "alt" in issue_type or "figure" in issue_type or elem_name in ("img", "figure"):
        target = "FigureItem"
    elif "table" in issue_type or elem_name == "table":
        target = "TableItem"
    elif (any(x in issue_type for x in ("heading", "h1", "h2"))
          or elem_name in ("h1", "h2", "h3", "h4", "h5", "h6")):
        target = "SectionHeaderItem"
    else:
        target = None

    if target:
        candidates = [e for e in elements if e.get("type") == target]
        if 0 <= idx < len(candidates):
            e = candidates[idx]
            return e.get("page_no"), e.get("bbox")

    # Unresolved: report "unknown" (None) rather than defaulting to page 1.
    # Callers keep such issues page-less instead of piling them on page 1.
    return None, None


def _render_issue_thumbnail(
    pdf_path: str,
    page_no: int,
    bbox: Optional[dict],
    pages_info: dict,
    dpi: int = 120,
    padding: int = 30,
    annotate: bool = False,
) -> Optional[str]:
    """Render a PDF region as a base64-encoded PNG thumbnail.

    When *annotate* is True and a bbox is provided, a red outline is drawn
    around the element region on the rendered image.

    Uses PyMuPDF.  Returns None on any failure.
    """
    try:
        import fitz
        doc = fitz.open(pdf_path)
        if page_no < 1 or page_no > len(doc):
            doc.close()
            return None

        page = doc[page_no - 1]
        ph = pages_info.get(str(page_no), {}).get("height", page.rect.height)

        # Element bbox in fitz coordinates (for annotation drawing)
        elem_fitz_rect = None

        if bbox and all(k in bbox for k in ("l", "t", "r", "b")):
            # Convert from bottom-left PDF origin to top-left fitz origin
            raw_rect = fitz.Rect(
                bbox["l"],
                ph - bbox["t"],
                bbox["r"],
                ph - bbox["b"],
            )
            elem_fitz_rect = raw_rect
            fitz_rect = fitz.Rect(
                bbox["l"] - padding,
                ph - bbox["t"] - padding,
                bbox["r"] + padding,
                ph - bbox["b"] + padding,
            ) & page.rect
        else:
            fitz_rect = page.rect
            dpi = 120  # full-page thumbnails at readable resolution

        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)

        # Draw red outline annotation if requested
        annot = None
        if annotate and elem_fitz_rect is not None:
            annot = page.add_rect_annot(elem_fitz_rect)
            annot.set_colors(stroke=(1, 0, 0))
            annot.set_border(width=2)
            annot.update()

        pix = page.get_pixmap(matrix=mat, clip=fitz_rect)

        # Remove temporary annotation
        if annot is not None:
            page.delete_annot(annot)

        doc.close()
        return base64.b64encode(pix.tobytes("png")).decode("ascii")
    except Exception:
        return None


def _render_coverage_thumbnail(
    pdf_path: str,
    page_no: int,
    elements: list[dict],
    pages_info: dict,
    dpi: int = 100,
) -> Optional[str]:
    """Render a full-page thumbnail with tagged element bboxes highlighted.

    Green semi-transparent overlays mark tagged regions; untagged areas remain
    bare, making coverage gaps immediately visible.  Returns base64 PNG or None.
    """
    try:
        import fitz
        doc = fitz.open(pdf_path)
        if page_no < 1 or page_no > len(doc):
            doc.close()
            return None

        page = doc[page_no - 1]
        ph = pages_info.get(str(page_no), {}).get("height", page.rect.height)

        # Collect element bboxes on this page
        page_elems = [
            e for e in elements
            if e.get("page_no") == page_no
            and e.get("bbox")
            and all(k in e["bbox"] for k in ("l", "t", "r", "b"))
        ]

        # Draw green overlay rectangles for tagged elements
        annots = []
        for e in page_elems:
            bb = e["bbox"]
            # Convert from PDF bottom-left origin to fitz top-left origin
            rect = fitz.Rect(bb["l"], ph - bb["t"], bb["r"], ph - bb["b"])
            rect = rect & page.rect  # clamp to page
            if rect.is_empty or rect.is_infinite:
                continue
            tag = e.get("tag", "")
            # Color by element type
            if tag in ("Figure", "FigureItem"):
                color = (0.2, 0.5, 0.9)   # blue for images
            elif tag in ("Table", "TableItem"):
                color = (0.9, 0.6, 0.1)   # orange for tables
            else:
                color = (0.15, 0.7, 0.3)  # green for text elements
            annot = page.add_rect_annot(rect)
            annot.set_colors(stroke=color, fill=color)
            annot.set_opacity(0.18)
            annot.set_border(width=1.5)
            annot.update()
            annots.append(annot)

        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)

        # Clean up annotations
        for a in annots:
            page.delete_annot(a)

        doc.close()
        return base64.b64encode(pix.tobytes("png")).decode("ascii")
    except Exception:
        return None


def _match_issues_to_bboxes(
    issues: list[dict],
    structure: dict,
) -> dict[int, list[dict]]:
    """Group issues by page and attach best-match bboxes from enriched structure.

    Returns {page_no: [{"issue": issue, "bbox": dict|None}, ...]}.
    """
    elements = structure.get("elements", [])
    by_page: dict[int, list] = {}

    # Pre-index elements by page
    elems_by_page: dict[int, list] = {}
    for e in elements:
        pn = e.get("page_no")
        if pn:
            elems_by_page.setdefault(pn, []).append(e)

    for issue in issues:
        loc = issue.get("location") or {}
        page_no = loc.get("page_number")
        if not page_no:
            page_no, bbox = _get_issue_location(issue, structure)
            if page_no:
                by_page.setdefault(page_no, []).append({"issue": issue, "bbox": bbox})
            continue

        # Try to find matching element on this page for bbox
        bbox = None
        page_elems = elems_by_page.get(page_no, [])
        ctx = issue.get("context") or {}
        text_content = ctx.get("text_content", "") if isinstance(ctx, dict) else ""

        if text_content and page_elems:
            snippet = text_content[:100].lower()
            for e in page_elems:
                etxt = (e.get("text") or "")[:100].lower()
                if etxt and snippet and (snippet in etxt or etxt in snippet):
                    bbox = e.get("bbox")
                    break

        if not bbox:
            # Fallback: try _get_issue_location
            _, bbox = _get_issue_location(issue, structure)

        by_page.setdefault(page_no, []).append({"issue": issue, "bbox": bbox})

    return by_page
