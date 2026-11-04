# SPDX-License-Identifier: AGPL-3.0-or-later
"""A task that fails on a worker must not be re-run in the web process."""
import pytest

import app


class _Handle:
    def __init__(self, ready_after, failed=False):
        self.n, self.ready_after, self._failed = 0, ready_after, failed

    def is_ready(self):
        self.n += 1
        return self.n > self.ready_after

    def get_progress(self):
        return 0.5, "phase", []

    def failed(self):
        return self._failed

    def get_error(self):
        return "TimeLimitExceeded"

    def get_result(self):
        return {"download_files": []}

    def is_queued(self):
        return False

    def revoke(self):
        pass


def test_worker_failure_raises_task_failed(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda s: None)
    with pytest.raises(app._TaskFailed):
        app._poll_celery(_Handle(0, failed=True), 60, lambda *a: None)


def test_poll_has_a_deadline(monkeypatch):
    clock = iter(range(0, 10_000, 10))
    monkeypatch.setattr(app.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(app.time, "sleep", lambda s: None)
    with pytest.raises(app._TaskFailed, match="deadline"):
        app._poll_celery(_Handle(10**9), 60, lambda *a: None)


def test_success_returns_result(monkeypatch):
    monkeypatch.setattr(app.time, "sleep", lambda s: None)
    assert app._poll_celery(_Handle(2), 60, lambda *a: None) == {"download_files": []}


def test_reject_on_worker_lost_is_off():
    from tasks.celery_app import make_celery_app
    assert make_celery_app().conf.task_reject_on_worker_lost is False


class _QueuedHandle(_Handle):
    """Reports PENDING (queued) for ``queued_polls`` polls, then runs."""

    def __init__(self, queued_polls, ready_after):
        super().__init__(ready_after)
        self.queued_polls = queued_polls
        self.revoked = False

    def is_queued(self):
        return self.n <= self.queued_polls

    def revoke(self):
        self.revoked = True


def _fake_clock(monkeypatch, step=10):
    clock = iter(range(0, 10_000_000, step))
    monkeypatch.setattr(app.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(app.time, "sleep", lambda s: None)


def test_queue_wait_does_not_count_toward_run_deadline(monkeypatch):
    _fake_clock(monkeypatch)
    # ~500s queued (50 polls x 10s), then ~50s running: under a 60s run
    # deadline once queue time is excluded.
    h = _QueuedHandle(queued_polls=50, ready_after=55)
    assert app._poll_celery(h, 60, lambda *a: None, queue_deadline_s=10_000) == {
        "download_files": []}
    assert not h.revoked


def test_run_deadline_revokes_the_task(monkeypatch):
    _fake_clock(monkeypatch)
    h = _QueuedHandle(queued_polls=0, ready_after=10**9)
    with pytest.raises(app._TaskFailed, match="deadline"):
        app._poll_celery(h, 60, lambda *a: None, queue_deadline_s=10_000)
    assert h.revoked


def test_queue_wait_has_its_own_ceiling(monkeypatch):
    _fake_clock(monkeypatch)
    h = _QueuedHandle(queued_polls=10**9, ready_after=10**9)
    with pytest.raises(app._TaskFailed, match="busy"):
        app._poll_celery(h, 60, lambda *a: None, queue_deadline_s=300)
    assert h.revoked


def test_celery_handle_reports_queued_and_revokes():
    from tasks.task_runner import CeleryTaskHandle

    class _AR:
        id = "t1"
        state = "PENDING"
        revoked_with = None

        def revoke(self, terminate=False):
            self.revoked_with = terminate

    ar = _AR()
    h = CeleryTaskHandle(ar)
    assert h.is_queued() is True
    ar.state = "STARTED"
    assert h.is_queued() is False
    h.revoke()
    assert ar.revoked_with is True


def test_error_after_submission_is_not_rerun_in_process(monkeypatch, tmp_path):
    class _Broken:
        def is_ready(self):
            raise ConnectionError("redis went away")

    import tasks.task_runner as tr
    monkeypatch.setattr(tr, "submit_task", lambda *a, **k: _Broken())
    monkeypatch.setattr(
        app, "_run_single_doc_pipeline",
        lambda *a, **k: pytest.fail("must not re-run in the web process"))
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    out = tmp_path / "out"
    out.mkdir()
    res = app._process_via_celery(pdf, "doc.pdf", out, {}, lambda *a: None, [])
    report_parts, download_files, pipeline_result = res
    assert "error" in pipeline_result
    assert download_files == []
    assert report_parts["status_html"]


def test_error_before_submission_requests_sync_fallback(monkeypatch, tmp_path):
    import tasks.task_runner as tr

    def _boom(*a, **k):
        raise ConnectionError("no broker")

    monkeypatch.setattr(tr, "submit_task", _boom)
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    out = tmp_path / "out"
    out.mkdir()
    assert app._process_via_celery(pdf, "doc.pdf", out, {}, lambda *a: None, []) is None
