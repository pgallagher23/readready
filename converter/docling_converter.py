# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF to HTML converter using Docling for document understanding.

This module replaces both Adobe AutoTag and AWS Bedrock Data Automation
with a fully local document understanding pipeline. Docling identifies
headings, paragraphs, tables, figures, reading order, and exports
semantic HTML suitable for WCAG 2.1 AA remediation.
"""

from utils.raster import safe_dpi
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    AcceleratorOptions,
    LayoutOptions,
    PdfPipelineOptions,
    TableFormerMode,
    TableStructureOptions,
    TesseractCliOcrOptions,
)
from docling.datamodel.layout_model_specs import (
    DOCLING_LAYOUT_EGRET_LARGE,
    DOCLING_LAYOUT_EGRET_XLARGE,
    DOCLING_LAYOUT_HERON,
)
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import ImageRefMode, PictureItem, TableItem
try:
    from docling_core.types.doc import FormulaItem
except ImportError:
    FormulaItem = None  # Older docling_core without FormulaItem

from converter.element_stamper import stamp_element_ids
from output_gen.heading_detection import promote_document_heading
from output_gen.heading_inference import infer_heading_levels
from output_gen.tag_mapping import map_element_to_tag
from utils.toc_detector import classify_tables_in_soup, TYPE_DATA

logger = logging.getLogger(__name__)

# Canonical classification strings — shared with the yield tripwire.
from logging_util.failure_taxonomy import (  # noqa: E402
    CLASS_FULLY_SCANNED,
    CLASS_MIXED,
    CLASS_TEXT_ONLY,
)

# iText tagger jar — reused here for the font-fix pre-pass (--tag-mode fix-fonts).
_ITEXT_JAR = Path(__file__).resolve().parent.parent / "output_gen" / "itext_tagger.jar"


def _text_corruption_ratio(pdf_path: Path) -> float:
    """Fraction of control/PUA chars in pdfium's text extraction.

    Type-3 LaTeX fonts that lack /ToUnicode make extractors (including
    Docling's pdfium backend) emit raw glyph codes — C0/C1 control chars or
    Private Use Area code points — instead of real Unicode. Legitimate body
    text is ~0%; a non-trivial ratio reliably flags a PDF whose source text
    needs a ToUnicode pre-pass. Returns 0.0 when extraction is unavailable or
    the page text is too short to judge.
    """
    try:
        import pypdfium2
        doc = pypdfium2.PdfDocument(str(pdf_path))
        try:
            text = "".join(
                doc[i].get_textpage().get_text_range() for i in range(len(doc))
            )
        finally:
            doc.close()
    except Exception as e:  # pragma: no cover - extractor edge cases
        logger.debug("Corruption check skipped (%s)", e)
        return 0.0

    nonspace = [c for c in text if not c.isspace()]
    if len(nonspace) < 200:
        return 0.0
    bad = sum(
        1 for c in nonspace
        if ord(c) < 0x20 or 0x80 <= ord(c) <= 0x9F or 0xE000 <= ord(c) <= 0xF8FF
    )
    return bad / len(nonspace)


def _fix_fonts_prepass(pdf_path: Path, output_dir: Path) -> Optional[Path]:
    """Inject correct /ToUnicode CMaps into ``pdf_path`` via the iText tagger.

    Runs ``--tag-mode fix-fonts``, which reuses the tagger's proven CMap
    generation. Returns the path to a font-fixed copy of the PDF, or None if
    the pre-pass is unavailable or fails (caller falls back to the original).
    """
    import shutil
    import subprocess

    if not _ITEXT_JAR.exists() or not shutil.which("java"):
        logger.debug("Font-fix pre-pass unavailable (jar or java missing)")
        return None

    fixed = output_dir / f"{pdf_path.stem}_fontfixed.pdf"
    cmd = [
        "java", "-jar", str(_ITEXT_JAR),
        "--tag-mode", "fix-fonts",
        "--input", str(pdf_path),
        "--output", str(fixed),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("Font-fix pre-pass failed to run (%s)", e)
        return None
    if proc.returncode != 0 or not fixed.exists():
        logger.warning(
            "Font-fix pre-pass failed (exit %s); using original PDF",
            proc.returncode,
        )
        return None
    logger.info("Font-fix pre-pass: injected ToUnicode CMaps -> %s", fixed.name)
    return fixed

# Scale factor for rendered images (1.0 = 72 DPI, 2.0 = 144 DPI)
IMAGE_RESOLUTION_SCALE = 2.0


def _classify_pdf_text(pdf_path: Path) -> Dict[str, Any]:
    """Classify all pages by text extraction potential.

    Scans every page to determine which have native text layers and which
    are likely scanned/image-only.  The ``recommendation`` field gates OCR
    forcing in :func:`convert_pdf_to_html`: a ``fully_scanned`` document is
    converted with ``force_full_page_ocr=True``, because Docling's own OCR
    triggers (programmatic text, bitmap coverage) are both blind to
    vectorized scans — pages whose glyphs are drawn as filled paths with no
    bitmaps and no text objects (e.g. Acrobat ClearScan output).

    Returns:
        Dict with total_pages, pages_with_text, pages_without_text,
        text_per_page (char counts), and recommendation.
    """
    result = {
        "total_pages": 0,
        "pages_with_text": [],
        "pages_without_text": [],
        "text_per_page": {},
        "recommendation": "text_only",
    }
    try:
        import pypdf
        with open(pdf_path, "rb") as f:
            reader = pypdf.PdfReader(f)
            result["total_pages"] = len(reader.pages)
            for i, page in enumerate(reader.pages):
                page_no = i + 1
                page_text = (page.extract_text() or "").strip()
                char_count = len(page_text)
                result["text_per_page"][page_no] = char_count
                if char_count > 50:
                    result["pages_with_text"].append(page_no)
                else:
                    result["pages_without_text"].append(page_no)
    except Exception as e:
        logger.warning("Could not classify PDF pages for text (%s)", e)
        return result

    n_with = len(result["pages_with_text"])
    n_without = len(result["pages_without_text"])
    total = result["total_pages"]

    # NOTE: "has text" is a proxy — pypdf extract_text() > 50 chars. A page
    # stamped with a long watermark ("Downloaded from ...") counts as texty,
    # so a stamped vector scan can classify text_only and dodge both the OCR
    # forcing and the yield tripwire. Known limitation; the mechanism-level
    # check (no text objects AND no bitmaps, per page) is future work.
    if n_without == 0:
        result["recommendation"] = CLASS_TEXT_ONLY
    elif n_with == 0:
        result["recommendation"] = CLASS_FULLY_SCANNED
    else:
        result["recommendation"] = CLASS_MIXED

    logger.info(
        "PDF text classification: %s (%d/%d pages with text, %d without)",
        result["recommendation"], n_with, total, n_without,
    )
    return result


# A "fully_scanned" doc may still carry a trickle of real text (page
# numbers, junk form fields). Above this many extracted chars across the
# WHOLE document we assume the text is genuine — e.g. a photo deck with
# short captions — and never force, because force would drop those real
# text cells (see below). The motivating vector-scan class extracts 0.
FORCE_OCR_MAX_DOC_CHARS = 200


def _should_force_full_page_ocr(
    classification: Dict[str, Any], opts: Dict[str, Any], do_ocr: bool
) -> bool:
    """Whether to run Docling with ``force_full_page_ocr=True``.

    Forced ONLY for ``fully_scanned`` documents with essentially no
    extractable text anywhere (< ``FORCE_OCR_MAX_DOC_CHARS`` total): under
    force, Docling drops all non-OCR text cells
    (``base_ocr_model._combine_cells``), so forcing a ``mixed`` document —
    or a sparse-text one like a captioned photo deck — would replace real
    text with OCR.  For true bitmap scans the flag is a no-op (their
    full-page bitmaps already trigger full-page OCR via the coverage gate;
    measured byte-identical); it exists for the class the coverage gate
    cannot see — vectorized scans.  ``conversion.force_ocr_scanned: false``
    is the config kill-switch.

    Known limitation: the decision is per-document, so a vector-scan book
    with one genuinely texty page classifies ``mixed`` and is not forced;
    the structure-yield tripwire (``check_docling_yield_low``) still flags
    that case.
    """
    if not (do_ocr and opts.get("force_ocr_scanned", True)):
        return False
    if classification.get("recommendation") != CLASS_FULLY_SCANNED:
        return False
    total_chars = sum((classification.get("text_per_page") or {}).values())
    return total_chars < FORCE_OCR_MAX_DOC_CHARS


# Map common Tesseract 3-letter language codes to the ISO 639-1 codes that
# wordfreq expects. Falls back to the first two letters when unknown.
_TESS_TO_WORDFREQ_LANG = {
    "eng": "en", "fra": "fr", "deu": "de", "spa": "es",
    "ita": "it", "por": "pt", "nld": "nl",
}


def _quick_ocr_text(image_path: str, angle: int, tesseract_cmd: str,
                    lang_codes: List[str]) -> str:
    """OCR ``image_path`` (optionally rotated by ``angle``) and return raw text.

    Rotation matches Docling's own convention (``Image.rotate(-angle,
    expand=True)``) so the comparison reflects what Docling would actually
    feed to recognition.
    """
    import subprocess
    import tempfile

    target = image_path
    tmp_path: Optional[str] = None
    try:
        if angle:
            from PIL import Image
            with Image.open(image_path) as im:
                rotated = im.rotate(-angle, expand=True)
                fd = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                tmp_path = fd.name
                fd.close()
                rotated.save(tmp_path)
            target = tmp_path
        cmd = [tesseract_cmd, "-l", "+".join(lang_codes or ["eng"]),
               "--psm", "3", target, "stdout"]
        out = subprocess.run(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, check=True, timeout=60)
        return out.stdout.decode("utf-8", "replace")
    except Exception as e:
        logger.debug("quick OCR (angle=%s) failed: %s", angle, e)
        return ""
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _verified_osd_orientation(image_path: str, proposed_angle: int, *,
                              tesseract_cmd: str = "tesseract",
                              lang_codes: Optional[List[str]] = None,
                              zipf_threshold: float = 3.0) -> int:
    """Return the rotation angle to actually apply for an OCR region.

    Tesseract OSD is run unconditionally by Docling and can be *confidently*
    wrong (e.g. an upright English magazine scan: a page for which OSD
    reports ``Rotate: 180`` at confidence ~7). Trusting it flips a good page
    upside-down and the OCR comes out as gibberish.

    When OSD proposes a non-zero rotation we OCR BOTH the original and the
    proposed rotation and keep whichever yields the higher real-word ratio
    (reusing the wordfreq scorer from ``audit.ocr_quality``). Returns 0 when
    the original is at least as good — defeating OSD false-positives — and the
    proposed angle when rotating genuinely improves the text.
    """
    if not proposed_angle:
        return 0
    lang_codes = lang_codes or ["eng"]
    wf_lang = _TESS_TO_WORDFREQ_LANG.get(lang_codes[0], lang_codes[0][:2])

    try:
        from audit.ocr_quality import _wordfreq_hit_ratio
    except Exception as e:
        # Without the scorer we cannot verify; fail safe by NOT rotating,
        # since an unverified OSD flip is the failure mode we're guarding.
        logger.debug("wordfreq scorer unavailable, skipping OSD rotation: %s", e)
        return 0

    txt_orig = _quick_ocr_text(image_path, 0, tesseract_cmd, lang_codes)
    txt_rot = _quick_ocr_text(image_path, proposed_angle, tesseract_cmd, lang_codes)
    r_orig = _wordfreq_hit_ratio(txt_orig, wf_lang, zipf_threshold)
    r_rot = _wordfreq_hit_ratio(txt_rot, wf_lang, zipf_threshold)

    logger.debug(
        "OSD proposed %s°: real-word ratio original=%.3f rotated=%.3f",
        proposed_angle, r_orig, r_rot,
    )
    return proposed_angle if r_rot > r_orig else 0


_OSD_PATCH_INSTALLED = False


def _install_osd_verification_patch() -> None:
    """Wrap Docling's Tesseract OSD so proposed rotations are OCR-verified.

    Docling 2.96.0 applies the OSD-proposed rotation unconditionally (it never
    consults the orientation confidence), which corrupts upright scans where
    OSD false-positives a 180° flip. We wrap ``_perform_osd`` so that when it
    proposes a rotation we verify it with :func:`_verified_osd_orientation` and
    rewrite the ``Orientation in degrees`` row when verification disagrees.
    Best-effort: if Docling's internals have changed, the patch is skipped.
    """
    global _OSD_PATCH_INSTALLED
    if _OSD_PATCH_INSTALLED:
        return
    try:
        from docling.models.stages.ocr import tesseract_ocr_cli_model as _t
    except Exception as e:
        logger.debug("OSD verification patch skipped (import failed): %s", e)
        return

    Model = getattr(_t, "TesseractOcrCliModel", None)
    parse_orientation = getattr(_t, "_parse_orientation", None)
    if Model is None or parse_orientation is None or not hasattr(Model, "_perform_osd"):
        logger.debug("OSD verification patch skipped (unexpected docling layout)")
        return

    orig_perform_osd = Model._perform_osd

    def _patched_perform_osd(self, ifilename):
        df = orig_perform_osd(self, ifilename)
        try:
            proposed = parse_orientation(df)
        except Exception:
            return df
        if not proposed:
            return df
        lang_codes = list(self.options.lang) if self.options.lang else ["eng"]
        verified = _verified_osd_orientation(
            ifilename, proposed,
            tesseract_cmd=self.options.tesseract_cmd,
            lang_codes=lang_codes,
        )
        if verified != proposed:
            mask = df["key"].astype(str).str.strip() == "Orientation in degrees"
            df.loc[mask, "value"] = f" {verified}"
            logger.info(
                "OSD proposed %s° rotation; OCR verification overrode to %s° "
                "(real-word ratio) for %s",
                proposed, verified, os.path.basename(str(ifilename)),
            )
        return df

    Model._perform_osd = _patched_perform_osd
    _OSD_PATCH_INSTALLED = True
    logger.info("Installed OCR-verified OSD orientation patch for Docling Tesseract model")


def _build_converter(
    options: Optional[Dict[str, Any]] = None,
    do_ocr: bool = True,
    force_full_page_ocr: bool = False,
) -> DocumentConverter:
    """Create a configured DocumentConverter instance.

    Args:
        options: Conversion options from config.yaml. Supports:
            - extract_images (bool): Whether to extract images. Default True.
            - image_scale (float): Image resolution scale. Default 2.0.
        do_ocr: Whether to enable OCR.  Defaults to True so Docling's
            per-page bitmap_area_threshold can automatically OCR scanned
            pages while using native text on text-based pages.
        force_full_page_ocr: OCR every page at full size regardless of
            bitmap coverage.  Set for fully_scanned documents, whose pages
            may carry no detectable bitmaps at all (vectorized scans) and
            would otherwise silently get no OCR.

    Returns:
        A configured DocumentConverter.
    """
    opts = options or {}

    # Prevent HuggingFace Hub from phoning home for model updates,
    # telemetry, or implicit token transmission.
    if opts.get("offline_models", False):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"

    extract_images = opts.get("extract_images", True)
    image_scale = opts.get("image_scale", IMAGE_RESOLUTION_SCALE)

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = do_ocr
    pipeline_options.images_scale = image_scale
    pipeline_options.generate_page_images = False
    pipeline_options.generate_picture_images = extract_images

    if do_ocr:
        # Use Tesseract CLI for OCR (no tesserocr Python binding needed).
        # bitmap_area_threshold=0.05 means only pages where bitmaps cover
        # >5% of the area get OCR'd — text-based pages skip OCR automatically.
        # CAVEAT: the coverage gate sees only bitmaps. A page with neither
        # text nor bitmaps (vector-drawn scan) gets NO OCR unless
        # force_full_page_ocr is set (see _should_force_full_page_ocr).
        pipeline_options.ocr_options = TesseractCliOcrOptions(
            lang=["eng"],
            force_full_page_ocr=force_full_page_ocr,
            bitmap_area_threshold=0.05,
        )
        # Docling applies Tesseract's OSD-proposed rotation unconditionally,
        # which flips upright scans upside-down when OSD false-positives a
        # 180° rotation. Verify proposed rotations against OCR
        # real-word ratio before trusting them.
        _install_osd_verification_patch()
    else:
        # Explicit opt-out: use embedded text directly (fast path).
        pipeline_options.force_backend_text = True

    # Layout model selection: "heron" (default), "egret_large", "egret_xlarge"
    layout_model_name = opts.get("layout_model", "heron")
    _layout_model_map = {
        "heron": DOCLING_LAYOUT_HERON,
        "egret_large": DOCLING_LAYOUT_EGRET_LARGE,
        "egret_xlarge": DOCLING_LAYOUT_EGRET_XLARGE,
    }
    layout_spec = _layout_model_map.get(layout_model_name)
    if layout_spec:
        pipeline_options.layout_options = LayoutOptions(model_spec=layout_spec)

    # Table structure mode: "fast" (default) or "accurate"
    table_mode_str = opts.get("table_mode", "fast")
    table_mode = TableFormerMode.ACCURATE if table_mode_str == "accurate" else TableFormerMode.FAST
    pipeline_options.table_structure_options = TableStructureOptions(
        do_cell_matching=True,
        mode=table_mode,
    )
    device = opts.get("device", "auto")
    num_threads = opts.get("num_threads", 8)
    pipeline_options.accelerator_options = AcceleratorOptions(
        num_threads=num_threads,
        device=device,
        cuda_use_flash_attention2=opts.get("cuda_flash_attention2", False),
    )

    # Larger batch sizes when GPU is available (32GB VRAM can handle it).
    # Fall back to conservative sizes on CPU.
    default_batch = opts.get("batch_size", 32 if device != "cpu" else 4)
    pipeline_options.layout_batch_size = default_batch
    pipeline_options.table_batch_size = default_batch
    pipeline_options.ocr_batch_size = default_batch
    pipeline_options.batch_polling_interval_seconds = 0.1

    # Log device selection once
    logger.info("Docling accelerator: device=%s, batch_size=%d, threads=%d",
                device, default_batch, num_threads)

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
        }
    )
    return converter


def _save_images(
    conv_result,
    image_dir: Path,
) -> List[str]:
    """Extract and save figure images from the conversion result.

    Args:
        conv_result: The ConversionResult from Docling.
        image_dir: Directory to save extracted images.

    Returns:
        List of saved image file paths.
    """
    image_dir.mkdir(parents=True, exist_ok=True)
    saved_images = []
    picture_counter = 0

    for element, _level in conv_result.document.iterate_items():
        if isinstance(element, PictureItem):
            picture_counter += 1
            img = element.get_image(conv_result.document)
            if img is None:
                logger.warning(f"Picture {picture_counter} has no image data, skipping")
                continue

            filename = f"figure_{picture_counter}.png"
            filepath = image_dir / filename
            img.save(str(filepath), "PNG")
            saved_images.append(str(filepath))
            logger.debug(f"Saved figure image: {filepath}")

    logger.info(f"Extracted {len(saved_images)} figure images")
    return saved_images


def _latin_letter_fraction(sample: str) -> float:
    """Fraction of the sample's letters that are ASCII A-Za-z.

    Returns 1.0 when the sample contains no letters at all, so an empty
    or digits-only sample is treated as Latin (and therefore falls
    through to the safe "en" fallback) rather than as a non-Latin script.
    """
    letters = [c for c in sample if c.isalpha()]
    if not letters:
        return 1.0
    return sum(1 for c in letters if "a" <= c.lower() <= "z") / len(letters)


def _corroborate_language(sample: str, detected: str, *,
                          zipf_threshold: float = 3.0,
                          min_latin_fraction: float = 0.5) -> str:
    """Confirm a non-English langdetect result against wordfreq.

    langdetect reports ~1.0 confidence even when it is wrong: a
    table/number-heavy English report scored ``nl`` at 0.99999, three English financial-aid forms scored ``cy``, two
    scored ``de`` and one ``tl``. Confidence thresholds therefore cannot
    filter these. Real-word frequency can — genuine foreign prose scores
    far higher in its own language than in English (measured 0.76-0.86 vs
    0.23-0.50), while a misdetected English document does not.

    The comparison is strict ``>`` on purpose. wordfreq does not reject
    an unsupported code, it silently ALIASES it to the nearest wordlist
    (``cy`` -> ``en``, ``tl`` -> ``fil``); for the Welsh false positives
    that makes both sides score identically, and only ``>`` reverts them.

    Returns ``detected`` when it out-scores English, else ``"en"``.
    """
    if not detected or detected == "en":
        return "en"

    # wordfreq's tokenizer is [A-Za-z]+, so a Cyrillic/Arabic/Greek/CJK
    # sample yields zero tokens and BOTH ratios come back 0.0 — which
    # would force "en" onto a document langdetect got right. Corroboration
    # is only meaningful for Latin-script text; trust langdetect otherwise.
    if _latin_letter_fraction(sample) < min_latin_fraction:
        logger.info(
            "Language %s accepted without corroboration "
            "(non-Latin script — wordfreq cannot score it)", detected,
        )
        return detected

    try:
        from audit.ocr_quality import _wordfreq_hit_ratio
    except Exception as e:
        # Without the scorer we cannot corroborate. Fail safe to English:
        # a wrong /Lang makes a screen reader mispronounce every word.
        logger.debug("wordfreq scorer unavailable, keeping 'en': %s", e)
        return "en"

    try:
        hit_detected = _wordfreq_hit_ratio(sample, detected, zipf_threshold)
    except Exception as e:
        # zipf_frequency raises LookupError for a code langcodes cannot
        # match at all (e.g. "zz"); an empty code raises LanguageTagError.
        logger.warning("wordfreq has no data for %r — keeping 'en': %s",
                       detected, e)
        return "en"
    hit_english = _wordfreq_hit_ratio(sample, "en", zipf_threshold)
    if hit_detected > hit_english:
        logger.info(
            "Language %s corroborated (real-word ratio %.3f vs en %.3f)",
            detected, hit_detected, hit_english,
        )
        return detected
    logger.warning(
        "Language detection said %s but wordfreq disagrees "
        "(%s=%.3f, en=%.3f) — keeping 'en'",
        detected, detected, hit_detected, hit_english,
    )
    return "en"


def _detect_language(conv_result, config_lang: str = None) -> str:
    """Detect the dominant language of the document text.

    Args:
        conv_result: The ConversionResult from Docling.
        config_lang: Language override from config. If set (and not "auto"),
            skip detection and return this value.

    Returns:
        ISO 639-1 language code (e.g., 'en', 'es', 'fr').
    """
    if config_lang and config_lang != "auto":
        logger.info(f"Using configured document language: {config_lang}")
        return config_lang

    try:
        from langdetect import detect_langs

        text = conv_result.document.export_to_markdown()
        # Skip first ~200 chars (titles/proper nouns confuse detection)
        # and use a larger sample for better accuracy
        sample = text[200:7000] if len(text) > 7000 else text[200:]
        if not sample.strip():
            return "en"
        results = detect_langs(sample)
        if results and results[0].prob > 0.5:
            lang = results[0].lang
            logger.info(
                f"Detected document language: {lang} "
                f"(confidence: {results[0].prob:.2f})"
            )
            return _corroborate_language(sample, lang)
        logger.warning("Language detection low confidence, defaulting to 'en'")
        return "en"
    except Exception as e:
        logger.warning(f"Language detection failed, defaulting to 'en': {e}")
        return "en"


# Uploaded filenames often carry an 8-hex-digit prefix, as Gradio and other
# upload tools add (e.g. "6afe45d6_Annual%20Report...").  Exactly 8 hex chars + _/-.
# An all-digit 8-char run is also valid hex, but a real content hash will
# essentially always contain at least one a-f letter, while an 8-digit
# YYYYMMDD date prefix (e.g. "20240426-agenda") never does -- so the
# prefix is only stripped when it contains an a-f letter, to avoid eating
# the one distinguishing token in date-prefixed filenames.
_UPLOAD_HASH_PREFIX_RE = re.compile(r"^[0-9a-f]{8}[_-]")
_HAS_HEX_LETTER_RE = re.compile(r"[a-f]")


def _title_from_filename(stem: str) -> str:
    """Clean a filename stem for use as a document title.

    Percent-decodes URL-encoded names, strips one leading hex
    prefix (only when it contains an a-f letter -- an all-digit prefix is
    treated as a date, not a hash, and kept), and normalizes ``_``/``-``
    separators to spaces.  Falls back to the raw stem if cleaning
    produces an empty string.
    """
    from urllib.parse import unquote

    decoded = unquote(stem)
    match = _UPLOAD_HASH_PREFIX_RE.match(decoded)
    if match and _HAS_HEX_LETTER_RE.search(match.group()[:8]):
        decoded = decoded[match.end():]
    cleaned = re.sub(r"[_\-]+", " ", decoded)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned if cleaned else stem


def _extract_title(conv_result, pdf_path: Path = None) -> str:
    """Extract the document title from Docling content, PDF metadata, or filename.

    Priority:
    1. TitleItem or level-1 SectionHeaderItem from Docling
    2. First substantial text element from Docling
    3. /Title from the original PDF's metadata
    4. Cleaned-up filename (without extension)

    Args:
        conv_result: The ConversionResult from Docling.
        pdf_path: Path to the original PDF (for metadata/filename fallback).

    Returns:
        The document title string.
    """
    from docling_core.types.doc import PictureItem, TableItem

    # First pass: look for explicit title or H1
    for element, _level in conv_result.document.iterate_items():
        elem_type = type(element).__name__
        if elem_type == "TitleItem" and hasattr(element, "text") and element.text:
            title = element.text.strip()
            if title:
                logger.info(f"Extracted title from TitleItem: {title[:80]}")
                return title
        if (elem_type == "SectionHeaderItem"
                and hasattr(element, "level") and element.level == 1
                and hasattr(element, "text") and element.text):
            title = element.text.strip()
            if title:
                logger.info(f"Extracted title from H1: {title[:80]}",
                            extra={"contains_content": True, "content_ref": "title"})
                return title

    # Second pass: first non-trivial text element
    for element, _level in conv_result.document.iterate_items():
        if isinstance(element, (PictureItem, TableItem)):
            continue
        if hasattr(element, "text") and element.text and len(element.text.strip()) > 5:
            title = element.text.strip()[:200]
            logger.info(f"Extracted title from first text: {title[:80]}")
            return title

    # Fallback: try PDF metadata
    if pdf_path:
        try:
            import pypdf
            with open(pdf_path, "rb") as f:
                pdf_reader = pypdf.PdfReader(f)
                info = pdf_reader.metadata
                if info and info.title and info.title.strip():
                    title = info.title.strip()
                    logger.info(f"Extracted title from PDF metadata: {title[:80]}")
                    return title
        except Exception as e:
            logger.debug(f"Could not read PDF metadata for title: {e}")

    # Final fallback: cleaned filename (percent-decoded, hash prefix stripped)
    if pdf_path:
        title = _title_from_filename(pdf_path.stem)
        logger.info(f"Using filename as title: {title[:80]}")
        return title

    return "Untitled Document"


def _detect_and_fix_type3_gibberish(
    html_path: Path, pdf_path: Path,
) -> "tuple[bool, Optional[callable]]":
    """Detect and fix garbled text from Type 3 fonts with custom encoding.

    Some LaTeX-generated PDFs use Type 3 bitmap fonts with glyph names like
    /a65, /a97, etc. (where the number is the ASCII code).  Docling's backend
    (pdfium) cannot decode these at the region level and outputs dingbat/symbol
    characters, even though pdfium's page-level text extraction is correct.

    This function:
    1. Checks whether the HTML has a high ratio of dingbat characters.
    2. If so, builds a character-level translation table by aligning correct
       pdfium page-level text against the garbled HTML text using
       difflib.SequenceMatcher on word lengths.
    3. Applies the translation to every text node in the HTML.

    Returns:
        (repaired, translate_fn) — True if gibberish was detected and fixed,
        and a callable that translates garbled text to correct text (or None).
    """
    DINGBAT_RANGES = (
        (0x2460, 0x24FF),  # Enclosed Alphanumerics  (①②③…)
        (0x2600, 0x26FF),  # Miscellaneous Symbols   (♠♥♦♣…)
        (0x2700, 0x27BF),  # Dingbats                (✐❛❜❝❞…)
        (0x25A0, 0x25FF),  # Geometric Shapes        (■▲▼●◆…)
    )

    def _is_dingbat(ch: str) -> bool:
        cp = ord(ch)
        return any(lo <= cp <= hi for lo, hi in DINGBAT_RANGES)

    def _garbled_len(word: str) -> int:
        """Logical char count, treating /aNNN as 1 char."""
        return len(re.sub(r'/a\d+', 'X', word))

    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    soup = BeautifulSoup(html_content, "html.parser")
    all_text = soup.get_text()

    # --- Step 1: detect gibberish ------------------------------------------
    text_chars = [c for c in all_text if not c.isspace()]
    if len(text_chars) < 100:
        return False, None
    dingbat_ratio = sum(1 for c in text_chars if _is_dingbat(c)) / len(text_chars)
    if dingbat_ratio < 0.10:
        return False, None

    logger.warning(
        f"Detected Type 3 font gibberish ({dingbat_ratio:.0%} dingbat chars). "
        "Attempting automatic text repair via pdfium/PyMuPDF …"
    )

    # --- Step 2: build character mapping -----------------------------------
    # Get correct text from pdfium's page-level extraction (which works fine)
    # or fall back to PyMuPDF.
    correct_text = ""
    try:
        import pypdfium2
        pdfium_doc = pypdfium2.PdfDocument(str(pdf_path))
        page_texts = []
        for i in range(len(pdfium_doc)):
            page_texts.append(pdfium_doc[i].get_textpage().get_text_range())
        correct_text = "\n".join(page_texts)
        pdfium_doc.close()
    except Exception:
        pass

    if not correct_text:
        try:
            import fitz
            with fitz.open(str(pdf_path)) as mu_doc:
                correct_text = "\n".join(mu_doc[i].get_text() for i in range(len(mu_doc)))
        except ImportError:
            logger.error("Neither pypdfium2 nor PyMuPDF available for text repair")
            return False, None

    # Tokenize both texts into words
    garbled_words = re.findall(r'\S+', all_text)
    correct_words = re.findall(r'\S+', correct_text)

    # Use difflib to align by word-length sequences.
    # SequenceMatcher finds the longest common subsequences of word lengths,
    # which reliably pairs garbled words with their correct counterparts.
    from difflib import SequenceMatcher

    garbled_lens = [_garbled_len(w) for w in garbled_words]
    correct_lens = [len(w) for w in correct_words]

    sm = SequenceMatcher(None, correct_lens, garbled_lens, autojunk=False)

    char_map: Dict[str, str] = {}
    glyph_map: Dict[str, str] = {}
    matched_pairs = 0

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            continue
        for gi, bi in zip(range(i1, i2), range(j1, j2)):
            gw = correct_words[gi]
            bw = garbled_words[bi]
            if gw == bw:
                continue  # already correct, no mapping needed

            # Align char-by-char within the matched word pair
            gpos = 0
            bpos = 0
            while gpos < len(gw) and bpos < len(bw):
                gc = gw[gpos]
                bc = bw[bpos]

                if gc == bc:
                    gpos += 1
                    bpos += 1
                    continue

                # /aNNN glyph-name literal in garbled word
                if bc == "/" and bpos + 1 < len(bw):
                    m = re.match(r"/a(\d+)", bw[bpos:])
                    if m:
                        glyph_map[m.group(0)] = gc
                        bpos += len(m.group(0))
                        gpos += 1
                        continue

                # Dingbat → correct character
                if _is_dingbat(bc):
                    char_map[bc] = gc
                    gpos += 1
                    bpos += 1
                    continue

                # Non-dingbat mismatch — stop this word
                break

            matched_pairs += 1

    mapped_count = len(char_map) + len(glyph_map)
    if mapped_count < 5:
        logger.warning(
            f"Only built {mapped_count} mappings from {matched_pairs} word pairs — "
            "too few for reliable repair. Skipping Type 3 fix."
        )
        return False, None

    # --- Step 2b: fill gaps via character-level scan -------------------------
    # The word-level alignment can miss characters that only appear in contexts
    # where word lengths diverge (ligatures, standalone symbols).  Do a second
    # pass: scan the garbled text and correct text character-by-character,
    # only recording mappings for dingbat chars that are still unmapped.
    gi = bi = 0
    while gi < len(correct_text) and bi < len(all_text):
        gc, bc = correct_text[gi], all_text[bi]
        if gc == bc:
            gi += 1; bi += 1; continue
        if gc in " \n\r\t" and bc in " \n\r\t":
            gi += 1; bi += 1; continue
        if gc in " \n\r\t":
            gi += 1; continue
        if bc in " \n\r\t":
            bi += 1; continue
        if bc == "/" and bi + 1 < len(all_text) and all_text[bi + 1] == "a":
            m = re.match(r"/a(\d+)", all_text[bi:])
            if m:
                if m.group(0) not in glyph_map:
                    glyph_map[m.group(0)] = gc
                bi += len(m.group(0)); gi += 1; continue
        if _is_dingbat(bc) and bc not in char_map:
            char_map[bc] = gc
        gi += 1; bi += 1

    mapped_count = len(char_map) + len(glyph_map)

    logger.info(
        f"Built Type 3 translation table: {len(char_map)} char + "
        f"{len(glyph_map)} glyph-name mappings (from {matched_pairs} word pairs)"
    )

    # --- Step 3: apply the mapping to all text in the HTML -----------------
    glyph_patterns = sorted(glyph_map.keys(), key=len, reverse=True)
    glyph_re = (
        re.compile("|".join(re.escape(g) for g in glyph_patterns))
        if glyph_patterns else None
    )

    def _translate(text: str) -> str:
        if glyph_re:
            text = glyph_re.sub(
                lambda m: glyph_map.get(m.group(0), m.group(0)), text
            )
        return "".join(char_map.get(c, c) for c in text)

    from bs4 import NavigableString

    changed = 0
    for text_node in soup.find_all(string=True):
        if isinstance(text_node, NavigableString):
            original = str(text_node)
            fixed = _translate(original)
            if fixed != original:
                text_node.replace_with(NavigableString(fixed))
                changed += 1

    if changed == 0:
        return False, None

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(str(soup))

    logger.info(f"Repaired Type 3 gibberish in {changed} text nodes")
    return True, _translate


def _postprocess_html(
    html_path: Path,
    image_files: List[str],
    language: str,
) -> None:
    """Post-process Docling's HTML output for accessibility.

    Fixes:
    - Ensures <html lang="..."> is set
    - Rewrites image src paths to point to our images/ directory
    - Adds empty alt="" to images missing alt text (audit will flag these)
    - Maps Docling's auto-generated artifact images to our extracted images

    Note: Image embedding (base64 data URIs) is handled separately by
    ``embed_images_as_base64()`` and must run AFTER VLM alt-text generation
    to avoid breaking downstream systems that use ``src`` as an identifier.
    """
    with open(html_path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    # Fix <html> lang attribute
    html_tag = soup.find("html")
    if html_tag:
        html_tag["lang"] = language

    # Fix image references
    img_tags = soup.find_all("img")
    for i, img in enumerate(img_tags):
        # Add alt="" if missing (audit phase will flag for VLM remediation)
        if not img.get("alt"):
            img["alt"] = ""

        # Rewrite src to point to our images/ directory
        if i < len(image_files):
            img_path = Path(image_files[i])
            # Use relative path from extracted_html/ to images/
            img["src"] = f"../images/{img_path.name}"

    # Mark formula-not-decoded divs with accessible math attributes
    formula_divs = soup.find_all("div", class_="formula-not-decoded")
    for i, div in enumerate(formula_divs):
        div["role"] = "math"
        div["data-formula-index"] = str(i)
        div["aria-label"] = ""
        div.clear()  # remove "Formula not decoded" text
    if formula_divs:
        logger.info("Marked %d formula placeholder(s) for VLM enrichment", len(formula_divs))

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(str(soup))

    # Clean up Docling's auto-generated artifact directory
    artifacts_dir = html_path.parent / f"{html_path.stem}_artifacts"
    if artifacts_dir.exists():
        import shutil
        shutil.rmtree(artifacts_dir)
        logger.debug(f"Cleaned up Docling artifacts: {artifacts_dir}")

    # Also clean up any nested artifact directories Docling may have created
    for nested in html_path.parent.rglob("*_artifacts"):
        if nested.is_dir():
            import shutil
            shutil.rmtree(nested)
            logger.debug(f"Cleaned up nested artifacts: {nested}")
            break


def embed_images_as_base64(html_path: str) -> None:
    """Replace ``<img src>`` file paths with inline base64 data URIs.

    Makes the HTML self-contained (no external image files needed).
    Must be called AFTER VLM alt-text generation and remediation, because
    those phases use ``src`` as a short, stable identifier for each image.
    """
    import base64
    import mimetypes

    hp = Path(html_path)
    soup = BeautifulSoup(hp.read_text(encoding="utf-8"), "html.parser")
    embedded = 0

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src or src.startswith("data:"):
            continue  # already embedded or empty
        # Resolve relative path from the HTML file's directory
        img_path = (hp.parent / src).resolve()
        if not img_path.exists():
            continue
        mime_type = mimetypes.guess_type(str(img_path))[0] or "image/png"
        b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
        img["src"] = f"data:{mime_type};base64,{b64}"
        embedded += 1

    if embedded:
        hp.write_text(str(soup), encoding="utf-8")
        logger.info("Embedded %d images as base64 data URIs", embedded)


def _enhance_table_html(html_path: Path, doc) -> None:
    """Apply <th>, colspan, and rowspan to HTML tables using Docling's cell data.

    Docling's save_as_html() emits <td> for every cell regardless of whether
    Docling flagged it as a column or row header, and omits colspan/rowspan
    attributes entirely.  This function corrects both gaps so that downstream
    remediation and the iText tagger see proper table semantics.
    """
    table_items = [
        item
        for item, _ in doc.iterate_items()
        if isinstance(item, TableItem)
        and hasattr(item, "data")
        and hasattr(item.data, "table_cells")
    ]
    if not table_items:
        return

    with open(html_path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    html_tables = soup.find_all("table")
    modified = False

    # Classify tables as TOC/LoF/LoT/Index/data before enhancing.
    # Any classification marks the soup as modified so attributes persist.
    classifications = classify_tables_in_soup(soup)
    if any(v != TYPE_DATA for v in classifications.values()):
        modified = True

    for html_table, table_item in zip(html_tables, table_items):
        # Skip TOC/LoF/LoT/Index tables — they aren't data tables and
        # should not get <th>/scope treatment
        table_type = html_table.get("data-table-type", TYPE_DATA)
        if table_type != TYPE_DATA:
            logger.debug(
                "Skipping table enhancement for %s table", table_type
            )
            continue

        cells = list(table_item.data.table_cells)
        if not cells:
            continue

        # (row, col) → Docling cell
        cell_map = {
            (c.start_row_offset_idx, c.start_col_offset_idx): c
            for c in cells
        }

        # Track grid positions occupied by rowspan from prior rows so we can
        # advance col_idx past them when iterating the HTML cells.
        occupied: set = set()

        for row_idx, tr in enumerate(html_table.find_all("tr")):
            col_idx = 0
            for td in tr.find_all(["td", "th"]):
                # Skip positions already claimed by a rowspan above
                while (row_idx, col_idx) in occupied:
                    col_idx += 1

                dcell = cell_map.get((row_idx, col_idx))
                if dcell is None:
                    col_idx += 1
                    continue

                col_span = max(1, dcell.col_span or 1)
                row_span = max(1, dcell.row_span or 1)

                # Clip colspan if it would overlap the next cell in this row.
                # Docling sometimes misdetects colspan extents, causing two
                # adjacent cells to claim the same grid position.
                if col_span > 1:
                    next_dcell_col = None
                    for probe in range(col_idx + 1, col_idx + col_span + 1):
                        if (row_idx, probe) in cell_map:
                            next_dcell_col = probe
                            break
                    if next_dcell_col is not None and col_idx + col_span > next_dcell_col:
                        col_span = max(1, next_dcell_col - col_idx)
                        logger.warning(
                            "Clipped colspan at row %d col %d to %d (overlap with col %d)",
                            row_idx, col_idx, col_span, next_dcell_col,
                        )

                if col_span > 1:
                    td["colspan"] = str(col_span)
                    modified = True
                if row_span > 1:
                    td["rowspan"] = str(row_span)
                    modified = True
                    # Reserve future-row positions covered by this span
                    for dr in range(1, row_span):
                        for dc in range(col_span):
                            occupied.add((row_idx + dr, col_idx + dc))

                if (dcell.column_header or dcell.row_header) and td.name == "td":
                    td.name = "th"
                    td["scope"] = "col" if dcell.column_header else "row"
                    modified = True

                col_idx += col_span

    if modified:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(str(soup))
        logger.debug("Enhanced table HTML with th/colspan/rowspan: %s", html_path.name)


def _is_columnar_text_table(cells: List[Dict]) -> bool:
    """Detect tables that are actually space-separated columnar text.

    Docling sometimes misidentifies plain columnar numeric data (no grid
    lines) as a table, merging multiple columns into the first cell.
    Heuristic: no header cells, many rows (>10), and most first-column
    cells contain multiple space-separated numeric tokens.
    """
    if not cells or len(cells) < 20:
        return False

    # Must have no header cells at all
    if any(c.get("is_header") or c.get("is_col_header") or c.get("is_row_header")
           for c in cells):
        return False

    num_rows = len(set(c["row"] for c in cells))
    if num_rows < 10:
        return False

    col0_cells = [c for c in cells if c["col"] == 0]
    if not col0_cells:
        return False

    _NUM_RE = re.compile(r'^-?\.?\d[\d.]*(?:[Ee]-?\d+)?$')
    multi_numeric = 0
    for c in col0_cells:
        tokens = c.get("text", "").strip().split()
        if len(tokens) > 1 and all(_NUM_RE.match(t) for t in tokens):
            multi_numeric += 1

    # If >70% of first-column cells have merged numeric tokens, it's columnar
    return multi_numeric / len(col0_cells) > 0.7


def _fix_docling_table_column_order(doc) -> int:
    """Fix tables where Docling assigned wrong col indices to body cells.

    Mutates table_cells in-place before HTML export. Only activates under
    strict conditions to avoid false positives:
    - Header row has >=2 cells, all with bboxes, spatial order matches col indices
    - No spans > 1 anywhere in the table
    - Every body row has exactly N cells (same as headers), all with bboxes
    - Every body row's spatial order differs from assigned col indices

    Returns count of tables fixed.
    """
    from collections import defaultdict

    fixed = 0
    for item, _ in doc.iterate_items():
        if not isinstance(item, TableItem):
            continue
        if not hasattr(item, "data") or not hasattr(item.data, "table_cells"):
            continue
        cells = list(item.data.table_cells)
        if len(cells) < 4:
            continue

        # Group by row
        rows_map = defaultdict(list)
        for c in cells:
            rows_map[c.start_row_offset_idx].append(c)

        header_cells = rows_map.get(0, [])
        num_cols = len(header_cells)
        if num_cols < 2:
            continue

        # Criterion 1+2: All headers have bboxes, spatial order matches col indices
        if not all(c.bbox for c in header_cells):
            continue
        headers_by_x = sorted(header_cells, key=lambda c: c.bbox.l)
        if [c.start_col_offset_idx for c in headers_by_x] != list(range(num_cols)):
            continue

        # Criterion 3: No spans > 1
        if any((c.col_span or 1) > 1 or (c.row_span or 1) > 1 for c in cells):
            continue

        # Criterion 4+5: Every body row has N cells with bboxes, spatial != assigned
        body_rows = {k: v for k, v in rows_map.items() if k > 0}
        if not body_rows:
            continue

        needs_fix = True
        for row_cells in body_rows.values():
            if len(row_cells) != num_cols or not all(c.bbox for c in row_cells):
                needs_fix = False
                break
            by_x = sorted(row_cells, key=lambda c: c.bbox.l)
            cols_spatial = [c.start_col_offset_idx for c in by_x]
            if cols_spatial == list(range(num_cols)):
                needs_fix = False  # Already correct
                break
            if len(set(cols_spatial)) != num_cols:
                needs_fix = False  # Not a clean permutation
                break

        if not needs_fix:
            continue

        # Fix: reassign col indices by spatial order
        for row_cells in body_rows.values():
            by_x = sorted(row_cells, key=lambda c: c.bbox.l)
            for i, c in enumerate(by_x):
                c.start_col_offset_idx = i
                c.end_col_offset_idx = i + 1

        fixed += 1
        header_text = " | ".join(
            c.text for c in headers_by_x if hasattr(c, "text") and c.text
        )
        logger.info(
            "Fixed Docling table column order: %d cols, %d body rows [%s]",
            num_cols, len(body_rows), header_text[:80],
        )

    return fixed


def _normalize_table_grid(cells: List[Dict]) -> List[Dict]:
    """Fill gaps in a table cell grid so every row has complete column coverage.

    Docling sometimes misses cells (e.g. empty corners, cells with only
    symbols).  If any (row, col) position is not covered by a cell or a
    rowspan/colspan from another cell, insert an empty placeholder.  This
    prevents the iText tagger from creating irregular TR elements that PAC
    flags as accessibility failures.
    """
    if not cells:
        return cells

    # Clip overlapping colspans within the same row before building the
    # coverage map.  Docling sometimes gives a cell col_span that extends
    # into the next cell's start position (e.g. col=6 cs=2, col=7 cs=1).
    from collections import defaultdict as _dtc
    by_row: dict = _dtc(list)
    for c in cells:
        by_row[c["row"]].append(c)
    for r, row_cells in by_row.items():
        row_cells.sort(key=lambda x: x["col"])
        for i in range(len(row_cells) - 1):
            curr = row_cells[i]
            nxt = row_cells[i + 1]
            curr_cs = curr.get("col_span", 1) or 1
            if curr["col"] + curr_cs > nxt["col"]:
                curr["col_span"] = max(1, nxt["col"] - curr["col"])

    # Build coverage map: which (row, col) positions are occupied
    covered: dict = {}  # (row, col) -> True
    max_row = 0
    max_col = 0
    for c in cells:
        r, col = c["row"], c["col"]
        rs = c.get("row_span", 1) or 1
        cs = c.get("col_span", 1) or 1
        for dr in range(rs):
            for dc in range(cs):
                covered[(r + dr, col + dc)] = True
        max_row = max(max_row, r + rs - 1)
        max_col = max(max_col, col + cs - 1)

    # Find gaps and insert empty cells
    added = 0
    for r in range(max_row + 1):
        for col in range(max_col + 1):
            if (r, col) not in covered:
                cells.append({
                    "row": r,
                    "col": col,
                    "row_span": 1,
                    "col_span": 1,
                    "text": "",
                    "is_header": False,
                    "is_col_header": False,
                    "is_row_header": False,
                })
                added += 1

    if added:
        logger.debug("Table grid normalization: added %d empty cells", added)

    return cells


def _split_text_into_spans(text: str) -> List[Dict[str, str]]:
    """Split paragraph text into sentence-level Span children.

    This increases tag granularity — one Span per sentence instead of
    one P per paragraph. Also detects inline URLs and wraps them as
    Link children.

    Returns a list of dicts: [{"tag": "Span"|"Link", "text": "..."}]
    """
    if not text or len(text.strip()) < 10:
        return []

    # URL pattern for link detection
    url_pattern = re.compile(
        r'(https?://[^\s<>"\')\]]+|www\.[^\s<>"\')\]]+)'
    )

    # Split into sentences (simple heuristic: ., !, ? followed by space + capital)
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', text.strip())

    if len(sentences) <= 1:
        # Not enough sentences to split — check for URLs only
        urls = url_pattern.findall(text)
        if not urls:
            return []
        # Split around URLs
        return _split_text_with_links(text, url_pattern)

    children = []
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        # Check if this sentence contains URLs
        urls = url_pattern.findall(sentence)
        if urls:
            children.extend(_split_text_with_links(sentence, url_pattern))
        else:
            children.append({"tag": "Span", "text": sentence})

    return children if len(children) > 1 else []


def _split_text_with_links(text: str, url_pattern) -> List[Dict[str, str]]:
    """Split text into Span and Link children around URL patterns."""
    parts = []
    last_end = 0
    for match in url_pattern.finditer(text):
        # Text before the URL
        before = text[last_end:match.start()].strip()
        if before:
            parts.append({"tag": "Span", "text": before})
        # The URL itself
        parts.append({"tag": "Link", "text": match.group(0)})
        last_end = match.end()
    # Text after the last URL
    after = text[last_end:].strip()
    if after:
        parts.append({"tag": "Span", "text": after})
    return parts


def _render_element_image(
    pdf_path: str,
    page_no: int,
    bbox: Dict[str, float],
    page_height: float,
    output_path: str,
    padding: int = 10,
    dpi: int = 200,
) -> Optional[str]:
    """Render an element region from the PDF as a PNG image using PyMuPDF.

    Args:
        pdf_path: Path to original PDF.
        page_no: 1-based page number.
        bbox: Dict with l, b, r, t (bottom-left origin, PDF coords).
        page_height: Page height in PDF points.
        output_path: Where to save the rendered PNG.
        padding: Extra points around the bbox.
        dpi: Render resolution.

    Returns:
        Path to rendered image, or None on failure.
    """
    try:
        import fitz

        with fitz.open(pdf_path) as doc:
            page_idx = page_no - 1
            if page_idx < 0 or page_idx >= len(doc):
                return None

            page = doc[page_idx]

            # Convert from bottom-left origin (PDF) to top-left origin (fitz)
            fitz_rect = fitz.Rect(
                bbox["l"] - padding,
                page_height - bbox["t"] - padding,
                bbox["r"] + padding,
                page_height - bbox["b"] + padding,
            )
            fitz_rect = fitz_rect & page.rect

            zoom = safe_dpi(fitz_rect.width, fitz_rect.height, dpi) / 72.0
            mat = fitz.Matrix(zoom, zoom)
            pixmap = page.get_pixmap(matrix=mat, clip=fitz_rect)
            pixmap.save(output_path)
            return output_path

    except Exception as e:
        logger.error("Failed to render element image: %s", e)
        return None


def _export_structure_json(
    conv_result,
    output_path: Path,
    image_files: List[str],
    language: str,
    title: str = None,
    table_classifications: Optional[Dict[int, str]] = None,
    text_translate_fn: Optional[Any] = None,
    pdf_path: Optional[Path] = None,
    vlm_render_dpi: int = 200,
    detect_heading: bool = True,
    promote_first_text: bool = True,
) -> int:
    """Export Docling's structural analysis as JSON with bounding boxes.

    This JSON drives the PDF tagger — it contains the semantic structure,
    reading order, and spatial coordinates needed to inject a PDF/UA
    structure tree into the original PDF.

    Args:
        conv_result: The ConversionResult from Docling.
        output_path: Where to write the JSON file.
        image_files: List of extracted image paths (to link figures).
        language: Detected document language code.

    Returns:
        Number of exported elements (the JSON is written to *output_path*).
    """
    doc = conv_result.document

    # Build page dimension lookup
    pages_info = {}
    for page_no, page_item in doc.pages.items():
        pages_info[page_no] = {
            "width": page_item.size.width,
            "height": page_item.size.height,
        }

    elements = []
    reading_order = 0
    picture_counter = 0
    table_counter = 0
    formula_counter = 0
    # Track consecutive ListItem elements to group them under an L tag
    pending_list_items = []
    current_list_page = None

    def _flush_list_items():
        """Wrap pending ListItem entries in a single L (List) container."""
        nonlocal reading_order
        if not pending_list_items:
            return
        # Create the L (list) wrapper — union bbox of same-page items only.
        # Cross-page children retain their own page_no/bbox for matching.
        first = pending_list_items[0]
        target_page = first.get("page_no")
        union_bbox = dict(first["bbox"]) if first["bbox"] else {}
        for li in pending_list_items[1:]:
            if li.get("bbox") and li.get("page_no") == target_page:
                b = li["bbox"]
                union_bbox["l"] = min(union_bbox.get("l", b["l"]), b["l"])
                union_bbox["b"] = min(union_bbox.get("b", b["b"]), b["b"])
                union_bbox["r"] = max(union_bbox.get("r", b["r"]), b["r"])
                union_bbox["t"] = max(union_bbox.get("t", b["t"]), b["t"])
        list_entry = {
            "id": f"elem_{reading_order:04d}",
            "type": "ListGroup",
            "tag": "L",
            "level": None,
            "text": "",
            "page_no": first["page_no"],
            "bbox": union_bbox,
            "reading_order": reading_order,
            "alt_text": None,
            "image_ref": None,
            "children": [],
        }
        reading_order += 1

        for li_entry in pending_list_items:
            # Each LI contains an LBody child — preserve bbox for matching
            text = li_entry["text"]
            li_child = {
                "tag": "LI",
                "text": text,
                "bbox": li_entry.get("bbox"),
                "page_no": li_entry.get("page_no"),  # per-child page for cross-page lists
                "children": [
                    {"tag": "LBody", "text": text},
                ],
            }
            # Try to extract a label (bullet/number) from the text
            label_match = re.match(
                r'^(\d+[.)]\s*|[-•●◦▪]\s*)', text)
            if label_match:
                li_child["children"].insert(
                    0, {"tag": "Lbl", "text": label_match.group(0).strip()})
                li_child["children"][1]["text"] = text[label_match.end():]
            list_entry["children"].append(li_child)

        elements.append(list_entry)
        pending_list_items.clear()

    for element, level in doc.iterate_items():
        elem_type = type(element).__name__

        # Skip group-level items that don't map to content
        if elem_type in ("GroupItem", "ListGroup", "InlineGroup"):
            continue

        # Get provenance (bbox + page)
        if not hasattr(element, "prov") or not element.prov:
            continue

        prov = element.prov[0]
        page_no = prov.page_no
        page_height = pages_info.get(page_no, {}).get("height", 792.0)

        # Convert bbox to bottom-left origin (PDF convention)
        bbox = prov.bbox
        try:
            bl_bbox = bbox.to_bottom_left_origin(page_height)
            bbox_dict = {
                "l": round(bl_bbox.l, 2),
                "b": round(bl_bbox.b, 2),
                "r": round(bl_bbox.r, 2),
                "t": round(bl_bbox.t, 2),
            }
        except Exception as e:
            logger.error(
                "bbox conversion failed for element on page %d: %s — skipping element",
                page_no, e,
            )
            continue

        # Determine heading level
        heading_level = None
        if hasattr(element, "level"):
            heading_level = element.level

        # Map to PDF tag
        tag = map_element_to_tag(elem_type, heading_level)

        # Extract text content
        text = ""
        if hasattr(element, "text"):
            text = element.text or ""
        elif hasattr(element, "export_to_markdown"):
            try:
                text = element.export_to_markdown()
            except Exception:
                pass

        # Apply Type 3 font translation if available
        if text_translate_fn and text:
            text = text_translate_fn(text)

        # Flush pending list items if we hit a non-list element or new page
        if elem_type != "ListItem" and pending_list_items:
            _flush_list_items()

        entry = {
            "id": f"elem_{reading_order:04d}",
            "type": elem_type,
            "tag": tag,
            "level": heading_level,
            "text": text,
            "page_no": page_no,
            "bbox": bbox_dict,
            "reading_order": reading_order,
            "alt_text": None,
            "image_ref": None,
        }

        # Handle list items — accumulate for L grouping
        if elem_type == "ListItem":
            pending_list_items.append(entry)
            reading_order += 1
            continue

        # Handle pictures — link to extracted image files
        if isinstance(element, PictureItem):
            picture_counter += 1
            if picture_counter <= len(image_files):
                entry["image_ref"] = image_files[picture_counter - 1]

        # Handle formulas — render formula region as image for VLM captioning
        if FormulaItem is not None and isinstance(element, FormulaItem) and pdf_path:
            formula_counter += 1
            formula_img_dir = output_path.parent / "images" / "formula_renders"
            formula_img_dir.mkdir(parents=True, exist_ok=True)
            formula_img_path = formula_img_dir / f"formula_{formula_counter}.png"
            rendered = _render_element_image(
                pdf_path=str(pdf_path),
                page_no=page_no,
                bbox=bbox_dict,
                page_height=page_height,
                output_path=str(formula_img_path),
                dpi=vlm_render_dpi,
            )
            if rendered:
                entry["image_ref"] = str(formula_img_path)

        # Handle tables — extract cell structure
        if isinstance(element, TableItem):
            # Check if this table was classified as TOC/LoF/LoT/Index
            table_type = (table_classifications or {}).get(
                table_counter, TYPE_DATA
            )
            table_counter += 1

            if table_type != TYPE_DATA:
                # Emit as TOC with TOCI children instead of Table with cells
                entry["tag"] = "TOC"
                entry["table_type"] = table_type
                toci_children = []
                if hasattr(element, "data") and hasattr(element.data, "table_cells"):
                    # Group cells by row and concatenate text per row
                    from collections import defaultdict
                    rows_map = defaultdict(list)
                    for cell in element.data.table_cells:
                        rows_map[cell.start_row_offset_idx].append(cell)
                    for row_idx in sorted(rows_map.keys()):
                        row_cells = sorted(
                            rows_map[row_idx],
                            key=lambda c: c.start_col_offset_idx,
                        )
                        row_text = " ".join(
                            c.text.strip() for c in row_cells if c.text
                        ).strip()
                        if text_translate_fn and row_text:
                            row_text = text_translate_fn(row_text)
                        if row_text:
                            toci_entry = {
                                "tag": "TOCI",
                                "text": row_text,
                            }
                            # Compute row bbox as union of cell bboxes
                            row_bboxes = []
                            for c in row_cells:
                                if c.bbox:
                                    try:
                                        bl = c.bbox.to_bottom_left_origin(
                                            page_height
                                        )
                                        row_bboxes.append(bl)
                                    except Exception:
                                        pass
                            if row_bboxes:
                                toci_entry["bbox"] = {
                                    "l": round(
                                        min(b.l for b in row_bboxes), 2
                                    ),
                                    "b": round(
                                        min(b.b for b in row_bboxes), 2
                                    ),
                                    "r": round(
                                        max(b.r for b in row_bboxes), 2
                                    ),
                                    "t": round(
                                        max(b.t for b in row_bboxes), 2
                                    ),
                                }
                            toci_children.append(toci_entry)
                # Multi-column TOC fallback: Docling's cell-row concatenation
                # scrambles multi-column TOCs because cell grid detection is
                # unreliable for tightly-spaced column layouts.
                if toci_children and pdf_path:
                    try:
                        from utils.toc_text_extractor import (
                            is_multicolumn_toc,
                            extract_toc_from_pymupdf,
                        )
                        if is_multicolumn_toc(
                            pdf_path, page_no, bbox_dict, page_height
                        ):
                            logger.info(
                                "TOC on page %d is multi-column, "
                                "re-extracting via PyMuPDF",
                                page_no,
                            )
                            pymupdf_children = extract_toc_from_pymupdf(
                                pdf_path,
                                page_no,
                                bbox_dict,
                                page_height,
                                text_translate_fn=text_translate_fn,
                            )
                            if pymupdf_children:
                                toci_children = pymupdf_children
                    except Exception as e:
                        logger.debug(
                            "TOC fallback skipped on page %d: %s",
                            page_no,
                            e,
                        )

                if toci_children:
                    entry["children"] = toci_children
            else:
                # Regular data table — emit cells with header type distinction
                cells = []
                if hasattr(element, "data") and hasattr(element.data, "table_cells"):
                    for cell in element.data.table_cells:
                        _cell_text = cell.text if cell.text else ""
                        if text_translate_fn and _cell_text:
                            _cell_text = text_translate_fn(_cell_text)
                        cell_entry = {
                            "row": cell.start_row_offset_idx,
                            "col": cell.start_col_offset_idx,
                            "row_span": cell.row_span,
                            "col_span": cell.col_span,
                            "text": _cell_text,
                            "is_header": cell.column_header or cell.row_header,
                            "is_col_header": bool(cell.column_header),
                            "is_row_header": bool(cell.row_header),
                        }
                        if cell.bbox:
                            try:
                                cell_bl = cell.bbox.to_bottom_left_origin(page_height)
                                cell_entry["bbox"] = {
                                    "l": round(cell_bl.l, 2),
                                    "b": round(cell_bl.b, 2),
                                    "r": round(cell_bl.r, 2),
                                    "t": round(cell_bl.t, 2),
                                }
                            except Exception:
                                pass
                        cells.append(cell_entry)
                cells = _normalize_table_grid(cells)

                # Cap excessive header rows: Docling sometimes marks
                # most rows as headers.  If >30% of rows (or >3 rows)
                # are flagged, keep only row 0 as the header row.
                if cells:
                    total_rows = max(c["row"] for c in cells) + 1
                    header_rows = {
                        c["row"] for c in cells
                        if c.get("is_header") or c.get("is_col_header")
                    }
                    if len(header_rows) > max(3, int(total_rows * 0.3)):
                        logger.debug(
                            "Capping header rows on page %s: %d/%d rows "
                            "marked as headers, keeping only row 0",
                            page_no, len(header_rows), total_rows,
                        )
                        for c in cells:
                            if c["row"] > 0:
                                c["is_header"] = False
                                c["is_col_header"] = False

                # Detect columnar text misidentified as table
                if _is_columnar_text_table(cells):
                    logger.info(
                        "Reclassifying table on page %s as Figure "
                        "(columnar text, not a bordered table)", page_no
                    )
                    entry["tag"] = "Figure"
                    entry["type"] = "PictureItem"
                    entry["alt_text"] = (
                        "Numerical data table: " + text[:400]
                        if text else "Numerical data"
                    )
                    # Don't emit cells — Figure elements don't have them
                else:
                    entry["cells"] = cells

        # Split paragraph text into sentence-level Span children
        if tag == "P" and text:
            children = _split_text_into_spans(text)
            if children:
                entry["children"] = children

        elements.append(entry)
        reading_order += 1

    # Flush any remaining list items at end of document
    _flush_list_items()

    # Some documents are typeset in a single font size, so Docling's size-delta
    # heuristic finds no heading at all and the tagged PDF carries none. Promote
    # one existing element when a conservative signal says it is the heading.
    # Runs before the level pass so a promoted H1 goes through it normally.
    heading_detection = None
    try:
        heading_detection = promote_document_heading(
            elements,
            title=title or "",
            enabled=detect_heading,
            promote_first_text=promote_first_text,
        )
    except Exception as e:
        logger.warning("Heading detection failed: %s", e)

    # Infer heading levels from source-PDF font sizes + numbering. Docling emits
    # every heading as level=1; without this pass every heading would be tagged
    # H1 in the output, breaking WCAG 2.4.6 / PDF/UA heading hierarchy.
    if pdf_path is not None:
        try:
            updated = infer_heading_levels(elements, str(pdf_path))
            if updated:
                logger.info("Heading inference: re-leveled %d heading(s)", updated)
        except Exception as e:
            logger.warning("Heading-level inference failed: %s", e)

    structure = {
        "metadata": {
            # The original input PDF filename. Was derived from the output dir
            # structure (output_path.parent.parent.name), which yields the job/
            # work-dir name (a batch directory name) and breaks
            # filename-derived title remediation. pdf_path is the original input
            # (the font-fix prepass writes a separate convert_path).
            "source_pdf": pdf_path.name,
            "num_pages": len(pages_info),
            "language": language,
            "title": title,
            # Audit trail: which element (if any) was promoted to H1, and why.
            # Saved with the artifact so a corpus run stays greppable later.
            "heading_detection": heading_detection,
        },
        "pages": {str(k): v for k, v in pages_info.items()},
        "elements": elements,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(structure, f, indent=2, ensure_ascii=False)

    logger.info(
        f"Exported structure JSON: {len(elements)} elements across "
        f"{len(pages_info)} pages -> {output_path}"
    )
    return len(elements)


def convert_pdf_to_html(
    pdf_path: str,
    output_dir: str,
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Convert a PDF to accessible HTML using Docling.

    This is the main entry point for Phase 1. It produces semantic HTML
    with proper heading hierarchy, table structure, figure placeholders,
    and reading order — all determined locally by Docling's AI models.

    Args:
        pdf_path: Path to the input PDF file.
        output_dir: Directory to write output files.
        options: Conversion options from config. Supports:
            - extract_images (bool): Extract figure images. Default True.
            - image_format (str): Image format. Default 'png'.
            - image_scale (float): Resolution scale. Default 2.0.
            - multi_page (bool): One HTML file per page. Default True.

    Returns:
        Dictionary containing:
            - html_path: Path to the main/index HTML file.
            - html_files: List of all generated HTML files.
            - image_files: List of extracted image file paths.
            - image_dir: Path to the image directory.
            - language: Detected document language code.
            - stats: Conversion statistics (pages, figures, tables, time).

    Raises:
        FileNotFoundError: If the PDF file doesn't exist.
        RuntimeError: If conversion fails.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    opts = options or {}
    extract_images = opts.get("extract_images", True)

    # Count pages up front so we can show meaningful progress
    try:
        import pypdf
        with open(pdf_path, "rb") as f:
            page_count = len(pypdf.PdfReader(f).pages)
        logger.info(f"Converting PDF: {pdf_path.name} ({page_count} pages)")
    except Exception:
        page_count = None
        logger.info(f"Converting PDF: {pdf_path.name}")

    if page_count and page_count > 5:
        device_hint = opts.get("device", "auto")
        logger.info(
            f"  Docling layout analysis (device={device_hint}, {page_count} pages). "
            "Please wait..."
        )

    start_time = time.time()

    # Resolve OCR setting — default is True. Docling's per-page gating
    # (bitmap_area_threshold) makes pages WITH native text skip OCR, but the
    # converse does not hold: a page with no text AND no bitmaps gets no OCR
    # either — that gap is closed by force_full_page_ocr below.
    do_ocr_cfg = opts.get("do_ocr", True)
    if do_ocr_cfg == "auto" or do_ocr_cfg is None:
        do_ocr = True  # "auto" now maps to always-on
    else:
        do_ocr = bool(do_ocr_cfg)

    # Classification gates OCR forcing: a fully_scanned document (e.g. a
    # vectorized/ClearScan book with no text and no bitmaps) is invisible to
    # Docling's own OCR triggers, so it must be forced explicitly.
    text_classification = _classify_pdf_text(pdf_path)
    force_full_page_ocr = _should_force_full_page_ocr(
        text_classification, opts, do_ocr)
    if force_full_page_ocr:
        logger.info(
            "Document is fully_scanned — forcing full-page OCR so Docling "
            "extracts text and structure (vector scans carry no bitmaps for "
            "the coverage gate to detect)")

    # Font-fix pre-pass: when the source's text extraction looks corrupted
    # (control / PUA chars from Type-3 LaTeX fonts that lack /ToUnicode), inject
    # correct ToUnicode CMaps via the iText tagger BEFORE Docling reads the PDF.
    # Docling sources the screen-reader /ActualText, so this turns garbled
    # ligatures ("deWne") into real words ("define"). Only the text layer
    # changes — layout, bounding boxes, and rendered images are identical, so
    # downstream structure/matching (which still use the original PDF) is
    # unaffected.
    convert_path = pdf_path
    if opts.get("fix_fonts_prepass", True):
        threshold = float(opts.get("fix_fonts_prepass_threshold", 0.003))
        corruption = _text_corruption_ratio(pdf_path)
        if corruption > threshold:
            logger.info(
                "Text corruption ratio %.2f%% > %.2f%% — running font-fix "
                "pre-pass before Docling", corruption * 100, threshold * 100,
            )
            fixed = _fix_fonts_prepass(pdf_path, output_dir)
            if fixed is not None:
                convert_path = fixed

    # Build and run the converter
    converter = _build_converter(
        options, do_ocr=do_ocr, force_full_page_ocr=force_full_page_ocr)
    conv_result = converter.convert(str(convert_path))

    doc = conv_result.document
    doc_stem = pdf_path.stem

    # Fix Docling tables with scrambled body cell column indices (before HTML export)
    _fix_docling_table_column_order(doc)

    # Detect language (with optional config override)
    config_lang = opts.get("language")
    language = _detect_language(conv_result, config_lang=config_lang)

    # Extract document title
    title = _extract_title(conv_result, pdf_path=pdf_path)

    # Extract images if requested
    image_dir = output_dir / "images"
    image_files = []
    if extract_images:
        image_files = _save_images(conv_result, image_dir)

    # Determine image mode for HTML export
    if extract_images and image_files:
        image_mode = ImageRefMode.REFERENCED
    else:
        image_mode = ImageRefMode.PLACEHOLDER

    # Export HTML
    html_files = []
    extracted_html_dir = output_dir / "extracted_html"
    extracted_html_dir.mkdir(parents=True, exist_ok=True)

    # Export as a single HTML document
    html_filename = f"{doc_stem}.html"
    html_path = extracted_html_dir / html_filename

    doc.save_as_html(
        filename=html_path,
        image_mode=image_mode,
        html_lang=language,
    )
    html_files.append(str(html_path))

    # Post-process: fix image paths, add alt attrs, set lang, clean artifacts
    _postprocess_html(html_path, image_files, language)

    # Detect and fix Type 3 font gibberish (LaTeX PDFs with custom-encoded
    # bitmap fonts that pdfium cannot decode).
    type3_repaired, _type3_translate = _detect_and_fix_type3_gibberish(html_path, pdf_path)

    # Enhance table HTML: apply <th>, colspan, and rowspan from Docling cell data.
    # Docling's save_as_html() emits <td> for all cells and omits span attributes.
    # This also classifies tables as TOC/LoF/LoT/Index/data via toc_detector.
    _enhance_table_html(html_path, doc)

    # Read back the classified table types for the structure JSON export.
    # This parse already existed; stamping rides along on it so the ids cost
    # only a write, not a second parse.
    with open(html_path, "r", encoding="utf-8") as f:
        _soup = BeautifulSoup(f.read(), "html.parser")

    # Stamp AFTER _enhance_table_html: that call promotes td -> th, and th is
    # the majority of audit leaf tags. Stamping earlier would miss them.
    _stamped = stamp_element_ids(_soup)
    if _stamped:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(str(_soup))
        logger.info("Stamped %d element ids for stable issue resolution", _stamped)

    _table_classifications = {}
    for idx, tbl in enumerate(_soup.find_all("table")):
        ttype = tbl.get("data-table-type")
        if ttype:
            _table_classifications[idx] = ttype

    logger.info(f"Saved HTML: {html_path}")

    # Export structural analysis JSON (for PDF tagger)
    structure_json_path = output_dir / "docling_structure.json"
    structure_elements = _export_structure_json(
        conv_result, structure_json_path, image_files, language, title,
        table_classifications=_table_classifications,
        text_translate_fn=_type3_translate,
        pdf_path=pdf_path,
        vlm_render_dpi=opts.get("vlm_render_dpi", 200),
        detect_heading=opts.get("detect_document_heading", True),
        promote_first_text=opts.get("heading_promote_first_text", True),
    )

    # Gather statistics
    num_pages = len(doc.pages) if hasattr(doc, "pages") else 0
    num_pictures = num_tables = num_formulas = 0
    for item, _ in doc.iterate_items():
        if isinstance(item, PictureItem):
            num_pictures += 1
        elif isinstance(item, TableItem):
            num_tables += 1
        elif FormulaItem is not None and isinstance(item, FormulaItem):
            num_formulas += 1
    elapsed = time.time() - start_time

    stats = {
        "pages": num_pages,
        "figures": num_pictures,
        "tables": num_tables,
        "formulas": num_formulas,
        "conversion_time_seconds": round(elapsed, 2),
        # Structure-yield observability (see gate_structure_yield): how many
        # elements Docling itself produced, on what kind of document, and
        # whether full-page OCR was forced to get them.
        "structure_elements": structure_elements,
        "text_classification": text_classification.get("recommendation"),
        "pages_with_text": len(text_classification.get("pages_with_text") or []),
        "forced_full_page_ocr": force_full_page_ocr,
    }

    logger.info(
        f"Conversion complete: {num_pages} pages, {num_pictures} figures, "
        f"{num_tables} tables, {num_formulas} formulas in {elapsed:.1f}s"
    )

    return {
        "html_path": str(html_path),
        "html_files": html_files,
        "image_files": image_files,
        "image_dir": str(image_dir) if image_files else None,
        "language": language,
        "title": title,
        "stats": stats,
        "type3_repaired": type3_repaired,
        "structure_json_path": str(structure_json_path),
    }
