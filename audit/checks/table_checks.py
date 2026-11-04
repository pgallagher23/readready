# SPDX-License-Identifier: AGPL-3.0-or-later
# Portions derived from Content Accessibility Utility on AWS
# (https://github.com/awslabs/content-accessibility-utility-on-aws),
# Copyright 2025 Amazon.com, Inc. or its affiliates, Apache-2.0.
# Modified by the ReadReady project; see NOTICE.
"""Table-related accessibility checks."""

import re

from bs4 import Tag
from bs4.element import NavigableString

from audit.base_check import AccessibilityCheck


def _is_navigational_table(table: Tag) -> bool:
    """Check if a table has been classified as TOC/LoF/LoT/Index."""
    return table.get("data-table-type") in ("toc", "lof", "lot", "index")


def _is_layout_table(table: Tag) -> bool:
    """Determine if a table is likely used for layout rather than data."""
    if table.get("role") == "presentation":
        return True

    css_classes = table.get("class", [])
    layout_class_patterns = ["layout", "grid", "non-data"]
    if any(
        pattern in " ".join(css_classes).lower()
        for pattern in layout_class_patterns
    ):
        return True

    if not table.find("th") and not table.find("caption"):
        rows = table.find_all("tr")
        if len(rows) <= 1:
            return True
        first_row_cells = rows[0].find_all(["td", "th"]) if rows else []
        if len(first_row_cells) <= 1:
            return True

    return False


def _is_complex_table(table: Tag) -> bool:
    """Determine if a table is complex."""
    cells = table.find_all(["td", "th"])
    for cell in cells:
        if cell.has_attr("rowspan") and int(cell["rowspan"]) > 1:
            return True
        if cell.has_attr("colspan") and int(cell["colspan"]) > 1:
            return True

    header_rows = [row for row in table.find_all("tr") if row.find("th")]
    if len(header_rows) > 1:
        return True

    rows = table.find_all("tr")
    if len(rows) > 10:
        return True

    return False


class TableHeaderCheck(AccessibilityCheck):
    """Check for proper table headers (WCAG 1.3.1)."""

    def check(self) -> None:
        tables = self.soup.find_all("table")

        for table in tables:
            if _is_layout_table(table) or _is_navigational_table(table):
                continue

            # Skip empty tables (no rows) — extraction failure, not a WCAG issue
            if not table.find("tr"):
                continue

            headers = table.find_all("th")
            if not headers:
                self.add_issue(
                    "table-missing-headers",
                    "1.3.1",
                    "major",
                    element=table,
                    description="Data table has no header cells (th elements)",
                )
            else:
                for header in headers:
                    if not header.has_attr("scope"):
                        self.add_issue(
                            "table-missing-scope",
                            "1.3.1",
                            "minor",
                            element=header,
                            description="Table header missing scope attribute",
                        )

            if _is_complex_table(table) and not table.find("caption"):
                self.add_issue(
                    "table-missing-caption",
                    "1.3.1",
                    "minor",
                    element=table,
                    description="Complex table missing caption element",
                )


class TableStructureCheck(AccessibilityCheck):
    """Check for proper table structure (WCAG 1.3.1)."""

    def check(self) -> None:
        tables = self.soup.find_all("table")

        for table in tables:
            if _is_layout_table(table) or _is_navigational_table(table):
                continue

            if not table.find("tr"):
                continue

            first_row = table.find("tr")
            if first_row and first_row.find("th") and not table.find("thead"):
                self.add_issue(
                    "table-missing-thead",
                    "1.3.1",
                    "minor",
                    element=table,
                    description="Table with headers should use thead element",
                )

            if not table.find("tbody") and len(table.find_all("tr")) > 1:
                self.add_issue(
                    "table-missing-tbody",
                    "1.3.1",
                    "minor",
                    element=table,
                    description="Table should use tbody element",
                )

            if _is_complex_table(table):
                cells_with_headers = table.find_all("td", attrs={"headers": True})
                headers_with_id = table.find_all("th", attrs={"id": True})

                if not cells_with_headers and not headers_with_id:
                    self.add_issue(
                        "table-missing-headers-id",
                        "1.3.1",
                        "major",
                        element=table,
                        description="Complex table should use headers/id attributes for cell associations",
                    )

            self._check_irregular_headers(table)

    def _check_irregular_headers(self, table: Tag) -> None:
        rows = table.find_all("tr")
        if len(rows) < 2:
            return

        # Separate header-section rows (thead/tfoot) from data rows
        thead = table.find("thead")
        data_rows = [
            r for r in rows
            if not (r.parent and r.parent.name in ("thead", "tfoot"))
        ]
        header_rows = [
            r for r in rows
            if r.parent and r.parent.name == "thead"
        ]

        # If no thead, treat the first row as the candidate header row
        check_row = header_rows[0] if header_rows else rows[0]
        check_cells = check_row.find_all(["td", "th"])
        check_th = len(check_row.find_all("th"))

        if check_th > 0 and check_th < len(check_cells):
            self.add_issue(
                "table-irregular-headers",
                "1.3.1",
                "major",
                element=table,
                description="Table has irregular header structure in the first row",
            )

        # Check first-column consistency only among data rows
        # (thead rows are expected to have th and should not count)
        # Exclude empty rows (no cells) — they don't affect header structure
        check_rows = data_rows if data_rows else rows
        first_col_headers = 0
        rows_with_cells = 0
        for row in check_rows:
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            rows_with_cells += 1
            if cells[0].name == "th":
                first_col_headers += 1

        if first_col_headers > 0 and first_col_headers < rows_with_cells:
            self.add_issue(
                "table-irregular-headers",
                "1.3.1",
                "major",
                element=table,
                description="Table has irregular header structure in the first column",
            )


