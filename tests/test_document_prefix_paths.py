# SPDX-License-Identifier: AGPL-3.0-or-later
"""The auditor's '[document] > ' path prefix, and the strategies that consume it.

_get_element_path walks up to the soup root, whose name is the literal string
'[document]'. It only stops early at an ancestor carrying a data-a11y-id or an
id, so *every* fully positional path it emits starts '[document] > '.

That prefix is not the CSS it looks like: '[document]' parses as an
attribute-presence selector for an attribute named "document", which no element
has. Passing such a path straight to soup.select() therefore raises nothing and
matches nothing -- it fails silently.

Fourteen strategies did exactly that until the resolver consolidation, so their
positional fallback never once resolved an element; the per-strategy attribute
tail behind it quietly absorbed the miss, which is why no test caught it. These
tests pin both halves: that the auditor still emits the prefix, and that every
consumer strips it.
"""
import importlib

import pytest
from bs4 import BeautifulSoup

from audit.auditor import AccessibilityAuditor
from remediate.element_resolver import get_id_index, resolve_issue_element

# The fourteen migrated in Task 6, plus the two that already stripped the
# prefix themselves. All sixteen must resolve a '[document] > ' path.
STRATEGY_MODULES = [
    "color_contrast_remediation",
    "color_link_remediation",
    "error_identification_remediation",
    "focus_remediation",
    "form_extended_remediation",
    "input_purpose_remediation",
    "keyboard_remediation",
    "language_parts_remediation",
    "name_role_value_remediation",
    "reading_order_remediation",
    "reflow_remediation",
    "resize_text_remediation",
    "text_spacing_remediation",
    "widget_remediation",
]

# A decoy precedes the target and carries the same attributes. A strategy that
# falls through to its attribute tail lands on the decoy (or on nothing, for
# tails that do not search 'class'); only honouring the positional path finds
# the target. Without the decoy this test would pass on the broken code.
_HTML = (
    "<html><body>"
    '<p class="c" style="color:#777">decoy</p>'
    '<p class="c" style="color:#777">target</p>'
    "</body></html>"
)
_PATH = "[document] > html > body > p:nth-of-type(2)"
_ISSUE = {
    "location": {"path": _PATH},
    "context": {
        "element_name": "p",
        "attributes": {"class": "c", "style": "color:#777"},
    },
}


def _soup():
    return BeautifulSoup(_HTML, "html.parser")


def test_auditor_still_emits_the_document_prefix():
    """Pins the producer half. If this ever fails, the stripping can go."""
    soup = _soup()
    target = soup.find_all("p")[1]
    path = AccessibilityAuditor()._get_element_path(target)
    assert path.startswith("[document] > "), path


def test_the_prefix_fails_silently_rather_than_raising():
    """Why the bug was invisible: no exception, no match, no log line."""
    soup = _soup()
    assert soup.select(_PATH) == []


def test_resolver_strips_the_prefix():
    soup = _soup()
    el = resolve_issue_element(soup, _ISSUE, id_index=get_id_index(soup))
    assert el is not None, "resolver returned None for a '[document] > ' path"
    assert el.get_text() == "target", f"resolved to the {el.get_text()!r} element"


@pytest.mark.parametrize("module_name", STRATEGY_MODULES)
def test_strategy_resolves_a_document_prefixed_path(module_name):
    module = importlib.import_module(f"remediate.strategies.{module_name}")
    soup = _soup()
    el = module._find_issue_element(soup, _ISSUE)
    assert el is not None, f"{module_name} returned None"
    assert el.get_text() == "target", (
        f"{module_name} resolved to the {el.get_text()!r} element -- it fell "
        "through to its attribute tail instead of honouring the path"
    )
