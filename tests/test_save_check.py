"""Tests for the save-time check: the rules, the vocabulary, the log, the routes."""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import save_check
from src.models import Glossary, GlossaryTerm, IgnoredTerm, IgnoredTerms
from web_ui.app import app


@pytest.fixture(scope="module")
def speller():
    return save_check.default_speller()


def _rules(hits):
    return {h["rule"] for h in hits}


def _spelt(hits):
    return [h["text"] for h in hits if h["rule"] == "spelling"]


# -------- introduced words --------

class TestIntroducedWords:
    def test_only_words_the_edit_typed(self):
        assert save_check.introduced_words(
            "El gato se sentó.", "El gato gris se sentó."
        ) == ["gris"]

    def test_short_words_and_dialect_are_left_out(self):
        assert save_check.introduced_words("Voy a casa.", "Voy pa' casa de mi 'mano.") == []
        assert save_check.introduced_words("Voy a casa.", "Voy a mi casa.") == []

    def test_image_and_caption_tags_are_not_words(self):
        assert save_check.introduced_words(
            "Un mapa.", "[CAPTION] Un mapa. [IMAGE:images/i01.jpg]"
        ) == []


# -------- spelling --------

class TestSpelling:
    def test_a_typo_is_flagged(self, speller):
        hits = save_check.check_write(
            "The truck stopped.", "El camión se detuvo.", "La camiometa se detuvo.",
            vocabulary={}, speller=speller,
        )
        assert _spelt(hits) == ["camiometa"]

    def test_a_real_word_is_not(self, speller):
        hits = save_check.check_write(
            "The truck stopped.", "El camión se detuvo.", "La camioneta se detuvo.",
            vocabulary={}, speller=speller,
        )
        assert hits == []

    def test_a_word_the_book_uses_in_any_case_passes(self, speller):
        hits = save_check.check_write(
            "", "Los soldados llegaron.", "Los Mirmidones llegaron.",
            vocabulary={"mirmidones": 12}, speller=speller,
        )
        assert hits == []

    def test_a_word_in_the_english_sentence_passes(self, speller):
        hits = save_check.check_write(
            "They sailed to Hekla.", "Navegaron al volcán.", "Navegaron al hekla.",
            vocabulary={}, speller=speller,
        )
        assert hits == []

    def test_a_glossary_form_passes(self, speller):
        glossary = save_check.glossary_words(Glossary(terms=[
            GlossaryTerm(english="Osmond", spanish="Osmundo", alternatives=["Osmondo"]),
        ]))
        for word in ("osmundo", "osmondo"):
            hits = save_check.check_write(
                "", "Vino el escudero.", f"Vino el {word}.",
                vocabulary={}, glossary=glossary, speller=speller,
            )
            assert hits == []

    def test_the_glossary_does_not_excuse_a_dropped_accent(self, speller):
        # Glossary.matches_word folds accents; this check must not.
        glossary = save_check.glossary_words(Glossary(terms=[
            GlossaryTerm(english="Diaz", spanish="Díaz"),
        ]))
        hits = save_check.check_write(
            "", "Habló el señor.", "Habló el señor Diaz.",
            vocabulary={"díaz": 9}, glossary=glossary, speller=speller,
        )
        assert _spelt(hits) == ["Diaz"]

    def test_the_ignore_list_passes_a_word(self, speller):
        ignored = IgnoredTerms(terms=[IgnoredTerm(term="frenón", eval_name="dictionary")])
        hits = save_check.check_write(
            "", "Se detuvo de golpe.", "Se detuvo de un frenón.",
            vocabulary={}, ignored=ignored, speller=speller,
        )
        assert hits == []

    def test_a_grammar_ignore_entry_does_not(self, speller):
        ignored = IgnoredTerms(terms=[
            IgnoredTerm(term="frenón", eval_name="grammar", rule_id="X"),
        ])
        hits = save_check.check_write(
            "", "Se detuvo de golpe.", "Se detuvo de un frenón.",
            vocabulary={}, ignored=ignored, speller=speller,
        )
        assert _spelt(hits) == ["frenón"]

    def test_a_capitalised_word_in_mid_sentence_is_a_name(self, speller):
        hits = save_check.check_write(
            "", "Llegaron hasta Bayard aquella tarde.", "Llegaron hasta Bayardo aquella tarde.",
            vocabulary={"bayard": 30}, speller=speller,
        )
        assert hits == []

    def test_unless_it_is_one_swap_from_a_known_word(self, speller):
        hits = save_check.check_write(
            "", "Fundó la empresa.", "Fundó la Comapñía.",
            vocabulary={}, speller=speller,
        )
        assert _spelt(hits) == ["Comapñía"]

    def test_a_capitalised_typo_that_opens_the_sentence_is_flagged(self, speller):
        hits = save_check.check_write(
            "", "En rigor, no vuela.", "Tecnicamente, no vuela.",
            vocabulary={}, speller=speller,
        )
        assert _spelt(hits) == ["Tecnicamente"]

    def test_without_enchant_the_punctuation_rules_still_run(self):
        hits = save_check.check_write(
            "", "El camión se detuvo.", "La camiometa se detuvo",
            vocabulary={}, speller=None,
        )
        assert _rules(hits) == {"closing_mark_dropped"}


