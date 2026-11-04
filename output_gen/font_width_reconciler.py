# SPDX-License-Identifier: AGPL-3.0-or-later
"""Post-tagging pass: reconcile PDF width arrays with embedded font programs.

veraPDF PDF/UA rule 7.21.5 t1 requires that, for every embedded font used for
rendering, the advance widths in the PDF font dictionary (``/Widths`` for simple
fonts, ``/W`` for CID fonts) match the embedded font program within ±1. The
content-preserving tagger embeds/substitutes fonts (e.g. Standard-14 → bundled
TrueType, CID re-subset) but can leave the width arrays out of sync with the new
program, producing 7.21.5 failures.

This pass rewrites the PDF width arrays to match the program (NOT the program to
match the arrays). Two font-program families are handled:

* **TrueType** (``/FontFile2``): ``hmtx`` advances are integers every tool agrees
  on, so reconciliation is exact.

* **Simple CFF** (``/FontFile3`` ``/Type1C``) whose Private DICT has a *fractional*
  ``nominalWidthX``/``defaultWidthX``. These are Adobe variable-font instances
  (e.g. ``SourceSansVariable-Roman``). CFF encodes each width as
  ``nominalWidthX + delta``; **veraPDF truncates the fractional nominal to an
  integer**, so its computed width is low by ``frac(nominalWidthX) < 1`` — enough
  to tip an otherwise-correct ``/Widths`` just past the ±1 tolerance of PDF/UA
  7.21.5. Because the disagreement is always < 1 and the tolerance is ±1, an
  integer ``/Widths`` value always exists inside BOTH veraPDF's window and the true
  width's window; we write that. This is a workaround for a veraPDF defect
  (fractional ``nominalWidthX`` truncation), and it stays correct for a
  spec-conformant validator too. CFF fonts with an *integral* Private DICT are not
  affected by the bug and are left untouched, as are Type1 (``/FontFile``) and
  CID-keyed CFF fonts.

The PDF content stream positions glyphs explicitly, so ``/Widths`` is metadata:
rewriting it satisfies veraPDF without moving any rendered glyph (visual diff ~0%).
The embedded font program is never modified.

Public API: :func:`reconcile_font_widths`.
"""

from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import pikepdf
from fontTools.cffLib import CFFFontSet
from fontTools.misc.psCharStrings import T2WidthExtractor
from fontTools.pens.basePen import NullPen
from fontTools.ttLib import TTFont

logger = logging.getLogger(__name__)


@dataclass
class FontWidthFix:
    """Record of one font whose width array was rewritten."""

    basefont: str
    kind: str  # "simple_tt" | "cid_tt" | "simple_cff"
    codes_changed: int


@dataclass
class ReconcileResult:
    """Outcome of a reconcile pass over one PDF."""

    pdf_path: str
    changed: bool = False
    fonts_examined: int = 0
    fonts_fixed: int = 0
    fixes: list = field(default_factory=list)
    skipped: list = field(default_factory=list)  # list[str] "basefont (reason)"
    error: Optional[str] = None


def _program_advances_tt(raw: bytes):
    """Return (unitsPerEm, hmtx, glyph_order, best_cmap) for a TrueType program."""
    tt = TTFont(io.BytesIO(raw), fontNumber=0, lazy=True)
    upm = tt["head"].unitsPerEm
    hmtx = tt["hmtx"]
    glyph_order = tt.getGlyphOrder()
    try:
        cmap = tt.getBestCmap() or {}
    except Exception:
        cmap = {}
    return upm, hmtx, glyph_order, cmap


def _parse_w_array(w_array) -> dict:
    """Parse a CID ``/W`` array into {cid: width}. Handles both forms:
    ``[ c [w0 w1 ...] ]`` and ``[ cFirst cLast w ]``.
    """
    out: dict[int, int] = {}
    arr = list(w_array)
    i = 0
    while i < len(arr):
        if i + 1 < len(arr) and isinstance(arr[i + 1], pikepdf.Array):
            cid = int(arr[i])
            for j, w in enumerate(arr[i + 1]):
                out[cid + j] = int(w)
            i += 2
        elif i + 2 < len(arr):
            c1, c2, w = int(arr[i]), int(arr[i + 1]), int(arr[i + 2])
            for cid in range(c1, c2 + 1):
                out[cid] = w
            i += 3
        else:
            break
    return out


