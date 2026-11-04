# SPDX-License-Identifier: AGPL-3.0-or-later
"""Layer 2: aggregate issues by type and synthesize SR/verdict-gate issues."""
from audit.standards import resolve_verdict_config
from output_gen.changes_report import _DOCUMENT_WIDE_TYPES
from reporting.constants import _ISSUE_DISPLAY_NAMES, _SR_DEFAULT_SEVERITY, _CE_LINK
from reporting.issue_location import _get_issue_location


def _aggregate_issues_by_type(
    wcag_issues: list[dict],
    sr_issues: list[dict],
    hollow_pages: list[int],
    sr_data: dict = None,
    structure: dict = None,
) -> list[dict]:
    """Aggregate issues by type with page numbers and highest severity.

    Returns list of dicts sorted by earliest affected page number,
    with document-wide issues at the end.
    """
    _SEV_RANK = {"critical": 0, "major": 1, "minor": 2}
    by_type: dict[str, dict] = {}

    for issue in wcag_issues + sr_issues:
        itype = issue.get("type", "")
        if not itype:
            continue
        sev = issue.get("severity") or _SR_DEFAULT_SEVERITY.get(itype, "minor")
        page = (issue.get("location") or {}).get("page_number")

        # Fall back to structure JSON for page number (single-file HTML audits)
        if (not page or page <= 0) and structure and structure.get("elements"):
            struct_page, _ = _get_issue_location(issue, structure)
            if struct_page and struct_page > 0:
                page = struct_page

        if itype not in by_type:
            by_type[itype] = {
                "type": itype,
                "display_name": _ISSUE_DISPLAY_NAMES.get(itype, itype),
                "severity": sev,
                "pages": set(),
                "is_document_wide": itype in _DOCUMENT_WIDE_TYPES,
            }
        entry = by_type[itype]
        # Keep highest severity
        if _SEV_RANK.get(sev, 2) < _SEV_RANK.get(entry["severity"], 2):
            entry["severity"] = sev
        if page and page > 0:
            entry["pages"].add(page)

    # Enrich coverage issues with page numbers from sr_data
    if sr_data:
        low_cov_pages = {
            lc.get("page") for lc in sr_data.get("low_coverage_pages", [])
            if lc.get("page")
        }
        deficit_pages = {
            d.get("page") for d in sr_data.get("page_deficits", [])
            if d.get("page")
        }
        coverage_pages = low_cov_pages | deficit_pages
        for cov_type in ("pdf-low-text-coverage", "pdf-moderate-text-coverage"):
            if cov_type in by_type and coverage_pages:
                by_type[cov_type]["pages"].update(coverage_pages)
                by_type[cov_type]["is_document_wide"] = False

    # Add hollow pages as synthetic entry
    if hollow_pages:
        by_type["pdf-hollow-pages"] = {
            "type": "pdf-hollow-pages",
            "display_name": "Hollow Pages",
            "severity": "critical",
            "pages": set(hollow_pages),
            "is_document_wide": False,
        }

    # Sort: by severity (critical → major → minor), then earliest page
    def _sort_key(entry):
        sev_rank = _SEV_RANK.get(entry.get("severity", "minor"), 2)
        if entry["is_document_wide"] or not entry["pages"]:
            return (sev_rank, 1, 0, entry["display_name"])
        return (sev_rank, 0, min(entry["pages"]), entry["display_name"])

    result = sorted(by_type.values(), key=_sort_key)
    # Convert page sets to sorted lists
    for entry in result:
        entry["pages"] = sorted(entry["pages"])
    return result


def _verdict_gate_reasons(sr_data: dict, vc: dict) -> list[dict]:
    """Synthesize issue dicts for document-level verdict gates that fail but
    are not represented as per-page / per-element issues.

    ``compute_wcag_verdict`` can return ``needs_review`` for a document-level
    metric — most commonly average text coverage below ``min_coverage`` — even
    when the per-page SR check (which uses a looser ``coverage_threshold``)
    emits no issue. In that 50–80% dead band the document is flagged for review
    but nothing explains why: the status banner has no detail line and the
    Improvement Guide returns empty, so its tab disappears. This bridges that
    gap. Returned dicts are compatible with :func:`_aggregate_issues_by_type`
    and :func:`_build_remediation_guide_html`.
    """
    reasons: list[dict] = []
    sr = sr_data or {}

    # Text-coverage gate (document-level average).  Use the same resolved
    # threshold the verdict uses, so the reason appears exactly when the gate
    # fails (no drift between this banner detail and compute_wcag_verdict).
    avg_cov = sr.get("avg_coverage")
    rvc = resolve_verdict_config(vc)
    min_cov = rvc["min_coverage"]
    if avg_cov is not None and avg_cov < min_cov:
        reasons.append({
            "type": "pdf-low-text-coverage",
            "severity": "major",
            "remediation_status": "needs_remediation",
            "is_document_wide": True,
            "location": {},
            "coverage_pct": round(avg_cov * 100),
            "threshold_pct": round(min_cov * 100),
        })

    # Reading-order gate (document-level worst-page).  Detection only — the
    # reading order may not match the visual layout, which needs a human/screen-
    # reader check in the manual pass.  Same resolved threshold the verdict uses,
    # so this reason appears exactly when the gate fails (no drift).
    ro_tau = sr.get("reading_order_min_tau")
    min_tau = rvc["min_reading_order_tau"]
    if ro_tau is not None and ro_tau < min_tau:
        # Advisory by default: the finding is reported but does not fail the
        # document, matching the verdict gate (VERDICT_DEFAULTS
        # ``reading_order_advisory``). Emitting it as informational routes it
        # to the best-practice section instead of the failure list, and keeps
        # this reason in lockstep with compute_wcag_verdict — a needs_review
        # reason must never appear on a document the verdict passed.
        advisory = rvc["reading_order_advisory"]
        reasons.append({
            "type": "pdf-reading-order",
            "severity": "info" if advisory else "major",
            "remediation_status": (
                "informational" if advisory else "needs_remediation"
            ),
            "is_document_wide": True,
            "location": {},
            "failing_pages": sr.get("reading_order_failing_pages", []),
            "tau": round(ro_tau, 2),
        })
    return reasons


