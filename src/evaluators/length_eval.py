"""
Length evaluator for translation quality.

Checks that the translated text length is within expected ranges
compared to the source text. Spanish translations typically run
10-20% longer than English.
"""

import re
from typing import Any

from ..models import Chunk, EvalResult, Issue, IssueLevel
from .base import BaseEvaluator


class LengthEvaluator(BaseEvaluator):
    """
    Evaluates translation length against source text.

    Checks:
    - Translation is not suspiciously short (< 0.5x source)
    - Translation is not excessively long (> 2.0x source)
    - Translation falls within expected range (1.1x-1.3x for Spanish)

    Configuration (passed in context dict):
    - min_ratio: Minimum acceptable ratio (default: 0.5)
    - max_ratio: Maximum acceptable ratio (default: 2.0)
    - expected_min: Expected minimum ratio (default: 1.1)
    - expected_max: Expected maximum ratio (default: 1.3)
    - count_by: "words" or "chars" (default: "words")
    """

    name = "length"
    version = "1.0.0"
    description = "Checks translation length against source text"

    # Default thresholds
    DEFAULT_MIN_RATIO = 0.5
    DEFAULT_MAX_RATIO = 2.0
    DEFAULT_EXPECTED_MIN = 1.1
    DEFAULT_EXPECTED_MAX = 1.3

    def evaluate(self, chunk: Chunk, context: dict[str, Any]) -> EvalResult:
        """
        Evaluate the length of the translation.

        Args:
            chunk: Chunk with source_text and translated_text
            context: Configuration options (see class docstring)

        Returns:
            EvalResult with length check results

        Raises:
            ValueError: If chunk.translated_text is None
        """
        if chunk.translated_text is None:
            raise ValueError(f"Chunk {chunk.id} has no translation")

        # Get configuration
        config = context.get("length_config", {})
        count_by = config.get("count_by", "words")
        min_ratio = config.get("min_ratio", self.DEFAULT_MIN_RATIO)
        max_ratio = config.get("max_ratio", self.DEFAULT_MAX_RATIO)
        expected_min = config.get("expected_min", self.DEFAULT_EXPECTED_MIN)
        expected_max = config.get("expected_max", self.DEFAULT_EXPECTED_MAX)

        # Count source and target
        if count_by == "chars":
            source_count = self._count_chars(chunk.source_text)
            target_count = self._count_chars(chunk.translated_text)
            unit = "characters"
        else:
            source_count = self._count_words(chunk.source_text)
            target_count = self._count_words(chunk.translated_text)
            unit = "words"

        # Calculate ratio
        ratio = self._calculate_ratio(source_count, target_count)

        # Determine issues
        issues = []
        severity = self._determine_severity(ratio, min_ratio, max_ratio, expected_min, expected_max)

        if severity:
            issue = self._create_length_issue(
                severity=severity,
                ratio=ratio,
                source_count=source_count,
                target_count=target_count,
                unit=unit,
                expected_min=expected_min,
                expected_max=expected_max,
            )
            issues.append(issue)

        # Calculate score (1.0 = perfect, decreases as ratio deviates)
        score = self._calculate_score(ratio, expected_min, expected_max)

        # Create metadata
        metadata = {
            "ratio": ratio,
            "source_count": source_count,
            "target_count": target_count,
            "unit": unit,
            "thresholds": {
                "min_ratio": min_ratio,
                "max_ratio": max_ratio,
                "expected_min": expected_min,
                "expected_max": expected_max,
            },
        }

        return self.create_result(chunk, issues, score, metadata)

    def _count_words(self, text: str) -> int:
        """
        Count words in text.

        Args:
            text: Text to count

        Returns:
            Number of words
        """
        # Split on whitespace and filter out empty strings
        words = text.split()
        return len(words)

    def _count_chars(self, text: str) -> int:
        """
        Count non-whitespace characters in text.

        Args:
            text: Text to count

        Returns:
            Number of non-whitespace characters
        """
        # Remove all whitespace and count
        return len(re.sub(r'\s+', '', text))

    def _calculate_ratio(self, source_count: int, target_count: int) -> float:
        """
        Calculate the target/source ratio.

        Args:
            source_count: Count from source text
            target_count: Count from target text

        Returns:
            Ratio (target/source), or 0.0 if source is empty
        """
        if source_count == 0:
            return 0.0
        return target_count / source_count

    def _determine_severity(
        self,
        ratio: float,
        min_ratio: float,
        max_ratio: float,
        expected_min: float,
        expected_max: float,
    ) -> IssueLevel | None:
        """
        Determine the severity level based on ratio.

        Args:
            ratio: Calculated target/source ratio
            min_ratio: Minimum acceptable ratio
            max_ratio: Maximum acceptable ratio
            expected_min: Expected minimum ratio
            expected_max: Expected maximum ratio

        Returns:
            IssueLevel or None if ratio is acceptable
        """
        # Critical errors: outside acceptable bounds
        if ratio < min_ratio or ratio > max_ratio:
            return IssueLevel.ERROR

        # Warnings: outside expected range but within acceptable bounds
        if ratio < expected_min or ratio > expected_max:
            return IssueLevel.WARNING

        # Within expected range
        return None

    def _create_length_issue(
        self,
        severity: IssueLevel,
        ratio: float,
        source_count: int,
        target_count: int,
        unit: str,
        expected_min: float,
        expected_max: float,
    ) -> Issue:
        """
        Create an issue describing the length problem.

        Args:
            severity: Error, warning, or info
            ratio: Calculated ratio
            source_count: Source text count
            target_count: Target text count
            unit: "words" or "characters"
            expected_min: Expected minimum ratio
            expected_max: Expected maximum ratio

        Returns:
            Issue instance
        """
        # Format the message based on whether it's too short or too long
        if ratio < expected_min:
            message = (
                f"Translation is {ratio:.1%} the length of the source "
                f"({target_count} vs {source_count} {unit}). "
                f"Expected at least {expected_min:.1%} ({expected_min * source_count:.0f} {unit})."
            )
            if severity == IssueLevel.ERROR:
                suggestion = "Check for missing content, truncated paragraphs, or incomplete translation."
            else:
                suggestion = "Translation is shorter than expected. Verify completeness."
        else:
            message = (
                f"Translation is {ratio:.1%} the length of the source "
                f"({target_count} vs {source_count} {unit}). "
                f"Expected at most {expected_max:.1%} ({expected_max * source_count:.0f} {unit})."
            )
            if severity == IssueLevel.ERROR:
                suggestion = "Check for duplicated content, added commentary, or overly verbose translation."
            else:
                suggestion = "Translation is longer than expected. Verify nothing was added."

        return self.create_issue(
            severity=severity,
            message=message,
            location=f"{source_count} {unit} -> {target_count} {unit}",
            suggestion=suggestion,
        )

    def _calculate_score(
        self,
        ratio: float,
        expected_min: float,
        expected_max: float,
    ) -> float:
        """
        Calculate a quality score based on how close the ratio is to expected.

        Score is 1.0 when ratio is within expected range, decreasing as it
        deviates from the expected range.

        Args:
            ratio: Calculated target/source ratio
            expected_min: Expected minimum ratio
            expected_max: Expected maximum ratio

        Returns:
            Score between 0.0 and 1.0
        """
        # Perfect score if within expected range
        if expected_min <= ratio <= expected_max:
            return 1.0

        # Calculate distance from expected range
        expected_mid = (expected_min + expected_max) / 2

        if ratio < expected_min:
            # Too short: calculate how far below expected_min
            deviation = (expected_min - ratio) / expected_min
        else:
            # Too long: calculate how far above expected_max
            deviation = (ratio - expected_max) / expected_max

        # Score decreases with deviation (capped at 0.0)
        score = max(0.0, 1.0 - deviation)
        return score
