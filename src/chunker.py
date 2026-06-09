"""
Chunking module for dividing chapters into translation-sized chunks.

Uses a three-phase approach:
1. Plan: determine optimal chunk count based on chapter length
2. Score: rate each paragraph boundary for split quality
3. Optimize: find split points that balance even sizing with good boundaries
"""

import bisect
import logging
import math
import re
from datetime import datetime
from typing import List, Optional, Sequence, Set

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

# --- Subchapter heading patterns ---

# Pattern A: Roman numeral prefix, e.g. "I. THE WIZARD OF THE PYRENEES"
ROMAN_HEADING_RE = re.compile(r"^[IVXLCDM]+\.\s+[A-Z][A-Z0-9 .,;:'\-—–&]*$")

# Pattern B: Arabic numeral prefix, e.g. "1. SOME TITLE"
ARABIC_HEADING_RE = re.compile(r"^\d+\.\s+[A-Z][A-Z0-9 .,;:'\-—–&]*$")

# Pattern C (bare all-caps) is implemented in code, not a single regex.
SENTENCE_END_CHARS = {'.', '?', '!'}


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

def _optimal_chunk_count(total_words: float, config: ChunkingConfig) -> int:
    """Determine how many chunks a chapter should be split into.

    ``total_words`` is interpreted in *effective-word* space: when paragraph
    weights are applied it is ``Σ(words_p × m_p)``. With uniform weights
    (``m_p = 1.0``) effective words equal real words, so the result is
    byte-for-byte identical to the unweighted case. ``target_size`` /
    ``min_chunk_size`` / ``max_chunk_size`` stay scalar thresholds compared
    against the effective totals.
    """
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


def _is_subchapter_heading(para: str) -> bool:
    """
    Detect whether a paragraph is a subchapter heading.

    Matches one of three patterns:
      A. Roman numeral prefix, e.g. "I. THE WIZARD OF THE PYRENEES"
      B. Arabic numeral prefix, e.g. "1. SOME TITLE"
      C. Bare all-caps title (strict): 2-10 words, all letters uppercase,
         not ending in . ? !, not opening with a dialogue quote, not a scene break.
    """
    stripped = para.strip()
    if not stripped:
        return False

    if ROMAN_HEADING_RE.match(stripped) or ARABIC_HEADING_RE.match(stripped):
        return True

    if _is_scene_break(stripped):
        return False
    if stripped[0] in DIALOGUE_STARTERS:
        return False
    if stripped[-1] in SENTENCE_END_CHARS:
        return False

    words = stripped.split()
    if not (2 <= len(words) <= 10):
        return False

    letters = [c for c in stripped if c.isalpha()]
    if len(letters) < 4:
        return False
    if any(c.islower() for c in letters):
        return False

    return True


