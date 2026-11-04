# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-page reading-order scorer.

Measures how faithfully the tagged PDF's structure-tree reading order (the DFS
order a screen reader follows through ``/StructTreeRoot``) tracks the page's own
column-aware *visual* order (``pdftotext -layout`` on the tagged file).  Agreement
is reported per page as Kendall's tau in ``[-1.0, 1.0]``; 1.0 means the screen
reader reads in the same order the page is laid out.

This is a **detection** aid only — it never reorders content.  A low worst-page
tau means the reading order is uncertain and the document should be flagged for
the manual pass (soft ``needs_review``), not auto-rejected.  The visual reference
(pdftotext) is an independent heuristic, not ground truth, so the metric measures
*agreement* between two order estimates rather than correctness.

Honest scope: pages whose text cannot be cleanly extracted from both the tag tree
(CID/Type0 fonts, sparse text) and the visual extractor yield ``None`` (no-op) and
are excluded from the verdict — they are never failed, only reported as
``n_noop_pages``.
"""

from __future__ import annotations

import bisect
import re
import subprocess
from typing import Dict, List, Optional

WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'\-]{2,}")


def tokenize(text: Optional[str], *, split_hyphens: bool = True) -> List[str]:
    """Lowercase word tokens of length >= 3 (drops noise like 'a', 'I', 'of').

    With ``split_hyphens`` (default), tokens containing internal hyphens are
    split into their >=3-char parts.  pdftotext preserves line-break
    hyphenation ("rese-arch") while /ActualText carries the dehyphenated form
    ("rese arch"); without the split the two sides tokenize differently and
    the alignment collapses on hyphen-rich pages (references sections).  The
    3-char floor mirrors WORD_RE so both sides stay symmetric.
    """
    toks = [m.group(0).lower() for m in WORD_RE.finditer(text or "")]
    if not split_hyphens:
        return toks
    out: List[str] = []
    for tok in toks:
        if "-" in tok:
            out.extend(p for p in tok.split("-") if len(p) >= 3)
        else:
            out.append(tok)
    return out


def _count_inversions(values: List[int]) -> int:
    """Count pairs (i, j) with i < j and values[i] > values[j]."""
    seen: List[int] = []
    inv = 0
    for v in reversed(values):
        idx = bisect.bisect_left(seen, v)
        inv += idx
        bisect.insort(seen, v)
    return inv


def _align_positions(
    tag_tokens: List[str],
    vis_tokens: List[str],
) -> List[int]:
    """Greedy first-fit alignment: visual positions of tag tokens, in tag order.

    Each tag token is matched to the next unused position of the same token in
    ``vis_tokens``; tag tokens absent from the visual sequence are dropped.
    """
    if not tag_tokens or not vis_tokens:
        return []

    positions: Dict[str, List[int]] = {}
    for i, tok in enumerate(vis_tokens):
        positions.setdefault(tok, []).append(i)
    # Consume positions left-to-right via an index cursor per token.
    cursor: Dict[str, int] = {}

    aligned: List[int] = []
    for tok in tag_tokens:
        plist = positions.get(tok)
        if not plist:
            continue
        c = cursor.get(tok, 0)
        if c < len(plist):
            aligned.append(plist[c])
            cursor[tok] = c + 1
    return aligned


def _tau_from_positions(aligned: List[int]) -> Optional[float]:
    """Kendall's tau from an aligned-position sequence (None when < 2 items)."""
    pairs = len(aligned) * (len(aligned) - 1) // 2
    if pairs == 0:
        return None
    inversions = _count_inversions(list(aligned))
    return 1.0 - 2.0 * inversions / pairs


