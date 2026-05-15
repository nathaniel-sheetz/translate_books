#!/usr/bin/env python3
"""
Extract glossary candidate terms from a source text file.

Analyzes a book's source text to find character names, place names, technical
terms, and uncommon words that are good candidates for a translation glossary.
Uses heuristic extraction (no LLM calls) to produce a ranked list for human
or LLM review.

Usage:
    python extract_glossary_candidates.py source.txt -o candidates.json
    python extract_glossary_candidates.py source.txt -o candidates.json -g glossary.json
    python extract_glossary_candidates.py source.txt -o candidates.json --min-frequency 3

First-run setup for rare-literary detection:
    python -c "import nltk; nltk.download('brown'); nltk.download('gutenberg')"
"""

import argparse
import json
import math
import pickle
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from pydantic import BaseModel, Field

from src.models import Glossary, GlossaryTermType
from src.utils.file_io import load_glossary
from src.utils.text_utils import count_words, normalize_newlines, strip_image_placeholders

try:
    import enchant
    ENCHANT_AVAILABLE = True
except ImportError:
    enchant = None
    ENCHANT_AVAILABLE = False

try:
    from nltk.corpus import brown, gutenberg
    from nltk import FreqDist
    NLTK_AVAILABLE = True
except ImportError:
    NLTK_AVAILABLE = False

try:
    from wordfreq import zipf_frequency as _wf_zipf
    WORDFREQ_AVAILABLE = True
except ImportError:
    WORDFREQ_AVAILABLE = False

CACHE_VERSION = 1
CACHE_PATH = Path.home() / ".cache" / "translate_books" / "literary_freqdist.pkl"

# Tokenization pattern matching dictionary_eval.py
TOKEN_PATTERN = re.compile(r"[\w'áéíóúüñÁÉÍÓÚÜÑ]+")

# Title words that indicate a character name follows
TITLE_WORDS = {
    "mr", "mrs", "ms", "miss", "dr", "prof", "professor",
    "uncle", "aunt", "auntie", "brother", "sister", "father", "mother",
    "sir", "lady", "lord", "king", "queen", "prince", "princess",
    "captain", "major", "colonel", "general", "sergeant",
    "saint", "st",
}

# Geographic prefix words that indicate a place name follows
GEO_WORDS = {
    "mount", "mt", "lake", "river", "cape", "bay", "gulf", "isle",
    "island", "valley", "fort", "port", "north", "south", "east", "west",
    "new", "old", "great", "upper", "lower",
}

# English stopwords for n-gram filtering
STOPWORDS = {
    "the", "a", "an", "in", "of", "to", "and", "is", "was", "were",
    "for", "with", "that", "this", "it", "on", "at", "by", "from",
    "or", "but", "not", "are", "be", "as", "has", "had", "have",
    "been", "will", "would", "could", "should", "may", "might",
    "shall", "can", "do", "did", "does", "its", "his", "her", "he",
    "she", "they", "them", "their", "we", "our", "you", "your",
    "my", "me", "him", "i", "no", "so", "if", "up", "out", "all",
    "one", "two", "who", "which", "when", "where", "how", "what",
    "than", "then", "there", "here", "very", "just", "about", "into",
    "over", "after", "before", "between", "through", "during", "without",
    "again", "each", "also", "more", "some", "any", "only", "other",
    "such", "these", "those", "same", "own", "too", "most", "s", "t",
    "yes", "oh", "ah", "well", "quite", "still", "already", "enough",
}

# Words that should not be part of multi-word proper noun sequences
# (common words that happen to be capitalized at sentence starts or as "I")
SEQUENCE_BREAKERS = {
    "i", "the", "a", "an", "it", "he", "she", "they", "we", "you",
    "if", "but", "and", "or", "so", "yet", "for", "nor",
    "this", "that", "these", "those", "there", "here",
    "is", "was", "are", "were", "be", "been", "has", "had", "have",
    "do", "did", "does", "will", "would", "could", "should",
    "not", "no", "all", "my", "his", "her", "its", "our", "your", "their",
    # Dialogue/response words that get capitalized at phrase starts
    "yes", "exactly", "oh", "ah", "well", "now", "then", "just",
    "too", "also", "still", "indeed", "certainly", "perhaps", "maybe",
    "to", "in", "on", "at", "by", "from", "with", "of", "up", "out",
}

# Dialogue attribution verbs (and other common adjacent words) that create
# false positive n-grams like "said Smith" or "while Smith".
DIALOGUE_VERBS = {
    "said", "asked", "replied", "answered", "cried", "exclaimed",
    "whispered", "shouted", "murmured", "continued", "added",
    "remarked", "observed", "explained", "declared", "repeated",
    "interrupted", "suggested", "inquired", "responded", "called",
    "interposed", "returned", "put", "took", "went", "began",
    "resumed", "protested", "objected", "insisted", "concluded",
    "queried", "ventured", "pursued", "urged", "asserted", "affirmed",
    "either", "whole", "case", "return", "leave", "while",
}

