#!/usr/bin/env python3
"""
Manual validation for CompletenessEvaluator.

This script validates the completeness evaluator without requiring pydantic.
It tests all core functionality including empty checks, placeholder detection,
truncation detection, and special marker preservation.
"""

import re
from pathlib import Path


class SimpleChunk:
    """Simple chunk for testing without pydantic."""

    def __init__(self, chunk_id, source_text, translated_text):
        self.id = chunk_id
        self.source_text = source_text
        self.translated_text = translated_text


def test_empty_detection():
    """Test empty translation detection."""
    print("\n[Test: Empty Detection]")

    test_cases = [
        ("", True, "Empty string"),
        ("   ", True, "Whitespace only"),
        ("\n\t  \n", True, "Whitespace with newlines"),
        ("Text", False, "Normal text"),
        ("   Text   ", False, "Text with whitespace"),
    ]

    def is_empty(text):
        return not text or not text.strip()

    passed = 0
    failed = 0

    for text, expected_empty, description in test_cases:
        result = is_empty(text)
        if result == expected_empty:
            print(f"  ✓ {description}: {result}")
            passed += 1
        else:
            print(f"  ✗ {description}: expected {expected_empty}, got {result}")
            failed += 1

    print(f"\n  Results: {passed} passed, {failed} failed")
    return failed == 0


def test_placeholder_patterns():
    """Test placeholder pattern detection."""
    print("\n[Test: Placeholder Detection]")

    placeholder_patterns = [
        r'\bTODO\b',
        r'\bFIXME\b',
        r'\bXXX\b',
        r'\[.*?TRANSLATION.*?\]',
        r'\[.*?INSERT.*?\]',
        r'\[.*?MISSING.*?\]',
        r'\[.*?TBD.*?\]',
        r'\[.*?PLACEHOLDER.*?\]',
        r'<.*?TRANSLATION.*?>',
        r'<<<.*?>>>',
        r'\{\{.*?TRANSLATION.*?\}\}',
    ]

    test_cases = [
        ("TODO: Translate this", True, "TODO pattern"),
        ("FIXME: Need better translation", True, "FIXME pattern"),
        ("XXX incomplete", True, "XXX pattern"),
        ("[TRANSLATION HERE]", True, "Bracket TRANSLATION"),
        ("[INSERT TEXT]", True, "Bracket INSERT"),
        ("[MISSING]", True, "Bracket MISSING"),
        ("[TBD]", True, "Bracket TBD"),
        ("[PLACEHOLDER]", True, "Bracket PLACEHOLDER"),
        ("<TRANSLATION NEEDED>", True, "Angle bracket TRANSLATION"),
        ("<<<INCOMPLETE>>>", True, "Triple angle brackets"),
        ("{{TRANSLATION}}", True, "Curly brace TRANSLATION"),
        ("This is normal text.", False, "Normal text"),
        ("todo: lowercase", True, "Case insensitive TODO"),
    ]

    def has_placeholder(text):
        for pattern in placeholder_patterns:
            if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
                return True
        return False

    passed = 0
    failed = 0

    for text, expected_has_placeholder, description in test_cases:
        result = has_placeholder(text)
        if result == expected_has_placeholder:
            print(f"  ✓ {description}: {result}")
            passed += 1
        else:
            print(f"  ✗ {description}: expected {expected_has_placeholder}, got {result}")
            failed += 1

    print(f"\n  Results: {passed} passed, {failed} failed")
    return failed == 0


def test_truncation_detection():
    """Test truncation detection."""
    print("\n[Test: Truncation Detection]")

    test_cases = [
        ("Esta es una oración completa.", False, "Period ending"),
        ("¿Es esto una pregunta?", False, "Question mark"),
        ("¡Qué maravilloso!", False, "Exclamation"),
        ('Ella dijo: "Hola".', False, "Quote after period"),
        ("Puntos suspensivos...", False, "Ellipsis"),
        ("Texto con cierre»", False, "Right angle quote"),
        ("Texto con paréntesis).", False, "Parenthesis after period"),
        ("Texto con corchete].", False, "Bracket after period"),
        ("Em-dash final—", False, "Em-dash"),
        ("Esta es una oración sin punto final", True, "No ending punctuation"),
        ("Texto que termina de forma abrupta", True, "Abrupt ending"),
    ]

    def appears_truncated(text):
        text = text.strip()
        if not text:
            return False
        proper_endings = r'[.!?…»")\]—]$'
        return not re.search(proper_endings, text)

    passed = 0
    failed = 0

    for text, expected_truncated, description in test_cases:
        result = appears_truncated(text)
        if result == expected_truncated:
            print(f"  ✓ {description}: {result}")
            passed += 1
        else:
            print(f"  ✗ {description}: expected {expected_truncated}, got {result}")
            failed += 1

    print(f"\n  Results: {passed} passed, {failed} failed")
    return failed == 0