# -------- punctuation --------

class TestPunctuation:
    @pytest.mark.parametrize("rule, before, after", [
        ("unbalanced", "¿Vienes hoy?", "¿Vienes hoy, o mañana."),
        ("comma_before_paren", "Vino tarde (como siempre).", "Vino tarde, (como siempre)."),
        ("period_before_raya", "—Veremos; pero lo dudo.", "—Veremos. —Pero lo dudo."),
        ("raya_closes_guillemet", "«Vamos ya».", "—Vamos ya»"),
        ("closing_mark_dropped", "Llegó a casa tarde.", "Llegó a casa muy tarde"),
        ("doubled_mark", "Vino, y se fue.", "Vino,, y se fue."),
        ("space_before_mark", "Vino, y se fue.", "Vino , y se fue."),
        ("no_space_after_period", "Vino. Luego se fue.", "Vino.Luego se fue."),
        ("repeated_word", "Vino de lejos.", "Vino de de lejos."),
    ])
    def test_each_rule_fires_on_what_the_edit_broke(self, rule, before, after):
        assert rule in _rules(save_check.punctuation_hits(before, after))

    @pytest.mark.parametrize("text", [
        "¿Vienes hoy, o mañana.",
        "Vino tarde, (como siempre).",
        "Vino , y se fue.",
        "Vino de de lejos.",
    ])
    def test_a_fault_already_there_is_not_the_edit_s(self, text):
        assert save_check.punctuation_hits(text, text.replace("Vin", "Lleg")) == []

    def test_a_lone_closing_guillemet_is_left_alone(self):
        # A speech that runs on opens its next paragraph with a lone ».
        assert save_check.punctuation_hits("«Y entonces volvió.", "»Y entonces volvió.") == []

    @pytest.mark.parametrize("before, after", [
        ("—Heb.", "—He"),
        ("—2 Sam.", "—2 S"),
        ("OTRO CEREAL QUE ENCONTRAMOS EN CASI TODA MESA.", "ASÍ SE CULTIVA EL ARROZ"),
    ])
    def test_a_label_or_a_heading_may_lose_its_period(self, before, after):
        assert save_check.punctuation_hits(before, after) == []

    def test_a_sentence_ending_in_a_short_word_still_owes_its_period(self):
        hits = save_check.punctuation_hits("Volvió a su casa.", "Volvió por fin a su casa")
        assert _rules(hits) == {"closing_mark_dropped"}

    def test_a_lower_case_opening_is_not_a_rule(self):
        assert save_check.punctuation_hits("¿Una vaca?", "¿y las vacas?") == []

    def test_a_disabled_rule_is_silent(self, speller):
        hits = save_check.check_write(
            "", "Llegó a casa tarde.", "Llegó a casa muy tardde",
            vocabulary={}, disabled_rules=["closing_mark_dropped"], speller=speller,
        )
        assert _rules(hits) == {"spelling"}
        hits = save_check.check_write(
            "", "Llegó a casa tarde.", "Llegó a casa muy tardde",
            vocabulary={}, disabled_rules=["spelling"], speller=speller,
        )
        assert _rules(hits) == {"closing_mark_dropped"}


# -------- the book's vocabulary --------

def _write_alignment(project_dir: Path, chapter_id: str, sentences: list[str], en=None):
    rows = [
        {"es_idx": n, "en_idx": n, "es": es, "en": (en or [""] * len(sentences))[n],
         "chunk_id": f"{chapter_id}_chunk_000"}
        for n, es in enumerate(sentences)
    ]
    path = project_dir / "alignments" / f"{chapter_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"chapter_id": chapter_id, "alignments": rows},
                               ensure_ascii=False), encoding="utf-8")
    return path


