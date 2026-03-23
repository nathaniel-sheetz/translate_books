#!/usr/bin/env python3
"""
CLI script for combining translated chunks back into complete chapters.

This script merges translated chunks using the "use_previous" overlap
resolution strategy.

Usage:
    python combine_chunks.py chunks/translated/chapter_01_chunk_*.json --output chapter_01.txt
    python combine_chunks.py chunk_001.json chunk_002.json chunk_003.json --output chapter.txt

Examples:
    # Combine all chunks for a chapter
    python combine_chunks.py chunks/translated/chapter_01_chunk_*.json --output chapter_01_translated.txt

    # Explicit chunk list
    python combine_chunks.py chunk_001.json chunk_002.json chunk_003.json --output chapter.txt

    # Show detailed combination info
    python combine_chunks.py chunks/translated/ch01*.json --output chapter.txt --verbose
"""

import argparse
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.combiner import combine_chunks, validate_chunk_completeness
from src.utils.file_io import load_chunk
from src.utils.text_utils import count_words, count_paragraphs


def load_chunks_from_files(chunk_files):
    """Load chunks from JSON files."""
    chunks = []
    failed_files = []

    for file_path in chunk_files:
        try:
            chunk = load_chunk(file_path)
            chunks.append(chunk)
        except FileNotFoundError:
            failed_files.append((file_path, "File not found"))
        except json.JSONDecodeError as e:
            failed_files.append((file_path, f"Invalid JSON: {e}"))
        except Exception as e:
            failed_files.append((file_path, str(e)))

    return chunks, failed_files


def print_validation_errors(errors):
    """Print validation errors in a user-friendly format."""
    print("\n" + "=" * 70)
    print("VALIDATION ERRORS")
    print("=" * 70)
    for error in errors:
        print(f"  ✗ {error}")
    print()


def print_combination_summary(chunks, output_file, combined_text, verbose=False):
    """Print summary of combination results."""
    print("\n" + "=" * 70)
    print("COMBINATION COMPLETE!")
    print("=" * 70)
    print(f"\nLoaded {len(chunks)} chunk(s) for {chunks[0].chapter_id}")

    print("\nValidation: ✓ Passed")
    print(f"  - All chunks present (positions {chunks[0].position}-{chunks[-1].position})")
    print(f"  - All chunks translated")
    print(f"  - Same chapter_id: {chunks[0].chapter_id}")

    if verbose and len(chunks) > 1:
        print("\nCombining with 'use_previous' strategy:")
        for i, chunk in enumerate(chunks):
            if i == 0:
                print(f"  - Chunk {i}: {chunk.metadata.word_count:,} words (full text)")
            else:
                overlap_removed = chunk.metadata.overlap_start
                remaining_words = count_words(chunk.translated_text[overlap_removed:])
                print(f"  - Chunk {i}: {remaining_words:,} words "
                      f"({overlap_removed} chars overlap removed)")

    # Combined chapter stats
    final_words = count_words(combined_text)
    final_paras = count_paragraphs(combined_text)

    print(f"\nCombined chapter:")
    print(f"  Words: {final_words:,}")
    print(f"  Paragraphs: {final_paras}")
    print(f"\nOutput: {output_file}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description='Combine translated chunks into a complete chapter.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Combine all chunks for a chapter (glob pattern)
  %(prog)s chunks/translated/chapter_01_chunk_*.json --output chapter_01.txt

  # Explicit chunk list
  %(prog)s chunk_001.json chunk_002.json chunk_003.json --output chapter.txt

  # With verbose output
  %(prog)s chunks/translated/ch01*.json --output chapter.txt --verbose

Strategy:
  Uses "use_previous" overlap resolution:
  - Keeps overlap from the chunk that ends with it
  - Discards overlap from the chunk that starts with it
  - Rationale: Translator has more context at END of chunk
        """
    )

    # Required arguments
    parser.add_argument(
        'chunk_files',
        nargs='+',
        type=str,
        help='Chunk JSON files (supports glob patterns like "ch01*.json")'
    )

    parser.add_argument(
        '--output',
        type=Path,
        required=True,
        help='Output file for combined chapter'
    )

    # Optional arguments
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Show detailed combination statistics'
    )

    args = parser.parse_args()

    # Expand glob patterns
    all_files = []
    for pattern in args.chunk_files:
        # Check if it's a glob pattern or explicit file
        if '*' in pattern or '?' in pattern:
            matched = glob.glob(pattern)
            if not matched:
                print(f"Warning: Pattern '{pattern}' matched no files")
            all_files.extend(matched)
        else:
            all_files.append(pattern)

    if not all_files:
        print("Error: No chunk files specified or found")
        sys.exit(1)

    # Convert to Path objects
    chunk_paths = [Path(f) for f in all_files]

    print(f"\nCombining chunks...")
    print(f"Found {len(chunk_paths)} chunk file(s)")

    # Load chunks
    chunks, failed_files = load_chunks_from_files(chunk_paths)

    if failed_files:
        print("\n" + "=" * 70)
        print("ERRORS LOADING CHUNKS")
        print("=" * 70)
        for file_path, error in failed_files:
            print(f"  ✗ {file_path}: {error}")
        print(f"\nFailed to load {len(failed_files)} file(s)")
        sys.exit(1)

    if not chunks:
        print("Error: No chunks loaded successfully")
        sys.exit(1)

    # Validate chunks
    print(f"\nValidating chunks...")
    is_valid, errors = validate_chunk_completeness(chunks)

    if not is_valid:
        print_validation_errors(errors)
        print("Cannot combine chunks - please fix the errors above")
        sys.exit(1)

    # Combine chunks
    try:
        combined_text = combine_chunks(chunks)
    except ValueError as e:
        print(f"\nError during combination: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nUnexpected error during combination: {e}")
        sys.exit(1)

    # Save combined chapter
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(combined_text, encoding='utf-8')
    except Exception as e:
        print(f"\nError saving output file: {e}")
        sys.exit(1)

    # Print summary
    # Sort chunks for display
    sorted_chunks = sorted(chunks, key=lambda c: c.position)
    print_combination_summary(sorted_chunks, args.output, combined_text, args.verbose)


if __name__ == '__main__':
    main()
