"""
Dictionary evaluator for translation quality.

Checks that words in the translation are valid Spanish words, and flags
English words or unknown words that may be misspellings.
"""

import re
from typing import Any, Optional

try:
    import enchant
    ENCHANT_AVAILABLE = True
except ImportError:
    enchant = None
    ENCHANT_AVAILABLE = False

from ..models import Chunk, EvalResult, Issue, IssueLevel, Glossary
from .base import BaseEvaluator


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
    """

    name = "dictionary"
    version = "1.0.0"
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

        # Tokenize and get word positions
        words_with_positions = self._tokenize_with_positions(chunk.translated_text)

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
            if self._check_spanish_word(word):
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

        # Use regex to find words (Unicode letters and hyphens/apostrophes within words)
        # This pattern matches words including accented characters
        pattern = r"[\w'áéíóúüñÁÉÍÓÚÜÑ]+"

        for match in re.finditer(pattern, text):
            word = match.group()
            position = match.start()
            words_with_positions.append((word, position))

        return words_with_positions

    def _is_special_case(self, word: str) -> bool:
        """
        Check if word is a special case that should be ignored.

        Special cases:
        - Numbers (123, 1.5, etc.)
        - Single characters
        - All punctuation

        Args:
            word: Word to check

        Returns:
            True if word should be ignored
        """
        # Single character (except meaningful ones like "a", "y")
        if len(word) == 1 and word.lower() not in ('a', 'o', 'e', 'y'):
            return True

        # All digits (possibly with decimal point or comma)
        if re.match(r'^\d+([.,]\d+)*$', word):
            return True

        # Roman numerals (e.g. chapter headings like I, II, XIV, LXXX)
        if re.match(r'^[IVXLCDMivxlcdm]+$', word):
            return True

        return False

    def _check_spanish_word(self, word: str) -> bool:
        """
        Check if word exists in Spanish dictionary.

        Checks both es_ES (Spain Spanish) and es_MX (Mexican Spanish).
        Returns True if word is valid in EITHER dictionary.

        Tries both the original word AND lowercase version to handle
        proper nouns (which are capitalized in dictionaries like "Inglaterra").

        Args:
            word: Word to check

        Returns:
            True if word is in either Spanish dictionary (es_ES OR es_MX)
        """
        # First try the word as-is (handles proper nouns like "Inglaterra")
        if self.spanish_dict_es.check(word) or self.spanish_dict_mx.check(word):
            return True

        # If word is capitalized, also try lowercase version
        # This handles cases where proper noun is at start of sentence
        if word and word[0].isupper() and len(word) > 1:
            lowercase_word = word.lower()
            if self.spanish_dict_es.check(lowercase_word) or self.spanish_dict_mx.check(lowercase_word):
                return True

        # Morphological fallback: diminutives, clitics, etc.
        if self._check_spanish_morphology(word.lower()):
            return True

        return False

    def _check_spanish_morphology(self, word_lower: str) -> bool:
        """
        Morphological fallback for words not found in the dictionary.

        Handles two common cases:
        - Diminutive suffixes (-ito/-ita/-itos/-itas, -illo/-illa, -cito/-cita, etc.)
        - Verb + clitic pronouns (-lo/-la/-los/-las/-le/-les/-me/-te/-se/-nos/-os/-monos)

        Args:
            word_lower: Lowercase word to check

        Returns:
            True if a valid Spanish base form can be recovered
        """
        def _is_valid(candidate: str) -> bool:
            return bool(
                self.spanish_dict_es.check(candidate)
                or self.spanish_dict_mx.check(candidate)
            )

        def _normalize_accents(s: str) -> str:
            return s.translate(str.maketrans("áéíóúü", "aeiouu"))

        # Pass A — Diminutive suffix stripping (longest first)
        diminutive_suffixes = [
            "citos", "citas", "cito", "cita",
            "itos", "itas", "illos", "illas", "illo", "illa",
            "ito", "ita",
        ]
        for suffix in diminutive_suffixes:
            if word_lower.endswith(suffix):
                stem = word_lower[: -len(suffix)]
                if len(stem) < 3:
                    continue
                candidates = [
                    stem,
                    stem + "o",
                    stem + "a",
                    stem + "e",
                ]
                # If stem ends in a vowel, try vowel-swap (e.g. amigu → amigo)
                if stem[-1] in "aeiouáéíóú":
                    candidates.append(stem[:-1] + "o")
                    candidates.append(stem[:-1] + "a")
                for candidate in candidates:
                    if _is_valid(candidate):
                        return True

        # Pass B — Verb clitic stripping (longest first)
        clitic_suffixes = [
            "monos", "selos", "melo", "mela", "telo", "tela",
            "nos", "los", "las", "les",
            "me", "te", "se", "lo", "la", "le", "os",
        ]
        for suffix in clitic_suffixes:
            if word_lower.endswith(suffix):
                stem = word_lower[: -len(suffix)]
                if len(stem) < 3:
                    continue
                for candidate in (stem, _normalize_accents(stem)):
                    if _is_valid(candidate):
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
        word_lower = word.lower()
        for term in glossary.terms:
            # Exact match on the full term (single-word terms, or exact multi-word match)
            if term.spanish.lower() == word_lower or term.english.lower() == word_lower:
                return True
            # Token match: word is one component of a multi-word term
            spanish_tokens = {t.lower() for t in term.spanish.split()}
            english_tokens = {t.lower() for t in term.english.split()}
            if word_lower in spanish_tokens or word_lower in english_tokens:
                return True
        return False

    def _get_suggestions(self, word: str) -> list[str]:
        """
        Get spelling suggestions for a word from Spanish dictionaries.

        Combines suggestions from both es_ES and es_MX dictionaries.

        Args:
            word: Word to get suggestions for

        Returns:
            List of suggested corrections (deduplicated)
        """
        suggestions = []
        try:
            # Get suggestions from Spain Spanish
            suggestions.extend(self.spanish_dict_es.suggest(word))
        except:
            pass

        try:
            # Get suggestions from Mexican Spanish
            suggestions.extend(self.spanish_dict_mx.suggest(word))
        except:
            pass

        # Deduplicate while preserving order
        seen = set()
        unique_suggestions = []
        for s in suggestions:
            if s.lower() not in seen:
                seen.add(s.lower())
                unique_suggestions.append(s)

        return unique_suggestions

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
            suggestion=suggestion
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
