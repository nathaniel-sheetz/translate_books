"""
Glossary Evaluator

Validates that glossary terms are translated correctly and consistently according
to the project glossary. Ensures proper use of character names, places, and
technical terms throughout the translation.

This evaluator differs from DictionaryEvaluator:
- DictionaryEvaluator: Excludes glossary terms from spell-checking
- GlossaryEvaluator: Validates glossary terms are used correctly
"""

from typing import Any, Optional
import re
from ..models import Chunk, EvalResult, Issue, IssueLevel, Glossary, GlossaryTerm, GlossaryTermType
from .base import BaseEvaluator


class GlossaryEvaluator(BaseEvaluator):
    """
    Evaluates translation compliance with project glossary.

    Checks:
    - English terms in source are translated to correct Spanish terms
    - Spanish terms match glossary (primary or valid alternatives)
    - Consistent use of alternatives within a chunk (no mixing)
    - Character names, places, and concepts follow glossary rules

    Example:
        evaluator = GlossaryEvaluator()
        chunk = Chunk(source_text="Mr. Bennet said...",
                     translated_text="Sr. Bennet dijo...")
        glossary = Glossary(terms=[...])
        result = evaluator.evaluate(chunk, {"glossary": glossary})
    """

    name = "glossary"
    version = "1.0.0"
    description = "Validates glossary term compliance and consistency"

    def evaluate(self, chunk: Chunk, context: dict[str, Any]) -> EvalResult:
        """
        Evaluate glossary compliance for a translated chunk.

        Args:
            chunk: Chunk containing source and translated text
            context: Dictionary that should contain "glossary" key with Glossary object

        Returns:
            EvalResult with issues for glossary violations
        """
        issues: list[Issue] = []

        # Get glossary from context
        glossary: Optional[Glossary] = context.get("glossary")
        if not glossary or not glossary.terms:
            # No glossary provided - nothing to check
            return self.create_result(
                chunk=chunk,
                issues=[],
                score=1.0,
                metadata={"glossary_terms_checked": 0}
            )

        # Check if translation exists
        if not chunk.translated_text or not chunk.translated_text.strip():
            issue = self.create_issue(
                severity=IssueLevel.ERROR,
                message="No translation provided - cannot validate glossary compliance",
                location="translation"
            )
            return self.create_result(
                chunk=chunk,
                issues=[issue],
                score=0.0,
                metadata={"glossary_terms_checked": 0}
            )

        if not chunk.source_text or not chunk.source_text.strip():
            issue = self.create_issue(
                severity=IssueLevel.ERROR,
                message="No source text provided - cannot validate glossary",
                location="source"
            )
            return self.create_result(
                chunk=chunk,
                issues=[issue],
                score=0.0,
                metadata={"glossary_terms_checked": 0}
            )

        # Track statistics for scoring
        terms_checked = 0
        terms_found_in_source = 0
        terms_correct = 0
        consistency_warnings = 0

        # Check each glossary term
        for term in glossary.terms:
            terms_checked += 1

            # Find English term in source text
            english_occurrences = self._find_term_occurrences(
                chunk.source_text,
                term.english,
                term.type
            )

            if not english_occurrences:
                # Term not in source - skip validation
                continue

            terms_found_in_source += 1

            # Find Spanish term(s) in translation
            spanish_occurrences = self._find_all_spanish_variants(
                chunk.translated_text,
                term
            )

            # Check translation correctness
            translation_issues = self._check_term_translation(
                term=term,
                english_occurrences=english_occurrences,
                spanish_occurrences=spanish_occurrences,
                source_text=chunk.source_text,
                translation_text=chunk.translated_text
            )
            issues.extend(translation_issues)

            if not translation_issues:
                terms_correct += 1

                # Check consistency if term appears multiple times
                consistency_issue = self._check_consistency(term, spanish_occurrences)
                if consistency_issue:
                    issues.append(consistency_issue)
                    consistency_warnings += 1

        # Calculate quality score
        quality_score = self._calculate_quality_score(
            terms_found=terms_found_in_source,
            terms_correct=terms_correct,
            consistency_warnings=consistency_warnings
        )

        # Create metadata
        metadata = {
            "glossary_terms_total": terms_checked,
            "glossary_terms_in_source": terms_found_in_source,
            "glossary_terms_correct": terms_correct,
            "consistency_warnings": consistency_warnings
        }

        return self.create_result(
            chunk=chunk,
            issues=issues,
            score=quality_score,
            metadata=metadata
        )

    def _find_term_occurrences(
        self,
        text: str,
        term: str,
        term_type: GlossaryTermType
    ) -> list[int]:
        """
        Find all occurrences of a term in text and return character positions.

        Args:
            text: Text to search in
            term: Term to find (may be multi-word like "Mr. Bennet")
            term_type: Type of term (affects case sensitivity)

        Returns:
            List of character positions where term starts

        Example:
            >>> self._find_term_occurrences("Mr. Bennet said to Mr. Bennet", "Mr. Bennet", ...)
            [0, 20]
        """
        positions = []

        if not text or not term:
            return positions

        # Normalize term for matching
        search_term = term

        # For case-insensitive matching, use regex with IGNORECASE flag
        # Character names and places: case-insensitive (user might type "mr. darcy")
        # Concepts: case-insensitive
        # All types: case-insensitive for flexibility

        # Escape special regex characters in the term
        escaped_term = re.escape(search_term)

        # Always use word boundary at the end to ensure exact matches
        # This prevents "Bennet" from matching "Bennett"
        # For terms ending in punctuation (like "Mr."), the punctuation acts as boundary
        if search_term.endswith('.'):
            # Term ends with period - no word boundary needed
            pattern = escaped_term
        elif ' ' in search_term:
            # Multi-word term - add word boundary at end only
            pattern = escaped_term + r'\b'
        else:
            # Single word - use word boundaries on both sides
            pattern = r'\b' + escaped_term + r'\b'

        # Find all matches
        for match in re.finditer(pattern, text, re.IGNORECASE):
            positions.append(match.start())

        return positions

    def _find_all_spanish_variants(
        self,
        text: str,
        term: GlossaryTerm
    ) -> dict[str, list[int]]:
        """
        Find all Spanish variants (primary + alternatives) of a term in text.

        Args:
            text: Translated text to search
            term: Glossary term with spanish field and alternatives

        Returns:
            Dict mapping Spanish variant to list of positions
            Example: {"Sr. Bennet": [0, 50], "señor Bennet": [100]}
        """
        variants_found: dict[str, list[int]] = {}

        # Check primary translation
        positions = self._find_term_occurrences(text, term.spanish, term.type)
        if positions:
            variants_found[term.spanish] = positions

        # Check alternatives
        for alternative in term.alternatives:
            positions = self._find_term_occurrences(text, alternative, term.type)
            if positions:
                variants_found[alternative] = positions

        return variants_found

    def _check_term_translation(
        self,
        term: GlossaryTerm,
        english_occurrences: list[int],
        spanish_occurrences: dict[str, list[int]],
        source_text: str,
        translation_text: str
    ) -> list[Issue]:
        """
        Check if English term is correctly translated to Spanish.

        Args:
            term: Glossary term being validated
            english_occurrences: Positions where English term appears in source
            spanish_occurrences: Dict of Spanish variants found in translation
            source_text: Source text (for context in error messages)
            translation_text: Translation text (for context)

        Returns:
            List of Issue objects for violations
        """
        issues = []

        num_english = len(english_occurrences)
        num_spanish = sum(len(positions) for positions in spanish_occurrences.values())

        # Case 1: English term in source but no Spanish translation
        if num_english > 0 and num_spanish == 0:
            # Build alternatives list for error message
            alternatives_str = ""
            if term.alternatives:
                alts = "', '".join(term.alternatives)
                alternatives_str = f" (or alternatives: '{alts}')"

            issue = self.create_issue(
                severity=IssueLevel.ERROR,
                message=(
                    f"Glossary term '{term.english}' appears {num_english} time(s) in source "
                    f"but Spanish translation '{term.spanish}'{alternatives_str} not found in translation"
                ),
                location=f"source positions: {english_occurrences}",
                suggestion=f"Use '{term.spanish}' for '{term.english}'"
            )
            issues.append(issue)

        # Case 2: Count mismatch (might have missed some occurrences)
        elif num_english > 0 and num_spanish > 0 and num_english != num_spanish:
            issue = self.create_issue(
                severity=IssueLevel.WARNING,
                message=(
                    f"Glossary term '{term.english}' appears {num_english} time(s) in source "
                    f"but Spanish translation appears {num_spanish} time(s). Possible missing translation."
                ),
                location=f"source: {english_occurrences}, translation: {list(spanish_occurrences.keys())}",
                suggestion="Verify all occurrences are translated"
            )
            issues.append(issue)

        return issues

    def _check_consistency(
        self,
        term: GlossaryTerm,
        spanish_occurrences: dict[str, list[int]]
    ) -> Optional[Issue]:
        """
        Check if alternatives are used consistently within the chunk.

        Args:
            term: Glossary term being checked
            spanish_occurrences: Dict of Spanish variants found with their positions

        Returns:
            Issue if inconsistent usage detected, None otherwise

        Example of inconsistency:
            Using both "Sr. Bennet" and "señor Bennet" in same chunk
        """
        # If no alternatives defined, can't be inconsistent
        if not term.alternatives:
            return None

        # If only one variant used, it's consistent
        if len(spanish_occurrences) <= 1:
            return None

        # Multiple variants used - this is inconsistent
        variants_used = list(spanish_occurrences.keys())
        total_occurrences = sum(len(positions) for positions in spanish_occurrences.values())

        variants_str = "', '".join(variants_used)
        issue = self.create_issue(
            severity=IssueLevel.WARNING,
            message=(
                f"Inconsistent use of alternatives for '{term.english}': "
                f"found '{variants_str}' in same chunk ({total_occurrences} total occurrences). "
                f"Use one variant consistently."
            ),
            location=f"variants used: {variants_used}",
            suggestion=f"Use either '{term.spanish}' or one alternative consistently throughout"
        )

        return issue

    def _calculate_quality_score(
        self,
        terms_found: int,
        terms_correct: int,
        consistency_warnings: int
    ) -> float:
        """
        Calculate quality score based on glossary compliance.

        Args:
            terms_found: Number of glossary terms found in source
            terms_correct: Number of terms translated correctly
            consistency_warnings: Number of consistency warnings

        Returns:
            Score from 0.0 (all wrong) to 1.0 (perfect)
        """
        if terms_found == 0:
            # No terms to check - perfect score
            return 1.0

        # Base score: proportion of correct terms
        base_score = terms_correct / terms_found

        # Reduce score for consistency warnings (0.1 per warning, min 0.0)
        penalty = consistency_warnings * 0.1
        final_score = max(0.0, base_score - penalty)

        return round(final_score, 2)
