# SPDX-License-Identifier: AGPL-3.0-or-later
"""Focus order and focus visibility accessibility checks.

WCAG 2.4.3 Focus Order: Focusable components receive focus in an order
that preserves meaning and operability.

WCAG 2.4.7 Focus Visible: Any keyboard operable user interface has a
mode of operation where the keyboard focus indicator is visible.
"""

import re

from audit.base_check import AccessibilityCheck

# Elements that are natively keyboard-focusable
NATIVELY_FOCUSABLE = {"a", "button", "input", "select", "textarea", "summary"}


class FocusOrderCheck(AccessibilityCheck):
    """Check that focus order preserves meaning and operability
    (WCAG 2.4.3)."""

    def check(self) -> None:
        self._found_issue = False
        self._check_positive_tabindex()
        self._check_focus_order_vs_dom()
        if not self._found_issue:
            self.add_issue(
                "compliant-focus-order",
                "2.4.3",
                "minor",
                description="Focus order preserves meaning and operability",
                status="compliant",
            )

    def _check_positive_tabindex(self) -> None:
        """Positive tabindex values override natural DOM focus order,
        which can cause confusion."""
        elements = self.soup.find_all(attrs={"tabindex": True})
        for elem in elements:
            try:
                tabindex = int(elem.get("tabindex", "0"))
            except (ValueError, TypeError):
                continue

            if tabindex > 0:
                self._found_issue = True
                self.add_issue(
                    "positive-tabindex",
                    "2.4.3",
                    "major",
                    element=elem,
                    description=(
                        f"Element <{elem.name}> has tabindex='{tabindex}' "
                        f"which overrides natural focus order. Use tabindex="
                        f"'0' or restructure the DOM instead."
                    ),
                )

    def _check_focus_order_vs_dom(self) -> None:
        """Check for CSS patterns that may cause visual order to
        diverge from DOM order, breaking focus order."""
        # CSS 'order' property changes visual order but not tab order
        elements = self.soup.find_all(style=re.compile(r"(?:^|[\s;])order\s*:", re.I))
        for elem in elements:
            style = elem.get("style", "")
            match = re.search(r"order\s*:\s*(-?\d+)", style, re.I)
            if match:
                order_val = int(match.group(1))
                if order_val != 0:
                    # Check if element or its children are focusable
                    has_focusable = (
                        elem.name in NATIVELY_FOCUSABLE
                        or elem.has_attr("tabindex")
                        or elem.find(
                            lambda e: e.name in NATIVELY_FOCUSABLE
                            or e.has_attr("tabindex")
                        )
                    )
                    if has_focusable:
                        self._found_issue = True
                        self.add_issue(
                            "css-order-focus-mismatch",
                            "2.4.3",
                            "major",
                            element=elem,
                            description=(
                                f"Element <{elem.name}> uses CSS "
                                f"order:{order_val} which may cause focus "
                                f"order to differ from visual order"
                            ),
                        )

        # flex-direction: row-reverse / column-reverse
        reverse_elements = self.soup.find_all(
            style=re.compile(r"flex-direction\s*:\s*\w+-reverse", re.I)
        )
        for elem in reverse_elements:
            has_focusable = elem.find(
                lambda e: e.name in NATIVELY_FOCUSABLE
                or e.has_attr("tabindex")
            )
            if has_focusable:
                self._found_issue = True
                self.add_issue(
                    "flex-reverse-focus-mismatch",
                    "2.4.3",
                    "major",
                    element=elem,
                    description=(
                        f"Element <{elem.name}> uses flex-direction reverse "
                        f"which may cause focus order to differ from visual "
                        f"order"
                    ),
                )

        # Check <style> blocks for global order/reverse patterns
        for style_tag in self.soup.find_all("style"):
            css_text = style_tag.string or ""
            if re.search(r"(?:^|[\s;{])order\s*:\s*-?[1-9]", css_text, re.I | re.M):
                self._found_issue = True
                self.add_issue(
                    "stylesheet-css-order",
                    "2.4.3",
                    "minor",
                    element=style_tag,
                    description=(
                        "Stylesheet uses CSS 'order' property which may "
                        "cause focus order to differ from visual order"
                    ),
                )
            if re.search(
                r"flex-direction\s*:\s*\w+-reverse", css_text, re.I
            ):
                self._found_issue = True
                self.add_issue(
                    "stylesheet-flex-reverse",
                    "2.4.3",
                    "minor",
                    element=style_tag,
                    description=(
                        "Stylesheet uses flex-direction reverse which may "
                        "cause focus order to differ from visual order"
                    ),
                )


