# SPDX-License-Identifier: AGPL-3.0-or-later
"""A figcaption must not destroy a richer alt description (G-15).

Run: python -m pytest tests/test_figcaption_alt_preservation.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bs4 import BeautifulSoup

from remediate.strategies.figure_remediation import clear_redundant_img_alt


def _soup(figcaption, alt):
    return BeautifulSoup(
        f'<figure><img src="a.png" alt="{alt}">'
        f'<figcaption>{figcaption}</figcaption></figure>',
        "html.parser",
    )


def test_duplicate_caption_still_clears_alt():
    s = _soup("A cat sitting on a mat", "A cat sitting on a mat")
    clear_redundant_img_alt(s)
    assert s.find("img")["alt"] == "", "an exact duplicate is genuinely redundant"


def test_bare_figure_number_caption_does_not_destroy_alt():
    s = _soup("Figure 3", "Enrollment by college, 2019 to 2024")
    clear_redundant_img_alt(s)
    assert s.find("img")["alt"] == "Enrollment by college, 2019 to 2024", (
        "a bare figure number is not a description; clearing alt loses the only "
        "accessible text for this image"
    )


def test_short_caption_does_not_destroy_long_alt():
    s = _soup("Source: IPEDS", "Bar chart comparing six colleges by headcount")
    clear_redundant_img_alt(s)
    assert s.find("img")["alt"] == "Bar chart comparing six colleges by headcount"


def test_aria_label_survives_when_alt_survives():
    s = BeautifulSoup(
        '<figure><img src="a.png" alt="Detailed chart" aria-label="Detailed chart">'
        '<figcaption>Figure 2</figcaption></figure>',
        "html.parser",
    )
    clear_redundant_img_alt(s)
    assert s.find("img").get("aria-label") == "Detailed chart"
