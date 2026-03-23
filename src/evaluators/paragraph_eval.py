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
    Evaluates paragraph structure preservation in translation.

    Checks:
    - Paragraph count matches between source and translation
    - No merged paragraphs (fewer paragraphs in translation)
    - No split paragraphs (more paragraphs in translation)
    - Handles different newline conventions (\n, \r\n)

    Configuration (passed in context dict):
    - allow_mismatch: Allow paragraph count differences (default: False)
    - mismatch_threshold: Max allowed difference if allow_mismatch=True (default: 0)
    """

    name = "paragraph"
    version = "1.0.0"
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

        # Determine issues
        issues = []

        if source_count != translation_count:
            # Check if within allowed threshold
            difference = abs(source_count - translation_count)

            if allow_mismatch and difference <= mismatch_threshold:
                # Within threshold, just a warning
                issue = self._create_paragraph_issue(
                    severity=IssueLevel.WARNING,
                    source_count=source_count,
                    translation_count=translation_count,
                )
                issues.append(issue)
            else:
                # Outside threshold or mismatch not allowed, error
                issue = self._create_paragraph_issue(
                    severity=IssueLevel.ERROR,
                    source_count=source_count,
                    translation_count=translation_count,
                )
                issues.append(issue)

        # Calculate score (1.0 if perfect match, decreases with difference)
        score = self._calculate_score(source_count, translation_count)

        # Create metadata
        metadata = {
            "source_paragraphs": source_count,
            "translation_paragraphs": translation_count,
            "difference": abs(source_count - translation_count),
            "match": source_count == translation_count,
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

    def _create_paragraph_issue(
        self,
        severity: IssueLevel,
        source_count: int,
        translation_count: int,
    ) -> Issue:
        """
        Create an issue describing the paragraph mismatch.

        Args:
            severity: Error or warning level
            source_count: Number of paragraphs in source
            translation_count: Number of paragraphs in translation

        Returns:
            Issue instance
        """
        difference = abs(source_count - translation_count)

        if translation_count < source_count:
            # Fewer paragraphs - possible merge
            message = (
                f"Paragraph count mismatch: source has {source_count} paragraph(s), "
                f"translation has {translation_count} paragraph(s). "
                f"{difference} paragraph(s) appear to be merged."
            )
            suggestion = (
                "Check if paragraphs were incorrectly merged. "
                "Ensure each source paragraph maps to a translation paragraph."
            )
        elif translation_count > source_count:
            # More paragraphs - possible split
            message = (
                f"Paragraph count mismatch: source has {source_count} paragraph(s), "
                f"translation has {translation_count} paragraph(s). "
                f"{difference} extra paragraph(s) in translation."
            )
            suggestion = (
                "Check if paragraphs were incorrectly split. "
                "Ensure translation maintains source paragraph structure."
            )
        else:
            # Should not happen, but handle it
            message = f"Paragraph counts match: {source_count} paragraph(s)."
            suggestion = None

        return self.create_issue(
            severity=severity,
            message=message,
            location=f"{source_count} paragraphs -> {translation_count} paragraphs",
            suggestion=suggestion,
        )

    def _calculate_score(
        self,
        source_count: int,
        translation_count: int,
    ) -> float:
        """
        Calculate a quality score based on paragraph count match.

        Score is 1.0 if counts match exactly, decreasing as the
        difference increases.

        Args:
            source_count: Number of paragraphs in source
            translation_count: Number of paragraphs in translation

        Returns:
            Score between 0.0 and 1.0
        """
        if source_count == translation_count:
            return 1.0

        # Avoid division by zero
        if source_count == 0:
            return 0.0 if translation_count > 0 else 1.0

        # Calculate proportional difference
        difference = abs(source_count - translation_count)
        max_count = max(source_count, translation_count)

        # Score decreases linearly with difference
        # difference of 1 in 10 paragraphs = 0.9 score
        # difference of 5 in 10 paragraphs = 0.5 score
        score = max(0.0, 1.0 - (difference / max_count))

        return score
