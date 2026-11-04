# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate accessibility audit reports."""

import json
import logging
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)


def generate_report(
    audit_data: Dict[str, Any],
    output_path: str,
    report_format: str = "json",
) -> Dict[str, Any]:
    """Generate an accessibility audit report.

    Args:
        audit_data: Dictionary containing the audit results.
        output_path: Path where the report should be saved.
        report_format: Format of the report ('json' or 'text').

    Returns:
        The audit data (passthrough).
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if report_format == "json":
        _write_json_report(audit_data, output_path)
    elif report_format == "text":
        _write_text_report(audit_data, output_path)
    else:
        logger.warning(f"Unknown report format '{report_format}', defaulting to JSON")
        _write_json_report(audit_data, output_path)

    logger.info(f"Report saved to: {output_path}")
    return audit_data


def _write_json_report(audit_data: Dict[str, Any], output_path: Path) -> None:
    """Write a JSON report, stripping non-serializable context fields."""
    # Create a clean copy without BeautifulSoup elements in context
    clean_data = _clean_for_json(audit_data)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(clean_data, f, indent=2, default=str)


def _write_text_report(audit_data: Dict[str, Any], output_path: Path) -> None:
    """Write a human-readable text report."""
    lines = []
    summary = audit_data.get("summary", {})

    lines.append("=" * 60)
    lines.append("WCAG 2.1 AA Accessibility Audit Report")
    lines.append("=" * 60)
    lines.append("")

    # Compliance score section
    compliance = audit_data.get("compliance", {})
    if compliance:
        lines.append(f"Compliance Score:    {compliance.get('overall_score', '?')}%")
        lines.append("")
        by_principle = compliance.get("by_principle", {})
        if by_principle:
            lines.append("By principle:")
            for principle in ["Perceivable", "Operable", "Understandable", "Robust"]:
                p_data = by_principle.get(principle, {})
                score = p_data.get("score", 100.0)
                passing = p_data.get("passing", 0)
                total = p_data.get("total", 0)
                lines.append(f"  {principle:20s}: {score:5.1f}%  ({passing}/{total} criteria passing)")
            lines.append("")

        by_criterion = compliance.get("by_criterion", {})
        if by_criterion:
            lines.append("By criterion:")
            for crit_id in sorted(by_criterion.keys()):
                crit_data = by_criterion[crit_id]
                status = "PASS" if crit_data["passed"] else "FAIL"
                failing = crit_data["failing"]
                total = crit_data["total"]
                lines.append(f"  WCAG {crit_id:6s}: {status}  ({failing} failing / {total} total)")
            lines.append("")

    lines.append(f"Total issues:        {summary.get('total_issues', 0)}")
    lines.append(f"Needs remediation:   {summary.get('needs_remediation', 0)}")
    lines.append(f"Compliant:           {summary.get('compliant', 0)}")
    lines.append("")

    severity_counts = summary.get("severity_counts", {})
    lines.append("By severity:")
    for sev in ["critical", "major", "minor", "info"]:
        count = severity_counts.get(sev, 0)
        if count > 0:
            lines.append(f"  {sev:10s}: {count}")
    lines.append("")

    # List issues by principle
    by_principle_issues = audit_data.get("by_principle", {})
    if by_principle_issues:
        for principle in ["Perceivable", "Operable", "Understandable", "Robust"]:
            principle_issues = by_principle_issues.get(principle, [])
            needs_fix = [i for i in principle_issues if i.get("remediation_status") == "needs_remediation"]
            if needs_fix:
                lines.append("-" * 60)
                lines.append(f"{principle.upper()} - ISSUES NEEDING REMEDIATION")
                lines.append("-" * 60)
                for issue in needs_fix:
                    lines.append(
                        f"  [{issue.get('severity', '?').upper():8s}] "
                        f"WCAG {issue.get('wcag_criterion', '?')} - "
                        f"{issue.get('description', 'No description')}"
                    )
                lines.append("")
    else:
        # Fallback to flat list if by_principle not available
        needs_fix = audit_data.get("by_status", {}).get("needs_remediation", [])
        if needs_fix:
            lines.append("-" * 60)
            lines.append("ISSUES NEEDING REMEDIATION")
            lines.append("-" * 60)
            for issue in needs_fix:
                lines.append(
                    f"  [{issue.get('severity', '?').upper():8s}] "
                    f"WCAG {issue.get('wcag_criterion', '?')} - "
                    f"{issue.get('description', 'No description')}"
                )
            lines.append("")

    # List compliant items
    compliant = audit_data.get("by_status", {}).get("compliant", [])
    if compliant:
        lines.append("-" * 60)
        lines.append("COMPLIANT ITEMS")
        lines.append("-" * 60)
        for issue in compliant:
            lines.append(
                f"  [OK      ] "
                f"WCAG {issue.get('wcag_criterion', '?')} - "
                f"{issue.get('description', 'No description')}"
            )
        lines.append("")

    lines.append("=" * 60)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _clean_for_json(obj):
    """Recursively clean an object for JSON serialization."""
    if isinstance(obj, dict):
        clean = {}
        for key, value in obj.items():
            if key == "context" and isinstance(value, dict):
                # Strip HTML snippets and attributes that may have BS4 objects
                clean_ctx = {}
                for ck, cv in value.items():
                    if ck == "attributes":
                        # Convert attribute values to strings
                        clean_ctx[ck] = {
                            ak: str(av) if not isinstance(av, (str, int, float, bool, type(None))) else av
                            for ak, av in (cv or {}).items()
                        }
                    elif ck == "parent" and isinstance(cv, dict):
                        clean_ctx[ck] = _clean_for_json(cv)
                    else:
                        clean_ctx[ck] = str(cv) if not isinstance(cv, (str, int, float, bool, type(None), list, dict)) else cv
                clean[key] = clean_ctx
            else:
                clean[key] = _clean_for_json(value)
        return clean
    elif isinstance(obj, list):
        return [_clean_for_json(item) for item in obj]
    elif isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    else:
        return str(obj)
