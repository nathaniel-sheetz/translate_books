"""
Chunking module for dividing chapters into translation-sized chunks.

Uses a three-phase approach:
1. Plan: determine optimal chunk count based on chapter length
2. Score: rate each paragraph boundary for split quality
3. Optimize: find split points that balance even sizing with good boundaries
"""

import logging
import math
import re
from datetime import datetime
from typing import List, Set

from src.models import Chunk, ChunkMetadata, ChunkingConfig, ChunkStatus
from src.utils.text_utils import (
    normalize_newlines,
    extract_paragraphs,
    count_words,
    count_paragraphs
)

logger = logging.getLogger(__name__)


# --- Constants for split-point scoring ---

CONTINUATION_WORDS = {
    "and", "or", "but", "so", "then", "yet", "still",
    "however", "moreover", "furthermore", "besides",
    "meanwhile", "afterward", "afterwards", "after",
    "also", "nor", "hence", "thus", "therefore",
    "nevertheless", "nonetheless", "consequently",
    "additionally", "similarly", "likewise",
}

CONTINUATION_BIGRAMS = {
    ("after", "that"), ("and", "then"), ("but", "then"),
    ("just", "then"), ("even", "so"), ("in", "addition"),
}

DIALOGUE_STARTERS = {'"', '\u201c', '\u2018', "'", '\u00ab'}

ATTRIBUTION_RE = re.compile(
    r'\b(said|replied|asked|cried|exclaimed|whispered|murmured|'
    r'shouted|answered|called|declared|muttered|continued|added|'
    r'remarked|responded|inquired)\b',
    re.IGNORECASE
)

SCENE_BREAK_RE = re.compile(r'^[\s*\-_]{1,}$')


def _generate_chunk_id(chapter_id: str, position: int) -> str:
    """Generate a standardized chunk ID."""
    return f"{chapter_id}_chunk_{position:03d}"


def _calculate_overlap(prev_paragraphs: List[str], config: ChunkingConfig) -> List[str]:
    """
    Calculate overlap paragraphs using dual-constraint strategy.

    Takes paragraphs from the end of the previous chunk until BOTH conditions are met:
    1. At least config.overlap_paragraphs paragraphs
    2. At least config.min_overlap_words words
    """
    if not prev_paragraphs:
        return []

    if config.overlap_paragraphs == 0 and config.min_overlap_words == 0:
        return []

    overlap = []
    word_count = 0

    for i in range(len(prev_paragraphs) - 1, -1, -1):
        para = prev_paragraphs[i]
        overlap.insert(0, para)
        word_count += count_words(para)

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
    """Calculate metadata for a chunk."""
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
    """Validate chunk size and return warning messages."""
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


# --- Phase 1: Optimal chunk count ---

def _optimal_chunk_count(total_words: int, config: ChunkingConfig) -> int:
    """Determine how many chunks a chapter should be split into."""
    if total_words <= config.max_chunk_size:
        return 1

    n_ideal = round(total_words / config.target_size)
    n_min = math.ceil(total_words / config.max_chunk_size)
    n_max = math.floor(total_words / config.min_chunk_size)

    n = max(n_min, min(n_max, n_ideal))
    return max(1, n)


# --- Phase 2: Score split points ---

def _is_dialogue(para: str) -> bool:
    """Detect if a paragraph is dialogue."""
    stripped = para.lstrip()
    if stripped and stripped[0] in DIALOGUE_STARTERS:
        return True
    if ATTRIBUTION_RE.search(para) and ('"' in para or '\u201c' in para or '\u201d' in para):
        return True
    return False


def _is_scene_break(para: str) -> bool:
    """Detect if a paragraph is a scene-break marker like *** or ---."""
    stripped = para.strip()
    if not stripped:
        return False
    # Must be short and consist only of *, -, _, whitespace
    if len(stripped) > 20 and not SCENE_BREAK_RE.match(stripped):
        return False
    return bool(re.match(r'^[\s*\-_]+$', stripped)) and len(stripped.replace(' ', '')) >= 3


