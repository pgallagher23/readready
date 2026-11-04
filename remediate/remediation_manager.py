# SPDX-License-Identifier: AGPL-3.0-or-later
"""Remediation Manager — orchestrates accessibility fixes.

Maps audit issue types to remediation strategy functions and
applies them to the HTML document.
"""

import logging
from typing import Any, Callable, Dict, List, Optional

from bs4 import BeautifulSoup

from remediate.element_resolver import get_id_index, invalidate_id_index
from remediate.strategies.landmark_remediation import (
    remediate_missing_footer_landmark,
    remediate_missing_header_landmark,
    remediate_missing_main_landmark,
    remediate_missing_navigation_landmark,
    remediate_missing_skip_link,
)
from remediate.strategies.document_structure_remediation import (
    remediate_missing_document_title,
    remediate_missing_language,
)
from remediate.strategies.heading_remediation import (
    remediate_empty_heading_content,
    remediate_missing_h1,
    remediate_missing_headings,
    remediate_skipped_heading_level,
)
from remediate.strategies.table_remediation import (
    remediate_table_headers_id,
    remediate_table_irregular_columns,
    remediate_table_irregular_headers,
    remediate_table_missing_caption,
    remediate_table_missing_headers,
    remediate_table_missing_scope,
    remediate_table_missing_tbody,
    remediate_table_missing_thead,
)
from remediate.strategies.link_remediation import (
    remediate_empty_link_text,
    remediate_generic_link_text,
    remediate_new_window_link_no_warning,
)
from remediate.strategies.color_contrast_remediation import (
    remediate_insufficient_color_contrast,
    remediate_insufficient_non_text_contrast,
    remediate_potential_color_contrast_issue,
)
from remediate.strategies.form_remediation import (
    remediate_missing_fieldsets,
    remediate_missing_form_labels,
    remediate_missing_required_indicators,
)
from remediate.strategies.figure_remediation import (
    remediate_alt_text,
    remediate_improper_figure_structure,
    remediate_missing_figure_caption,
)
from remediate.strategies.keyboard_remediation import (
    flag_container_may_trap_focus,
    flag_potential_keyboard_trap,
    remediate_interactive_tabindex_negative,
    remediate_mouse_only_handler,
    remediate_onclick_missing_keyboard_handler,
    remediate_onclick_not_keyboard_accessible,
    remediate_interactive_missing_role,
)
from remediate.strategies.focus_remediation import (
    remediate_blanket_outline_removal,
    remediate_css_order_focus_mismatch,
    remediate_element_focus_outline_removed,
    remediate_flex_reverse_focus_mismatch,
    remediate_focus_outline_removed,
    remediate_positive_tabindex,
    remediate_stylesheet_css_order,
    remediate_stylesheet_flex_reverse,
    remediate_stylesheet_focus_outline_removed,
)
from remediate.strategies.error_identification_remediation import (
    remediate_aria_errormessage_missing_target,
    remediate_aria_invalid_no_error_message,
    remediate_form_no_error_live_region,
    remediate_pattern_input_no_description,
    remediate_required_field_no_error_mechanism,
    remediate_type_validated_input_no_description,
)
from remediate.strategies.name_role_value_remediation import (
    remediate_aria_describedby_missing_target,
    remediate_aria_labelledby_empty_target,
    remediate_aria_labelledby_missing_target,
    remediate_button_missing_name,
    remediate_custom_widget_missing_aria_attrs,
    remediate_iframe_missing_title,
    remediate_input_image_missing_alt,
    remediate_invalid_aria_role,
)
from remediate.strategies.parsing_remediation import (
    remediate_duplicate_id,
    remediate_invalid_nesting,
)
from remediate.strategies.text_spacing_remediation import (
    remediate_fixed_line_height,
    remediate_text_spacing_important_override,
    remediate_text_spacing_overflow_risk,
)
from remediate.strategies.resize_text_remediation import (
    remediate_fixed_font_size,
    remediate_text_overflow_hidden,
)
from remediate.strategies.reflow_remediation import (
    remediate_fixed_width_prevents_reflow,
    remediate_horizontal_scroll_required,
    remediate_nowrap_text,
)
from remediate.strategies.reading_order_remediation import (
    remediate_reading_order_css_reorder,
    remediate_reading_order_tabindex,
)
from remediate.strategies.input_purpose_remediation import (
    remediate_input_missing_autocomplete,
)
from remediate.strategies.language_parts_remediation import (
    remediate_invalid_lang_attribute,
    remediate_missing_lang_on_foreign_content,
    remediate_xml_lang_without_lang,
)
from remediate.strategies.color_link_remediation import (
    remediate_color_only_link_distinction,
    remediate_url_as_link_text,
)
from remediate.strategies.form_extended_remediation import (
    remediate_form_control_missing_name,
    remediate_form_label_empty,
    remediate_form_required_field_missing_aria,
)
from remediate.strategies.widget_remediation import (
    remediate_color_only_status,
    remediate_custom_widget_missing_name,
    remediate_duplicate_link_text_different_url,
    remediate_fieldset_missing_legend,
)

