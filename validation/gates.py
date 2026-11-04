#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Automated validation gates for PDF accessibility pipeline.

Six gates that catch regressions automatically:
  Gate 1: Visual pixel-diff — render pages before/after, compare
  Gate 2: Text extraction comparison — pdftotext before/after
  Gate 3: Operator balance — BDC/EMC, BT/ET, q/Q in content streams
  Gate 4: MCID completeness — every MCID in structure ↔ content stream
  Gate 5: Bare container tree — backstop for false-Figure split regression
  Gate 6: Screen reader simulation — structure tree walk, hollow tag
          detection, cross-engine text coverage, alt text / lang checks

Usage:
    from validation.gates import run_all_gates
    results = run_all_gates(source_pdf, tagged_pdf)

Each gate returns: {"gate": str, "passed": bool, "details": dict, "message": str}
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import fitz  # PyMuPDF

from converter.alt_harvest import is_generic_alt
from output_gen.struct_tree_validator import coverage_tokens


# ---------------------------------------------------------------------------
# Screen-reader coverage DIAGNOSTIC thresholds (single source of truth).
#
# These are NOT the verdict gate. The blocking coverage gate is
# ``verdict.min_coverage`` in config.yaml, applied in
# ``audit/standards.compute_wcag_verdict`` to the document-average coverage.
# The constants below only tune diagnostics: which individual pages are flagged
# "low coverage" (informational, shown in the Improvement Guide), and the
# sr_quality label used in overlay/fallback mode (when no element-match data
# exists). Callers in the pipeline override these from the ``verdict:`` config
# block; the defaults here preserve historical behavior.
# ---------------------------------------------------------------------------
DEFAULT_PAGE_LOW_COVERAGE = 0.50   # per-page low-coverage flag (informational)
DEFAULT_SR_POOR_COVERAGE = 0.50    # overlay-mode sr_quality "poor" cutoff
DEFAULT_SR_FAIR_COVERAGE = 0.70    # overlay-mode sr_quality "fair" cutoff


# ---------------------------------------------------------------------------
# Gate 1: Visual pixel-diff
# ---------------------------------------------------------------------------

def gate_visual_diff(
    source_pdf: str,
    tagged_pdf: str,
    dpi: int = 150,
    threshold: float = 2.0,
) -> Dict[str, Any]:
    """Compare rendered pages pixel-by-pixel.

    Renders each page of both PDFs at the given DPI, compares pixel values.
    A difference > threshold% means the tagged PDF looks visibly different,
    indicating content stream corruption.

    Returns gate result dict.
    """
    try:
        src_doc = fitz.open(source_pdf)
        tag_doc = fitz.open(tagged_pdf)
    except Exception as e:
        return _gate_result("visual_diff", False, str(e))

    src_pages = src_doc.page_count
    tag_pages = tag_doc.page_count

    if src_pages != tag_pages:
        src_doc.close()
        tag_doc.close()
        return _gate_result(
            "visual_diff", False,
            f"Page count mismatch: source={src_pages}, tagged={tag_pages}",
            {"source_pages": src_pages, "tagged_pages": tag_pages},
        )

    page_diffs = []
    failed_pages = []

    for i in range(src_pages):
        try:
            src_pix = src_doc[i].get_pixmap(dpi=dpi)
            tag_pix = tag_doc[i].get_pixmap(dpi=dpi)

            # Handle size mismatches (can happen with minor PDF differences)
            if src_pix.width != tag_pix.width or src_pix.height != tag_pix.height:
                page_diffs.append(100.0)
                failed_pages.append(i + 1)
                continue

            src_samples = src_pix.samples
            tag_samples = tag_pix.samples

            if len(src_samples) != len(tag_samples):
                page_diffs.append(100.0)
                failed_pages.append(i + 1)
                continue

            # Count differing pixels
            total_pixels = src_pix.width * src_pix.height
            diff_pixels = 0
            # Compare in chunks for performance (every 3 or 4 bytes = 1 pixel)
            n = src_pix.n  # samples per pixel (3=RGB, 4=RGBA)
            for j in range(0, len(src_samples), n):
                if src_samples[j:j+n] != tag_samples[j:j+n]:
                    diff_pixels += 1

            pct = (diff_pixels / total_pixels * 100) if total_pixels > 0 else 0
            page_diffs.append(round(pct, 2))
            if pct > threshold:
                failed_pages.append(i + 1)

        except Exception as e:
            page_diffs.append(-1)
            failed_pages.append(i + 1)

    src_doc.close()
    tag_doc.close()

    max_diff = max(page_diffs) if page_diffs else 0
    avg_diff = sum(d for d in page_diffs if d >= 0) / max(len(page_diffs), 1)
    passed = len(failed_pages) == 0

    msg = f"Max diff: {max_diff:.2f}%, avg: {avg_diff:.2f}%"
    if failed_pages:
        msg += f" — pages exceeding {threshold}% threshold: {failed_pages}"

    return _gate_result("visual_diff", passed, msg, {
        "max_diff_pct": max_diff,
        "avg_diff_pct": round(avg_diff, 2),
        "failed_pages": failed_pages,
        "page_diffs": page_diffs,
        "threshold": threshold,
    })


# ---------------------------------------------------------------------------
# Gate 2: Text extraction comparison
# ---------------------------------------------------------------------------

def gate_text_extraction(
    source_pdf: str,
    tagged_pdf: str,
    min_ratio: float = 0.85,
    max_ratio: float = 2.0,
    enriched_structure_path: str = None,
) -> Dict[str, Any]:
    """Compare extracted text between source and tagged PDFs.

    Uses pdftotext (poppler) for accurate comparison, falling back to
    PyMuPDF if pdftotext is not available. The tagged PDF should have
    a word count within [min_ratio, max_ratio] of the source.

    PyMuPDF's get_text() can over-count on tagged PDFs because BDC
    markers expose text that Type3 font encoding previously hid.
    pdftotext is more reliable for this comparison.

    Returns gate result dict.
    """
    # Try pdftotext first (more accurate for tagged PDFs)
    src_count, tag_count, empty_pages, method = _extract_with_pdftotext(
        source_pdf, tagged_pdf)

    if src_count is None:
        # Fall back to PyMuPDF
        src_count, tag_count, empty_pages, method = _extract_with_pymupdf(
            source_pdf, tagged_pdf)

    if src_count == 0:
        return _gate_result("text_extraction", True,
            "Source PDF has no extractable text (likely scanned)",
            {"source_words": 0, "tagged_words": tag_count, "ratio": None,
             "method": method})

    ratio = tag_count / src_count if src_count > 0 else 0

    # If pdftotext found 0 words in the tagged PDF, the content may
    # be accessible only via /ActualText in the structure tree (common
    # for scanned docs where invisible OCR text was stripped).  Fall back to structure tree text.
    if tag_count == 0 and src_count > 0:
        try:
            tag_doc = fitz.open(tagged_pdf)
            struct_text = _extract_struct_text_by_page(tag_doc)
            struct_words = sum(len(t.split()) for t in struct_text.values())
            tag_doc.close()
            if struct_words > 0:
                tag_count = struct_words
                method = method + "+struct_tree"
                ratio = tag_count / src_count
                empty_pages = []  # Re-evaluate with struct tree
        except Exception:
            pass

    # --- Load OCR/overlay page sets from enriched structure ---
    # invisible_only_pages: ALL text runs have render_mode=3 (invisible OCR)
    # overlay_pages: 0 visible text runs in content stream but Docling has
    #   elements (text hidden in Form XObjects or invisible OCR).
    # Both indicate that pdftotext may extract text from the source PDF that
    # the tagger correctly strips or can't preserve in the tagged output.
    ocr_stripped_pages: set = set()
    if enriched_structure_path and Path(enriched_structure_path).exists():
        try:
            with open(enriched_structure_path, encoding="utf-8") as _ef:
                _enriched = json.load(_ef)
            ocr_stripped_pages = set(
                _enriched.get("invisible_only_pages", [])
            ) | set(
                _enriched.get("overlay_pages", [])
            )
        except Exception:
            pass

    # --- Per-page scanned page analysis ---
    # For each page flagged as "empty" (source has text, tagged has 0 via
    # pdftotext), check if the tagged PDF's structure tree has text for that
    # page (Category A: accessible via screen reader) or if the source page
    # is image-dominant (Category B: scanned — no accessible text expected)
    # or if the page had only invisible OCR text that the tagger correctly
    # stripped (Category B2). Pages matching none are genuine loss (Category C).
    if empty_pages and tag_count > 0:
        try:
            tag_doc = fitz.open(tagged_pdf)
            src_doc = fitz.open(source_pdf)
            struct_text = _extract_struct_text_by_page(tag_doc)

            adjusted_empty = []
            excluded_src_words = 0
            recovered_tag_words = 0
            scanned_pages = []
            struct_recovered_pages = []

            for page_num in empty_pages:  # 1-indexed
                page_idx = page_num - 1

                # Category A: struct tree has meaningful text for this page
                # Threshold of 2 words (not 5) because sparse pages like
                # title/signature pages may have only "CHAPTER 1" etc.
                struct_words = len(struct_text.get(page_idx, "").split())
                if struct_words >= 2:
                    struct_recovered_pages.append(page_num)
                    recovered_tag_words += struct_words
                    continue

                # Category B: source page is image-dominant (scanned)
                if page_idx < len(src_doc) and _is_image_dominant_page(src_doc[page_idx]):
                    scanned_pages.append(page_num)
                    pw = _extract_page_words_pdftotext(source_pdf, page_num)
                    if pw == 0:
                        pw = len(src_doc[page_idx].get_text("text").split())
                    excluded_src_words += pw
                    continue

                # Category B2: page had only invisible/overlay text (OCR
                # render mode 3, or text hidden in Form XObjects) which
                # the tagger correctly stripped or couldn't preserve.
                # Common in ETD theses with scanned appendices.
                if page_num in ocr_stripped_pages:
                    scanned_pages.append(page_num)
                    pw = _extract_page_words_pdftotext(source_pdf, page_num)
                    if pw == 0:
                        pw = len(src_doc[page_idx].get_text("text").split())
                    excluded_src_words += pw
                    continue

                # Category C: genuine content loss
                adjusted_empty.append(page_num)

            # --- Overlay page ratio adjustment ---
            # Overlay pages (0 visible text runs, text in XObjects or OCR)
            # have source text that pdftotext extracts but the tagger strips.
            # For non-empty overlay pages, subtract the word deficit from the
            # source count so the ratio reflects only pages where pdftotext
            # comparison is meaningful.
            overlay_adjusted = 0
            handled_pages = set(
                struct_recovered_pages + scanned_pages + adjusted_empty
            )
            for page_num in sorted(ocr_stripped_pages):
                if page_num in handled_pages:
                    continue  # already handled above
                page_idx = page_num - 1
                if page_idx >= len(src_doc) or page_idx >= len(tag_doc):
                    continue
                src_pw = len(src_doc[page_idx].get_text("text").split())
                if src_pw == 0:
                    continue
                # Use struct tree words if available (what screen readers see)
                struct_pw = len(struct_text.get(page_idx, "").split())
                tag_pw = len(tag_doc[page_idx].get_text("text").split())
                effective_tag = max(struct_pw, tag_pw)
                if effective_tag < src_pw:
                    deficit = src_pw - effective_tag
                    excluded_src_words += deficit
                    overlay_adjusted += 1

            src_doc.close()
            tag_doc.close()

            if struct_recovered_pages or scanned_pages or overlay_adjusted:
                tag_count_adj = tag_count + recovered_tag_words
                src_count_adj = src_count - excluded_src_words
                if src_count_adj > 0:
                    ratio = tag_count_adj / src_count_adj
                    tag_count = tag_count_adj
                    src_count = src_count_adj
                empty_pages = adjusted_empty
                if struct_recovered_pages:
                    method += f"+struct_recovery({len(struct_recovered_pages)}pp)"
                if scanned_pages:
                    method += f"+scanned_excl({len(scanned_pages)}pp)"
                if overlay_adjusted:
                    method += f"+overlay_adj({overlay_adjusted}pp)"
        except Exception:
            pass  # Fall through to original logic

    # max_ratio is not enforced — more text in the tagged PDF means better
    # accessibility (OCR recovery on image-only pages).  Content duplication
    # bugs are caught by visual_diff and operator_balance gates instead.
    passed = ratio >= min_ratio and len(empty_pages) == 0

    msg = f"Source: {src_count} words, Tagged: {tag_count} words, Ratio: {ratio:.2f} ({method})"
    if empty_pages:
        msg += f" — empty pages in tagged output: {empty_pages}"

    return _gate_result("text_extraction", passed, msg, {
        "source_words": src_count,
        "tagged_words": tag_count,
        "ratio": round(ratio, 3),
        "min_ratio": min_ratio,
        "max_ratio": max_ratio,
        "empty_pages": empty_pages,
        "method": method,
    })


