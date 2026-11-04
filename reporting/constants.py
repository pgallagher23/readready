# SPDX-License-Identifier: AGPL-3.0-or-later
"""Layer 0: pure data + remediation-tips loader for the reporting package.

No dependency on any other reporting module, on app.py, or on gradio.
"""
import re
from pathlib import Path

import logging
logger = logging.getLogger(__name__)

from utils.branding import get_branding as _get_branding
_B = _get_branding()
BRAND_PRIMARY = _B.primary
BRAND_ACCENT = _B.accent
BRAND_WARM_BG = _B.warm_bg

# ── Severity badge styles (shared by page-issues and criteria table) ──
_SEV_BADGE = {
    "critical": "background:#fee2e2;color:#7f1d1d;border:1px solid #fca5a5;",
    "major":    "background:#ffedd5;color:#7c2d12;border:1px solid #fdba74;",
    "minor":    "background:#fef9c3;color:#713f12;border:1px solid #fde047;",
}
_SEV_DEFAULT = "background:#f1f5f9;color:#334155;border:1px solid #cbd5e1;"

# ── Remediation tips (loaded from static/remediation_tips.yaml) ───────

def _md_to_html(text: str) -> str:
    """Convert markdown bold and code to HTML in remediation step strings."""
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(
        r'`(.+?)`',
        r'<code style="font-family:inherit;font-size:inherit;background:#e0f2fe;'
        r'padding:1px 4px;border-radius:2px;">\1</code>',
        text,
    )
    return text


def _load_remediation_tips() -> tuple[dict, dict, dict]:
    """Load remediation tips from static/remediation_tips.yaml.

    Returns (remediation_info, fix_hints, display_names) dicts.
    Converts markdown in acrobat_steps to HTML.
    """
    tips_path = Path(__file__).resolve().parent.parent / "static" / "remediation_tips.yaml"
    try:
        import yaml
        data = yaml.safe_load(tips_path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Could not load remediation_tips.yaml — using empty tips")
        return {}, {}, {}

    remediation_info: dict[str, dict] = {}
    fix_hints: dict[str, str] = {}
    display_names: dict[str, str] = {}

    for entry in data.get("entries", []):
        # Convert markdown steps to HTML
        entry["acrobat_steps"] = [_md_to_html(s) for s in entry.get("acrobat_steps", [])]

        keys = entry.get("keys", [])
        for key in keys:
            remediation_info[key] = entry
            if entry.get("fix_hint"):
                fix_hints[key] = entry["fix_hint"]
            if entry.get("display_name"):
                display_names[key] = entry["display_name"]

    # Merge fallback sections (lower priority — don't overwrite entry-derived values)
    for key, val in data.get("display_names", {}).items():
        display_names.setdefault(key, val)
    for key, val in data.get("fix_hints", {}).items():
        fix_hints[key] = val  # fix_hints overrides allow per-key specificity

    return remediation_info, fix_hints, display_names


_ACROBAT_REMEDIATION_INFO, _FIX_HINTS, _ISSUE_DISPLAY_NAMES = _load_remediation_tips()


def _get_remediation_info(issue_type: str) -> dict | None:
    """Look up Acrobat remediation info for an issue type, with prefix fallback."""
    if issue_type in _ACROBAT_REMEDIATION_INFO:
        return _ACROBAT_REMEDIATION_INFO[issue_type]
    # Prefix match (e.g. "table-missing-headers-complex" -> "table-missing-headers")
    for key in _ACROBAT_REMEDIATION_INFO:
        if issue_type.startswith(key):
            return _ACROBAT_REMEDIATION_INFO[key]
    return None


# Default severity for SR issues (pdf-* types) which lack a severity field.
_SR_DEFAULT_SEVERITY = {
    "pdf-no-structure-tree": "critical",
    "pdf-not-marked": "critical",
    "pdf-hollow-pages": "critical",
    "pdf-no-document-lang": "major",
    "pdf-low-text-coverage": "major",
    "pdf-reading-order": "major",
    "pdf-fair-screen-reader-quality": "major",
    "pdf-poor-screen-reader-quality": "critical",
    "pdf-poor-ocr-quality": "major",
    "pdf-blank-structure-elements": "minor",
    "pdf-blank-subtrees": "minor",
    "pdf-figures-missing-alt": "minor",
}


_CE_LINK = (
    '<a href="#" onclick="return window._gotoContentEditor(this)" '
    'style="color:#1d4ed8;font-weight:700;text-decoration:underline;'
    'cursor:pointer;">Content Editor</a>'
)
