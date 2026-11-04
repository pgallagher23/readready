# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-PDF outcome records, JSONL writer, and log helpers.

``build_outcome_record`` is the single source of truth for the structured
record emitted by both production (``main.py``/``app.py``) and
the batch manifest harness. ``OutcomeWriter`` appends those records to
``outcomes.jsonl`` (and error events to a separate ``errors.jsonl``) safely
across processes. ``phase_guard`` standardises per-phase try/except +
``phase_status`` recording; ``run_file_log`` tees a content-scrubbed copy of the
console to ``console.log``.

Content safety: records hold paths, counts, codes, and numeric metrics only —
never the PDF's body text. The document title is reduced to ``has_title``;
content-bearing log lines are redacted by ``ContentScrubFilter``.
"""
import contextlib
import datetime
import fcntl
import json
import logging
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from audit.standards import resolve_verdict_config
from logging_util.failure_taxonomy import (
    FailureReason,
    _PHASE_TO_REASON,
    classify_failure_reasons,
    summarize_post_audit_advisories,
    summarize_post_audit_issue_types,
)

OUTCOME_SCHEMA_VERSION = 4  # 4: + docling yield fields (docling_pages/docling_elements/text_classification/forced_full_page_ocr)

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATEFMT = "%H:%M:%S"

# Critical phases — a failure here means the PDF was not properly remediated.
_CRITICAL_PHASES = ("wcag_pre_audit", "remediation", "wcag_post_audit")

# Loggers whose messages may contain document-derived text.
_CONTENT_LOGGERS = {
    "vlm.alt_text_generator": "alt text",
    "vlm.table_caption_generator": "caption",
    "vlm.formula_processor": "formula description",
}

# Superset of the manifest harness's ``_RESULT_DEFAULTS`` — every legacy key is
# kept (so ``--resume`` and the existing report tooling are unaffected); new
# keys are added on top.
OUTCOME_DEFAULTS: Dict[str, Any] = {
    # v4 docling-yield fields — present (possibly None) on EVERY record,
    # including manifest_fields/exception paths that bypass
    # _populate_from_result.
    "docling_pages": None,
    "docling_elements": None,
    "docling_pages_with_text": None,
    "text_classification": None,
    "forced_full_page_ocr": None,
    # legacy manifest keys
    "index": -1, "pdf": "?", "name": "?", "category": "unknown",
    "pages": 0, "status": "error", "error": None, "error_phase": None,
    "timings": {}, "phases": {}, "failed_gates": [],
    "wcag_pre_score": None, "wcag_post_score": None,
    "wcag_issues_before": None, "wcag_issues_after": None,
    "remediation_stats": None, "vlm_alt_text": None,
    "vlm_table_captions": None, "vlm_formulas": None, "gates": None,
    "verdict": None, "avg_coverage": None, "element_match_rate": None,
    "hollow_pages": [],
    # new outcome keys
    "schema_version": OUTCOME_SCHEMA_VERSION, "source": None, "run_id": None,
    "timestamp": None, "record_type": "pdf_outcome",
    "failure_reasons": [], "error_detail": None, "has_title": False,
    "sr_quality": None, "font_substitution": None, "source_quality_warnings": None,
    "struct_tree_issue_checks": None, "post_audit_issue_types": None,
    "post_audit_advisories": None,
    "wcag_failing_criteria": None,
    "stripped_text_pages_count": 0, "pdf_error": None,
}


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def new_run_id() -> str:
    """Sortable, collision-resistant run id: ``20260624T141233-ab12cd``."""
    return time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _format_tb(exc: BaseException) -> str:
    if exc.__traceback__ is not None:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return "".join(traceback.format_exception_only(type(exc), exc))


# ── record builder ──────────────────────────────────────────────────────────

def build_outcome_record(
    *, source: str, run_id: str, pdf_path: Any, index: int = -1,
    category: str = "unknown", pages: int = 0, result: Optional[Dict[str, Any]] = None,
    manifest_fields: Optional[Dict[str, Any]] = None, verdict: Optional[str] = None,
    exc: Optional[BaseException] = None, error_phase: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a per-PDF outcome record from a pipeline result / manifest record / exception."""
    rec = dict(OUTCOME_DEFAULTS)
    name = Path(str(pdf_path)).stem if pdf_path else "?"
    rec.update({
        "schema_version": OUTCOME_SCHEMA_VERSION, "source": source, "run_id": run_id,
        "timestamp": _utc_now_iso(), "record_type": "pdf_outcome",
        "index": index, "pdf": str(pdf_path) if pdf_path else "?",
        "name": name, "category": category, "pages": pages,
    })

    if manifest_fields is not None:
        for k, v in manifest_fields.items():
            rec[k] = v
        # re-stamp metadata the manifest record doesn't carry
        rec["schema_version"] = OUTCOME_SCHEMA_VERSION
        rec["source"] = source
        rec["run_id"] = run_id
        rec["record_type"] = "pdf_outcome"
        rec.setdefault("timestamp", _utc_now_iso())
    elif exc is not None:
        rec["status"] = "error"
        rec["error"] = str(exc)[:500]
        rec["error_phase"] = error_phase or "unknown"
        rec["error_detail"] = {
            "type": type(exc).__name__, "phase": error_phase,
            "traceback": _format_tb(exc)[-4000:],
        }
    elif result is not None:
        _populate_from_result(rec, result, verdict, config)

    rec["failure_reasons"] = classify_failure_reasons(rec, config=config)
    return rec


