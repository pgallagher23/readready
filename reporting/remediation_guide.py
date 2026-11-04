# SPDX-License-Identifier: AGPL-3.0-or-later
"""Layer 3: build the per-document Improvement/Remediation Guide HTML."""
from html import escape

from audit.standards import verdict_from_sr
from output_gen.report_page import _NAMES, _LEVEL, _WCAG_URL
from reporting.constants import (
    _SEV_BADGE, _SEV_DEFAULT, _FIX_HINTS, _ISSUE_DISPLAY_NAMES,
    _get_remediation_info, BRAND_PRIMARY, BRAND_ACCENT, BRAND_WARM_BG,
)
from reporting.issue_location import (
    _match_issues_to_bboxes, _render_issue_thumbnail, _render_coverage_thumbnail,
)
from reporting.issue_aggregation import (
    _aggregate_issues_by_type, _sr_is_poor, _sr_action_items, _verdict_gate_reasons,
)


def _render_color_swatches(issues: list[dict]) -> str:
    """Render color swatch HTML for color contrast issues."""
    seen_pairs: set[tuple] = set()
    swatches: list[str] = []

    for issue in issues[:10]:  # Cap at 10 unique pairs
        loc = issue.get("location") or {}
        fg = loc.get("text_color", "")
        bg = loc.get("background_color", "")
        ratio = loc.get("contrast_ratio", "")
        required = loc.get("required_ratio", "")
        affected = loc.get("affected_text", [])

        if not fg or not bg:
            continue
        pair = (fg, bg)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        sample = ""
        if affected and isinstance(affected, list):
            sample_text = affected[0][:60] + ("..." if len(affected[0]) > 60 else "")
            sample = (
                f'<span style="color:#4b5563;font-size:0.6875rem;margin-left:8px;">'
                f'e.g. &ldquo;{escape(sample_text)}&rdquo;</span>'
            )

        swatches.append(
            f'<div style="display:flex;align-items:center;gap:6px;margin:4px 0;">'
            f'<div style="width:24px;height:24px;background:{escape(fg)};'
            f'border:1px solid #ccc;border-radius:4px;" title="Text: {escape(fg)}"></div>'
            f'<span style="font-size:0.75rem;color:#4b5563;">on</span>'
            f'<div style="width:24px;height:24px;background:{escape(bg)};'
            f'border:1px solid #ccc;border-radius:4px;" title="Background: {escape(bg)}"></div>'
            f'<span style="font-size:0.75rem;font-weight:600;">{escape(str(ratio))}'
            f'{" (needs " + escape(str(required)) + ")" if required else ""}</span>'
            f'{sample}'
            f'</div>'
        )

    if not swatches:
        return ""
    return (
        '<div style="margin:10px 0;padding:10px;background:#fafafa;'
        'border:1px solid #e5e7eb;border-radius:6px;">'
        '<div style="font-size:0.75rem;font-weight:600;color:#374151;margin-bottom:6px;">'
        'Color Pairs</div>'
        + "\n".join(swatches)
        + '</div>'
    )


