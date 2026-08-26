"""
Dictionary evaluator for translation quality.

Checks that words in the translation are valid Spanish words, and flags
English words or unknown words that may be misspellings.
"""

import re
import unicodedata
from typing import Any, Optional

try:
    import enchant
    ENCHANT_AVAILABLE = True
except ImportError:
    enchant = None
    ENCHANT_AVAILABLE = False

from ..models import Chunk, EvalResult, Issue, IssueLevel, Glossary
from ..utils.text_utils import (
    blank_caption_markers,
    blank_footnote_markers,
    strip_image_placeholders,
)
from .base import BaseEvaluator


def _fold_accents_preserving_enye(s: str) -> str:
    """Fold Spanish vowel accents/diaeresis while preserving ñ.

    á→a, é→e, í→i, ó→o, ú→u, ü→u — but ñ/Ñ are kept intact. ñ is a distinct
    Spanish letter, not an accented n; stripping its tilde (ñ→n) would let a
    genuine misspelling validate against a different real word (e.g. "moño" →
    "mono"). Decomposes per character so ñ can be passed through untouched.
    """
    out = []
    for ch in s:
        if ch in "ñÑ":
            out.append(ch)
            continue
        nfd = unicodedata.normalize("NFD", ch)
        out.append("".join(c for c in nfd if unicodedata.category(c) != "Mn"))
    return "".join(out)


# ---------------------------------------------------------------------------
# Suffix tables for the morphological fallback
#
# Longest first within each table: "montoncito" has to strip "cito" and land on
# "monton", not strip "ito" and land on the dead "montonc".
# ---------------------------------------------------------------------------

#: Diminutive endings. The -ec- forms are the epenthetic variants Spanish uses
#: after a short base ("nube" -> "nubecilla"), and -uelo/-zuelo the older
#: diminutive that still turns up in period prose ("mozuelo").
_DIMINUTIVE_SUFFIXES = (
    "ecillos", "ecillas", "ecitos", "ecitas",
    "ecillo", "ecilla", "cillos", "cillas",
    "zuelos", "zuelas", "ecito", "ecita",
    "citos", "citas", "cillo", "cilla",
    "illos", "illas", "uelos", "uelas",
    "zuelo", "zuela", "itos", "itas",
    "cito", "cita", "illo", "illa",
    "uelo", "uela", "ito", "ita",
)

#: Superlatives. The written accent is part of the suffix, which is what keeps
#: the unaccented misspelling "grandisimo" flagged.
_SUPERLATIVE_SUFFIXES = ("ísimos", "ísimas", "ísimo", "ísima")

#: Enclitic pronoun clusters attached to an infinitive, gerund or imperative
#: ("decirle", "dándoselo", "callaos").
#:
#: "monos" is deliberately absent. Pass C already reconstructs every real
#: -monos form by restoring the verb's deleted -s ("vámonos", "marchémonos",
#: "sentémonos", "quedémonos"), so a "monos" entry here caught nothing Pass C
#: had missed -- it only let an unknown word through whenever its first three
#: or more characters happened to be a dictionary word ("algomonos" -> "algo",
#: "casimonos" -> "casi").
_CLITIC_SUFFIXES = (
    "noslo", "selos", "selas", "melos", "telas",
    "selo", "sela", "melo", "mela", "telo", "tela", "seme",
    "nos", "los", "las", "les",
    "me", "te", "se", "lo", "la", "le", "os",
)

#: A written acute accent on a vowel. Attaching an enclitic pushes the stress
#: back far enough that the result is always esdrújula or sobresdrújula, and
#: Spanish writes the accent on every one of those without exception -- so a
#: -monos form that carries no acute vowel is not a contraction, it is the
#: contraction misspelled.
#:
#: Deliberately not ``word != _fold_accents_preserving_enye(word)``. That test
#: also fires on the diæresis, which is not a stress mark: "apacigüemonos"
#: reads as accented because of its ü and would be laundered straight back
#: into "apacigüemos". Naming the five vowels states the orthographic rule
#: instead of approximating it.
_ACUTE_VOWEL_RE = re.compile(r"[áéíóú]")


