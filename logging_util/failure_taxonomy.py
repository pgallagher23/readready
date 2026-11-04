# SPDX-License-Identifier: AGPL-3.0-or-later
"""Failure-reason taxonomy for the accessibility pipeline.

A fixed set of reason codes (``FailureReason``) plus ``classify_failure_reasons``,
a *pure* function that maps a per-PDF outcome record to a list of structured
reason dicts ``{code, severity, detail, signal}``. The point is goal #2 — turn
"this PDF couldn't be remediated" into queryable codes instead of ad-hoc strings.

The mapping tables fold the existing ad-hoc identifiers (struct-tree validator
``check`` codes, post-audit issue ``type`` codes, manifest gate names) into the
taxonomy. Unmapped ``pdf-*`` codes surface as ``unknown_error`` with the raw code
preserved in ``detail`` so nothing silently disappears.
"""
import enum
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

# Verdict thresholds + resolver are owned by ``audit.standards`` (the single
# source of truth, also used by ``compute_wcag_verdict``) so the taxonomy's
# gate boundaries can never drift from the verdict's. ``audit.standards`` is
# import-pure (typing only), so this keeps the module free of import-time I/O.
from audit.standards import emr_passes, resolve_verdict_config


class FailureReason(str, enum.Enum):
    """Closed set of reason codes (str values for clean JSON serialisation)."""
    # input / preflight
    input_encrypted_user_password = "input_encrypted_user_password"
    input_not_a_pdf = "input_not_a_pdf"
    input_unreadable = "input_unreadable"
    # conversion / recovery
    docling_conversion_failed = "docling_conversion_failed"
    hollow_pages_unrecovered = "hollow_pages_unrecovered"
    ocr_quality_poor = "ocr_quality_poor"
    scanned_source_low_native = "scanned_source_low_native"
    # audit / remediation
    pre_audit_failed = "pre_audit_failed"
    remediation_failed = "remediation_failed"
    remediation_partial = "remediation_partial"
    post_audit_failed = "post_audit_failed"
    wcag_score_below_threshold = "wcag_score_below_threshold"
    wcag_unresolved_issues = "wcag_unresolved_issues"
    # vlm / enrichment
    vlm_provider_error = "vlm_provider_error"
    figures_missing_alt = "figures_missing_alt"
    alt_text_unavailable = "alt_text_unavailable"
    # tagging / structure
    tagger_crashed = "tagger_crashed"
    tagger_not_run_no_structure = "tagger_not_run_no_structure"
    font_substituted_lossy = "font_substituted_lossy"
    tounicode_unmappable = "tounicode_unmappable"
    struct_tree_blank_elements = "struct_tree_blank_elements"
    text_coverage_low = "text_coverage_low"
    text_coverage_moderate = "text_coverage_moderate"
    element_match_rate_low = "element_match_rate_low"
    reading_order_low = "reading_order_low"
    mcr_all_invisible = "mcr_all_invisible"
    docling_yield_low = "docling_yield_low"
    pdf_no_structure_tree = "pdf_no_structure_tree"
    pdf_not_marked = "pdf_not_marked"
    pdf_no_document_lang = "pdf_no_document_lang"
    # gates (manifest only)
    gate_failed_screen_reader_sim = "gate_failed_screen_reader_sim"
    gate_failed_visual_diff = "gate_failed_visual_diff"
    gate_failed_text_extraction = "gate_failed_text_extraction"
    gate_failed_operator_balance = "gate_failed_operator_balance"
    gate_failed_mcid_completeness = "gate_failed_mcid_completeness"
    gate_failed_bare_tree = "gate_failed_bare_tree"
    # process / infra
    oom_or_memory_error = "oom_or_memory_error"
    timeout = "timeout"
    unknown_error = "unknown_error"
    # sentinel — clean pass, so ranking always has a baseline row
    none = "none"


R = FailureReason

# Text-classification values produced by converter._classify_pdf_text and
# compared across module boundaries (OCR forcing, yield tripwire, tests).
# One definition; the converter imports these.
CLASS_FULLY_SCANNED = "fully_scanned"
CLASS_TEXT_ONLY = "text_only"
CLASS_MIXED = "mixed"

