# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fallback text recovery for pages Docling failed to extract.

When Docling's iterate_items() produces zero elements for a page (common
with bibliography, appendix, figure-heavy, and complex table pages), the
iText tagger wraps that page as Part with no semantic structure.  Screen
readers get no /ActualText and the page is effectively inaccessible.

This module detects those empty pages, extracts text from them via PyMuPDF,
and injects synthetic P elements into the structure JSON so the tagger can
create proper tagged content with /ActualText and MCR.

For truly scanned/image-only pages (no native text layer), OCR recovery
via Tesseract is available as a second pass (Phase 1c).

Usage (called from pipeline_core.py Phase 1b and 1c):
    stats = inject_fallback_elements(structure_json_path, source_pdf_path)
    ocr_stats = ocr_recover_pages(structure_json_path, source_pdf_path)
"""

from utils.raster import safe_dpi
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("fallback_text")


def _split_text_into_spans(text: str) -> List[Dict[str, str]]:
    """Import-free copy of the span splitter for fallback elements.

    Mirrors docling_converter._split_text_into_spans but avoids circular
    imports.  Splits paragraph text into sentence-level Span children.
    """
    if not text or len(text.strip()) < 10:
        return []

    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip())
    if len(sentences) <= 1:
        return []

    return [{"tag": "Span", "text": s} for s in sentences if s.strip()]


def _merge_adjacent_blocks(
    blocks: List[Dict[str, Any]],
    gap_threshold: float = 14.0,
) -> List[Dict[str, Any]]:
    """Merge vertically adjacent text blocks into single paragraphs.

    PyMuPDF sometimes splits a single paragraph across multiple blocks.
    Blocks are merged when they are vertically close (gap < threshold)
    and horizontally overlapping.
    """
    if not blocks:
        return []

    merged = [blocks[0].copy()]
    for blk in blocks[1:]:
        prev = merged[-1]
        # Vertical gap between bottom of prev and top of current
        gap = blk["bbox"][1] - prev["bbox"][3]  # y0_cur - y1_prev (fitz coords)
        # Horizontal overlap check
        h_overlap = min(prev["bbox"][2], blk["bbox"][2]) - max(
            prev["bbox"][0], blk["bbox"][0]
        )
        page_width = max(prev["bbox"][2], blk["bbox"][2])
        h_overlap_pct = h_overlap / max(page_width, 1)

        if 0 < gap < gap_threshold and h_overlap_pct > 0.3:
            # Merge: extend bbox and concatenate text
            prev["text"] = prev["text"].rstrip() + " " + blk["text"].lstrip()
            prev["bbox"] = (
                min(prev["bbox"][0], blk["bbox"][0]),
                prev["bbox"][1],  # keep original top
                max(prev["bbox"][2], blk["bbox"][2]),
                blk["bbox"][3],  # extend bottom
            )
        else:
            merged.append(blk.copy())
    return merged


def inject_fallback_elements(
    structure_json_path: str,
    source_pdf_path: str,
    min_words: int = 3,
) -> Dict[str, Any]:
    """Detect pages without Docling elements and inject fallback P elements.

    Args:
        structure_json_path: Path to docling_structure.json (modified in-place).
        source_pdf_path: Path to the original source PDF.
        min_words: Minimum word count on a source page to attempt recovery.
            Pages with fewer words are likely decorative/blank.

    Returns:
        Dict with recovery stats:
            pages_checked: total pages in document
            pages_with_elements: pages Docling covered
            pages_without_elements: pages with no Docling elements
            pages_recovered: pages where fallback text was injected
            fallback_elements_added: total P elements created
            recovered_pages: list of 1-based page numbers recovered
            fallback_page_details: per-page diagnostic list
    """
    import fitz

    stats = {
        "pages_checked": 0,
        "pages_with_elements": 0,
        "pages_without_elements": 0,
        "pages_recovered": 0,
        "fallback_elements_added": 0,
        "recovered_pages": [],
        "fallback_page_details": [],
    }

    # Load structure JSON
    struct_path = Path(structure_json_path)
    with open(struct_path, encoding="utf-8") as f:
        structure = json.load(f)

    elements = structure.get("elements", [])
    pages_info = structure.get("pages", {})
    num_pages = structure.get("metadata", {}).get("num_pages", 0)
    stats["pages_checked"] = num_pages

    if num_pages == 0:
        return stats

    # Identify pages that Docling covered
    covered_pages = set()
    for elem in elements:
        pn = elem.get("page_no")
        if pn is not None:
            covered_pages.add(pn)
    stats["pages_with_elements"] = len(covered_pages)

    # Find pages with no elements
    empty_pages = []
    for p in range(1, num_pages + 1):
        if p not in covered_pages:
            empty_pages.append(p)
    stats["pages_without_elements"] = len(empty_pages)

    if not empty_pages:
        return stats

    # Open source PDF
    try:
        doc = fitz.open(source_pdf_path)
    except Exception as e:
        logger.warning("Cannot open source PDF for fallback: %s", e)
        return stats

    # Extract text from empty pages and build fallback elements
    fallback_elements = []

    for page_no in empty_pages:
        if page_no > len(doc):
            continue

        page = doc[page_no - 1]

        # Extract text blocks with positions (sort=True for reading order)
        try:
            page_dict = page.get_text("dict", sort=True)
        except Exception:
            continue

        raw_blocks = page_dict.get("blocks", [])

        # Filter to text blocks with meaningful content
        text_blocks = []
        for blk in raw_blocks:
            if blk.get("type", 0) != 0:  # type 0 = text
                continue
            # Reconstruct block text from lines/spans
            block_text = ""
            for line in blk.get("lines", []):
                line_text = ""
                for span in line.get("spans", []):
                    line_text += span.get("text", "")
                if line_text.strip():
                    block_text += line_text.strip() + " "
            block_text = block_text.strip()
            if len(block_text) < 5:
                continue
            text_blocks.append({
                "text": block_text,
                "bbox": blk["bbox"],  # (x0, y0, x1, y1) in fitz coords
            })

        if not text_blocks:
            stats["fallback_page_details"].append(
                {"page": page_no, "blocks": 0, "words": 0, "accepted": False, "reason": "no_text_blocks"}
            )
            continue

        # Check minimum word count
        total_words = sum(len(b["text"].split()) for b in text_blocks)
        if total_words < min_words:
            stats["fallback_page_details"].append(
                {"page": page_no, "blocks": len(text_blocks), "words": total_words,
                 "accepted": False, "reason": f"below_min_words ({total_words}<{min_words})"}
            )
            logger.debug("  Fallback page %d: %d words < min_words=%d, skipped", page_no, total_words, min_words)
            continue

        # Merge adjacent blocks
        text_blocks = _merge_adjacent_blocks(text_blocks)

        # Get page dimensions for coordinate conversion
        page_info = pages_info.get(str(page_no), {})
        page_height = page_info.get("height", page.rect.height)

        # Create P elements
        for i, blk in enumerate(text_blocks):
            text = blk["text"]
            # Convert fitz bbox (top-left origin) to PDF bbox (bottom-left origin)
            fx0, fy0, fx1, fy1 = blk["bbox"]
            bbox_dict = {
                "l": round(fx0, 2),
                "b": round(page_height - fy1, 2),
                "r": round(fx1, 2),
                "t": round(page_height - fy0, 2),
            }

            entry = {
                "id": f"fallback_{page_no:04d}_{i:02d}",
                "type": "TextItem",
                "tag": "P",
                "level": None,
                "text": text,
                "page_no": page_no,
                "bbox": bbox_dict,
                "reading_order": 0,  # will be renumbered
                "alt_text": None,
                "image_ref": None,
                "source": "fallback",
            }

            # Add Span children for multi-sentence text
            children = _split_text_into_spans(text)
            if children:
                entry["children"] = children

            fallback_elements.append(entry)

        stats["fallback_page_details"].append(
            {"page": page_no, "blocks": len(text_blocks), "words": total_words,
             "accepted": True, "reason": "recovered"}
        )
        stats["pages_recovered"] += 1
        stats["recovered_pages"].append(page_no)

    doc.close()

    if not fallback_elements:
        return stats

    stats["fallback_elements_added"] = len(fallback_elements)

    # Insert fallback elements preserving Docling's column-aware reading order.
    # A naive (page_no, -bbox.t) sort destroys multi-column ordering.
    from output_gen.reading_order import insert_elements_preserving_order

    all_elements = insert_elements_preserving_order(elements, fallback_elements)

    # Re-number reading_order sequentially
    for idx, elem in enumerate(all_elements):
        elem["reading_order"] = idx

    structure["elements"] = all_elements

    # Write back
    with open(struct_path, "w", encoding="utf-8") as f:
        json.dump(structure, f, indent=2, ensure_ascii=False)

    logger.info(
        "Fallback recovery: %d pages recovered, %d elements injected (pages: %s)",
        stats["pages_recovered"],
        stats["fallback_elements_added"],
        stats["recovered_pages"],
    )

    return stats


def ocr_recover_pages(
    structure_json_path: str,
    source_pdf_path: str,
    min_words: int = 3,
    language: str = "eng",
    dpi: int = 150,
    progress_fn=None,
) -> Dict[str, Any]:
    """OCR pages where both Docling and PyMuPDF native extraction found no text.

    This is Phase 1c — runs AFTER inject_fallback_elements() (Phase 1b).
    Only targets pages that still have zero elements in the structure JSON
    AND have no native text layer (truly scanned/image-only pages).

    Requires Tesseract to be installed (``apt install tesseract-ocr``).
    Gracefully skips if Tesseract is unavailable.

    Args:
        structure_json_path: Path to docling_structure.json (modified in-place).
        source_pdf_path: Path to the original source PDF.
        min_words: Minimum OCR word count to accept a page (filters noise).
        language: Tesseract language code (default "eng").
        dpi: Resolution for OCR rendering (default 150).

    Returns:
        Dict with recovery stats (same shape as inject_fallback_elements).
    """
    import time

    import fitz

    stats = {
        "pages_checked": 0,
        "pages_with_elements": 0,
        "pages_without_elements": 0,
        "pages_recovered": 0,
        "fallback_elements_added": 0,
        "recovered_pages": [],
        "ocr_pages_scanned": 0,
        "ocr_skipped_reason": None,
        "ocr_page_details": [],
    }

    # Check Tesseract availability
    import shutil

    if not shutil.which("tesseract"):
        logger.warning("Tesseract not installed — skipping OCR recovery")
        stats["ocr_skipped_reason"] = "tesseract_not_installed"
        return stats

    # Load structure JSON
    struct_path = Path(structure_json_path)
    with open(struct_path, encoding="utf-8") as f:
        structure = json.load(f)

    elements = structure.get("elements", [])
    pages_info = structure.get("pages", {})
    num_pages = structure.get("metadata", {}).get("num_pages", 0)
    stats["pages_checked"] = num_pages

    if num_pages == 0:
        return stats

    # Identify pages that already have elements (Docling + Phase 1b fallback)
    covered_pages = set()
    for elem in elements:
        pn = elem.get("page_no")
        if pn is not None:
            covered_pages.add(pn)
    stats["pages_with_elements"] = len(covered_pages)

    # Find pages still empty after Phase 1b
    empty_pages = [p for p in range(1, num_pages + 1) if p not in covered_pages]
    stats["pages_without_elements"] = len(empty_pages)

    if not empty_pages:
        return stats

    # Open source PDF
    try:
        doc = fitz.open(source_pdf_path)
    except Exception as e:
        logger.warning("Cannot open source PDF for OCR recovery: %s", e)
        return stats

    # Filter to truly scanned pages (no native text)
    scanned_pages = []
    for page_no in empty_pages:
        if page_no > len(doc):
            continue
        page = doc[page_no - 1]
        native_text = page.get_text("text").strip()
        if len(native_text.split()) < min_words:
            scanned_pages.append(page_no)

    if not scanned_pages:
        doc.close()
        return stats

    logger.info(
        "OCR recovery: %d candidate scanned pages out of %d empty pages",
        len(scanned_pages),
        len(empty_pages),
    )

    # OCR each scanned page
    ocr_elements = []
    t0 = time.time()

    for i, page_no in enumerate(scanned_pages):
        if progress_fn:
            progress_fn(i + 1, len(scanned_pages), page_no)
        page = doc[page_no - 1]
        stats["ocr_pages_scanned"] += 1

        try:
            tp = page.get_textpage_ocr(
                language=language,
                dpi=safe_dpi(page.rect.width, page.rect.height, dpi),
                full=True,
            )
            page_dict = page.get_text("dict", textpage=tp, sort=True)
        except Exception as e:
            logger.warning("OCR failed on page %d: %s", page_no, e)
            stats["ocr_page_details"].append(
                {"page": page_no, "blocks": 0, "words": 0, "accepted": False, "reason": f"ocr_error: {e}"}
            )
            continue

        raw_blocks = page_dict.get("blocks", [])

        # Filter to text blocks with meaningful content
        text_blocks = []
        for blk in raw_blocks:
            if blk.get("type", 0) != 0:
                continue
            block_text = ""
            for line in blk.get("lines", []):
                line_text = ""
                for span in line.get("spans", []):
                    line_text += span.get("text", "")
                if line_text.strip():
                    block_text += line_text.strip() + " "
            block_text = block_text.strip()
            if len(block_text) < 5:
                continue
            text_blocks.append({
                "text": block_text,
                "bbox": blk["bbox"],
            })

        if not text_blocks:
            stats["ocr_page_details"].append(
                {"page": page_no, "blocks": 0, "words": 0, "accepted": False, "reason": "no_text_blocks"}
            )
            continue

        # Check minimum word count
        total_words = sum(len(b["text"].split()) for b in text_blocks)
        if total_words < min_words:
            stats["ocr_page_details"].append(
                {"page": page_no, "blocks": len(text_blocks), "words": total_words,
                 "accepted": False, "reason": f"below_min_words ({total_words}<{min_words})"}
            )
            logger.debug("  OCR page %d: %d words < min_words=%d, skipped", page_no, total_words, min_words)
            continue

        # Merge adjacent blocks
        text_blocks = _merge_adjacent_blocks(text_blocks)

        # Get page dimensions for coordinate conversion
        page_info = pages_info.get(str(page_no), {})
        page_height = page_info.get("height", page.rect.height)

        # Create P elements
        for i, blk in enumerate(text_blocks):
            text = blk["text"]
            fx0, fy0, fx1, fy1 = blk["bbox"]
            bbox_dict = {
                "l": round(fx0, 2),
                "b": round(page_height - fy1, 2),
                "r": round(fx1, 2),
                "t": round(page_height - fy0, 2),
            }

            entry = {
                "id": f"ocr_{page_no:04d}_{i:02d}",
                "type": "TextItem",
                "tag": "P",
                "level": None,
                "text": text,
                "page_no": page_no,
                "bbox": bbox_dict,
                "reading_order": 0,
                "alt_text": None,
                "image_ref": None,
                "source": "ocr",
            }

            children = _split_text_into_spans(text)
            if children:
                entry["children"] = children

            ocr_elements.append(entry)

        stats["ocr_page_details"].append(
            {"page": page_no, "blocks": len(text_blocks), "words": total_words,
             "accepted": True, "reason": "recovered"}
        )
        stats["pages_recovered"] += 1
        stats["recovered_pages"].append(page_no)

    doc.close()
    elapsed = time.time() - t0

    if not ocr_elements:
        logger.info("OCR recovery: no text recovered from %d scanned pages (%.1fs)", stats["ocr_pages_scanned"], elapsed)
        return stats

    stats["fallback_elements_added"] = len(ocr_elements)

    # Insert OCR elements preserving Docling's column-aware reading order.
    from output_gen.reading_order import insert_elements_preserving_order

    all_elements = insert_elements_preserving_order(elements, ocr_elements)

    # Re-number reading_order sequentially
    for idx, elem in enumerate(all_elements):
        elem["reading_order"] = idx

    structure["elements"] = all_elements

    # Write back
    with open(struct_path, "w", encoding="utf-8") as f:
        json.dump(structure, f, indent=2, ensure_ascii=False)

    logger.info(
        "OCR recovery: %d pages recovered, %d elements injected (pages: %s) in %.1fs",
        stats["pages_recovered"],
        stats["fallback_elements_added"],
        stats["recovered_pages"],
        elapsed,
    )

    return stats


# ── Phase 3b: OCR replacement for gibberish pages ────────────────────────────


_CTRL_EXEMPT = {0x09, 0x0A, 0x0D}  # tab, newline, carriage return


def _control_char_ratio(text: str) -> float:
    """Ratio of control chars (0x00-0x1F excl tab/LF/CR) to non-whitespace chars."""
    non_ws = [c for c in text if not c.isspace()]
    if not non_ws:
        return 0.0
    ctrl = sum(1 for c in non_ws if ord(c) <= 0x1F and ord(c) not in _CTRL_EXEMPT)
    return ctrl / len(non_ws)


def ocr_replace_gibberish_pages(
    structure_json_path: str,
    source_pdf_path: str,
    language: str = "eng",
    dpi: int = 150,
    threshold: float = 0.30,
    min_words: int = 5,
    progress_fn=None,
) -> Dict[str, Any]:
    """Replace Docling elements on pages with gibberish text via Tesseract OCR.

    Detects pages where PyMuPDF text extraction has a high ratio of control
    characters (>threshold), indicating a scrambled font encoding.  Docling may
    produce printable glyph-name references (e.g. ``/56 /27``) for these fonts,
    so detection uses the source PDF directly via PyMuPDF.

    Returns stats dict with pages_detected, pages_replaced, etc.
    """
    import time

    stats: Dict[str, Any] = {
        "pages_detected": 0,
        "pages_replaced": 0,
        "elements_removed": 0,
        "elements_added": 0,
        "replaced_pages": [],
        "elements_replaced": 0,  # alias for pipeline logging
    }

    struct_path = Path(structure_json_path)
    if not struct_path.exists():
        return stats

    with open(struct_path, "r", encoding="utf-8") as f:
        structure = json.load(f)

    elements = structure.get("elements", [])
    pages_info = structure.get("pages", {})
    if not elements:
        return stats

    # Open source PDF — used for both detection and OCR
    try:
        import fitz
        doc = fitz.open(source_pdf_path)
    except Exception as e:
        logger.warning("Cannot open source PDF for OCR replacement: %s", e)
        return stats

    # Identify pages that have Docling elements
    pages_with_elements = set()
    for elem in elements:
        pn = elem.get("page_no")
        if pn is not None:
            pages_with_elements.add(pn)

    # Detect gibberish pages via PyMuPDF text extraction from source PDF
    gibberish_pages = []
    for pn in sorted(pages_with_elements):
        if pn > len(doc):
            continue
        page_text = doc[pn - 1].get_text()
        ratio = _control_char_ratio(page_text)
        if ratio > threshold:
            gibberish_pages.append(pn)
            logger.info(
                "Page %d: %.0f%% control chars in source PDF — flagged as gibberish",
                pn, ratio * 100,
            )

    stats["pages_detected"] = len(gibberish_pages)
    if not gibberish_pages:
        doc.close()
        return stats

    t0 = time.time()
    replacement_elements = []

    for i, page_no in enumerate(gibberish_pages):
        if page_no > len(doc):
            continue
        if progress_fn:
            progress_fn(i + 1, len(gibberish_pages), page_no)

        page = doc[page_no - 1]

        # Run Tesseract OCR
        try:
            tp = page.get_textpage_ocr(
                language=language,
                dpi=safe_dpi(page.rect.width, page.rect.height, dpi),
                full=True,
            )
            page_dict = page.get_text("dict", textpage=tp, sort=True)
        except Exception as e:
            logger.warning("OCR failed on gibberish page %d: %s", page_no, e)
            continue

        raw_blocks = page_dict.get("blocks", [])

        # Filter to text blocks with meaningful content
        text_blocks = []
        for blk in raw_blocks:
            if blk.get("type", 0) != 0:
                continue
            block_text = ""
            for line in blk.get("lines", []):
                line_text = ""
                for span in line.get("spans", []):
                    line_text += span.get("text", "")
                if line_text.strip():
                    block_text += line_text.strip() + " "
            block_text = block_text.strip()
            if len(block_text) < 5:
                continue
            text_blocks.append({
                "text": block_text,
                "bbox": blk["bbox"],
            })

        if not text_blocks:
            logger.debug("  OCR gibberish page %d: no text blocks from OCR", page_no)
            continue

        total_words = sum(len(b["text"].split()) for b in text_blocks)
        if total_words < min_words:
            logger.debug(
                "  OCR gibberish page %d: %d words < min_words=%d, skipped",
                page_no, total_words, min_words,
            )
            continue

        # Merge adjacent blocks
        text_blocks = _merge_adjacent_blocks(text_blocks)

        # Get page dimensions for coordinate conversion
        page_info = pages_info.get(str(page_no), {})
        page_height = page_info.get("height", page.rect.height)

        # Remove original elements for this page
        original_count = len(elements)
        elements = [e for e in elements if e.get("page_no") != page_no]
        removed = original_count - len(elements)
        stats["elements_removed"] += removed

        # Create replacement P elements from OCR
        for i, blk in enumerate(text_blocks):
            text = blk["text"]
            fx0, fy0, fx1, fy1 = blk["bbox"]
            bbox_dict = {
                "l": round(fx0, 2),
                "b": round(page_height - fy1, 2),
                "r": round(fx1, 2),
                "t": round(page_height - fy0, 2),
            }

            entry = {
                "id": f"ocr_replaced_{page_no:04d}_{i:02d}",
                "type": "TextItem",
                "tag": "P",
                "level": None,
                "text": text,
                "page_no": page_no,
                "bbox": bbox_dict,
                "reading_order": 0,
                "alt_text": None,
                "image_ref": None,
                "source": "ocr_replaced",
            }

            children = _split_text_into_spans(text)
            if children:
                entry["children"] = children

            replacement_elements.append(entry)

        stats["pages_replaced"] += 1
        stats["replaced_pages"].append(page_no)
        stats["elements_added"] += len(text_blocks)
        logger.info(
            "  Page %d: replaced %d gibberish elements with %d OCR elements",
            page_no, removed, len(text_blocks),
        )

    doc.close()
    elapsed = time.time() - t0

    if not replacement_elements:
        return stats

    stats["elements_replaced"] = stats["elements_added"]

    # Insert OCR elements preserving reading order
    from output_gen.reading_order import insert_elements_preserving_order

    all_elements = insert_elements_preserving_order(elements, replacement_elements)

    # Re-number reading_order sequentially
    for idx, elem in enumerate(all_elements):
        elem["reading_order"] = idx

    structure["elements"] = all_elements

    # Write back
    with open(struct_path, "w", encoding="utf-8") as f:
        json.dump(structure, f, indent=2, ensure_ascii=False)

    logger.info(
        "OCR gibberish replacement: %d pages, %d elements removed, %d added (pages: %s) in %.1fs",
        stats["pages_replaced"],
        stats["elements_removed"],
        stats["elements_added"],
        stats["replaced_pages"],
        elapsed,
    )

    return stats