def _write_chunk(project_dir: Path, chunk_id: str, text: str):
    path = project_dir / "chunks" / f"{chunk_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": chunk_id, "translated_text": text},
                               ensure_ascii=False), encoding="utf-8")


class TestBookVocabulary:
    def test_reads_alignments_and_the_chunks_of_unaligned_chapters(self, tmp_path):
        _write_alignment(tmp_path, "chapter_01", ["Los Mirmidones llegaron.", "[IMAGE:images/a.jpg]"])
        _write_chunk(tmp_path, "chapter_01_chunk_000", "Texto viejo del capítulo uno.")
        _write_chunk(tmp_path, "chapter_02_chunk_000", "El oso pardo durmió.")
        counts = save_check.BookVocabulary().counts(tmp_path)
        assert counts["mirmidones"] == 1
        assert counts["oso"] == 1
        # chapter_01 is aligned, so its chunk is not read a second time.
        assert counts.get("viejo", 0) == 0
        assert counts.get("image", 0) == 0

    def test_a_changed_file_is_re_read_and_a_removed_one_forgotten(self, tmp_path):
        path = _write_alignment(tmp_path, "chapter_01", ["El gato duerme."])
        other = _write_alignment(tmp_path, "chapter_02", ["El perro ladra."])
        vocabulary = save_check.BookVocabulary()
        assert vocabulary.counts(tmp_path)["gato"] == 1

        _write_alignment(tmp_path, "chapter_01", ["El zorro duerme."])
        stamp = path.stat().st_mtime + 5
        os.utime(path, (stamp, stamp))
        other.unlink()
        counts = vocabulary.counts(tmp_path)
        assert counts.get("gato", 0) == 0
        assert counts["zorro"] == 1
        assert counts.get("perro", 0) == 0

    def test_an_empty_project_has_no_words(self, tmp_path):
        assert not save_check.BookVocabulary().counts(tmp_path)


# -------- the warning log --------

class TestOpenWarnings:
    def _warn(self, tmp_path, es_after, hits, es_idx=0, chapter="chapter_01"):
        return save_check.append_warning(
            tmp_path, chapter_id=chapter, es_idx=es_idx, path="reader",
            en="", es_before="", es_after=es_after, hits=hits,
        )

    def test_a_warning_follows_its_sentence_through_a_realign(self, tmp_path):
        wid = self._warn(tmp_path, "La camiometa paró.", [{"rule": "spelling", "text": "camiometa"}])
        rows = [{"es_idx": 0, "es": "Otra frase."}, {"es_idx": 1, "es": "La camiometa paró."}]
        assert save_check.open_warnings(tmp_path, "chapter_01", rows) == [
            {"id": wid, "es_idx": 1, "hits": [{"rule": "spelling", "text": "camiometa"}]},
        ]

    def test_it_closes_when_the_sentence_is_edited(self, tmp_path):
        self._warn(tmp_path, "La camiometa paró.", [{"rule": "spelling", "text": "camiometa"}])
        rows = [{"es_idx": 0, "es": "La camioneta paró."}]
        assert save_check.open_warnings(tmp_path, "chapter_01", rows) == []

    def test_it_closes_when_dismissed(self, tmp_path):
        wid = self._warn(tmp_path, "Vino,, tarde.", [{"rule": "doubled_mark", "text": ",,"}])
        rows = [{"es_idx": 0, "es": "Vino,, tarde."}]
        save_check.append_outcome(tmp_path, wid, "dismissed")
        assert save_check.open_warnings(tmp_path, "chapter_01", rows) == []

    def test_an_ignored_word_drops_out_and_the_rest_stays(self, tmp_path):
        self._warn(tmp_path, "Un frenón,, seco.", [
            {"rule": "spelling", "text": "frenón"}, {"rule": "doubled_mark", "text": ",,"},
        ])
        rows = [{"es_idx": 0, "es": "Un frenón,, seco."}]
        ignored = IgnoredTerms(terms=[IgnoredTerm(term="Frenón", eval_name="dictionary")])
        (warning,) = save_check.open_warnings(tmp_path, "chapter_01", rows, ignored)
        assert warning["hits"] == [{"rule": "doubled_mark", "text": ",,"}]

    def test_another_chapter_s_warning_is_not_shown(self, tmp_path):
        self._warn(tmp_path, "La camiometa paró.", [{"rule": "spelling", "text": "camiometa"}],
                   chapter="chapter_02")
        rows = [{"es_idx": 0, "es": "La camiometa paró."}]
        assert save_check.open_warnings(tmp_path, "chapter_01", rows) == []

    def test_a_replaced_span_split_by_the_realign_keeps_its_flagged_sentence(self, tmp_path):
        wid = self._warn(tmp_path, "Llegó tarde. La camiometa paró.",
                         [{"rule": "spelling", "text": "camiometa"}])
        rows = [{"es_idx": 4, "es": "Llegó tarde."}, {"es_idx": 5, "es": "La camiometa paró."}]
        (warning,) = save_check.open_warnings(tmp_path, "chapter_01", rows)
        assert (warning["id"], warning["es_idx"]) == (wid, 5)

    def test_a_torn_log_line_is_skipped(self, tmp_path):
        self._warn(tmp_path, "Vino,, tarde.", [{"rule": "doubled_mark", "text": ",,"}])
        with open(tmp_path / save_check.LOG_NAME, "a", encoding="utf-8") as f:
            f.write('{"kind": "warn\n')
        warnings, outcomes = save_check.load_log(tmp_path)
        assert len(warnings) == 1 and outcomes == {}


