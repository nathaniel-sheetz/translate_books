"""Unit coverage for src/judges/fixes.py — the careful finding→edit classifier.

The house rule (friction-log Issue #5): only mechanically apply a judge finding
when it is a *uniquely-locatable text swap*. These tests pin each branch of
:func:`classify_fix` and the provenance-carrying :func:`to_correction_record`.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.judges.fixes import (
    ManualFinding,
    ProposedFix,
    REASON_EXCERPT_AMBIGUOUS,
    REASON_EXCERPT_NOT_FOUND,
    REASON_MIXED_REGISTER_REMAINS,
    REASON_NO_EXCERPT,
    REASON_NO_SUGGESTION,
    REASON_SUGGESTION_ADDS_ELLIPSIS,
    REASON_SUGGESTION_EQUALS_EXCERPT,
    REASON_SUGGESTION_NOT_LITERAL,
    REASON_SUGGESTION_PLACEHOLDER,
    REASON_SUGGESTION_RESTATES_CONTEXT,
    REASON_SUGGESTION_TOO_LONG,
    REASON_SUGGESTION_TOO_SHORT,
    REASON_SUGGESTION_UNBALANCED_RAYA,
    boundary_overlap,
    classify_fix,
    looks_like_instruction,
    to_correction_record,
    unopened_rayas,
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


class TestPlaceholderSuggestion:
    """Regression net for the 2026-07-29 pollyanna friction log, item 0.

    Three address findings reached ``applicable[]`` with ``"suggestion": "N/A"``.
    The judge writes that to mean "this is a note, not a fix" — it had inspected
    each passage and decided the form was actually correct — but the classifier
    treated any non-empty, non-equal suggestion as literal replacement text, so a
    swap would have spliced the string ``N/A`` over a line of dialogue and deleted
    it. Short enough to survive a skim of the ``old → new`` preview, which is what
    makes it worse than the duplication bug it followed.
    """

    def test_bare_na_is_withheld(self):
        text = "—Sí; soy su sobrina. Ella me ha tomado para criarme..."
        result = classify_fix(
            _issue("—Sí; soy su sobrina. Ella me ha tomado para criarme...", "N/A",
                   rule="wrong-form-tu-expected"),
            text,
        )
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_SUGGESTION_PLACEHOLDER
        # The suggestion is preserved so the operator sees what the judge said.
        assert result.suggestion == "N/A"

    @pytest.mark.parametrize("placeholder", ["N/A", "n/a", "na", "None", "null", "-", "--", "…",
                                            "...", " N/A "])
    def test_every_placeholder_spelling(self, placeholder):
        result = classify_fix(_issue("me parece que debes de ser una personita", placeholder), "x")
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_SUGGESTION_PLACEHOLDER

    def test_placeholder_beats_excerpt_not_found(self):
        """"There is no fix here" is the more useful diagnosis of the two.

        The excerpt of the third pollyanna case does not occur in its chunk
        either, but sending the operator to the web editor to hand-apply a fix the
        judge never wrote would waste the trip.
        """
        result = classify_fix(_issue("texto que no está en el capítulo", "N/A"), "otra cosa")
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_SUGGESTION_PLACEHOLDER

    def test_a_real_short_suggestion_is_not_a_placeholder(self):
        result = classify_fix(_issue("— Hola", "—Hola"), "x — Hola y")
        assert isinstance(result, ProposedFix)


class TestSpliceGuards:
    """The length/ellipsis shape checks — 2026-07-29 item 2, and 07-27's open fix.

    Every pair below is real. The thresholds are calibrated against the 239
    archived judge fixes in ``projects/*/corrections_applied.jsonl`` (see
    ``tests/test_applied_corrections_audit.py``): legitimate fixes grow by at most
    13 characters, the known corruptions by 25 to 89.
    """

    def test_ellipsis_joined_passages_pollyanna_chapter_03(self):
        """The finding that got past ``suggestion_restates_context`` (item 2).

        ``new`` is three passages ellipsis-joined, spanning text well outside the
        excerpt. The boundary measure cannot see it: the reused prose
        ("Deseo que vaya a recibirla a la estación") sits a sentence downstream, so
        it never touches the span, and the ``...`` fragments break any contiguous
        comparison.
        """
        text = (
            "He mandado pedir mosquiteros, pero hasta que lleguen espero que usted se encargue "
            "de que las ventanas permanezcan cerradas. Mi sobrina llegará mañana a las cuatro. "
            "Deseo que usted vaya a recibirla a la estación."
        )
        old = "espero que usted se encargue de que las ventanas permanezcan cerradas"
        new = (
            "espero que te encargues de que las ventanas permanezcan cerradas... Deseo que "
            "vayas a recibirla a la estación... pero creo que es suficiente para tu propósito."
        )
        result = classify_fix(_issue(old, new, rule="wrong-form-tu-expected"), text)
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_SUGGESTION_ADDS_ELLIPSIS

    def test_stray_trailing_ellipsis_gaudenzia(self):
        """A near-zero growth whose only defect is a truncation marker.

        Reduced from the real gaudenzia pair, which grew 213 → 214 characters: no
        ratio or delta can catch the stray ` …` that suggestion would print into
        the book, so the ellipsis check is not a length check in disguise.
        """
        text = (
            "—¡Para los dos! —el hombre se encogió de hombros—. ¿Quién puede entenderlo?\n\n"
            "Movió la mano en un ritmo entrecortado.\n\n—¡Es guerra!"
        )
        old = "Movió la mano en un ritmo entrecortado."
        new = "—Movió la mano en un ritmo entrecortado— …"
        result = classify_fix(_issue(old, new, rule="narration-separation"), text)
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_SUGGESTION_ADDS_ELLIPSIS

    def test_an_excerpt_that_already_elides_stays_applicable(self):
        """Only a *newly introduced* ellipsis counts — real prose is full of them."""
        text = "—Volveremos por usted más tarde... Sr. Jones."
        result = classify_fix(
            _issue("Volveremos por usted más tarde...", "Volveremos por ti más tarde...",
                   rule="wrong-form-tu-expected"),
            text,
        )
        assert isinstance(result, ProposedFix)

    def test_suggestion_quoting_the_following_passage_fabre2(self):
        """The whole paragraph after the excerpt, restated (really 67 → 513 chars).

        ``restated_context`` misses this one by construction — it aligns a
        *suffix* of the replacement against a *prefix* of the following text, and
        here the duplicated block is wholly contained in the replacement, ending
        mid-passage. The length guard is the net that catches that shape.
        """
        text = (
            "—Las palas, las tenazas, las parrillas, las estufas, son de hierro. Estos "
            "diversos objetos, siempre en contacto con el fuego, no se funden, sin embargo; "
            "ni siquiera se ablandan."
        )
        old = "—Las palas, las tenazas, las parrillas, las estufas, son de hierro."
        new = (
            "»Las palas, las tenazas, las parrillas, las estufas, son de hierro. Estos "
            "diversos objetos, siempre en contacto con el fuego, no se funden, sin embargo; "
            "ni siquiera se ablandan. Para ablandar el hierro, el herrero necesita todo el "
            "calor de su fragua."
        )
        result = classify_fix(_issue(old, new, rule="same-speaker-continuation"), text)
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_SUGGESTION_TOO_LONG

    def test_suggestion_dropping_a_narration_paragraph_stormy_misty(self):
        """45 → 13 characters: the excerpt spans narration + speech, `new` is the speech.

        Applying it deletes ``Paul le dio un codazo al abuelo.`` — the deletion
        class the friction log's second item-0 fix asked for, found in a second
        book while calibrating.
        """
        text = (
            "¿y yo tuviera que jalarte como costal de papas?\n\n"
            "Paul le dio un codazo al abuelo.\n\n—Díselo ya.\n\n—Así que ya ves, Idy."
        )
        old = "Paul le dio un codazo al abuelo.\n\n—Díselo ya."
        result = classify_fix(_issue(old, "—Dígaselo ya.", rule="wrong-form-usted-expected"), text)
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_SUGGESTION_TOO_SHORT

    def test_english_conditional_parenthetical_is_not_literal(self):
        """The judge hedging in the field that gets spliced into the book.

        Two `ambiguous`-rule findings carried one of these. It is caught as
        ``suggestion_not_literal`` rather than by a length threshold because that
        is what it is — the instruction detector just never opened with a verb.
        """
        text = "—Está equivocado, Klein —dijo—. Yo sé cuál gorra dicen."
        new = "Estás equivocado, Klein (if the two are meant to be familiar accomplices)"
        result = classify_fix(_issue("Está equivocado, Klein", new, rule="ambiguous"), text)
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_SUGGESTION_NOT_LITERAL

    @pytest.mark.parametrize(
        "old,new",
        [
            ("Te equivocas.", "Se equivoca usted."),
            ("Nadie te acusará de glotón", "Nadie lo acusará a usted de glotón"),
            ("A lo mejor lo conoces.", "A lo mejor lo conoce usted."),
        ],
    )
    def test_short_address_fixes_stay_applicable(self, old, new):
        """The three archived fixes a bare ``len(new) > len(old) * 1.2`` would break.

        Adding *usted* to a short line swings the ratio to 1.23-1.38 while adding
        5 to 8 characters, which is why the growth guard requires an absolute
        delta as well. These landed in books and are correct.
        """
        text = f"—{old} El resto de la línea sigue igual."
        result = classify_fix(_issue(old, new, rule="wrong-form-usted-expected"), text)
        assert isinstance(result, ProposedFix)


class TestRayaBalance:
    """2026-07-29 item 6: a malformed suggestion nothing was checking.

    ``chapter_09_chunk_000#0`` gave a second inciso a closing raya with no opening
    one — mechanically checkable, and the classifier was not checking it.
    """

    def test_unopened_rayas_reads_the_turn_opener_as_speech(self):
        # Turn-opening raya, then a balanced inciso: nothing unopened.
        assert unopened_rayas("—¡Vaya! —fue todo lo que dijo—. Pero, oiga.") == 0
        # A » continuation marker in front of the turn raya is still a turn opener.
        assert unopened_rayas("»—Bueno —dijo ella—. Ya veremos.") == 0
        # A closing raya with no inciso open.
        assert unopened_rayas("—¡Vaya! —fue todo lo que dijo—. Luego continuó—.") == 1
        # Unclassifiable rayas (spaced both sides, glued both sides) are left alone.
        assert unopened_rayas("Ella — dijo — algo raro.") == 0

    def test_newly_unbalanced_suggestion_is_withheld_pollyanna_chapter_09(self):
        text = (
            "Pollyanna, vieron algo que impidió que esas palabras se pronunciaran.\n\n"
            "—¡Vaya! —fue todo lo que dijo. Luego, mostrando su antiguo interés, continuó—: "
            "Pero, oiga, es raro que le hable a usted, de veras, señorita Pollyanna."
        )
        old = "—fue todo lo que dijo. Luego, mostrando su antiguo interés, continuó—: Pero, oiga,"
        new = "—fue todo lo que dijo—. Luego, mostrando su antiguo interés, continuó—. Pero, oiga,"
        result = classify_fix(_issue(old, new, rule="inciso-punctuation"), text)
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_SUGGESTION_UNBALANCED_RAYA

    def test_a_preexisting_imbalance_does_not_withhold_an_unrelated_fix(self):
        """Baseline discipline: a malformed paragraph is often the finding itself.

        Withholding on the *state* rather than the *change* would refuse to fix
        exactly the paragraphs the dialogue judge exists to report.
        """
        text = "—Bueno —dijo ella—. Ya lo veremos—."
        assert unopened_rayas(text) == 1
        result = classify_fix(_issue("Bueno", "Bien", rule="other"), text)
        assert isinstance(result, ProposedFix)


class TestInconsistentAddress:
    """2026-07-29 item 6: normalization in the wrong direction.

    ``inconsistent-address`` asserts that one speaker→addressee pair mixes tú and
    usted *within the passage*, which makes any single-line fix partial by
    construction. The judge normalized Nancy's ``usted le dijo`` to tú while the
    next, unflagged line still read ``Usted le dijo que podía estar contenta`` —
    applying it would have created the inconsistency it claimed to fix.
    """

    def test_fix_leaving_the_replaced_form_standing_is_withheld(self):
        text = (
            "—Ah, ya sé —rió por lo bajo—. Es justo lo contrario de lo que usted le dijo a la "
            "señora Snow.\n\n—¿Lo contrario? —repitió Pollyanna, evidentemente desconcertada."
            "\n\n—Sí. Usted le dijo que podía estar contenta porque los demás no eran como "
            "ella... todos enfermos, ¿sabe?"
        )
        old = (
            "—Ah, ya sé —rió por lo bajo—. Es justo lo contrario de lo que usted le dijo a la "
            "señora Snow."
        )
        new = (
            "—Ah, ya sé —rió por lo bajo—. Es justo lo contrario de lo que tú le dijiste a la "
            "señora Snow."
        )
        result = classify_fix(_issue(old, new, rule="inconsistent-address"), text)
        assert isinstance(result, ManualFinding)
        assert result.reason == REASON_MIXED_REGISTER_REMAINS

    def test_fix_that_resolves_the_whole_mixture_stays_applicable(self):
        text = (
            "—Ah, ya sé —rió—. Es justo lo contrario de lo que usted le dijo.\n\n"
            "—Sí —asintió Pollyanna."
        )
        old = "Es justo lo contrario de lo que usted le dijo."
        new = "Es justo lo contrario de lo que tú le dijiste."
        result = classify_fix(_issue(old, new, rule="inconsistent-address"), text)
        assert isinstance(result, ProposedFix)

    def test_the_guard_is_scoped_to_inconsistent_address(self):
        """``wrong-form-*`` names one direction the map dictates, not a mixture.

        Other characters in the chunk legitimately use the other register, so the
        same text must stay applicable under a different rule id.
        """
        text = (
            "—Ah, ya sé —rió—. Es justo lo contrario de lo que usted le dijo.\n\n"
            "—Sí. Usted ya lo sabía —dijo el doctor."
        )
        old = "Es justo lo contrario de lo que usted le dijo."
        new = "Es justo lo contrario de lo que tú le dijiste."
        result = classify_fix(_issue(old, new, rule="wrong-form-tu-expected"), text)
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