def _vlm_counts(d: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(d, dict):
        return None
    if d.get("error"):
        return {"error": str(d["error"])[:120]}
    updated = d.get("updated_count")
    if updated is None:
        updated = d.get("updated")
    skipped = d.get("skipped_count")
    if skipped is None:
        skipped = d.get("skipped")
    return {"updated": updated, "skipped": skipped}


_SR_SUBSET_KEYS = (
    "avg_coverage", "element_match_rate", "hollow_pages", "low_coverage_pages",
    "scanned_pages", "ocr_pages_passing_pct", "figures_total", "figures_missing_alt",
    "sr_quality", "has_struct_tree", "marked", "total_pages", "document_lang",
)


def _sr_subset(sr: Dict[str, Any]) -> Dict[str, Any]:
    # Note: ``issues`` is intentionally excluded — it can hold error message text.
    return {k: sr.get(k) for k in _SR_SUBSET_KEYS if k in sr}


def _validator_check_counts(issues: Any) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for it in issues or []:
        if isinstance(it, dict) and it.get("severity") in ("error", "warning"):
            chk = it.get("check")
            if chk:
                counts[chk] = counts.get(chk, 0) + 1
    return counts


def _font_substitution_summary(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Summarise the font-substitution sidecar — counts/reason codes only (no glyph text)."""
    report = result.get("font_substitution_report")
    data: Optional[Dict[str, Any]] = None
    if isinstance(report, dict):
        data = report
    elif isinstance(report, str) and report.endswith(".json"):
        try:
            with open(report, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
    if not isinstance(data, dict):
        return None
    summary = dict(data.get("summary") or {})
    reasons: Dict[str, int] = {}
    for sub in data.get("font_substitutions") or []:
        if isinstance(sub, dict) and sub.get("reason"):
            reasons[sub["reason"]] = reasons.get(sub["reason"], 0) + 1
    return {"summary": summary, "reasons": reasons}


def _compute_verdict(result: Dict[str, Any], config: Optional[Dict[str, Any]]) -> Optional[str]:
    """Compute the per-document WCAG verdict from a production result dict."""
    try:
        from audit.standards import compute_wcag_verdict
    except Exception:
        return None
    post = result.get("post_audit") or {}
    sr = result.get("sr_quality") or {}
    vc = resolve_verdict_config(config)
    scanned = sr.get("scanned_pages") or []
    total = sr.get("total_pages") or 0
    try:
        return compute_wcag_verdict(
            post,
            avg_coverage=sr.get("avg_coverage", 0) or 0,
            element_match_rate=sr.get("element_match_rate"),
            run_match_rate=sr.get("run_match_rate"),
            hollow_pages=sr.get("hollow_pages", []),
            sr_quality=sr.get("sr_quality", "unknown"),
            min_wcag_score=vc["min_wcag_score"],
            min_coverage=vc["min_coverage"],
            min_element_match_rate=vc["min_element_match_rate"],
            min_element_match_rate_when_covered=vc[
                "min_element_match_rate_when_covered"],
            min_run_match_rate_when_covered=vc[
                "min_run_match_rate_when_covered"],
            reject_hollow_pages=vc["reject_hollow_pages"],
            has_scanned_pages=bool(scanned),
            has_native_pages=(len(scanned) < (total or 1)),
            ocr_pages_passing_pct=sr.get("ocr_pages_passing_pct"),
            min_ocr_pages_passing_pct=vc["min_ocr_pages_passing_pct"],
        )
    except Exception:
        return None


def _failing_criteria(post: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    """Top-10 WCAG criteria still failing post-remediation (IDs + counts, no text).

    Mirrors the manifest runner's ``wcag_failing_criteria`` shape so production
    and batch records carry the same diagnostic field.
    """
    by_criterion = (post.get("compliance") or {}).get("by_criterion") or {}
    failing = {
        k: v for k, v in by_criterion.items()
        if isinstance(v, dict) and not v.get("passed", True) and v.get("failing", 0) > 0
    }
    return {
        k: {"failing": v["failing"], "total": v.get("total", 0)}
        for k, v in sorted(failing.items(), key=lambda x: -x[1].get("failing", 0))[:10]
    }


def _first_failed_critical(phase_status: Dict[str, Any]) -> Optional[str]:
    for name in _CRITICAL_PHASES:
        st = phase_status.get(name)
        if isinstance(st, dict) and st.get("status") == "failed":
            return name
    return None


def _populate_from_result(rec, result, verdict, config) -> None:
    ps = result.get("phase_status") or {}
    rec["phase_status"] = ps

    # Docling yield observability (conversion stats ride on the result dict).
    # Feeds check_docling_yield_low in classify_failure_reasons.
    _cstats = result.get("stats") or {}
    rec["docling_pages"] = _cstats.get("pages")
    rec["docling_elements"] = _cstats.get("structure_elements")
    rec["docling_pages_with_text"] = _cstats.get("pages_with_text")
    rec["text_classification"] = _cstats.get("text_classification")
    rec["forced_full_page_ocr"] = _cstats.get("forced_full_page_ocr")

    # Early-return error dicts (encryption / docling failure) carry only error+path.
    err = result.get("error")
    if err and not result.get("post_audit"):
        low = str(err).lower()
        rec["error"] = str(err)[:500]
        if "password" in low or "encrypt" in low:
            rec["status"] = "skipped"
            rec["error_phase"] = "preflight"
        elif "conversion failed" in low or "structure json" in low:
            rec["status"] = "error"
            rec["error_phase"] = "docling"
        else:
            rec["status"] = "error"
            rec["error_phase"] = "unknown"
        return

    audit = result.get("audit") or {}
    post = result.get("post_audit") or {}
    rec["wcag_pre_score"] = (audit.get("compliance") or {}).get("overall_score")
    rec["wcag_post_score"] = (post.get("compliance") or {}).get("overall_score")
    rec["wcag_issues_before"] = (audit.get("summary") or {}).get("total_issues")
    rec["wcag_issues_after"] = (post.get("summary") or {}).get("total_issues")
    rec["post_audit_issue_types"] = summarize_post_audit_issue_types(post)
    rec["post_audit_advisories"] = summarize_post_audit_advisories(post)
    rec["wcag_failing_criteria"] = _failing_criteria(post)

    rec["remediation_stats"] = (result.get("remediation") or {}).get("stats")
    rec["vlm_alt_text"] = _vlm_counts(result.get("alt_text"))
    rec["vlm_table_captions"] = _vlm_counts(result.get("table_captions"))
    rec["vlm_formulas"] = _vlm_counts(result.get("formula_descriptions"))

    sr = result.get("sr_quality") or {}
    rec["sr_quality"] = _sr_subset(sr)
    rec["avg_coverage"] = sr.get("avg_coverage")
    rec["element_match_rate"] = sr.get("element_match_rate")
    rec["hollow_pages"] = sr.get("hollow_pages") or []

    rec["source_quality_warnings"] = result.get("source_quality_warnings") or {}
    rec["struct_tree_issue_checks"] = _validator_check_counts(result.get("struct_tree_issues"))
    rec["stripped_text_pages_count"] = len(result.get("stripped_text_pages") or [])
    rec["has_title"] = bool(result.get("title"))
    rec["font_substitution"] = _font_substitution_summary(result)

    rec["verdict"] = verdict or _compute_verdict(result, config)

    # status precedence: tagger failure → critical-phase failure → verdict
    if result.get("pdf_error") or (ps.get("tagging", {}) or {}).get("status") == "failed":
        rec["status"] = "tagger_failed"
        rec["error_phase"] = "tagger"
        rec["pdf_error"] = result.get("pdf_error")
    else:
        failed_critical = _first_failed_critical(ps)
        if failed_critical:
            rec["status"] = "error"
            rec["error_phase"] = failed_critical
        else:
            rec["status"] = "passed" if rec["verdict"] == "pass" else "needs_review"


# ── JSONL writer ────────────────────────────────────────────────────────────

class OutcomeWriter:
    """Append-only JSONL writer, safe across processes via ``fcntl.flock``.

    Per-PDF outcome records go to ``outcomes.jsonl``; sub-phase error events go
    to a separate ``errors.jsonl`` so the outcomes file stays backward-compatible
    with ``--resume`` and the existing manifest report tooling.
    """

    def __init__(self, run_dir, run_id: str, *, jsonl_path=None, errors_path=None,
                 also_summary: bool = True):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.jsonl_path = Path(jsonl_path) if jsonl_path else self.run_dir / "outcomes.jsonl"
        self.errors_path = Path(errors_path) if errors_path else self.run_dir / "errors.jsonl"
        self.summary_path = self.run_dir / "run_summary.json" if also_summary else None

    @staticmethod
    def _append(path: Path, obj: Dict[str, Any]) -> None:
        line = json.dumps(obj, default=str) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line)
                f.flush()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def write_record(self, record: Dict[str, Any]) -> None:
        self._append(self.jsonl_path, record)

    def emit_error_event(self, *, pdf_path: Any, phase: str, message: str,
                         exc: Optional[BaseException] = None,
                         extra: Optional[Dict[str, Any]] = None) -> None:
        event = {
            "record_type": "error_event", "schema_version": OUTCOME_SCHEMA_VERSION,
            "run_id": self.run_id, "timestamp": _utc_now_iso(),
            "pdf": str(pdf_path) if pdf_path else "?",
            "name": Path(str(pdf_path)).stem if pdf_path else "?",
            "phase": phase, "message": str(message)[:500],
        }
        if exc is not None:
            event["error_detail"] = {"type": type(exc).__name__,
                                     "traceback": _format_tb(exc)[-4000:]}
        if extra:
            event.update(extra)
        self._append(self.errors_path, event)

    def emit_phase_errors(self, record: Dict[str, Any]) -> None:
        """Emit an error event for each ``failed`` phase recorded on an outcome."""
        for name, st in (record.get("phase_status") or {}).items():
            if isinstance(st, dict) and st.get("status") == "failed":
                self.emit_error_event(pdf_path=record.get("pdf"), phase=name,
                                      message=st.get("error") or "phase failed",
                                      extra={"reason": st.get("reason"),
                                             "type": st.get("type")})

    def write_summary(self, records: List[Dict[str, Any]], overall_time_s: float) -> None:
        if not self.summary_path:
            return
        statuses: Dict[str, int] = {}
        verdicts: Dict[str, int] = {}
        reason_counts: Dict[str, int] = {}
        for r in records:
            st = r.get("status") or "unknown"
            statuses[st] = statuses.get(st, 0) + 1
            if r.get("verdict"):
                verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
            for fr in r.get("failure_reasons") or []:
                c = fr.get("code")
                reason_counts[c] = reason_counts.get(c, 0) + 1
        summary = {
            "run_id": self.run_id, "timestamp": _utc_now_iso(),
            "total": len(records), "overall_time_s": round(overall_time_s, 1),
            "by_status": statuses, "by_verdict": verdicts,
            "top_failure_reasons": dict(sorted(reason_counts.items(),
                                               key=lambda kv: kv[1], reverse=True)),
        }
        with open(self.summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, default=str)


# ── phase guard ─────────────────────────────────────────────────────────────

def _exception_reason(exc: Exception, name: str) -> str:
    blob = (type(exc).__name__ + " " + str(exc)).lower()
    if isinstance(exc, MemoryError) or "outofmemory" in blob:
        return FailureReason.oom_or_memory_error.value
    if isinstance(exc, TimeoutError) or "timeout" in blob:
        return FailureReason.timeout.value
    code = _PHASE_TO_REASON.get(name)
    return code.value if code else FailureReason.unknown_error.value


@contextlib.contextmanager
def phase_guard(phase_status: Dict[str, Any], name: str, *,
                logger: Optional[logging.Logger] = None, reraise: bool = False):
    """Record a phase's success/failure into ``phase_status`` without crashing the PDF.

    On exception (``Exception`` only — ``KeyboardInterrupt``/``SystemExit`` propagate):
    records ``{status: failed, error, type, reason}``, logs a warning, and swallows
    unless ``reraise``. Pure (no writer dependency) so it is worker/pickle-safe.
    """
    t0 = time.monotonic()
    try:
        yield
    except Exception as exc:
        phase_status[name] = {
            "status": "failed", "error": str(exc)[:200], "type": type(exc).__name__,
            "reason": _exception_reason(exc, name), "time_s": round(time.monotonic() - t0, 3),
        }
        if logger is not None:
            logger.warning("Phase %s failed: %s", name, exc)
        if reraise:
            raise
    else:
        phase_status[name] = {"status": "success", "time_s": round(time.monotonic() - t0, 3)}


# ── content-scrubbing log filter + per-run file handler ─────────────────────

class ContentScrubFilter(logging.Filter):
    """Redact document-derived text from records before they reach ``console.log``.

    Matches records flagged ``extra={"contains_content": True}`` or emitted by a
    known content-bearing logger. Returns True (keep) but rewrites the message to a
    stub like ``figure-3.png: [alt text redacted]``. Attach to the file handler only.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        flagged = getattr(record, "contains_content", False)
        label = _CONTENT_LOGGERS.get(record.name)
        if flagged or label is not None:
            ref = getattr(record, "content_ref", "")
            label = label or "content"
            record.msg = (f"{ref}: " if ref else "") + f"[{label} redacted]"
            record.args = ()
        return True


@contextlib.contextmanager
def run_file_log(log_dir, *, level: Optional[int] = None, fmt: str = LOG_FORMAT):
    """Tee a content-scrubbed copy of the console to ``<log_dir>/console.log``.

    Attaches a ``FileHandler`` to the ROOT logger (so module loggers like
    ``vlm.*`` are captured), with a ``ContentScrubFilter``. Removes and closes the
    handler on exit to avoid handler-leak / cross-run bleed in long-running servers.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / "console.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter(fmt, datefmt=LOG_DATEFMT))
    handler.addFilter(ContentScrubFilter())
    if level is not None:
        handler.setLevel(level)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        handler.close()


def update_latest_symlink(runs_root, run_id: str) -> None:
    """Best-effort: point ``<runs_root>/latest`` at the newest run directory."""
    try:
        runs_root = Path(runs_root)
        link = runs_root / "latest"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(run_id)
    except Exception:
        pass


def iter_failed_phases(record: Dict[str, Any]) -> Iterable[str]:
    for name, st in (record.get("phase_status") or {}).items():
        if isinstance(st, dict) and st.get("status") == "failed":
            yield name