def _build_remediation_section(
    agg: dict,
    issues_for_type: list[dict],
    pdf_path: str,
    structure: dict,
    pages_info: dict,
) -> str:
    """Build one collapsible <details> section for an issue type."""
    itype = agg["type"]
    severity = agg.get("severity", "minor")
    pages = agg.get("pages", [])
    info = _get_remediation_info(itype)

    display_name = info["display_name"] if info else _ISSUE_DISPLAY_NAMES.get(itype, itype)
    badge_style = _SEV_BADGE.get(severity, _SEV_DEFAULT)
    fix_hint = _FIX_HINTS.get(itype, "")
    count = len(issues_for_type)

    # Non-gating: this issue type is a best-practice suggestion, not a WCAG
    # failure, when every matched issue is informational.
    is_informational = bool(issues_for_type) and all(
        i.get("remediation_status") == "informational" for i in issues_for_type
    )
    bp_badge = (
        '<span style="background:#f3f4f6;color:#374151;border-radius:4px;'
        'padding:1px 6px;font-size:0.6875rem;margin-right:6px;">Best practice</span>'
        if is_informational else ""
    )

    # Instance count string
    effort_str = f'{count} instance{"s" if count != 1 else ""}'

    html_parts = [
        '<details open style="margin-bottom:12px;">',
        f'<summary style="cursor:pointer;padding:12px 16px;background:#fff;'
        f'border:1px solid #e5e7eb;border-radius:8px;list-style:none;'
        f'display:flex;align-items:center;gap:10px;user-select:none;">',
        f'<span style="padding:2px 8px;border-radius:4px;font-size:0.6875rem;'
        f'font-weight:700;text-transform:uppercase;{badge_style}">{escape(severity)}</span>',
        f'<span style="font-weight:600;color:#1f2937;">{bp_badge}{escape(display_name)}</span>',
        f'<span style="margin-left:auto;color:#4b5563;font-size:0.75rem;">'
        f'{effort_str}</span>',
        '</summary>',
        '<div style="padding:16px;border:1px solid #e5e7eb;border-top:none;'
        'border-radius:0 0 8px 8px;background:#fff;">',
    ]

    # (WCAG criterion reference removed — not actionable for end users)

    # Source edit warning
    if info and info.get("requires_source_edit"):
        html_parts.append(
            '<div style="background:#fef3c7;border:1px solid #f59e0b;border-radius:6px;'
            'padding:10px 14px;margin-bottom:12px;font-size:0.8125rem;color:#713507;">'
            '<b>&#9888; Requires source document edit</b> &mdash; this issue cannot be '
            'fully fixed in Acrobat Pro alone. Edit the original file (Word, InDesign, etc.) '
            'and re-export to PDF.</div>'
        )

    # Affected pages
    if pages and not agg.get("is_document_wide"):
        page_display = ", ".join(str(p) for p in pages[:20])
        extra = f" (+{len(pages) - 20} more)" if len(pages) > 20 else ""
        html_parts.append(
            f'<div style="font-size:0.8125rem;color:#374151;margin-bottom:10px;">'
            f'<b>Affected pages:</b> {page_display}{extra}</div>'
        )
    elif agg.get("is_document_wide"):
        html_parts.append(
            '<div style="font-size:0.8125rem;color:#374151;margin-bottom:10px;">'
            '<b>Scope:</b> Document-wide issue</div>'
        )

    # Thumbnails
    is_coverage_type = any(
        k in itype for k in ("coverage", "hollow", "screen-reader-quality")
    )
    if pdf_path and pages and not agg.get("is_document_wide"):
        issue_bboxes = _match_issues_to_bboxes(issues_for_type, structure)
        all_elements = structure.get("elements", [])
        max_thumbs = 30 if is_coverage_type else 3

        # Render thumbnails
        thumb_data: list[tuple[int, str]] = []  # (page_no, base64)
        pages_shown = set()
        for pg in pages:
            if len(thumb_data) >= max_thumbs:
                break
            if pg in pages_shown:
                continue
            pages_shown.add(pg)

            if is_coverage_type:
                b64 = _render_coverage_thumbnail(
                    pdf_path, pg, all_elements, pages_info, dpi=100,
                )
            else:
                bbox = None
                for entry in issue_bboxes.get(pg, []):
                    if entry.get("bbox"):
                        bbox = entry["bbox"]
                        break
                b64 = _render_issue_thumbnail(
                    pdf_path, pg, bbox, pages_info, dpi=120, annotate=True,
                )

            if b64:
                thumb_data.append((pg, b64))

        if thumb_data and is_coverage_type and len(thumb_data) > 1:
            # ── Coverage carousel ──
            cid = f"cov_{id(agg)}_{itype.replace('-','_')}"
            legend = (
                '<div style="display:flex;gap:14px;flex-wrap:wrap;'
                'font-size:0.6875rem;color:#4b5563;margin-bottom:6px;">'
                '<span>Highlighted = tagged content:</span>'
                '<span><span style="display:inline-block;width:12px;height:12px;'
                'background:rgba(38,179,77,0.35);border:1px solid rgba(38,179,77,0.7);'
                'border-radius:2px;vertical-align:middle;margin-right:3px;"></span>Text</span>'
                '<span><span style="display:inline-block;width:12px;height:12px;'
                'background:rgba(51,128,230,0.35);border:1px solid rgba(51,128,230,0.7);'
                'border-radius:2px;vertical-align:middle;margin-right:3px;"></span>Figures</span>'
                '<span><span style="display:inline-block;width:12px;height:12px;'
                'background:rgba(230,153,26,0.35);border:1px solid rgba(230,153,26,0.7);'
                'border-radius:2px;vertical-align:middle;margin-right:3px;"></span>Tables</span>'
                '<span style="margin-left:4px;">Bare areas = untagged gaps</span>'
                '</div>'
            )

            # Build slide divs (only first visible, rest hidden)
            slides = []
            for idx, (pg, b64) in enumerate(thumb_data):
                vis = "block" if idx == 0 else "none"
                slides.append(
                    f'<div id="{cid}_s{idx}" style="display:{vis};text-align:center;">'
                    f'<button type="button" aria-label="Enlarge page {pg}" '
                    f"onclick=\"window._showLightbox(this.querySelector('img').src);\" "
                    f'style="background:none;border:none;padding:0;cursor:zoom-in;">'
                    f'<img src="data:image/png;base64,{b64}" loading="lazy" '
                    f'alt="Page {pg} thumbnail" '
                    f'style="max-height:500px;max-width:100%;border:1px solid #d1d5db;'
                    f'border-radius:6px;" />'
                    f'</button>'
                    f'<div style="font-size:0.75rem;color:#4b5563;margin-top:6px;">'
                    f'Page {pg}</div></div>'
                )
            slides_html = "\n".join(slides)

            remaining = len(pages) - len(pages_shown)
            remaining_note = (
                f' (+{remaining} more not shown)'
            ) if remaining > 0 else ""

            btn = (
                'style="background:none;border:1px solid #d1d5db;border-radius:6px;'
                'width:36px;height:36px;font-size:1.125rem;cursor:pointer;'
                'display:flex;align-items:center;justify-content:center;'
                'color:#374151;transition:background 0.15s;"'
                ' onmouseover="this.style.background=\'#f3f4f6\'"'
                ' onmouseout="this.style.background=\'none\'"'
            )

            carousel_html = f'''
            {legend}
            <div style="margin:12px 0;border:1px solid #e5e7eb;border-radius:8px;
                        padding:12px;background:#fafafa;">
              <div style="display:flex;align-items:center;justify-content:space-between;
                          margin-bottom:8px;">
                <button aria-label="Previous page" {btn} onclick="(function(){{
                  var c=document.getElementById('{cid}_cur');
                  var n={len(thumb_data)};
                  var i=(parseInt(c.dataset.i)-1+n)%n;
                  document.getElementById('{cid}_s'+c.dataset.i).style.display='none';
                  document.getElementById('{cid}_s'+i).style.display='block';
                  c.dataset.i=i;
                  c.textContent='Page '+ document.getElementById('{cid}_s'+i).querySelector('div').textContent.replace('Page ','') +' — '+(i+1)+' of '+n;
                }})()">&lsaquo;</button>
                <span id="{cid}_cur" data-i="0"
                      style="font-size:0.8125rem;font-weight:600;color:#374151;">
                  Page {thumb_data[0][0]} — 1 of {len(thumb_data)}{remaining_note}</span>
                <button aria-label="Next page" {btn} onclick="(function(){{
                  var c=document.getElementById('{cid}_cur');
                  var n={len(thumb_data)};
                  var i=(parseInt(c.dataset.i)+1)%n;
                  document.getElementById('{cid}_s'+c.dataset.i).style.display='none';
                  document.getElementById('{cid}_s'+i).style.display='block';
                  c.dataset.i=i;
                  c.textContent='Page '+ document.getElementById('{cid}_s'+i).querySelector('div').textContent.replace('Page ','') +' — '+(i+1)+' of '+n;
                }})()">&rsaquo;</button>
              </div>
              {slides_html}
            </div>
            '''
            html_parts.append(carousel_html)

        elif thumb_data:
            # ── Standard thumbnail grid (max 3) ──
            thumb_htmls = [
                f'<div style="text-align:center;">'
                f'<button type="button" aria-label="Enlarge page {pg}" '
                f"onclick=\"window._showLightbox(this.querySelector('img').src);\" "
                f'style="background:none;border:none;padding:0;cursor:zoom-in;">'
                f'<img src="data:image/png;base64,{b64}" '
                f'alt="Page {pg} thumbnail" '
                f'style="max-height:400px;max-width:100%;border:1px solid #d1d5db;border-radius:6px;" />'
                f'</button>'
                f'<div style="font-size:0.6875rem;color:#4b5563;margin-top:4px;">Page {pg}'
                f' <span style="color:#1e3a8a;font-size:0.625rem;">click to enlarge</span></div>'
                f'</div>'
                for pg, b64 in thumb_data
            ]
            remaining = len(pages) - len(pages_shown)
            extra_note = (
                f'<div style="font-size:0.6875rem;color:#4b5563;margin-top:4px;">'
                f'+ {remaining} more page{"s" if remaining != 1 else ""} affected</div>'
            ) if remaining > 0 else ""
            html_parts.append(
                '<div style="display:flex;gap:12px;flex-wrap:wrap;margin:12px 0;">'
                + "\n".join(thumb_htmls)
                + '</div>'
                + extra_note
            )

    # Color swatches (contrast issues only)
    if "contrast" in itype:
        swatch_html = _render_color_swatches(issues_for_type)
        if swatch_html:
            html_parts.append(swatch_html)

    # Problem description
    if fix_hint:
        html_parts.append(
            f'<div style="font-size:0.8125rem;color:#374151;margin:10px 0;">'
            f'<b>Problem:</b> {escape(fix_hint)}</div>'
        )

    # WCAG standard reference link
    wcag_ref = info.get("wcag_ref", "") if info else ""
    if not wcag_ref:
        # Fall back to first issue's wcag_criterion
        for iss in issues_for_type:
            wcag_ref = iss.get("wcag_criterion", "")
            if wcag_ref:
                break
    if wcag_ref:
        wcag_name = info.get("wcag_name", "") if info else ""
        if not wcag_name:
            wcag_name = _NAMES.get(wcag_ref, "")
            level = _LEVEL.get(wcag_ref, "")
            if level:
                wcag_name = f"{wcag_name} (Level {level})" if wcag_name else f"Level {level}"
        wcag_url = _WCAG_URL.get(wcag_ref, "")
        label = f"WCAG {wcag_ref}: {wcag_name}" if wcag_name else f"WCAG {wcag_ref}"
        if wcag_url:
            html_parts.append(
                f'<div style="margin:4px 0 10px 0;">'
                f'<a href="{wcag_url}" target="_blank" rel="noopener" '
                f'style="color:#1d4ed8;text-decoration:underline;font-size:0.75rem;'
                f'display:inline-flex;align-items:center;min-height:44px;'
                f'min-width:44px;padding:4px 0;">'
                f'{escape(label)}</a></div>'
            )
        else:
            html_parts.append(
                f'<div style="font-size:0.75rem;color:#475569;margin:4px 0 10px 0;">'
                f'{escape(label)}</div>'
            )

    # Acrobat Pro steps
    if info and info.get("acrobat_steps"):
        steps_html = "\n".join(f"<li>{step}</li>" for step in info["acrobat_steps"])
        html_parts.append(
            '<div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;'
            'padding:14px;margin:12px 0;">'
            '<div style="font-weight:700;color:#0c4a6e;margin-bottom:8px;">'
            'Adobe Acrobat Pro Steps</div>'
            f'<div style="font-size:0.75rem;color:#374151;margin-bottom:6px;">'
            f'Tool: {escape(info.get("acrobat_tool", ""))}</div>'
            f'<ol style="margin:0;padding-left:22px;font-size:0.8125rem;color:#1e3a5f;'
            f'line-height:1.7;">{steps_html}</ol>'
            '</div>'
        )

    html_parts.append('</div>')  # close content div
    html_parts.append('</details>')

    return "\n".join(html_parts)