class FocusVisibleCheck(AccessibilityCheck):
    """Check that focus indicators are visible (WCAG 2.4.7)."""

    def check(self) -> None:
        self._found_issue = False
        self._check_inline_outline_removal()
        self._check_stylesheet_outline_removal()
        if not self._found_issue:
            self.add_issue(
                "compliant-focus-visible",
                "2.4.7",
                "minor",
                description="Focus indicators are visible on interactive elements",
                status="compliant",
            )

    def _check_inline_outline_removal(self) -> None:
        """Flag elements with inline styles that suppress focus
        indicators."""
        outline_pattern = re.compile(
            r"outline\s*:\s*(none|0(?:px)?)\b", re.I
        )
        elements = self.soup.find_all(style=outline_pattern)
        for elem in elements:
            style = elem.get("style", "")
            # Check if there's a replacement indicator (box-shadow,
            # border, background change)
            has_replacement = bool(re.search(
                r"(box-shadow|border\s*:|border-color|"
                r"background-color)\s*:", style, re.I
            ))
            if not has_replacement:
                self._found_issue = True
                self.add_issue(
                    "focus-outline-removed",
                    "2.4.7",
                    "major",
                    element=elem,
                    description=(
                        f"Element <{elem.name}> has 'outline: none' without "
                        f"a visible replacement focus indicator"
                    ),
                )

    def _check_stylesheet_outline_removal(self) -> None:
        """Check <style> blocks for rules that suppress focus
        indicators."""
        for style_tag in self.soup.find_all("style"):
            css_text = style_tag.string or ""
            if not css_text.strip():
                continue

            # Look for :focus rules that remove outline
            focus_blocks = re.finditer(
                r"([^{}]+):focus\s*\{([^}]*)\}", css_text, re.I
            )
            for match in focus_blocks:
                selector = match.group(1).strip()
                declarations = match.group(2)

                removes_outline = bool(re.search(
                    r"outline\s*:\s*(none|0(?:px)?)", declarations, re.I
                ))
                if not removes_outline:
                    continue

                has_replacement = bool(re.search(
                    r"(box-shadow|border\s*:|border-color|"
                    r"background-color|text-decoration)\s*:",
                    declarations, re.I
                ))

                if not has_replacement:
                    self._found_issue = True
                    self.add_issue(
                        "stylesheet-focus-outline-removed",
                        "2.4.7",
                        "critical",
                        element=style_tag,
                        description=(
                            f"CSS rule '{selector}:focus' removes outline "
                            f"without a visible replacement focus indicator"
                        ),
                    )

            # Check for blanket outline removal: * { outline: none }
            blanket_match = re.search(
                r"\*\s*\{[^}]*outline\s*:\s*(none|0(?:px)?)[^}]*\}",
                css_text, re.I
            )
            if blanket_match:
                self._found_issue = True
                self.add_issue(
                    "blanket-outline-removal",
                    "2.4.7",
                    "critical",
                    element=style_tag,
                    description=(
                        "Global CSS rule '* { outline: none }' removes "
                        "focus indicators from all elements"
                    ),
                )

            # Check for a:focus, button:focus, input:focus etc.
            for tag in ("a", "button", "input", "select", "textarea"):
                pattern = re.compile(
                    rf"{tag}\s*:\s*focus\s*\{{([^}}]*)\}}", re.I
                )
                for m in pattern.finditer(css_text):
                    decls = m.group(1)
                    if re.search(
                        r"outline\s*:\s*(none|0(?:px)?)", decls, re.I
                    ) and not re.search(
                        r"(box-shadow|border\s*:|border-color)\s*:",
                        decls, re.I
                    ):
                        self._found_issue = True
                        self.add_issue(
                            "element-focus-outline-removed",
                            "2.4.7",
                            "major",
                            element=style_tag,
                            description=(
                                f"CSS rule '{tag}:focus' removes outline "
                                f"without a visible replacement"
                            ),
                        )
