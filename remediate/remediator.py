# SPDX-License-Identifier: AGPL-3.0-or-later
"""High-level remediation entry point.

Takes HTML (as file path or string) and audit issues, applies all
applicable remediation strategies, and returns the updated HTML.
"""

import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

from remediate.remediation_manager import RemediationManager
from remediate.strategies.figure_remediation import clear_redundant_img_alt
from remediate.strategies.table_remediation import (
    batch_fix_all_tables,
    configure_table_ai,
)
from remediate.strategies.toc_remediation import convert_toc_tables
from remediate.strategies.pipe_table_remediation import (
    convert_pipe_delimited_paragraphs,
)
from remediate.strategies.heading_remediation import (
    remediate_descriptive_title,
    remediate_flat_heading_nesting,
)

logger = logging.getLogger(__name__)


def _build_table_page_map(struct_data: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Build a table-text-prefix → PDF page number map from docling structure data."""
    if not struct_data:
        return {}
    page_map: Dict[str, int] = {}
    for elem in struct_data.get("elements", []):
        if elem.get("type") == "TableItem":
            page_no = elem.get("page_no")
            text = (elem.get("text") or "").strip()
            if page_no is not None and text:
                # Use first 40 chars of markdown text as a stable prefix key
                prefix = text[:40]
                page_map[prefix] = page_no
    return page_map


def _enrich_issues_with_page_numbers(
    issues: List[Dict[str, Any]],
    struct_data: Optional[Dict[str, Any]],
) -> None:
    """Assign page numbers to audit issues using the Docling structure JSON.

    When auditing a single HTML file, page_number is not set on issues.
    This function matches each issue's element context (tag + text) against
    structure JSON elements to resolve the original PDF page number.
    """
    if not struct_data:
        return

    elements = struct_data.get("elements", [])
    if not elements:
        return

    # Map structure tags to HTML tag names
    _TAG_MAP = {
        "H1": "h1", "H2": "h2", "H3": "h3", "H4": "h4", "H5": "h5", "H6": "h6",
        "P": "p", "Span": "span", "Caption": "caption",
        "L": "ul", "LI": "li",
    }

    # Index 1: (html_tag, text_prefix) → page_no for text-based matching
    text_index: Dict[tuple, int] = {}
    # Index 2: html_tag → [page_no, ...] ordered by document position
    #   Used with context.position.index to look up the Nth occurrence
    tag_pages: Dict[str, List[int]] = {}
    # Index 3: PictureItem pages in document order (images)
    image_pages: List[int] = []

    for elem in elements:
        page_no = elem.get("page_no")
        if page_no is None:
            continue

        elem_type = elem.get("type", "")
        struct_tag = elem.get("tag") or ""
        html_tag = _TAG_MAP.get(struct_tag, struct_tag.lower()) if struct_tag else ""

        if elem_type == "PictureItem":
            image_pages.append(page_no)
            tag_pages.setdefault("img", []).append(page_no)
            tag_pages.setdefault("figure", []).append(page_no)

        if elem_type == "TableItem":
            tag_pages.setdefault("table", []).append(page_no)

        if html_tag:
            tag_pages.setdefault(html_tag, []).append(page_no)
            text = (elem.get("text") or "").strip()
            if text:
                key = (html_tag, text[:60].lower())
                if key not in text_index:
                    text_index[key] = page_no

    for issue in issues:
        loc = issue.get("location") or {}
        if loc.get("page_number") and loc["page_number"] > 0:
            continue

        ctx = issue.get("context") or {}
        elem_name = (ctx.get("element_name") or "").lower()
        text_content = (ctx.get("text_content") or "").strip()
        position = ctx.get("position") or {}
        elem_index = position.get("index", -1)

        page_no = None

        # Strategy 1: text-based match (most precise for headings, paragraphs)
        if elem_name and text_content:
            page_no = text_index.get((elem_name, text_content[:60].lower()))

        # Strategy 2: position-based match using element index among same-tag
        if page_no is None and elem_name and elem_index >= 0:
            pages_list = tag_pages.get(elem_name, [])
            if elem_index < len(pages_list):
                page_no = pages_list[elem_index]

        # Strategy 3: for figure/img issues, try parent figure context
        if page_no is None and elem_name in ("img", "figure"):
            parent_ctx = ctx.get("parent", {})
            parent_name = (parent_ctx.get("element_name") or "").lower()
            if parent_name == "figure" and elem_index >= 0:
                pages_list = tag_pages.get("figure", [])
                if elem_index < len(pages_list):
                    page_no = pages_list[elem_index]

        if page_no is not None and page_no > 0:
            if "location" not in issue or issue["location"] is None:
                issue["location"] = {}
            issue["location"]["page_number"] = page_no


def _deduplicate_landmark_labels(soup: BeautifulSoup) -> int:
    """Ensure all landmark elements have unique aria-labels.

    When a document has multiple <nav> elements (e.g. TOC + LoT + second TOC),
    duplicated aria-labels cause axe-core landmark-unique violations.
    Appends a numeric suffix to duplicate labels.
    """
    fixed = 0
    for tag_name in ("nav", "main", "aside", "section", "form"):
        elements = soup.find_all(tag_name)
        if len(elements) <= 1:
            continue
        label_counts: Counter = Counter()
        for el in elements:
            label_counts[el.get("aria-label", tag_name)] += 1
        seen: Counter = Counter()
        for el in elements:
            label = el.get("aria-label", tag_name)
            if label_counts[label] > 1:
                seen[label] += 1
                if seen[label] > 1:
                    el["aria-label"] = f"{label} {seen[label]}"
                    fixed += 1
    return fixed


def _fix_scrollable_regions(soup: BeautifulSoup) -> int:
    """Make scrollable regions keyboard-focusable (WCAG 2.1.1).

    Elements with overflow:auto/scroll (including <pre> which gets it from
    the Docling stylesheet) need tabindex="0" so keyboard users can scroll.
    Only tabindex is added — no role="region", which would create duplicate
    landmarks when multiple scrollable elements exist.
    """
    fixed = 0
    for el in soup.find_all(True):
        style = el.get("style", "")
        has_inline_overflow = bool(re.search(r"overflow\s*:\s*(auto|scroll)", style))
        is_overflow_pre = el.name == "pre"
        if not has_inline_overflow and not is_overflow_pre:
            continue
        if el.get("tabindex") is not None:
            continue
        el["tabindex"] = "0"
        fixed += 1
    return fixed


def _derive_source_stem(
    html_path: Optional[str], struct_data: Optional[Dict[str, Any]]
) -> str:
    """Best-effort source-PDF stem for filename-title detection."""
    meta = (struct_data or {}).get("metadata") or {}
    if meta.get("source_pdf"):
        return Path(str(meta["source_pdf"])).stem
    if html_path:
        return Path(html_path).stem
    return ""


def _apply_structural_fixes(
    soup: BeautifulSoup,
    options: Optional[Dict[str, Any]],
    struct_data: Optional[Dict[str, Any]],
    source_stem: str,
) -> None:
    """Run the three converter-flattening structural fixes, gated by config."""
    opts = options or {}
    if opts.get("fix_document_title", True):
        remediate_descriptive_title(soup, struct_data, source_stem)
    if opts.get("fix_heading_nesting", True):
        remediate_flat_heading_nesting(soup)
    if opts.get("convert_pipe_tables", True):
        convert_pipe_delimited_paragraphs(soup)


def remediate_html(
    html_path: Optional[str] = None,
    html_content: Optional[str] = None,
    issues: Optional[List[Dict[str, Any]]] = None,
    options: Optional[Dict[str, Any]] = None,
    struct_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Remediate accessibility issues in HTML.

    Args:
        html_path: Path to HTML file (will be overwritten with fixed version).
        html_content: Raw HTML string (alternative to html_path).
        issues: List of audit issues to remediate.
        options: Remediation options from config.yaml.
        struct_data: Docling structure JSON dict (used to resolve PDF page
            numbers for per-table change records in the Document Changes Report).

    Returns:
        Dict with keys: html, stats, results, change_records, table_changes.
    """
    # Per-document table-AI state; no "ai_llm_config" option means no AI.
    configure_table_ai((options or {}).get("ai_llm_config"),
                       (options or {}).get("table_analysis"))

    if issues is None:
        issues = []

    # Load HTML
    if html_path:
        path = Path(html_path)
        if path.is_dir():
            return _remediate_directory(path, issues, options, struct_data)
        html_content = path.read_text(encoding="utf-8")

    if not html_content:
        logger.error("No HTML content provided for remediation")
        return {
            "html": "", "stats": {}, "results": [],
            "change_records": [], "table_changes": [],
        }

    soup = BeautifulSoup(html_content, "html.parser")

    # Enrich issues with page numbers from Docling structure data so
    # the changes report can show which page each fix applies to.
    _enrich_issues_with_page_numbers(issues, struct_data)

    # Convert TOC/LoF/LoT/Index tables to <nav>/<ol> before table remediation.
    # This removes navigational tables from table validation scope entirely.
    toc_stats = convert_toc_tables(soup)

    # Deduplicate landmark labels (e.g. multiple <nav aria-label="Table of Contents">)
    _deduplicate_landmark_labels(soup)

    # Make scrollable regions keyboard-focusable (pre, overflow divs)
    _fix_scrollable_regions(soup)

    # Structural WCAG fixes for converter-flattened content (title, heading
    # nesting, pipe-delimited "tables"). Run before per-issue + batch table
    # remediation so generated tables get the same downstream treatment.
    _apply_structural_fixes(
        soup, options, struct_data, _derive_source_stem(html_path, struct_data)
    )

    manager = RemediationManager(soup, options)
    result = manager.remediate_issues(issues)

    # Batch table fixup: catch anything per-issue processing missed
    table_changes: list = []
    if not options or options.get("fix_tables", True):
        table_page_map = _build_table_page_map(struct_data)
        _, table_changes = batch_fix_all_tables(soup, table_page_map)

    # Clear redundant img alt text inside captioned figures to prevent
    # nested-alt-text violations in the PDF structure tree.
    clear_redundant_img_alt(soup)

    updated_html = str(soup)

    # Write back if path was provided
    if html_path and not Path(html_path).is_dir():
        Path(html_path).write_text(updated_html, encoding="utf-8")
        logger.info("Wrote remediated HTML to %s", html_path)

    return {
        "html": updated_html,
        "stats": result["stats"],
        "results": result["results"],
        "change_records": result.get("change_records", []),
        "table_changes": table_changes,
    }


def _remediate_directory(
    dir_path: Path,
    issues: List[Dict[str, Any]],
    options: Optional[Dict[str, Any]] = None,
    struct_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Remediate all HTML files in a directory."""
    # Check for extracted_html subdirectory
    extracted = dir_path / "extracted_html"
    if extracted.is_dir():
        dir_path = extracted

    html_files = sorted(dir_path.glob("*.html"))
    if not html_files:
        logger.warning("No HTML files in %s", dir_path)
        return {"html": "", "stats": {}, "results": []}

    total_stats = {"processed": 0, "remediated": 0, "failed": 0, "skipped": 0}
    all_results = []
    all_change_records: list = []
    all_table_changes: list = []
    table_page_map = _build_table_page_map(struct_data)

    # Group issues by page
    issues_by_page: Dict[int, List[Dict[str, Any]]] = {}
    global_issues: List[Dict[str, Any]] = []

    for issue in issues:
        page = issue.get("location", {}).get("page_number")
        if page is not None and page > 0:
            issues_by_page.setdefault(page, []).append(issue)
        else:
            global_issues.append(issue)

    for html_file in html_files:
        html_content = html_file.read_text(encoding="utf-8")
        soup = BeautifulSoup(html_content, "html.parser")

        _apply_structural_fixes(
            soup, options, struct_data, _derive_source_stem(str(html_file), struct_data)
        )

        # Determine page number from filename
        import re
        match = re.search(r"page[_-]?(\d+)\.html$", html_file.name, re.IGNORECASE)
        page_num = int(match.group(1)) if match else 0

        # Collect issues for this page
        page_issues = issues_by_page.get(page_num, []) + global_issues

        if not page_issues:
            # Structural fixes (title, headings, pipe tables) may have mutated
            # the soup even when there are no per-page audit issues — persist them.
            html_file.write_text(str(soup), encoding="utf-8")
            continue

        manager = RemediationManager(soup, options)
        result = manager.remediate_issues(page_issues)

        # Batch table fixup for this page
        if not options or options.get("fix_tables", True):
            _, page_table_changes = batch_fix_all_tables(soup, table_page_map)
            all_table_changes.extend(page_table_changes)

        clear_redundant_img_alt(soup)

        # Accumulate stats
        for key in total_stats:
            total_stats[key] += result["stats"].get(key, 0)
        all_results.extend(result["results"])
        all_change_records.extend(result.get("change_records", []))

        # Write back
        html_file.write_text(str(soup), encoding="utf-8")

    logger.info(
        "Remediated %d issues across %d files",
        total_stats["remediated"],
        len(html_files),
    )

    return {
        "html": "",  # Not meaningful for multi-file
        "stats": total_stats,
        "results": all_results,
        "change_records": all_change_records,
        "table_changes": all_table_changes,
    }
