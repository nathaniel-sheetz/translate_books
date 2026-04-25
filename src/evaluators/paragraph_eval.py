"""
Paragraph evaluator for translation quality.

Checks that the paragraph structure is preserved between source and translation.
Paragraphs are defined as text blocks separated by double newlines.
"""

import re
from typing import Any

from ..models import Chunk, EvalResult, Issue, IssueLevel
from .base import BaseEvaluator


class ParagraphEvaluator(BaseEvaluator):
    """
    Evaluates paragraph structure preservation in translation (dialogue-aware).

    Spanish dialogue convention uses a raya (—) at the start of each speech
    turn, which often adds paragraphs that have no counterpart in the English
    source.  The evaluator grants a "dialogue budget" equal to the number of
    translation paragraphs that begin with — so those extra splits are not
    flagged as errors.

    Checks:
    - Paragraph count matches between source and translation
    - No merged paragraphs (fewer paragraphs in translation)
    - No split paragraphs beyond the dialogue-raya budget
    - Handles different newline conventions (\n, \r\n)

    Configuration (passed in context dict):
    - allow_mismatch: Allow paragraph count differences (default: False)
    - mismatch_threshold: Max allowed difference if allow_mismatch=True (default: 0)
    """

    name = "paragraph"
    version = "1.1.0"
    description = "Checks paragraph structure preservation in translation"

    def evaluate(self, chunk: Chunk, context: dict[str, Any]) -> EvalResult:
        """
        Evaluate the paragraph structure of the translation.

        Args:
            chunk: Chunk with source_text and translated_text
            context: Configuration options (see class docstring)

        Returns:
            EvalResult with paragraph check results

        Raises:
            ValueError: If chunk.translated_text is None
        """
        if chunk.translated_text is None:
            raise ValueError(f"Chunk {chunk.id} has no translation")

        # Get configuration
        config = context.get("paragraph_config", {})
        allow_mismatch = config.get("allow_mismatch", False)
        mismatch_threshold = config.get("mismatch_threshold", 0)

        # Normalize newlines for consistent parsing
        source_normalized = self._normalize_newlines(chunk.source_text)
        translation_normalized = self._normalize_newlines(chunk.translated_text)

        # Count paragraphs
        source_count = self._count_paragraphs(source_normalized)
        translation_count = self._count_paragraphs(translation_normalized)
        dialogue_count = self._count_dialogue_paragraphs(translation_normalized)

        # Determine unexplained delta (dialogue-aware)
        if translation_count < source_count:
            unexplained = source_count - translation_count  # dropped
        elif translation_count > source_count + dialogue_count:
            unexplained = translation_count - source_count - dialogue_count  # over-split beyond budget
        else:
            unexplained = 0  # within dialogue budget

        # Determine issues
        issues = []

        if unexplained > 0:
            if allow_mismatch and unexplained <= mismatch_threshold:
                issue = self._create_paragraph_issue(
                    severity=IssueLevel.WARNING,
                    source_count=source_count,
                    translation_count=translation_count,
                    dialogue_count=dialogue_count,
                )
                issues.append(issue)
            else:
                issue = self._create_paragraph_issue(
                    severity=IssueLevel.ERROR,
                    source_count=source_count,
                    translation_count=translation_count,
                    dialogue_count=dialogue_count,
                )
                issues.append(issue)

        # Calculate score (1.0 if perfect match, decreases with difference)
        score = self._calculate_score(source_count, translation_count, dialogue_count)

        # Create metadata
        metadata = {
            "source_paragraphs": source_count,
            "translation_paragraphs": translation_count,
            "dialogue_paragraphs": dialogue_count,
            "difference": abs(source_count - translation_count),
            "unexplained_delta": unexplained,
            "match": unexplained == 0,
        }

        return self.create_result(chunk, issues, score, metadata)

    def _normalize_newlines(self, text: str) -> str:
        """
        Normalize different newline conventions to \n.

        Handles:
        - Windows style (\r\n)
        - Unix style (\n)
        - Old Mac style (\r)

        Args:
            text: Text with potentially mixed newline styles

        Returns:
            Text with normalized \n newlines
        """
        # Replace \r\n with \n, then any remaining \r with \n
        text = text.replace('\r\n', '\n')
        text = text.replace('\r', '\n')
        return text

    def _count_paragraphs(self, text: str) -> int:
        """
        Count paragraphs in text.

        Paragraphs are defined as blocks of text separated by
        one or more blank lines (double newlines or more).

        Args:
            text: Text to count paragraphs in (should be newline-normalized)

        Returns:
            Number of non-empty paragraphs
        """
        # Split on one or more blank lines (two or more consecutive newlines)
        # This handles \n\n, \n\n\n, etc.
        paragraphs = re.split(r'\n\s*\n', text.strip())

        # Filter out empty paragraphs (whitespace-only)
        non_empty_paragraphs = [p for p in paragraphs if p.strip()]

        return len(non_empty_paragraphs)

    def _count_dialogue_paragraphs(self, text: str) -> int:
        """
        Count paragraphs that begin with a raya (—, U+2014).

        These correspond to Spanish dialogue turns that may introduce
        extra paragraph splits relative to the English source.

        Args:
            text: Text to inspect (should be newline-normalized)

        Returns:
            Number of paragraphs whose first non-whitespace character is —
        """
        paragraphs = re.split(r'\n\s*\n', text.strip())
        non_empty = [p for p in paragraphs if p.strip()]
        return sum(1 for p in non_empty if p.strip().startswith('\u2014'))

    def _create_paragraph_issue(
        self,
        severity: IssueLevel,
        source_count: int,
        translation_count: int,
        dialogue_count: int,
    ) -> Issue:
        """
        Create an issue describing the paragraph mismatch.

        Args:
            severity: Error or warning level
            source_count: Number of paragraphs in source
            translation_count: Number of paragraphs in translation
            dialogue_count: Number of dialogue-led paragraphs in translation

        Returns:
            Issue instance
        """
        difference = abs(source_count - translation_count)

        if translation_count < source_count:
            # Fewer paragraphs - possible merge/drop
            message = (
                f"source has {source_count} paragraph(s), "
                f"translation has {translation_count} ({dialogue_count} dialogue-led). "
                f"{difference} paragraph(s) appear to be merged or dropped."
            )
            suggestion = "Check if paragraphs were incorrectly merged or dropped."
        elif translation_count > source_count:
            # More paragraphs - possible over-split beyond dialogue budget
            unexplained = translation_count - source_count - dialogue_count
            message = (
                f"source has {source_count}, "
                f"translation has {translation_count} ({dialogue_count} dialogue-led). "
                f"{unexplained} extra paragraph(s) beyond what Spanish dialogue convention explains."
            )
            suggestion = "Check for incorrectly split paragraphs beyond dialogue convention."
        else:
            # Should not happen, but handle it
            message = f"Paragraph counts match: {source_count} paragraph(s)."
            suggestion = None

        return self.create_issue(
            severity=severity,
            message=message,
            location=f"{source_count} paragraphs -> {translation_count} paragraphs ({dialogue_count} dialogue-led)",
            suggestion=suggestion,
        )

    def _calculate_score(
        self,
        source_count: int,
        translation_count: int,
        dialogue_count: int = 0,
    ) -> float:
        """
        Calculate a quality score based on paragraph count match (dialogue-aware).

        Score is 1.0 if the unexplained delta is zero, decreasing as the
        unexplained difference increases.

        Args:
            source_count: Number of paragraphs in source
            translation_count: Number of paragraphs in translation
            dialogue_count: Number of dialogue-led paragraphs in translation

        Returns:
            Score between 0.0 and 1.0
        """
        # Avoid division by zero
        if source_count == 0:
            return 0.0 if translation_count > 0 else 1.0

        unexplained = max(
            source_count - translation_count,
            translation_count - source_count - dialogue_count,
            0,
        )

        if unexplained == 0:
            return 1.0

        max_count = max(source_count, translation_count)
        return max(0.0, 1.0 - (unexplained / max_count))