def _extract_with_pdftotext(source_pdf, tagged_pdf):
    """Extract text using pdftotext (poppler) for accurate comparison."""
    try:
        src_result = subprocess.run(
            ["pdftotext", source_pdf, "-"],
            capture_output=True, text=True, timeout=60)
        tag_result = subprocess.run(
            ["pdftotext", tagged_pdf, "-"],
            capture_output=True, text=True, timeout=60)

        if src_result.returncode != 0 or tag_result.returncode != 0:
            return None, None, [], "pdftotext"

        src_words = src_result.stdout.split()
        tag_words = tag_result.stdout.split()

        # Check for empty pages using PyMuPDF (pdftotext doesn't report per-page)
        empty_pages = []
        try:
            src_doc = fitz.open(source_pdf)
            tag_doc = fitz.open(tagged_pdf)
            for i in range(min(src_doc.page_count, tag_doc.page_count)):
                src_text = src_doc[i].get_text("text").strip()
                tag_text = tag_doc[i].get_text("text").strip()
                if src_text and not tag_text:
                    empty_pages.append(i + 1)
            src_doc.close()
            tag_doc.close()
        except Exception:
            pass

        return len(src_words), len(tag_words), empty_pages, "pdftotext"

    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, None, [], "pdftotext"


def _extract_with_pymupdf(source_pdf, tagged_pdf):
    """Extract text using PyMuPDF as fallback."""
    try:
        src_doc = fitz.open(source_pdf)
        tag_doc = fitz.open(tagged_pdf)
    except Exception:
        return 0, 0, [], "pymupdf"

    src_words = []
    tag_words = []
    empty_pages = []

    for i in range(src_doc.page_count):
        src_words.extend(src_doc[i].get_text("text").split())

    for i in range(tag_doc.page_count):
        tag_text = tag_doc[i].get_text("text").strip()
        tag_words.extend(tag_text.split())
        if not tag_text and i < src_doc.page_count:
            if src_doc[i].get_text("text").strip():
                empty_pages.append(i + 1)

    src_doc.close()
    tag_doc.close()

    return len(src_words), len(tag_words), empty_pages, "pymupdf"


def _is_image_dominant_page(page) -> bool:
    """Detect if a page is image-dominant (scanned or image-only).

    Returns True if images cover >50% of the page area, suggesting
    the page is a scan where 'text' is actually invisible OCR overlay.
    """
    page_rect = page.rect
    page_area = page_rect.width * page_rect.height
    if page_area <= 0:
        return False
    image_area = 0.0
    try:
        for img in page.get_images(full=True):
            try:
                for r in page.get_image_rects(img[0]):
                    image_area += r.width * r.height
            except Exception:
                pass
    except Exception:
        return False
    return image_area / page_area > 0.5


