# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared pipeline logic for ReadReady.

This module contains the canonical single-document processing pipeline
used by the CLI (main.py), Gradio web UI (app.py), and Celery tasks.
"""

import copy
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from dotenv import load_dotenv
load_dotenv()

from utils.secrets import get_secret
from utils.editor_input import iter_list_edit_targets
from utils.editor_records import ensure_image_editor_records

from converter.alt_harvest import is_generic_alt
from converter.docling_converter import convert_pdf_to_html
from audit.auditor import AccessibilityAuditor
from audit.report_generator import generate_report
from audit.standards import compute_compliance_score, get_criterion_info
from vlm.alt_text_generator import generate_alt_text_for_images
from vlm.table_caption_generator import generate_table_captions
from vlm.formula_processor import process_formulas, enrich_html_with_formulas
from remediate.remediator import remediate_html
from output_gen.accessible_css import inject_accessible_css
from output_gen.reading_order_edit import apply_sr_order_edits
from output_gen.pdf_generator import generate_pdf
from output_gen.pdf_tagger import (
    compute_java_heap,
    generate_pdf_bookmarks,
    resolve_tagger_slots,
    tag_pdf,
)
from output_gen.changes_report import generate_changes_report_html

from logging_util.outcome_recorder import phase_guard
from logging_util.failure_taxonomy import check_docling_yield_low

from bs4 import BeautifulSoup
from PIL import Image as _PILImage

_PILImage.MAX_IMAGE_PIXELS = 200_000_000  # ~14k x 14k; bounds decompression bombs

logger = logging.getLogger("pdf_accessibility")


def _empty_audit_result() -> Dict[str, Any]:
    """Canonical empty audit dict — the safe fallback when an audit phase fails.

    Matches the shape downstream readers expect (``_build_report_kwargs`` and
    ``_inject_sr_issues_into_post_audit`` read ``compliance`` and ``summary``).
    """
    return {
        "issues": [],
        "summary": {"total_issues": 0, "needs_remediation": 0, "compliant": 0,
                    "severity_counts": {"critical": 0, "major": 0, "minor": 0}},
        "compliance": {"overall_score": 0, "by_criterion": {},
                       "by_principle": {}, "by_severity": {}},
    }


def _noop_progress(frac: float, desc: str = "") -> None:
    """Default no-op progress callback."""
    pass


def _has_vlm_configured(llm_config: dict) -> bool:
    """Check if a VLM provider is configured (has a base URL or API key)."""
    if not llm_config:
        return False
    provider = llm_config.get("provider", "ollama")
    if provider == "openai":
        return bool(
            llm_config.get("api_key")
            or get_secret("OPENROUTER_API_KEY")
            or get_secret("OPENAI_API_KEY")
        )
    return bool(llm_config.get("base_url"))


def _table_ai_options(config: dict) -> dict:
    """Remediation options for AI table analysis.

    The run's LLM config is passed only when an LLM is configured; None
    means table remediation never builds a client.
    """
    llm_config = config.get("llm", {})
    return {
        "ai_llm_config": llm_config if _has_vlm_configured(llm_config) else None,
        "table_analysis": config.get("table_analysis", {}),
    }


def _inject_sr_issues_into_post_audit(
    post_audit: Dict[str, Any], sr_result: Dict[str, Any]
) -> None:
    """Inject screen reader quality findings into post-audit as proper issues.

    Translates PDF-level problems detected by quick_screen_reader_check()
    into audit issue dicts, appends them to post_audit["issues"], and
    recomputes compliance aggregates so scores reflect the tagged PDF state.

    Mutates post_audit in place.
    """
    issues = post_audit.get("issues", [])
    next_id = len(issues) + 1
    sr_issues = sr_result.get("issues", [])
    sr_quality = sr_result.get("sr_quality", "unknown")

    def _make_issue(
        issue_type: str,
        wcag_criterion: str,
        severity: str,
        description: str,
    ) -> dict:
        nonlocal next_id
        criterion_info = get_criterion_info(wcag_criterion)
        issue = {
            "id": f"issue-{next_id}",
            "type": issue_type,
            "wcag_criterion": wcag_criterion,
            "criterion_name": criterion_info.get("name", ""),
            "criterion_level": criterion_info.get("level", ""),
            "severity": severity,
            "element": "pdf",
            "description": description,
            "context": {"source": "screen_reader_quality_check"},
            "location": {"path": "tagged-pdf", "page_number": 0},
            "remediation_status": "needs_remediation",
            "remediation_source": None,
            "remediation_date": None,
        }
        next_id += 1
        return issue

    new_issues = []

    # Structural basics
    if not sr_result.get("has_struct_tree", True):
        new_issues.append(_make_issue(
            "pdf-no-structure-tree", "1.3.1", "critical",
            "Tagged PDF has no /StructTreeRoot — screen readers have no "
            "document structure to navigate.",
        ))

    if not sr_result.get("marked", True):
        new_issues.append(_make_issue(
            "pdf-not-marked", "4.1.2", "critical",
            "Tagged PDF /MarkInfo.Marked is not true — assistive technology "
            "cannot identify this as a tagged document.",
        ))

    if not sr_result.get("document_lang"):
        new_issues.append(_make_issue(
            "pdf-no-document-lang", "3.1.1", "critical",
            "Tagged PDF has no /Lang entry — screen readers cannot select "
            "the correct text-to-speech voice.",
        ))

    # Hollow pages
    hollow_pages = sr_result.get("hollow_pages", [])
    total_pages = max(
        len(hollow_pages),
        sr_result.get("total_pages", 1),
        1,
    )
    hollow_pct = len(hollow_pages) / total_pages
    if hollow_pages and (hollow_pct > 0.10 or len(hollow_pages) > 3):
        pages_str = ", ".join(str(p) for p in hollow_pages[:10])
        suffix = f"... ({len(hollow_pages)} total)" if len(hollow_pages) > 10 else ""
        new_issues.append(_make_issue(
            "pdf-hollow-pages", "1.3.1", "major",
            f"{len(hollow_pages)} page(s) have visual content but no tagged "
            f"text for screen readers: pages {pages_str}{suffix}.",
        ))

    # Figures missing alt in struct tree
    fig_total = sr_result.get("figures_total", 0)
    fig_missing = sr_result.get("figures_missing_alt", 0)
    if fig_total > 0 and fig_missing > 0:
        new_issues.append(_make_issue(
            "pdf-figures-missing-alt", "1.1.1", "major",
            f"{fig_missing} of {fig_total} Figure structure elements in the "
            f"tagged PDF are missing /Alt text.",
        ))

    # Pages where OCR'd text is garbled / unreadable to screen readers
    per_page_ocr = sr_result.get("per_page_ocr_quality", [])
    bad_ocr_pages = [p["page"] for p in per_page_ocr if p["label"] == "poor"]
    if bad_ocr_pages:
        pages_str = ", ".join(str(p) for p in bad_ocr_pages[:10])
        suffix = f"... ({len(bad_ocr_pages)} total)" if len(bad_ocr_pages) > 10 else ""
        new_issues.append(_make_issue(
            "pdf-poor-ocr-quality", "1.1.1", "major",
            f"OCR text quality is poor on page(s) {pages_str}{suffix}. "
            f"Screen readers will read garbled text on these pages.",
        ))

    if not new_issues:
        return  # Nothing to inject

    # Append new issues and recompute aggregates
    issues.extend(new_issues)
    post_audit["issues"] = issues

    # Recompute compliance and summary from full issue list
    post_audit["compliance"] = compute_compliance_score(issues)

    # Recompute summary counts
    severity_counts = {"critical": 0, "major": 0, "minor": 0, "info": 0}
    status_counts = {
        "needs_remediation": 0,
        "remediated": 0,
        "auto_remediated": 0,
        "compliant": 0,
    }
    for iss in issues:
        status = iss.get("remediation_status", "needs_remediation")
        if status in status_counts:
            status_counts[status] += 1
        sev = iss.get("severity", "info")
        if sev in severity_counts:
            severity_counts[sev] += 1

    post_audit["summary"] = {
        "total_issues": len(issues),
        "needs_remediation": status_counts["needs_remediation"],
        "remediated": status_counts["remediated"],
        "auto_remediated": status_counts["auto_remediated"],
        "compliant": status_counts["compliant"],
        "severity_counts": severity_counts,
    }

    logger.info(
        "  -> Injected %d PDF-level issues into post-audit (new score: %.1f%%)",
        len(new_issues),
        post_audit["compliance"].get("overall_score", 0),
    )


def _inject_tagger_issues_into_post_audit(
    post_audit: Dict[str, Any], struct_tree_issues: list
) -> None:
    """Inject struct tree validator findings into post-audit as proper issues.

    Aggregates blank-leaf-element, blank-subtree, and text-coverage findings
    from the struct tree validator into audit issue dicts. Skips checks
    already covered by _inject_sr_issues_into_post_audit (Phase 2.1).

    Mutates post_audit in place.
    """
    # Checks already covered by SR quality injection (Phase 2.1)
    _SKIP_CHECKS = frozenset({
        "no-struct-tree", "no-markinfo", "markinfo-not-marked",
        "no-document-elem", "file-missing", "open-failed", "summary",
    })

    # Aggregate counts by check type
    blank_leaf_count = 0
    blank_subtree_count = 0
    coverage_issue = None  # at most one

    for si in struct_tree_issues:
        check = si.get("check", "")
        if check in _SKIP_CHECKS:
            continue

        if check == "blank-leaf-element":
            blank_leaf_count += 1
        elif check == "blank-subtree":
            blank_subtree_count += 1
        elif check in ("low-text-coverage", "moderate-text-coverage"):
            coverage_issue = si  # last one wins (should be only one)

    issues = post_audit.get("issues", [])
    next_id = len(issues) + 1

    def _make_issue(
        issue_type: str,
        wcag_criterion: str,
        severity: str,
        description: str,
        remediation_status: str = "needs_remediation",
    ) -> dict:
        nonlocal next_id
        criterion_info = get_criterion_info(wcag_criterion)
        issue = {
            "id": f"issue-{next_id}",
            "type": issue_type,
            "wcag_criterion": wcag_criterion,
            "criterion_name": criterion_info.get("name", ""),
            "criterion_level": criterion_info.get("level", ""),
            "severity": severity,
            "element": "pdf",
            "description": description,
            "context": {"source": "struct_tree_validator"},
            "location": {"path": "tagged-pdf", "page_number": 0},
            "remediation_status": remediation_status,
            "remediation_source": None,
            "remediation_date": None,
        }
        next_id += 1
        return issue

    new_issues = []

    if blank_leaf_count > 0:
        new_issues.append(_make_issue(
            "pdf-blank-structure-elements", "1.3.1", "info",
            f"Internal tagger QA: {blank_leaf_count} structure element(s) "
            f"created without content. Not a user-actionable WCAG failure; "
            f"logged for developer triage of tagger correctness.",
            remediation_status="informational",
        ))

    if blank_subtree_count > 0:
        new_issues.append(_make_issue(
            "pdf-blank-subtrees", "1.3.1", "info",
            f"Internal tagger QA: {blank_subtree_count} structure subtree(s) "
            f"created with no content in any descendant. Not a user-actionable "
            f"WCAG failure; logged for developer triage of tagger correctness.",
            remediation_status="informational",
        ))

    if coverage_issue:
        check = coverage_issue["check"]
        msg = coverage_issue["message"]
        if check == "low-text-coverage":
            new_issues.append(_make_issue(
                "pdf-low-text-coverage", "4.1.2", "major",
                f"Structure tree text coverage is low: {msg}",
            ))
        elif check == "moderate-text-coverage":
            new_issues.append(_make_issue(
                "pdf-moderate-text-coverage", "4.1.2", "minor",
                f"Structure tree text coverage is moderate: {msg}",
            ))

    if not new_issues:
        return

    issues.extend(new_issues)
    post_audit["issues"] = issues

    # Recompute compliance and summary from full issue list
    post_audit["compliance"] = compute_compliance_score(issues)

    severity_counts = {"critical": 0, "major": 0, "minor": 0, "info": 0}
    status_counts = {
        "needs_remediation": 0,
        "remediated": 0,
        "auto_remediated": 0,
        "compliant": 0,
    }
    for iss in issues:
        status = iss.get("remediation_status", "needs_remediation")
        if status in status_counts:
            status_counts[status] += 1
        sev = iss.get("severity", "info")
        if sev in severity_counts:
            severity_counts[sev] += 1

    post_audit["summary"] = {
        "total_issues": len(issues),
        "needs_remediation": status_counts["needs_remediation"],
        "remediated": status_counts["remediated"],
        "auto_remediated": status_counts["auto_remediated"],
        "compliant": status_counts["compliant"],
        "severity_counts": severity_counts,
    }

    logger.info(
        "  -> Injected %d tagger-level issues into post-audit (new score: %.1f%%)",
        len(new_issues),
        post_audit["compliance"].get("overall_score", 0),
    )


def _build_report_kwargs(
    result: Dict[str, Any],
    post_audit: Dict[str, Any],
    pdf_path: Path,
    config: Optional[Dict[str, Any]] = None,
) -> dict:
    """Assemble kwargs for generate_changes_report_html/pdf from pipeline result.

    Builds all the parameters needed by the expanded changes report, including
    formula_records derived from formula_descriptions + structure JSON.
    """
    _rem = result.get("remediation", {})

    # Build formula records from descriptions + structure JSON
    formula_records = []
    fd = result.get("formula_descriptions", {})
    if fd and fd.get("descriptions"):
        struct_path = result.get("structure_json_path")
        if struct_path and Path(struct_path).exists():
            try:
                with open(struct_path, encoding="utf-8") as f:
                    struct_data = json.load(f)
                all_elements = struct_data.get("elements", [])
                for elem_id, desc_info in fd["descriptions"].items():
                    for elem in all_elements:
                        if elem.get("id") == elem_id:
                            formula_records.append({
                                "id": elem_id,
                                "description": desc_info.get("description", ""),
                                "latex": desc_info.get("latex"),
                                "page_number": elem.get("page_no"),
                                "image_ref": elem.get("image_ref"),
                            })
                            break
            except Exception as _e:
                logger.debug("Formula report enrichment skipped: %s", _e)

    return {
        "doc_title": result.get("title", pdf_path.stem),
        "change_records": _rem.get("change_records", []),
        "table_changes": _rem.get("table_changes", []),
        "image_records": result.get("alt_text", {}).get("image_records", []),
        "css_injected": True,
        "post_audit": post_audit,
        "stripped_text_pages": result.get("stripped_text_pages", []),
        # Expanded report data
        "doc_stats": result.get("stats"),
        "doc_language": result.get("language"),
        "source_filename": pdf_path.name,
        "pre_audit": result.get("audit"),
        "fallback_recovery": result.get("fallback_recovery"),
        "ocr_recovery": result.get("ocr_recovery"),
        "formula_records": formula_records or None,
        "phase_status": result.get("phase_status"),
        "alt_text_result": result.get("alt_text"),
        "table_captions_result": result.get("table_captions"),
        "remediation_result": _rem or None,
        "sr_data": result.get("sr_quality"),
        "verdict_config": (config or {}).get("verdict", {}),
        "source_quality_warnings": result.get("source_quality_warnings", {}),
    }


def _vlm_phase_status(alt_result: dict) -> dict:
    """Derive Phase 5's status from what actually happened.

    Per-image VLM failures increment skipped_count and set no top-level
    "error" key, so branching on that key alone recorded a run in which every
    image failed as a success (G-31).
    """
    if alt_result.get("error"):
        return {"status": "failed",
                "error": str(alt_result["error"])[:200],
                "reason": "vlm_provider_error"}
    updated = alt_result.get("updated_count", 0) or 0
    skipped = alt_result.get("skipped_count", 0) or 0
    if skipped and not updated:
        return {"status": "failed", "skipped": skipped,
                "reason": f"all {skipped} image(s) failed alt-text generation"}
    if skipped:
        return {"status": "degraded", "skipped": skipped, "updated": updated,
                "reason": f"{skipped} of {skipped + updated} image(s) failed"}
    return {"status": "success", "updated": updated}


_UNSAFE_STEM_RE = re.compile(r"[^\w.-]+")


def safe_pdf_stem(pdf_path: Path) -> str:
    """Return a filesystem-safe stem for naming per-document outputs.

    Upload names are user-controlled. A name such as ``...pdf`` has the stem
    ``..``, which would turn the per-document output directory into the
    session directory's parent. Collapse anything outside ``[\\w.-]`` to
    ``_``, strip leading/trailing dots and underscores, and fall back to
    ``document`` when nothing usable remains.
    """
    stem = _UNSAFE_STEM_RE.sub("_", pdf_path.stem).strip("._")
    return stem or "document"


def prepare_pdf_output_dir(pdf_path: Path, output_dir: Path, clean_output: bool) -> Path:
    """Create (and optionally clean) the per-document directory under ``output_dir``.

    The result is always a direct child of ``output_dir``; nothing outside it
    is ever removed.
    """
    output_dir = Path(output_dir)
    pdf_output_dir = output_dir / safe_pdf_stem(pdf_path)
    if pdf_output_dir.resolve().parent != output_dir.resolve():
        raise ValueError(f"Refusing output directory outside session dir: {pdf_output_dir}")
    if clean_output and pdf_output_dir.exists():
        shutil.rmtree(pdf_output_dir)
    pdf_output_dir.mkdir(parents=True, exist_ok=True)
    return pdf_output_dir


def run_pipeline(
    pdf_path: str | Path,
    output_dir: str | Path,
    config: dict,
    progress_fn: Optional[Callable[[float, str], None]] = None,
    clean_output: bool = False,
) -> Dict[str, Any]:
    """Run the full accessibility pipeline on a single PDF.

    Args:
        pdf_path: Path to the input PDF file.
        output_dir: Base output directory (a subdirectory per PDF is created).
        config: Full config dict (all sections from config.yaml).
        progress_fn: Optional callback(fraction, description) for progress
            updates. Called with values from 0.0 to 1.0.
        clean_output: If True, delete the per-PDF output directory before
            processing (used by Gradio to avoid stale results).

    Returns:
        JSON-serializable dict with all pipeline results:
        - html_path, pdf_path, structure_json_path, language, title
        - audit (pre-remediation), post_audit (post-remediation)
        - alt_text, table_captions, remediation
        - report_path, post_report_path, compliance_report_path
        - download_files: list of output file paths
    """
    progress = progress_fn or _noop_progress

    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)

    # Validate that the file is actually a PDF (magic byte check)
    try:
        with open(pdf_path, "rb") as f:
            header = f.read(5)
        if header != b"%PDF-":
            raise ValueError(
                f"Not a valid PDF file (bad magic bytes): {pdf_path.name}"
            )
    except OSError as e:
        raise FileNotFoundError(f"Cannot read input file: {pdf_path} ({e})")

    conversion_options = config.get("conversion", {})
    audit_options = config.get("audit", {})
    remediation_options = dict(config.get("remediation", {}))
    # Heading strategies read conversion switches (heading_promote_first_text).
    remediation_options["conversion"] = config.get("conversion", {})
    output_options = config.get("output", {})
    llm_config = config.get("llm", {})

    pdf_output_dir = prepare_pdf_output_dir(pdf_path, output_dir, clean_output)
    pdf_stem = safe_pdf_stem(pdf_path)

    warnings = []

    logger.info("Processing: %s", pdf_path.name)

    # ── Pre-flight: recover owner-password-only encrypted PDFs ───────────
    # Owner-only ("permissions") encryption with an empty user password is
    # fully readable — decrypt it to a working copy (same basename, so output
    # naming is unchanged) and proceed. Only PDFs that genuinely require a
    # user password (or are otherwise unrecoverable) are rejected.
    from utils.pdf_encryption import recover_if_owner_encrypted
    _recovered = recover_if_owner_encrypted(pdf_path, pdf_output_dir)
    if _recovered is None:
        logger.error("PDF is encrypted/password-protected: %s", pdf_path.name)
        return {
            "source_pdf_path": str(pdf_path),
            "error": (
                "PDF is encrypted with a user password and cannot be processed. "
                "Remove the password protection before processing."
            ),
        }
    pdf_path = _recovered

    # ── Phase 1: Convert PDF to HTML via Docling ──────────────────────────
    progress(0.05, "Phase 1/11: Converting PDF to HTML... (may take as long as five minutes, depending on size)")
    logger.info("Phase 1: Docling conversion...")
    try:
        result = convert_pdf_to_html(
            pdf_path=str(pdf_path),
            output_dir=str(pdf_output_dir),
            options=conversion_options,
        )
    except Exception as e:
        logger.error("Phase 1 failed: %s", e)
        return {
            "source_pdf_path": str(pdf_path),
            "error": f"PDF conversion failed: {e}",
        }
    result["source_pdf_path"] = str(pdf_path)
    result["phase_status"] = {}

    stats = result.get("stats", {})
    logger.info(
        "  -> %s pages, %s figures, %s tables, %s formulas, language: %s, %ss",
        stats.get("pages", "?"), stats.get("figures", "?"),
        stats.get("tables", "?"), stats.get("formulas", "?"),
        result.get("language", "?"),
        stats.get("conversion_time_seconds", "?"),
    )
    result["phase_status"]["docling"] = {
        "status": "success",
        "pages": stats.get("pages"),
        "time_s": stats.get("conversion_time_seconds"),
    }

    # Structure-yield tripwire: a scanned doc for which Docling produced
    # implausibly little structure. Even if Phase-3 recovery later rescues
    # the TEXT, that recovery emits paragraphs only — headings/tables/lists
    # stay lost, so this fires regardless of downstream rescue. The outcome
    # record carries the same signal via classify_failure_reasons; this
    # WARNING makes it visible in live logs.
    if check_docling_yield_low(
        stats.get("text_classification"),
        stats.get("structure_elements"),
        stats.get("pages"),
        stats.get("pages_with_text"),
    ):
        logger.warning(
            "  Docling structure yield is implausibly low: %s elements across "
            "%s pages on a %s document — the output will lack "
            "headings/tables/lists even if text recovery succeeds.",
            stats.get("structure_elements"), stats.get("pages"),
            stats.get("text_classification"),
        )

    html_path = result["html_path"]
    image_dir = result.get("image_dir")
    image_files = result.get("image_files", [])

    # Load docling structure JSON
    _struct_path = result.get("structure_json_path")
    _struct_data = None
    if _struct_path and Path(_struct_path).exists():
        try:
            with open(_struct_path, encoding="utf-8") as f:
                _struct_data = json.load(f)
        except Exception as e:
            logger.warning("Failed to parse structure JSON %s: %s", _struct_path, e)

    # ── Phase 2: Fallback text recovery for hollow pages ─────────────────
    progress(0.12, "Phase 2/11: Text recovery (hollow pages)...")
    if conversion_options.get("fallback_text", True) and _struct_path and Path(_struct_path).exists():
        from converter.fallback_text_extractor import inject_fallback_elements
        try:
            fallback_stats = inject_fallback_elements(
                structure_json_path=_struct_path,
                source_pdf_path=str(pdf_path),
                min_words=conversion_options.get("fallback_min_words", 3),
            )
            result["fallback_recovery"] = fallback_stats
            if fallback_stats["pages_recovered"] > 0:
                logger.info(
                    "  Phase 2: Recovered %d hollow pages (%d fallback elements, pages: %s)",
                    fallback_stats["pages_recovered"],
                    fallback_stats["fallback_elements_added"],
                    fallback_stats["recovered_pages"],
                )
                # Reload structure data since it was modified
                with open(_struct_path, encoding="utf-8") as f:
                    _struct_data = json.load(f)
            else:
                logger.debug("  Phase 2: No hollow pages to recover")
        except Exception as e:
            logger.warning("  Fallback text recovery failed: %s", e)
            result["phase_status"]["fallback_text"] = {
                "status": "failed", "error": str(e)[:200], "type": type(e).__name__}

    # ── Phase 3: OCR recovery for scanned pages (Tesseract) ──────────────
    progress(0.15, "Phase 3/11: Scanning for pages needing OCR...")
    if conversion_options.get("ocr_recovery", True) and _struct_path and Path(_struct_path).exists():
        from converter.fallback_text_extractor import ocr_recover_pages
        try:
            progress(0.16, "Phase 3/11: Running Tesseract OCR on scanned pages...")
            ocr_stats = ocr_recover_pages(
                structure_json_path=_struct_path,
                source_pdf_path=str(pdf_path),
                min_words=conversion_options.get("fallback_min_words", 3),
                dpi=conversion_options.get("ocr_dpi", 150),
                progress_fn=lambda cur, total, pg: progress(
                    0.15 + 0.02 * cur / max(total, 1),
                    "Phase 3/11: OCR scanning page %d/%d (page %d)..." % (cur, total, pg),
                ),
            )
            result["ocr_recovery"] = ocr_stats
            if ocr_stats["pages_recovered"] > 0:
                progress(0.17, "Phase 3/11: OCR recovered %d scanned page(s)" % ocr_stats["pages_recovered"])
                logger.info(
                    "  Phase 3: OCR recovered %d scanned pages (%d elements, pages: %s)",
                    ocr_stats["pages_recovered"],
                    ocr_stats["fallback_elements_added"],
                    ocr_stats["recovered_pages"],
                )
                # Reload structure data since it was modified
                with open(_struct_path, encoding="utf-8") as f:
                    _struct_data = json.load(f)
            else:
                progress(0.17, "Phase 3/11: No scanned pages found")
                logger.debug("  Phase 3: No scanned pages to OCR")
        except Exception as e:
            logger.warning("  OCR recovery failed: %s", e)
            result["phase_status"]["ocr_recovery"] = {
                "status": "failed", "error": str(e)[:200], "type": type(e).__name__}

    # ── Phase 3b: OCR replacement for gibberish pages ──────────────────────
    if _struct_path and Path(_struct_path).exists():
        from converter.fallback_text_extractor import ocr_replace_gibberish_pages
        try:
            progress(0.18, "Phase 3b/11: Checking for gibberish pages (font encoding issues)...")
            gibberish_stats = ocr_replace_gibberish_pages(
                structure_json_path=_struct_path,
                source_pdf_path=str(pdf_path),
                dpi=conversion_options.get("ocr_dpi", 150),
                progress_fn=lambda cur, total, pg: progress(
                    0.18 + 0.01 * cur / max(total, 1),
                    "Phase 3b/11: OCR replacing page %d/%d (page %d)..." % (cur, total, pg),
                ),
            )
            if gibberish_stats["pages_replaced"] > 0:
                progress(0.19, "Phase 3b/11: OCR-replaced %d gibberish page(s)" % gibberish_stats["pages_replaced"])
                logger.info(
                    "  Phase 3b: OCR-replaced %d gibberish pages (%d elements, pages: %s)",
                    gibberish_stats["pages_replaced"],
                    gibberish_stats["elements_replaced"],
                    gibberish_stats["replaced_pages"],
                )
                # Reload structure data since it was modified
                with open(_struct_path, encoding="utf-8") as f:
                    _struct_data = json.load(f)
        except Exception as e:
            logger.warning("  OCR gibberish replacement failed: %s", e)
            result["phase_status"]["ocr_gibberish"] = {
                "status": "failed", "error": str(e)[:200], "type": type(e).__name__}

    # ── Phase 3c: Harvest author-written alt text from the source PDF ─────
    # Docling reconstructs structure from visual layout alone and never reads
    # the input's /StructTreeRoot, so alt text an author already wrote was
    # discarded at ingestion.  Recover it here — before the Phase 4 audit (so
    # those images are not flagged as missing alt) and before Phase 5 (so the
    # VLM skips them and never overwrites a human description).
    if (conversion_options.get("harvest_source_alt", True)
            and _struct_path and Path(_struct_path).exists()):
        from converter.alt_harvest import apply_harvest
        try:
            progress(0.19, "Phase 3c/11: Recovering author-written alt text...")
            harvest_stats = apply_harvest(
                structure_json_path=_struct_path,
                source_pdf_path=str(pdf_path),
                html_path=html_path,
            )
            result["alt_harvest"] = harvest_stats
            _harvested = (harvest_stats["matched_figures"]
                          + harvest_stats["matched_formulas"])
            if _harvested:
                logger.info(
                    "  Phase 3c: Recovered %d figure + %d formula alt text(s) "
                    "from the source PDF (mean %d chars); %d source alt(s) unmatched",
                    harvest_stats["matched_figures"],
                    harvest_stats["matched_formulas"],
                    harvest_stats["mean_alt_chars"],
                    len(harvest_stats["unmatched_source_alt"]),
                )
                result["phase_status"]["alt_harvest"] = {"status": "success"}
                # Reload structure data since apply_harvest modified it on disk
                with open(_struct_path, encoding="utf-8") as f:
                    _struct_data = json.load(f)
            elif harvest_stats.get("skipped_reason"):
                logger.debug("  Phase 3c: no harvest (%s)",
                             harvest_stats["skipped_reason"])
        except Exception as e:
            logger.warning("  Alt text harvest failed: %s", e)
            result["phase_status"]["alt_harvest"] = {
                "status": "failed", "error": str(e)[:200], "type": type(e).__name__}

    # ── Phase 4: Pre-remediation audit ────────────────────────────────────
    progress(0.20, "Phase 4/11: Pre-remediation audit...")
    logger.info("Phase 4: Pre-remediation audit...")
    auditor = AccessibilityAuditor(
        html_path=html_path,
        image_dir=image_dir,
        options=audit_options,
        pdf_path=str(pdf_path),
    )
    audit_result = _empty_audit_result()
    with phase_guard(result["phase_status"], "wcag_pre_audit", logger=logger):
        audit_result = auditor.audit()
    result["audit"] = audit_result

    summary = audit_result.get("summary", {})
    severity = summary.get("severity_counts", {})
    logger.info(
        "  -> Audit: %d issues (%d need remediation, %d compliant) — "
        "critical: %d, major: %d, minor: %d",
        summary.get("total_issues", 0),
        summary.get("needs_remediation", 0),
        summary.get("compliant", 0),
        severity.get("critical", 0),
        severity.get("major", 0),
        severity.get("minor", 0),
    )

    # Save pre-audit report
    if output_options.get("generate_report", True):
        report_format = output_options.get("report_format", "json")
        report_ext = "json" if report_format == "json" else "txt"
        report_path = pdf_output_dir / f"audit_report.{report_ext}"
        generate_report(audit_result, str(report_path), report_format)
        result["report_path"] = str(report_path)

    # ── Phase 5: Alt text via VLM ─────────────────────────────────────────
    progress(0.30, "Phase 5/11: Generating alt text via AI...")
    vlm_available = _has_vlm_configured(llm_config)

    if image_files and vlm_available:
        logger.info("Phase 5: Generating alt text via AI for %d images...", len(image_files))
        alt_result = {}
        try:
            alt_result = generate_alt_text_for_images(
                html_path=html_path,
                image_files=image_files,
                image_dir=image_dir,
                llm_config=llm_config,
                audit_result=audit_result,
                struct_data=_struct_data,
            )
            result["alt_text"] = alt_result
            if alt_result.get("warning"):
                warnings.append(alt_result["warning"])
            status = _vlm_phase_status(alt_result)
            result["phase_status"]["vlm_alt_text"] = status
            logger.info(
                "  -> Alt text: %d updated, %d skipped (%.1fs)",
                alt_result.get("updated_count", 0),
                alt_result.get("skipped_count", 0),
                alt_result.get("stats", {}).get("time_seconds", 0.0),
            )
            if status["status"] != "success":
                logger.warning(
                    "  -> Alt text phase %s: %s",
                    status["status"], status.get("reason", ""),
                )
        except Exception as e:
            logger.warning("Alt text generation failed: %s", e)
            result["phase_status"]["vlm_alt_text"] = {
                "status": "failed", "error": str(e)[:200], "type": type(e).__name__,
                "reason": "vlm_provider_error"}
        # Mark audit issues resolved by VLM so Phase 8 doesn't re-attempt them
        alt_texts = alt_result.get("alt_texts", {})
        if alt_texts:
            _marked = 0
            for issue in audit_result.get("issues", []):
                if issue.get("remediation_status") != "needs_remediation":
                    continue
                if issue.get("type") not in ("empty-alt-text", "missing-alt-text", "generic-alt-text"):
                    continue
                src = (issue.get("context") or {}).get("attributes", {}).get("src", "")
                vlm_text = alt_texts.get(src, "")
                # A placeholder is not a remediation: "Figure" passed the old
                # six-word list and closed the issue it should have kept open (G-07).
                if vlm_text and not is_generic_alt(vlm_text):
                    issue["remediation_status"] = "auto_remediated"
                    issue["remediation_source"] = "vlm"
                    _marked += 1
            if _marked:
                logger.info("  -> Marked %d alt-text audit issues as resolved by VLM", _marked)

    elif image_files:
        logger.info("  Skipping alt text: no VLM configured")
        # Generate stub image_records so the alt tag editor can show images
        # for manual alt text entry even when AI is disabled.
        result["alt_text"] = {
            "image_records": [
                {"src": Path(img).name, "file_path": img,
                 "alt_text": "", "outcome": "needs_manual_alt_text", "skip_reason": None}
                for img in image_files
            ],
            "alt_texts": {},
        }
    else:
        logger.info("  Skipping alt text: no images extracted")

    # Every image needs an editor record, whatever happened above.  The VLM
    # only returns records for images it processed, so an image that carries
    # harvested alt — or that the VLM skipped or failed on — would otherwise
    # be invisible in the Alt Text & Captions tab, leaving the user unable to
    # act on the "figures have no alt text" warning.
    if image_files:
        ensure_image_editor_records(
            result.setdefault("alt_text", {}), image_files, _struct_data
        )

    # ── Phase 6: Table captions via VLM ──────────────────────────────────
    progress(0.38, "Phase 6/11: Generating table captions via AI...")
    structure_json_path = result.get("structure_json_path")

    if vlm_available and structure_json_path:
        logger.info("Phase 6: Generating table captions via AI...")
        try:
            caption_result = generate_table_captions(
                html_path=html_path,
                pdf_path=str(pdf_path),
                structure_json_path=structure_json_path,
                llm_config=llm_config,
                audit_result=audit_result,
                vlm_render_dpi=conversion_options.get("vlm_render_dpi", 200),
            )
            result["table_captions"] = caption_result
            if caption_result.get("warning"):
                warnings.append(caption_result["warning"])
            if caption_result.get("error"):
                logger.warning("  -> Table caption error: %s", caption_result["error"])
                result["phase_status"]["vlm_table_captions"] = {
                    "status": "failed", "error": str(caption_result["error"])[:200],
                    "reason": "vlm_provider_error"}
            elif caption_result["updated_count"] > 0:
                logger.info(
                    "  -> Table captions: %d generated, %d skipped (%.1fs)",
                    caption_result["updated_count"],
                    caption_result["skipped_count"],
                    caption_result["stats"]["time_seconds"],
                )
                result["phase_status"]["vlm_table_captions"] = {"status": "success"}
        except Exception as e:
            logger.warning("Table caption generation failed: %s", e)
            result["phase_status"]["vlm_table_captions"] = {
                "status": "failed", "error": str(e)[:200], "type": type(e).__name__,
                "reason": "vlm_provider_error"}
        # Mark table-caption audit issues resolved by VLM so Phase 8 skips them
        tc_captions = result.get("table_captions", {}).get("captions", {})
        if tc_captions:
            _marked_tc = 0
            for issue in audit_result.get("issues", []):
                if issue.get("remediation_status") != "needs_remediation":
                    continue
                if issue.get("type") != "table-missing-caption":
                    continue
                tbl_idx = (issue.get("context") or {}).get("position", {}).get("index", -1)
                if f"table_{tbl_idx}" in tc_captions:
                    issue["remediation_status"] = "auto_remediated"
                    issue["remediation_source"] = "vlm"
                    _marked_tc += 1
            if _marked_tc:
                logger.info("  -> Marked %d table-caption audit issues as resolved by VLM", _marked_tc)

    # ── Phase 7: Formula captioning via VLM ────────────────────────────
    formula_config = config.get("formulas", {})
    if formula_config.get("enabled", True) and vlm_available and structure_json_path:
        progress(0.45, "Phase 7/11: Generating formula descriptions via AI...")
        logger.info("Phase 7: Generating formula descriptions via AI...")
        try:
            formula_result = process_formulas(
                html_path=html_path,
                structure_json_path=structure_json_path,
                pdf_path=str(pdf_path),
                llm_config=llm_config,
                formula_config=formula_config,
            )
            result["formula_descriptions"] = formula_result
            result["phase_status"]["vlm_formulas"] = {"status": "success"}
            if formula_result.get("warning"):
                warnings.append(formula_result["warning"])
            if formula_result.get("error"):
                logger.warning("  -> Formula captioning error: %s", formula_result["error"])
                result["phase_status"]["vlm_formulas"] = {
                    "status": "failed", "error": str(formula_result["error"])[:200],
                    "reason": "vlm_provider_error"}
            elif formula_result["updated_count"] > 0:
                logger.info(
                    "  -> Formulas: %d described, %d skipped (%.1fs)",
                    formula_result["updated_count"],
                    formula_result["skipped_count"],
                    formula_result["stats"]["time_seconds"],
                )

                # Best-effort MathML enrichment in HTML
                if formula_config.get("mathml_conversion", True):
                    enriched = enrich_html_with_formulas(
                        html_path=html_path,
                        structure_json_path=structure_json_path,
                        formula_config=formula_config,
                    )
                    if enriched:
                        logger.info("  -> MathML: %d formulas enriched in HTML", enriched)

                # Mark formula audit issues resolved by VLM
                _marked_f = 0
                for issue in audit_result.get("issues", []):
                    if issue.get("remediation_status") != "needs_remediation":
                        continue
                    if issue.get("type") not in (
                        "formula-missing-alt-text",
                        "formula-generic-alt-text",
                        "formula-missing-description",
                    ):
                        continue
                    issue["remediation_status"] = "auto_remediated"
                    issue["remediation_source"] = "vlm"
                    _marked_f += 1
                if _marked_f:
                    logger.info("  -> Marked %d formula audit issues as resolved by VLM", _marked_f)
        except Exception as e:
            logger.warning("Formula captioning failed: %s", e)
            result["phase_status"]["vlm_formulas"] = {
                "status": "failed", "error": str(e)[:200], "type": type(e).__name__,
                "reason": "vlm_provider_error"}

    # ── Phase 8: Remediate accessibility issues ───────────────────────────
    progress(0.52, "Phase 8/11: Remediating HTML issues...")
    needs_remediation = [
        i for i in audit_result.get("issues", [])
        # informational issues are non-gating but still remediable (no-h1).
        if i.get("remediation_status") in ("needs_remediation", "informational")
    ]

    # Reload structure data in case VLM phases (5-7) modified it on disk
    if _struct_path and Path(_struct_path).exists():
        try:
            with open(_struct_path, encoding="utf-8") as f:
                _struct_data = json.load(f)
        except Exception:
            pass  # keep existing _struct_data

    if needs_remediation:
        logger.info("Phase 8: Remediating %d issues...", len(needs_remediation))
        remediation_result = None
        with phase_guard(result["phase_status"], "remediation", logger=logger):
            remediation_options.update(_table_ai_options(config))
            remediation_result = remediate_html(
                html_path=html_path,
                issues=needs_remediation,
                options=remediation_options,
                struct_data=_struct_data,
            )
        if remediation_result is None:
            remediation_result = {"stats": {}, "change_records": []}
        result["remediation"] = remediation_result

        rem_stats = remediation_result.get("stats", {})
        logger.info(
            "  -> Remediation: %d fixed, %d skipped, %d failed",
            rem_stats.get("remediated", 0),
            rem_stats.get("skipped", 0),
            rem_stats.get("failed", 0),
        )
    else:
        logger.info("  No issues require remediation, skipping Phase 8")

    # ── Phase 9: Post-remediation audit ───────────────────────────────────
    progress(0.60, "Phase 9/11: Post-remediation audit...")
    logger.info("Phase 9: Post-remediation audit...")
    post_auditor = AccessibilityAuditor(
        html_path=html_path,
        image_dir=image_dir,
        options=audit_options,
        pdf_path=str(pdf_path),
    )
    post_audit_result = _empty_audit_result()
    with phase_guard(result["phase_status"], "wcag_post_audit", logger=logger):
        post_audit_result = post_auditor.audit()
    result["post_audit"] = post_audit_result

    post_compliance = post_audit_result.get("compliance", {})
    logger.info(
        "  -> Post-audit score: %s%%",
        post_compliance.get("overall_score", "?"),
    )

    if output_options.get("generate_report", True):
        report_format = output_options.get("report_format", "json")
        report_ext = "json" if report_format == "json" else "txt"
        post_report_path = pdf_output_dir / f"post_audit_report.{report_ext}"
        generate_report(post_audit_result, str(post_report_path), report_format)
        result["post_report_path"] = str(post_report_path)

    # ── Phase 10: Output generation (PDF, reports) ────────────────────────
    progress(0.70, "Phase 10/11: Generating reports...")

    # Combined Verification Report (compliance + document changes)
    _report_path = pdf_output_dir / f"{pdf_stem}_WCAG_validation_report.html"
    _report_kw = _build_report_kwargs(result, post_audit_result, pdf_path, config=config)
    _report_generated = generate_changes_report_html(
        output_path=str(_report_path), **_report_kw,
    )
    if _report_generated:
        result["compliance_report_path"] = str(_report_path)

    # ── Phase 11: Tagged PDF + bookmarks ──────────────────────────────────
    progress(0.80, "Phase 11/11: Generating tagged PDF...")
    structure_json_path = result.get("structure_json_path")
    pdf_engine = output_options.get("pdf_engine", "auto")

    if output_options.get("generate_pdf", True):
        pdf_out = pdf_output_dir / f"{pdf_stem}_accessible.pdf"

        # Try tagged PDF first (preserves original layout)
        if pdf_engine != "weasyprint" and structure_json_path:
            logger.info("  Generating tagged PDF (preserving original layout)...")
            remediation_data = {"html_path": html_path}
            alt_text_result = result.get("alt_text", {})
            if alt_text_result and not alt_text_result.get("error"):
                remediation_data["alt_texts"] = alt_text_result.get("alt_texts", {})

            # Divide the heap budget by the taggers actually in flight —
            # batch_workers describes CLI mode, not the Celery worker.
            _tagger_slots = resolve_tagger_slots(config)
            _java_heap = compute_java_heap(config, batch_workers=_tagger_slots)

            _tag_mode = output_options.get("tag_mode", "content-preserving")

            _verdict_cfg = config.get("verdict", {})
            _coverage_warn = _verdict_cfg.get(
                "coverage_warn_pct",
                _verdict_cfg.get("min_coverage", 0.80) * 100,
            )

            pdf_tag_result = tag_pdf(
                original_pdf_path=str(pdf_path),
                structure_json_path=structure_json_path,
                output_pdf_path=str(pdf_out),
                remediation_data=remediation_data or None,
                language=result.get("language", "en-US"),
                engine=pdf_engine,
                title=result.get("title", pdf_path.stem),
                java_heap=_java_heap,
                tag_mode=_tag_mode,
                coverage_warn_pct=_coverage_warn,
                coverage_aware_fallback=output_options.get("coverage_aware_fallback", True),
                fallback_min_coverage=output_options.get("fallback_min_coverage", 1.0),
                font_substitution_report=output_options.get("font_substitution_report", True),
                normalize_block_order=output_options.get("normalize_block_order", True),
                table_first_row_th_fallback=output_options.get(
                    "table_first_row_th_fallback", True),
                split_reclassified_figures=output_options.get(
                    "split_reclassified_figures", True),
            )
            if pdf_tag_result:
                result["pdf_path"] = pdf_tag_result["path"]
                # HTML remediation may have produced a better document title
                # than the one extracted before remediation. Adopt it before
                # the PDF bookmarks and the WCAG validation report are built
                # from result["title"], so they agree with the PDF metadata.
                if pdf_tag_result.get("document_title"):
                    result["title"] = pdf_tag_result["document_title"]
                result["matching_summary"] = pdf_tag_result.get("matching_summary")
                result["struct_tree_issues"] = pdf_tag_result.get("struct_tree_issues", [])
                result["stripped_text_pages"] = pdf_tag_result.get("stripped_text_pages", [])
                result["source_quality_warnings"] = pdf_tag_result.get("source_quality_warnings", {})
                result["phase_status"]["tagging"] = {
                    "status": "success",
                    "mode": pdf_tag_result.get("effective_mode", _tag_mode),
                }
                logger.info("  -> Tagged PDF: %s", pdf_tag_result["path"])

                # Generate user-facing warnings for lossy font issues only
                sqw = result["source_quality_warnings"]
                if sqw.get("substitution_fonts_injected", 0) > 0:
                    warnings.append(
                        "Substitution fonts were injected — the tagged PDF may "
                        "render incorrectly in Acrobat. The HTML output is "
                        "recommended, or obtaining a newer copy of the source document."
                    )
                if sqw.get("tounicode_cmaps_failed", 0) > 0:
                    warnings.append(
                        f"Source PDF has {sqw['tounicode_cmaps_failed']} font(s) "
                        f"where Unicode mapping could not be generated"
                    )
            else:
                logger.error("  -> Tagged PDF generation failed")
                warnings.append(
                    "PDF tagging failed. The accessible HTML output is still "
                    "available. No accessible PDF was generated."
                )
                result["pdf_error"] = (
                    "Tagging failed: content-preserving and overlay modes "
                    "both unsuccessful"
                )
                result["phase_status"]["tagging"] = {
                    "status": "failed",
                    "error": result["pdf_error"],
                }
        elif not structure_json_path:
            logger.error("  -> No structure JSON available, cannot generate tagged PDF")
            warnings.append(
                "PDF tagging skipped: document structure extraction failed. "
                "The accessible HTML output is still available."
            )
            result["pdf_error"] = "No structure JSON from Docling conversion"

        # WeasyPrint only used if user explicitly chose it
        if pdf_engine == "weasyprint" and "pdf_path" not in result:
            inject_accessible_css(html_path)
            pdf_result = generate_pdf(html_path, str(pdf_out))
            if pdf_result:
                result["pdf_path"] = pdf_result
                logger.info("  -> PDF (WeasyPrint re-render): %s", pdf_result)
                warnings.append(
                    "PDF was generated via WeasyPrint re-render (explicitly "
                    "configured). Layout differs from original, and screen "
                    "reader support is limited."
                )
    else:
        inject_accessible_css(html_path)

    # PDF bookmarks
    if result.get("pdf_path") and result.get("structure_json_path"):
        generate_pdf_bookmarks(
            pdf_path=result["pdf_path"],
            structure_json_path=result["structure_json_path"],
            doc_title=result.get("title", ""),
        )

    # ── Screen reader quality check ─────────────────────────────────────
    if result.get("pdf_path"):
        try:
            from validation.gates import (
                quick_screen_reader_check,
                DEFAULT_PAGE_LOW_COVERAGE,
                DEFAULT_SR_POOR_COVERAGE,
                DEFAULT_SR_FAIR_COVERAGE,
            )
            _vc = config.get("verdict", {}) if config else {}
            sr_result = quick_screen_reader_check(
                tagged_pdf=result["pdf_path"],
                source_pdf=str(pdf_path),
                matching_summary=result.get("matching_summary"),
                tagger_mode=result.get("phase_status", {}).get(
                    "tagging", {}
                ).get("mode", ""),
                ocr_quality_cfg=config.get("ocr_quality", {}) if config else {},
                coverage_threshold=_vc.get("sr_page_low_coverage", DEFAULT_PAGE_LOW_COVERAGE),
                sr_poor_coverage=_vc.get("sr_quality_poor_coverage", DEFAULT_SR_POOR_COVERAGE),
                sr_fair_coverage=_vc.get("sr_quality_fair_coverage", DEFAULT_SR_FAIR_COVERAGE),
                two_witness=_vc.get("reading_order_two_witness", True),
                exclude_figure_text=_vc.get("reading_order_exclude_figure_text", True),
                reading_order_glued_guard=_vc.get("reading_order_glued_guard", True),
                reading_order_geo_tabular_guard=_vc.get("reading_order_geo_tabular_guard", True),
                reading_order_small_n_guard=_vc.get("reading_order_small_n_guard", False),
                reading_order_hyphen_normalize=_vc.get("reading_order_hyphen_normalize", True),
                corroborate_page_deficits=_vc.get("corroborate_page_deficits", True),
                deficit_corroboration_threshold=_vc.get("min_coverage", 0.80),
            )
            result["sr_quality"] = sr_result

            # Tell the user their download is incomplete.  Counted from the
            # delivered PDF (not the harvest), so it cannot disagree with what
            # they actually receive.
            _fig_missing = sr_result.get("figures_missing_alt", 0)
            _fig_total = sr_result.get("figures_total", 0)
            if _fig_total > 0 and _fig_missing > 0:
                warnings.append(
                    f"{_fig_missing} of {_fig_total} figures have no alt text "
                    f"— add descriptions in the Alt Text & Captions tab before "
                    f"publishing. This document is not accessible until "
                    f"every figure has alt text."
                )

            logger.info(
                "  -> SR quality: %s (coverage=%.0f%%, issues=%d)",
                sr_result.get("sr_quality", "unknown"),
                sr_result.get("avg_coverage", 0) * 100,
                len(sr_result.get("issues", [])),
            )

            # Inject PDF-level findings into post-audit so scores and
            # reports reflect the actual tagged PDF state.
            if result.get("post_audit"):
                _inject_sr_issues_into_post_audit(
                    result["post_audit"], sr_result
                )
                if result.get("struct_tree_issues"):
                    _inject_tagger_issues_into_post_audit(
                        result["post_audit"], result["struct_tree_issues"]
                    )
                # Regenerate JSON report with updated post-audit
                if result.get("post_report_path"):
                    generate_report(
                        result["post_audit"],
                        result["post_report_path"],
                        output_options.get("report_format", "json"),
                    )
                # Regenerate changes report with updated post-audit
                _report_path = (
                    pdf_output_dir
                    / f"{pdf_stem}_WCAG_validation_report.html"
                )
                _report_kw = _build_report_kwargs(
                    result, result["post_audit"], pdf_path, config=config,
                )
                _regen = generate_changes_report_html(
                    output_path=str(_report_path), **_report_kw,
                )
                if _regen:
                    result["compliance_report_path"] = str(_report_path)

        except Exception as e:
            logger.warning("Screen reader quality check failed: %s", e)
            result["sr_quality"] = {
                "sr_quality": "unknown",
                "avg_coverage": 0.0,
                "hollow_pages": [],
                "issues": [f"Screen reader quality check failed: {e}"],
                "low_coverage_pages": [],
                "page_deficits": [],
                "figures_total": 0,
                "figures_missing_alt": 0,
                "element_match_rate": None,
                "tagger_mode": "",
                "marked": False,
                "has_struct_tree": False,
                "document_lang": None,
                "total_leaves": 0,
                "total_pages": 0,
                "scanned_pages": [],
                "per_page_ocr_quality": [],
                "ocr_pages_passing_pct": None,
            }

    # ── Embed images as base64 (after VLM + remediation, before downloads) ──
    if config.get("conversion", {}).get("embed_images", False):
        try:
            from converter.docling_converter import embed_images_as_base64
            embed_images_as_base64(html_path)
        except Exception as e:
            logger.warning("Image embedding failed: %s", e)

    # ── Collect download files ────────────────────────────────────────────
    download_files = []
    if Path(html_path).exists():
        download_files.append(html_path)
    if result.get("pdf_path") and Path(result["pdf_path"]).exists():
        download_files.append(result["pdf_path"])
    if result.get("compliance_report_path") and Path(result["compliance_report_path"]).exists():
        download_files.append(result["compliance_report_path"])

    result["download_files"] = download_files
    if warnings:
        result["warnings"] = warnings

    progress(1.0, "Done")
    return result


def _apply_structure_order_edits(result, order_edits):
    """Persist reading-order reorders to the enriched structure JSON.

    Runs BEFORE re-tag so the iText tagger reads the new array order. Returns
    the list of changed page numbers (for logging)."""
    if not order_edits:
        return []
    struct_path = result.get("structure_json_path")
    if not struct_path or not Path(struct_path).exists():
        return []
    try:
        changed = apply_sr_order_edits(struct_path, order_edits)
        if changed:
            logger.info("  Applied reading-order reorder to page(s) %s",
                        ", ".join(str(p) for p in changed))
        return changed
    except Exception as e:  # never let a reorder break the re-tag
        logger.warning("Failed to apply reading-order edits: %s", e)
        return []


def rerun_output_phases(
    pipeline_result: Dict[str, Any],
    config: dict,
    image_edits: Optional[Dict[str, str]] = None,
    table_edits: Optional[Dict[str, str]] = None,
    formula_edits: Optional[Dict[str, str]] = None,
    text_edits: Optional[Dict[str, str]] = None,
    order_edits: Optional[Dict[int, List[str]]] = None,
    progress_fn: Optional[Callable[[float, str], None]] = None,
) -> Dict[str, Any]:
    """Re-run output phases (5-7) after user edits captions.

    Applies image alt text, table caption, formula description, and
    structure text edits, then re-runs post-audit, changes report
    generation, and PDF tagging.

    Args:
        pipeline_result: Full result dict from the original run_pipeline() call.
        config: Full config dict.
        image_edits: Dict mapping img src -> new alt text.
        table_edits: Dict mapping "table_N" -> new caption text.
        formula_edits: Dict mapping element ID -> new description text.
        text_edits: Dict mapping element ID -> new text (for structure elements).
        order_edits: Dict mapping page_no -> ordered list of element IDs
            (reorders that page's top-level blocks before re-tagging).
        progress_fn: Optional progress callback.

    Returns:
        Updated pipeline_result dict with new post_audit, reports, and PDF.
    """
    progress = progress_fn or _noop_progress
    result = copy.deepcopy(pipeline_result)

    html_path = result.get("html_path")
    if not html_path or not Path(html_path).exists():
        raise FileNotFoundError(f"HTML file not found: {html_path}")

    # ── Apply caption edits to HTML ──────────────────────────────────────
    progress(0.10, "Applying caption edits to HTML...")

    html_content = Path(html_path).read_text(encoding="utf-8")
    soup = BeautifulSoup(html_content, "html.parser")
    edits_applied = 0

    # Apply image alt text edits
    if image_edits:
        for src, new_alt in image_edits.items():
            img_tag = soup.find("img", attrs={"src": src})
            if img_tag:
                img_tag["alt"] = new_alt
                edits_applied += 1
                logger.info("  Updated alt text for image src=%s", src)

        # Also update the alt_texts dict for structure JSON merge
        alt_text_data = result.get("alt_text", {})
        alt_texts_dict = dict(alt_text_data.get("alt_texts", {}))
        image_records = list(alt_text_data.get("image_records", []))
        for src, new_alt in image_edits.items():
            alt_texts_dict[src] = new_alt
            # Update image_records too
            for rec in image_records:
                if rec.get("src") == src:
                    rec["alt_text"] = new_alt
                    break
        alt_text_data = dict(alt_text_data)
        alt_text_data["alt_texts"] = alt_texts_dict
        alt_text_data["image_records"] = image_records
        result["alt_text"] = alt_text_data

    # Apply table caption edits
    if table_edits:
        tables = soup.find_all("table")
        for tbl_key, new_caption in table_edits.items():
            idx = int(tbl_key.replace("table_", ""))
            if idx < len(tables):
                table = tables[idx]
                caption_tag = table.find("caption")
                if caption_tag:
                    caption_tag.string = new_caption
                else:
                    new_cap = soup.new_tag("caption")
                    new_cap.string = new_caption
                    table.insert(0, new_cap)
                edits_applied += 1
                logger.info("  Updated caption for %s", tbl_key)

        # Update table_captions in result
        tc_data = result.get("table_captions", {})
        if tc_data:
            tc_data = dict(tc_data)
            captions = dict(tc_data.get("captions", {}))
            captions.update(table_edits)
            tc_data["captions"] = captions
            result["table_captions"] = tc_data

    # Apply formula description edits
    if formula_edits:
        for elem_id, new_desc in formula_edits.items():
            # Update aria-label in HTML
            tag = soup.find(attrs={"data-formula-index": True})
            # Find the specific formula div via structure JSON match
            _struct_path = result.get("structure_json_path")
            if _struct_path and Path(_struct_path).exists():
                try:
                    with open(_struct_path, encoding="utf-8") as f:
                        _struct = json.load(f)
                    _formula_seq = 0
                    for el in _struct.get("elements", []):
                        if el.get("type") == "FormulaItem" and el.get("tag") == "Formula":
                            if el.get("id") == elem_id:
                                ftag = soup.find(attrs={"data-formula-index": str(_formula_seq)})
                                if ftag:
                                    ftag["aria-label"] = new_desc
                                    edits_applied += 1
                                    logger.info("  Updated description for formula %s", elem_id)
                                # Also update structure JSON
                                el["alt_text"] = new_desc
                                break
                            _formula_seq += 1
                    with open(_struct_path, "w", encoding="utf-8") as f:
                        json.dump(_struct, f, indent=2, ensure_ascii=False)
                except Exception as e:
                    logger.warning("Failed to apply formula edit for %s: %s", elem_id, e)

        # Update formula_descriptions in result
        fd_data = result.get("formula_descriptions", {})
        if fd_data:
            fd_data = dict(fd_data)
            descs = dict(fd_data.get("descriptions", {}))
            for elem_id, new_desc in formula_edits.items():
                if elem_id in descs:
                    descs[elem_id] = dict(descs[elem_id])
                    descs[elem_id]["description"] = new_desc
            fd_data["descriptions"] = descs
            result["formula_descriptions"] = fd_data

    # Apply reading-order reorders to the enriched JSON (before re-tag).
    _apply_structure_order_edits(result, order_edits)

    # Apply structure text edits directly to enriched JSON
    if text_edits:
        _struct_path = result.get("structure_json_path")
        if _struct_path and Path(_struct_path).exists():
            try:
                with open(_struct_path, encoding="utf-8") as f:
                    _struct = json.load(f)
                _text_edits_applied = 0
                for el in _struct.get("elements", []):
                    eid = el.get("id", "")
                    # Direct element text edit
                    if eid in text_edits:
                        if el.get("tag") in ("Figure", "Formula"):
                            el["alt_text"] = text_edits[eid]
                        else:
                            el["text"] = text_edits[eid]
                        _text_edits_applied += 1
                    # Table cell edits (elem_id.cell_R_C format)
                    for cell in (el.get("cells") or []):
                        cell_key = f"{eid}.cell_{cell.get('row', 0)}_{cell.get('col', 0)}"
                        if cell_key in text_edits:
                            cell["text"] = text_edits[cell_key]
                            _text_edits_applied += 1
                    # Child element edits
                    for child in (el.get("children") or []):
                        child_key = child.get("id", f"{eid}.child_{child.get('tag', '')}")
                        if child_key in text_edits:
                            child["text"] = text_edits[child_key]
                            _text_edits_applied += 1
                    # List item edits — positional path keys (elem_0015.LI3.LBody0)
                    # resolved by the same generator the renderer emits, so keys
                    # match. The leaf is the text the iText tagger reads (LBody /
                    # Lbl label / flat LI). See utils.editor_input.
                    if el.get("tag") in ("L", "List"):
                        for lkey, leaf_node, li_node, kind in iter_list_edit_targets(el, eid):
                            if lkey in text_edits:
                                leaf_node["text"] = text_edits[lkey]
                                if kind == "lbody":
                                    li_node["text"] = text_edits[lkey]  # keep LI consistent
                                _text_edits_applied += 1
                with open(_struct_path, "w", encoding="utf-8") as f:
                    json.dump(_struct, f, indent=2, ensure_ascii=False)
                edits_applied += _text_edits_applied
                logger.info("  Applied %d text edits to structure JSON", _text_edits_applied)
            except Exception as e:
                logger.warning("Failed to apply text edits: %s", e)

    if edits_applied > 0:
        Path(html_path).write_text(str(soup), encoding="utf-8")
        logger.info("  Saved %d caption edits to HTML", edits_applied)

    # ── Re-run: Post-remediation audit ──────────────────────────────────
    progress(0.30, "Re-running post-remediation audit...")
    audit_options = config.get("audit", {})
    image_dir = result.get("image_dir")

    post_auditor = AccessibilityAuditor(
        html_path=html_path,
        image_dir=image_dir,
        options=audit_options,
        pdf_path=result.get("source_pdf_path"),
    )
    post_audit_result = post_auditor.audit()
    result["post_audit"] = post_audit_result

    output_options = config.get("output", {})
    pdf_output_dir = Path(html_path).parent

    if output_options.get("generate_report", True):
        report_format = output_options.get("report_format", "json")
        report_ext = "json" if report_format == "json" else "txt"
        post_report_path = pdf_output_dir / f"post_audit_report.{report_ext}"
        generate_report(post_audit_result, str(post_report_path), report_format)
        result["post_report_path"] = str(post_report_path)

    # ── Re-run: Changes report ──────────────────────────────────────────
    progress(0.50, "Regenerating changes report...")
    source_pdf = Path(result.get("source_pdf_path", result.get("original_pdf_path", "")))
    _report_path = pdf_output_dir / f"{safe_pdf_stem(source_pdf)}_WCAG_validation_report.html"
    _report_kw = _build_report_kwargs(result, post_audit_result, source_pdf, config=config)
    _report_generated = generate_changes_report_html(
        output_path=str(_report_path), **_report_kw,
    )
    if _report_generated:
        result["compliance_report_path"] = str(_report_path)

    # ── Re-run: Tagged PDF + bookmarks ──────────────────────────────────
    progress(0.70, "Regenerating tagged PDF...")
    structure_json_path = result.get("structure_json_path")
    pdf_engine = output_options.get("pdf_engine", "auto")

    if output_options.get("generate_pdf", True):
        pdf_out = pdf_output_dir / f"{safe_pdf_stem(source_pdf)}_accessible.pdf"

        # Remove old PDF so we can detect success/failure
        if "pdf_path" in result:
            result.pop("pdf_path")

        if pdf_engine != "weasyprint" and structure_json_path:
            remediation_data = {"html_path": html_path}
            alt_text_result = result.get("alt_text", {})
            if alt_text_result and not alt_text_result.get("error"):
                remediation_data["alt_texts"] = alt_text_result.get("alt_texts", {})

            # Divide the heap budget by the taggers actually in flight —
            # batch_workers describes CLI mode, not the Celery worker.
            _tagger_slots = resolve_tagger_slots(config)
            _java_heap = compute_java_heap(config, batch_workers=_tagger_slots)
            _tag_mode = output_options.get("tag_mode", "content-preserving")
            _verdict_cfg2 = config.get("verdict", {})
            _coverage_warn2 = _verdict_cfg2.get(
                "coverage_warn_pct",
                _verdict_cfg2.get("min_coverage", 0.80) * 100,
            )

            pdf_tag_result = tag_pdf(
                original_pdf_path=str(result.get("source_pdf_path", result.get("original_pdf_path", ""))),
                structure_json_path=structure_json_path,
                output_pdf_path=str(pdf_out),
                remediation_data=remediation_data,
                language=result.get("language", "en-US"),
                engine=pdf_engine,
                title=result.get("title", source_pdf.stem),
                java_heap=_java_heap,
                tag_mode=_tag_mode,
                coverage_warn_pct=_coverage_warn2,
                coverage_aware_fallback=output_options.get("coverage_aware_fallback", True),
                fallback_min_coverage=output_options.get("fallback_min_coverage", 1.0),
                font_substitution_report=output_options.get("font_substitution_report", True),
                normalize_block_order=output_options.get("normalize_block_order", True),
                table_first_row_th_fallback=output_options.get(
                    "table_first_row_th_fallback", True),
                split_reclassified_figures=output_options.get(
                    "split_reclassified_figures", True),
            )
            if pdf_tag_result:
                result["pdf_path"] = pdf_tag_result["path"]
                # HTML remediation may have produced a better document title
                # than the one extracted before remediation. Adopt it before
                # the PDF bookmarks and the WCAG validation report are built
                # from result["title"], so they agree with the PDF metadata.
                if pdf_tag_result.get("document_title"):
                    result["title"] = pdf_tag_result["document_title"]
                result["matching_summary"] = pdf_tag_result.get("matching_summary")
                result["struct_tree_issues"] = pdf_tag_result.get("struct_tree_issues", [])
                result["stripped_text_pages"] = pdf_tag_result.get("stripped_text_pages", [])
                result["source_quality_warnings"] = pdf_tag_result.get("source_quality_warnings", {})
                result["phase_status"]["tagging"] = {
                    "status": "success",
                    "mode": pdf_tag_result.get("effective_mode", _tag_mode),
                }
            else:
                logger.error("  -> Tagged PDF generation failed during update")
                result["pdf_error"] = (
                    "Tagging failed during PDF update: content-preserving and "
                    "overlay modes both unsuccessful"
                )

    # PDF bookmarks
    if result.get("pdf_path") and result.get("structure_json_path"):
        generate_pdf_bookmarks(
            pdf_path=result["pdf_path"],
            structure_json_path=result["structure_json_path"],
            doc_title=result.get("title", ""),
        )

    # Screen reader quality check
    if result.get("pdf_path"):
        try:
            from validation.gates import (
                quick_screen_reader_check,
                DEFAULT_PAGE_LOW_COVERAGE,
                DEFAULT_SR_POOR_COVERAGE,
                DEFAULT_SR_FAIR_COVERAGE,
            )
            _vc = config.get("verdict", {}) if config else {}
            sr_result = quick_screen_reader_check(
                tagged_pdf=result["pdf_path"],
                source_pdf=str(result.get("source_pdf_path", result.get("original_pdf_path", ""))),
                matching_summary=result.get("matching_summary"),
                tagger_mode=result.get("phase_status", {}).get(
                    "tagging", {}
                ).get("mode", ""),
                ocr_quality_cfg=config.get("ocr_quality", {}) if config else {},
                coverage_threshold=_vc.get("sr_page_low_coverage", DEFAULT_PAGE_LOW_COVERAGE),
                sr_poor_coverage=_vc.get("sr_quality_poor_coverage", DEFAULT_SR_POOR_COVERAGE),
                sr_fair_coverage=_vc.get("sr_quality_fair_coverage", DEFAULT_SR_FAIR_COVERAGE),
                two_witness=_vc.get("reading_order_two_witness", True),
                exclude_figure_text=_vc.get("reading_order_exclude_figure_text", True),
                reading_order_glued_guard=_vc.get("reading_order_glued_guard", True),
                reading_order_geo_tabular_guard=_vc.get("reading_order_geo_tabular_guard", True),
                reading_order_small_n_guard=_vc.get("reading_order_small_n_guard", False),
                reading_order_hyphen_normalize=_vc.get("reading_order_hyphen_normalize", True),
                corroborate_page_deficits=_vc.get("corroborate_page_deficits", True),
                deficit_corroboration_threshold=_vc.get("min_coverage", 0.80),
            )
            result["sr_quality"] = sr_result

            # Inject PDF-level findings into post-audit
            if result.get("post_audit"):
                _inject_sr_issues_into_post_audit(
                    result["post_audit"], sr_result
                )
                if result.get("struct_tree_issues"):
                    _inject_tagger_issues_into_post_audit(
                        result["post_audit"], result["struct_tree_issues"]
                    )
                # Regenerate JSON report with updated post-audit
                if result.get("post_report_path"):
                    generate_report(
                        result["post_audit"],
                        result["post_report_path"],
                        output_options.get("report_format", "json"),
                    )
                # Regenerate changes report with updated post-audit
                _report_path = (
                    pdf_output_dir
                    / f"{safe_pdf_stem(source_pdf)}_WCAG_validation_report.html"
                )
                _report_kw = _build_report_kwargs(
                    result, result["post_audit"], source_pdf, config=config,
                )
                _regen = generate_changes_report_html(
                    output_path=str(_report_path), **_report_kw,
                )
                if _regen:
                    result["compliance_report_path"] = str(_report_path)

        except Exception as e:
            logger.warning("Screen reader quality check failed: %s", e)
            result["sr_quality"] = {
                "sr_quality": "unknown",
                "avg_coverage": 0.0,
                "hollow_pages": [],
                "issues": [f"Screen reader quality check failed: {e}"],
                "low_coverage_pages": [],
                "page_deficits": [],
                "figures_total": 0,
                "figures_missing_alt": 0,
                "element_match_rate": None,
                "tagger_mode": "",
                "marked": False,
                "has_struct_tree": False,
                "document_lang": None,
                "total_leaves": 0,
                "total_pages": 0,
                "scanned_pages": [],
                "per_page_ocr_quality": [],
                "ocr_pages_passing_pct": None,
            }

    # ── Embed images as base64 (after edits, before downloads) ─────────
    if config.get("conversion", {}).get("embed_images", False):
        try:
            from converter.docling_converter import embed_images_as_base64
            embed_images_as_base64(html_path)
        except Exception as e:
            logger.warning("Image embedding failed: %s", e)

    # ── Rebuild download files ───────────────────────────────────────────
    progress(0.90, "Finalizing...")
    download_files = []
    if Path(html_path).exists():
        download_files.append(html_path)
    if result.get("pdf_path") and Path(result["pdf_path"]).exists():
        download_files.append(result["pdf_path"])
    if result.get("compliance_report_path") and Path(result["compliance_report_path"]).exists():
        download_files.append(result["compliance_report_path"])
    result["download_files"] = download_files

    progress(1.0, "Done")
    return result
