# SPDX-License-Identifier: AGPL-3.0-or-later
"""Color contrast accessibility checks (WCAG 1.4.3).

Extracts actual text colors from the source PDF via PyMuPDF when
available, falling back to HTML inline-style parsing otherwise.
"""

from utils.raster import safe_dpi
import logging
import re
from typing import Dict, List, Optional, Tuple

from bs4 import Tag

from audit.base_check import AccessibilityCheck
from utils.color_math import (
    contrast_ratio,
    fitz_color_to_rgb,
    fitz_fill_to_rgb,
    relative_luminance,
    rgb_to_hex,
)

logger = logging.getLogger(__name__)

# Minimum area (sq pts) for a filled rectangle to be considered a background
_MIN_BG_AREA = 100

# Tolerance applied to the WCAG ratio threshold to avoid flagging
# rounding-edge failures (e.g. 4.49:1 vs 4.5:1 requirement) caused by
# 8-bit RGB quantization or sRGB→luminance precision loss.
_CONTRAST_TOLERANCE = 0.05

# When the background cannot be measured, foreground text at or above this
# relative luminance is treated as "must be on a non-white background we
# failed to detect" — flagging it would be a guaranteed false positive
# (visible white glyphs cannot sit on a white background).
_LIGHT_FG_LUMINANCE = 0.5


