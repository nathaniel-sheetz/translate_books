#!/usr/bin/env python3
"""
Import translations from a completed workbook.

This script parses a completed workbook, extracts translations, and updates
chunk JSON files with the translated text.

Usage:
    python import_workbook.py workbook_completed.md --output chunks/translated/
    python import_workbook.py workbook.md --chunks chunks/original/ --output chunks/translated/
"""

import argparse
import glob
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.translator import import_translations
from src.utils.file_io import load_chunk


def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Import translations from completed workbook",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage
  python import_workbook.py workbook_completed.md --output chunks/translated/

  # Specify original chunks directory
  python import_workbook.py workbook.md \\
      --chunks chunks/original/ \\
      --output chunks/translated/

  # Validate only (don't save files)
  python import_workbook.py workbook.md --validate-only

  # Force overwrite existing files
  python import_workbook.py workbook.md \\
      --output chunks/translated/ \\
      --force

The script will:
  1. Parse the completed workbook
  2. Validate structure and chunk IDs
  3. Extract translations from "PASTE TRANSLATION HERE" sections
  4. Update chunk JSON files with translations
  5. Save updated chunks to output directory

After import, evaluate and combine:
  python evaluate_chunk.py chunks/translated/*.json --glossary glossary.json
  python combine_chunks.py chunks/translated/*.json --output chapter.txt
        """
    )

    # Required arguments
    parser.add_argument(
        'workbook_file',
        type=Path,
        help='Path to completed workbook file',
    )

    parser.add_argument(
        '--output', '-o',
        type=Path,
        required=True,
        help='Output directory for translated chunk JSON files',
    )

    # Optional arguments
    parser.add_argument(
        '--chunks', '-c',
        type=Path,
        help='Directory containing original chunk JSON files (default: infer from workbook)',
    )

    parser.add_argument(
        '--validate-only',
        action='store_true',
        help='Only validate workbook, do not save files',
    )

    parser.add_argument(
        '--force', '-f',
        action='store_true',
        help='Overwrite existing translated chunk files',
    )

    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show detailed progress information',
    )

    return parser.parse_args()


def find_original_chunks(workbook_path: Path, chunks_dir: Optional[Path] = None):
    """
    Find original chunk files referenced in workbook.

    Args:
        workbook_path: Path to workbook file
        chunks_dir: Directory to search for chunks, or None to infer

    Returns:
        List of chunk file paths

    Raises:
        ValueError: If no chunks found or chunks directory not found
    """
    from src.translator import parse_workbook
    import re

    # Read workbook to find chunk IDs
    content = workbook_path.read_text(encoding='utf-8')
    chunk_pattern = r'## CHUNK \d+: ([\w_]+)'
    chunk_ids = re.findall(chunk_pattern, content)

    if not chunk_ids:
        raise ValueError("No chunk IDs found in workbook")

    # Try to infer chunks directory if not specified
    if chunks_dir is None:
        # Look for common locations
        possible_dirs = [
            Path("chunks/original"),
            Path("chunks"),
            Path("."),
        ]

        for dir_path in possible_dirs:
            if dir_path.exists():
                chunks_dir = dir_path
                break

        if chunks_dir is None:
            raise ValueError(
                "Could not find original chunks directory. "
                "Please specify with --chunks option."
            )

    if not chunks_dir.exists():
        raise ValueError(f"Chunks directory not found: {chunks_dir}")

    # Find chunk files
    chunk_files = []
    for chunk_id in chunk_ids:
        chunk_file = chunks_dir / f"{chunk_id}.json"
        if chunk_file.exists():
            chunk_files.append(chunk_file)
        else:
            print(f"Warning: Chunk file not found: {chunk_file}", file=sys.stderr)

    if not chunk_files:
        raise ValueError(
            f"No chunk files found in {chunks_dir}. "
            f"Expected files: {', '.join(f'{cid}.json' for cid in chunk_ids)}"
        )

    return chunk_files


def main():
    """Main entry point."""
    args = parse_arguments()

    # Validate workbook exists
    if not args.workbook_file.exists():
        print(f"Error: Workbook file not found: {args.workbook_file}", file=sys.stderr)
        sys.exit(1)

    if args.verbose:
        print(f"Workbook: {args.workbook_file}")

    # Find original chunk files
    try:
        chunk_files = find_original_chunks(args.workbook_file, args.chunks)
        if args.verbose:
            print(f"\nFound {len(chunk_files)} original chunk file(s)")
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Load original chunks
    if args.verbose:
        print("\nLoading original chunks...")

    original_chunks = []
    for chunk_file in chunk_files:
        if args.verbose:
            print(f"  Loading: {chunk_file.name}")

        try:
            chunk = load_chunk(chunk_file)
            original_chunks.append(chunk)
        except Exception as e:
            print(f"Error loading {chunk_file}: {e}", file=sys.stderr)
            sys.exit(1)

    if args.verbose:
        print(f"\n✓ Loaded {len(original_chunks)} chunks")

    # Validate only mode
    if args.validate_only:
        print("\nValidating workbook structure...")
        from src.translator import validate_workbook_structure

        content = args.workbook_file.read_text(encoding='utf-8')
        expected_ids = [chunk.id for chunk in original_chunks]
        is_valid, errors = validate_workbook_structure(content, expected_ids)

        if is_valid:
            print("✓ Workbook structure is valid")
            print(f"✓ All {len(expected_ids)} chunks have required sections")
            sys.exit(0)
        else:
            print("✗ Workbook validation failed:")
            for error in errors:
                print(f"  - {error}")
            sys.exit(1)

    # Check if output directory exists and has files
    if args.output.exists() and not args.force:
        existing_files = list(args.output.glob("*.json"))
        if existing_files:
            print(f"\nWarning: Output directory contains {len(existing_files)} JSON files", file=sys.stderr)
            print("Use --force to overwrite existing files", file=sys.stderr)
            response = input("Continue anyway? [y/N] ")
            if response.lower() != 'y':
                print("Aborted.")
                sys.exit(0)

    # Import translations
    if args.verbose:
        print("\nImporting translations...")

    try:
        updated_chunks, warnings = import_translations(
            args.workbook_file,
            original_chunks,
            args.output
        )
    except Exception as e:
        print(f"Error importing translations: {e}", file=sys.stderr)
        import traceback
        if args.verbose:
            traceback.print_exc()
        sys.exit(1)

    # Print summary
    print("\n" + "=" * 70)
    print("✓ Import Complete")
    print("=" * 70)

    print(f"\nWorkbook: {args.workbook_file.name}")
    print(f"Original chunks: {len(original_chunks)}")
    print(f"Imported translations: {len(updated_chunks)}")

    if warnings:
        print(f"\n⚠ Warnings: {len(warnings)}")
        for warning in warnings:
            print(f"  - {warning}")

    # Calculate statistics
    total_words = sum(c.metadata.word_count for c in updated_chunks)
    print(f"\nTotal words translated: {total_words:,}")
    print(f"Output directory: {args.output}")

    # List saved files
    if args.verbose and updated_chunks:
        print("\nSaved files:")
        for chunk in updated_chunks:
            output_file = args.output / f"{chunk.id}.json"
            print(f"  - {output_file}")

    # Print next steps
    print("\n" + "-" * 70)
    print("Next Steps:")
    print("-" * 70)

    if warnings:
        print("1. Review warnings above")
        print("2. Evaluate translations:")
    else:
        print("1. Evaluate translations:")

    print(f"   python evaluate_chunk.py {args.output}/*.json --glossary glossary.json")
    print("2. Combine chunks into chapter:")
    print(f"   python combine_chunks.py {args.output}/*.json --output chapter.txt")
    print("=" * 70)


if __name__ == "__main__":
    main()
