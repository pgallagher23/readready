# SPDX-License-Identifier: AGPL-3.0-or-later
"""The public build has no PII scanner and no "Disable AI" switch."""
import importlib.util
import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_pii_scanner_module_is_gone():
    assert importlib.util.find_spec("utils.pii_scanner") is None


def test_process_batch_has_no_disable_ai_parameter():
    import app
    assert "disable_ai" not in inspect.signature(app.process_batch).parameters


def test_no_pii_or_disable_ai_references_remain():
    pattern = re.compile(r"\bpii\b|pii_|disable_ai|Disable AI|institution_id", re.I)
    offenders = []
    for rel in ("app.py", "pipeline_core.py", "main.py", "config.yaml",
                "utils/config_schema.py", "logging_util/failure_taxonomy.py", "README.md"):
        for i, line in enumerate((ROOT / rel).read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()[:80]}")
    assert not offenders, "\n".join(offenders)


def test_old_config_with_pii_section_only_warns():
    from utils.config_schema import validate_config
    import yaml
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    cfg["pii_detection"] = {"enabled": True}
    warnings = validate_config(cfg)
    assert any("pii_detection" in w for w in warnings)
