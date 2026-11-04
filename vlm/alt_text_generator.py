# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alt text generation for images using a Vision Language Model.

Generates WCAG 2.1 AA compliant alt text by sending images + surrounding
context to a VLM (Ollama or OpenAI-compatible endpoint).
"""

import base64
import logging
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from converter.alt_harvest import is_generic_alt
from vlm.client_factory import create_vlm_client
from vlm.image_preprocessing import prepare_image_for_vlm

logger = logging.getLogger(__name__)

# System prompt for WCAG-compliant alt text generation
ALT_TEXT_SYSTEM_PROMPT = """You are an accessibility specialist generating alt text for images in PDF documents converted to HTML. Your alt text must comply with WCAG 2.1 AA standards.

Rules:
1. HARD LIMIT: Your entire response must be 150 characters or fewer. Count the characters before responding. If your description exceeds 150 characters, shorten it — do not exceed this limit under any circumstances.
2. Focus on the information the image conveys, not how it looks ("bar chart showing sales growth" not "colorful image").
3. For charts/graphs: state the type of visualization and the key takeaway.
4. For diagrams: describe the structure and relationships shown.
5. For photos: describe the relevant content and context.
6. For equations/formulas: provide the mathematical expression in text.
7. For decorative images: respond with exactly "decorative".
8. Do NOT start with "Image of" or "Picture of" — just describe the content directly.
9. Do NOT include the word "alt text" in your response.
10. Respond with ONLY the alt text, nothing else."""

# Prompt template for individual images
ALT_TEXT_PROMPT = """Generate alt text for this image from a PDF document.

{context}

