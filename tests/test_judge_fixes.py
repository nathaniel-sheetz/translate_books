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
    REASON_SUGGESTION_RESTATES_CONTEXT,
    boundary_overlap,
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


class TestRestatesContext:
    """Regression net for the 2026-07-27 duplication bug.

    A judge that rewrites a whole dialogue turn but keys the finding to a short
    excerpt produces a ``suggestion`` containing prose that already sits outside
    that excerpt. The excerpt still locates uniquely, so every earlier check
    passes — and splicing the suggestion in leaves the surrounding words printed
    twice. Each case below is the *actual* old/new pair from a real corrupting
    apply, against the real chunk text it was applied to.
    """

    def test_head_restatement_chapter_18(self):
        """`new` prepends the 10 words of dialogue that already precede the excerpt."""
        text = (
            "Aun así no hubo respuesta.\n\n"
            "—¿Te estás poniendo terco? ¿Ni siquiera me vas a contestar? —El jefe de la "
            "pandilla, evidentemente, se estaba enojando. De pronto gritó—:\n\n"
            "»¡Firma este papel, Hardy, o te vas a morir de hambre... tan seguro como que "
            "me llamo Snackley!"
        )
        old = (
            "—El jefe de la pandilla, evidentemente, se estaba enojando. De pronto gritó—:"
            "\n\n»¡Firma este papel, Hardy, o te vas a morir de hambre... tan seguro como "
            "que me llamo Snackley!"
        )
        new = (
            "—¿Te estás poniendo terco? ¿Ni siquiera me vas a contestar? —El jefe de la "
            "pandilla, evidentemente, se estaba enojando. De pronto gritó—. ¡Firma este "
            "papel, Hardy, o te vas a morir de hambre... tan seguro como que me llamo "
            "Snackley!"
        )
        result = classify_fix(_issue(old, new, rule="same-speaker-continuation"), text)
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_SUGGESTION_RESTATES_CONTEXT

    def test_tail_restatement_survives_repunctuation_chapter_03(self):
        """`new` appends 15 following words *and* repunctuates them.

        The restated span is not byte-identical to the original — the suggestion
        switches the turn to guillemets and moves a comma — so a duplicate-n-gram
        scan over the spliced result misses it. The word-level measure does not.
        """
        text = (
            "…habían aprovechado la oportunidad para gastarles una broma.\n\n"
            "—En ese caso —murmuró para sí—, la historia va a correr por toda la "
            "preparatoria de Bayport para el lunes, y nos van a molestar hasta el "
            "cansancio por haber salido corriendo. Deberíamos habernos quedado.\n\n"
            "Algo le decía, sin embargo, que no se trataba de una broma escolar cualquiera."
        )
        old = (
            "—En ese caso —murmuró para sí—, la historia va a correr por toda la "
            "preparatoria de Bayport para el lunes"
        )
        new = (
            "«En ese caso —pensó—, la historia va a correr por toda la preparatoria de "
            "Bayport para el lunes, y nos van a molestar hasta el cansancio por haber "
            "salido corriendo. Deberíamos habernos quedado»."
        )
        result = classify_fix(_issue(old, new, rule="guillemets-for-thoughts"), text)
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_SUGGESTION_RESTATES_CONTEXT

    def test_small_overlap_chapter_23(self):
        """1 restated word at the head, 2 at the tail — 10 and 14 characters.

        Proves a character-length threshold (`len(new) > len(old) * 1.2`, or
        "≥15 characters of adjacent text") is not enough, and that
        ``MIN_RESTATED_WORDS = 1`` is what catches the head-only single word.
        """
        text = (
            "Arriba se seguía oyendo el alboroto mientras la policía continuaba la batalla "
            "contra los contrabandistas.\n\n"
            "—¡Arriba! —espetó el detective con sequedad. Sin apartar la vista de Snackley, "
            "le dijo al hombre del catre:\n\n—Volveremos por usted más tarde... Sr. Jones."
        )
        old = (
            "—espetó el detective con sequedad. Sin apartar la vista de Snackley, le dijo "
            "al hombre del catre:\n\n—Volveremos por usted más tarde"
        )
        new = (
            "—¡Arriba! —espetó el detective con sequedad.\n\nSin apartar la vista de "
            "Snackley, le dijo al hombre del catre:\n\n—Volveremos por usted más tarde... "
            "Sr. Jones."
        )
        result = classify_fix(_issue(old, new, rule="narration-separation"), text)
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_SUGGESTION_RESTATES_CONTEXT

    def test_head_restatement_wonder_book_of_horses(self):
        """The 2026-07-01 occurrence — a human deleted this duplicate five hours later."""
        text = (
            "—Pero —dijo el muchacho— mi padre Helios, que conduce el ardiente carro, y que…"
            "\n\n—No me hables —lo interrumpió el insolente— , no me hables de tu padre el "
            "cochero. Pues, te morirías de miedo de llevar el carrito de cabras de tu "
            "hermana por el jardín."
        )
        old = "—lo interrumpió el insolente— , no me hables de tu padre el cochero."
        new = "—No me hables —lo interrumpió el insolente—, no me hables de tu padre el cochero."
        result = classify_fix(_issue(old, new, rule="inciso-punctuation"), text)
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_SUGGESTION_RESTATES_CONTEXT

    def test_preserving_an_existing_boundary_overlap_stays_applicable(self):
        """Only a *newly introduced* repetition is rejected.

        Here the excerpt already opens with the two words that precede it (an
        overlap of 2, at/over the threshold), and the suggestion keeps them —
        so it repeats nothing the text did not already say. Without the
        "greater than the excerpt's own overlap" guard this would be a false
        positive even at ``MIN_RESTATED_WORDS = 1``.
        """
        text = "Estaba muy bien. Muy bien —dijo ella."
        old, new = "Muy bien —dijo ella.", "Muy bien —dijo ella—."
        assert boundary_overlap(text, text.find(old), text.find(old) + len(old), old)[0] == 2
        result = classify_fix(_issue(old, new), text)
        assert isinstance(result, ProposedFix)

    def test_single_new_boundary_word_is_withheld(self):
        """A newly introduced one-word head restatement is enough to withhold."""
        text = "Dijo entonces. Entonces —respondió ella."
        old, new = "—respondió ella.", "Entonces —respondió ella."
        start = text.find(old)
        assert boundary_overlap(text, start, start + len(old), new)[0] == 1
        result = classify_fix(_issue(old, new), text)
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_SUGGESTION_RESTATES_CONTEXT

    def test_plain_inciso_repunctuation_stays_applicable(self):
        """A real archived fix (record #0): rewrites only the excerpt's own span."""
        text = (
            "—Ven conmigo —y dio unos golpecitos en el banco a su lado—. Hay sitio de sobra."
        )
        result = classify_fix(
            _issue("conmigo —y dio unos golpecitos", "conmigo. —Dio unos golpecitos",
                   rule="inciso-punctuation"),
            text,
        )
        assert isinstance(result, ProposedFix)


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
