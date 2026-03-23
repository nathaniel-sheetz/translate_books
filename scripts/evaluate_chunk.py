#!/usr/bin/env python3
"""
Evaluate a translation chunk with multiple evaluators and generate a combined report.

This script runs one or more evaluators on a translated chunk and generates
a combined report in text, JSON, or HTML format.

Usage:
    python evaluate_chunk.py <chunk.json> [options]

Examples:
    # Basic usage - run all evaluators, text output
    python evaluate_chunk.py tests/fixtures/chunk_translated_good.json

    # With glossary for glossary compliance checking
    python evaluate_chunk.py chunk.json --glossary glossary.json

    # Run specific evaluators only
    python evaluate_chunk.py chunk.json --evaluators length,paragraph

    # Generate HTML report
    python evaluate_chunk.py chunk.json --format html --output report.html

    # Generate all three report formats
    python evaluate_chunk.py chunk.json --format all --output reports/

Available Evaluators:
    - length: Check if translation length is within expected range
    - paragraph: Verify paragraph structure is preserved
    - dictionary: Check for English words and misspellings
    - glossary: Validate glossary term usage (requires --glossary)

Report Formats:
    - text: Rich-formatted console output (default)
    - json: Structured JSON data
    - html: Web-viewable HTML report
    - all: Generate all three formats
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.file_io import load_chunk, load_glossary
from src.evaluators import run_all_evaluators, aggregate_results, get_evaluator
from src.evaluators.reporting import (
    generate_text_report,
    generate_json_report,
    generate_html_report,
)
from src.config import create_default_config
from src.models import Chunk, Glossary


# Known evaluator names (from evaluator registry)
KNOWN_EVALUATORS = ["length", "paragraph", "dictionary", "glossary"]


def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Evaluate a translation chunk with multiple evaluators.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s chunk.json
  %(prog)s chunk.json --glossary glossary.json
  %(prog)s chunk.json --evaluators length,paragraph
  %(prog)s chunk.json --format html --output report.html
  %(prog)s chunk.json --format all --output reports/
        """
    )

    parser.add_argument(
        "chunk",
        help="Path to chunk JSON file"
    )

    parser.add_argument(
        "--glossary",
        help="Path to glossary JSON file (optional, enables glossary evaluator)"
    )

    parser.add_argument(
        "--evaluators",
        help=(
            "Comma-separated list of evaluators to run "
            "(default: all available). Available: " + ", ".join(KNOWN_EVALUATORS)
        )
    )

    parser.add_argument(
        "--format",
        choices=["text", "json", "html", "all"],
        default="text",
        help="Report format (default: text)"
    )

    parser.add_argument(
        "--output",
        help=(
            "Output file path (for single format) or directory path (for 'all' format). "
            "If not specified, text format prints to stdout."
        )
    )

    return parser.parse_args()


def validate_evaluator_names(names: list[str]) -> tuple[list[str], list[str]]:
    """
    Validate evaluator names against known evaluators.

    Args:
        names: List of evaluator names to validate

    Returns:
        Tuple of (valid_names, invalid_names)
    """
    valid = []
    invalid = []

    for name in names:
        if name in KNOWN_EVALUATORS:
            valid.append(name)
        else:
            invalid.append(name)

    return valid, invalid


def load_chunk_or_exit(chunk_path: Path) -> Chunk:
    """
    Load chunk from file, exit with error if fails.

    Args:
        chunk_path: Path to chunk JSON file

    Returns:
        Loaded Chunk object
    """
    try:
        return load_chunk(chunk_path)
    except FileNotFoundError:
        print(f"Error: Chunk file not found: {chunk_path}", file=sys.stderr)
        print(f"\nMake sure the file exists and the path is correct.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading chunk file: {e}", file=sys.stderr)
        print(f"\nFile: {chunk_path}", file=sys.stderr)
        print(f"Make sure it's a valid chunk JSON file.", file=sys.stderr)
        sys.exit(1)


def load_glossary_or_exit(glossary_path: Path) -> Glossary:
    """
    Load glossary from file, exit with error if fails.

    Args:
        glossary_path: Path to glossary JSON file

    Returns:
        Loaded Glossary object
    """
    try:
        return load_glossary(glossary_path)
    except FileNotFoundError:
        print(f"Error: Glossary file not found: {glossary_path}", file=sys.stderr)
        print(f"\nMake sure the file exists and the path is correct.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error loading glossary file: {e}", file=sys.stderr)
        print(f"\nFile: {glossary_path}", file=sys.stderr)
        print(f"Make sure it's a valid glossary JSON file.", file=sys.stderr)
        sys.exit(1)


