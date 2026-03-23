#!/usr/bin/env python3
"""
CLI script for chunking chapters into translation-sized chunks.

This script divides a chapter text file into chunks with intelligent overlap
using a dual-constraint strategy (paragraph count + word count).

Usage:
    python chunk_chapter.py chapter.txt --chapter-id chapter_01
    python chunk_chapter.py chapter.txt --config project_config.json
    python chunk_chapter.py chapter.txt --target-size 1500 --overlap 2

Examples:
    # Basic usage with default config
    python chunk_chapter.py chapters/chapter_01.txt --chapter-id chapter_01

    # Use project config
    python chunk_chapter.py chapters/chapter_01.txt --config projects/my_book/config.json

    # Override chunking parameters
    python chunk_chapter.py chapters/chapter_01.txt --target-size 1500 --overlap 2 --min-overlap-words 150

    # Save to specific directory
    python chunk_chapter.py chapters/chapter_01.txt --output chunks/original/

    # Show detailed statistics
    python chunk_chapter.py chapters/chapter_01.txt --verbose
"""

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.chunker import chunk_chapter
from src.models import ChunkingConfig, ProjectConfig
from src.utils.file_io import save_chunk
from src.utils.text_utils import count_words, count_paragraphs


def load_config_from_file(config_path: Path) -> ChunkingConfig:
    """Load ChunkingConfig from project config file."""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)

        # Parse project config and extract chunking section
        project_config = ProjectConfig(**config_data)
        return project_config.chunking
    except FileNotFoundError:
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in config file: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error loading config: {e}")
        sys.exit(1)


def derive_chapter_id(chapter_file: Path) -> str:
    """Derive chapter ID from filename."""
    # Remove extension and use stem as chapter ID
    # E.g., "chapter_01.txt" -> "chapter_01"
    return chapter_file.stem


def print_chunk_summary(chunks, chapter_file, config, verbose=False):
    """Print summary of chunking results."""
    total_words = sum(chunk.metadata.word_count for chunk in chunks)
    total_paras = sum(chunk.metadata.paragraph_count for chunk in chunks)

    print("\n" + "=" * 70)
    print("CHUNKING COMPLETE!")
    print("=" * 70)
    print(f"\nChapter: {chapter_file.name}")
    print(f"  Total words: {total_words:,}")
    print(f"  Total paragraphs: {total_paras}")
    print(f"\nCreated {len(chunks)} chunk(s):")

    for chunk in chunks:
        overlap_info = ""
        if chunk.metadata.overlap_start > 0 or chunk.metadata.overlap_end > 0:
            # Calculate overlap in paragraphs (approximate)
            overlap_para_count = 0
            if chunk.metadata.overlap_start > 0:
                # Count paragraphs in overlap (rough estimate)
                overlap_para_count += chunk.source_text[:chunk.metadata.overlap_start].count('\n\n') + 1

            overlap_word_count = count_words(chunk.source_text[:chunk.metadata.overlap_end]) if chunk.metadata.overlap_end > 0 else 0

            if chunk.metadata.overlap_end > 0:
                overlap_info = f" (overlap: ~{overlap_para_count} para, {overlap_word_count} words)"

        print(f"  - {chunk.id}: {chunk.metadata.word_count:,} words, "
              f"{chunk.metadata.paragraph_count} paragraphs{overlap_info}")

    if verbose:
        print("\nConfiguration used:")
        print(f"  Target size: {config.target_size} words")
        print(f"  Overlap paragraphs: {config.overlap_paragraphs}")
        print(f"  Min overlap words: {config.min_overlap_words}")
        print(f"  Min chunk size: {config.min_chunk_size} words")
        print(f"  Max chunk size: {config.max_chunk_size} words")

    print()