def _iter_fonts(pdf):
    """Yield each unique font dict reachable from page (and nested XObject)
    resources, de-duplicated by object id."""
    seen = set()

    def walk(resources):
        if resources is None:
            return
        fonts = resources.get("/Font")
        if fonts is not None:
            for _, fd in fonts.items():
                try:
                    key = fd.objgen
                except Exception:
                    key = id(fd)
                if key not in seen:
                    seen.add(key)
                    yield fd
        xobjects = resources.get("/XObject")
        if xobjects is not None:
            for _, xobj in xobjects.items():
                try:
                    sub_res = xobj.get("/Resources")
                except Exception:
                    sub_res = None
                yield from walk(sub_res)

    for page in pdf.pages:
        yield from walk(page.get("/Resources"))


def _reconcile_simple_tt(font_dict, tolerance: int):
    """Rewrite a simple TrueType ``/Widths`` to match hmtx. Returns codes changed."""
    desc = font_dict.get("/FontDescriptor")
    if desc is None or "/FontFile2" not in desc:
        return None  # signal "not a simple TT we handle"
    first_char = font_dict.get("/FirstChar")
    widths = font_dict.get("/Widths")
    if first_char is None or widths is None:
        return 0
    first_char = int(first_char)
    wl = [int(w) for w in widths]
    upm, hmtx, _go, cmap = _program_advances_tt(bytes(desc["/FontFile2"].read_bytes()))
    changed = 0
    for code, glyph in cmap.items():
        i = code - first_char
        if 0 <= i < len(wl):
            adv = round(hmtx[glyph][0] * 1000 / upm)
            if abs(wl[i] - adv) > tolerance:
                wl[i] = adv
                changed += 1
    if changed:
        font_dict["/Widths"] = pikepdf.Array(wl)
    return changed


def _build_w_array(cid_to_width: dict):
    """Build a CID ``/W`` array in the ``[cid [w]]`` form from {cid: width}."""
    new_arr = []
    for cid in sorted(cid_to_width):
        new_arr.append(cid)
        new_arr.append(pikepdf.Array([cid_to_width[cid]]))
    return pikepdf.Array(new_arr)


def _differences_map(font_dict) -> dict:
    """Parse ``/Encoding /Differences`` into ``{code: glyph_name}``.

    Returns an empty dict when ``/Encoding`` is absent, is a name (base encoding
    only), or carries no ``/Differences``.
    """
    enc = font_dict.get("/Encoding")
    if enc is None or isinstance(enc, pikepdf.Name):
        return {}
    diffs = enc.get("/Differences") if hasattr(enc, "get") else None
    if diffs is None:
        return {}
    out: dict[int, str] = {}
    code = 0
    for item in diffs:
        if isinstance(item, pikepdf.Name):
            out[code] = str(item).lstrip("/")
            code += 1
        else:
            code = int(item)
    return out


def _cff_glyph_widths(raw: bytes, names):
    """For a simple CFF program, return ``{name: (true_w, vera_w)}`` for ``names``.

    ``true_w`` is the real advance; ``vera_w`` emulates veraPDF by flooring the
    Private DICT ``nominalWidthX``/``defaultWidthX`` before extracting the width.
    Raises ``NotImplementedError`` for CID-keyed CFF (``ROS``/``FDArray``), whose
    per-glyph Private DICTs are out of scope.
    """
    cff = CFFFontSet()
    cff.decompile(io.BytesIO(raw), None)
    td = cff[cff.fontNames[0]]
    if hasattr(td, "ROS") or hasattr(td, "FDArray"):
        raise NotImplementedError("CID-keyed CFF")
    priv = td.Private
    charstrings = td.CharStrings
    nwx = float(getattr(priv, "nominalWidthX", 0) or 0)
    dwx = float(getattr(priv, "defaultWidthX", 0) or 0)
    subrs = getattr(priv, "Subrs", [])
    floor_nwx = math.floor(nwx)
    floor_dwx = math.floor(dwx)
    out = {}
    for name in names:
        if name not in charstrings:
            continue
        cs = charstrings[name]
        cs.draw(NullPen())          # populates cs.width with the true advance
        true_w = cs.width
        # A fresh extractor per glyph — T2WidthExtractor is stateful and returns a
        # stale width if reused across charstrings.
        ex = T2WidthExtractor(subrs, charstrings.globalSubrs, floor_nwx, floor_dwx)
        ex.execute(cs)
        out[name] = (true_w, ex.width)
    return out


