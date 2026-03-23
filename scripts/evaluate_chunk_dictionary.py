#!/usr/bin/env python
"""
Simple script to evaluate a translation chunk using the dictionary evaluator.

Checks that all words are valid Spanish words, flags English words (errors)
and unknown words (warnings), and provides character positions for each.

Usage:
    python evaluate_chunk_dictionary.py translation.txt
    python evaluate_chunk_dictionary.py translation.txt --glossary glossary.json
    python evaluate_chunk_dictionary.py translation.txt --case-sensitive

Note: This evaluator only checks the translation text, not the source.
"""

import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import Chunk, ChunkMetadata, ChunkStatus, Glossary
from src.evaluators.dictionary_eval import DictionaryEvaluator


def load_text_file(file_path: Path) -> str:
    """Load text from a file."""
    try:
        return file_path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        # Try with different encoding if UTF-8 fails
        return file_path.read_text(encoding='latin-1')


def load_glossary(glossary_path: Path) -> Glossary:
    """Load glossary from JSON file."""
    with open(glossary_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return Glossary.model_validate(data)


def count_paragraphs(text: str) -> int:
    """Count paragraphs (double newline separated)."""
    return len([p for p in text.split('\n\n') if p.strip()])


def main():
    # Parse arguments
    if len(sys.argv) < 2:
        print("Usage: python evaluate_chunk_dictionary.py <translation.txt> [options]")
        print("\nExample:")
        print("  python evaluate_chunk_dictionary.py chapter1_es.txt")
        print("  python evaluate_chunk_dictionary.py chapter1_es.txt --glossary glossary.json")
        print("\nOptions:")
        print("  --glossary FILE      Path to glossary JSON file")
        print("  --case-sensitive     Enable case-sensitive word checking")
        sys.exit(1)

    translation_path = Path(sys.argv[1])

    # Parse options
    glossary = None
    case_sensitive = False

    if "--glossary" in sys.argv:
        glossary_idx = sys.argv.index("--glossary")
        if glossary_idx + 1 < len(sys.argv):
            glossary_path = Path(sys.argv[glossary_idx + 1])
            if glossary_path.exists():
                print(f">> Loading glossary from {glossary_path.name}...")
                glossary = load_glossary(glossary_path)
            else:
                print(f"[ERROR] Glossary file not found: {glossary_path}")
                sys.exit(1)

    if "--case-sensitive" in sys.argv:
        case_sensitive = True

    # Validate file exists
    if not translation_path.exists():
        print(f"[ERROR] Translation file not found: {translation_path}")
        sys.exit(1)

    # Load translation
    print(f"\n>> Loading translation file...")
    translated_text = load_text_file(translation_path)

    # Create chunk
    chunk = Chunk(
        id=f"{translation_path.stem}_chunk",
        chapter_id=translation_path.stem,
        position=1,
        source_text="Placeholder source text.",  # Not used for dictionary eval
        translated_text=translated_text,
        metadata=ChunkMetadata(
            char_start=0,
            char_end=len(translated_text),
            overlap_start=0,
            overlap_end=0,
            paragraph_count=count_paragraphs(translated_text),
            word_count=len(translated_text.split())
        ),
        status=ChunkStatus.TRANSLATED
    )

    # Configure evaluator
    context = {}
    if glossary:
        context["glossary"] = glossary
        print(f"   Glossary loaded: {len(glossary.terms)} terms")
    if case_sensitive:
        context["case_sensitive"] = True
        print("   Case-sensitive mode enabled")

    # Run evaluation
    print(f">> Evaluating translation dictionary...\n")
    evaluator = DictionaryEvaluator()
    result = evaluator.evaluate(chunk, context)

    # Display results
    print("=" * 70)
    print("DICTIONARY EVALUATION RESULTS")
    print("=" * 70)
    print(f"\nTranslation File: {translation_path.name}")
    print(f"Dictionaries: es_ES (Spain Spanish), es_MX (Mexican Spanish)")
    print(f"\nStatistics:")
    print(f"  Total words:      {result.metadata['total_words']:,}")
    print(f"  Unique words:     {result.metadata['unique_words']:,}")
    print(f"  Glossary words:   {result.metadata['glossary_words']:,}")
    print(f"  English words:    {result.metadata['english_words']:,} [ERROR]")
    print(f"  Unknown words:    {result.metadata['unknown_words']:,} [WARNING]")
    print(f"  Flagged instances: {result.metadata['flagged_instances']:,}")
    print(f"\nScore: {result.score:.2f} / 1.00")

    # Overall status
    print("\n" + "-" * 70)
    if result.passed:
        print("[PASSED] Dictionary check passed")
    else:
        print("[FAILED] Dictionary check found errors")
    print("-" * 70)

    # Issues
    if result.issues:
        print(f"\n{len(result.issues)} issue(s) found:\n")

        # Separate by severity
        errors = [i for i in result.issues if i.severity.value == "error"]
        warnings = [i for i in result.issues if i.severity.value == "warning"]
        infos = [i for i in result.issues if i.severity.value == "info"]

        # Show errors first
        if errors:
            print("[ERRORS] - English words in translation:")
            print("-" * 70)
            for i, issue in enumerate(errors, 1):
                print(f"{i}. {issue.message}")
                if issue.location:
                    print(f"   Location: {issue.location}")
                if issue.suggestion:
                    print(f"   Suggestion: {issue.suggestion}")
                print()

        # Then warnings
        if warnings:
            print("[WARNINGS] - Unknown words (not in Spanish or English dictionaries):")
            print("-" * 70)
            for i, issue in enumerate(warnings, 1):
                print(f"{i}. {issue.message}")
                if issue.location:
                    print(f"   Location: {issue.location}")
                if issue.suggestion:
                    print(f"   Suggestion: {issue.suggestion}")
                print()

        # Then infos
        if infos:
            print("[INFO] - Informational messages:")
            print("-" * 70)
            for i, issue in enumerate(infos, 1):
                print(f"{i}. {issue.message}")
                if issue.location:
                    print(f"   Location: {issue.location}")
                if issue.suggestion:
                    print(f"   Suggestion: {issue.suggestion}")
                print()
    else:
        print("\nNo issues found! All words are valid Spanish.\n")

    print("=" * 70)

    # Return exit code based on result
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
