# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``converter/alt_harvest.py`` — recovering author-written ``/Alt``
from a source PDF's structure tree.

The pipeline hands the input PDF to Docling, which reconstructs structure from
visual layout only and never opens ``/StructTreeRoot``.  Authored ``/Alt`` was
therefore discarded at ingestion and replaced by VLM output — or, when AI was
disabled, by the literal placeholder ``"Figure"``.  These tests pin the harvest
that recovers it.

Fixtures are built in-memory with ``pikepdf.Pdf.new()``; no binaries are
committed.

Run: python -m pytest tests/test_alt_harvest.py -v
"""

import json
import os
import sys

import pikepdf
import pytest
from pikepdf import Array, Dictionary, Name

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from converter.alt_harvest import (  # noqa: E402
    extract_source_alt,
    is_generic_alt,
    match_harvest_to_elements,
    apply_harvest,
)


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------

def build_tagged_pdf(
    path,
    figures,
    *,
    num_pages=1,
    rolemap=None,
    rotation=0,
    mediabox=(0, 0, 612, 792),
    struct_tree=True,
    nest_under_sect=None,
):
    """Write a minimal tagged PDF and return its path.

    ``figures`` is a list of dicts::

        {"page": 1, "alt": "...", "bbox": (l, b, r, t), "tag": "/Figure",
         "actualtext": "...", "attr_owner": "/Layout", "attr_style": "dict"}

    ``attr_style`` selects how ``/A`` is written: ``"dict"`` (a bare
    dictionary), ``"array"`` (an array of dictionaries), or ``"indirect"``.
    ``page`` may be None to omit ``/Pg`` entirely — with ``nest_under_sect``
    the page is then inherited from the parent Sect.
    """
    pdf = pikepdf.Pdf.new()
    for _ in range(num_pages):
        page = pdf.add_blank_page(page_size=(mediabox[2] - mediabox[0],
                                             mediabox[3] - mediabox[1]))
        page.obj["/MediaBox"] = Array([*mediabox])
        if rotation:
            page.obj["/Rotate"] = rotation

    if not struct_tree:
        pdf.save(str(path))
        return str(path)

    struct_root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    if rolemap:
        role_map = Dictionary()
        for src_tag, dst_tag in rolemap.items():
            role_map[src_tag] = Name(dst_tag)
        struct_root["/RoleMap"] = role_map

    doc_elem = pdf.make_indirect(
        Dictionary(Type=Name.StructElem, S=Name("/Document"), P=struct_root,
                   K=Array([]))
    )

    parent = doc_elem
    if nest_under_sect:
        sect = pdf.make_indirect(
            Dictionary(Type=Name.StructElem, S=Name("/Sect"), P=doc_elem,
                       K=Array([]))
        )
        # The Sect carries /Pg; children below inherit it.
        sect["/Pg"] = pdf.pages[nest_under_sect - 1].obj
        doc_elem["/K"].append(sect)
        parent = sect

    for idx, fig in enumerate(figures):
        elem = pdf.make_indirect(
            Dictionary(Type=Name.StructElem,
                       S=Name(fig.get("tag", "/Figure")),
                       P=parent,
                       K=idx)
        )
        if fig.get("page") is not None:
            elem["/Pg"] = pdf.pages[fig["page"] - 1].obj
        if fig.get("alt") is not None:
            elem["/Alt"] = pikepdf.String(fig["alt"])
        if fig.get("actualtext") is not None:
            elem["/ActualText"] = pikepdf.String(fig["actualtext"])
        if fig.get("bbox") is not None:
            attr = Dictionary(BBox=Array([float(v) for v in fig["bbox"]]))
            owner = fig.get("attr_owner", "/Layout")
            if owner:
                attr["/O"] = Name(owner)
            style = fig.get("attr_style", "dict")
            if style == "array":
                elem["/A"] = Array([attr])
            elif style == "indirect":
                elem["/A"] = pdf.make_indirect(attr)
            else:
                elem["/A"] = attr
        elif fig.get("attr_owner_no_bbox"):
            elem["/A"] = Dictionary(O=Name(fig["attr_owner_no_bbox"]))
        parent["/K"].append(elem)

    struct_root["/K"] = Array([doc_elem])
    pdf.Root["/StructTreeRoot"] = struct_root
    pdf.Root["/MarkInfo"] = Dictionary(Marked=True)
    pdf.save(str(path))
    return str(path)


# ---------------------------------------------------------------------------
# is_generic_alt
# ---------------------------------------------------------------------------

class TestIsGenericAlt:
    @pytest.mark.parametrize("alt", ["image", "Photo", "  chart  ", "figure",
                                     "logo", "screenshot", ""])
    def test_rejects_generic_labels(self, alt):
        assert is_generic_alt(alt) is True

    @pytest.mark.parametrize("alt", [
        "Figure",                          # our own content-preserving placeholder
        "Formula",
        "Figure on page 5",                # overlay tagger placeholder
        "Figure: figure 3 (page 5)",
        "Unrecognized formula (page 12)",
    ])
    def test_rejects_our_own_placeholders(self, alt):
        """Users re-upload our output; without this the fix defeats itself."""
        assert is_generic_alt(alt) is True

    @pytest.mark.parametrize("alt", [
        "A screenshot of a Twitter post from Michael Duffy @duffylibrarian.",
        "This pie chart illustrates the same data given in the table in figure 4.",
        "Poetry month logo that reads 'Example Campus Press Celebrate National Poetry Month.'",
    ])
    def test_keeps_real_descriptions(self, alt):
        assert is_generic_alt(alt) is False

    def test_rejects_bare_filename(self):
        assert is_generic_alt("figure_3.png") is True

    @pytest.mark.parametrize("alt", [
        "Picture 8", "Text Box 9", "Rectangle 28", "Line 29", "Group 5",
        "Content Placeholder 4", "Straight Connector 3", "TextBox 12",
        "Picture_39", "Oval 2",
    ])
    def test_rejects_office_shape_names(self, alt):
        """PowerPoint/Word object names, not descriptions — common in real-world alt."""
        assert is_generic_alt(alt) is True

    @pytest.mark.parametrize("alt", [
        "_DSC1902", "P2TB4#y1", "P437TB1#y1", "P549TB38bA#y1",
    ])
    def test_rejects_opaque_codes(self, alt):
        """Camera filenames and PDF-tool artifacts — common in real-world alt."""
        assert is_generic_alt(alt) is True

    def test_strips_office_boilerplate_suffix(self, tmp_path):
        """Keep the author's description, drop the tool's signature."""
        p = build_tagged_pdf(tmp_path / "msauto.pdf", [
            {"page": 1, "bbox": (10, 10, 110, 110),
             "alt": "A close up of a sign  Description automatically generated"},
        ])
        figs = extract_source_alt(p)["figures"]
        assert figs[0]["alt"] == "A close up of a sign"

    def test_strips_boilerplate_with_confidence_qualifier(self, tmp_path):
        p = build_tagged_pdf(tmp_path / "msauto2.pdf", [
            {"page": 1, "bbox": (10, 10, 110, 110),
             "alt": "A yellow and black logo\n\nDescription automatically "
                    "generated with medium confidence"},
        ])
        figs = extract_source_alt(p)["figures"]
        assert figs[0]["alt"] == "A yellow and black logo"

    def test_boilerplate_only_alt_is_rejected(self, tmp_path):
        p = build_tagged_pdf(tmp_path / "msauto3.pdf", [
            {"page": 1, "bbox": (10, 10, 110, 110),
             "alt": "Description automatically generated"},
        ])
        assert extract_source_alt(p)["figures"] == []

    @pytest.mark.parametrize("alt", [
        "Skull", "Books", "PDF Association logo", "Logo: allyant",
        "Street map marked with locations of pumps and cholera cases.",
    ])
    def test_keeps_short_but_genuine_alt(self, alt):
        assert is_generic_alt(alt) is False


# ---------------------------------------------------------------------------
# extract_source_alt
# ---------------------------------------------------------------------------

class TestExtractSourceAlt:
    def test_untagged_pdf_reports_no_struct_tree(self, tmp_path):
        p = build_tagged_pdf(tmp_path / "untagged.pdf", [], struct_tree=False)
        result = extract_source_alt(p)
        assert result["has_struct_tree"] is False
        assert result["figures"] == []

    def test_extracts_alt_with_page_and_bbox(self, tmp_path):
        p = build_tagged_pdf(tmp_path / "one.pdf", [
            {"page": 1, "alt": "A bar chart of survey responses.",
             "bbox": (72, 300, 540, 500)},
        ])
        figs = extract_source_alt(p)["figures"]
        assert len(figs) == 1
        assert figs[0]["alt"] == "A bar chart of survey responses."
        assert figs[0]["page_no"] == 1
        assert figs[0]["bbox"] == {"l": 72.0, "b": 300.0, "r": 540.0, "t": 500.0}
        assert figs[0]["source"] == "alt"

    def test_page_number_is_one_based_across_pages(self, tmp_path):
        p = build_tagged_pdf(tmp_path / "multi.pdf", [
            {"page": 3, "alt": "Third page figure.", "bbox": (10, 10, 100, 100)},
        ], num_pages=4)
        assert extract_source_alt(p)["figures"][0]["page_no"] == 3

    def test_generic_alt_is_rejected(self, tmp_path):
        p = build_tagged_pdf(tmp_path / "generic.pdf", [
            {"page": 1, "alt": "image", "bbox": (10, 10, 100, 100)},
        ])
        result = extract_source_alt(p)
        assert result["figures"] == []
        assert result["counts"]["figures_generic"] == 1

    def test_our_own_placeholder_is_rejected(self, tmp_path):
        p = build_tagged_pdf(tmp_path / "reprocessed.pdf", [
            {"page": 1, "alt": "Figure", "bbox": (10, 10, 100, 100)},
        ])
        result = extract_source_alt(p)
        assert result["figures"] == []
        assert result["counts"]["figures_placeholder"] == 1

    def test_rolemap_diagram_resolves_to_figure(self, tmp_path):
        p = build_tagged_pdf(tmp_path / "rolemap.pdf", [
            {"page": 1, "tag": "/Diagram", "alt": "An architecture diagram.",
             "bbox": (10, 10, 100, 100)},
        ], rolemap={"/Diagram": "/Figure"})
        figs = extract_source_alt(p)["figures"]
        assert len(figs) == 1
        assert figs[0]["raw_tag"] == "/Diagram"

    def test_rolemap_cycle_terminates(self, tmp_path):
        p = build_tagged_pdf(tmp_path / "cycle.pdf", [
            {"page": 1, "tag": "/Foo", "alt": "Cyclic role.",
             "bbox": (10, 10, 100, 100)},
        ], rolemap={"/Foo": "/Bar", "/Bar": "/Foo"})
        result = extract_source_alt(p)  # must not hang or recurse forever
        assert result["has_struct_tree"] is True

    @pytest.mark.parametrize("style", ["dict", "array", "indirect"])
    def test_attribute_dict_array_and_indirect_give_same_bbox(self, tmp_path, style):
        p = build_tagged_pdf(tmp_path / f"attr_{style}.pdf", [
            {"page": 1, "alt": "Same box either way.", "bbox": (72, 100, 300, 400),
             "attr_style": style},
        ])
        figs = extract_source_alt(p)["figures"]
        assert figs[0]["bbox"] == {"l": 72.0, "b": 100.0, "r": 300.0, "t": 400.0}

    def test_non_layout_attribute_without_bbox_is_bbox_less(self, tmp_path):
        p = build_tagged_pdf(tmp_path / "table_attr.pdf", [
            {"page": 1, "alt": "Has alt but no layout box.",
             "attr_owner_no_bbox": "/Table"},
        ])
        figs = extract_source_alt(p)["figures"]
        assert len(figs) == 1
        assert figs[0]["bbox"] is None

    def test_page_inherited_from_ancestor(self, tmp_path):
        p = build_tagged_pdf(tmp_path / "inherit.pdf", [
            {"page": None, "alt": "Inherits its page from the Sect.",
             "bbox": (10, 10, 100, 100)},
        ], num_pages=3, nest_under_sect=2)
        assert extract_source_alt(p)["figures"][0]["page_no"] == 2

    def test_formula_falls_back_to_actualtext(self, tmp_path):
        p = build_tagged_pdf(tmp_path / "formula.pdf", [
            {"page": 1, "tag": "/Formula", "actualtext": "E = mc^2",
             "bbox": (10, 10, 100, 100)},
        ])
        formulas = extract_source_alt(p)["formulas"]
        assert len(formulas) == 1
        assert formulas[0]["alt"] == "E = mc^2"
        assert formulas[0]["source"] == "actualtext"

    def test_figure_does_not_fall_back_to_actualtext(self, tmp_path):
        """Figure /ActualText is the glyph run under the image, not a description."""
        p = build_tagged_pdf(tmp_path / "fig_at.pdf", [
            {"page": 1, "actualtext": "46.92% 53.85% 6.92%",
             "bbox": (10, 10, 100, 100)},
        ])
        assert extract_source_alt(p)["figures"] == []

    def test_non_zero_mediabox_origin_is_normalized(self, tmp_path):
        p = build_tagged_pdf(tmp_path / "origin.pdf", [
            {"page": 1, "alt": "Offset page.", "bbox": (110, 150, 210, 250)},
        ], mediabox=(100, 100, 712, 892))
        figs = extract_source_alt(p)["figures"]
        assert figs[0]["bbox"] == {"l": 10.0, "b": 50.0, "r": 110.0, "t": 150.0}

    def test_degenerate_bbox_is_dropped(self, tmp_path):
        p = build_tagged_pdf(tmp_path / "sliver.pdf", [
            {"page": 1, "alt": "Zero-height box.", "bbox": (72, 300, 540, 300.4)},
        ])
        assert extract_source_alt(p)["figures"][0]["bbox"] is None

    def test_reversed_bbox_coordinates_are_normalized(self, tmp_path):
        p = build_tagged_pdf(tmp_path / "reversed.pdf", [
            {"page": 1, "alt": "Backwards box.", "bbox": (540, 500, 72, 300)},
        ])
        assert extract_source_alt(p)["figures"][0]["bbox"] == {
            "l": 72.0, "b": 300.0, "r": 540.0, "t": 500.0}

    def test_rotated_page_bbox_lands_in_display_space(self, tmp_path):
        """Docling bboxes are post-rotation; /A /BBox is pre-rotation."""
        p = build_tagged_pdf(tmp_path / "rot90.pdf", [
            {"page": 1, "alt": "Rotated figure.", "bbox": (72, 100, 172, 300)},
        ], rotation=90)
        bbox = extract_source_alt(p)["figures"][0]["bbox"]
        # 90deg: (x, y) -> (y, raw_w - x); raw page is 612x792.
        assert bbox == {"l": 100.0, "b": 440.0, "r": 300.0, "t": 540.0}

    def test_counts_reflect_totals(self, tmp_path):
        p = build_tagged_pdf(tmp_path / "counts.pdf", [
            {"page": 1, "alt": "A real description of a chart.",
             "bbox": (10, 10, 100, 100)},
            {"page": 1, "alt": "image", "bbox": (10, 110, 100, 200)},
            {"page": 1, "bbox": (10, 210, 100, 300)},
        ])
        counts = extract_source_alt(p)["counts"]
        assert counts["figures_total"] == 3
        assert counts["figures_with_alt"] == 1
        assert counts["figures_generic"] == 1


# ---------------------------------------------------------------------------
# match_harvest_to_elements — pure geometry, no PDF I/O
# ---------------------------------------------------------------------------

def src(page, alt="A genuine description of the figure.", bbox=(10, 10, 110, 110),
        order=1, page_order=1):
    return {"alt": alt, "source": "alt", "page_no": page,
            "bbox": None if bbox is None else
                    {"l": bbox[0], "b": bbox[1], "r": bbox[2], "t": bbox[3]},
            "tag": "/Figure", "raw_tag": "/Figure",
            "order": order, "page_order": page_order}


def elem(eid, page, bbox=(10, 10, 110, 110), *, alt_text=None,
         image_ref="/out/images/figure_1.png", tag="Figure", reading_order=1):
    return {"id": eid, "tag": tag, "type": "PictureItem", "page_no": page,
            "bbox": None if bbox is None else
                    {"l": bbox[0], "b": bbox[1], "r": bbox[2], "t": bbox[3]},
            "alt_text": alt_text, "image_ref": image_ref,
            "reading_order": reading_order}


class TestMatching:
    def test_single_figure_on_a_page_is_matched(self):
        result = match_harvest_to_elements(
            [src(1)], [elem("elem_0001", 1)], "Figure")
        assert len(result["matches"]) == 1
        assert result["matches"][0]["elem_id"] == "elem_0001"

    def test_never_matches_across_pages(self):
        result = match_harvest_to_elements(
            [src(1)], [elem("elem_0001", 2)], "Figure")
        assert result["matches"] == []
        assert len(result["unmatched_source"]) == 1

    def test_sixteen_source_against_twelve_targets(self):
        """The reported document: 16 source figures, 12 Docling figures."""
        sources = [src(p, bbox=(10, 10, 110, 110), order=p, page_order=1)
                   for p in range(1, 17)]
        targets = [elem(f"elem_{p:04d}", p, bbox=(12, 12, 108, 108))
                   for p in range(1, 13)]
        result = match_harvest_to_elements(sources, targets, "Figure")
        assert len(result["matches"]) == 12
        assert len(result["unmatched_source"]) == 4
        assert len({m["elem_id"] for m in result["matches"]}) == 12

    def test_geometry_beats_document_order(self):
        """Two figures on one page, listed in reversed order in the source."""
        top = src(1, alt="The upper photograph of a choir.",
                  bbox=(10, 500, 110, 600), order=1, page_order=1)
        bottom = src(1, alt="The lower bar chart of responses.",
                     bbox=(10, 100, 110, 200), order=2, page_order=2)
        t_bottom = elem("elem_low", 1, bbox=(10, 100, 110, 200), reading_order=1)
        t_top = elem("elem_high", 1, bbox=(10, 500, 110, 600), reading_order=2)

        result = match_harvest_to_elements([top, bottom], [t_bottom, t_top],
                                           "Figure")
        by_id = {m["elem_id"]: m["alt"] for m in result["matches"]}
        assert by_id["elem_high"] == "The upper photograph of a choir."
        assert by_id["elem_low"] == "The lower bar chart of responses."

    def test_containment_matches_when_author_box_spans_the_caption(self):
        """Author /BBox often covers figure + caption; Docling's covers the image."""
        wide = src(1, bbox=(72, 82, 540, 698))
        image_only = elem("elem_0001", 1, bbox=(200, 400, 400, 690))
        result = match_harvest_to_elements([wide], [image_only], "Figure")
        assert len(result["matches"]) == 1
        assert result["matches"][0]["method"] == "geometry"

    def test_disjoint_boxes_do_not_match(self):
        far = src(1, bbox=(10, 10, 60, 60))
        other = elem("elem_0001", 1, bbox=(400, 700, 500, 780))
        result = match_harvest_to_elements([far], [other], "Figure",
                                           ordinal_fallback=False)
        assert result["matches"] == []

    def test_ordinal_fallback_fires_for_lone_bbox_less_pair(self):
        result = match_harvest_to_elements(
            [src(1, bbox=None)], [elem("elem_0001", 1)], "Figure")
        assert len(result["matches"]) == 1
        assert result["matches"][0]["method"] == "ordinal"

    def test_ordinal_fallback_does_not_guess_between_two_targets(self):
        targets = [elem("elem_0001", 1, bbox=(10, 10, 110, 110)),
                   elem("elem_0002", 1, bbox=(10, 500, 110, 600))]
        result = match_harvest_to_elements([src(1, bbox=None)], targets, "Figure")
        assert result["matches"] == []

    def test_geometry_rejection_is_not_overridden_by_ordinal(self):
        """A source that HAS a bbox and failed geometry must stay unmatched."""
        far = src(1, bbox=(10, 10, 60, 60))
        other = elem("elem_0001", 1, bbox=(400, 700, 500, 780))
        result = match_harvest_to_elements([far], [other], "Figure",
                                           ordinal_fallback=True)
        assert result["matches"] == []

    def test_bbox_less_sources_pair_by_order_when_counts_match(self):
        sources = [src(1, alt="First figure description.", bbox=None,
                       order=1, page_order=1),
                   src(1, alt="Second figure description.", bbox=None,
                       order=2, page_order=2)]
        targets = [elem("elem_a", 1, bbox=None, reading_order=1),
                   elem("elem_b", 1, bbox=None, reading_order=2)]
        result = match_harvest_to_elements(sources, targets, "Figure")
        by_id = {m["elem_id"]: m["alt"] for m in result["matches"]}
        assert by_id == {"elem_a": "First figure description.",
                         "elem_b": "Second figure description."}

    def test_element_with_existing_alt_is_never_overwritten(self):
        target = elem("elem_0001", 1, alt_text="Numerical data table: 1 2 3")
        result = match_harvest_to_elements([src(1)], [target], "Figure")
        assert result["matches"] == []

    def test_figure_without_image_ref_is_excluded(self):
        """Columnar-text tables reclassified as Figure carry no image."""
        target = elem("elem_0001", 1, image_ref=None)
        result = match_harvest_to_elements([src(1)], [target], "Figure")
        assert result["matches"] == []

    def test_formula_candidates_do_not_require_an_image_ref(self):
        target = elem("elem_0001", 1, image_ref=None, tag="Formula")
        result = match_harvest_to_elements([src(1)], [target], "Formula")
        assert len(result["matches"]) == 1

    def test_wrong_tag_is_excluded(self):
        target = elem("elem_0001", 1, tag="Table")
        result = match_harvest_to_elements([src(1)], [target], "Figure")
        assert result["matches"] == []

    def test_result_is_deterministic_under_ties(self):
        sources = [src(1, alt="Alpha description here.", order=1, page_order=1),
                   src(1, alt="Bravo description here.", order=2, page_order=2)]
        targets = [elem("elem_a", 1, reading_order=1),
                   elem("elem_b", 1, reading_order=2)]
        first = match_harvest_to_elements(sources, targets, "Figure")
        second = match_harvest_to_elements(sources, targets, "Figure")
        assert first["matches"] == second["matches"]


