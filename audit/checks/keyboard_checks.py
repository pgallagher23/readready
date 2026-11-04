# SPDX-License-Identifier: AGPL-3.0-or-later
"""Keyboard accessibility checks (WCAG 2.1.1, 2.1.2).

2.1.1 Keyboard: All functionality available via keyboard.
2.1.2 No Keyboard Trap: Focus can be moved away from any component
using only a keyboard.
"""

import re

from audit.base_check import AccessibilityCheck

# Elements that are natively keyboard-focusable
NATIVELY_FOCUSABLE = {"a", "button", "input", "select", "textarea", "summary"}

# Event handlers that indicate interactive behavior
MOUSE_ONLY_HANDLERS = {"onmousedown", "onmouseup", "onmouseover", "onmouseout",
                        "onmousemove", "ondblclick"}
KEYBOARD_HANDLERS = {"onkeydown", "onkeyup", "onkeypress", "onfocus", "onblur"}


class KeyboardAccessCheck(AccessibilityCheck):
    """Check that interactive content is keyboard accessible (WCAG 2.1.1)."""

    def check(self) -> None:
        self._found_issue = False
        self._check_tabindex_negative()
        self._check_mouse_only_handlers()
        self._check_onclick_without_keyboard()
        self._check_non_interactive_with_handlers()
        self._check_keyboard_trap_indicators()
        if not self._found_issue:
            self.add_issue(
                "compliant-keyboard-access",
                "2.1.1",
                "minor",
                description="All interactive content is keyboard accessible",
                status="compliant",
            )

    def _check_tabindex_negative(self) -> None:
        """Flag interactive elements with tabindex='-1' that removes
        them from keyboard tab order."""
        elements = self.soup.find_all(attrs={"tabindex": "-1"})
        for elem in elements:
            # tabindex="-1" on non-interactive elements is fine
            # (used for programmatic focus)
            is_interactive = (
                elem.name in NATIVELY_FOCUSABLE
                or elem.get("role") in (
                    "button", "link", "checkbox", "radio", "tab",
                    "menuitem", "option", "switch", "textbox",
                    "combobox", "listbox", "slider", "spinbutton",
                )
                or elem.has_attr("onclick")
            )
            if is_interactive:
                # Links with href="#" and tabindex=-1 are often
                # anchors, not navigation — skip those
                if elem.name == "a" and not elem.get("href", "").strip("#"):
                    continue
                self._found_issue = True
                self.add_issue(
                    "interactive-tabindex-negative",
                    "2.1.1",
                    "critical",
                    element=elem,
                    description=(
                        f"Interactive element <{elem.name}> has tabindex='-1', "
                        f"making it unreachable via keyboard"
                    ),
                )

    def _check_mouse_only_handlers(self) -> None:
        """Flag elements with mouse-only event handlers but no keyboard
        equivalent."""
        for handler in MOUSE_ONLY_HANDLERS:
            elements = self.soup.find_all(attrs={handler: True})
            for elem in elements:
                has_keyboard = any(
                    elem.has_attr(kb) for kb in KEYBOARD_HANDLERS
                )
                has_click = elem.has_attr("onclick")
                is_focusable = (
                    elem.name in NATIVELY_FOCUSABLE
                    or elem.has_attr("tabindex")
                )

                if not has_keyboard and not has_click and not is_focusable:
                    self._found_issue = True
                    self.add_issue(
                        "mouse-only-handler",
                        "2.1.1",
                        "major",
                        element=elem,
                        description=(
                            f"Element <{elem.name}> has mouse handler "
                            f"'{handler}' with no keyboard equivalent"
                        ),
                    )

    def _check_onclick_without_keyboard(self) -> None:
        """Flag non-interactive elements with onclick but no keyboard
        access."""
        elements = self.soup.find_all(attrs={"onclick": True})
        for elem in elements:
            if elem.name in NATIVELY_FOCUSABLE:
                continue

            has_role = elem.get("role") in (
                "button", "link", "checkbox", "radio", "tab",
                "menuitem", "option", "switch",
            )
            has_tabindex = elem.has_attr("tabindex")
            has_keyboard = any(
                elem.has_attr(kb) for kb in KEYBOARD_HANDLERS
            )

            if not has_role and not has_tabindex and not has_keyboard:
                self._found_issue = True
                self.add_issue(
                    "onclick-not-keyboard-accessible",
                    "2.1.1",
                    "critical",
                    element=elem,
                    description=(
                        f"Element <{elem.name}> has onclick handler but is "
                        f"not keyboard accessible (add role, tabindex, and "
                        f"keydown handler)"
                    ),
                )
            elif has_tabindex and not has_keyboard and not has_role:
                self._found_issue = True
                self.add_issue(
                    "onclick-missing-keyboard-handler",
                    "2.1.1",
                    "major",
                    element=elem,
                    description=(
                        f"Element <{elem.name}> is focusable with onclick "
                        f"but has no keyboard event handler"
                    ),
                )

    def _check_non_interactive_with_handlers(self) -> None:
        """Flag non-interactive elements acting as controls without
        proper roles."""
        for elem in self.soup.find_all(
            attrs={"onclick": True, "tabindex": True}
        ):
            if elem.name in NATIVELY_FOCUSABLE:
                continue
            if not elem.get("role"):
                self._found_issue = True
                self.add_issue(
                    "interactive-missing-role",
                    "2.1.1",
                    "major",
                    element=elem,
                    description=(
                        f"Element <{elem.name}> acts as a control (has "
                        f"onclick and tabindex) but is missing an ARIA role"
                    ),
                )

    def _check_keyboard_trap_indicators(self) -> None:
        """Check for common patterns that may cause keyboard traps
        (WCAG 2.1.2)."""
        # Detect elements that capture keyboard events and may trap focus
        elements = self.soup.find_all(attrs={"onkeydown": True})
        for elem in elements:
            handler = elem.get("onkeydown", "")
            # Flag if the handler uses preventDefault broadly
            # (not on specific keys)
            if "preventDefault" in handler and "event.key" not in handler:
                self._found_issue = True
                self.add_issue(
                    "potential-keyboard-trap",
                    "2.1.1",
                    "major",
                    element=elem,
                    description=(
                        f"Element <{elem.name}> has onkeydown handler with "
                        f"preventDefault that may trap keyboard focus"
                    ),
                )

        # Detect tabindex on container elements that could trap focus
        for elem in self.soup.find_all(
            attrs={"tabindex": "0", "onkeydown": True}
        ):
            if elem.name in ("div", "section", "article", "span"):
                focusable_children = elem.find_all(
                    lambda e: e.name in NATIVELY_FOCUSABLE
                    or e.has_attr("tabindex")
                )
                if focusable_children:
                    self._found_issue = True
                    self.add_issue(
                        "container-may-trap-focus",
                        "2.1.1",
                        "minor",
                        element=elem,
                        description=(
                            f"Focusable container <{elem.name}> with "
                            f"onkeydown handler contains focusable children "
                            f"\u2014 verify focus is not trapped"
                        ),
                    )
