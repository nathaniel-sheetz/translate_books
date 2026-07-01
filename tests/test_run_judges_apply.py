"""Integration coverage for `run_judges.py apply` — judge-review's fix path.

Exercises the plan-first flow end to end on a tiny throwaway project: preview
(nothing changes), select-and-apply (edit + backup + recombine/realign + archive
+ stale-guard), the reader-queue is left alone, and re-judging clears the stale
marker. Realignment (BERT) is stubbed — this test is about the wiring, not the
aligner.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import run_judges
from src.models import Chunk, ChunkMetadata, ChunkStatus
from src.utils.file_io import load_chunk, save_chunk
from web_ui.evaluations import load_chunk_evaluation, merge_judge_result

CHUNK_ID = "chapter_01_chunk_000"
# One clean swap (— Hola), one instruction suggestion (dijo él), one ambiguous
# excerpt ("bien bien" twice). Only the first should be applicable.
TRANSLATED = "— Hola, dijo él. Aquí hay bien bien y más bien bien al final."


def _make_chunk(source: str, translated: str) -> Chunk:
    return Chunk(
        id=CHUNK_ID,
        chapter_id="chapter_01",
        position=0,
        source_text=source,
        translated_text=translated,
        metadata=ChunkMetadata(
            char_start=0,
            char_end=len(source),
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=len(source.split()),
        ),
        status=ChunkStatus.TRANSLATED,
    )


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "demo"
    (proj / "chunks").mkdir(parents=True)
    save_chunk(_make_chunk("Hi, he said.", TRANSLATED), proj / "chunks" / f"{CHUNK_ID}.json")
    merge_judge_result(
        proj,
        CHUNK_ID,
        "dialogue",
        {
            "eval_name": "dialogue",
            "issues": [
                {"severity": "error", "message": "[raya-spacing] space after raya",
                 "location": "— Hola", "suggestion": "—Hola"},
                {"severity": "warning", "message": "[one-turn-one-paragraph] turn",
                 "location": "dijo él", "suggestion": "move to its own line"},
                {"severity": "warning", "message": "[other] duplicate",
                 "location": "bien bien", "suggestion": "bien"},
            ],
        },
    )
    return proj


def _run(capsys, argv):
    rc = run_judges.main(argv)
    return rc, json.loads(capsys.readouterr().out)


def test_plan_lists_applicable_and_manual(project, capsys):
    rc, payload = _run(capsys, ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}"])
    assert rc == 0
    assert payload["mode"] == "plan"
    assert {a["id"] for a in payload["applicable"]} == {f"{CHUNK_ID}#0"}
    assert payload["applicable"][0]["old"] == "— Hola"
    assert payload["applicable"][0]["new"] == "—Hola"
    assert {m["reason"] for m in payload["manual"]} == {"suggestion_not_literal", "excerpt_ambiguous"}
    # Plan mode writes nothing.
    assert not (project / "corrections_applied.jsonl").exists()
    assert "stale" not in load_chunk_evaluation(project, CHUNK_ID)


def test_apply_selected_edits_backs_up_and_logs(project, capsys, monkeypatch):
    monkeypatch.setattr("src.corrections_apply.realign_chapter", lambda *a, **k: None)
    # A pending reader correction must survive untouched.
    (project / "corrections.jsonl").write_text('{"chunk_id":"x"}\n', encoding="utf-8")

    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#0"],
    )
    assert rc == 0
    assert payload["mode"] == "applied"
    assert payload["applied"] == [f"{CHUNK_ID}#0"]
    assert payload["chapters_realigned"] == ["chapter_01"]
    assert payload["stale_marked"] == [CHUNK_ID]

    # Only the applicable swap landed; manual findings' text is untouched.
    chunk = load_chunk(project / "chunks" / f"{CHUNK_ID}.json")
    assert "—Hola" in chunk.translated_text
    assert "— Hola" not in chunk.translated_text
    assert "dijo él" in chunk.translated_text
    assert chunk.translated_text.count("bien bien") == 2

    # Pre-edit backup written under .chunk_edits/.
    backups = list((project / ".chunk_edits" / "chapter_01" / CHUNK_ID).glob("*.json"))
    assert len(backups) == 1

    # Archived to the shared audit log with judge provenance.
    lines = (project / "corrections_applied.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["source"] == "judge:dialogue"
    assert rec["original_es"] == "— Hola"
    assert rec["corrected_es"] == "—Hola"
    assert "applied_at" in rec

    # Reader queue untouched.
    assert (project / "corrections.jsonl").read_text(encoding="utf-8") == '{"chunk_id":"x"}\n'

    # Evaluation stale-guarded.
    ev = load_chunk_evaluation(project, CHUNK_ID)
    assert ev["stale"] is True
    assert "judge-review apply" in ev["stale_reason"]


def test_rejudge_clears_stale(project, capsys, monkeypatch):
    monkeypatch.setattr("src.corrections_apply.realign_chapter", lambda *a, **k: None)
    _run(capsys, ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#0"])
    assert load_chunk_evaluation(project, CHUNK_ID)["stale"] is True

    # A fresh judge run supersedes the stale marker.
    merge_judge_result(project, CHUNK_ID, "dialogue", {"eval_name": "dialogue", "issues": []})
    ev = load_chunk_evaluation(project, CHUNK_ID)
    assert "stale" not in ev
    assert "stale_since" not in ev
    assert "stale_reason" not in ev


def test_selecting_a_manual_id_is_rejected(project, capsys):
    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#1"],
    )
    assert rc == 1
    assert payload["status"] == "error"
    assert f"{CHUNK_ID}#1" in payload["unknown_ids"]
    assert not (project / "corrections_applied.jsonl").exists()


def test_apply_rejects_selected_id_when_text_changed_before_apply(project, capsys):
    """If chunk text changed since the plan, a previously-applicable id is rejected."""
    chunk_path = project / "chunks" / f"{CHUNK_ID}.json"
    chunk = load_chunk(chunk_path)
    chunk.translated_text = chunk.translated_text.replace("— Hola", "XXX")
    save_chunk(chunk, chunk_path)

    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#0"],
    )
    assert rc == 1
    assert payload["status"] == "error"
    assert f"{CHUNK_ID}#0" in payload["unknown_ids"]
    assert not (project / "corrections_applied.jsonl").exists()


def test_apply_partial_success_archives_only_located_fixes(project, capsys, monkeypatch):
    """When one of two applicable fixes on a chunk cannot locate, report partial apply."""
    monkeypatch.setattr("src.corrections_apply.realign_chapter", lambda *a, **k: None)
    merge_judge_result(
        project,
        CHUNK_ID,
        "dialogue",
        {
            "eval_name": "dialogue",
            "issues": [
                {"severity": "error", "message": "[a] raya", "location": "— Hola", "suggestion": "—Hola"},
                {
                    "severity": "error",
                    "message": "[b] comma",
                    "location": " Hola,",
                    "suggestion": " X,",
                },
            ],
        },
    )
    rc, payload = _run(
        capsys,
        [
            "apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}",
            "--select", f"{CHUNK_ID}#0,{CHUNK_ID}#1",
        ],
    )
    assert rc == 0
    assert payload["status"] == "partial"
    assert payload["applied"] == [f"{CHUNK_ID}#1"]
    assert payload["failed"] == [f"{CHUNK_ID}#0"]
    lines = (project / "corrections_applied.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["source"] == "judge:dialogue"


def test_apply_errors_without_persisted_findings(tmp_path, capsys):
    proj = tmp_path / "bare"
    (proj / "chunks").mkdir(parents=True)
    save_chunk(_make_chunk("Hi", "— Hola x"), proj / "chunks" / f"{CHUNK_ID}.json")
    rc, payload = _run(capsys, ["apply", "--project", str(proj), "--scope", f"chunk:{CHUNK_ID}"])
    assert rc == 1
    assert payload["status"] == "error"
    assert "persist" in payload["error"].lower()
