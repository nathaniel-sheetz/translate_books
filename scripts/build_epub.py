#!/usr/bin/env python3
"""
CLI script for building an EPUB from translated chapter files.

Reads per-chapter .txt files, resolves [IMAGE:...] placeholders against
the project images/ directory, and produces a valid EPUB 3 file with
embedded images, table of contents, and basic styling.

Usage:
    python build_epub.py projects/fabre2 --title "Book Title" --author "Author Name"
    python build_epub.py projects/fabre2 --title "Book" --author "A" --language es --verbose

Examples:
    # Basic build with auto-detected cover
    python build_epub.py projects/fabre2 --title "The Story-Book of Science" --author "Jean-Henri Fabre"

    # Specify cover and output path
    python build_epub.py projects/fabre2 --title "Book" --author "Author" \\
        --cover images/cover.jpg --output output/book.epub

    # Use a different chapters directory
    python build_epub.py projects/fabre2 --title "Book" --author "Author" \\
        --chapters-dir projects/fabre2/chapters/translated
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.epub_builder import build_epub, collect_referenced_images


def main():
    parser = argparse.ArgumentParser(
        description='Build an EPUB from translated chapter files with embedded images.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s projects/fabre2 --title "The Story-Book of Science" --author "Jean-Henri Fabre"
  %(prog)s projects/fabre2 --title "Book" --author "Author" --cover images/cover.jpg
  %(prog)s projects/fabre2 --title "Book" --author "Author" --chapters-dir chapters/translated
        """
    )

    parser.add_argument(
        'project_dir',
        type=Path,
        help='Path to the project directory (contains images/ and chapters/)'
    )
    parser.add_argument(
        '--title',
        required=True,
        help='Book title for EPUB metadata'
    )
    parser.add_argument(
        '--author',
        required=True,
        help='Author name for EPUB metadata'
    )
    parser.add_argument(
        '--language',
        default='es',
        help='EPUB language code (default: es)'
    )
    parser.add_argument(
        '--cover',
        type=Path,
        default=None,
        help='Cover image path (relative to project dir). Auto-detects images/cover.jpg if omitted.'
    )
    parser.add_argument(
        '--output',
        type=Path,
        default=None,
        help='Output EPUB path. Defaults to {project_dir}/{name}.epub'
    )
    parser.add_argument(
        '--chapters-dir',
        type=Path,
        default=None,
        help='Chapter files directory. Defaults to {project_dir}/chapters/'
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Show detailed build information'
    )

    args = parser.parse_args()

    # Set up logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(message)s',
        stream=sys.stderr,
    )

    project_dir = args.project_dir.resolve()
    if not project_dir.is_dir():
        print(f"Error: Project directory not found: {project_dir}", file=sys.stderr)
        sys.exit(1)

    chapters_dir = args.chapters_dir
    if chapters_dir and not chapters_dir.is_absolute():
        chapters_dir = project_dir / chapters_dir

    try:
        output = build_epub(
            project_path=project_dir,
            title=args.title,
            author=args.author,
            language=args.language,
            cover_image=args.cover,
            output_path=args.output,
            chapters_dir=chapters_dir,
        )
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error building EPUB: {e}", file=sys.stderr)
        sys.exit(1)

    # Print summary
    size_kb = output.stat().st_size / 1024
    print(f"\nEPUB built successfully: {output}")
    print(f"  Size: {size_kb:.1f} KB")


if __name__ == '__main__':
    main()
