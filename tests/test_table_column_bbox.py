# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for _fix_docling_table_column_order in docling_converter."""

import sys
import os
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from converter.docling_converter import _fix_docling_table_column_order


def _make_cell(row, col, text, bbox_l=None):
    """Create a mock Docling TableCell."""
    cell = MagicMock()
    cell.start_row_offset_idx = row
    cell.start_col_offset_idx = col
    cell.end_col_offset_idx = col + 1
    cell.col_span = 1
    cell.row_span = 1
    cell.text = text
    cell.column_header = (row == 0)
    cell.row_header = False
    if bbox_l is not None:
        cell.bbox = MagicMock()
        cell.bbox.l = bbox_l
    else:
        cell.bbox = None
    return cell


def _make_doc(tables):
    """Create a mock Docling document with given table cell lists."""
    doc = MagicMock()
    items = []
    for cells in tables:
        from docling_core.types.doc import TableItem
        item = MagicMock(spec=TableItem)
        item.data = MagicMock()
        item.data.table_cells = cells
        items.append((item, 0))
    doc.iterate_items.return_value = items
    return doc


def test_fixes_scrambled_body_columns():
    """Body cells with wrong col indices get reassigned by spatial order."""
    cells = [
        _make_cell(0, 0, "Room", bbox_l=78),
        _make_cell(0, 1, "Count", bbox_l=140),
        _make_cell(0, 2, "Use", bbox_l=326),
        # Body: "3001" at x=82 got col=2, "28" at x=126 got col=0, "Trainings" at x=246 got col=1
        _make_cell(1, 2, "3001", bbox_l=82),
        _make_cell(1, 0, "28", bbox_l=126),
        _make_cell(1, 1, "Trainings", bbox_l=246),
    ]
    doc = _make_doc([cells])

    result = _fix_docling_table_column_order(doc)

    assert result == 1
    # "3001" (leftmost) should now be col 0
    assert cells[3].start_col_offset_idx == 0
    assert cells[3].end_col_offset_idx == 1
    # "28" (middle) should now be col 1
    assert cells[4].start_col_offset_idx == 1
    assert cells[4].end_col_offset_idx == 2
    # "Trainings" (rightmost) should now be col 2
    assert cells[5].start_col_offset_idx == 2
    assert cells[5].end_col_offset_idx == 3


def test_no_change_when_correctly_assigned():
    """Tables with correct column order are untouched."""
    cells = [
        _make_cell(0, 0, "A", bbox_l=10),
        _make_cell(0, 1, "B", bbox_l=60),
        _make_cell(1, 0, "a1", bbox_l=10),
        _make_cell(1, 1, "b1", bbox_l=60),
    ]
    doc = _make_doc([cells])

    result = _fix_docling_table_column_order(doc)

    assert result == 0
    assert cells[2].start_col_offset_idx == 0
    assert cells[3].start_col_offset_idx == 1


def test_no_change_without_bboxes():
    """Tables without bboxes are skipped."""
    cells = [
        _make_cell(0, 0, "A", bbox_l=None),
        _make_cell(0, 1, "B", bbox_l=None),
        _make_cell(1, 0, "a1", bbox_l=None),
        _make_cell(1, 1, "b1", bbox_l=None),
    ]
    doc = _make_doc([cells])

    result = _fix_docling_table_column_order(doc)

    assert result == 0


def test_no_change_with_colspan():
    """Tables with spans > 1 are skipped even if cols look wrong."""
    cells = [
        _make_cell(0, 0, "A", bbox_l=10),
        _make_cell(0, 1, "B", bbox_l=60),
        _make_cell(1, 1, "x", bbox_l=10),
        _make_cell(1, 0, "y", bbox_l=60),
    ]
    cells[2].col_span = 2  # Has a colspan > 1
    doc = _make_doc([cells])

    result = _fix_docling_table_column_order(doc)

    assert result == 0


def test_fixes_multiple_scrambled_body_rows():
    """All body rows scrambled get reassigned (real-world pattern)."""
    cells = [
        _make_cell(0, 0, "Room", bbox_l=78),
        _make_cell(0, 1, "Count", bbox_l=140),
        _make_cell(0, 2, "Use", bbox_l=326),
        # Row 1: scrambled
        _make_cell(1, 2, "3001", bbox_l=82),
        _make_cell(1, 0, "28", bbox_l=126),
        _make_cell(1, 1, "Trainings", bbox_l=246),
        # Row 2: also scrambled
        _make_cell(2, 2, "3027", bbox_l=81),
        _make_cell(2, 0, "16", bbox_l=126),
        _make_cell(2, 1, "Meetings", bbox_l=246),
    ]
    doc = _make_doc([cells])

    result = _fix_docling_table_column_order(doc)

    assert result == 1
    # Row 1
    assert cells[3].start_col_offset_idx == 0  # 3001
    assert cells[4].start_col_offset_idx == 1  # 28
    assert cells[5].start_col_offset_idx == 2  # Trainings
    # Row 2
    assert cells[6].start_col_offset_idx == 0  # 3027
    assert cells[7].start_col_offset_idx == 1  # 16
    assert cells[8].start_col_offset_idx == 2  # Meetings


def test_no_change_with_duplicate_col_indices():
    """Skipped when body cells have duplicate col assignments (not a permutation)."""
    cells = [
        _make_cell(0, 0, "A", bbox_l=10),
        _make_cell(0, 1, "B", bbox_l=60),
        # Both body cells assigned col=0
        _make_cell(1, 0, "x", bbox_l=10),
        _make_cell(1, 0, "y", bbox_l=60),
    ]
    doc = _make_doc([cells])

    result = _fix_docling_table_column_order(doc)

    assert result == 0


def test_no_change_when_one_row_correct():
    """Skipped if any body row already has correct spatial order."""
    cells = [
        _make_cell(0, 0, "A", bbox_l=10),
        _make_cell(0, 1, "B", bbox_l=60),
        # Row 1: scrambled
        _make_cell(1, 1, "x", bbox_l=10),
        _make_cell(1, 0, "y", bbox_l=60),
        # Row 2: correct
        _make_cell(2, 0, "m", bbox_l=10),
        _make_cell(2, 1, "n", bbox_l=60),
    ]
    doc = _make_doc([cells])

    result = _fix_docling_table_column_order(doc)

    assert result == 0
