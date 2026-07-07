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
import re
from collections import defaultdict

try:
    import language_tool_python
    LANGUAGETOOL_AVAILABLE = True
except ImportError:
    language_tool_python = None
    LANGUAGETOOL_AVAILABLE = False

from ..models import Chunk, EvalResult, Issue, IssueLevel, Glossary
from ..utils.text_utils import image_placeholder_ranges, strip_image_placeholders
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

    # LanguageTool spell-checker rules whose findings overlap the dictionary
    # evaluator (they flag unknown/misspelled words). skip_spelling suppresses
    # only these — not the whole TYPOS category — so accent/real-word TYPOS rules
    # (tu/tú, más/mas, él/el, ...) still surface. Those fire on valid dictionary
    # words, which the dictionary evaluator cannot catch, so keeping them adds no
    # double-reporting.
    SPELLING_RULE_ID_PREFIXES = (
        "MORFOLOGIK_RULE",
        "HUNSPELL_RULE",
        "HUNSPELL_NO_SUGGEST_RULE",
    )

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
                - skip_spelling: bool (suppress only the unknown-word spell rules
                  MORFOLOGIK_RULE_*/HUNSPELL_*; accent/real-word TYPOS are still
                  reported)
                - max_issues: int (default 50)

        Returns:
            EvalResult with issues found
        """
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

        # Strip [IMAGE:...] placeholders so tokens like "IMAGE", "jpg", and
        # filename fragments don't get flagged as spelling/grammar issues.
        # Equal-length whitespace keeps match.offset aligned to the original text.
        placeholder_ranges = image_placeholder_ranges(chunk.translated_text)
        text_to_check = strip_image_placeholders(chunk.translated_text)

        # Run LanguageTool check
        matches = self._check_grammar(text_to_check)

        # Process matches (deduplicated by rule + flagged word)
        grouped_matches: dict[tuple, list] = defaultdict(list)

        for match in matches:
            # Skip matches whose offset falls inside a replaced placeholder
            # (the whitespace run we substituted triggers a spurious spaces warning)
            if any(start <= match.offset < end for start, end in placeholder_ranges):
                continue

            # Check if this match should be ignored
            if self._should_ignore_match(match, context, text_to_check):
                continue

            rule_id = getattr(match, "rule_id", "") or ""
            flagged_word = self._extract_word_from_match(match, text_to_check) or ""
            if rule_id or flagged_word:
                key = (rule_id, flagged_word)
            else:
                # Neither the rule id nor the flagged word is known; keying on
                # ("", "") would merge unrelated findings into one bogus
                # "(found N time(s))", so disambiguate by offset to keep each
                # match a separate reported issue.
                key = ("", "", match.offset)
            grouped_matches[key].append(match)

        issues: list[Issue] = []
        for match_group in grouped_matches.values():
            issue = self._convert_match_group_to_issue(match_group)
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

    def _convert_match_group_to_issue(self, matches: list) -> Issue:
        """
        Convert one or more LanguageTool matches (same rule + word) to a single Issue.

        Args:
            matches: Group of LanguageTool Match objects

        Returns:
            Issue instance
        """
        match = matches[0]
        severity = self._determine_severity(match)

        message = match.message
        if hasattr(match, "context") and match.context:
            message = f"{message} Context: '{match.context}'"
        if len(matches) > 1:
            message = f"{message} (found {len(matches)} time(s))"

        suggestion = None
        if match.replacements:
            suggestions_list = match.replacements[:3]
            suggestion = f"Consider: {', '.join(suggestions_list)}"

        offsets = sorted(m.offset for m in matches)
        if len(offsets) == 1:
            location = f"char {offsets[0]}"
            if match.error_length:
                location += f"-{offsets[0] + match.error_length}"
        elif len(offsets) <= 3:
            location = f"char positions: {', '.join(str(o) for o in offsets)}"
        else:
            location = (
                f"char positions: {', '.join(str(o) for o in offsets[:3])}, "
                f"... ({len(offsets)} total)"
            )

        return self.create_issue(
            severity=severity,
            message=message,
            location=location,
            suggestion=suggestion,
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

    def _should_ignore_match(
        self, match, context: dict, text: str = ""
    ) -> bool:
        """
        Determine if a match should be ignored based on context.

        Args:
            match: LanguageTool Match object
            context: Evaluation context
            text: Full checked text (offsets are relative to this string)

        Returns:
            True if match should be ignored, False otherwise
        """
        # skip_spelling suppresses only LanguageTool's unknown-word spell checker
        # (the SPELLING_RULE_ID_PREFIXES: MORFOLOGIK_RULE_*/HUNSPELL_*), which the
        # dictionary evaluator already owns. Real-word / diacritic TYPOS rules
        # (tu/tú, más/mas, él/el, ...) are kept — the dictionary can't catch those,
        # so there's no double-reporting.
        if context.get('skip_spelling', False):
            rule_id = getattr(match, 'rule_id', '') or ''
            if any(rule_id.startswith(p) for p in self.SPELLING_RULE_ID_PREFIXES):
                return True

        # Check ignore_categories
        ignore_categories = context.get('ignore_categories', [])
        match_category = getattr(match, 'category', None)
        if match_category in ignore_categories:
            return True

        # Check ignore_rules
        ignore_rules = context.get('ignore_rules', [])
        rule_id = getattr(match, 'rule_id', None)
        if rule_id in ignore_rules:
            return True

        # Check glossary for TYPOS category
        if match_category == 'TYPOS':
            glossary = context.get('glossary')
            if glossary:
                word = self._extract_word_from_match(match, text)
                if word and glossary.matches_word(word):
                    return True  # Ignore - it's in glossary

        return False  # Don't ignore

    def _extract_word_from_match(self, match, text: str = "") -> Optional[str]:
        """
        Extract the flagged word from a LanguageTool match.

        Args:
            match: LanguageTool Match object
            text: Full checked text (offsets are relative to this string)

        Returns:
            The flagged word, or None if can't extract
        """
        try:
            word = None
            if text and hasattr(match, "offset") and match.error_length:
                start = match.offset
                end = start + match.error_length
                word = text[start:end]
            elif hasattr(match, "context") and match.context:
                offset_in_context = getattr(match, "offset_in_context", match.offset)
                if match.error_length:
                    start = offset_in_context
                    end = start + match.error_length
                    word = match.context[start:end]
            elif hasattr(match, "matched_text") and match.matched_text:
                word = match.matched_text

            if not word:
                return None

            # Strip surrounding punctuation
            word = re.sub(
                r"^[^\w'áéíóúüñÁÉÍÓÚÜÑ]+|[^\w'áéíóúüñÁÉÍÓÚÜÑ]+$",
                "",
                word,
            )
            return word.strip() or None
        except (AttributeError, TypeError, IndexError):
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