logger = logging.getLogger(__name__)


# Issue types whose per-issue "skip" is expected and benign. Logging one of
# these at WARNING/INFO for every occurrence clutters the manifest log (1500+
# lines across a 1000-PDF run) without signalling a real defect, so they are
# quieted to DEBUG. The authoritative compliance state still comes from the
# post-remediation audit — not these per-issue lines — so nothing is hidden.
#
#   filename-like-* : resolved earlier by the structural title pre-pass
#                     (remediator._apply_structural_fixes); no per-issue
#                     strategy is registered here by design.
#   table-missing-scope : moot for the output PDF — the iText tagger writes
#                         /Scope on every TH structure element.
#   missing-*-landmark / missing-skip-link : added by a sibling landmark fix
#                     (the strategies chain) or not applicable to a linear
#                     converted document with no navigation blocks.
# How many benign (DEBUG-logged) skips pass before an INFO progress line.
# Low enough that a stall is visible within seconds, high enough that an
# ordinary document adds no noise.
_QUIET_SKIP_INTERVAL = 200

_BENIGN_SKIP_REASONS = {
    "filename-like-title": "resolved by the structural title pre-pass",
    "filename-like-heading": "resolved by the structural title pre-pass",
    "table-missing-scope": "header scope is set by the PDF tagger (/Scope on TH)",
    "missing-skip-link": "added by a sibling landmark fix or N/A to a linear document",
    "missing-navigation-landmark": "added by a sibling landmark fix or N/A to a linear document",
    "missing-header-landmark": "added by a sibling landmark fix or N/A to a linear document",
    "missing-footer-landmark": "added by a sibling landmark fix or N/A to a linear document",
}