def _score_split_points(paragraphs: List[str]) -> List[float]:
    """
    Score each paragraph boundary from 0.0 (bad split) to 1.0 (good split).

    Returns a list of len(paragraphs) - 1 scores.
    """
    if len(paragraphs) <= 1:
        return []

    scores = []
    dialogue_flags = [_is_dialogue(p) for p in paragraphs]

    for i in range(len(paragraphs) - 1):
        score = 0.5

        next_para = paragraphs[i + 1]
        curr_para = paragraphs[i]

        # --- Continuation signal penalty ---
        words = next_para.split()[:3]
        if words:
            first_word = words[0].lower().rstrip(".,;:!?")
            if first_word in CONTINUATION_WORDS:
                score -= 0.3
            # Check bigrams
            if len(words) >= 2:
                bigram = (words[0].lower().rstrip(".,;:!?"), words[1].lower().rstrip(".,;:!?"))
                if bigram in CONTINUATION_BIGRAMS:
                    score -= 0.3

        # --- Dialogue continuity ---
        curr_dialogue = dialogue_flags[i]
        next_dialogue = dialogue_flags[i + 1]
        if curr_dialogue and next_dialogue:
            score -= 0.25
        elif curr_dialogue and not next_dialogue:
            score += 0.15  # End of dialogue sequence

        # --- Scene break ---
        if _is_scene_break(next_para):
            score += 0.4
        # Also check if current paragraph is a scene break (split after it)
        if _is_scene_break(curr_para):
            score += 0.4

        # --- Long paragraph bonus ---
        if count_words(curr_para) > 150:
            score += 0.1

        # --- Finality bonus ---
        if not curr_dialogue:
            sentences = re.split(r'[.!?]+', curr_para)
            last_sentence = sentences[-1].strip() if sentences else ""
            # If last sentence is empty (paragraph ended with punctuation), use second-to-last
            if not last_sentence and len(sentences) >= 2:
                last_sentence = sentences[-2].strip()
            if last_sentence and len(last_sentence.split()) < 15:
                score += 0.15

        scores.append(max(0.0, min(1.0, score)))

    return scores


# --- Phase 3: Find optimal splits via DP ---

def _find_optimal_splits(
    para_words: List[int],
    scores: List[float],
    n_chunks: int,
    ideal_size: float,
    config: ChunkingConfig
) -> List[int]:
    """
    Find optimal paragraph indices where new chunks start.

    Returns a list of start indices, e.g. [0, 12, 25] for 3 chunks.
    Uses dynamic programming to balance even sizing with split quality.
    """
    n_paras = len(para_words)

    if n_chunks == 1:
        return [0]

    # Precompute prefix sums for fast range word counts
    prefix = [0] * (n_paras + 1)
    for i in range(n_paras):
        prefix[i + 1] = prefix[i] + para_words[i]

    def range_words(a: int, b: int) -> int:
        """Word count for paragraphs [a, b)."""
        return prefix[b] - prefix[a]

    weight = config.split_quality_weight

    def chunk_cost(a: int, b: int) -> float:
        """Cost of a chunk spanning paragraphs [a, b)."""
        words = range_words(a, b)
        size_cost = ((words - ideal_size) / ideal_size) ** 2

        # Split quality cost at the right boundary (where we cut)
        if b < n_paras and scores:
            boundary_idx = b - 1  # boundary between para b-1 and para b
            split_cost = 1.0 - scores[boundary_idx]
        else:
            split_cost = 0.0

        return size_cost + weight * split_cost

    # DP: dp[k][j] = min cost of splitting paragraphs [0, j) into k chunks
    INF = float('inf')
    dp = [[INF] * (n_paras + 1) for _ in range(n_chunks + 1)]
    parent = [[0] * (n_paras + 1) for _ in range(n_chunks + 1)]

    # Base: 1 chunk covering [0, j)
    for j in range(1, n_paras + 1):
        dp[1][j] = chunk_cost(0, j)
        parent[1][j] = 0

    # Fill DP table
    for k in range(2, n_chunks + 1):
        for j in range(k, n_paras + 1):
            for i in range(k - 1, j):
                cost = dp[k - 1][i] + chunk_cost(i, j)
                if cost < dp[k][j]:
                    dp[k][j] = cost
                    parent[k][j] = i

    # Backtrack to find split indices
    splits = []
    j = n_paras
    for k in range(n_chunks, 0, -1):
        splits.append(parent[k][j] if k > 1 else 0)
        j = parent[k][j] if k > 1 else 0

    splits.reverse()
    # splits[0] should always be 0
    if splits[0] != 0:
        splits[0] = 0

    return splits


# --- Build chunks from split indices ---

