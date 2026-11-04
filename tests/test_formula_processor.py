# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for formula alt-text / LaTeX cleanup helpers in
vlm/formula_processor.py.

Covers two behaviours added to keep VLM output clean:
  1. Stripping meta-preamble openings ("This formula shows ...") from the
     natural-language description.
  2. Discarding / trimming prose that the VLM emits in place of (or appended
     to) the LaTeX line ("The LaTeX representation is unknown due to ...").
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json  # noqa: E402

from vlm import formula_processor  # noqa: E402
from vlm.formula_processor import (  # noqa: E402
    _is_latex_failure,
    _parse_vlm_response,
    _strip_boilerplate_prefix,
    _trim_latex_prose,
    enrich_html_with_formulas,
    latex_to_mathml,
)


# --------------------------------------------------------------------------- #
# Description boilerplate stripping
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text, expected", [
    ("This formula shows the sum of x and y.", "The sum of x and y."),
    ("This image shows a mathematical formula for a visually impaired "
     "reader: the integral of f(x).", "The integral of f(x)."),
    ("The formula represents the area of a circle, pi r squared.",
     "The area of a circle, pi r squared."),
    ("This equation: a squared plus b squared equals c squared.",
     "A squared plus b squared equals c squared."),
    # "That/Which" lead-ins
    ("That represents a spatially correlated random effect in a model.",
     "A spatially correlated random effect in a model."),
    ("Which shows the gradient of the loss function.",
     "The gradient of the loss function."),
    # -ing verb must not be truncated to "ing ..."
    ("This is a mathematical formula representing Einstein's equation, E = mc^2.",
     "Einstein's equation, E = mc^2."),
    # stacked / double prefix
    ("This image shows a formula. The formula is related to Poisson variables.",
     "Related to Poisson variables."),
])
def test_strip_boilerplate_removes_preamble(text, expected):
    assert _strip_boilerplate_prefix(text) == expected


@pytest.mark.parametrize("text", [
    "The sum of the first n integers equals n times n plus one over two.",
    "A matrix where each entry denotes adjacency between nodes.",
    "x equals 5.",
    "The equation of motion describes a damped oscillator.",
    "That value equals five plus three.",
])
def test_strip_boilerplate_preserves_real_content(text):
    assert _strip_boilerplate_prefix(text) == text


# --------------------------------------------------------------------------- #
# LaTeX failure / prose detection
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("latex", [
    "The LaTeX representation of this mathematical formula is unknown due to "
    "the resolution and angle of the image provided",
    "unknown",
    "N/A",
    "I cannot determine the LaTeX from this image.",
    "Sorry, the image is too blurry to extract LaTeX.",
    "The first equation is shown",          # prose, <6 words, no latex chars
    "The",                                  # bare stop-word left after a trim
    "where Hdot = EHdot i .",               # opens with a prose marker word
    "Note: x = 5",
])
def test_is_latex_failure_true(latex):
    assert _is_latex_failure(latex) is True


@pytest.mark.parametrize("latex", [
    "x = 5",
    "E = mc^2",
    r"\frac{a}{b}",
    r"\sum_{i=1}^{n} i",
    r"\lambda_i = e_i + u_i",
    r"W_{ij} = \begin{cases} 1 & 0 \end{cases}",
    r"\sin/abc",                            # contains "n/a" substring but is real
])
def test_is_latex_failure_false(latex):
    assert _is_latex_failure(latex) is False


# --------------------------------------------------------------------------- #
# LaTeX trailer trimming
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("latex, expected", [
    (r"\bar{I} = e^T I + V + U   Where: - \bar{I} is the relative risk",
     r"\bar{I} = e^T I + V + U"),
    (r"\mathbf{u}   CRITICAL: The ICAR model predicts y.", r"\mathbf{u}"),
    ("x = y + z   Note that this is approximate.", "x = y + z"),
    # single-space-joined "where:" (multi-line LaTeX is joined with one space)
    (r"Y = \alpha + \epsilon where: - Y is the observed value",
     r"Y = \alpha + \epsilon"),
])
def test_trim_latex_prose_cuts_trailer(latex, expected):
    assert _trim_latex_prose(latex) == expected


def test_trim_latex_prose_preserves_inline_text_where():
    """An inline \\text{where ...} (single space, no colon) must survive."""
    latex = r"f(x) = \begin{cases} 1 & \text{where } x>0 \\ 0 & 0 \end{cases}"
    assert _trim_latex_prose(latex) == latex


# --------------------------------------------------------------------------- #
# End-to-end response parsing
# --------------------------------------------------------------------------- #