def _sr_is_poor(sr_data: dict) -> bool:
    """True when the tagged output is broadly unusable by a screen reader.

    Keyed on the internal categorical (no struct tree / element-match < 50% /
    hollow-dominant) so the UI can keep a single "may not be usable" headline
    for genuinely-broken output while everything else becomes a plain
    page-specific action list.
    """
    return (sr_data or {}).get("sr_quality") == "poor"


def _sr_action_items(sr_data: dict) -> list[dict]:
    """Turn the sr_quality dict into ordered, page-specific action items.

    Single source of truth for every screen-reader display surface (needs-review
    banner, overview metrics card, improvement guide). Replaces the old vague
    "Screen Reader Quality: Fair (84% element match)" grade with concrete,
    page-numbered guidance ("Review pages 4, 7 — low text coverage").

    Each item: ``{"key", "severity", "text", "pages": list[int], "html": str|None}``.
    ``pages`` empty ⇒ document-level item (no page attribution available).
    Ordered critical-first, then by source signal.
    """
    sr = sr_data or {}
    items: list[dict] = []

    hollow = list(sr.get("hollow_pages") or [])
    if hollow:
        items.append({
            "key": "hollow", "severity": "critical",
            "text": "No tagged text — screen readers skip these pages",
            "pages": hollow, "html": None,
        })

    low_cov = sr.get("low_coverage_pages") or []
    if low_cov:
        pcts = ", ".join(f'{int(round(d.get("coverage", 0) * 100))}%' for d in low_cov[:6])
        items.append({
            "key": "low_coverage", "severity": "major",
            "text": f"Low text coverage ({pcts})",
            "pages": [d["page"] for d in low_cov if d.get("page")], "html": None,
        })

    deficits = sr.get("page_deficits") or []
    if deficits:
        items.append({
            "key": "page_deficit", "severity": "major",
            "text": "Content not linked to screen-reader text",
            "pages": [d["page"] for d in deficits if d.get("page")], "html": None,
        })

    ocr_bad = [p for p in (sr.get("per_page_ocr_quality") or []) if p.get("label") == "poor"]
    if ocr_bad:
        items.append({
            "key": "ocr", "severity": "major",
            "text": "Garbled OCR text",
            "pages": [p["page"] for p in ocr_bad if p.get("page")], "html": None,
        })

    ro_fail = list(sr.get("reading_order_failing_pages") or [])
    if ro_fail:
        items.append({
            "key": "reading_order", "severity": "major",
            "text": "Reading order may not match the visual layout",
            "pages": ro_fail,
            "html": (
                "Reading order may not match the visual layout — "
                f"review and change it in the {_CE_LINK}"
            ),
        })

    fig_missing = sr.get("figures_missing_alt") or 0
    if fig_missing:
        items.append({
            "key": "figures", "severity": "major",
            "text": f'{fig_missing} image{"s" if fig_missing != 1 else ""} missing alt text',
            "pages": [], "html": None,
        })

    # Document-level element-match fallback: only when no per-page deficit
    # already explains it, and never for fully-scanned docs (EMR is meaningless
    # there — mirrors the fully_scanned guards elsewhere).
    emr = sr.get("element_match_rate")
    scanned = sr.get("scanned_pages") or []
    total_pages = sr.get("total_pages") or 0
    fully_scanned = bool(scanned) and bool(total_pages) and len(scanned) == total_pages
    if emr is not None and emr < 85 and not deficits and not fully_scanned:
        items.append({
            "key": "emr", "severity": "major",
            "text": f"{emr:.0f}% of content not linked to screen-reader text",
            "pages": [], "html": None,
        })

    return items
