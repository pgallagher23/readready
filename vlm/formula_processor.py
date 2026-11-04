# SPDX-License-Identifier: AGPL-3.0-or-later
"""Formula captioning using a Vision Language Model.

Renders formula regions from the original PDF as images (via PyMuPDF),
sends them to a VLM along with surrounding text context, and writes
alt text + LaTeX back into the structure JSON.  Runs as Phase 3c of the
pipeline, between table caption generation and heuristic remediation.
"""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

from vlm.client_factory import create_vlm_client

logger = logging.getLogger(__name__)

FORMULA_SYSTEM_PROMPT = """You are an accessibility specialist describing mathematical formulas for visually impaired readers. Your descriptions must comply with WCAG 2.1 AA standards.

Rules:
1. Provide TWO parts in your response, clearly separated.
2. DESCRIPTION: A natural language description of the formula that a screen reader can speak aloud. Describe what the formula represents and its components. Include context if provided. Keep under 300 characters.
3. LATEX: The LaTeX representation of the formula.
4. Start the description directly with the formula's content. Do NOT begin with any meta-preamble such as "This formula", "This image shows", "This is a mathematical formula", "The image displays...", "The formula represents...", "This equation", or any variant referencing the image/formula/reader. Jump straight into the actual mathematical content (e.g. start with "The sum of...", "A matrix where...", "x equals...").
5. Format your response exactly as:
DESCRIPTION: <your description>
LATEX: <your latex>
6. The LATEX line must contain ONLY the LaTeX code itself — no prose, no explanation. If you cannot determine the LaTeX, leave it completely empty (write "LATEX:" with nothing after it). Do NOT write sentences like "The LaTeX representation is unknown due to..." — either give the LaTeX or write nothing.
7. Do NOT start the description with "Formula:" or "Equation:" — just describe it directly.
8. For simple expressions (e.g. x = 5), keep the description brief."""

FORMULA_PROMPT = """Describe this mathematical formula for a visually impaired reader.

{context}

Provide a natural language description and the LaTeX representation."""


