"""Figures added to a book after its text (src/figures.py).

A picture and its captions must land after the same paragraph on both sides,
even where the translation splits that paragraph in two; a batch that cannot
be placed writes nothing; and a remove gives back the text as it was.
Realignment (BERT) and the coded evaluators are stubbed: this is the wiring.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import figures
from src.models import Chunk, ChunkMetadata, ChunkStatus
from src.utils.file_io import load_chunk, save_chunk

C1, C2 = "chapter_01", "chapter_02"
K1, K2 = f"{C1}_chunk_000", f"{C2}_chunk_000"

EN1 = ("CHAPTER I. Peter Goes Visiting.\n\n"
       "Peter hopped along the lane. He met Bully the English Sparrow.\n\n"
       "Then he went home.")
ES1 = ("CAPÍTULO I. Pedrito de visita.\n\n"
       "Pedrito iba saltando por el camino. Se encontró con Bravucón el Gorrión.\n\n"
       "Luego se fue a casa.")
EN2 = ("CHAPTER II. Jenny Wren Arrives.\n\n"
       "\"Hello, Jenny Wren!\" cried Peter. \"When did you come?\"\n\n"
       "Jenny Wren was too busy to answer. Jenny Wren never stops.")
# The translation splits the dialogue paragraph in two, as the dialogue rules do.
ES2 = ("CAPÍTULO II. Llega Lupita Saltapared.\n\n"
       "—¡Hola, Lupita Saltapared! —exclamó Pedrito.\n\n"
       "—¿Cuándo llegaste?\n\n"
       "Lupita Saltapared estaba demasiado ocupada para contestar. Nunca se detiene.")
ROWS = {
    C1: [(0, "CHAPTER I. Peter Goes Visiting.", "CAPÍTULO I. Pedrito de visita."),
         (1, "Peter hopped along the lane.", "Pedrito iba saltando por el camino."),
         (2, "He met Bully the English Sparrow.", "Se encontró con Bravucón el Gorrión."),
         (3, "Then he went home.", "Luego se fue a casa.")],
    C2: [(0, "CHAPTER II. Jenny Wren Arrives.", "CAPÍTULO II. Llega Lupita Saltapared."),
         (1, "\"Hello, Jenny Wren!\" cried Peter.", "—¡Hola, Lupita Saltapared! —exclamó Pedrito."),
         (2, "\"When did you come?\"", "—¿Cuándo llegaste?"),
         (3, "Jenny Wren was too busy to answer.",
          "Lupita Saltapared estaba demasiado ocupada para contestar."),
         (4, "Jenny Wren never stops.", "Nunca se detiene.")],
}


def _chunk(cid: str, chapter: str, en: str, es: str) -> Chunk:
    return Chunk(id=cid, chapter_id=chapter, position=0, source_text=en, translated_text=es,
                 metadata=ChunkMetadata(char_start=0, char_end=len(en), overlap_start=0,
                                        overlap_end=0, paragraph_count=3, word_count=20),
                 status=ChunkStatus.TRANSLATED)


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "demo"
    (proj / "chunks").mkdir(parents=True)
    (proj / "alignments").mkdir()
    for cid, ch, en, es in ((K1, C1, EN1, ES1), (K2, C2, EN2, ES2)):
        save_chunk(_chunk(cid, ch, en, es), proj / "chunks" / f"{cid}.json")
        rows = [{"es_idx": i, "en": e, "es": s, "chunk_id": cid} for i, e, s in ROWS[ch]]
        (proj / "alignments" / f"{ch}.json").write_text(
            json.dumps({"alignments": rows}, ensure_ascii=False), encoding="utf-8")
    (proj / "source.txt").write_text(EN1 + "\n\n" + EN2 + "\n", encoding="utf-8")
    plates = tmp_path / "ocr" / "out"
    (plates / "plates").mkdir(parents=True)
    (plates / "plates" / "leaf035_1.jpg").write_bytes(b"\xff\xd8jpeg")
    (plates / "plates.json").write_text(json.dumps({"plates": [{
        "name": "leaf035_1", "image": "plates/leaf035_1.jpg", "leaf": 35,
        "captions": [{"text": "JENNY WREN. This is the saucy little House Wren.",
                      "doubtful": False}],
    }]}), encoding="utf-8")
    return proj


@pytest.fixture(autouse=True)
def realign_calls(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("src.corrections_apply.realign_chapter",
                        lambda project_dir, chapter_id, *a, **k: calls.append(chapter_id))
    monkeypatch.setattr("web_ui.evaluations.evaluate_and_persist_chunk",
                        lambda project_dir, chunk, **kw: {})
    return calls


def _import(project):
    return figures.import_figures(project, project.parent / "ocr" / "out" / "plates.json")


def _translate(project, **extra):
    figures.set_fields(project, [{"id": "leaf035_1", "alt_es": "Lupita Saltapared",
                                  "captions_es": ["LUPITA SALTAPARED. Es el reyezuelo."],
                                  **extra}])


@pytest.mark.parametrize("caption,terms", [
    ("BULLY THE ENGLISH SPARROW, the common sparrow.", ("Bully", "English Sparrow")),
    ("JENNY WREN. This is the saucy little House Wren.", ("Jenny Wren", None)),
    ("THE MALE COWBIRD. You may know him.", (None, "Male Cowbird")),
    ("KING EAGLE, the bald or whiteheaded Eagle.", ("King Eagle", None)),
    ("He is dressed in black.", (None, None)),
])
def test_a_caption_names_its_subject(caption, terms):
    assert figures.bird_terms(caption) == terms


def test_import_places_a_figure_in_the_chapter_its_heading_names(project):
    out = _import(project)
    assert out["added"] == ["leaf035_1"] and not out["unplaced"]
    fig = figures.load_plan(project)["figures"][0]
    # Chapter I never names Jenny Wren; Chapter II's heading does.
    assert fig["chapter_id"] == C2
    assert fig["anchor_en"].startswith('"Hello, Jenny Wren!"')
    assert fig["status"] == "draft" and fig["image"] == "images/plate_leaf035_1.jpg"


def test_a_figure_cannot_be_approved_before_its_spanish_caption(project):
    _import(project)
    out = figures.set_fields(project, [{"id": "leaf035_1", "status": "approved"}])
    assert out["refused"] and not out["changed"]


def test_the_spanish_side_lands_after_the_whole_split_paragraph(project):
    _import(project)
    _translate(project)
    p = figures.plan_insert(project, figures.load_plan(project)["figures"][0])
    assert p["ok"], p
    # The English paragraph is one; its Spanish is two. The figure goes after both.
    assert p["es_context"] == "—¿Cuándo llegaste?"
    assert p["en_insert"].startswith("[IMAGE:images/plate_leaf035_1.jpg:Jenny Wren]\n\n[CAPTION] JENNY")


def test_apply_inserts_on_both_sides_and_in_the_source(project, realign_calls):
    _import(project)
    _translate(project, status="approved")
    out = figures.apply(project)
    assert out["status"] == "ok" and out["inserted"] == ["leaf035_1"], out
    chunk = load_chunk(project / "chunks" / f"{K2}.json")
    block_en = ("[IMAGE:images/plate_leaf035_1.jpg:Jenny Wren]\n\n"
                "[CAPTION] JENNY WREN. This is the saucy little House Wren.")
    block_es = ("[IMAGE:images/plate_leaf035_1.jpg:Lupita Saltapared]\n\n"
                "[CAPTION] LUPITA SALTAPARED. Es el reyezuelo.")
    assert f'come?"\n\n{block_en}\n\nJenny Wren was too busy' in chunk.source_text
    assert f"—¿Cuándo llegaste?\n\n{block_es}\n\nLupita Saltapared estaba" in chunk.translated_text
    assert block_en in (project / "source.txt").read_text(encoding="utf-8")
    assert (project / "images" / "plate_leaf035_1.jpg").read_bytes() == b"\xff\xd8jpeg"
    assert list((project / ".chunk_edits" / C2 / K2).glob("*.json"))
    assert realign_calls == [C2]
    assert figures.load_plan(project)["figures"][0]["status"] == "inserted"
    # Again: nothing approved is left, and the inserted one is not inserted twice.
    assert figures.apply(project)["planned"] == []


def test_a_figure_that_cannot_be_placed_stops_the_batch(project):
    _import(project)
    _translate(project, status="approved")
    figures.set_fields(project, [{"id": "leaf035_1", "anchor_en": "No paragraph opens so"}])
    out = figures.apply(project)
    assert out["status"] == "error" and out["refused"][0]["id"] == "leaf035_1"
    assert load_chunk(project / "chunks" / f"{K2}.json").translated_text == ES2
    assert not (project / "images").exists()


def test_a_dry_run_writes_nothing(project):
    _import(project)
    _translate(project, status="approved")
    out = figures.apply(project, dry_run=True)
    assert out["planned"][0]["chunk_id"] == K2
    assert load_chunk(project / "chunks" / f"{K2}.json").source_text == EN2


def test_remove_gives_the_text_back(project):
    _import(project)
    _translate(project, status="approved")
    figures.apply(project)
    out = figures.remove(project, ["leaf035_1"])
    assert out["removed"] == ["leaf035_1"]
    chunk = load_chunk(project / "chunks" / f"{K2}.json")
    assert chunk.source_text == EN2 and chunk.translated_text == ES2
    assert (project / "source.txt").read_text(encoding="utf-8") == EN1 + "\n\n" + EN2 + "\n"
    assert not (project / "images" / "plate_leaf035_1.jpg").exists()
    assert figures.load_plan(project)["figures"][0]["status"] == "draft"


def test_a_reimport_keeps_the_translation_and_place(project):
    _import(project)
    _translate(project)
    figures.set_fields(project, [{"id": "leaf035_1", "anchor_en": "Jenny Wren was too busy"}])
    out = _import(project)
    assert out["refreshed"] == ["leaf035_1"] and not out["added"]
    fig = figures.load_plan(project)["figures"][0]
    assert fig["captions_es"] == ["LUPITA SALTAPARED. Es el reyezuelo."]
    assert fig["anchor_en"] == "Jenny Wren was too busy"
