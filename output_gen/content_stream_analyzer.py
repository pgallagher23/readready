#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Python wrapper for the Java ContentStreamAnalyzer.

Invokes the iText-based content stream analyzer and returns parsed JSON.
This is the Python entry point for Phase 1 of the content-preserving tagger.

Usage:
    from output_gen.content_stream_analyzer import analyze_content_streams
    analysis = analyze_content_streams("input.pdf")
    # analysis is a dict with page-by-page text runs, images, fonts
"""

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from utils.font_dir import jvm_font_flag

logger = logging.getLogger(__name__)

# Location of the iText tagger JAR
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_JAR_PATH = Path(__file__).resolve().parent / "itext_tagger.jar"
_CDS_ARCHIVE = Path(__file__).resolve().parent / "itext_tagger.jsa"


def analyze_content_streams(
    pdf_path: str,
    jar_path: Optional[str] = None,
    timeout: int = 300,
    java_heap: str = "1g",
) -> Dict[str, Any]:
    """Analyze content streams of a PDF using iText's PdfCanvasProcessor.

    Args:
        pdf_path: Path to the PDF to analyze.
        jar_path: Path to itext_tagger.jar (auto-detected if None).
        timeout: Maximum seconds to wait for analysis.

    Returns:
        Dict with structure:
        {
            "file": str,
            "pages": int,
            "page_analyses": [
                {
                    "page": int,
                    "width": float,
                    "height": float,
                    "text_run_count": int,
                    "text_runs": [
                        {
                            "index": int,
                            "text": str,
                            "x": float, "y": float,
                            "end_x": float, "width": float,
                            "font": str, "font_type": str,
                            "font_size": float,
                            "render_mode": int,
                            "has_unicode": bool,
                            "char_count": int,
                        }
                    ],
                    "image_count": int,
                    "images": [...],
                }
            ],
            "summary": {
                "total_text_runs": int,
                "total_images": int,
                "total_chars": int,
                "total_fonts": int,
                "all_fonts": [str],
                "pages_with_existing_mcids": int,
            }
        }

    Raises:
        FileNotFoundError: If PDF or JAR not found.
        RuntimeError: If Java analysis fails.
    """
    if jar_path is None:
        jar_path = str(_JAR_PATH)

    if not os.path.exists(jar_path):
        raise FileNotFoundError(f"iText JAR not found: {jar_path}")
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    # Create temp file for output
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        output_path = tmp.name

    try:
        jvm_flags = [
            f"-Xmx{java_heap}",
            "-XX:TieredStopAtLevel=1",
            "-XX:+UseSerialGC",
            jvm_font_flag(),
        ]
        if _CDS_ARCHIVE.exists():
            jvm_flags.append(f"-XX:SharedArchiveFile={_CDS_ARCHIVE}")
        cmd = [
            "java", *jvm_flags, "-jar", jar_path,
            "--analyze",
            "--input", pdf_path,
            "--output", output_path,
        ]
        logger.info(
            "Running content stream analyzer (-Xmx%s, timeout=%ds): %s",
            java_heap, timeout, Path(pdf_path).name,
        )

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(_PROJECT_ROOT),
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"Content stream analysis failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )

        with open(output_path, "r", encoding="utf-8") as f:
            analysis = json.load(f)

        return analysis

    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"Content stream analysis timed out after {timeout}s"
        )
    finally:
        # Clean up temp file
        try:
            os.unlink(output_path)
        except OSError:
            pass


def get_text_runs_for_page(
    analysis: Dict[str, Any],
    page_num: int,
) -> list:
    """Get text runs for a specific page (1-indexed).

    Args:
        analysis: Output from analyze_content_streams().
        page_num: Page number (1-indexed).

    Returns:
        List of text run dicts for that page.
    """
    if page_num < 1 or page_num > analysis["pages"]:
        return []
    return analysis["page_analyses"][page_num - 1].get("text_runs", [])


def get_visible_text_runs(
    analysis: Dict[str, Any],
    page_num: int,
) -> list:
    """Get only visible text runs (render_mode != 3) for a page.

    Filters out invisible text (rendering mode 3 = invisible,
    used for OCR overlays and the current invisible MCR approach).
    """
    runs = get_text_runs_for_page(analysis, page_num)
    return [r for r in runs if r.get("render_mode", 0) != 3]


def print_analysis_summary(analysis: Dict[str, Any]) -> None:
    """Print a human-readable summary of the analysis."""
    s = analysis["summary"]
    print(f"Content Stream Analysis: {analysis['file']}")
    print(f"  Pages: {analysis['pages']}")
    print(f"  Text runs: {s['total_text_runs']}")
    print(f"  Characters: {s['total_chars']}")
    print(f"  Images: {s['total_images']}")
    print(f"  Fonts ({s['total_fonts']}): {', '.join(s['all_fonts'])}")
    print(f"  Pages with existing MCIDs: {s['pages_with_existing_mcids']}")

    # Per-page summary
    for pa in analysis["page_analyses"]:
        runs = pa["text_run_count"]
        imgs = pa["image_count"]
        chars = pa["total_chars"]
        mcid = "MCIDs" if pa["has_existing_mcids"] else ""
        if runs > 0 or imgs > 0:
            print(f"    Page {pa['page']:3d}: {runs:4d} runs, "
                  f"{chars:5d} chars, {imgs:2d} images {mcid}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Analyze PDF content streams (Phase 1)"
    )
    parser.add_argument("pdf", help="Path to PDF file")
    parser.add_argument("-o", "--output", help="Write JSON to file")
    parser.add_argument("--verbose", "-v", action="store_true",
                       help="Print per-page details")
    args = parser.parse_args()

    analysis = analyze_content_streams(args.pdf)
    print_analysis_summary(analysis)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(analysis, f, indent=2)
        print(f"\nJSON written to: {args.output}")