def build_remediation_guide_html(
    result: dict,
    pdf_path: str,
    structure: dict,
    verdict_config: dict = None,
) -> str:
    """Build the Remediation Guide tab HTML.

    Returns empty string if the document passes (no remediation needed).
    """
    from audit.standards import verdict_from_sr

    post_audit = result.get("post_audit", {})
    sr_data = result.get("sr_quality", {}) or {}

    # Check verdict
    hollow = sr_data.get("hollow_pages", [])
    vc = verdict_config or {}
    verdict = verdict_from_sr(post_audit, sr_data, verdict_config)

    # Gather issues needing remediation, plus informational (non-gating)
    # best-practice suggestions such as "no-h1". Informational issues never
    # affect the verdict, so a passing document with only advisories must
    # still surface them here rather than hitting the early return below.
    all_issues = post_audit.get("issues", [])
    needs_remediation = [
        i for i in all_issues
        if i.get("remediation_status") in ("needs_remediation", "informational")
    ]
    has_informational = any(
        i.get("remediation_status") == "informational" for i in all_issues
    )

    # Document-level verdict gates that are not per-page issues. Resolved up
    # front because they decide whether there is anything to show at all: an
    # advisory gate finding (reading order, per VERDICT_DEFAULTS
    # ``reading_order_advisory``) lives on a document the verdict passes, so
    # keying the early return on the verdict alone emptied the guide on
    # exactly the documents the demotion turns green.
    gate_reasons = _verdict_gate_reasons(sr_data, vc)

    if verdict == "pass" and not has_informational and not gate_reasons:
        return ""

    # Add SR-level issues from sr_data
    sr_issues = []
    if hollow:
        sr_issues.append({
            "type": "pdf-hollow-pages",
            "severity": "critical",
            "remediation_status": "needs_remediation",
            "location": {},
        })

    # Gate reasons go in before the action items below, not after. Both can
    # describe the same finding, but only the gate reason knows whether it
    # gates: the action item hardcodes needs_remediation/major. Whichever
    # lands first wins the dedup, so the authority has to be first or a
    # passing document shows its advisory with a failure badge.
    sr_issues.extend(gate_reasons)

    # Screen-reader concerns → specific, page-typed guide entries (each with its
    # own Acrobat remediation steps) instead of one vague "Fair Screen Reader
    # Quality" grade. Broadly-broken output keeps its dedicated poor headline.
    present_types = {i.get("type") for i in needs_remediation + sr_issues}
    if _sr_is_poor(sr_data):
        sr_issues.append({
            "type": "pdf-poor-screen-reader-quality",
            "severity": "critical",
            "remediation_status": "needs_remediation",
            "location": {},
        })
        present_types.add("pdf-poor-screen-reader-quality")
    # Map each action-item concern to the existing tip type that documents how
    # to fix it. reading-order / coverage may also come from _verdict_gate_reasons
    # below; present_types dedups so they appear once. hollow is added by the
    # aggregator itself.
    # "emr" is deliberately absent: the document-level element-match fallback has
    # no specific pages to act on, so it stays a banner note rather than a guide
    # section that would mislabel a passing-coverage doc as "Low Text Coverage".
    _item_guide_type = {
        "low_coverage": "pdf-low-text-coverage",
        "page_deficit": "pdf-low-text-coverage",
        "ocr": "pdf-poor-ocr-quality",
        "reading_order": "pdf-reading-order",
        "figures": "pdf-figures-missing-alt",
    }
    for _it in _sr_action_items(sr_data):
        _t = _item_guide_type.get(_it["key"])
        if _t and _t not in present_types:
            sr_issues.append({
                "type": _t,
                "severity": _it["severity"],
                "remediation_status": "needs_remediation",
                "location": {},
            })
            present_types.add(_t)


    # ── Pipeline warning synthetic issues ──
    pipeline_warnings = result.get("warnings", [])
    pipeline_issues: list[dict] = []

    # Document limits (images/tables/formulas exceeded processing limit)
    # Patterns are lowercase; matched case-insensitively.
    _limit_patterns = [
        "images were processed for alt text",
        "tables were processed for captions",
        "formulas were processed",
    ]
    if any(p in w.lower() for w in pipeline_warnings for p in _limit_patterns):
        pipeline_issues.append({
            "type": "pipeline-document-limits",
            "severity": "major",
            "remediation_status": "needs_remediation",
            "is_document_wide": True,
            "location": {},
        })

    # Output quality (WeasyPrint re-render)
    _quality_patterns = ["weasyprint re-render"]
    if any(p in w.lower() for w in pipeline_warnings for p in _quality_patterns):
        pipeline_issues.append({
            "type": "pipeline-output-quality",
            "severity": "major",
            "remediation_status": "needs_remediation",
            "is_document_wide": True,
            "location": {},
        })

    # Font issues (substitution fonts, failed ToUnicode CMaps)
    sqw = result.get("source_quality_warnings", {})
    if sqw.get("substitution_fonts_injected", 0) > 0 or sqw.get("tounicode_cmaps_failed", 0) > 0:
        pipeline_issues.append({
            "type": "pipeline-font-issues",
            "severity": "critical",
            "remediation_status": "needs_remediation",
            "is_document_wide": True,
            "location": {},
        })

    if not needs_remediation and not sr_issues and not pipeline_issues:
        return ""

    # Aggregate by type
    aggregated = _aggregate_issues_by_type(
        needs_remediation, sr_issues, hollow, sr_data,
        structure=structure,
    )

    # Add pipeline warning aggregations
    for pi in pipeline_issues:
        aggregated.append({
            "type": pi["type"],
            "severity": pi["severity"],
            "count": 1,
            "pages": [],
            "is_document_wide": True,
        })

    if not aggregated:
        return ""

    pages_info = structure.get("pages", {})

    # Count severities
    sev_counts: dict[str, int] = {}
    for agg in aggregated:
        s = agg.get("severity", "minor")
        sev_counts[s] = sev_counts.get(s, 0) + 1

    # Build severity pills
    sev_pills = []
    for sev_name, sev_label in [("critical", "Critical"), ("major", "Major"), ("minor", "Minor")]:
        c = sev_counts.get(sev_name, 0)
        if c > 0:
            style = _SEV_BADGE.get(sev_name, _SEV_DEFAULT)
            sev_pills.append(
                f'<span style="padding:3px 10px;border-radius:12px;font-size:0.75rem;'
                f'font-weight:600;{style}">{c} {sev_label}</span>'
            )

    # All affected pages
    all_pages = set()
    for agg in aggregated:
        all_pages.update(agg.get("pages", []))

    # Summary banner
    parts = [
        '<div style="font-family:\'Source Sans Pro\',\'Segoe UI\',system-ui,sans-serif;'
        'width:100%;padding:16px;box-sizing:border-box;">',

        # Header banner
        f'<div style="background:{BRAND_WARM_BG};border:2px solid {BRAND_ACCENT};'
        f'border-radius:10px;padding:20px;margin-bottom:20px;">',
        f'<h2 style="margin:0 0 8px 0;color:{BRAND_PRIMARY};font-size:1.25rem;">'
        f'Improvement Guide</h2>',
        f'<div style="color:#4b5563;font-size:0.875rem;margin-bottom:8px;">'
        f'{len(aggregated)} issue type{"s" if len(aggregated) != 1 else ""} found '
        f'across {len(all_pages)} page{"s" if len(all_pages) != 1 else ""}</div>',
        f'<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;">'
        + " ".join(sev_pills) + '</div>',
        '<div style="margin-top:12px;padding-top:10px;border-top:1px solid #e8d5b0;'
        'font-size:0.75rem;color:#44403c;line-height:1.5;">'
        '<b>Note:</b> This guide is generated by automated analysis and may not '
        'catch every issue or may occasionally flag items that are not true errors. '
        'Always verify the results and use your professional judgement when '
        'remediating your document.</div>',
        '</div>',
    ]

    # Per-issue-type sections
    for agg in aggregated:
        # Collect issues matching this aggregated type
        atype = agg["type"]
        info = _get_remediation_info(atype)
        matching_keys = set(info["keys"]) if info else {atype}

        issues_for_type = [
            i for i in needs_remediation
            if i.get("type") in matching_keys
        ]
        # Also include sr_issues and pipeline_issues if matching
        for si in sr_issues:
            if si.get("type") == atype:
                issues_for_type.append(si)
        for pi in pipeline_issues:
            if pi.get("type") == atype:
                issues_for_type.append(pi)

        parts.append(_build_remediation_section(
            agg, issues_for_type, pdf_path, structure, pages_info,
        ))

    parts.append('</div>')  # close outer wrapper
    return "\n".join(parts)
