# SPDX-License-Identifier: AGPL-3.0-or-later
"""Without an AI model, images get an empty alt and nothing is invented.

The user is prompted instead (warning banner + manual alt-text editor).
"""
from bs4 import BeautifulSoup

from remediate.strategies.figure_remediation import (
    clear_redundant_img_alt,
    remediate_alt_text,
    remediate_improper_figure_structure,
    remediate_missing_figure_caption,
)


def _issue(src):
    return {"context": {"html_snippet": f'<img src="{src}" alt="">'}}


def _doc(body):
    return BeautifulSoup(body, "html.parser")


def test_missing_alt_becomes_empty_not_invented():
    s = _doc('<h2>Quarterly figures</h2><figure><img src="q.png"></figure>')
    assert remediate_alt_text(s, _issue("q.png")) is None   # issue stays open
    assert s.find("img")["alt"] == ""


def test_generic_alt_is_emptied_not_replaced():
    s = _doc('<h2>Results</h2><img src="r.png" alt="image">')
    remediate_alt_text(s, _issue("r.png"))
    assert s.find("img")["alt"] == ""


def test_real_author_alt_is_left_alone():
    s = _doc('<img src="r.png" alt="Bar chart of visits by quarter">')
    remediate_alt_text(s, _issue("r.png"))
    assert s.find("img")["alt"] == "Bar chart of visits by quarter"


def test_no_caption_is_invented_from_a_heading():
    s = _doc('<h2>Quarterly figures</h2><figure><img src="q.png" alt=""></figure>'
             '<p>Body text.</p>')
    remediate_alt_text(s, _issue("q.png"))
    remediate_missing_figure_caption(s, _issue("q.png"))
    clear_redundant_img_alt(s)
    assert s.find("figcaption") is None
    assert s.find("img")["alt"] == ""
    assert "Quarterly figures" not in str(s.find("figure"))


def test_real_figure_paragraph_still_becomes_the_caption():
    s = _doc('<figure><img src="q.png" alt=""></figure>'
             '<p>Figure 2: Visits per quarter, 2024.</p>')
    remediate_missing_figure_caption(s, _issue("q.png"))
    cap = s.find("figcaption")
    assert cap is not None and cap.get_text() == "Figure 2: Visits per quarter, 2024."


def test_bare_img_is_wrapped_without_invented_caption():
    s = _doc('<h2>Results</h2><img src="r.png" alt="">')
    remediate_improper_figure_structure(s, {})
    assert s.find("img").find_parent("figure") is not None
    assert s.find("figcaption") is None
    assert s.find("figure").get("aria-label") is None
    assert s.find("img")["alt"] == ""


def test_bare_img_keeps_its_alt_when_no_source_caption():
    s = _doc('<img src="r.png" alt="Bar chart of visits by quarter">')
    remediate_improper_figure_structure(s, {})
    assert s.find("figcaption") is None
    assert s.find("img")["alt"] == "Bar chart of visits by quarter"


def test_bare_figure_number_caption_does_not_erase_real_alt():
    # A1: a "Figure 3" paragraph is a caption source, but it carries none of
    # the alt's information, so the description must survive and the bare
    # number must not become the figure's accessible name.
    alt = "Bar chart of enrollment rising from 200 in 2010 to 900 in 2020"
    s = _doc(f'<img src="a.png" alt="{alt}"><p>Figure 3</p>')
    remediate_improper_figure_structure(s, {})
    fig = s.find("figure")
    assert fig is not None
    assert s.find("img")["alt"] == alt
    assert fig.get("aria-label") is None
    assert fig.find("figcaption").get_text(strip=True) == "Figure 3"


def test_caption_duplicating_alt_still_clears_it():
    s = _doc('<img src="a.png" alt="Enrollment by year">'
             '<p>Figure 3: Enrollment by year</p>')
    remediate_improper_figure_structure(s, {})
    assert s.find("img")["alt"] == ""
    assert s.find("figure")["aria-label"] == "Figure 3: Enrollment by year"


def test_caption_only_from_immediately_following_paragraph():
    # A2: the first image must not borrow the second figure's caption.
    s = _doc('<img src="a.png" alt=""><h2>Next section</h2>'
             '<img src="b.png" alt=""><p>Figure 2: Visits.</p>')
    remediate_improper_figure_structure(s, {})
    figs = s.find_all("figure")
    assert len(figs) == 2
    assert figs[0].find("figcaption") is None
    assert figs[1].find("figcaption").get_text(strip=True) == "Figure 2: Visits."


def test_missing_caption_ignores_non_adjacent_figure_paragraph():
    s = _doc('<figure><img src="q.png" alt=""></figure><h3>Next</h3>'
             '<p>Figure 2: Visits.</p>')
    assert remediate_missing_figure_caption(s, _issue("q.png")) is None
    assert s.find("figcaption") is None


def test_adjacent_caption_paragraph_is_moved_not_copied():
    # A3: screen readers must not hear the caption twice.
    s = _doc('<img src="a.png" alt=""><p>Figure 3: Visits per quarter.</p>')
    remediate_improper_figure_structure(s, {})
    assert s.get_text().count("Figure 3: Visits per quarter.") == 1
    assert s.find("figcaption").get_text(strip=True) == "Figure 3: Visits per quarter."

    s = _doc('<figure><img src="q.png" alt=""></figure>'
             '<p>Figure 2: Visits per quarter, 2024.</p>')
    remediate_missing_figure_caption(s, _issue("q.png"))
    assert s.get_text().count("Figure 2: Visits per quarter, 2024.") == 1
    assert s.find("figure").find("figcaption") is not None


def test_cli_logs_pipeline_warnings(monkeypatch, tmp_path, caplog):
    """The CLI surfaces result["warnings"] so a no-AI run prompts the user."""
    import logging
    import sys

    import main as cli

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(
        cli, "run_pipeline",
        lambda *a, **k: {"warnings": ["1 of 1 figures have no alt text"],
                         "download_files": []},
    )
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["main.py", str(pdf), "-o", str(tmp_path / "out")])
    with caplog.at_level(logging.WARNING):
        cli.main()
    assert any("1 of 1 figures have no alt text" in r.getMessage()
               and r.levelno == logging.WARNING for r in caplog.records)
