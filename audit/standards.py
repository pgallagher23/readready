# SPDX-License-Identifier: AGPL-3.0-or-later
"""WCAG standards and criteria information."""

from typing import Any, Dict, List, Optional


SEVERITY_LEVELS = {
    "minor": 1,
    "major": 2,
    "critical": 3,
    "compliant": 0,
}

WCAG_CRITERIA = {
    # Perceivable — Level A
    "1.1.1": {
        "name": "Non-text Content",
        "level": "A",
        "description": "All non-text content has a text alternative that serves the equivalent purpose.",
    },
    "1.3.1": {
        "name": "Info and Relationships",
        "level": "A",
        "description": "Information, structure, and relationships conveyed through presentation can be programmatically determined.",
    },
    "1.3.2": {
        "name": "Meaningful Sequence",
        "level": "A",
        "description": "A correct reading sequence can be programmatically determined.",
    },
    "1.4.1": {
        "name": "Use of Color",
        "level": "A",
        "description": "Color is not used as the only visual means of conveying information.",
    },
    # Perceivable — Level AA
    "1.3.5": {
        "name": "Identify Input Purpose",
        "level": "AA",
        "description": "The purpose of form input fields can be programmatically determined.",
    },
    "1.4.3": {
        "name": "Contrast (Minimum)",
        "level": "AA",
        "description": "Text has a contrast ratio of at least 4.5:1.",
    },
    "1.4.4": {
        "name": "Resize Text",
        "level": "AA",
        "description": "Text can be resized up to 200% without loss of content or functionality.",
    },
    "1.4.5": {
        "name": "Images of Text",
        "level": "AA",
        "description": "If technologies can achieve the visual presentation, text is used rather than images of text.",
    },
    "1.4.10": {
        "name": "Reflow",
        "level": "AA",
        "description": "Content can be presented without loss of information or functionality and without scrolling in two dimensions.",
    },
    "1.4.11": {
        "name": "Non-text Contrast",
        "level": "AA",
        "description": "Visual presentation of UI components and graphical objects have at least 3:1 contrast ratio.",
    },
    "1.4.12": {
        "name": "Text Spacing",
        "level": "AA",
        "description": "No loss of content or functionality when text spacing is adjusted.",
    },
    # Operable — Level A
    "2.1.1": {
        "name": "Keyboard",
        "level": "A",
        "description": "All functionality of the content is operable through a keyboard interface.",
    },
    "2.4.1": {
        "name": "Bypass Blocks",
        "level": "A",
        "description": "A mechanism is available to bypass blocks of repeated content.",
    },
    "2.4.2": {
        "name": "Page Titled",
        "level": "A",
        "description": "Web pages have titles that describe topic or purpose.",
    },
    "2.4.3": {
        "name": "Focus Order",
        "level": "A",
        "description": "Focusable components receive focus in an order that preserves meaning and operability.",
    },
    "2.4.4": {
        "name": "Link Purpose (In Context)",
        "level": "A",
        "description": "The purpose of each link can be determined from the link text alone or together with its context.",
    },
    # Operable — Level AA
    "2.4.6": {
        "name": "Headings and Labels",
        "level": "AA",
        "description": "Headings and labels describe topic or purpose.",
    },
    "2.4.7": {
        "name": "Focus Visible",
        "level": "AA",
        "description": "Any keyboard operable user interface has a mode of operation where the keyboard focus indicator is visible.",
    },
    # Operable — Level AAA (informational, excluded from AA scoring)
    "2.4.9": {
        "name": "Link Purpose (Link Only)",
        "level": "AAA",
        "description": "The purpose of each link can be identified from the link text alone.",
    },
    # Understandable — Level A
    "3.1.1": {
        "name": "Language of Page",
        "level": "A",
        "description": "The default human language of each page can be programmatically determined.",
    },
    "3.3.1": {
        "name": "Error Identification",
        "level": "A",
        "description": "If an input error is automatically detected, the item in error is identified and the error is described to the user in text.",
    },
    "3.3.2": {
        "name": "Labels or Instructions",
        "level": "A",
        "description": "Labels or instructions are provided when content requires user input.",
    },
    # Understandable — Level AA
    "3.1.2": {
        "name": "Language of Parts",
        "level": "AA",
        "description": "The human language of each passage or phrase can be programmatically determined.",
    },
    # Understandable — Level AAA (informational, excluded from AA scoring)
    "3.2.5": {
        "name": "Change on Request",
        "level": "AAA",
        "description": "Changes of context are initiated only by user request.",
    },
    # Robust — Level A
    "4.1.1": {
        "name": "Parsing",
        "level": "A",
        "description": "Elements have complete start and end tags, no duplicate attributes, and IDs are unique.",
    },
    "4.1.2": {
        "name": "Name, Role, Value",
        "level": "A",
        "description": "For all UI components, name and role can be programmatically determined.",
    },
}


