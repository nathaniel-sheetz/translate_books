"""
Combiner module for merging translated chunks back into complete chapters.

Chunks are stitched by **plain concatenation** with a normalized paragraph break
at each boundary. Chunk *overlap* is disabled: the historical "use_previous"
char-slice de-dup path is known-broken — a worker that sees the prior translation
in its prompt may drop the overlapping text, so slicing ``overlap_start`` chars
off its draft would chop real content. ``validate_chunk_completeness`` therefore
rejects any chunk that still carries nonzero overlap metadata, and the harness
creates chunks with zero overlap. The prompt's "previous section" block is
continuity context only and is never re-combined here.

See ``docs/design/TRANSLATE_HARNESS_FRICTION_LOG_4.md`` #20.
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

    # Check 4: No chunk carries overlap. The overlap/combine de-dup path is
    # disabled (known-broken); chunks must be produced with zero overlap. A
    # nonzero overlap_start would make the old char-slice chop real translated
    # content, so refuse loudly here instead of silently corrupting the chapter.
    overlapping = [
        f"{c.id} (overlap_start={c.metadata.overlap_start}, "
        f"overlap_end={c.metadata.overlap_end})"
        for c in sorted_chunks
        if c.metadata.overlap_start > 0 or c.metadata.overlap_end > 0
    ]
    if overlapping:
        errors.append(
            "Overlap/combine de-dup is disabled (known-broken). These chunks "
            f"carry overlap: {', '.join(overlapping)}. Re-chunk with "
            "--overlap-paragraphs 0 --min-overlap-words 0."
        )

    is_valid = len(errors) == 0
    if is_valid:
        logger.debug("Chunk validation passed")
    else:
        for error in errors:
            logger.error(f"Validation error: {error}")
    return is_valid, errors


def combine_chunks(chunks: List[Chunk]) -> str:
    """
    Combine translated chunks into a complete chapter by plain concatenation.

    Chunk overlap is disabled (the "use_previous" char-slice de-dup is
    known-broken), so each chunk contributes its full ``translated_text``. The
    boundary between consecutive chunks is normalized to exactly one blank line.

    Args:
        chunks: List of Chunk objects (may be unsorted)

    Returns:
        Combined chapter text

    Raises:
        ValueError: If chunks fail validation — gaps, untranslated chunks, or
            (per ``validate_chunk_completeness``) any chunk that still carries
            nonzero overlap metadata.

    Example:
        >>> chunks = [chunk1, chunk2, chunk3]
        >>> chapter_text = combine_chunks(chunks)
        >>> print(f"Combined: {len(chapter_text)} characters")

    Algorithm:
        1. Validate chunks (completeness, translations, zero overlap).
        2. Sort chunks by position.
        3. Concatenate each chunk's translated_text, normalizing every chunk
           boundary to a single blank line.

    Edge Cases:
        - Single chunk: Returns translated_text as-is
        - Empty translated_text: Caught by validation
    """
    # Validate chunks
    is_valid, errors = validate_chunk_completeness(chunks)
    if not is_valid:
        error_msg = "Cannot combine chunks - validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        logger.error(error_msg)
        raise ValueError(error_msg)

    logger.info(f"Combining {len(chunks)} chunks by plain concatenation")

    # Sort chunks by position
    sorted_chunks = sorted(chunks, key=lambda c: c.position)

    # Handle single chunk
    if len(sorted_chunks) == 1:
        logger.info("Single chunk - returning translated text as-is")
        return sorted_chunks[0].translated_text

    # Stitch by plain concatenation. Overlap is disabled (rejected by validation
    # above), so each chunk's full translated_text belongs in the chapter.
    chapter_text = ""

    for i, chunk in enumerate(sorted_chunks):
        if i == 0:
            # First chunk: use entire translated text
            chapter_text = chunk.translated_text
            continue

        # Chunks are paragraph-aligned by construction (the chunker splits on
        # \n\n and stores "\n\n".join(paragraphs) with no leading or trailing
        # separator), so the boundary between two consecutive chunks is always a
        # paragraph break. Normalize it to exactly one blank line so the break
        # survives reassembly regardless of any stray trailing newlines (or
        # Windows-style \r\n) the translator may have emitted.
        chunk_text = chunk.translated_text.lstrip("\r\n")
        if chunk_text:
            chapter_text = chapter_text.rstrip("\r\n") + "\n\n" + chunk_text

    logger.info(
        f"Combination complete: {len(chapter_text)} characters total from "
        f"{len(sorted_chunks)} chunks"
    )
    return chapter_text
