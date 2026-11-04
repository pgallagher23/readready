# SPDX-License-Identifier: AGPL-3.0-or-later
"""Table caption generation using a Vision Language Model.

Renders tables from the original PDF as images (via PyMuPDF), sends them
to a VLM along with extracted text context, and inserts the generated
captions into the HTML.  Runs as Phase 3b of the pipeline, between
alt-text generation and heuristic remediation.
"""

from utils.raster import safe_dpi
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from vlm.client_factory import create_vlm_client
from vlm.image_preprocessing import prepare_image_for_vlm

logger = logging.getLogger(__name__)

TABLE_CAPTION_SYSTEM_PROMPT = """You write short, factual captions describing what data a table contains.

Rules:
1. HARD LIMIT: Your entire response must be 120 characters or fewer. Count the characters before responding. Shorten if needed — never exceed.
2. State what the table contains in plain terms ("Quarterly sales by region" not "A table showing data").
3. For tables of contents: name the section listed ("Chapter 3 subsections", "List of figures").
4. For data tables: name the variables and their relationship ("Vehicle speed vs. sound pressure level").
5. Your caption must NOT start with "The image", "This table", "A table", "An image", or any meta-description.
6. Your caption must NOT contain the words "image", "PDF", "WCAG", "accessibility", or "screen reader".
7. Your caption must NOT start with "Caption:" — write the caption text directly.
8. Respond with ONLY the caption text, nothing else."""

TABLE_CAPTION_PROMPT = """Caption this table.

{context}

Write a short factual caption (under 120 chars) describing the data this table contains."""