WCAG_PRINCIPLES = {
    "Perceivable": [
        "1.1.1", "1.3.1", "1.3.2", "1.3.5",
        "1.4.1", "1.4.3", "1.4.4", "1.4.5", "1.4.10", "1.4.11", "1.4.12",
    ],
    "Operable": [
        "2.1.1", "2.4.1", "2.4.2", "2.4.3", "2.4.4", "2.4.6", "2.4.7",
        "2.4.9",
    ],
    "Understandable": ["3.1.1", "3.1.2", "3.2.5", "3.3.1", "3.3.2"],
    "Robust": ["4.1.1", "4.1.2"],
}

# AAA criteria included for informational purposes but excluded from
# AA conformance scoring.
AAA_CRITERIA = {"2.4.9", "3.2.5"}


def get_criterion_info(criterion_id: str) -> dict:
    """Get information about a WCAG criterion."""
    return WCAG_CRITERIA.get(
        criterion_id,
        {
            "name": "Unknown Criterion",
            "level": "Unknown",
            "description": "No description available",
        },
    )


def get_principle_for_criterion(criterion_id: str) -> str:
    """Return the WCAG principle name for a given criterion ID."""
    for principle, criteria in WCAG_PRINCIPLES.items():
        if criterion_id in criteria:
            return principle
    return "Unknown"


def compute_compliance_score(issues: list) -> dict:
    """Compute a compliance score from a list of audit issues.

    AAA-level criteria (2.4.9, 3.2.5) are tracked in by_criterion but
    excluded from the overall AA conformance score and principle scores.

    All A- and AA-level criteria from WCAG_CRITERIA are always present in
    by_criterion (defaulting to passed with zero issues).  This ensures the
    report denominator reflects all 24 criteria, not just those that happened
    to emit issues.

    Returns dict with:
        - overall_score: float 0-100 (percentage of tested A/AA criteria passing)
        - by_criterion: dict mapping criterion_id -> {passed, total, failing}
        - by_principle: dict mapping principle -> {score, total, passing}
        - by_severity: dict mapping severity -> count of failing issues
    """
    # Pre-populate every defined criterion so the denominator is always
    # complete.  Criteria with no issues default to passed.
    by_criterion = {
        cid: {"total": 0, "failing": 0}
        for cid, info in WCAG_CRITERIA.items()
    }

    for issue in issues:
        crit = issue.get("wcag_criterion", "unknown")
        if crit not in by_criterion:
            by_criterion[crit] = {"total": 0, "failing": 0}
        by_criterion[crit]["total"] += 1
        if issue.get("remediation_status") == "needs_remediation":
            by_criterion[crit]["failing"] += 1

    for crit, data in by_criterion.items():
        data["passed"] = data["failing"] == 0

    by_principle = {}
    for principle, criteria_ids in WCAG_PRINCIPLES.items():
        # Exclude AAA criteria from principle scores
        relevant = {
            c: by_criterion[c]
            for c in criteria_ids
            if c in by_criterion and c not in AAA_CRITERIA
        }
        total = len(relevant)
        passing = sum(1 for d in relevant.values() if d["passed"])
        by_principle[principle] = {
            "score": (passing / total * 100) if total > 0 else 100.0,
            "total": total,
            "passing": passing,
        }

    # Exclude AAA criteria from overall score
    aa_criteria = {
        c: d for c, d in by_criterion.items() if c not in AAA_CRITERIA
    }
    total_criteria = len(aa_criteria)
    passing_criteria = sum(1 for d in aa_criteria.values() if d["passed"])
    overall_score = (passing_criteria / total_criteria * 100) if total_criteria > 0 else 100.0

    by_severity = {"critical": 0, "major": 0, "minor": 0, "info": 0}
    for issue in issues:
        if issue.get("remediation_status") == "needs_remediation":
            sev = issue.get("severity", "info")
            if sev in by_severity:
                by_severity[sev] += 1

    return {
        "overall_score": round(overall_score, 1),
        "by_criterion": by_criterion,
        "by_principle": by_principle,
        "by_severity": by_severity,
    }


