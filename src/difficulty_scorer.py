"""
Deterministic translation-difficulty scoring for English source text.

Rates how hard a block of English text will be to translate into Spanish using
two orthogonal, deterministic signals:

1. **Sentence length (long-tail-weighted)** — long sentences carry more
   subordinate clauses; EN→ES translation often reorders clauses / triggers the
   subjunctive, so long sentences are where an LLM is most likely to drop or
   mangle content. We use a word-weighted mean ``Σ(len²)/Σ(len)`` so the long
   tail dominates, and also record the plain mean and p90 for transparency.
2. **Lexical rarity (wordfreq Zipf)** — rare / archaic / technical words are
   where the LLM is least certain of the Spanish equivalent. We aggregate the
   fraction of scored tokens whose Zipf frequency falls below ``RARE_ZIPF``.
   **Glossary terms are excluded** so recurring proper names (Betsy,
   Aunt Harriet) — which are handled deterministically by the glossary and would
   otherwise look "rare" — don't inflate the stat.

Each sub-metric is mapped to ``[0, 1]`` via fixed (absolute) calibration
thresholds and combined into a single ``difficulty`` score, which maps to a
suggested chunk ``target_size`` (harder ⇒ smaller). Scores are produced at the
book and per-chapter level and cached to ``{project_dir}/difficulty.json``
(re-run when the source mtime is newer), mirroring ``text_feature_detector``.

Phase 1: book + chapter suggestions only. The suggestions populate the existing
dashboard target inputs; nothing is auto-applied. Per-paragraph weights
(``para_weights`` fed to the chunker DP) are a deferred phase.

The calibration constants below are deliberate, tunable starting points — run
``scripts/score_difficulty.py`` across a few books and adjust.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from src.sentence_aligner import split_sentences
from src.utils.source_text import load_chapter_source_text, load_clean_source_text
from src.utils.text_utils import count_words

logger = logging.getLogger(__name__)

try:
    from wordfreq import zipf_frequency as _zipf_frequency

    WORDFREQ_AVAILABLE = True
except ImportError:  # pragma: no cover - wordfreq is a declared dependency
    WORDFREQ_AVAILABLE = False


# ---------------------------------------------------------------------------
# Calibration constants (tunable)
# ---------------------------------------------------------------------------

# Sentence-length sub-score: weighted words/sentence at or below LENGTH_EASY
# scores 0.0; at or above LENGTH_HARD scores 1.0; linear between.
LENGTH_EASY = 18.0
LENGTH_HARD = 32.0

# Lexical-rarity sub-score: a token whose Zipf frequency is below RARE_ZIPF is
# "rare". The fraction of rare tokens at or below RARITY_EASY scores 0.0; at or
# above RARITY_HARD scores 1.0; linear between.
RARE_ZIPF = 3.0
RARITY_EASY = 0.015
RARITY_HARD = 0.10

# Relative weights of the two sub-scores in the combined difficulty. Length is
# the stronger signal (it tracks human difficulty ordering best); rarity is a
# lighter tiebreaker that pushes rare-vocabulary books down further.
WEIGHT_LENGTH = 0.85
WEIGHT_RARITY = 0.15

# difficulty → suggested chunk target_size (words). difficulty 0.0 yields
# TARGET_EASY (bigger chunks), 1.0 yields TARGET_HARD (smaller chunks).
# Calibrated so an easy book lands on the standard 2000-word default and the
# hardest books fall toward ~1300.
TARGET_EASY = 2000
TARGET_HARD = 1260

# Token pattern for rarity: alphabetic runs with optional internal apostrophe
# (don't, O'Hara). Lowercased before lookup. Digits/punctuation are ignored.
_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")


def calibration() -> dict:
    """Return the calibration constants used to produce a score.

    Stored alongside every manifest so a re-tuning session can see exactly what
    thresholds/weights generated the cached numbers.
    """
    return {
        "length_easy": LENGTH_EASY,
        "length_hard": LENGTH_HARD,
        "rare_zipf": RARE_ZIPF,
        "rarity_easy": RARITY_EASY,
        "rarity_hard": RARITY_HARD,
        "weight_length": WEIGHT_LENGTH,
        "weight_rarity": WEIGHT_RARITY,
        "target_easy": TARGET_EASY,
        "target_hard": TARGET_HARD,
        "wordfreq_available": WORDFREQ_AVAILABLE,
    }


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DifficultyMetrics:
    """Difficulty scores + supporting raw stats for one block of text."""

    sentence_count: int = 0
    word_count: int = 0
    mean_sentence_length: float = 0.0
    p90_sentence_length: float = 0.0
    sentence_length_weighted: float = 0.0
    tokens_scored: int = 0
    rare_word_fraction: float = 0.0
    length_score: float = 0.0
    rarity_score: float = 0.0
    difficulty: float = 0.0
    suggested_target_size: int = TARGET_EASY

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DifficultyMetrics":
        names = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in (data or {}).items() if k in names})


@dataclass
class ChapterDifficulty:
    """A chapter id paired with its difficulty metrics."""

    chapter_id: str
    metrics: DifficultyMetrics

    def to_dict(self) -> dict:
        return {"chapter_id": self.chapter_id, "metrics": self.metrics.to_dict()}

    @classmethod
    def from_dict(cls, data: dict) -> "ChapterDifficulty":
        return cls(
            chapter_id=data.get("chapter_id", ""),
            metrics=DifficultyMetrics.from_dict(data.get("metrics", {})),
        )


@dataclass
class DifficultyManifest:
    """Book-level + per-chapter difficulty results for a project."""

    generated_at: str
    book: DifficultyMetrics
    chapters: List[ChapterDifficulty] = field(default_factory=list)
    source_mtime: Optional[float] = None
    calibration: dict = field(default_factory=calibration)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "source_mtime": self.source_mtime,
            "calibration": self.calibration,
            "book": self.book.to_dict(),
            "chapters": [c.to_dict() for c in self.chapters],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DifficultyManifest":
        return cls(
            generated_at=data.get("generated_at", ""),
            source_mtime=data.get("source_mtime"),
            calibration=data.get("calibration") or {},
            book=DifficultyMetrics.from_dict(data.get("book", {})),
            chapters=[
                ChapterDifficulty.from_dict(c) for c in data.get("chapters", [])
            ],
        )


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _linear_score(value: float, easy: float, hard: float) -> float:
    """Map ``value`` to ``[0, 1]``: ``<=easy`` → 0, ``>=hard`` → 1, linear between."""
    if hard <= easy:
        return 0.0
    return _clamp01((value - easy) / (hard - easy))


def _zipf(word: str) -> float:
    """Zipf frequency of ``word`` (0–8). Returns a neutral-common value when
    wordfreq is unavailable so nothing is counted as rare (rarity degrades to 0).
    """
    if not WORDFREQ_AVAILABLE:
        return 8.0
    return _zipf_frequency(word, "en")


def build_glossary_skip(glossary) -> set:
    """Build the set of lowercased tokens to exclude from rarity scoring.

    ``glossary`` is a :class:`src.models.Glossary` (or ``None``). Every word in
    each term's ``english`` string is added — multi-word terms contribute each
    constituent token (``"Aunt Harriet"`` → ``{"aunt", "harriet"}``). Tokenized
    with the same pattern used on the text so membership tests line up.
    """
    skip: set = set()
    if glossary is None:
        return skip
    for term in getattr(glossary, "terms", []) or []:
        english = getattr(term, "english", "") or ""
        for tok in _WORD_RE.findall(english.lower()):
            skip.add(tok)
    return skip


def _sentence_word_counts(text: str) -> List[int]:
    """Word count per sentence (drops empties)."""
    counts = [count_words(s) for s in split_sentences(text, "en")]
    return [c for c in counts if c > 0]


def _weighted_mean_sentence_length(counts: List[int]) -> float:
    """Word-weighted mean sentence length ``Σ(len²)/Σ(len)``.

    Emphasizes the long tail: one 45-word sentence weighs far more than several
    short lines, which matches where translation actually gets hard.
    """
    total = sum(counts)
    if total == 0:
        return 0.0
    return sum(c * c for c in counts) / total


def _percentile(sorted_counts: List[int], q: float) -> float:
    """Nearest-rank percentile of an ascending-sorted list (q in [0, 1])."""
    if not sorted_counts:
        return 0.0
    idx = int(round(q * (len(sorted_counts) - 1)))
    idx = max(0, min(len(sorted_counts) - 1, idx))
    return float(sorted_counts[idx])


def suggest_target_size(
    difficulty: float,
    easy_target: int = TARGET_EASY,
    hard_target: int = TARGET_HARD,
) -> int:
    """Map a ``difficulty`` in ``[0, 1]`` to a suggested chunk ``target_size``.

    Linear interpolation: ``0.0`` → ``easy_target`` (bigger chunks), ``1.0`` →
    ``hard_target`` (smaller chunks). Snapped to the ChunkingConfig floor (100).
    """
    d = _clamp01(difficulty)
    size = easy_target + (hard_target - easy_target) * d
    return max(100, int(round(size)))


def score_text(text: str, glossary_skip: Optional[set] = None) -> DifficultyMetrics:
    """Score one block of English text (a chapter, or the whole book joined).

    ``glossary_skip`` is a set of lowercased tokens (from
    :func:`build_glossary_skip`) excluded from the rarity calculation.
    """
    skip = glossary_skip or set()

    counts = _sentence_word_counts(text)
    word_count = count_words(text)
    if counts:
        weighted = _weighted_mean_sentence_length(counts)
        mean = sum(counts) / len(counts)
        p90 = _percentile(sorted(counts), 0.9)
    else:
        weighted = mean = p90 = 0.0

    tokens = [
        t for t in (w.lower() for w in _WORD_RE.findall(text)) if t not in skip
    ]
    tokens_scored = len(tokens)
    if tokens_scored:
        freq = Counter(tokens)
        rare = sum(count for tok, count in freq.items() if _zipf(tok) < RARE_ZIPF)
        rare_fraction = rare / tokens_scored
    else:
        rare_fraction = 0.0

    length_score = _linear_score(weighted, LENGTH_EASY, LENGTH_HARD)
    rarity_score = _linear_score(rare_fraction, RARITY_EASY, RARITY_HARD)
    denom = WEIGHT_LENGTH + WEIGHT_RARITY
    difficulty = (
        _clamp01((WEIGHT_LENGTH * length_score + WEIGHT_RARITY * rarity_score) / denom)
        if denom
        else 0.0
    )

    return DifficultyMetrics(
        sentence_count=len(counts),
        word_count=word_count,
        mean_sentence_length=round(mean, 2),
        p90_sentence_length=round(p90, 2),
        sentence_length_weighted=round(weighted, 2),
        tokens_scored=tokens_scored,
        rare_word_fraction=round(rare_fraction, 4),
        length_score=round(length_score, 4),
        rarity_score=round(rarity_score, 4),
        difficulty=round(difficulty, 4),
        suggested_target_size=suggest_target_size(difficulty),
    )


# ---------------------------------------------------------------------------
# Book-level scoring with on-disk caching
# ---------------------------------------------------------------------------


def manifest_path(project_dir: Path) -> Path:
    return Path(project_dir) / "difficulty.json"


def _save_manifest(manifest: DifficultyManifest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest.to_dict(), fh, ensure_ascii=False, indent=2)


def _load_manifest(path: Path) -> Optional[DifficultyManifest]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return DifficultyManifest.from_dict(json.load(fh))
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        logger.warning("Failed to load difficulty manifest at %s: %s", path, exc)
        return None


def _load_glossary_skip(project_dir: Path) -> set:
    """Load the project's glossary (if any) and build the rarity skip-set."""
    glossary_path = Path(project_dir) / "glossary.json"
    if not glossary_path.exists():
        return set()
    try:
        from src.utils.file_io import load_glossary

        return build_glossary_skip(load_glossary(glossary_path))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to load glossary for %s: %s", project_dir, exc)
        return set()


