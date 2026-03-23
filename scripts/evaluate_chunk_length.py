#!/usr/bin/env python
"""
Simple script to evaluate a translation chunk using the length evaluator.

Usage:
    python evaluate_chunk.py source.txt translation.txt
    python evaluate_chunk.py source.txt translation.txt --by-chars
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import Chunk, ChunkMetadata, ChunkStatus
from src.evaluators.length_eval import LengthEvaluator


def load_text_file(file_path: Path) -> str:
    """Load text from a file."""
    try:
        return file_path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        # Try with different encoding if UTF-8 fails
        return file_path.read_text(encoding='latin-1')


def count_paragraphs(text: str) -> int:
    """Count paragraphs (double newline separated)."""
    return len([p for p in text.split('\n\n') if p.strip()])


def main():
    # Parse arguments
    if len(sys.argv) < 3:
        print("Usage: python evaluate_chunk.py <source.txt> <translation.txt> [--by-chars]")
        print("\nExample:")
        print("  python evaluate_chunk.py chapter1_en.txt chapter1_es.txt")
        print("\nOptions:")
        print("  --by-chars    Count by characters instead of words")
        sys.exit(1)

    source_path = Path(sys.argv[1])
    translation_path = Path(sys.argv[2])
    count_by = "chars" if "--by-chars" in sys.argv else "words"

    # Validate files exist
    if not source_path.exists():
        print(f"[ERROR] Source file not found: {source_path}")
        sys.exit(1)

    if not translation_path.exists():
        print(f"[ERROR] Translation file not found: {translation_path}")
        sys.exit(1)

    # Load texts
    print(f"\n>> Loading files...")
    source_text = load_text_file(source_path)
    translated_text = load_text_file(translation_path)

    # Create chunk
    chunk = Chunk(
        id=f"{source_path.stem}_chunk",
        chapter_id=source_path.stem,
        position=1,
        source_text=source_text,
        translated_text=translated_text,
        metadata=ChunkMetadata(
            char_start=0,
            char_end=len(source_text),
            overlap_start=0,
            overlap_end=0,
            paragraph_count=count_paragraphs(source_text),
            word_count=len(source_text.split())
        ),
        status=ChunkStatus.TRANSLATED
    )

    # Configure evaluator
    context = {}
    if count_by == "chars":
        context = {"length_config": {"count_by": "chars"}}

    # Run evaluation
    print(f">> Evaluating translation...\n")
    evaluator = LengthEvaluator()
    result = evaluator.evaluate(chunk, context)

    # Display results
    print("=" * 70)
    print("EVALUATION RESULTS")
    print("=" * 70)
    print(f"\nSource File:      {source_path.name}")
    print(f"Translation File: {translation_path.name}")
    print(f"\nCounting by: {result.metadata['unit']}")
    print(f"  Source:      {result.metadata['source_count']:,} {result.metadata['unit']}")
    print(f"  Translation: {result.metadata['target_count']:,} {result.metadata['unit']}")
    print(f"  Ratio:       {result.metadata['ratio']:.2f}x")
    print(f"\nScore: {result.score:.2f} / 1.00")

    # Overall status
    print("\n" + "-" * 70)
    if result.passed:
        print("[PASSED] Translation length is acceptable")
    else:
        print("[FAILED] Translation has length issues")
    print("-" * 70)

    # Issues
    if result.issues:
        print(f"\n{len(result.issues)} issue(s) found:\n")
        for i, issue in enumerate(result.issues, 1):
            severity_icon = {
                "error": "[ERROR]",
                "warning": "[WARNING]",
                "info": "[INFO]"
            }.get(issue.severity.value, "[-]")

            print(f"{i}. {severity_icon} {issue.severity.upper()}")
            print(f"   {issue.message}")
            if issue.location:
                print(f"   Location: {issue.location}")
            if issue.suggestion:
                print(f"   Suggestion: {issue.suggestion}")
            print()
    else:
        print("\nNo issues found! Translation length looks good.\n")

    # Thresholds info
    thresholds = result.metadata['thresholds']
    print("\nThreshold Configuration:")
    print(f"  Expected range: {thresholds['expected_min']:.1f}x - {thresholds['expected_max']:.1f}x")
    print(f"  Acceptable range: {thresholds['min_ratio']:.1f}x - {thresholds['max_ratio']:.1f}x")

    print("\n" + "=" * 70)

    # Return exit code based on result
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