# -------- routes --------

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def project(tmp_path, monkeypatch):
    """One chapter of two aligned sentences, with a fresh vocabulary cache."""
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "test-project"
    _write_alignment(
        proj_dir, "chapter_01",
        ["El gato se sentó.", "El perro ladró."],
        en=["The cat sat.", "The dog barked."],
    )
    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    monkeypatch.setattr(save_check, "book_vocabulary", save_check.BookVocabulary())
    return proj_dir


def _save(client, es_idx, original, corrected, en):
    return client.post("/api/correction", json={
        "project_id": "test-project", "chapter_id": "chapter_01", "es_idx": es_idx,
        "original_es": original, "corrected_es": corrected, "en_reference": en,
    })


def _open(client):
    return client.get("/api/save-checks/test-project/chapter_01").get_json()["warnings"]


class TestSaveRoute:
    def test_a_typo_save_lands_and_comes_back_with_a_warning(self, client, project):
        rv = _save(client, 0, "El gato se sentó.", "El gatto se sentó.", "The cat sat.")
        body = rv.get_json()
        assert body["saved"] is True
        assert body["check"]["hits"] == [{"rule": "spelling", "text": "gatto"}]

        (row,) = [json.loads(line) for line in
                  (project / "save_checks.jsonl").read_text(encoding="utf-8").splitlines()]
        assert row["kind"] == "warning" and row["id"] == body["check"]["id"]
        assert row["es_before"] == "El gato se sentó." and row["path"] == "reader"
        assert _open(client) == [{"id": row["id"], "es_idx": 0, "hits": row["hits"]}]

    def test_a_clean_save_has_no_warning_and_writes_no_log(self, client, project):
        body = _save(client, 0, "El gato se sentó.", "El gato se acostó.", "The cat sat.").get_json()
        assert body["saved"] is True and "check" not in body
        assert not (project / "save_checks.jsonl").exists()

    def test_a_check_that_raises_does_not_cost_the_save(self, client, project, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("dictionary on fire")
        monkeypatch.setattr(save_check, "check_write", boom)
        body = _save(client, 0, "El gato se sentó.", "El gatto se sentó.", "The cat sat.").get_json()
        assert body["saved"] is True and "check" not in body
        assert "El gatto se sentó." in (project / "corrections.jsonl").read_text(encoding="utf-8")

    def test_the_check_can_be_switched_off(self, client, project, monkeypatch):
        import src.app_config as app_config
        monkeypatch.setattr(app_config, "get_save_check_config", lambda: {"enabled": False})
        body = _save(client, 0, "El gato se sentó.", "El gatto se sentó.", "The cat sat.").get_json()
        assert body["saved"] is True and "check" not in body

    def test_one_rule_can_be_switched_off(self, client, project, monkeypatch):
        import src.app_config as app_config
        monkeypatch.setattr(app_config, "get_save_check_config",
                            lambda: {"disabled_rules": ["spelling"]})
        body = _save(client, 0, "El gato se sentó.", "El gatto se sentó", "The cat sat.").get_json()
        assert [h["rule"] for h in body["check"]["hits"]] == ["closing_mark_dropped"]

    def test_fixing_the_sentence_closes_the_warning(self, client, project):
        _save(client, 0, "El gato se sentó.", "El gatto se sentó.", "The cat sat.")
        assert len(_open(client)) == 1
        body = _save(client, 0, "El gatto se sentó.", "El gato se sentó.", "The cat sat.").get_json()
        assert "check" not in body
        assert _open(client) == []

    def test_a_pending_save_teaches_the_vocabulary(self, client, project):
        # The first Save of a coined word warns; once it stands in the reading
        # text, typing it again in another sentence does not.
        first = _save(client, 0, "El gato se sentó.", "El gatto se sentó.", "The cat sat.").get_json()
        assert "check" in first
        second = _save(client, 1, "El perro ladró.", "El gatto ladró.", "The dog barked.").get_json()
        assert "check" not in second


class TestDismissRoute:
    def _warned(self, client):
        return _save(client, 0, "El gato se sentó.", "El gatto se sentó.",
                     "The cat sat.").get_json()["check"]["id"]

    def test_dismiss_closes_the_warning(self, client, project):
        wid = self._warned(client)
        rv = client.post("/api/save-check/dismiss", json={
            "project_id": "test-project", "id": wid, "action": "dismiss"})
        assert rv.status_code == 200
        assert _open(client) == []
        _, outcomes = save_check.load_log(project)
        assert outcomes[wid]["outcome"] == "dismissed"

    def test_ignore_term_teaches_the_book(self, client, project):
        wid = self._warned(client)
        rv = client.post("/api/save-check/dismiss", json={
            "project_id": "test-project", "id": wid, "action": "ignore_term", "term": "gatto"})
        assert rv.status_code == 200
        entries = json.loads((project / "ignored_terms.json").read_text(encoding="utf-8"))["terms"]
        assert [(e["term"], e["eval_name"], e["added_from"]) for e in entries] == [
            ("gatto", "dictionary", "save-check")]
        assert _open(client) == []
        _, outcomes = save_check.load_log(project)
        assert outcomes[wid]["outcome"] == "ignored"

        # Put the first sentence back, so the word is no longer in the book and
        # only the ignore list can be letting it through.
        _save(client, 0, "El gatto se sentó.", "El gato se sentó.", "The cat sat.")
        again = _save(client, 1, "El perro ladró.", "El gatto ladró.", "The dog barked.").get_json()
        assert "check" not in again

    def test_ignore_term_must_name_a_word_the_warning_flagged(self, client, project):
        wid = self._warned(client)
        rv = client.post("/api/save-check/dismiss", json={
            "project_id": "test-project", "id": wid, "action": "ignore_term", "term": "gato"})
        assert rv.status_code == 400
        assert not (project / "ignored_terms.json").exists()

    def test_an_unknown_warning_is_a_404(self, client, project):
        rv = client.post("/api/save-check/dismiss", json={
            "project_id": "test-project", "id": "nope", "action": "dismiss"})
        assert rv.status_code == 404


class TestReplaceRoute:
    def test_the_replace_response_carries_the_warning(self, client, project, monkeypatch):
        import web_ui.app as app_module
        from src.models import Chunk, ChunkMetadata, ChunkStatus
        from src.utils.file_io import save_chunk

        text = "El gato se sentó. El perro ladró."
        source = "The cat sat. The dog barked."
        (project / "chunks").mkdir()
        save_chunk(Chunk(
            id="chapter_01_chunk_000", chapter_id="chapter_01", position=0,
            source_text=source, translated_text=text,
            metadata=ChunkMetadata(char_start=0, char_end=len(source), overlap_start=0,
                                   overlap_end=0, paragraph_count=1, word_count=6),
            status=ChunkStatus.TRANSLATED,
        ), project / "chunks" / "chapter_01_chunk_000.json")

        def fake_apply(project_dir, project_id, chapter_id, edits):
            # Stands in for save + recombine + realign: the new text is in the
            # alignment by the time the warning is logged.
            _write_alignment(project_dir, chapter_id, ["El gatto se sentó.", "El perro ladró."])
            return {"mtimes": {}, "orphaned_annotations": [], "corrections_purged": 0}
        monkeypatch.setattr(app_module, "_apply_chunk_edits", fake_apply)

        rv = client.post("/api/sentence/replace", json={
            "project_id": "test-project", "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000", "es_idx": 0,
            "current_translation": "El gato se sentó.",
            "new_translation": "El gatto se sentó.",
        })
        body = rv.get_json()
        assert rv.status_code == 200, body
        assert body["check"]["hits"] == [{"rule": "spelling", "text": "gatto"}]
        (row,) = save_check.load_log(project)[0]
        assert row["path"] == "replace" and row["en"] == "The cat sat."
        assert [w["es_idx"] for w in _open(client)] == [0]
