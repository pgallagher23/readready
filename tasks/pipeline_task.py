# SPDX-License-Identifier: AGPL-3.0-or-later
"""Celery task wrapping the PDF accessibility pipeline."""

import math
from pathlib import Path

from tasks.celery_app import celery_app


def _sanitize_for_json(obj):
    """Recursively replace non-JSON-safe values (NaN, Inf, bytes, Path, set).

    Celery's JSON serializer rejects these types. Must be applied before
    returning task results.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, set):
        return list(obj)
    return obj


def _apply_tagger_slots(config: dict) -> dict:
    """Declare this worker's real tagger concurrency in the config.

    ``concurrency.batch_workers`` describes CLI batch mode, where several
    PDFs run in parallel.  A Celery worker runs ``--concurrency=N``
    instead, so sizing the JVM heap off ``batch_workers`` divides the
    memory budget by the wrong number and can starve the tagger down to
    its 512m floor.  Pin the real value so ``resolve_tagger_slots`` uses
    it.  Returns a copy — the caller's dict is left alone.
    """
    from output_gen.pdf_tagger import celery_tagger_slots

    prepared = dict(config or {})
    prepared["concurrency"] = {
        **prepared.get("concurrency", {}),
        "tagger_slots": celery_tagger_slots(config),
    }
    return prepared


def _local_llm_api_key() -> str:
    """``llm.api_key`` from this worker's own config.yaml, or empty."""
    try:
        from utils.config_loader import load_config
        return str((load_config().get("llm") or {}).get("api_key") or "")
    except Exception:
        return ""


def _restore_local_secrets(config: dict) -> dict:
    """Refill ``llm.api_key`` that the submitter stripped before queueing.

    Environment keys are read by the clients themselves; this only covers a
    key kept in the worker's mounted config.yaml. Returns a copy.
    """
    key = _local_llm_api_key()
    if not key:
        return config
    out = dict(config or {})
    llm = dict(out.get("llm") or {})
    if not llm.get("api_key"):
        llm["api_key"] = key
    out["llm"] = llm
    return out


@celery_app.task(bind=True, name="process_pdf")
def process_pdf_task(self, pdf_path: str, output_dir: str, config: dict) -> dict:
    """Process a single PDF through the accessibility pipeline.

    This runs in a Celery worker process. Progress is reported via
    Celery's task state mechanism so the web UI can poll for updates.

    Args:
        pdf_path: Absolute path to the input PDF.
        output_dir: Base output directory.
        config: Full config dict (all sections from config.yaml).

    Returns:
        JSON-serializable dict with pipeline results (same as
        pipeline_core.run_pipeline return value).
    """
    from pipeline_core import run_pipeline

    phase_log = []

    def progress_fn(frac: float, desc: str = "") -> None:
        if desc:
            phase_log.append(desc)
        self.update_state(
            state="PROGRESS",
            meta={"progress": frac, "phase": desc, "phase_log": list(phase_log)},
        )

    result = run_pipeline(
        pdf_path=pdf_path,
        output_dir=output_dir,
        config=_apply_tagger_slots(_restore_local_secrets(config)),
        progress_fn=progress_fn,
        clean_output=True,
    )
    return _sanitize_for_json(result)
