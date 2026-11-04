# SPDX-License-Identifier: AGPL-3.0-or-later
"""Non-text contrast accessibility checks."""

import re
from typing import Tuple, Optional

from audit.base_check import AccessibilityCheck


class NonTextContrastCheck(AccessibilityCheck):
    """Check non-text contrast ratios for UI components (WCAG 1.4.11)."""

    MIN_CONTRAST = 3.0

    def check(self) -> None:
        self._found_issue = False
        self._check_input_border_contrast()
        self._check_svg_contrast()
        if not self._found_issue:
            self.add_issue(
                "compliant-non-text-contrast",
                "1.4.11",
                "minor",
                description="Non-text visual elements meet minimum contrast requirements",
                status="compliant",
            )

    def _check_input_border_contrast(self) -> None:
        form_controls = self.soup.find_all(["input", "select", "textarea"])

        for control in form_controls:
            input_type = control.get("type", "text")
            if input_type in ("hidden", "submit", "reset", "button"):
                continue

            style = control.get("style", "")
            border_color = self._extract_border_color(style)
            bg_color = self._get_background_color(control)

            if border_color and bg_color:
                ratio = self._calculate_contrast_ratio(border_color, bg_color)
                if ratio < self.MIN_CONTRAST:
                    self._found_issue = True
                    self.add_issue(
                        "insufficient-non-text-contrast",
                        "1.4.11",
                        "major",
                        element=control,
                        description=f"Form input border contrast ratio is {ratio:.2f}:1 (minimum required: {self.MIN_CONTRAST}:1)",
                        status="needs_remediation",
                    )

    def _check_svg_contrast(self) -> None:
        svgs = self.soup.find_all("svg")
        for svg in svgs:
            fill = svg.get("fill", "")
            stroke = svg.get("stroke", "")
            color = fill or stroke

            if not color or color == "none" or color == "currentColor":
                continue

            color_hex = self._normalize_color(color)
            if color_hex:
                bg_color = self._get_background_color(svg)
                if bg_color:
                    ratio = self._calculate_contrast_ratio(color_hex, bg_color)
                    if ratio < self.MIN_CONTRAST:
                        self._found_issue = True
                        self.add_issue(
                            "insufficient-non-text-contrast",
                            "1.4.11",
                            "major",
                            element=svg,
                            description=f"SVG graphical element contrast ratio is {ratio:.2f}:1 (minimum required: {self.MIN_CONTRAST}:1)",
                            status="needs_remediation",
                        )

    def _extract_border_color(self, style: str) -> Optional[str]:
        match = re.search(
            r"border(?:-color)?\s*:\s*[^;]*?(#[0-9a-fA-F]{3,6}|rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\))",
            style,
        )
        if match:
            return self._normalize_color(match.group(1))
        return None

    def _get_background_color(self, element) -> str:
        if element.get("style"):
            bg_match = re.search(
                r"background-color:\s*(#[0-9a-fA-F]{3,6}|rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\))",
                element["style"],
            )
            if bg_match:
                return self._normalize_color(bg_match.group(1))

        parent = element.parent
        while parent and parent.name != "html":
            if parent.get("style"):
                bg_match = re.search(
                    r"background-color:\s*(#[0-9a-fA-F]{3,6}|rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\))",
                    parent["style"],
                )
                if bg_match:
                    return self._normalize_color(bg_match.group(1))
            parent = parent.parent
        return "#FFFFFF"

    def _normalize_color(self, color: str) -> Optional[str]:
        if not color:
            return None
        if color.startswith("#"):
            if len(color) == 4:
                r, g, b = color[1], color[2], color[3]
                return f"#{r}{r}{g}{g}{b}{b}".upper()
            if len(color) == 7:
                return color.upper()
            return None

        rgb_match = re.match(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", color)
        if rgb_match:
            r, g, b = map(int, rgb_match.groups())
            return f"#{r:02X}{g:02X}{b:02X}"
        return None

    def _calculate_contrast_ratio(self, color1: str, color2: str) -> float:
        rgb1 = self._hex_to_rgb(color1)
        rgb2 = self._hex_to_rgb(color2)
        if not rgb1 or not rgb2:
            return 21.0
        l1 = self._relative_luminance(rgb1)
        l2 = self._relative_luminance(rgb2)
        if l1 > l2:
            return (l1 + 0.05) / (l2 + 0.05)
        return (l2 + 0.05) / (l1 + 0.05)

    def _hex_to_rgb(self, hex_color: str) -> Optional[Tuple[int, int, int]]:
        try:
            hex_color = hex_color.lstrip("#")
            return tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
        except (ValueError, IndexError):
            return None

    def _relative_luminance(self, rgb: Tuple[int, int, int]) -> float:
        r, g, b = rgb
        r = self._gamma_correct(r / 255)
        g = self._gamma_correct(g / 255)
        b = self._gamma_correct(b / 255)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def _gamma_correct(self, value: float) -> float:
        if value <= 0.03928:
            return value / 12.92
        return ((value + 0.055) / 1.055) ** 2.4