def _align_and_tau(
    tag_tokens: List[str],
    vis_tokens: List[str],
    *,
    min_aligned: int = 15,
) -> Optional[float]:
    """Kendall's tau between two token orderings, via first-fit alignment.

    Tau is computed from the inversion count of the aligned visual positions:
    ``1 - 2 * inversions / pairs``.  Returns ``None`` when fewer than
    ``min_aligned`` tokens align (too little signal to judge order —
    inconclusive, never a failure).
    """
    aligned = _align_positions(tag_tokens, vis_tokens)
    if len(aligned) < min_aligned:
        return None
    return _tau_from_positions(aligned)


# ── Orchestration ─────────────────────────────────────────────────────────────

# Default worst-page tau below which a page is flagged for manual review.  Kept
# in sync with ``audit.standards.VERDICT_DEFAULTS['min_reading_order_tau']``.
DEFAULT_FAIL_BELOW = 0.6
DEFAULT_MIN_ALIGNED = 15
# Above this median token length a page's text is degenerate (CID/Type3 fonts
# extract as concatenated word-runs with no spaces) — the page is UNDECODED, not
# mis-ordered, so it is treated as a no-op rather than scored.  Normal prose with
# the 3+ char filter sits around 5-9; 14 is a wide safety margin.
DEFAULT_MAX_MEDIAN_TOKEN_LEN = 14
# The median guard above misses *partially* glued pages: on Type3/LaTeX-math
# /ActualText the prose extracts as space-less word-runs, but a few short repeated
# function words (e.g. "con"/"dence" from a dropped "fi" ligature) hold the median
# <= 14 while most of the prose is still glued.  A token longer than
# DEFAULT_GLUED_TOKEN_LEN is very likely >=2 words glued together; when more than
# DEFAULT_MAX_GLUED_FRACTION of a page's tokens are that long the alignment is too
# unreliable to judge order, so the page is treated as a no-op.  Measured margin:
# normal English pages sit at 0-3% glued, degenerate Type3-math pages at 33-59%.
DEFAULT_GLUED_TOKEN_LEN = 15
DEFAULT_MAX_GLUED_FRACTION = 0.30
# A page with more than this fraction of its tokens inside Table structures is
# table-dominated and excluded from reading-order scoring.
DEFAULT_TABLE_DOMINANCE = 0.6
# GEOMETRICALLY tabular pages: many real pages render as visual tables or
# side-by-side columns while being tagged flat P (in testing, most such
# failing pages had no structural table — unreachable by the
# TABLE_STRUCT_TYPES exclusion above).  Signature: a ``pdftotext -layout`` line
# with a >=3-space INTERNAL gap is a column-gutter line; when at least
# DEFAULT_GEO_TABULAR_FRAC of a page's non-blank lines are gap lines the page
# has no meaningful 1-D reading order, so a low tau there is row-vs-column
# ambiguity, not a defect — the page is a no-op, mirroring the structural
# table exclusion.  Measured on sample documents: benign failing pages
# median 0.49; pages with a real reading-order defect about 0.14.
DEFAULT_GEO_TABULAR_FRAC = 0.5
_GAP_LINE_RE = re.compile(r"\S {3,}\S")
# Small-sample significance: Kendall's tau over few aligned tokens is noisy
# (sigma_tau(n) = sqrt(2(2n+5)/(9n(n-1))) under the null).  For pages with
# fewer than DEFAULT_SMALL_N aligned tokens, a tau just under the threshold is
# indistinguishable from sampling noise, so the page flags only when tau falls
# below ``fail_below - SMALL_N_Z * sigma_tau(n)`` — otherwise it is an honest
# no-op (inconclusive).  Bars at fail_below=0.6: n=15 -> 0.284, n=26 -> 0.371,
# n=39 -> 0.417.  Real defects on short pages align only 20-30 tokens with a
# tau near 0.15, well under the n=26 bar, so the floor of 15 keeps them in
# scope; a floor of 30 would silence that whole class of page.
# GUARD DEFAULT OFF: an offline A/B measured the band's yield at 8
# suppressed docs of which 4 were VERIFIED real block-order defects (el-tau
# geometric evidence) not fixed elsewhere — a 50% recall cost for 4 benign
# suppressions.  The implementation stays behind the kill-switch for future
# recalibration (e.g. a narrower Z), but it must not ship enabled.
DEFAULT_SMALL_N = 40
SMALL_N_Z = 1.64


