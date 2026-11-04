# SPDX-License-Identifier: AGPL-3.0-or-later
"""bare_container_tree check: a tree with MCIDs but zero content-role SEs
(only Document/Part/Art/Sect/Div/NonStruct/Private) must raise an error.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from output_gen.struct_tree_validator import (
    _element_has_mcid,
    _resolve_role,
    validate_tagged_pdf,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def _bare_issues(pdf):
    return [i for i in validate_tagged_pdf(pdf)
            if i["check"] == "bare_container_tree"]


def test_resolve_role_follows_rolemap_chain():
    rm = {"Workbook": "Document", "MyPara": "P"}
    assert _resolve_role("Workbook", rm) == "Document"
    assert _resolve_role("MyPara", rm) == "P"
    assert _resolve_role("P", rm) == "P"


def test_resolve_role_cycle_safe():
    rm = {"A": "B", "B": "A"}
    assert _resolve_role("A", rm) in ("A", "B")  # terminates


# ======================================================================
# Tests for _element_has_mcid() precision guard
# ======================================================================

def test_element_has_mcid_explicit_mcid_dict():
    """Element with explicit /MCID in dict → True"""
    # /K [ << /Type /MCR /MCID 5 >> ]
    obj_str = "<< /S /P /K [ << /Type /MCR /MCID 5 >> ] >>"
    assert _element_has_mcid(obj_str) is True


def test_element_has_mcid_bare_integer_in_array():
    """Element with bare integer MCID in array → True"""
    # /K [ 0 ]
    obj_str = "<< /S /P /K [ 0 ] >>"
    assert _element_has_mcid(obj_str) is True


def test_element_has_mcid_indirect_refs_only():
    """Element with ONLY indirect refs to struct children, no MCID → False"""
    # /K [ 42 0 R 43 0 R ] — these are struct element refs, not MCIDs
    obj_str = "<< /S /Document /K [ 42 0 R 43 0 R ] >>"
    assert _element_has_mcid(obj_str) is False


def test_element_has_mcid_bare_integer_non_array():
    """Element with bare integer MCID (non-array form) → True"""
    # /K 5
    obj_str = "<< /S /P /K 5 >>"
    assert _element_has_mcid(obj_str) is True


def test_element_has_mcid_indirect_ref_non_array():
    """Element with indirect ref to struct child (non-array) → False"""
    # /K 5 0 R
    obj_str = "<< /S /Div /K 5 0 R >>"
    assert _element_has_mcid(obj_str) is False


def test_element_has_mcid_no_k():
    """Element with no /K attribute → False"""
    obj_str = "<< /S /Document >>"
    assert _element_has_mcid(obj_str) is False


def test_element_has_mcid_mixed_mcid_and_refs():
    """Element with BOTH bare MCIDs and indirect refs → True"""
    # /K [ 0 1 2 0 R 1 R ] — first 3 are MCIDs, last 2 are struct refs
    obj_str = "<< /S /P /K [ 0 1 2 0 R 1 R ] >>"
    assert _element_has_mcid(obj_str) is True


def test_grouping_tree_no_mcid_not_flagged(tmp_path):
    """Grouping-only tree with child struct elements but NO MCIDs must NOT flag.

    Regression test for reviewer finding 2026-08-07 fix round 1:
    the over-reporting has_mcr field caused false positives on
    Document > Div > Div structures (pure struct hierarchy, no marked content).
    The _element_has_mcid() guard now correctly distinguishes between
    indirect struct refs (N 0 R) and real MCIDs (bare integers).

    This test uses a fixture PDF created externally that has a structure tree:
    StructTreeRoot → Document → [Div, Div] where every /K array contains ONLY
    indirect struct-element references (N 0 R) and zero real MCIDs exist anywhere.
    The bare_container_tree check must NOT flag this tree because no marked
    content is present, even though the tree has multiple elements.
    """
    pytest.importorskip("pikepdf")
    import subprocess
    import struct

    # Create a minimal valid PDF with structure tree using raw PDF syntax
    # This is more robust than trying to construct it programmatically
    pdf_content = b"""%PDF-1.7
1 0 obj
<< /Type /Catalog /Pages 2 0 R /StructTreeRoot 3 0 R /MarkInfo << /Marked true >> >>
endobj

2 0 obj
<< /Type /Pages /Kids [4 0 R] /Count 1 >>
endobj

3 0 obj
<< /Type /StructTreeRoot /K [5 0 R] /ParentTree << /Nums [] >> >>
endobj

4 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 6 0 R >>
endobj

5 0 obj
<< /Type /StructElem /S /Document /P 3 0 R /K [7 0 R 8 0 R] >>
endobj

6 0 obj
<< /Length 0 >>
stream
endstream
endobj

7 0 obj
<< /Type /StructElem /S /Div /P 5 0 R /K [] >>
endobj

8 0 obj
<< /Type /StructElem /S /Div /P 5 0 R /K [] >>
endobj

xref
0 9
0000000000 65535 f
0000000009 00000 n
0000000107 00000 n
0000000174 00000 n
0000000261 00000 n
0000000363 00000 n
0000000463 00000 n
0000000522 00000 n
0000000600 00000 n
trailer
<< /Size 9 /Root 1 0 R >>
startxref
678
%%EOF
"""

    pdf_path = str(tmp_path / "grouping_tree_no_mcid.pdf")
    with open(pdf_path, "wb") as f:
        f.write(pdf_content)

    # Validate: should NOT flag bare_container_tree
    issues = validate_tagged_pdf(pdf_path)
    bare_issues = [i for i in issues if i["check"] == "bare_container_tree"]

    assert len(bare_issues) == 0, (
        f"Grouping-only tree with no real MCIDs should not flag bare_container_tree, "
        f"but got: {bare_issues}"
    )


# ======================================================================
# Tests for gate_bare_tree integration
# ======================================================================

from validation.gates import gate_bare_tree, run_all_gates  # noqa: E402


def test_gate_wired_into_run_all_gates():
    import inspect
    src = inspect.getsource(run_all_gates)
    assert "gate_bare_tree" in src


def test_gate_bare_tree_fails_on_missing_pdf():
    # The validator returns a "file-missing" issue rather than raising, so
    # a missing PDF must not silently pass the gate (M2).
    g = gate_bare_tree("/nonexistent/path/does-not-exist.pdf")
    assert g["gate"] == "bare_tree"
    assert g["passed"] is False