def process_formulas(
    html_path: str,
    structure_json_path: str,
    pdf_path: str,
    llm_config: Dict[str, Any],
    formula_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Generate descriptions for formulas using VLM.

    Args:
        html_path: Path to the HTML file.
        structure_json_path: Path to docling_structure.json.
        pdf_path: Path to the original PDF file.
        llm_config: LLM configuration from config.yaml.
        formula_config: Formula-specific configuration from config.yaml.

    Returns:
        Dict with updated_count, skipped_count, stats, descriptions, error.
    """
    start_time = time.time()
    formula_config = formula_config or {}

    if not formula_config.get("vlm_captioning", True):
        return {
            "descriptions": {},
            "updated_count": 0,
            "skipped_count": 0,
            "stats": {"time_seconds": 0},
        }

    client = create_vlm_client(llm_config)
    if not client.check_connection():
        logger.warning(
            "VLM unavailable at %s — skipping formula captioning",
            client.base_url,
        )
        return {
            "descriptions": {},
            "updated_count": 0,
            "skipped_count": 0,
            "error": f"Cannot connect to VLM at {client.base_url}",
            "stats": {"time_seconds": 0},
        }

    # Load structure JSON
    try:
        with open(structure_json_path, "r", encoding="utf-8") as f:
            structure = json.load(f)
    except Exception as e:
        logger.error("Failed to load structure JSON: %s", e)
        return {
            "descriptions": {},
            "updated_count": 0,
            "skipped_count": 0,
            "error": str(e),
            "stats": {"time_seconds": 0},
        }

    # Find formula elements with rendered images
    formula_elements = _find_formula_elements(structure)
    if not formula_elements:
        logger.info("No formula elements found, skipping formula captioning")
        return {
            "descriptions": {},
            "updated_count": 0,
            "skipped_count": 0,
            "stats": {"time_seconds": 0},
        }

    # Enforce per-document limit
    max_formulas = formula_config.get("max_per_document", 200)
    capped_warning = None
    if len(formula_elements) > max_formulas:
        capped_warning = (
            f"Only {max_formulas} of {len(formula_elements)} formulas were processed. "
            f"Remaining formulas will use fallback alt text."
        )
        logger.warning(
            "Document has %d formulas — capping at %d",
            len(formula_elements), max_formulas,
        )
        formula_elements = formula_elements[:max_formulas]

    logger.info("Generating descriptions for %d formulas via VLM", len(formula_elements))

    # Gather surrounding context for each formula
    all_elements = structure.get("elements", [])
    include_context = formula_config.get("include_context", True)

    # Build work items
    work_items = []
    skipped_count = 0

    for fe in formula_elements:
        elem = fe["element"]
        elem_idx = fe["index"]
        image_ref = elem.get("image_ref")

        # Author-written alt recovered from the source PDF always wins over a
        # generated description.  Unlike the image path, this loop would
        # otherwise overwrite alt_text unconditionally further down.
        if (elem.get("alt_source") == "source_pdf"
                and (elem.get("alt_text") or "").strip()):
            logger.debug(
                "Formula %s: keeping author-written alt text from the source PDF",
                elem.get("id", elem_idx),
            )
            skipped_count += 1
            continue

        if not image_ref or not Path(image_ref).exists():
            logger.warning(
                "Formula %s: no rendered image available, skipping",
                elem.get("id", elem_idx),
            )
            skipped_count += 1
            continue

        context_parts = []
        if include_context:
            context_text = _gather_context(all_elements, elem_idx)
            if context_text:
                context_parts.append(f"Surrounding context:\n{context_text}")

        # Include any text Docling extracted (may be garbled but still useful)
        formula_text = elem.get("text", "").strip()
        if formula_text:
            context_parts.append(f"Extracted text (may be garbled): {formula_text[:200]}")

        prompt = FORMULA_PROMPT.format(
            context="\n\n".join(context_parts) if context_parts else "No surrounding context available."
        )

        work_items.append({
            "elem_id": elem.get("id", f"formula_{elem_idx}"),
            "elem_idx": elem_idx,
            "image_ref": image_ref,
            "prompt": prompt,
        })

    # Run VLM calls in parallel
    max_workers = min(
        llm_config.get("max_parallel", 4),
        len(work_items),
    ) if work_items else 1
    vlm_results: Dict[str, Dict] = {}

    if work_items:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_item = {
                pool.submit(
                    _generate_single_description, client, item["image_ref"], item["prompt"],
                ): item
                for item in work_items
            }
            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    vlm_results[item["elem_id"]] = future.result()
                except Exception as e:
                    logger.error(
                        "Formula %s: VLM generation failed — %s",
                        item["elem_id"], e,
                    )
                    vlm_results[item["elem_id"]] = {"error": str(e)}

    # Apply results back to structure JSON
    descriptions = {}
    updated_count = 0

    for item in work_items:
        elem_id = item["elem_id"]
        elem_idx = item["elem_idx"]
        result = vlm_results.get(elem_id, {})

        if result.get("error"):
            skipped_count += 1
            continue

        description = result.get("description", "")
        latex = result.get("latex", "")

        if not description or len(description) < 3:
            logger.warning("Formula %s: VLM returned empty description, skipping", elem_id)
            skipped_count += 1
            continue

        # Update structure JSON element
        all_elements[elem_idx]["alt_text"] = description
        if latex and latex.lower() != "unknown":
            all_elements[elem_idx]["latex"] = latex
            # Optionally embed MathML in the tagged PDF as an Associated File.
            # The /Alt caption remains the universal fallback; this is additive.
            if formula_config.get("mathml_pdf_embed", False):
                mathml = latex_to_mathml(latex, description)
                if mathml:
                    all_elements[elem_idx]["mathml"] = mathml

        descriptions[elem_id] = {
            "description": description,
            "latex": latex if latex and latex.lower() != "unknown" else None,
        }
        updated_count += 1
        logger.info("Formula %s: description = \"%s\"", elem_id, description[:80],
                    extra={"contains_content": True, "content_ref": f"formula {elem_id}"})

    # Write updated structure JSON
    if updated_count > 0:
        try:
            with open(structure_json_path, "w", encoding="utf-8") as f:
                json.dump(structure, f, indent=2, ensure_ascii=False)
            logger.info("Updated structure JSON with %d formula descriptions", updated_count)
        except Exception as e:
            logger.error("Failed to write updated structure JSON: %s", e)

    elapsed = time.time() - start_time
    logger.info(
        "Formula captioning complete: %d generated, %d skipped in %.1fs",
        updated_count, skipped_count, elapsed,
    )

    result = {
        "descriptions": descriptions,
        "updated_count": updated_count,
        "skipped_count": skipped_count,
        "stats": {"time_seconds": round(elapsed, 2)},
    }
    if capped_warning:
        result["warning"] = capped_warning
    return result


def _generate_single_description(
    client, image_path: str, prompt: str,
) -> Dict[str, Any]:
    """Call VLM for a single formula image. Thread-safe.

    Returns:
        {"description": str, "latex": str} on success,
        {"error": str} on failure.
    """
    try:
        response = client.generate(
            prompt=prompt,
            images=[image_path],
            system_prompt=FORMULA_SYSTEM_PROMPT,
        )
        return _parse_vlm_response(response)
    except Exception as e:
        return {"error": str(e)}


def _parse_vlm_response(response: str) -> Dict[str, Any]:
    """Parse the structured VLM response into description and LaTeX.

    Expected format:
        DESCRIPTION: <text>
        LATEX: <text>

    Falls back to treating the entire response as a description if
    the format is not followed.
    """
    response = response.strip()
    description = ""
    latex = ""

    lines = response.split("\n")
    desc_lines = []
    latex_lines = []
    current_section = None

    for line in lines:
        line_stripped = line.strip()
        upper = line_stripped.upper()

        if upper.startswith("DESCRIPTION:"):
            current_section = "desc"
            desc_lines.append(line_stripped[len("DESCRIPTION:"):].strip())
        elif upper.startswith("LATEX:"):
            current_section = "latex"
            latex_lines.append(line_stripped[len("LATEX:"):].strip())
        elif current_section == "desc":
            desc_lines.append(line_stripped)
        elif current_section == "latex":
            latex_lines.append(line_stripped)

    if desc_lines:
        description = " ".join(desc_lines).strip()
    if latex_lines:
        latex = " ".join(latex_lines).strip()

    # Fallback: if no structured response, use entire response as description
    if not description:
        description = response[:500]

    # Clean up LaTeX delimiters
    if latex:
        for delim in ("$$", "$", "\\[", "\\]", "\\(", "\\)"):
            latex = latex.replace(delim, "")
        latex = latex.strip()

    # Cut off prompt-instruction echoes and natural-language explanations the
    # VLM sometimes appends after otherwise-valid LaTeX, e.g.
    # "...\mathbf{u}   CRITICAL: ..." or "...+ V + U   Where: ...".
    if latex:
        latex = _trim_latex_prose(latex)

    # Discard prose non-answers like "The LaTeX representation is unknown due
    # to the resolution and angle of the image" — treat as no LaTeX.
    if latex and _is_latex_failure(latex):
        latex = ""

    description = _strip_boilerplate_prefix(description)

    return {"description": description, "latex": latex}


# Phrases the VLM emits when it gives up on the LaTeX instead of leaving the
# line empty. If the LATEX value contains any of these (case-insensitively),
# it is prose, not LaTeX, and is discarded.
_LATEX_FAILURE_PHRASES = (
    "unknown", "cannot", "can't", "can not", "unable", "not able",
    "not possible", "not clear", "not legible", "illegible", "unclear",
    "no latex", "not available", "not determine", "could not",
    "couldn't", "sorry", "due to the", "representation is", "i'm not",
    "not enough", "too blurry", "too low", "resolution", "not provided",
)

# Prompt-instruction labels the VLM sometimes echoes back, appended after real
# LaTeX. Truncate the LaTeX at the first such label.
_LATEX_ECHO_CUT_RE = re.compile(
    r"\b(?:CRITICAL|DESCRIPTION|IMPORTANT|NOTE|NOTES|RULES?|FORMAT|LATEX)\b",
    re.IGNORECASE,
)

# Natural-language explanation the VLM sometimes appends after the LaTeX, e.g.
# "...+ V + U   Where: - \bar{I} is the relative risk". Two variants:
#   - a colon-terminated marker ("where:", "note:") — unambiguous prose, matched
#     anywhere (multi-line LaTeX is joined with single spaces, so a leading-space
#     guard would miss it);
#   - a bare marker guarded by 2+ spaces so it cannot hit an inline
#     "\text{where ...}" inside real LaTeX.
_LATEX_TRAILER_CUT_RE = re.compile(
    r"\b(?:where|note|nb|here|given|defined\s+as)\s*:"
    r"|\s{2,}(?:where|here|note that|in which|such that)\b",
    re.IGNORECASE,
)


def _trim_latex_prose(latex: str) -> str:
    """Cut the LaTeX at the earliest prompt-echo or explanation trailer."""
    cut = len(latex)
    for rx in (_LATEX_ECHO_CUT_RE, _LATEX_TRAILER_CUT_RE):
        m = rx.search(latex)
        if m:
            cut = min(cut, m.start())
    return latex[:cut].strip()


def _is_latex_failure(latex: str) -> bool:
    """Return True if `latex` is an apology / "can't tell" message rather than
    actual LaTeX code, so it can be dropped."""
    lowered = latex.lower()

    if lowered in ("unknown", "none", "n/a", "na", "null", "-"):
        return True

    # A bare English stop-word left over after trimming a prose trailer (e.g.
    # the model wrote prose, "where:..." was cut, leaving just "The") is junk.
    if lowered in ("the", "a", "an", "and", "is", "this", "that", "for",
                   "where", "here", "it", "of", "in"):
        return True

    if any(phrase in lowered for phrase in _LATEX_FAILURE_PHRASES):
        return True

    # Valid LaTeX never opens with a bare prose marker word — if it does, the
    # whole value is an explanation, not code (e.g. "where Hdot = EHdot...").
    if re.match(r"(?:where|note|suppose|here|given|assuming|thus|hence|"
                r"therefore|recall|let\b)\b", lowered):
        return True

    # A run of plain words with no LaTeX-ish characters is prose, not code.
    # Real LaTeX of this length always carries an operator or command.
    has_latex_chars = any(c in latex for c in "\\{}^_=+/<>|&")
    if not has_latex_chars and len(latex.split()) >= 4:
        return True

    return False


# Boilerplate openings the VLM sometimes emits despite prompt instructions,
# e.g. "This formula shows ...", "This image shows a mathematical formula for
# a visually impaired reader: ...", "The equation represents ...". Matched
# case-insensitively at the very start of the description and stripped so the
# alt text begins with the actual content. The preamble must contain a meta
# verb (shows/represents/is/...), a "for a ... reader" clause, or end in a
# colon — otherwise nothing is stripped, to avoid mangling real content like
# "The equation of motion ...".
_BOILERPLATE_PREFIX_RE = re.compile(
    r"""
    ^\s*
    (?:                                       # ---- lead-in ----
        (?:this|the|it)\s+(?:is|was)\s+       # "This is a mathematical formula"
            (?:(?:a|an|the)\s+)?
            (?:(?:mathematical|simple|complex|following|given)\s+)*
            (?:formula|equation|expression|image|figure|diagram)
      |
        (?:this|the|here\s+is|here['’]s)\s+   # "This formula" / "The equation"
            (?:image|formula|equation|expression|figure|diagram)
      |
        (?:that|which)                        # "That represents ..." / "Which shows ..."
    )
    (?:                                       # ---- verb / reader-clause / colon ----
        \s+(?:that\s+)?
        (?:shows?|displays?|depicts?|illustrates?|presents?|represents?
           |contains?|describes?|defines?|expresses?|gives?|provides?
           |showing|displaying|depicting|representing|describing|illustrating
           |is|are|was|were)\b
        (?:\s+
            (?:(?:a|an|the)\s+)?
            (?:(?:mathematical|simple|complex|following|given)\s+)*
            (?:formula|equation|expression|image|figure|diagram)
        )?
        (?:\s+for\s+(?:a\s+)?(?:visually[\s-]impaired|blind|sighted)[^.:,;]*?reader[s]?)?
        \s*[:.,;—-]*
      |
        \s+for\s+(?:a\s+)?(?:visually[\s-]impaired|blind|sighted)[^.:,;]*?reader[s]?
        \s*[:.,;—-]*
      |
        \s*:
    )
    \s*
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _strip_boilerplate_prefix(description: str) -> str:
    """Remove a leading meta-preamble such as "This formula shows ..." or
    "This image shows a mathematical formula for a visually impaired reader:".

    Strips at most one such prefix. Only re-capitalises and returns the
    trimmed text when a prefix was actually removed; otherwise returns the
    description unchanged. Also leaves it unchanged if removal would empty it.
    """
    if not description:
        return description

    # Peel up to a few stacked preambles, e.g. "This image shows a formula.
    # The formula is related to ..." Each pass removes at most one prefix.
    stripped = description
    for _ in range(3):
        candidate = _BOILERPLATE_PREFIX_RE.sub("", stripped, count=1).lstrip()
        if candidate == stripped or len(candidate) < 3:
            break
        stripped = candidate

    # Nothing matched, or removal would leave essentially nothing.
    if stripped == description or len(stripped) < 3:
        return description

    if stripped[0].islower():
        stripped = stripped[0].upper() + stripped[1:]

    return stripped


def _find_formula_elements(structure: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Find FormulaItem elements in structure JSON.

    Returns list of dicts with 'element' and 'index' keys.
    """
    results = []
    for i, elem in enumerate(structure.get("elements", [])):
        if elem.get("type") == "FormulaItem" and elem.get("tag") == "Formula":
            results.append({"element": elem, "index": i})
    return results


def _gather_context(
    all_elements: List[Dict[str, Any]],
    formula_idx: int,
    window: int = 3,
) -> str:
    """Gather surrounding text context for a formula element.

    Looks at up to `window` elements before and after the formula
    to find section headings and nearby paragraph text.
    """
    parts = []

    # Look backward for context (headings and paragraphs)
    start = max(0, formula_idx - window)
    for i in range(start, formula_idx):
        elem = all_elements[i]
        text = elem.get("text", "").strip()
        tag = elem.get("tag", "")
        if not text:
            continue
        if tag and tag.startswith("H"):
            parts.append(f"Section: {text[:100]}")
        elif tag == "P":
            parts.append(f"Preceding text: {text[:150]}")

    # Look forward for context
    end = min(len(all_elements), formula_idx + window + 1)
    for i in range(formula_idx + 1, end):
        elem = all_elements[i]
        text = elem.get("text", "").strip()
        tag = elem.get("tag", "")
        if not text:
            continue
        if tag == "P":
            parts.append(f"Following text: {text[:150]}")
            break  # One following paragraph is enough

    return "\n".join(parts)


def enrich_html_with_formulas(
    html_path: str,
    structure_json_path: str,
    formula_config: Optional[Dict[str, Any]] = None,
) -> int:
    """Enrich HTML formula elements with MathML or aria-label.

    After VLM processing has populated alt_text and latex fields in
    the structure JSON, this function finds corresponding elements in
    the HTML and adds semantic markup.

    Args:
        html_path: Path to the HTML file (modified in-place).
        structure_json_path: Path to docling_structure.json.
        formula_config: Formula-specific configuration.

    Returns:
        Number of formulas enriched in the HTML.
    """
    formula_config = formula_config or {}
    if not formula_config.get("mathml_conversion", True):
        return 0

    try:
        with open(structure_json_path, "r", encoding="utf-8") as f:
            structure = json.load(f)
    except Exception as e:
        logger.error("Failed to load structure JSON for formula enrichment: %s", e)
        return 0

    formula_elements = _find_formula_elements(structure)
    formulas_with_data = [
        fe for fe in formula_elements
        if fe["element"].get("alt_text") or fe["element"].get("latex")
    ]
    if not formulas_with_data:
        return 0

    html_path = Path(html_path)
    with open(html_path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    enriched = 0
    for formula_seq_index, fe in enumerate(formula_elements):
        elem = fe["element"]
        if not (elem.get("alt_text") or elem.get("latex")):
            continue  # skip formulas without VLM data, but count the index
        alt_text = elem.get("alt_text", "")
        latex = elem.get("latex", "")
        elem_text = elem.get("text", "").strip()

        html_elem = _find_html_formula(
            soup, elem_text, formula_seq_index=formula_seq_index,
        )
        if not html_elem:
            continue

        mathml_inserted = False
        if latex:
            # Guard per-formula: a single formula's MathML failure must not
            # abort the whole phase and lose alt text for every other formula.
            # On failure we fall through to the aria-label path below.
            try:
                mathml_inserted = _try_insert_mathml(
                    soup, html_elem, latex, alt_text
                )
            except Exception as e:
                logger.warning(
                    "MathML insertion failed for formula %d (%s); "
                    "falling back to aria-label",
                    formula_seq_index, e,
                )

        if not mathml_inserted and alt_text:
            # Fallback: wrap with role="math" and aria-label
            html_elem["role"] = "math"
            html_elem["aria-label"] = alt_text
            enriched += 1
        elif mathml_inserted:
            enriched += 1

    if enriched > 0:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(str(soup))
        logger.info("Enriched %d formulas in HTML", enriched)

    return enriched


def latex_to_mathml(latex: Optional[str], alt_text: str = "") -> Optional[str]:
    """Convert a LaTeX string to a standalone MathML document.

    Returns a namespaced ``<math>`` document prefixed with an XML declaration,
    with the natural-language caption carried as the ``alttext`` attribute for
    MathML-aware assistive tech.  Returns ``None`` when the input is empty or
    the conversion fails / produces no ``<math>`` root.

    Shared by the HTML enrichment path (:func:`_try_insert_mathml`) and the PDF
    Associated File embedding path (:func:`process_formulas`) so both carry
    identical MathML.
    """
    if not latex or not latex.strip():
        return None
    try:
        import latex2mathml.converter
        mathml_str = latex2mathml.converter.convert(latex)
    except ImportError:
        logger.debug("latex2mathml not installed, skipping MathML conversion")
        return None
    except Exception as e:
        logger.debug("MathML conversion failed for LaTeX '%s': %s", latex[:50], e)
        return None

    if not mathml_str or "<math" not in mathml_str:
        return None

    # latex2mathml already emits the xmlns namespace on the root element.
    if alt_text:
        from xml.sax.saxutils import quoteattr
        # Use a function replacement: the caption is arbitrary text and often
        # contains backslash sequences (e.g. ``\mathbf``, ``\mu``). A string
        # replacement would interpret those as regex escapes and raise
        # ``re.error: bad escape``; a callable returns the text literally.
        replacement = "<math alttext=" + quoteattr(alt_text)
        mathml_str = re.sub(
            r"<math\b",
            lambda _m: replacement,
            mathml_str,
            count=1,
        )

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + mathml_str


def _try_insert_mathml(
    soup: BeautifulSoup,
    html_elem,
    latex: str,
    alt_text: str,
) -> bool:
    """Attempt to convert LaTeX to MathML and insert into HTML element.

    Returns True if MathML was successfully inserted.
    """
    mathml_doc = latex_to_mathml(latex, alt_text)
    if not mathml_doc:
        return False

    # Extract just the <math> subtree (drop the XML declaration) for HTML.
    from bs4 import BeautifulSoup as BS
    math_tag = BS(mathml_doc, "html.parser").find("math")
    if not math_tag:
        return False

    # Replace element content with MathML
    html_elem.clear()
    html_elem.append(math_tag)
    html_elem["role"] = "math"
    if alt_text:
        html_elem["aria-label"] = alt_text
    return True


def _find_html_formula(soup, formula_text: str,
                       formula_seq_index: int = None):
    """Find a formula element in HTML by index or text.

    Matching priority:
      1. data-formula-index attribute (set by _postprocess_html)
      2. Text content fallback (for non-Docling HTML)
    """
    # Match by data-formula-index attribute
    if formula_seq_index is not None:
        elem = soup.find(attrs={"data-formula-index": str(formula_seq_index)})
        if elem:
            return elem

    # Text content fallback
    if not formula_text:
        return None
    candidates = soup.find_all(
        lambda tag: (
            tag.name in ("p", "span", "div", "pre")
            and tag.string
            and formula_text[:50] in tag.string
        )
    )
    if candidates:
        return candidates[0]
    for tag in soup.find_all(["p", "span", "div", "pre"]):
        text = tag.get_text(strip=True)
        if text and formula_text[:50] in text and len(text) < len(formula_text) * 3:
            return tag

    return None
