# SPDX-License-Identifier: AGPL-3.0-or-later
"""JVM heap sizing for the iText tagger / content-stream analyzer.

Regression cover for the production OOM where the Celery worker ran the
analyzer JVM at the 512m floor (see ``compute_java_heap``): the memory
budget was divided by ``concurrency.batch_workers`` (a CLI-only setting)
even though the worker runs ``--concurrency=1`` and only ever has one PDF
in flight.
"""

import logging

import pytest

from output_gen.pdf_tagger import (
    celery_tagger_slots,
    compute_java_heap,
    resolve_tagger_slots,
)


def _heap_mb(heap: str) -> int:
    """Parse a '-Xmx' style string like '512m' / '2g' into MB."""
    heap = heap.strip().lower()
    if heap.endswith("g"):
        return int(float(heap[:-1]) * 1024)
    if heap.endswith("m"):
        return int(float(heap[:-1]))
    raise AssertionError(f"unparseable heap string: {heap!r}")


# Primary VM worker container: PodmanArgs=--memory=10g
PROD_WORKER_RAM_MB = 10 * 1024
# deploy/config.production.yaml
PROD_RESOURCES = {
    "java_heap": "auto",
    "max_ram_percent": 85,
    "python_worker_mb": 4000,
}
PROD_CONCURRENCY = {"batch_workers": 2, "celery_concurrency": 1}


def prod_config() -> dict:
    """A fresh copy of the production worker config, per test."""
    return {
        "resources": dict(PROD_RESOURCES),
        "concurrency": dict(PROD_CONCURRENCY),
    }


@pytest.fixture
def fixed_ram(monkeypatch):
    """Pin the detected system RAM so heap math is deterministic."""

    def _set(total_mb):
        monkeypatch.setattr(
            "output_gen.pdf_tagger.get_system_ram_mb", lambda: total_mb
        )

    return _set


# ── resolve_tagger_slots ────────────────────────────────────────────

def test_tagger_slots_defaults_to_batch_workers():
    config = {"concurrency": {"batch_workers": 4}}
    assert resolve_tagger_slots(config) == 4


def test_tagger_slots_prefers_explicit_override_over_batch_workers():
    """An injected tagger_slots wins — that is how the Celery worker
    declares that only one tagger runs at a time."""
    config = {"concurrency": {"batch_workers": 2, "tagger_slots": 1}}
    assert resolve_tagger_slots(config) == 1


def test_tagger_slots_is_at_least_one():
    assert resolve_tagger_slots({"concurrency": {"batch_workers": 0}}) == 1


def test_tagger_slots_on_empty_config_uses_documented_default():
    assert resolve_tagger_slots({}) == 2


# ── celery_tagger_slots ─────────────────────────────────────────────

def test_celery_slots_come_from_celery_concurrency_not_batch_workers():
    config = {"concurrency": PROD_CONCURRENCY}
    assert celery_tagger_slots(config) == 1


def test_celery_slots_default_to_one_when_unset():
    assert celery_tagger_slots({"concurrency": {"batch_workers": 8}}) == 1


# ── compute_java_heap ───────────────────────────────────────────────

def test_celery_worker_is_not_starved_to_the_floor(fixed_ram):
    """THE REGRESSION.

    On the 10GB worker container the old code computed
    (10240*0.85 - 2*4000) // 2 = 352MB -> clamped to the 512m floor,
    which OOMs the analyzer on large PDFs. With one tagger in flight the
    same container affords ~4.7GB.
    """
    fixed_ram(PROD_WORKER_RAM_MB)
    config = prod_config()

    # What tasks/pipeline_task.py injects before handing config to the
    # pipeline, then what pipeline_core does with it.
    config["concurrency"]["tagger_slots"] = celery_tagger_slots(config)
    heap = compute_java_heap(config, batch_workers=resolve_tagger_slots(config))

    assert _heap_mb(heap) > 512, "heap collapsed to the floor"
    assert _heap_mb(heap) >= 2048, f"heap {heap} too small for large PDFs"


def test_heap_budget_exhaustion_is_logged_not_silent(fixed_ram, caplog):
    """Hitting the 512m floor must warn — the silent clamp is what let
    this ship to production unnoticed."""
    fixed_ram(PROD_WORKER_RAM_MB)
    config = prod_config()

    with caplog.at_level(logging.WARNING, logger="output_gen.pdf_tagger"):
        heap = compute_java_heap(config, batch_workers=2)

    assert _heap_mb(heap) == 512
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "clamping to the 512m floor produced no warning"
    )


def test_explicit_java_heap_is_passed_through_untouched(fixed_ram):
    fixed_ram(PROD_WORKER_RAM_MB)
    config = {"resources": {"java_heap": "3g"}}
    assert compute_java_heap(config, batch_workers=8) == "3g"


def test_bulk_box_sizing_is_unchanged(fixed_ram):
    """Guard against collateral damage: the 24GB bulk container already
    computed a healthy heap and must keep doing so."""
    fixed_ram(24 * 1024)
    config = {"resources": dict(PROD_RESOURCES)}
    assert _heap_mb(compute_java_heap(config, batch_workers=2)) == 6444


def test_heap_is_capped_at_8g(fixed_ram):
    fixed_ram(256 * 1024)
    config = {"resources": dict(PROD_RESOURCES)}
    assert _heap_mb(compute_java_heap(config, batch_workers=1)) == 8192


# ── Celery task wiring ──────────────────────────────────────────────

def test_celery_task_declares_its_true_tagger_concurrency():
    """The worker must tell the pipeline it runs one PDF at a time,
    otherwise the heap budget gets divided by batch_workers."""
    from tasks.pipeline_task import _apply_tagger_slots

    config = prod_config()
    prepared = _apply_tagger_slots(config)

    assert resolve_tagger_slots(prepared) == 1
    assert "tagger_slots" not in config["concurrency"], (
        "caller's config was mutated"
    )