# ── ad-hoc-identifier → taxonomy mappings ───────────────────────────────────

VALIDATOR_CHECK_TO_REASON: Dict[str, FailureReason] = {
    "no-struct-tree": R.pdf_no_structure_tree,
    "no-document-elem": R.pdf_no_structure_tree,
    "no-markinfo": R.pdf_not_marked,
    "markinfo-not-marked": R.pdf_not_marked,
    "blank-leaf-element": R.struct_tree_blank_elements,
    "blank-subtree": R.struct_tree_blank_elements,
    "blank-pages": R.struct_tree_blank_elements,
    "all-mcr-invisible": R.mcr_all_invisible,
    "page-all-mcr-invisible": R.mcr_all_invisible,
    "mcr-invisible-count": R.mcr_all_invisible,
    "low-text-coverage": R.text_coverage_low,
    "moderate-text-coverage": R.text_coverage_moderate,
    "file-missing": R.tagger_crashed,
    "open-failed": R.tagger_crashed,
    "source-open-failed": R.unknown_error,
    "bare_container_tree": R.gate_failed_bare_tree,
}

POST_AUDIT_TYPE_TO_REASON: Dict[str, FailureReason] = {
    "pdf-no-structure-tree": R.pdf_no_structure_tree,
    "pdf-not-marked": R.pdf_not_marked,
    "pdf-no-document-lang": R.pdf_no_document_lang,
    "pdf-hollow-pages": R.hollow_pages_unrecovered,
    "pdf-figures-missing-alt": R.figures_missing_alt,
    "pdf-poor-ocr-quality": R.ocr_quality_poor,
    "pdf-blank-structure-elements": R.struct_tree_blank_elements,
    "pdf-blank-subtrees": R.struct_tree_blank_elements,
    "pdf-low-text-coverage": R.text_coverage_low,
    "pdf-moderate-text-coverage": R.text_coverage_moderate,
}

GATE_TO_REASON: Dict[str, FailureReason] = {
    "screen_reader_sim": R.gate_failed_screen_reader_sim,
    "visual_diff": R.gate_failed_visual_diff,
    "text_extraction": R.gate_failed_text_extraction,
    "operator_balance": R.gate_failed_operator_balance,
    "mcid_completeness": R.gate_failed_mcid_completeness,
    "bare_tree": R.gate_failed_bare_tree,
    "structure_yield": R.docling_yield_low,
}

# pipeline phase name → reason when that phase is recorded "failed"
_PHASE_TO_REASON: Dict[str, FailureReason] = {
    "docling": R.docling_conversion_failed,
    "wcag_pre_audit": R.pre_audit_failed,
    "remediation": R.remediation_failed,
    "wcag_post_audit": R.post_audit_failed,
    "tagging": R.tagger_crashed,
    "vlm_alt_text": R.vlm_provider_error,
    "vlm_table_captions": R.vlm_provider_error,
    "vlm_formulas": R.vlm_provider_error,
}

# ── severity ────────────────────────────────────────────────────────────────

_SEVERITY: Dict[str, str] = {}


def _sev(level: str, codes) -> None:
    for c in codes:
        _SEVERITY[c.value] = level


_sev("critical", [R.docling_conversion_failed, R.pre_audit_failed, R.remediation_failed,
                  R.post_audit_failed, R.tagger_crashed, R.tagger_not_run_no_structure,
                  R.pdf_no_structure_tree, R.oom_or_memory_error, R.timeout, R.input_unreadable])
_sev("major", [R.hollow_pages_unrecovered, R.ocr_quality_poor, R.scanned_source_low_native,
               R.figures_missing_alt, R.alt_text_unavailable, R.vlm_provider_error,
               R.text_coverage_low, R.element_match_rate_low,
               R.font_substituted_lossy,
               R.tounicode_unmappable, R.mcr_all_invisible, R.pdf_not_marked,
               R.pdf_no_document_lang, R.wcag_score_below_threshold, R.remediation_partial,
               R.input_encrypted_user_password, R.input_not_a_pdf, R.unknown_error,
               R.gate_failed_screen_reader_sim, R.gate_failed_visual_diff,
               R.gate_failed_text_extraction, R.gate_failed_operator_balance,
               R.gate_failed_mcid_completeness, R.gate_failed_bare_tree,
               R.docling_yield_low])
