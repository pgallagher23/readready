# SPDX-License-Identifier: AGPL-3.0-or-later
"""Adaptive task dispatch — Celery when available, synchronous fallback.

This is the main entry point for submitting pipeline tasks. It
automatically detects whether Redis/Celery is available and falls
back to synchronous in-process execution for local development.

Usage:
    from tasks.task_runner import submit_task, is_celery_available

    handle = submit_task(pdf_path, output_dir, config)
    while not handle.is_ready():
        progress = handle.get_progress()
        time.sleep(1)
    result = handle.get_result()
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_celery_available: Optional[bool] = None


def _redact_url(url: str) -> str:
    """Return the URL with its password replaced by *** (for logging)."""
    parts = urlsplit(url)
    if not parts.password:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return parts._replace(netloc=f"{parts.username or ''}:***@{host}").geturl()


def is_celery_available() -> bool:
    """Check if Redis is reachable and Celery can be used.

    Result is cached after the first check. Returns False if celery
    or redis packages are not installed, or if Redis is unreachable.
    """
    global _celery_available
    if _celery_available is not None:
        return _celery_available

    try:
        import redis as _redis
        from tasks.celery_app import celery_app

        broker_url = celery_app.conf.broker_url
        conn = _redis.Redis.from_url(broker_url, socket_timeout=2)
        conn.ping()
        conn.close()
        _celery_available = True
        logger.info("Celery/Redis available at %s", _redact_url(broker_url))
    except ImportError:
        logger.info("Celery or redis package not installed — using synchronous mode")
        _celery_available = False
    except Exception as e:
        logger.info("Redis not reachable (%s) — using synchronous mode", e)
        _celery_available = False

    return _celery_available


def reset_celery_check() -> None:
    """Reset the cached Celery availability check (for testing)."""
    global _celery_available
    _celery_available = None


class TaskHandle(ABC):
    """Unified interface for async (Celery) and sync task results."""

    @abstractmethod
    def is_ready(self) -> bool:
        """Check if the task has completed."""
        ...

    @abstractmethod
    def get_result(self) -> Dict[str, Any]:
        """Get the task result. Blocks if not ready."""
        ...

    @abstractmethod
    def get_progress(self) -> Tuple[float, str, List[str]]:
        """Get current progress as (fraction, description, phase_log)."""
        ...

    @abstractmethod
    def failed(self) -> bool:
        """Check if the task failed."""
        ...

    @abstractmethod
    def get_error(self) -> Optional[str]:
        """Get error message if task failed, else None."""
        ...

    def is_queued(self) -> bool:
        """True while the task waits in the queue and has not started."""
        return False

    def revoke(self) -> None:
        """Stop the task (no-op when it already ran)."""


class CeleryTaskHandle(TaskHandle):
    """Wraps a Celery AsyncResult."""

    def __init__(self, async_result):
        self._result = async_result

    @property
    def task_id(self) -> str:
        return self._result.id

    def is_ready(self) -> bool:
        return self._result.ready()

    def get_result(self) -> Dict[str, Any]:
        return self._result.get()

    def get_progress(self) -> Tuple[float, str, List[str]]:
        if self._result.state == "PROGRESS":
            info = self._result.info or {}
            return info.get("progress", 0.0), info.get("phase", ""), info.get("phase_log", [])
        if self._result.ready():
            return 1.0, "Done", []
        return 0.0, "Queued", []

    def failed(self) -> bool:
        return self._result.failed()

    def get_error(self) -> Optional[str]:
        if self._result.failed():
            return str(self._result.result)
        return None

    def is_queued(self) -> bool:
        # task_track_started=True, so a task a worker picked up is STARTED
        # (then PROGRESS); PENDING means it is still waiting in the queue.
        return self._result.state == "PENDING"

    def revoke(self) -> None:
        self._result.revoke(terminate=True)


class SyncTaskHandle(TaskHandle):
    """Wraps a synchronously completed result."""

    def __init__(self, result: Optional[Dict] = None, error: Optional[str] = None):
        self._result = result
        self._error = error

    def is_ready(self) -> bool:
        return True

    def get_result(self) -> Dict[str, Any]:
        if self._error:
            raise RuntimeError(self._error)
        return self._result or {}

    def get_progress(self) -> Tuple[float, str, List[str]]:
        return 1.0, "Done", []

    def failed(self) -> bool:
        return self._error is not None

    def get_error(self) -> Optional[str]:
        return self._error


def strip_task_secrets(config: dict) -> dict:
    """Return a copy of ``config`` without ``llm.api_key``.

    Task arguments are stored in the broker and result backend; the worker
    refills the key from its own environment or mounted config.yaml.
    """
    out = dict(config or {})
    llm = out.get("llm")
    if isinstance(llm, dict) and "api_key" in llm:
        llm = dict(llm)
        llm.pop("api_key")
        out["llm"] = llm
    return out


def submit_task(
    pdf_path: str,
    output_dir: str,
    config: dict,
) -> TaskHandle:
    """Submit a pipeline task. Returns a handle for polling.

    If Celery/Redis is available, submits as an async Celery task.
    Otherwise, runs synchronously in-process and returns immediately.
    """
    if is_celery_available():
        from tasks.pipeline_task import process_pdf_task
        async_result = process_pdf_task.delay(pdf_path, output_dir, strip_task_secrets(config))
        logger.info("Submitted Celery task %s for %s", async_result.id, pdf_path)
        return CeleryTaskHandle(async_result)

    # Synchronous fallback — blocks the calling thread (Gradio UI) for
    # the full pipeline duration.  In production this means Redis is down.
    logger.warning(
        "Redis/Celery unavailable — running pipeline SYNCHRONOUSLY for %s. "
        "This blocks the web UI thread. Check Redis connectivity.",
        pdf_path,
    )
    try:
        from pipeline_core import run_pipeline
        result = run_pipeline(
            pdf_path=pdf_path,
            output_dir=output_dir,
            config=config,
            clean_output=True,
        )
        return SyncTaskHandle(result=result)
    except Exception as e:
        logger.exception("Pipeline failed for %s", pdf_path)
        return SyncTaskHandle(error=str(e))