def _is_degenerate(
    tokens: List[str],
    max_median_len: float,
    *,
    glued_token_len: int = DEFAULT_GLUED_TOKEN_LEN,
    max_glued_fraction: float = DEFAULT_MAX_GLUED_FRACTION,
    glued_guard: bool = True,
) -> bool:
    """True when tokens look un-word-segmented (concatenated CID/Type3 runs).

    Two independent signals: a high *median* token length (fully-degenerate page)
    or, when ``glued_guard`` is set, a high *fraction* of over-long tokens
    (partially-glued page whose short function words mask the median).
    """
    if len(tokens) < 2:
        return False
    lengths = sorted(len(t) for t in tokens)
    if lengths[len(lengths) // 2] > max_median_len:
        return True
    if glued_guard:
        glued = sum(1 for length in lengths if length > glued_token_len) / len(lengths)
        if glued > max_glued_fraction:
            return True
    return False


def _small_n_bar(n: int, fail_below: float, *, z: float = SMALL_N_Z) -> float:
    """Noise-adjusted flag bar for a page with ``n`` aligned tokens.

    ``fail_below - z * sigma_tau(n)`` with the Kendall-tau null variance
    ``sigma^2 = 2(2n+5) / (9n(n-1))``.  A tau above this bar (but below
    ``fail_below``) is statistically indistinguishable from sampling noise at
    this sample size — inconclusive, not a defect.
    """
    if n < 2:
        return float("-inf")
    sigma = (2.0 * (2 * n + 5) / (9.0 * n * (n - 1))) ** 0.5
    return fail_below - z * sigma


def score_sequences(
    tagged_by_page: Dict[int, List[str]],
    visual_by_page: Dict[int, List[str]],
    *,
    min_aligned: int = DEFAULT_MIN_ALIGNED,
    fail_below: float = DEFAULT_FAIL_BELOW,
    max_median_token_len: float = DEFAULT_MAX_MEDIAN_TOKEN_LEN,
    scanned_pages: Optional[List[int]] = None,
    table_pages: Optional[List[int]] = None,
    geo_tabular_pages: Optional[List[int]] = None,
    mu_by_page: Optional[Dict[int, List[str]]] = None,
    glued_guard: bool = True,
    small_n_guard: bool = False,
) -> Dict[str, object]:
    """Score per-page reading order from pre-extracted token sequences.

    A page is *eligible* when it has tagged tokens, has visual tokens, and is in
    neither ``scanned_pages`` (OCR order is degraded — the OCR gate governs those)
    nor ``table_pages`` (table-dominated: cells read in row-major order, which
    legitimately differs from pdftotext's spatial order, so a low tau there is
    not a defect).  Each eligible page gets a tau; pages with too little aligned
    signal score ``None`` and are tallied as ``n_noop_pages`` (never failed).

    **Two-witness mode** (``mu_by_page`` given): a second independent visual order
    (e.g. PyMuPDF) is scored alongside ``pdftotext -layout``, and a page's score
    becomes ``max(pdf_tau, mu_tau)`` — a page escapes the flag if *either* witness
    confirms the tag-tree order.  This raises the bar to flag (both witnesses must
    disagree), cutting false positives where ``pdftotext`` mis-threads multi-column
    layouts.  Eligibility is unchanged — still driven by the primary (pdf) witness,
    so no new pages are scored; a missing/empty mu page (``mu_tau`` None) falls back
    to ``pdf_tau``.  ``mu_by_page=None`` reproduces single-witness behavior exactly.

    Returns a dict with:
      * ``per_page``        {page_idx: tau} for scored pages (the *combined* tau
                            in two-witness mode).
      * ``min_tau``         worst scored tau, or ``None`` if nothing scored.
      * ``failing_pages``   sorted page indices whose tau < ``fail_below``.
      * ``n_scored_pages``  count of pages that produced a tau.
      * ``n_noop_pages``    count of eligible pages that were inconclusive.
      * ``n_table_pages``   count of pages excluded as table-dominated.
      * ``pdf_per_page``    {page_idx: pdf_tau}  — primary-witness tau (transparency).
      * ``mu_per_page``     {page_idx: mu_tau}   — second-witness tau (two-witness only).
    """
    scanned = set(scanned_pages or [])
    tables = set(table_pages or [])
    geo_tabular = set(geo_tabular_pages or [])
    per_page: Dict[int, float] = {}
    pdf_per_page: Dict[int, float] = {}
    mu_per_page: Dict[int, Optional[float]] = {}
    n_noop = 0
    n_table = 0
    n_geo_tabular = 0
    n_small_n = 0
    small_n_noop: List[int] = []

    for page_idx in sorted(tagged_by_page):
        if page_idx in scanned:
            continue
        if page_idx in tables:
            n_table += 1
            continue
        if page_idx in geo_tabular:
            # Visually tabular/columnar layout (flat-tagged): no meaningful 1-D
            # order exists, mirroring the structural table exclusion above.  A
            # page in both lists counts as a table page (first check wins).
            n_geo_tabular += 1
            continue
        vis = visual_by_page.get(page_idx)
        if not vis:
            continue
        tagged = tagged_by_page[page_idx]
        if _is_degenerate(tagged, max_median_token_len, glued_guard=glued_guard):
            n_noop += 1  # undecoded page (CID/Type3) — not scoreable, not a failure
            continue
        al_pdf = _align_positions(tagged, vis)
        if len(al_pdf) < min_aligned:
            n_noop += 1  # eligibility stays driven by the primary witness
            continue
        tau = _tau_from_positions(al_pdf)
        if tau is None:
            n_noop += 1
            continue
        pdf_per_page[page_idx] = tau
        combined, n_win = tau, len(al_pdf)
        if mu_by_page is not None:
            # Two-witness: a page passes if EITHER witness agrees -> max tau.
            al_mu = _align_positions(tagged, mu_by_page.get(page_idx, []))
            mu_tau = (_tau_from_positions(al_mu)
                      if len(al_mu) >= min_aligned else None)
            mu_per_page[page_idx] = mu_tau
            if mu_tau is not None and (
                    mu_tau > combined
                    or (mu_tau == combined and len(al_mu) > n_win)):
                combined, n_win = mu_tau, len(al_mu)
        # Small-sample significance: at n_win < DEFAULT_SMALL_N a tau in the
        # band [bar, fail_below) is sampling noise, not evidence of disorder —
        # the page is inconclusive (no-op), never scored, so it cannot drag
        # min_tau under the verdict threshold.  The witness taus above are
        # still recorded for transparency.
        if (small_n_guard and combined < fail_below
                and n_win < DEFAULT_SMALL_N
                and combined >= _small_n_bar(n_win, fail_below)):
            n_noop += 1
            n_small_n += 1
            small_n_noop.append(page_idx)
            continue
        per_page[page_idx] = combined

    min_tau = min(per_page.values()) if per_page else None
    failing_pages = sorted(p for p, t in per_page.items() if t < fail_below)
    return {
        "per_page": per_page,
        "min_tau": min_tau,
        "failing_pages": failing_pages,
        "n_scored_pages": len(per_page),
        "n_noop_pages": n_noop,
        "n_table_pages": n_table,
        "n_geo_tabular_pages": n_geo_tabular,
        "n_small_n_noop_pages": n_small_n,
        "small_n_noop_pages": sorted(small_n_noop),
        "pdf_per_page": pdf_per_page,
        "mu_per_page": mu_per_page,
    }


# ── PDF extraction ─────────────────────────────────────────────────────────────


def _visual_sequence_and_layout(
    pdf_path: str,
    timeout: int = 120,
    *,
    hyphen_normalize: bool = True,
) -> tuple:
    """Visual token order plus per-page layout stats, one ``pdftotext -layout``.

    One subprocess for the whole document; pages are split on the form-feed
    (``\\f``) pdftotext emits between pages, so a 72-page PDF costs one call,
    not 72.  Returns ``(tokens_by_page, gap_frac_by_page)`` where
    ``gap_frac_by_page[page]`` is the fraction of the page's non-blank layout
    lines containing a >=3-space *internal* gap — the column-gutter signature
    ``pdftotext -layout`` renders for side-by-side content.  Computed on the
    raw text before tokenization.
    """
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, timeout=timeout,
        ).stdout.decode("utf-8", errors="replace")
    except Exception:
        return {}, {}
    tokens_by_page: Dict[int, List[str]] = {}
    gap_frac_by_page: Dict[int, float] = {}
    for i, page in enumerate(out.split("\f")):
        tokens_by_page[i] = tokenize(page, split_hyphens=hyphen_normalize)
        lines = [ln for ln in page.splitlines() if ln.strip()]
        gap = sum(1 for ln in lines if _GAP_LINE_RE.search(ln.strip()))
        gap_frac_by_page[i] = gap / len(lines) if lines else 0.0
    return tokens_by_page, gap_frac_by_page


