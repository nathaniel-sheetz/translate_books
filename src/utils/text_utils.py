"""
Text utility functions for paragraph detection and word counting.

This module provides utilities for processing chapter text, including:
- Normalizing newlines across different platforms
- Detecting and extracting paragraphs
- Counting words and paragraphs consistently with evaluators

All functions handle edge cases like empty text, single paragraphs,
and mixed newline conventions.
"""

import re
from typing import List


def normalize_newlines(text: str) -> str:
    """
    Convert all newline styles to Unix format (\\n).

    Handles Windows (\\r\\n), old Mac (\\r), and Unix (\\n) formats.
    Multiple consecutive newlines are preserved for paragraph detection.

    Args:
        text: Input text with potentially mixed newline styles

    Returns:
        Text with all newlines normalized to \\n

    Example:
        >>> normalize_newlines("Hello\\r\\nWorld")
        'Hello\\nWorld'
        >>> normalize_newlines("Line1\\r\\rLine2")
        'Line1\\n\\nLine2'
    """
    # Replace Windows CRLF first (before individual CR)
    text = text.replace('\r\n', '\n')
    # Replace old Mac CR
    text = text.replace('\r', '\n')
    return text


def count_words(text: str) -> int:
    """
    Count words in text using whitespace splitting.

    Matches the word counting behavior used by Chunk.word_count and evaluators.
    Handles multi-language text including Spanish accents.

    Args:
        text: Input text to count words in

    Returns:
        Number of words (0 for empty or whitespace-only text)

    Example:
        >>> count_words("The quick brown fox")
        4
        >>> count_words("El niño comió pan")
        4
        >>> count_words("")
        0
        >>> count_words("   ")
        0
    """
    if not text or not text.strip():
        return 0
    return len(text.split())


def extract_paragraphs(text: str) -> List[str]:
    """
    Split text into paragraphs separated by double newlines.

    A paragraph boundary is defined as two or more consecutive newlines.
    Leading/trailing whitespace is stripped from each paragraph.
    Empty paragraphs are filtered out.

    Args:
        text: Input text with paragraph breaks (\\n\\n)

    Returns:
        List of paragraph strings, stripped and non-empty

    Example:
        >>> extract_paragraphs("Para 1\\n\\nPara 2\\n\\nPara 3")
        ['Para 1', 'Para 2', 'Para 3']
        >>> extract_paragraphs("Single paragraph")
        ['Single paragraph']
        >>> extract_paragraphs("")
        []
    """
    # Normalize newlines first
    text = normalize_newlines(text)

    # Split on double newline (or more)
    # Pattern matches 2 or more consecutive newlines
    paragraphs = re.split(r'\n\n+', text)

    # Strip each paragraph and filter empty ones
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    return paragraphs


def count_paragraphs(text: str) -> int:
    """
    Count number of paragraphs in text.

    Paragraphs are separated by double newlines (\\n\\n).

    Args:
        text: Input text to count paragraphs in

    Returns:
        Number of paragraphs (0 for empty text)

    Example:
        >>> count_paragraphs("Para 1\\n\\nPara 2\\n\\nPara 3")
        3
        >>> count_paragraphs("Single paragraph")
        1
        >>> count_paragraphs("")
        0
    """
    return len(extract_paragraphs(text))


def detect_paragraph_boundaries(text: str) -> List[int]:
    """
    Find character positions where each paragraph starts.

    Returns 0-indexed positions in the original (normalized) text where
    paragraphs begin. The first paragraph always starts at position 0.

    Args:
        text: Input text with paragraph breaks

    Returns:
        List of starting character positions for each paragraph
        Empty list if text is empty

    Example:
        >>> detect_paragraph_boundaries("Para 1\\n\\nPara 2")
        [0, 8]
        >>> detect_paragraph_boundaries("Single paragraph")
        [0]
        >>> detect_paragraph_boundaries("")
        []
    """
    # Normalize newlines
    text = normalize_newlines(text)

    if not text.strip():
        return []

    boundaries = [0]  # First paragraph always starts at position 0

    # Find all double-newline positions
    for match in re.finditer(r'\n\n+', text):
        # Next paragraph starts after the blank lines
        start_pos = match.end()

        # Skip any leading whitespace (spaces/tabs) after blank lines
        while start_pos < len(text) and text[start_pos] in ' \t':
            start_pos += 1

        # Only add if there's actual content after the boundary
        if start_pos < len(text):
            boundaries.append(start_pos)

    return boundaries
