# SPDX-License-Identifier: AGPL-3.0-or-later
"""The prefork pool child must be recycled after every task.

A long-lived child never returns the memory a large document drove it to:
on prod it sat idle at 9.2GB anon RSS under a 10GB container cap, so the next
trivial one-page job was OOM-killed (6 kills, 2026-10-04..08). Docling's
converter is rebuilt per task anyway, so a fresh child costs only imports.

The default lives in code, not config.yaml: prod's config.yaml is frozen at
first install (server-setup.sh never overwrites it), so a config-only key
would never reach the box.

Run: python -m pytest tests/test_celery_worker_recycling.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytest.importorskip("celery")

from tasks import celery_app as celery_app_module


def _make_with_queue_cfg(monkeypatch, queue_cfg):
    import utils.config_loader as config_loader
    monkeypatch.setattr(config_loader, "load_config", lambda *a, **k: {"queue": queue_cfg})
    return celery_app_module.make_celery_app()


def test_default_recycles_child_after_every_task(monkeypatch):
    app = _make_with_queue_cfg(monkeypatch, {})
    assert app.conf.worker_max_tasks_per_child == 1


def test_default_survives_unreadable_config(monkeypatch):
    import utils.config_loader as config_loader

    def _boom(*a, **k):
        raise OSError("config.yaml missing")

    monkeypatch.setattr(config_loader, "load_config", _boom)
    app = celery_app_module.make_celery_app()
    assert app.conf.worker_max_tasks_per_child == 1


def test_config_can_override(monkeypatch):
    app = _make_with_queue_cfg(monkeypatch, {"worker_max_tasks_per_child": 5})
    assert app.conf.worker_max_tasks_per_child == 5
