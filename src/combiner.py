"""
Combiner module for merging translated chunks back into complete chapters.

This module implements the "use_previous" overlap resolution strategy:
- Keep overlap from the chunk that ends with it
- Discard overlap from the chunk that starts with it
- Rationale: Translator has more context at the END of a chunk
"""

import logging
from typing import List, Tuple

from src.models import Chunk

_LIGHT_RULE = "—" * 24
_HEAVY_RULE = "═" * 24


def generate_bilingual_text(chunks: List[Chunk]) -> str:
    """
    Generate a bilingual (parallel) text with source and translation interleaved.

    Sorts chunks by (chapter_id, position) and emits an Original section
    followed by a Translation section for each chunk. Chunks without a
    translation get a placeholder — the function never aborts on missing
    translations.

    Separators:
        - Light rule (—) between source and translation within a chunk
        - Heavy rule (═) between chunks

    Args:
        chunks: List of Chunk objects (may be unsorted, may span chapters)

    Returns:
        Bilingual text as a single string

    Example:
        >>> text = generate_bilingual_text(chunks)
        >>> Path("review.txt").write_text(text, encoding="utf-8")
    """
    if not chunks:
        return ""

    sorted_chunks = sorted(chunks, key=lambda c: (c.chapter_id, c.position))

    sections = []
    for chunk in sorted_chunks:
        label = f"[{chunk.chapter_id} / chunk {chunk.position:03d}]"

        original_block = f"{label} — Original\n\n{chunk.source_text}"

        if chunk.has_translation:
            translation_block = f"{label} — Translation\n\n{chunk.translated_text}"
        else:
            translation_block = f"{label} — Translation\n\n(not yet translated)"

        sections.append(f"{original_block}\n\n{_LIGHT_RULE}\n\n{translation_block}")

    return f"\n\n{_HEAVY_RULE}\n\n".join(sections) + "\n"
logger = logging.getLogger(__name__)


def validate_chunk_completeness(chunks: List[Chunk]) -> Tuple[bool, List[str]]:
    """
    Validate that chunks form a complete, translatable set.

    Checks:
    - No gaps in sequence (positions are consecutive: 0, 1, 2, ...)
    - All chunks have the same chapter_id
    - All chunks have translations (translated_text is not None/empty)

    Args:
        chunks: List of chunks to validate (may be unsorted)

    Returns:
        Tuple of (is_valid, error_messages)
        - is_valid: True if all checks pass, False otherwise
        - error_messages: List of error descriptions (empty if valid)

    Example:
        >>> valid, errors = validate_chunk_completeness(chunks)
        >>> if not valid:
        ...     print("\\n".join(errors))
    """
    if not chunks:
        logger.error("Validation failed: No chunks provided")
        return False, ["No chunks provided"]

    errors = []
    logger.debug(f"Validating {len(chunks)} chunks for completeness")

    # Sort chunks by position for validation
    sorted_chunks = sorted(chunks, key=lambda c: c.position)

    # Check 1: All chunks have same chapter_id
    chapter_ids = set(c.chapter_id for c in sorted_chunks)
    if len(chapter_ids) > 1:
        errors.append(
            f"Multiple chapter IDs found: {', '.join(sorted(chapter_ids))}. "
            f"All chunks must belong to the same chapter."
        )

    # Check 2: No gaps in sequence (should be 0, 1, 2, 3, ...)
    expected_positions = list(range(len(sorted_chunks)))
    actual_positions = [c.position for c in sorted_chunks]

    if actual_positions != expected_positions:
        missing = set(expected_positions) - set(actual_positions)
        extra = set(actual_positions) - set(expected_positions)

        if missing:
            errors.append(
                f"Missing chunk positions: {sorted(missing)}. "
                f"Cannot combine incomplete chapter."
            )
        if extra:
            errors.append(
                f"Unexpected chunk positions: {sorted(extra)}. "
                f"Expected positions 0-{len(sorted_chunks)-1}."
            )

    # Check 3: All chunks have translations
    untranslated = [
        c.id for c in sorted_chunks
        if not c.translated_text or not c.translated_text.strip()
    ]

    if untranslated:
        errors.append(
            f"Untranslated chunks found: {', '.join(untranslated)}. "
            f"All chunks must be translated before combining."
        )

    is_valid = len(errors) == 0
    if is_valid:
        logger.debug("Chunk validation passed")
    else:
        for error in errors:
            logger.error(f"Validation error: {error}")
    return is_valid, errors


