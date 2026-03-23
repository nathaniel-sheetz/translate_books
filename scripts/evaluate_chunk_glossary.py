#!/usr/bin/env python3
"""
Standalone script to evaluate glossary compliance for a translated chunk.

Usage:
    python evaluate_chunk_glossary.py chunk.json --glossary glossary.json

Example:
    python evaluate_chunk_glossary.py tests/fixtures/chunk_translated_good.json \
        --glossary tests/fixtures/glossary_sample.json
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import Chunk, Glossary
from src.evaluators.glossary_eval import GlossaryEvaluator


def main():
    if len(sys.argv) < 2:
        print("Usage: python evaluate_chunk_glossary.py <chunk_file> [--glossary <glossary_file>]")
        print("\nExample:")
        print("  python evaluate_chunk_glossary.py chunk.json --glossary glossary.json")
        sys.exit(1)

    chunk_path = Path(sys.argv[1])
    glossary_path = None

    # Parse --glossary option
    if "--glossary" in sys.argv:
        try:
            glossary_idx = sys.argv.index("--glossary")
            glossary_path = Path(sys.argv[glossary_idx + 1])
        except (IndexError, ValueError):
            print("Error: --glossary requires a file path")
            sys.exit(1)

    # Load chunk
    try:
        with open(chunk_path, 'r', encoding='utf-8') as f:
            chunk_data = json.load(f)
            chunk = Chunk(**chunk_data)
    except FileNotFoundError:
        print(f"Error: Chunk file not found: {chunk_path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in chunk file: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error loading chunk: {e}")
        sys.exit(1)

    # Load glossary if provided
    glossary = None
    if glossary_path:
        try:
            with open(glossary_path, 'r', encoding='utf-8') as f:
                glossary_data = json.load(f)
                glossary = Glossary(**glossary_data)
        except FileNotFoundError:
            print(f"Error: Glossary file not found: {glossary_path}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in glossary file: {e}")
            sys.exit(1)
        except Exception as e:
            print(f"Error loading glossary: {e}")
            sys.exit(1)

    # Run evaluation
    evaluator = GlossaryEvaluator()
    context = {"glossary": glossary} if glossary else {}
    result = evaluator.evaluate(chunk, context)

    # Display results
    print("=" * 70)
    print("GLOSSARY COMPLIANCE EVALUATION")
    print("=" * 70)
    print(f"Chunk ID: {chunk.id}")
    print(f"Glossary: {'Provided' if glossary else 'Not provided'}")
    if glossary:
        print(f"  Terms in glossary: {len(glossary.terms)}")
    print()

    # Status
    status_symbol = "[PASS]" if result.passed else "[FAIL]"
    print(f"Status: {status_symbol}")
    print(f"Score: {result.score:.2f}")
    print()

    # Metadata
    print("Statistics:")
    print(f"  Glossary terms checked: {result.metadata.get('glossary_terms_total', 0)}")
    print(f"  Terms found in source: {result.metadata.get('glossary_terms_in_source', 0)}")
    print(f"  Terms correct: {result.metadata.get('glossary_terms_correct', 0)}")
    print(f"  Consistency warnings: {result.metadata.get('consistency_warnings', 0)}")
    print()

    # Issues
    if result.issues:
        print(f"Issues Found: {len(result.issues)}")
        print(f"  Errors: {result.error_count}")
        print(f"  Warnings: {result.warning_count}")
        print(f"  Info: {result.info_count}")
        print()

        print("Details:")
        print("-" * 70)
        for i, issue in enumerate(result.issues, 1):
            severity_symbol = {
                "error": "[ERROR]",
                "warning": "[WARNING]",
                "info": "[INFO]"
            }.get(issue.severity.value, "[*]")

            print(f"\n{i}. {severity_symbol}")
            print(f"   {issue.message}")

            if issue.location:
                print(f"   Location: {issue.location}")

            if issue.suggestion:
                print(f"   Suggestion: {issue.suggestion}")

        print()
    else:
        print("No issues found - glossary compliance perfect!")
        print()

    print("=" * 70)

    # Exit with appropriate code
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
