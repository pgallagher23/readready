# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-page OCR text quality scoring.

When the source PDF has no native text on a page (scanned image), the pipeline
relies on Docling/Tesseract OCR. OCR can produce gibberish on pages with
rotated text, unusual layouts, or low-quality scans. The element_match_rate
metric is structurally meaningless for such pages (no native runs to match
against), so we score the OCR output directly.

Three composable signals:
    1. control_char_ratio  — high values indicate font-encoding failures.
    2. wordfreq_hit_ratio  — fraction of alpha tokens that hit the
       wordfreq frequency table at or above ``zipf_threshold``. Real
       English text scores ~0.85; gibberish scores ~0.10.
    3. langdetect_conf     — probability that the detected language
       matches ``lang_hint``.

The motivating case is a fully-scanned two-page PDF: page 1 OCR'd as gibberish
(mixed text orientations), page 2 OCR'd cleanly. The scorer must
distinguish them.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from converter.fallback_text_extractor import _control_char_ratio


_ALPHA_TOKEN_RE = re.compile(r"[A-Za-z]+")


def _wordfreq_hit_ratio(text: str, lang: str, zipf_threshold: float) -> float:
    """Fraction of alpha tokens (length >= 3) with zipf_frequency >= threshold."""
    try:
        from wordfreq import zipf_frequency
    except ImportError:
        return 0.0
    tokens = [m.group(0).lower() for m in _ALPHA_TOKEN_RE.finditer(text)
              if len(m.group(0)) >= 3]
    if not tokens:
        return 0.0
    hits = sum(1 for t in tokens if zipf_frequency(t, lang) >= zipf_threshold)
    return hits / len(tokens)


def _langdetect_signal(text: str, lang_hint: str) -> tuple[Optional[str], Optional[float]]:
    """Return (top_lang, prob_for_lang_hint) or (None, None) on failure.

    Uses the probability for ``lang_hint`` even when it's not the top
    result (e.g. a clean English page wrongly classified as Italian still
    keeps some English probability).
    """
    try:
        from langdetect import detect_langs
        langs = detect_langs(text)
        if not langs:
            return None, None
        top_lang = langs[0].lang
        target_prob = next((l.prob for l in langs if l.lang == lang_hint), 0.0)
        return top_lang, target_prob
    except Exception:
        return None, None


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def score_ocr_text(
    text: str,
    *,
    lang_hint: str = "en",
    min_chars: int = 30,
    zipf_threshold: float = 3.0,
    good_threshold: float = 0.65,
    fair_threshold: float = 0.40,
) -> Dict[str, Any]:
    """Score an OCR'd page's text quality.

    Returns a dict with ``score`` (0.0-1.0), ``label`` (``"good"`` /
    ``"fair"`` / ``"poor"``), human-readable ``reasons`` (top three
    contributors that pulled the score down), and a ``signals`` dict
    with each raw input.
    """
    if not text or len(text) < min_chars:
        return {
            "score": 0.0,
            "label": "fair",
            "reasons": ["text too short to score"],
            "signals": {
                "control_char_ratio": 0.0,
                "wordfreq_hit_ratio": 0.0,
                "langdetect_lang": None,
                "langdetect_conf": None,
                "skipped": True,
            },
        }

    ctrl = _control_char_ratio(text)
    wf = _wordfreq_hit_ratio(text, lang_hint, zipf_threshold)
    top_lang, ld_conf = _langdetect_signal(text, lang_hint)

    # Composite — wordfreq is the dominant signal because it's the most
    # discriminating (langdetect's character n-grams classify clearly
    # garbled OCR as about 70% English in testing). langdetect is
    # also gated by wordfreq density: when the page has very few real
    # words, langdetect's confidence on the residual noise isn't
    # trustworthy — a few scattered real words can lift it to 100%
    # English on a mostly-gibberish page.
    ctrl_term = 0.10 * (1.0 - _clamp(ctrl / 0.30))
    wf_term = 0.75 * _clamp(wf / 0.50)
    ld_raw = ld_conf if ld_conf is not None else 0.5
    ld_gate = _clamp(wf / 0.30)
    ld_term = 0.15 * ld_raw * ld_gate
    score = round(_clamp(ctrl_term + wf_term + ld_term), 3)

    if score >= good_threshold:
        label = "good"
    elif score >= fair_threshold:
        label = "fair"
    else:
        label = "poor"

    reasons: list[str] = []
    if wf < 0.30:
        reasons.append(f"{wf:.0%} of tokens match common-word table")
    if ctrl > 0.10:
        reasons.append(f"{ctrl:.0%} control characters in text")
    if ld_conf is not None and ld_conf < 0.70 and top_lang != lang_hint:
        reasons.append(f"language detected as {top_lang!r}, not {lang_hint!r}")
    elif ld_conf is None:
        reasons.append("language detection failed")

    return {
        "score": score,
        "label": label,
        "reasons": reasons[:3],
        "signals": {
            "control_char_ratio": round(ctrl, 3),
            "wordfreq_hit_ratio": round(wf, 3),
            "langdetect_lang": top_lang,
            "langdetect_conf": round(ld_conf, 3) if ld_conf is not None else None,
            "skipped": False,
        },
    }