# Words that should never appear as glossary candidates (single-word or as
# any token in a multi-word n-gram). Days of the week and months of the year
# are universally known and don't need glossary entries.
BLOCKED_WORDS = {
    # Days of the week
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday",
    # Months of the year
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class GlossaryCandidate(BaseModel):
    """A candidate term for glossary inclusion."""
    term: str
    type_guess: GlossaryTermType
    frequency: int
    score: float = 0.0
    context_sentence: str = ""
    detection_reasons: list[str] = Field(default_factory=list)


class CandidateReport(BaseModel):
    """Output report of glossary candidate extraction."""
    source_file: str
    total_words: int
    total_unique_words: int
    candidates: list[GlossaryCandidate]
    excluded_glossary_terms: int = 0
    generated_at: datetime = Field(default_factory=datetime.now)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def split_into_sentences(text: str) -> list[str]:
    """Split text into sentences, handling common abbreviations."""
    # Split on sentence-ending punctuation followed by whitespace and uppercase
    # This avoids splitting on "Mr. Smith" or "U.S.A."
    parts = re.split(r'(?<=[.!?])\s+(?=[A-Z"\'])', text)
    # Also split on newlines that start new sentences
    sentences = []
    for part in parts:
        sub = re.split(r'\n+(?=[A-Z"\'])', part)
        sentences.extend(sub)
    return [s.strip() for s in sentences if s.strip()]


def tokenize(text: str) -> list[str]:
    """Extract word tokens from text."""
    return TOKEN_PATTERN.findall(text)


def is_special_case(word: str) -> bool:
    """Check if word should be ignored (numbers, single chars)."""
    if len(word) == 1 and word.lower() not in ('a', 'o', 'e', 'y', 'i'):
        return True
    if re.match(r'^\d+([.,]\d+)*$', word):
        return True
    return False


def get_context_sentence(term: str, sentences: list[str]) -> str:
    """Find the first sentence containing the term."""
    term_lower = term.lower()
    for sent in sentences:
        if term_lower in sent.lower():
            # Truncate long sentences
            if len(sent) > 200:
                idx = sent.lower().index(term_lower)
                start = max(0, idx - 80)
                end = min(len(sent), idx + len(term) + 80)
                return ("..." if start > 0 else "") + sent[start:end] + ("..." if end < len(sent) else "")
            return sent
    return ""


# ---------------------------------------------------------------------------
# Dictionary helper
# ---------------------------------------------------------------------------

class DictionaryChecker:
    """Wraps PyEnchant for English dictionary lookups."""

    def __init__(self):
        if not ENCHANT_AVAILABLE:
            self.english_dict = None
            return
        try:
            self.english_dict = enchant.Dict("en_US")
        except Exception:
            self.english_dict = None

    @property
    def available(self) -> bool:
        return self.english_dict is not None

    def is_english_word(self, word: str) -> bool:
        """Check if word is in the English dictionary."""
        if not self.english_dict:
            return False
        try:
            return self.english_dict.check(word) or self.english_dict.check(word.lower())
        except Exception:
            return False


class FrequencyChecker:
    """Literary-English frequency baseline (Brown fiction + Gutenberg),
    with wordfreq fallback for words the corpus doesn't cover.

    Higher Zipf = more common word. Range roughly 1.0 (very rare) to 7.0
    (the/and). Used to surface dictionary words that are rare in literary
    English (archaic, domain-specific, or character names that happen to
    collide with real words).
    """

    def __init__(self):
        self._fd = None
        self._total = 0
        self.available = NLTK_AVAILABLE or WORDFREQ_AVAILABLE
        if NLTK_AVAILABLE:
            try:
                self._fd, self._total = self._load_or_build()
            except LookupError:
                # Brown/Gutenberg not downloaded — fall back to wordfreq
                self._fd = None
                self._total = 0

    def _load_or_build(self):
        if CACHE_PATH.exists():
            try:
                blob = pickle.loads(CACHE_PATH.read_bytes())
                if blob.get("version") == CACHE_VERSION:
                    return blob["fd"], blob["total"]
            except Exception:
                pass  # fall through to rebuild
        words = (list(brown.words(categories=["fiction", "mystery",
                                              "romance", "humor"]))
                 + list(gutenberg.words()))
        fd = FreqDist(w.lower() for w in words if w.isalpha())
        total = sum(fd.values())
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_bytes(pickle.dumps(
            {"version": CACHE_VERSION, "fd": fd, "total": total}))
        return fd, total

    def literary_zipf(self, word: str) -> float:
        """Return literary-English Zipf. Higher = more common.
        Range roughly 1.0 (very rare) to 7.0 (the/and)."""
        w = word.lower()
        if self._fd is not None:
            count = self._fd[w]
            if count >= 3:
                return math.log10((count / self._total) * 1e9)
        if WORDFREQ_AVAILABLE:
            return _wf_zipf(w, "en")
        return 2.0  # last-resort: assume rare


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------

def extract_proper_nouns(
    sentences: list[str],
    dict_checker: DictionaryChecker,
    min_frequency: int,
) -> dict[str, GlossaryCandidate]:
    """Extract proper nouns and multi-word names from sentences."""
    # Track: term -> list of surface forms seen
    capitalized_occurrences: Counter = Counter()  # lowered term -> count of capitalized
    total_occurrences: Counter = Counter()  # lowered term -> total count
    first_forms: dict[str, str] = {}  # lowered -> first capitalized surface form
    detection_reasons_map: dict[str, list[str]] = {}
    type_guesses: dict[str, GlossaryTermType] = {}

    for sent in sentences:
        tokens = tokenize(sent)
        if not tokens:
            continue

        i = 0
        while i < len(tokens):
            token = tokens[i]

            # Skip special cases
            if is_special_case(token):
                i += 1
                continue

            # Check for capitalized word NOT at sentence start
            if (i > 0 and token[0].isupper()
                    and token.lower() not in SEQUENCE_BREAKERS
                    and token.lower() not in BLOCKED_WORDS):
                # Try to build a multi-word sequence
                sequence = [token]
                j = i + 1
                while (j < len(tokens)
                       and tokens[j][0].isupper()
                       and not is_special_case(tokens[j])
                       and tokens[j].lower() not in SEQUENCE_BREAKERS
                       and tokens[j].lower() not in BLOCKED_WORDS):
                    sequence.append(tokens[j])
                    j += 1

                # Record multi-word sequences (2+ words)
                if len(sequence) >= 2:
                    term = " ".join(sequence)
                    term_lower = term.lower()
                    capitalized_occurrences[term_lower] += 1
                    total_occurrences[term_lower] += 1
                    if term_lower not in first_forms:
                        first_forms[term_lower] = term
                        detection_reasons_map[term_lower] = ["capitalized_sequence"]
                    elif first_forms[term_lower].isupper() and not term.isupper():
                        first_forms[term_lower] = term

                    # Check for title prefix (token before the sequence)
                    if i >= 1 and tokens[i - 1].lower().rstrip('.') in TITLE_WORDS:
                        type_guesses[term_lower] = GlossaryTermType.CHARACTER
                        if "title_word_prefix" not in detection_reasons_map.get(term_lower, []):
                            detection_reasons_map.setdefault(term_lower, []).append("title_word_prefix")
                    elif tokens[i].lower().rstrip('.') in TITLE_WORDS:
                        # First word of sequence is a title word (e.g., "Uncle Paul")
                        type_guesses[term_lower] = GlossaryTermType.CHARACTER
                        if "title_word_prefix" not in detection_reasons_map.get(term_lower, []):
                            detection_reasons_map.setdefault(term_lower, []).append("title_word_prefix")
                    elif tokens[i].lower() in GEO_WORDS:
                        type_guesses.setdefault(term_lower, GlossaryTermType.PLACE)
                        if "geo_word_prefix" not in detection_reasons_map.get(term_lower, []):
                            detection_reasons_map.setdefault(term_lower, []).append("geo_word_prefix")
                    else:
                        type_guesses.setdefault(term_lower, GlossaryTermType.CHARACTER)

                    # Also record individual words from the sequence
                    for word in sequence:
                        w_lower = word.lower()
                        capitalized_occurrences[w_lower] += 1
                        total_occurrences[w_lower] += 1

                    i = j
                    continue

                # Single capitalized word at non-start position
                t_lower = token.lower()
                capitalized_occurrences[t_lower] += 1
                total_occurrences[t_lower] += 1
                if t_lower not in first_forms:
                    first_forms[t_lower] = token
                    detection_reasons_map[t_lower] = ["capitalized_mid_sentence"]
                elif first_forms[t_lower].isupper() and not token.isupper():
                    first_forms[t_lower] = token
                type_guesses.setdefault(t_lower, GlossaryTermType.OTHER)

            else:
                # Not capitalized or at sentence start — track total for ratio
                t_lower = token.lower()
                total_occurrences[t_lower] += 1

            i += 1

    # Filter: keep terms where >80% of occurrences are capitalized
    candidates = {}
    for term_lower, cap_count in capitalized_occurrences.items():
        total = total_occurrences.get(term_lower, cap_count)
        if total < min_frequency:
            continue
        cap_ratio = cap_count / total if total > 0 else 0
        if cap_ratio < 0.8:
            continue
        # Skip if it's a common English word (unless multi-word)
        if " " not in term_lower and dict_checker.is_english_word(term_lower):
            continue

        surface = first_forms.get(term_lower, term_lower)
        reasons = detection_reasons_map.get(term_lower, [])
        if not dict_checker.is_english_word(term_lower):
            if "not_in_dictionary" not in reasons:
                reasons.append("not_in_dictionary")

        candidates[term_lower] = GlossaryCandidate(
            term=surface,
            type_guess=type_guesses.get(term_lower, GlossaryTermType.OTHER),
            frequency=cap_count,
            detection_reasons=reasons,
        )

    return candidates


def extract_uncommon_words(
    text: str,
    dict_checker: DictionaryChecker,
    proper_noun_keys: set[str],
    min_frequency: int,
    sentences: list[str],
) -> dict[str, GlossaryCandidate]:
    """Extract words not found in the English dictionary."""
    if not dict_checker.available:
        return {}

    word_counts: Counter = Counter()
    first_form: dict[str, str] = {}

    for token in tokenize(text):
        if is_special_case(token):
            continue
        lower = token.lower()
        if lower in STOPWORDS:
            continue
        if lower in BLOCKED_WORDS:
            continue
        word_counts[lower] += 1
        if lower not in first_form:
            first_form[lower] = token
        elif first_form[lower].isupper() and not token.isupper():
            first_form[lower] = token

    candidates = {}
    for word_lower, count in word_counts.items():
        if count < min_frequency:
            continue
        if word_lower in proper_noun_keys:
            continue
        if dict_checker.is_english_word(word_lower):
            continue
        if len(word_lower) <= 2:
            continue

        type_guess = GlossaryTermType.TECHNICAL if count >= 3 else GlossaryTermType.OTHER
        candidates[word_lower] = GlossaryCandidate(
            term=first_form[word_lower],
            type_guess=type_guess,
            frequency=count,
            detection_reasons=["not_in_dictionary"],
        )

    return candidates


def extract_frequent_ngrams(
    sentences: list[str],
    dict_checker: DictionaryChecker,
    proper_noun_keys: set[str],
    min_frequency: int,
) -> dict[str, GlossaryCandidate]:
    """Extract high-frequency bigrams and trigrams."""
    ngram_counts: Counter = Counter()
    first_form: dict[str, str] = {}

    for sent in sentences:
        tokens = tokenize(sent)
        lowers = [t.lower() for t in tokens]

        for n in (2, 3):
            for i in range(len(tokens) - n + 1):
                gram_lowers = lowers[i:i + n]
                gram_tokens = tokens[i:i + n]

                # At least one non-stopword
                if all(w in STOPWORDS for w in gram_lowers):
                    continue
                # Skip if any token is a special case
                if any(is_special_case(t) for t in gram_tokens):
                    continue
                # Skip dialogue attributions ("said Jules", "asked Emile")
                if any(w in DIALOGUE_VERBS for w in gram_lowers):
                    continue
                # Skip any n-gram containing a blocked word (days/months)
                if any(w in BLOCKED_WORDS for w in gram_lowers):
                    continue
                # Skip n-grams that start or end with a conjunction/stopword
                if gram_lowers[0] in STOPWORDS or gram_lowers[-1] in STOPWORDS:
                    continue

                key = " ".join(gram_lowers)
                ngram_counts[key] += 1
                surface = " ".join(gram_tokens)
                if key not in first_form:
                    first_form[key] = surface
                elif first_form[key].isupper() and not surface.isupper():
                    first_form[key] = surface

    candidates = {}
    for ngram_lower, count in ngram_counts.items():
        if count < min_frequency:
            continue
        if ngram_lower in proper_noun_keys:
            continue

        words = ngram_lower.split()
        # Skip "Name and/or Name" patterns (conjoined proper nouns, not compound terms)
        if len(words) == 3 and words[1] in ("and", "or"):
            continue
        # At least one word not in dictionary (filters common English phrases)
        if dict_checker.available:
            all_in_dict = all(dict_checker.is_english_word(w) for w in words)
            if all_in_dict:
                continue

        candidates[ngram_lower] = GlossaryCandidate(
            term=first_form[ngram_lower],
            type_guess=GlossaryTermType.TECHNICAL,
            frequency=count,
            detection_reasons=["frequent_ngram"],
        )

    return candidates


def extract_repeated_capitalized(
    text: str,
    dict_checker: DictionaryChecker,
    freq_checker: "FrequencyChecker",
    already_found: set[str],
    min_frequency: int,
    max_zipf_capitalized: float = 4.0,
) -> dict[str, GlossaryCandidate]:
    """Safety net: find words that are always capitalized and either not in
    the English dictionary, or rare in literary English (catches names like
    'Merlin' / 'Mammon' that collide with real but uncommon words).
    """
    cap_counts: Counter = Counter()
    lower_counts: Counter = Counter()
    first_form: dict[str, str] = {}

    for token in tokenize(text):
        if is_special_case(token):
            continue
        lower = token.lower()
        if lower in STOPWORDS:
            continue
        if lower in BLOCKED_WORDS:
            continue
        if token[0].isupper():
            cap_counts[lower] += 1
        else:
            lower_counts[lower] += 1
        if lower not in first_form and token[0].isupper():
            first_form[lower] = token
        elif lower in first_form and first_form[lower].isupper() and not token.isupper() and token[0].isupper():
            first_form[lower] = token

    candidates = {}
    for word_lower, cap_count in cap_counts.items():
        if cap_count < min_frequency:
            continue
        if word_lower in already_found:
            continue
        # Must always be capitalized (never appears lowercase)
        if lower_counts.get(word_lower, 0) > 0:
            continue
        if len(word_lower) <= 1:
            continue

        in_dict = dict_checker.is_english_word(word_lower)
        if in_dict:
            # Word is in the dictionary — admit only if rare in literary English.
            if not freq_checker.available:
                continue
            if freq_checker.literary_zipf(word_lower) >= max_zipf_capitalized:
                continue
            reasons = ["always_capitalized", "rare_in_literary_english"]
        else:
            reasons = ["always_capitalized", "not_in_dictionary"]

        candidates[word_lower] = GlossaryCandidate(
            term=first_form.get(word_lower, word_lower),
            type_guess=GlossaryTermType.OTHER,
            frequency=cap_count,
            detection_reasons=reasons,
        )

    return candidates


def extract_rare_literary_words(
    text: str,
    dict_checker: DictionaryChecker,
    freq_checker: "FrequencyChecker",
    already_found: set[str],
    min_frequency: int,
    max_zipf_mixed: float = 3.0,
) -> dict[str, GlossaryCandidate]:
    """Find dictionary words that are rare in literary English and recur in
    the project text. Catches archaic / domain-specific terms like 'palmer',
    'gaoler', 'halyard', 'carpel'.
    """
    if not freq_checker.available:
        return {}

    word_counts: Counter = Counter()
    first_form: dict[str, str] = {}
    for token in tokenize(text):
        if is_special_case(token):
            continue
        lower = token.lower()
        if lower in STOPWORDS:
            continue
        if lower in BLOCKED_WORDS:
            continue
        if len(lower) <= 2:
            continue
        word_counts[lower] += 1
        if lower not in first_form:
            first_form[lower] = token
        elif first_form[lower].isupper() and not token.isupper():
            first_form[lower] = token

    candidates = {}
    for word_lower, count in word_counts.items():
        if count < min_frequency:
            continue
        if word_lower in already_found:
            continue
        # Must be in the dictionary (otherwise extract_uncommon_words got it)
        if not dict_checker.is_english_word(word_lower):
            continue
        if freq_checker.literary_zipf(word_lower) >= max_zipf_mixed:
            continue
        candidates[word_lower] = GlossaryCandidate(
            term=first_form[word_lower],
            type_guess=GlossaryTermType.TECHNICAL,
            frequency=count,
            detection_reasons=["rare_in_literary_english"],
        )
    return candidates


# ---------------------------------------------------------------------------
# Merge, filter, score
# ---------------------------------------------------------------------------

TYPE_PRIORITY = {
    GlossaryTermType.CHARACTER: 5,
    GlossaryTermType.PLACE: 4,
    GlossaryTermType.TECHNICAL: 3,
    GlossaryTermType.CONCEPT: 2,
    GlossaryTermType.OTHER: 1,
}


def merge_candidates(
    *candidate_dicts: dict[str, GlossaryCandidate],
) -> dict[str, GlossaryCandidate]:
    """Merge candidates from multiple extractors, deduplicating by key."""
    merged: dict[str, GlossaryCandidate] = {}

    for cdict in candidate_dicts:
        for key, candidate in cdict.items():
            if key in merged:
                existing = merged[key]
                # Keep higher-priority type
                if TYPE_PRIORITY.get(candidate.type_guess, 0) > TYPE_PRIORITY.get(existing.type_guess, 0):
                    existing.type_guess = candidate.type_guess
                # Merge reasons
                for reason in candidate.detection_reasons:
                    if reason not in existing.detection_reasons:
                        existing.detection_reasons.append(reason)
                # Keep higher frequency
                existing.frequency = max(existing.frequency, candidate.frequency)
            else:
                merged[key] = candidate.model_copy()

    return merged


def exclude_glossary_terms(
    candidates: dict[str, GlossaryCandidate],
    glossary: Glossary,
) -> int:
    """Remove candidates that are already in the glossary. Returns count excluded."""
    excluded = 0
    to_remove = []
    for key in candidates:
        if glossary.find_term(key) is not None:
            to_remove.append(key)
        # Also check the surface form
        elif glossary.find_term(candidates[key].term) is not None:
            to_remove.append(key)
    for key in to_remove:
        del candidates[key]
        excluded += 1
    return excluded


def score_and_rank(
    candidates: dict[str, GlossaryCandidate],
    dict_checker: DictionaryChecker,
    max_candidates: int,
    sentences: list[str],
) -> list[GlossaryCandidate]:
    """Score, sort, and cap candidates."""
    if not candidates:
        return []

    max_freq = max(c.frequency for c in candidates.values())
    log_max = math.log(max_freq + 1)

    for key, c in candidates.items():
        norm_freq = math.log(c.frequency + 1) / log_max if log_max > 0 else 0
        not_in_dict = 1.0 if not dict_checker.is_english_word(key) else 0.0
        is_multi = 1.0 if " " in key else 0.0
        norm_reasons = min(len(c.detection_reasons) / 3.0, 1.0)
        rare_bonus = 1.0 if "rare_in_literary_english" in c.detection_reasons else 0.0

        c.score = round(
            0.35 * norm_freq + 0.25 * not_in_dict + 0.20 * is_multi
            + 0.10 * norm_reasons + 0.10 * rare_bonus,
            4,
        )

        # Fill context sentence
        if not c.context_sentence:
            c.context_sentence = get_context_sentence(c.term, sentences)

    ranked = sorted(
        candidates.values(),
        key=lambda c: (c.score, c.frequency),
        reverse=True,
    )
    return ranked[:max_candidates]


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def extract_candidates(
    text: str,
    glossary: Optional[Glossary] = None,
    min_frequency: int = 2,
    max_candidates: int = 500,
    verbose: bool = False,
    freq_checker: Optional["FrequencyChecker"] = None,
    max_zipf_capitalized: float = 4.0,
    max_zipf_mixed: float = 3.0,
) -> CandidateReport:
    """Run the full extraction pipeline on source text."""
    text = normalize_newlines(text)
    # Strip [IMAGE:...] placeholders so "IMAGE", "jpg", and filename fragments
    # aren't surfaced as candidate glossary terms. Equal-length whitespace
    # keeps word/sentence boundaries (and total_words count) sensible.
    text = strip_image_placeholders(text)
    sentences = split_into_sentences(text)
    total_words = count_words(text)
    unique_words = len(set(t.lower() for t in tokenize(text)))

    dict_checker = DictionaryChecker()
    if not dict_checker.available and verbose:
        print("Warning: PyEnchant not available. Dictionary-based extraction disabled.",
              file=sys.stderr)

    if freq_checker is None:
        freq_checker = FrequencyChecker()
    if not freq_checker.available and verbose:
        print("Warning: NLTK and wordfreq unavailable. "
              "Skipping rare-literary extraction.", file=sys.stderr)

    if verbose:
        print(f"Analyzing {total_words:,} words ({unique_words:,} unique)...")
        print(f"Found {len(sentences):,} sentences")

    # 1. Proper nouns
    proper_nouns = extract_proper_nouns(sentences, dict_checker, min_frequency)
    if verbose:
        print(f"  Proper noun candidates: {len(proper_nouns)}")

    # 2. Uncommon words
    uncommon = extract_uncommon_words(
        text, dict_checker, set(proper_nouns.keys()), min_frequency, sentences
    )
    if verbose:
        print(f"  Uncommon word candidates: {len(uncommon)}")

    # 3. N-grams
    ngrams = extract_frequent_ngrams(
        sentences, dict_checker, set(proper_nouns.keys()), min_frequency
    )
    if verbose:
        print(f"  N-gram candidates: {len(ngrams)}")

    # 4. Repeated capitalized (safety net)
    already = set(proper_nouns.keys()) | set(uncommon.keys()) | set(ngrams.keys())
    repeated_cap = extract_repeated_capitalized(
        text, dict_checker, freq_checker, already, min_frequency,
        max_zipf_capitalized,
    )
    if verbose:
        print(f"  Repeated capitalized candidates: {len(repeated_cap)}")

    # 5. Rare literary words (dictionary words rare in literary English)
    rare_literary = extract_rare_literary_words(
        text, dict_checker, freq_checker,
        already | set(repeated_cap.keys()),
        min_frequency, max_zipf_mixed,
    )
    if verbose:
        print(f"  Rare literary word candidates: {len(rare_literary)}")

    # Merge
    merged = merge_candidates(proper_nouns, uncommon, ngrams, repeated_cap, rare_literary)
    if verbose:
        print(f"  Merged total: {len(merged)}")

    # Glossary exclusion
    excluded = 0
    if glossary:
        excluded = exclude_glossary_terms(merged, glossary)
        if verbose:
            print(f"  Excluded (in glossary): {excluded}")

    # Score and rank
    ranked = score_and_rank(merged, dict_checker, max_candidates, sentences)

    return CandidateReport(
        source_file=str(Path(text[:0] or "").name),  # placeholder, set by caller
        total_words=total_words,
        total_unique_words=unique_words,
        candidates=ranked,
        excluded_glossary_terms=excluded,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_arguments():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Extract glossary candidate terms from a source text file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic extraction
  python extract_glossary_candidates.py full_book.txt -o candidates.json

  # Exclude terms already in a glossary
  python extract_glossary_candidates.py book.txt -o candidates.json -g glossary.json

  # Require higher frequency threshold
  python extract_glossary_candidates.py book.txt -o candidates.json --min-frequency 3
""",
    )
    parser.add_argument(
        "source_file", type=Path,
        help="Plain text file to analyze"
    )
    parser.add_argument(
        "--output", "-o", type=Path, required=True,
        help="Output JSON file for candidates"
    )
    parser.add_argument(
        "--glossary", "-g", type=Path, default=None,
        help="Existing glossary.json to exclude known terms"
    )
    parser.add_argument(
        "--min-frequency", type=int, default=2,
        help="Minimum occurrences to be a candidate (default: 2)"
    )
    parser.add_argument(
        "--max-candidates", type=int, default=500,
        help="Maximum number of candidates to output (default: 500)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Show detailed progress"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print summary but do not write output file"
    )
    parser.add_argument(
        "--bootstrap", action="store_true",
        help="Send candidates to LLM for translation proposals"
    )
    parser.add_argument(
        "--bootstrap-output", type=Path, default=None,
        help="Output path for bootstrapped glossary.json (default: same dir as -o)"
    )
    parser.add_argument(
        "--style-guide", type=Path, default=None,
        help="Style guide JSON for bootstrap context"
    )
    parser.add_argument(
        "--provider", default="anthropic", choices=["anthropic", "openai"],
        help="API provider for bootstrap (default: anthropic)"
    )
    parser.add_argument(
        "--model", default="claude-sonnet-4-20250514",
        help="Model for bootstrap (default: claude-sonnet-4-20250514)"
    )
    parser.add_argument(
        "--max-literary-zipf-capitalized", type=float, default=4.0,
        help="Max literary Zipf for always-capitalized words to be flagged "
             "(default: 4.0; higher = more permissive)"
    )
    parser.add_argument(
        "--max-literary-zipf-mixed", type=float, default=3.0,
        help="Max literary Zipf for mixed-case dictionary words to be flagged "
             "(default: 3.0)"
    )
    parser.add_argument(
        "--bootstrap-context-mode", choices=["full-text", "word"], default="full-text",
        help="Bootstrap prompt shape: 'full-text' (default, today's behavior — "
             "flat candidate list + first 10 KB of book) or 'word' (candidates "
             "ordered by first appearance, each followed by per-term word-window "
             "fragments, no bulk source-text dump)"
    )
    parser.add_argument(
        "--bootstrap-fragments-per-term", type=int, default=2,
        help="Word-mode only: number of in-text fragments to attach to each "
             "candidate (default: 2)"
    )
    parser.add_argument(
        "--bootstrap-words-before", type=int, default=10,
        help="Word-mode only: word tokens to include before each match "
             "(default: 10)"
    )
    parser.add_argument(
        "--bootstrap-words-after", type=int, default=6,
        help="Word-mode only: word tokens to include after each match "
             "(default: 6)"
    )
    return parser.parse_args()


def print_summary(report: CandidateReport, source_path: Path, output_path: Optional[Path]):
    """Print extraction summary to stdout."""
    type_counts: Counter = Counter()
    for c in report.candidates:
        type_counts[c.type_guess.value] += 1

    print("=" * 70)
    print("Glossary Candidate Extraction")
    print("=" * 70)
    print()
    print(f"Source: {source_path}")
    print(f"Total words: {report.total_words:,}")
    print(f"Unique words: {report.total_unique_words:,}")
    print()
    print(f"Candidates found: {len(report.candidates)}")
    for type_name in ["character", "place", "technical", "concept", "other"]:
        count = type_counts.get(type_name, 0)
        if count > 0:
            print(f"  {type_name.upper():12s}: {count}")
    print()
    if report.excluded_glossary_terms > 0:
        print(f"Excluded (already in glossary): {report.excluded_glossary_terms}")
        print()
    if output_path:
        print(f"Output: {output_path}")
    else:
        print("(dry run — no file written)")
    print("=" * 70)


def save_report(report: CandidateReport, output_path: Path) -> None:
    """Save candidate report to JSON with atomic write."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = report.model_dump(mode='json')

    temp_path = output_path.with_suffix('.tmp')
    try:
        with temp_path.open('w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')
        temp_path.replace(output_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _read_source_text(source_file: Path, verbose: bool = False) -> str:
    """Read source text from a file or auto-promote to a project's chunks/chapters.

    When ``source_file`` lives in a project directory, delegate to
    ``load_clean_source_text`` so we prefer ``chunks/*_chunk_*.json``
    ``source_text`` (immutable source-language, survives the combine stage
    overwriting ``chapters/``) over ``chapters/chapter_*.txt`` (clean but
    may be translated text on Stage-6'd projects). Falls back to reading
    ``source_file`` as-is for standalone files outside a project dir.
    """
    from src.utils.source_text import load_clean_source_text

    project_dir = source_file.parent
    text, _mtime, source_kind = load_clean_source_text(project_dir)
    if source_kind in ("chunks", "chapters"):
        if verbose:
            print(
                f"Using {source_kind}/ from {project_dir} "
                f"(skipping front matter in {source_file.name})"
            )
        return text
    return source_file.read_text(encoding="utf-8")


def main():
    args = parse_arguments()

    # Validate inputs
    if not args.source_file.exists():
        print(f"Error: Source file not found: {args.source_file}", file=sys.stderr)
        sys.exit(1)
    if not args.source_file.is_file():
        print(f"Error: Not a file: {args.source_file}", file=sys.stderr)
        sys.exit(1)

    glossary = None
    if args.glossary:
        if not args.glossary.exists():
            print(f"Error: Glossary file not found: {args.glossary}", file=sys.stderr)
            sys.exit(1)
        glossary = load_glossary(args.glossary)
        if args.verbose:
            print(f"Loaded glossary with {len(glossary.terms)} terms")

    # Read source text. If the user pointed at a project's source.txt and a
    # sibling chapters/ directory exists, prefer those clean per-chapter files
    # so we don't feed TOC, copyright, and other front matter into extraction.
    text = _read_source_text(args.source_file, verbose=args.verbose)
    if not text.strip():
        print("Error: Source file is empty", file=sys.stderr)
        sys.exit(1)

    # Extract
    report = extract_candidates(
        text,
        glossary=glossary,
        min_frequency=args.min_frequency,
        max_candidates=args.max_candidates,
        verbose=args.verbose,
        max_zipf_capitalized=args.max_literary_zipf_capitalized,
        max_zipf_mixed=args.max_literary_zipf_mixed,
    )
    report.source_file = str(args.source_file)

    # Output
    print_summary(report, args.source_file, args.output if not args.dry_run else None)

    if not args.dry_run:
        save_report(report, args.output)

    # Bootstrap: send candidates to LLM for translation proposals
    if args.bootstrap and report.candidates:
        from src.glossary_bootstrap import (
            build_glossary_prompt,
            parse_glossary_response,
            glossary_terms_from_proposals,
            proposals_to_glossary,
        )
        from src.utils.file_io import save_glossary

        # Load style guide if provided
        style_content = ""
        if args.style_guide and args.style_guide.exists():
            from src.utils.file_io import load_style_guide
            sg = load_style_guide(args.style_guide)
            style_content = sg.content

        # Build candidate list for prompt
        candidates = [
            {"term": c.term, "type_guess": c.type_guess.value, "frequency": c.frequency}
            for c in report.candidates
        ]

        context_mode = args.bootstrap_context_mode
        book_title = ""
        context_unit_label = ""
        source_sample = ""

        if context_mode == "word":
            from src.utils.glossary_context import find_first_word_contexts

            chapter_texts = [("source", text)]
            no_match = 0
            for cand in candidates:
                pos, ctx = find_first_word_contexts(
                    cand["term"], chapter_texts,
                    max_contexts=args.bootstrap_fragments_per_term,
                    words_before=args.bootstrap_words_before,
                    words_after=args.bootstrap_words_after,
                )
                cand["first_position"] = pos
                cand["contexts"] = ctx
                if not ctx:
                    no_match += 1

            candidates.sort(
                key=lambda c: c["first_position"] if c["first_position"] is not None
                              else (10**9, 10**9)
            )

            book_title = args.source_file.parent.name or args.source_file.stem
            context_unit_label = (
                f"fragments (~{args.bootstrap_words_before} words before / "
                f"{args.bootstrap_words_after} words after)"
            )
            print(
                f"  word-mode: {args.bootstrap_fragments_per_term} fragment(s) "
                f"per term; {no_match} candidate(s) had no in-text match"
            )
        else:
            # Truncate source text for context (full-text mode, today's behavior)
            source_sample = text[:10000]

        print("\nBootstrapping glossary translations via LLM...")
        try:
            from src.api_translator import call_llm
            prompt = build_glossary_prompt(
                candidates, source_sample, style_content, "Spanish",
                context_mode=context_mode,
                book_title=book_title,
                context_unit_label=context_unit_label,
            )
            response = call_llm(prompt, provider=args.provider, model=args.model, max_tokens=4096, call_type="glossary")
            proposals = parse_glossary_response(response)
            terms = glossary_terms_from_proposals(proposals)
            glossary_obj = proposals_to_glossary(terms)

            # Interactive review
            accepted = []
            for term in glossary_obj.terms:
                print(f"\n  {term.english} -> {term.spanish} ({term.type})")
                if term.context:
                    print(f"    Context: {term.context}")
                if term.alternatives:
                    print(f"    Alternatives: {', '.join(term.alternatives)}")

                choice = input("  [y]es / [n]o / [e]dit / [s]kip: ").strip().lower()
                if choice in ("y", "yes", ""):
                    accepted.append(term)
                elif choice in ("e", "edit"):
                    new_spanish = input(f"  New translation [{term.spanish}]: ").strip()
                    if new_spanish:
                        term.spanish = new_spanish
                    accepted.append(term)
                elif choice in ("s", "skip"):
                    continue
                # 'n' or 'no' = reject, don't add

            if accepted:
                result_glossary = proposals_to_glossary(accepted)
                out_path = args.bootstrap_output or args.output.parent / "glossary.json"

                # If glossary already exists, merge
                if out_path.exists():
                    existing = load_glossary(out_path)
                    existing_terms = {t.english.lower() for t in existing.terms}
                    new_terms = [t for t in accepted if t.english.lower() not in existing_terms]
                    existing.terms.extend(new_terms)
                    save_glossary(existing, out_path)
                    print(f"\nMerged {len(new_terms)} new terms into {out_path} ({len(existing.terms)} total)")
                else:
                    save_glossary(result_glossary, out_path)
                    print(f"\nSaved {len(accepted)} terms to {out_path}")
            else:
                print("\nNo terms accepted.")

        except Exception as e:
            print(f"\nError during bootstrap: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
