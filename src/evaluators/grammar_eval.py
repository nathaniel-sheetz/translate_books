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
from ..utils.text_utils import (
    blank_caption_markers,
    caption_marker_ranges,
    image_placeholder_ranges,
    strip_image_placeholders,
)
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

    # Rules that have never once produced a real defect across the local books.
    #
    # Derived, not guessed: scripts/replay_grammar_marks.py re-runs this
    # evaluator over every chunk carrying a human mark in
    # ``evaluations/_feedback.jsonl``, learns a message-prefix -> rule_id
    # lexicon, then scores each rule against those labels ("resolved" = the
    # human fixed a real defect, "false_positive" = noise). A rule earns a place
    # here only with zero resolved findings and at least two false positives, so
    # dropping it costs no recall on the measured corpus. Counts are from the
    # 2026-08-25 run (27 real / 92 false overall, 23% precision):
    #
    #   MAS                          0 real / 10 false
    #   COMMA_ADVERB                 0 real /  8 false
    #   COMMA_PERO                   0 real /  4 false
    #   AUN2                         0 real /  2 false
    #   ES_INITIAL_QUESTION_MARK     0 real /  2 false
    #   HACIA_TILDE                  0 real /  2 false
    #   INTERROGATIVOS_CON_TILDE_OS  0 real /  2 false
    #   QUE_TILDE2                   0 real /  2 false
    #
    # A comma-placement family and a set of archaic-tilde rules; both fight the
    # house prose style rather than catching defects. Suppressing these takes
    # precision from 23% to 31% with 0 real defects lost. Re-run the script after
    # more marking to revisit -- a rule with one real catch belongs back in.
    #
    # These counts supersede a 2026-08-24 run that measured only 72% of the
    # corpus: it discovered projects with iterdir() and so never saw the books
    # under projects/.published/ and projects/.macdonald/, double-counted a
    # .bak snapshot of the-little-duke, and attributed 74 marks by a stale
    # issue_index. COMMA_SINO left the list because its message prefix is shared
    # with COMMA_SINO2 and its marks can no longer be attributed to either;
    # EL_TILDE, QUE_TILDE1 and SERIA left it because they now measure one false
    # positive each, under the threshold. An under-covered measurement is
    # recoverable; a wrong suppression is not.
    #
    # Deliberately NOT here: UPPERCASE_SENTENCE_START (0 real / 7 false) and
    # CAPITALIZATION_AFTER_QUESTION_MARK (1 real / 4 false). The first would
    # qualify, but both fail for a *mechanical* reason (LanguageTool cannot parse
    # raya dialogue) rather than a stylistic one, so they are gated by
    # DIALOGUE_SENSITIVE_RULE_IDS below and keep checking narration, where
    # capitalization errors are real -- the sibling rule MAYUSCULAS_INICIO_FRASE
    # scores 6 real / 0 false, and CAPITALIZATION_AFTER_QUESTION_MARK's one real
    # catch is exactly what a flat suppression would have thrown away.
    DEFAULT_IGNORE_RULES = frozenset(
        {
            "AUN2",
            "COMMA_ADVERB",
            "COMMA_PERO",
            "ES_INITIAL_QUESTION_MARK",
            "HACIA_TILDE",
            "INTERROGATIVOS_CON_TILDE_OS",
            "MAS",
            "QUE_TILDE2",
        }
    )

    # Rules suppressed only INSIDE a dialogue paragraph. LanguageTool reads
    # "--!Ah! ?que? --dijo Ricardo--." as one malformed sentence: the raya is not
    # a sentence opener it knows, the inciso is not a parenthetical it knows, and
    # so it demands a capital that Spanish dialogue convention forbids. Narration
    # keeps the rule.
    #
    # Measured on the 2026-08-25 corpus, placing each marked finding's offset
    # against _dialogue_paragraph_ranges:
    #
    #   UPPERCASE_SENTENCE_START            7 false: 5 inside, 2 outside
    #   CAPITALIZATION_AFTER_QUESTION_MARK  4 false: 0 inside, 4 outside
    #                                       1 real:  0 inside, 1 outside
    #
    # So the gate is what the first rule needs and is *precautionary* for the
    # second: the parsing failure is identical for both, but only five marks
    # exist for the second rule and none of them landed in dialogue. It stays
    # gated because the gate cannot cost it anything -- its one real catch is in
    # narration -- and because a raya false positive is the failure the rule is
    # known to have. Revisit if it accumulates inside-dialogue marks.
    DIALOGUE_SENSITIVE_RULE_IDS = frozenset(
        {
            "UPPERCASE_SENTENCE_START",
            "CAPITALIZATION_AFTER_QUESTION_MARK",
        }
    )

    # A paragraph opening with one of these is a spoken turn under the house
    # dialogue rules (prompts/dialogue.txt): raya for a new turn, guillemet for a
    # same-speaker continuation.
    _DIALOGUE_OPENERS = ("—", "»", "«")

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
                - ignore_rules: list[str] (specific rule IDs to skip; these
                  EXTEND DEFAULT_IGNORE_RULES rather than replacing it)
                - apply_default_ignores: bool (default True; False turns off
                  both built-in gates, DEFAULT_IGNORE_RULES and
                  DIALOGUE_SENSITIVE_RULE_IDS, so the raw evaluator can be
                  scored -- see scripts/replay_grammar_marks.py)
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
        placeholder_ranges = (
            image_placeholder_ranges(chunk.translated_text)
            + caption_marker_ranges(chunk.translated_text)
        )
        text_to_check = strip_image_placeholders(chunk.translated_text)
        # Same treatment for the [CAPTION] marker, or "CAPTION" is reported as a
        # misspelling in every captioned paragraph. The caption's prose stays.
        text_to_check = blank_caption_markers(text_to_check)

        # Run LanguageTool check
        dialogue_ranges = self._dialogue_paragraph_ranges(text_to_check)

        matches = self._check_grammar(text_to_check)

        # Process matches (deduplicated by rule + flagged word)
        grouped_matches: dict[tuple, list] = defaultdict(list)

        for match in matches:
            # Skip matches whose offset falls inside a replaced placeholder
            # (the whitespace run we substituted triggers a spurious spaces warning)
            if any(start <= match.offset < end for start, end in placeholder_ranges):
                continue

            # Check if this match should be ignored
            if self._should_ignore_match(
                match, context, text_to_check, dialogue_ranges=dialogue_ranges
            ):
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

    def _dialogue_paragraph_ranges(self, text: str) -> list[tuple[int, int]]:
        """Half-open [start, end) spans of paragraphs that are spoken turns.

        Offsets index the same string LanguageTool checked, so a match can be
        placed by comparing ``match.offset`` against these spans -- no rewriting
        of the text, which would shift every other offset the evaluator relies on.
        """
        ranges: list[tuple[int, int]] = []
        pos = 0
        for paragraph in text.split("\n"):
            end = pos + len(paragraph)
            if paragraph.lstrip()[:1] in self._DIALOGUE_OPENERS:
                ranges.append((pos, end))
            pos = end + 1  # the newline itself
        return ranges

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
            rule_id=getattr(match, "rule_id", None) or None,
            category=getattr(match, "category", None) or None,
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
        self,
        match,
        context: dict,
        text: str = "",
        dialogue_ranges: Optional[list[tuple[int, int]]] = None,
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

        # Check ignore_rules. The caller's list extends the measured default
        # rather than replacing it; pass apply_default_ignores=False to opt out
        # of both built-in gates (the replay script does, to score the raw
        # evaluator against the human marks that produced those gates).
        ignore_rules = context.get('ignore_rules', [])
        rule_id = getattr(match, 'rule_id', None)
        if rule_id in ignore_rules:
            return True
        apply_defaults = context.get('apply_default_ignores', True)
        if apply_defaults and rule_id in self.DEFAULT_IGNORE_RULES:
            return True

        # Rules that only misfire inside spoken dialogue keep working elsewhere.
        if (
            apply_defaults
            and rule_id in self.DIALOGUE_SENSITIVE_RULE_IDS
            and dialogue_ranges
        ):
            offset = getattr(match, 'offset', None)
            if offset is not None and any(
                start <= offset < end for start, end in dialogue_ranges
            ):
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
