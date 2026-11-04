# SPDX-License-Identifier: AGPL-3.0-or-later
# Portions derived from Content Accessibility Utility on AWS
# (https://github.com/awslabs/content-accessibility-utility-on-aws),
# Copyright 2025 Amazon.com, Inc. or its affiliates, Apache-2.0.
# Modified by the ReadReady project; see NOTICE.
"""Accessibility check classes."""

from audit.checks.heading_checks import (
    HeadingHierarchyCheck,
    HeadingContentCheck,
    DocumentTitleCheck,
    FlatHeadingNestingCheck,
)
from audit.checks.structure_checks import (
    DocumentLanguageCheck,
    MainLandmarkCheck,
    SkipLinkCheck,
    LandmarksCheck,
)
from audit.checks.image_checks import AltTextCheck, FigureStructureCheck
from audit.checks.link_checks import LinkTextCheck, NewWindowLinkCheck
from audit.checks.table_checks import TableHeaderCheck, TableStructureCheck, TableRegularityCheck, PipeDelimitedDataCheck
from audit.checks.color_contrast_checks import ColorContrastCheck
from audit.checks.form_checks import (
    FormLabelCheck,
    FormRequiredFieldCheck,
    FormFieldsetCheck,
)
from audit.checks.reading_order_checks import ReadingOrderCheck
from audit.checks.color_use_checks import ColorUseCheck
from audit.checks.resize_text_checks import ResizeTextCheck
from audit.checks.images_of_text_checks import ImagesOfTextCheck
from audit.checks.reflow_checks import ReflowCheck
from audit.checks.non_text_contrast_checks import NonTextContrastCheck
from audit.checks.text_spacing_checks import TextSpacingCheck
from audit.checks.language_parts_checks import LanguageOfPartsCheck
from audit.checks.parsing_checks import HTMLParsingCheck
from audit.checks.input_purpose_checks import InputPurposeCheck
from audit.checks.name_role_value_checks import NameRoleValueCheck
from audit.checks.keyboard_checks import KeyboardAccessCheck
from audit.checks.focus_checks import FocusOrderCheck, FocusVisibleCheck
from audit.checks.error_identification_checks import ErrorIdentificationCheck
from audit.checks.formula_checks import FormulaAltTextCheck

__all__ = [
    "HeadingHierarchyCheck",
    "HeadingContentCheck",
    "DocumentTitleCheck",
    "FlatHeadingNestingCheck",
    "DocumentLanguageCheck",
    "MainLandmarkCheck",
    "SkipLinkCheck",
    "LandmarksCheck",
    "AltTextCheck",
    "FigureStructureCheck",
    "LinkTextCheck",
    "NewWindowLinkCheck",
    "TableHeaderCheck",
    "TableStructureCheck",
    "TableRegularityCheck",
    "PipeDelimitedDataCheck",
    "ColorContrastCheck",
    "FormLabelCheck",
    "FormRequiredFieldCheck",
    "FormFieldsetCheck",
    "ReadingOrderCheck",
    "ColorUseCheck",
    "ResizeTextCheck",
    "ImagesOfTextCheck",
    "ReflowCheck",
    "NonTextContrastCheck",
    "TextSpacingCheck",
    "LanguageOfPartsCheck",
    "HTMLParsingCheck",
    "InputPurposeCheck",
    "NameRoleValueCheck",
    "KeyboardAccessCheck",
    "FocusOrderCheck",
    "FocusVisibleCheck",
    "ErrorIdentificationCheck",
    "FormulaAltTextCheck",
]
