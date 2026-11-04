# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render remediated HTML as a tagged, accessible PDF via WeasyPrint.

Produces a PDF with a full structure tree (H1-H6, P, Table, TH, TD, Link,
Figure, etc.) suitable for screen readers.  Uses ``write_pdf(pdf_tags=True)``
so WeasyPrint automatically adds StructTreeRoot, MarkInfo.Marked,
ViewerPreferences.DisplayDocTitle, and /Lang from ``<html lang="...">``.
"""

import logging
from pathlib import Path

from bs4 import BeautifulSoup
from weasyprint import HTML

logger = logging.getLogger(__name__)

# Print stylesheet injected before rendering.  Keeps the PDF clean and
# readable while preserving the semantic structure that drives tagging.
_PRINT_CSS = """\
@page {
    size: A4;
    margin: 2cm;
}
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.5;
    color: #1a1a1a;
}
h1 { font-size: 22pt; margin: 0.8em 0 0.4em; }
h2 { font-size: 18pt; margin: 0.7em 0 0.35em; }
h3 { font-size: 15pt; margin: 0.6em 0 0.3em; }
h4 { font-size: 13pt; margin: 0.5em 0 0.25em; }
h5 { font-size: 12pt; margin: 0.4em 0 0.2em; }
h6 { font-size: 11pt; margin: 0.4em 0 0.2em; font-style: italic; }
p  { margin: 0.4em 0; }
img {
    max-width: 100%;
    height: auto;
}
figure {
    margin: 1em 0;
    page-break-inside: avoid;
}
figcaption {
    font-size: 10pt;
    color: #494949;
    margin-top: 0.3em;
}
table {
    width: 100%;
    border-collapse: collapse;
    margin: 0.8em 0;
    page-break-inside: avoid;
}
th, td {
    border: 1px solid #999;
    padding: 4pt 6pt;
    text-align: left;
    font-size: 10pt;
}
th {
    background: #f0f0f0;
    font-weight: bold;
}
caption {
    font-size: 10pt;
    font-weight: bold;
    margin-bottom: 0.3em;
    text-align: left;
}
a {
    color: #1a5276;
    text-decoration: underline;
}
ul, ol {
    margin: 0.4em 0 0.4em 1.5em;
}
li {
    margin: 0.15em 0;
}
/* Avoid widows/orphans and bad breaks */
h1, h2, h3, h4, h5, h6 {
    page-break-after: avoid;
}
tr {
    page-break-inside: avoid;
}
"""


def render_html_as_pdf(
    html_path: str,
    output_path: str,
    language: str = "en",
) -> str:
    """Render remediated HTML as a tagged PDF.

    Args:
        html_path: Path to the remediated HTML file.
        output_path: Destination path for the PDF.
        language: ISO 639-1 language code (used if ``<html lang>`` is missing).

    Returns:
        The *output_path* on success.

    Raises:
        RuntimeError: If WeasyPrint rendering fails.
    """
    html_file = Path(html_path)
    if not html_file.exists():
        raise FileNotFoundError(f"HTML file not found: {html_path}")

    soup = BeautifulSoup(html_file.read_text(encoding="utf-8"), "html.parser")

    # Ensure <html lang="..."> is present (drives /Lang in the PDF catalog)
    html_tag = soup.find("html")
    if html_tag:
        if not html_tag.get("lang"):
            html_tag["lang"] = language
    else:
        # Wrap everything in <html lang="..."> if missing
        new_html = soup.new_tag("html", lang=language)
        for child in list(soup.children):
            new_html.append(child.extract())
        soup.append(new_html)
        html_tag = new_html

    # Ensure <title> exists (drives DisplayDocTitle in PDF metadata)
    head = soup.find("head")
    if not head:
        head = soup.new_tag("head")
        html_tag.insert(0, head)
    if not head.find("title"):
        title_tag = soup.new_tag("title")
        title_tag.string = Path(html_path).stem.replace("_", " ").replace("-", " ")
        head.append(title_tag)

    # Inject print stylesheet
    style_tag = soup.new_tag("style")
    style_tag.string = _PRINT_CSS
    head.append(style_tag)

    # Sanitize tables before WeasyPrint — empty tables (no rows) can trigger
    # "Table wrapper without a table" in WeasyPrint's layout engine.
    for table in soup.find_all("table"):
        if not table.find("tr"):
            table.decompose()

    # Ensure every <img> has an alt attribute to silence WeasyPrint warnings
    for img in soup.find_all("img"):
        if not img.get("alt"):
            img["alt"] = ""

    html_str = str(soup)

    # Apply the WeasyPrint font-subsetting monkeypatch (HarfBuzz 68.x bug)
    from output_gen.report_page import _patch_weasyprint_font_subsetting
    _patch_weasyprint_font_subsetting()

    try:
        # base_url lets WeasyPrint resolve relative image paths if any remain
        html_obj = HTML(string=html_str, base_url=str(html_file.parent))
        try:
            pdf_bytes = html_obj.write_pdf(pdf_tags=True)
        except ValueError as ve:
            # WeasyPrint can raise "Table wrapper without a table" when
            # pdf_tags=True and certain CSS/table structures confuse the
            # tag tree builder.  Fall back to untagged — the iText tagger
            # handles real PDF structure tagging anyway.
            logger.warning("pdf_tags=True failed (%s), retrying without tags", ve)
            pdf_bytes = html_obj.write_pdf()
    except Exception as e:
        raise RuntimeError(f"WeasyPrint rendering failed: {e}") from e

    # WeasyPrint doesn't write Catalog /Metadata (XMP).  Inject XMP metadata
    # via PyMuPDF.
    pdf_bytes = _inject_xmp_metadata(pdf_bytes, language)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pdf_bytes)

    logger.info(
        "Rendered HTML-as-PDF: %s (%.1f KB)",
        out.name,
        len(pdf_bytes) / 1024,
    )
    return str(out)


def _inject_xmp_metadata(pdf_bytes: bytes, language: str = "en") -> bytes:
    """Add XMP metadata to a WeasyPrint PDF.

    WeasyPrint sets the Info dictionary but not the Catalog /Metadata stream.
    """
    try:
        import fitz

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        title = doc.metadata.get("title", "")
        xmp = (
            '<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">\n'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
            '  <rdf:Description rdf:about=""\n'
            '    xmlns:dc="http://purl.org/dc/elements/1.1/"\n'
            '    xmlns:pdf="http://ns.adobe.com/pdf/1.3/">\n'
            f'    <dc:title><rdf:Alt><rdf:li xml:lang="x-default">'
            f'{_xml_escape(title)}</rdf:li></rdf:Alt></dc:title>\n'
            f'    <dc:language><rdf:Bag><rdf:li>{language}</rdf:li>'
            f'</rdf:Bag></dc:language>\n'
            '    <pdf:Producer>WeasyPrint + PDF Accessibility Pipeline'
            '</pdf:Producer>\n'
            '  </rdf:Description>\n'
            '</rdf:RDF>\n'
            '</x:xmpmeta>\n'
            '<?xpacket end="w"?>'
        )
        doc.set_xml_metadata(xmp)
        result = doc.tobytes(deflate=True)
        doc.close()
        return result
    except Exception as e:
        logger.warning("Failed to inject XMP metadata: %s", e)
        return pdf_bytes


def _xml_escape(s: str) -> str:
    """Escape XML special characters."""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))
