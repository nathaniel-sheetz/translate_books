"""
Book splitting functionality for automatic chapter detection.

This module provides utilities to detect chapter boundaries in full book files
and split them into individual chapter files. Supports Roman numerals, numeric
chapter patterns, and custom regex patterns.
"""

import re
from typing import List, Optional
from pydantic import BaseModel, Field


class DetectedChapter(BaseModel):
    """
    Represents a detected chapter with its content and metadata.

    Example:
        DetectedChapter(
            chapter_number=1,
            chapter_title="Chapter I",
            content="It was the best of times...",
            start_line=1,
            end_line=145
        )
    """
    chapter_number: int = Field(ge=1, description="Sequential chapter number (1, 2, 3...)")
    chapter_title: str = Field(description="Chapter title as it appears in text (e.g., 'Chapter I')")
    content: str = Field(min_length=1, description="Chapter text content")
    start_line: int = Field(ge=0, description="Starting line number in source file")
    end_line: int = Field(ge=0, description="Ending line number in source file")


# Roman numeral conversion tables
ROMAN_NUMERALS = {
    'I': 1, 'IV': 4, 'V': 5, 'IX': 9,
    'X': 10, 'XL': 40, 'L': 50, 'XC': 90,
    'C': 100, 'CD': 400, 'D': 500, 'CM': 900,
    'M': 1000
}


def roman_to_int(roman: str) -> Optional[int]:
    """
    Convert Roman numeral to integer.

    Args:
        roman: Roman numeral string (e.g., 'I', 'IV', 'XII', 'C')

    Returns:
        Integer value, or None if invalid Roman numeral

    Example:
        >>> roman_to_int('IV')
        4
        >>> roman_to_int('XLII')
        42
        >>> roman_to_int('C')
        100
    """
    roman = roman.upper().strip()

    if not roman:
        return None

    # Validate characters
    if not all(c in 'IVXLCDM' for c in roman):
        return None

    result = 0
    i = 0

    while i < len(roman):
        # Check for two-character numerals first
        if i + 1 < len(roman):
            two_char = roman[i:i+2]
            if two_char in ROMAN_NUMERALS:
                result += ROMAN_NUMERALS[two_char]
                i += 2
                continue

        # Single character numeral
        one_char = roman[i]
        if one_char in ROMAN_NUMERALS:
            result += ROMAN_NUMERALS[one_char]
            i += 1
        else:
            return None  # Invalid character

    return result


def int_to_roman(num: int) -> str:
    """
    Convert integer to Roman numeral.

    Args:
        num: Integer to convert (1-3999)

    Returns:
        Roman numeral string

    Example:
        >>> int_to_roman(4)
        'IV'
        >>> int_to_roman(42)
        'XLII'
        >>> int_to_roman(100)
        'C'
    """
    if num < 1 or num > 3999:
        raise ValueError("Number must be between 1 and 3999")

    val = [
        1000, 900, 500, 400,
        100, 90, 50, 40,
        10, 9, 5, 4,
        1
    ]
    syms = [
        'M', 'CM', 'D', 'CD',
        'C', 'XC', 'L', 'XL',
        'X', 'IX', 'V', 'IV',
        'I'
    ]

    result = ''
    for i, v in enumerate(val):
        count = num // v
        if count:
            result += syms[i] * count
            num -= v * count

    return result


