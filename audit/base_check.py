# SPDX-License-Identifier: AGPL-3.0-or-later
# Portions derived from Content Accessibility Utility on AWS
# (https://github.com/awslabs/content-accessibility-utility-on-aws),
# Copyright 2025 Amazon.com, Inc. or its affiliates, Apache-2.0.
# Modified by the ReadReady project; see NOTICE.
"""Base classes for accessibility checks."""

import logging
from typing import Callable, Optional, List
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)


class AccessibilityCheck:
    """Base class for all accessibility checks."""

    def __init__(self, soup: BeautifulSoup, add_issue_callback: Callable):
        self.soup = soup
        self.add_issue = add_issue_callback

    def check(self) -> None:
        raise NotImplementedError("Subclasses must implement check()")

    def find_elements(self, selector: str) -> List[Tag]:
        try:
            return self.soup.select(selector)
        except Exception as e:
            logger.error(f"Error finding elements with selector '{selector}': {e}")
            return []

    def element_exists(self, selector: str) -> bool:
        return len(self.find_elements(selector)) > 0

    def get_element_text(self, element: Tag) -> str:
        try:
            return element.get_text(strip=True)
        except Exception:
            return ""

    def get_attribute(self, element: Tag, attribute: str) -> Optional[str]:
        try:
            return element.get(attribute)
        except Exception:
            return None
