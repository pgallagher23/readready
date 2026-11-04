# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the screen-reader coverage DIAGNOSTIC thresholds.

These thresholds (per-page low-coverage flag + overlay-mode sr_quality cutoffs)
were hardcoded literals; they are now named constants in ``validation_gates``,
exposed as function params, and overridable from config.yaml ``verdict:``. They
do NOT gate the verdict (that is ``verdict.min_coverage``). This suite locks in:

1. The constants equal their historical values (no behavior change).
2. The two SR functions' signature defaults equal those constants.
"""

import inspect

from validation.gates import (
    quick_screen_reader_check,
    gate_screen_reader_sim,
    DEFAULT_PAGE_LOW_COVERAGE,
    DEFAULT_SR_POOR_COVERAGE,
    DEFAULT_SR_FAIR_COVERAGE,
)


# 1. Historical values preserved -------------------------------------------

def test_constants_match_historical_values():
    assert DEFAULT_PAGE_LOW_COVERAGE == 0.50
    assert DEFAULT_SR_POOR_COVERAGE == 0.50
    assert DEFAULT_SR_FAIR_COVERAGE == 0.70


# 2. Signature defaults equal the constants --------------------------------

def test_quick_check_signature_defaults():
    params = inspect.signature(quick_screen_reader_check).parameters
    assert params["coverage_threshold"].default == DEFAULT_PAGE_LOW_COVERAGE
    assert params["sr_poor_coverage"].default == DEFAULT_SR_POOR_COVERAGE
    assert params["sr_fair_coverage"].default == DEFAULT_SR_FAIR_COVERAGE


def test_gate_sim_signature_default():
    params = inspect.signature(gate_screen_reader_sim).parameters
    assert params["coverage_threshold"].default == DEFAULT_PAGE_LOW_COVERAGE


# 4. Coverage is ONE number — shared _page_coverage_core ---------------------

def test_page_coverage_core_full_and_partial():
    """Deterministic guard for the shared per-page coverage core used by both
    gate_screen_reader_sim and quick_screen_reader_check."""
    import fitz
    from validation.gates import _page_coverage_core, coverage_tokens

    src = fitz.open()
    page = src.new_page()
    page.insert_text((72, 72), "the quick brown fox jumps over the lazy dog")
    source_text = src[0].get_text("text").strip()
    source_words = coverage_tokens(source_text)
    swc = len(source_words)
    assert swc >= 6

    # Full overlap: struct-tree text == source text → coverage 1.0
    cov_full, _txt, _w = _page_coverage_core(
        0, source_words, swc, {0: source_text}, src, True,
        mcid_gated_fallback=False,
    )
    assert cov_full == 1.0

    # Partial overlap: only half the source words present in struct-tree text
    half = " ".join(list(source_words)[: swc // 2])
    cov_half, _t2, _w2 = _page_coverage_core(
        0, source_words, swc, {0: half}, src, True,
        mcid_gated_fallback=False,
    )
    assert 0.0 < cov_half < 1.0
    src.close()


# 5. page_deficit corroboration against post-tagging coverage ---------------
#
# `page_deficits` is a MATCHING-TIME diagnostic computed on the ORIGINAL content
# runs (before the tagger strips duplicate invisible-text layers). On pages that
# carried such a layer it fires a false "content not linked" deficit even though
# the FINAL tagged PDF reads fine. `_corroborate_page_deficits` overrules the
# diagnostic with the authoritative post-tagging per-page coverage.

def test_quick_check_signature_has_corroborate_flag():
    params = inspect.signature(quick_screen_reader_check).parameters
    assert params["corroborate_page_deficits"].default is True
    assert params["deficit_corroboration_threshold"].default == 0.80


def test_deficit_dropped_when_post_tagging_coverage_high():
    """The karen-ch15 case: matching-time deficit but ~1.0 tagged coverage."""
    from validation.gates import _corroborate_page_deficits
    raw = [{"page": 7, "deficit_pct": 40.0}]
    kept = _corroborate_page_deficits(
        raw, page_coverage={7: 1.0}, low_cov_pages=set(), threshold=0.80,
    )
    assert kept == []


def test_deficit_kept_when_post_tagging_coverage_low():
    """Genuine moderate deficit in the [0.50, 0.80) band still surfaces."""
    from validation.gates import _corroborate_page_deficits
    raw = [{"page": 7, "deficit_pct": 40.0}]
    kept = _corroborate_page_deficits(
        raw, page_coverage={7: 0.62}, low_cov_pages=set(), threshold=0.80,
    )
    assert [d["page"] for d in kept] == [7]


def test_deficit_dropped_when_already_low_coverage():
    """A page already in low_coverage_pages is shown by the 'Low text coverage'
    item — don't double-warn it via page_deficit."""
    from validation.gates import _corroborate_page_deficits
    raw = [{"page": 7, "deficit_pct": 40.0}]
    kept = _corroborate_page_deficits(
        raw, page_coverage={7: 0.30}, low_cov_pages={7}, threshold=0.80,
    )
    assert kept == []


def test_deficit_kept_when_coverage_unknown():
    """Pages with <=10 source words have no coverage entry; keep them
    (recall-safe: no authoritative signal to overturn the deficit)."""
    from validation.gates import _corroborate_page_deficits
    raw = [{"page": 7, "deficit_pct": 40.0}]
    kept = _corroborate_page_deficits(
        raw, page_coverage={}, low_cov_pages=set(), threshold=0.80,
    )
    assert [d["page"] for d in kept] == [7]


def test_deficit_corroboration_kill_switch_off_passes_through():
    """corroborate=False returns the raw list unchanged."""
    from validation.gates import _corroborate_page_deficits
    raw = [{"page": 7, "deficit_pct": 40.0}, {"page": 9, "deficit_pct": 25.0}]
    kept = _corroborate_page_deficits(
        raw, page_coverage={7: 1.0, 9: 1.0}, low_cov_pages=set(),
        threshold=0.80, corroborate=False,
    )
    assert kept == raw


def test_quick_screen_reader_check_reading_order_guard_params():
    """The three validator-precision kill-switches thread through
    quick_screen_reader_check with default True."""
    import inspect
    from validation.gates import quick_screen_reader_check
    params = inspect.signature(quick_screen_reader_check).parameters
    assert params["reading_order_geo_tabular_guard"].default is True
    assert params["reading_order_hyphen_normalize"].default is True
    # small-n band ships OFF: an A/B run measured a 50% true-positive rate
    # among its suppressions (see config.yaml rationale)
    assert params["reading_order_small_n_guard"].default is False