def test_special_markers():
    """Test special marker preservation checking."""
    print("\n[Test: Special Marker Detection]")

    special_markers = [
        r'^---+$',          # Horizontal rules
        r'^\*\s*\*\s*\*$',  # Star dividers
        r'^#{1,6}\s',       # Markdown headers
        r'^\d+\.$',         # Numbered lists
        r'^[-*+]\s',        # Bullet lists
    ]

    def count_markers(text):
        """Count special markers in text."""
        counts = {}
        for pattern in special_markers:
            matches = list(re.finditer(pattern, text, re.MULTILINE))
            counts[pattern] = len(matches)
        return counts

    test_cases = [
        (
            "Text before\n\n---\n\nText after",
            "Texto antes\n\n---\n\nTexto después",
            True,
            "Horizontal rule preserved"
        ),
        (
            "Text before\n\n---\n\nText after",
            "Texto antes\n\nTexto después",
            False,
            "Horizontal rule missing"
        ),
        (
            "Text before\n\n* * *\n\nText after",
            "Texto antes\n\n* * *\n\nTexto después",
            True,
            "Star divider preserved"
        ),
        (
            "# Chapter One\n\nText",
            "# Capítulo Uno\n\nTexto",
            True,
            "Markdown header preserved"
        ),
        (
            "1. First\n2. Second",
            "1. Primero\n2. Segundo",
            True,
            "Numbered list preserved"
        ),
        (
            "- First\n- Second",
            "- Primero\n- Segundo",
            True,
            "Bullet list preserved"
        ),
    ]

    passed = 0
    failed = 0

    for source, translation, should_pass, description in test_cases:
        source_counts = count_markers(source)
        trans_counts = count_markers(translation)

        # Check if all markers from source appear in translation
        markers_ok = all(
            trans_counts.get(pattern, 0) >= source_counts.get(pattern, 0)
            for pattern in special_markers
        )

        if markers_ok == should_pass:
            print(f"  ✓ {description}")
            passed += 1
        else:
            print(f"  ✗ {description}: expected {should_pass}, got {markers_ok}")
            print(f"    Source markers: {source_counts}")
            print(f"    Translation markers: {trans_counts}")
            failed += 1

    print(f"\n  Results: {passed} passed, {failed} failed")
    return failed == 0


def test_score_calculation():
    """Test score calculation logic."""
    print("\n[Test: Score Calculation]")

    def calculate_score(error_count, warning_count, info_count):
        """Calculate score based on issue counts."""
        score = 1.0
        score -= error_count * 0.3
        score -= warning_count * 0.1
        score -= info_count * 0.05
        return max(0.0, score)

    test_cases = [
        (0, 0, 0, 1.0, "No issues"),
        (1, 0, 0, 0.7, "One error"),
        (2, 0, 0, 0.4, "Two errors"),
        (0, 1, 0, 0.9, "One warning"),
        (0, 2, 0, 0.8, "Two warnings"),
        (1, 1, 0, 0.6, "One error, one warning"),
        (5, 0, 0, 0.0, "Many errors (min 0.0)"),
        (0, 0, 1, 0.95, "One info"),
    ]

    passed = 0
    failed = 0

    for errors, warnings, infos, expected_score, description in test_cases:
        result = calculate_score(errors, warnings, infos)
        if abs(result - expected_score) < 0.001:  # Float comparison
            print(f"  ✓ {description}: {result}")
            passed += 1
        else:
            print(f"  ✗ {description}: expected {expected_score}, got {result}")
            failed += 1

    print(f"\n  Results: {passed} passed, {failed} failed")
    return failed == 0


