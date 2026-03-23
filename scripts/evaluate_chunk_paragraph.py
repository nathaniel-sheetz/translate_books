#!/usr/bin/env python
"""
Simple script to evaluate a translation chunk using the paragraph evaluator.

Usage:
    python evaluate_chunk_paragraph.py source.txt translation.txt
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import Chunk, ChunkMetadata, ChunkStatus
from src.evaluators.paragraph_eval import ParagraphEvaluator


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
        print("Usage: python evaluate_chunk_paragraph.py <source.txt> <translation.txt>")
        print("\nExample:")
        print("  python evaluate_chunk_paragraph.py chapter1_en.txt chapter1_es.txt")
        sys.exit(1)

    source_path = Path(sys.argv[1])
    translation_path = Path(sys.argv[2])

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

    # Run evaluation
    print(f">> Evaluating paragraph structure...\n")
    evaluator = ParagraphEvaluator()
    result = evaluator.evaluate(chunk, {})

    # Display results
    print("=" * 70)
    print("PARAGRAPH EVALUATION RESULTS")
    print("=" * 70)
    print(f"\nSource File:      {source_path.name}")
    print(f"Translation File: {translation_path.name}")
    print(f"\nParagraph Count:")
    print(f"  Source:      {result.metadata['source_paragraphs']} paragraph(s)")
    print(f"  Translation: {result.metadata['translation_paragraphs']} paragraph(s)")

    if result.metadata['match']:
        print(f"  Match:       Yes (perfect match)")
    else:
        difference = result.metadata['difference']
        print(f"  Match:       No (difference: {difference})")

    print(f"\nScore: {result.score:.2f} / 1.00")

    # Overall status
    print("\n" + "-" * 70)
    if result.passed:
        print("[PASSED] Paragraph structure is preserved")
    else:
        print("[FAILED] Paragraph structure has issues")
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
        print("\nNo issues found! Paragraph structure is preserved.\n")

    # Additional info
    print("\nWhat this checks:")
    print("  - Paragraph count matches between source and translation")
    print("  - No merged paragraphs (fewer in translation)")
    print("  - No split paragraphs (more in translation)")
    print("  - Handles different newline styles (Windows/Unix)")

    print("\n" + "=" * 70)

    # Return exit code based on result
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