def main():
    parser = argparse.ArgumentParser(
        description='Chunk a chapter into translation-sized pieces with intelligent overlap.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage with default config
  %(prog)s chapter_01.txt --chapter-id chapter_01

  # Use project config
  %(prog)s chapter_01.txt --config projects/my_book/config.json

  # Override chunking parameters
  %(prog)s chapter_01.txt --target-size 1500 --overlap 2 --min-overlap-words 150

  # Save to specific directory with verbose output
  %(prog)s chapter_01.txt --output chunks/original/ --verbose
        """
    )

    # Required arguments
    parser.add_argument(
        'chapter_file',
        type=Path,
        help='Path to chapter text file'
    )

    # Optional arguments
    parser.add_argument(
        '--chapter-id',
        type=str,
        help='Chapter identifier (default: derived from filename)'
    )

    parser.add_argument(
        '--config',
        type=Path,
        help='Path to project config.json (uses chunking settings)'
    )

    parser.add_argument(
        '--output',
        type=Path,
        default=Path('chunks'),
        help='Output directory for chunk JSON files (default: ./chunks/)'
    )

    # Chunking configuration overrides
    parser.add_argument(
        '--target-size',
        type=int,
        help='Target words per chunk (overrides config, default: 2000)'
    )

    parser.add_argument(
        '--overlap',
        type=int,
        dest='overlap_paragraphs',
        help='Minimum paragraphs of overlap (overrides config, default: 2)'
    )

    parser.add_argument(
        '--min-overlap-words',
        type=int,
        help='Minimum words in overlap (overrides config, default: 100)'
    )

    parser.add_argument(
        '--min-chunk-size',
        type=int,
        help='Minimum words per chunk (overrides config, default: 500)'
    )

    parser.add_argument(
        '--max-chunk-size',
        type=int,
        help='Maximum words per chunk (overrides config, default: 3000)'
    )

    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Show detailed chunking statistics'
    )

    args = parser.parse_args()

    # Validate chapter file exists
    if not args.chapter_file.exists():
        print(f"Error: Chapter file not found: {args.chapter_file}")
        sys.exit(1)

    # Load chapter text
    try:
        chapter_text = args.chapter_file.read_text(encoding='utf-8')
    except Exception as e:
        print(f"Error reading chapter file: {e}")
        sys.exit(1)

    if not chapter_text.strip():
        print("Error: Chapter file is empty")
        sys.exit(1)

    # Determine chapter ID
    chapter_id = args.chapter_id or derive_chapter_id(args.chapter_file)

    # Load or create chunking config
    if args.config:
        config = load_config_from_file(args.config)
    else:
        config = ChunkingConfig()

    # Apply command-line overrides
    if args.target_size is not None:
        config.target_size = args.target_size
    if args.overlap_paragraphs is not None:
        config.overlap_paragraphs = args.overlap_paragraphs
    if args.min_overlap_words is not None:
        config.min_overlap_words = args.min_overlap_words
    if args.min_chunk_size is not None:
        config.min_chunk_size = args.min_chunk_size
    if args.max_chunk_size is not None:
        config.max_chunk_size = args.max_chunk_size

    # Show chapter info
    print(f"\nChunking: {args.chapter_file.name}")
    print(f"Chapter ID: {chapter_id}")
    word_count = count_words(chapter_text)
    para_count = count_paragraphs(chapter_text)
    print(f"Input: {word_count:,} words, {para_count} paragraphs")

    # Perform chunking
    try:
        chunks = chunk_chapter(chapter_text, config, chapter_id)
    except Exception as e:
        print(f"\nError during chunking: {e}")
        sys.exit(1)

    if not chunks:
        print("\nWarning: No chunks created (chapter may be empty)")
        sys.exit(0)

    # Create output directory
    args.output.mkdir(parents=True, exist_ok=True)

    # Save chunks
    print(f"\nSaving chunks to: {args.output}/")
    for chunk in chunks:
        output_file = args.output / f"{chunk.id}.json"
        try:
            save_chunk(chunk, output_file)
        except Exception as e:
            print(f"Error saving chunk {chunk.id}: {e}")
            sys.exit(1)

    # Print summary
    print_chunk_summary(chunks, args.chapter_file, config, args.verbose)
    print(f"Output directory: {args.output.absolute()}")
    print()


if __name__ == '__main__':
    main()
