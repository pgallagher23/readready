# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for gate_mcid_completeness — structure↔content MCID consistency.

Regression for false positives: the gate scanned every
xref with an /S key and counted /K integers, scooping up stray, unlinked
``<< /K n /Pg p /S /Artifact >>`` objects (artifacts, not structure content) whose
MCIDs are absent from the content stream. That inflated the structure-MCID count and
failed otherwise-clean docs. The gate must count only genuine structure elements
(skip /S /Artifact), while still catching real orphans on real structure elements.
"""

import os
import sys

import fitz

sys.path.insert(0, os.path.dirname(__file__))
from validation.gates import gate_mcid_completeness  # noqa: E402

# Content stream with two marked-content sequences: MCID 0 and MCID 1.
_CONTENT = (
    b"/P << /MCID 0 >> BDC\nBT /F0 12 Tf 72 720 Td (Hello) Tj ET\nEMC\n"
    b"/P << /MCID 1 >> BDC\nBT /F0 12 Tf 72 700 Td (World) Tj ET\nEMC\n"
)


def _build_pdf(tmp_path, *, extra_objs):
    """One page with content MCIDs {0,1}, a real /P struct elem per MCID, plus
    whatever ``extra_objs(doc, page_xref, doc_elem)`` appends (returns kid refs)."""
    pdf_path = str(tmp_path / "mcid.pdf")
    doc = fitz.open()
    page = doc.new_page()
    page_xref = page.xref

    # Set the page content stream so it carries /MCID 0 and /MCID 1.
    cont = doc.get_new_xref()
    doc.update_object(cont, "<< >>")
    doc.update_stream(cont, _CONTENT)
    doc.xref_set_key(page_xref, "Contents", f"{cont} 0 R")

    struct_root = doc.get_new_xref()
    doc_elem = doc.get_new_xref()

    p0 = doc.get_new_xref()
    doc.update_object(p0, f"<< /Type /StructElem /S /P /P {doc_elem} 0 R "
                          f"/K 0 /Pg {page_xref} 0 R >>")
    p1 = doc.get_new_xref()
    doc.update_object(p1, f"<< /Type /StructElem /S /P /P {doc_elem} 0 R "
                          f"/K 1 /Pg {page_xref} 0 R >>")
    kids = [f"{p0} 0 R", f"{p1} 0 R"]

    kids += extra_objs(doc, page_xref, doc_elem)

    doc.update_object(doc_elem, f"<< /Type /StructElem /S /Document "
                                f"/P {struct_root} 0 R /K [{' '.join(kids)}] >>")
    doc.update_object(struct_root, f"<< /Type /StructTreeRoot /K {doc_elem} 0 R >>")
    cat = doc.pdf_catalog()
    doc.xref_set_key(cat, "StructTreeRoot", f"{struct_root} 0 R")
    doc.xref_set_key(cat, "MarkInfo", "<< /Marked true >>")
    # garbage=0: keep unreferenced objects so stray artifact dicts persist
    # (in real docs they survive via the ParentTree); the gate scans all xrefs.
    doc.save(pdf_path)
    doc.close()
    return pdf_path


def test_clean_doc_passes(tmp_path):
    """Struct MCIDs {0,1} == content MCIDs {0,1} → pass."""
    pdf = _build_pdf(tmp_path, extra_objs=lambda d, pg, de: [])
    res = gate_mcid_completeness(pdf)
    assert res["passed"] is True, res["message"]


def test_stray_artifact_se_excluded(tmp_path):
    """A stray, unlinked << /K 198 /Pg /S /Artifact >> (MCID absent from content)
    must NOT count as a structure MCID → gate still passes."""
    def extra(doc, page_xref, doc_elem):
        # unlinked artifact dicts (no /P), MCIDs 198/203 not in content
        for mcid in (198, 203):
            a = doc.get_new_xref()
            doc.update_object(a, f"<< /K {mcid} /Pg {page_xref} 0 R /S /Artifact >>")
        return []  # not added to the Document /K — stray like the real cases
    pdf = _build_pdf(tmp_path, extra_objs=extra)
    res = gate_mcid_completeness(pdf)
    assert res["passed"] is True, res["message"]


def test_real_orphan_still_fails(tmp_path):
    """A genuine /StructElem whose /K MCID is absent from content is still caught."""
    def extra(doc, page_xref, doc_elem):
        refs = []
        for mcid in (198, 203):  # real /P structure, MCID not in content
            p = doc.get_new_xref()
            doc.update_object(p, f"<< /Type /StructElem /S /P /P {doc_elem} 0 R "
                                 f"/K {mcid} /Pg {page_xref} 0 R >>")
            refs.append(f"{p} 0 R")
        return refs
    pdf = _build_pdf(tmp_path, extra_objs=extra)
    res = gate_mcid_completeness(pdf)
    assert res["passed"] is False, res["message"]