def generate_table_captions(
    html_path: str,
    pdf_path: str,
    structure_json_path: str,
    llm_config: Dict[str, Any],
    audit_result: Optional[Dict[str, Any]] = None,
    vlm_render_dpi: int = 200,
) -> Dict[str, Any]:
    """Generate captions for tables that lack them using VLM.

    Args:
        html_path: Path to the HTML file (will be modified in-place).
        pdf_path: Path to the original PDF file.
        structure_json_path: Path to docling_structure.json.
        llm_config: LLM configuration from config.yaml.
        audit_result: Pre-audit results (optional, for future filtering).
        vlm_render_dpi: DPI for rendering table regions. Default 200.

    Returns:
        Dict with captions, updated_count, skipped_count, stats, error.
    """
    start_time = time.time()

    client = create_vlm_client(llm_config)
    if not client.check_connection():
        logger.warning(
            "VLM unavailable at %s — skipping table caption generation "
            "(heuristic fallback in Phase 4)",
            client.base_url,
        )
        return {
            "captions": {},
            "updated_count": 0,
            "skipped_count": 0,
            "error": f"Cannot connect to VLM at {client.base_url}",
            "stats": {"time_seconds": 0},
        }

    # Load HTML
    html_path = Path(html_path)
    with open(html_path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    # Load structure JSON
    try:
        with open(structure_json_path, "r", encoding="utf-8") as f:
            structure = json.load(f)
    except Exception as e:
        logger.error("Failed to load structure JSON: %s", e)
        return {
            "captions": {},
            "updated_count": 0,
            "skipped_count": 0,
            "error": str(e),
            "stats": {"time_seconds": 0},
        }

    # Match HTML tables to structure JSON entries
    table_map = _match_html_tables_to_structure(soup, structure)

    # Find tables that need captions
    tables_needing_captions = _find_tables_needing_captions(soup)
    if not tables_needing_captions:
        logger.info("All tables already have captions, skipping VLM generation")
        return {
            "captions": {},
            "updated_count": 0,
            "skipped_count": 0,
            "stats": {"time_seconds": 0},
        }

    # Enforce per-document table limit to prevent VLM cost abuse
    max_tables = llm_config.get("max_tables_per_document", 100)
    capped_tables_warning = None
    if len(tables_needing_captions) > max_tables:
        capped_tables_warning = (
            f"Only {max_tables} of {len(tables_needing_captions)} tables were processed "
            f"for captions. The remaining tables may need manual captions."
        )
        logger.warning(
            "Document has %d tables needing captions — capping at %d (max_tables_per_document)",
            len(tables_needing_captions), max_tables,
        )
        tables_needing_captions = tables_needing_captions[:max_tables]

    logger.info("Generating captions for %d tables via VLM", len(tables_needing_captions))
    table_image_max_long_edge = int(llm_config.get("vlm_table_image_max_long_edge", 1800))
    table_image_output_format = str(llm_config.get("vlm_table_image_output_format", "png"))
    table_image_jpeg_quality = int(llm_config.get("vlm_image_jpeg_quality", 90))

    # Set up temp dir for rendered table images
    table_images_dir = html_path.parent.parent / "images" / "table_renders"
    table_images_dir.mkdir(parents=True, exist_ok=True)

    pages_info = structure.get("pages", {})

    # --- Phase A: Prepare work items (render images, build prompts — single-threaded) ---
    work_items = []
    skipped_count = 0

    for entry in tables_needing_captions:
        table_tag = entry["table"]
        table_idx = entry["index"]
        text_context = entry["text_context"]

        struct_entry = table_map.get(table_idx)
        if not struct_entry:
            logger.warning("  Table %d: no structure JSON match, skipping VLM caption", table_idx)
            skipped_count += 1
            continue

        page_no = struct_entry.get("page_no")
        bbox = struct_entry.get("bbox")
        if not page_no or not bbox:
            logger.warning("  Table %d: missing page_no or bbox in structure JSON", table_idx)
            skipped_count += 1
            continue

        page_dims = pages_info.get(str(page_no), {})
        img_path = str(table_images_dir / f"table_{table_idx}.png")
        rendered = _render_table_image(
            pdf_path=pdf_path, page_no=page_no, bbox=bbox,
            page_dimensions=page_dims, output_path=img_path,
            dpi=vlm_render_dpi,
        )
        if not rendered:
            logger.warning("  Table %d: failed to render image, skipping VLM caption", table_idx)
            skipped_count += 1
            continue

        prepared_rendered, prepared_is_temp, prepared_meta = prepare_image_for_vlm(
            rendered,
            max_long_edge=table_image_max_long_edge,
            output_format=table_image_output_format,
            jpeg_quality=table_image_jpeg_quality,
        )

        context_parts = []
        if text_context:
            context_parts.append(f"Compact table structure:\n{text_context}")
        prompt = TABLE_CAPTION_PROMPT.format(
            context="\n\n".join(context_parts) if context_parts else ""
        )

        work_items.append({
            "table_tag": table_tag,
            "table_idx": table_idx,
            "rendered": prepared_rendered,
            "rendered_is_temp": prepared_is_temp,
            "prepared_meta": prepared_meta,
            "prompt": prompt,
        })

    # --- Phase B: Run VLM calls in parallel ---
    max_workers = min(llm_config.get("max_parallel", 4), len(work_items)) if work_items else 1
    vlm_results: Dict[int, Dict] = {}  # keyed by table_idx

    if work_items:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_item = {
                pool.submit(
                    _generate_single_caption, client, item["rendered"], item["prompt"],
                ): item
                for item in work_items
            }
            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    vlm_results[item["table_idx"]] = future.result()
                except Exception as e:
                    logger.error("  Table %d: VLM generation failed — %s", item["table_idx"], e)
                    vlm_results[item["table_idx"]] = {"caption": None, "error": str(e)}

    # --- Phase C: Apply captions to soup (single-threaded) ---
    captions = {}
    caption_confidences = {}
    updated_count = 0

    for item in work_items:
        table_idx = item["table_idx"]
        table_tag = item["table_tag"]
        result = vlm_results.get(table_idx, {})

        if result.get("error"):
            skipped_count += 1
            continue

        caption_text = result.get("caption", "")
        confidence = result.get("confidence")
        if not caption_text or len(caption_text) < 3:
            logger.warning("  Table %d: VLM returned empty/trivial caption, skipping", table_idx)
            skipped_count += 1
            continue

        if len(caption_text) > 500:
            caption_text = caption_text[:497] + "..."

        caption_tag = soup.new_tag("caption")
        caption_tag.string = caption_text
        table_tag.insert(0, caption_tag)

        key = f"table_{table_idx}"
        captions[key] = caption_text
        if confidence is not None:
            caption_confidences[key] = confidence
        conf_str = f" (confidence {confidence}%)" if confidence is not None else ""
        updated_count += 1
        logger.info("  Table %d: caption = \"%s\"%s", table_idx, caption_text, conf_str,
                    extra={"contains_content": True, "content_ref": f"table {table_idx}"})

    for item in work_items:
        if item.get("rendered_is_temp"):
            try:
                Path(item["rendered"]).unlink()
            except OSError:
                pass

    # Write updated HTML back
    if updated_count > 0:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(str(soup))

    elapsed = time.time() - start_time
    logger.info(
        "Table caption generation complete: %d generated, %d skipped in %.1fs",
        updated_count,
        skipped_count,
        elapsed,
    )

    result = {
        "captions": captions,
        "caption_confidences": caption_confidences,
        "updated_count": updated_count,
        "skipped_count": skipped_count,
        "stats": {"time_seconds": round(elapsed, 2)},
    }
    if capped_tables_warning:
        result["warning"] = capped_tables_warning
    return result


def _parse_confidence(raw_text: str) -> Tuple[str, Optional[int]]:
    """Extract caption text and confidence score from VLM response.

    Expects format:
        <caption text>
        CONFIDENCE: XX%

    Returns:
        (clean_caption, confidence_int_or_None)
    """
    match = re.search(r'CONFIDENCE:\s*(\d{1,3})%', raw_text, re.IGNORECASE)
    if match:
        confidence = min(int(match.group(1)), 100)
        caption = re.sub(
            r'\n?\s*CONFIDENCE:\s*\d{1,3}%\s*$', '', raw_text, flags=re.IGNORECASE,
        ).strip()
        return caption, confidence
    return raw_text.strip(), None


def _generate_single_caption(client, rendered_path: str, prompt: str) -> Dict[str, Any]:
    """Call VLM for a single table image. Thread-safe — no soup mutation.

    Returns:
        {"caption": str, "confidence": int|None} on success,
        {"caption": None, "error": str} on failure.
    """
    try:
        raw = client.generate(
            prompt=prompt,
            images=[rendered_path],
            system_prompt=TABLE_CAPTION_SYSTEM_PROMPT,
        )
        caption_text, confidence = _parse_confidence(raw)
        caption_text = caption_text.strip('"').strip("'")
        return {"caption": caption_text, "confidence": confidence}
    except Exception as e:
        return {"caption": None, "confidence": None, "error": str(e)}


def _find_tables_needing_captions(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    """Identify HTML tables that need captions.

    Returns list of dicts with 'table', 'index', 'text_context'.
    Skips layout tables and tables that already have a <caption>.
    """
    results = []
    tables = soup.find_all("table")

    for i, table in enumerate(tables):
        # Skip if already has a caption
        if table.find("caption"):
            continue

        # Skip layout tables
        if table.get("role") == "presentation":
            continue
        css_classes = table.get("class", [])
        if any(
            p in " ".join(css_classes).lower()
            for p in ("layout", "grid", "non-data")
        ):
            continue

        # Skip very small tables (1 row, likely layout)
        rows = table.find_all("tr")
        if len(rows) <= 1:
            continue

        # Skip tables that the audit treats as layout tables.
        # Single-column tables with no <th> are layout; adding a caption
        # would reclassify them as data tables and trigger new issues.
        if not table.find("th"):
            first_row_cells = rows[0].find_all(["td", "th"]) if rows else []
            if len(rows) <= 1 or len(first_row_cells) <= 1:
                continue

        # Skip tables whose few stray <th> elements will be removed by
        # the Phase 4 stray-th fix (they'll become layout tables).
        all_cells = table.find_all(["th", "td"])
        all_th = [c for c in all_cells if c.name == "th"]
        th_count = len(all_th)
        total_cells = len(all_cells)
        if total_cells > 0 and 0 < th_count <= 3:
            th_ratio = th_count / total_cells
            first_row_cells = rows[0].find_all(["th", "td"])
            first_row_all_th = (
                all(c.name == "th" for c in first_row_cells)
                if first_row_cells else False
            )
            thead = table.find("thead")
            has_thead_header = (
                thead is not None and thead.find("th") is not None
            )
            if (
                th_ratio < 0.10
                and not first_row_all_th
                and not has_thead_header
            ):
                continue

        # Collect compact structural content for VLM context.
        text = _summarize_table_for_prompt(table)

        results.append({
            "table": table,
            "index": i,
            "text_context": text,
        })

    return results


def _summarize_table_for_prompt(
    table,
    *,
    max_rows: int = 8,
    max_cols: int = 8,
    max_cell_chars: int = 80,
    max_chars: int = 1400,
) -> str:
    """Create a bounded, structure-aware table representation for VLM prompts."""
    parts: List[str] = []

    prev_heading = table.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
    if prev_heading and prev_heading.get_text(strip=True):
        parts.append(f"Section: {_clean_cell_text(prev_heading.get_text(' ', strip=True), 120)}")

    headers: List[str] = []
    header_rows = table.find_all("tr")
    for row in header_rows:
        row_headers = [
            _clean_cell_text(cell.get_text(" ", strip=True), max_cell_chars)
            for cell in row.find_all("th")
        ]
        if row_headers:
            headers = row_headers[:max_cols]
            break
    if headers:
        parts.append("Headers: " + " | ".join(headers))

    rows = table.find_all("tr")
    body_lines: List[str] = []
    omitted_rows = 0
    for row in rows:
        cells = row.find_all(["th", "td"])
        if not cells:
            continue
        if cells and all(cell.name == "th" for cell in cells):
            continue
        values = [
            _clean_cell_text(cell.get_text(" ", strip=True), max_cell_chars)
            for cell in cells[:max_cols]
        ]
        if not any(values):
            continue
        if len(body_lines) >= max_rows:
            omitted_rows += 1
            continue
        if len(cells) > max_cols:
            values.append(f"... {len(cells) - max_cols} more columns")
        body_lines.append(" | ".join(values))

    if body_lines:
        parts.append("Representative rows:")
        parts.extend(body_lines)
    if omitted_rows:
        parts.append(f"... {omitted_rows} more rows omitted")

    summary = "\n".join(parts)
    if len(summary) <= max_chars:
        return summary
    return summary[: max_chars - 3].rstrip() + "..."


def _clean_cell_text(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _match_html_tables_to_structure(
    soup: BeautifulSoup,
    structure: Dict[str, Any],
) -> Dict[int, Dict[str, Any]]:
    """Map HTML table index to structure JSON TableItem entry.

    Both HTML tables and structure JSON entries preserve Docling's reading
    order, so they correspond 1:1 by position.
    """
    html_tables = soup.find_all("table")
    json_tables = [
        e for e in structure.get("elements", [])
        if e.get("type") == "TableItem"
    ]

    mapping = {}
    for i, json_entry in enumerate(json_tables):
        if i < len(html_tables):
            mapping[i] = json_entry

    return mapping


def _render_table_image(
    pdf_path: str,
    page_no: int,
    bbox: Dict[str, float],
    page_dimensions: Dict[str, float],
    output_path: str,
    padding: int = 10,
    dpi: int = 200,
) -> Optional[str]:
    """Render a table region from the PDF as a PNG image using PyMuPDF.

    Args:
        pdf_path: Path to original PDF.
        page_no: 1-based page number.
        bbox: Dict with l, b, r, t (bottom-left origin, PDF coords).
        page_dimensions: Dict with 'width' and 'height'.
        output_path: Where to save the rendered PNG.
        padding: Extra points around the bbox.
        dpi: Render resolution.

    Returns:
        Path to rendered image, or None on failure.
    """
    try:
        import fitz

        doc = fitz.open(pdf_path)
        page_idx = page_no - 1  # PyMuPDF is 0-based
        if page_idx < 0 or page_idx >= len(doc):
            logger.error(
                "Page %d out of range (PDF has %d pages)", page_no, len(doc)
            )
            doc.close()
            return None

        page = doc[page_idx]
        page_height = page_dimensions.get("height", page.rect.height)

        # Convert from bottom-left origin (PDF) to top-left origin (fitz)
        fitz_rect = fitz.Rect(
            bbox["l"] - padding,
            page_height - bbox["t"] - padding,
            bbox["r"] + padding,
            page_height - bbox["b"] + padding,
        )

        # Clip to page boundaries
        fitz_rect = fitz_rect & page.rect

        # Render at specified DPI
        zoom = safe_dpi(fitz_rect.width, fitz_rect.height, dpi) / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pixmap = page.get_pixmap(matrix=mat, clip=fitz_rect)
        pixmap.save(output_path)

        doc.close()
        logger.debug(
            "Rendered table image: page %d, bbox %s -> %s",
            page_no, bbox, output_path,
        )
        return output_path

    except Exception as e:
        logger.error("Failed to render table image: %s", e)
        return None
