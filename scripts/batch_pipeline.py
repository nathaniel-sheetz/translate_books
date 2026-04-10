#!/usr/bin/env python3
"""
Run pipeline stages (evaluate, combine) across all chapters in a project.

Cross-platform replacement for combine_all.ps1. Discovers chapters
automatically from chunk filenames in the project's chunks/ directory.

Usage:
    python batch_pipeline.py <project_dir> [--stages STAGES] [options]

Examples:
    # Combine all chapters
    python batch_pipeline.py projects/lang-faerie --stages combine

    # Evaluate then combine
    python batch_pipeline.py projects/lang-faerie --stages evaluate,combine

    # Evaluate only, with glossary
    python batch_pipeline.py projects/lang-faerie --stages evaluate --glossary projects/lang-faerie/glossary.json

    # Process specific chapters
    python batch_pipeline.py projects/lang-faerie --stages combine --chapters chapter_01,chapter_02

    # Dry run to see what would happen
    python batch_pipeline.py projects/lang-faerie --stages evaluate,combine --dry-run
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.combiner import combine_chunks, validate_chunk_completeness
from src.evaluators import run_all_evaluators, aggregate_results
from src.models import ChunkStatus, EvaluationConfig
from src.utils.file_io import load_chunk, save_chunk, load_glossary


def discover_chapters(chunks_dir: Path) -> dict[str, list[Path]]:
    """
    Discover chapters by scanning chunk JSON files and grouping by chapter_id.

    Extracts the chapter_id from filenames by stripping the _chunk_NNN.json
    suffix. Returns a dict mapping chapter_id to sorted list of chunk paths.
    """
    chapters: dict[str, list[Path]] = {}

    for chunk_path in sorted(chunks_dir.glob("*_chunk_*.json")):
        # Extract chapter_id: everything before _chunk_NNN.json
        match = re.match(r"^(.+)_chunk_\d+\.json$", chunk_path.name)
        if not match:
            continue
        chapter_id = match.group(1)
        chapters.setdefault(chapter_id, []).append(chunk_path)

    # Sort chunk paths within each chapter by name (ensures position order)
    for chapter_id in chapters:
        chapters[chapter_id].sort()

    # Sort chapters naturally (chapter_01 before chapter_10)
    return dict(sorted(chapters.items(), key=lambda kv: _natural_sort_key(kv[0])))


def _natural_sort_key(s: str):
    """Sort key that handles embedded numbers naturally."""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", s)]


def run_evaluate_stage(chapter_id, chunks, eval_config, glossary, verbose):
    """
    Evaluate all translated chunks for a chapter.

    Returns dict with: chapter_id, total, evaluated, passed, failed, skipped, errors.
    """
    result = {
        "chapter_id": chapter_id,
        "total": len(chunks),
        "evaluated": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "errors": [],
    }

    for chunk in chunks:
        if not chunk.translated_text or not chunk.translated_text.strip():
            result["skipped"] += 1
            if verbose:
                print(f"    {chunk.id}: skipped (no translation)")
            continue

        try:
            eval_results = run_all_evaluators(chunk, eval_config, glossary)
            aggregated = aggregate_results(eval_results)
            result["evaluated"] += 1

            if aggregated["overall_passed"]:
                result["passed"] += 1
                chunk.status = ChunkStatus.VALIDATED
                if verbose:
                    print(f"    {chunk.id}: PASSED")
            else:
                result["failed"] += 1
                chunk.status = ChunkStatus.FAILED
                errors = aggregated["issues_by_severity"]["error"]
                warnings = aggregated["issues_by_severity"]["warning"]
                if verbose:
                    print(f"    {chunk.id}: FAILED ({errors} errors, {warnings} warnings)")
        except Exception as e:
            result["errors"].append(f"{chunk.id}: {e}")
            if verbose:
                print(f"    {chunk.id}: ERROR ({e})")

    return result


def run_combine_stage(chapter_id, chunks, output_dir, verbose):
    """
    Validate and combine chunks for a chapter, write output file.

    Returns dict with: chapter_id, combined (bool), output_path, error.
    """
    result = {
        "chapter_id": chapter_id,
        "combined": False,
        "output_path": None,
        "error": None,
    }

    # Validate
    is_valid, errors = validate_chunk_completeness(chunks)
    if not is_valid:
        result["error"] = "; ".join(errors)
        if verbose:
            for error in errors:
                print(f"    {error}")
        return result

    # Combine
    try:
        combined_text = combine_chunks(chunks)
    except ValueError as e:
        result["error"] = str(e)
        return result

    # Write output
    output_path = output_dir / f"{chapter_id}.txt"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(combined_text, encoding="utf-8")

    result["combined"] = True
    result["output_path"] = output_path
    if verbose:
        from src.utils.text_utils import count_words
        print(f"    {count_words(combined_text):,} words -> {output_path}")

    return result


def print_summary(eval_results, combine_results, stages):
    """Print a summary table of results."""
    print()
    print("=" * 70)
    print("BATCH PIPELINE SUMMARY")
    print("=" * 70)

    # Build header and rows
    header_parts = ["Chapter"]
    if "evaluate" in stages:
        header_parts.extend(["Chunks", "Eval'd", "Passed"])
    if "combine" in stages:
        header_parts.append("Combined")

    # Determine column widths
    col_widths = [max(14, len(h)) for h in header_parts]
    col_widths[0] = 20  # chapter column wider

    header_line = "  ".join(h.ljust(w) for h, w in zip(header_parts, col_widths))
    print(f"\n{header_line}")
    print("-" * len(header_line))

    # Collect all chapter IDs
    all_chapters = set()
    eval_by_chapter = {}
    combine_by_chapter = {}
    if eval_results:
        for r in eval_results:
            all_chapters.add(r["chapter_id"])
            eval_by_chapter[r["chapter_id"]] = r
    if combine_results:
        for r in combine_results:
            all_chapters.add(r["chapter_id"])
            combine_by_chapter[r["chapter_id"]] = r

    sorted_chapters = sorted(all_chapters, key=_natural_sort_key)

    total_chunks = 0
    total_evaluated = 0
    total_passed = 0
    total_combined = 0
    total_chapters = len(sorted_chapters)

    for ch in sorted_chapters:
        parts = [ch]

        if "evaluate" in stages:
            er = eval_by_chapter.get(ch)
            if er:
                total_chunks += er["total"]
                total_evaluated += er["evaluated"]
                total_passed += er["passed"]
                parts.append(str(er["total"]))
                parts.append(str(er["evaluated"]))
                passed_str = f"{er['passed']}/{er['evaluated']}" if er["evaluated"] else "-"
                parts.append(passed_str)
            else:
                parts.extend(["-", "-", "-"])

        if "combine" in stages:
            cr = combine_by_chapter.get(ch)
            if cr:
                if cr["combined"]:
                    total_combined += 1
                    parts.append("Yes")
                else:
                    reason = cr["error"] or "unknown error"
                    # Truncate long error messages
                    if len(reason) > 30:
                        reason = reason[:27] + "..."
                    parts.append(f"No ({reason})")
            else:
                parts.append("-")

        row = "  ".join(p.ljust(w) for p, w in zip(parts, col_widths))
        print(row)

    # Totals
    print("-" * len(header_line))
    total_parts = ["TOTAL"]
    if "evaluate" in stages:
        total_parts.append(str(total_chunks))
        total_parts.append(str(total_evaluated))
        total_parts.append(f"{total_passed}/{total_evaluated}" if total_evaluated else "-")
    if "combine" in stages:
        total_parts.append(f"{total_combined}/{total_chapters}")

    total_row = "  ".join(p.ljust(w) for p, w in zip(total_parts, col_widths))
    print(total_row)
    print()


def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Run pipeline stages across all chapters in a project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s projects/lang-faerie --stages combine
  %(prog)s projects/lang-faerie --stages evaluate,combine
  %(prog)s projects/lang-faerie --stages evaluate --glossary glossary.json
  %(prog)s projects/lang-faerie --stages combine --chapters chapter_01,chapter_02
  %(prog)s projects/lang-faerie --dry-run
        """,
    )

    parser.add_argument(
        "project_dir",
        type=Path,
        help="Project directory (e.g., projects/lang-faerie)",
    )

    parser.add_argument(
        "--stages",
        default="combine",
        help="Comma-separated stages to run: evaluate, combine (default: combine)",
    )

    parser.add_argument(
        "--glossary",
        type=Path,
        help="Path to glossary JSON (auto-discovers {project}/glossary.json if omitted)",
    )

    parser.add_argument(
        "--evaluators",
        help="Comma-separated evaluator names (default: length,paragraph,completeness)",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Override combined chapter output directory (default: {project}/chapters/)",
    )

    parser.add_argument(
        "--chapters",
        help="Comma-separated chapter IDs to process (default: all)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without doing it",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show per-chunk details",
    )

    return parser.parse_args()


