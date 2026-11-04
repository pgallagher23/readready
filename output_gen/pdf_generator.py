# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF generation from accessible HTML using WeasyPrint.

Converts the remediated, accessible HTML into a tagged PDF.
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def generate_pdf(
    html_path: str,
    output_path: Optional[str] = None,
) -> Optional[str]:
    """Generate a PDF from accessible HTML.

    Args:
        html_path: Path to the HTML file or directory of HTML files.
        output_path: Path for the output PDF. If None, derives from html_path.

    Returns:
        Path to the generated PDF, or None on failure.
    """
    try:
        from weasyprint import HTML
    except ImportError:
        logger.error(
            "WeasyPrint is not installed. Install it with: pip install weasyprint"
        )
        return None

    path = Path(html_path)

    # Find the actual HTML file(s)
    if path.is_dir():
        extracted = path / "extracted_html"
        target_dir = extracted if extracted.is_dir() else path
        html_files = sorted(target_dir.glob("*.html"))
        if not html_files:
            logger.error("No HTML files found in %s", target_dir)
            return None
        # Use first file (single-page) or combine for multi-page
        if len(html_files) == 1:
            source_file = html_files[0]
        else:
            source_file = _combine_html_files(html_files, target_dir)
    else:
        source_file = path

    if not source_file.exists():
        logger.error("HTML file not found: %s", source_file)
        return None

    # Determine output path
    if output_path is None:
        output_path = str(source_file.parent.parent / f"{source_file.stem}.pdf")

    logger.info("Generating PDF from %s", source_file.name)

    try:
        html = HTML(filename=str(source_file), encoding="utf-8")
        html.write_pdf(output_path)
        logger.info("PDF generated: %s", output_path)
        return output_path
    except Exception as e:
        logger.error("PDF generation failed: %s", e)
        return None


def _combine_html_files(html_files: list, target_dir: Path) -> Path:
    """Combine multiple HTML pages into one file for PDF generation."""
    from bs4 import BeautifulSoup

    # Use first file as base
    base_html = html_files[0].read_text(encoding="utf-8")
    base_soup = BeautifulSoup(base_html, "html.parser")

    body = base_soup.find("body")
    if not body:
        return html_files[0]

    # Append content from remaining files
    for html_file in html_files[1:]:
        page_html = html_file.read_text(encoding="utf-8")
        page_soup = BeautifulSoup(page_html, "html.parser")

        # Add page break
        page_break = base_soup.new_tag("div")
        page_break["class"] = "page-break"
        page_break["style"] = "page-break-before: always;"
        body.append(page_break)

        # Append body content
        page_body = page_soup.find("body")
        if page_body:
            for child in list(page_body.children):
                body.append(child.__copy__() if hasattr(child, "__copy__") else child)

    combined_path = target_dir / "_combined.html"
    combined_path.write_text(str(base_soup), encoding="utf-8")
    return combined_path