class RemediationManager:
    """Applies remediation strategies to HTML based on audit issues."""

    def __init__(self, soup: BeautifulSoup, options: Optional[Dict[str, Any]] = None):
        self.soup = soup
        self.options = options or {}
        self.strategies = self._get_remediation_strategies()
        self.stats = {
            "processed": 0,
            "remediated": 0,
            "failed": 0,
            "skipped": 0,
        }
        # Benign skips log at DEBUG; this counter drives the periodic INFO
        # progress line so a long quiet stretch is never mistaken for a hang.
        self._quiet_skips = 0

    def _get_remediation_strategies(self) -> Dict[str, Callable]:
        """Return mapping of issue types to fix functions."""

        def _with_options(fn):
            """Heading strategies read config (window switches) via options."""
            def _call(soup, issue):
                return fn(soup, issue, self.options)
            return _call

        strategies = {
            # Landmarks
            "missing-main-landmark": _with_options(remediate_missing_main_landmark),
            "missing-navigation-landmark": _with_options(remediate_missing_navigation_landmark),
            "missing-header-landmark": _with_options(remediate_missing_header_landmark),
            "missing-footer-landmark": remediate_missing_footer_landmark,
            "missing-skip-link": remediate_missing_skip_link,
            # Document structure
            "missing-document-title": remediate_missing_document_title,
            "missing-document-language": remediate_missing_language,
            "invalid-document-language": remediate_missing_language,
            # Headings
            "no-h1": _with_options(remediate_missing_h1),
            "missing-h1": _with_options(remediate_missing_h1),
            "missing-headings": _with_options(remediate_missing_headings),
            "skipped-heading-level": remediate_skipped_heading_level,
            "empty-heading-content": remediate_empty_heading_content,
            "empty-heading": remediate_empty_heading_content,
            # Tables
            "table-missing-headers": remediate_table_missing_headers,
            "table-headers-id": remediate_table_headers_id,
            "table-missing-headers-id": remediate_table_headers_id,
            "table-missing-scope": remediate_table_missing_scope,
            "table-missing-caption": remediate_table_missing_caption,
            "table-missing-thead": remediate_table_missing_thead,
            "table-missing-tbody": remediate_table_missing_tbody,
            "table-irregular-headers": remediate_table_irregular_headers,
            "table-irregular-columns": remediate_table_irregular_columns,
            # Links
            "empty-link-text": remediate_empty_link_text,
            "generic-link-text": remediate_generic_link_text,
            "new-window-link-no-warning": remediate_new_window_link_no_warning,
            # Color contrast (1.4.3, 1.4.11)
            "insufficient-color-contrast": remediate_insufficient_color_contrast,
            "potential-color-contrast-issue": remediate_potential_color_contrast_issue,
            "insufficient-non-text-contrast": remediate_insufficient_non_text_contrast,
            # Forms
            "missing-form-label": remediate_missing_form_labels,
            "missing-required-indicator": remediate_missing_required_indicators,
            "missing-fieldset": remediate_missing_fieldsets,
            # Figure structure
            "improper-figure-structure": remediate_improper_figure_structure,
            "missing-figure-caption": remediate_missing_figure_caption,
            "empty-figure-caption": remediate_missing_figure_caption,
            # Image alt text (heuristic fallback — Phase 3 VLM handles
            # these first; this catches anything VLM missed)
            "empty-alt-text": remediate_alt_text,
            "missing-alt-text": remediate_alt_text,
            "generic-alt-text": remediate_alt_text,
            # Keyboard access (2.1.1)
            "interactive-tabindex-negative": remediate_interactive_tabindex_negative,
            "onclick-not-keyboard-accessible": remediate_onclick_not_keyboard_accessible,
            "onclick-missing-keyboard-handler": remediate_onclick_missing_keyboard_handler,
            "mouse-only-handler": remediate_mouse_only_handler,
            "interactive-missing-role": remediate_interactive_missing_role,
            # Focus order (2.4.3)
            "positive-tabindex": remediate_positive_tabindex,
            "stylesheet-css-order": remediate_stylesheet_css_order,
            "css-order-focus-mismatch": remediate_css_order_focus_mismatch,
            "flex-reverse-focus-mismatch": remediate_flex_reverse_focus_mismatch,
            "stylesheet-flex-reverse": remediate_stylesheet_flex_reverse,
            # Focus visible (2.4.7)
            "focus-outline-removed": remediate_focus_outline_removed,
            "stylesheet-focus-outline-removed": remediate_stylesheet_focus_outline_removed,
            "blanket-outline-removal": remediate_blanket_outline_removal,
            "element-focus-outline-removed": remediate_element_focus_outline_removed,
            # Error identification (3.3.1)
            "required-field-no-error-mechanism": remediate_required_field_no_error_mechanism,
            "pattern-input-no-description": remediate_pattern_input_no_description,
            "type-validated-input-no-description": remediate_type_validated_input_no_description,
            "aria-invalid-no-error-message": remediate_aria_invalid_no_error_message,
            "aria-errormessage-missing-target": remediate_aria_errormessage_missing_target,
            "form-no-error-live-region": remediate_form_no_error_live_region,
            # Name, Role, Value (4.1.2)
            "invalid-aria-role": remediate_invalid_aria_role,
            "aria-labelledby-missing-target": remediate_aria_labelledby_missing_target,
            "aria-labelledby-empty-target": remediate_aria_labelledby_empty_target,
            "aria-describedby-missing-target": remediate_aria_describedby_missing_target,
            "button-missing-name": remediate_button_missing_name,
            "input-image-missing-alt": remediate_input_image_missing_alt,
            "iframe-missing-title": remediate_iframe_missing_title,
            "custom-widget-missing-aria-attrs": remediate_custom_widget_missing_aria_attrs,
            # Parsing (4.1.1)
            "duplicate-id": remediate_duplicate_id,
            "invalid-nesting": remediate_invalid_nesting,
            # Text spacing (1.4.12)
            "fixed-line-height": remediate_fixed_line_height,
            "text-spacing-important-override": remediate_text_spacing_important_override,
            "text-spacing-overflow-risk": remediate_text_spacing_overflow_risk,
            # Resize text (1.4.4)
            "fixed-font-size": remediate_fixed_font_size,
            "text-overflow-hidden": remediate_text_overflow_hidden,
            # Reflow (1.4.10)
            "fixed-width-prevents-reflow": remediate_fixed_width_prevents_reflow,
            "nowrap-text": remediate_nowrap_text,
            "horizontal-scroll-required": remediate_horizontal_scroll_required,
            # Reading order (1.3.2)
            "reading-order-tabindex": remediate_reading_order_tabindex,
            "reading-order-css-reorder": remediate_reading_order_css_reorder,
            # Input purpose (1.3.5)
            "input-missing-autocomplete": remediate_input_missing_autocomplete,
            # Language of parts (3.1.2)
            "xml-lang-without-lang": remediate_xml_lang_without_lang,
            "invalid-lang-attribute": remediate_invalid_lang_attribute,
            "missing-lang-on-foreign-content": remediate_missing_lang_on_foreign_content,
            # Color use (1.4.1)
            "color-only-link-distinction": remediate_color_only_link_distinction,
            # Link purpose (2.4.4)
            "url-as-link-text": remediate_url_as_link_text,
            # Extended forms (1.3.1, 3.3.2)
            "form-control-missing-name": remediate_form_control_missing_name,
            "form-label-empty": remediate_form_label_empty,
            "form-required-field-missing-aria": remediate_form_required_field_missing_aria,
            # Aliases — map audit issue types to existing strategies
            "missing-title": remediate_missing_document_title,
            "empty-title": remediate_missing_document_title,
            "generic-title": remediate_missing_document_title,
            "no-headings": _with_options(remediate_missing_headings),
            "form-control-missing-label": remediate_missing_form_labels,
            "form-required-field-not-indicated": remediate_missing_required_indicators,
            "form-fieldset-missing-legend": remediate_fieldset_missing_legend,
            "form-related-controls-no-fieldset": remediate_missing_fieldsets,
            # Widget accessible names (4.1.2)
            "custom-widget-missing-name": remediate_custom_widget_missing_name,
            # Duplicate link text (2.4.4)
            "duplicate-link-text-different-url": remediate_duplicate_link_text_different_url,
            # Color-only status (1.4.1)
            "color-only-status": remediate_color_only_status,
            # Keyboard traps — manual review only (2.1.2)
            "potential-keyboard-trap": flag_potential_keyboard_trap,
            "container-may-trap-focus": flag_container_may_trap_focus,
        }

        # Add underscore variants for compatibility
        underscore_variants = {}
        for key, func in strategies.items():
            underscore_key = key.replace("-", "_")
            if underscore_key != key:
                underscore_variants[underscore_key] = func
        strategies.update(underscore_variants)

        return strategies

    @staticmethod
    def _issue_element_context(issue: Dict[str, Any]) -> Dict[str, Any]:
        """Extract compact element context from an issue for the changes report."""
        ctx = issue.get("context") or {}
        attrs = ctx.get("attributes") or {}
        loc = issue.get("location") or {}
        result: Dict[str, Any] = {
            "element_tag": ctx.get("element_name", ""),
            "element_text": (ctx.get("text_content") or "")[:80],
            "element_src": attrs.get("src", ""),
        }
        page_num = loc.get("page_number")
        if page_num is not None and page_num > 0:
            result["page_number"] = page_num
        return result

    def _log_skip(
        self, issue_type: str, severity: str, wcag: str, description: str, *, no_strategy: bool
    ) -> None:
        """Log a skipped issue, quieting known-benign types to DEBUG.

        ``no_strategy`` distinguishes "no strategy registered" (a WARNING for
        unhandled types) from "strategy ran but could not apply a fix" (an INFO).
        Benign types (see ``_BENIGN_SKIP_REASONS``) drop to DEBUG either way.

        Benign skips are counted and a progress line is emitted at INFO every
        ``_QUIET_SKIP_INTERVAL`` of them. A table-heavy document can otherwise
        spend minutes in this branch emitting nothing at all, which is
        indistinguishable from a hang — 1788 of 2855 issues on one 2026-08-14
        document, 400 consecutive silent seconds at 100% CPU.
        """
        benign = _BENIGN_SKIP_REASONS.get(issue_type)
        if benign:
            self._quiet_skips += 1
            if self._quiet_skips % _QUIET_SKIP_INTERVAL == 0:
                logger.info(
                    "  ... still working — %d issues skipped so far "
                    "(benign, detail at DEBUG)",
                    self._quiet_skips,
                )
            logger.debug(
                "  Skipped [%s] (%s, WCAG %s): %s — %s",
                issue_type, severity, wcag, benign, description,
            )
        elif no_strategy:
            logger.warning(
                "  Skipped [%s] (%s, WCAG %s): no remediation strategy available — %s",
                issue_type, severity, wcag, description,
            )
        else:
            logger.info(
                "  Skipped [%s] (%s, WCAG %s): strategy could not apply a fix — %s",
                issue_type, severity, wcag, description,
            )

    def remediate_issue(self, issue: Dict[str, Any]) -> Optional[str]:
        """Apply remediation for a single issue.

        Returns a message describing what was done, or None if skipped.
        """
        issue_type = issue.get("type", "")
        status = issue.get("remediation_status", "needs_remediation")

        description = issue.get("description", "")
        severity = issue.get("severity", "?")
        wcag = issue.get("wcag_criterion", "?")

        # Skip already-handled issues
        if status in ("compliant", "remediated", "auto_remediated"):
            logger.info(
                "  Skipped [%s] (%s, WCAG %s): already %s — %s",
                issue_type, severity, wcag, status, description,
            )
            self.stats["skipped"] += 1
            return None

        strategy = self.strategies.get(issue_type)
        if not strategy:
            self._log_skip(issue_type, severity, wcag, description, no_strategy=True)
            self.stats["skipped"] += 1
            return None

        self.stats["processed"] += 1

        try:
            result = strategy(self.soup, issue)
            if result:
                logger.info(
                    "  Remediated [%s] (%s, WCAG %s): %s",
                    issue_type, severity, wcag, result,
                )
                issue["remediation_status"] = "auto_remediated"
                issue["remediation_source"] = "local_pipeline"
                self.stats["remediated"] += 1
                return result
            else:
                self._log_skip(issue_type, severity, wcag, description, no_strategy=False)
                self.stats["skipped"] += 1
                return None
        except Exception as e:
            logger.error(
                "  Failed [%s] (%s, WCAG %s): %s — %s",
                issue_type, severity, wcag, e, description,
            )
            self.stats["failed"] += 1
            return None

    @staticmethod
    def _sort_issues_for_remediation(
        issues: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Sort issues so table fixes run in the correct order.

        Table remediation is order-sensitive:
        1. Create headers (missing-headers, irregular-headers)
        2. Add structural wrappers (thead, tbody, caption)
        3. Add attributes (scope)
        4. Link headers to cells (headers-id) — must run last

        Issues are grouped by their target table (via context position
        index) so all fixes for one table complete before moving to
        the next.  Non-table issues keep their original order and run
        before table issues (landmarks, headings, images, etc. should
        be processed first).
        """
        TABLE_ISSUE_TYPES = {
            "table-irregular-columns",
            "table-missing-headers", "table-irregular-headers",
            "table-missing-thead", "table-missing-tbody",
            "table-missing-caption", "table-missing-scope",
            "table-missing-headers-id", "table-headers-id",
        }

        # Priority within a single table (lower = earlier)
        # Regularity fix runs first so column counts are consistent
        # before header normalization.
        TABLE_FIX_PRIORITY = {
            "table-irregular-columns": -1,
            "table-missing-headers": 0,
            "table-irregular-headers": 1,
            "table-missing-thead": 2,
            "table-missing-tbody": 3,
            "table-missing-caption": 4,
            "table-missing-scope": 5,
            "table-missing-headers-id": 6,
            "table-headers-id": 6,
        }

        non_table = []
        table_issues = []

        for issue in issues:
            if issue.get("type", "") in TABLE_ISSUE_TYPES:
                table_issues.append(issue)
            else:
                non_table.append(issue)

        def _table_sort_key(issue):
            # Group by table position index, then by fix priority
            ctx = issue.get("context", {})
            pos = ctx.get("position", {}) if isinstance(ctx, dict) else {}
            idx = pos.get("index", 9999) if isinstance(pos, dict) else 9999
            # For th-level issues, the index is the th index, not the
            # table index.  Use a large bucket so they sort after
            # table-level issues within the same table.
            elem = ctx.get("element_name", "") if isinstance(ctx, dict) else ""
            if elem == "th":
                idx = 10000 + idx
            priority = TABLE_FIX_PRIORITY.get(issue.get("type", ""), 99)
            return (idx, priority)

        table_issues.sort(key=_table_sort_key)

        return non_table + table_issues

    def remediate_issues(self, issues: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Remediate all issues and return stats plus full change records.

        ``change_records`` is a list of dicts suitable for the Document Changes
        Report.  Every issue that was attempted is recorded — fixed, skipped,
        or failed — so the report can show unresolved items alongside successes.
        """
        sorted_issues = self._sort_issues_for_remediation(issues)

        # One index for the whole pass. It is deliberately NOT invalidated per
        # fix: the resolver checks that an indexed element is still attached
        # and scans on a miss, so a stale index degrades to a slower lookup
        # rather than a wrong one. Invalidating after each of ~1000 successful
        # fixes would cost a 6 ms rebuild every time and buy nothing.
        invalidate_id_index(self.soup)
        get_id_index(self.soup)

        results = []        # legacy list (type + message, fixed only)
        change_records = []  # full records for the changes report

        for issue in sorted_issues:
            issue_type = issue.get("type", "")
            wcag = issue.get("wcag_criterion", "")
            severity = issue.get("severity", "")
            description = issue.get("description", "")
            elem_ctx = self._issue_element_context(issue)

            # Skip already-compliant — don't pollute the changes report
            if issue.get("remediation_status") in ("compliant", "remediated", "auto_remediated"):
                self.stats["skipped"] += 1
                continue

            strategy = self.strategies.get(issue_type)
            if not strategy:
                self._log_skip(issue_type, severity, wcag, description, no_strategy=True)
                self.stats["skipped"] += 1
                change_records.append({
                    "outcome": "skipped",
                    "type": issue_type,
                    "wcag_criterion": wcag,
                    "severity": severity,
                    "description": description,
                    "detail": "No automated strategy available",
                    **elem_ctx,
                })
                continue

            self.stats["processed"] += 1
            try:
                msg = strategy(self.soup, issue)
                if msg:
                    # Check if strategy flagged for manual review
                    is_manual = issue.get("remediation_status") == "requires_manual_review"
                    if is_manual:
                        logger.info(
                            "  Manual review [%s] (%s, WCAG %s): %s",
                            issue_type, severity, wcag, msg,
                            extra={"contains_content": True,
                                   "content_ref": f"Manual review [{issue_type}] ({severity}, WCAG {wcag})"},
                        )
                        self.stats["skipped"] += 1
                        change_records.append({
                            "outcome": "manual_review",
                            "type": issue_type,
                            "wcag_criterion": wcag,
                            "severity": severity,
                            "description": description,
                            "detail": msg,
                            **elem_ctx,
                        })
                    else:
                        logger.info(
                            "  Remediated [%s] (%s, WCAG %s): %s",
                            issue_type, severity, wcag, msg,
                            extra={"contains_content": True,
                                   "content_ref": f"Remediated [{issue_type}] ({severity}, WCAG {wcag})"},
                        )
                        issue["remediation_status"] = "auto_remediated"
                        issue["remediation_source"] = "local_pipeline"
                        self.stats["remediated"] += 1
                        results.append({"type": issue_type, "message": msg})
                        change_records.append({
                            "outcome": "fixed",
                            "type": issue_type,
                            "wcag_criterion": wcag,
                            "severity": severity,
                            "description": description,
                            "detail": msg,
                            **elem_ctx,
                        })
                else:
                    self._log_skip(issue_type, severity, wcag, description, no_strategy=False)
                    self.stats["skipped"] += 1
                    change_records.append({
                        "outcome": "skipped",
                        "type": issue_type,
                        "wcag_criterion": wcag,
                        "severity": severity,
                        "description": description,
                        "detail": "Strategy could not apply a fix",
                        **elem_ctx,
                    })
            except Exception as e:
                logger.error(
                    "  Failed [%s] (%s, WCAG %s): %s — %s",
                    issue_type, severity, wcag, e, description,
                )
                self.stats["failed"] += 1
                change_records.append({
                    "outcome": "failed",
                    "type": issue_type,
                    "wcag_criterion": wcag,
                    "severity": severity,
                    "description": description,
                    "detail": str(e),
                    **elem_ctx,
                })

        return {
            "stats": self.stats,
            "results": results,
            "change_records": change_records,
        }
