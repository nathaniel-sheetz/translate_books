"""Tests for src/harness_guard.py — the translate-harness validation guards.

Covers the two boundaries an agent-authored artifact crosses:
  1. glossary proposals (dicts)  -> guard_glossary_proposals  (parse boundary / KeyError)
  2. written style.json / glossary.json / chunk.json -> validate_*_file (Pydantic loaders)
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from src.harness_guard import (
    MIN_TERMS_FOR_DIACRITIC_CHECK,
    HarnessValidationError,
    address_map_name_warnings,
    diacritic_warning,
    glossary_convention_warnings,
    guard_glossary_proposals,
    guard_translation_draft,
    validate_chunk_file,
    validate_glossary_file,
    validate_style_guide_file,
)
from src.glossary_bootstrap import glossary_terms_from_proposals
from src.models import (
    AddressMap,
    AddressPair,
    AddressRule,
    Chunk,
    ChunkMetadata,
    Glossary,
    GlossaryTerm,
    StyleGuide,
)
from src.utils.file_io import save_chunk, save_glossary, save_style_guide


# --------------------------------------------------------------------------- #
# guard_glossary_proposals — the parse boundary before glossary_terms_from_proposals
# --------------------------------------------------------------------------- #

class TestGuardGlossaryProposals:
    def test_valid_proposals_pass_through(self):
        proposals = [
            {"english": "Harry", "translation": "Harry", "type": "character"},
            {"english": "wand", "spanish": "varita", "type": "other"},
        ]
        assert guard_glossary_proposals(proposals) is proposals
        # And the bootstrap helper that previously KeyError'd now succeeds.
        terms = glossary_terms_from_proposals(proposals)
        assert {t.english for t in terms} == {"Harry", "wand"}

    def test_missing_english_raises_not_keyerror(self):
        # Without the guard this dict hits `p["english"]` -> KeyError 500.
        proposals = [{"translation": "varita", "type": "other"}]
        with pytest.raises(HarnessValidationError) as exc:
            guard_glossary_proposals(proposals)
        assert "english" in str(exc.value)
        assert "entry 0" in str(exc.value)

    def test_empty_english_rejected(self):
        with pytest.raises(HarnessValidationError):
            guard_glossary_proposals([{"english": "   ", "translation": "x"}])

    def test_missing_translation_and_spanish_rejected(self):
        with pytest.raises(HarnessValidationError) as exc:
            guard_glossary_proposals([{"english": "Harry"}])
        assert "translation" in str(exc.value)

    def test_reports_every_bad_entry_at_once(self):
        proposals = [
            {"english": "ok", "translation": "ok"},   # good
            {"translation": "missing english"},        # bad: no english
            {"english": "no translation"},             # bad: no translation
        ]
        with pytest.raises(HarnessValidationError) as exc:
            guard_glossary_proposals(proposals)
        msg = str(exc.value)
        assert "entry 1" in msg and "entry 2" in msg

    def test_non_list_rejected(self):
        with pytest.raises(HarnessValidationError):
            guard_glossary_proposals({"english": "Harry"})  # type: ignore[arg-type]

    def test_non_dict_entry_rejected(self):
        with pytest.raises(HarnessValidationError):
            guard_glossary_proposals(["just a string"])  # type: ignore[list-item]

    def test_integer_translation_does_not_raise_attribute_error(self):
        # LLM can produce {"translation": 3} — guard must raise HarnessValidationError,
        # not AttributeError from calling .strip() on an int.
        proposals = [{"english": "three", "translation": 3}]
        try:
            guard_glossary_proposals(proposals)
        except HarnessValidationError:
            pass  # also acceptable — int coerced to "3" is truthy, may pass or fail
        except AttributeError as e:
            pytest.fail(f"guard raised AttributeError instead of HarnessValidationError: {e}")


# --------------------------------------------------------------------------- #
# diacritic_warning — soft accent-stripping smell-check (#21)
# --------------------------------------------------------------------------- #

class TestDiacriticWarning:
    @staticmethod
    def _ascii_spanish(n: int) -> list[dict]:
        # n all-ASCII "Spanish" proposals — the accent-stripped smell.
        words = ["senor", "lenera", "manana", "Tia", "Dia", "montana", "nino", "cancion",
                 "corazon", "pequeno", "arbol", "rapido"]
        return [{"english": f"e{i}", "translation": words[i % len(words)]} for i in range(n)]

    def test_all_ascii_spanish_warns(self):
        warn = diacritic_warning(self._ascii_spanish(MIN_TERMS_FOR_DIACRITIC_CHECK), "es")
        assert warn is not None
        assert "es" in warn

    def test_one_accented_term_silences_warning(self):
        proposals = self._ascii_spanish(MIN_TERMS_FOR_DIACRITIC_CHECK)
        proposals[0]["translation"] = "Tomás"  # a single real accent is enough
        assert diacritic_warning(proposals, "es") is None

    def test_english_target_never_warns(self):
        # English glossary is legitimately ASCII — language gate must skip it.
        assert diacritic_warning(self._ascii_spanish(MIN_TERMS_FOR_DIACRITIC_CHECK), "en") is None

    def test_small_glossary_below_threshold_no_warning(self):
        assert diacritic_warning(self._ascii_spanish(MIN_TERMS_FOR_DIACRITIC_CHECK - 1), "es") is None

    def test_none_language_code_no_crash(self):
        assert diacritic_warning(self._ascii_spanish(MIN_TERMS_FOR_DIACRITIC_CHECK), None) is None

    def test_uppercase_and_padded_language_code_normalized(self):
        assert diacritic_warning(self._ascii_spanish(MIN_TERMS_FOR_DIACRITIC_CHECK), " ES ") is not None

    def test_reads_spanish_key_alias(self):
        # Proposals may carry "spanish" instead of "translation" (same alias the guard accepts).
        proposals = [{"english": f"e{i}", "spanish": "senor"} for i in range(MIN_TERMS_FOR_DIACRITIC_CHECK)]
        assert diacritic_warning(proposals, "es") is not None


# --------------------------------------------------------------------------- #
# validate_*_file — wrap the existing Pydantic loaders
# --------------------------------------------------------------------------- #

class TestValidateGlossaryFile:
    def test_valid_glossary_file(self, tmp_path: Path):
        path = tmp_path / "glossary.json"
        save_glossary(Glossary(terms=[GlossaryTerm(english="Harry", spanish="Harry")]), path)
        result = validate_glossary_file(path)
        assert result.terms[0].english == "Harry"

    def test_malformed_glossary_raises(self, tmp_path: Path):
        path = tmp_path / "glossary.json"
        path.write_text('{"terms": "not a list"}', encoding="utf-8")
        with pytest.raises(HarnessValidationError) as exc:
            validate_glossary_file(path)
        assert "Glossary" in str(exc.value)

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(HarnessValidationError):
            validate_glossary_file(tmp_path / "nope.json")


class TestValidateStyleGuideFile:
    def test_valid_style_guide_file(self, tmp_path: Path):
        path = tmp_path / "style.json"
        save_style_guide(StyleGuide(content="TONE: formal"), path)
        result = validate_style_guide_file(path)
        assert "formal" in result.content

    def test_malformed_style_guide_raises(self, tmp_path: Path):
        path = tmp_path / "style.json"
        path.write_text('{"version": "1.0"}', encoding="utf-8")  # missing required `content`
        with pytest.raises(HarnessValidationError) as exc:
            validate_style_guide_file(path)
        assert "Style guide" in str(exc.value)


class TestValidateChunkFile:
    def test_valid_chunk_file(self, tmp_path: Path):
        path = tmp_path / "chapter_01_chunk_000.json"
        chunk = Chunk(
            id="chapter_01_chunk_000", chapter_id="chapter_01", position=0,
            source_text="Hello.",
            metadata=ChunkMetadata(char_start=0, char_end=6, overlap_start=0,
                                   overlap_end=0, paragraph_count=1, word_count=1),
        )
        save_chunk(chunk, path)
        result = validate_chunk_file(path)
        assert result.source_text == "Hello."

    def test_malformed_chunk_raises(self, tmp_path: Path):
        path = tmp_path / "bad_chunk.json"
        path.write_text('{"id": 123}', encoding="utf-8")  # wrong types / missing fields
        with pytest.raises(HarnessValidationError):
            validate_chunk_file(path)


# --------------------------------------------------------------------------- #
# guard_translation_draft — worker-prose validation before stamping (Phase B A4)
# --------------------------------------------------------------------------- #

def _chunk(source_text: str) -> Chunk:
    """Build a PENDING chunk with the given English source (no translation yet)."""
    return Chunk(
        id="ch01_chunk_001",
        chapter_id="chapter_01",
        position=1,
        source_text=source_text,
        metadata=ChunkMetadata(
            char_start=0, char_end=max(1, len(source_text)),
            overlap_start=0, overlap_end=0, paragraph_count=1,
            word_count=len(source_text.split()),
        ),
    )


# A natural-length English source and a plausible Spanish rendering (~same length).
_SRC = "The sun rose over the quiet village and the children ran out to play in the fresh morning air."
_OK = "El sol salio sobre el tranquilo pueblo y los ninos corrieron a jugar en el aire fresco de la manana."


class TestGuardTranslationDraft:
    def test_valid_translation_passes(self):
        assert guard_translation_draft(_chunk(_SRC), _OK) == []

    def test_empty_is_flagged(self):
        assert guard_translation_draft(_chunk(_SRC), "") == ["empty or whitespace-only translation"]
        assert guard_translation_draft(_chunk(_SRC), "   \n  ")[0].startswith("empty")

    def test_echo_of_source_is_flagged(self):
        problems = guard_translation_draft(_chunk(_SRC), _SRC)
        assert any("verbatim copy" in p for p in problems)

    def test_dropped_image_token_is_flagged(self):
        src = f"{_SRC} [IMAGE:img/p7.jpg]"
        # Same prose but the image token was dropped by the worker.
        problems = guard_translation_draft(_chunk(src), _OK)
        assert any("image-token filename mismatch" in p and "dropped" in p for p in problems)

    def test_hallucinated_image_token_is_flagged(self):
        problems = guard_translation_draft(_chunk(_SRC), f"{_OK} [IMAGE:fake.png]")
        assert any("image-token filename mismatch" in p and "hallucinated" in p for p in problems)

    def test_preserved_image_token_with_translated_description_passes(self):
        src = f"{_SRC} [IMAGE:img/p7.jpg:a dog runs]"
        out = f"{_OK} [IMAGE:img/p7.jpg:un perro corre]"
        # Filename matches; description differs (translated) -> no image problem.
        problems = guard_translation_draft(_chunk(src), out)
        assert not any("image-token" in p for p in problems)

    def test_duplicated_image_token_in_output_is_flagged(self):
        # Worker echoed the source block then translated below it — token appears twice.
        src = f"{_SRC} [IMAGE:img/p7.jpg]"
        out = f"{_OK} [IMAGE:img/p7.jpg] [IMAGE:img/p7.jpg]"
        problems = guard_translation_draft(_chunk(src), out)
        assert any("image-token filename mismatch" in p for p in problems)

    def test_dropped_footnote_token_is_flagged(self):
        src = f"{_SRC} [FOOTNOTE:1] more. [FOOTNOTE:2]"
        # Worker kept #1 and dropped #2 (observed Animal Story Book friction).
        out = f"{_OK} [FOOTNOTE:1] mas."
        problems = guard_translation_draft(_chunk(src), out)
        assert any("footnote-token-parity" in p and "dropped" in p for p in problems)

    def test_hallucinated_footnote_token_is_flagged(self):
        problems = guard_translation_draft(_chunk(_SRC), f"{_OK} [FOOTNOTE:9]")
        assert any("footnote-token-parity" in p and "hallucinated" in p for p in problems)

    def test_preserved_footnote_tokens_pass(self):
        src = f"{_SRC} [FOOTNOTE:1] end. [FOOTNOTE:2]"
        out = f"{_OK} [FOOTNOTE:1] fin. [FOOTNOTE:2]"
        problems = guard_translation_draft(_chunk(src), out)
        assert not any("footnote-token-parity" in p for p in problems)

    def test_too_short_translation_is_flagged_by_length(self):
        # < 0.5x source length -> length evaluator ERROR.
        problems = guard_translation_draft(_chunk(_SRC), "El sol.")
        assert any(p.startswith("length:") for p in problems)

    def test_placeholder_text_is_flagged_by_completeness(self):
        problems = guard_translation_draft(_chunk(_SRC), _OK + " [TRANSLATION HERE]")
        assert any(p.startswith("completeness:") for p in problems)

    def test_broken_evaluator_caught_not_propagated(self, monkeypatch):
        """A crashing evaluator must NOT raise — its error is captured as a problem string."""
        from unittest.mock import MagicMock
        from src import harness_guard

        bad_eval = MagicMock()
        bad_eval.name = "boom"
        bad_eval.evaluate.side_effect = RuntimeError("internal evaluator failure")
        monkeypatch.setattr(harness_guard, "CompletenessEvaluator", lambda: bad_eval)
        problems = guard_translation_draft(_chunk(_SRC), _OK)
        assert any("boom evaluator failed" in p for p in problems)


# ── glossary alternatives conventions (advisory) ────────────────────────────
#
# `alternatives` is the one glossary field that lets a worker pick a different
# rendering per chunk. Right for a term genuinely rendered several ways; wrong
# for a name, where it silently licenses book-wide inconsistency. These checks
# are advisory — a real exception (Atlántico / océano Atlántico) trips one
# legitimately — so they must never raise.

def _prop(english, translation, type_="other", alternatives=None):
    return {"english": english, "translation": translation, "type": type_,
            "alternatives": alternatives or []}


class TestGlossaryConventionWarnings:
    def test_place_with_alternatives_flagged(self):
        w = glossary_convention_warnings(
            [_prop("Beldingsville", "Beldingsville", "place", ["el pueblo de Beldingsville"])]
        )
        assert len(w) == 1
        assert w[0].startswith("REVIEW:")
        assert "Beldingsville" in w[0] and "place" in w[0]

    def test_place_without_alternatives_clean(self):
        assert glossary_convention_warnings([_prop("Boston", "Boston", "place")]) == []

    def test_bare_personal_name_with_alternatives_flagged(self):
        w = glossary_convention_warnings([_prop("Pollyanna", "Pollyanna", "character", ["Poli"])])
        assert len(w) == 1
        assert "bare personal name" in w[0]

    def test_bare_personal_name_without_alternatives_clean(self):
        assert glossary_convention_warnings([_prop("Pollyanna", "Pollyanna", "character")]) == []

    def test_title_name_missing_article_flagged(self):
        w = glossary_convention_warnings([_prop("Aunt Polly", "tía Polly", "character")])
        assert len(w) == 1
        assert "narration form with the article" in w[0]

    def test_title_name_missing_vocative_alternative_flagged(self):
        w = glossary_convention_warnings([_prop("Uncle Antony", "el tío Antony", "character")])
        assert len(w) == 1
        assert "no vocative alternative" in w[0]
        assert "tío Antony" in w[0]

    def test_title_name_done_correctly_is_clean(self):
        """Narration form with article primary, bare vocative as the single alternative."""
        assert glossary_convention_warnings([
            _prop("Uncle Antony", "el tío Antony", "character", ["tío Antony"]),
            _prop("Mrs. Banks", "la señora Banks", "character", ["señora Banks"]),
            _prop("Doctor Hernández", "el doctor Hernández", "character", ["doctor Hernández"]),
        ]) == []

    def test_non_name_term_may_carry_alternatives(self):
        assert glossary_convention_warnings([
            _prop("the game", "el juego de alegrarse", "concept", ["el juego"]),
            _prop("stall", "establo", "technical", ["casilla"]),
        ]) == []

    def test_never_raises_on_malformed_input(self):
        """The structural guard owns malformed drafts; this must stay advisory."""
        assert glossary_convention_warnings("not a list") == []
        assert glossary_convention_warnings([None, 42, {}, {"english": "x"}]) == []

    def test_flags_are_prefixed_for_triage(self):
        """The skill splits REVIEW: judgement calls from draft bugs by this prefix."""
        w = glossary_convention_warnings([
            _prop("Beldingsville", "Beldingsville", "place", ["pueblo"]),
            _prop("Aunt Polly", "tía Polly", "character"),
        ])
        assert len(w) == 2
        assert all(x.startswith("REVIEW:") for x in w)


# ── address-map cast reconciliation (advisory) ──────────────────────────────

def _glossary(*pairs):
    return Glossary(terms=[
        GlossaryTerm(english=en, spanish=es, type="character") for en, es in pairs
    ])


def _map(content, *, pairs=(), summary=None):
    return AddressMap(
        content=content,
        style_guide_summary=summary,
        pairs=[AddressPair(a=a, b=b, directions={
            "a_to_b": [AddressRule(form="tú", when="default")],
        }) for a, b in pairs],
    )


class TestAddressMapNameWarnings:
    def test_english_names_flagged_after_glossary_approval(self):
        w = address_map_name_warnings(
            _glossary(("Aunt Polly", "la tía Polly"), ("Pollyanna", "Pollyanna")),
            _map("Pollyanna uses usted to Aunt Polly.", pairs=[("Pollyanna", "Aunt Polly")]),
        )
        assert len(w) == 1
        assert w[0].startswith("REVIEW:")
        assert "Aunt Polly" in w[0] and "la tía Polly" in w[0]
        # Pollyanna is unchanged by translation, so it is not drift.
        assert "1 approved character" in w[0]

    def test_reconciled_map_is_silent(self):
        assert address_map_name_warnings(
            _glossary(("Aunt Polly", "la tía Polly")),
            _map("Pollyanna uses usted to la tía Polly.", pairs=[("Pollyanna", "la tía Polly")]),
        ) == []

    def test_name_found_in_summary_counts_as_reconciled(self):
        assert address_map_name_warnings(
            _glossary(("Aunt Polly", "la tía Polly")),
            _map("...", summary="Everyone addresses la tía Polly with usted."),
        ) == []

    def test_missing_artifacts_are_not_an_error(self):
        assert address_map_name_warnings(None, _map("x")) == []
        assert address_map_name_warnings(_glossary(("A", "B")), None) == []
        assert address_map_name_warnings(_glossary(("A", "B")), _map("")) == []

    def test_only_character_terms_are_checked(self):
        g = Glossary(terms=[GlossaryTerm(english="Boston", spanish="Bostón", type="place")])
        assert address_map_name_warnings(g, _map("A scene set in Boston.")) == []
