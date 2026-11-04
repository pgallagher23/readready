# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolving an issue to its element, by id where possible."""
from bs4 import BeautifulSoup

from remediate.element_resolver import (
    build_id_index,
    get_id_index,
    invalidate_id_index,
    resolve_issue_element,
)


def _soup(html):
    return BeautifulSoup(html, "html.parser")


def _issue(path, **ctx):
    return {"location": {"path": path}, "context": ctx}


def test_resolves_by_id_from_the_last_path_segment():
    s = _soup('<table><tr><th data-a11y-id="a11y-e00042">target</th></tr></table>')
    el = resolve_issue_element(s, _issue('th[data-a11y-id="a11y-e00042"]'))
    assert el is not None and el.get_text() == "target"


def test_ancestor_id_does_not_hijack_resolution():
    """A path like 'div[data-a11y-id="..."] > table > tr > th' names the th, not the div.

    _get_element_path walks upward and stops at the first ancestor with a
    stamped id, so the id segment can land on an ancestor. Resolving by that
    id returns the wrong element -- it must fall through to the CSS path
    instead.
    """
    s = _soup(
        '<div data-a11y-id="a11y-e00000">'
        "<table><tr><th>target</th></tr></table>"
        "</div>"
    )
    el = resolve_issue_element(
        s, _issue('div[data-a11y-id="a11y-e00000"] > table > tr > th')
    )
    assert el is not None
    assert el.name == "th", f"resolved to <{el.name}>, expected <th>"


def test_falls_back_to_positional_path_when_no_id():
    s = _soup("<body><table><tr><th>a</th></tr></table>"
              "<table><tr><th>b</th></tr></table></body>")
    el = resolve_issue_element(s, _issue("body > table:nth-of-type(2) > tr > th"))
    assert el is not None and el.get_text() == "b"


def test_returns_none_for_unusable_path():
    s = _soup("<table></table>")
    assert resolve_issue_element(s, _issue("")) is None
    assert resolve_issue_element(s, _issue("Unknown path")) is None


def test_returns_none_for_malformed_selector():
    s = _soup("<table></table>")
    assert resolve_issue_element(s, _issue("> > >")) is None


def test_index_lookup_agrees_with_uncached_lookup():
    s = _soup('<table><tr><th data-a11y-id="a11y-e00007">x</th></tr></table>')
    issue = _issue('th[data-a11y-id="a11y-e00007"]')
    assert resolve_issue_element(s, issue) is resolve_issue_element(
        s, issue, build_id_index(s))


def test_index_is_cached_and_invalidatable():
    s = _soup('<table id="a11y-e00000"><tr><th id="a11y-e00001">x</th></tr></table>')
    first = get_id_index(s)
    assert get_id_index(s) is first, "index was rebuilt instead of cached"
    invalidate_id_index(s)
    assert get_id_index(s) is not first, "index survived invalidation"


def test_stale_index_does_not_return_a_removed_element():
    """A strategy that removes an element leaves it in the index.

    Returning it would hand the caller a detached node that no write-back
    would ever include. The index must be self-correcting so no caller has to
    remember to invalidate it.
    """
    s = _soup('<table><tr><th data-a11y-id="a11y-e00001">x</th></tr></table>')
    index = build_id_index(s)
    s.find("th").decompose()
    assert resolve_issue_element(
        s, _issue('th[data-a11y-id="a11y-e00001"]'), index
    ) is None


def test_element_added_after_the_index_is_still_found():
    """An index miss must fall back to a scan, not report absence."""
    s = _soup("<table></table>")
    index = build_id_index(s)
    new = s.new_tag("caption")
    new["data-a11y-id"] = "brand-new"
    s.find("table").append(new)
    found = resolve_issue_element(
        s, _issue('caption[data-a11y-id="brand-new"]'), index
    )
    assert found is not None and found.name == "caption"


def test_index_path_does_not_fall_through_to_css_select(monkeypatch):
    """An id-addressed path must resolve via the index, not soup.select.

    `[data-a11y-id="..."]` is valid CSS, so soup.select() can resolve it
    correctly too -- the index and the fallback are correctness-equivalent
    by construction, both return the same element. That means a plain
    behavioural test ("assert the right element comes back") cannot tell
    whether the answer came from an O(1) dict lookup or an O(n) tree scan.
    The actual contract this module exists to provide -- the reason the
    index exists at all -- is *which code path ran*, and that is only
    observable through instrumentation, not through the return value. This
    test stubs soup.select to raise if it is ever called, so a regression
    that quietly starts scanning again (e.g. the id branch's index check
    getting deleted or short-circuited) fails loudly here instead of
    passing every other test in this file undetected.
    """
    s = _soup('<table><tr><th data-a11y-id="a11y-e00042">target</th></tr></table>')
    index = build_id_index(s)

    def _boom(*_args, **_kwargs):
        raise AssertionError("soup.select() was called -- fast path did not fire")

    monkeypatch.setattr(s, "select", _boom)

    el = resolve_issue_element(s, _issue('th[data-a11y-id="a11y-e00042"]'), index)
    assert el is not None and el.get_text() == "target"


def test_positional_path_does_reach_css_select(monkeypatch):
    """Companion to the id-path test above: pins the other direction.

    Without this, a future reader could "fix" the index path so eagerly
    that it swallows positional paths too, and nothing in this file would
    notice -- the positional fallback is a permanent code path (legacy
    documents, remediation-created elements, tags outside STAMPED_TAGS),
    not dead code being phased out.
    """
    calls = []
    s = _soup("<body><table><tr><th>a</th></tr></table>"
              "<table><tr><th>b</th></tr></table></body>")
    real_select = s.select

    def _spy(selector, *args, **kwargs):
        calls.append(selector)
        return real_select(selector, *args, **kwargs)

    monkeypatch.setattr(s, "select", _spy)

    el = resolve_issue_element(s, _issue("body > table:nth-of-type(2) > tr > th"))
    assert el is not None and el.get_text() == "b"
    assert calls, "positional path should reach soup.select()"
