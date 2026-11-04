# SPDX-License-Identifier: AGPL-3.0-or-later
"""Apache-2.0 obligations for code derived from Content Accessibility Utility on AWS.

Redistributing modified copies requires shipping the license text, keeping the
copyright notice with the derived files, and marking them as modified.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DERIVED_FILES = [
    "audit/auditor.py",
    "audit/base_check.py",
    "audit/context_collector.py",
    "audit/standards.py",
    "audit/checks/__init__.py",
    "audit/checks/color_contrast_checks.py",
    "audit/checks/form_checks.py",
    "audit/checks/heading_checks.py",
    "audit/checks/image_checks.py",
    "audit/checks/link_checks.py",
    "audit/checks/structure_checks.py",
    "audit/checks/table_checks.py",
    "remediate/remediation_manager.py",
    "remediate/strategies/color_contrast_remediation.py",
    "remediate/strategies/document_structure_remediation.py",
    "remediate/strategies/figure_remediation.py",
    "remediate/strategies/form_remediation.py",
    "remediate/strategies/heading_remediation.py",
    "remediate/strategies/landmark_remediation.py",
    "remediate/strategies/link_remediation.py",
    "remediate/strategies/table_remediation.py",
]

COPYRIGHT = "Copyright 2025 Amazon.com, Inc. or its affiliates"


def test_apache_license_text_is_shipped():
    text = (ROOT / "LICENSES" / "Apache-2.0.txt").read_text()
    assert "Apache License" in text and "Version 2.0" in text
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in text


def test_notice_credits_the_upstream_utility():
    notice = (ROOT / "NOTICE").read_text()
    assert "Content Accessibility Utility on AWS" in notice
    assert COPYRIGHT in notice
    assert "Apache-2.0" in notice
    assert "LICENSES/Apache-2.0.txt" in notice


def test_readme_acknowledges_both_projects():
    readme = (ROOT / "README.md").read_text()
    assert "awslabs/content-accessibility-utility-on-aws" in readme
    assert "ASUCICREPO/PDF_Accessibility" in readme


def test_every_derived_file_keeps_the_notice_and_marks_modification():
    for rel in DERIVED_FILES:
        head = "\n".join((ROOT / rel).read_text().splitlines()[:8])
        assert head.startswith("# SPDX-License-Identifier: AGPL-3.0-or-later"), rel
        assert COPYRIGHT in head, rel
        assert "Apache-2.0" in head, rel
        assert "modified" in head.lower(), rel