def determine_output_path(output_arg: Optional[str], format: str, chunk_id: str) -> Optional[Path]:
    """
    Determine output path based on arguments.

    Args:
        output_arg: Value from --output argument
        format: Report format (text, json, html, all)
        chunk_id: Chunk identifier for filename generation

    Returns:
        Path object or None if stdout
    """
    if output_arg is None:
        if format == "text":
            return None  # Print to stdout
        else:
            print(f"Error: --output is required for format '{format}'", file=sys.stderr)
            sys.exit(1)

    return Path(output_arg)


def save_report_to_file(report: str, output_path: Path, format: str):
    """
    Save report to file.

    Args:
        report: Report content as string
        output_path: Path to save to
        format: Format (for extension if needed)
    """
    try:
        # Create parent directory if needed
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Write file
        output_path.write_text(report, encoding='utf-8')
        print(f"Report saved to: {output_path}")
    except Exception as e:
        print(f"Error saving report: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    """Main entry point."""
    # Fix Windows console encoding for Rich output
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

    args = parse_arguments()

    # Load chunk
    chunk_path = Path(args.chunk)
    chunk = load_chunk_or_exit(chunk_path)

    # Load glossary if provided
    glossary = None
    if args.glossary:
        glossary_path = Path(args.glossary)
        glossary = load_glossary_or_exit(glossary_path)

    # Determine which evaluators to run
    if args.evaluators:
        # Parse comma-separated list
        requested_evals = [e.strip() for e in args.evaluators.split(",")]

        # Validate evaluator names
        valid_evals, invalid_evals = validate_evaluator_names(requested_evals)

        if invalid_evals:
            print(f"Error: Unknown evaluator(s): {', '.join(invalid_evals)}", file=sys.stderr)
            print(f"\nAvailable evaluators: {', '.join(KNOWN_EVALUATORS)}", file=sys.stderr)
            sys.exit(1)

        enabled_evals = valid_evals
    else:
        # Use all available evaluators
        enabled_evals = KNOWN_EVALUATORS.copy()

        # Remove glossary evaluator if no glossary provided
        if glossary is None and "glossary" in enabled_evals:
            enabled_evals.remove("glossary")

    # Warn if glossary evaluator requested but no glossary provided
    if "glossary" in enabled_evals and glossary is None:
        print("Warning: Glossary evaluator requires --glossary flag, skipping...", file=sys.stderr)
        enabled_evals.remove("glossary")

    # Create config with selected evaluators
    config = create_default_config("temp_eval")
    config.evaluation.enabled_evals = enabled_evals

    # Print what we're doing
    print(f"Evaluating chunk: {chunk.id}")
    print(f"Evaluators: {', '.join(enabled_evals)}")
    if glossary:
        print(f"Glossary: {len(glossary.terms)} terms")
    print()

    # Run evaluators
    try:
        results = run_all_evaluators(chunk, config.evaluation, glossary)
        aggregated = aggregate_results(results)
    except Exception as e:
        print(f"Error running evaluators: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # Generate and output report(s)
    if args.format == "all":
        # Generate all three formats
        output_path = determine_output_path(args.output, "all", chunk.id)

        if not output_path.is_dir() and not str(output_path).endswith('/'):
            # Create as directory
            output_path.mkdir(parents=True, exist_ok=True)

        # Generate all three
        text_report = generate_text_report(results, aggregated, chunk)
        json_report = generate_json_report(results, aggregated, chunk)
        html_report = generate_html_report(results, aggregated, chunk)

        # Save each with appropriate extension
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        text_path = output_path / f"eval_{chunk.id}_{timestamp}.txt"
        json_path = output_path / f"eval_{chunk.id}_{timestamp}.json"
        html_path = output_path / f"eval_{chunk.id}_{timestamp}.html"

        save_report_to_file(text_report, text_path, "text")
        save_report_to_file(json_report, json_path, "json")
        save_report_to_file(html_report, html_path, "html")

    elif args.format == "text":
        # Generate text report
        report = generate_text_report(results, aggregated, chunk)

        if args.output:
            output_path = determine_output_path(args.output, "text", chunk.id)
            save_report_to_file(report, output_path, "text")
        else:
            # Print to stdout
            print(report)

    elif args.format == "json":
        # Generate JSON report
        report = generate_json_report(results, aggregated, chunk)

        output_path = determine_output_path(args.output, "json", chunk.id)
        save_report_to_file(report, output_path, "json")

    elif args.format == "html":
        # Generate HTML report
        report = generate_html_report(results, aggregated, chunk)

        output_path = determine_output_path(args.output, "html", chunk.id)
        save_report_to_file(report, output_path, "html")

    # Print summary
    print()
    if aggregated["overall_passed"]:
        print("✓ Evaluation PASSED")
        sys.exit(0)
    else:
        print("✗ Evaluation FAILED")
        print(f"  Errors: {aggregated['issues_by_severity']['error']}")
        print(f"  Warnings: {aggregated['issues_by_severity']['warning']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