class TableRegularityCheck(AccessibilityCheck):
    """Check that tables have consistent column counts (WCAG 1.3.1)."""

    def check(self) -> None:
        tables = self.soup.find_all("table")

        for table in tables:
            if _is_layout_table(table) or _is_navigational_table(table):
                continue

            rows = table.find_all("tr")
            if len(rows) < 2:
                continue

            # Compute effective column count per row
            col_counts = []
            occupied = {}  # (row_idx, col_idx) -> True for rowspan coverage
            for row_idx, row in enumerate(rows):
                col_idx = 0
                for cell in row.find_all(["td", "th"]):
                    while (row_idx, col_idx) in occupied:
                        col_idx += 1
                    colspan = int(cell.get("colspan", 1) or 1)
                    rowspan = int(cell.get("rowspan", 1) or 1)
                    for r in range(rowspan):
                        for c in range(colspan):
                            occupied[(row_idx + r, col_idx + c)] = True
                    col_idx += colspan
                # Count any trailing positions from rowspan
                while (row_idx, col_idx) in occupied:
                    col_idx += 1
                col_counts.append(col_idx)

            if col_counts and len(set(col_counts)) > 1:
                self.add_issue(
                    "table-irregular-columns",
                    "1.3.1",
                    "major",
                    element=table,
                    description=(
                        f"Table has inconsistent column counts across rows "
                        f"(found: {min(col_counts)}-{max(col_counts)} columns)"
                    ),
                )


class PipeDelimitedDataCheck(AccessibilityCheck):
    """Detect tabular data encoded as pipe-delimited paragraphs (WCAG 1.3.1).

    Docling sometimes flattens two-column tables into ``<p>A|B</p>`` rows. The
    table-structure checks only inspect real ``<table>`` elements, so this gap
    would otherwise pass silently. Mirrors the detection used by the
    pipe_table_remediation strategy.
    """

    _CODE_LIKE = re.compile(r"^[A-Z]{2,4}\s?\d{2,4}[A-Z]?$")

    def _pipe_pair(self, p: Tag) -> tuple[str, str] | None:
        """Return (left, right) if ``p`` is a clean single-pipe pair, else None."""
        text = p.get_text()
        if text.count("|") != 1:
            return None
        left, right = text.split("|", 1)
        left, right = left.strip(), right.strip()
        if not left or not right:
            return None
        return left, right

    def _count(self, run: list[tuple[str, str]]) -> int:
        """A run of pipe-pairs counts when it has >=2 rows, or one code row."""
        if not run:
            return 0
        all_codes = all(self._CODE_LIKE.match(left) for left, _ in run)
        if len(run) >= 2 or (len(run) == 1 and all_codes):
            return len(run)
        return 0

    def check(self) -> None:
        total_rows = 0
        # Walk every element; nested elements re-visit the same content as parents.
        # Because pipe pairs are always leaf <p>s, this does not double-count rows.
        for parent in self.soup.find_all(True):
            run = []
            for child in parent.children:
                if isinstance(child, NavigableString):
                    if child.strip():        # non-whitespace text breaks the run
                        total_rows += self._count(run)
                        run = []
                    continue                 # whitespace keeps the run open
                pair = (
                    self._pipe_pair(child)
                    if isinstance(child, Tag) and child.name == "p"
                    else None
                )
                if pair is not None:
                    run.append(pair)
                else:
                    total_rows += self._count(run)
                    run = []
            total_rows += self._count(run)

        if total_rows:
            self.add_issue(
                "pipe-delimited-tabular-data",
                "1.3.1",
                "major",
                element=self.soup.find("body"),
                description=(
                    f"{total_rows} row(s) of tabular data encoded as "
                    f"pipe-delimited text; should be a table or list"
                ),
                status="needs_remediation",
            )