def _find_heading_anchors(
    paragraphs: List[str],
    para_words: List[int],
    config: ChunkingConfig
) -> List[int]:
    """
    Identify subchapter heading paragraphs that can serve as forced split points.

    A heading at index h is accepted as an anchor only when both the section
    that would end at h and the section that would start at h contain at least
    config.min_chunk_size words. Walks headings in order, greedily accepting
    feasible ones. Naturally excludes the chapter title at index 1 (only the
    "Chapter N" paragraph precedes it) and any heading too close to the end.
    """
    if len(paragraphs) < 2:
        return []

    n = len(paragraphs)
    prefix = [0] * (n + 1)
    for i, w in enumerate(para_words):
        prefix[i + 1] = prefix[i] + w

    anchors: List[int] = []
    last_anchor = 0

    for h in range(1, n):
        if not _is_subchapter_heading(paragraphs[h]):
            continue
        before = prefix[h] - prefix[last_anchor]
        after = prefix[n] - prefix[h]
        if before >= config.min_chunk_size and after >= config.min_chunk_size:
            anchors.append(h)
            last_anchor = h

    return anchors


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

        # --- Subchapter heading boundary ---
        # Helps DP prefer heading boundaries when subdividing oversized sections
        # (forced anchors are honored regardless via _find_optimal_splits).
        if _is_subchapter_heading(next_para):
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
    config: ChunkingConfig,
    forced_splits: Set[int] = None,
    para_effective: Optional[Sequence[float]] = None,
) -> List[int]:
    """
    Find optimal paragraph indices where new chunks start.

    Returns a list of start indices, e.g. [0, 12, 25] for 3 chunks.
    Uses dynamic programming to balance even sizing with split quality.

    If forced_splits is provided, any chunk straddling a forced split index
    is assigned infinite cost, forcing the DP to honor those boundaries.

    Sizing operates in *effective-word* space: ``size_cost`` measures each
    chunk's deviation from ``ideal_size`` using ``para_effective`` (= per-
    paragraph ``words × weight``) when provided, else the raw ``para_words``.
    Boundary scoring, forced anchors, and backtracking are unchanged, so
    ``para_effective=None`` reproduces the unweighted splits exactly.
    """
    n_paras = len(para_words)

    if n_chunks == 1:
        return [0]

    forced = forced_splits or set()
    # Sorted list of forced indices for fast straddle checks via bisect.
    forced_sorted = sorted(forced)

    # Precompute prefix sums for fast range word counts. When weights are
    # supplied, sums are over effective words so the DP balances effective
    # (not raw) sizes; uniform weights make this identical to para_words.
    size_basis = para_effective if para_effective is not None else para_words
    prefix = [0.0] * (n_paras + 1)
    for i in range(n_paras):
        prefix[i + 1] = prefix[i] + size_basis[i]

    def range_words(a: int, b: int) -> float:
        """Effective word count for paragraphs [a, b)."""
        return prefix[b] - prefix[a]

    weight = config.split_quality_weight

    def straddles_forced(a: int, b: int) -> bool:
        """True if any f in forced satisfies a < f < b."""
        if not forced_sorted:
            return False
        i = bisect.bisect_right(forced_sorted, a)
        return i < len(forced_sorted) and forced_sorted[i] < b

    def chunk_cost(a: int, b: int) -> float:
        """Cost of a chunk spanning paragraphs [a, b)."""
        if straddles_forced(a, b):
            return float('inf')

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
    chapter_id: str = "chapter_01",
    para_weights: Optional[List[float]] = None,
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
        para_weights: Optional per-paragraph density multipliers (1.0 = neutral,
            >1.0 packs tighter ⇒ smaller chunks, <1.0 looser ⇒ bigger chunks).
            Length must equal the chapter's paragraph count. When omitted (or
            uniform 1.0), sizing is identical to the unweighted behavior. The
            weights only steer *where* splits land; chunks are still built from
            the real paragraphs, so char offsets, overlap, and metadata are
            unaffected.

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

    # Effective-word space drives sizing. Uniform weights (or None) make
    # `effective == para_words`, reproducing today's behavior byte-for-byte.
    if para_weights is None:
        para_effective = None
        effective = para_words
    else:
        if len(para_weights) != len(all_paragraphs):
            raise ValueError(
                f"para_weights length ({len(para_weights)}) must equal paragraph "
                f"count ({len(all_paragraphs)}) for {chapter_id}"
            )
        effective = [w * m for w, m in zip(para_words, para_weights)]
        para_effective = effective
    total_effective = sum(effective)

    logger.info(
        f"Starting chunking for {chapter_id}: {len(all_paragraphs)} paragraphs, "
        f"{total_words} words"
        + ("" if para_weights is None else f", {total_effective:.0f} effective words")
    )

    # Phase 1: Determine chunk count (in effective-word space)
    n_chunks = _optimal_chunk_count(total_effective, config)

    # Phase 1b: Detect subchapter heading anchors and adjust chunk count to
    # honor them. Anchors are feasible heading boundaries (sections on either
    # side meet min_chunk_size).
    anchors = _find_heading_anchors(all_paragraphs, para_words, config)
    if anchors:
        # Each section between consecutive anchors (and the head/tail sections)
        # may need multiple chunks if it exceeds max_chunk_size.
        section_starts = [0] + anchors + [len(all_paragraphs)]
        n_for_max = 0
        for a, b in zip(section_starts, section_starts[1:]):
            section_words = sum(effective[a:b])
            n_for_max += max(1, math.ceil(section_words / config.max_chunk_size))
        n_chunks = max(n_chunks, len(anchors) + 1, n_for_max)

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

    # Phase 3: Find optimal splits (in effective-word space)
    ideal_size = total_effective / n_chunks
    split_indices = _find_optimal_splits(
        para_words, scores, n_chunks, ideal_size, config,
        forced_splits=set(anchors) if anchors else None,
        para_effective=para_effective,
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