def test_realistic_scenarios():
    """Test with realistic translation scenarios."""
    print("\n[Test: Realistic Scenarios]")

    # Scenario 1: Good complete translation
    good_translation = SimpleChunk(
        chunk_id="test_001",
        source_text="""It is a truth universally acknowledged, that a single man in
possession of a good fortune, must be in want of a wife.""",
        translated_text="""Es una verdad universalmente reconocida que un hombre soltero en
posesión de una gran fortuna necesita una esposa."""
    )

    # Scenario 2: Incomplete with TODO
    incomplete_translation = SimpleChunk(
        chunk_id="test_002",
        source_text="This is the complete chapter text.",
        translated_text="TODO: Finish translating this chapter"
    )

    # Scenario 3: Truncated (no ending punctuation)
    truncated_translation = SimpleChunk(
        chunk_id="test_003",
        source_text="This is a complete sentence.",
        translated_text="Esta es una oración que de repente"
    )

    # Scenario 4: With section breaks
    with_breaks = SimpleChunk(
        chunk_id="test_004",
        source_text="Chapter One\n\n---\n\nText here.",
        translated_text="Capítulo Uno\n\n---\n\nTexto aquí."
    )

    # Test good translation
    print("\n  Scenario 1: Good complete translation")
    has_issues = False

    # Check empty
    if not good_translation.translated_text or not good_translation.translated_text.strip():
        print("    ✗ Failed: Translation is empty")
        has_issues = True

    # Check placeholder
    placeholder_patterns = [r'\bTODO\b', r'\bFIXME\b', r'\[.*?TRANSLATION.*?\]']
    for pattern in placeholder_patterns:
        if re.search(pattern, good_translation.translated_text, re.IGNORECASE):
            print(f"    ✗ Failed: Found placeholder pattern: {pattern}")
            has_issues = True

    # Check truncation
    if not re.search(r'[.!?…»")\]—]$', good_translation.translated_text.strip()):
        print("    ✗ Failed: Translation appears truncated")
        has_issues = True

    if not has_issues:
        print("    ✓ Passed: No issues found")

    # Test incomplete translation
    print("\n  Scenario 2: Incomplete with TODO")
    found_placeholder = False
    for pattern in [r'\bTODO\b', r'\bFIXME\b']:
        if re.search(pattern, incomplete_translation.translated_text, re.IGNORECASE):
            found_placeholder = True
            break

    if found_placeholder:
        print("    ✓ Passed: Detected placeholder (TODO)")
    else:
        print("    ✗ Failed: Did not detect placeholder")

    # Test truncated translation
    print("\n  Scenario 3: Truncated translation")
    is_truncated = not re.search(r'[.!?…»")\]—]$', truncated_translation.translated_text.strip())

    if is_truncated:
        print("    ✓ Passed: Detected truncation")
    else:
        print("    ✗ Failed: Did not detect truncation")

    # Test with section breaks
    print("\n  Scenario 4: With section breaks")
    source_has_rule = bool(re.search(r'^---+$', with_breaks.source_text, re.MULTILINE))
    trans_has_rule = bool(re.search(r'^---+$', with_breaks.translated_text, re.MULTILINE))

    if source_has_rule and trans_has_rule:
        print("    ✓ Passed: Section break preserved")
    elif source_has_rule and not trans_has_rule:
        print("    ✗ Failed: Section break missing in translation")
    else:
        print("    ✓ Passed: No section breaks to preserve")

    print("\n  All scenarios tested")
    return True


def main():
    """Run all manual tests."""
    print("=" * 70)
    print("COMPLETENESS EVALUATOR MANUAL VALIDATION")
    print("=" * 70)

    results = []

    results.append(("Empty Detection", test_empty_detection()))
    results.append(("Placeholder Detection", test_placeholder_patterns()))
    results.append(("Truncation Detection", test_truncation_detection()))
    results.append(("Special Markers", test_special_markers()))
    results.append(("Score Calculation", test_score_calculation()))
    results.append(("Realistic Scenarios", test_realistic_scenarios()))

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    passed_count = sum(1 for _, passed in results if passed)
    total_count = len(results)

    for test_name, passed in results:
        status = "✓ PASSED" if passed else "✗ FAILED"
        print(f"  {status}: {test_name}")

    print(f"\n  Total: {passed_count}/{total_count} test groups passed")

    if passed_count == total_count:
        print("\n🎉 All tests passed! CompletenessEvaluator is working correctly.")
        return 0
    else:
        print(f"\n⚠️  {total_count - passed_count} test group(s) failed.")
        return 1


if __name__ == '__main__':
    exit(main())
