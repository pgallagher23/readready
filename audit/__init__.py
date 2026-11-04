# SPDX-License-Identifier: AGPL-3.0-or-later
"""Accessibility audit module."""

from audit.auditor import AccessibilityAuditor
from audit.report_generator import generate_report

__all__ = ["AccessibilityAuditor", "generate_report"]
