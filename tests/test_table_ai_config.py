# SPDX-License-Identifier: AGPL-3.0-or-later
"""Table AI uses the run's LLM config, per document, never config.yaml directly."""
import pytest

from remediate.strategies import table_remediation as tr


class _FakeClient:
    def check_connection(self):
        return True


@pytest.fixture(autouse=True)
def _reset_table_ai():
    """Leave no table-AI state behind for later tests in this context."""
    tr._CONNECTIVITY.clear()
    yield
    tr.configure_table_ai(None, None)
    tr._CONNECTIVITY.clear()


@pytest.fixture
def client_spy(monkeypatch):
    calls = []

    def _fake_get_text_client(llm_config):
        calls.append(llm_config)
        return _FakeClient()

    import vlm.table_structure_fixer as tsf
    monkeypatch.setattr(tsf, "_get_text_client", _fake_get_text_client)
    return calls


def test_no_llm_config_means_no_client_is_ever_built(client_spy):
    tr.configure_table_ai(None, {"enabled": True, "max_tables_per_document": 30})
    assert tr._can_use_ai_table_analysis() is False
    assert client_spy == []


def test_client_is_built_from_the_given_config(client_spy):
    cfg = {"provider": "ollama", "base_url": "http://run-specific"}
    tr.configure_table_ai(cfg, {"enabled": True, "max_tables_per_document": 30})
    assert tr._can_use_ai_table_analysis() is True
    assert client_spy == [cfg]


def test_state_does_not_leak_between_documents(client_spy):
    tr.configure_table_ai({"provider": "ollama", "base_url": "http://x"}, {"enabled": True})
    assert tr._can_use_ai_table_analysis() is True
    tr.configure_table_ai(None, {"enabled": True})
    assert tr._can_use_ai_table_analysis() is False


def test_per_document_call_cap(client_spy):
    tr.configure_table_ai({"provider": "ollama", "base_url": "http://x"},
                          {"enabled": True, "max_tables_per_document": 1})
    assert tr._can_use_ai_table_analysis() is True
    tr._TABLE_AI.get().calls = 1
    assert tr._can_use_ai_table_analysis() is False


def test_remediate_html_without_ai_option_never_builds_a_client(client_spy):
    from remediate.remediator import remediate_html
    html = ("<html lang='en'><body><table><tr><td>Name</td><td>Age</td></tr>"
            "<tr><td>A</td><td>1</td></tr></table></body></html>")
    remediate_html(html_content=html, issues=[], options={"fix_tables": True})
    assert client_spy == []


def test_table_remediation_never_reads_config_yaml():
    from pathlib import Path
    src = Path(tr.__file__).read_text()
    assert "load_config" not in src


def test_client_failure_fails_closed(monkeypatch):
    class _Raising:
        def check_connection(self):
            raise ConnectionError("refused")

    import vlm.table_structure_fixer as tsf
    monkeypatch.setattr(tsf, "_get_text_client", lambda cfg: _Raising())
    tr.configure_table_ai({"provider": "ollama", "base_url": "http://x"}, {"enabled": True})
    assert tr._table_ai_client() is None
    assert tr._can_use_ai_table_analysis() is False


def test_client_build_failure_fails_closed(monkeypatch):
    def _boom(cfg):
        raise ValueError("bad provider")

    import vlm.table_structure_fixer as tsf
    monkeypatch.setattr(tsf, "_get_text_client", _boom)
    tr.configure_table_ai({"provider": "nope", "base_url": "http://x"}, {"enabled": True})
    assert tr._table_ai_client() is None


def test_pipeline_passes_llm_config_only_when_an_llm_is_configured():
    import pipeline_core
    llm = {"provider": "ollama", "base_url": "http://127.0.0.1:9", "model": "m"}
    opts = pipeline_core._table_ai_options({"llm": llm, "table_analysis": {"enabled": True}})
    assert opts["ai_llm_config"] is llm
    assert opts["table_analysis"] == {"enabled": True}

    opts = pipeline_core._table_ai_options({"llm": {}, "table_analysis": {"enabled": True}})
    assert opts["ai_llm_config"] is None
    assert pipeline_core._table_ai_options({})["ai_llm_config"] is None


def test_run_pipeline_uses_the_table_ai_options_helper():
    import inspect
    import pipeline_core
    src = inspect.getsource(pipeline_core.run_pipeline)
    assert "remediation_options.update(_table_ai_options(config))" in src
    assert 'remediation_options["ai_llm_config"]' not in src


def test_connectivity_is_checked_once_per_process_for_the_same_config(monkeypatch):
    pings = []

    class _Counting:
        def check_connection(self):
            pings.append(1)
            return True

    import vlm.table_structure_fixer as tsf
    monkeypatch.setattr(tsf, "_get_text_client", lambda cfg: _Counting())
    cfg = {"provider": "ollama", "base_url": "http://once"}
    for _ in range(3):
        tr.configure_table_ai(cfg, {"enabled": True})
        assert tr._table_ai_client() is not None
    assert len(pings) == 1


def test_unreachable_endpoint_is_remembered_per_process(monkeypatch):
    pings = []

    class _Down:
        def check_connection(self):
            pings.append(1)
            return False

    import vlm.table_structure_fixer as tsf
    monkeypatch.setattr(tsf, "_get_text_client", lambda cfg: _Down())
    cfg = {"provider": "ollama", "base_url": "http://down"}
    for _ in range(3):
        tr.configure_table_ai(cfg, {"enabled": True})
        assert tr._table_ai_client() is None
    assert len(pings) == 1