def _cff_private_is_fractional(raw: bytes) -> bool:
    """True when the CFF Private DICT ``nominalWidthX``/``defaultWidthX`` is
    fractional — the precondition for veraPDF's truncation false-positive."""
    cff = CFFFontSet()
    cff.decompile(io.BytesIO(raw), None)
    td = cff[cff.fontNames[0]]
    if hasattr(td, "ROS") or hasattr(td, "FDArray"):
        return False
    priv = td.Private
    nwx = float(getattr(priv, "nominalWidthX", 0) or 0)
    dwx = float(getattr(priv, "defaultWidthX", 0) or 0)
    return nwx != int(nwx) or dwx != int(dwx)


def _pick_width(vera_w: float, true_w: float, tolerance: int) -> Optional[int]:
    """Choose an integer ``/Widths`` value satisfying both the veraPDF-emulated and
    true widths, closest to the true width. ``None`` when no such integer exists.

    Because ``|vera_w - true_w| = frac(nominalWidthX) < 1`` and ``tolerance`` is 1,
    the two ±1 windows always overlap on at least one integer — so ``None`` is a
    theoretical guard, reachable only for a future ``tolerance == 0``.
    """
    lo = math.ceil(max(vera_w - tolerance, true_w - tolerance))
    hi = math.floor(min(vera_w + tolerance, true_w + tolerance))
    if lo > hi:
        return None
    return min(range(lo, hi + 1), key=lambda w: (abs(w - true_w), w))


def _reconcile_simple_cff(font_dict, tolerance: int):
    """Rewrite a simple CFF ``/Widths`` for veraPDF's floor(nominalWidthX) bug.

    Returns ``(codes_changed, skip_reason)``: ``codes_changed`` is an int (0 = a
    clean no-op) or ``None`` when the font is skipped, with ``skip_reason`` set.
    """
    desc = font_dict.get("/FontDescriptor")
    if desc is None or "/FontFile3" not in desc:
        return None, "no /FontFile3"
    ff3 = desc["/FontFile3"]
    subtype = str(ff3.get("/Subtype", ""))
    if subtype != "/Type1C":
        # /OpenType may wrap glyf outlines, not CFF; /CIDFontType0C is CID-keyed.
        return None, f"/FontFile3 {subtype or '(no subtype)'} not /Type1C"
    first_char = font_dict.get("/FirstChar")
    widths = font_dict.get("/Widths")
    if first_char is None or widths is None:
        return 0, None
    raw = bytes(ff3.read_bytes())
    if not _cff_private_is_fractional(raw):
        return 0, None  # integral Private DICT → veraPDF computes correctly
    code_to_name = _differences_map(font_dict)
    if not code_to_name:
        return None, "no /Differences code→glyph map"
    first_char = int(first_char)
    wl = [int(w) for w in widths]
    try:
        gw = _cff_glyph_widths(raw, set(code_to_name.values()))
    except NotImplementedError as e:
        return None, str(e)

    picks: dict[int, int] = {}
    for code, name in code_to_name.items():
        i = code - first_char
        if not (0 <= i < len(wl)) or name not in gw:
            continue
        true_w, vera_w = gw[name]
        if abs(wl[i] - vera_w) <= tolerance:
            continue  # already passes veraPDF
        pick = _pick_width(vera_w, true_w, tolerance)
        if pick is None:
            # No integer satisfies both windows; refuse to write a value wrong by
            # the true metric — skip the whole font.
            return None, f"no safe width for code {code}"
        picks[i] = pick

    for i, w in picks.items():
        wl[i] = w
    if picks:
        font_dict["/Widths"] = pikepdf.Array(wl)
    return len(picks), None


