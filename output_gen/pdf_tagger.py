# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF accessibility tagger orchestrator.

Selects the best available backend (iText 7 → WeasyPrint)
and runs it to produce a tagged PDF from the original PDF + Docling
structural analysis.
"""

import functools
import importlib.util
import io
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from output_gen.content_stream_analyzer import analyze_content_streams
from utils.font_dir import jvm_font_flag
from output_gen.content_matcher import match_elements_to_content

logger = logging.getLogger(__name__)

# Path to the pre-built iText tagger JAR (relative to this file)
_ITEXT_JAR = Path(__file__).parent / "itext_tagger.jar"

# Optional font-diagnostic utilities (coverage-aware broken-font fallback).
try:
    from utils.font_inventory import scan_font_inventory
    from utils.font_resolver import resolve_font
    from utils.glyph_coverage import format_missing_glyphs
    _FONT_UTILS_AVAILABLE = True
except Exception:  # pragma: no cover - utils are optional
    _FONT_UTILS_AVAILABLE = False


@functools.lru_cache(maxsize=1)
def _cached_font_inventory():
    """Scan installed fonts once per process (≈1000+ faces — expensive)."""
    return scan_font_inventory()


def _parse_source_quality(stdout: str) -> dict:
    """Extract SOURCE_QUALITY_JSON from Java tagger stdout."""
    if not stdout:
        return {}
    for line in stdout.split("\n"):
        if line.startswith("SOURCE_QUALITY_JSON:"):
            try:
                return json.loads(line[len("SOURCE_QUALITY_JSON:"):])
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("Malformed SOURCE_QUALITY_JSON: %s", e)
    return {}


def _maybe_reconcile_widths(output_pdf: str, source_quality: dict) -> None:
    """Reconcile /Widths and /W with the embedded program, clearing veraPDF
    7.21.5 t1 (font-dict widths must match the program). Runs on every tagged
    PDF — font injection is one cause of desync, but originally-embedded fonts
    can also ship inconsistent or absent width arrays (e.g. CID TrueType with no
    /W), or a fractional-nominalWidthX CFF that veraPDF misreads. The reconciler
    handles TrueType and simple CFF (never the font program), is idempotent, a
    no-op on consistent fonts, and saves only on change, so running it
    unconditionally is safe; Type1 and CID-keyed CFF are skipped. Best-effort:
    never fatal.
    """
    try:
        from output_gen.font_width_reconciler import reconcile_font_widths
        res = reconcile_font_widths(output_pdf)
        if res.changed:
            codes = sum(f.codes_changed for f in res.fixes)
            logger.info(
                "  Font-width reconcile: %d font(s), %d width(s) corrected",
                res.fonts_fixed, codes,
            )
        elif res.error:
            logger.warning("  Font-width reconcile error: %s", res.error)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("  Font-width reconcile skipped: %s", e)


def detect_available_backends() -> List[str]:
    """Return an ordered list of available PDF tagging backends.

    Checks for:
    - ``"itext"``: Java 11+ and ``itext_tagger.jar`` present
    - ``"weasyprint"``: weasyprint Python package importable (fallback, re-renders from HTML)
    """
    backends = []

    # Check iText (Java + JAR)
    if not _ITEXT_JAR.exists():
        logger.info("iText backend: unavailable (JAR not found at %s)", _ITEXT_JAR)
    elif not shutil.which("java"):
        logger.info("iText backend: unavailable (java not found on PATH)")
    else:
        try:
            result = subprocess.run(
                ["java", "-version"],
                capture_output=True, text=True, timeout=10,
            )
            version_str = result.stderr + result.stdout
            # Java 11+ required — parse major version number
            ver_match = re.search(r'version "(\d+)', version_str)
            java_major = int(ver_match.group(1)) if ver_match else 0
            if java_major >= 11:
                backends.append("itext")
                logger.info("iText backend: available (Java %d)", java_major)
            else:
                java_ver = version_str.strip().split("\n")[0] if version_str.strip() else "unknown"
                logger.warning(
                    "iText backend: unavailable (requires Java 11+, found: %s)",
                    java_ver,
                )
        except Exception as e:
            logger.warning("iText backend: unavailable (java version check failed: %s)", e)

    # Check WeasyPrint
    if importlib.util.find_spec("weasyprint"):
        backends.append("weasyprint")
        logger.info("WeasyPrint backend: available")
    else:
        logger.info("WeasyPrint backend: unavailable (package not installed)")

    return backends


def tag_pdf(
    original_pdf_path: str,
    structure_json_path: str,
    output_pdf_path: str,
    remediation_data: Optional[Dict[str, Any]] = None,
    language: str = "en-US",
    engine: str = "auto",
    title: str = None,
    java_heap: str = "2g",
    tag_mode: str = "content-preserving",
    coverage_warn_pct: float = 95.0,
    coverage_aware_fallback: bool = True,
    fallback_min_coverage: float = 1.0,
    font_substitution_report: bool = True,
    normalize_block_order: bool = True,
    table_first_row_th_fallback: bool = True,
    split_reclassified_figures: bool = True,
) -> Optional[Dict[str, Any]]:
    """Tag an existing PDF with accessibility structure.

    Injects a PDF/UA structure tree into the original PDF using the
    Docling structural analysis (bounding boxes + semantic labels).
    The original visual layout is fully preserved.

    Args:
        original_pdf_path: Path to the untagged source PDF.
        structure_json_path: Path to the Docling structure JSON.
        output_pdf_path: Where to save the tagged PDF.
        remediation_data: Dict with ``alt_texts`` (from Phase 3) and
            optionally ``heading_fixes`` (from Phase 4).
        language: BCP 47 language code (e.g. ``"en-US"``).
        engine: ``"auto"``, ``"itext"``, or ``"weasyprint"``.
        title: Document title for metadata.
        java_heap: JVM max heap size for the iText tagger (e.g. ``"2g"``).
        tag_mode: ``"content-preserving"`` (wraps original visible content)
            or ``"overlay"`` (legacy invisible text layer).
        table_first_row_th_fallback: Promote first row to TH when a table has
            no header flags at all (JAR --first-row-th-fallback).
        split_reclassified_figures: Split text-only false-Figures (flowcharts
            Docling saw as one image) into per-block P elements instead of a
            single Div (config output.split_reclassified_figures).

    Returns:
        Dict with keys ``path`` (str), ``matching_summary`` (dict or None),
        and ``effective_mode`` (str), or ``None`` on failure.
        ``document_title`` (str) is present only when a title sync actually
        occurred; consumers must use ``.get()`` since the key is absent
        otherwise.
    """
    if not Path(original_pdf_path).exists():
        logger.error(f"Original PDF not found: {original_pdf_path}")
        return None

    if not Path(structure_json_path).exists():
        logger.error(f"Structure JSON not found: {structure_json_path}")
        return None

    # Merge remediation data (alt text, heading fixes) into structure JSON
    merged_json = structure_json_path
    if remediation_data:
        merged_json = _merge_remediation_into_structure(
            structure_json_path, remediation_data,
        )

    # Sync table header flags from remediated HTML into structure JSON
    html_path = remediation_data.get("html_path") if remediation_data else None
    synced_title = None
    if html_path:
        _sync_table_headers_from_html(merged_json, html_path)
        _sync_table_captions_from_html(merged_json, html_path)
        # HTML remediation may have replaced a filename-like title with a
        # readable one. Batch reads metadata.title from the JSON, but
        # production passes --title explicitly, so adopt it here too.
        synced_title = _sync_title_from_html(merged_json, html_path)
        if synced_title:
            title = synced_title

    # Select backend
    if engine == "auto":
        logger.info("PDF tagger: auto-detecting available backends...")
        available = detect_available_backends()
        logger.info("PDF tagger: detected backends: %s", available if available else "none")
        # Exclude weasyprint from auto — it can't tag original PDFs
        tag_backends = [b for b in available if b != "weasyprint"]
        if not tag_backends:
            logger.warning(
                "No PDF tagging backend available. iText requires Java 11+ "
                "and itext_tagger.jar at %s. Falling back to WeasyPrint "
                "re-render (original layout will not be preserved).",
                _ITEXT_JAR,
            )
            return None
        engine = tag_backends[0]

    logger.info(f"PDF tagger: using engine '{engine}'")

    if engine == "itext":
        tag_result = _run_itext_tagger(
            original_pdf_path, merged_json, output_pdf_path, language,
            title=title, java_heap=java_heap,
            tag_mode=tag_mode,
            coverage_warn_pct=coverage_warn_pct,
            coverage_aware_fallback=coverage_aware_fallback,
            fallback_min_coverage=fallback_min_coverage,
            font_substitution_report=font_substitution_report,
            normalize_block_order=normalize_block_order,
            table_first_row_th_fallback=table_first_row_th_fallback,
            split_reclassified_figures=split_reclassified_figures,
        )
        if tag_result is not None and synced_title:
            tag_result["document_title"] = synced_title
        return tag_result
    else:
        logger.warning(
            "PDF tagger: engine '%s' is not implemented — "
            "available engines are: itext, weasyprint (fallback)",
            engine,
        )
        return None


def generate_pdf_bookmarks(pdf_path: str, structure_json_path: str,
                           doc_title: str = "") -> bool:
    """Generate PDF bookmarks (outline) from the heading structure.

    Reads heading elements from the Docling structure JSON and creates a PDF
    /Outlines tree.  Required for WCAG 2.4.5 and Acrobat's "Bookmarks are
    present in large documents" check (>=21 pages).

    When no headings are found but the document has 21+ pages, a single
    title bookmark pointing to page 1 is created as a fallback.

    Args:
        pdf_path:             Path to the tagged PDF (modified in-place).
        structure_json_path:  Path to docling_structure.json.
        doc_title:            Optional document title for fallback bookmark.

    Returns:
        True on success, False on failure.
    """
    try:
        import fitz
    except ImportError:
        logger.warning("PyMuPDF not installed — skipping bookmark generation")
        return False

    try:
        with open(structure_json_path, "r", encoding="utf-8") as f:
            structure = json.load(f)
    except Exception as e:
        logger.warning("Failed to load structure JSON for bookmarks: %s", e)
        return False

    _HEADING_LEVEL = {"H1": 1, "H2": 2, "H3": 3, "H4": 4, "H5": 5, "H6": 6}

    toc = []
    for elem in structure.get("elements", []):
        tag = elem.get("tag", "")
        if tag not in _HEADING_LEVEL:
            continue
        text = (elem.get("text") or "").strip()
        page_no = int(elem.get("page_no") or 1)
        if not text:
            continue
        if len(text) > 120:
            text = text[:117] + "..."
        toc.append([_HEADING_LEVEL[tag], text, page_no])

    _ACROBAT_PAGE_THRESHOLD = 21  # Acrobat flags docs with >=21 pages and no bookmarks

    if not toc:
        try:
            _doc = fitz.open(pdf_path)
            page_count = len(_doc)
            _doc.close()
        except Exception:
            page_count = 0
        if page_count < _ACROBAT_PAGE_THRESHOLD:
            logger.info("No headings and < %d pages — skipping bookmark generation",
                        _ACROBAT_PAGE_THRESHOLD)
            return True
        # Fallback: single title bookmark for large documents
        title = doc_title or Path(pdf_path).stem.replace("-", " ").replace("_", " ").title()
        toc.append([1, title, 1])
        logger.info("No headings but %d pages — adding title bookmark", page_count)

    try:
        doc = fitz.open(pdf_path)
        doc.set_toc(toc)
        buf = io.BytesIO()
        doc.save(buf, garbage=0, deflate=True)
        doc.close()
        buf.seek(0)
        Path(pdf_path).write_bytes(buf.read())
        logger.info("Generated %d bookmarks in %s", len(toc), Path(pdf_path).name)
        return True
    except Exception as e:
        logger.error("Failed to generate PDF bookmarks: %s", e)
        return False


def _merge_remediation_into_structure(
    structure_json_path: str,
    remediation_data: Dict[str, Any],
) -> str:
    """Merge alt text and heading fixes into the structure JSON.

    Returns the path to the merged JSON (may be the same file if
    modified in-place).
    """
    try:
        with open(structure_json_path, "r", encoding="utf-8") as f:
            structure = json.load(f)

        alt_texts = remediation_data.get("alt_texts", {})
        modified = False

        if alt_texts:
            for elem in structure.get("elements", []):
                image_ref = elem.get("image_ref")
                if image_ref and elem.get("tag") == "Figure":
                    # Try matching by full path or just filename
                    ref_name = Path(image_ref).name
                    for key, alt in alt_texts.items():
                        if key == image_ref or Path(key).name == ref_name:
                            elem["alt_text"] = alt
                            modified = True
                            break

        if modified:
            with open(structure_json_path, "w", encoding="utf-8") as f:
                json.dump(structure, f, indent=2, ensure_ascii=False)
            logger.info(
                f"Merged {sum(1 for e in structure['elements'] if e.get('alt_text'))} "
                f"alt texts into structure JSON"
            )

    except Exception as e:
        logger.warning(f"Failed to merge remediation data: {e}")

    return structure_json_path


def _table_fingerprint(cell_texts: Iterable[str], limit: int = 12) -> set:
    """Normalized cell-text fingerprint for table matching.

    Samples the first ``limit`` cell texts, normalizes each (remove
    internal whitespace, lowercase, truncate to 30 chars) and returns the
    set of non-empty normalized values. Normalization removes the
    whitespace/case noise that causes false non-matches.
    """
    fp = set()
    for i, t in enumerate(cell_texts):
        if i >= limit:
            break
        norm = "".join((t or "").split()).lower()[:30]
        if norm:
            fp.add(norm)
    return fp


def _match_html_tables_to_struct(
    html_tables: list,
    struct_tables: list,
) -> Dict[int, int]:
    """Map struct-table index -> html-table index for confident matches.

    ``html_tables`` are bs4 ``<table>`` elements (already filtered of nav
    tables); ``struct_tables`` are Docling ``TableItem`` dicts (each with a
    non-empty ``cells`` list). Both lists are in document order.

    - Equal counts: positional ``i -> i`` for every table (tables share
      document order from the same Docling pass). If a positional pair has
      zero text overlap but another HTML table overlaps strongly, log a
      WARNING (probable reorder) but KEEP the positional match.
    - Unequal counts: greedy best-overlap assignment, each HTML table used
      at most once, minimum overlap >= 1; struct tables with no match are
      absent from the result (callers log specifics).
    """
    n_html = len(html_tables)
    n_struct = len(struct_tables)
    if n_html == 0 or n_struct == 0:
        return {}

    html_fps = [
        _table_fingerprint(
            c.get_text(strip=True) for c in t.find_all(["th", "td"])
        )
        for t in html_tables
    ]
    struct_fps = [
        _table_fingerprint(
            (c.get("text") or "") for c in st.get("cells", [])
        )
        for st in struct_tables
    ]

    matches: Dict[int, int] = {}
    if n_html == n_struct:
        for i in range(n_struct):
            if len(struct_fps[i] & html_fps[i]) == 0:
                best_j, best_o = i, 0
                for j in range(n_html):
                    o = len(struct_fps[i] & html_fps[j])
                    if o > best_o:
                        best_o, best_j = o, j
                if best_o > 0 and best_j != i:
                    logger.warning(
                        "Table match: struct table #%d has no text overlap "
                        "with positional HTML table #%d, but HTML table #%d "
                        "overlaps (%d matching cell texts); keeping positional match",
                        i, i, best_j, best_o,
                    )
            matches[i] = i
    else:
        logger.warning(
            "Table count mismatch: %d HTML tables vs %d struct tables; "
            "matching by cell-text overlap",
            n_html, n_struct,
        )
        used = set()
        for i in range(n_struct):
            best_j, best_o = -1, 0
            for j in range(n_html):
                if j in used:
                    continue
                o = len(struct_fps[i] & html_fps[j])
                if o > best_o:
                    best_o, best_j = o, j
            if best_j >= 0 and best_o >= 1:
                matches[i] = best_j
                used.add(best_j)
    return matches


def _norm_title(text: str) -> str:
    """Normalize a title for comparison: collapse whitespace, strip.

    ``\\s`` matches ``\\xa0`` for ``str`` patterns, so non-breaking spaces
    and CR/LF differences between the HTML and JSON copies of the same
    title compare equal. Entities are NOT unescaped here — BeautifulSoup's
    ``get_text()`` already did that, and doing it twice would turn a
    literal ``&amp;amp;`` into ``&``.
    """
    return re.sub(r"\s+", " ", text or "").strip()


def _sync_title_from_html(
    structure_json_path: str,
    html_path: str,
) -> Optional[str]:
    """Sync the remediated document title into the structure JSON.

    HTML remediation rewrites a filename-like <title> into a human-readable
    one (``remediate_descriptive_title``), but the tagger reads the title
    from the structure JSON's ``metadata.title``, which is never updated.
    Four documents in a large batch shipped a junk PDF title -- a URL, an OCR fragment
    (an all-caps word with a digit for a letter), a stray date (``1/1/2020``) and a word fragment
    (``lement``) -- while the HTML already held the correct one.

    The gate is simply "the two differ", and that is stronger than it looks:
    ``remediate_descriptive_title`` runs on nearly every document and PREFERS
    ``metadata.title``, adopting it verbatim whenever it is prose. A
    divergence is therefore the recorded outcome of that strategy judging
    ``metadata.title`` unusable, not a proxy for one. Measured over a batch of 972
    documents: 4 divergences, all improvements, 968 untouched.

    Writes in place (matching ``_sync_table_headers_from_html``) and is
    idempotent -- a second run finds the titles equal and no-ops.

    Returns the newly written title, or None when nothing was written.
    """
    try:
        from bs4 import BeautifulSoup

        html_file = Path(html_path)
        if html_file.is_dir():
            html_files = sorted(html_file.glob("*.html"))
            if not html_files:
                html_files = sorted(
                    (html_file / "extracted_html").glob("*.html")
                )
            if not html_files:
                return None
            html_content = html_files[0].read_text(
                encoding="utf-8", errors="ignore")
        else:
            html_content = html_file.read_text(
                encoding="utf-8", errors="ignore")

        title_el = BeautifulSoup(html_content, "html.parser").find("title")
        html_title = _norm_title(title_el.get_text() if title_el else "")
        # "Untitled Document" is remediate_missing_document_title's
        # placeholder for documents that had no title at all; never let it
        # overwrite a real one.
        if not html_title or html_title == "Untitled Document":
            return None

        with open(structure_json_path, "r", encoding="utf-8") as f:
            structure = json.load(f)
        metadata = structure.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            structure["metadata"] = metadata
        if _norm_title(metadata.get("title") or "") == html_title:
            return None

        metadata["title"] = html_title
        with open(structure_json_path, "w", encoding="utf-8") as f:
            json.dump(structure, f, indent=2, ensure_ascii=False)
        logger.info(
            "Synced document title from remediated HTML: %r", html_title)
        return html_title
    except Exception as e:
        # A metadata nicety must never fail tagging.
        logger.warning(
            "Title sync from HTML failed (keeping existing title): %s", e)
        return None


def _sync_table_headers_from_html(
    structure_json_path: str,
    html_path: str,
) -> None:
    """Sync table header flags in the structure JSON from remediated HTML.

    The HTML remediation promotes cells to <th scope="col|row"> but the
    iText tagger reads header flags from the Docling structure JSON.  This
    function reads the remediated HTML tables and updates the JSON cells'
    ``is_header``, ``is_col_header``, and ``is_row_header`` fields so the
    PDF structure tree gets proper TH elements with Scope attributes.
    """
    try:
        from bs4 import BeautifulSoup

        html_file = Path(html_path)
        if html_file.is_dir():
            # Multi-page: find all HTML files
            html_files = sorted(html_file.glob("*.html"))
            if not html_files:
                html_files = sorted(
                    (html_file / "extracted_html").glob("*.html")
                )
            html_content = "\n".join(f.read_text() for f in html_files)
        else:
            html_content = html_file.read_text()

        soup = BeautifulSoup(html_content, "html.parser")
        html_tables = [
            t for t in soup.find_all("table")
            if not t.get("data-table-type")  # skip nav tables
        ]
        if not html_tables:
            return

        with open(structure_json_path, "r", encoding="utf-8") as f:
            structure = json.load(f)

        # Build lookup: for each HTML table, map (row, col) -> header info
        html_table_maps = []
        for table in html_tables:
            cell_map = {}
            for row_idx, tr in enumerate(table.find_all("tr")):
                grid_col = 0
                for cell in tr.find_all(["th", "td"]):
                    is_th = cell.name == "th"
                    scope = cell.get("scope", "")
                    colspan = int(cell.get("colspan", 1))
                    for _c in range(colspan):
                        cell_map[(row_idx, grid_col)] = {
                            "is_header": is_th,
                            "is_col_header": is_th and scope == "col",
                            "is_row_header": is_th and scope == "row",
                        }
                        grid_col += 1
            html_table_maps.append(cell_map)

        # Match structure JSON tables to HTML tables (positional fallback).
        struct_tables = [
            e for e in structure.get("elements", [])
            if e.get("type") == "TableItem" and e.get("cells")
        ]

        matches = _match_html_tables_to_struct(html_tables, struct_tables)

        modified = False
        for si, struct_table in enumerate(struct_tables):
            hi = matches.get(si)
            cells = struct_table["cells"]
            if hi is None:
                first = ""
                if cells:
                    first = (cells[0].get("text") or "").strip()[:30]
                logger.warning(
                    "Table headers could not be matched for struct table #%d "
                    "(first cell: %r); header flags not synced", si, first,
                )
                continue
            cell_map = html_table_maps[hi]
            for cell in cells:
                row = cell.get("row", 0)
                col = cell.get("col", 0)
                info = cell_map.get((row, col))
                if info is None:
                    continue
                if info["is_header"] != cell.get("is_header", False):
                    cell["is_header"] = info["is_header"]
                    modified = True
                if info["is_col_header"] != cell.get("is_col_header", False):
                    cell["is_col_header"] = info["is_col_header"]
                    modified = True
                if info["is_row_header"] != cell.get("is_row_header", False):
                    cell["is_row_header"] = info["is_row_header"]
                    modified = True

        if modified:
            with open(structure_json_path, "w", encoding="utf-8") as f:
                json.dump(structure, f, indent=2, ensure_ascii=False)
            n_headers = sum(
                1 for e in structure["elements"]
                if e.get("type") == "TableItem"
                for c in e.get("cells", [])
                if c.get("is_header")
            )
            logger.info(
                f"Synced table header flags from HTML: "
                f"{n_headers} header cells marked"
            )

    except Exception as e:
        logger.warning(f"Failed to sync table headers from HTML: {e}")


def _sync_table_captions_from_html(
    structure_json_path: str,
    html_path: str,
) -> None:
    """Sync table captions from remediated HTML into the structure JSON.

    The VLM table caption generator inserts <caption> elements into the
    HTML, but the iText tagger reads from the Docling structure JSON.
    This function reads captions from the HTML and writes a ``caption``
    field on matching Table entries so the tagger can emit a Caption struct
    element (first child of the Table) carrying that text for screen readers.
    """
    try:
        from bs4 import BeautifulSoup

        html_file = Path(html_path)
        if html_file.is_dir():
            html_files = sorted(html_file.glob("*.html"))
            if not html_files:
                html_files = sorted(
                    (html_file / "extracted_html").glob("*.html")
                )
            html_content = "\n".join(f.read_text() for f in html_files)
        else:
            html_content = html_file.read_text()

        soup = BeautifulSoup(html_content, "html.parser")
        html_tables = [
            t for t in soup.find_all("table")
            if not t.get("data-table-type")
        ]
        if not html_tables:
            return

        with open(structure_json_path, "r", encoding="utf-8") as f:
            structure = json.load(f)

        struct_tables = [
            e for e in structure.get("elements", [])
            if e.get("type") == "TableItem" and e.get("cells")
        ]

        def _caption_of(table):
            cap = table.find("caption")
            return cap.get_text(strip=True) if cap else ""

        if not struct_tables:
            # Captions exist in HTML but there is nothing to attach them to.
            for t in html_tables:
                cap_text = _caption_of(t)
                if cap_text:
                    logger.warning(
                        "Table caption could not be matched to a struct "
                        "table and was NOT tagged: %r", cap_text[:80],
                    )
            return

        matches = _match_html_tables_to_struct(html_tables, struct_tables)

        modified = False
        placed = 0
        for si, struct_table in enumerate(struct_tables):
            hi = matches.get(si)
            if hi is None:
                continue
            cap_text = _caption_of(html_tables[hi])
            if not cap_text:
                continue
            placed += 1
            if cap_text != struct_table.get("caption", ""):
                struct_table["caption"] = cap_text
                modified = True

        # No silent drops: warn for every HTML caption whose table was
        # never matched to a struct table.
        matched_html = set(matches.values())
        n_html_captions = 0
        for hi, t in enumerate(html_tables):
            cap_text = _caption_of(t)
            if not cap_text:
                continue
            n_html_captions += 1
            if hi not in matched_html:
                logger.warning(
                    "Table caption could not be matched to a struct table "
                    "and was NOT tagged: %r", cap_text[:80],
                )

        if modified:
            with open(structure_json_path, "w", encoding="utf-8") as f:
                json.dump(structure, f, indent=2, ensure_ascii=False)
        if n_html_captions:
            logger.info(
                "Synced table captions from HTML: matched %d of %d "
                "HTML captions", placed, n_html_captions,
            )

    except Exception as e:
        logger.warning(f"Failed to sync table captions from HTML: {e}")


def get_system_ram_mb() -> int:
    """Detect total system RAM in MB, cgroup-aware for Docker containers."""
    # Try cgroups v2 first (Docker containers)
    cgroup_limit = Path("/sys/fs/cgroup/memory.max")
    if cgroup_limit.exists():
        try:
            val = cgroup_limit.read_text().strip()
            if val != "max":
                cgroup_mb = int(val) // (1024 * 1024)
                # Only trust if it's less than physical RAM (i.e. actually limited)
                phys_mb = _get_physical_ram_mb()
                if cgroup_mb < phys_mb:
                    return cgroup_mb
        except (ValueError, OSError):
            pass

    # Try cgroups v1 (older Docker)
    cgroup_v1 = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    if cgroup_v1.exists():
        try:
            val = int(cgroup_v1.read_text().strip())
            cgroup_mb = val // (1024 * 1024)
            phys_mb = _get_physical_ram_mb()
            if cgroup_mb < phys_mb:
                return cgroup_mb
        except (ValueError, OSError):
            pass

    return _get_physical_ram_mb()


def _get_physical_ram_mb() -> int:
    """Get physical RAM in MB via os.sysconf."""
    try:
        return (
            os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        ) // (1024 * 1024)
    except (ValueError, OSError):
        return 8192  # safe fallback: 8GB


def _positive_int(value: Any, default: int) -> int:
    """Coerce a config value to an int >= 1, falling back on garbage."""
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return default


def resolve_tagger_slots(config: Optional[dict] = None) -> int:
    """How many tagger JVMs may run concurrently in this process context.

    This is the divisor for the JVM heap budget, so it must reflect how
    many PDFs are actually in flight — not how many the CLI *could* run.

    ``concurrency.tagger_slots``, when present, is authoritative: it is
    injected by callers that know their real concurrency (the Celery
    worker runs ``--concurrency=1``).  Otherwise fall back to
    ``concurrency.batch_workers``, which is correct for CLI batch mode.
    """
    conc = (config or {}).get("concurrency", {})
    if "tagger_slots" in conc:
        return _positive_int(conc.get("tagger_slots"), 1)
    return _positive_int(conc.get("batch_workers", 2), 2)


def celery_tagger_slots(config: Optional[dict] = None) -> int:
    """Concurrent tagger JVMs inside a single Celery worker process.

    A worker started with ``--concurrency=N`` runs N PDFs at a time, so
    ``concurrency.celery_concurrency`` — not ``batch_workers`` — is the
    right divisor for the heap budget on that path.
    """
    conc = (config or {}).get("concurrency", {})
    return _positive_int(conc.get("celery_concurrency", 1), 1)


def compute_java_heap(config: Optional[dict] = None, batch_workers: int = 1) -> str:
    """Compute the JVM -Xmx heap size string.

    Args:
        config: Full pipeline config dict (reads ``resources`` section).
        batch_workers: Number of tagger JVMs that may run concurrently —
            i.e. how many PDFs are actually in flight. Use
            :func:`resolve_tagger_slots` to derive it; passing the CLI's
            ``batch_workers`` on a single-PDF path over-divides the
            budget and starves the tagger to its 512m floor.

    Returns:
        Heap string like ``"2g"`` or ``"512m"``.
    """
    resources = (config or {}).get("resources", {})
    heap_setting = str(resources.get("java_heap", "auto")).strip().lower()

    # Explicit value — pass through directly
    if heap_setting != "auto":
        return heap_setting

    # Auto-detect
    max_ram_pct = resources.get("max_ram_percent", 75)
    python_worker_mb = resources.get("python_worker_mb", 3000)
    total_mb = get_system_ram_mb()
    usable_mb = int(total_mb * max_ram_pct / 100)

    # Budget: usable RAM minus Python workers, divided among JVM taggers.
    # In practice only one tagger runs per worker, so workers == taggers.
    workers = max(batch_workers, 1)
    python_budget = workers * python_worker_mb
    remaining_mb = usable_mb - python_budget
    per_tagger_mb = max(remaining_mb, 0) // workers

    # Floor at 512m, ceiling at 8g.  Hitting the floor means the Python
    # budget has eaten the whole allowance — the tagger will then OOM on
    # large PDFs, so say so loudly rather than clamping in silence.
    if per_tagger_mb < 512:
        logger.warning(
            "JVM heap budget exhausted — clamping to the 512m floor. "
            "system=%dMB, usable=%dMB (%s%%), python_budget=%dMB "
            "(%d worker(s) x %dMB) leaves %dMB, i.e. %dMB per tagger. "
            "The iText tagger/analyzer will likely OOM on large PDFs; "
            "lower resources.python_worker_mb, raise the container "
            "memory limit, or reduce the concurrent worker count.",
            total_mb, usable_mb, max_ram_pct, python_budget,
            workers, python_worker_mb, remaining_mb, per_tagger_mb,
        )
    per_tagger_mb = max(per_tagger_mb, 512)
    per_tagger_mb = min(per_tagger_mb, 8192)

    # Express as "Xg" or "Xm"
    if per_tagger_mb >= 1024 and per_tagger_mb % 1024 == 0:
        heap = f"{per_tagger_mb // 1024}g"
    else:
        heap = f"{per_tagger_mb}m"

    logger.info(
        "Auto-detected JVM heap: %s (system=%dMB, usable=%dMB, "
        "workers=%d, python_budget=%dMB)",
        heap, total_mb, usable_mb, workers, python_budget,
    )
    return heap


def _compute_timeout(original_pdf: str, base: int = 300) -> int:
    """Scale subprocess timeout by input file size.

    Base 300s + 2s per MB of input, capped at 1800s (30min).
    """
    try:
        size_mb = Path(original_pdf).stat().st_size / (1024 * 1024)
    except OSError:
        size_mb = 0
    return min(base + int(size_mb * 2), 1800)


def _is_broken_font_name(name: str) -> bool:
    """True for the ``NotDefSpecial`` family, with or without a subset prefix."""
    return name.split("+", 1)[-1].startswith("NotDef")


# Text-showing operators, and what replaces each when its text is removed:
# ' and " also move to the next line (and " sets spacing), so keep that part.
_SHOW_OPS = {"Tj", "TJ", "'", '"'}


def _strip_font_text(instructions, broken_resources: set):
    """Drop text shown in a broken font; keep every other instruction.

    Tracks the current font through ``Tf`` and the ``q``/``Q`` stack (the font
    is part of the graphics state), so text in good fonts, vector graphics and
    images survive untouched.
    """
    import pikepdf

    out = []
    font = None
    saved = []
    for operands, operator in instructions:
        op = str(operator)
        if op == "q":
            saved.append(font)
        elif op == "Q":
            font = saved.pop() if saved else None
        elif op == "Tf" and operands:
            font = str(operands[0]).lstrip("/")
        elif op in _SHOW_OPS and font in broken_resources:
            if op == "'":
                out.append(([], pikepdf.Operator("T*")))
            elif op == '"':
                out.append(([operands[0]], pikepdf.Operator("Tw")))
                out.append(([operands[1]], pikepdf.Operator("Tc")))
                out.append(([], pikepdf.Operator("T*")))
            continue
        out.append((operands, operator))
    return out


def _preprocess_broken_fonts(
    pdf_path: str,
    *,
    coverage_aware: bool = True,
    min_coverage: float = 1.0,
    report=None,
) -> str:
    """Replace broken .notdef-only font text with an embedded fallback font.

    Scanned theses and dissertations often use ``NotDefSpecial`` — a Type0
    font where every glyph maps to ``.notdef``.  PyMuPDF can still
    extract the text (via ToUnicode CMap), so we:

    1. Extract per-span text + positions with ``page.get_text('dict')``
    2. Remove only the text shown in the broken font; images, vector
       graphics and text in other fonts are kept as they are
    3. Re-add the broken-font text with the fallback font (Identity-H) at the
       original positions and sizes, with white fill (invisible)
    4. Insert the fallback font into page resources

    When ``coverage_aware`` is set (default), the fallback font is chosen by
    glyph coverage of the page text via :func:`utils.font_resolver.resolve_font`
    rather than a hardcoded first-found font, so glyphs the fallback lacks are
    logged (and recorded on ``report``) instead of being silently emitted as
    ``.notdef``.  Falls back to the legacy hardcoded list when resolution is
    unavailable or selects an unembeddable collection face.

    Returns the path to the preprocessed PDF, or the original path
    if no broken fonts were detected (no-op).
    """
    try:
        import fitz
        import pikepdf
        from fontTools.ttLib import TTFont
    except ImportError:
        logger.warning("PyMuPDF or fontTools not available — skipping "
                       "broken font preprocessing")
        return pdf_path

    # Legacy hardcoded fallback list (used when coverage-aware resolution is
    # disabled, unavailable, or selects an unembeddable collection face).
    legacy_font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    ]

    def _legacy_font():
        for fp in legacy_font_paths:
            if os.path.isfile(fp):
                return fp
        return None

    doc = fitz.open(pdf_path)

    # Quick scan: any pages with broken fonts?  Capture the broken basefont
    # names too (used as the requested family for coverage resolution).
    broken_pages = []
    broken_basefonts = []
    broken_resources = {}
    for pg_idx in range(len(doc)):
        page_broken = [f for f in doc.get_page_fonts(pg_idx) if _is_broken_font_name(f[3])]
        if page_broken:
            broken_pages.append(pg_idx)
            broken_basefonts.extend(f[3] for f in page_broken)
            broken_resources[pg_idx] = {f[4] for f in page_broken}
    if not broken_pages:
        doc.close()
        return pdf_path

    # Union of text across broken pages — the sample the fallback must cover.
    doc_text = "".join(doc[pg].get_text() for pg in broken_pages)

    # --- Choose the fallback font ---
    ttf_path = None
    ttf_font_number = 0
    if coverage_aware and _FONT_UTILS_AVAILABLE:
        try:
            inventory = _cached_font_inventory()
        except Exception as e:  # inventory scan failed — degrade to legacy
            logger.warning("Font inventory scan failed (%s) — using legacy fallback", e)
            inventory = None
        if inventory:
            requested_family = broken_basefonts[0] if broken_basefonts else ""
            resolution = resolve_font(
                requested_family, doc_text,
                fonts=inventory, min_coverage=min_coverage,
            )
            sel = resolution.selected
            # insert_font() can only embed a single-face .ttf, so reject .ttc
            # collection members (font_number != 0) to avoid GID mismatch.
            if sel is not None and sel.font_number == 0 and os.path.isfile(sel.path):
                ttf_path = sel.path
                cov = resolution.coverage
                if cov is not None and not cov.fully_covered:
                    logger.warning(
                        "  Fallback font %s misses %d glyph(s) in broken-font text: %s",
                        sel.full_name, cov.missing_count,
                        format_missing_glyphs(cov.missing),
                    )
                else:
                    logger.info("  Coverage-aware fallback font: %s", sel.full_name)
                if report is not None:
                    report.add(resolution)
            elif sel is not None:
                logger.warning(
                    "  Resolved font %s is a collection face (%d) — using legacy fallback",
                    sel.full_name, sel.font_number,
                )
            else:
                logger.warning("  No usable font resolved — using legacy fallback")

    if ttf_path is None:
        ttf_path = _legacy_font()
    if ttf_path is None:
        logger.warning("No embeddable TrueType font found — skipping "
                       "broken font preprocessing")
        doc.close()
        return pdf_path

    # Build Unicode → GID hex encoder for the replacement font
    ttfont = TTFont(ttf_path, fontNumber=ttf_font_number)
    tt_cmap = ttfont.getBestCmap()
    tt_order = ttfont.getGlyphOrder()

    def _text_to_hex(text: str) -> str:
        parts = []
        for ch in text:
            gn = tt_cmap.get(ord(ch))
            if gn and gn in tt_order:
                parts.append(f"{tt_order.index(gn):04X}")
            else:
                parts.append("0000")
        return "".join(parts)

    # Rewrite broken-font pages
    font_ref_name = "F_dvs"
    for pg_idx in broken_pages:
        page = doc[pg_idx]
        streams = page.get_contents()
        if not streams:
            # Inherited font resources list on blank pages too; nothing to rewrite.
            continue
        blocks = page.get_text("dict")

        # Original content, with the broken-font text removed and everything
        # else (images, vector graphics, text in good fonts) kept.
        # Streams are divided only at token boundaries (PDF 7.7.3.3), so join
        # with whitespace or "...Q" + "q..." would parse as one operator "Qq".
        orig_bytes = b"\n".join(doc.xref_stream(sx) for sx in streams)
        with pikepdf.Pdf.new() as scratch:
            instructions = pikepdf.parse_content_stream(scratch.make_stream(orig_bytes))
            kept = pikepdf.unparse_content_stream(
                _strip_font_text(instructions, broken_resources[pg_idx]))

        # Generate replacement text ops from PyMuPDF lines, for the
        # broken-font spans only. Aggregate a line's spans into a single text
        # string so spaces between words are preserved.
        text_ops = []
        for b in blocks["blocks"]:
            if b["type"] != 0:
                continue
            for line in b["lines"]:
                spans = [s for s in line["spans"] if _is_broken_font_name(s["font"])]
                if not spans:
                    continue
                # Concatenate all span texts (including space spans)
                line_text = "".join(s["text"] for s in spans)
                if not line_text.strip():
                    continue
                # Position from the first span; size from the
                # first non-space span (space spans may have
                # slightly different sizes)
                x = spans[0]["bbox"][0]
                y_pdf = page.rect.height - spans[0]["bbox"][3]
                size = max(1, next(
                    (s["size"] for s in spans if s["text"].strip()),
                    spans[0]["size"],
                ))
                hex_text = _text_to_hex(line_text)
                text_ops.append(
                    f"BT\n/{font_ref_name} {size:.1f} Tf\n"
                    f"1 1 1 rg\n0 Tr\n"
                    f"{x:.2f} {y_pdf:.2f} Td\n"
                    f"<{hex_text}>Tj\nET\n"
                )

        # Write once: the first stream gets everything, any others are emptied
        # (writing the same content into each stream would draw it twice).
        new_content = kept + b"\n" + "\n".join(text_ops).encode("latin-1")
        doc.update_stream(streams[0], new_content)
        for sx in streams[1:]:
            doc.update_stream(sx, b"")

        # Add replacement font to page resources
        page.insert_font(fontname=font_ref_name, fontfile=ttf_path)

    # Save preprocessed PDF
    out_dir = Path(pdf_path).parent
    out_path = str(out_dir / (Path(pdf_path).stem + "_preprocessed.pdf"))
    doc.save(out_path, garbage=4, deflate=True)
    doc.close()

    logger.info(
        "  Preprocessed %d broken-font page(s) → %s",
        len(broken_pages), Path(out_path).name,
    )
    return out_path


def _classify_oom(returncode: int, stderr: str) -> Optional[str]:
    """Classify an OOM failure — the two kinds need opposite responses.

    Returns ``"container"`` when the kernel SIGKILLed the JVM for
    exceeding its cgroup limit, ``"heap"`` when the JVM exhausted its own
    ``-Xmx`` ceiling, or ``None`` when the failure was not an OOM.
    """
    if returncode == -9:  # SIGKILL — cgroup / Linux OOM killer
        return "container"
    if stderr and ("OutOfMemoryError" in stderr or "ExitOnOutOfMemoryError" in stderr):
        return "heap"
    return None


def _describe_tagger_failure(returncode: Optional[int], stderr: str) -> str:
    """Explain a tagger subprocess failure for the log.

    A bare ``rc=1`` hides the difference between a broken PDF and an
    exhausted heap; the latter is a capacity misconfiguration with a
    concrete remedy, so name it.
    """
    kind = _classify_oom(returncode, stderr)
    if kind == "heap":
        return (
            f"rc={returncode} — JVM heap exhausted. Raise the worker "
            f"container memory limit or lower resources.python_worker_mb"
        )
    if kind == "container":
        return (
            f"rc={returncode} — killed by the OOM killer; the JVM did not "
            f"fit inside its cgroup memory limit"
        )
    return f"rc={returncode}"


def _serial_gc_is_default() -> bool:
    """Whether JVM ergonomics would already select SerialGC.

    HotSpot picks SerialGC on what it considers a small machine — fewer
    than 2 available processors, or less than ~1792MB of memory — and G1
    otherwise.  Where Serial is already the default, retrying with
    ``-XX:+UseSerialGC`` re-runs a byte-identical command line.
    """
    try:
        cpus = os.cpu_count() or 1
    except Exception:  # pragma: no cover — os.cpu_count is not expected to raise
        cpus = 1
    return cpus < 2 or get_system_ram_mb() < 1792


def _oom_retry_heap(kind: Optional[str], java_heap: str) -> Optional[str]:
    """Heap for the single OOM retry, or ``None`` to skip retrying.

    ``"container"`` — the JVM footprint did not fit the cgroup, so halve
    the heap to shrink it back inside the limit.

    ``"heap"`` — the ``-Xmx`` ceiling was already too low. Halving it
    guarantees the retry fails too, and costs a full second JVM run
    (minutes on a large PDF). Keep the ceiling; the only lever left is
    the GC, so retry only where that lever actually moves.
    """
    if kind == "container":
        return _halve_heap(java_heap)
    if kind == "heap":
        if _serial_gc_is_default():
            return None
        return java_heap
    return None


def _validate_struct_tree(output_pdf: str, original_pdf: str, *, coverage_warn_pct: float = 95.0) -> list:
    """Post-tagging structure tree validation.

    Runs the struct_tree_validator checks and logs results.
    Non-blocking — warnings/errors are logged but do not fail the pipeline.

    Returns the list of issue dicts from the validator (empty on failure).
    """
    try:
        from output_gen.struct_tree_validator import validate_tagged_pdf as _validate_st
    except ImportError:
        return []

    try:
        issues = _validate_st(output_pdf, original_pdf, coverage_warn_pct=coverage_warn_pct)

        errors = [i for i in issues if i["severity"] == "error"]
        warnings = [i for i in issues if i["severity"] == "warning"]

        for issue in errors:
            elem = issue.get("element", {})
            loc = f" ({elem.get('path', '')})" if elem and elem.get("path") else ""
            logger.warning("  Struct tree: %s%s", issue["message"], loc)

        for issue in warnings:
            logger.info("  Struct tree: %s", issue["message"])

        # Log summary
        summary = next((i for i in issues if i["check"] == "summary"), None)
        if summary:
            logger.info("  Struct tree: %s", summary["message"])

        return issues

    except Exception as e:
        logger.debug("  Struct tree validation skipped: %s", e)
        return []



def _run_itext_tagger(
    original_pdf: str,
    structure_json: str,
    output_pdf: str,
    lang: str,
    title: str = None,
    java_heap: str = "2g",
    tag_mode: str = "content-preserving",
    coverage_warn_pct: float = 95.0,
    coverage_aware_fallback: bool = True,
    fallback_min_coverage: float = 1.0,
    font_substitution_report: bool = True,
    normalize_block_order: bool = True,
    table_first_row_th_fallback: bool = True,
    split_reclassified_figures: bool = True,
) -> Optional[Dict[str, Any]]:
    """Run the iText Java tagger via subprocess.

    When *tag_mode* is ``"content-preserving"``, runs content stream
    analysis and element-to-content matching before tagging. Falls back
    to ``"overlay"`` if analysis/matching fails.

    Args:
        java_heap: JVM max heap size (e.g. ``"2g"``, ``"512m"``).
            Passed as ``-Xmx`` flag.
        tag_mode: ``"content-preserving"`` or ``"overlay"``.
    """
    jar_path = str(_ITEXT_JAR)
    timeout = _compute_timeout(original_pdf)
    file_size_mb = Path(original_pdf).stat().st_size / (1024 * 1024)
    input_pdf = original_pdf

    # --- Content-preserving: analyze + match before tagging ---
    enriched_json = None
    preprocessed_pdf = None
    matching_summary = None
    stripped_text_pages = []
    tagger_structure = structure_json
    effective_tag_mode = tag_mode

    if tag_mode == "content-preserving":
        try:
            # Pre-process: replace broken .notdef fonts (e.g. NotDefSpecial)
            # with an embedded fallback BEFORE analysis, so the analyzer sees
            # real text runs instead of .notdef glyphs.  When coverage_aware,
            # the fallback is chosen by glyph coverage and substitutions are
            # recorded in a sibling diagnostic report.
            font_report = None
            if font_substitution_report and _FONT_UTILS_AVAILABLE:
                try:
                    from output_gen.font_substitution_report import FontSubstitutionReport
                    font_report = FontSubstitutionReport()
                except Exception as e:  # pragma: no cover
                    logger.debug("Font substitution report unavailable: %s", e)
            preprocessed = _preprocess_broken_fonts(
                input_pdf,
                coverage_aware=coverage_aware_fallback,
                min_coverage=fallback_min_coverage,
                report=font_report,
            )
            if preprocessed != input_pdf:
                preprocessed_pdf = preprocessed
                input_pdf = preprocessed
            if font_report is not None and font_report.has_substitutions():
                try:
                    _p = Path(output_pdf)
                    json_path, _md_path = font_report.write(_p.parent, _p.stem)
                    logger.info(
                        "  Font substitution: %d font(s) substituted%s → %s",
                        len(font_report.records),
                        " (residual missing glyphs)" if font_report.has_residual_missing() else "",
                        json_path.name,
                    )
                except Exception as e:  # pragma: no cover
                    logger.debug("Failed to write font substitution report: %s", e)

            # --- Try single-JVM mode (content-preserving-full) first ---
            result = _run_single_jvm_content_preserving(
                jar_path, input_pdf, structure_json, output_pdf,
                lang, title, java_heap, timeout, original_pdf,
                coverage_warn_pct,
                normalize_block_order=normalize_block_order,
                table_first_row_th_fallback=table_first_row_th_fallback,
                split_reclassified_figures=split_reclassified_figures,
            )
            if result is not None:
                # This return skips the cleanup in the finally block below.
                if preprocessed_pdf and os.path.exists(preprocessed_pdf):
                    try:
                        os.unlink(preprocessed_pdf)
                    except OSError:
                        pass
                return result

            # Single-JVM failed — fall back to two-process mode
            logger.info("  Falling back to two-process content-preserving mode")

            logger.info("  Content-preserving: analyzing content streams...")
            cs_analysis = analyze_content_streams(input_pdf, java_heap=java_heap)
            logger.info(
                "  Content-preserving: %d text runs, %d images across %d pages",
                cs_analysis.get("summary", {}).get("total_text_runs", 0),
                cs_analysis.get("summary", {}).get("total_images", 0),
                cs_analysis.get("pages", 0),
            )

            enriched, matching_summary, stripped_text_pages = \
                _do_matching_and_gap_recovery(
                    cs_analysis, structure_json, original_pdf,
                    split_reclassified_figures=split_reclassified_figures,
                )

            # Write enriched structure to temp file alongside original
            _p = Path(structure_json)
            enriched_json = str(_p.with_name(_p.stem + "_enriched" + _p.suffix))
            with open(enriched_json, "w", encoding="utf-8") as f:
                json.dump(enriched, f)
            tagger_structure = enriched_json

        except Exception as e:
            logger.warning(
                "Content-preserving analysis/matching failed for %s: %s — "
                "falling back to overlay mode.",
                Path(original_pdf).name, e,
            )
            effective_tag_mode = "overlay"
            enriched_json = None

    try:
        tagger_result = _invoke_tagger(
            jar_path, input_pdf, tagger_structure, output_pdf,
            lang, title, java_heap, timeout,
            tag_mode=effective_tag_mode,
            table_first_row_th_fallback=table_first_row_th_fallback,
        )

        if tagger_result["path"] is not None:
            struct_tree_issues = _validate_struct_tree(output_pdf, original_pdf, coverage_warn_pct=coverage_warn_pct)
            _sq = _parse_source_quality(tagger_result.get("stdout", ""))
            _maybe_reconcile_widths(output_pdf, _sq)
            return {
                "path": tagger_result["path"],
                "matching_summary": matching_summary,
                "effective_mode": effective_tag_mode,
                "struct_tree_issues": struct_tree_issues,
                "stripped_text_pages": stripped_text_pages,
                "source_quality_warnings": _sq,
            }

        # --- Single retry on OOM ---
        # Shrinking the heap is right for a cgroup OOM-kill and wrong for
        # heap exhaustion, so the retry heap is chosen per kind.
        _oom_kind = _classify_oom(
            tagger_result["returncode"], tagger_result["stderr"]
        )
        retry_heap = _oom_retry_heap(_oom_kind, java_heap)
        if retry_heap is not None:
            if _oom_kind == "container":
                logger.warning(
                    "iText tagger killed by the OOM killer with -Xmx%s on "
                    "%.1fMB PDF — retrying with -Xmx%s and SerialGC",
                    java_heap, file_size_mb, retry_heap,
                )
            else:
                logger.warning(
                    "iText tagger exhausted its -Xmx%s heap on %.1fMB PDF — "
                    "retrying at the same heap with SerialGC; lowering the "
                    "ceiling would only fail again. If this recurs, raise the "
                    "worker container memory limit or lower "
                    "resources.python_worker_mb.",
                    java_heap, file_size_mb,
                )
            tagger_result = _invoke_tagger(
                jar_path, input_pdf, tagger_structure, output_pdf,
                lang, title, retry_heap, timeout, use_serial_gc=True,
                tag_mode=effective_tag_mode,
                table_first_row_th_fallback=table_first_row_th_fallback,
            )
            if tagger_result["path"] is not None:
                struct_tree_issues = _validate_struct_tree(output_pdf, original_pdf, coverage_warn_pct=coverage_warn_pct)
                _sq = _parse_source_quality(tagger_result.get("stdout", ""))
                _maybe_reconcile_widths(output_pdf, _sq)
                return {
                    "path": tagger_result["path"],
                    "matching_summary": matching_summary,
                    "effective_mode": effective_tag_mode,
                    "struct_tree_issues": struct_tree_issues,
                    "stripped_text_pages": stripped_text_pages,
                    "source_quality_warnings": _sq,
                }
            logger.error(
                "iText tagger retry also failed for %s (%.1fMB)",
                Path(original_pdf).name, file_size_mb,
            )
        return None

    finally:
        # Clean up temp files
        if enriched_json and os.path.exists(enriched_json):
            try:
                os.unlink(enriched_json)
            except OSError:
                pass
        if preprocessed_pdf and os.path.exists(preprocessed_pdf):
            try:
                os.unlink(preprocessed_pdf)
            except OSError:
                pass


def _halve_heap(heap: str) -> str:
    """Halve a heap string like '4g' -> '2g' or '2g' -> '1g'."""
    m = re.match(r"(\d+)(g|m)", heap.lower())
    if not m:
        return "512m"
    val, unit = int(m.group(1)), m.group(2)
    if unit == "g":
        halved = val // 2
        if halved >= 1:
            return f"{halved}g"
        return "512m"
    else:
        halved = max(val // 2, 256)
        return f"{halved}m"


def _apply_cover_page_figure_treatment(
    enriched: Dict[str, Any],
    cs_analysis: Dict[str, Any],
) -> Dict[str, Any]:
    """Replace overlay fallback page 1 elements with a single full-page Figure.

    Triggers when page 1 is in overlay_pages and ALL page 1 elements have
    0 content runs (scanned cover page). Replaces with a Figure wrapping
    the page background image, with alt text synthesized from element text.
    """
    overlay_pages = enriched.get("overlay_pages", [])
    if 1 not in overlay_pages:
        return enriched

    elements = enriched.get("elements", [])
    page1_elems = [e for e in elements if e.get("page_no") == 1]
    if not page1_elems:
        return enriched

    # Guard 1: a single-page document has no "cover" — never collapse it.
    total_pages = (
        cs_analysis.get("pages")
        or len(cs_analysis.get("page_analyses", []))
        or max((e.get("page_no", 1) for e in elements), default=1)
    )
    if total_pages <= 1:
        logger.info(
            "  Cover page: skipped — single-page document (no cover to collapse)"
        )
        return enriched

    # Guard 2: a page Docling OCR'd into real structure (a heading or several
    # substantive paragraphs) is a document, not a bare scanned cover. Collapsing
    # it into one Figure would destroy that structure and tank text coverage / SR.
    _HEADING_TAGS = {"H1", "H2", "H3", "H4", "H5", "H6", "Title"}
    has_heading = any(e.get("tag") in _HEADING_TAGS for e in page1_elems)
    substantive_text = sum(
        1 for e in page1_elems
        if e.get("tag") != "Figure" and len((e.get("text") or "").strip()) > 3
    )
    if has_heading or substantive_text > 2:
        logger.info(
            "  Cover page: skipped — page 1 has real structure "
            "(heading=%s, substantive text elements=%d)",
            has_heading, substantive_text,
        )
        return enriched

    # Only trigger if ALL page 1 elements have 0 content runs
    if any(e.get("content_run_count", 0) > 0 for e in page1_elems):
        return enriched

    # Build alt text from original elements in reading order
    texts = []
    for elem in page1_elems:
        text = (elem.get("text") or "").strip()
        if text:
            texts.append(text)
    alt_text = "Cover page: " + ". ".join(texts) if texts else "Cover page"

    # Find page 1 image and dimensions from cs_analysis
    image_run_indices = []
    page_width = 612.0
    page_height = 792.0
    for pa in cs_analysis.get("page_analyses", []):
        if pa["page"] == 1:
            page_width = pa.get("width", 612.0)
            page_height = pa.get("height", 792.0)
            imgs = pa.get("images", [])
            if imgs:
                best = max(imgs, key=lambda im: im.get("width", 0) * im.get("height", 0))
                image_run_indices = [{"page": 1, "image_index": best["index"]}]
            break

    # Create single Figure element for the full page
    figure_elem = {
        "id": "cover_page_figure",
        "type": "FigureItem",
        "tag": "Figure",
        "page_no": 1,
        "text": "",
        "alt_text": alt_text,
        "bbox": {"l": 0, "b": 0, "r": page_width, "t": page_height},
        "image_run_indices": image_run_indices,
        "content_run_indices": [],
        "content_run_count": 0,
        "match_confidence": "cover_page",
        "match_method": "cover_page_figure",
    }

    # Replace page 1 elements with the Figure, keep other pages
    new_elements = [e for e in elements if e.get("page_no") != 1]
    new_elements.insert(0, figure_elem)
    enriched["elements"] = new_elements

    # Remove page 1 from overlay_pages so tagger uses image wrapping path
    enriched["overlay_pages"] = [p for p in overlay_pages if p != 1]

    logger.info("  Cover page: replaced %d elements with single Figure (alt: %s)",
                len(page1_elems), alt_text[:80])

    return enriched


def _do_matching_and_gap_recovery(
    cs_analysis: Dict[str, Any],
    structure_json: str,
    original_pdf: str,
    normalize_block_order: bool = True,
    split_reclassified_figures: bool = True,
) -> tuple:
    """Run element-to-content matching and gap recovery.

    Returns (enriched_dict, matching_summary, stripped_text_pages).
    """
    with open(structure_json, encoding="utf-8") as f:
        docling_structure = json.load(f)
    enriched = match_elements_to_content(
        docling_structure, cs_analysis, source_pdf_path=original_pdf,
        normalize_block_order=normalize_block_order,
        split_reclassified=split_reclassified_figures,
    )
    matching_summary = enriched.get("matching_summary")

    # Cover page treatment: replace overlay-only page 1 with single Figure
    enriched = _apply_cover_page_figure_treatment(enriched, cs_analysis)

    stripped_text_pages = sorted(
        set(enriched.get("invisible_only_pages", []))
        | set(enriched.get("overlay_pages", []))
        | set(enriched.get("form_image_pages", []))
    )

    matched = sum(
        1 for e in enriched.get("elements", [])
        if e.get("content_run_count", 0) > 0
    )
    total_elems = len(enriched.get("elements", []))
    logger.info(
        "  Content-preserving: %d/%d elements matched to content runs",
        matched, total_elems,
    )

    try:
        from output_gen.gap_recovery import recover_content_gaps
        enriched = recover_content_gaps(
            enriched, original_pdf, cs_analysis
        )
    except Exception as gap_err:
        logger.warning(
            "Gap recovery failed for %s: %s (continuing without)",
            Path(original_pdf).name, gap_err,
        )

    # Final safety net: re-level the merged heading sequence so the structure
    # tree the tagger emits is PDF/UA 7.4.2-valid (H1 first, no skipped levels).
    # Cover-page treatment and gap recovery above can inject/reorder headings
    # after heading_inference normalized the source JSON, re-introducing skips.
    try:
        from output_gen.heading_inference import normalize_heading_levels
        n_fixed = normalize_heading_levels(enriched.get("elements", []))
        if n_fixed:
            logger.info(
                "  Heading levels normalized for PDF/UA 7.4.2: %d", n_fixed
            )
    except Exception as e:
        logger.warning("Heading-level normalization skipped: %s", e)

    return enriched, matching_summary, stripped_text_pages


class _PipeDrain:
    """Read a child's stdout and stderr on background threads.

    A child that writes more than a pipe holds (64 KB on Linux) blocks until
    someone reads; draining keeps it running while we wait for something else.
    """

    def __init__(self, proc: "subprocess.Popen[str]") -> None:
        self._out: List[str] = []
        self._err: List[str] = []
        self._threads = [
            threading.Thread(target=self._read, args=(proc.stdout, self._out), daemon=True),
            threading.Thread(target=self._read, args=(proc.stderr, self._err), daemon=True),
        ]
        for t in self._threads:
            t.start()

    @staticmethod
    def _read(stream, sink: List[str]) -> None:
        if stream is not None:
            for chunk in iter(lambda: stream.read(65536), ""):
                sink.append(chunk)

    def result(self, timeout: float = 5) -> "tuple[str, str]":
        """Join the readers (after the child exits) and return (stdout, stderr)."""
        for t in self._threads:
            t.join(timeout)
        return "".join(self._out), "".join(self._err)


def _run_single_jvm_content_preserving(
    jar_path: str,
    input_pdf: str,
    structure_json: str,
    output_pdf: str,
    lang: str,
    title: Optional[str],
    java_heap: str,
    timeout: int,
    original_pdf: str,
    coverage_warn_pct: float,
    normalize_block_order: bool = True,
    table_first_row_th_fallback: bool = True,
    split_reclassified_figures: bool = True,
) -> Optional[Dict[str, Any]]:
    """Run analyze + match + tag in a single JVM process.

    Uses the ``content-preserving-full`` tag mode: Java does analysis,
    writes a signal file, Python does matching/gap-recovery, writes
    enriched JSON, and the same JVM reads it and tags.

    Returns the standard result dict on success, or ``None`` if the
    single-JVM mode is unavailable or fails (caller should fall back
    to two-process mode).
    """
    analysis_path = output_pdf + ".analysis.json"
    enriched_path = output_pdf + ".enriched.json"

    # Clean up any stale handshake files
    for p in (analysis_path, enriched_path):
        if os.path.exists(p):
            try:
                os.unlink(p)
            except OSError:
                pass

    cds_archive = Path(__file__).parent / "itext_tagger.jsa"
    jvm_flags = [
        f"-Xmx{java_heap}",
        "-XX:+ExitOnOutOfMemoryError",
        "-XX:TieredStopAtLevel=1",
        jvm_font_flag(),
    ]
    if cds_archive.exists():
        jvm_flags.append(f"-XX:SharedArchiveFile={cds_archive}")

    cmd = [
        "java", *jvm_flags, "-jar", jar_path,
        "--input", input_pdf,
        "--structure", structure_json,
        "--output", output_pdf,
        "--lang", lang,
        "--first-row-th-fallback",
        "true" if table_first_row_th_fallback else "false",
        "--tag-mode", "content-preserving-full",
    ]
    if title:
        cmd.extend(["--title", title])

    logger.info(
        "Running single-JVM content-preserving tagger (-Xmx%s, timeout=%ds): %s",
        java_heap, timeout, Path(input_pdf).name,
    )

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as e:
        logger.warning("Failed to launch single-JVM tagger: %s", e)
        return None
    drain = _PipeDrain(proc)

    try:
        # Wait for analysis file to appear
        deadline = time.monotonic() + min(timeout, 300)
        while not os.path.exists(analysis_path):
            if proc.poll() is not None:
                # Process died before producing analysis
                stdout, stderr = drain.result()
                logger.warning(
                    "Single-JVM tagger exited early (rc=%d) before analysis: %s",
                    proc.returncode, stderr[:500] if stderr else "",
                )
                return None
            if time.monotonic() > deadline:
                proc.kill()
                proc.wait(timeout=5)
                logger.warning("Single-JVM tagger: timeout waiting for analysis file")
                return None
            time.sleep(0.2)

        # Read the analysis JSON
        logger.info("  Single-JVM: analysis ready, running matching...")
        with open(analysis_path, encoding="utf-8") as f:
            cs_analysis = json.load(f)

        logger.info(
            "  Single-JVM: %d text runs, %d images across %d pages",
            cs_analysis.get("summary", {}).get("total_text_runs", 0),
            cs_analysis.get("summary", {}).get("total_images", 0),
            cs_analysis.get("pages", 0),
        )

        # Run matching + gap recovery (Python side)
        enriched, matching_summary, stripped_text_pages = \
            _do_matching_and_gap_recovery(
                cs_analysis, structure_json, original_pdf,
                split_reclassified_figures=split_reclassified_figures,
            )

        # Write enriched JSON atomically (write to temp, then rename)
        tmp_enriched = enriched_path + ".tmp"
        with open(tmp_enriched, "w", encoding="utf-8") as f:
            json.dump(enriched, f)
        os.replace(tmp_enriched, enriched_path)
        logger.info("  Single-JVM: enriched JSON written, waiting for tagging...")

        # Wait for Java to finish tagging
        proc.wait(timeout=timeout)
        stdout, stderr = drain.result()

        if stdout:
            for line in stdout.strip().split("\n"):
                logger.info("  [itext] %s", line)
        if stderr:
            for line in stderr.strip().split("\n"):
                logger.warning("  [itext] %s", line)

        if proc.returncode == 0 and Path(output_pdf).exists():
            struct_tree_issues = _validate_struct_tree(
                output_pdf, original_pdf,
                coverage_warn_pct=coverage_warn_pct,
            )
            _sq = _parse_source_quality(stdout or "")
            _maybe_reconcile_widths(output_pdf, _sq)
            return {
                "path": output_pdf,
                "matching_summary": matching_summary,
                "effective_mode": "content-preserving",
                "struct_tree_issues": struct_tree_issues,
                "stripped_text_pages": stripped_text_pages,
                "source_quality_warnings": _sq,
            }

        logger.warning(
            "Single-JVM tagger failed (%s) for %s",
            _describe_tagger_failure(proc.returncode, stderr or ""),
            Path(input_pdf).name,
        )
        return None

    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        logger.warning("Single-JVM tagger timed out after %ds", timeout)
        return None
    except Exception as e:
        # Kill Java process if still running
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        logger.warning("Single-JVM tagger error: %s", e)
        return None
    finally:
        # Clean up handshake files (Java also cleans up, but be safe)
        for p in (analysis_path, enriched_path, enriched_path + ".tmp"):
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


def _invoke_tagger(
    jar_path: str,
    original_pdf: str,
    structure_json: str,
    output_pdf: str,
    lang: str,
    title: Optional[str],
    java_heap: str,
    timeout: int,
    use_serial_gc: bool = False,
    tag_mode: str = "overlay",
    table_first_row_th_fallback: bool = True,
) -> Optional[str]:
    """Low-level: build the java command and run it.

    Stores ``_last_returncode`` and ``_last_stderr`` on the function
    object for the caller to inspect on failure.
    """
    cds_archive = Path(__file__).parent / "itext_tagger.jsa"
    jvm_flags = [
        f"-Xmx{java_heap}",
        "-XX:+ExitOnOutOfMemoryError",
        "-XX:TieredStopAtLevel=1",
        jvm_font_flag(),
    ]
    if use_serial_gc:
        jvm_flags.append("-XX:+UseSerialGC")
    if cds_archive.exists():
        jvm_flags.append(f"-XX:SharedArchiveFile={cds_archive}")

    cmd = [
        "java", *jvm_flags, "-jar", jar_path,
        "--input", original_pdf,
        "--structure", structure_json,
        "--output", output_pdf,
        "--lang", lang,
        "--first-row-th-fallback",
        "true" if table_first_row_th_fallback else "false",
    ]
    if tag_mode == "content-preserving":
        cmd.extend(["--tag-mode", "content-preserving"])
    if title:
        cmd.extend(["--title", title])

    logger.info(
        "Running iText tagger (-Xmx%s, timeout=%ds, mode=%s): %s",
        java_heap, timeout, tag_mode, Path(original_pdf).name,
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                logger.info(f"  [itext] {line}")
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                logger.warning(f"  [itext] {line}")

        if result.returncode == 0 and Path(output_pdf).exists():
            return {"path": output_pdf, "returncode": 0, "stderr": "",
                    "stdout": result.stdout or ""}

        if result.returncode == -9:
            logger.error(
                "iText tagger killed by SIGKILL (likely OOM killer) "
                "for %s", Path(original_pdf).name,
            )
        else:
            logger.error(
                "iText tagger exited with code %d for %s",
                result.returncode, Path(original_pdf).name,
            )
        return {"path": None, "returncode": result.returncode,
                "stderr": result.stderr or ""}

    except subprocess.TimeoutExpired:
        logger.error(
            "iText tagger timed out after %ds for %s",
            timeout, Path(original_pdf).name,
        )
        return {"path": None, "returncode": None, "stderr": ""}
    except Exception as e:
        logger.error(f"iText tagger failed: {e}")
        return {"path": None, "returncode": None, "stderr": str(e)}