def test_parse_strips_description_and_blanks_prose_latex():
    resp = (
        "DESCRIPTION: This formula shows the sum of i from 1 to n.\n"
        "LATEX: The LaTeX representation is unknown due to the resolution."
    )
    out = _parse_vlm_response(resp)
    assert out["description"] == "The sum of i from 1 to n."
    assert out["latex"] == ""


def test_parse_keeps_valid_latex_and_cleans_description():
    resp = (
        "DESCRIPTION: The image displays a mathematical formula for E.\n"
        "LATEX: E = mc^2"
    )
    out = _parse_vlm_response(resp)
    assert not out["description"].lower().startswith("the image displays")
    assert out["latex"] == "E = mc^2"


# --------------------------------------------------------------------------- #
# LaTeX -> standalone MathML (used for PDF Associated File + HTML enrichment)
# --------------------------------------------------------------------------- #

def test_latex_to_mathml_produces_standalone_document():
    out = latex_to_mathml(r"A = \pi r^2", "The area of a circle.")
    assert out is not None
    # Standalone document: XML declaration + namespaced <math> root.
    assert out.startswith("<?xml version=\"1.0\" encoding=\"UTF-8\"?>")
    assert 'xmlns="http://www.w3.org/1998/Math/MathML"' in out
    # Caption carried as alttext for MathML-aware readers.
    assert 'alttext="The area of a circle."' in out


def test_latex_to_mathml_without_alt_text_omits_alttext():
    out = latex_to_mathml("x = 5")
    assert out is not None
    assert "<math" in out
    assert "alttext=" not in out


def test_latex_to_mathml_escapes_alttext_special_chars():
    out = latex_to_mathml("x < y", 'a < b & "c"')
    assert out is not None
    # Raw special characters must not appear unescaped inside the attribute.
    assert 'alttext="a < b & "c""' not in out
    assert "&lt;" in out or "&amp;" in out


def test_latex_to_mathml_caption_with_backslash_does_not_crash():
    """A VLM caption containing a backslash sequence (e.g. ``\\mathbf``) must
    not be interpreted as a regex escape when injected as the ``alttext``.

    Regression: the caption was passed as the *replacement* string to
    ``re.sub``, so ``\\m`` raised ``re.error: bad escape \\m`` and crashed the
    whole formula phase.
    """
    caption = r"The adjacency matrix uses \mathbf bold vectors and \mu weights"
    out = latex_to_mathml(r"A = \pi r^2", caption)
    assert out is not None
    # The literal backslash caption is carried through verbatim (escaped only
    # for XML attribute quoting), not consumed as a regex escape.
    assert r"\mathbf" in out
    assert r"\mu" in out


def test_enrich_survives_one_failing_formula(tmp_path, monkeypatch):
    """A single formula whose MathML insertion raises must not abort the whole
    phase — the remaining formulas still get their aria-label fallback.

    Regression: an unguarded ``_try_insert_mathml`` propagated out of the loop,
    so one bad formula lost alt text for *every* formula in the document.
    """
    html = (
        "<html><body>"
        '<div role="math" data-formula-index="0">E0</div>'
        '<div role="math" data-formula-index="1">E1</div>'
        "</body></html>"
    )
    html_path = tmp_path / "doc.html"
    html_path.write_text(html, encoding="utf-8")

    structure = {
        "elements": [
            {"type": "FormulaItem", "tag": "Formula", "text": "E0",
             "latex": "a=0", "alt_text": "first formula", "page_no": 1},
            {"type": "FormulaItem", "tag": "Formula", "text": "E1",
             "latex": "b=1", "alt_text": "second formula", "page_no": 2},
        ]
    }
    struct_path = tmp_path / "structure.json"
    struct_path.write_text(json.dumps(structure), encoding="utf-8")

    # Force the first formula's MathML insertion to blow up; the second must
    # still be enriched.
    real_insert = formula_processor._try_insert_mathml

    def flaky_insert(soup, html_elem, latex, alt_text):
        if latex == "a=0":
            raise ValueError("boom")
        return real_insert(soup, html_elem, latex, alt_text)

    monkeypatch.setattr(formula_processor, "_try_insert_mathml", flaky_insert)

    # Must not raise, and must enrich at least the surviving formula.
    enriched = enrich_html_with_formulas(str(html_path), str(struct_path), {})
    assert enriched >= 1
    out = html_path.read_text(encoding="utf-8")
    assert "second formula" in out


@pytest.mark.parametrize("latex", ["", "   ", None])
def test_latex_to_mathml_empty_returns_none(latex):
    assert latex_to_mathml(latex) is None
