"""Unit coverage for src/judges/fixes.py — the careful finding→edit classifier.

The house rule (friction-log Issue #5): only mechanically apply a judge finding
when it is a *uniquely-locatable text swap*. These tests pin each branch of
:func:`classify_fix` and the provenance-carrying :func:`to_correction_record`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.judges.fixes import (
    ManualFinding,
    ProposedFix,
    REASON_EXCERPT_AMBIGUOUS,
    REASON_EXCERPT_NOT_FOUND,
    REASON_NO_EXCERPT,
    REASON_NO_SUGGESTION,
    REASON_SUGGESTION_EQUALS_EXCERPT,
    REASON_SUGGESTION_NOT_LITERAL,
    classify_fix,
    looks_like_instruction,
    to_correction_record,
)


def _issue(location=None, suggestion=None, severity="error", rule="raya-spacing", msg="space after raya"):
    return {
        "severity": severity,
        "message": f"[{rule}] {msg}",
        "location": location,
        "suggestion": suggestion,
    }


class TestClassifyApplicable:
    def test_unique_match_becomes_proposed_fix(self):
        text = "Dijo algo. — Hola, respondió."
        result = classify_fix(_issue("— Hola", "—Hola"), text)
        assert isinstance(result, ProposedFix)
        assert result.excerpt == "— Hola"
        assert result.suggestion == "—Hola"
        assert result.rule == "raya-spacing"
        assert result.severity == "error"
        # Offsets slice exactly back to the excerpt.
        assert text[result.char_start:result.char_end] == "— Hola"

    def test_issue_can_be_an_object_not_just_dict(self):
        class _Obj:
            severity = "warning"
            message = "[other] x"
            location = "foo"
            suggestion = "bar"

        result = classify_fix(_Obj(), "a foo b")
        assert isinstance(result, ProposedFix)
        assert result.suggestion == "bar"


class TestClassifyManual:
    def test_instruction_suggestion_is_manual(self):
        result = classify_fix(_issue("dijo él", "split into two paragraphs"), "y dijo él aquí")
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_SUGGESTION_NOT_LITERAL

    def test_excerpt_not_found_is_manual(self):
        result = classify_fix(_issue("no existe", "—Hola"), "texto distinto")
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_EXCERPT_NOT_FOUND

    def test_ambiguous_twin_is_manual(self):
        # "bien bien" appears twice — no offset to disambiguate, so withhold.
        result = classify_fix(_issue("bien bien", "bien"), "bien bien y otra vez bien bien")
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_EXCERPT_AMBIGUOUS

    def test_suggestion_equals_excerpt_is_manual(self):
        result = classify_fix(_issue("—Hola", "—Hola"), "x —Hola y")
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_SUGGESTION_EQUALS_EXCERPT

    def test_no_suggestion_is_manual(self):
        result = classify_fix(_issue("— Hola", None), "x — Hola y")
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_NO_SUGGESTION

    def test_no_excerpt_is_manual(self):
        result = classify_fix(_issue(None, "—Hola"), "x — Hola y")
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_NO_EXCERPT

    def test_manual_preserves_excerpt_and_suggestion(self):
        result = classify_fix(_issue("dijo él", "move to a new line"), "dijo él")
        assert isinstance(result, ManualFinding)
        assert result.excerpt == "dijo él"
        assert result.suggestion == "move to a new line"
        assert result.rule == "raya-spacing"


class TestInstructionHeuristic:
    def test_flags_english_imperatives(self):
        assert looks_like_instruction("split into two paragraphs")
        assert looks_like_instruction("Move this to its own line")
        assert looks_like_instruction("fold into inciso #42")
        assert looks_like_instruction("use guillemets here")

    def test_leaves_real_spanish_replacements_alone(self):
        assert not looks_like_instruction("—Hola")
        assert not looks_like_instruction("«Entonces se dijo»")
        assert not looks_like_instruction("—dijo él—")


class TestToCorrectionRecord:
    def test_carries_offsets_and_provenance(self):
        text = "a — Hola b"
        fix = classify_fix(_issue("— Hola", "—Hola"), text)
        assert isinstance(fix, ProposedFix)
        record = to_correction_record(
            fix,
            chunk_id="chapter_01_chunk_000",
            chapter_id="chapter_01",
            project_id="demo",
            judge_name="dialogue",
        )
        assert record["original_es"] == "— Hola"
        assert record["corrected_es"] == "—Hola"
        assert record["chunk_offset_start"] == fix.char_start
        assert record["chunk_offset_end"] == fix.char_end
        assert record["es_idx"] is None
        assert record["source"] == "judge:dialogue"
        assert record["rule"] == "raya-spacing"
        assert record["severity"] == "error"
        assert record["chunk_id"] == "chapter_01_chunk_000"
        assert record["chapter_id"] == "chapter_01"
        assert record["project_id"] == "demo"
        assert "timestamp" in record
