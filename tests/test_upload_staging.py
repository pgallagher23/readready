# SPDX-License-Identifier: AGPL-3.0-or-later
"""Uploads are copied into the session directory before processing.

Gradio deletes its upload cache after ``delete_cache`` seconds, but the
session (and its Update PDF re-tag, which re-reads the source) lives for
hours. Both the Celery and the in-process paths must therefore run on a
copy that shares the session's lifetime.
"""
import inspect

import app


def test_stage_upload_copies_into_session_dir(tmp_path):
    src = tmp_path / "cache" / "report.pdf"
    src.parent.mkdir()
    src.write_bytes(b"%PDF-1.4\n")
    session = tmp_path / "session"
    session.mkdir()

    staged = app._stage_upload(src, session, "report.pdf")

    assert staged == session / "report.pdf"
    assert staged.read_bytes() == b"%PDF-1.4\n"
    assert src.exists()


def test_stage_upload_is_idempotent_when_already_in_place(tmp_path):
    session = tmp_path / "session"
    session.mkdir()
    src = session / "report.pdf"
    src.write_bytes(b"%PDF-1.4\n")

    assert app._stage_upload(src, session, "report.pdf") == src
    assert src.read_bytes() == b"%PDF-1.4\n"


def test_both_processing_paths_stage_the_upload():
    assert "_stage_upload(" in inspect.getsource(app._process_via_celery)
    assert "_stage_upload(" in inspect.getsource(app.process_batch)
