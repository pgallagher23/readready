# SPDX-License-Identifier: AGPL-3.0-or-later
"""Post-tagging structure tree validator.

Inspects the tagged PDF's structure tree and content streams to detect:
  1. Blank structure elements (no /ActualText, no /Alt, no MCR, no children with text)
  2. Invisible-only MCR content (rendering mode 3 placeholders without /ActualText)
  3. Source-to-structure text coverage gaps
  4. Missing /ActualText on text-bearing leaf elements

Usage:
    from output_gen.struct_tree_validator import validate_tagged_pdf

    issues = validate_tagged_pdf(
        tagged_pdf_path="output.pdf",
        source_pdf_path="original.pdf",  # optional, for coverage check
    )
    for issue in issues:
        print(f"[{issue['severity']}] {issue['message']}")

Can also be run standalone:
    python -m output_gen.struct_tree_validator tagged.pdf [source.pdf]
"""

import logging
import re
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Match runs of letters/digits, optionally joined by a single internal
# apostrophe (so "children's" stays one token). Everything else — whitespace,
# pipes, slashes, dashes, parentheses, bullets — is a word boundary.
_WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)?", re.UNICODE)
_LINEBREAK_HYPHEN_RE = re.compile(r"(\w)-\s*\n\s*(\w)")


def coverage_tokens(text: str) -> set:
    """Tokenize text into a set of words for text-coverage comparison.

    This is the single source of truth shared by
    :func:`_check_text_coverage` and
    ``validation_gates.quick_screen_reader_check`` so the two coverage metrics
    cannot drift. Coverage compares the *visible* source text (extracted by
    PyMuPDF) against the *tagged* structure-tree text (``/ActualText``). Those
    two extractors tokenize layout differently: a course catalog row rendered
    as ``ENG 111|English`` comes out of PyMuPDF as the single token
    ``111|english`` but is tagged as separate cells ``111`` and ``english``.

    Splitting on every non-alphanumeric boundary (not just whitespace) makes
    both sides tokenize identically, so separator characters — pipes, slashes,
    dashes, parentheses — never manufacture phantom "missing" words. Genuinely
    dropped content still registers as missing because its letters are absent
    from the tagged side under any tokenization.

    Line-break hyphenation is healed first ("geogra-\\nphy" -> "geography")
    and smart apostrophes are normalized so possessives match.
    """
    if not text:
        return set()
    text = _LINEBREAK_HYPHEN_RE.sub(r"\1\2", text)
    text = text.lower().replace("‘", "'").replace("’", "'")
    return set(_WORD_RE.findall(text))


# Structure element tags that should carry readable text (directly or via children)
TEXT_TAGS = frozenset({
    "P", "Span", "H1", "H2", "H3", "H4", "H5", "H6",
    "LBody", "Lbl", "TD", "TH", "Link", "Caption", "TOCI",
})

# Container tags that are expected to have children, not own text
CONTAINER_TAGS = frozenset({
    "Document", "Part", "Art", "Sect", "Div", "BlockQuote",
    "TOC", "L", "LI", "Table", "TR", "THead", "TBody", "TFoot",
    "Form", "Figure",
})

# Pure grouping roles (ISO 32000-1 §14.8.4.2 subset): a tree whose SEs are
# ONLY these gives a screen reader nothing to land on.  Deliberately
# excludes BlockQuote/TOC/L/Table/Figure etc. — those carry content.
GROUPING_ROLES = frozenset({
    "Document", "Part", "Art", "Sect", "Div", "NonStruct", "Private",
})

# Tags whose /ActualText is a short label (AcroForm field caption, image alt
# text), not source body prose. Excluded when judging whether the structure
# tree's readable text is /ActualText-based or MCR-based.
LABEL_TAGS = frozenset({"Figure", "Form"})


