# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mapping between Docling element types and PDF structure tag names.

Provides the translation layer between Docling's document model (TitleItem,
SectionHeaderItem, TextItem, etc.) and the standard PDF structure tags
defined in PDF 1.7 / ISO 32000-1 Table 333 (block-level) and Table 338
(inline-level).
"""

# Standard PDF structure tag names (PDF 1.7 §14.8.4)
# Block-level
TAG_DOCUMENT = "Document"
TAG_PART = "Part"
TAG_SECT = "Sect"
TAG_H = "H"
TAG_H1 = "H1"
TAG_H2 = "H2"
TAG_H3 = "H3"
TAG_H4 = "H4"
TAG_H5 = "H5"
TAG_H6 = "H6"
TAG_P = "P"
TAG_L = "L"
TAG_LI = "LI"
TAG_LBL = "Lbl"
TAG_LBODY = "LBody"
TAG_TABLE = "Table"
TAG_TR = "TR"
TAG_TH = "TH"
TAG_TD = "TD"
TAG_THEAD = "THead"
TAG_TBODY = "TBody"
TAG_CAPTION = "Caption"
TAG_FIGURE = "Figure"
TAG_FORMULA = "Formula"
TAG_BLOCKQUOTE = "BlockQuote"
TAG_TOC = "TOC"
TAG_TOCI = "TOCI"
TAG_INDEX = "Index"
TAG_DIV = "Div"
# Inline-level
TAG_SPAN = "Span"
TAG_LINK = "Link"
TAG_CODE = "Code"

# Heading level tags indexed by level number
HEADING_TAGS = {
    1: TAG_H1,
    2: TAG_H2,
    3: TAG_H3,
    4: TAG_H4,
    5: TAG_H5,
    6: TAG_H6,
}

# Docling element class name → default PDF tag
# SectionHeaderItem uses heading level to pick H1–H6 (see map_element_to_tag)
DOCLING_TO_PDF_TAG = {
    "TitleItem": TAG_H1,
    "SectionHeaderItem": TAG_H,      # resolved to H1–H6 by level
    "TextItem": TAG_P,
    "PictureItem": TAG_FIGURE,
    "TableItem": TAG_TABLE,
    "ListItem": TAG_LI,
    "CaptionItem": TAG_CAPTION,
    "FormulaItem": TAG_FORMULA,
    "CodeItem": TAG_CODE,
    "KeyValueItem": TAG_P,
    "FormItem": TAG_DIV,
    "GroupItem": TAG_PART,
    "ListGroup": TAG_L,
    "InlineGroup": TAG_SPAN,
    "DocItem": TAG_P,                 # generic fallback
}


def map_element_to_tag(element_type: str, level: int = None) -> str:
    """Map a Docling element type name to a PDF structure tag.

    Args:
        element_type: The class name of the Docling element (e.g.
            ``"SectionHeaderItem"``, ``"PictureItem"``).
        level: Heading level (1–6) for ``SectionHeaderItem``.  Ignored
            for other element types.

    Returns:
        A standard PDF structure tag name such as ``"H2"`` or ``"Figure"``.
    """
    if element_type == "SectionHeaderItem" and level is not None:
        return HEADING_TAGS.get(level, TAG_H)
    return DOCLING_TO_PDF_TAG.get(element_type, TAG_P)