def _remove_start_overlap(text: str, overlap_chars: int) -> str:
    """
    Remove overlap characters from the start of text.

    Used to extract the non-overlapping portion of subsequent chunks
    when applying "use_previous" strategy.

    Args:
        text: Full text of chunk
        overlap_chars: Number of characters to remove from start

    Returns:
        Text with overlap removed

    Example:
        >>> _remove_start_overlap("overlap text here", 7)
        ' text here'

    Edge Cases:
        - overlap_chars > len(text): Returns empty string
        - overlap_chars = 0: Returns full text
        - overlap_chars < 0: Treated as 0
    """
    if overlap_chars <= 0:
        return text

    if overlap_chars >= len(text):
        # Overlap exceeds text length - return empty
        # This might indicate a metadata error
        return ""

    return text[overlap_chars:]


def combine_chunks(chunks: List[Chunk]) -> str:
    """
    Combine translated chunks into a complete chapter.

    Uses "use_previous" overlap resolution strategy:
    - First chunk: Use entire translated_text
    - Subsequent chunks: Remove overlap_start characters, use remainder

    The overlap from chunk N is kept (it ends with the overlap).
    The overlap from chunk N+1 is discarded (it starts with the overlap).

    Rationale: Translator has more context at the END of chunk N
    (they've been reading it) than at the START of chunk N+1
    (they're just beginning).

    Args:
        chunks: List of Chunk objects (may be unsorted)

    Returns:
        Combined chapter text

    Raises:
        ValueError: If chunks fail validation (gaps, untranslated, etc.)

    Example:
        >>> chunks = [chunk1, chunk2, chunk3]
        >>> chapter_text = combine_chunks(chunks)
        >>> print(f"Combined: {len(chapter_text)} characters")

    Algorithm:
        1. Validate chunks (completeness, translations, etc.)
        2. Sort chunks by position
        3. For first chunk: Add entire translated_text
        4. For each subsequent chunk:
           a. Get overlap_start from metadata
           b. Remove overlap_start chars from beginning
           c. Append remaining text to chapter
        5. Return combined chapter

    Edge Cases:
        - Single chunk: Returns translated_text as-is
        - Zero overlap: Just concatenates chunks
        - Empty translated_text: Caught by validation
    """
    # Validate chunks
    is_valid, errors = validate_chunk_completeness(chunks)
    if not is_valid:
        error_msg = "Cannot combine chunks - validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        logger.error(error_msg)
        raise ValueError(error_msg)

    logger.info(f"Combining {len(chunks)} chunks using 'use_previous' strategy")

    # Sort chunks by position
    sorted_chunks = sorted(chunks, key=lambda c: c.position)

    # Handle single chunk
    if len(sorted_chunks) == 1:
        logger.info("Single chunk - returning translated text as-is")
        return sorted_chunks[0].translated_text

    # Combine chunks using "use_previous" strategy
    chapter_text = ""

    for i, chunk in enumerate(sorted_chunks):
        if i == 0:
            # First chunk: use entire translated text
            chapter_text = chunk.translated_text
        else:
            # Subsequent chunks: remove overlap from start
            overlap_chars = chunk.metadata.overlap_start

            # Remove overlap and append remainder
            non_overlap_text = _remove_start_overlap(
                chunk.translated_text,
                overlap_chars
            )

            logger.debug(
                f"Chunk {chunk.id}: removed {overlap_chars} overlap chars, "
                f"appending {len(non_overlap_text)} chars"
            )

            # Add separator if needed (chunks should already have proper formatting)
            # Just concatenate - chunks should maintain their internal structure
            chapter_text += non_overlap_text

    logger.info(
        f"Combination complete: {len(chapter_text)} characters total from "
        f"{len(sorted_chunks)} chunks"
    )
    return chapter_text