def _build_chunks_from_splits(
    all_paragraphs: List[str],
    split_indices: List[int],
    chapter_id: str,
    config: ChunkingConfig
) -> List[Chunk]:
    """Assemble Chunk objects from split indices, with overlap and metadata."""
    chunks = []
    n_chunks = len(split_indices)
    char_position = 0

    for idx in range(n_chunks):
        start = split_indices[idx]
        end = split_indices[idx + 1] if idx + 1 < n_chunks else len(all_paragraphs)
        chunk_paragraphs = all_paragraphs[start:end]

        # Calculate overlap with previous chunk
        overlap_prev_chars = 0
        if chunks:
            prev_chunk_paragraphs = extract_paragraphs(chunks[-1].source_text)
            overlap_paras = _calculate_overlap(prev_chunk_paragraphs, config)
            if overlap_paras:
                # Prepend overlap paragraphs to current chunk
                overlap_text = "\n\n".join(overlap_paras)
                overlap_prev_chars = len(overlap_text)
                chunk_paragraphs = overlap_paras + chunk_paragraphs

        # Calculate overlap with next chunk (estimate from current chunk)
        is_last = (idx == n_chunks - 1)
        overlap_next_chars = 0
        if not is_last:
            overlap_next_paras = _calculate_overlap(chunk_paragraphs, config)
            if overlap_next_paras:
                overlap_next_chars = len("\n\n".join(overlap_next_paras))

        chunk_text = "\n\n".join(chunk_paragraphs)

        metadata = _calculate_chunk_metadata(
            chunk_paragraphs,
            char_position,
            overlap_prev_chars,
            overlap_next_chars
        )

        chunk = Chunk(
            id=_generate_chunk_id(chapter_id, idx),
            chapter_id=chapter_id,
            position=idx,
            source_text=chunk_text,
            translated_text=None,
            metadata=metadata,
            status=ChunkStatus.PENDING,
            created_at=datetime.now(),
            translated_at=None
        )

        warnings = _validate_chunk_size(chunk, config)
        for warning in warnings:
            logger.warning(warning)

        chunks.append(chunk)
        logger.debug(
            f"Created chunk {chunk.id}: {chunk.metadata.word_count} words, "
            f"{chunk.metadata.paragraph_count} paragraphs"
        )

        # Advance char position (use non-overlap portion for tracking)
        char_position += len(chunk_text) + 2  # +2 for \n\n separator

    return chunks


def chunk_chapter(
    chapter_text: str,
    config: ChunkingConfig,
    chapter_id: str = "chapter_01"
) -> List[Chunk]:
    """
    Divide chapter into translation-sized chunks with intelligent splitting.

    Uses a three-phase approach:
    1. Plan: determine optimal chunk count from word count and config
    2. Score: rate each paragraph boundary for split quality
    3. Optimize: DP solver finds splits balancing even sizing with good boundaries

    Args:
        chapter_text: Full chapter text to chunk
        config: ChunkingConfig with target_size, overlap constraints, etc.
        chapter_id: Identifier for this chapter (default: "chapter_01")

    Returns:
        List of Chunk objects, ordered by position
    """
    chapter_text = normalize_newlines(chapter_text)
    all_paragraphs = extract_paragraphs(chapter_text)

    if not all_paragraphs:
        logger.warning("Empty chapter provided - no paragraphs found")
        return []

    para_words = [count_words(p) for p in all_paragraphs]
    total_words = sum(para_words)

    logger.info(
        f"Starting chunking for {chapter_id}: {len(all_paragraphs)} paragraphs, "
        f"{total_words} words"
    )

    # Phase 1: Determine chunk count
    n_chunks = _optimal_chunk_count(total_words, config)
    # Can't split into more chunks than paragraphs
    n_chunks = min(n_chunks, len(all_paragraphs))

    if n_chunks == 1:
        # Single chunk - build directly
        chunk_text = "\n\n".join(all_paragraphs)
        metadata = _calculate_chunk_metadata(all_paragraphs, 0, 0, 0)
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
        warnings = _validate_chunk_size(chunk, config)
        for warning in warnings:
            logger.warning(warning)
        logger.info(f"Chunking complete: created 1 chunk for {chapter_id}")
        return [chunk]

    # Phase 2: Score split points
    scores = _score_split_points(all_paragraphs)

    # Phase 3: Find optimal splits
    ideal_size = total_words / n_chunks
    split_indices = _find_optimal_splits(
        para_words, scores, n_chunks, ideal_size, config
    )

    logger.debug(
        f"Optimal splits for {n_chunks} chunks: paragraph indices {split_indices}"
    )

    # Build chunks with overlap
    chunks = _build_chunks_from_splits(
        all_paragraphs, split_indices, chapter_id, config
    )

    logger.info(f"Chunking complete: created {len(chunks)} chunk(s) for {chapter_id}")
    return chunks