# ── Verdict thresholds: single source of truth ──────────────────────────────
# These mirror config.yaml's ``verdict:`` section. config.yaml wins in
# production; these are the canonical fallbacks when no config is threaded
# through. compute_wcag_verdict's signature defaults reference this dict so the
# bare default can never silently drift from config again.
VERDICT_DEFAULTS = {
    "min_wcag_score": 100.0,
    "min_coverage": 0.80,
    "min_element_match_rate": 85,
    # When text coverage corroborates that content is present
    # (avg_coverage >= min_coverage), a low EMR reflects benign attribution
    # noise (over-segmentation, text-less figures) rather than missing
    # content, so the EMR requirement relaxes to this floor.  Below the floor
    # the doc still needs review even with high coverage — that band signals
    # genuine structural collapse, not noise.
    "min_element_match_rate_when_covered": 70,
    # Relaxed run-match corroboration floor — FULLY-NATIVE docs only.  When text
    # coverage corroborates content is present AND this share of content-stream
    # runs were attributed to the tag tree, a low element_match_rate is benign
    # over-segmentation (phantom/duplicate/cell-split Docling elements with no
    # distinct run), not missing content.  Excluded for docs with any scanned
    # page, where EMR/coverage are unreliable and the OCR gate governs instead.
    "min_run_match_rate_when_covered": 80,
    "reject_hollow_pages": True,
    "min_ocr_pages_passing_pct": 0.80,
    # Minimum per-page reading-order correlation (Kendall's tau) between the
    # tag-tree DFS order and the page's own column-aware visual order.  The worst
    # eligible page must score >= this.  None (no eligible page scored) skips the
    # gate.  Detection only — a low score flags the doc for the manual pass.
    "min_reading_order_tau": 0.6,
    # Reading order is reported as a best-practice finding, not a verdict
    # failure.  A low tau means the tag-tree order disagrees with an
    # extractor's visual order — it is NOT a WCAG failure: 1.3.2 is audited
    # separately by ReadingOrderCheck (tabindex / CSS reorder / absolute
    # positioning), and the labelled 36-document study found 0/36 flagged documents that
    # actually read wrong to a screen reader, at every tau band.  Detection,
    # telemetry, and reporting are unchanged; only the banner stops failing.
    # Set false to restore the blocking gate.
    "reading_order_advisory": True,
}


