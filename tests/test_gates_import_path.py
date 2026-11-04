# SPDX-License-Identifier: AGPL-3.0-or-later
import importlib

def test_product_module_exists():
    g = importlib.import_module("validation.gates")
    assert callable(g.quick_screen_reader_check)
    assert hasattr(g, "DEFAULT_SR_POOR_COVERAGE")

def test_runtime_sites_do_not_import_from_tests():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for rel in ("pipeline_core.py", "audit/reading_order_score.py"):
        assert "from tests.validation_gates" not in (root / rel).read_text(), rel
