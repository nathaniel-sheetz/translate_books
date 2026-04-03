#!/usr/bin/env python3
"""
Generate a translation workbook for manual translation workflow.

This script creates a formatted workbook containing complete prompts for each
chunk, ready to copy/paste into any LLM interface (Claude.ai, ChatGPT, etc.).

Usage:
    python generate_workbook.py chunks/*.json --output workbook.md
    python generate_workbook.py chunks/chapter_01_*.json --glossary glossary.json
    python generate_workbook.py chunks/*.json --glossary glossary.json --style-guide style.json
"""

import argparse
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import Chunk
from src.translator import generate_workbook, save_workbook
from src.utils.file_io import load_chunk, load_glossary, load_style_guide


def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Generate translation workbook for manual translation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage - generate workbook from chunks
  python generate_workbook.py chunks/chapter_01_*.json --output workbook.md

  # With glossary
  python generate_workbook.py chunks/*.json \\
      --glossary glossary.json \\
      --output workbook.md

  # With glossary and style guide
  python generate_workbook.py chunks/*.json \\
      --glossary glossary.json \\
      --style-guide style_guide.json \\
      --output workbook.md

  # With all options
  python generate_workbook.py chunks/chapter_01_*.json \\
      --project "Pride and Prejudice" \\
      --glossary glossary.json \\
      --style-guide style_guide.json \\
      --source-lang English \\
      --target-lang Spanish \\
      --context "19th century novel about manners" \\
      --output workbook.md

  # With previous chapter context (for Chapter 2+)
  python generate_workbook.py chunks/chapter_02_*.json \\
      --glossary glossary.json \\
      --previous-chapter chapters/translated/chapter_01.txt \\
      --context-paragraphs 2 \\
      --output workbook_ch02.md

The generated workbook includes:
  - Complete translation prompts for each chunk
  - Glossary reference (if provided)
  - Style guide reference (if provided)
  - Clear instructions for manual translation
  - Sections to paste translations into

After translation, import using:
  python import_workbook.py workbook.md --output chunks/translated/
        """
    )

    # Required arguments
    parser.add_argument(
        'chunk_files',
        nargs='+',
        help='Chunk JSON files (supports glob patterns like chunks/*.json)',
    )

    parser.add_argument(
        '--output', '-o',
        type=Path,
        required=True,
        help='Output path for workbook file (e.g., workbook.md)',
    )

    # Optional arguments
    parser.add_argument(
        '--glossary', '-g',
        type=Path,
        help='Path to glossary JSON file',
    )

    parser.add_argument(
        '--style-guide', '-s',
        type=Path,
        help='Path to style guide JSON file',
    )

    parser.add_argument(
        '--project', '-p',
        default='Translation Project',
        help='Project name (default: "Translation Project")',
    )

    parser.add_argument(
        '--source-lang',
        default='English',
        help='Source language (default: English)',
    )

    parser.add_argument(
        '--target-lang',
        default='Spanish',
        help='Target language (default: Spanish)',
    )

    parser.add_argument(
        '--context', '-c',
        default='',
        help='Book context (genre, time period, style notes)',
    )

    parser.add_argument(
        '--previous-chapter',
        type=Path,
        help='Path to previous chapter source text (for continuity context)',
    )

    parser.add_argument(
        '--previous-chapter-translated',
        type=Path,
        help='Path to previous chapter translated text (for continuity context)',
    )

    parser.add_argument(
        '--context-language',
        choices=['both', 'source', 'translation'],
        default='both',
        help='What previous context to include: both, source, or translation (default: both)',
    )

    parser.add_argument(
        '--context-paragraphs',
        type=int,
        default=2,
        help='Number of paragraphs from previous chapter to include (default: 2)',
    )

    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show detailed progress information',
    )

    return parser.parse_args()


def expand_glob_patterns(file_patterns):
    """Expand glob patterns to actual file paths."""
    files = []
    for pattern in file_patterns:
        # Expand glob pattern
        matches = glob.glob(pattern)
        if not matches:
            # No matches - might be a literal filename
            if Path(pattern).exists():
                files.append(Path(pattern))
            else:
                print(f"Warning: No files match pattern: {pattern}", file=sys.stderr)
        else:
            files.extend(Path(f) for f in matches)

    return sorted(set(files))  # Remove duplicates and sort


def load_chunks(chunk_paths, verbose=False):
    """Load all chunks from file paths."""
    chunks = []

    for path in chunk_paths:
        if verbose:
            print(f"Loading: {path}")

        try:
            chunk = load_chunk(path)
            chunks.append(chunk)
        except Exception as e:
            print(f"Error loading {path}: {e}", file=sys.stderr)
            sys.exit(1)

    # Sort by position to ensure correct order
    chunks.sort(key=lambda c: c.position)

    return chunks


def main():
    """Main entry point."""
    args = parse_arguments()

    # Expand glob patterns to get actual file paths
    chunk_paths = expand_glob_patterns(args.chunk_files)

    if not chunk_paths:
        print("Error: No chunk files found", file=sys.stderr)
        print("", file=sys.stderr)
        print("Make sure your chunk files exist and the pattern is correct.", file=sys.stderr)
        print("Example: chunks/chapter_01_*.json", file=sys.stderr)
        sys.exit(1)

    if args.verbose:
        print(f"Found {len(chunk_paths)} chunk file(s)")

    # Load chunks
    if args.verbose:
        print("\nLoading chunks...")

    chunks = load_chunks(chunk_paths, verbose=args.verbose)

    if not chunks:
        print("Error: No chunks loaded", file=sys.stderr)
        sys.exit(1)

    # Load glossary if provided
    glossary = None
    if args.glossary:
        if not args.glossary.exists():
            print(f"Error: Glossary file not found: {args.glossary}", file=sys.stderr)
            sys.exit(1)

        if args.verbose:
            print(f"\nLoading glossary: {args.glossary}")

        try:
            glossary = load_glossary(args.glossary)
            if args.verbose:
                print(f"  Loaded {len(glossary.terms)} glossary terms")
        except Exception as e:
            print(f"Error loading glossary: {e}", file=sys.stderr)
            sys.exit(1)

    # Load style guide if provided
    style_guide = None
    if args.style_guide:
        if not args.style_guide.exists():
            print(f"Error: Style guide file not found: {args.style_guide}", file=sys.stderr)
            sys.exit(1)

        if args.verbose:
            print(f"\nLoading style guide: {args.style_guide}")

        try:
            style_guide = load_style_guide(args.style_guide)
            if args.verbose:
                print(f"  Version: {style_guide.version}")
        except Exception as e:
            print(f"Error loading style guide: {e}", file=sys.stderr)
            sys.exit(1)

    # Load previous chapter source if provided
    previous_chapter_source = None
    if args.previous_chapter:
        if not args.previous_chapter.exists():
            print(f"Error: Previous chapter file not found: {args.previous_chapter}", file=sys.stderr)
            sys.exit(1)

        if args.verbose:
            print(f"\nLoading previous chapter source: {args.previous_chapter}")

        try:
            previous_chapter_source = args.previous_chapter.read_text(encoding='utf-8')
            if args.verbose:
                word_count = len(previous_chapter_source.split())
                print(f"  Word count: {word_count:,}")
                print(f"  Including last {args.context_paragraphs} paragraphs for context")
        except Exception as e:
            print(f"Error loading previous chapter: {e}", file=sys.stderr)
            sys.exit(1)

    # Load previous chapter translation if provided
    previous_chapter_translated = None
    if args.previous_chapter_translated:
        if not args.previous_chapter_translated.exists():
            print(f"Error: Previous chapter translation not found: {args.previous_chapter_translated}", file=sys.stderr)
            sys.exit(1)

        if args.verbose:
            print(f"Loading previous chapter translation: {args.previous_chapter_translated}")

        try:
            previous_chapter_translated = args.previous_chapter_translated.read_text(encoding='utf-8')
            if args.verbose:
                word_count = len(previous_chapter_translated.split())
                print(f"  Word count: {word_count:,}")
        except Exception as e:
            print(f"Error loading previous chapter translation: {e}", file=sys.stderr)
            sys.exit(1)

    # Generate workbook
    if args.verbose:
        print(f"\nGenerating workbook for {len(chunks)} chunks...")
        if previous_chapter_source or previous_chapter_translated:
            print(f"  Including previous chapter context (mode: {args.context_language})")

    try:
        workbook = generate_workbook(
            chunks=chunks,
            glossary=glossary,
            style_guide=style_guide,
            project_name=args.project,
            source_language=args.source_lang,
            target_language=args.target_lang,
            book_context=args.context,
            previous_chapter_source=previous_chapter_source,
            previous_chapter_translated=previous_chapter_translated,
            context_paragraphs=args.context_paragraphs,
            context_language=args.context_language,
        )
    except Exception as e:
        print(f"Error generating workbook: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # Save workbook
    if args.verbose:
        print(f"\nSaving workbook to: {args.output}")

    try:
        save_workbook(workbook, args.output)
    except Exception as e:
        print(f"Error saving workbook: {e}", file=sys.stderr)
        sys.exit(1)

    # Print summary
    print("\n" + "=" * 70)
    print("[OK] Workbook Generated Successfully")
    print("=" * 70)
    print(f"\nProject: {args.project}")
    print(f"Language: {args.source_lang} -> {args.target_lang}")
    print(f"Chunks: {len(chunks)}")

    if glossary:
        print(f"Glossary: {len(glossary.terms)} terms (v{glossary.version})")

    if style_guide:
        print(f"Style Guide: v{style_guide.version}")

    if previous_chapter_source or previous_chapter_translated:
        print(f"Previous Chapter Context: {args.context_paragraphs} paragraphs ({args.context_language})")

    print(f"\nOutput: {args.output}")
    print(f"Size: {args.output.stat().st_size:,} bytes")

    # Calculate total words
    total_words = sum(c.metadata.word_count for c in chunks)
    print(f"\nTotal words to translate: {total_words:,}")

    print("\n" + "-" * 70)
    print("Next Steps:")
    print("-" * 70)
    print("1. Open the workbook in a text editor")
    print("2. For each chunk:")
    print("   - Copy the PROMPT section")
    print("   - Paste into Claude.ai, ChatGPT, or your LLM")
    print("   - Copy the translation response")
    print("   - Paste into the TRANSLATION section")
    print("3. Save the completed workbook")
    print("4. Import translations:")
    print(f"   python import_workbook.py {args.output} --output chunks/translated/")
    print("=" * 70)


if __name__ == "__main__":
    main()
