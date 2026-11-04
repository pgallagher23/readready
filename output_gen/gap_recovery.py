# SPDX-License-Identifier: AGPL-3.0-or-later
"""PyMuPDF-based gap detection and content recovery.

Runs AFTER content_matcher.py to find text on pages that Docling missed.
Uses PyMuPDF's get_text("dict") to extract all text blocks, compares
against the enriched structure, and creates supplementary elements for
uncovered text regions.

This addresses the dominant failure pattern: Docling matches 95-100% of
its detected elements to content runs, but the source PDF has text that
Docling didn't detect at all (headers, footers, form labels, decorative
text, complex table captions).

Usage (called from pdf_tagger.py):
    from output_gen.gap_recovery import recover_content_gaps
    enriched = recover_content_gaps(enriched, source_pdf, cs_analysis)
"""

import json
import logging
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("gap_recovery")


def _normalize_text(text: str) -> str:
    """Normalize text for comparison: lowercase, collapse whitespace."""
    return re.sub(r"\s+", " ", text.lower().strip())


def _text_overlap(a: str, b: str) -> float:
    """Word-level overlap ratio between two strings."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    overlap = words_a & words_b
    return len(overlap) / min(len(words_a), len(words_b))


def _bbox_overlap(a: Dict, b: Dict, tolerance: float = 10.0) -> bool:
    """Check if two PDF-coordinate bboxes overlap (with tolerance)."""
    return (
        a["l"] - tolerance < b["r"]
        and a["r"] + tolerance > b["l"]
        and a["b"] - tolerance < b["t"]
        and a["t"] + tolerance > b["b"]
    )


def _fitz_to_pdf_bbox(
    fitz_bbox: Tuple[float, float, float, float],
    page_height: float,
) -> Dict[str, float]:
    """Convert fitz bbox (top-left origin) to PDF bbox (bottom-left origin)."""
    x0, y0, x1, y1 = fitz_bbox
    return {
        "l": round(x0, 2),
        "b": round(page_height - y1, 2),
        "r": round(x1, 2),
        "t": round(page_height - y0, 2),
    }


def _extract_form_fields(doc, pages_meta: Dict) -> List[Dict]:
    """Extract text from Widget annotation fields as synthetic elements.

    Form-heavy PDFs (fillable forms) store key text in Widget annotation
    metadata (/TU, /T, /V) rather than the page content stream.  Docling
    cannot extract this text, so we create synthetic P elements from the
    widget labels for tagging.
    """
    import fitz as _fitz

    elements = []
    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_no = page_idx + 1
        page_height = pages_meta.get(str(page_no), {}).get(
            "height", page.rect.height
        )

        try:
            widgets = list(page.widgets())
        except Exception:
            continue
        if not widgets:
            continue

        for widget in widgets:
            label = (widget.field_label or "").strip()
            if not label:
                continue

            # Field type prefix (matches Java ContentPreservingTagger pattern)
            ft = widget.field_type
            if ft == _fitz.PDF_WIDGET_TYPE_TEXT:
                prefix = "Text field"
            elif ft == _fitz.PDF_WIDGET_TYPE_BUTTON:
                prefix = "Button"
            elif ft in (_fitz.PDF_WIDGET_TYPE_COMBOBOX, _fitz.PDF_WIDGET_TYPE_LISTBOX):
                prefix = "Dropdown"
            elif ft == _fitz.PDF_WIDGET_TYPE_SIGNATURE:
                prefix = "Signature field"
            else:
                prefix = "Form field"

            text = f"{prefix}: {label}"

            # Include field value if non-empty
            value = (widget.field_value or "").strip()
            if value:
                text += f" \u2014 {value}"

            # Skip tiny widgets (likely hidden or decorative)
            rect = widget.rect
            if rect.width < 5 or rect.height < 5:
                continue

            bbox = _fitz_to_pdf_bbox(
                (rect.x0, rect.y0, rect.x1, rect.y1), page_height
            )

            elements.append({
                "id": None,  # assigned later
                "type": "TextItem",
                "tag": "P",
                "level": None,
                "text": text,
                "page_no": page_no,
                "bbox": bbox,
                "reading_order": 0,
                "alt_text": None,
                "image_ref": None,
                "source": "form_field",
                "children": [],
                "content_run_indices": [],
                "content_run_count": 0,
                "matched_text_preview": text[:80],
                "match_confidence": "form_field_extracted",
            })

    return elements


def _dedup_form_fields(
    form_elements: List[Dict], existing_elements: List[Dict]
) -> List[Dict]:
    """Remove form field elements whose label text already appears in Docling elements."""
    # Build per-page word sets from existing elements
    page_words: Dict[int, Set[str]] = defaultdict(set)
    for elem in existing_elements:
        if elem.get("text"):
            page_words[elem["page_no"]].update(elem["text"].lower().split())

    # Prefixes to strip before overlap check — these are synthetic, not from source
    prefix_words = {"text", "field:", "button:", "dropdown:", "signature", "form"}

    result = []
    for fe in form_elements:
        fe_words = set(fe["text"].lower().split())
        label_words = fe_words - prefix_words
        if not label_words:
            continue
        existing = page_words.get(fe["page_no"], set())
        overlap = len(label_words & existing) / len(label_words)
        if overlap < 0.70:
            result.append(fe)
    return result


def _classify_heading(
    font_size: float,
    is_bold: bool,
    body_font_size: float,
) -> str:
    """Classify text as heading or paragraph based on font size."""
    if body_font_size <= 0:
        return "P"
    ratio = font_size / body_font_size
    if ratio >= 2.0 or (ratio >= 1.6 and is_bold):
        return "H1"
    if ratio >= 1.6 or (ratio >= 1.3 and is_bold):
        return "H2"
    if ratio >= 1.3:
        return "H3"
    return "P"


def _extract_pymupdf_blocks(page, page_height: float) -> List[Dict]:
    """Extract text blocks from a PyMuPDF page with font info.

    Returns list of dicts with keys: text, bbox (PDF coords), font_size,
    is_bold, block_no.
    """
    try:
        page_dict = page.get_text("dict", sort=True)
    except Exception:
        return []

    blocks = []
    for block in page_dict.get("blocks", []):
        if block.get("type") != 0:  # text blocks only
            continue

        text_parts = []
        font_sizes = []
        bold_count = 0
        total_spans = 0

        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span_text = span.get("text", "")
                if not span_text.strip():
                    continue
                text_parts.append(span_text)
                font_sizes.append(span.get("size", 0))
                total_spans += 1
                flags = span.get("flags", 0)
                if flags & 2**4:  # bold flag
                    bold_count += 1

        text = " ".join(text_parts).strip()
        if not text or len(text.split()) < 2:
            continue

        avg_font_size = sum(font_sizes) / len(font_sizes) if font_sizes else 0
        is_bold = bold_count > total_spans / 2 if total_spans > 0 else False

        blocks.append({
            "text": text,
            "bbox": _fitz_to_pdf_bbox(block["bbox"], page_height),
            "font_size": round(avg_font_size, 1),
            "is_bold": is_bold,
            "word_count": len(text.split()),
        })

    return blocks


def _detect_columns(blocks: List[Dict], min_gap: float = 100.0) -> int:
    """Detect number of columns from block left-edge positions.

    Returns estimated column count (1 if single-column).
    """
    if len(blocks) < 3:
        return 1

    left_edges = sorted(b["bbox"]["l"] for b in blocks)
    # Find gaps > min_gap between consecutive sorted left edges
    gaps = []
    for i in range(1, len(left_edges)):
        gap = left_edges[i] - left_edges[i - 1]
        if gap > min_gap:
            gaps.append(gap)

    return len(gaps) + 1


def _compute_body_font_size(
    all_blocks: List[Dict],
) -> float:
    """Compute the body text font size (mode of all block font sizes)."""
    if not all_blocks:
        return 12.0
    sizes = [round(b["font_size"]) for b in all_blocks if b["font_size"] > 0]
    if not sizes:
        return 12.0
    from collections import Counter
    size_counts = Counter(sizes)
    return float(size_counts.most_common(1)[0][0])


def _is_element_matched(elem: Dict) -> bool:
    """Check if an element has matched content runs (element or cell level).

    Table elements have their runs redistributed to cells by the content
    matcher, leaving the parent's ``content_run_count`` near zero.  This
    helper also checks cell-level ``content_run_indices`` so that Tables
    with matched cells are correctly identified as covered.

    TOC elements similarly redistribute runs to TOCI children — check
    child-level ``content_run_indices`` so the TOC bbox suppresses gap
    recovery on TOC pages.
    """
    if elem.get("content_run_count", 0) > 0:
        return True
    cells = elem.get("cells")
    if cells:
        return any(c.get("content_run_indices") for c in cells)
    if elem.get("tag") in ("TOC", "L"):
        children = elem.get("children", [])
        return any(c.get("content_run_indices") for c in children)
    return False


def recover_content_gaps(
    enriched_structure: Dict,
    source_pdf_path: str,
    cs_analysis: Dict,
    min_gap_words: int = 3,
    unmatched_threshold: float = 0.30,
) -> Dict:
    """Recover content gaps by creating supplementary elements from PyMuPDF.

    Args:
        enriched_structure: Output from content_matcher (modified in place).
        source_pdf_path: Path to source PDF.
        cs_analysis: Content stream analysis JSON.
        min_gap_words: Minimum words in a gap block to create an element.
        unmatched_threshold: Page run unmatched ratio to trigger recovery.

    Returns:
        Modified enriched_structure with additional gap elements and
        cleaned empty elements.
    """
    import fitz

    elements = enriched_structure.get("elements", [])
    unmatched_runs = enriched_structure.get("unmatched_runs", [])
    pages_info = enriched_structure.get("pages", {})

    # Build per-page element coverage map
    page_elements: Dict[int, List[Dict]] = {}
    for elem in elements:
        pn = elem.get("page_no")
        if pn is not None:
            page_elements.setdefault(pn, []).append(elem)

    # Build per-page unmatched run info from content analysis
    page_run_counts: Dict[int, int] = {}
    page_all_runs: Dict[int, List[Dict]] = {}  # ALL runs (for Figure reassign)
    for pa in cs_analysis.get("page_analyses", []):
        page_run_counts[pa["page"]] = pa.get("text_run_count", 0)
        page_all_runs[pa["page"]] = pa.get("text_runs", [])

    # Build per-page unmatched run lookup
    page_unmatched: Dict[int, List[Dict]] = {}
    for run in unmatched_runs:
        pn = run.get("page", run.get("page_no", 0))
        page_unmatched.setdefault(pn, []).append(run)

    # Identify gap pages: pages where Docling has elements but significant
    # content might be missing (based on unmatched run ratio)
    gap_pages = set()
    for pn, total_runs in page_run_counts.items():
        if total_runs == 0:
            # Scanned/image-only page: 0 content stream text runs but
            # Docling may have OCR-derived elements. Include in gap_pages
            # so gap recovery can detect PyMuPDF text blocks.
            if pn in page_elements:
                gap_pages.add(pn)
            continue  # skip unmatched ratio calc (no runs to compare)
        unmatched_count = len(page_unmatched.get(pn, []))
        # Also consider pages where Docling has elements but coverage
        # may be low — even with 100% match rate, Docling might not
        # detect all text blocks on the page
        if pn in page_elements:
            gap_pages.add(pn)  # Check ALL pages with Docling elements

    if not gap_pages:
        logger.info("  Gap recovery: no pages to analyze")
        return enriched_structure

    # Open source PDF
    try:
        doc = fitz.open(source_pdf_path)
    except Exception as e:
        logger.warning("  Gap recovery: cannot open source PDF: %s", e)
        return enriched_structure

    # --- Form field extraction ---
    # Form-heavy PDFs store text in Widget annotations, invisible to Docling.
    form_elements = _extract_form_fields(doc, pages_info)
    if form_elements:
        form_elements = _dedup_form_fields(form_elements, elements)
        for i, fe in enumerate(form_elements):
            fe["id"] = f"form_{i:04d}"
        if form_elements:
            logger.info(
                "  Gap recovery: extracted %d form field labels from %d pages",
                len(form_elements),
                len(set(fe["page_no"] for fe in form_elements)),
            )

    # Collect all PyMuPDF blocks across pages for body font size estimation
    all_pymupdf_blocks = []
    page_pymupdf: Dict[int, List[Dict]] = {}

    for page_no in sorted(gap_pages):
        if page_no < 1 or page_no > len(doc):
            continue
        page = doc[page_no - 1]
        page_height = float(page.rect.height)
        blocks = _extract_pymupdf_blocks(page, page_height)
        page_pymupdf[page_no] = blocks
        all_pymupdf_blocks.extend(blocks)

    body_font_size = _compute_body_font_size(all_pymupdf_blocks)
    logger.info("  Gap recovery: body font size = %.0fpt, analyzing %d pages",
                body_font_size, len(page_pymupdf))

    # For each gap page, find PyMuPDF blocks not covered by Docling elements
    new_elements = []
    recovered_count = 0
    elem_counter = len(elements)  # for generating unique IDs

    for page_no, pymupdf_blocks in sorted(page_pymupdf.items()):
        if not pymupdf_blocks:
            continue

        docling_elems = page_elements.get(page_no, [])

        # Build set of text covered by MATCHED Docling elements only.
        # Elements with content_run_count == 0 have text in the structure
        # but no actual content stream linkage — they're effectively gaps.
        # Table elements are special: the content matcher redistributes
        # runs to cells, leaving the parent's count near zero.  We check
        # cell-level runs via _is_element_matched().
        docling_words: Set[str] = set()
        docling_bboxes = []
        for elem in docling_elems:
            if not _is_element_matched(elem):
                continue  # Unmatched element — don't count as covered
            elem_text = elem.get("text", "")
            if elem_text:
                docling_words.update(elem_text.lower().split())
            bbox = elem.get("bbox")
            if bbox:
                docling_bboxes.append(bbox)
            # Add cell-level text for matched tables so gap recovery
            # recognises table content as already covered.
            for cell in elem.get("cells", []):
                cell_text = cell.get("text", "")
                if cell_text:
                    docling_words.update(cell_text.lower().split())
            # Add LI children text for matched L (List) elements so gap
            # recovery recognises list item content as already covered.
            if elem.get("tag") == "L":
                for child in elem.get("children", []):
                    child_text = child.get("text", "")
                    if child_text:
                        docling_words.update(child_text.lower().split())
            # Add TOCI children text for matched TOC elements so gap recovery
            # recognises TOC page content as already covered.
            if elem.get("tag") == "TOC":
                for child in elem.get("children", []):
                    child_text = child.get("text", "")
                    if child_text:
                        docling_words.update(child_text.lower().split())

        # Collect non-Figure MATCHED Docling bboxes for spatial dedup.
        # Two exclusions prevent false suppression of gap recovery:
        # 1. Figure bboxes: Docling often classifies entire page regions
        #    as PictureItem/Figure even when real, extractable text exists
        #    there (flowcharts, forms, screenshot pages).
        # 2. Unmatched elements (content_run_count == 0): Docling detected
        #    a Table/List/etc. but couldn't match any content stream runs
        #    to it. Their bboxes (often 15-64% of page area) would block
        #    recovery of all text inside them — the dominant cause of
        #    screen reader coverage failures on data-heavy pages.
        all_docling_bboxes = []
        figure_bboxes = []
        # Build set of run indices assigned to non-Figure elements.
        # The "all runs" fallback should only steal from Figures, not
        # from semantic elements (P, H1, Table, etc.) — stealing from
        # semantic elements creates duplicate assignments that the Java
        # tagger resolves by last-writer-wins, leaving the gap element
        # empty (no MCR, no children → PAC "empty structure element").
        non_figure_run_indices: Set[int] = set()
        for elem in docling_elems:
            bbox = elem.get("bbox")
            if not bbox:
                continue
            if elem.get("tag") == "Figure":
                figure_bboxes.append(bbox)
                continue  # Don't suppress text recovery inside Figure regions
            if not _is_element_matched(elem):
                continue  # Don't suppress recovery inside unmatched elements
            all_docling_bboxes.append(bbox)
            for cri in elem.get("content_run_indices", []):
                ri = cri.get("run_index", cri) if isinstance(cri, dict) else cri
                non_figure_run_indices.add(ri)
            # Include child-level run indices so the "all runs"
            # fallback won't steal runs from container children.
            # Table cells, LI children, and TOCI children all have
            # their runs redistributed from the parent element.
            for cell in elem.get("cells", []):
                for cri in cell.get("content_run_indices", []):
                    ri = cri.get("run_index", cri) if isinstance(cri, dict) else cri
                    non_figure_run_indices.add(ri)
            if elem.get("tag") in ("L", "TOC"):
                for child in elem.get("children", []):
                    for cri in child.get("content_run_indices", []):
                        ri = cri.get("run_index", cri) if isinstance(cri, dict) else cri
                        non_figure_run_indices.add(ri)

        # Find PyMuPDF blocks that contain text NOT covered by Docling
        for block in pymupdf_blocks:
            block_words = set(block["text"].lower().split())
            if not block_words:
                continue

            # Check word overlap: how much of this block's text is already
            # in Docling elements?
            overlap = block_words & docling_words
            coverage = len(overlap) / len(block_words) if block_words else 1.0

            if coverage >= 0.70:
                # This block's text is mostly covered by Docling elements
                continue

            # Check spatial overlap: is this block inside any Docling bbox?
            # Use ALL bboxes (including unmatched) to avoid duplicating
            # elements that Docling already detected.
            inside_docling = False
            for db in all_docling_bboxes:
                # Check if block center is inside Docling bbox (generous tolerance)
                bcx = (block["bbox"]["l"] + block["bbox"]["r"]) / 2
                bcy = (block["bbox"]["b"] + block["bbox"]["t"]) / 2
                if (db["l"] - 20 <= bcx <= db["r"] + 20
                        and db["b"] - 20 <= bcy <= db["t"] + 20):
                    inside_docling = True
                    break

            if inside_docling:
                continue

            # This block has uncovered text not in any Docling element
            if block["word_count"] < min_gap_words:
                continue

            # Check if this block is inside a Figure bbox.  Text recovered
            # from Figure regions (flowcharts, diagrams) should always be
            # tagged as P — font-size-based heading classification is
            # misleading for diagram labels which use varied sizes.
            bcx = (block["bbox"]["l"] + block["bbox"]["r"]) / 2
            bcy = (block["bbox"]["b"] + block["bbox"]["t"]) / 2
            inside_figure = False
            for fb in figure_bboxes:
                if (fb["l"] - 5 <= bcx <= fb["r"] + 5
                        and fb["b"] - 5 <= bcy <= fb["t"] + 5):
                    inside_figure = True
                    break

            # Classify as heading or paragraph (P for Figure-interior blocks)
            if inside_figure:
                tag = "P"
            else:
                tag = _classify_heading(
                    block["font_size"], block["is_bold"], body_font_size
                )

            # Try to match this block to content stream runs.
            # First try unmatched runs; if none found, search ALL runs
            # (including those claimed by Figures on the same page).
            page_ur = page_unmatched.get(page_no, [])
            matched_run_indices = []
            bb = block["bbox"]
            for run in page_ur:
                # Whitespace-only runs are always artifact-wrapped at the
                # tagger (see ContentPreservingTagger fix #35); assigning
                # them here would create structure elements whose only
                # MCID is whitespace, producing blank-leaf SEs after the
                # tagger strips them.
                if not run.get("text", "").strip():
                    continue
                rx = run.get("x", 0)
                ry = run.get("y", 0)
                # Check if run position is inside block bbox (30pt tolerance)
                if (bb["l"] - 30 <= rx <= bb["r"] + 30
                        and bb["b"] - 30 <= ry <= bb["t"] + 30):
                    matched_run_indices.append({
                        "page": page_no,
                        "run_index": run.get("run_index", run.get("index", 0)),
                    })

            # If no unmatched runs found, try ALL runs on this page.
            # This handles the case where a Figure element claimed all
            # runs because its bbox spans the full page.  We'll steal
            # the matching runs from the Figure in a post-processing step.
            # Only steal from Figure elements — runs assigned to semantic
            # elements (P, H1, etc.) must not be duplicated.
            if not matched_run_indices:
                for run in page_all_runs.get(page_no, []):
                    if not run.get("text", "").strip():
                        continue
                    run_idx = run.get("run_index", run.get("index", 0))
                    if run_idx in non_figure_run_indices:
                        continue  # Already assigned to a semantic element
                    rx = run.get("x", 0)
                    ry = run.get("y", 0)
                    if (bb["l"] - 30 <= rx <= bb["r"] + 30
                            and bb["b"] - 30 <= ry <= bb["t"] + 30):
                        matched_run_indices.append({
                            "page": page_no,
                            "run_index": run_idx,
                        })

            # Skip blocks with no matched content stream runs —
            # creating an element with no runs produces an empty
            # structure element that PAC/validators flag.
            if not matched_run_indices:
                continue

            # Create supplementary element
            elem_id = f"gap_{elem_counter:04d}"
            elem_counter += 1

            # Build Span children (sentence-level)
            children = []
            sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", block["text"].strip())
            if len(sentences) > 1:
                children = [{"tag": "Span", "text": s} for s in sentences if s.strip()]

            new_elem = {
                "id": elem_id,
                "type": "TextItem",
                "tag": tag,
                "level": None,
                "text": block["text"],
                "page_no": page_no,
                "bbox": block["bbox"],
                "reading_order": 0,  # Will be re-sorted
                "alt_text": None,
                "image_ref": None,
                "source": "gap_recovery",
                "children": children,
                "content_run_indices": matched_run_indices,
                "content_run_count": len(matched_run_indices),
                "matched_text_preview": block["text"][:80],
                "match_confidence": "gap_recovered",
            }
            new_elements.append(new_elem)
            recovered_count += 1

    doc.close()

    # Reassign content runs stolen from Figure elements.
    # When gap recovery matched runs from page_all_runs (not unmatched),
    # those runs may still be claimed by a Figure.  Remove them from the
    # Figure's content_run_indices to avoid double-tagging.
    if new_elements:
        # Collect all run indices claimed by gap-recovered elements, per page
        gap_run_indices: Dict[int, Set[int]] = {}
        for ne in new_elements:
            pn = ne.get("page_no")
            for cri in ne.get("content_run_indices", []):
                gap_run_indices.setdefault(pn, set()).add(cri.get("run_index", -1))

        # Remove those from Figure elements on the same pages
        for pn, stolen_indices in gap_run_indices.items():
            for elem in page_elements.get(pn, []):
                if elem.get("tag") != "Figure":
                    continue
                orig_cri = elem.get("content_run_indices", [])
                if not orig_cri:
                    continue
                new_cri = [
                    cri for cri in orig_cri
                    if cri.get("run_index", -1) not in stolen_indices
                ]
                if len(new_cri) < len(orig_cri):
                    removed = len(orig_cri) - len(new_cri)
                    logger.info(
                        "  Gap recovery: reassigned %d runs from Figure on page %d",
                        removed, pn,
                    )
                    elem["content_run_indices"] = new_cri
                    elem["content_run_count"] = len(new_cri)

    # Populate Figure alt_text from recovered text that falls inside
    # the Figure's bbox.  This gives Figures a meaningful /Alt even
    # when VLM hasn't run.
    if new_elements:
        for pn, docling_elems in page_elements.items():
            for elem in docling_elems:
                if elem.get("tag") != "Figure" or elem.get("alt_text"):
                    continue
                fig_bbox = elem.get("bbox")
                if not fig_bbox:
                    continue
                fig_texts = []
                for ne in new_elements:
                    if ne.get("page_no") != pn:
                        continue
                    nb = ne.get("bbox", {})
                    if not nb:
                        continue
                    ncx = (nb.get("l", 0) + nb.get("r", 0)) / 2
                    ncy = (nb.get("b", 0) + nb.get("t", 0)) / 2
                    if (fig_bbox["l"] - 5 <= ncx <= fig_bbox["r"] + 5
                            and fig_bbox["b"] - 5 <= ncy <= fig_bbox["t"] + 5):
                        fig_texts.append(ne.get("text", ""))
                if fig_texts:
                    elem["alt_text"] = " ".join(t for t in fig_texts if t)[:500]

    # Merge form field elements into the new_elements list
    if form_elements:
        new_elements.extend(form_elements)

    # --- Detect form-image pages ---
    # Pages where the visible form layout is a rasterized background image
    # and field labels live in widget annotations, not the content stream.
    # These need special tagger handling: keep form background visible as
    # Figure instead of Artifact-wrapping (which causes AROL empty pages).
    #
    # Detection: page has widget annotations AND either:
    #   (a) is on overlay_pages (0 visible text runs), OR
    #   (b) has very few content-matched elements vs. form_field elements
    #       (content stream has only trivial text like spaces)
    if form_elements:
        overlay_set = set(enriched_structure.get("overlay_pages", []))
        form_image_pages = set()

        # Collect pages with form_field elements
        form_field_pages: Dict[int, int] = defaultdict(int)
        for fe in form_elements:
            form_field_pages[fe["page_no"]] += 1

        # Count content-matched elements per page
        matched_per_page: Dict[int, int] = defaultdict(int)
        for elem in elements:
            if elem.get("content_run_count", 0) > 0:
                matched_per_page[elem["page_no"]] += 1

        for page_no, widget_count in form_field_pages.items():
            matched = matched_per_page.get(page_no, 0)
            # Form-image page: widgets present + very few content matches
            # (threshold: fewer content-matched elements than widgets)
            if page_no in overlay_set or matched < widget_count:
                form_image_pages.add(page_no)

        if form_image_pages:
            enriched_structure["form_image_pages"] = sorted(form_image_pages)
            enriched_structure["overlay_pages"] = sorted(
                overlay_set - form_image_pages
            )
            logger.info(
                "  %d form-image page(s) (widgets on image-based forms): %s",
                len(form_image_pages),
                sorted(form_image_pages),
            )

    if new_elements:
        # Insert new elements preserving Docling's column-aware reading order.
        # A naive (page_no, -bbox.t) sort destroys multi-column ordering.
        from output_gen.reading_order import insert_elements_preserving_order

        elements = insert_elements_preserving_order(elements, new_elements)

        # Re-number reading_order
        for i, elem in enumerate(elements):
            elem["reading_order"] = i

        enriched_structure["elements"] = elements
        logger.info(
            "  Gap recovery: added %d new elements across %d pages",
            len(new_elements), len(set(e["page_no"] for e in new_elements)),
        )
    else:
        logger.info("  Gap recovery: no gaps found")

    # --- Empty MCID cleanup ---
    # Remove elements that will produce empty structure tree nodes:
    # no content runs, no text, and not a Figure/Formula
    before_count = len(enriched_structure["elements"])
    enriched_structure["elements"] = [
        e for e in enriched_structure["elements"]
        if (
            e.get("content_run_count", 0) > 0
            or (e.get("text", "").strip())
            or e.get("tag") in ("Figure", "Formula", "L")
            or e.get("source") in ("fallback", "ocr", "gap_recovery", "form_field")
        )
    ]
    removed = before_count - len(enriched_structure["elements"])
    if removed:
        logger.info("  Gap recovery: removed %d empty elements", removed)

    return enriched_structure
