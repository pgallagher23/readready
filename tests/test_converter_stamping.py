# SPDX-License-Identifier: AGPL-3.0-or-later
"""Converted HTML must carry stamped ids, added after td->th promotion."""
import inspect

import converter.docling_converter as dc


def test_converter_stamps_after_table_enhancement():
    """Stamping must follow _enhance_table_html, which promotes td to th.

    th is 55.8% of audit leaf tags and td is not stamped, so stamping before
    the promotion would silently miss most addressable elements.
    """
    src = inspect.getsource(dc.convert_pdf_to_html)
    enhance = src.index("_enhance_table_html(html_path, doc)")
    stamp = src.index("stamp_element_ids(")
    assert stamp > enhance, (
        "stamp_element_ids runs before _enhance_table_html promotes td->th; "
        "promoted th elements would never be stamped"
    )


def test_converter_writes_the_stamps_back():
    """Stamping an in-memory soup is useless unless the file is rewritten."""
    src = inspect.getsource(dc.convert_pdf_to_html)
    tail = src[src.index("stamp_element_ids("):]
    assert "write" in tail, (
        "no write after stamp_element_ids -- the stamps never reach disk, and "
        "the remediator re-reads from disk"
    )
