"""
Grammar Evaluator

Uses LanguageTool to check for grammar, spelling, and style issues in Spanish
translations. Provides context-aware checking beyond simple dictionary lookups,
detecting issues like verb conjugation errors, gender agreement problems, and
style improvements.

Key features:
- Context-aware grammar checking (subject-verb agreement, tense, gender)
- Spelling checks (can be disabled with skip_spelling flag)
- Style suggestions (redundancy, wordiness, clarity)
- Glossary integration (excludes glossary terms from spelling checks)
- Dialect support (es, es-MX, es-ES, es-AR, etc.)
- Configurable severity mapping and filtering
"""

from typing import Any, Optional
from pathlib import Path

try:
    import language_tool_python
    LANGUAGETOOL_AVAILABLE = True
except ImportError:
    language_tool_python = None
    LANGUAGETOOL_AVAILABLE = False

from ..models import Chunk, EvalResult, Issue, IssueLevel, Glossary
from .base import BaseEvaluator


class GrammarEvaluator(BaseEvaluator):
    """
    Evaluates translation for grammar, spelling, and style issues using LanguageTool.

    Checks:
    - Grammar errors (verb conjugation, agreement, gender, tense)
    - Spelling mistakes (context-aware)
    - Style issues (redundancy, wordiness)
    - Punctuation errors

    Features:
    - Respects glossary terms (no false positives on spelling)
    - Grammar checks work regardless of glossary
    - Configurable severity mapping
    - Category-based filtering

    Example:
        evaluator = GrammarEvaluator()
        result = evaluator.evaluate(chunk, {
            'dialect': 'es-MX',
            'glossary': my_glossary,
            'skip_spelling': True,  # Run after dictionary check
            'max_issues': 50
        })
    """

    name = "grammar"
    version = "1.0.0"
    description = "Checks grammar, spelling, and style using LanguageTool"

    # Severity mapping from LanguageTool categories to our IssueLevel
    CATEGORY_SEVERITY = {
        'GRAMMAR': IssueLevel.ERROR,
        'TYPOS': IssueLevel.ERROR,
        'STYLE': IssueLevel.WARNING,
        'PUNCTUATION': IssueLevel.WARNING,
        'TYPOGRAPHY': IssueLevel.INFO,
        'REDUNDANCY': IssueLevel.INFO,
        'MISC': IssueLevel.WARNING,
    }

    def __init__(self, dialect: str = 'es'):
        """
        Initialize Grammar Evaluator with LanguageTool.

        Args:
            dialect: Spanish dialect code (es, es-MX, es-ES, es-AR, etc.)
                    Default: 'es' (generic Spanish)

        Raises:
            RuntimeError: If LanguageTool is not available
        """
        super().__init__()

        if not LANGUAGETOOL_AVAILABLE:
            raise RuntimeError(
                "LanguageTool is required for grammar evaluation. "
                "Install it with: pip install language-tool-python"
            )

        try:
            # Initialize LanguageTool with specified dialect
            # Note: First run downloads JAR file (~200MB)
            self.tool = language_tool_python.LanguageTool(dialect)
            self.dialect = dialect
        except Exception as e:
            raise RuntimeError(
                f"Failed to initialize LanguageTool for dialect '{dialect}': {e}"
            )

    def evaluate(self, chunk: Chunk, context: dict[str, Any]) -> EvalResult:
        """
        Evaluate chunk for grammar, spelling, and style issues.

        Args:
            chunk: Chunk containing translated text to check
            context: Configuration options:
                - dialect: str (overrides init dialect)
                - glossary: Glossary (exclude terms from TYPOS)
                - ignore_rules: list[str] (specific rule IDs to skip)
                - ignore_categories: list[str] (categories to skip, e.g. ['TYPOS'])
                - skip_spelling: bool (convenience for ignore_categories=['TYPOS'])
                - max_issues: int (default 50)

        Returns:
            EvalResult with issues found
        """
        issues: list[Issue] = []

        # Check if translation exists
        if not chunk.translated_text or not chunk.translated_text.strip():
            # No translation to check
            return self.create_result(
                chunk=chunk,
                issues=[],
                score=1.0,
                metadata={"checks_performed": 0}
            )

        # Handle dialect override
        dialect = context.get('dialect', self.dialect)
        if dialect != self.dialect:
            # Reinitialize with new dialect
            self.tool = language_tool_python.LanguageTool(dialect)
            self.dialect = dialect

        # Run LanguageTool check
        matches = self._check_grammar(chunk.translated_text)

        # Process matches
        for match in matches:
            # Check if this match should be ignored
            if self._should_ignore_match(match, context):
                continue

            # Convert to Issue
            issue = self._convert_match_to_issue(match)
            issues.append(issue)

        # Apply max_issues limit
        max_issues = context.get('max_issues', 50)
        if len(issues) > max_issues:
            # Sort by severity (ERROR > WARNING > INFO)
            issues.sort(key=lambda iss: (
                0 if iss.severity == IssueLevel.ERROR else
                1 if iss.severity == IssueLevel.WARNING else 2
            ))
            issues = issues[:max_issues]

        # Calculate score
        score = self._calculate_score(issues)

        return self.create_result(
            chunk=chunk,
            issues=issues,
            score=score,
            metadata={
                "checks_performed": len(matches),
                "issues_reported": len(issues),
                "dialect": dialect
            }
        )

    def _check_grammar(self, text: str) -> list:
        """
        Run LanguageTool check on text.

        Args:
            text: Text to check

        Returns:
            List of LanguageTool Match objects
        """
        try:
            matches = self.tool.check(text)
            return matches
        except Exception as e:
            # If LanguageTool fails, log and return empty list
            # Don't fail the entire evaluation
            import logging
            logging.warning(f"LanguageTool check failed: {e}")
            return []

    def _convert_match_to_issue(self, match) -> Issue:
        """
        Convert LanguageTool Match to our Issue format.

        Args:
            match: LanguageTool Match object

        Returns:
            Issue instance
        """
        # Determine severity
        severity = self._determine_severity(match)

        # Build message
        message = match.message
        if hasattr(match, 'context') and match.context:
            # Include context if available
            message = f"{message} Context: '{match.context}'"

        # Build suggestion from replacements
        suggestion = None
        if match.replacements:
            # Take top 3 suggestions
            suggestions_list = match.replacements[:3]
            suggestion = f"Consider: {', '.join(suggestions_list)}"

        # Build location
        location = f"char {match.offset}"
        if match.errorLength:
            location += f"-{match.offset + match.errorLength}"

        return self.create_issue(
            severity=severity,
            message=message,
            location=location,
            suggestion=suggestion
        )

    def _determine_severity(self, match) -> IssueLevel:
        """
        Determine severity level for a LanguageTool match.

        Args:
            match: LanguageTool Match object

        Returns:
            IssueLevel (ERROR, WARNING, or INFO)
        """
        # Try to get category from match
        category = getattr(match, 'category', 'MISC')

        # Map category to severity
        return self.CATEGORY_SEVERITY.get(category, IssueLevel.WARNING)

    def _should_ignore_match(self, match, context: dict) -> bool:
        """
        Determine if a match should be ignored based on context.

        Args:
            match: LanguageTool Match object
            context: Evaluation context

        Returns:
            True if match should be ignored, False otherwise
        """
        # Check skip_spelling convenience flag
        if context.get('skip_spelling', False):
            if 'ignore_categories' not in context:
                context['ignore_categories'] = []
            if 'TYPOS' not in context['ignore_categories']:
                context['ignore_categories'].append('TYPOS')

        # Check ignore_categories
        ignore_categories = context.get('ignore_categories', [])
        match_category = getattr(match, 'category', None)
        if match_category in ignore_categories:
            return True

        # Check ignore_rules
        ignore_rules = context.get('ignore_rules', [])
        rule_id = getattr(match, 'ruleId', None)
        if rule_id in ignore_rules:
            return True

        # Check glossary for TYPOS category
        if match_category == 'TYPOS':
            glossary = context.get('glossary')
            if glossary:
                # Extract the word being flagged (will be in Spanish)
                word = self._extract_word_from_match(match)
                if word and glossary.find_term_by_spanish(word):
                    return True  # Ignore - it's in glossary

        return False  # Don't ignore

    def _extract_word_from_match(self, match) -> Optional[str]:
        """
        Extract the flagged word from a LanguageTool match.

        Args:
            match: LanguageTool Match object

        Returns:
            The flagged word, or None if can't extract
        """
        try:
            # The matched text is in the context or can be extracted
            if hasattr(match, 'matchedText') and match.matchedText:
                return match.matchedText.strip()

            # Try to extract from context
            if hasattr(match, 'context') and match.context:
                # Context usually shows the error in a snippet
                # Extract word at match position
                return match.context.strip()

            return None
        except (AttributeError, TypeError):
            return None

    def _calculate_score(self, issues: list[Issue]) -> float:
        """
        Calculate quality score based on issues found.

        Args:
            issues: List of grammar/spelling/style issues

        Returns:
            Score from 0.0 (many issues) to 1.0 (no issues)
        """
        if not issues:
            return 1.0

        # Penalize based on severity and count
        penalty = 0.0
        for issue in issues:
            if issue.severity == IssueLevel.ERROR:
                penalty += 0.05  # 5% per error
            elif issue.severity == IssueLevel.WARNING:
                penalty += 0.02  # 2% per warning
            else:  # INFO
                penalty += 0.01  # 1% per info

        # Calculate score (minimum 0.0)
        score = max(0.0, 1.0 - penalty)
        return score
