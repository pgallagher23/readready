# SPDX-License-Identifier: AGPL-3.0-or-later
"""API keys never travel through the Celery broker.

The web process strips ``llm.api_key`` before the task is queued; the
worker refills it from its own environment or mounted config.yaml.
"""
from tasks import pipeline_task, task_runner


def test_submit_payload_has_no_api_key():
    cfg = {"llm": {"provider": "openai", "api_key": "sk-live", "model": "m"}, "ui": {}}
    out = task_runner.strip_task_secrets(cfg)
    assert "api_key" not in out["llm"]
    assert out["llm"]["model"] == "m"
    assert cfg["llm"]["api_key"] == "sk-live", "caller's config is left intact"


def test_strip_handles_missing_llm_section():
    assert task_runner.strip_task_secrets({"ui": {}}) == {"ui": {}}


def test_worker_refills_api_key_from_local_config(monkeypatch):
    monkeypatch.setattr(pipeline_task, "_local_llm_api_key", lambda: "sk-local")
    cfg = pipeline_task._restore_local_secrets({"llm": {"provider": "openai"}})
    assert cfg["llm"]["api_key"] == "sk-local"


def test_worker_keeps_a_key_the_task_already_has(monkeypatch):
    monkeypatch.setattr(pipeline_task, "_local_llm_api_key", lambda: "sk-local")
    cfg = pipeline_task._restore_local_secrets({"llm": {"api_key": "sk-task"}})
    assert cfg["llm"]["api_key"] == "sk-task"


def test_worker_without_local_key_leaves_config_alone(monkeypatch):
    monkeypatch.setattr(pipeline_task, "_local_llm_api_key", lambda: "")
    assert pipeline_task._restore_local_secrets({"ui": {}}) == {"ui": {}}
