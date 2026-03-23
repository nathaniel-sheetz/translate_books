"""
Base evaluator class for translation quality checks.

All evaluators should inherit from BaseEvaluator and implement the evaluate method.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from ..models import Chunk, EvalResult, Issue, IssueLevel


class BaseEvaluator(ABC):
    """
    Abstract base class for all evaluators.

    Subclasses must implement the evaluate() method and define
    name, version, and description attributes.
    """

    # Subclasses must set these
    name: str = "base"
    version: str = "1.0.0"
    description: str = "Base evaluator"

    @abstractmethod
    def evaluate(self, chunk: Chunk, context: dict[str, Any]) -> EvalResult:
        """
        Run evaluation on a chunk.

        Args:
            chunk: The chunk to evaluate (must have translated_text)
            context: Additional context (e.g., glossary, config)

        Returns:
            EvalResult with pass/fail status and any issues found

        Raises:
            ValueError: If chunk.translated_text is None
        """
        pass

    def create_issue(
        self,
        severity: IssueLevel,
        message: str,
        location: str | None = None,
        suggestion: str | None = None,
    ) -> Issue:
        """
        Helper to create an Issue with consistent formatting.

        Args:
            severity: Error, warning, or info
            message: Description of the issue
            location: Where the issue was found (optional)
            suggestion: How to fix it (optional)

        Returns:
            Issue instance
        """
        return Issue(
            severity=severity,
            message=message,
            location=location,
            suggestion=suggestion,
        )

    def calculate_pass_fail(self, issues: list[Issue]) -> bool:
        """
        Determine if the evaluation passed based on issues found.

        By default, any error-level issue causes failure.

        Args:
            issues: List of issues found during evaluation

        Returns:
            True if passed (no errors), False otherwise
        """
        return not any(issue.severity == IssueLevel.ERROR for issue in issues)

    def issue_summary(self, issues: list[Issue]) -> dict[str, int]:
        """
        Count issues by severity level.

        Args:
            issues: List of issues to summarize

        Returns:
            Dict with counts: {"errors": N, "warnings": N, "info": N}
        """
        errors = sum(1 for issue in issues if issue.severity == IssueLevel.ERROR)
        warnings = sum(1 for issue in issues if issue.severity == IssueLevel.WARNING)
        info = sum(1 for issue in issues if issue.severity == IssueLevel.INFO)

        return {
            "errors": errors,
            "warnings": warnings,
            "info": info,
        }

    def create_result(
        self,
        chunk: Chunk,
        issues: list[Issue],
        score: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EvalResult:
        """
        Helper to create an EvalResult with consistent formatting.

        Args:
            chunk: The chunk that was evaluated
            issues: List of issues found
            score: Optional score 0.0-1.0
            metadata: Optional evaluator-specific metadata

        Returns:
            EvalResult instance
        """
        passed = self.calculate_pass_fail(issues)

        return EvalResult(
            eval_name=self.name,
            eval_version=self.version,
            target_id=chunk.id,
            target_type="chunk",
            passed=passed,
            score=score,
            issues=issues,
            metadata=metadata or {},
            executed_at=datetime.now(),
        )

    def format_issues(self, issues: list[Issue]) -> str:
        """
        Format issues as human-readable text.

        Args:
            issues: List of issues to format

        Returns:
            Formatted string with all issues
        """
        if not issues:
            return "No issues found."

        lines = []
        for issue in issues:
            severity_marker = {
                IssueLevel.ERROR: "❌ ERROR",
                IssueLevel.WARNING: "⚠️  WARNING",
                IssueLevel.INFO: "ℹ️  INFO",
            }.get(issue.severity, "•")

            lines.append(f"{severity_marker}: {issue.message}")

            if issue.location:
                lines.append(f"  Location: {issue.location}")

            if issue.suggestion:
                lines.append(f"  Suggestion: {issue.suggestion}")

            lines.append("")  # Blank line between issues

        return "\n".join(lines)

    def should_fail(self, result: EvalResult) -> bool:
        """
        Determine if errors in the result should cause pipeline failure.

        By default, any error-level issue is considered critical.
        Subclasses can override for custom logic.

        Args:
            result: The evaluation result to check

        Returns:
            True if pipeline should halt, False otherwise
        """
        return result.error_count > 0

    def __repr__(self) -> str:
        """String representation of evaluator."""
        return f"{self.__class__.__name__}(name={self.name}, version={self.version})"