class DictionaryEvaluator(BaseEvaluator):
    """
    Evaluates words in translation against Spanish and English dictionaries.

    Checks:
    - All words in translation should be valid Spanish words
    - Flags English words found in translation (errors)
    - Flags unknown words not in either dictionary (warnings)
    - Optionally uses glossary to exclude known terms
    - Reports character positions for each flagged word

    Configuration (passed in context dict):
    - glossary: Optional Glossary object with known terms
    - case_sensitive: Whether to treat Case and case differently (default: False)
    - apply_morphology: bool (default True; False turns off the morphological
      fallback so the raw dictionary lookup can be scored -- see
      scripts/replay_dictionary_marks.py, and grammar_eval's apply_default_ignores)
    """

    name = "dictionary"
    version = "1.1.0"
    description = "Checks words against Spanish/English dictionaries"

    def __init__(self):
        """Initialize dictionaries."""
        super().__init__()

        if not ENCHANT_AVAILABLE:
            raise RuntimeError(
                "PyEnchant is required for dictionary evaluation. "
                "Install it with: pip install pyenchant"
            )

        try:
            # Initialize both Spanish dictionary variants
            # Using both es_ES (Spain) and es_MX (Mexican) for maximum coverage
            self.spanish_dict_es = enchant.Dict("es_ES")
            self.spanish_dict_mx = enchant.Dict("es_MX")
            self.english_dict = enchant.Dict("en_US")
        except enchant.errors.DictNotFoundError as e:
            raise RuntimeError(
                f"Dictionary not found: {e}. "
                "Make sure Spanish (es_ES, es_MX) and English (en_US) dictionaries are installed."
            )

        # Lookup memos, keyed by the exact string handed to enchant. The
        # morphological fallback probes up to a dozen candidate base forms per
        # flagged word, and the same words recur across every chunk of a book,
        # so this collapses most of that work. suggest() in particular is the
        # expensive call and only ever runs on the fallback path.
        self._valid_cache: dict[str, bool] = {}
        self._suggest_cache: dict[str, list[str]] = {}

    def evaluate(self, chunk: Chunk, context: dict[str, Any]) -> EvalResult:
        """
        Evaluate the translation for dictionary issues.

        Args:
            chunk: Chunk with source_text and translated_text
            context: Configuration options (see class docstring)

        Returns:
            EvalResult with dictionary check results

        Raises:
            ValueError: If chunk.translated_text is None
        """
        if chunk.translated_text is None:
            raise ValueError(f"Chunk {chunk.id} has no translation")

        # Get configuration
        glossary = context.get("glossary")
        case_sensitive = context.get("case_sensitive", False)
        apply_morphology = context.get("apply_morphology", True)

        # Replace image placeholders with equal-length whitespace before tokenizing
        # (e.g. [IMAGE:images/i010.jpg]) — preserves character offsets for all subsequent words
        text_to_check = strip_image_placeholders(chunk.translated_text)
        # Blank the [CAPTION] marker too (offsets preserved) so it is not
        # tokenized as an unknown word. The caption's own prose is still checked.
        text_to_check = blank_caption_markers(text_to_check)
        # And [FOOTNOTE:N], for the same reason: without this, "FOOTNOTE" is
        # reported as an unknown word once per footnote reference in the book.
        text_to_check = blank_footnote_markers(text_to_check)

        # Tokenize and get word positions
        words_with_positions = self._tokenize_with_positions(text_to_check)

        # Track issues by word (to avoid duplicate reporting)
        english_words = {}  # word -> list of positions
        unknown_words = {}  # word -> list of positions
        glossary_words = {}  # word -> list of positions

        # Check each unique word
        unique_words = set(word for word, _ in words_with_positions)

        for word in unique_words:
            # Skip special cases
            if self._is_special_case(word):
                continue

            # Get all positions of this word
            positions = [pos for w, pos in words_with_positions if w == word]

            # For glossary, use lowercase for case-insensitive matching
            glossary_word = word if case_sensitive else word.lower()

            # Check if in glossary first
            if glossary and self._in_glossary(glossary_word, glossary):
                glossary_words[word] = positions
                continue

            # Check if valid Spanish (method handles capitalization internally)
            if self._check_spanish_word(word, apply_morphology=apply_morphology):
                continue

            # Not in Spanish dictionary - check if English
            if self._check_english_word(word):
                english_words[word] = positions
                continue

            # Not in either dictionary
            unknown_words[word] = positions

        # Create issues
        issues = []

        # English words are errors
        for word, positions in sorted(english_words.items()):
            issue = self._create_word_issue(
                word=word,
                positions=positions,
                severity=IssueLevel.ERROR,
                reason="English word in translation",
                suggestion=f"Translate '{word}' to Spanish or add to glossary if it's a proper noun"
            )
            issues.append(issue)

        # Unknown words are warnings
        for word, positions in sorted(unknown_words.items()):
            # Try to get suggestions
            suggestions = self._get_suggestions(word)
            suggestion_text = f"Possible misspelling. Suggestions: {', '.join(suggestions[:3])}" if suggestions else "Possible misspelling or proper noun. Verify spelling or add to glossary."

            issue = self._create_word_issue(
                word=word,
                positions=positions,
                severity=IssueLevel.WARNING,
                reason="Unknown word (not in Spanish or English dictionary)",
                suggestion=suggestion_text
            )
            issues.append(issue)

        # Calculate score based on error/warning counts
        total_words = len(words_with_positions)
        flagged_words = sum(len(positions) for positions in english_words.values())
        flagged_words += sum(len(positions) for positions in unknown_words.values())

        score = self._calculate_score(total_words, flagged_words)

        # Create metadata
        metadata = {
            "total_words": total_words,
            "unique_words": len(unique_words),
            "english_words": len(english_words),
            "unknown_words": len(unknown_words),
            "glossary_words": len(glossary_words),
            "flagged_instances": flagged_words,
        }

        return self.create_result(chunk, issues, score, metadata)

    def _tokenize_with_positions(self, text: str) -> list[tuple[str, int]]:
        """
        Tokenize text and return words with their character positions.

        Args:
            text: Text to tokenize

        Returns:
            List of (word, character_position) tuples
        """
        words_with_positions = []

        # Unicode letters only, with an internal apostrophe allowed so
        # "d'Artagnan" stays one token -- straight, curly and modifier-letter
        # alike ('’ʼ), the same set glossary_context tokenizes on.
        # Typeset sources use the curly form, and clean_translation_text only
        # normalizes it on the paths that run through it; a hand-edited chunk
        # can still carry one. Splitting it would drop the "d" (a single
        # character _is_special_case discards) and leave "Artagnan" unable to
        # match the glossary entry it belongs to.
        #
        # [^\W\d_] is "\w minus digits minus underscore". The underscore matters:
        # \w includes it, so the old pattern tokenized markdown emphasis *with*
        # its delimiters -- "_sí_", "_usted_", "_Sabueso_" -- and every one of
        # those was reported as an unknown word. That single character was the
        # largest source of false positives this evaluator had.
        #
        # Digits drop out of the same class; they are never emitted as tokens.
        #
        # No hyphen, deliberately: "bien-amado" splits into two real words,
        # which both check out, whereas hunspell has no entry for the compound.
        # (The old comment claimed hyphens were kept; the character class never
        # had one, so the behavior here is unchanged -- only the comment is.)
        pattern = r"[^\W\d_]+(?:['’ʼ][^\W\d_]+)*"

        for match in re.finditer(pattern, text):
            word = match.group()
            position = match.start()
            words_with_positions.append((word, position))

        return words_with_positions

    def _is_special_case(self, word: str) -> bool:
        """
        Check if word is a special case that should be ignored.

        Special cases:
        - Single characters
        - Roman numerals

        Args:
            word: Word to check

        Returns:
            True if word should be ignored
        """
        # Single character (except meaningful ones like "a", "y")
        if len(word) == 1 and word.lower() not in ('a', 'o', 'e', 'y'):
            return True

        # Roman numerals (e.g. chapter headings like I, II, XIV, LXXX)
        if re.match(r'^[IVXLCDMivxlcdm]+$', word):
            return True

        return False

    def _check_spanish_word(self, word: str, apply_morphology: bool = True) -> bool:
        """
        Check if word exists in Spanish dictionary.

        Checks both es_ES (Spain Spanish) and es_MX (Mexican Spanish).
        Returns True if word is valid in EITHER dictionary.

        Tries both the original word AND lowercase version to handle
        proper nouns (which are capitalized in dictionaries like "Inglaterra").

        Args:
            word: Word to check
            apply_morphology: Run the morphological fallback (default True).
                False leaves only the raw dictionary lookups, which is how the
                replay harness scores the fallback's effect.

        Returns:
            True if the word is in a Spanish dictionary, or (when
            apply_morphology is True) a recoverable base form is.
        """
        # First try the word as-is (handles proper nouns like "Inglaterra")
        if self._is_valid(word):
            return True

        # If word is capitalized, also try lowercase version
        # This handles cases where proper noun is at start of sentence
        if word and word[0].isupper() and len(word) > 1:
            if self._is_valid(word.lower()):
                return True

        # Morphological fallback: diminutives, superlatives, clitics, plurals.
        if apply_morphology and self._check_spanish_morphology(word.lower()):
            return True

        return False

    def _is_valid(self, candidate: str) -> bool:
        """Memoized "is this exact string in either Spanish dictionary"."""
        # Only the empty string is short-circuited: enchant raises on it, and
        # "y", "o", "e" and "a" are real one-letter Spanish words that
        # _is_special_case deliberately lets through to be checked here.
        if not candidate:
            return False
        cached = self._valid_cache.get(candidate)
        if cached is None:
            try:
                cached = bool(
                    self.spanish_dict_es.check(candidate)
                    or self.spanish_dict_mx.check(candidate)
                )
            except Exception:
                cached = False
            self._valid_cache[candidate] = cached
        return cached

    def _suggest_cached(self, word: str) -> list[str]:
        """Memoized union of es_ES and es_MX suggestions, order preserved."""
        cached = self._suggest_cache.get(word)
        if cached is not None:
            return cached

        suggestions: list[str] = []
        for dictionary in (self.spanish_dict_es, self.spanish_dict_mx):
            try:
                suggestions.extend(dictionary.suggest(word))
            except Exception:
                pass

        seen = set()
        unique: list[str] = []
        for suggestion in suggestions:
            if suggestion.lower() not in seen:
                seen.add(suggestion.lower())
                unique.append(suggestion)

        self._suggest_cache[word] = unique
        return unique

    def _valid_or_folded(self, candidate: str) -> bool:
        """True if *candidate*, or *candidate* stripped of its vowel accents, is
        a dictionary word. Removes accents only; see
        :meth:`_accent_insensitive_valid` for the direction that restores them.
        """
        if self._is_valid(candidate):
            return True
        folded = _fold_accents_preserving_enye(candidate)
        return folded != candidate and self._is_valid(folded)

    def _accent_insensitive_valid(self, candidate: str) -> bool:
        """True if a dictionary word differs from *candidate* only by vowel accents.

        Spanish drops the written accent when a suffix moves the stress:
        "montón" -> "montoncito", "árbol" -> "arbolito". Stripping the suffix
        therefore hands back an *unaccented* stem that no dictionary contains,
        while the base form it came from does. Folding the candidate is not
        enough, because the accent lives on the dictionary side; the accented
        form is recovered from ``suggest()``, which reliably offers it for a
        one-accent difference.

        **Call this only from the diminutive/superlative pass, never on the raw
        word and never on a plural stem.** A global accent-insensitive lookup
        would validate "nivea" as "nívea", "razon" as "razón" and "tambien" as
        "también" -- genuine typos, and among the few real defects this
        evaluator has ever caught. Stripping a plural -s is not enough
        confinement either: it hands back those same typos ("razons" -> "razon")
        and re-accents them. Only suffixes that actually move the stress off the
        base's written accent belong here, which is Pass A.

        ``_fold_accents_preserving_enye`` keeps ñ intact, so "nino" still does
        not reach "niño".
        """
        if self._valid_or_folded(candidate):
            return True
        folded = _fold_accents_preserving_enye(candidate)
        for suggestion in self._suggest_cached(candidate):
            # An accent has to actually differ. Without this guard hunspell's
            # habit of offering a capitalized proper noun for a lowercase stem
            # ("hill" -> "Hill", "cos" -> "Cos", "cuart" -> "Cuart") validated
            # the stem on a pure case difference, which is not the contract
            # this method documents and let English words through as Spanish.
            if suggestion.lower() == candidate.lower():
                continue
            if _fold_accents_preserving_enye(suggestion.lower()) == folded:
                return True
        return False

    def _stem_base_forms(self, stem: str) -> list[str]:
        """Base forms a diminutive/superlative stem could have been built from.

        Suffixation eats the base's final vowel ("mesa" -> "mesita") and can
        respell the stem-final consonant to preserve its sound. Both are undone
        here rather than left to each caller.
        """
        candidates = [stem, stem + "o", stem + "a", stem + "e"]

        # Stem still ends in a vowel: the suffix attached after the base's own
        # vowel was kept ("amigo" -> "amiguito" leaves "amigu").
        if stem[-1] in "aeiouáéíóú":
            candidates.append(stem[:-1] + "o")
            candidates.append(stem[:-1] + "a")

        # Orthographic alternations that keep the base's sound in front of the
        # suffix's front vowel: "banco" -> "banquito", "rico" -> "riquísimo"
        # (c -> qu), and "nariz" -> "naricita", "feliz" -> "felicísimo"
        # (z -> c). Undo them.
        if stem.endswith("qu"):
            base = stem[:-2] + "c"
            candidates += [base, base + "o", base + "a", base + "e"]
        if stem.endswith("c"):
            base = stem[:-1] + "z"
            candidates += [base, base + "o", base + "a", base + "e"]

        return candidates

    def _check_spanish_morphology(self, word_lower: str) -> bool:
        """
        Morphological fallback for words not found in the dictionary.

        Hunspell's Spanish dictionaries list base forms and regular inflections
        but not the productive derivations Spanish prose uses constantly, so a
        correctly spelled "pastorcillo" or "vámonos" is simply absent from them.
        Each pass below strips one derivation and asks whether what is left is a
        real word; a word whose base form is in the dictionary is not a
        misspelling by construction.

        Passes, in order: diminutives and superlatives; ``-mente`` adverbs;
        the ``-monos`` contraction; enclitic pronouns; plurals.

        Args:
            word_lower: Lowercase word to check

        Returns:
            True if a valid Spanish base form can be recovered
        """
        # Pass A — diminutives and superlatives.
        #
        # The superlative suffixes carry the written accent, so the common
        # misspelling "grandisimo" is not rescued here.
        for suffix in _DIMINUTIVE_SUFFIXES + _SUPERLATIVE_SUFFIXES:
            if not word_lower.endswith(suffix):
                continue
            stem = word_lower[: -len(suffix)]
            if len(stem) < 3:
                continue
            for candidate in self._stem_base_forms(stem):
                if self._accent_insensitive_valid(candidate):
                    return True

        # Pass B — -mente adverbs. Strict lookup on purpose: the adverb is built
        # on the feminine adjective and keeps its accent exactly ("rápida" ->
        # "rápidamente"), so an unaccented remainder means the word really is
        # misspelled and should stay flagged.
        if word_lower.endswith("mente"):
            stem = word_lower[: -len("mente")]
            if len(stem) >= 3 and self._is_valid(stem):
                return True

        # Pass C — the -monos contraction. "vámonos" is "vamos" + "nos" with the
        # verb's final -s deleted, so no amount of stripping recovers it:
        # "vámonos"[:-5] is "vá" and "vámonos"[:-3] is "vámo", both dead ends.
        # Put the -s back instead. The length guard applies to the
        # reconstruction rather than the bare stem — guarding the stem is
        # precisely what used to kill this word.
        #
        # The reconstruction has to start from an accented word. Restoring the
        # -s folds accents away, which is right for "vámonos" -> "vámos" ->
        # "vamos" but hands every unaccented typo the same free pass:
        # "vamonos", "marchemonos", "quedemonos" and "demonos" all validated.
        # Requiring the accent up front costs nothing measurable -- all six
        # distinct -monos tokens in the corpus carry one, as the orthography
        # requires -- and it puts this pass back under the same accent contract
        # Pass B and Pass E already keep.
        if word_lower.endswith("monos") and _ACUTE_VOWEL_RE.search(word_lower):
            candidate = word_lower[:-3] + "s"
            if len(candidate) >= 3 and self._valid_or_folded(candidate):
                return True

        # Pass D — enclitic pronouns (longest first).
        for suffix in _CLITIC_SUFFIXES:
            if not word_lower.endswith(suffix):
                continue
            stem = word_lower[: -len(suffix)]
            if len(stem) < 3:
                continue
            if self._valid_or_folded(stem):
                return True

        # Pass E — plurals. "cacareos" is perfectly regular but absent from
        # hunspell, while its singular "cacareo" is present.
        #
        # Strict lookup (plus accent *folding*), never the accent-restoring one.
        # A plural is the one derivation that does not move the stress off a
        # written accent the way a diminutive does: "café" -> "cafés" keeps it,
        # and the -es forms that do drop it ("razón" -> "razones") are regular
        # enough that hunspell already lists them, so this pass never sees
        # them. Restoring accents here instead accepted every pinned typo the
        # moment it wore a plural -s -- "razons", "niveas", "cancions",
        # "tambiens" -- which is the exact class _accent_insensitive_valid's
        # docstring promises to keep flagged. Measured over the corpus the
        # restoring version bought no legitimate acceptance at all: the only
        # four tokens that reached it ("Lias", "Hills", "catarinas", "solitas")
        # were themselves over-accepts.
        for suffix in ("es", "s"):
            if not word_lower.endswith(suffix):
                continue
            stem = word_lower[: -len(suffix)]
            if len(stem) < 3:
                continue
            if self._valid_or_folded(stem):
                return True

        return False

    def _check_english_word(self, word: str) -> bool:
        """
        Check if word exists in English dictionary.

        Tries both the original word AND lowercase version to handle
        proper nouns and capitalized words.

        Args:
            word: Word to check

        Returns:
            True if word is in English dictionary
        """
        # First try the word as-is
        if self.english_dict.check(word):
            return True

        # If word is capitalized, also try lowercase version
        if word and word[0].isupper() and len(word) > 1:
            lowercase_word = word.lower()
            if self.english_dict.check(lowercase_word):
                return True

        return False

    def _in_glossary(self, word: str, glossary: Glossary) -> bool:
        """
        Check if word is in the glossary.

        Args:
            word: Word to check (lowercase)
            glossary: Glossary to search

        Returns:
            True if word matches a glossary term
        """
        return glossary.matches_word(word)

    def _get_suggestions(self, word: str) -> list[str]:
        """
        Get spelling suggestions for a word from Spanish dictionaries.

        Combines suggestions from both es_ES and es_MX dictionaries.

        Args:
            word: Word to get suggestions for

        Returns:
            List of suggested corrections (deduplicated)
        """
        return self._suggest_cached(word)

    def _create_word_issue(
        self,
        word: str,
        positions: list[int],
        severity: IssueLevel,
        reason: str,
        suggestion: str
    ) -> Issue:
        """
        Create an issue for a flagged word.

        Args:
            word: The flagged word
            positions: List of character positions where word appears
            severity: Error, warning, or info
            reason: Why the word was flagged
            suggestion: How to fix it

        Returns:
            Issue instance
        """
        # Format positions
        if len(positions) == 1:
            location = f"Character position {positions[0]}"
        elif len(positions) <= 3:
            location = f"Character positions: {', '.join(str(p) for p in positions)}"
        else:
            location = f"Character positions: {', '.join(str(p) for p in positions[:3])}, ... ({len(positions)} total)"

        message = f"'{word}': {reason} (found {len(positions)} time(s))"

        return self.create_issue(
            severity=severity,
            message=message,
            location=location,
            suggestion=suggestion,
            term=word,
        )

    def _calculate_score(self, total_words: int, flagged_words: int) -> float:
        """
        Calculate a quality score based on flagged word ratio.

        Args:
            total_words: Total number of words
            flagged_words: Number of flagged word instances

        Returns:
            Score between 0.0 and 1.0
        """
        if total_words == 0:
            return 1.0

        # Score is percentage of words that passed
        clean_words = max(0, total_words - flagged_words)
        score = clean_words / total_words

        return score