def _reconcile_cid_tt(font_dict, tolerance: int):
    """Rewrite — or generate — a CID TrueType ``/W`` to match hmtx.

    Returns codes changed/generated, or ``None`` to signal "not a CID TrueType we
    handle" (CFF/Type1 descendant, or a non-Identity ``CIDToGIDMap``).
    """
    descendants = font_dict.get("/DescendantFonts")
    if descendants is None or len(descendants) == 0:
        return 0
    cidfont = descendants[0]
    desc = cidfont.get("/FontDescriptor")
    if desc is None or "/FontFile2" not in desc:
        return None  # not a CID TrueType
    c2g = cidfont.get("/CIDToGIDMap")
    identity = c2g is None or str(c2g) == "/Identity"
    if not identity:
        return None  # non-identity CIDToGIDMap streams: out of scope, skip
    upm, hmtx, glyph_order, _cmap = _program_advances_tt(
        bytes(desc["/FontFile2"].read_bytes())
    )

    if "/W" in cidfont:
        # Reconcile EXISTING entries against the program.
        current = _parse_w_array(cidfont["/W"])
        merged = dict(current)
        changed = 0
        for cid, width in current.items():
            if cid < len(glyph_order):
                adv = round(hmtx[glyph_order[cid]][0] * 1000 / upm)
                if abs(width - adv) > tolerance:
                    merged[cid] = adv
                    changed += 1
        if changed:
            cidfont["/W"] = _build_w_array(merged)
        return changed

    # GENERATE a /W when absent: a CID font with no /W (and typically no /DW)
    # defaults every glyph to width 1000 (the /DW default), mismatching the
    # program for proportional fonts → veraPDF 7.21.5. Emit /W for every CID
    # whose program advance differs from the effective default width.
    dw = int(cidfont.get("/DW", 1000))
    generated = {}
    for cid in range(len(glyph_order)):
        adv = round(hmtx[glyph_order[cid]][0] * 1000 / upm)
        if abs(adv - dw) > tolerance:
            generated[cid] = adv
    if generated:
        cidfont["/W"] = _build_w_array(generated)
        if "/DW" not in cidfont:
            cidfont["/DW"] = dw
    return len(generated)


def reconcile_font_widths(
    pdf_path: str,
    *,
    tolerance: int = 1,
    in_place: bool = True,
) -> ReconcileResult:
    """Reconcile font width arrays in ``pdf_path`` with their embedded programs.

    Rewrites ``/Widths`` (simple TrueType) and ``/W`` (CID TrueType) entries that
    differ from the program's hmtx advance by more than ``tolerance``, and simple
    CFF ``/Widths`` that fail veraPDF's fractional-``nominalWidthX`` truncation.
    Type1 (``/FontFile``), integral-Private CFF, and CID-keyed CFF are skipped. The
    PDF is saved only if something changed; on a consistent PDF this is a no-op.
    Idempotent. Never raises — a failure is reported in ``ReconcileResult.error``
    and the file is left untouched.
    """
    result = ReconcileResult(pdf_path=pdf_path)
    try:
        pdf = pikepdf.open(pdf_path, allow_overwriting_input=in_place)
    except Exception as e:
        result.error = f"open failed: {e}"
        return result

    try:
        for font_dict in _iter_fonts(pdf):
            result.fonts_examined += 1
            subtype = str(font_dict.get("/Subtype"))
            basefont = str(font_dict.get("/BaseFont", "?"))
            skip_reason = None
            try:
                if subtype == "/Type0":
                    changed = _reconcile_cid_tt(font_dict, tolerance)
                    kind = "cid_tt"
                else:
                    changed = _reconcile_simple_tt(font_dict, tolerance)
                    kind = "simple_tt"
                    if changed is None:
                        # Not a simple TrueType; try the simple-CFF path.
                        changed, skip_reason = _reconcile_simple_cff(
                            font_dict, tolerance)
                        kind = "simple_cff"
            except Exception as e:
                result.skipped.append(f"{basefont} (parse error: {e})")
                continue
            if changed is None:
                reason = skip_reason or f"{subtype} non-TrueType"
                result.skipped.append(f"{basefont} ({reason})")
                continue
            if changed > 0:
                result.fonts_fixed += 1
                result.fixes.append(FontWidthFix(basefont=basefont, kind=kind,
                                                 codes_changed=changed))

        if result.fonts_fixed > 0:
            result.changed = True
            if in_place:
                pdf.save(pdf_path)
    except Exception as e:
        result.error = f"reconcile failed: {e}"
        result.changed = False
    finally:
        pdf.close()

    return result
