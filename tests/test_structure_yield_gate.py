# SPDX-License-Identifier: AGPL-3.0-or-later
"""gate_structure_yield: Docling produced implausibly little structure.

Takes the PRISTINE export-time counts (conversion stats), never a path to
docling_structure.json — Phase 1b/1c recovery mutates that file in place
before gates run, which would mask exactly the low-yield class this gate
exists to catch. Rule validated against 645 historical conversions
(fully_scanned + the >=80%-textless mixed extension): one hit — the
vector-scan textbook — zero false positives.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from validation.gates import gate_structure_yield, run_all_gates


def test_vector_scan_profile_fails():
    r = gate_structure_yield(33, 241, "fully_scanned")
    assert r["gate"] == "structure_yield"
    assert r["passed"] is False


def test_healthy_scan_passes():
    assert gate_structure_yield(120, 25, "fully_scanned")["passed"] is True


def test_mostly_textless_mixed_fails():
    """One texty page must not disarm the tripwire (cover-page escape)."""
    r = gate_structure_yield(34, 241, "mixed", docling_pages_with_text=1)
    assert r["passed"] is False


def test_balanced_mixed_not_applicable():
    assert gate_structure_yield(3, 20, "mixed", docling_pages_with_text=10)["passed"] is True


def test_missing_inputs_skip():
    assert gate_structure_yield(None, None, "fully_scanned")["passed"] is True
    assert gate_structure_yield(33, 241, None)["passed"] is True


def test_gate_absent_from_run_all_gates_without_inputs(tmp_path):
    """Legacy callers must not gain a permanently-passing extra gate."""
    import fitz
    p = tmp_path / "t.pdf"
    d = fitz.open(); d.new_page(); d.save(str(p))
    res = run_all_gates(str(p), str(p))
    assert "structure_yield" not in [g["gate"] for g in res["gates"]]
