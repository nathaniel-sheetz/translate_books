"""The modernize mode: spelling pass, over-edit guard, archaism list, prompt."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from src.api_translator import build_translation_prompt, build_translation_prompt_parts
from src.harness_guard import guard_translation_draft
from src.models import Chunk, ChunkMetadata
from src.modernize import archaisms, guard, mode
from src.modernize.orthography import modernize_spelling

WORDS = {"árbol", "áfrica", "casa", "maría", "mar", "maria"}


def is_word(w: str) -> bool:
    return w.lower() in WORDS


def _chunk(source_text: str) -> Chunk:
    return Chunk(
        id="ch01_chunk_001", chapter_id="chapter_01", position=1, source_text=source_text,
        metadata=ChunkMetadata(
            char_start=0, char_end=max(1, len(source_text)), overlap_start=0, overlap_end=0,
            paragraph_count=1, word_count=len(source_text.split()),
        ),
    )


PASSAGE = (
    "Era la noche del veinticuatro de diciembre, y los niños del Presidente estaban "
    "en el comedor, esperando con gran impaciencia que se abriese la puerta del salón, "
    "donde su padre y su madre preparaban el árbol con mucho cuidado y en silencio.\n\n"
    "María, que era la menor de todos, se acercó a la puerta y miró por el agujero de "
    "la cerradura, pero no vio nada más que una luz muy viva que salía del salón."
)


# ── spelling pass ────────────────────────────────────────────────────────────

class TestOrthography:
    def test_standalone_accented_letters(self):
        assert modernize_spelling("fué á casa ó al mar", is_word=is_word) == "fue a casa o al mar"

    def test_letters_inside_words_untouched(self):
        assert modernize_spelling("está allá", is_word=is_word) == "está allá"

    def test_capital_standalone(self):
        assert modernize_spelling("Á las doce", is_word=is_word) == "A las doce"

    def test_monosyllables_keep_case(self):
        assert modernize_spelling("Dió un paso. Sólo él vió", is_word=is_word) == "Dio un paso. Solo él vio"

    def test_demonstratives(self):
        assert modernize_spelling("éste y aquélla", is_word=is_word) == "este y aquella"

    def test_capital_accent_from_dictionary(self):
        assert modernize_spelling("EL ARBOL de Africa", is_word=is_word) == "EL ÁRBOL de África"

    def test_capital_accent_left_when_bare_form_is_a_word(self):
        # «Maria» is in the word list, so it is not touched.
        assert modernize_spelling("Maria", is_word=is_word) == "Maria"

    def test_idempotent(self):
        once = modernize_spelling("fué á ARBOL éste", is_word=is_word)
        assert modernize_spelling(once, is_word=is_word) == once

    def test_counts(self):
        counts: Counter = Counter()
        modernize_spelling("á y á", is_word=is_word, counts=counts)
        assert counts["á -> a"] == 2

    def test_verse_lines_keep_their_breaks(self):
        verse = "Debe la péndola hacer rom, rom,\navanza y retrocede á su son;"
        assert modernize_spelling(verse, is_word=is_word) == (
            "Debe la péndola hacer rom, rom,\navanza y retrocede a su son;")


# ── over-edit guard ──────────────────────────────────────────────────────────

class TestOverEditGuard:
    def test_identical_draft_passes(self):
        rep = guard.over_edit_report(PASSAGE, PASSAGE)
        assert rep.changed == 0 and not rep.problems and not rep.warnings

    def test_small_edit_passes(self):
        draft = PASSAGE.replace("se abriese", "se abriera")
        rep = guard.over_edit_report(PASSAGE, draft)
        assert 0 < rep.changed < guard.WARN_CHANGED and not rep.problems

    def test_rewrite_is_rejected(self):
        draft = ("La víspera de Navidad los hijos del juez aguardaban impacientes en el "
                 "comedor a que sus papás terminaran de adornar el árbol en la sala.\n\n"
                 "La pequeña María espió por la cerradura y solo distinguió un resplandor.")
        rep = guard.over_edit_report(PASSAGE, draft)
        assert any("over-edit" in p for p in rep.problems)

    def test_dropped_text_fails_length(self):
        draft = PASSAGE.split("\n\n")[0]
        rep = guard.over_edit_report(PASSAGE, draft)
        assert any("length ratio" in p for p in rep.problems)

    def test_duplicated_sentence_is_rejected(self):
        first, second = PASSAGE.split("\n\n")
        draft = second + "\n\n" + first + "\n\n" + second
        rep = guard.over_edit_report(PASSAGE, draft)
        assert any(p.startswith("added") for p in rep.problems)

    def test_dropped_sentence_is_rejected(self):
        draft = PASSAGE.replace("con mucho cuidado y en silencio", "")
        draft = draft.replace(", pero no vio nada más que una luz muy viva que salía del salón", "")
        rep = guard.over_edit_report(PASSAGE, draft)
        assert any(p.startswith("dropped") for p in rep.problems)

    def test_small_insert_passes(self):
        draft = PASSAGE.replace("se acercó a la puerta", "se acercó despacio a la puerta")
        assert not guard.over_edit_report(PASSAGE, draft).problems

    def test_paragraph_warning_names_the_paragraph(self):
        first, second = PASSAGE.split("\n\n")
        second_new = ("La menor de todos, María, fue hasta la puerta para espiar por la "
                      "cerradura y solo distinguió una luz intensa saliendo de allí.")
        rep = guard.over_edit_report(PASSAGE, first + "\n\n" + second_new)
        assert any(w.startswith("paragraph 2") for w in rep.warnings)

    def test_harness_guard_skips_echo_check_for_modernize(self):
        chunk = _chunk(PASSAGE)
        # A translation guard rejects a copy of the source...
        assert any("verbatim" in p for p in guard_translation_draft(chunk, PASSAGE))
        # ...a modernize guard accepts it: nothing needed changing.
        warns: list[str] = []
        assert guard_translation_draft(
            chunk, PASSAGE, modernize_reference=PASSAGE, warnings=warns) == []

    def test_harness_guard_rejects_modernize_rewrite(self):
        chunk = _chunk(PASSAGE)
        problems = guard_translation_draft(
            chunk, "Texto distinto por completo, nada que ver con el original de antes.",
            modernize_reference=PASSAGE)
        assert any("over-edit" in p or "length" in p for p in problems)


# ── archaism list ────────────────────────────────────────────────────────────

def _entries(*rows):
    return [archaisms.Entry.from_json(r) for r in rows]


class TestArchaisms:
    def test_literal_swap_keeps_case(self):
        e = _entries({"id": "b1", "old": "Mas", "replacement": "Pero", "mode": "literal"},
                     {"id": "b2", "old": "No te enfades", "replacement": "no te enojes",
                      "mode": "literal"})
        out, changes = archaisms.apply_literal("Mas no. ¡No te enfades! mas tarde", e)
        assert out == "Pero no. ¡No te enojes! pero tarde"
        assert [c["id"] for c in changes] == ["b1", "b2", "b1"]

    def test_whole_words_only(self):
        e = _entries({"id": "b1", "old": "calzón", "replacement": "pantalón", "mode": "literal"})
        out, _ = archaisms.apply_literal("calzones y calzón", e)
        assert out == "calzones y pantalón"

    def test_longest_first_and_single_pass(self):
        e = _entries(
            {"id": "s", "old": "menester", "replacement": "necesario", "mode": "literal"},
            {"id": "l", "old": "es menester", "replacement": "hace falta", "mode": "literal"},
        )
        out, changes = archaisms.apply_literal("Es menester ir; lo menester.", e)
        assert out == "Hace falta ir; lo necesario."
        assert [c["id"] for c in changes] == ["l", "s"]

    def test_old_form_is_spelling_fixed_and_spaces_flexible(self):
        e = _entries({"id": "b", "old": "á guisa de", "replacement": "a modo de", "mode": "literal"})
        out, _ = archaisms.apply_literal("puesto a  guisa\tde sombrero", e)
        assert out == "puesto a modo de sombrero"

    def test_verse_left_alone(self):
        e = _entries({"id": "b", "old": "calzón", "replacement": "pantalón", "mode": "literal"})
        text = "Un calzón.\n\nverso del calzón\nsegundo verso"
        out, changes = archaisms.apply_literal(text, e)
        assert out == "Un pantalón.\n\nverso del calzón\nsegundo verso"
        assert len(changes) == 1

    def test_guided_not_swapped_but_found(self):
        e = _entries({"id": "g", "old": "barba", "replacement": "mandíbula", "mode": "guided",
                      "scope": "only", "only_when": "the jaw"})
        text = "se le dislocó la barba"
        out, changes = archaisms.apply_literal(text, e)
        assert out == text and not changes
        hits = archaisms.guided_hits(text, e)
        assert [h["id"] for h in hits] == ["g"] and hits[0]["offset"] == text.index("barba")

    def test_load_list_and_log(self, tmp_path):
        (tmp_path / archaisms.LIST_NAME).write_text(json.dumps({"entries": [
            {"id": "b", "old": "calzón", "replacement": "pantalón", "mode": "literal"},
            {"id": "x", "old": "nada", "replacement": "", "mode": "literal"},
        ]}), encoding="utf-8")
        entries = archaisms.load_list(tmp_path)
        assert [e.id for e in entries] == ["b"]  # an entry with no replacement is dropped
        _, changes = archaisms.apply_literal("el calzón", entries)
        archaisms.log_changes(tmp_path, "c1", changes, writer="t")
        row = json.loads((tmp_path / archaisms.LOG_NAME).read_text(encoding="utf-8"))
        assert row["chunk_id"] == "c1" and row["new"] == "pantalón"

    def test_no_list_is_empty(self, tmp_path):
        assert archaisms.load_list(tmp_path) == []


# ── mode and prompt ──────────────────────────────────────────────────────────

class TestModeAndPrompt:
    def test_translate_book_has_no_template_and_raw_source(self):
        cfg = {"task": "translate"}
        assert mode.template_path(cfg) is None
        assert mode.prompt_source(cfg, "fué á") == "fué á"
        assert mode.template_path({}) is None

    def test_modernize_template_and_spelling(self):
        cfg = {"task": "modernize"}
        assert mode.template_path(cfg).name == "modernize.txt"
        assert mode.prompt_source(cfg, "fué á casa") == "fue a casa"
        assert mode.template_path({"task": "modernize", "prompt_template": "x/y.txt"}).name == "y.txt"

    def test_language_name(self):
        assert mode.language_name("es") == "Spanish"
        assert mode.language_name("es-MX") == "Spanish"
        assert mode.language_name("xx") == "xx"

    def test_default_template_path_is_byte_identical(self):
        chunk = _chunk(PASSAGE)
        assert build_translation_prompt(chunk) == build_translation_prompt(chunk, template_path=None)

    def test_modernize_prompt_renders_and_splits_for_the_cache(self):
        chunk = _chunk("fue a casa")
        tpl = mode.template_path({"task": "modernize"})
        prefix, suffix = build_translation_prompt_parts(
            chunk, project_name="Historia de un cascanueces", source_language="Spanish",
            always_include_dialogue=True, template_path=tpl,
        )
        full = prefix + suffix
        assert prefix and suffix.startswith("=" * 80 + "\nGLOSSARY TERMS")
        assert "MODERNIZED EDITION TASK" in prefix and "«Historia de un cascanueces»" in prefix
        assert "DIALOGUE FORMATTING" in prefix
        assert "{{" not in full
        assert "fue a casa" in suffix
        assert not full.startswith("#")  # header comments stripped


# ── replacements already in the text ─────────────────────────────────────────

class TestAlreadyReplaced:
    def test_literal_does_not_double_a_modern_phrase(self):
        e = _entries({"id": "g", "old": "Cuanto a su madre", "replacement": "En cuanto a su madre",
                      "mode": "literal"})
        out, changes = archaisms.apply_literal("En cuanto a su madre, nada. Cuanto a su madre, todo.", e)
        assert out == "En cuanto a su madre, nada. En cuanto a su madre, todo."
        assert len(changes) == 1

    def test_guided_hit_inside_its_replacement_is_skipped(self):
        e = _entries({"id": "g", "old": "Cuanto al", "replacement": "En cuanto al", "mode": "guided",
                      "scope": "only", "only_when": "bare form"})
        hits = archaisms.guided_hits("En cuanto al astrólogo, no. Cuanto al joven, sí.", e)
        assert [h["old"] for h in hits] == ["Cuanto al"]


# ── the guided pass ──────────────────────────────────────────────────────────

GUIDED = {"id": "b057", "old": "barba", "replacement": "mandíbula", "mode": "guided",
          "scope": "only", "only_when": "Only the Nutcracker's jaw."}


def _item(text: str, old: str = "barba", nth: int = 0) -> dict:
    import re as _re
    start = [m.start() for m in _re.finditer(old, text)][nth]
    return {"n": 1, "chunk_id": "c", "id": "b057", "old": old, "offset": start,
            "paragraph": text, "paragraph_start": 0}


class TestGuidedCheck:
    from src.modernize import guided as G  # noqa: E402

    TEXT = "Tenía la barba sujeta con una cinta, y su barba blanca le sentaba bien."

    def entry(self):
        return archaisms.Entry.from_json(GUIDED)

    def test_keep(self):
        edit, problem = self.G.check_answer(_item(self.TEXT), self.entry(),
                                            {"decision": "keep", "reason": "beard"})
        assert edit is None and problem is None

    def test_replace_lands_on_the_marked_occurrence(self):
        item = _item(self.TEXT, nth=1)
        edit, problem = self.G.check_answer(item, self.entry(), {
            "decision": "replace", "before": "su barba blanca", "after": "su mandíbula blanca"})
        assert problem is None and self.TEXT[edit["start"]:edit["end"]] == "su barba blanca"

    def test_span_around_another_occurrence_is_rejected(self):
        edit, problem = self.G.check_answer(_item(self.TEXT, nth=1), self.entry(), {
            "decision": "replace", "before": "la barba sujeta", "after": "la mandíbula sujeta"})
        assert edit is None and "not copied" in problem

    def test_copied_marks_are_tolerated(self):
        edit, problem = self.G.check_answer(_item(self.TEXT), self.entry(), {
            "decision": "replace", "before": "la ⟦barba⟧ sujeta", "after": "la mandíbula sujeta"})
        assert problem is None and edit["before"] == "la barba sujeta"

    def test_old_form_left_in_is_rejected(self):
        _e, problem = self.G.check_answer(_item(self.TEXT), self.entry(), {
            "decision": "replace", "before": "la barba sujeta", "after": "la barba atada"})
        assert "still in" in problem

    def test_rewrite_is_rejected(self):
        _e, problem = self.G.check_answer(_item(self.TEXT), self.entry(), {
            "decision": "replace", "before": "Tenía la barba sujeta con una cinta",
            "after": "Llevaba la mandíbula amarrada con un listón de seda roja"})
        assert "more than the swap" in problem

    def test_unknown_decision_is_rejected(self):
        _e, problem = self.G.check_answer(_item(self.TEXT), self.entry(), {"decision": "maybe"})
        assert "neither" in problem


class TestGuidedRoundTrip:
    def _book(self, tmp_path):
        from src.utils.file_io import save_chunk

        book = tmp_path / "book"
        (book / "chunks").mkdir(parents=True)
        (book / ".harness").mkdir()
        (book / ".harness" / "config.json").write_text(json.dumps(
            {"task": "modernize", "title": "T", "worker_model": "claude-sonnet-5-5",
             "source_language_code": "es", "language_code": "es"}), encoding="utf-8")
        (book / archaisms.LIST_NAME).write_text(json.dumps({"entries": [GUIDED]}), encoding="utf-8")
        text = "Tenía la barba sujeta con una cinta.\n\nSu barba blanca le sentaba bien."
        chunk = _chunk(text).model_copy(update={"id": "chapter_01_chunk_001", "translated_text": text})
        save_chunk(chunk, book / "chunks" / "chapter_01_chunk_001.json")
        return book

    def test_prepare_apply_and_rerun(self, tmp_path):
        from src.modernize import guided as G
        from src.utils.file_io import load_chunk

        book = self._book(tmp_path)
        out = G.prepare(book)
        assert out["jobs"] == 1 and out["occurrences"] == 2
        body = (G.pass_dir(book) / "chapter_01_chunk_001.body.txt").read_text(encoding="utf-8")
        assert "la ⟦barba⟧ sujeta" in body and "Replace only when" in body
        (G.pass_dir(book) / "chapter_01_chunk_001.draft.json").write_text(json.dumps({"items": [
            {"n": 1, "decision": "replace", "before": "la barba sujeta", "after": "la mandíbula sujeta",
             "reason": "jaw"},
            {"n": 2, "decision": "keep", "before": "", "after": "", "reason": "beard"},
        ]}), encoding="utf-8")
        res = G.apply(book, no_realign=True)
        assert res["status"] == "ok" and len(res["replaced"]) == 1 and len(res["kept"]) == 1
        text = load_chunk(book / "chunks" / "chapter_01_chunk_001.json").translated_text
        assert text == "Tenía la mandíbula sujeta con una cinta.\n\nSu barba blanca le sentaba bien."
        assert list((book / ".chunk_edits" / "chapter_01").rglob("*.json"))
        # Both decisions are recorded, so nothing is asked twice.
        assert G.prepare(book)["occurrences"] == 0

    def test_failed_check_is_asked_again(self, tmp_path):
        from src.modernize import guided as G

        book = self._book(tmp_path)
        G.prepare(book)
        (G.pass_dir(book) / "chapter_01_chunk_001.draft.json").write_text(json.dumps({"items": [
            {"n": 1, "decision": "replace", "before": "nada de esto", "after": "x"},
            {"n": 2, "decision": "keep", "reason": "beard"},
        ]}), encoding="utf-8")
        res = G.apply(book, no_realign=True)
        assert res["status"] == "partial" and len(res["problems"]) == 1
        assert G.prepare(book)["occurrences"] == 1


# ── the glossary beat ────────────────────────────────────────────────────────

GLOSS_TEXT = (
    "Fritz miró a Drosselmayer. DROSSELMAYER llegó con Zacarías y su levitón.\n\n"
    "Luego Zacarias dijo que Drosselmayer rompya otro levitón."
)


def _fake_classify(text):
    words = {"Fritz": "english", "Drosselmayer": "unknown", "DROSSELMAYER": "unknown",
             "Zacarías": "unknown", "Zacarias": "unknown", "levitón": "unknown"}
    out: dict = {}
    for w in __import__("re").findall(r"[^\W\d_]+", text):
        if w in words:
            out.setdefault(w, {"kind": words[w], "count": 0})["count"] += 1
    return out


class TestModernizeGlossary:
    def test_candidates_group_case_and_mark_accent_pairs(self):
        from src.modernize import glossary as MG

        cands = MG.candidates(GLOSS_TEXT, classify=_fake_classify)
        by = {c["term"]: c for c in cands}
        assert cands[0]["term"] == "Drosselmayer" and cands[0]["frequency"] == 3
        assert by["Drosselmayer"]["forms"] == {"DROSSELMAYER": 1, "Drosselmayer": 2}
        assert by["Zacarías"]["accent_pair"] == ["Zacarias ×1"]
        assert by["Zacarias"]["accent_pair"] == ["Zacarías ×1"]
        assert by["Fritz"]["detection_reasons"] == ["dictionary_english"]
        assert "levitón" in by["levitón"]["contexts"][0][1] and len(by["levitón"]["contexts"]) == 2
        assert MG.candidates(GLOSS_TEXT, classify=_fake_classify, max_candidates=2)[1]["term"] == "levitón"

    def test_format_shows_the_accent_line(self):
        from src.modernize import glossary as MG

        text = MG.format_candidates(MG.candidates(GLOSS_TEXT, classify=_fake_classify))
        assert "SAME WORD, OTHER ACCENTS: Zacarias ×1" in text
        assert "also written: DROSSELMAYER" in text

    def test_accent_slip_warning_names_the_hidden_form(self):
        from src.models import GlossaryTerm
        from src.modernize import glossary as MG

        cands = MG.candidates(GLOSS_TEXT, classify=_fake_classify)
        terms = [GlossaryTerm(english="Zacarías", spanish="Zacarías", type="character"),
                 GlossaryTerm(english="Drosselmayer", spanish="Drosselmayer", type="character")]
        warnings = MG.accent_slip_warnings(terms, cands)
        assert len(warnings) == 1 and "«Zacarias»" in warnings[0]
        # An alias that spells the other form on purpose clears it.
        terms[0] = GlossaryTerm(english="Zacarías", spanish="Zacarías", type="character",
                                aliases=["Zacarias"])
        assert MG.accent_slip_warnings(terms, cands) == []

    def test_defect_draft_is_normalized(self, tmp_path):
        from src.modernize import glossary as MG

        assert MG.load_defects(tmp_path) == ([], [])
        (tmp_path / MG.DEFECTS_DRAFT).write_text(json.dumps([
            {"form": "rompya", "fix": "rompía", "kind": "OCR"},
            {"form": "váis", "kind": "weird"},
            {"fix": "x"},
        ]), encoding="utf-8")
        defects, problems = MG.load_defects(tmp_path)
        assert [d["kind"] for d in defects] == ["ocr", "other"] and len(problems) == 1

    def test_prompt_renders_without_header_or_placeholders(self):
        from src.glossary_bootstrap import build_glossary_prompt
        from src.modernize import glossary as MG

        prompt = build_glossary_prompt(
            MG.candidates(GLOSS_TEXT, classify=_fake_classify), "", "Keep names.", "Spanish",
            context_mode="modernize", book_title="Historia de un cascanueces",
        )
        assert not prompt.startswith("#") and "{{" not in prompt
        assert "«Historia de un cascanueces»" in prompt and "Keep names." in prompt
        assert "1. Drosselmayer" in prompt

    def test_flow_prepare_commit_rescore(self, tmp_path):
        from src.harness import flow, state
        from src.modernize import glossary as MG
        from src.utils.file_io import save_chunk
        from web_ui.evaluations import load_chunk_evaluation

        book = tmp_path / "book"
        (book / "chunks").mkdir(parents=True)
        (book / "chapters").mkdir()
        (book / "source.txt").write_text(GLOSS_TEXT, encoding="utf-8")
        (book / "chapters" / "chapter_01.txt").write_text(GLOSS_TEXT, encoding="utf-8")
        state.save_config(book, {"task": "modernize", "title": "T", "target_language": "Spanish",
                                 "source_language_code": "es", "language_code": "es"})
        chunk = _chunk(GLOSS_TEXT).model_copy(
            update={"id": "chapter_01_chunk_001", "translated_text": GLOSS_TEXT})
        save_chunk(chunk, book / "chunks" / "chapter_01_chunk_001.json")

        prep = flow.glossary_prepare(str(book))
        terms = {c["term"] for c in json.loads(
            (book / ".harness" / MG.CANDIDATES_FILE).read_text(encoding="utf-8"))}
        assert {"Drosselmayer", "Zacarias"} <= terms
        assert "Zacarias" in prep["accent_pairs"]
        assert prep["house_candidates_added"] == 0
        assert "SAME WORD, OTHER ACCENTS" in Path(prep["prompt_path"]).read_text(encoding="utf-8")

        Path(prep["draft_path"]).write_text(json.dumps([
            {"english": "Drosselmayer", "translation": "Drosselmayer", "type": "character"},
            {"english": "Fritz", "translation": "Fritz", "type": "character"},
            {"english": "Zacarías", "translation": "Zacarías", "type": "character"},
            {"english": "levitón", "translation": "levitón", "type": "other"},
        ]), encoding="utf-8")
        Path(prep["defects_draft_path"]).write_text(json.dumps([
            {"form": "Zacarias", "fix": "Zacarías", "kind": "accent", "why": "missing accent"},
        ]), encoding="utf-8")
        out = flow.glossary_commit(str(book))
        assert out["defects"][0]["fix"] == "Zacarías"
        assert any("«Zacarias»" in w for w in out["warnings"])
        assert out["rescored"] == {"chunks": 1}
        evaluation = load_chunk_evaluation(book, "chapter_01_chunk_001")
        dictionary = json.dumps(evaluation, ensure_ascii=False)
        assert "'Drosselmayer'" not in dictionary and "'Fritz'" not in dictionary
        assert "'rompya'" in dictionary  # the checker ran; a word with no entry stays flagged