def _visual_sequence_by_page(
    pdf_path: str, timeout: int = 120, *, hyphen_normalize: bool = True,
) -> Dict[int, List[str]]:
    """Column-aware visual token order per page (see _visual_sequence_and_layout).

    Kept as a thin wrapper returning only ``{page_idx: [tokens]}`` — the shape
    consumed by the standalone harnesses and pinned by the unit tests.
    """
    return _visual_sequence_and_layout(
        pdf_path, timeout, hyphen_normalize=hyphen_normalize)[0]


def _pymupdf_sequence_by_page(doc, *, hyphen_normalize: bool = True) -> Dict[int, List[str]]:
    """Second independent visual token order per page, via PyMuPDF ``get_text``.

    Takes the already-open ``fitz`` doc (no second open).  PyMuPDF's text
    extraction uses a different reading-order heuristic than ``pdftotext -layout``;
    the two are used together as independent witnesses so a multi-column page is
    only flagged when BOTH disagree with the tag-tree order.  Returns
    ``{page_idx: [tokens]}``; a page that yields nothing simply contributes an
    empty list (treated as no second-witness signal for that page).
    """
    out: Dict[int, List[str]] = {}
    for i in range(doc.page_count):
        try:
            out[i] = tokenize(doc[i].get_text("text"),
                              split_hyphens=hyphen_normalize)
        except Exception:
            out[i] = []
    return out


