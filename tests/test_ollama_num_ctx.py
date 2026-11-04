# SPDX-License-Identifier: AGPL-3.0-or-later
from vlm.ollama_client import OllamaClient


class _R:  # minimal fake response
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"content": "ok"}}


def _capture(monkeypatch, client):
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs["json"])
        return _R()

    monkeypatch.setattr(client.session, "post", fake_post)
    return captured


def test_default_base_url_is_localhost():
    assert OllamaClient().base_url == "http://localhost:11434"


def test_from_config_default_base_url_is_localhost():
    assert OllamaClient.from_config({}).base_url == "http://localhost:11434"


def test_from_config_reads_num_ctx():
    c = OllamaClient.from_config({"num_ctx": 8192})
    assert c.num_ctx == 8192


def test_payload_includes_num_ctx(monkeypatch):
    c = OllamaClient(num_ctx=8192)
    captured = _capture(monkeypatch, c)
    c.generate("hi", images=None, system_prompt=None)
    assert captured["options"]["num_ctx"] == 8192


def test_payload_omits_num_ctx_when_unset(monkeypatch):
    c = OllamaClient()
    captured = _capture(monkeypatch, c)
    c.generate("hi")
    assert "num_ctx" not in captured["options"]