def get_chapter_pattern(pattern_type: str = "roman", custom_regex: Optional[str] = None) -> re.Pattern:
    """
    Get compiled regex pattern for chapter detection.

    Args:
        pattern_type: Type of pattern - "roman", "numeric", or "custom"
        custom_regex: Custom regex pattern (required if pattern_type is "custom")

    Returns:
        Compiled regex pattern that matches chapter headers

    Raises:
        ValueError: If pattern_type is invalid or custom_regex missing for "custom" type

    Example:
        >>> pattern = get_chapter_pattern("roman")
        >>> match = pattern.search("Chapter I\\n\\nIt was the best of times...")
        >>> match.group(1)  # Chapter number/title
        'I'
    """
    if pattern_type == "roman":
        # Matches: "Chapter I", "CHAPTER II", "Chapter III", etc.
        # Case-insensitive, allows optional colon or period after chapter
        # Captures the Roman numeral
        return re.compile(
            r'^\s*chapter\s+([IVXLCDM]+)[\.\:\s]*$',
            re.IGNORECASE | re.MULTILINE
        )

    elif pattern_type == "numeric":
        # Matches: "Chapter 1", "CHAPTER 2", "Chapter 3", etc.
        # Case-insensitive, allows optional colon or period
        # Captures the number
        return re.compile(
            r'^\s*chapter\s+(\d+)[\.\:\s]*$',
            re.IGNORECASE | re.MULTILINE
        )

    elif pattern_type == "custom":
        if not custom_regex:
            raise ValueError("custom_regex is required when pattern_type is 'custom'")

        try:
            return re.compile(custom_regex, re.IGNORECASE | re.MULTILINE)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}")

    else:
        raise ValueError(f"Invalid pattern_type: {pattern_type}. Must be 'roman', 'numeric', or 'custom'")


def split_book_into_chapters(
    book_text: str,
    pattern_type: str = "roman",
    custom_regex: Optional[str] = None,
    min_chapter_size: int = 100,
) -> List[DetectedChapter]:
    """
    Split a full book text into individual chapters.

    Detects chapter boundaries using the specified pattern and extracts
    chapter content. Handles edge cases like prefaces, epilogues, and
    varying whitespace.

    Args:
        book_text: Full text of the book to split
        pattern_type: Type of chapter pattern - "roman", "numeric", or "custom"
        custom_regex: Custom regex pattern (required if pattern_type is "custom")
        min_chapter_size: Minimum characters for valid chapter (filters false matches)

    Returns:
        List of DetectedChapter objects in sequential order

    Raises:
        ValueError: If no chapters detected or invalid parameters

    Example:
        >>> book = "Chapter I\\n\\nIt was...\\n\\nChapter II\\n\\nIt was still..."
        >>> chapters = split_book_into_chapters(book, pattern_type="roman")
        >>> len(chapters)
        2
        >>> chapters[0].chapter_title
        'Chapter I'
    """
    if not book_text or not book_text.strip():
        raise ValueError("Book text cannot be empty")

    # Get chapter detection pattern
    pattern = get_chapter_pattern(pattern_type, custom_regex)

    # Find all chapter headers
    matches = list(pattern.finditer(book_text))

    if not matches:
        raise ValueError(
            f"No chapters detected with pattern type '{pattern_type}'. "
            f"Check that your book uses the expected chapter format."
        )

    # Split text into lines for line number tracking
    lines = book_text.split('\n')

    detected_chapters = []

    for i, match in enumerate(matches):
        # Extract chapter number/title from match
        chapter_identifier = match.group(1)  # The captured group (Roman/number)

        # Determine chapter number
        if pattern_type == "roman":
            chapter_num = roman_to_int(chapter_identifier)
            if chapter_num is None:
                # Skip invalid Roman numerals
                continue
            chapter_title = f"Chapter {chapter_identifier.upper()}"

        elif pattern_type == "numeric":
            try:
                chapter_num = int(chapter_identifier)
                chapter_title = f"Chapter {chapter_num}"
            except ValueError:
                continue

        else:  # custom
            # For custom patterns, use sequential numbering
            chapter_num = i + 1
            chapter_title = match.group(0).strip()

        # Find start and end positions
        start_pos = match.end()  # Start after the chapter header

        # End position is start of next chapter, or end of book
        if i + 1 < len(matches):
            end_pos = matches[i + 1].start()
        else:
            end_pos = len(book_text)

        # Extract chapter content
        content = book_text[start_pos:end_pos].strip()

        # Skip if content is too short (likely false positive)
        if len(content) < min_chapter_size:
            continue

        # Calculate line numbers
        start_line = book_text[:match.start()].count('\n')
        end_line = book_text[:end_pos].count('\n')

        detected_chapters.append(DetectedChapter(
            chapter_number=chapter_num,
            chapter_title=chapter_title,
            content=content,
            start_line=start_line,
            end_line=end_line
        ))

    if not detected_chapters:
        raise ValueError(
            "No valid chapters found. Chapters may be too short or pattern may not match."
        )

    # Sort by chapter number to ensure sequential order
    detected_chapters.sort(key=lambda c: c.chapter_number)

    return detected_chapters


