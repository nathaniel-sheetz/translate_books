#!/usr/bin/env python3
"""
CLI script for exporting a bilingual (parallel) review file.

Produces a single text file with source and translation interleaved, chunk by
chunk, so you can read both languages side-by-side and catch translation errors.

Usage:
    python export_bilingual.py fabre_chunks/chapter_01_chunk_000.json --output review_ch01.txt
    python export_bilingual.py fabre_chunks/chapter_*.json --output review_all.txt
    python export_bilingual.py fabre_chunks/*.json   # defaults to bilingual_export.txt

Chunks without a translation are included with a placeholder — the script
never aborts because of untranslated chunks.
"""

import argparse
import glob
import json
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.combiner import generate_bilingual_text
from src.utils.file_io import load_chunk


def load_chunks_from_files(chunk_files):
    """Load chunks from JSON files, collecting errors without aborting."""
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


def main():
    parser = argparse.ArgumentParser(
        description="Export a bilingual review file with source and translation interleaved.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single chapter
  %(prog)s fabre_chunks/chapter_01_chunk_*.json --output review_ch01.txt

  # All chapters
  %(prog)s fabre_chunks/*.json --output review_all.txt

  # Default output filename
  %(prog)s fabre_chunks/chapter_01_chunk_000.json
        """,
    )

    parser.add_argument(
        "chunk_files",
        nargs="+",
        type=str,
        help='Chunk JSON files (supports glob patterns like "chapter_01*.json")',
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("bilingual_export.txt"),
        help="Output file (default: bilingual_export.txt)",
    )

    args = parser.parse_args()

    # Expand glob patterns (the shell may not do this on Windows)
    all_files = []
    for pattern in args.chunk_files:
        if "*" in pattern or "?" in pattern or "[" in pattern:
            matched = sorted(glob.glob(pattern))
            if not matched:
                print(f"Warning: Pattern '{pattern}' matched no files")
            all_files.extend(matched)
        else:
            all_files.append(pattern)

    if not all_files:
        print("Error: No chunk files specified or found")
        sys.exit(1)

    chunk_paths = [Path(f) for f in all_files]
    print(f"\nLoading {len(chunk_paths)} chunk file(s)...")

    chunks, failed_files = load_chunks_from_files(chunk_paths)

    if failed_files:
        print("\nWarning: Failed to load some files:")
        for file_path, error in failed_files:
            print(f"  x {file_path}: {error}")

    if not chunks:
        print("Error: No chunks loaded successfully")
        sys.exit(1)

    translated_count = sum(1 for c in chunks if c.has_translation)
    pending_count = len(chunks) - translated_count

    print(f"Loaded {len(chunks)} chunk(s): {translated_count} translated, {pending_count} pending")

    bilingual_text = generate_bilingual_text(chunks)

    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(bilingual_text, encoding="utf-8")
    except Exception as e:
        print(f"\nError saving output file: {e}")
        sys.exit(1)

    print(f"\nExport complete: {args.output}")
    print(f"  Chunks: {len(chunks)}  |  Translated: {translated_count}  |  Pending: {pending_count}")


if __name__ == "__main__":
    main()