def _extract_page_words_pdftotext(pdf_path: str, page_num: int) -> int:
    """Extract word count for a single 1-indexed page using pdftotext."""
    try:
        result = subprocess.run(
            ["pdftotext", "-f", str(page_num), "-l", str(page_num),
             pdf_path, "-"],
            capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return len(result.stdout.split())
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# Gate 3: Operator balance validation
# ---------------------------------------------------------------------------

def gate_operator_balance(
    source_pdf: str,
    tagged_pdf: str,
) -> Dict[str, Any]:
    """Validate that the tagger did not introduce operator imbalances.

    Compares content stream operator balance between source and tagged PDFs.
    Only flags NEW imbalances introduced by tagging — pre-existing imbalances
    in the source PDF are ignored.

    Checks per page:
    - BDC/BMC vs EMC (marked content)
    - BT vs ET (text objects)
    - q vs Q (graphics state)

    Returns gate result dict.
    """
    source_deltas = _get_page_deltas(source_pdf)
    tagged_deltas = _get_page_deltas(tagged_pdf)

    if isinstance(source_deltas, str):
        return _gate_result("operator_balance", False, f"Source: {source_deltas}")
    if isinstance(tagged_deltas, str):
        return _gate_result("operator_balance", False, f"Tagged: {tagged_deltas}")

    new_issues = []
    pre_existing = 0

    all_pages = set(source_deltas.keys()) | set(tagged_deltas.keys())
    for page_num in sorted(all_pages):
        src = source_deltas.get(page_num, (0, 0, 0))
        tag = tagged_deltas.get(page_num, (0, 0, 0))

        # Compare each operator pair: (mc_delta, bt_delta, q_delta)
        labels = [("BDC/BMC-EMC", "marked content"),
                  ("BT-ET", "text objects"),
                  ("q-Q", "graphics state")]
        for idx, (op_name, desc) in enumerate(labels):
            if tag[idx] != 0 and tag[idx] != src[idx]:
                new_issues.append(
                    f"Page {page_num}: {op_name} delta changed "
                    f"from {src[idx]} to {tag[idx]} — "
                    f"tagger introduced {desc} imbalance"
                )
            elif tag[idx] != 0 and tag[idx] == src[idx]:
                pre_existing += 1

    all_ok = len(new_issues) == 0
    msg = "All operators balanced (no new imbalances)" if all_ok else (
        f"{len(new_issues)} new balance issues introduced by tagger"
    )
    if pre_existing > 0:
        msg += f" ({pre_existing} pre-existing in source, ignored)"

    return _gate_result("operator_balance", all_ok, msg, {
        "issues": new_issues[:20],
        "total_new_issues": len(new_issues),
        "pre_existing_issues": pre_existing,
    })


def _get_page_deltas(pdf_path: str):
    """Extract per-page operator balance deltas from a PDF.

    Returns dict of {page_num: (mc_delta, bt_delta, q_delta)} or error string.
    mc_delta = (BDC+BMC) - EMC, bt_delta = BT - ET, q_delta = q - Q.
    """
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return str(e)

    deltas = {}
    for i in range(doc.page_count):
        try:
            raw = doc[i].read_contents()
            if not raw:
                continue
            content = raw.decode("latin-1", errors="replace")

            # Strip string literals so that letters inside text (e.g.
            # the letter "q" in a TJ array like "( q)") are not
            # miscounted as operators.
            stripped = re.sub(r'\([^)]*\)', '', content)   # (...) strings
            stripped = re.sub(r'<[0-9A-Fa-f\s]*>', '', stripped)  # <hex> strings
            stripped = re.sub(r'\[.*?\]', '', stripped)     # [...] TJ arrays

            bdc = len(re.findall(r'\bBDC\b', stripped))
            bmc = len(re.findall(r'\bBMC\b', stripped))
            emc = len(re.findall(r'\bEMC\b', stripped))
            bt = len(re.findall(r'\bBT\b', stripped))
            et = len(re.findall(r'\bET\b', stripped))
            q_save = len(re.findall(r'\bq\b', stripped))
            q_restore = len(re.findall(r'\bQ\b', stripped))

            mc_delta = (bdc + bmc) - emc
            bt_delta = bt - et
            q_delta = q_save - q_restore

            if mc_delta != 0 or bt_delta != 0 or q_delta != 0:
                deltas[i + 1] = (mc_delta, bt_delta, q_delta)
        except Exception:
            pass

    doc.close()
    return deltas


# ---------------------------------------------------------------------------
# Gate 4: MCID completeness
# ---------------------------------------------------------------------------

def gate_mcid_completeness(tagged_pdf: str) -> Dict[str, Any]:
    """Verify MCID consistency between structure tree and content streams.

    Checks:
    1. Every MCID referenced in a structure element exists in a content stream
    2. Every MCID in a content stream is referenced by a structure element
    3. No duplicate MCIDs on the same page

    Returns gate result dict.
    """
    try:
        doc = fitz.open(tagged_pdf)
    except Exception as e:
        return _gate_result("mcid_completeness", False, str(e))

    # --- Collect MCIDs from structure tree ---
    struct_mcids = {}  # page_xref -> set of MCIDs
    se_count = 0

    for xref in range(1, doc.xref_length()):
        try:
            keys = doc.xref_get_keys(xref)
            if "S" not in keys:
                continue
            # Skip artifacts: ``<< /K n /Pg p /S /Artifact >>`` dicts are
            # content-stream artifacts (SR-invisible), not structure content —
            # and are often stray/unlinked.  Counting their /K MCIDs as
            # structure references inflated total_struct_mcids and produced
            # false struct↔content mismatches.
            if doc.xref_get_key(xref, "S")[1] == "/Artifact":
                continue
            type_val = doc.xref_get_key(xref, "Type") if "Type" in keys else ("", "")
            if type_val[1] != "/StructElem" and "S" not in keys:
                continue

            se_count += 1

            # Get /K entry (kids — may contain MCIDs)
            if "K" not in keys:
                continue
            k_val = doc.xref_get_key(xref, "K")

            # Get page reference
            pg_val = doc.xref_get_key(xref, "Pg") if "Pg" in keys else ("", "")
            pg_xref = pg_val[1].split()[0] if pg_val[0] == "xref" else None

            # Parse MCIDs from /K
            mcids = _extract_mcids_from_k(k_val)
            if mcids and pg_xref:
                if pg_xref not in struct_mcids:
                    struct_mcids[pg_xref] = set()
                struct_mcids[pg_xref].update(mcids)

        except Exception:
            pass

    # --- Collect MCIDs from content streams ---
    content_mcids = {}  # page_num -> set of MCIDs

    for i in range(doc.page_count):
        try:
            page = doc[i]
            raw = page.read_contents()
            if not raw:
                continue
            content = raw.decode("latin-1", errors="replace")

            # Find all MCID values in BDC operators
            mcids_in_stream = set()
            for m in re.finditer(r'/MCID\s+(\d+)', content):
                mcids_in_stream.add(int(m.group(1)))

            if mcids_in_stream:
                content_mcids[i + 1] = mcids_in_stream

        except Exception:
            pass

    doc.close()

    # --- Compare ---
    # Flatten struct MCIDs (we have page xrefs, content has page numbers)
    # Since we can't easily map page xref → page number without more work,
    # just compare total counts and check for obvious issues
    total_struct_mcids = sum(len(v) for v in struct_mcids.values())
    total_content_mcids = sum(len(v) for v in content_mcids.values())

    # Check for duplicate MCIDs per page in content streams
    duplicates = []
    for page_num, mcids in content_mcids.items():
        # Duplicates within a page would mean the same MCID appears twice
        # (set already deduplicates, so check raw count)
        pass  # sets handle this — if we need raw count, parse differently

    issues = []
    if total_struct_mcids == 0 and se_count > 0:
        issues.append(f"Structure tree has {se_count} elements but no MCID references")

    if total_content_mcids == 0 and total_struct_mcids > 0:
        issues.append(
            f"Structure tree references {total_struct_mcids} MCIDs "
            f"but content streams contain none"
        )

    if total_struct_mcids > 0 and total_content_mcids > 0:
        ratio = total_content_mcids / total_struct_mcids
        if ratio < 0.8:
            issues.append(
                f"MCID count mismatch: {total_struct_mcids} in structure tree, "
                f"{total_content_mcids} in content streams (ratio: {ratio:.2f})"
            )

    passed = len(issues) == 0
    msg = (f"Structure: {total_struct_mcids} MCIDs, "
           f"Content streams: {total_content_mcids} MCIDs")
    if issues:
        msg += f" — {len(issues)} issues"

    return _gate_result("mcid_completeness", passed, msg, {
        "struct_mcids": total_struct_mcids,
        "content_mcids": total_content_mcids,
        "struct_elements": se_count,
        "pages_with_mcids": len(content_mcids),
        "issues": issues,
    })


def _extract_mcids_from_k(k_val: tuple) -> List[int]:
    """Extract MCID integers from a /K value."""
    mcids = []
    k_type, k_str = k_val

    if k_type == "int":
        # /K is a bare integer — that's the MCID
        try:
            mcids.append(int(k_str))
        except ValueError:
            pass
    elif k_type == "string" or k_type == "array" or k_type == "dict":
        # Parse MCID values from the string representation
        for m in re.finditer(r'/MCID\s+(\d+)', k_str):
            mcids.append(int(m.group(1)))
        # Also check for bare integers in arrays (e.g., /K [0 5 0 R 3])
        # These are MCIDs when they're plain integers in the /K array
        if k_type == "array":
            for m in re.finditer(r'(?<!\d\s)\b(\d+)\b(?!\s+\d+\s+R)', k_str):
                try:
                    val = int(m.group(1))
                    if val not in mcids and val < 10000:  # sanity check
                        mcids.append(val)
                except ValueError:
                    pass

    return mcids


# ---------------------------------------------------------------------------
# Gate 5: Bare container tree
# ---------------------------------------------------------------------------

def gate_bare_tree(tagged_pdf: str) -> Dict[str, Any]:
    """HARD gate: fail when the structure tree has MCIDs but only
    grouping-role SEs (Document/Part/Art/Sect/Div/NonStruct/Private).

    Detection-only backstop for the false-Figure split (spec
    2026-08-07-bare-div-*): a bare Document>Div tree reads as untagged
    to Ally and screen readers.
    """
    try:
        from output_gen.struct_tree_validator import validate_tagged_pdf
        issues = validate_tagged_pdf(tagged_pdf)  # no source: coverage skipped
    except Exception as e:
        return _gate_result("bare_tree", False, f"validator error: {e}")

    unreadable = [i for i in issues if i["check"] in ("file-missing", "open-failed")]
    if unreadable:
        return _gate_result(
            "bare_tree", False, unreadable[0]["message"],
            details={"issues": unreadable})

    bare = [i for i in issues if i["check"] == "bare_container_tree"]
    if bare:
        return _gate_result(
            "bare_tree", False, bare[0]["message"],
            details={"issues": bare})
    return _gate_result("bare_tree", True, "content-role SEs present")


# ---------------------------------------------------------------------------
# Gate: Docling structure yield (tripwire for OCR-invisible inputs)
# ---------------------------------------------------------------------------

def gate_structure_yield(
    docling_elements: Optional[int],
    docling_pages: Optional[int],
    text_classification: Optional[str],
    docling_pages_with_text: Optional[int] = None,
) -> Dict[str, Any]:
    """HARD gate: a scanned document for which Docling produced implausibly
    little structure (< 1 element/page, pages >= 3; fully_scanned, or mixed
    that is >= 80% textless).

    Catches input classes invisible to Docling's OCR triggers — e.g.
    vectorized scans (glyphs as filled paths: no text, no bitmaps), which
    otherwise sail through every other gate: coverage is self-referential on
    textless docs and visual diff sees no change.

    Takes the PRISTINE export-time counts from conversion stats — never a
    path to docling_structure.json, which Phase 1b/1c recovery mutates in
    place before gates run and would mask exactly this failure. Rule shared
    with the outcome taxonomy (``check_docling_yield_low``); validated
    against 645 historical conversions with zero false positives.

    Skips (passes) when inputs are unavailable, e.g. --skip-docling runs.
    """
    name = "structure_yield"
    if docling_elements is None or docling_pages is None or not text_classification:
        return _gate_result(
            name, True, "skipped (no conversion stats / classification)")

    from logging_util.failure_taxonomy import check_docling_yield_low
    if check_docling_yield_low(text_classification, docling_elements,
                               docling_pages, docling_pages_with_text):
        return _gate_result(
            name, False,
            f"Docling yield {docling_elements} elements / {docling_pages} pages "
            f"({docling_elements / docling_pages:.2f}/page) on a "
            f"{text_classification} document — an input class Docling produced "
            f"almost no structure for (headings/tables/lists will be missing)",
            details={"elements": docling_elements, "pages": docling_pages,
                     "text_classification": text_classification,
                     "pages_with_text": docling_pages_with_text})
    return _gate_result(
        name, True,
        f"{docling_elements} elements / {docling_pages} pages ({text_classification})")


# ---------------------------------------------------------------------------
# Gate 6: Screen reader simulation — helpers
# ---------------------------------------------------------------------------


def _parse_balanced_paren(obj_str: str, start: int) -> str:
    """Parse balanced parenthesized PDF literal string starting after '('.

    Handles nested parentheses and escape sequences (``\\)``, ``\\(``,
    ``\\\\``).  Returns the content between the opening and closing parens.
    """
    depth = 1
    i = start
    while i < len(obj_str) and depth > 0:
        ch = obj_str[i]
        if ch == "\\" and i + 1 < len(obj_str):
            i += 2  # skip escaped char entirely
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    return obj_str[start : i - 1]


def _get_actual_text_from_xref(doc, xref: int) -> Optional[str]:
    """Extract /ActualText or /Alt from a structure element via xref.

    Handles hex-encoded UTF-16BE and literal parenthesized strings.
    Adapted from output_gen/struct_tree_validator.py:_get_actual_text().
    """
    try:
        obj_str = doc.xref_object(xref, compressed=False)
    except Exception:
        return None

    # Hex-encoded /ActualText (UTF-16BE with BOM)
    hex_m = re.search(r"/ActualText\s*<([0-9a-fA-F\s]+)>", obj_str, re.DOTALL)
    if hex_m:
        h = hex_m.group(1).replace("\n", "").replace(" ", "")
        try:
            bs = bytes.fromhex(h)
            if bs[:2] == b"\xfe\xff":
                return bs[2:].decode("utf-16-be", errors="replace")
            return bs.decode("utf-8", errors="replace")
        except ValueError:
            pass

    # Literal /ActualText in parentheses (balanced-paren parser)
    lit_m = re.search(r"/ActualText\s*\(", obj_str)
    if lit_m:
        text = _parse_balanced_paren(obj_str, lit_m.end())
        if text:
            return text

    # Literal /Alt (balanced-paren parser — handles nested parens in alt text)
    alt_lit = re.search(r"/Alt\s*\(", obj_str)
    if alt_lit:
        text = _parse_balanced_paren(obj_str, alt_lit.end())
        if text.strip():
            return text

    # Hex /Alt
    alt_hex = re.search(r"/Alt\s*<([0-9a-fA-F\s]+)>", obj_str, re.DOTALL)
    if alt_hex:
        h = alt_hex.group(1).replace("\n", "").replace(" ", "")
        try:
            bs = bytes.fromhex(h)
            if bs[:2] == b"\xfe\xff":
                return bs[2:].decode("utf-16-be", errors="replace")
        except ValueError:
            pass

    return None


def _get_child_se_xrefs(doc, obj_str: str) -> List[int]:
    """Extract child structure element xrefs from /K array.

    Adapted from output_gen/struct_tree_validator.py:_get_child_struct_elems().
    """
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


def _extract_text_from_content_block(block: str) -> str:
    """Extract readable text from a PDF content stream BDC/EMC block.

    Parses literal strings from Tj/TJ operators.  TJ segments are often
    sub-word fragments (e.g. ['Tot', 'a', 'l ']) so they are joined without
    separators.
    """
    parts = []
    # Literal strings in TJ arrays and Tj ops: (text)
    # Regex handles escaped chars (\), \(, \\) inside the string.
    for m in re.finditer(r"\(([^()\\]*(?:\\.[^()\\]*)*)\)", block):
        s = m.group(1)
        if s:
            s = s.replace("\\)", ")").replace("\\(", "(").replace("\\\\", "\\")
            parts.append(s)
    # Hex strings: <hex> (rare in our tagger output, but handle as fallback)
    for h in re.findall(r"<([0-9a-fA-F]{2,})>", block):
        try:
            bs = bytes.fromhex(h)
            decoded = bs.decode("latin-1", errors="replace")
            if decoded.strip():
                parts.append(decoded)
        except ValueError:
            pass
    return "".join(parts).strip()


def _get_mcids_from_k(obj_str: str) -> List[int]:
    """Extract bare integer MCIDs from /K in a structure element.

    /K 691           → [691]
    /K [0 1]         → [0, 1]
    /K [0 57429 0 R] → [0]  (indirect refs excluded)
    /K <</Type /MCR /MCID 5 /Pg 1 0 R>> → [5]
    """
    mcids: List[int] = []

    # Check for /K array
    k_arr = re.search(r"/K\s*\[([^\]]+)\]", obj_str)
    if k_arr:
        content = k_arr.group(1)
        # Extract MCIDs from MCR dicts: << ... /MCID N ... >>
        for m in re.finditer(r"<<[^>]*/MCID\s+(\d+)[^>]*>>", content):
            mcids.append(int(m.group(1)))
        # Remove indirect refs and dict blocks, then find bare integers
        cleaned = re.sub(r"\d+\s+0\s+R", "", content)
        cleaned = re.sub(r"<<[^>]*>>", "", cleaned)
        for m in re.findall(r"\b(\d+)\b", cleaned):
            mcids.append(int(m))
        return mcids

    # Check for /K with MCR dict (single, not array)
    mcr_dict = re.search(r"/K\s*<<[^>]*/MCID\s+(\d+)[^>]*>>", obj_str)
    if mcr_dict:
        return [int(mcr_dict.group(1))]

    # Single /K N (not /K N 0 R — negative lookahead excludes indirect refs)
    k_single = re.search(r"/K\s+(\d+)(?!\s+0\s+R)\b", obj_str)
    if k_single:
        return [int(k_single.group(1))]

    return mcids


def _build_mcid_text_index(doc) -> Dict[int, Dict[int, str]]:
    """Build page_idx → {mcid → text} index from content streams.

    Pre-parses BDC blocks with /MCID markers on every page so that
    _extract_struct_text_by_page can look up MCR text without re-parsing
    content streams per structure element.
    """
    index: Dict[int, Dict[int, str]] = {}
    for page_idx in range(len(doc)):
        try:
            page = doc[page_idx]
            raw = page.read_contents()
        except Exception:
            continue
        if not raw:
            continue
        content = raw.decode("latin-1", errors="replace")
        page_mcids: Dict[int, str] = {}
        for m in re.finditer(
            r"<<\s*/MCID\s+(\d+)\s*>>\s*BDC(.*?)EMC",
            content,
            re.DOTALL,
        ):
            mcid = int(m.group(1))
            text = _extract_text_from_content_block(m.group(2))
            if text:
                page_mcids[mcid] = text
        if page_mcids:
            index[page_idx] = page_mcids
    return index


def _extract_struct_text_by_page(doc) -> Dict[int, str]:
    """Walk the structure tree and collect text per page.

    Returns a dict mapping page index (0-based) to concatenated text that a
    screen reader would see when traversing the structure tree.  Text is
    extracted from:
      1. /ActualText on structure elements (our tagger sets this on P, H1, etc.)
      2. /Alt on Figure elements
      3. MCR fallback — when /K contains bare integer MCIDs (used by TD/TH),
         text is extracted from the corresponding BDC/EMC blocks in the
         content stream via a pre-built MCID index.
    Artifact content is excluded.
    """
    page_xref_to_idx = {}
    for i in range(len(doc)):
        page_xref_to_idx[doc[i].xref] = i

    # Find StructTreeRoot xref from catalog
    try:
        cat_str = doc.xref_object(doc.pdf_catalog(), compressed=False)
    except Exception:
        return {}

    root_m = re.search(r"/StructTreeRoot\s+(\d+)\s+0\s+R", cat_str)
    if not root_m:
        return {}
    root_xref = int(root_m.group(1))

    # Find Document element (direct child of StructTreeRoot)
    try:
        root_str = doc.xref_object(root_xref, compressed=False)
    except Exception:
        return {}

    # Pre-build MCID → text index from content streams (for MCR fallback)
    mcid_index = _build_mcid_text_index(doc)

    # Collect text per page
    text_by_page: Dict[int, List[str]] = {}

    def _resolve_page_idx(obj_str: str, inherited_page: Optional[int]) -> Optional[int]:
        """Resolve page index from /Pg reference in an object string."""
        pg_m = re.search(r"/Pg\s+(\d+)\s+0\s+R", obj_str)
        if pg_m:
            pg_xref = int(pg_m.group(1))
            return page_xref_to_idx.get(pg_xref, inherited_page)
        return inherited_page

    def _walk(xref: int, page_idx: Optional[int] = None) -> None:
        """Recursively walk structure tree, collecting text per page."""
        try:
            obj_str = doc.xref_object(xref, compressed=False)
        except Exception:
            return

        page_idx = _resolve_page_idx(obj_str, page_idx)

        child_xrefs = _get_child_se_xrefs(doc, obj_str)

        if child_xrefs:
            # Non-leaf: recurse into children
            for cx in child_xrefs:
                _walk(cx, page_idx)
        else:
            # Leaf element — try /ActualText or /Alt first
            text = _get_actual_text_from_xref(doc, xref)
            if text and page_idx is not None:
                text_by_page.setdefault(page_idx, []).append(text)
                return

            # MCR fallback: TD/TH and other elements without /ActualText
            # carry text via MCR — bare integer MCIDs in /K pointing to
            # BDC/EMC blocks in the content stream.
            if page_idx is not None:
                mcids = _get_mcids_from_k(obj_str)
                for mcid in mcids:
                    mcr_text = mcid_index.get(page_idx, {}).get(mcid, "")
                    if mcr_text:
                        text_by_page.setdefault(page_idx, []).append(mcr_text)

    # Walk from root's children
    root_children = _get_child_se_xrefs(doc, root_str)
    if root_children:
        for cx in root_children:
            _walk(cx)
    else:
        # StructTreeRoot itself might be the Document element
        _walk(root_xref)

    # Concatenate text lists into strings
    result = {page: " ".join(texts) for page, texts in text_by_page.items()}

    # ── Per-page hybrid fallback ──────────────────────────────────────
    # Content-preserving pages have no /ActualText — text is only in MCR
    # BDC/EMC blocks.  The regex parser (_extract_text_from_content_block)
    # cannot decode CID hex strings or resolve font ToUnicode CMaps, so
    # it returns garbled or empty text for those pages.
    #
    # For content-preserving pages the visible content IS the MCR content
    # (no invisible overlay), so PyMuPDF's get_text() is a valid proxy
    # for what a screen reader would see via MCR.  Replace struct-tree
    # text with get_text() on pages where regex extraction is poor.
    for page_idx in range(len(doc)):
        source_text = doc[page_idx].get_text("text").strip()
        source_words = coverage_tokens(source_text)
        if len(source_words) < 5:
            continue  # too few words to judge quality

        struct_text = result.get(page_idx, "")
        struct_words = set(struct_text.lower().split()) if struct_text else set()
        overlap = len(source_words & struct_words)
        coverage = overlap / len(source_words)

        if coverage < 0.50:
            # Check that page actually has MCIDs (content-preserving tagging)
            try:
                raw = doc[page_idx].read_contents()
                if raw:
                    content = raw.decode("latin-1", errors="replace")
                    if re.search(r'/MCID\s+\d+', content):
                        result[page_idx] = source_text
            except Exception:
                pass

    return result


def _page_coverage_core(
    page_num: int,
    source_words: set,
    source_word_count: int,
    struct_text_by_page: Dict[int, str],
    tagged_doc,
    use_struct_tree: bool,
    *,
    mcid_gated_fallback: bool,
    decoded_struct_by_page: Optional[Dict[int, str]] = None,
):
    """Shared per-page coverage computation for the two SR functions.

    This is the single source of the "what fraction of source visual words
    appear in the structure-tree text" metric, so ``gate_screen_reader_sim``
    and ``quick_screen_reader_check`` produce the SAME coverage number.

    Args:
        page_num: 0-based page index.
        source_words / source_word_count: tokenized source page text.
        struct_text_by_page: output of ``_extract_struct_text_by_page``.
        tagged_doc: open PyMuPDF document for the tagged PDF.
        use_struct_tree: caller's resolved policy (struct-tree text vs get_text).
        mcid_gated_fallback: in the non-struct-tree branch, only fall back to
            ``get_text()`` when the page has MCIDs (gate behavior); when False,
            fall back unconditionally (quick-check behavior). Only affects
            third-party PDFs — production content-preserving output always runs
            the ``use_struct_tree=True`` path.

    Returns:
        ``(coverage, tagged_text, tagged_words)`` where coverage is
        ``overlap / source_word_count`` (1.0 for a blank source page).
    """
    if use_struct_tree:
        tagged_text = struct_text_by_page.get(page_num, "").strip()
        # Per-page MCR fallback: content-preserving pages skip /ActualText and
        # rely on BDC/EMC MCR content.  The regex parser cannot decode CID hex
        # strings — only PyMuPDF's get_text() resolves font ToUnicode CMaps.
        # If struct-tree extraction is garbled (low overlap) and the page has
        # MCIDs, fall back to get_text() for this page.
        if source_word_count > 5:
            tagged_word_count = len(tagged_text.split()) if tagged_text else 0
            struct_words_set = coverage_tokens(tagged_text)
            struct_overlap = len(source_words & struct_words_set)
            struct_coverage = struct_overlap / source_word_count if source_word_count else 1.0
            if struct_coverage < 0.20 or tagged_word_count < source_word_count * 0.3:
                try:
                    tagged_page = tagged_doc[page_num]
                    raw = tagged_page.read_contents()
                    if raw:
                        content = raw.decode("latin-1", errors="replace")
                        if re.search(r'/MCID\s+\d+', content):
                            tagged_text = tagged_page.get_text("text").strip()
                except Exception:
                    pass
    elif mcid_gated_fallback:
        tagged_text = ""
        try:
            tagged_page = tagged_doc[page_num]
            raw = tagged_page.read_contents()
            if raw:
                content = raw.decode("latin-1", errors="replace")
                if re.search(r'/MCID\s+\d+', content):
                    tagged_text = tagged_page.get_text("text").strip()
        except Exception:
            pass
    else:
        tagged_text = tagged_doc[page_num].get_text("text").strip()

    tagged_words = coverage_tokens(tagged_text)
    if source_word_count > 0:
        overlap = source_words & tagged_words
        coverage = len(overlap) / source_word_count
    else:
        coverage = 1.0  # blank page = full coverage

    # Per-page max with the ToUnicode-decoded, artifact-excluded extractor.
    # The regex path above cannot decode CID/Type0 text (reads as gibberish),
    # so it under-reports coverage on fully-tagged CID-font docs.  The decoded
    # extractor recovers that text; we take the MAX so coverage can only RISE —
    # decoded is unreliable on OCG/broken-font docs (returns little), and max
    # leaves those at the regex value, guaranteeing no regression.
    if decoded_struct_by_page is not None and source_word_count > 0:
        decoded_text = (decoded_struct_by_page.get(page_num) or "").strip()
        if decoded_text:
            decoded_words = coverage_tokens(decoded_text)
            decoded_cov = len(source_words & decoded_words) / source_word_count
            if decoded_cov > coverage:
                coverage = decoded_cov
                tagged_text = decoded_text
                tagged_words = decoded_words

    return coverage, tagged_text, tagged_words


# ---------------------------------------------------------------------------
# Sub-check E: MCR linkage — orphaned /ActualText, disconnected MCIDs
# ---------------------------------------------------------------------------

def _check_mcr_linkage(tagged_pdf_path: str) -> Dict[str, Any]:
    """Check MCR linkage and /ActualText quality in a tagged PDF.

    Sub-check E for gate_screen_reader_sim. Detects:
      E1: Orphaned /ActualText — elements with text but no MCR linkage
      E2: Disconnected MCIDs — content stream MCIDs not in structure tree
      E3: Garbled /ActualText — text quality heuristic

    Phase 0 finding: /ActualText BLOCKS AROL from reading content.
    Only elements with MCR and NO /ActualText are AROL-readable.
    Elements with /ActualText (with or without MCR) are AROL-invisible.

    Returns dict with counts and percentages.
    """
    doc = fitz.open(tagged_pdf_path)

    # --- Build page xref → page index map ---
    page_xref_to_idx = {}
    for i in range(len(doc)):
        page_xref_to_idx[doc[i].xref] = i

    # --- Find StructTreeRoot ---
    empty_result = {
        "arol_invisible": 0, "total_text_leaves": 0,
        "arol_invisible_pct": 0.0, "disconnected_mcids": 0,
        "garbled_actual_text": 0, "garbled_actual_text_pct": 0.0,
        "pages_with_issues": [],
    }
    try:
        cat_str = doc.xref_object(doc.pdf_catalog(), compressed=False)
    except Exception:
        doc.close()
        return empty_result

    root_m = re.search(r"/StructTreeRoot\s+(\d+)\s+0\s+R", cat_str)
    if not root_m:
        doc.close()
        return empty_result

    root_xref = int(root_m.group(1))
    root_str = doc.xref_object(root_xref, compressed=False)

    # --- Walk structure tree, classify each leaf ---
    arol_invisible = 0
    garbled = 0
    total_text_leaves = 0
    struct_mcids: set = set()  # (page_idx, mcid) pairs from structure tree
    pages_with_issues: set = set()

    def _is_garbled(text: str) -> bool:
        """Heuristic: text is garbled if mostly short fragments."""
        words = text.split()
        if len(words) < 3:
            return False  # Too short to assess
        short = sum(1 for w in words if len(w) <= 2)
        single = sum(1 for w in words if len(w) == 1)
        # Control characters (except tab/newline)
        has_control = any(
            ord(c) < 0x20 and c not in ('\t', '\n', '\r') for c in text
        )
        return (short / len(words) > 0.50
                or single / len(words) > 0.40
                or has_control)

    def _walk_leaf(xref: int, inherited_page: int = None) -> None:
        nonlocal arol_invisible, garbled, total_text_leaves
        try:
            obj_str = doc.xref_object(xref, compressed=False)
        except Exception:
            return

        if "/Type /StructElem" not in obj_str:
            return

        # Resolve page
        pg_m = re.search(r"/Pg\s+(\d+)\s+0\s+R", obj_str)
        page_idx = (page_xref_to_idx.get(int(pg_m.group(1)))
                    if pg_m else inherited_page)

        child_xrefs = _get_child_se_xrefs(doc, obj_str)
        if child_xrefs:
            for cx in child_xrefs:
                _walk_leaf(cx, page_idx)
            return

        # --- Leaf element ---
        at_text = _get_actual_text_from_xref(doc, xref)
        mcids = _get_mcids_from_k(obj_str)
        has_mcr = len(mcids) > 0 and page_idx is not None
        has_at = at_text is not None

        # Check for /ActualText specifically (NOT /Alt).
        # /Alt on Figure/Formula is PDF/UA-required and does NOT block AROL.
        # Only /ActualText blocks AROL from reading MCR content.
        has_actual_text_only = bool(
            re.search(r"/ActualText\s*[<(]", obj_str)
        )

        if not has_at and not has_mcr:
            return  # No text content at all — skip

        total_text_leaves += 1

        # Collect struct MCIDs for E2 disconnected check
        if has_mcr and page_idx is not None:
            for mcid in mcids:
                struct_mcids.add((page_idx, mcid))

        # Phase 0 finding: /ActualText blocks AROL from reading content.
        # ANY element with /ActualText is AROL-invisible, even if it
        # also has MCR (P3 experiment confirmed this).
        # /Alt does NOT block AROL — only count /ActualText.
        if has_actual_text_only:
            arol_invisible += 1
            if page_idx is not None:
                pages_with_issues.add(page_idx + 1)  # 1-based
            else:
                pages_with_issues.add(-1)

            # Check text quality
            if _is_garbled(at_text):
                garbled += 1

    root_children = _get_child_se_xrefs(doc, root_str)
    if root_children:
        for cx in root_children:
            _walk_leaf(cx)
    else:
        _walk_leaf(root_xref)

    # --- E2: Disconnected MCIDs ---
    content_mcids: set = set()
    mcid_index = _build_mcid_text_index(doc)
    for page_idx, mcid_map in mcid_index.items():
        for mcid in mcid_map:
            content_mcids.add((page_idx, mcid))
    disconnected = len(content_mcids - struct_mcids)

    doc.close()

    invisible_pct = (
        round(arol_invisible / total_text_leaves * 100, 1)
        if total_text_leaves > 0 else 0.0
    )
    garbled_pct = (
        round(garbled / total_text_leaves * 100, 1)
        if total_text_leaves > 0 else 0.0
    )

    return {
        "arol_invisible": arol_invisible,
        "total_text_leaves": total_text_leaves,
        "arol_invisible_pct": invisible_pct,
        "disconnected_mcids": disconnected,
        "garbled_actual_text": garbled,
        "garbled_actual_text_pct": garbled_pct,
        "pages_with_issues": sorted(pages_with_issues),
    }


def _collect_struct_text(blocks, parent_tag, out):
    """Walk a PyMuPDF TEXT_COLLECT_STRUCTURE block tree.

    Recurses into struct blocks (type=2) and collects span text from
    text blocks (type=0) whose containing struct ancestor is not
    /Artifact. Text comes back with the font's /ToUnicode CMap already
    applied — the same path AROL uses.
    """
    for b in blocks:
        btype = b.get("type")
        if btype == 2:
            tag = b.get("std") or b.get("raw") or parent_tag
            _collect_struct_text(b.get("blocks", []), tag, out)
        elif btype == 0:
            if parent_tag and parent_tag != "Artifact":
                for line in b.get("lines", []):
                    for span in line.get("spans", []):
                        text = span.get("text")
                        if text:
                            out.append(text)


def _struct_decoded_text_by_page(doc) -> Dict[int, str]:
    """Per-page tagged text decoded via /ToUnicode, with /Artifact excluded.

    Uses PyMuPDF's TEXT_COLLECT_STRUCTURE flag + _collect_struct_text — the same
    AROL-equivalent path as _check_extraction_divergence.  Unlike the regex MCR
    parser in _extract_struct_text_by_page, this resolves font ToUnicode CMaps, so
    it decodes CID/Type0 text the regex returns as gibberish.  Used by
    _page_coverage_core to take a per-page max against the regex result: the
    decoded value only ever *raises* coverage, so it rescues under-decoded
    false-positives without regressing OCG/broken-font docs where this extractor
    itself returns little.
    """
    out: Dict[int, str] = {}
    for i in range(doc.page_count):
        try:
            sd = doc[i].get_text("dict", flags=fitz.TEXT_COLLECT_STRUCTURE)
            chunks: List[str] = []
            _collect_struct_text(sd.get("blocks", []), None, chunks)
            text = " ".join(chunks).strip()
            if text:
                out[i] = text
        except Exception:
            continue  # omit page → max() falls back to the regex result
    return out


def _check_extraction_divergence(tagged_pdf_path: str) -> Dict[str, Any]:
    """Compare structure-accessible text vs total extractable text.

    Sub-check F for gate_screen_reader_sim. Measures how much extractable
    text is NOT reachable via the structure tree.

    High divergence = text exists in the PDF but screen readers can't
    reach it through the tag tree.

    Uses PyMuPDF's TEXT_COLLECT_STRUCTURE flag so struct-tree text is
    decoded via each font's /ToUnicode CMap — the same path AROL uses.
    The previous implementation parsed raw content-stream bytes, which
    read as gibberish for any CID font and falsely inflated divergence
    to ~1.0 on every CID-using page.
    """
    doc = fitz.open(tagged_pdf_path)

    empty_result = {
        "avg_divergence": 0.0, "max_divergence": 0.0,
        "pages_with_high_divergence": [],
        "divergence_by_page": {},
    }

    # No struct tree → no meaningful divergence comparison.
    try:
        cat_str = doc.xref_object(doc.pdf_catalog(), compressed=False)
    except Exception:
        doc.close()
        return empty_result
    if not re.search(r"/StructTreeRoot\s+\d+\s+0\s+R", cat_str):
        doc.close()
        return empty_result

    def _normalize(text: str) -> set:
        text = text.lower()
        text = re.sub(r"[^\w\s]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return set(text.split()) if text else set()

    divergence_by_page = {}
    total_extractable = 0
    total_inaccessible = 0

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        extractable = _normalize(page.get_text())
        if not extractable:
            continue

        try:
            sd = page.get_text("dict", flags=fitz.TEXT_COLLECT_STRUCTURE)
        except Exception:
            sd = None

        chunks: List[str] = []
        if sd:
            _collect_struct_text(sd.get("blocks", []), None, chunks)
        accessible = _normalize(" ".join(chunks))

        overlap = len(accessible & extractable)
        div = 1.0 - overlap / len(extractable)
        divergence_by_page[page_idx + 1] = round(div, 3)  # 1-based pages
        total_extractable += len(extractable)
        total_inaccessible += len(extractable) - overlap

    doc.close()

    divs = list(divergence_by_page.values())
    # Word-weighted divergence: the fraction of the document's TOTAL
    # extractable text that is not reachable via the structure tree.  This is
    # the correct reading of "how much extractable text is inaccessible" and
    # prevents a tiny artifact-only page (e.g. a lone page number, which
    # PDF/UA correctly excludes from the tag tree) from dominating an
    # unweighted per-page average on short documents.  A genuinely
    # inaccessible large page still contributes proportionally, so real gaps
    # are still caught.
    avg_div = total_inaccessible / total_extractable if total_extractable else 0.0
    max_div = max(divs) if divs else 0.0
    high_div_pages = [p for p, d in divergence_by_page.items() if d > 0.30]

    return {
        "avg_divergence": round(avg_div, 3),
        "max_divergence": round(max_div, 3),
        "pages_with_high_divergence": high_div_pages,
        "divergence_by_page": divergence_by_page,
    }


# ---------------------------------------------------------------------------
# Gate 6: Screen reader simulation
# ---------------------------------------------------------------------------

def gate_screen_reader_sim(
    source_pdf: str,
    tagged_pdf: str,
    coverage_threshold: float = DEFAULT_PAGE_LOW_COVERAGE,
) -> Dict[str, Any]:
    """Simulate what a screen reader would see by inspecting the tagged PDF.

    Sub-checks:
      A. Structure basics — /MarkInfo Marked, /StructTreeRoot, /Lang present
      B. Structure tree walk — extract reading-order text from the tag tree,
         detect hollow tags (structure elements with no content)
      C. Cross-engine text coverage — compare structure-tree text against
         PyMuPDF and (optionally) pdfminer.six extraction to catch CMap /
         encoding failures
      D. Alt text — /Figure elements without /Alt
      E. MCR linkage — orphaned /ActualText, disconnected MCIDs, garbled text
      F. Extraction divergence — structure-accessible vs total extractable text

    The gate PASSES when:
      - /MarkInfo Marked is true and /StructTreeRoot exists
      - /Lang is set on the document
      - Fewer than 10% of leaf structure elements are hollow
      - Per-page coverage of visual text by structure-tree text >= threshold
      - All /Figure elements have /Alt text

    Returns gate result dict.
    """
    try:
        import pypdf
        reader = pypdf.PdfReader(tagged_pdf)
    except ImportError:
        return _gate_result("screen_reader_sim", True,
            "pypdf not installed — gate skipped", {"available": False})
    except Exception as e:
        return _gate_result("screen_reader_sim", False,
            f"Cannot open tagged PDF: {e}")

    issues = []
    details: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Sub-check A: Structure basics
    # ------------------------------------------------------------------
    root = reader.trailer.get("/Root", {})
    if hasattr(reader, "root_object"):
        root = reader.root_object  # pypdf >= 4

    # /MarkInfo
    mark_info = root.get("/MarkInfo", {})
    marked = bool(mark_info.get("/Marked", False)) if mark_info else False
    details["marked"] = marked
    if not marked:
        issues.append("Document /MarkInfo.Marked is not true")

    # /StructTreeRoot
    struct_tree_root = root.get("/StructTreeRoot")
    has_struct_tree = struct_tree_root is not None
    details["has_struct_tree"] = has_struct_tree
    if not has_struct_tree:
        issues.append("No /StructTreeRoot — screen reader has no structure")
        # Nothing else to check without a structure tree
        return _gate_result("screen_reader_sim", False,
            f"{len(issues)} issues: {'; '.join(issues)}", details)

    # /Lang
    doc_lang = root.get("/Lang")
    details["document_lang"] = str(doc_lang) if doc_lang else None
    if not doc_lang:
        issues.append("No document /Lang — screen reader cannot select TTS voice")

    # ------------------------------------------------------------------
    # Sub-check B: Structure tree walk — reading order + hollow tags
    # ------------------------------------------------------------------
    total_leaves = 0
    hollow_leaves = 0
    figures_total = 0
    figures_missing_alt = 0

    # Map page objects to indices for resolving /Pg references
    page_obj_map = {}
    for i, page in enumerate(reader.pages):
        page_obj_map[id(page.indirect_reference)] = i
        # Also try the resolved object
        page_obj_map[id(page)] = i

    def _resolve(obj):
        """Resolve indirect references."""
        if hasattr(obj, "get_object"):
            return obj.get_object()
        return obj

    def _walk_tree(node, page_idx=None):
        """Recursively walk structure tree, collecting text and checking tags."""
        nonlocal total_leaves, hollow_leaves, figures_total, figures_missing_alt

        node = _resolve(node)
        if not hasattr(node, "get"):
            return

        # Determine page index from /Pg if present
        pg_ref = node.get("/Pg")
        if pg_ref is not None:
            pg_obj = _resolve(pg_ref)
            page_idx = page_obj_map.get(id(pg_ref), page_obj_map.get(id(pg_obj), page_idx))

        s_type = str(node.get("/S", ""))

        # Check /Figure for /Alt
        if s_type == "/Figure":
            figures_total += 1
            # Presence of the key is not a description.  /Alt () and
            # /Alt (Figure) both leave the image undescribed (G-07).
            # node is a pypdf DictionaryObject: use .get("/Alt"), not .Alt.
            alt_value = node.get("/Alt")
            if alt_value is None or is_generic_alt(str(alt_value)):
                figures_missing_alt += 1
                issues.append(f"Figure missing /Alt text (page ~{page_idx})")

        # Process /K (kids)
        k = node.get("/K")
        if k is None:
            return

        kids = k if isinstance(k, list) else [k]

        for kid in kids:
            kid = _resolve(kid)

            if isinstance(kid, int):
                # Bare integer = MCID reference → this is a leaf
                total_leaves += 1
                # We can't extract text from MCID alone via pypdf;
                # we'll check coverage via extraction engines below
            elif hasattr(kid, "get"):
                kid_type = kid.get("/Type")
                if kid_type is not None and str(kid_type) == "/OBJR":
                    # Object reference (annotation, form field) — not text
                    pass
                elif kid_type is not None and str(kid_type) == "/MCR":
                    # Marked content reference dict
                    total_leaves += 1
                    mcr_pg = kid.get("/Pg")
                    if mcr_pg is not None:
                        mcr_pg_obj = _resolve(mcr_pg)
                        pg_for_mcr = page_obj_map.get(
                            id(mcr_pg), page_obj_map.get(id(mcr_pg_obj), page_idx))
                    else:
                        pg_for_mcr = page_idx
                elif "/S" in kid:
                    # Nested structure element — recurse
                    _walk_tree(kid, page_idx)
                else:
                    # Could be a bare MCID dict or something else
                    if "/MCID" in kid:
                        total_leaves += 1
            elif isinstance(kid, str):
                # Actual text content in the structure tree (rare)
                pass

    try:
        tree_root = _resolve(struct_tree_root)
        root_kids = tree_root.get("/K")
        if root_kids is not None:
            if isinstance(root_kids, list):
                for kid in root_kids:
                    _walk_tree(kid)
            else:
                _walk_tree(root_kids)
    except Exception as e:
        issues.append(f"Error walking structure tree: {e}")

    details["total_leaf_elements"] = total_leaves
    details["figures_total"] = figures_total
    details["figures_missing_alt"] = figures_missing_alt

    # ------------------------------------------------------------------
    # Sub-check C: Cross-engine text coverage per page
    # ------------------------------------------------------------------
    # Compare structure-tree text (what a screen reader sees) against
    # source PDF visual text (what the user sees).  This catches both
    # Artifact wrapping (content not in structure tree) and text loss
    # during tagging (content missing from tagged PDF entirely).
    try:
        source_doc = fitz.open(source_pdf)
        tagged_doc = fitz.open(tagged_pdf)
    except Exception as e:
        issues.append(f"PyMuPDF cannot open PDFs: {e}")
        return _gate_result("screen_reader_sim", False,
            f"{len(issues)} issues", details)

    # Optional pdfminer import
    pdfminer_available = False
    try:
        from pdfminer.high_level import extract_text as pdfminer_extract
        pdfminer_available = True
    except ImportError:
        pass

    page_results = []
    low_coverage_pages = []
    extraction_disagreements = []
    hollow_pages = []  # pages with visual content but no tagged text at all
    artifact_heavy_pages = []  # pages where Artifact regions >> MCID regions

    total_pages = tagged_doc.page_count
    details["total_pages"] = total_pages

    # Extract text from structure tree (what a screen reader actually sees).
    # This excludes Artifact content — unlike get_text() which returns all
    # visible text regardless of tagging.
    struct_text_by_page = _extract_struct_text_by_page(tagged_doc)
    decoded_struct_by_page = _struct_decoded_text_by_page(tagged_doc)
    details["struct_tree_text_pages"] = len(struct_text_by_page)

    # Fallback: if the structure tree has leaves (from Sub-check B) but we
    # found zero /ActualText, this is likely a third-party tagged PDF that
    # uses MCR without /ActualText.  Fall back to get_text() per page.
    use_struct_tree_text = True
    if total_leaves > 10 and not any(struct_text_by_page.values()):
        use_struct_tree_text = False

    for page_num in range(total_pages):
        # Source text (what the user sees in the original PDF)
        if page_num < source_doc.page_count:
            source_text = source_doc[page_num].get_text("text").strip()
        else:
            source_text = ""
        source_words = coverage_tokens(source_text)
        source_word_count = len(source_words)

        # pdfminer extraction of source page for cross-engine check
        pdfminer_text = ""
        pdfminer_words = set()
        if pdfminer_available:
            try:
                pdfminer_text = pdfminer_extract(
                    source_pdf, page_numbers=[page_num]).strip()
                pdfminer_words = set(pdfminer_text.lower().split()) if pdfminer_text else set()
            except Exception:
                pass

        # Extract tagged text + per-page coverage via the shared core (so the
        # gate and quick_screen_reader_check compute coverage identically).
        # Gate uses the MCID-gated fallback in the non-struct-tree branch.
        coverage, tagged_text, tagged_words = _page_coverage_core(
            page_num, source_words, source_word_count, struct_text_by_page,
            tagged_doc, use_struct_tree_text, mcid_gated_fallback=True,
            decoded_struct_by_page=decoded_struct_by_page,
        )

        # Cross-engine disagreement check (source PDF extraction)
        engine_disagree = False
        if pdfminer_available and source_word_count > 5:
            pdfminer_word_count = len(pdfminer_words)
            if source_word_count > 0 and pdfminer_word_count > 0:
                cross_overlap = source_words & pdfminer_words
                agreement = len(cross_overlap) / max(source_word_count, pdfminer_word_count)
                if agreement < 0.30:
                    engine_disagree = True
                    extraction_disagreements.append({
                        "page": page_num + 1,
                        "source_words": source_word_count,
                        "pdfminer_words": pdfminer_word_count,
                        "agreement": round(agreement, 2),
                    })

        # Detect hollow pages (source has content, structure tree has nothing)
        if source_word_count > 10 and not tagged_text:
            hollow_pages.append(page_num + 1)

        if source_word_count > 10 and coverage < coverage_threshold:
            low_coverage_pages.append({
                "page": page_num + 1,
                "coverage": round(coverage, 2),
                "source_words": source_word_count,
            })

        # Artifact ratio diagnostic — count MCID vs Artifact regions
        # in the tagged PDF's content stream
        mcid_count = 0
        artifact_count = 0
        try:
            tagged_page = tagged_doc[page_num]
            raw = tagged_page.read_contents()
            if raw:
                cs = raw.decode("latin-1", errors="replace")
                mcid_count = len(re.findall(r'/MCID\s+\d+', cs))
                artifact_count = len(re.findall(
                    r'/Artifact\s+BMC|/Artifact\s*<<[^>]*>>\s*BDC', cs))
        except Exception:
            pass

        if mcid_count > 0 and artifact_count > mcid_count * 3:
            artifact_heavy_pages.append(page_num + 1)

        page_results.append({
            "page": page_num + 1,
            "source_words": source_word_count,
            "tagged_words": len(tagged_words),
            "coverage": round(coverage, 2),
            "coverage_raw": coverage,
            "engine_disagreement": engine_disagree,
            "mcid_regions": mcid_count,
            "artifact_regions": artifact_count,
        })

    source_doc.close()
    tagged_doc.close()

    # Aggregate coverage
    pages_with_content = [p for p in page_results if p["source_words"] > 10]
    if pages_with_content:
        # Average UNROUNDED per-page coverage (then round once), matching
        # quick_screen_reader_check so the gate and the quick check agree.
        avg_coverage = sum(p["coverage_raw"] for p in pages_with_content) / len(pages_with_content)
    else:
        avg_coverage = 1.0

    details["avg_coverage"] = round(avg_coverage, 2)
    details["low_coverage_pages"] = low_coverage_pages
    details["hollow_pages"] = hollow_pages
    details["extraction_disagreements"] = extraction_disagreements
    details["pdfminer_available"] = pdfminer_available
    details["pages_with_content"] = len(pages_with_content)
    details["artifact_heavy_pages"] = artifact_heavy_pages
    details["text_source"] = "struct_tree" if use_struct_tree_text else "get_text_fallback"

    # Build issues (hard failures) vs warnings (informational)
    sr_warnings = []

    if not use_struct_tree_text:
        sr_warnings.append(
            "Structure tree has MCR but no /ActualText; using get_text() fallback"
        )

    if artifact_heavy_pages:
        sr_warnings.append(
            f"{len(artifact_heavy_pages)} page(s) are artifact-heavy "
            f"(Artifact regions >> MCID regions): {artifact_heavy_pages[:10]}"
        )

    # Hollow pages are a hard failure only if they're a significant
    # fraction of the document.  A single hollow page in a 14-page doc
    # is a Docling extraction gap, not a tagger bug.
    hollow_pct = len(hollow_pages) / max(total_pages, 1)
    if hollow_pages and (hollow_pct > 0.10 or len(hollow_pages) > 3):
        issues.append(
            f"{len(hollow_pages)} page(s) ({hollow_pct:.0%}) have visual content "
            f"but no tagged text: {hollow_pages[:10]}"
        )
    elif hollow_pages:
        sr_warnings.append(
            f"{len(hollow_pages)} page(s) have visual content but no tagged text "
            f"(below 10% threshold): {hollow_pages[:10]}"
        )
    # Low-coverage pages: allow a proportional budget for multi-page
    # documents.  A 115-page thesis with 2 low-coverage pages (appendix,
    # bibliography) shouldn't fail the entire document.
    # Budget: max(1, floor(N * 0.05)) pages for N-page documents,
    # but never more than 20% of total pages.
    low_cov_budget = max(1, int(total_pages * 0.05))
    low_cov_pct = len(low_coverage_pages) / max(total_pages, 1)
    if low_coverage_pages and (
        len(low_coverage_pages) > low_cov_budget or low_cov_pct > 0.20
    ):
        issues.append(
            f"{len(low_coverage_pages)} page(s) below {coverage_threshold:.0%} "
            f"text coverage (budget={low_cov_budget}): "
            f"{[p['page'] for p in low_coverage_pages[:10]]}"
        )
    elif low_coverage_pages:
        sr_warnings.append(
            f"{len(low_coverage_pages)} page(s) below {coverage_threshold:.0%} "
            f"text coverage (within budget of {low_cov_budget}): "
            f"{[p['page'] for p in low_coverage_pages[:10]]}"
        )
    # Overall average coverage must still be reasonable even if individual
    # low-coverage pages are within budget.
    if avg_coverage < coverage_threshold and pages_with_content:
        issues.append(
            f"Overall avg coverage {avg_coverage:.0%} below threshold "
            f"{coverage_threshold:.0%}"
        )

    # Cross-engine disagreements are warnings, not hard failures.
    # Different PDF libraries parse font encodings differently — this
    # doesn't indicate a real screen reader problem if coverage is high
    # and pdftotext (gate 2) confirms text extraction is correct.
    if extraction_disagreements:
        sr_warnings.append(
            f"{len(extraction_disagreements)} page(s) with PyMuPDF/pdfminer "
            f"extraction disagreement (possible CMap/encoding difference): "
            f"{[d['page'] for d in extraction_disagreements[:10]]}"
        )

    # ------------------------------------------------------------------
    # Sub-check E: MCR linkage — orphaned /ActualText, disconnected MCIDs
    # ------------------------------------------------------------------
    mcr_linkage = _check_mcr_linkage(tagged_pdf)
    details["sub_e_mcr_linkage"] = mcr_linkage

    invisible_pct = mcr_linkage.get("arol_invisible_pct", 0)
    garbled_pct = mcr_linkage.get("garbled_actual_text_pct", 0)
    if invisible_pct > 10.0:
        issues.append(
            f"{mcr_linkage['arol_invisible']}/{mcr_linkage['total_text_leaves']} "
            f"text elements ({invisible_pct:.0f}%) have /ActualText — "
            f"AROL cannot read elements with /ActualText (Phase 0 confirmed)"
        )
    elif invisible_pct > 5.0:
        sr_warnings.append(
            f"{mcr_linkage['arol_invisible']} text elements ({invisible_pct:.0f}%) "
            f"have /ActualText (AROL-invisible)"
        )
    if garbled_pct > 15.0:
        issues.append(
            f"{mcr_linkage['garbled_actual_text']} elements ({garbled_pct:.0f}%) "
            f"have garbled /ActualText (possible CID font decoding failure)"
        )
    if mcr_linkage.get("disconnected_mcids", 0) > 0:
        sr_warnings.append(
            f"{mcr_linkage['disconnected_mcids']} content stream MCID(s) "
            f"not referenced by any structure element"
        )

    # ------------------------------------------------------------------
    # Sub-check F: Extraction divergence
    # ------------------------------------------------------------------
    divergence = _check_extraction_divergence(tagged_pdf)
    details["sub_f_extraction_divergence"] = divergence

    avg_div = divergence.get("avg_divergence", 0)
    if avg_div > 0.30:
        issues.append(
            f"Extraction divergence {avg_div:.0%} — significant text in PDF "
            f"is not accessible via structure tree MCR"
        )
    elif avg_div > 0.15:
        sr_warnings.append(
            f"Extraction divergence {avg_div:.0%} — some text may not be "
            f"accessible via structure tree"
        )

    details["warnings"] = sr_warnings

    # ------------------------------------------------------------------
    # Pass/fail decision — only hard issues cause failure
    # ------------------------------------------------------------------
    passed = len(issues) == 0

    # Build summary message
    parts = []
    parts.append(f"Marked={marked}")
    parts.append(f"Lang={'yes' if doc_lang else 'MISSING'}")
    parts.append(f"leaves={total_leaves}")
    if figures_total > 0:
        parts.append(f"figures={figures_total} (missing alt: {figures_missing_alt})")
    parts.append(f"avg_coverage={avg_coverage:.0%}")
    if not use_struct_tree_text:
        parts.append("text_src=get_text_fallback")
    if pdfminer_available:
        parts.append(f"cross-engine={'OK' if not extraction_disagreements else f'{len(extraction_disagreements)} warn'}")
    if hollow_pages:
        parts.append(f"hollow_pages={len(hollow_pages)}")
    if artifact_heavy_pages:
        parts.append(f"artifact_heavy={len(artifact_heavy_pages)}")
    if mcr_linkage.get("arol_invisible", 0) > 0:
        parts.append(f"arol_invisible={mcr_linkage['arol_invisible']}")
    if avg_div > 0.05:
        parts.append(f"divergence={avg_div:.0%}")

    msg = ", ".join(parts)
    if issues:
        msg += f" — {len(issues)} issue(s)"
    if sr_warnings:
        msg += f" — {len(sr_warnings)} warning(s)"

    return _gate_result("screen_reader_sim", passed, msg, details)


# ---------------------------------------------------------------------------
# Quick screen reader check (lightweight, for pipeline use)
# ---------------------------------------------------------------------------

def _corroborate_page_deficits(
    raw_deficits: List[Dict[str, Any]],
    page_coverage: Dict[int, float],
    low_cov_pages: set,
    threshold: float = 0.80,
    corroborate: bool = True,
) -> List[Dict[str, Any]]:
    """Overrule matching-time page deficits with authoritative post-tagging coverage.

    ``page_deficits`` (from the content matcher) is computed on the ORIGINAL content
    runs, before the tagger strips duplicate invisible-text layers, so a page that
    carried such a layer shows a false "content not linked to screen-reader text"
    deficit even though the FINAL tagged PDF reads fine. ``page_coverage`` maps
    1-based page -> post-tagging word-overlap coverage (the ground truth of what a
    screen reader extracts). Keep a deficit only when the tagged page is genuinely
    under-covered.

    Rules (per deficit page ``pg`` with post-tagging coverage ``cov``):

    * ``pg`` already in ``low_cov_pages`` -> drop (already surfaced by the separate
      "Low text coverage" item; don't double-warn).
    * ``cov`` unknown (page had <=10 source words, not scored) -> keep (recall-safe:
      no authoritative signal to overturn the deficit).
    * ``cov < threshold`` -> keep (post-tagging coverage genuinely low).
    * ``cov >= threshold`` -> drop (high post-tagging coverage refutes the deficit).

    ``corroborate=False`` returns ``raw_deficits`` unchanged (config kill-switch).
    """
    if not corroborate:
        return raw_deficits
    kept: List[Dict[str, Any]] = []
    for d in raw_deficits:
        pg = d.get("page")
        if pg in low_cov_pages:
            continue
        cov = page_coverage.get(pg)
        if cov is None or cov < threshold:
            kept.append(d)
    return kept


def quick_screen_reader_check(
    tagged_pdf: str,
    source_pdf: str,
    coverage_threshold: float = DEFAULT_PAGE_LOW_COVERAGE,
    matching_summary: Dict[str, Any] = None,
    tagger_mode: str = "",
    ocr_quality_cfg: Dict[str, Any] = None,
    sr_poor_coverage: float = DEFAULT_SR_POOR_COVERAGE,
    sr_fair_coverage: float = DEFAULT_SR_FAIR_COVERAGE,
    two_witness: bool = True,
    exclude_figure_text: bool = True,
    reading_order_glued_guard: bool = True,
    reading_order_geo_tabular_guard: bool = True,
    reading_order_small_n_guard: bool = False,
    reading_order_hyphen_normalize: bool = True,
    corroborate_page_deficits: bool = True,
    deficit_corroboration_threshold: float = 0.80,
) -> Dict[str, Any]:
    """Fast screen reader quality check for use during pipeline execution.

    A lightweight subset of gate_screen_reader_sim that skips pdfminer
    cross-engine checks and BDC/EMC content stream parsing.  Runs in
    ~200-500ms for typical PDFs.

    For content-preserving mode, the primary quality signal is
    element_match_rate from the content matcher (how many semantic
    elements were matched to content stream runs).  Text coverage
    (PyMuPDF word-set overlap) is informational only — it measures
    extraction similarity, not screen reader fidelity.

    For scanned-source pages (PyMuPDF ``get_text() == ""``), EMR is
    structurally meaningless — there are no native runs to match
    against — so each scanned page is scored by ``audit.ocr_quality``
    against the OCR text in the tagged PDF's ``/ActualText`` instead.
    Mixed docs use the worse of the two SR labels.

    Returns dict with keys:
        sr_quality: "good" | "fair" | "poor"
        avg_coverage: float (0-1)
        hollow_pages: list[int] (1-based page numbers)
        marked: bool
        has_struct_tree: bool
        document_lang: str | None
        total_leaves: int
        total_pages: int
        figures_total: int
        figures_missing_alt: int
        element_match_rate: float | None
        tagger_mode: str
        scanned_pages: list[int] (1-based)
        per_page_ocr_quality: list[{"page", "score", "label", "reasons"}]
        ocr_pages_passing_pct: float | None (share with label != "poor",
            excluding skipped pages; None when no scanned pages graded)
        issues: list[str]
    """
    result: Dict[str, Any] = {
        "sr_quality": "poor",
        "avg_coverage": 0.0,
        "hollow_pages": [],
        "marked": False,
        "has_struct_tree": False,
        "document_lang": None,
        "total_leaves": 0,
        "total_pages": 0,
        "figures_total": 0,
        "figures_missing_alt": 0,
        "element_match_rate": None,
        "run_match_rate": None,
        "tagger_mode": tagger_mode or "",
        "low_coverage_pages": [],
        "page_deficits": [],
        "scanned_pages": [],
        "per_page_ocr_quality": [],
        "ocr_pages_passing_pct": None,
        "reading_order_min_tau": None,
        "reading_order_failing_pages": [],
        "reading_order_noop_pages": 0,
        "issues": [],
    }
    issues = result["issues"]

    # ------------------------------------------------------------------
    # Sub-check A: Structure basics via pypdf
    # ------------------------------------------------------------------
    try:
        import pypdf
        reader = pypdf.PdfReader(tagged_pdf)
    except ImportError:
        issues.append("pypdf not installed — cannot check structure")
        return result
    except Exception as e:
        issues.append(f"Cannot open tagged PDF: {e}")
        return result

    root = reader.root_object if hasattr(reader, "root_object") else reader.trailer.get("/Root", {})

    mark_info = root.get("/MarkInfo", {})
    result["marked"] = bool(mark_info.get("/Marked", False)) if mark_info else False
    if not result["marked"]:
        issues.append("Document /MarkInfo.Marked is not true")

    struct_tree_root = root.get("/StructTreeRoot")
    result["has_struct_tree"] = struct_tree_root is not None
    if not result["has_struct_tree"]:
        issues.append("No /StructTreeRoot — screen reader has no structure")
        return result

    doc_lang = root.get("/Lang")
    result["document_lang"] = str(doc_lang) if doc_lang else None
    if not doc_lang:
        issues.append("No document /Lang — screen reader cannot select TTS voice")

    # ------------------------------------------------------------------
    # Sub-check B: Structure tree walk — leaf count + figure alt text
    # ------------------------------------------------------------------
    total_leaves = 0
    figures_total = 0
    figures_missing_alt = 0

    page_obj_map = {}
    for i, page in enumerate(reader.pages):
        page_obj_map[id(page.indirect_reference)] = i
        page_obj_map[id(page)] = i

    def _resolve(obj):
        return obj.get_object() if hasattr(obj, "get_object") else obj

    def _walk(node, page_idx=None):
        nonlocal total_leaves, figures_total, figures_missing_alt
        node = _resolve(node)
        if not hasattr(node, "get"):
            return

        pg_ref = node.get("/Pg")
        if pg_ref is not None:
            pg_obj = _resolve(pg_ref)
            page_idx = page_obj_map.get(id(pg_ref), page_obj_map.get(id(pg_obj), page_idx))

        s_type = str(node.get("/S", ""))
        if s_type == "/Figure":
            figures_total += 1
            # Presence of the key is not a description (G-07); see above.
            alt_value = node.get("/Alt")
            if alt_value is None or is_generic_alt(str(alt_value)):
                figures_missing_alt += 1

        k = node.get("/K")
        if k is None:
            return
        kids = k if isinstance(k, list) else [k]

        for kid in kids:
            kid = _resolve(kid)
            if isinstance(kid, int):
                total_leaves += 1
            elif hasattr(kid, "get"):
                kid_type = kid.get("/Type")
                if kid_type is not None and str(kid_type) in ("/OBJR",):
                    pass
                elif kid_type is not None and str(kid_type) == "/MCR":
                    total_leaves += 1
                elif "/S" in kid:
                    _walk(kid, page_idx)
                elif "/MCID" in kid:
                    total_leaves += 1
            elif isinstance(kid, str):
                pass  # literal text in tree (rare)

    try:
        tree_root = _resolve(struct_tree_root)
        root_kids = tree_root.get("/K")
        if root_kids is not None:
            if isinstance(root_kids, list):
                for kid in root_kids:
                    _walk(kid)
            else:
                _walk(root_kids)
    except Exception as e:
        issues.append(f"Error walking structure tree: {e}")

    result["total_leaves"] = total_leaves
    result["figures_total"] = figures_total
    result["figures_missing_alt"] = figures_missing_alt

    if figures_total > 0 and figures_missing_alt > 0:
        issues.append(
            f"{figures_missing_alt} of {figures_total} figures missing /Alt text"
        )

    # ------------------------------------------------------------------
    # Sub-check C: Per-page text coverage (PyMuPDF source vs tagged)
    # ------------------------------------------------------------------
    hollow_pages = []
    low_coverage_pages = []

    try:
        source_doc = fitz.open(source_pdf)
        tagged_doc = fitz.open(tagged_pdf)
    except Exception as e:
        issues.append(f"PyMuPDF cannot open PDFs: {e}")
        result["sr_quality"] = "poor"
        return result

    total_pages = tagged_doc.page_count
    result["total_pages"] = total_pages
    coverages = []
    page_coverage: Dict[int, float] = {}  # 1-based page -> post-tagging coverage
    scanned_pages: List[int] = []
    per_page_ocr_quality: List[Dict[str, Any]] = []
    ocr_cfg = ocr_quality_cfg or {}
    ocr_kwargs = {
        "lang_hint": ocr_cfg.get("lang_hint", "en"),
        "min_chars": ocr_cfg.get("min_text_chars", 30),
        "zipf_threshold": ocr_cfg.get("zipf_threshold", 3.0),
        "good_threshold": ocr_cfg.get("good_threshold", 0.65),
        "fair_threshold": ocr_cfg.get("fair_threshold", 0.40),
    }

    # Extract text from structure tree (what a screen reader actually sees).
    # Excludes Artifact content — unlike get_text() which returns all visible text.
    struct_text_by_page = _extract_struct_text_by_page(tagged_doc)
    decoded_struct_by_page = _struct_decoded_text_by_page(tagged_doc)

    # Fallback: if structure tree has leaves but zero /ActualText,
    # this is a third-party tagged PDF — fall back to get_text().
    # For our own tagger output, ALWAYS use struct tree text so that
    # empty /ActualText is surfaced as a real problem (not hidden by
    # get_text() which includes Artifact content).
    if tagger_mode:
        use_struct_tree = True
    else:
        use_struct_tree = bool(struct_text_by_page) or total_leaves == 0

    for page_num in range(total_pages):
        # Source text (what the user sees visually)
        if page_num < source_doc.page_count:
            source_text = source_doc[page_num].get_text("text").strip()
        else:
            source_text = ""
        source_words = coverage_tokens(source_text)
        source_word_count = len(source_words)

        # Tagged text + per-page coverage via the shared core (so this matches
        # gate_screen_reader_sim exactly).  Quick-check uses the unconditional
        # get_text fallback in the non-struct-tree branch.
        coverage, tagged_text, tagged_words = _page_coverage_core(
            page_num, source_words, source_word_count, struct_text_by_page,
            tagged_doc, use_struct_tree, mcid_gated_fallback=False,
            decoded_struct_by_page=decoded_struct_by_page,
        )

        # ----- Scanned-page OCR quality scoring -----
        # PyMuPDF get_text() reads only native content-stream text (no
        # OCR), so source_text == "" reliably marks an image-only page.
        if not source_text:
            scanned_pages.append(page_num + 1)
            if not tagged_text.strip():
                # Tagger failed to write any /ActualText for this scanned page
                per_page_ocr_quality.append({
                    "page": page_num + 1,
                    "score": 0.0,
                    "label": "poor",
                    "reasons": ["no tagged text"],
                })
            else:
                from audit.ocr_quality import score_ocr_text
                ocr_result = score_ocr_text(tagged_text, **ocr_kwargs)
                per_page_ocr_quality.append({
                    "page": page_num + 1,
                    "score": ocr_result["score"],
                    "label": ocr_result["label"],
                    "reasons": ocr_result["reasons"],
                    "signals": ocr_result["signals"],
                })

        if source_word_count > 10:
            coverages.append(coverage)
            page_coverage[page_num + 1] = coverage

            if not tagged_text:
                hollow_pages.append(page_num + 1)
            elif coverage < coverage_threshold:
                low_coverage_pages.append({
                    "page": page_num + 1,
                    "coverage": round(coverage, 2),
                })
        elif source_word_count >= 2:
            # Small page (2-10 words): too few words for reliable coverage,
            # but detect hollow pages (source has text, tagged has nothing).
            if not tagged_text:
                hollow_pages.append(page_num + 1)

    source_doc.close()
    tagged_doc.close()

    result["hollow_pages"] = hollow_pages

    if coverages:
        avg_coverage = sum(coverages) / len(coverages)
    else:
        avg_coverage = 1.0
    result["avg_coverage"] = round(avg_coverage, 2)
    result["low_coverage_pages"] = low_coverage_pages

    # Hollow pages are an issue only if they're a significant fraction
    hollow_pct = len(hollow_pages) / max(total_pages, 1)
    if hollow_pages and (hollow_pct > 0.10 or len(hollow_pages) > 3):
        issues.append(
            f"{len(hollow_pages)} page(s) ({hollow_pct:.0%}) have visual content "
            f"but no tagged text: pages {hollow_pages[:10]}"
        )
    is_content_preserving = (
        tagger_mode == "content-preserving" and matching_summary
    )

    if low_coverage_pages:
        if is_content_preserving:
            issues.append(
                f"{len(low_coverage_pages)} page(s) below "
                f"{coverage_threshold:.0%} text extraction similarity "
                f"(informational — element matching is the primary "
                f"quality signal)"
            )
        else:
            issues.append(
                f"{len(low_coverage_pages)} page(s) below "
                f"{coverage_threshold:.0%} text coverage"
            )

    # ------------------------------------------------------------------
    # Sub-check D: OCR quality aggregation for scanned pages
    # ------------------------------------------------------------------
    result["scanned_pages"] = scanned_pages
    result["per_page_ocr_quality"] = per_page_ocr_quality

    graded = [
        p for p in per_page_ocr_quality
        if not p.get("signals", {}).get("skipped")
    ]
    if graded:
        passing = sum(1 for p in graded if p["label"] != "poor")
        result["ocr_pages_passing_pct"] = round(passing / len(graded), 3)
    else:
        result["ocr_pages_passing_pct"] = None

    poor_ocr_pages = [p["page"] for p in per_page_ocr_quality
                      if p["label"] == "poor"]
    if poor_ocr_pages:
        issues.append(
            f"OCR text quality is poor on {len(poor_ocr_pages)} page(s): "
            f"{poor_ocr_pages[:10]}"
        )

    # ------------------------------------------------------------------
    # Quality determination
    # ------------------------------------------------------------------
    alt_missing_pct = (figures_missing_alt / figures_total * 100) if figures_total > 0 else 0
    significant_hollow = hollow_pct > 0.10 or len(hollow_pages) > 3

    # OCR label for scanned-page subset (None if no scanned pages graded).
    _rank = {"good": 2, "fair": 1, "poor": 0}
    ocr_label: Optional[str] = None
    if scanned_pages and result["ocr_pages_passing_pct"] is not None:
        ocr_pct = result["ocr_pages_passing_pct"]
        if ocr_pct < 0.50 or significant_hollow:
            ocr_label = "poor"
        elif ocr_pct < 0.80 or alt_missing_pct > 30:
            ocr_label = "fair"
        else:
            ocr_label = "good"

        # Fully-scanned doc — OCR label is final.
        if len(scanned_pages) == total_pages:
            result["sr_quality"] = ocr_label
            return result

    if is_content_preserving:
        # Content-preserving mode: element match rate is the primary
        # signal — it directly measures whether semantic elements have
        # been linked to visible content stream runs.
        emr = matching_summary.get("element_match_rate", 0)
        result["element_match_rate"] = emr
        # run_match_rate corroborates the EMR gate for fully-native docs: a high
        # share of matched content runs proves the stream is attributed even when
        # over-segmentation depresses the element-level rate. Threaded into the
        # verdict via build_verdict_inputs -> emr_passes.
        result["run_match_rate"] = matching_summary.get("run_match_rate")
        # Corroborate matching-time page deficits (computed on the ORIGINAL
        # content runs, before the tagger strips duplicate invisible-text
        # layers) against the authoritative post-tagging per-page coverage, so
        # pages that read fine in the FINAL PDF aren't falsely flagged
        # "content not linked to screen-reader text".
        result["page_deficits"] = _corroborate_page_deficits(
            matching_summary.get("page_deficits", []),
            page_coverage,
            {d["page"] for d in low_coverage_pages},
            threshold=deficit_corroboration_threshold,
            corroborate=corroborate_page_deficits,
        )

        if emr < 50 or not result["has_struct_tree"] or significant_hollow:
            result["sr_quality"] = "poor"
        elif emr < 85 or alt_missing_pct > 30:
            result["sr_quality"] = "fair"
        else:
            result["sr_quality"] = "good"
    else:
        # Overlay / fallback mode: text coverage is the best available
        # signal (no element-to-content matching data).
        n_issues = len(issues)
        if avg_coverage < sr_poor_coverage or significant_hollow or alt_missing_pct > 30:
            result["sr_quality"] = "poor"
        elif avg_coverage < sr_fair_coverage or n_issues > 1:
            result["sr_quality"] = "fair"
        else:
            result["sr_quality"] = "good"

    # Mixed doc: downgrade to the worse of {EMR label, OCR label}.
    if ocr_label is not None:
        result["sr_quality"] = min(
            result["sr_quality"], ocr_label, key=lambda x: _rank[x]
        )

    # ------------------------------------------------------------------
    # Sub-check G: reading-order correlation (Matterhorn checkpoint 09)
    # ------------------------------------------------------------------
    # Compare the tag-tree DFS order (screen-reader order) against the tagged
    # file's own column-aware visual order.  Detection only — flags pages whose
    # reading order is uncertain for the manual pass; never reorders content and
    # never hard-fails (a no-eligible-page doc returns min_tau None -> gate is
    # skipped in the verdict).  scanned_pages here is 1-based; the scorer is
    # 0-based, so convert.  failing_pages are surfaced 1-based for display.
    try:
        from audit.reading_order_score import score_reading_order
        ro = score_reading_order(
            tagged_pdf,
            scanned_pages=[p - 1 for p in scanned_pages],
            two_witness=two_witness,
            exclude_figure_text=exclude_figure_text,
            glued_guard=reading_order_glued_guard,
            geo_tabular_guard=reading_order_geo_tabular_guard,
            small_n_guard=reading_order_small_n_guard,
            hyphen_normalize=reading_order_hyphen_normalize,
        )
        result["reading_order_min_tau"] = ro["min_tau"]
        result["reading_order_failing_pages"] = [p + 1 for p in ro["failing_pages"]]
        result["reading_order_noop_pages"] = ro["n_noop_pages"]
        result["reading_order_geo_tabular_pages"] = ro.get("n_geo_tabular_pages", 0)
        result["reading_order_small_n_noop_pages"] = ro.get("n_small_n_noop_pages", 0)
        result["reading_order_per_page"] = {
            p + 1: round(t, 2) for p, t in ro.get("per_page", {}).items()
        }
        if ro["failing_pages"]:
            pages = ", ".join(str(p + 1) for p in ro["failing_pages"])
            issues.append(
                f"Reading order may not match visual layout on page(s) {pages} "
                f"(worst tau {ro['min_tau']:.2f}) — verify in a screen reader"
            )
    except Exception as e:
        issues.append(f"Reading-order check skipped: {e}")

    return result


# ---------------------------------------------------------------------------
# Run all gates
# ---------------------------------------------------------------------------

def run_all_gates(
    source_pdf: str,
    tagged_pdf: str,
    pixel_diff_threshold: float = 2.0,
    text_ratio_threshold: float = 0.85,
    enriched_structure_path: str = None,
    docling_elements: int = None,
    docling_pages: int = None,
    text_classification: str = None,
    docling_pages_with_text: int = None,
) -> Dict[str, Any]:
    """Run all validation gates on a source/tagged PDF pair.

    Returns:
        {
            "all_passed": bool,
            "gates_passed": int,
            "gates_total": int,
            "gates": [gate_result, ...],
            "summary": str,
        }
    """
    gates = []

    # Gate 1: Visual diff
    gates.append(gate_visual_diff(source_pdf, tagged_pdf,
                                   threshold=pixel_diff_threshold))

    # Gate 2: Text extraction
    gates.append(gate_text_extraction(source_pdf, tagged_pdf,
                                       min_ratio=text_ratio_threshold,
                                       enriched_structure_path=enriched_structure_path))

    # Gate 3: Operator balance (differential — only flags new imbalances)
    gates.append(gate_operator_balance(source_pdf, tagged_pdf))

    # Gate 4: MCID completeness
    gates.append(gate_mcid_completeness(tagged_pdf))

    # Gate 5: Bare container tree (Document>Div-only backstop)
    gates.append(gate_bare_tree(tagged_pdf))

    # Gate: Docling structure yield. Appended only when the caller supplies
    # conversion stats — legacy callers keep their original gate count
    # instead of gaining a permanently-passing extra gate.
    if docling_elements is not None and docling_pages is not None:
        gates.append(gate_structure_yield(
            docling_elements, docling_pages, text_classification,
            docling_pages_with_text))

    # Gate 6: Screen reader simulation
    gates.append(gate_screen_reader_sim(source_pdf, tagged_pdf))

    passed = sum(1 for g in gates if g["passed"])
    total = len(gates)
    all_passed = passed == total

    failed_gates = [g["gate"] for g in gates if not g["passed"]]
    summary = f"{passed}/{total} gates passed"
    if failed_gates:
        summary += f" — FAILED: {', '.join(failed_gates)}"

    return {
        "all_passed": all_passed,
        "gates_passed": passed,
        "gates_total": total,
        "gates": gates,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gate_result(
    gate: str,
    passed: bool,
    message: str,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "gate": gate,
        "passed": passed,
        "message": message,
        "details": details or {},
    }


def print_gate_results(results: Dict[str, Any]) -> None:
    """Pretty-print gate results."""
    print(f"\n{'='*70}")
    print(f"VALIDATION GATES: {results['summary']}")
    print(f"{'='*70}")

    for g in results["gates"]:
        status = "PASS" if g["passed"] else "FAIL"
        icon = "+" if g["passed"] else "X"
        print(f"  [{icon}] {status} Gate: {g['gate']}")
        print(f"        {g['message']}")
        # Print key details for failed gates
        if not g["passed"] and g["details"]:
            for key, val in g["details"].items():
                if key in ("issues", "failed_details", "failed_pages") and val:
                    if isinstance(val, list):
                        for item in val[:5]:
                            print(f"        - {item}")
    print()


# ---------------------------------------------------------------------------
# CLI for standalone testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run validation gates on a PDF pair")
    parser.add_argument("source", help="Source (original) PDF path")
    parser.add_argument("tagged", help="Tagged (accessible) PDF path")
    parser.add_argument("--threshold", type=float, default=2.0,
                       help="Pixel diff threshold (default: 2.0%%)")
    args = parser.parse_args()

    results = run_all_gates(
        args.source, args.tagged,
        pixel_diff_threshold=args.threshold,
    )
    print_gate_results(results)

    # Exit with code 1 if any gate failed
    exit(0 if results["all_passed"] else 1)
