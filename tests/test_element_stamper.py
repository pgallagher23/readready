# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stamping gives audited elements a name so audit paths stop being positional."""
from bs4 import BeautifulSoup

from converter.element_stamper import (
    A11Y_ID_ATTR,
    A11Y_ID_PREFIX,
    STAMPED_TAGS,
    stamp_element_ids,
)


def _soup(html):
    return BeautifulSoup(html, "html.parser")


def test_stamps_audited_tags():
    s = _soup("<table><tr><th>a</th><th>b</th></tr></table>")
    assert stamp_element_ids(s) == 3          # table + 2 th
    assert all(el.get(A11Y_ID_ATTR, "").startswith(A11Y_ID_PREFIX)
               for el in s.find_all(["table", "th"]))


def test_does_not_stamp_unaudited_tags():
    """td/tr/p never appear as audit leaf tags; stamping them is pure size cost."""
    s = _soup("<table><tr><td>a</td></tr></table><p>x</p>")
    stamp_element_ids(s)
    assert s.find("td").get(A11Y_ID_ATTR) is None
    assert s.find("tr").get(A11Y_ID_ATTR) is None
    assert s.find("p").get(A11Y_ID_ATTR) is None


def test_never_overwrites_an_existing_data_a11y_id():
    s = _soup(f'<table {A11Y_ID_ATTR}="authors-table"><tr><th>a</th></tr></table>')
    stamp_element_ids(s)
    assert s.find("table")[A11Y_ID_ATTR] == "authors-table"


def test_ids_are_unique():
    s = _soup("<table>" + "<tr><th>x</th></tr>" * 50 + "</table>")
    stamp_element_ids(s)
    ids = [el[A11Y_ID_ATTR] for el in s.find_all(attrs={A11Y_ID_ATTR: True})]
    assert len(ids) == len(set(ids))


def test_does_not_collide_with_existing_stamps():
    """A pre-existing data-a11y-id that looks like ours must not be duplicated."""
    s = _soup(f'<table {A11Y_ID_ATTR}="{A11Y_ID_PREFIX}00000"><tr><th>a</th></tr></table>')
    stamp_element_ids(s)
    ids = [el[A11Y_ID_ATTR] for el in s.find_all(attrs={A11Y_ID_ATTR: True})]
    assert len(ids) == len(set(ids)), f"collision: {ids}"


def test_is_idempotent():
    s = _soup("<table><tr><th>a</th></tr></table>")
    stamp_element_ids(s)
    before = str(s)
    assert stamp_element_ids(s) == 0
    assert str(s) == before


def test_stamped_tags_excludes_the_expensive_ones():
    """Guard the measured decision: td/tr/p/h1/h3-h6 cost +37.8% for no coverage."""
    for tag in ("td", "tr", "p", "h1", "h3", "h4", "h5", "h6"):
        assert tag not in STAMPED_TAGS


def test_never_writes_the_id_attribute():
    """Regression guard: the addressing attribute is data-a11y-id, never id.

    id is not an inert addressing slot -- audit/checks/table_checks.py's
    table-missing-headers-id check (WCAG 1.3.1, major) treats any th[id] as
    evidence of a header/cell association, and a separate check flags
    duplicate-id. Stamping id therefore silently suppresses real findings:
    measured on one document, stamping id dropped table-missing-headers-id
    findings from 217 to 1 while adding zero real headers= associations on
    the td cells. data-a11y-id is a private namespace no WCAG check reads,
    so this must stay false for every stamped element, and stamping must
    never introduce a plain id attribute as a side effect.
    """
    s = _soup(
        "<table><tr><th>a</th><th>b</th></tr></table>"
        '<img src="x.png"><figure></figure><a href="#">link</a><h2>heading</h2>'
    )
    stamp_element_ids(s)
    for el in s.find_all(list(STAMPED_TAGS)):
        assert el.get("id") is None, f"stamper wrote a plain id attribute on <{el.name}>"
