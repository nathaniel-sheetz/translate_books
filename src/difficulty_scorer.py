"""
Deterministic translation-difficulty scoring for English source text.

Rates how hard a block of English text will be to translate into Spanish using
five peer sub-scores, each mapped to ``[0, 1]`` via fixed calibration
thresholds and combined with a **weighted power-mean** (Hölder mean):

1. **Sentence length (long-tail-weighted)** — long sentences carry more
   subordinate clauses; EN→ES translation often reorders clauses / triggers the
   subjunctive. We use a word-weighted mean ``Σ(len²)/Σ(len)`` so the long
   tail dominates.
2. **Lexical rarity (wordfreq Zipf)** — rare / archaic / technical words are
   where the LLM is least certain of the Spanish equivalent. **Glossary terms
   are excluded** so recurring proper names don't inflate the stat.
3. **Dialect density (eye-dialect markers)** — non-standard / dialect speech
   (``jes'``, ``comin'``, ``a-thinkin'``, ``'twas``, ``reckon``) tracked via
   deterministic eye-dialect markers normalized by word count.
4. **Nested dialogue density** — opening double-quote speech plus nested
   single-quote quotations inside speech (``"He said 'hello' to her."``) that
   become ``« »`` in Spanish. Nested openings are weighted far more heavily
   than plain double-quote dialogue.
5. **Verse / poetry density** — short non-terminal line runs detected by the
   existing ``detect_verse`` heuristic, normalized by non-blank line count.

The power-mean knob ``AGGREGATION_P`` (default 3) lets a single extreme hazard
pull difficulty up without inflating its weight; ``p=1`` reduces to a plain
weighted mean. The combined ``difficulty`` maps to a suggested chunk
``target_size`` (harder ⇒ smaller). Scores are produced at the book and
per-chapter level and cached to ``{project_dir}/difficulty.json`` (re-run when
the source mtime is newer or the calibration changes).

Phase 1: book + chapter suggestions only. Per-paragraph weights
(``para_weights`` fed to the chunker DP) are a deferred phase.

The calibration constants below are deliberate, tunable starting points — run
``scripts/score_difficulty.py`` across a few books and adjust.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from src.sentence_aligner import split_sentences
from src.text_feature_detector import detect_verse
from src.utils.file_io import load_glossary
from src.utils.source_text import load_chapter_source_text, load_clean_source_text
from src.utils.text_utils import count_words, extract_paragraphs, normalize_newlines

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

# Relative weights of the five peer sub-scores (sum to 1.0). Length is the
# strongest signal; rarity is a lighter tiebreaker; dialect, dialogue, and verse
# capture orthogonal translation hazards.
WEIGHT_LENGTH = 0.35
WEIGHT_RARITY = 0.10
WEIGHT_DIALECT = 0.25
WEIGHT_DIALOGUE = 0.15
WEIGHT_VERSE = 0.15

# Power-mean exponent for aggregating sub-scores. p=1 is a plain weighted mean;
# p>1 lets a single extreme hazard pull difficulty up without inflating weight.
AGGREGATION_P = 3.0

# Dialogue-density sub-score: weighted count of opening double-quotes and nested
# single-quotes per word. At or below DIALOGUE_EASY scores 0.0; at or above
# DIALOGUE_HARD scores 1.0; linear between.
DIALOGUE_EASY = 0.01
DIALOGUE_HARD = 0.08
DQ_COEF = 1.0
NESTED_COEF = 6.0

# Verse-density sub-score: fraction of non-blank lines that look like verse
# (via detect_verse). At or below VERSE_EASY scores 0.0; at or above VERSE_HARD
# scores 1.0; linear between.
VERSE_EASY = 0.0
VERSE_HARD = 0.25

# difficulty → suggested chunk target_size (words). difficulty 0.0 yields
# TARGET_EASY (bigger chunks), 1.0 yields TARGET_HARD (smaller chunks).
# Widened to 2200→900 (from the old 2000→1200 range) so the scale still spreads
# once all five signals participate as peers rather than additive boosts.
TARGET_EASY = 2200
TARGET_HARD = 900

# Token pattern for rarity: alphabetic runs with optional internal apostrophe
# (don't, O'Hara). Lowercased before lookup. Digits/punctuation are ignored.
_WORD_RE = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)*")

# Dialect-density sub-score: fraction of word-tokens that are eye-dialect
# markers. At or below DIALECT_EASY scores 0.0; at or above DIALECT_HARD scores
# 1.0; linear between. Calibrated on stormy-misty-s-foal, whose dialect chapters
# run ~2–5% markers.
DIALECT_EASY = 0.0
DIALECT_HARD = 0.035

# Normalized weight vector for the five peer sub-scores (entries sum to 1.0).
WEIGHTS = {
    "length": WEIGHT_LENGTH,
    "rarity": WEIGHT_RARITY,
    "dialect": WEIGHT_DIALECT,
    "dialogue": WEIGHT_DIALOGUE,
    "verse": WEIGHT_VERSE,
}

# ---------------------------------------------------------------------------
# Dialect detection (eye-dialect markers)
# ---------------------------------------------------------------------------

# Standard English contractions. Any apostrophe-bearing token *outside* this
# closed, stable set (and not a possessive) is treated as a dialect marker.
STANDARD_CONTRACTIONS = frozenset({
    "don't", "doesn't", "didn't", "won't", "wouldn't", "can't", "couldn't",
    "shouldn't", "mustn't", "needn't", "mightn't", "oughtn't", "daren't",
    "mayn't", "isn't", "aren't", "wasn't", "weren't", "hasn't", "haven't",
    "hadn't", "shan't", "you're", "we're", "they're", "i've", "you've", "we've",
    "they've", "would've", "should've", "could've", "i'll", "you'll", "he'll",
    "she'll", "we'll", "they'll", "it'll", "that'll", "there'll", "where'll",
    "who'll", "what'll", "i'd", "you'd", "he'd",
    "she'd", "we'd", "they'd", "it'd", "that'd", "i'm", "it's", "he's", "she's",
    "that's", "what's", "who's", "there's", "here's", "let's", "where's",
    "how's", "o'clock", "ma'am",
})

# Common eye-dialect trailing reductions (token ends in a bare apostrophe).
# Trailing apostrophes are otherwise ambiguous with closing single-quotes
# (``separate'``) and plural possessives (``horses'``), so non-``-in'`` g-drops
# are matched against this curated set rather than counted wholesale. Clearly
# extensible — grow as new books surface reductions.
_TRAILING_REDUCTIONS = frozenset({
    "o'", "an'", "ol'", "jes'", "jus'", "mo'", "wi'", "t'", "d'", "sho'",
    "fo'", "yo'",
})

# Leading-apostrophe elisions — a curated closed set. Leading apostrophes are
# otherwise ambiguous with closing quotes, so a general pattern is unsafe.
_LEADING_ELISIONS = frozenset({
    "'twas", "'tis", "'em", "'cause", "'round", "'bout", "'nuff",
    "'fraid", "'gainst", "'neath", "'cept", "'peared", "'specially",
    # "'twasn't" omitted: its internal apostrophe is matched by the
    # apostrophe-token rule first; the leading-apostrophe RE only sees "'twasn".
})

# Small curated apostrophe-free dialect lexicon. Clearly extensible starter set
# — grow it as new books surface markers. Lowercased before lookup.
_DIALECT_LEXICON = frozenset({
    "reckon", "yonder", "naw", "yep", "yup", "nope", "gonna", "gotta", "wanna",
    "gimme", "lemme", "dunno", "kinda", "sorta", "critter", "varmint",
    "vittles", "hoss", "hosses", "nacherel", "acrost", "wimmenfolk", "menfolk",
    "afeared", "onliest", "heerd", "deef", "nary", "yer", "ye", "younguns",
})

# Apostrophe-bearing token: an alphabetic run, an apostrophe (straight or
# curly), then an optional alphabetic run. Catches don't, comin', jes', off'n,
# young'un, smarter'n, ain't, o'.
_APOSTROPHE_TOKEN_RE = re.compile("[A-Za-z]+['‘’][A-Za-z]*")

# Leading-apostrophe token (for matching the curated elision set). The leading
# apostrophe may be a straight or curly quote.
_LEADING_APOSTROPHE_RE = re.compile("['‘’][A-Za-z]+")

# a-prefixed progressives: a-thinkin', a-ridin', a-walking. Hyphen may be a
# plain or non-breaking hyphen; the verb ends in -in, optionally + g/'/’.
_A_PREFIX_RE = re.compile(r"\ba[-‑][a-z]+in[g'’]?\b", re.IGNORECASE)

# Plain-word token (no apostrophes) for lexicon membership tests.
_PLAIN_WORD_RE = re.compile(r"[A-Za-z]+")


def _norm_apos(s: str) -> str:
    """Normalize typographic apostrophes to a straight ``'``.

    Most typeset books use the curly apostrophe ``’`` (U+2019), so ``don’t``
    must compare equal to the whitelisted ``don't``. The regexes already match
    both forms; this keeps the whitelist/elision lookups in step.
    """
    return s.replace("‘", "'").replace("’", "'").replace("ʼ", "'")


def _is_possessive(token: str) -> bool:
    """True for ``'s`` possessives (``Misty's``, ``Grandpa's``).

    These carry an apostrophe but are standard English, not dialect. Only the
    ``'s`` form is excluded — a bare trailing apostrophe is the g-drop signal
    (``comin'``, ``jes'``, ``o'``), which must stay counted.
    """
    low = _norm_apos(token.lower())
    return low.endswith("'s")


def _is_proper_name_prefix(token: str) -> bool:
    """True for Irish/Scottish surname prefixes: ``O'Brien``, ``O'Hara``.

    Pattern: single uppercase letter before the apostrophe, capitalized word
    after. These are standard proper nouns, not eye-dialect.
    """
    norm = _norm_apos(token)
    idx = norm.find("'")
    if idx < 0:
        return False
    prefix, suffix = norm[:idx], norm[idx + 1:]
    return len(prefix) == 1 and prefix.isupper() and bool(suffix) and suffix[0].isupper()


def dialect_marker_count(text: str) -> int:
    """Count deterministic eye-dialect markers in ``text``.

    Whitelist-based and dependency-free. Counts, once per occurrence:

    * **Internal-apostrophe tokens** (letters on both sides) not in
      :data:`STANDARD_CONTRACTIONS` and not a ``'s`` possessive — catches
      mid-word elisions (``off'n``), non-standard contractions (``ain't``,
      ``young'un``, ``t'other``), reliably (a closing quote can't be internal).
    * **Trailing-apostrophe tokens** only when they are ``-in'`` g-drops
      (``comin'``, ``standin'``) or a curated reduction in
      :data:`_TRAILING_REDUCTIONS` (``o'``, ``jes'``). Bare trailing apostrophes
      are otherwise ambiguous with closing single-quotes (``separate'``) and
      plural possessives (``horses'``), which must not count.
    * **Leading-apostrophe elisions** in :data:`_LEADING_ELISIONS` (``'twas``,
      ``'em``).
    * **a-prefixed progressives** (``a-thinkin'``, ``a-ridin'``).
    * **Curated apostrophe-free lexicon** hits (``reckon``, ``yonder``,
      ``nacherel``).

    Each textual occurrence is counted at most once: a marker matched by more
    than one rule (e.g. ``a-thinkin'`` hits both the a-prefix and the
    apostrophe-token rule) does not double-count. Magnitude is normalized to a
    density by the caller, so nothing is capped.
    """
    counted: List[tuple] = []  # (start, end) spans already counted

    def _take(start: int, end: int) -> int:
        for s, e in counted:
            if start < e and s < end:  # overlaps an already-counted span
                return 0
        counted.append((start, end))
        return 1

    count = 0

    # Apostrophe-bearing tokens. Internal apostrophes are reliable; bare
    # trailing apostrophes are filtered to real g-drops / reductions.
    for m in _APOSTROPHE_TOKEN_RE.finditer(text):
        low = _norm_apos(m.group().lower())
        if low in STANDARD_CONTRACTIONS:
            continue
        if low.endswith("'"):
            if not (low.endswith("in'") or low in _TRAILING_REDUCTIONS):
                continue
        elif _is_possessive(m.group()):
            continue
        elif _is_proper_name_prefix(m.group()):
            continue
        count += _take(m.start(), m.end())

    # Leading-apostrophe elisions (curated set only — avoids closing-quote noise).
    for m in _LEADING_APOSTROPHE_RE.finditer(text):
        if _norm_apos(m.group().lower()) in _LEADING_ELISIONS:
            count += _take(m.start(), m.end())

    # a-prefixed progressives (a-walking has no apostrophe, so this rule earns
    # its keep; a-thinkin' overlaps the apostrophe rule above and won't recount).
    for m in _A_PREFIX_RE.finditer(text):
        count += _take(m.start(), m.end())

    # Curated apostrophe-free lexicon.
    for m in _PLAIN_WORD_RE.finditer(text):
        if m.group().lower() in _DIALECT_LEXICON:
            count += _take(m.start(), m.end())

    return count


# ---------------------------------------------------------------------------
# Dialogue detection (nested-quote openings)
# ---------------------------------------------------------------------------

# Opening double-quote: straight " (U+0022) or left curly " (U+201C).
_DOUBLE_QUOTE_RE = re.compile(r'["\u201c]')

# Opening nested single-quote inside speech: straight ' (U+0027) or left curly '
# (U+2018), followed by a word character.
_NESTED_OPEN_RE = re.compile(r"['\u2018](?=\w)")

# Token after a nested opening quote (for elision filtering).
_NESTED_TOKEN_RE = re.compile(r"['\u2018][A-Za-z]+")

_DOUBLE_OPEN_BEFORE = frozenset(" \t\n\r(-\u2014\u2013")


def _is_double_quote_open(text: str, pos: int) -> bool:
    """True when ``text[pos]`` is an opening double-quote."""
    if pos > 0 and text[pos - 1] not in _DOUBLE_OPEN_BEFORE:
        return False
    nxt = pos + 1
    return nxt < len(text) and not text[nxt].isspace()


def _is_nested_quote_open(text: str, pos: int) -> bool:
    """True when ``text[pos]`` is an opening nested single-quote.

    Calibration tradeoffs for the American children's-book corpus this targets,
    where nested single-quotes are almost always quotations *inside* double-quote
    speech:
      * British-style primary dialogue (``'Hello,' she said.``) is counted as a
        nested opening \u2014 acceptable since such texts still carry real ``\u00ab \u00bb``
        translation work; it just credits it to this signal rather than the
        double-quote one.
      * Only U+0027 and left-curly U+2018 are treated as openings; a source that
        uses right-curly U+2019 as an *opening* single-quote is under-counted.
      * A ``'89``-style year elision (digit after the apostrophe) slips past the
        ``[A-Za-z]+`` elision filter in :func:`dialogue_marker_counts` and adds a
        spurious count \u2014 rare enough in this corpus to ignore.
    """
    if pos == 0:
        return True
    prev = text[pos - 1]
    return prev in _DOUBLE_OPEN_BEFORE or prev in '"\u201c\u201d'


def dialogue_marker_counts(text: str) -> tuple[int, int]:
    """Count opening double-quotes and nested single-quote openings.

    Returns ``(double_open, nested_open)``. Nested openings skip tokens in
    :data:`_LEADING_ELISIONS` so they don't overlap the dialect signal.
    """
    double_open = sum(
        1 for m in _DOUBLE_QUOTE_RE.finditer(text) if _is_double_quote_open(text, m.start())
    )
    nested_open = 0
    for m in _NESTED_OPEN_RE.finditer(text):
        if not _is_nested_quote_open(text, m.start()):
            continue
        token_m = _NESTED_TOKEN_RE.match(text, m.start())
        if token_m and _norm_apos(token_m.group().lower()) in _LEADING_ELISIONS:
            continue
        nested_open += 1
    return double_open, nested_open


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
        "dialect_easy": DIALECT_EASY,
        "dialect_hard": DIALECT_HARD,
        "dialogue_easy": DIALOGUE_EASY,
        "dialogue_hard": DIALOGUE_HARD,
        "dq_coef": DQ_COEF,
        "nested_coef": NESTED_COEF,
        "verse_easy": VERSE_EASY,
        "verse_hard": VERSE_HARD,
        "weight_length": WEIGHT_LENGTH,
        "weight_rarity": WEIGHT_RARITY,
        "weight_dialect": WEIGHT_DIALECT,
        "weight_dialogue": WEIGHT_DIALOGUE,
        "weight_verse": WEIGHT_VERSE,
        "aggregation_p": AGGREGATION_P,
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
    dialect_marker_count: int = 0
    dialect_density: float = 0.0
    double_quote_count: int = 0
    nested_quote_count: int = 0
    dialogue_density: float = 0.0
    verse_line_count: int = 0
    verse_density: float = 0.0
    length_score: float = 0.0
    rarity_score: float = 0.0
    dialect_score: float = 0.0
    dialogue_score: float = 0.0
    verse_score: float = 0.0
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


def _aggregate_difficulty(
    scores: dict[str, float],
    weights: dict[str, float],
    p: float,
) -> float:
    """Weighted power-mean (Hölder mean) of sub-scores in ``[0, 1]``."""
    if abs(p - 1.0) < 1e-9:
        return sum(weights[k] * scores[k] for k in scores)
    total = sum(weights[k] * (scores[k] ** p) for k in scores)
    return total ** (1.0 / p)


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

    marker_count = dialect_marker_count(text)
    dialect_density = marker_count / max(word_count, 1)

    double_open, nested_open = dialogue_marker_counts(text)
    dialogue_density = (
        DQ_COEF * double_open + NESTED_COEF * nested_open
    ) / max(word_count, 1)

    # detect_verse ignores its first arg and re-derives lines from the full
    # text, so extract_paragraphs here is nominal — kept to match the detector's
    # signature in case it starts consuming paragraphs. non_blank_lines is
    # computed independently for the density denominator.
    paragraphs = extract_paragraphs(text)
    non_blank_lines = sum(
        1 for line in normalize_newlines(text).split("\n") if line.strip()
    )
    verse_result = detect_verse(paragraphs, text)
    verse_density = verse_result.count / max(non_blank_lines, 1)

    length_score = _linear_score(weighted, LENGTH_EASY, LENGTH_HARD)
    rarity_score = _linear_score(rare_fraction, RARITY_EASY, RARITY_HARD)
    dialect_score = _linear_score(dialect_density, DIALECT_EASY, DIALECT_HARD)
    dialogue_score = _linear_score(dialogue_density, DIALOGUE_EASY, DIALOGUE_HARD)
    verse_score = _linear_score(verse_density, VERSE_EASY, VERSE_HARD)

    scores = {
        "length": length_score,
        "rarity": rarity_score,
        "dialect": dialect_score,
        "dialogue": dialogue_score,
        "verse": verse_score,
    }
    difficulty = _clamp01(_aggregate_difficulty(scores, WEIGHTS, AGGREGATION_P))

    return DifficultyMetrics(
        sentence_count=len(counts),
        word_count=word_count,
        mean_sentence_length=round(mean, 2),
        p90_sentence_length=round(p90, 2),
        sentence_length_weighted=round(weighted, 2),
        tokens_scored=tokens_scored,
        rare_word_fraction=round(rare_fraction, 4),
        dialect_marker_count=marker_count,
        dialect_density=round(dialect_density, 4),
        double_quote_count=double_open,
        nested_quote_count=nested_open,
        dialogue_density=round(dialogue_density, 4),
        verse_line_count=verse_result.count,
        verse_density=round(verse_density, 4),
        length_score=round(length_score, 4),
        rarity_score=round(rarity_score, 4),
        dialect_score=round(dialect_score, 4),
        dialogue_score=round(dialogue_score, 4),
        verse_score=round(verse_score, 4),
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
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest.to_dict(), fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load_manifest(path: Path) -> Optional[DifficultyManifest]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return DifficultyManifest.from_dict(json.load(fh))
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        logger.warning("Failed to load difficulty manifest at %s: %s", path, exc)
        return None


def load_manifest(project_dir) -> Optional[DifficultyManifest]:
    """Load the cached difficulty manifest for ``project_dir`` (or ``None``).

    Public accessor over the on-disk ``difficulty.json`` so callers (e.g. the
    harness chunk step) can read the per-chapter ``suggested_target_size`` the
    ``difficulty`` step produced without re-scoring or touching private helpers.
    """
    return _load_manifest(manifest_path(Path(project_dir)))


def _load_glossary_skip(project_dir: Path) -> set:
    """Load the project's glossary (if any) and build the rarity skip-set."""
    glossary_path = Path(project_dir) / "glossary.json"
    if not glossary_path.exists():
        return set()
    try:
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
            logger.warning("Skipping chapter %s: no English source text found", ch_id)
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
            fresh = source_mtime is None or cached_mtime >= source_mtime
            # Re-score when the calibration (algorithm/thresholds) changed, even
            # if the source is untouched — otherwise cached numbers go stale
            # silently after a re-tune.
            if fresh and cached.calibration == calibration():
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