def resolve_verdict_config(config_or_dict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge a config over ``VERDICT_DEFAULTS`` to get a complete verdict config.

    Accepts a full ``config.yaml`` dict (reads its ``verdict`` sub-dict), a bare
    ``verdict`` sub-dict, or ``None``. Precedence: an explicit non-``None`` value
    for a known key wins over the default. A present-but-null ``verdict:`` section
    (``{"verdict": None}``) is treated as empty rather than crashing.
    """
    cfg = config_or_dict if isinstance(config_or_dict, dict) else {}
    # Full config dict → use its ``verdict`` section; bare verdict dict → itself.
    vc = cfg.get("verdict", cfg)
    if not isinstance(vc, dict):
        vc = {}
    out = dict(VERDICT_DEFAULTS)
    for key in VERDICT_DEFAULTS:
        if vc.get(key) is not None:
            out[key] = vc[key]
    return out


def emr_passes(
    element_match_rate: Optional[float],
    *,
    avg_coverage: float,
    has_native_pages: bool,
    min_element_match_rate: float,
    min_element_match_rate_when_covered: float,
    min_coverage: float,
    run_match_rate: Optional[float] = None,
    min_run_match_rate_when_covered: float = VERDICT_DEFAULTS[
        "min_run_match_rate_when_covered"
    ],
    has_scanned_pages: bool = False,
) -> bool:
    """Whether the element-match-rate gate passes.

    Single source of truth for the EMR gate, shared by ``compute_wcag_verdict``
    (the verdict) and ``classify_failure_reasons`` (the failure taxonomy) so the
    reason can never disagree with the verdict it explains.

    * Skipped (passes) when the source has no native-text pages.
    * ``None`` EMR (content-preserving mode did not run) → fails (needs review).
    * Passes at/above the hard threshold, OR — when text coverage corroborates
      that content is present (``avg_coverage >= min_coverage``) — at/above the
      relaxed coverage floor.
    * Run-match corroboration (FULLY-NATIVE docs only): also passes when coverage
      corroborates AND a high share of content-stream runs were attributed
      (``run_match_rate >= min_run_match_rate_when_covered``).  On a native doc,
      high coverage + high run-match prove the stream is present and attributed,
      so a low EMR is benign over-segmentation bookkeeping (Docling emitting more
      elements than there are distinct runs), not missing content.  Excluded when
      ``has_scanned_pages`` — there EMR/coverage are unreliable and the OCR gate
      governs.
    * Otherwise (below both floors) it fails even at high coverage (genuine
      structural collapse, not benign attribution noise).

    Callers must pass ``avg_coverage`` already coerced to a float (None → 0.0),
    matching ``build_verdict_inputs``.
    """
    if not has_native_pages:
        return True
    if element_match_rate is None:
        return False
    if element_match_rate >= min_element_match_rate:
        return True
    if (avg_coverage >= min_coverage
            and element_match_rate >= min_element_match_rate_when_covered):
        return True
    return (not has_scanned_pages
            and avg_coverage >= min_coverage
            and run_match_rate is not None
            and run_match_rate >= min_run_match_rate_when_covered)


def compute_wcag_verdict(
    post_audit: Dict[str, Any],
    *,
    avg_coverage: float = 0.0,
    element_match_rate: Optional[float] = None,
    hollow_pages: Optional[List[int]] = None,
    sr_quality: str = "unknown",
    min_wcag_score: float = VERDICT_DEFAULTS["min_wcag_score"],
    min_coverage: float = VERDICT_DEFAULTS["min_coverage"],
    min_element_match_rate: float = VERDICT_DEFAULTS["min_element_match_rate"],
    min_element_match_rate_when_covered: float = VERDICT_DEFAULTS[
        "min_element_match_rate_when_covered"
    ],
    run_match_rate: Optional[float] = None,
    min_run_match_rate_when_covered: float = VERDICT_DEFAULTS[
        "min_run_match_rate_when_covered"
    ],
    reject_hollow_pages: bool = VERDICT_DEFAULTS["reject_hollow_pages"],
    has_scanned_pages: bool = False,
    has_native_pages: bool = True,
    ocr_pages_passing_pct: Optional[float] = None,
    min_ocr_pages_passing_pct: float = VERDICT_DEFAULTS["min_ocr_pages_passing_pct"],
    reading_order_min_tau: Optional[float] = None,
    min_reading_order_tau: float = VERDICT_DEFAULTS["min_reading_order_tau"],
    reading_order_advisory: bool = VERDICT_DEFAULTS["reading_order_advisory"],
) -> str:
    """Compute a two-state WCAG + screen reader verdict.

    Combines WCAG compliance score with screen reader quality metrics
    into a single pass/needs_review verdict.  Thresholds are
    configurable via ``config.yaml`` ``verdict`` section.

    Two SR gates apply independently — both must pass when applicable:

    * **EMR gate** (native-text pages): requires ``element_match_rate
      >= min_element_match_rate``. Skipped when ``has_native_pages``
      is False (fully-scanned source PDF).
    * **OCR gate** (scanned pages): requires ``ocr_pages_passing_pct
      >= min_ocr_pages_passing_pct``. Skipped when ``has_scanned_pages``
      is False (no OCR'd content to grade).

    Args:
        post_audit: Post-remediation audit dict with ``compliance`` key.
        avg_coverage: Text coverage ratio (0.0-1.0) from SR check.
        element_match_rate: Element match rate (0-100) from content
            matcher, or ``None`` if content-preserving mode did not run.
        hollow_pages: List of page numbers with zero tagged text.
        sr_quality: SR quality categorical (``"good"``/``"fair"``/
            ``"poor"``/``"unknown"``).
        min_wcag_score: Minimum overall_score for pass (default 100.0).
        min_coverage: Minimum avg_coverage for pass (default 0.80,
            from ``VERDICT_DEFAULTS``).
        min_element_match_rate: Minimum EMR for pass (default 85).
        reject_hollow_pages: If True, any hollow page forces
            needs_review (default True).
        has_scanned_pages: True when any page has no native text
            (source was image-only on that page). When True, the OCR
            gate applies.
        has_native_pages: True when at least one page has native text.
            When False, the EMR gate is skipped because there is
            nothing to match against. Default True for backward
            compatibility with existing callers.
        ocr_pages_passing_pct: Share of scanned pages whose OCR
            quality is ``"good"`` or ``"fair"`` (0.0-1.0). ``None``
            when no scanned pages were graded.
        min_ocr_pages_passing_pct: OCR gate threshold (default 0.80).

    Returns:
        ``"pass"``         — document is Valid.
        ``"needs_review"`` — document needs manual review.
    """
    compliance = post_audit.get("compliance", {})
    overall_score = compliance.get("overall_score") or 0.0

    emr_ok = emr_passes(
        element_match_rate,
        avg_coverage=avg_coverage,
        has_native_pages=has_native_pages,
        min_element_match_rate=min_element_match_rate,
        min_element_match_rate_when_covered=min_element_match_rate_when_covered,
        min_coverage=min_coverage,
        run_match_rate=run_match_rate,
        min_run_match_rate_when_covered=min_run_match_rate_when_covered,
        has_scanned_pages=has_scanned_pages,
    )
    ocr_ok = (
        not has_scanned_pages
        or (ocr_pages_passing_pct is not None
            and ocr_pages_passing_pct >= min_ocr_pages_passing_pct)
    )
    # Reading-order gate: advisory by default (see VERDICT_DEFAULTS).  A low
    # tau is reported as a best-practice finding but does not fail the doc.
    # None = no eligible page scored (CID/sparse text) → skip, mirroring EMR.
    ro_ok = (
        reading_order_advisory
        or reading_order_min_tau is None
        or reading_order_min_tau >= min_reading_order_tau
    )

    is_valid = (
        overall_score >= min_wcag_score
        and avg_coverage >= min_coverage
        and emr_ok
        and ocr_ok
        and ro_ok
        and (not reject_hollow_pages or not hollow_pages)
        and sr_quality != "unknown"
    )
    return "pass" if is_valid else "needs_review"


def build_verdict_inputs(
    post_audit: Dict[str, Any],
    sr_data: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    sr_quality_fallback: str = "unknown",
) -> Dict[str, Any]:
    """Assemble ``compute_wcag_verdict`` kwargs from an ``sr_data`` dict + config.

    ``sr_data`` is the dict returned by
    ``validation.gates.quick_screen_reader_check`` (or the ``sr_quality``
    entry of a pipeline result). ``config`` may be a full ``config.yaml`` dict,
    a bare ``verdict`` sub-dict, or ``None``.

    ``sr_quality_fallback`` is the SR-quality categorical to use when ``sr_data``
    has no ``sr_quality`` key — used by the report builders, which historically
    fell back to a function parameter.

    Single place that derives ``has_scanned_pages`` / ``has_native_pages`` from
    the page composition and applies the resolved verdict thresholds, so every
    surface (UI, reports, bulk, manifest) computes the verdict identically.
    """
    sr = sr_data or {}
    vc = resolve_verdict_config(config)
    scanned = sr.get("scanned_pages") or []
    total = sr.get("total_pages") or 0
    avg = sr.get("avg_coverage")
    return dict(
        post_audit=post_audit,
        avg_coverage=avg if avg is not None else 0.0,
        element_match_rate=sr.get("element_match_rate"),
        run_match_rate=sr.get("run_match_rate"),
        hollow_pages=sr.get("hollow_pages", []),
        sr_quality=sr.get("sr_quality", sr_quality_fallback),
        has_scanned_pages=bool(scanned),
        has_native_pages=(len(scanned) < (total or 1)),
        ocr_pages_passing_pct=sr.get("ocr_pages_passing_pct"),
        reading_order_min_tau=sr.get("reading_order_min_tau"),
        **vc,
    )


def verdict_from_sr(
    post_audit: Dict[str, Any],
    sr_data: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    sr_quality_fallback: str = "unknown",
) -> str:
    """Convenience wrapper: assemble inputs and return the verdict string.

    See :func:`build_verdict_inputs` for argument semantics.
    """
    return compute_wcag_verdict(
        **build_verdict_inputs(post_audit, sr_data, config, sr_quality_fallback)
    )


def summarize_pipeline_result(
    result: Dict[str, Any], config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Summarize a run_pipeline result dict into a per-document verdict row.

    Every surface scores a document through this one code path.

    Returns ``{status, wcag_score, issue_count, issues}`` where ``status`` is
    the verdict string ("pass"/"needs_review") or "error" for a failed run,
    and ``issues`` is a " | "-joined list of open issue descriptions.
    """
    if "error" in result:
        return {
            "status": "error",
            "wcag_score": 0.0,
            "issue_count": 0,
            "issues": result["error"],
        }

    post_audit = result.get("post_audit", {})
    compliance = post_audit.get("compliance", {})
    overall_score = compliance.get("overall_score", 0.0) or 0.0

    sr_data = result.get("sr_quality", {}) or {}
    verdict = verdict_from_sr(post_audit, sr_data, config)

    all_issues = post_audit.get("issues", [])
    needs_rem = [
        i for i in all_issues
        if i.get("remediation_status") == "needs_remediation"
    ]
    issue_descs = [i.get("description", "Unknown issue") for i in needs_rem]

    return {
        "status": verdict,
        "wcag_score": round(overall_score, 1),
        "issue_count": len(needs_rem),
        "issues": " | ".join(issue_descs) if issue_descs else "",
    }