# ---------------------------------------------------------------------------
# apply_harvest — writes struct JSON on disk and the extracted HTML
# ---------------------------------------------------------------------------

def write_struct(tmp_path, elements, num_pages=1, name="docling_structure.json"):
    path = tmp_path / name
    pages = {str(i): {"width": 612.0, "height": 792.0}
             for i in range(1, num_pages + 1)}
    path.write_text(json.dumps(
        {"metadata": {"num_pages": num_pages}, "pages": pages,
         "elements": elements},
        indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_html(tmp_path, body, name="extracted.html"):
    path = tmp_path / name
    path.write_text(f"<html><body>{body}</body></html>", encoding="utf-8")
    return path


ALT_ONE = "A horizontal bar chart of survey responses by category."


class TestApplyHarvest:
    def test_writes_alt_into_struct_json_on_disk(self, tmp_path):
        pdf = build_tagged_pdf(tmp_path / "src.pdf", [
            {"page": 1, "alt": ALT_ONE, "bbox": (10, 10, 110, 110)}])
        struct = write_struct(tmp_path, [elem("elem_0001", 1)])

        stats = apply_harvest(str(struct), pdf)

        assert stats["matched_figures"] == 1
        reloaded = json.loads(struct.read_text(encoding="utf-8"))
        assert reloaded["elements"][0]["alt_text"] == ALT_ONE
        assert reloaded["elements"][0]["alt_source"] == "source_pdf"

    def test_sets_html_img_alt_by_basename(self, tmp_path):
        pdf = build_tagged_pdf(tmp_path / "src.pdf", [
            {"page": 1, "alt": ALT_ONE, "bbox": (10, 10, 110, 110)}])
        struct = write_struct(tmp_path, [
            elem("elem_0001", 1, image_ref="/out/images/figure_1.png")])
        html = write_html(tmp_path, '<img src="../images/figure_1.png" alt="">')

        stats = apply_harvest(str(struct), pdf, html_path=str(html))

        assert stats["html_imgs_updated"] == 1
        assert f'alt="{ALT_ONE}"' in html.read_text(encoding="utf-8")

    def test_does_not_overwrite_real_html_alt(self, tmp_path):
        pdf = build_tagged_pdf(tmp_path / "src.pdf", [
            {"page": 1, "alt": ALT_ONE, "bbox": (10, 10, 110, 110)}])
        struct = write_struct(tmp_path, [elem("elem_0001", 1)])
        html = write_html(
            tmp_path,
            '<img src="../images/figure_1.png" alt="An editor wrote this.">')

        apply_harvest(str(struct), pdf, html_path=str(html))

        assert "An editor wrote this." in html.read_text(encoding="utf-8")

    def test_sets_formula_aria_label(self, tmp_path):
        pdf = build_tagged_pdf(tmp_path / "src.pdf", [
            {"page": 1, "tag": "/Formula", "alt": "The quadratic formula.",
             "bbox": (10, 10, 110, 110)}])
        struct = write_struct(tmp_path, [
            {"id": "elem_0001", "tag": "Formula", "type": "FormulaItem",
             "page_no": 1, "bbox": {"l": 10, "b": 10, "r": 110, "t": 110},
             "alt_text": None, "image_ref": None, "reading_order": 1}])
        html = write_html(
            tmp_path,
            '<div class="formula-not-decoded" data-formula-index="0"'
            ' role="math" aria-label=""></div>')

        stats = apply_harvest(str(struct), pdf, html_path=str(html))

        assert stats["matched_formulas"] == 1
        assert 'aria-label="The quadratic formula."' in html.read_text(
            encoding="utf-8")

    def test_is_idempotent(self, tmp_path):
        pdf = build_tagged_pdf(tmp_path / "src.pdf", [
            {"page": 1, "alt": ALT_ONE, "bbox": (10, 10, 110, 110)}])
        struct = write_struct(tmp_path, [elem("elem_0001", 1)])

        apply_harvest(str(struct), pdf)
        after_first = struct.read_text(encoding="utf-8")
        second = apply_harvest(str(struct), pdf)

        assert second["matched_figures"] == 0
        assert struct.read_text(encoding="utf-8") == after_first

    def test_page_count_mismatch_aborts_without_mutating(self, tmp_path):
        pdf = build_tagged_pdf(tmp_path / "src.pdf", [
            {"page": 1, "alt": ALT_ONE, "bbox": (10, 10, 110, 110)}],
            num_pages=2)
        struct = write_struct(tmp_path, [elem("elem_0001", 1)], num_pages=1)
        before = struct.read_text(encoding="utf-8")

        stats = apply_harvest(str(struct), pdf)

        assert stats["skipped_reason"] == "page_count_mismatch"
        assert stats["matched_figures"] == 0
        assert struct.read_text(encoding="utf-8") == before

    def test_untagged_source_is_a_no_op(self, tmp_path):
        pdf = build_tagged_pdf(tmp_path / "src.pdf", [], struct_tree=False)
        struct = write_struct(tmp_path, [elem("elem_0001", 1)])
        before = struct.read_text(encoding="utf-8")

        stats = apply_harvest(str(struct), pdf)

        assert stats["skipped_reason"] == "no_struct_tree"
        assert struct.read_text(encoding="utf-8") == before

    def test_non_ascii_alt_round_trips(self, tmp_path):
        alt = "Un diagramme à barres montrant les réponses — 46,92 %."
        pdf = build_tagged_pdf(tmp_path / "src.pdf", [
            {"page": 1, "alt": alt, "bbox": (10, 10, 110, 110)}])
        struct = write_struct(tmp_path, [elem("elem_0001", 1)])

        apply_harvest(str(struct), pdf)

        reloaded = json.loads(struct.read_text(encoding="utf-8"))
        assert reloaded["elements"][0]["alt_text"] == alt

    def test_reports_unmatched_source_alt(self, tmp_path):
        pdf = build_tagged_pdf(tmp_path / "src.pdf", [
            {"page": 1, "alt": ALT_ONE, "bbox": (10, 10, 110, 110)},
            {"page": 2, "alt": "A second figure nobody kept.",
             "bbox": (10, 10, 110, 110)}], num_pages=2)
        struct = write_struct(tmp_path, [elem("elem_0001", 1)], num_pages=2)

        stats = apply_harvest(str(struct), pdf)

        assert stats["matched_figures"] == 1
        assert len(stats["unmatched_source_alt"]) == 1
        assert stats["unmatched_source_alt"][0]["page_no"] == 2