def main():
    args = parse_arguments()

    # Validate project directory
    project_dir = args.project_dir
    if not project_dir.is_dir():
        print(f"Error: Project directory not found: {project_dir}", file=sys.stderr)
        sys.exit(1)

    chunks_dir = project_dir / "chunks"
    if not chunks_dir.is_dir():
        print(f"Error: No chunks/ directory in {project_dir}", file=sys.stderr)
        sys.exit(1)

    # Parse stages
    stages = [s.strip() for s in args.stages.split(",")]
    valid_stages = {"evaluate", "combine"}
    invalid = set(stages) - valid_stages
    if invalid:
        print(f"Error: Unknown stages: {', '.join(invalid)}", file=sys.stderr)
        print(f"Valid stages: {', '.join(valid_stages)}", file=sys.stderr)
        sys.exit(1)

    # Discover chapters
    chapters = discover_chapters(chunks_dir)
    if not chapters:
        print(f"No chunk files found in {chunks_dir}")
        sys.exit(0)

    # Filter chapters if requested
    if args.chapters:
        requested = [c.strip() for c in args.chapters.split(",")]
        missing = [c for c in requested if c not in chapters]
        if missing:
            print(f"Warning: Chapters not found: {', '.join(missing)}", file=sys.stderr)
        chapters = {k: v for k, v in chapters.items() if k in requested}
        if not chapters:
            print("Error: No matching chapters found", file=sys.stderr)
            sys.exit(1)

    # Load glossary
    glossary = None
    if "evaluate" in stages:
        glossary_path = args.glossary
        if glossary_path is None:
            # Auto-discover
            auto_path = project_dir / "glossary.json"
            if auto_path.exists():
                glossary_path = auto_path

        if glossary_path:
            try:
                glossary = load_glossary(glossary_path)
                print(f"Glossary: {len(glossary.terms)} terms from {glossary_path}")
            except Exception as e:
                print(f"Warning: Could not load glossary: {e}", file=sys.stderr)

    # Build evaluation config
    eval_config = EvaluationConfig()
    if args.evaluators:
        eval_config.enabled_evals = [e.strip() for e in args.evaluators.split(",")]

    # Output directory for combine
    output_dir = args.output_dir or (project_dir / "chapters")

    # Print plan
    print(f"\nProject: {project_dir}")
    print(f"Stages: {', '.join(stages)}")
    print(f"Chapters: {len(chapters)}")
    total_chunks = sum(len(paths) for paths in chapters.values())
    print(f"Total chunks: {total_chunks}")
    if "combine" in stages:
        print(f"Output: {output_dir}")
    print()

    if args.dry_run:
        print("DRY RUN - no changes will be made\n")
        for chapter_id, chunk_paths in chapters.items():
            print(f"  {chapter_id}: {len(chunk_paths)} chunk(s)")
            if args.verbose:
                for p in chunk_paths:
                    print(f"    {p.name}")
        print(f"\nWould run: {', '.join(stages)} on {len(chapters)} chapters")
        sys.exit(0)

    # Run stages
    eval_results = []
    combine_results = []

    for chapter_id, chunk_paths in chapters.items():
        print(f"  {chapter_id} ({len(chunk_paths)} chunks)")

        # Load chunks
        chunks = []
        load_failed = False
        for chunk_path in chunk_paths:
            try:
                chunks.append(load_chunk(chunk_path))
            except Exception as e:
                print(f"    Error loading {chunk_path.name}: {e}", file=sys.stderr)
                load_failed = True

        if load_failed or not chunks:
            print(f"    Skipping {chapter_id}: failed to load chunks")
            continue

        # Sort by position
        chunks.sort(key=lambda c: c.position)

        # Evaluate stage
        if "evaluate" in stages:
            if args.verbose:
                print(f"    Evaluating...")
            er = run_evaluate_stage(chapter_id, chunks, eval_config, glossary, args.verbose)
            eval_results.append(er)

            # Save updated chunk statuses
            for chunk, chunk_path in zip(chunks, chunk_paths):
                try:
                    save_chunk(chunk, chunk_path)
                except Exception as e:
                    if args.verbose:
                        print(f"    Warning: could not save {chunk.id}: {e}")

        # Combine stage
        if "combine" in stages:
            if args.verbose:
                print(f"    Combining...")
            cr = run_combine_stage(chapter_id, chunks, output_dir, args.verbose)
            combine_results.append(cr)

            if not cr["combined"]:
                print(f"    Could not combine: {cr['error']}")

    # Summary
    print_summary(eval_results, combine_results, stages)

    # Exit code: non-zero if any combine failed
    if combine_results and not all(r["combined"] for r in combine_results):
        sys.exit(1)


if __name__ == "__main__":
    main()