_sev("minor", [R.text_coverage_moderate, R.struct_tree_blank_elements, R.wcag_unresolved_issues,
               R.reading_order_low])
_sev("info", [R.none])


# ── public API ──────────────────────────────────────────────────────────────

def summarize_post_audit_issue_types(post_audit: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Count post-audit issues by ``type``, keeping only unresolved ones.

    Storing this compact histogram (instead of the full issues list, which
    carries text snippets) keeps records small and free of document content.
    """
    counts: Counter = Counter()
    for issue in (post_audit or {}).get("issues", []) or []:
        if issue.get("remediation_status") == "needs_remediation":
            counts[issue.get("type") or "unknown"] += 1
    return dict(counts)


def summarize_post_audit_advisories(post_audit: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Count informational (non-gating) post-audit issues by ``type``.

    Kept separate from ``summarize_post_audit_issue_types`` so that field's
    unresolved-failure semantics stay comparable across manifest runs.
    """
    counts: Counter = Counter()
    for issue in (post_audit or {}).get("issues", []) or []:
        if issue.get("remediation_status") == "informational":
            counts[issue.get("type") or "unknown"] += 1
    return dict(counts)


def check_docling_yield_low(
    text_classification: Any, elements: Any, pages: Any,
    pages_with_text: Any = None,
) -> bool:
    """Structure-yield tripwire: Docling produced implausibly little.

    Fires below 1 element/page (pages >= 3) for fully_scanned documents,
    and for mixed documents that are >= 80% textless — so a vector-scan
    book with one texty cover page cannot disarm the tripwire. Healthy
    scanned docs yield 4-50 elements/page; the vectorized-scan textbook
    that motivated this yielded 0.14/page while every other signal stayed
    green. Both arms validated against 645 historical conversions: exactly
    one hit (that textbook), zero false positives.

    Single source of the rule — used by classify_failure_reasons, the
    pipeline WARNING, and validation.gates.gate_structure_yield.
    Inputs must be the PRISTINE export-time counts (conversion stats):
    Phase 1b/1c recovery mutates docling_structure.json in place, so
    re-counting that file later masks the failure.
    """
    if not isinstance(elements, int) or not isinstance(pages, int):
        return False
    if pages < 3 or elements / pages >= 1.0:
        return False
    if text_classification == CLASS_FULLY_SCANNED:
        return True
    if (text_classification == CLASS_MIXED
            and isinstance(pages_with_text, int)
            and (pages - pages_with_text) / pages >= 0.8):
        return True
    return False


def classify_failure_reasons(
    record: Dict[str, Any], *, config: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Map an outcome record to a list of ``{code, severity, detail, signal}``.

    Pure — performs no I/O. ``detail``/``signal`` carry only counts, page
    numbers, and codes (never document text). A clean pass returns the single
    ``none`` sentinel so reason-ranking always has a baseline row.
    """
    reasons: List[Dict[str, Any]] = []
    seen = set()

    def add(code, *, detail: str = "", signal: str = "") -> None:
        c = code.value if isinstance(code, FailureReason) else str(code)
        if c in seen:
            return
        seen.add(c)
        reasons.append({"code": c, "severity": _SEVERITY.get(c, "major"),
                        "detail": detail, "signal": signal})

    status = record.get("status")
    verdict = record.get("verdict")
    error = record.get("error") or ""
    error_detail = record.get("error_detail") or {}
    phase_status = record.get("phase_status") or {}

    # Only phases that carry a reason or map to one count as failures. Best-effort
    # phases (text/OCR recovery) are recorded in phase_status for visibility but do
    # not flag the PDF — a clean pass with a recovery hiccup stays clean.
    failed_phases = [
        name for name, st in phase_status.items()
        if isinstance(st, dict) and st.get("status") == "failed"
        and (st.get("reason") or name in _PHASE_TO_REASON)
    ]
    failed_gates = set(record.get("failed_gates") or [])
    gates = record.get("gates") or {}
    if isinstance(gates, dict):
        for g in (gates.get("gates") or []):
            if isinstance(g, dict) and g.get("gate") and not g.get("passed", True):
                failed_gates.add(g.get("gate"))

    # Structure-yield tripwire participates in cleanliness: its whole point
    # is to flag documents every other signal waves through.
    yield_low = check_docling_yield_low(
        record.get("text_classification"),
        record.get("docling_elements"),
        record.get("docling_pages"),
        record.get("docling_pages_with_text"),
    )

    is_clean = (
        verdict == "pass"
        and status in (None, "passed", "pass")
        and not error and not error_detail
        and not failed_phases and not failed_gates
        and not yield_low
    )
    if is_clean:
        return [{"code": R.none.value, "severity": "info", "detail": "", "signal": "verdict"}]

    if yield_low:
        add(R.docling_yield_low,
            detail=(f"{record.get('docling_elements')} elements / "
                    f"{record.get('docling_pages')} pages"),
            signal="stats.structure_elements")

    # ── infra / exception type ──
    etype = error_detail.get("type") or ""
    blob = (etype + " " + str(error)).lower()
    if etype == "MemoryError" or "outofmemory" in blob or "memoryerror" in blob:
        add(R.oom_or_memory_error, detail=etype or "memory", signal="error_detail.type")
    if "timeout" in blob:
        add(R.timeout, detail=etype or "timeout", signal="error_detail.type")

    if status == "skipped" and ("password" in error.lower() or "encrypt" in error.lower()):
        add(R.input_encrypted_user_password, detail=error[:120], signal="status.skipped")
    if status == "skipped" and ("not a valid pdf" in error.lower()
                                or "not a pdf" in error.lower()
                                or "bad magic" in error.lower()):
        add(R.input_not_a_pdf, detail=error[:120], signal="status.skipped")

    # ── phase failures (prefer a reason the phase_guard already stamped) ──
    for name in failed_phases:
        st = phase_status[name]
        code = st.get("reason") or _PHASE_TO_REASON.get(name, R.unknown_error)
        add(code, detail=(st.get("error") or "")[:200], signal=f"phase_status.{name}")

    if status == "tagger_failed" or record.get("pdf_error"):
        add(R.tagger_crashed, detail=str(record.get("pdf_error") or "")[:200],
            signal="status.tagger_failed")
    if record.get("error_phase") == "docling":
        add(R.docling_conversion_failed, signal="error_phase.docling")

    # ── gates ──
    for gname in sorted(g for g in failed_gates if g):
        code = GATE_TO_REASON.get(gname)
        if code:
            add(code, signal=f"gate.{gname}")

    # ── struct-tree validator checks (info checks aren't in the table → ignored) ──
    for chk, count in (record.get("struct_tree_issue_checks") or {}).items():
        code = VALIDATOR_CHECK_TO_REASON.get(chk)
        if code:
            add(code, detail=str(count), signal=f"validator.{chk}")

    # ── post-audit issue types ──
    for t, count in (record.get("post_audit_issue_types") or {}).items():
        code = POST_AUDIT_TYPE_TO_REASON.get(t)
        if code:
            add(code, detail=f"{count} issue(s)", signal=f"post_audit.{t}")
        elif str(t).startswith("pdf-"):
            add(R.unknown_error, detail=f"unmapped issue type: {t}", signal=f"post_audit.{t}")

    # ── screen-reader-derived deficiencies ──
    # Production records carry a full ``sr_quality`` dict; manifest records carry
    # ``avg_coverage``/``hollow_pages`` at the top level — fall back to those.
    vc = resolve_verdict_config(config)
    sr = record.get("sr_quality") or {}
    hp = sr.get("hollow_pages") or record.get("hollow_pages") or []
    if hp:
        add(R.hollow_pages_unrecovered, detail=f"pages {list(hp)}", signal="sr_quality.hollow_pages")
    cov = sr.get("avg_coverage")
    if cov is None:
        cov = record.get("avg_coverage")
    if cov is not None and cov < vc["min_coverage"]:
        add(R.text_coverage_low, detail=f"coverage {cov:.2f} < {vc['min_coverage']}",
            signal="sr_quality.avg_coverage")
    # EMR gate — mirrors compute_wcag_verdict's ``emr_ok``: only applies when the
    # document has native-text pages (the gate is skipped for all-scanned docs,
    # where there is nothing to match against). has_native derived as in
    # build_verdict_inputs (audit/standards.py); defaults to native-present when
    # page totals are absent, matching has_native_pages=True backward-compat.
    total_pages = sr.get("total_pages") or 0
    scanned_pages = sr.get("scanned_pages") or []
    has_native = (total_pages - len(scanned_pages)) > 0 if total_pages else True
    emr = sr.get("element_match_rate")
    if emr is None:
        emr = record.get("element_match_rate")
    # The EMR reason fires only when the gate actually fails, using the SAME
    # predicate as the verdict (emr_passes) so the two can never disagree. cov
    # is coerced to 0.0 when absent, matching build_verdict_inputs / the
    # verdict's avg_coverage default.
    run_match = sr.get("run_match_rate")
    if run_match is None:
        run_match = record.get("run_match_rate")
    if (has_native and emr is not None
            and not emr_passes(
                emr,
                avg_coverage=cov if cov is not None else 0.0,
                has_native_pages=has_native,
                min_element_match_rate=vc["min_element_match_rate"],
                min_element_match_rate_when_covered=vc[
                    "min_element_match_rate_when_covered"],
                min_coverage=vc["min_coverage"],
                run_match_rate=run_match,
                min_run_match_rate_when_covered=vc[
                    "min_run_match_rate_when_covered"],
                has_scanned_pages=bool(scanned_pages))):
        add(R.element_match_rate_low,
            detail=f"{emr:.1f}% < {vc['min_element_match_rate']}",
            signal="sr_quality.element_match_rate")
    # Reading-order gate — mirrors compute_wcag_verdict's ``ro_ok``: fires only
    # when the worst eligible page scores below the configured tau (None means no
    # eligible page was scored → gate skipped, so no reason).  Same resolved
    # threshold the verdict uses, so the two never disagree.
    ro_tau = sr.get("reading_order_min_tau")
    if ro_tau is None:
        ro_tau = record.get("reading_order_min_tau")
    if ro_tau is not None and ro_tau < vc["min_reading_order_tau"]:
        fp = sr.get("reading_order_failing_pages") or record.get("reading_order_failing_pages") or []
        add(R.reading_order_low,
            detail=f"tau {ro_tau:.2f} < {vc['min_reading_order_tau']} (pages {list(fp)})",
            signal="sr_quality.reading_order_min_tau")
    if (sr.get("figures_missing_alt") or 0) > 0:
        add(R.figures_missing_alt,
            detail=f"{sr['figures_missing_alt']}/{sr.get('figures_total', '?')}",
            signal="sr_quality.figures_missing_alt")
    if (sr.get("scanned_pages") and sr.get("ocr_pages_passing_pct") is not None
            and sr["ocr_pages_passing_pct"] < vc["min_ocr_pages_passing_pct"]):
        add(R.ocr_quality_poor,
            detail=f"{sr['ocr_pages_passing_pct']:.2f} < {vc['min_ocr_pages_passing_pct']}",
            signal="sr_quality.ocr_pages_passing_pct")

    # ── wcag score ──
    score = record.get("wcag_post_score")
    if score is not None and score < vc["min_wcag_score"]:
        add(R.wcag_score_below_threshold, detail=f"{score} < {vc['min_wcag_score']}",
            signal="wcag_post_score")

    # ── font / tounicode ──
    fsum = (record.get("font_substitution") or {}).get("summary") or {}
    if (fsum.get("with_residual_missing") or 0) > 0:
        add(R.font_substituted_lossy, detail=f"{fsum['with_residual_missing']} font(s)",
            signal="font_substitution")
    if ((record.get("source_quality_warnings") or {}).get("tounicode_cmaps_failed") or 0) > 0:
        n = record["source_quality_warnings"]["tounicode_cmaps_failed"]
        add(R.tounicode_unmappable, detail=f"{n} font(s)", signal="source_quality_warnings")

    if not reasons:
        add(R.unknown_error, detail=str(error)[:200] or str(status or ""), signal="fallback")

    return reasons
