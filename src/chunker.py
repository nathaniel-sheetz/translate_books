"""
Chunking module for dividing chapters into translation-sized chunks.

This module implements paragraph-based chunking with intelligent overlap
using a dual-constraint strategy (minimum paragraphs + minimum words).
"""

import logging
from datetime import datetime
from typing import List

from src.models import Chunk, ChunkMetadata, ChunkingConfig, ChunkStatus
from src.utils.text_utils import (
    normalize_newlines,
    extract_paragraphs,
    count_words,
    count_paragraphs
)

logger = logging.getLogger(__name__)


def _generate_chunk_id(chapter_id: str, position: int) -> str:
    """
    Generate a standardized chunk ID.

    Args:
        chapter_id: Chapter identifier (e.g., "chapter_01")
        position: 0-indexed position of chunk in chapter

    Returns:
        Formatted chunk ID (e.g., "chapter_01_chunk_000")

    Example:
        >>> _generate_chunk_id("chapter_01", 0)
        'chapter_01_chunk_000'
        >>> _generate_chunk_id("chapter_01", 3)
        'chapter_01_chunk_003'
    """
    return f"{chapter_id}_chunk_{position:03d}"


def _calculate_overlap(prev_paragraphs: List[str], config: ChunkingConfig) -> List[str]:
    """
    Calculate overlap paragraphs using dual-constraint strategy.

    Takes paragraphs from the end of the previous chunk until BOTH conditions are met:
    1. At least config.overlap_paragraphs paragraphs
    2. At least config.min_overlap_words words

    This handles both long paragraphs (fewer needed) and short dialogue (more needed).

    Args:
        prev_paragraphs: All paragraphs from previous chunk
        config: Chunking configuration with overlap constraints

    Returns:
        List of paragraphs to use as overlap (from end of prev_paragraphs)

    Example:
        Long paragraphs (200 words each):
        - overlap_paragraphs=2, min_overlap_words=100
        - Result: 2 paragraphs (meets both constraints)

        Short dialogue (10 words each):
        - overlap_paragraphs=2, min_overlap_words=100
        - Result: 10+ paragraphs (until word count reached)
    """
    if not prev_paragraphs:
        return []

    # If both constraints are 0, no overlap
    if config.overlap_paragraphs == 0 and config.min_overlap_words == 0:
        return []

    overlap = []
    word_count = 0

    # Start from end of previous chunk, work backwards
    for i in range(len(prev_paragraphs) - 1, -1, -1):
        para = prev_paragraphs[i]
        overlap.insert(0, para)  # Insert at beginning to maintain order
        word_count += count_words(para)

        # Check if both conditions met
        paragraphs_met = len(overlap) >= config.overlap_paragraphs
        words_met = word_count >= config.min_overlap_words

        if paragraphs_met and words_met:
            break

    return overlap


def _calculate_chunk_metadata(
    paragraphs: List[str],
    char_start: int,
    overlap_prev: int,
    overlap_next: int
) -> ChunkMetadata:
    """
    Calculate metadata for a chunk.

    Args:
        paragraphs: List of paragraph strings in this chunk
        char_start: Character position where chunk starts in original chapter
        overlap_prev: Number of characters that overlap with previous chunk
        overlap_next: Number of characters that will overlap with next chunk

    Returns:
        ChunkMetadata with all fields calculated

    Example:
        >>> paragraphs = ["Para 1", "Para 2"]
        >>> metadata = _calculate_chunk_metadata(paragraphs, 0, 0, 0)
        >>> metadata.paragraph_count
        2
    """
    # Join paragraphs with double newline (how they were originally separated)
    chunk_text = "\n\n".join(paragraphs)
    char_count = len(chunk_text)

    return ChunkMetadata(
        char_start=char_start,
        char_end=char_start + char_count,
        overlap_start=overlap_prev,
        overlap_end=overlap_next,
        paragraph_count=len(paragraphs),
        word_count=count_words(chunk_text)
    )


def _validate_chunk_size(chunk: Chunk, config: ChunkingConfig) -> List[str]:
    """
    Validate chunk size and return warning messages.

    Args:
        chunk: Chunk to validate
        config: Chunking configuration with size constraints

    Returns:
        List of warning messages (empty if no warnings)

    Example:
        >>> warnings = _validate_chunk_size(small_chunk, config)
        >>> warnings
        ['Chunk ch01_chunk_001 is too small: 300 words < 500 minimum']
    """
    warnings = []

    word_count = chunk.metadata.word_count

    if word_count < config.min_chunk_size:
        warnings.append(
            f"Chunk {chunk.id} is too small: "
            f"{word_count} words < {config.min_chunk_size} minimum"
        )

    if word_count > config.max_chunk_size:
        warnings.append(
            f"Chunk {chunk.id} is too large: "
            f"{word_count} words > {config.max_chunk_size} maximum"
        )

    return warnings


