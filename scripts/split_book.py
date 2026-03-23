#!/usr/bin/env python3
"""
Split a full book file into individual chapter files based on chapter detection.

This script automatically detects chapter boundaries in a full book text file
and creates separate files for each chapter. Supports Roman numerals, numeric
chapters, and custom patterns.

Usage:
    python split_book.py full_book.txt --output chapters/
    python split_book.py book.txt --output chapters/ --pattern numeric
    python split_book.py book.txt --output chapters/ --pattern custom --custom-regex "^CHAPTER \\d+"
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.book_splitter import (
    split_book_into_chapters,
    save_chapters_to_files,
    validate_chapter_sequence
)


def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Split a full book into individual chapter files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Split book with Roman numeral chapters (Chapter I, Chapter II, etc.)
  python split_book.py full_book.txt --output chapters/

  # Split book with numeric chapters (Chapter 1, Chapter 2, etc.)
  python split_book.py book.txt --output chapters/ --pattern numeric

  # Split with custom chapter pattern
  python split_book.py book.txt --output chapters/ \\
      --pattern custom --custom-regex "^CHAPTER \\\\d+"

  # Custom output filename prefix
  python split_book.py book.txt --output chapters/ --prefix princesa

Chapter Detection Patterns:
  roman   - Matches "Chapter I", "Chapter II", "Chapter III", etc.
  numeric - Matches "Chapter 1", "Chapter 2", "Chapter 3", etc.
  custom  - Use --custom-regex to specify your own pattern

The script will:
  1. Read the full book file
  2. Detect chapter boundaries
  3. Validate the chapter sequence
  4. Create individual chapter files
  5. Report any warnings or issues
        """
    )

    # Required arguments
    parser.add_argument(
        'book_file',
        type=Path,
        help='Path to full book text file',
    )

    parser.add_argument(
        '--output', '-o',
        type=Path,
        required=True,
        help='Output directory for chapter files',
    )

    # Optional arguments
    parser.add_argument(
        '--pattern', '-p',
        choices=['roman', 'numeric', 'custom'],
        default='roman',
        help='Chapter detection pattern type (default: roman)',
    )

    parser.add_argument(
        '--custom-regex',
        type=str,
        help='Custom regex pattern (required if --pattern is custom)',
    )

    parser.add_argument(
        '--prefix',
        type=str,
        default='chapter',
        help='Filename prefix for chapter files (default: "chapter")',
    )

    parser.add_argument(
        '--min-size',
        type=int,
        default=100,
        help='Minimum characters for valid chapter (default: 100)',
    )

    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show detailed progress and chapter information',
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Detect chapters but do not create files',
    )

    return parser.parse_args()


def main():
    """Main entry point."""
    args = parse_arguments()

    # Validate input file
    if not args.book_file.exists():
        print(f"Error: Book file not found: {args.book_file}", file=sys.stderr)
        sys.exit(1)

    if not args.book_file.is_file():
        print(f"Error: {args.book_file} is not a file", file=sys.stderr)
        sys.exit(1)

    # Validate custom pattern argument
    if args.pattern == 'custom' and not args.custom_regex:
        print("Error: --custom-regex is required when --pattern is 'custom'", file=sys.stderr)
        sys.exit(1)

    # Read book file
    if args.verbose:
        print(f"Reading book file: {args.book_file}")
        print(f"File size: {args.book_file.stat().st_size:,} bytes")

    try:
        book_text = args.book_file.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        # Try with different encoding
        try:
            book_text = args.book_file.read_text(encoding='latin-1')
            print("Warning: Used latin-1 encoding (UTF-8 failed)", file=sys.stderr)
        except Exception as e:
            print(f"Error reading file: {e}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"Error reading file: {e}", file=sys.stderr)
        sys.exit(1)

    if args.verbose:
        word_count = len(book_text.split())
        line_count = book_text.count('\n') + 1
        print(f"Total words: {word_count:,}")
        print(f"Total lines: {line_count:,}")
        print()

    # Split book into chapters
    if args.verbose:
        print(f"Detecting chapters using pattern: {args.pattern}")
        if args.custom_regex:
            print(f"Custom regex: {args.custom_regex}")

    try:
        chapters = split_book_into_chapters(
            book_text=book_text,
            pattern_type=args.pattern,
            custom_regex=args.custom_regex,
            min_chapter_size=args.min_size
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        print("\nTroubleshooting:", file=sys.stderr)
        print("  - Check that your book uses the expected chapter format", file=sys.stderr)
        print("  - Try a different --pattern option", file=sys.stderr)
        print("  - Use --pattern custom with --custom-regex for unusual formats", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print(f"\n[OK] Detected {len(chapters)} chapters")

    # Validate chapter sequence
    is_valid, warnings = validate_chapter_sequence(chapters)

    if warnings:
        print("\nWarnings:")
        for warning in warnings:
            print(f"  [!] {warning}")
        print()

    # Display chapter information
    if args.verbose:
        print("\nChapter Details:")
        print("-" * 80)
        for chapter in chapters:
            word_count = len(chapter.content.split())
            print(f"  {chapter.chapter_title:20} | {word_count:6,} words | Lines {chapter.start_line:4}-{chapter.end_line:4}")
        print("-" * 80)
        print()

    # Save chapters to files
    if args.dry_run:
        print("Dry run - no files created")
        print(f"\nWould create {len(chapters)} chapter files in: {args.output}")

        # Show what would be created
        for chapter in chapters:
            filename = f"{args.prefix}_{chapter.chapter_number:02d}.txt"
            filepath = args.output / filename
            print(f"  {filepath}")

    else:
        if args.verbose:
            print(f"Creating chapter files in: {args.output}")

        try:
            created_files = save_chapters_to_files(
                chapters=chapters,
                output_dir=str(args.output),
                filename_prefix=args.prefix,
                filename_suffix=".txt"
            )
        except Exception as e:
            print(f"Error creating chapter files: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
            sys.exit(1)

        print(f"\n[OK] Created {len(created_files)} chapter files")

        if args.verbose:
            print("\nCreated files:")
            for filepath in created_files:
                file_path = Path(filepath)
                size = file_path.stat().st_size
                print(f"  {filepath} ({size:,} bytes)")

    # Print summary
    print("\n" + "=" * 80)
    print("Split Complete")
    print("=" * 80)

    total_words = sum(len(c.content.split()) for c in chapters)
    print(f"Chapters: {len(chapters)}")
    print(f"Total words: {total_words:,}")
    print(f"Output directory: {args.output}")

    if not args.dry_run:
        print("\n" + "-" * 80)
        print("Next Steps:")
        print("-" * 80)
        print("1. Review the generated chapter files")
        print("2. Chunk each chapter:")
        print(f"   python chunk_chapter.py {args.output}/{args.prefix}_01.txt --chapter-id chapter_01")
        print("3. Generate workbooks:")
        print("   python generate_workbook.py chunks/chapter_01_*.json --output workbook_ch01.md")
        print("4. For subsequent chapters, include previous chapter context:")
        print("   python generate_workbook.py chunks/chapter_02_*.json \\")
        print("       --previous-chapter chapters/translated/chapter_01.txt \\")
        print("       --output workbook_ch02.md")
        print("=" * 80)


if __name__ == "__main__":
    main()
