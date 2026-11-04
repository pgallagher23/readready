# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inject accessible CSS enhancements into remediated HTML.

Adds screen-reader-only utility class, focus indicators,
skip-link styling, print styles, and high-contrast support.
"""

import logging
from pathlib import Path

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

ACCESSIBLE_CSS = """
/* ===== Accessibility Enhancements ===== */

/* Screen-reader-only utility */
.sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
}

/* Skip link (visible on focus) */
.skip-link {
    position: absolute;
    top: -44px;
    left: 0;
    background: #000;
    color: #fff;
    padding: 10px 16px;
    min-height: 44px;
    min-width: 44px;
    z-index: 100;
    font-size: 1rem;
    text-decoration: none;
    transition: top 0.2s ease;
    box-sizing: border-box;
}
.skip-link:focus {
    top: 0;
    outline: 3px solid #4A90D9;
    outline-offset: 2px;
}

/* Focus indicators (WCAG 2.4.7) */
a:focus,
button:focus,
input:focus,
select:focus,
textarea:focus,
[tabindex]:focus {
    outline: 3px solid #4A90D9;
    outline-offset: 2px;
}

/* Ensure minimum touch/pointer target size (WCAG 2.5.5) */
a, button {
    min-height: 44px;
    min-width: 44px;
}

/* Landmark visual hints (subtle borders for structure) */
header[role="banner"] {
    border-bottom: 2px solid #eee;
    padding-bottom: 1em;
    margin-bottom: 1em;
}
footer[role="contentinfo"] {
    border-top: 2px solid #eee;
    padding-top: 1em;
    margin-top: 2em;
    font-size: 0.9em;
    color: #595959;
}
nav[role="navigation"] {
    padding: 0.5em 0;
    margin-bottom: 1em;
}
nav[role="navigation"] ul {
    list-style: none;
    padding: 0;
    margin: 0;
}
nav[role="navigation"] li {
    display: inline-block;
    margin-right: 1em;
}

/* Figure accessibility styling */
figure[role="group"] {
    margin: 1.5em 0;
    padding: 0.5em;
    border: 1px solid #ddd;
    border-radius: 4px;
}
figure[role="group"] figcaption {
    margin-top: 0.5em;
    font-style: italic;
    color: #494949;
    font-size: 0.95em;
}

/* High contrast mode support */
@media (prefers-contrast: high) {
    body {
        background-color: #fff;
        color: #000;
    }
    a { color: #0000EE; }
    a:visited { color: #551A8B; }
    th { background-color: #ddd; }
    figure[role="group"] { border-color: #000; }
}

/* Reduced motion support (WCAG 2.3.3) */
@media (prefers-reduced-motion: reduce) {
    *, *::before, *::after {
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
    }
}

/* Print styles */
@media print {
    html { background-color: #fff; }
    body {
        max-width: 100%;
        margin: 0;
        padding: 0;
        box-shadow: none;
        font-size: 11pt;
        line-height: 1.4;
    }
    .skip-link, nav[role="navigation"] {
        display: none;
    }
    a[href]::after {
        content: " (" attr(href) ")";
        font-size: 0.85em;
        color: #494949;
    }
    a[href^="#"]::after {
        content: "";
    }
    table { page-break-inside: avoid; }
    figure { page-break-inside: avoid; }
    h1, h2, h3 { page-break-after: avoid; }
    img { max-width: 100% !important; }
}
"""


def inject_accessible_css(html_path: str) -> None:
    """Inject accessible CSS into an HTML file.

    Args:
        html_path: Path to the HTML file to enhance.
    """
    path = Path(html_path)

    if path.is_dir():
        # Handle directory of HTML files
        extracted = path / "extracted_html"
        target_dir = extracted if extracted.is_dir() else path

        for html_file in sorted(target_dir.glob("*.html")):
            _inject_css_into_file(html_file)
        return

    _inject_css_into_file(path)


def _inject_css_into_file(file_path: Path) -> None:
    """Inject CSS into a single HTML file."""
    html = file_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")

    style = soup.find("style")
    if not style:
        style = soup.new_tag("style")
        head = soup.find("head")
        if head:
            head.append(style)
        else:
            return

    # Check if already injected
    if style.string and ".sr-only" in style.string:
        return

    if style.string:
        style.string = style.string + ACCESSIBLE_CSS
    else:
        style.string = ACCESSIBLE_CSS

    file_path.write_text(str(soup), encoding="utf-8")
    logger.info("Injected accessible CSS into %s", file_path.name)