def _chapter_texts(project_dir: Path) -> tuple:
    """Return ``(chapter_id_text_pairs, max_mtime)`` for a project.

    Uses ``load_chapter_source_text`` so the English source is read even on
    projects whose ``chapters/`` files were overwritten with translations.
    """
    chapters_dir = Path(project_dir) / "chapters"
    pairs: list = []
    max_mtime: Optional[float] = None
    if not chapters_dir.exists():
        return pairs, max_mtime
    for ch_file in sorted(chapters_dir.glob("chapter_*.txt")):
        ch_id = ch_file.stem
        text, mtime, _kind = load_chapter_source_text(project_dir, ch_id)
        if not text:
            try:
                text = ch_file.read_text(encoding="utf-8")
                mtime = ch_file.stat().st_mtime
            except OSError as exc:  # pragma: no cover - defensive
                logger.warning("Failed to read chapter %s: %s", ch_file, exc)
                continue
        pairs.append((ch_id, text))
        if mtime is not None:
            max_mtime = mtime if max_mtime is None else max(max_mtime, mtime)
    return pairs, max_mtime


def score_book(project_dir, *, force: bool = False) -> DifficultyManifest:
    """Score a project's book and chapters, with on-disk caching.

    Args:
        project_dir: Project directory containing ``chapters/`` (and optionally
            ``glossary.json``).
        force: Re-score even if a fresh cached manifest exists.

    The manifest is cached to ``{project_dir}/difficulty.json`` and reused when
    its ``source_mtime`` is at least as new as the current source mtime.
    """
    project_dir = Path(project_dir)
    cache_path = manifest_path(project_dir)

    chapter_pairs, source_mtime = _chapter_texts(project_dir)
    if source_mtime is None:
        # No chapters/ — fall back to the whole-book source for the book score.
        _book_text, source_mtime, _kind = load_clean_source_text(project_dir)

    if not force:
        cached = _load_manifest(cache_path)
        if cached is not None:
            cached_mtime = cached.source_mtime or 0.0
            if source_mtime is None or cached_mtime >= source_mtime:
                logger.info("Using cached difficulty manifest at %s", cache_path)
                return cached

    glossary_skip = _load_glossary_skip(project_dir)

    chapters: List[ChapterDifficulty] = []
    for ch_id, text in chapter_pairs:
        chapters.append(
            ChapterDifficulty(ch_id, score_text(text, glossary_skip=glossary_skip))
        )

    if chapter_pairs:
        book_text = "\n\n".join(text for _id, text in chapter_pairs)
    else:
        book_text, _mtime, _kind = load_clean_source_text(project_dir)
    book_metrics = score_text(book_text, glossary_skip=glossary_skip)

    manifest = DifficultyManifest(
        generated_at=datetime.now(timezone.utc).isoformat(),
        book=book_metrics,
        chapters=chapters,
        source_mtime=source_mtime,
    )

    try:
        _save_manifest(manifest, cache_path)
    except OSError as exc:  # pragma: no cover - best effort
        logger.warning("Failed to cache difficulty manifest at %s: %s", cache_path, exc)

    return manifest
