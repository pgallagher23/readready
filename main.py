#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ReadReady — CLI Entry Point.

Converts inaccessible PDFs to accessible HTML (WCAG 2.1 AA) using
Docling for document understanding and a local VLM for remediation.

Usage:
    # Convert a single PDF
    python main.py input.pdf -o output/

    # Convert all PDFs in a directory
    python main.py input_dir/ -o output/

    # Use a custom config file
    python main.py input.pdf -o output/ --config my_config.yaml
"""

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import List

from dotenv import load_dotenv
load_dotenv()

# Guard: ensure required packages are importable before doing anything else.
try:
    import bs4  # noqa: F401
    import docling  # noqa: F401
except ModuleNotFoundError as _e:
    print(
        f"ERROR: Missing dependency ({_e}). "
        "Run the pipeline inside the project virtualenv:\n"
        "  source .venv/bin/activate\n"
        "  python main.py ...\n"
        "Or install dependencies first:\n"
        "  pip install -r requirements.txt",
        file=sys.stderr,
    )
    sys.exit(1)

from output_gen.pdf_tagger import compute_java_heap, get_system_ram_mb
from pipeline_core import run_pipeline
from utils.config_loader import load_config
from logging_util.outcome_recorder import (
    OutcomeWriter,
    build_outcome_record,
    new_run_id,
    run_file_log,
    update_latest_symlink,
)

logger = logging.getLogger("pdf_accessibility")
PROJECT_ROOT = Path(__file__).resolve().parent


def setup_logging(verbose: bool = False) -> None:
    """Configure logging for the pipeline."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress HTTP library debug logs (they dump full request bodies
    # including base64-encoded images, bloating log files).
    for noisy in ("urllib3", "requests", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def find_pdfs(input_path: Path) -> List[Path]:
    """Find PDF files from the input path.

    Args:
        input_path: A single PDF file or a directory containing PDFs.

    Returns:
        List of PDF file paths.

    Raises:
        FileNotFoundError: If the input path doesn't exist.
        ValueError: If no PDFs are found.
    """
    if not input_path.exists():
        raise FileNotFoundError(f"Input path not found: {input_path}")

    if input_path.is_file():
        if input_path.suffix.lower() != ".pdf":
            raise ValueError(f"Input file is not a PDF: {input_path}")
        return [input_path]

    if input_path.is_dir():
        pdfs = sorted(input_path.glob("*.pdf"))
        if not pdfs:
            raise ValueError(f"No PDF files found in: {input_path}")
        return pdfs

    raise ValueError(f"Input path is neither a file nor directory: {input_path}")


def process_single_pdf(pdf_path: Path, output_dir: Path, config: dict) -> dict:
    """Process a single PDF through the full accessibility pipeline.

    Thin wrapper around pipeline_core.run_pipeline for CLI usage.
    """
    return run_pipeline(
        pdf_path=str(pdf_path),
        output_dir=str(output_dir),
        config=config,
    )


def main() -> int:
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        description="Convert inaccessible PDFs to accessible HTML (WCAG 2.1 AA)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py document.pdf -o output/
  python main.py ./pdfs/ -o output/
  python main.py document.pdf -o output/ --config my_config.yaml -v
        """,
    )
    parser.add_argument(
        "input",
        type=str,
        help="Input PDF file or directory containing PDFs",
    )
    parser.add_argument(
        "-o", "--output",
        type=str,
        default="output",
        help="Output directory (default: ./output)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument(
        "-j", "--jobs",
        type=int,
        default=None,
        help="Number of parallel workers for batch processing (default: from config)",
    )
    parser.add_argument(
        "--max-heap",
        type=str,
        default=None,
        help="JVM max heap for iText tagger (e.g. '2g', '4g'). "
             "Overrides config.yaml resources.java_heap. Default: auto-detect.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip PDFs whose output already exists (resume interrupted batch)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose/debug logging",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)

    # Load configuration
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        logger.error(str(e))
        return 1

    # Apply CLI overrides
    if args.max_heap:
        config.setdefault("resources", {})
        config["resources"]["java_heap"] = args.max_heap

    # Find PDFs to process
    input_path = Path(args.input)
    try:
        pdf_files = find_pdfs(input_path)
    except (FileNotFoundError, ValueError) as e:
        logger.error(str(e))
        return 1

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Found {len(pdf_files)} PDF(s) to process")
    logger.info(f"Output directory: {output_dir.resolve()}")

    # Resume mode: skip PDFs whose accessible output already exists
    if args.resume:
        before = len(pdf_files)
        pdf_files = [
            p for p in pdf_files
            if not (output_dir / p.stem / f"{p.stem}_accessible.pdf").exists()
        ]
        skipped = before - len(pdf_files)
        if skipped:
            logger.info(f"Resume mode: skipping {skipped} already-completed PDFs")
        logger.info(f"Remaining to process: {len(pdf_files)}")
        if not pdf_files:
            logger.info("Nothing to do — all PDFs already processed.")
            return 0

    # Determine parallelism — auto-detect safe worker count from RAM
    batch_workers = args.jobs or config.get("concurrency", {}).get("batch_workers", 2)
    resources = config.get("resources", {})
    max_ram_pct = resources.get("max_ram_percent", 75)
    python_worker_mb = resources.get("python_worker_mb", 3000)
    total_ram_mb = get_system_ram_mb()
    usable_mb = int(total_ram_mb * max_ram_pct / 100)

    # Auto-cap workers so they fit in available RAM (Python + JVM per worker)
    # Minimum 512MB for JVM per worker
    per_worker_mb = python_worker_mb + 512
    max_safe_workers = max(usable_mb // per_worker_mb, 1)
    if batch_workers > max_safe_workers:
        logger.warning(
            "Reducing batch_workers from %d to %d "
            "(system RAM: %dMB, usable: %dMB, ~%dMB per worker)",
            batch_workers, max_safe_workers, total_ram_mb, usable_mb, per_worker_mb,
        )
        batch_workers = max_safe_workers

    # Store effective worker count in config so pipeline_core can read it
    config.setdefault("concurrency", {})["batch_workers"] = batch_workers

    # Compute heap for startup log
    java_heap = compute_java_heap(config, batch_workers=batch_workers)

    # Resource summary
    logger.info(
        "System RAM: %dMB | usable (%d%%): %dMB | workers: %d | "
        "JVM heap per tagger: %s",
        total_ram_mb, max_ram_pct, usable_mb, batch_workers, java_heap,
    )

    # Sort PDFs largest-first so big files start early (avoids long tail)
    if len(pdf_files) > 1:
        pdf_files.sort(key=lambda p: p.stat().st_size, reverse=True)

    # Per-run structured outcome log (errors + remediation reasons → JSONL)
    run_id = new_run_id()
    runs_root = PROJECT_ROOT / "logs" / "runs"
    run_dir = runs_root / run_id
    writer = OutcomeWriter(run_dir, run_id)
    logger.info("Run log: %s", writer.jsonl_path)

    def _record(pdf_path, result=None, exc=None):
        """Build + persist a per-PDF outcome record (parent-side; never in workers)."""
        rec = build_outcome_record(
            source="production", run_id=run_id, pdf_path=str(pdf_path),
            result=result, exc=exc, error_phase="pipeline" if exc else None, config=config,
        )
        outcome_records.append(rec)
        writer.write_record(rec)
        if result is not None:
            writer.emit_phase_errors(rec)
            for warning in result.get("warnings") or []:
                logger.warning("%s: %s", Path(pdf_path).name, warning)

    # Process PDFs
    total_start = time.time()
    results = []
    outcome_records = []
    failed = []

    with run_file_log(run_dir):
        if len(pdf_files) > 1 and batch_workers > 1:
            from concurrent.futures import ProcessPoolExecutor, as_completed

            logger.info(f"Processing {len(pdf_files)} PDFs with {batch_workers} workers")
            with ProcessPoolExecutor(max_workers=batch_workers) as pool:
                future_to_pdf = {
                    pool.submit(process_single_pdf, pdf, output_dir, config): pdf
                    for pdf in pdf_files
                }
                for future in as_completed(future_to_pdf):
                    pdf_path = future_to_pdf[future]
                    try:
                        result = future.result()
                        results.append(result)
                        _record(pdf_path, result=result)
                        logger.info("Completed: %s", pdf_path.name)
                    except Exception as e:
                        logger.error(f"Failed to process {pdf_path.name}: {e}")
                        if args.verbose:
                            logger.exception("Full traceback:")
                        failed.append(pdf_path)
                        writer.emit_error_event(pdf_path=str(pdf_path), phase="pipeline",
                                                message=str(e), exc=e)
                        _record(pdf_path, exc=e)
        else:
            for pdf_path in pdf_files:
                try:
                    result = process_single_pdf(pdf_path, output_dir, config)
                    results.append(result)
                    _record(pdf_path, result=result)
                except Exception as e:
                    logger.error(f"Failed to process {pdf_path.name}: {e}")
                    if args.verbose:
                        logger.exception("Full traceback:")
                    failed.append(pdf_path)
                    writer.emit_error_event(pdf_path=str(pdf_path), phase="pipeline",
                                            message=str(e), exc=e)
                    _record(pdf_path, exc=e)

    # Summary
    total_elapsed = time.time() - total_start
    writer.write_summary(outcome_records, total_elapsed)
    update_latest_symlink(runs_root, run_id)
    logger.info("")
    logger.info("=" * 50)
    logger.info(f"Processing complete in {total_elapsed:.1f}s")
    logger.info(f"  Succeeded: {len(results)}")
    if failed:
        logger.info(f"  Failed:    {len(failed)}")
        for f in failed:
            logger.info(f"    - {f.name}")
        logger.info(
            "  Tip: If failures were caused by memory, try reducing "
            "--jobs or increasing --max-heap."
        )
    logger.info("=" * 50)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
