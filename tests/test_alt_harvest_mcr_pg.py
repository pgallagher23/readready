# SPDX-License-Identifier: AGPL-3.0-or-later
"""Author alt must survive when /Pg lives on the MCR dict (G-02).

Run: python -m pytest tests/test_alt_harvest_mcr_pg.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

import pikepdf

from converter.alt_harvest import apply_harvest, extract_source_alt


def _make_pdf_with_pg_on_mcr(path):
    """One page, one Figure whose /Pg is ONLY on the MCR dict inside /K."""
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    page.Contents = pdf.make_stream(b"/Figure <</MCID 0>> BDC EMC\n")
    mcr = pikepdf.Dictionary(Type=pikepdf.Name.MCR, Pg=page.obj, MCID=0)
    fig = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.StructElem, S=pikepdf.Name.Figure,
        Alt=pikepdf.String("State of Michigan Coat of Arms"),
        K=pikepdf.Array([mcr]),
    ))
    doc = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.StructElem, S=pikepdf.Name.Document,
        K=pikepdf.Array([fig]),
    ))
    pdf.Root.StructTreeRoot = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.StructTreeRoot, K=doc))
    pdf.save(path)


def test_pg_on_mcr_resolves_to_a_page(tmp_path):
    p = str(tmp_path / "mcr_pg.pdf")
    _make_pdf_with_pg_on_mcr(p)
    result = extract_source_alt(p)
    assert result["counts"]["no_page"] == 0, (
        "/Pg on the MCR dict inside /K must resolve the page "
        "(PDF 32000-1 14.7.4.3)"
    )
    assert result["counts"]["figures_total"] == 1
    # _page_geometry documents page_index as 1-BASED (enumerate(..., start=1)),
    # so the sole page of a one-page document is 1, never 0.
    assert result["figures"][0]["page_no"] == 1, "the figure must resolve to page 1"


def test_alt_existed_but_none_applied_is_reported_not_silent(tmp_path):
    """`skipped_reason` lives on apply_harvest(), NOT extract_source_alt().

    extract_source_alt returns {has_struct_tree, page_count, figures, formulas,
    counts, error} and never carries skipped_reason — asserting it there would
    pass vacuously.  The silent-failure guard belongs to apply_harvest, so the
    test must exercise that function.
    """
    p = str(tmp_path / "src.pdf")
    _make_pdf_with_pg_on_mcr(p)
    struct = tmp_path / "struct.json"
    # One page (matching the source, so the page-count guard does not fire) and
    # no elements to match against, so matching yields zero.
    struct.write_text(json.dumps(
        {"pages": {"1": {"width": 612, "height": 792}}, "elements": []}
    ))
    stats = apply_harvest(str(struct), p)
    assert stats["source_figures_with_alt"] == 1
    assert stats["matched_figures"] == 0
    assert stats["skipped_reason"] == "no_alt_applied", (
        "author alt existed and none of it was applied — leaving skipped_reason "
        "None is the production incident's exact signature"
    )
