# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTML Accessibility Auditor.

Audits HTML content against WCAG 2.1 accessibility standards.
"""

import logging
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

from bs4 import BeautifulSoup

from audit.checks import (
    HeadingHierarchyCheck,
    HeadingContentCheck,
    DocumentTitleCheck,
    FlatHeadingNestingCheck,
    DocumentLanguageCheck,
    MainLandmarkCheck,
    SkipLinkCheck,
    LandmarksCheck,
    AltTextCheck,
    FigureStructureCheck,
    LinkTextCheck,
    NewWindowLinkCheck,
    TableHeaderCheck,
    TableStructureCheck,
    TableRegularityCheck,
    PipeDelimitedDataCheck,
    ColorContrastCheck,
    FormLabelCheck,
    FormRequiredFieldCheck,
    FormFieldsetCheck,
    ReadingOrderCheck,
    ColorUseCheck,
    ResizeTextCheck,
    ImagesOfTextCheck,
    ReflowCheck,
    NonTextContrastCheck,
    TextSpacingCheck,
    LanguageOfPartsCheck,
    HTMLParsingCheck,
    InputPurposeCheck,
    NameRoleValueCheck,
    KeyboardAccessCheck,
    FocusOrderCheck,
    FocusVisibleCheck,
    ErrorIdentificationCheck,
    FormulaAltTextCheck,
)
from audit.standards import (
    SEVERITY_LEVELS,
    WCAG_PRINCIPLES,
    get_criterion_info,
    get_principle_for_criterion,
    compute_compliance_score,
)
from audit.context_collector import ContextCollector

logger = logging.getLogger(__name__)


class AccessibilityAuditor:
    """Audits HTML content for WCAG 2.1 accessibility compliance."""

    def __init__(
        self,
        html_path: Optional[str] = None,
        html_content: Optional[str] = None,
        image_dir: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
        pdf_path: Optional[str] = None,
    ):
        self.html_path = html_path
        self.html_content = html_content
        self.image_dir = image_dir
        self.pdf_path = pdf_path
        self.soup = None
        self.html_files = []

        self.options = {
            "severity_threshold": "minor",
            "report_format": "json",
            "detailed": True,
            "include_remediated": True,
        }
        if options:
            self.options.update(options)

        self.issues: List[Dict[str, Any]] = []

    def load_html(self) -> bool:
        """Load and parse the HTML content."""
        try:
            if self.html_content:
                self.soup = BeautifulSoup(self.html_content, "html.parser")
                return True

            if self.html_path and os.path.exists(self.html_path):
                if os.path.isdir(self.html_path):
                    # Check for extracted_html subdirectory
                    extracted_html_dir = os.path.join(self.html_path, "extracted_html")
                    if os.path.isdir(extracted_html_dir):
                        self.html_path = extracted_html_dir

                    html_files = sorted(
                        f
                        for f in os.listdir(self.html_path)
                        if f.endswith(".html")
                    )
                    if not html_files:
                        logger.error("No HTML files found in: %s", self.html_path)
                        return False

                    self.html_files = [
                        os.path.join(self.html_path, f) for f in html_files
                    ]
                    with open(self.html_files[0], "r", encoding="utf-8") as f:
                        self.html_content = f.read()
                    self.soup = BeautifulSoup(self.html_content, "html.parser")
                    return True
                else:
                    with open(self.html_path, "r", encoding="utf-8") as f:
                        self.html_content = f.read()
                    self.soup = BeautifulSoup(self.html_content, "html.parser")
                    return True

            logger.error("No HTML content or valid file path provided")
            return False

        except (FileNotFoundError, IOError) as e:
            logger.error("Error loading HTML: %s", str(e))
            return False

    def audit(self) -> Dict[str, Any]:
        """Perform a comprehensive accessibility audit."""
        self.issues = []

        if not self.soup:
            if not self.load_html():
                return self._build_report()

        # Open source PDF for color extraction (optional)
        fitz_doc = None
        if self.pdf_path:
            try:
                import fitz
                fitz_doc = fitz.open(self.pdf_path)
            except Exception as e:
                logger.debug("Could not open PDF for color analysis: %s", e)

        try:
            if self.html_files:
                for html_file in self.html_files:
                    try:
                        with open(html_file, "r", encoding="utf-8") as f:
                            page_soup = BeautifulSoup(f.read(), "html.parser")

                        page_num = None
                        file_name = os.path.basename(html_file)
                        match = re.search(r"page[_-]?(\d+)\.html$", file_name, re.IGNORECASE)
                        if match:
                            page_num = int(match.group(1))

                        self._audit_page(page_soup, page_num, html_file, file_name, fitz_doc=fitz_doc)
                    except Exception as e:
                        logger.error("Error processing %s: %s", html_file, str(e))
            else:
                file_path = self.html_path if self.html_path else None
                file_name = os.path.basename(file_path) if file_path else None
                self._audit_page(self.soup, None, file_path, file_name, fitz_doc=fitz_doc)

            # Document-level PDF font checks
            if fitz_doc:
                self._check_pdf_fonts(fitz_doc)
        finally:
            if fitz_doc:
                try:
                    fitz_doc.close()
                except Exception:
                    pass

        logger.info("Audit completed. Total issues found: %d", len(self.issues))
        return self._build_report()

    def _audit_page(self, soup, page_num=None, file_path=None, file_name=None, fitz_doc=None):
        """Audit a single HTML page."""
        if file_name is None and file_path:
            file_name = os.path.basename(file_path)

        if page_num is None and file_name:
            match = re.search(r"page[_-]?(\d+)\.html$", file_name, re.IGNORECASE)
            if match:
                page_num = int(match.group(1))

        current_issues_count = len(self.issues)

        # Resolve PyMuPDF page for color contrast extraction
        fitz_page = None
        if fitz_doc is not None and page_num is not None:
            try:
                fitz_page = fitz_doc[page_num - 1]  # 1-based → 0-based
            except (IndexError, Exception):
                pass

        checks = [
            # Existing checks
            HeadingHierarchyCheck(soup, self._add_issue),
            HeadingContentCheck(soup, self._add_issue),
            FlatHeadingNestingCheck(soup, self._add_issue),
            DocumentTitleCheck(soup, self._add_issue),
            DocumentLanguageCheck(soup, self._add_issue),
            MainLandmarkCheck(soup, self._add_issue),
            SkipLinkCheck(soup, self._add_issue),
            LandmarksCheck(soup, self._add_issue),
            AltTextCheck(soup, self._add_issue),
            FigureStructureCheck(soup, self._add_issue),
            LinkTextCheck(soup, self._add_issue),
            NewWindowLinkCheck(soup, self._add_issue),
            TableHeaderCheck(soup, self._add_issue),
            TableStructureCheck(soup, self._add_issue),
            TableRegularityCheck(soup, self._add_issue),
            PipeDelimitedDataCheck(soup, self._add_issue),
            FormLabelCheck(soup, self._add_issue),
            FormRequiredFieldCheck(soup, self._add_issue),
            FormFieldsetCheck(soup, self._add_issue),
            # New WCAG 2.1 AA checks
            ReadingOrderCheck(soup, self._add_issue),
            ColorUseCheck(soup, self._add_issue),
            ResizeTextCheck(soup, self._add_issue),
            ImagesOfTextCheck(soup, self._add_issue),
            ReflowCheck(soup, self._add_issue),
            NonTextContrastCheck(soup, self._add_issue),
            TextSpacingCheck(soup, self._add_issue),
            LanguageOfPartsCheck(soup, self._add_issue),
            HTMLParsingCheck(soup, self._add_issue, raw_html=str(soup)),
            InputPurposeCheck(soup, self._add_issue),
            # Tier 1 checks — critical AA conformance gaps
            NameRoleValueCheck(soup, self._add_issue),
            KeyboardAccessCheck(soup, self._add_issue),
            FocusOrderCheck(soup, self._add_issue),
            FocusVisibleCheck(soup, self._add_issue),
            ErrorIdentificationCheck(soup, self._add_issue),
            # Formula checks
            FormulaAltTextCheck(soup, self._add_issue),
        ]

        for check in checks:
            try:
                check.check()
            except Exception as e:
                logger.error("Error in %s: %s", check.__class__.__name__, str(e))

        # Color contrast: run per-page when PDF is available
        self._run_color_contrast_checks(soup, fitz_doc, fitz_page)

        # Annotate new issues with page/file info
        for issue in self.issues[current_issues_count:]:
            if "location" not in issue or issue["location"] is None:
                issue["location"] = {}
            if file_path:
                issue["location"]["file_path"] = file_path
                issue["location"]["file_name"] = file_name
            if page_num is not None and "page_number" not in issue["location"]:
                issue["location"]["page_number"] = page_num

    def _run_color_contrast_checks(self, soup, fitz_doc, fitz_page):
        """Run color contrast checks, iterating all PDF pages when needed.

        Single-file HTML (fitz_page is None, fitz_doc available): runs
        ColorContrastCheck once per PDF page so every page is checked.
        """
        if fitz_page is not None:
            # Multi-file case: page already resolved
            try:
                ColorContrastCheck(soup, self._add_issue, pdf_page=fitz_page).check()
            except Exception as e:
                logger.error("Error in ColorContrastCheck: %s", e)
            return

        if fitz_doc is None:
            # No PDF at all: HTML fallback
            try:
                ColorContrastCheck(soup, self._add_issue, pdf_page=None).check()
            except Exception as e:
                logger.error("Error in ColorContrastCheck: %s", e)
            return

        # Single-file case: iterate all PDF pages
        loop_start = len(self.issues)
        for page_idx in range(len(fitz_doc)):
            pre_count = len(self.issues)
            try:
                ColorContrastCheck(
                    soup, self._add_issue, pdf_page=fitz_doc[page_idx]
                ).check()
            except Exception as e:
                logger.error("Error in ColorContrastCheck page %d: %s", page_idx + 1, e)
            # Tag new issues from this page
            for issue in self.issues[pre_count:]:
                if "location" not in issue or issue["location"] is None:
                    issue["location"] = {}
                issue["location"]["page_number"] = page_idx + 1

        # Deduplicate compliant messages: keep failures only, or one compliant
        new_issues = self.issues[loop_start:]
        has_failures = any(
            i["type"] == "insufficient-color-contrast" for i in new_issues
        )
        if has_failures:
            # Drop all compliant entries from this run
            self.issues[loop_start:] = [
                i for i in new_issues if i["type"] != "compliant-color-contrast"
            ]
        elif len(new_issues) > 1:
            # All pages passed — keep exactly one compliant entry
            first_compliant = next(
                (i for i in new_issues if i["type"] == "compliant-color-contrast"),
                None,
            )
            self.issues[loop_start:] = [first_compliant] if first_compliant else []

    def _check_pdf_fonts(self, fitz_doc):
        """Detect Type 3 fonts with opaque custom encoding (no usable ToUnicode).

        These fonts use numeric-only glyph names and lack a ToUnicode CMap,
        making text extraction produce gibberish.  One issue per unique font.
        """
        _NUMERIC_GLYPH = re.compile(r"^i?\d+$")
        seen_fonts: set = set()

        for page in fitz_doc:
            for font_info in page.get_fonts(full=True):
                xref, _, ftype, fname = font_info[0], font_info[1], font_info[2], font_info[3]
                if "Type3" not in str(ftype) and "type3" not in str(ftype):
                    continue
                if fname in seen_fonts:
                    continue
                seen_fonts.add(fname)

                try:
                    font_obj = fitz_doc.xref_object(xref)
                except Exception:
                    continue

                # Skip if font already has a ToUnicode CMap
                if "/ToUnicode" in font_obj:
                    continue

                # Find Encoding → Differences glyph names
                enc_match = re.search(r"/Encoding\s+(\d+)\s+\d+\s+R", font_obj)
                if not enc_match:
                    continue
                try:
                    enc_obj = fitz_doc.xref_object(int(enc_match.group(1)))
                except Exception:
                    continue
                if "/Differences" not in enc_obj:
                    continue

                # Extract glyph names from Differences array
                diff_match = re.search(r"/Differences\s*\[([^\]]+)\]", enc_obj, re.DOTALL)
                if not diff_match:
                    continue
                tokens = diff_match.group(1).split()
                glyph_names = [t.lstrip("/") for t in tokens if t.startswith("/")]

                if not glyph_names:
                    continue

                # Flag if ALL glyph names are numeric (e.g. /1, /2, /i255)
                if all(_NUMERIC_GLYPH.match(g) for g in glyph_names):
                    self._add_issue(
                        issue_type="scrambled-type3-font",
                        wcag_criterion="1.3.1",
                        severity="critical",
                        element=None,
                        description=(
                            f"Type 3 font '{fname}' uses opaque custom encoding "
                            f"with no ToUnicode CMap. Text extraction will produce "
                            f"gibberish \u2014 the accessible output will not contain "
                            f"readable text from this font."
                        ),
                        location={"page_number": 0},
                        status="needs_remediation",
                    )

    def _add_issue(
        self,
        issue_type: str,
        wcag_criterion: str,
        severity: str,
        element=None,
        description=None,
        context=None,
        location=None,
        status="needs_remediation",
        remediation_source=None,
    ):
        """Add an issue to the issues list."""
        # compliant and informational statuses must always be recorded and must
        # bypass the severity threshold: compliant = the check passed (no score
        # impact); informational = an advisory, non-gating finding (e.g. the
        # flat-heading-nesting advisory) whose severity "info" is intentionally
        # not in SEVERITY_LEVELS and would otherwise be silently dropped.
        if status in ("compliant", "informational"):
            pass
        else:
            threshold = self.options.get("severity_threshold", "minor")
            min_severity = SEVERITY_LEVELS.get(threshold, 1)
            if SEVERITY_LEVELS.get(severity, 0) < min_severity:
                return
            if (
                not self.options.get("include_remediated", True)
                and status != "needs_remediation"
            ):
                return

        issue_id = f"issue-{len(self.issues) + 1}"

        # Collect context if detailed mode
        if self.options.get("detailed", True) and element and context is None:
            try:
                context = ContextCollector(element).collect()
            except Exception:
                context = {}
                if element and hasattr(element, "name"):
                    context = {"element_name": element.name}

        element_str = (
            element.name if element and hasattr(element, "name") else "unknown"
        )

        if location is None and element:
            location = {
                "path": self._get_element_path(element),
            }

        criterion_info = get_criterion_info(wcag_criterion)

        issue = {
            "id": issue_id,
            "type": issue_type,
            "wcag_criterion": wcag_criterion,
            "criterion_name": criterion_info.get("name", ""),
            "criterion_level": criterion_info.get("level", ""),
            "severity": severity,
            "element": element_str,
            "description": description or f"WCAG {wcag_criterion} issue: {issue_type}",
            "context": context if self.options.get("detailed", True) else None,
            "location": location,
            "remediation_status": status,
            "remediation_source": remediation_source,
            "remediation_date": (
                datetime.now(timezone.utc).isoformat()
                if status in ["remediated", "auto_remediated"]
                else None
            ),
        }

        self.issues.append(issue)

    def _get_element_path(self, element) -> str:
        """Get the CSS selector path to an element."""
        try:
            path = []
            current = element
            while current and hasattr(current, "name"):
                if current.get("data-a11y-id"):
                    path.append(f'{current.name}[data-a11y-id="{current.get("data-a11y-id")}"]')
                    break
                elif current.get("id"):
                    path.append(f"{current.name}#{current.get('id')}")
                    break
                elif current.get("class"):
                    classes = current.get("class", [])
                    if classes:
                        path.append(f"{current.name}.{'.'.join(str(c) for c in classes)}")
                else:
                    siblings = current.find_previous_siblings(current.name)
                    if siblings:
                        path.append(f"{current.name}:nth-of-type({len(siblings) + 1})")
                    else:
                        path.append(current.name)
                current = current.parent
            return " > ".join(reversed(path))
        except Exception:
            return "Unknown path"

    def _build_report(self) -> Dict[str, Any]:
        """Build the audit report from collected issues."""
        # Ensure all issues have location
        for issue in self.issues:
            if "location" not in issue or issue["location"] is None:
                issue["location"] = {}
            if "page_number" not in issue["location"]:
                issue["location"]["page_number"] = 0

        # Group by page
        issues_by_page = {}
        for issue in self.issues:
            page_num = issue["location"].get("page_number", 0)
            if page_num not in issues_by_page:
                issues_by_page[page_num] = []
            issues_by_page[page_num].append(issue)

        # Group by status
        issues_by_status = {
            "needs_remediation": [],
            "remediated": [],
            "auto_remediated": [],
            "compliant": [],
        }
        for issue in self.issues:
            status = issue.get("remediation_status", "needs_remediation")
            if status in issues_by_status:
                issues_by_status[status].append(issue)

        # Severity counts
        severity_counts = {"critical": 0, "major": 0, "minor": 0, "info": 0}
        for issue in self.issues:
            sev = issue.get("severity", "info")
            if sev in severity_counts:
                severity_counts[sev] += 1

        # Compliance scoring
        compliance = compute_compliance_score(self.issues)

        # Group issues by WCAG principle
        issues_by_principle = {p: [] for p in WCAG_PRINCIPLES}
        for issue in self.issues:
            principle = get_principle_for_criterion(issue.get("wcag_criterion", ""))
            if principle in issues_by_principle:
                issues_by_principle[principle].append(issue)

        return {
            "summary": {
                "total_issues": len(self.issues),
                "needs_remediation": len(issues_by_status["needs_remediation"]),
                "remediated": len(issues_by_status["remediated"]),
                "auto_remediated": len(issues_by_status["auto_remediated"]),
                "compliant": len(issues_by_status["compliant"]),
                "severity_counts": severity_counts,
            },
            "compliance": compliance,
            "by_principle": issues_by_principle,
            "by_page": {
                page: {
                    "total": len(page_issues),
                    "needs_remediation": sum(
                        1 for i in page_issues if i["remediation_status"] == "needs_remediation"
                    ),
                    "compliant": sum(
                        1 for i in page_issues if i["remediation_status"] == "compliant"
                    ),
                    "issues": page_issues,
                }
                for page, page_issues in issues_by_page.items()
            },
            "by_status": issues_by_status,
            "issues": self.issues,
        }
