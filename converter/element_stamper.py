# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stamp stable ids on audited elements so audit paths stop being positional.

The auditor's ``_get_element_path`` gains a branch that prefers this attribute
and short-circuits on finding one; that branch never fires today because
converted HTML carries no stamps at all. Stamping makes it fire, which turns a
positional ``:nth-of-type(N)`` path -- O(N) to resolve, with N rising through
the document -- into a direct reference.

Stamping happens in the converter rather than the audit because the remediator
re-reads the HTML from disk and the auditor never writes it, so an in-memory
stamp made during the audit would be discarded before remediation saw it.

The addressing attribute is ``data-a11y-id``, not ``id``. This is deliberate,
not stylistic: ``id`` is not an inert addressing slot -- it is read by WCAG
checks (``audit/checks/table_checks.py``'s ``table-missing-headers-id`` treats
any ``th[id]`` as evidence of a header/cell association, and there is a
separate ``duplicate-id`` check). Stamping ``id`` therefore silently
manufactures false passes: measured on one document, stamping ``id`` dropped
``table-missing-headers-id`` findings from 217 to 1 while adding zero real
``headers=`` associations. ``data-a11y-id`` is a namespace this pipeline owns
exclusively, so it cannot be read by any check, present or future, and the
``duplicate-id`` collision hazard that ``id``-stamping had to design around
does not exist here -- uniqueness within the document still matters (two
stamped elements must not share an id), but there is nothing external to
collide with.
"""
from typing import Any

A11Y_ID_ATTR = "data-a11y-id"
A11Y_ID_PREFIX = "a11y-e"

# Measured over 50 documents / 12,463 issues: these are the only tags that
# appear as audit-path leaves often enough to matter. td/tr/p/h1/h3-h6 never
# appear at all and would cost +37.8% HTML growth against +12.1% for this set.
# html/body/title are singletons that resolve without any nth-of-type.
STAMPED_TAGS = ("th", "table", "img", "figure", "a", "h2")


def stamp_element_ids(soup: Any) -> int:
    """Give unstamped audited-type elements a unique data-a11y-id. Returns the count added.

    Existing data-a11y-id attributes always win -- stamps from earlier pipeline
    stages are never overwritten. Idempotent: stamping twice adds nothing.

    Unlike an ``id`` stamp, there is nothing *external* to collide with --
    no author attribute and no WCAG check reads ``data-a11y-id``. But
    uniqueness within the document still has to hold: a re-stamping pass
    over a document that already carries some stamps (e.g. elements
    inserted between an original stamp and a later one) must not hand a
    freshly-assigned counter value to an id that an untouched, previously
    stamped element already owns. The used-set (built once, not rebuilt per
    element -- that is what made the original headers-id strategy
    quadratic) still guards that; only the second attribute this used to
    check (generic ``id``) is gone, since that was the collision this
    attribute was chosen to make impossible.
    """
    used = {el[A11Y_ID_ATTR] for el in soup.find_all(attrs={A11Y_ID_ATTR: True})}

    counter = 0
    stamped = 0
    for el in soup.find_all(list(STAMPED_TAGS)):
        if el.get(A11Y_ID_ATTR):
            continue
        candidate = f"{A11Y_ID_PREFIX}{counter:05d}"
        while candidate in used:
            counter += 1
            candidate = f"{A11Y_ID_PREFIX}{counter:05d}"
        el[A11Y_ID_ATTR] = candidate
        used.add(candidate)
        counter += 1
        stamped += 1
    return stamped