Provide concise, descriptive alt text suitable for a screen reader."""


def generate_alt_text_for_images(
    html_path: str,
    image_files: List[str],
    image_dir: Optional[str],
    llm_config: Dict[str, Any],
    audit_result: Optional[Dict[str, Any]] = None,
    struct_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate alt text for images that need it.

    Examines the audit results to find images with empty/missing/generic alt text,
    then uses the VLM to generate proper alt text.

    Args:
        html_path: Path to the HTML file.
        image_files: List of extracted image file paths.
        image_dir: Directory containing the images.
        llm_config: LLM configuration from config.yaml.
        audit_result: Audit report (used to identify which images need alt text).
        struct_data: Docling structure JSON dict — used to look up PDF page
            numbers for each image in the Document Changes Report.

    Returns:
        Dictionary with:
            - alt_texts: Dict mapping image src to generated alt text.
            - updated_count: Number of images updated.
            - skipped_count: Number of images skipped.
            - image_records: List of per-image dicts for the changes report.
            - stats: Timing and token stats.
    """
    # Build image_ref-basename → page_no map from docling structure
    _img_page_map: Dict[str, int] = {}
    if struct_data:
        for elem in struct_data.get("elements", []):
            if elem.get("type") == "PictureItem":
                ref = elem.get("image_ref", "")
                pno = elem.get("page_no")
                if ref and pno is not None:
                    _img_page_map[Path(ref).name] = pno
    start_time = time.time()

    def _page_for_src(src: str) -> Optional[int]:
        """Look up the PDF page number for an image src using the struct map."""
        return _img_page_map.get(Path(src).name)

    # Initialize VLM client
    client = create_vlm_client(llm_config)

    # Check connectivity
    if not client.check_connection():
        logger.error(f"Cannot connect to VLM at {client.base_url}")
        return {
            "alt_texts": {},
            "updated_count": 0,
            "skipped_count": 0,
            "image_records": [],
            "error": f"Cannot connect to VLM at {client.base_url}",
            "stats": {"time_seconds": 0},
        }

    # Parse the HTML to find images needing alt text
    html_path = Path(html_path)
    with open(html_path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    img_tags = soup.find_all("img")
    if not img_tags:
        logger.info("No images found in HTML")
        return {
            "alt_texts": {},
            "updated_count": 0,
            "skipped_count": 0,
            "image_records": [],
            "stats": {"time_seconds": 0},
        }

    # Determine which images need alt text
    images_to_process = _find_images_needing_alt(img_tags, image_dir, image_files)

    if not images_to_process:
        logger.info(
            "All %d images already have proper alt text, skipping VLM generation",
            len(img_tags),
        )
        # Not skipped: skipped_count means "VLM failed", and the phase status
        # would report every one of these as a failure.
        return {
            "alt_texts": {},
            "updated_count": 0,
            "skipped_count": 0,
            "already_had_alt_count": len(img_tags),
            "image_records": [],
            "stats": {"time_seconds": 0},
        }

    # Enforce per-document image limit to prevent VLM cost abuse
    max_images = llm_config.get("max_images_per_document", 100)
    capped_images_warning = None
    if len(images_to_process) > max_images:
        capped_images_warning = (
            f"Only {max_images} of {len(images_to_process)} images were processed "
            f"for alt text. The remaining images may need manual alt text."
        )
        logger.warning(
            "Document has %d images needing alt text — capping at %d (max_images_per_document)",
            len(images_to_process), max_images,
        )
        images_to_process = images_to_process[:max_images]

    logger.info(f"Generating alt text for {len(images_to_process)} images")
    image_max_long_edge = int(llm_config.get("vlm_image_max_long_edge", 1600))
    image_output_format = str(llm_config.get("vlm_image_output_format", "png"))
    image_jpeg_quality = int(llm_config.get("vlm_image_jpeg_quality", 90))

    # --- Phase A: Prepare work items (single-threaded, accesses soup) ---
    work_items = []
    skipped_early = []

    for img_info in images_to_process:
        img_tag = img_info["tag"]
        img_file = img_info.get("file")
        src = img_tag.get("src", "")
        page_number = _page_for_src(src)

        if not img_file:
            logger.warning(
                "  Skipped image src=%s: could not resolve file path (not in image_dir or extracted files)",
                src,
            )
            skipped_early.append({
                "src": src, "file_path": None, "alt_text": None,
                "outcome": "skipped", "skip_reason": "Image file could not be resolved",
                "page_number": page_number,
            })
            continue
        if not Path(img_file).exists():
            logger.warning(
                "  Skipped image src=%s: resolved path %s does not exist on disk",
                src, img_file,
            )
            skipped_early.append({
                "src": src, "file_path": img_file, "alt_text": None,
                "outcome": "skipped", "skip_reason": "Image file not found on disk",
                "page_number": page_number,
            })
            continue

        prepared_img_file, prepared_is_temp, prepared_meta = prepare_image_for_vlm(
            img_file,
            max_long_edge=image_max_long_edge,
            output_format=image_output_format,
            jpeg_quality=image_jpeg_quality,
        )

        # Collect context from soup (must be single-threaded)
        context = _collect_image_context(img_tag, soup)
        context_text = f"Surrounding context from the document:\n{context}" if context else ""
        prompt = ALT_TEXT_PROMPT.format(context=context_text)

        work_items.append({
            "tag": img_tag,
            "src": src,
            "img_file": img_file,
            "vlm_img_file": prepared_img_file,
            "vlm_img_is_temp": prepared_is_temp,
            "prepared_meta": prepared_meta,
            "prompt": prompt,
            "page_number": page_number,
        })

    # --- Phase B: Run VLM calls in parallel (I/O-bound, thread-safe) ---
    max_workers = min(llm_config.get("max_parallel", 4), len(work_items)) if work_items else 1
    vlm_results: Dict[str, Dict] = {}  # keyed by src

    if work_items:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_src = {
                pool.submit(
                    _generate_single_alt_text,
                    client, item["vlm_img_file"], item["prompt"],
                ): item
                for item in work_items
            }
            for future in as_completed(future_to_src):
                item = future_to_src[future]
                src = item["src"]
                try:
                    vlm_results[src] = future.result()
                except Exception as e:
                    logger.error("  Skipped image src=%s: VLM generation failed — %s", src, e)
                    vlm_results[src] = {"alt_text": None, "error": str(e)}

    # Clean up temp files created from data URIs
    for item in work_items:
        img_file = item.get("img_file", "")
        if img_file and os.path.basename(img_file).startswith("alt_text_img_"):
            try:
                os.unlink(img_file)
            except OSError:
                pass
        vlm_img_file = item.get("vlm_img_file", "")
        if item.get("vlm_img_is_temp") and vlm_img_file:
            try:
                os.unlink(vlm_img_file)
            except OSError:
                pass

    # --- Phase C: Apply results to soup (single-threaded) ---
    updated_count, skipped_count, image_records, alt_texts = _apply_vlm_results(
        work_items, vlm_results, skipped_early
    )

    # Write updated HTML back
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(str(soup))

    elapsed = time.time() - start_time
    logger.info(
        f"Alt text generation complete: {updated_count} updated, "
        f"{skipped_count} skipped in {elapsed:.1f}s"
    )

    result = {
        "alt_texts": alt_texts,
        "updated_count": updated_count,
        "skipped_count": skipped_count,
        "image_records": image_records,
        "stats": {"time_seconds": round(elapsed, 2)},
    }
    if capped_images_warning:
        result["warning"] = capped_images_warning
    return result


def _parse_confidence(raw_text: str) -> Tuple[str, Optional[int]]:
    """Extract alt text and confidence score from VLM response.

    Expects format:
        <alt text>
        CONFIDENCE: XX%

    Returns:
        (clean_alt_text, confidence_int_or_None)
    """
    # Try to find CONFIDENCE: XX% anywhere in the response
    match = re.search(r'CONFIDENCE:\s*(\d{1,3})%', raw_text, re.IGNORECASE)
    if match:
        confidence = min(int(match.group(1)), 100)
        # Remove the confidence line from the alt text
        alt_text = re.sub(
            r'\n?\s*CONFIDENCE:\s*\d{1,3}%\s*$', '', raw_text, flags=re.IGNORECASE,
        ).strip()
        return alt_text, confidence
    return raw_text.strip(), None


def _generate_single_alt_text(client, img_file: str, prompt: str) -> Dict[str, Any]:
    """Call VLM for a single image. Thread-safe — no soup mutation.

    Returns:
        {"alt_text": str, "confidence": int|None} on success,
        {"alt_text": None, "error": str} on failure.
    """
    try:
        raw = client.generate(
            prompt=prompt,
            images=[img_file],
            system_prompt=ALT_TEXT_SYSTEM_PROMPT,
        )
        alt_text, confidence = _parse_confidence(raw)
        alt_text = alt_text.strip('"').strip("'")
        return {"alt_text": alt_text, "confidence": confidence}
    except Exception as e:
        return {"alt_text": None, "confidence": None, "error": str(e)}


def _apply_vlm_results(work_items, vlm_results, skipped_early=None):
    """Apply VLM results to image tags.

    Returns ``(updated_count, skipped_count, image_records, alt_texts)``.
    Extracted from the inline Phase-C loop so the empty-completion guard is
    testable without a live model (G-29).  ``skipped_early`` carries images
    already rejected before the VLM ran; it seeds both the record list and the
    skipped counter exactly as the inline version did.
    """
    skipped_early = list(skipped_early or [])
    alt_texts = {}
    image_records = list(skipped_early)
    updated_count = 0
    skipped_count = len(skipped_early)

    for item in work_items:
        src = item["src"]
        img_tag = item["tag"]
        img_file = item["img_file"]
        page_number = item["page_number"]
        result = vlm_results.get(src, {})

        if result.get("error"):
            skipped_count += 1
            image_records.append({
                "src": src, "file_path": img_file, "alt_text": None,
                "outcome": "skipped",
                "skip_reason": f"VLM generation failed: {result['error']}",
                "page_number": page_number,
            })
            continue

        alt_text = result.get("alt_text", "")

        confidence = result.get("confidence")

        # An empty or near-empty completion is a FAILURE, not a description.
        # The VLM can return only its confidence footer, which _parse_confidence
        # strips to "" with no error key — so the guard above does not catch it.
        # Both sibling VLM paths already reject len < 3 (table_caption_generator
        # :231, formula_processor :225); alt text must too (G-29).
        if not alt_text or len(alt_text.strip()) < 3:
            skipped_count += 1
            image_records.append({
                "src": src, "file_path": img_file, "alt_text": None,
                "outcome": "skipped",
                "skip_reason": "VLM returned an empty or too-short description",
                "page_number": page_number, "confidence": confidence,
            })
            continue

        if alt_text.lower() == "decorative":
            img_tag["alt"] = ""
            img_tag["role"] = "presentation"
            logger.info(f"  Image {src}: marked as decorative")
            alt_texts[src] = alt_text
            updated_count += 1
            image_records.append({
                "src": src, "file_path": img_file, "alt_text": "",
                "outcome": "decorative", "skip_reason": None,
                "page_number": page_number, "confidence": confidence,
                "image_preprocessing": item.get("prepared_meta") or None,
            })
        else:
            img_tag["alt"] = alt_text
            conf_str = f" (confidence {confidence}%)" if confidence is not None else ""
            logger.info(
                f"  Image {src}: \"{alt_text[:80]}...\"{conf_str}" if len(alt_text) > 80
                else f"  Image {src}: \"{alt_text}\"{conf_str}",
                extra={"contains_content": True, "content_ref": src},
            )
            alt_texts[src] = alt_text
            updated_count += 1
            image_records.append({
                "src": src, "file_path": img_file, "alt_text": alt_text,
                "outcome": "generated", "skip_reason": None,
                "page_number": page_number, "confidence": confidence,
                "image_preprocessing": item.get("prepared_meta") or None,
            })

    return updated_count, skipped_count, image_records, alt_texts


def _find_images_needing_alt(
    img_tags: list,
    image_dir: Optional[str],
    image_files: List[str],
) -> List[Dict[str, Any]]:
    """Identify images that need alt text generated.

    Returns a list of dicts with 'tag' (the BS4 img tag) and 'file' (path to image file).
    """
    results = []
    image_dir = Path(image_dir) if image_dir else None

    # Build a lookup from filename to full path
    file_lookup = {}
    for f in image_files:
        file_lookup[Path(f).name] = f

    for img in img_tags:
        alt = img.get("alt", None)

        # Needs alt text if: missing alt, empty alt (not decorative), or generic alt
        needs_alt = False
        if alt is None:
            needs_alt = True
        elif alt.strip() == "" and img.get("role") != "presentation" and img.get("aria-hidden") != "true":
            needs_alt = True
        # `alt.strip() and` is load-bearing: is_generic_alt("") is True, and
        # without the guard a decorative image (alt="" role="presentation")
        # would fall through to here and be sent to the VLM (G-07).
        elif alt.strip() and is_generic_alt(alt):
            needs_alt = True

        if not needs_alt:
            continue

        # Resolve the image file path
        src = img.get("src", "")
        img_file = _resolve_image_path(src, image_dir, file_lookup)

        results.append({"tag": img, "file": img_file, "src": src})

    return results


def _resolve_image_path(
    src: str,
    image_dir: Optional[Path],
    file_lookup: Dict[str, str],
) -> Optional[str]:
    """Resolve an img src attribute to an actual file path.

    Handles data URIs (base64-embedded images) by decoding them to temp files.
    """
    if not src:
        return None

    # Handle base64 data URIs — decode to a temp file
    if src.startswith("data:"):
        return _data_uri_to_temp_file(src)

    # Try direct path
    try:
        if Path(src).exists():
            return str(Path(src))
    except OSError:
        # Path too long or invalid characters
        return None

    # Try filename in lookup
    try:
        src_name = Path(src).name
    except (OSError, ValueError):
        return None
    if src_name in file_lookup:
        return file_lookup[src_name]

    # Try in image_dir
    if image_dir:
        candidate = image_dir / src_name
        if candidate.exists():
            return str(candidate)

    return None


def _data_uri_to_temp_file(data_uri: str) -> Optional[str]:
    """Decode a base64 data URI and write to a temp file. Returns the file path."""
    # Parse: data:[<mediatype>][;base64],<data>
    match = re.match(r"data:image/(\w+);base64,(.+)", data_uri, re.DOTALL)
    if not match:
        logger.warning("Unrecognized data URI format (expected data:image/...;base64,...)")
        return None

    ext = match.group(1)
    if ext == "jpeg":
        ext = "jpg"
    b64_data = match.group(2)

    try:
        img_bytes = base64.b64decode(b64_data)
    except Exception as e:
        logger.warning("Failed to decode base64 image data: %s", e)
        return None

    try:
        tmp = tempfile.NamedTemporaryFile(
            suffix=f".{ext}", prefix="alt_text_img_", delete=False,
        )
        tmp.write(img_bytes)
        tmp.close()
        logger.debug("Decoded data URI to temp file: %s (%d bytes)", tmp.name, len(img_bytes))
        return tmp.name
    except Exception as e:
        logger.warning("Failed to write temp image file: %s", e)
        return None


def _collect_image_context(img_tag, soup: BeautifulSoup) -> str:
    """Collect surrounding text context for an image to help the VLM."""
    parts = []

    # Figure caption
    figure = img_tag.find_parent("figure")
    if figure:
        caption = figure.find("figcaption")
        if caption and caption.get_text(strip=True):
            parts.append(f"Caption: {caption.get_text(strip=True)}")

    # Nearest heading before the image
    prev_heading = img_tag.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
    if prev_heading and prev_heading.get_text(strip=True):
        parts.append(f"Section heading: {prev_heading.get_text(strip=True)}")

    # Previous paragraph
    prev_p = img_tag.find_previous("p")
    if prev_p and prev_p.get_text(strip=True):
        text = prev_p.get_text(strip=True)[:200]
        parts.append(f"Preceding text: {text}")

    # Next paragraph
    next_p = img_tag.find_next("p")
    if next_p and next_p.get_text(strip=True):
        text = next_p.get_text(strip=True)[:200]
        parts.append(f"Following text: {text}")

    return "\n".join(parts) if parts else ""
