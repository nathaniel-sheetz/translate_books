"""The harness's hand-edit verbs: ``sentence-edit`` and ``chunk-edit``.

They must land an edit the way the web UI does (exact span, audit row,
snapshot, stale stamp, one realign per chapter) without the web server, and
never write anything when a batch cannot be placed. Realignment (BERT) is
stubbed: this is about the wiring, not the aligner.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.harness import flow, text_edit
from src.models import Chunk, ChunkMetadata, ChunkStatus
from src.utils.file_io import load_chunk, save_chunk
from web_ui.evaluations import (
    load_chunk_evaluation,
    load_feedback_for_chunk,
    merge_judge_result,
)

CH = "chapter_01"
C0 = f"{CH}_chunk_000"
C1 = f"{CH}_chunk_001"
TEXT0 = "Hola, dijo él.\n\nEl servicio real fue bueno. Era un día claro."
TEXT1 = "Otra frase aquí. Y la viga transversal cayó."
ROWS = [  # (es_idx, chunk, es, en)
    (0, C0, "Hola, dijo él.", "Hello, he said."),
    (1, C0, "El servicio real fue bueno.", "The actual service was good."),
    (2, C0, "Era un día claro.", "It was a clear day."),
    (3, C1, "Otra frase aquí.", "Another sentence here."),
    (4, C1, "Y la viga transversal cayó.", "And the cross-beam fell."),
]


def _chunk(chunk_id: str, position: int, text: str, *, overlap_start=0, overlap_end=0) -> Chunk:
    return Chunk(
        id=chunk_id, chapter_id=CH, position=position,
        source_text="source " * 5, translated_text=text,
        metadata=ChunkMetadata(char_start=0, char_end=10, overlap_start=overlap_start,
                               overlap_end=overlap_end, paragraph_count=1, word_count=5),
        status=ChunkStatus.TRANSLATED,
    )


def _write_alignment(proj: Path) -> None:
    texts = {C0: TEXT0, C1: TEXT1}
    rows = []
    for es_idx, chunk_id, es, en in ROWS:
        start = texts[chunk_id].index(es)
        rows.append({"es_idx": es_idx, "es": es, "en": en, "chunk_id": chunk_id,
                     "chunk_offset_start": start, "chunk_offset_end": start + len(es)})
    (proj / "alignments").mkdir(exist_ok=True)
    (proj / "alignments" / f"{CH}.json").write_text(
        json.dumps({"alignments": rows}, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "demo"
    (proj / "chunks").mkdir(parents=True)
    save_chunk(_chunk(C0, 0, TEXT0), proj / "chunks" / f"{C0}.json")
    save_chunk(_chunk(C1, 1, TEXT1), proj / "chunks" / f"{C1}.json")
    _write_alignment(proj)
    merge_judge_result(proj, C0, "editorial", {
        "eval_name": "editorial",
        "issues": [{"severity": "warning", "message": "false friend",
                    "location": "servicio real", "suggestion": "servicio efectivo"}],
    })
    return proj


@pytest.fixture(autouse=True)
def realign_calls(monkeypatch):
    calls: list[str] = []

    def _fake(project_dir, chapter_id, *args, **kwargs):
        calls.append(chapter_id)

    monkeypatch.setattr("src.corrections_apply.realign_chapter", _fake)
    return calls


@pytest.fixture(autouse=True)
def evaluator_calls(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("web_ui.evaluations.evaluate_and_persist_chunk",
                        lambda project_dir, chunk, **kw: calls.append(chunk.id) or {})
    return calls


@pytest.fixture(autouse=True)
def save_check_rules_only(monkeypatch):
    """The punctuation rules only: the speller depends on what is installed."""
    monkeypatch.setattr(text_edit, "_save_check_settings",
                        lambda: (True, frozenset({"spelling", "house_rule"})))


def _text(proj: Path, chunk_id: str) -> str:
    return load_chunk(proj / "chunks" / f"{chunk_id}.json").translated_text


def _archive(proj: Path) -> list[dict]:
    path = proj / "corrections_applied.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _queue(proj: Path, *rows: dict) -> None:
    with open(proj / "corrections.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ── sentence-edit ────────────────────────────────────────────────────────────


def test_a_find_lands_like_a_reader_save(project, realign_calls):
    out = text_edit.sentence_edit(project, [{
        "chapter": CH, "find": "servicio real", "replace": "servicio efectivo",
        "finding": {"eval_name": "editorial", "chunk_id": C0, "issue_index": 0},
        "reason": "false friend",
    }])
    assert out["status"] == "ok", out
    assert _text(project, C0) == TEXT0.replace("servicio real", "servicio efectivo")
    assert _text(project, C1) == TEXT1

    [row] = _archive(project)
    start = TEXT0.index("El servicio real fue bueno.")
    assert row["source"] == "harness:chat"
    assert row["status"] == "applied"
    assert row["es_idx"] == 1 and row["chapter_id"] == CH and row["chunk_id"] == C0
    assert row["original_es"] == "El servicio real fue bueno."
    assert row["corrected_es"] == "El servicio efectivo fue bueno."
    assert row["en_reference"] == "The actual service was good."
    assert (row["chunk_offset_start"], row["chunk_offset_end"]) == (start, start + 27)
    assert row["from_finding"]["eval_name"] == "editorial"
    assert row["reason"] == "false friend"
    assert "verified_by" not in row  # not a reader-confirmed write

    assert list((project / ".chunk_edits" / CH / C0).glob("*.json"))
    assert load_chunk_evaluation(project, C0)["stale"] is True
    marks = load_feedback_for_chunk(project, C0)
    assert [m["feedback_type"] for m in marks] == ["applied"]
    assert out["findings_marked"] == 1
    assert realign_calls == [CH]
    assert (project / "chapters" / f"{CH}.txt").exists()
    assert out["reader_links"] == [f"http://localhost:5000/read/demo/{CH}"]


def test_the_authorship_facts_count_it_as_a_model_fix(project):
    from src.kdp.facts import _authorship

    text_edit.sentence_edit(project, [{"chapter": CH, "find": "claro", "replace": "despejado"}])
    facts = _authorship(project)
    assert facts["model_fixes"] == 1
    assert facts["reader_saves_typed"] == 0
    assert facts["reader_saves_from_finding"] == 0


def test_a_dry_run_writes_nothing(project, realign_calls):
    out = text_edit.sentence_edit(
        project, [{"chapter": CH, "find": "claro", "replace": "despejado"}], dry_run=True)
    assert out["status"] == "ok" and out["write"] is False
    assert out["planned"][0]["before"] == "Era un día claro."
    assert out["planned"][0]["after"] == "Era un día despejado."
    assert out["planned"][0]["en"] == "It was a clear day."
    assert _text(project, C0) == TEXT0
    assert not _archive(project)
    assert not (project / ".chunk_edits").exists()
    assert realign_calls == []


def test_the_es_idx_form_rewrites_the_whole_sentence(project):
    out = text_edit.sentence_edit(project, [{"chapter": CH, "es_idx": 3, "new": "Otra oración."}])
    assert out["status"] == "ok", out
    assert _text(project, C1) == "Otra oración. Y la viga transversal cayó."


def test_es_idx_scopes_a_find(project):
    out = text_edit.sentence_edit(project, [{"chapter": CH, "es_idx": 4, "find": "la",
                                             "replace": "el"}])
    assert out["status"] == "ok", out
    assert _text(project, C1) == "Otra frase aquí. Y el viga transversal cayó."


@pytest.mark.parametrize("edit, needle", [
    ({"chapter": CH, "find": "inexistente", "replace": "x"}, "not in any sentence"),
    ({"chapter": CH, "find": "bueno. Era", "replace": "x"}, "chunk-edit"),
    ({"chapter": CH, "find": "a", "replace": "x"}, "more than one sentence"),
    ({"chapter": CH, "es_idx": 99, "new": "x"}, "no sentence es_idx 99"),
    ({"chapter": "chapter_09", "find": "a", "replace": "b"}, "no alignment"),
    ({"chapter": CH, "find": "claro", "replace": "claro"}, "changes nothing"),
    ({"chapter": CH, "find": "x", "new": "y", "es_idx": 1}, "either"),
    ({"chapter": CH, "find": "claro", "replace": "x", "finding": {"eval_name": "e"}},
     "finding needs"),
])
def test_an_edit_that_cannot_be_placed_is_refused(project, edit, needle):
    out = text_edit.sentence_edit(project, [edit])
    assert out["status"] == "error" and out["write"] is False
    assert needle in out["refused"][0]["error"]


def test_one_bad_edit_stops_the_whole_batch(project, realign_calls):
    out = text_edit.sentence_edit(project, [
        {"chapter": CH, "find": "claro", "replace": "despejado"},
        {"chapter": CH, "find": "inexistente", "replace": "x"},
    ])
    assert out["status"] == "error"
    assert [r["n"] for r in out["refused"]] == [1]
    assert _text(project, C0) == TEXT0
    assert not _archive(project) and realign_calls == []


def test_ambiguous_find_lists_its_candidates(project):
    out = text_edit.sentence_edit(project, [{"chapter": CH, "find": "Era", "replace": "Fue"},
                                            {"chapter": CH, "find": "e", "replace": "E"}])
    refused = out["refused"]
    assert [r["n"] for r in refused] == [1]
    assert len(refused[0]["candidates"]) > 1


def test_two_edits_of_one_sentence_fold_into_one_row(project):
    out = text_edit.sentence_edit(project, [
        {"chapter": CH, "find": "servicio real", "replace": "servicio efectivo"},
        {"chapter": CH, "find": "fue bueno", "replace": "fue excelente"},
    ])
    assert out["status"] == "ok", out
    assert "El servicio efectivo fue excelente." in _text(project, C0)
    [row] = _archive(project)
    assert row["original_es"] == "El servicio real fue bueno."
    assert row["corrected_es"] == "El servicio efectivo fue excelente."


def test_edits_across_chunks_realign_the_chapter_once(project, realign_calls):
    out = text_edit.sentence_edit(project, [
        {"chapter": CH, "find": "claro", "replace": "despejado"},
        {"chapter": CH, "find": "viga transversal", "replace": "travesaño"},
    ])
    assert out["status"] == "ok", out
    assert "travesaño" in _text(project, C1)
    assert len(out["backups"]) == 2
    assert realign_calls == [CH]


def test_a_save_check_hit_is_logged_for_the_reader(project):
    out = text_edit.sentence_edit(project, [{"chapter": CH, "find": "un día",
                                             "replace": "un un día"}])
    assert out["status"] == "ok", out
    assert out["applied"][0]["save_check"][0]["rule"] == "repeated_word"
    assert out["save_check_warnings"][0]["es_idx"] == 2
    log = [json.loads(line) for line in
           (project / "save_checks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert log[0]["path"] == "harness"


PENDING = {"project_id": "demo", "chapter_id": CH, "chunk_id": C1, "es_idx": 3,
           "original_es": "Otra frase aquí.", "corrected_es": "Otra frase allí.",
           "en_reference": "Another sentence here.", "timestamp": "2026-11-21T10:00:00"}


def test_queued_reader_saves_block_the_edit(project, realign_calls):
    _queue(project, PENDING)
    out = text_edit.sentence_edit(project, [{"chapter": CH, "find": "claro",
                                             "replace": "despejado"}])
    assert out["status"] == "error"
    assert out["pending"][0]["es_idx"] == 3
    assert _text(project, C0) == TEXT0 and realign_calls == []


def test_include_pending_applies_and_drains_the_queue(project):
    other = {**PENDING, "chapter_id": "chapter_02", "chunk_id": "chapter_02_chunk_000"}
    _queue(project, PENDING, other)
    out = text_edit.sentence_edit(project, [{"chapter": CH, "find": "claro",
                                             "replace": "despejado"}], include_pending=True)
    assert out["status"] == "ok", out
    assert _text(project, C1) == "Otra frase allí. Y la viga transversal cayó."
    assert "despejado" in _text(project, C0)
    archived = _archive(project)
    assert {r.get("source") for r in archived} == {None, "harness:chat"}
    remaining = (project / "corrections.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(r)["chapter_id"] for r in remaining] == ["chapter_02"]


def test_an_edit_on_a_sentence_with_a_queued_save_reads_the_saved_text(project):
    # The reader patched the alignment in place when it queued the Save.
    _queue(project, PENDING)
    align = json.loads((project / "alignments" / f"{CH}.json").read_text(encoding="utf-8"))
    align["alignments"][3]["es"] = "Otra frase allí."
    (project / "alignments" / f"{CH}.json").write_text(json.dumps(align), encoding="utf-8")
    out = text_edit.sentence_edit(project, [{"chapter": CH, "find": "allí", "replace": "acá"}],
                                  include_pending=True)
    assert out["status"] == "ok", out
    assert _text(project, C1).startswith("Otra frase acá.")


DIALOGUE = "—Hola —dijo él.\n—¿Qué tal?\n\nEra un día claro."


def _merged_dialogue(proj: Path) -> None:
    """A merged alignment row whose two sentences the chunk splits with a newline."""
    save_chunk(_chunk(C0, 0, DIALOGUE), proj / "chunks" / f"{C0}.json")
    align_path = proj / "alignments" / f"{CH}.json"
    align = json.loads(align_path.read_text(encoding="utf-8"))
    start = DIALOGUE.index("Era")
    align["alignments"][:3] = [
        {"es_idx": 0, "es": "—Hola —dijo él. —¿Qué tal?", "en": "Hi, he said. How are you?",
         "chunk_id": C0},
        {"es_idx": 1, "es": "Era un día claro.", "en": "It was a clear day.", "chunk_id": C0,
         "chunk_offset_start": start, "chunk_offset_end": start + 17},
    ]
    align_path.write_text(json.dumps(align, ensure_ascii=False), encoding="utf-8")
    _age_chunks(proj)


def _age_chunks(proj: Path) -> None:
    """Put every chunk's mtime before the alignment's, as a fresh realign leaves them."""
    aligned_at = (proj / "alignments" / f"{CH}.json").stat().st_mtime
    for p in (proj / "chunks").glob("*.json"):
        os.utime(p, (aligned_at - 10, aligned_at - 10))


def test_a_find_on_a_merged_dialogue_row_keeps_the_line_break(project):
    _merged_dialogue(project)
    out = text_edit.sentence_edit(project, [{"chapter": CH, "find": "dijo él",
                                             "replace": "dijo"}])
    assert out["status"] == "ok", out
    assert _text(project, C0) == "—Hola —dijo.\n—¿Qué tal?\n\nEra un día claro."
    [row] = _archive(project)
    assert row["original_es"] == "—Hola —dijo él. —¿Qué tal?"
    assert row["corrected_es"] == "—Hola —dijo. —¿Qué tal?"


def test_a_whole_row_rewrite_on_a_merged_row_still_restores_the_break(project):
    _merged_dialogue(project)
    out = text_edit.sentence_edit(project, [{"chapter": CH, "es_idx": 0,
                                             "new": "—Hola —dijo ella. —¿Qué tal?"}])
    assert out["status"] == "ok", out
    assert _text(project, C0) == "—Hola —dijo ella.\n—¿Qué tal?\n\nEra un día claro."


def test_a_stale_alignment_refuses_the_batch(project, realign_calls):
    aligned_at = (project / "alignments" / f"{CH}.json").stat().st_mtime
    os.utime(project / "chunks" / f"{C0}.json", (aligned_at + 10, aligned_at + 10))
    out = text_edit.sentence_edit(project, [{"chapter": CH, "find": "claro",
                                             "replace": "despejado"}])
    assert out["status"] == "error" and out["write"] is False
    assert "older than its chunks" in out["refused"][0]["error"]
    assert _text(project, C0) == TEXT0 and realign_calls == []


@pytest.mark.parametrize("expect, ok", [
    ("Otra frase", True),
    ("la viga", False),  # es_idx 4's text: the numbering moved
    ("", False),
])
def test_expect_guards_a_sentence_named_by_es_idx(project, expect, ok):
    out = text_edit.sentence_edit(project, [{"chapter": CH, "es_idx": 3, "expect": expect,
                                             "new": "Otra oración."}])
    if ok:
        assert out["status"] == "ok", out
        assert _text(project, C1).startswith("Otra oración.")
    else:
        assert out["status"] == "error" and "expect" in out["refused"][0]["error"]
        assert _text(project, C1) == TEXT1


def test_an_es_idx_scoped_miss_is_not_called_a_cross_sentence_find(project):
    out = text_edit.sentence_edit(project, [{"chapter": CH, "es_idx": 4, "find": "claro",
                                             "replace": "despejado"}])
    error = out["refused"][0]["error"]
    assert "not in any sentence" in error and "es_idx 4" in error


def test_chunk_edit_refuses_a_drive_relative_chunk_id(project):
    out = text_edit.chunk_edit(project, "D:x_chunk_000", [{"find": "a", "replace": "b"}])
    assert out["status"] == "error" and "must look like" in out["error"]


def test_a_queued_save_with_an_unsafe_chunk_id_stays_queued(project):
    bad = {**PENDING, "chunk_id": "D:x_chunk_000"}
    _queue(project, bad)
    out = text_edit.sentence_edit(project, [{"chapter": CH, "find": "claro",
                                             "replace": "despejado"}], include_pending=True)
    assert out["status"] == "ok", out
    assert "despejado" in _text(project, C0)
    assert [r["chunk_id"] for r in out["pending_unplaced"]] == ["D:x_chunk_000"]
    queued = (project / "corrections.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(r)["chunk_id"] for r in queued] == ["D:x_chunk_000"]


def test_the_flow_reads_a_file_and_inline_flags(project, tmp_path):
    edits = tmp_path / "edits.json"
    edits.write_text(json.dumps([{"chapter": CH, "find": "claro", "replace": "despejado"}],
                                ensure_ascii=False), encoding="utf-8")
    out = flow.sentence_edit(str(project), file=str(edits), dry_run=True)
    assert out["planned"][0]["after"] == "Era un día despejado."
    out = flow.sentence_edit(str(project), chapter=CH, es_idx=0, new="Hola, dijo ella.")
    assert out["status"] == "ok", out
    assert _text(project, C0).startswith("Hola, dijo ella.")
    assert flow.sentence_edit(str(project))["status"] == "error"


# ── chunk-edit ───────────────────────────────────────────────────────────────


def test_a_chunk_edit_inserts_a_heading(project, realign_calls, evaluator_calls):
    out = text_edit.chunk_edit(project, C0, [{
        "find": "Hola, dijo él.\n\n", "replace": "Hola, dijo él.\n\nEL SERVICIO\n\n",
    }], reason="dropped subheading")
    assert out["status"] == "ok", out
    assert "\n\nEL SERVICIO\n\n" in _text(project, C0)
    [row] = _archive(project)
    assert row["source"] == "harness:chat" and row["kind"] == "chunk" and row["es_idx"] is None
    assert row["reason"] == "dropped subheading"
    assert realign_calls == [CH] and evaluator_calls == [C0]
    assert load_chunk_evaluation(project, C0)["stale"] is True
    assert Path(out["backup"]).exists()


def test_a_chunk_edit_dry_run_writes_nothing(project, realign_calls):
    out = text_edit.chunk_edit(project, C0, [{"find": "claro", "replace": "despejado"}],
                               dry_run=True)
    assert out["status"] == "ok" and out["write"] is False
    assert _text(project, C0) == TEXT0 and not _archive(project) and realign_calls == []


def test_chunk_edit_refuses_a_find_that_is_not_unique(project):
    out = text_edit.chunk_edit(project, C0, [{"find": "a", "replace": "b"}])
    assert out["status"] == "error" and "exactly once" in out["error"]


def test_chunk_edit_keeps_image_tokens(project):
    save_chunk(_chunk(C0, 0, "Antes [IMAGE:fig1.png] después."), project / "chunks" / f"{C0}.json")
    out = text_edit.chunk_edit(project, C0, [{"find": " [IMAGE:fig1.png]", "replace": ""}])
    assert out["status"] == "error" and "IMAGE" in out["error"]


def test_chunk_edit_keeps_the_overlap(project):
    save_chunk(_chunk(C0, 0, TEXT0, overlap_start=5), project / "chunks" / f"{C0}.json")
    out = text_edit.chunk_edit(project, C0, [{"find": "Hola", "replace": "Adiós"}])
    assert out["status"] == "error" and "overlap" in out["error"]


def test_chunk_edit_refuses_queued_saves_unless_purged(project):
    _queue(project, PENDING)
    out = text_edit.chunk_edit(project, C1, [{"find": "viga transversal",
                                              "replace": "travesaño"}])
    assert out["status"] == "error" and out["pending"]
    out = text_edit.chunk_edit(project, C1, [{"find": "viga transversal",
                                              "replace": "travesaño"}], purge_pending=True)
    assert out["status"] == "ok", out
    assert out["corrections_purged"] == 1
    assert not (project / "corrections.jsonl").exists()


def test_chunk_edit_refuses_queued_saves_elsewhere_in_the_chapter(project):
    _queue(project, PENDING)
    out = text_edit.chunk_edit(project, C0, [{"find": "claro", "replace": "despejado"}],
                               purge_pending=True)
    assert out["status"] == "error" and "elsewhere" in out["error"]