def validate_tagged_pdf(
    tagged_pdf_path: str,
    source_pdf_path: Optional[str] = None,
    *,
    coverage_warn_pct: float = 95.0,
) -> List[Dict[str, Any]]:
    """Validate a tagged PDF's structure tree for accessibility completeness.

    Args:
        tagged_pdf_path: Path to the tagged PDF to validate.
        source_pdf_path: Optional path to the original source PDF,
            used for text coverage comparison.

    Returns:
        List of issue dicts, each with keys:
            ``severity``  – "error", "warning", or "info"
            ``check``     – short check identifier
            ``message``   – human-readable description
            ``element``   – dict with tag, xref, path (when applicable)
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("PyMuPDF not installed — structure tree validation skipped")
        return []

    if not Path(tagged_pdf_path).exists():
        return [_issue("error", "file-missing", f"Tagged PDF not found: {tagged_pdf_path}")]

    try:
        doc = fitz.open(tagged_pdf_path)
    except Exception as e:
        return [_issue("error", "open-failed", f"Cannot open tagged PDF: {e}")]

    issues: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Check 1: StructTreeRoot exists and is well-formed
    # ------------------------------------------------------------------
    cat_str = doc.xref_object(doc.pdf_catalog(), compressed=False)

    if "/MarkInfo" not in cat_str:
        issues.append(_issue("error", "no-markinfo", "/MarkInfo missing from catalog"))
    elif "true" not in cat_str.split("/MarkInfo")[1][:50].lower():
        issues.append(_issue("warning", "markinfo-not-marked",
                             "/MarkInfo present but /Marked is not true"))

    str_root_m = re.search(r"/StructTreeRoot\s+(\d+)\s+0\s+R", cat_str)
    if not str_root_m:
        issues.append(_issue("error", "no-struct-tree", "No /StructTreeRoot in catalog"))
        doc.close()
        return issues

    sr_str = doc.xref_object(int(str_root_m.group(1)), compressed=False)

    # Find Document element
    doc_xref = _find_document_element(doc, sr_str)
    if doc_xref is None:
        issues.append(_issue("error", "no-document-elem",
                             "Cannot locate Document structure element"))
        doc.close()
        return issues

    # ------------------------------------------------------------------
    # Check 2: Walk structure tree — find blank elements
    # ------------------------------------------------------------------
    all_elems: List[Dict[str, Any]] = []
    _walk_struct_tree(doc, doc_xref, all_elems, depth=0, path="")

    text_elems = [e for e in all_elems if e["tag"] in TEXT_TAGS]
    blank_leaves = [
        e for e in text_elems
        if not e["text"] and e["children"] == 0
        # Link elements with OBJR (annotation reference) are not truly blank —
        # screen readers access the link via the annotation's /Contents + /A action
        and not (e["tag"] == "Link" and e.get("has_objr"))
        # Elements with MCR (marked content reference) point to visible text
        # in the content stream — screen readers follow the MCR to read text
        and not e.get("has_mcr")
    ]
    blank_subtrees = []
    for elem in text_elems:
        if not elem["text"] and elem["children"] > 0:
            descendants = [
                d for d in all_elems
                if d["path"].startswith(elem["path"] + "/")
            ]
            if not any(d["text"] or d.get("has_mcr") for d in descendants):
                blank_subtrees.append(elem)

    for e in blank_leaves:
        issues.append(_issue(
            "error", "blank-leaf-element",
            f"<{e['tag']}> has no /ActualText, no /Alt, no MCR content, "
            f"and no children",
            element=e,
        ))

    for e in blank_subtrees:
        issues.append(_issue(
            "error", "blank-subtree",
            f"<{e['tag']}> and all its descendants have no readable text",
            element=e,
        ))

    # ------------------------------------------------------------------
    # Check 3: Bare container tree
    # A tree whose SEs are ONLY grouping roles (Document>Div etc.) reads
    # as untagged to AT even when MCIDs are present.  The tagger must
    # never emit this; the repair is the false-Figure split.
    # ------------------------------------------------------------------
    role_map = _parse_role_map(doc, sr_str)
    content_roles = [
        e for e in all_elems
        if _resolve_role(e["tag"], role_map) not in GROUPING_ROLES
    ]
    # Check for real MCIDs using _element_has_mcid() instead of the
    # over-reporting has_mcr field from _walk_struct_tree (which conflates
    # indirect struct-element refs with real marked content). See reviewer
    # finding 2026-08-07 fix round 1.
    # Use stashed _obj_str to avoid redundant xref_object fetch.
    any_mcr = any(
        _element_has_mcid(e.get("_obj_str", ""))
        for e in all_elems
    )
    if all_elems and any_mcr and not content_roles:
        issues.append(_issue(
            "error", "bare_container_tree",
            f"Structure tree has {len(all_elems)} element(s), all grouping "
            f"roles ({', '.join(sorted({e['tag'] for e in all_elems}))}) — "
            "no content-role SE; screen readers treat this as untagged",
        ))

    # ------------------------------------------------------------------
    # Check 4: Content stream MCR analysis
    # ------------------------------------------------------------------
    mcr_issues = _check_mcr_content(doc)
    issues.extend(mcr_issues)

    # ------------------------------------------------------------------
    # Check 5: Source text coverage (if source PDF provided)
    # ------------------------------------------------------------------
    if source_pdf_path and Path(source_pdf_path).exists():
        coverage_issues = _check_text_coverage(
            doc, source_pdf_path, all_elems,
            warn_threshold=coverage_warn_pct,
        )
        issues.extend(coverage_issues)

    # ------------------------------------------------------------------
    # Summary info
    # ------------------------------------------------------------------
    n_with_text = sum(1 for e in text_elems if e["text"])
    issues.append(_issue(
        "info", "summary",
        f"Structure tree: {len(all_elems)} total elements, "
        f"{len(text_elems)} text-bearing, {n_with_text} with text, "
        f"{len(blank_leaves)} blank leaves, {len(blank_subtrees)} blank subtrees",
    ))

    doc.close()
    return issues


# ======================================================================
# Internal helpers
# ======================================================================

def _issue(
    severity: str,
    check: str,
    message: str,
    element: Optional[Dict] = None,
) -> Dict[str, Any]:
    d: Dict[str, Any] = {"severity": severity, "check": check, "message": message}
    if element:
        d["element"] = {
            "tag": element.get("tag"),
            "xref": element.get("xref"),
            "path": element.get("path"),
        }
    return d


def _find_document_element(doc, sr_str: str) -> Optional[int]:
    """Find the Document structure element xref from StructTreeRoot."""
    # /K [ N 0 R ]
    k_arr = re.search(r"/K\s*\[\s*(\d+)\s+0\s+R", sr_str)
    if k_arr:
        return int(k_arr.group(1))
    # /K N 0 R
    k_single = re.search(r"/K\s+(\d+)\s+0\s+R", sr_str)
    if k_single:
        return int(k_single.group(1))
    return None


def _parse_role_map(doc, sr_str: str) -> Dict[str, str]:
    """Extract /RoleMap (inline dict or indirect ref) as {custom: standard}."""
    m = re.search(r"/RoleMap\s+(\d+)\s+0\s+R", sr_str)
    if m:
        try:
            rm_str = doc.xref_object(int(m.group(1)), compressed=False)
        except Exception:
            return {}
    else:
        m = re.search(r"/RoleMap\s*<<(.*?)>>", sr_str, re.DOTALL)
        if not m:
            return {}
        rm_str = m.group(1)
    return dict(re.findall(r"/(\w+)\s+/(\w+)", rm_str))


def _resolve_role(tag: str, role_map: Dict[str, str]) -> str:
    """Follow a RoleMap chain (bounded hops; cycle-safe)."""
    seen = set()
    while tag in role_map and tag not in seen and len(seen) < 8:
        seen.add(tag)
        tag = role_map[tag]
    return tag


def _element_has_mcid(obj_str: str) -> bool:
    """Detect whether a structure element object contains a real marked-content ID.

    Distinguishes between:
    - Real MCIDs: /MCID in explicit dict, or bare integer in /K
    - Struct-element children: indirect refs like "N 0 R"

    Over-reporting from _walk_struct_tree's has_mcr field (which conflates
    both) means we cannot use it; this helper answers precisely for one
    element. Used by bare_container_tree check to avoid false positives on
    grouping-only trees that have child struct elements but no marked content.

    Args:
        obj_str: xref_object output (uncompressed) for a structure element.

    Returns:
        True if element has /MCID dict or bare integer MCID in /K.
        False if /K contains only indirect struct-element refs or is absent.
    """
    # Check for explicit /MCID in a dict kid (e.g., /K [ << /MCID 5 >> ])
    if "/MCID" in obj_str:
        return True

    # Extract /K content
    k_arr = re.search(r"/K\s*\[\s*([^\]]*)\]", obj_str)
    if k_arr:
        # Array form: /K [ ... ]
        # Delete all "N 0 R" indirect references, then check if bare digits remain
        k_body = k_arr.group(1)
        # Remove indirect struct refs (N 0 R patterns)
        k_body_no_refs = re.sub(r"\d+\s+0\s+R", "", k_body)
        # If any digit remains, it's a bare MCID
        return bool(re.search(r"\d", k_body_no_refs))
    else:
        # Non-array form: /K N or /K N 0 R
        k_single = re.search(r"/K\s+(\d+)(?:\s+0\s+R)?", obj_str)
        if k_single:
            # Check if it's followed by "0 R" (indirect ref) or not
            # Re-match with the full /K pattern to see what follows
            full_match = re.search(r"/K\s+\d+(?:\s+0\s+R)?", obj_str)
            if full_match:
                # If the match includes "0 R", it's an indirect ref (False)
                # If not, it's a bare MCID (True)
                return "0 R" not in full_match.group(0)
        return False


def _get_actual_text(doc, xref: int) -> Optional[str]:
    """Extract /ActualText or /Alt from a structure element."""
    obj_str = doc.xref_object(xref, compressed=False)

    # Hex-encoded /ActualText (UTF-16BE with BOM)
    hex_m = re.search(r"/ActualText\s*<([0-9a-fA-F\s]+)>", obj_str, re.DOTALL)
    if hex_m:
        h = hex_m.group(1).replace("\n", "").replace(" ", "")
        try:
            bs = bytes.fromhex(h)
            if bs[:2] == b"\xfe\xff":
                return bs[2:].decode("utf-16-be", errors="replace")
            return bs.decode("utf-8", errors="replace")
        except ValueError:
            pass

    # Literal /ActualText
    lit_m = re.search(r"/ActualText\s*\(", obj_str)
    if lit_m:
        start = lit_m.end()
        depth = 1
        i = start
        while i < len(obj_str) and depth > 0:
            c = obj_str[i]
            if c == "(" and (i == 0 or obj_str[i - 1] != "\\"):
                depth += 1
            elif c == ")" and (i == 0 or obj_str[i - 1] != "\\"):
                depth -= 1
            i += 1
        text = obj_str[start : i - 1]
        if text:
            return text

    # /Alt (used for Figures)
    alt_m = re.search(r"/Alt\s*\(([^)]*)\)", obj_str)
    if alt_m and alt_m.group(1).strip():
        return alt_m.group(1)

    # Hex /Alt
    alt_hex = re.search(r"/Alt\s*<([0-9a-fA-F\s]+)>", obj_str, re.DOTALL)
    if alt_hex:
        h = alt_hex.group(1).replace("\n", "").replace(" ", "")
        try:
            bs = bytes.fromhex(h)
            if bs[:2] == b"\xfe\xff":
                return bs[2:].decode("utf-16-be", errors="replace")
        except ValueError:
            pass

    return None


def _walk_struct_tree(
    doc,
    xref: int,
    results: List[Dict],
    depth: int = 0,
    path: str = "",
) -> None:
    """Recursively walk the structure tree, collecting element metadata."""
    try:
        obj_str = doc.xref_object(xref, compressed=False)
    except Exception:
        return

    s_m = re.search(r"/S\s+/(\w+)", obj_str)
    tag = s_m.group(1) if s_m else "Unknown"

    text = _get_actual_text(doc, xref)
    # MCR can be expressed as /MCID in a dict, OR as bare integers in /K
    # e.g. /K [ 0 ] means MCID 0, /K [ << /Type /MCR /MCID 5 >> ] is explicit
    has_mcr = "/MCID" in obj_str
    if not has_mcr:
        # Check for bare integer MCID in /K (e.g. "/K [ 0 ]" or "/K 5")
        k_bare = re.search(r"/K\s*\[\s*(\d+)", obj_str)
        if not k_bare:
            k_bare = re.search(r"/K\s+(\d+)\s", obj_str)
        if k_bare:
            # Bare integer in /K = MCID reference (not an indirect object ref)
            # Indirect refs look like "N 0 R", bare MCIDs are just integers
            val = k_bare.group(0)
            if "0 R" not in val:
                has_mcr = True
    has_objr = "/Type /OBJR" in obj_str or "/OBJR" in obj_str
    # Also check children for OBJR (Link SEs contain OBJR as kid)
    if not has_objr and "/K" in obj_str:
        k_arr = re.search(r"/K\s*\[([^\]]+)\]", obj_str)
        if k_arr:
            for ref in re.findall(r"(\d+)\s+0\s+R", k_arr.group(1)):
                try:
                    co = doc.xref_object(int(ref), compressed=False)
                    if "/Type /OBJR" in co:
                        has_objr = True
                        break
                except Exception:
                    pass
    full_path = f"{path}/{tag}"

    # Find child structure elements
    child_xrefs = _get_child_struct_elems(doc, obj_str)

    results.append({
        "tag": tag,
        "text": text,
        "has_mcr": has_mcr,
        "has_objr": has_objr,
        "children": len(child_xrefs),
        "depth": depth,
        "path": full_path,
        "xref": xref,
        "_obj_str": obj_str,  # Stashed for bare_container_tree check to avoid refetch
    })

    for cx in child_xrefs:
        _walk_struct_tree(doc, cx, results, depth + 1, full_path)


def _get_child_struct_elems(doc, obj_str: str) -> List[int]:
    """Extract child structure element xrefs from /K."""
    child_xrefs = []

    k_arr = re.search(r"/K\s*\[([^\]]+)\]", obj_str)
    if k_arr:
        for r in re.findall(r"(\d+)\s+0\s+R", k_arr.group(1)):
            try:
                co = doc.xref_object(int(r), compressed=False)
                if "/Type /StructElem" in co:
                    child_xrefs.append(int(r))
            except Exception:
                pass
    else:
        k_s = re.search(r"/K\s+(\d+)\s+0\s+R", obj_str)
        if k_s:
            try:
                co = doc.xref_object(int(k_s.group(1)), compressed=False)
                if "/Type /StructElem" in co:
                    child_xrefs.append(int(k_s.group(1)))
            except Exception:
                pass

    return child_xrefs


def _check_mcr_content(doc) -> List[Dict[str, Any]]:
    """Check content streams for invisible-only MCR blocks.

    Acrobat Read Out Loud ignores rendering mode 3 (Tr=3) text
    entirely, so pages whose MCR blocks are all invisible will be
    reported as empty by screen readers.  This is an error, not a
    warning.
    """
    issues = []
    invisible_mcr_count = 0
    total_mcr_count = 0
    page_invisible: Dict[int, int] = {}
    page_total: Dict[int, int] = {}

    for page_num in range(len(doc)):
        page = doc[page_num]
        page_str = doc.xref_object(page.xref, compressed=False)
        page_inv = 0
        page_tot = 0

        # Find content stream references
        stream_xrefs = []
        arr_m = re.search(r"/Contents\s*\[([^\]]+)\]", page_str)
        if arr_m:
            stream_xrefs = [int(x) for x in re.findall(r"(\d+)\s+0\s+R", arr_m.group(1))]
        else:
            single_m = re.search(r"/Contents\s+(\d+)\s+0\s+R", page_str)
            if single_m:
                stream_xrefs = [int(single_m.group(1))]

        for sx in stream_xrefs:
            content = _get_stream_text(doc, sx)
            if not content:
                continue

            # Find MCR blocks
            bdc_pat = re.compile(
                r"/(\w+)\s*<<[^>]*/MCID\s+(\d+)[^>]*>>\s*BDC(.*?)EMC",
                re.DOTALL,
            )
            for tag, mcid, body in bdc_pat.findall(content):
                total_mcr_count += 1
                page_tot += 1
                body_s = body.strip()

                # Check if body uses invisible rendering (3 Tr) with minimal text
                is_invisible = "3 Tr" in body_s or (
                    re.search(r"\bTr\b", body_s) is None
                    and len(body_s) < 100
                    and re.search(r"<0003>|<00>|\( \)", body_s)
                )
                if is_invisible:
                    invisible_mcr_count += 1
                    page_inv += 1

        page_invisible[page_num] = page_inv
        page_total[page_num] = page_tot

    if total_mcr_count > 0 and invisible_mcr_count == total_mcr_count:
        issues.append(_issue(
            "error", "all-mcr-invisible",
            f"All {total_mcr_count} MCR blocks use invisible rendering "
            f"(Tr=3). Acrobat Read Out Loud will not vocalize this "
            f"content.",
        ))
    elif invisible_mcr_count > 0:
        issues.append(_issue(
            "warning", "mcr-invisible-count",
            f"{invisible_mcr_count}/{total_mcr_count} MCR blocks use "
            f"invisible placeholder text",
        ))

    # Per-page: flag pages where ALL MCRs are invisible
    for pg, tot in page_total.items():
        if tot > 0 and page_invisible.get(pg, 0) == tot:
            issues.append(_issue(
                "error", "page-all-mcr-invisible",
                f"Page {pg + 1}: all {tot} MCR blocks use invisible "
                f"rendering (Tr=3) — Acrobat Read Out Loud will skip "
                f"this page",
            ))

    return issues


def _get_stream_text(doc, xref: int) -> Optional[str]:
    """Decompress and decode a content stream."""
    try:
        raw = doc.xref_stream_raw(xref)
        if raw:
            try:
                return zlib.decompress(raw).decode("latin-1")
            except zlib.error:
                return raw.decode("latin-1", errors="replace")
        stream = doc.xref_stream(xref)
        if stream:
            return stream.decode("latin-1", errors="replace")
    except Exception:
        pass
    return None


def _check_text_coverage(
    doc,
    source_pdf_path: str,
    all_elems: List[Dict],
    *,
    warn_threshold: float = 95.0,
) -> List[Dict[str, Any]]:
    """Compare source PDF text with structure tree /ActualText coverage."""
    import fitz as fitz_mod

    issues = []
    try:
        src = fitz_mod.open(source_pdf_path)
    except Exception as e:
        issues.append(_issue("warning", "source-open-failed",
                             f"Cannot open source PDF for coverage check: {e}"))
        return issues

    # Collect all text from source
    src_text = ""
    for i in range(len(src)):
        src_text += src[i].get_text("text")
    src.close()

    # Collect all /ActualText from structure tree
    struct_text = " ".join(e["text"] for e in all_elems if e["text"])

    # If structure tree has little /ActualText but has MCR-linked content,
    # use the tagged PDF's own page text (MCR content is real visible text).
    # This handles PRESERVE mode PDFs where text lives in visible MCR blocks,
    # not in /ActualText metadata.
    #
    # For the sparse check, exclude Figure /Alt text — VLM-generated image
    # descriptions are not source document content and can inflate the word
    # count past the threshold on image-heavy PDFs (e.g. 8 Figures with
    # descriptive alt text = 100+ words, exceeding 15% of a short document).
    src_norm_quick = re.sub(r"\s+", " ", src_text).strip()
    src_word_count = len(src_norm_quick.split()) if src_norm_quick else 0
    # LABEL_TAGS /ActualText is a widget/image label (AcroForm field caption,
    # VLM-generated Figure alt), not source body prose — exclude it so a form
    # full of "Text field: X" captions can't mask genuinely sparse body
    # /ActualText and keep the get_text() rescue from firing.
    body_actualtext = " ".join(
        e["text"] for e in all_elems
        if e["text"] and e["tag"] not in LABEL_TAGS
    )
    body_word_count = len(re.sub(r"\s+", " ", body_actualtext).strip().split()) if body_actualtext.strip() else 0
    sparse_struct = (src_word_count > 0
                     and body_word_count < src_word_count * 0.15)
    # Content-preserving tagging carries body text in marked-content references
    # (MCR), not /ActualText, so the /ActualText concatenation systematically
    # under-counts. When MCR-only body leaves outnumber /ActualText body
    # leaves the tree is MCR-based; measure against the page's rendered text
    # (what the screen reader reads from the marked content) via the same
    # get_text() proxy used for the sparse case. Overlay-mode leaves carry
    # both MCR and /ActualText, so they keep using the /ActualText path.
    mcr_body_leaves = sum(
        1 for e in all_elems
        if e.get("has_mcr") and not e["text"] and e["tag"] in TEXT_TAGS
    )
    actualtext_body_leaves = sum(
        1 for e in all_elems
        if e["text"] and e["tag"] not in LABEL_TAGS
    )
    mcr_dominant = mcr_body_leaves > actualtext_body_leaves
    if not struct_text.strip() or sparse_struct or mcr_dominant:
        tagged_text = ""
        for i in range(len(doc)):
            tagged_text += doc[i].get_text("text")
        if tagged_text.strip():
            struct_text = tagged_text

    # Tokenize via the shared word splitter (see ``coverage_tokens``) so
    # separator-only differences -- pipes, dashes, parens, smart quotes,
    # line-break hyphenation -- never manufacture phantom coverage gaps, and so
    # this metric stays in lock-step with the screen-reader gate's metric.
    src_words = coverage_tokens(src_text)
    struct_words = coverage_tokens(struct_text)
    if not src_words:
        return issues

    missing = src_words - struct_words
    coverage = len(src_words - missing) / len(src_words) * 100

    # Error threshold is always half of warn_threshold (floor 50%)
    error_threshold = max(50.0, warn_threshold / 2)

    if coverage < error_threshold:
        issues.append(_issue(
            "error", "low-text-coverage",
            f"Only {coverage:.1f}% of source words found in structure tree "
            f"({len(missing)} words missing out of {len(src_words)})",
        ))
    elif coverage < warn_threshold:
        issues.append(_issue(
            "warning", "moderate-text-coverage",
            f"{coverage:.1f}% source word coverage in structure tree "
            f"({len(missing)} words not found)",
        ))
    else:
        issues.append(_issue(
            "info", "text-coverage",
            f"{coverage:.1f}% source word coverage in structure tree",
        ))

    # Per-page check: source had text but tagged doesn't
    tagged_doc = doc
    src2 = fitz_mod.open(source_pdf_path)
    blank_pages = []
    for i in range(min(len(src2), len(tagged_doc))):
        src_page_text = src2[i].get_text("text").strip()
        tagged_page_text = tagged_doc[i].get_text("text").strip()
        if src_page_text and not tagged_page_text:
            blank_pages.append(i + 1)
    src2.close()

    if blank_pages:
        issues.append(_issue(
            "error", "blank-pages",
            f"Pages with source text but no tagged text: {blank_pages}",
        ))

    return issues


# ======================================================================
# CLI entry point
# ======================================================================

def main():
    import sys

    if len(sys.argv) < 2:
        print(f"Usage: python -m output_gen.struct_tree_validator <tagged.pdf> [source.pdf]")
        sys.exit(1)

    tagged_path = sys.argv[1]
    source_path = sys.argv[2] if len(sys.argv) > 2 else None

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    issues = validate_tagged_pdf(tagged_path, source_path)

    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    infos = [i for i in issues if i["severity"] == "info"]

    for issue in errors:
        elem = issue.get("element", {})
        loc = f" ({elem.get('path', '')} xref={elem.get('xref', '')})" if elem else ""
        print(f"  ERROR: {issue['message']}{loc}")

    for issue in warnings:
        print(f"  WARNING: {issue['message']}")

    for issue in infos:
        print(f"  INFO: {issue['message']}")

    print()
    if errors:
        print(f"FAIL: {len(errors)} error(s), {len(warnings)} warning(s)")
        sys.exit(1)
    elif warnings:
        print(f"PASS with warnings: {len(warnings)} warning(s)")
        sys.exit(0)
    else:
        print("PASS: no issues found")
        sys.exit(0)


if __name__ == "__main__":
    main()
