"""Tests for grammar rule identity, the measured ignore list, and raya gating.

Covers the three changes that let a LanguageTool rule be suppressed or tracked
by a stable key instead of by its localized Spanish message:

* ``Issue.rule_id`` / ``Issue.category`` survive onto the persisted issue;
* ``GrammarEvaluator.DEFAULT_IGNORE_RULES`` drops rules with no measured value;
* ``DIALOGUE_SENSITIVE_RULE_IDS`` drops rules only inside a spoken turn, so they
  keep checking narration.
"""

import pytest
from unittest.mock import Mock, patch

from src.models import Chunk, ChunkMetadata
from src.evaluators.grammar_eval import GrammarEvaluator, LANGUAGETOOL_AVAILABLE

skip_if_no_lt = pytest.mark.skipif(
    not LANGUAGETOOL_AVAILABLE, reason="LanguageTool not installed"
)


def make_metadata():
    return ChunkMetadata(
        char_start=0,
        char_end=100,
        overlap_start=0,
        overlap_end=0,
        paragraph_count=1,
        word_count=10,
    )


def make_mock_match(message, category, rule_id, offset=0, length=5, context=""):
    match = Mock()
    match.message = message
    match.category = category
    match.rule_id = rule_id
    match.offset = offset
    match.error_length = length
    match.replacements = []
    match.context = context
    return match


def make_chunk(translated_text, source_text="Test"):
    return Chunk(
        id="test",
        source_text=source_text,
        translated_text=translated_text,
        chapter_id="test",
        position=0,
        metadata=make_metadata(),
    )


@patch("language_tool_python.LanguageTool")
class TestRuleIdentityPersisted:
    """rule_id/category must reach the Issue -- everything else keys on them."""

    def test_rule_id_and_category_survive(self, mock_lt_class):
        pytest.importorskip("language_tool_python")
        mock_tool = Mock()
        mock_tool.check.return_value = [
            make_mock_match("Algo va mal", "GRAMMAR", "AGREEMENT_DET_GN")
        ]
        mock_lt_class.return_value = mock_tool

        result = GrammarEvaluator().evaluate(make_chunk("Texto de prueba."), {})

        assert len(result.issues) == 1
        assert result.issues[0].rule_id == "AGREEMENT_DET_GN"
        assert result.issues[0].category == "GRAMMAR"

    def test_missing_rule_id_becomes_none_not_empty_string(self, mock_lt_class):
        """A checker without a rule concept must not persist a falsy sentinel
        that a suppression set could accidentally match."""
        pytest.importorskip("language_tool_python")
        mock_tool = Mock()
        mock_tool.check.return_value = [make_mock_match("Algo", "MISC", "")]
        mock_lt_class.return_value = mock_tool

        result = GrammarEvaluator().evaluate(make_chunk("Texto de prueba."), {})

        assert result.issues[0].rule_id is None


@patch("language_tool_python.LanguageTool")
class TestDefaultIgnoreRules:
    """The measured ignore list is applied by default and is opt-out."""

    def test_listed_rule_is_dropped_unlisted_is_kept(self, mock_lt_class):
        pytest.importorskip("language_tool_python")
        assert "COMMA_ADVERB" in GrammarEvaluator.DEFAULT_IGNORE_RULES
        mock_tool = Mock()
        mock_tool.check.return_value = [
            make_mock_match("Coma tras el adverbio", "PUNCTUATION", "COMMA_ADVERB"),
            make_mock_match("Concordancia", "GRAMMAR", "AGREEMENT_DET_GN", offset=40),
        ]
        mock_lt_class.return_value = mock_tool

        result = GrammarEvaluator().evaluate(make_chunk("Texto de prueba."), {})

        rule_ids = {iss.rule_id for iss in result.issues}
        assert rule_ids == {"AGREEMENT_DET_GN"}

    def test_disabling_default_ignores_restores_the_raw_evaluator(self, mock_lt_class):
        """The replay script measures these rules, so it must be able to see
        them; without this the next measurement would report them as absent."""
        pytest.importorskip("language_tool_python")
        mock_tool = Mock()
        mock_tool.check.return_value = [
            make_mock_match("Coma tras el adverbio", "PUNCTUATION", "COMMA_ADVERB")
        ]
        mock_lt_class.return_value = mock_tool

        evaluator = GrammarEvaluator()
        chunk = make_chunk("Texto de prueba.")

        assert evaluator.evaluate(chunk, {}).issues == []
        raw = evaluator.evaluate(chunk, {"apply_default_ignores": False})
        assert [iss.rule_id for iss in raw.issues] == ["COMMA_ADVERB"]

    def test_caller_ignore_rules_extend_rather_than_replace(self, mock_lt_class):
        pytest.importorskip("language_tool_python")
        mock_tool = Mock()
        mock_tool.check.return_value = [
            make_mock_match("Coma tras el adverbio", "PUNCTUATION", "COMMA_ADVERB"),
            make_mock_match("Otra cosa", "GRAMMAR", "CUSTOM_RULE", offset=40),
        ]
        mock_lt_class.return_value = mock_tool

        result = GrammarEvaluator().evaluate(
            make_chunk("Texto de prueba."), {"ignore_rules": ["CUSTOM_RULE"]}
        )

        assert result.issues == []


