# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Docker prefetch must initialise pipelines, not just construct them.

DocumentConverter() alone downloads nothing; models load on first use.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "prefetch_models", ROOT / "scripts" / "prefetch_models.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_prefetch_initialises_every_layout(monkeypatch):
    mod = _load()
    initialised = []

    class _Conv:
        def __init__(self, format_options):
            self.opts = format_options

        def initialize_pipeline(self, fmt):
            initialised.append(fmt)

    monkeypatch.setattr(mod, "DocumentConverter", _Conv)
    assert mod.main() == 0
    assert len(initialised) == len(mod.LAYOUT_SPECS) >= 2


def test_dockerfile_uses_the_script():
    text = (ROOT / "Dockerfile").read_text()
    assert "scripts/prefetch_models.py" in text


def test_dockerignore_keeps_the_prefetch_script_in_the_build_context():
    # `COPY . .` honours .dockerignore; a later negation must re-include the script.
    lines = [ln.strip() for ln in (ROOT / ".dockerignore").read_text().splitlines()]
    rules = [ln for ln in lines if ln and not ln.startswith("#")]
    negation = "!scripts/prefetch_models.py"
    assert negation in rules
    excluders = {"scripts/", "scripts", "scripts/*"}
    neg_at = rules.index(negation)
    for i, rule in enumerate(rules):
        if rule in excluders:
            assert i < neg_at, f"{rule!r} at rule {i} would exclude the script after the negation"