def validate_chapter_sequence(chapters: List[DetectedChapter]) -> tuple[bool, List[str]]:
    """
    Validate that detected chapters form a proper sequence.

    Checks for gaps in chapter numbering, duplicates, and other issues
    that might indicate detection problems.

    Args:
        chapters: List of detected chapters

    Returns:
        Tuple of (is_valid, list_of_warnings)

    Example:
        >>> chapters = [chapter1, chapter2, chapter4]  # Missing chapter 3
        >>> is_valid, warnings = validate_chapter_sequence(chapters)
        >>> print(warnings[0])
        'Gap in sequence: Missing chapter 3'
    """
    if not chapters:
        return (False, ["No chapters provided"])

    warnings = []

    # Sort by chapter number
    sorted_chapters = sorted(chapters, key=lambda c: c.chapter_number)

    # Check for duplicates
    chapter_nums = [c.chapter_number for c in sorted_chapters]
    duplicates = [num for num in set(chapter_nums) if chapter_nums.count(num) > 1]

    if duplicates:
        for num in duplicates:
            warnings.append(f"Duplicate chapter number: {num}")

    # Check for gaps in sequence
    expected_next = 1
    for chapter in sorted_chapters:
        if chapter.chapter_number > expected_next:
            # Found a gap
            missing = list(range(expected_next, chapter.chapter_number))
            warnings.append(f"Gap in sequence: Missing chapter(s) {missing}")

        expected_next = chapter.chapter_number + 1

    # Check if first chapter is 1
    if sorted_chapters[0].chapter_number != 1:
        warnings.append(
            f"First chapter is {sorted_chapters[0].chapter_number}, not 1. "
            f"Book may have prologue or preface."
        )

    # Check for very short chapters (potential false positives)
    for chapter in sorted_chapters:
        if len(chapter.content) < 500:
            warnings.append(
                f"Chapter {chapter.chapter_number} is very short ({len(chapter.content)} chars). "
                f"May be a false positive."
            )

    is_valid = len(warnings) == 0
    return (is_valid, warnings)


def save_chapters_to_files(
    chapters: List[DetectedChapter],
    output_dir: str,
    filename_prefix: str = "chapter",
    filename_suffix: str = ".txt"
) -> List[str]:
    """
    Save detected chapters to individual text files.

    Args:
        chapters: List of detected chapters
        output_dir: Directory to save chapter files
        filename_prefix: Prefix for chapter filenames (default: "chapter")
        filename_suffix: File extension (default: ".txt")

    Returns:
        List of created file paths

    Example:
        >>> chapters = split_book_into_chapters(book_text)
        >>> files = save_chapters_to_files(chapters, "chapters/")
        >>> print(files[0])
        'chapters/chapter_01.txt'
    """
    from pathlib import Path

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    created_files = []

    for chapter in chapters:
        # Generate filename with zero-padded chapter number
        filename = f"{filename_prefix}_{chapter.chapter_number:02d}{filename_suffix}"
        filepath = output_path / filename

        # Write chapter content
        filepath.write_text(f"{chapter.chapter_title}\n\n{chapter.content}", encoding='utf-8')

        created_files.append(str(filepath))

    return created_files