class TestDialogueParagraphRanges:
    """Offsets, not rewriting: the guard has to place a match without moving it."""

    @patch("language_tool_python.LanguageTool")
    def test_only_spoken_turns_are_ranged(self, mock_lt_class):
        pytest.importorskip("language_tool_python")
        mock_lt_class.return_value = Mock()
        evaluator = GrammarEvaluator()

        text = "Narracion primera.\n—Hola —dijo—. Adios.\nMas narracion.\n»Sigo hablando."
        ranges = evaluator._dialogue_paragraph_ranges(text)

        # Two dialogue paragraphs: the raya turn and the guillemet continuation.
        assert len(ranges) == 2
        for start, end in ranges:
            assert text[start:end].lstrip()[0] in ("—", "»", "«")
        # Narration paragraphs fall outside every range.
        narration_offset = text.index("Mas narracion")
        assert not any(s <= narration_offset < e for s, e in ranges)


@patch("language_tool_python.LanguageTool")
class TestDialogueSensitiveGating:
    """A rule that only misfires inside dialogue must still work in narration."""

    def _run(self, mock_lt_class, offset, context=None):
        mock_tool = Mock()
        mock_tool.check.return_value = [
            make_mock_match(
                "Esta frase no empieza con mayuscula.",
                "CASING",
                "UPPERCASE_SENTENCE_START",
                offset=offset,
            )
        ]
        mock_lt_class.return_value = mock_tool
        text = "Narracion sin mayuscula aqui.\n—hola —dijo Ricardo—. Adios."
        return GrammarEvaluator().evaluate(make_chunk(text), context or {}), text

    def test_suppressed_inside_a_spoken_turn(self, mock_lt_class):
        pytest.importorskip("language_tool_python")
        text = "Narracion sin mayuscula aqui.\n—hola —dijo Ricardo—. Adios."
        result, _ = self._run(mock_lt_class, offset=text.index("hola"))
        assert result.issues == []

    def test_still_fires_in_narration(self, mock_lt_class):
        pytest.importorskip("language_tool_python")
        result, _ = self._run(mock_lt_class, offset=0)
        assert [iss.rule_id for iss in result.issues] == ["UPPERCASE_SENTENCE_START"]

    def test_disabling_default_ignores_bypasses_the_guard_too(self, mock_lt_class):
        pytest.importorskip("language_tool_python")
        text = "Narracion sin mayuscula aqui.\n—hola —dijo Ricardo—. Adios."
        result, _ = self._run(
            mock_lt_class,
            offset=text.index("hola"),
            context={"apply_default_ignores": False},
        )
        assert [iss.rule_id for iss in result.issues] == ["UPPERCASE_SENTENCE_START"]

    def test_sibling_capitalization_rule_is_untouched(self, mock_lt_class):
        """MAYUSCULAS_INICIO_FRASE measured 6 real / 0 false -- it must not be
        swept up with the two that fail on raya parsing."""
        pytest.importorskip("language_tool_python")
        assert (
            "MAYUSCULAS_INICIO_FRASE"
            not in GrammarEvaluator.DIALOGUE_SENSITIVE_RULE_IDS
        )
        assert "MAYUSCULAS_INICIO_FRASE" not in GrammarEvaluator.DEFAULT_IGNORE_RULES

        text = "Narracion.\n—hola —dijo Ricardo—. Adios."
        mock_tool = Mock()
        mock_tool.check.return_value = [
            make_mock_match(
                "Revise las mayusculas a principio de frase.",
                "CASING",
                "MAYUSCULAS_INICIO_FRASE",
                offset=text.index("hola"),
            )
        ]
        mock_lt_class.return_value = mock_tool

        result = GrammarEvaluator().evaluate(make_chunk(text), {})
        assert [iss.rule_id for iss in result.issues] == ["MAYUSCULAS_INICIO_FRASE"]
