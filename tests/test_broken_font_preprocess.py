# SPDX-License-Identifier: AGPL-3.0-or-later
"""Broken-font (.notdef) preprocessing must only replace the broken text.

The rewrite targets scans whose hidden text layer uses ``NotDefSpecial``.
Anything else on the page (vector graphics, images, text in good fonts) must
survive, and a page with several content streams must not be duplicated.
"""
import fitz
import pikepdf
import pytest

from output_gen import pdf_tagger

DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _make_pdf(path, broken_name="NotDefSpecial"):
    pdf = pikepdf.Pdf.new()
    fonts = pikepdf.Dictionary(
        F1=pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                              BaseFont=pikepdf.Name("/" + broken_name),
                              Encoding=pikepdf.Name.WinAnsiEncoding),
        F2=pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                              BaseFont=pikepdf.Name.Helvetica,
                              Encoding=pikepdf.Name.WinAnsiEncoding),
    )
    first = (b"q 0 0 1 rg 50 600 100 50 re f Q\n"
             b"BT /F1 12 Tf 72 700 Td (Hidden scan text) Tj ET\n"
             b"BT /F2 12 Tf 72 680 Td (Visible caption) Tj ET\n")
    second = b"q 1 0 0 rg 300 300 80 40 re f Q\n"
    page = pikepdf.Dictionary(
        Type=pikepdf.Name.Page, MediaBox=[0, 0, 612, 792],
        Resources=pikepdf.Dictionary(Font=fonts),
        Contents=pikepdf.Array([pdf.make_stream(first), pdf.make_stream(second)]),
    )
    pdf.pages.append(pikepdf.Page(page))
    pdf.save(path)


def _content(path):
    with pikepdf.open(path) as pdf:
        page = pdf.pages[0]
        return b"".join(
            pikepdf.unparse_content_stream(pikepdf.parse_content_stream(page))
            .splitlines(keepends=True))


@pytest.fixture
def needs_font():
    import os
    if not os.path.isfile(DEJAVU):
        pytest.skip("DejaVuSans not installed")


def test_only_broken_text_is_replaced(tmp_path, needs_font):
    src = tmp_path / "scan.pdf"
    _make_pdf(src)
    out = pdf_tagger._preprocess_broken_fonts(str(src), coverage_aware=False)
    assert out != str(src)

    content = _content(out)
    assert b"300 300 80 40 re" in content, "graphics after the text were dropped"
    assert b"50 600 100 50 re" in content
    assert b"/F2 12 Tf" in content and b"(Visible caption)" in content, \
        "text in a good font must keep its original font"
    assert b"(Hidden scan text)" not in content, "broken-font text must be replaced"

    with fitz.open(out) as doc:
        text = str(doc[0].get_text())
    assert text.count("Hidden scan text") == 1
    assert text.count("Visible caption") == 1


def test_fonts_merely_containing_notdef_are_left_alone(tmp_path, needs_font):
    src = tmp_path / "other.pdf"
    _make_pdf(src, broken_name="ShowNotDefGlyphs")
    assert pdf_tagger._preprocess_broken_fonts(str(src), coverage_aware=False) == str(src)


def test_subset_prefixed_name_is_recognised(tmp_path, needs_font):
    src = tmp_path / "subset.pdf"
    _make_pdf(src, broken_name="ABCDEF+NotDefSpecial")
    out = pdf_tagger._preprocess_broken_fonts(str(src), coverage_aware=False)
    assert out != str(src)
    assert b"(Hidden scan text)" not in _content(out)


def _font_dict(broken_name="NotDefSpecial"):
    return pikepdf.Dictionary(
        F1=pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                              BaseFont=pikepdf.Name("/" + broken_name),
                              Encoding=pikepdf.Name.WinAnsiEncoding),
    )


def test_blank_page_with_inherited_broken_font_does_not_abort(tmp_path, needs_font):
    """Fonts inherited from /Pages list on every page, including blank ones.

    A blank page has no content stream; preprocessing must skip it rather
    than raise (which made the whole document fall back to overlay mode).
    """
    src = tmp_path / "inherited.pdf"
    pdf = pikepdf.Pdf.new()
    text_page = pikepdf.Dictionary(
        Type=pikepdf.Name.Page, MediaBox=[0, 0, 612, 792],
        Contents=pdf.make_stream(b"BT /F1 12 Tf 72 700 Td (Hidden scan text) Tj ET\n"),
    )
    blank_page = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=[0, 0, 612, 792])
    pdf.pages.append(pikepdf.Page(text_page))
    pdf.pages.append(pikepdf.Page(blank_page))
    # Move the font resources up to the /Pages node so both pages inherit them.
    pdf.Root.Pages.Resources = pikepdf.Dictionary(Font=_font_dict())
    pdf.save(src)

    out = pdf_tagger._preprocess_broken_fonts(str(src), coverage_aware=False)

    assert out != str(src)
    with fitz.open(out) as doc:
        assert doc.page_count == 2
        assert "Hidden scan text" in str(doc[0].get_text())


def test_multiple_content_streams_join_at_a_token_boundary(tmp_path, needs_font):
    """PDF 7.7.3.3: streams are divided only at token boundaries, not by whitespace.

    Concatenating ``...Q`` and ``q...`` without a separator yields the bogus
    operator ``Qq``, which breaks save/restore tracking.
    """
    src = tmp_path / "split.pdf"
    pdf = pikepdf.Pdf.new()
    first = b"q 0 0 1 rg 50 600 100 50 re f Q"      # no trailing newline
    second = (b"q 1 0 0 rg 300 300 80 40 re f Q\n"
              b"BT /F1 12 Tf 72 700 Td (Hidden scan text) Tj ET\n")
    page = pikepdf.Dictionary(
        Type=pikepdf.Name.Page, MediaBox=[0, 0, 612, 792],
        Resources=pikepdf.Dictionary(Font=_font_dict()),
        Contents=pikepdf.Array([pdf.make_stream(first), pdf.make_stream(second)]),
    )
    pdf.pages.append(pikepdf.Page(page))
    pdf.save(src)

    out = pdf_tagger._preprocess_broken_fonts(str(src), coverage_aware=False)

    content = _content(out)
    assert b"Qq" not in content
    assert b"50 600 100 50 re" in content
    assert b"300 300 80 40 re" in content
    assert content.count(b"Q") == content.count(b"q"), "save/restore must stay balanced"
