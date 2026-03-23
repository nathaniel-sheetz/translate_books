"""
Completeness evaluator for translation quality.

Checks that translations are complete, not truncated, and don't contain
placeholder text or indicators of incomplete work.
"""

import re
from typing import Any

from ..models import Chunk, EvalResult, Issue, IssueLevel
from .base import BaseEvaluator


class CompletenessEvaluator(BaseEvaluator):
    """
    Evaluates translation completeness.

    Checks:
    - Translation is not None or empty
    - Translation does not contain placeholder text
    - Translation does not appear truncated (ends mid-sentence)
    - Special markers are preserved from source to translation

    Configuration (passed in context dict):
    - strict_markers: Fail on missing markers (default: False, warning only)
    - custom_placeholders: Additional placeholder patterns to check (default: [])
    - check_markers: Enable marker checking (default: True)
    """

    name = "completeness"
    version = "1.0.0"
    description = "Checks translation completeness and integrity"

    # Common placeholder patterns
    PLACEHOLDER_PATTERNS = [
        r'\bTODO\b',
        r'\bFIXME\b',
        r'\bXXX\b',
        r'\[.*?TRANSLATION.*?\]',
        r'\[.*?INSERT.*?\]',
        r'\[.*?MISSING.*?\]',
        r'\[.*?TBD.*?\]',
        r'\[.*?PLACEHOLDER.*?\]',
        r'<.*?TRANSLATION.*?>',
        r'<<<.*?>>>',
        r'\{\{.*?TRANSLATION.*?\}\}',
    ]

    # Special markers to check for (section breaks, dividers, etc.)
    SPECIAL_MARKERS = [
        r'^---+$',          # Horizontal rules (----)
        r'^\*\s*\*\s*\*$',  # Star dividers (* * *)
        r'^#{1,6}\s',       # Markdown headers
        r'^\d+\.$',         # Numbered lists (1. 2. etc.)
        r'^[-*+]\s',        # Bullet lists
    ]

    def evaluate(self, chunk: Chunk, context: dict[str, Any]) -> EvalResult:
        """
        Evaluate the completeness of the translation.

        Args:
            chunk: Chunk with source_text and translated_text
            context: Configuration options (see class docstring)

        Returns:
            EvalResult with completeness check results

        Raises:
            ValueError: If chunk.translated_text is None
        """
        if chunk.translated_text is None:
            raise ValueError(f"Chunk {chunk.id} has no translation")

        # Get configuration
        config = context.get("completeness_config", {})
        strict_markers = config.get("strict_markers", False)
        custom_placeholders = config.get("custom_placeholders", [])
        check_markers = config.get("check_markers", True)

        issues = []

        # Check 1: Empty translation
        if self._is_empty(chunk.translated_text):
            issues.append(Issue(
                severity=IssueLevel.ERROR,
                message="Translation is empty",
                location="translated_text",
                suggestion="Translation must contain text"
            ))
            # If empty, skip other checks
            return self.create_result(
                chunk=chunk,
                issues=issues,
                score=0.0,
                metadata={"empty": True}
            )

        # Check 2: Placeholder text
        placeholder_issues = self._check_placeholders(
            chunk.translated_text,
            custom_placeholders
        )
        issues.extend(placeholder_issues)

        # Check 3: Truncation
        truncation_issue = self._check_truncation(chunk.translated_text)
        if truncation_issue:
            issues.append(truncation_issue)

        # Check 4: Special markers preservation
        if check_markers:
            marker_issues = self._check_markers(
                chunk.source_text,
                chunk.translated_text,
                strict=strict_markers
            )
            issues.extend(marker_issues)

        # Calculate score
        score = self._calculate_score(issues)

        # Create metadata
        metadata = {
            "empty": False,
            "has_placeholders": len(placeholder_issues) > 0,
            "appears_truncated": truncation_issue is not None,
            "marker_issues_count": len([i for i in issues if "marker" in i.message.lower()]),
        }

        return self.create_result(chunk, issues, score, metadata)

    def _is_empty(self, text: str) -> bool:
        """
        Check if text is empty or contains only whitespace.

        Args:
            text: Text to check

        Returns:
            True if empty, False otherwise
        """
        return not text or not text.strip()

    def _check_placeholders(
        self,
        text: str,
        custom_patterns: list[str]
    ) -> list[Issue]:
        """
        Check for placeholder text patterns.

        Args:
            text: Translated text to check
            custom_patterns: Additional regex patterns to check

        Returns:
            List of issues found
        """
        issues = []
        all_patterns = self.PLACEHOLDER_PATTERNS + custom_patterns

        for pattern in all_patterns:
            matches = list(re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE))
            for match in matches:
                issues.append(Issue(
                    severity=IssueLevel.ERROR,
                    message=f"Placeholder text found: '{match.group()}'",
                    location=f"char {match.start()}-{match.end()}",
                    suggestion="Replace placeholder with actual translation"
                ))

        return issues

    def _check_truncation(self, text: str) -> Issue | None:
        """
        Check if text appears to be truncated.

        Truncation indicators:
        - Ends without proper sentence-ending punctuation
        - Ends mid-word (unlikely but check for it)
        - Very short text that seems incomplete

        Args:
            text: Translated text to check

        Returns:
            Issue if truncation detected, None otherwise
        """
        text = text.strip()

        if not text:
            return None

        # Check if ends with proper punctuation
        # Spanish: . ! ? ... » " ) ]
        # Allow closing quotes, parentheses, brackets after punctuation
        proper_endings = r'[.!?…»")\]—]$'

        if not re.search(proper_endings, text):
            # Get last 50 chars for context
            context = text[-50:] if len(text) > 50 else text
            return Issue(
                severity=IssueLevel.WARNING,
                message="Translation may be truncated (no proper ending punctuation)",
                location=f"end of text: '...{context}'",
                suggestion="Ensure translation ends with proper punctuation (. ! ? etc.)"
            )

        return None

    def _check_markers(
        self,
        source: str,
        translation: str,
        strict: bool = False
    ) -> list[Issue]:
        """
        Check that special markers from source appear in translation.

        Special markers include:
        - Horizontal rules (---)
        - Star dividers (* * *)
        - Section breaks
        - Markdown formatting

        Args:
            source: Source text
            translation: Translated text
            strict: If True, missing markers are errors; if False, warnings

        Returns:
            List of issues found
        """
        issues = []

        for pattern in self.SPECIAL_MARKERS:
            source_matches = list(re.finditer(pattern, source, re.MULTILINE))
            translation_matches = list(re.finditer(pattern, translation, re.MULTILINE))

            source_count = len(source_matches)
            translation_count = len(translation_matches)

            if source_count > translation_count:
                severity = IssueLevel.ERROR if strict else IssueLevel.WARNING
                missing_count = source_count - translation_count

                # Get example of missing marker
                example = source_matches[0].group() if source_matches else "unknown"

                issues.append(Issue(
                    severity=severity,
                    message=f"Special marker may be missing: '{example}' "
                            f"(found {translation_count} in translation, "
                            f"expected {source_count})",
                    location="special markers",
                    suggestion=f"Preserve special markers from source text"
                ))

        return issues

    def _calculate_score(self, issues: list[Issue]) -> float:
        """
        Calculate quality score based on issues found.

        Score calculation:
        - Start at 1.0
        - Each ERROR: -0.3
        - Each WARNING: -0.1
        - Each INFO: -0.05
        - Minimum score: 0.0

        Args:
            issues: List of issues found

        Returns:
            Quality score (0.0-1.0)
        """
        score = 1.0

        for issue in issues:
            if issue.severity == IssueLevel.ERROR:
                score -= 0.3
            elif issue.severity == IssueLevel.WARNING:
                score -= 0.1
            elif issue.severity == IssueLevel.INFO:
                score -= 0.05

        return max(0.0, score)