# Structure types whose content reads in row-major cell order — its sequence
# legitimately differs from pdftotext's spatial order, so such pages are not
# scored for reading order (see ``_tagged_sequence_by_page`` / ``score_sequences``).
TABLE_STRUCT_TYPES = frozenset({"Table", "TR", "TD", "TH", "THead", "TBody", "TFoot"})

# Structure types whose text is a NON-POSITIONAL description (a VLM-generated /Alt
# or /ActualText caption), not the page's visible running text.  Those tokens have
# no visual position to align against, so on figure-heavy (scanned) or formula-heavy
# (math) documents they scramble the tau and produce false low-tau flags.  They are
# dropped from the reading-order token stream by default (see
# ``_tagged_sequence_by_page``'s ``exclude_figure_text``).  Mirrors the row-major
# rationale for ``TABLE_STRUCT_TYPES``.
FIGURE_STRUCT_TYPES = frozenset({"Figure", "Formula"})


def _tagged_sequence_by_page(doc, *, exclude_figure_text: bool = True,
                             hyphen_normalize: bool = True):
    """DFS walk of the structure tree → (tokens_by_page, table_fraction_by_page).

    Mirrors the screen-reader traversal: descend ``/K`` children in declaration
    order, and at each leaf emit tokens from ``/ActualText`` (preferred — decodable
    even on CID/Type0 fonts) else the leaf's MCR content-stream text.

    Crucially this does NOT fall back to ``page.get_text()`` (geometric order) the
    way ``_extract_struct_text_by_page`` does — substituting geometric order for
    tag-tree order would silently inflate the reading-order score and hide the
    very failure we are trying to detect.  When neither /ActualText nor decodable
    MCR text is available, the page simply yields too few tokens and the scorer
    treats it as an honest no-op.

    When ``exclude_figure_text`` is true (default), tokens inside a Figure/Formula
    subtree (``FIGURE_STRUCT_TYPES``) are NOT emitted: their /Alt or /ActualText is a
    non-positional description, not visible running text, so it only scrambles the
    tau against the visual extractors.  A figure/formula-dominated page then yields
    too few tokens and becomes an honest no-op; mixed pages are scored on their real
    prose only.  Set false to restore the pre-fix behavior (kill-switch).

    ``table_fraction_by_page`` maps page_idx → fraction of that page's (emitted)
    tokens that live inside a Table subtree, so the caller can drop table-dominated
    pages.
    """
    # Reuse the well-tested low-level walkers from the validation gates.
    from validation.gates import (
        _build_mcid_text_index,
        _get_actual_text_from_xref,
        _get_child_se_xrefs,
        _get_mcids_from_k,
    )

    page_xref_to_idx = {doc[i].xref: i for i in range(len(doc))}

    try:
        cat_str = doc.xref_object(doc.pdf_catalog(), compressed=False)
    except Exception:
        return {}, {}
    root_m = re.search(r"/StructTreeRoot\s+(\d+)\s+0\s+R", cat_str)
    if not root_m:
        return {}, {}
    root_xref = int(root_m.group(1))
    try:
        root_str = doc.xref_object(root_xref, compressed=False)
    except Exception:
        return {}, {}

    mcid_index = _build_mcid_text_index(doc)
    tokens_by_page: Dict[int, List[str]] = {}
    total_tok: Dict[int, int] = {}
    table_tok: Dict[int, int] = {}

    def _resolve_pg(obj_str: str, inherited: Optional[int]) -> Optional[int]:
        m = re.search(r"/Pg\s+(\d+)\s+0\s+R", obj_str)
        if m:
            return page_xref_to_idx.get(int(m.group(1)), inherited)
        return inherited

    def _struct_type(obj_str: str) -> str:
        m = re.search(r"/S\s*/(\w+)", obj_str)
        return m.group(1) if m else ""

    def _emit(page_idx: int, text: str, in_table: bool, in_figure: bool) -> None:
        # Figure/Formula descriptions are non-positional — drop them from the
        # reading-order stream so they can't scramble the tau (kill-switch above).
        if in_figure and exclude_figure_text:
            return
        toks = tokenize(text, split_hyphens=hyphen_normalize)
        if toks:
            tokens_by_page.setdefault(page_idx, []).extend(toks)
            total_tok[page_idx] = total_tok.get(page_idx, 0) + len(toks)
            if in_table:
                table_tok[page_idx] = table_tok.get(page_idx, 0) + len(toks)

    def _walk(xref: int, page_idx: Optional[int], in_table: bool,
              in_figure: bool) -> None:
        try:
            obj_str = doc.xref_object(xref, compressed=False)
        except Exception:
            return
        page_idx = _resolve_pg(obj_str, page_idx)
        stype = _struct_type(obj_str)
        in_table = in_table or (stype in TABLE_STRUCT_TYPES)
        in_figure = in_figure or (stype in FIGURE_STRUCT_TYPES)
        children = _get_child_se_xrefs(doc, obj_str)
        if children:
            for cx in children:
                _walk(cx, page_idx, in_table, in_figure)
            return
        if page_idx is None:
            return
        text = _get_actual_text_from_xref(doc, xref)
        if text:
            _emit(page_idx, text, in_table, in_figure)
            return
        for mcid in _get_mcids_from_k(obj_str):
            mcr = mcid_index.get(page_idx, {}).get(mcid, "")
            if mcr:
                _emit(page_idx, mcr, in_table, in_figure)

    root_children = _get_child_se_xrefs(doc, root_str)
    if root_children:
        for cx in root_children:
            _walk(cx, None, False, False)
    else:
        _walk(root_xref, None, False, False)

    table_fraction_by_page = {
        p: table_tok.get(p, 0) / total_tok[p] for p in total_tok if total_tok[p]
    }
    return tokens_by_page, table_fraction_by_page


