"""
Blacklist Evaluator

Validates that translations do not contain forbidden words or phrases from a
predefined blacklist. Supports flexible matching options including variations
for handling conjugations and plurals, configurable severity levels, and
suggested alternatives.

Use cases:
- Prevent offensive or inappropriate language
- Enforce translator style preferences (avoid colloquialisms)
- Ensure formality level (block informal slang in formal translations)
- Block terms that should use glossary translations instead
"""

from typing import Any, Optional
import json
import re
from pathlib import Path
from ..models import Chunk, EvalResult, Issue, IssueLevel, BlacklistEntry, Blacklist
from .base import BaseEvaluator


class BlacklistEvaluator(BaseEvaluator):
    """
    Evaluates translation for forbidden words and phrases.

    Checks:
    - Scans translated text for blacklisted terms and their variations
    - Supports case-sensitive and case-insensitive matching
    - Supports whole-word matching (avoid partial matches)
    - Tracks all occurrences with character positions
    - Configurable severity per blacklist entry (error, warning, info)

    Example:
        evaluator = BlacklistEvaluator()
        chunk = Chunk(translated_text="Él cogió el libro...")
        blacklist = Blacklist(entries=[
            BlacklistEntry(term="coger", variations=["coger", "cogió"],
                          reason="Offensive in Latin America", severity="warning")
        ])
        result = evaluator.evaluate(chunk, {"blacklist": blacklist})
    """

    name = "blacklist"
    version = "1.0.0"
    description = "Validates translation does not contain forbidden words"

    def evaluate(self, chunk: Chunk, context: dict[str, Any]) -> EvalResult:
        """
        Evaluate blacklist compliance for a translated chunk.

        Args:
            chunk: Chunk containing translated text to check
            context: Dictionary that should contain "blacklist" key with Blacklist object
                    or "blacklist_path" key with Path to blacklist.json file

        Returns:
            EvalResult with issues for blacklist violations
        """
        issues: list[Issue] = []

        # Get blacklist from context (either object or path)
        blacklist: Optional[Blacklist] = context.get("blacklist")
        blacklist_path: Optional[Path] = context.get("blacklist_path")

        if not blacklist and blacklist_path:
            # Load blacklist from file if path provided
            blacklist = self._load_blacklist(blacklist_path)

        if not blacklist or not blacklist.entries:
            # No blacklist provided - nothing to check
            return self.create_result(
                chunk=chunk,
                issues=[],
                score=1.0,
                metadata={"blacklist_entries_checked": 0}
            )

        # Check if translation exists
        if not chunk.translated_text or not chunk.translated_text.strip():
            # No translation to check - pass
            return self.create_result(
                chunk=chunk,
                issues=[],
                score=1.0,
                metadata={"blacklist_entries_checked": len(blacklist.entries)}
            )

        # Check each blacklist entry
        for entry in blacklist.entries:
            entry_issues = self._check_entry(chunk.translated_text, entry)
            issues.extend(entry_issues)

        # Calculate score based on severity of issues found
        score = self._calculate_score(issues, len(blacklist.entries))

        return self.create_result(
            chunk=chunk,
            issues=issues,
            score=score,
            metadata={
                "blacklist_entries_checked": len(blacklist.entries),
                "violations_found": len(issues)
            }
        )

    def _load_blacklist(self, path: Path) -> Blacklist:
        """Load blacklist from JSON file."""
        if not path.exists():
            raise FileNotFoundError(f"Blacklist file not found: {path}")

        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        return Blacklist(**data)

    def _check_entry(self, text: str, entry: BlacklistEntry) -> list[Issue]:
        """
        Check for all occurrences of a blacklist entry in text.

        Args:
            text: Translated text to check
            entry: Blacklist entry with term, variations, and settings

        Returns:
            List of issues for all matches found
        """
        issues: list[Issue] = []

        # Build list of all terms to search for (base term + variations)
        terms_to_check = [entry.term]
        if entry.variations:
            # Add all variations (but avoid duplicates)
            terms_to_check.extend([v for v in entry.variations if v not in terms_to_check])

        # Find all matches for each term
        all_matches: list[tuple[str, int]] = []  # (matched_word, position)

        for term in terms_to_check:
            matches = self._find_blacklist_matches(text, term, entry.case_sensitive, entry.whole_word)
            all_matches.extend([(term, pos) for pos in matches])

        # Create an issue for each match
        for matched_word, position in all_matches:
            # Build issue message
            message = f"Blacklisted term '{entry.term}' found: '{matched_word}' at position {position}"
            if entry.reason:
                message += f". {entry.reason}"

            # Build suggestion
            suggestion = None
            if entry.alternatives:
                suggestion = f"Consider using: {', '.join(entry.alternatives)}"

            # Map severity string to IssueLevel
            severity = self._map_severity(entry.severity)

            issue = self.create_issue(
                severity=severity,
                message=message,
                location=f"char {position}",
                suggestion=suggestion
            )
            issues.append(issue)

        return issues

    def _find_blacklist_matches(self, text: str, term: str, case_sensitive: bool, whole_word: bool) -> list[int]:
        """
        Find all positions where a term appears in text.

        Args:
            text: Text to search
            term: Term to find
            case_sensitive: Whether to match case
            whole_word: Whether to require word boundaries

        Returns:
            List of character positions where term was found
        """
        positions: list[int] = []

        # Build regex pattern
        pattern = re.escape(term)

        if whole_word:
            # Add word boundaries
            pattern = r'\b' + pattern + r'\b'

        # Compile with appropriate flags
        flags = 0 if case_sensitive else re.IGNORECASE
        regex = re.compile(pattern, flags)

        # Find all matches
        for match in regex.finditer(text):
            positions.append(match.start())

        return positions

    def _map_severity(self, severity_str: str) -> IssueLevel:
        """Map severity string to IssueLevel enum."""
        severity_map = {
            'error': IssueLevel.ERROR,
            'warning': IssueLevel.WARNING,
            'info': IssueLevel.INFO
        }
        return severity_map.get(severity_str.lower(), IssueLevel.ERROR)

    def _calculate_score(self, issues: list[Issue], total_entries: int) -> float:
        """
        Calculate quality score based on issues found.

        Args:
            issues: List of blacklist violations
            total_entries: Total number of blacklist entries checked

        Returns:
            Score from 0.0 (many violations) to 1.0 (no violations)
        """
        if not issues:
            return 1.0

        # Penalize based on severity
        penalty = 0.0
        for issue in issues:
            if issue.severity == IssueLevel.ERROR:
                penalty += 0.3
            elif issue.severity == IssueLevel.WARNING:
                penalty += 0.1
            else:  # INFO
                penalty += 0.05

        # Calculate score (minimum 0.0)
        score = max(0.0, 1.0 - penalty)
        return score
