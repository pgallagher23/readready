# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Redis password must never reach the logs."""
import logging

import redis

from tasks import task_runner
from tasks.celery_app import celery_app


class _Conn:
    def ping(self):
        return True

    def close(self):
        pass


def test_availability_log_hides_password(monkeypatch, caplog):
    monkeypatch.setattr(celery_app.conf, "broker_url", "redis://:hunter2secret@redis:6379/0")
    monkeypatch.setattr(redis.Redis, "from_url", classmethod(lambda cls, url, **kw: _Conn()))
    task_runner.reset_celery_check()
    try:
        with caplog.at_level(logging.INFO, logger=task_runner.__name__):
            assert task_runner.is_celery_available()
    finally:
        task_runner.reset_celery_check()
    assert "hunter2secret" not in caplog.text
    assert "redis:6379/0" in caplog.text


def test_redact_url_keeps_user_and_host():
    assert task_runner._redact_url("redis://admin:pw@host:6380/1") == "redis://admin:***@host:6380/1"
    assert task_runner._redact_url("redis://localhost:6379/0") == "redis://localhost:6379/0"