def score_reading_order(
    tagged_pdf: str,
    *,
    min_aligned: int = DEFAULT_MIN_ALIGNED,
    fail_below: float = DEFAULT_FAIL_BELOW,
    table_dominance: float = DEFAULT_TABLE_DOMINANCE,
    scanned_pages: Optional[List[int]] = None,
    two_witness: bool = True,
    exclude_figure_text: bool = True,
    glued_guard: bool = True,
    hyphen_normalize: bool = True,
    geo_tabular_guard: bool = True,
    geo_tabular_frac: float = DEFAULT_GEO_TABULAR_FRAC,
    small_n_guard: bool = False,
) -> Dict[str, object]:
    """Score a tagged PDF's reading order against its own visual layout.

    Opens ``tagged_pdf`` once, extracts the tag-tree DFS token order and the
    ``pdftotext -layout`` visual token order per page, and returns the
    :func:`score_sequences` verdict dict.  Pages whose tokens are more than
    ``table_dominance`` fraction inside Table structures are excluded (row-major
    cell order legitimately differs from spatial order).  Safe on PDFs with no
    structure tree (nothing scored, ``min_tau`` is ``None``).

    When ``two_witness`` is true (default), a second independent visual order
    (PyMuPDF ``get_text``) is also extracted and a page is flagged only when BOTH
    witnesses disagree with the tag-tree order — cutting false positives where
    ``pdftotext -layout`` mis-threads multi-column layouts.  Set false to restore
    single-witness behavior (kill-switch).

    When ``exclude_figure_text`` is true (default), Figure/Formula descriptions
    (VLM /Alt or /ActualText) are dropped from the tag-tree token stream — they are
    non-positional and otherwise scramble the tau, producing false low-tau flags on
    figure-heavy (scanned) and formula-heavy (math) documents.  Set false to
    restore the pre-fix behavior (kill-switch).
    """
    import fitz

    try:
        doc = fitz.open(tagged_pdf)
    except Exception:
        return score_sequences({}, {}, min_aligned=min_aligned, fail_below=fail_below)
    try:
        tagged_by_page, table_frac = _tagged_sequence_by_page(
            doc, exclude_figure_text=exclude_figure_text,
            hyphen_normalize=hyphen_normalize,
        )
        mu_by_page = (_pymupdf_sequence_by_page(
            doc, hyphen_normalize=hyphen_normalize) if two_witness else None)
    finally:
        doc.close()
    visual_by_page, gap_frac_by_page = _visual_sequence_and_layout(
        tagged_pdf, hyphen_normalize=hyphen_normalize)
    table_pages = [p for p, frac in table_frac.items() if frac > table_dominance]
    geo_tabular_pages = (
        sorted(p for p, frac in gap_frac_by_page.items()
               if frac >= geo_tabular_frac)
        if geo_tabular_guard else None
    )
    result = score_sequences(
        tagged_by_page, visual_by_page,
        min_aligned=min_aligned, fail_below=fail_below,
        scanned_pages=scanned_pages, table_pages=table_pages,
        geo_tabular_pages=geo_tabular_pages,
        mu_by_page=mu_by_page, glued_guard=glued_guard,
        small_n_guard=small_n_guard,
    )
    result["geo_tabular_pages"] = geo_tabular_pages or []
    return result