class ColorContrastCheck(AccessibilityCheck):
    """Check for proper color contrast (WCAG 1.4.3).

    When a PyMuPDF page object is available, extracts real text colors
    and background fills from the PDF.  Otherwise falls back to
    HTML inline-style inspection.
    """

    def __init__(self, soup, add_issue_callback, pdf_page=None):
        super().__init__(soup, add_issue_callback)
        self._pdf_page = pdf_page
        # Lazy-loaded caches
        self._pdf_spans: Optional[List[Dict]] = None
        self._bg_rects: Optional[List[Dict]] = None
        self._page_pixmap = None  # rendered pixmap for bg sampling

    # ── Public entry point ──────────────────────────────────────────

    def check(self) -> None:
        if self._pdf_page is not None:
            self._check_with_pdf()
        else:
            self._check_html_fallback()

    # ── PDF-based checking ──────────────────────────────────────────

    def _check_with_pdf(self) -> None:
        spans = self._get_pdf_spans()
        if not spans:
            # Scanned/image-only page — nothing to check
            self._check_html_fallback()
            return

        bg_rects = self._get_bg_rects()

        # Check all PDF spans directly against their backgrounds.
        # Previous approach tried to match HTML elements → PDF spans, but
        # Docling text often doesn't match raw PDF span text (encoding,
        # ligatures, whitespace), causing most spans to be skipped silently.
        failures: Dict[Tuple[str, str], Dict] = {}

        for span_info in spans:
            fg_rgb = span_info["color"]
            bg_rgb = self._find_bg_color(span_info["bbox"], bg_rects, fg_rgb)

            if bg_rgb is None:
                # Background genuinely undetermined.  Visible white/light
                # glyphs cannot sit on a white background, so flagging them
                # would be a guaranteed false positive — skip.  Dark text on
                # an unknown background is safe to assume white (worst case is
                # high contrast, so no real failure is suppressed).
                if relative_luminance(fg_rgb) >= _LIGHT_FG_LUMINANCE:
                    continue
                bg_rgb = (255, 255, 255)

            ratio = contrast_ratio(fg_rgb, bg_rgb)
            is_large = self._is_large_text_from_span(span_info)
            min_ratio = 3.0 if is_large else 4.5

            if ratio < min_ratio - _CONTRAST_TOLERANCE:
                fg_hex = rgb_to_hex(*fg_rgb)
                bg_hex = rgb_to_hex(*bg_rgb)
                key = (fg_hex, bg_hex)

                if key not in failures:
                    failures[key] = {
                        "ratio": ratio,
                        "min_ratio": min_ratio,
                        "is_large": is_large,
                        "texts": [],
                    }
                # Collect up to 5 snippets per color pair
                if len(failures[key]["texts"]) < 5:
                    snippet = span_info["text"].strip()[:80]
                    if snippet and snippet not in failures[key]["texts"]:
                        failures[key]["texts"].append(snippet)
                # Track the worst ratio for this pair
                if ratio < failures[key]["ratio"]:
                    failures[key]["ratio"] = ratio

        # Emit one issue per unique color pair
        for (fg_hex, bg_hex), info in failures.items():
            text_list = info["texts"]
            sample = "; ".join(f'"{t}"' for t in text_list[:3])
            if len(text_list) > 3:
                sample += f" (+{len(text_list) - 3} more)"

            self.add_issue(
                "insufficient-color-contrast",
                "1.4.3",
                "major",
                description=(
                    f"Insufficient color contrast: {info['ratio']:.2f}:1 "
                    f"(minimum required: {info['min_ratio']:.1f}:1). "
                    f"Text color {fg_hex} on background {bg_hex}. "
                    f"Affected text: {sample}"
                ),
                location={
                    "text_color": fg_hex,
                    "background_color": bg_hex,
                    "contrast_ratio": f"{info['ratio']:.2f}:1",
                    "required_ratio": f"{info['min_ratio']:.1f}:1",
                    "is_large_text": info["is_large"],
                    "affected_text": text_list,
                },
            )

        if not failures:
            self.add_issue(
                "compliant-color-contrast",
                "1.4.3",
                "minor",
                description="Text color contrast meets minimum requirements",
                status="compliant",
            )

    # ── HTML fallback (no PDF available) ────────────────────────────

    def _check_html_fallback(self) -> None:
        """Fall back to HTML inline-style inspection.

        Docling-generated HTML rarely has color styles, so this path
        inspects whatever is available and reports honestly when colors
        cannot be determined.
        """
        found_issue = False
        text_elements = self.soup.find_all(
            ["p", "h1", "h2", "h3", "h4", "h5", "h6",
             "a", "span", "div", "li", "td", "th"]
        )

        for element in text_elements:
            if not self.get_element_text(element).strip():
                continue

            text_color = self._get_css_text_color(element)
            bg_color = self._get_css_bg_color(element)

            if text_color is None or bg_color is None:
                continue

            ratio = contrast_ratio(text_color, bg_color)
            is_large = self._is_large_text_from_html(element)
            min_ratio = 3.0 if is_large else 4.5

            if ratio < min_ratio - _CONTRAST_TOLERANCE:
                found_issue = True
                fg_hex = rgb_to_hex(*text_color)
                bg_hex = rgb_to_hex(*bg_color)
                self.add_issue(
                    "insufficient-color-contrast",
                    "1.4.3",
                    "major",
                    element=element,
                    description=(
                        f"Insufficient color contrast: {ratio:.2f}:1 "
                        f"(minimum required: {min_ratio:.1f}:1)"
                    ),
                    location={
                        "text_color": fg_hex,
                        "background_color": bg_hex,
                        "contrast_ratio": f"{ratio:.2f}:1",
                        "required_ratio": f"{min_ratio:.1f}:1",
                        "is_large_text": is_large,
                    },
                )

        if not found_issue:
            self.add_issue(
                "compliant-color-contrast",
                "1.4.3",
                "minor",
                description="Text color contrast meets minimum requirements",
                status="compliant",
            )

    # ── PDF span extraction ─────────────────────────────────────────

    def _get_pdf_spans(self) -> List[Dict]:
        if self._pdf_spans is not None:
            return self._pdf_spans

        self._pdf_spans = []
        try:
            text_dict = self._pdf_page.get_text("dict", sort=True)
        except Exception as e:
            logger.debug("get_text('dict') failed: %s", e)
            return self._pdf_spans

        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:  # text blocks only
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    if not text.strip():
                        continue
                    self._pdf_spans.append({
                        "text": text,
                        "color": fitz_color_to_rgb(span.get("color", 0)),
                        "size": span.get("size", 12.0),
                        "flags": span.get("flags", 0),
                        "bbox": tuple(span.get("bbox", (0, 0, 0, 0))),
                    })

        return self._pdf_spans

    def _get_bg_rects(self) -> List[Dict]:
        if self._bg_rects is not None:
            return self._bg_rects

        self._bg_rects = []
        try:
            drawings = self._pdf_page.get_drawings()
        except Exception as e:
            logger.debug("get_drawings() failed: %s", e)
            return self._bg_rects

        for d in drawings:
            fill = d.get("fill")
            if fill is None:
                continue
            fill_opacity = d.get("fill_opacity", 1.0)
            if fill_opacity is not None and fill_opacity <= 0.0:
                continue  # fully transparent — invisible
            rect = d.get("rect")
            if rect is None:
                continue
            # Filter to rectangles large enough to be backgrounds
            w = abs(rect[2] - rect[0])
            h = abs(rect[3] - rect[1])
            if w * h < _MIN_BG_AREA:
                continue
            rgb = fitz_fill_to_rgb(fill)
            # Skip white fills — they're the default anyway
            if rgb == (255, 255, 255):
                continue
            self._bg_rects.append({
                "fill": rgb,
                "fill_opacity": fill_opacity if fill_opacity is not None else 1.0,
                "rect": rect,
                "area": w * h,
            })

        # Sort by area ascending — smallest first for tightest-fit lookup
        self._bg_rects.sort(key=lambda r: r["area"])
        return self._bg_rects

    # ── Text matching ───────────────────────────────────────────────

    def _find_span_for_text(
        self, el_text: str, spans: List[Dict]
    ) -> Optional[Dict]:
        """Find the best-matching PDF span for an HTML element's text."""
        normalized = _normalize_text(el_text)
        if not normalized:
            return None

        words = normalized.split()
        if not words:
            return None

        best_span = None
        best_score = 0.0

        # For short text, try exact substring match
        if len(words) <= 4:
            for span in spans:
                span_norm = _normalize_text(span["text"])
                if normalized in span_norm or span_norm in normalized:
                    return span

        # Prefix match on first N words
        prefix = " ".join(words[:5])
        for span in spans:
            span_norm = _normalize_text(span["text"])
            if not span_norm:
                continue

            # Check if prefix appears in span or vice versa
            if prefix in span_norm or span_norm.startswith(prefix[:len(span_norm)]):
                return span

            # Word overlap score
            span_words = set(span_norm.split())
            el_words = set(words[:10])  # limit to first 10 words
            if not span_words or not el_words:
                continue
            overlap = len(span_words & el_words)
            score = overlap / max(len(span_words), len(el_words))
            if score > best_score and score >= 0.4:
                best_score = score
                best_span = span

        return best_span

    # ── Background color lookup ─────────────────────────────────────

    def _find_bg_color(
        self, bbox: Tuple, bg_rects: List[Dict],
        fg_rgb: Optional[Tuple[int, int, int]] = None,
    ) -> Optional[Tuple[int, int, int]]:
        """Find the background color for a text span's bounding box.

        Returns the fill color of the smallest *opaque* rectangle that
        fully contains the bbox.  When a semi-transparent rectangle is
        the best match, or no vector rectangle covers the text, falls
        back to sampling the rendered page pixmap (catches transparency,
        image backgrounds, pattern/gradient fills, etc.).

        Returns ``None`` when the background genuinely cannot be
        determined — this is distinct from a *measured* white background
        and lets the caller avoid false white-on-white failures.
        """
        x0, y0, x1, y1 = bbox
        for rect_info in bg_rects:  # sorted smallest-first
            rx0, ry0, rx1, ry1 = rect_info["rect"][:4]
            if rx0 <= x0 and ry0 <= y0 and rx1 >= x1 and ry1 >= y1:
                if rect_info.get("fill_opacity", 1.0) >= 1.0:
                    return rect_info["fill"]
                # Semi-transparent rect covers the text — need pixmap

        # Semi-transparent background or no vector background found —
        # sample rendered page pixels for ground-truth composited color
        if self._pdf_page is not None:
            sampled = self._sample_bg_from_pixmap(bbox, fg_rgb)
            if sampled is not None:
                return sampled

        return None

    def _sample_bg_from_pixmap(
        self, bbox: Tuple,
        fg_rgb: Optional[Tuple[int, int, int]] = None,
    ) -> Optional[Tuple[int, int, int]]:
        """Sample background color from the rendered page at the text bbox.

        Renders the page once (cached) and samples pixel rows *within* the
        text band — primarily the mid-height row across the bbox width.
        The glyph strokes occupy only a small fraction of that row, so
        excluding the foreground (and, for light text, near-white) pixels
        and taking the dominant remaining color reliably recovers a
        colored box/banner background — even tight banners and
        pattern/gradient fills that ``get_drawings()`` reports as having
        no vector fill.

        Returns ``None`` only when no usable background pixels remain
        (genuinely undetermined).  A *measured* white IS returned.
        """
        try:
            if self._page_pixmap is None:
                # Render at 72 DPI (1:1 with PDF points) — sufficient
                # for color sampling and keeps memory low.
                self._page_pixmap = self._pdf_page.get_pixmap(
                    dpi=safe_dpi(self._pdf_page.rect.width, self._pdf_page.rect.height, 72))

            pm = self._page_pixmap
            x0, y0, x1, y1 = bbox

            # Sample rows inside the text band where a colored background is
            # guaranteed to exist: the mid-height row plus the just-inside
            # top/bottom rows.  (The old approach sampled 2px outside the
            # bbox, which missed tight banners and landed on the page margin.)
            mid_y = int((y0 + y1) / 2)
            rows = {mid_y, int(y0) + 1, int(y1) - 1}
            xa, xb = int(x0), int(x1)
            if xb <= xa:
                xb = xa + 1

            fg_light = fg_rgb is not None and relative_luminance(fg_rgb) >= 0.5

            colors: List[Tuple[int, int, int]] = []
            for sy in rows:
                sy_c = max(0, min(sy, pm.height - 1))
                for sx in range(xa, xb):
                    sx_c = max(0, min(sx, pm.width - 1))
                    pixel = pm.pixel(sx_c, sy_c)  # (r, g, b[, a])
                    c = (pixel[0], pixel[1], pixel[2])
                    # Drop foreground glyph pixels so they don't pollute the bg.
                    if fg_rgb is not None and _color_close(c, fg_rgb, 24):
                        continue
                    # Anti-aliased edges of light text read as near-white; drop
                    # them too, but only when the text itself is light (a real
                    # white page background must survive when text is dark).
                    if fg_light and min(c) >= 235:
                        continue
                    colors.append(c)

            if len(colors) < 2:
                return None

            # Pick the dominant color: quantize to merge anti-aliasing noise,
            # take the most frequent bucket, return the average of its members.
            buckets: Dict[Tuple[int, int, int], List[Tuple[int, int, int]]] = {}
            for c in colors:
                key = (c[0] // 16, c[1] // 16, c[2] // 16)
                buckets.setdefault(key, []).append(c)
            members = max(buckets.values(), key=len)
            n = len(members)
            result = (
                sum(c[0] for c in members) // n,
                sum(c[1] for c in members) // n,
                sum(c[2] for c in members) // n,
            )

            # A narrow/small glyph (e.g. a 7pt table digit ~3px wide at 72 DPI)
            # can leave no clean background pixels in the sampled band: after
            # dropping the near-black foreground, only the anti-aliased fg→white
            # edge survives, so the dominant bucket is a neutral mid-gray that is
            # not a real background.  When ``result`` carries that signature the
            # true page background is white — substitute it so dark text is
            # measured against white (its real, high-contrast ground) instead of
            # a fabricated gray.  Genuinely low-contrast foregrounds (light gray,
            # colored) still fail against white, so no real failure is masked.
            if fg_rgb is not None and _is_antialiased_gray(result, fg_rgb):
                return (255, 255, 255)

            return result

        except Exception as e:
            logger.debug("pixmap bg sampling failed: %s", e)
            return None

    # ── Large text detection ────────────────────────────────────────

    @staticmethod
    def _is_large_text_from_span(span_info: Dict) -> bool:
        """Determine large text from PDF font metrics.

        WCAG large text: >= 18pt, or >= 14pt and bold.
        """
        size = span_info.get("size", 12.0)
        is_bold = bool(span_info.get("flags", 0) & (1 << 4))
        if size >= 18.0:
            return True
        if size >= 14.0 and is_bold:
            return True
        return False

    def _is_large_text_from_html(self, element: Tag) -> bool:
        """Determine large text from HTML element/styles."""
        if element.name in ("h1", "h2", "h3"):
            return True
        style = element.get("style", "")
        if not style:
            return False
        size_match = re.search(
            r"font-size:\s*(\d+(?:\.\d+)?)\s*(px|pt|em|rem)", style
        )
        if size_match:
            size = float(size_match.group(1))
            unit = size_match.group(2)
            if unit == "pt":
                size_px = size * 1.333
            elif unit in ("em", "rem"):
                size_px = size * 16
            else:
                size_px = size
            if size_px >= 24:
                return True
            if size_px >= 18.67:
                weight = re.search(r"font-weight:\s*(bold|[7-9]00)", style)
                if weight or element.name in ("b", "strong"):
                    return True
        return False

    # ── CSS color extraction (fallback) ─────────────────────────────

    _COLOR_RE = re.compile(
        r"(?<!background-)color:\s*"
        r"(#[0-9a-fA-F]{3,6}|rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\))"
    )
    _BG_COLOR_RE = re.compile(
        r"background-color:\s*"
        r"(#[0-9a-fA-F]{3,6}|rgb\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\))"
    )

    def _get_css_text_color(self, element: Tag) -> Optional[Tuple[int, int, int]]:
        style = element.get("style", "")
        if style:
            m = self._COLOR_RE.search(style)
            if m:
                return _parse_css_color(m.group(1))
        return None

    def _get_css_bg_color(self, element: Tag) -> Optional[Tuple[int, int, int]]:
        el = element
        while el and el.name != "html":
            style = el.get("style", "")
            if style:
                m = self._BG_COLOR_RE.search(style)
                if m:
                    return _parse_css_color(m.group(1))
            el = el.parent
        # Only return white if we found an explicit fg color (caller checks None)
        return (255, 255, 255) if element.get("style") and self._COLOR_RE.search(element.get("style", "")) else None


# ── Module-level helpers ────────────────────────────────────────────

def _normalize_text(text: str) -> str:
    """Collapse whitespace and lowercase for matching."""
    return re.sub(r"\s+", " ", text.strip().lower())


# Max per-channel spread for a color to count as "neutral gray".
_GRAY_NEUTRAL_TOL = 20
# Backgrounds at or above this relative luminance are treated as effectively
# white already, so no anti-aliasing substitution is needed (and genuine
# gray-on-near-white failures must be preserved).
_AA_GRAY_MAX_LUMINANCE = 0.98


def _is_antialiased_gray(
    bg: Tuple[int, int, int], fg: Tuple[int, int, int]
) -> bool:
    """True when a sampled background carries the anti-aliasing signature of
    dark/neutral text on a white page.

    A tiny glyph sampled from the rendered pixmap can yield a neutral mid-gray
    "background" that is really the fg→white anti-aliased edge, not a real fill.
    The signature is: ``bg`` is a neutral gray, strictly lighter than ``fg`` and
    strictly darker than white.  Saturated/colored backgrounds (real banners,
    boxes) and near-white backgrounds (genuine gray-on-white failures) do not
    match, so substituting white for this case removes false failures without
    masking real ones.
    """
    if bg == (255, 255, 255):
        return False
    if max(bg) - min(bg) > _GRAY_NEUTRAL_TOL:  # not neutral → a real color
        return False
    return relative_luminance(fg) < relative_luminance(bg) < _AA_GRAY_MAX_LUMINANCE


def _color_close(
    a: Tuple[int, int, int], b: Tuple[int, int, int], tol: int
) -> bool:
    """True when two RGB colors are within ``tol`` on every channel."""
    return (
        abs(a[0] - b[0]) <= tol
        and abs(a[1] - b[1]) <= tol
        and abs(a[2] - b[2]) <= tol
    )


def _parse_css_color(css_val: str) -> Optional[Tuple[int, int, int]]:
    """Parse a CSS hex or rgb() value to an (R, G, B) tuple."""
    css_val = css_val.strip()
    if css_val.startswith("#"):
        h = css_val[1:]
        if len(h) == 3:
            return (int(h[0] * 2, 16), int(h[1] * 2, 16), int(h[2] * 2, 16))
        if len(h) == 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    m = re.match(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", css_val)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None