def chunk_chapter(
    chapter_text: str,
    config: ChunkingConfig,
    chapter_id: str = "chapter_01"
) -> List[Chunk]:
    """
    Divide chapter into translation-sized chunks with intelligent overlap.

    Uses paragraph-based chunking with dual-constraint overlap strategy:
    - Overlap must have at least N paragraphs AND M words
    - Handles both long paragraphs and short dialogue effectively

    Algorithm:
    1. Normalize newlines and extract paragraphs
    2. Group paragraphs until target_size reached
    3. Calculate overlap using dual constraints
    4. Generate metadata for each chunk
    5. Create Chunk objects

    Args:
        chapter_text: Full chapter text to chunk
        config: ChunkingConfig with target_size, overlap constraints, etc.
        chapter_id: Identifier for this chapter (default: "chapter_01")

    Returns:
        List of Chunk objects, ordered by position

    Example:
        >>> config = ChunkingConfig(target_size=1000, overlap_paragraphs=2, min_overlap_words=100)
        >>> chunks = chunk_chapter(chapter_text, config, "chapter_01")
        >>> len(chunks)
        3
        >>> chunks[0].id
        'chapter_01_chunk_000'

    Edge Cases:
        - Chapter shorter than min_chunk_size: Creates single chunk with warning
        - Last chunk smaller than min_chunk_size: Kept separate with warning
        - Single paragraph > max_chunk_size: Kept intact with warning
        - Zero overlap config: No overlap between chunks
    """
    # Normalize newlines
    chapter_text = normalize_newlines(chapter_text)

    # Extract paragraphs
    all_paragraphs = extract_paragraphs(chapter_text)

    if not all_paragraphs:
        # Empty chapter - return empty list
        logger.warning("Empty chapter provided - no paragraphs found")
        return []

    logger.info(
        f"Starting chunking for {chapter_id}: {len(all_paragraphs)} paragraphs, "
        f"{count_words(chapter_text)} words"
    )
    logger.debug(
        f"Chunking config: target_size={config.target_size}, "
        f"overlap_paragraphs={config.overlap_paragraphs}, "
        f"min_overlap_words={config.min_overlap_words}"
    )

    chunks = []
    current_chunk_paragraphs = []
    char_position = 0  # Track position in original chapter
    position = 0  # Chunk sequence number

    for i, paragraph in enumerate(all_paragraphs):
        # Add paragraph to current chunk
        current_chunk_paragraphs.append(paragraph)

        # Calculate current chunk word count
        chunk_word_count = sum(count_words(p) for p in current_chunk_paragraphs)

        # Decide if we should finalize this chunk
        is_last_paragraph = (i == len(all_paragraphs) - 1)
        reached_target = chunk_word_count >= config.target_size
        reached_min_and_last = chunk_word_count >= config.min_chunk_size and is_last_paragraph

        if reached_target or reached_min_and_last:
            # Calculate overlap with previous chunk (if exists)
            overlap_prev_chars = 0
            if chunks:
                # Get overlap paragraphs from previous chunk
                prev_chunk_paragraphs = extract_paragraphs(chunks[-1].source_text)
                overlap_paras = _calculate_overlap(prev_chunk_paragraphs, config)

                if overlap_paras:
                    # Calculate how many chars in current chunk are overlap
                    overlap_text = "\n\n".join(overlap_paras)
                    overlap_prev_chars = len(overlap_text)

            # Calculate overlap with next chunk (will be determined by next chunk)
            # For now, estimate based on current chunk
            overlap_next_paras = _calculate_overlap(current_chunk_paragraphs, config)
            overlap_next_text = "\n\n".join(overlap_next_paras) if overlap_next_paras else ""
            overlap_next_chars = len(overlap_next_text)

            # Create chunk text
            chunk_text = "\n\n".join(current_chunk_paragraphs)

            # Generate metadata
            metadata = _calculate_chunk_metadata(
                current_chunk_paragraphs,
                char_position,
                overlap_prev_chars,
                overlap_next_chars
            )

            # Create Chunk object
            chunk = Chunk(
                id=_generate_chunk_id(chapter_id, position),
                chapter_id=chapter_id,
                position=position,
                source_text=chunk_text,
                translated_text=None,
                metadata=metadata,
                status=ChunkStatus.PENDING,
                created_at=datetime.now(),
                translated_at=None
            )

            # Validate size and collect warnings
            warnings = _validate_chunk_size(chunk, config)
            for warning in warnings:
                logger.warning(warning)

            chunks.append(chunk)
            logger.debug(
                f"Created chunk {chunk.id}: {chunk.metadata.word_count} words, "
                f"{chunk.metadata.paragraph_count} paragraphs"
            )

            # Update position for next chunk
            position += 1
            char_position += len(chunk_text) + 2  # +2 for \n\n separator

            # Calculate overlap for next chunk
            if not is_last_paragraph:
                overlap_paragraphs = _calculate_overlap(current_chunk_paragraphs, config)
                current_chunk_paragraphs = overlap_paragraphs
            else:
                current_chunk_paragraphs = []

    # Handle any remaining paragraphs (shouldn't happen with proper logic, but safety check)
    if current_chunk_paragraphs and not chunks:
        # Very short chapter - create single chunk
        chunk_text = "\n\n".join(current_chunk_paragraphs)

        metadata = _calculate_chunk_metadata(
            current_chunk_paragraphs,
            0,  # char_start
            0,  # overlap_prev
            0   # overlap_next (no next chunk)
        )

        chunk = Chunk(
            id=_generate_chunk_id(chapter_id, 0),
            chapter_id=chapter_id,
            position=0,
            source_text=chunk_text,
            translated_text=None,
            metadata=metadata,
            status=ChunkStatus.PENDING,
            created_at=datetime.now(),
            translated_at=None
        )

        chunks.append(chunk)
        logger.debug(f"Created single chunk for short chapter: {chunk.id}")

    logger.info(f"Chunking complete: created {len(chunks)} chunk(s) for {chapter_id}")
    return chunks
