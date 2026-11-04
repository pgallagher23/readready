# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for audit/ocr_quality.py — per-page OCR text scoring.

Motivating fixture: 2AAF3 example 1.pdf, a fully-scanned 2-page PDF where
page 1 OCR'd as gibberish (mixed text orientations: horizontal body,
vertical "HO CHI MINH" headline, rotated copyright strip) and page 2
OCR'd cleanly. The scorer must distinguish them.

Run:  python -m pytest tests/test_ocr_quality.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audit.ocr_quality import score_ocr_text


# Verbatim from the enriched_structure.json `unmatched_texts` for 2AAF3.
PAGE_1_GIBBERISH = (
    "SN ZL SL) me; yyBuAdoo Aq (apog payeajoid aq Ae jeuayew siuy "
    "OALFAVA VIFLLIONS JO 1B ATprap 94} popoy.tad pue WsTUNuOD 07 "
    "WSTTRUORU PALLVUl o "
    "MONUYY AZINWLS Ag "
    "OF LOY \"\"S*] OAISSEUL YY Pay, -SHLY AOU] ATULE PUGH UA. UO "
    "APHBAR] B OFUL YING \"TayeyBuy "
    "ATQRLAOUL pylon suodro.n payoysiydos wy yyy VOISD][L OY] PoAN LIN "
    "Spry ies SLOOF YRaaq SEY,"
)

PAGE_2_CLEAN = (
    "he had at least two wives or perhaps concubines. One was a Chinese "
    "woman: the other was Giap's sister-in-law, who was guillotined by the "
    "French. In 1940, Japan's legions swept into Indochina and French "
    "officials in Vietnam, loyal to the proGerman Vichy administration in "
    "France, collaborated with them. France undervalued the power Ho "
    "wielded. There's no doubt that he aspired to power."
)


class TestPage1Gibberish:
    """Page 1 of 2AAF3 — should be classified poor."""

    def test_label_is_poor(self):
        r = score_ocr_text(PAGE_1_GIBBERISH)
        assert r["label"] == "poor", (
            f"Expected 'poor' for 2AAF3 page-1 gibberish, got {r['label']!r} "
            f"with score={r['score']} signals={r['signals']}"
        )

    def test_wordfreq_hit_ratio_low(self):
        r = score_ocr_text(PAGE_1_GIBBERISH)
        assert r["signals"]["wordfreq_hit_ratio"] < 0.15, (
            f"Expected wordfreq_hit_ratio < 0.15 (gibberish), got "
            f"{r['signals']['wordfreq_hit_ratio']}"
        )

    def test_reasons_include_wordfreq(self):
        r = score_ocr_text(PAGE_1_GIBBERISH)
        # The dominant signal pulling page-1 down is the wordfreq miss.
        assert any("token" in reason or "common-word" in reason
                   for reason in r["reasons"]), (
            f"Expected 'tokens match common-word table' reason, got {r['reasons']}"
        )

    def test_not_skipped(self):
        r = score_ocr_text(PAGE_1_GIBBERISH)
        assert r["signals"]["skipped"] is False


class TestPage2Clean:
    """Page 2 of 2AAF3 — should be classified good or at worst fair."""

    def test_label_is_good_or_fair(self):
        r = score_ocr_text(PAGE_2_CLEAN)
        assert r["label"] in ("good", "fair"), (
            f"Expected 'good' or 'fair' for clean OCR, got {r['label']!r} "
            f"with score={r['score']} signals={r['signals']}"
        )

    def test_wordfreq_hit_ratio_high(self):
        r = score_ocr_text(PAGE_2_CLEAN)
        assert r["signals"]["wordfreq_hit_ratio"] > 0.70, (
            f"Expected wordfreq_hit_ratio > 0.70 (clean English), got "
            f"{r['signals']['wordfreq_hit_ratio']}"
        )

    def test_langdetect_high_confidence_english(self):
        r = score_ocr_text(PAGE_2_CLEAN)
        assert r["signals"]["langdetect_lang"] == "en"
        assert r["signals"]["langdetect_conf"] > 0.95


class TestEdgeCases:
    def test_empty_text_returns_skipped_fair(self):
        r = score_ocr_text("")
        assert r["label"] == "fair"
        assert r["signals"]["skipped"] is True
        assert r["score"] == 0.0

    def test_text_below_min_chars_skipped(self):
        r = score_ocr_text("hi there")  # < 30 chars
        assert r["signals"]["skipped"] is True
        assert r["label"] == "fair"

    def test_pure_punctuation_skipped_when_short(self):
        r = score_ocr_text("...!!!???,,,;;;")  # 15 chars
        assert r["signals"]["skipped"] is True

    def test_long_pure_punctuation_falls_through(self):
        # Long enough not to skip; should land poor (no real tokens).
        text = "..." * 20 + "!!!" * 20  # 120 chars, no letters
        r = score_ocr_text(text)
        assert r["signals"]["skipped"] is False
        assert r["signals"]["wordfreq_hit_ratio"] == 0.0
        assert r["label"] == "poor"

    def test_spanish_with_correct_hint_not_poor(self):
        spanish = (
            "El gato come pescado todos los días en la cocina. La casa es "
            "grande y tiene un jardín muy bonito con flores y árboles. Los "
            "niños juegan en el parque por las tardes."
        )
        r = score_ocr_text(spanish, lang_hint="es")
        assert r["label"] in ("good", "fair"), (
            f"Expected Spanish-with-Spanish-hint not 'poor', got {r['label']!r} "
            f"score={r['score']} signals={r['signals']}"
        )


class TestReproducibility:
    def test_same_input_same_output(self):
        a = score_ocr_text(PAGE_2_CLEAN)
        b = score_ocr_text(PAGE_2_CLEAN)
        assert a["score"] == b["score"]
        assert a["label"] == b["label"]
        assert a["signals"] == b["signals"]


class TestThresholdOverrides:
    def test_strict_thresholds_demote_to_poor(self):
        # Page 2 is normally "good"; raise good_threshold above its score.
        r_default = score_ocr_text(PAGE_2_CLEAN)
        r_strict = score_ocr_text(
            PAGE_2_CLEAN,
            good_threshold=0.99,
            fair_threshold=0.95,
        )
        # With same score, the strict thresholds should re-classify.
        assert r_default["score"] == r_strict["score"]
        if r_strict["score"] < 0.95:
            assert r_strict["label"] == "poor"
        elif r_strict["score"] < 0.99:
            assert r_strict["label"] == "fair"
