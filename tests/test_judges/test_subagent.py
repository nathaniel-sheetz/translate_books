"""Tests for the subagent judge backend (prepare / commit) and the shared seam.

The subagent backend renders prompts to files and parses worker drafts; it must
produce the same EvalResult the API path does, persisted the same way. These tests
drive the deterministic prepare/commit functions directly — no LLM, no Task tool.
"""

from __future__ import annotations

import json

import pytest

from src.judges import llm_io, subagent
from src.judges.base import JudgeTarget
from src.judges.dialogue_judge import DialogueComplianceJudge
from src.judges.llm_io import JudgeParseError
from src.models import Chunk, ChunkMetadata, ChunkStatus
from src.utils.file_io import save_chunk
from web_ui.evaluations import load_chunk_evaluation


# ---------------------------------------------------------------------------
# Fixtures: a tmp project with one translated chunk
# ---------------------------------------------------------------------------


def _chunk(cid: str, chapter: str, pos: int, translated: str = "—Hola.") -> Chunk:
    return Chunk(
        id=cid,
        chapter_id=chapter,
        position=pos,
        source_text='"Hello."',
        translated_text=translated,
        metadata=ChunkMetadata(
            char_start=0,
            char_end=10,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=2,
        ),
        status=ChunkStatus.TRANSLATED,
    )


def _project_with_chunk(tmp_path, cid="chapter_01_chunk_000", chapter="chapter_01"):
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir(exist_ok=True)
    save_chunk(_chunk(cid, chapter, 0), chunks_dir / f"{cid}.json")
    return tmp_path, cid


_GOOD_VERDICT = json.dumps(
    {
        "compliant": False,
        "findings": [
            {
                "rule": "raya-spacing",
                "severity": "error",
                "excerpt": "— Hola",
                "message": "space after the opening raya",
                "suggestion": "—Hola",
            }
        ],
        "summary": "one issue",
    }
)


# ---------------------------------------------------------------------------
# The shared seam: build_prompt feeds the API path; parse_response is shared
# ---------------------------------------------------------------------------


def test_build_prompt_is_what_the_api_path_sends(monkeypatch):
    """The prompt prepare writes is byte-identical to what run() sends the LLM."""
    target = JudgeTarget("c0", "chunk", '"Hi."', "—Hola.", {})
    ctx = {"dialogue_rules": "Use the raya."}
    judge = DialogueComplianceJudge()

    sent = {}

    def fake_call(prompt, **kwargs):
        sent["prompt"] = prompt
        return json.dumps({"findings": [], "summary": "ok"})

    monkeypatch.setattr(llm_io, "call_judge", fake_call)
    judge.run(target, ctx)

    assert sent["prompt"] == judge.build_prompt(target, ctx)


def test_parse_response_raises_on_bad_json():
    """parse_response must raise (not swallow) so commit can mark a draft failed."""
    judge = DialogueComplianceJudge()
    with pytest.raises(JudgeParseError):
        judge.parse_response(JudgeTarget("c0", "chunk", "", "", {}), "not json", {})


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_writes_prompt_and_manifest(tmp_path):
    project, cid = _project_with_chunk(tmp_path)
    out = subagent.prepare(project, ["dialogue"], f"chunk:{cid}")

    assert out["status"] == "ok"
    assert len(out["manifest"]) == 1
    entry = out["manifest"][0]
    assert entry["target_id"] == cid
    assert entry["judge"] == "dialogue"

    # Prompt file exists and carries the rendered dialogue prompt.
    prompt_text = (tmp_path / ".harness" / "judges" / f"{cid}.dialogue.prompt.txt").read_text(
        encoding="utf-8"
    )
    assert "—Hola." in prompt_text

    # Manifest persisted with the scope(s) so commit is self-contained.
    manifest = json.loads(
        (tmp_path / ".harness" / "judges" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["scopes"] == [f"chunk:{cid}"]
    assert out["scopes"] == [f"chunk:{cid}"]
    assert manifest["worker_model"] == "sonnet"
    assert out["usage_summary"]["pairs"] == 1


def test_prepare_clears_stale_draft(tmp_path):
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}")
    draft = tmp_path / ".harness" / "judges" / f"{cid}.dialogue.draft.json"
    draft.write_text("stale", encoding="utf-8")

    # A second prepare must wipe the orphaned draft so commit can't read it.
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}")
    assert not draft.exists()


def test_prepare_multiple_scopes_single_manifest(tmp_path):
    """Repeated scopes render into one manifest a single commit can collect."""
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir(exist_ok=True)
    save_chunk(_chunk("chapter_05_chunk_000", "chapter_05", 0), chunks_dir / "chapter_05_chunk_000.json")
    save_chunk(_chunk("chapter_06_chunk_000", "chapter_06", 0), chunks_dir / "chapter_06_chunk_000.json")

    out = subagent.prepare(
        tmp_path, ["dialogue"], ["chapter:chapter_05", "chapter:chapter_06"]
    )

    assert out["scopes"] == ["chapter:chapter_05", "chapter:chapter_06"]
    target_ids = {e["target_id"] for e in out["manifest"]}
    assert target_ids == {"chapter_05_chunk_000", "chapter_06_chunk_000"}
    assert out["usage_summary"]["pairs"] == 2

    manifest = json.loads(
        (tmp_path / ".harness" / "judges" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["scopes"] == ["chapter:chapter_05", "chapter:chapter_06"]
    assert {e["target_id"] for e in manifest["entries"]} == target_ids


def test_prepare_overlapping_scopes_dedup(tmp_path):
    """An overlapping chapter:/chunk: pair must render the shared chunk only once."""
    project, cid = _project_with_chunk(tmp_path)  # chapter_01 / chapter_01_chunk_000

    out = subagent.prepare(
        project, ["dialogue"], ["chapter:chapter_01", f"chunk:{cid}"]
    )

    assert len(out["manifest"]) == 1
    assert out["usage_summary"]["pairs"] == 1


def test_prepare_keep_drafts_preserves_existing(tmp_path):
    """--keep-drafts lets a recovery re-prepare retain already-written worker output."""
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}")
    draft = tmp_path / ".harness" / "judges" / f"{cid}.dialogue.draft.json"
    draft.write_text(_GOOD_VERDICT, encoding="utf-8")

    subagent.prepare(project, ["dialogue"], f"chunk:{cid}", keep_drafts=True)
    assert draft.exists()
    assert draft.read_text(encoding="utf-8") == _GOOD_VERDICT


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------


def _write_draft(tmp_path, cid, judge, body):
    (tmp_path / ".harness" / "judges" / f"{cid}.{judge}.draft.json").write_text(
        body, encoding="utf-8"
    )


def test_commit_success_persists(tmp_path):
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}")
    _write_draft(tmp_path, cid, "dialogue", _GOOD_VERDICT)

    out = subagent.commit(project, persist=True)

    assert out["status"] == "ok"
    assert out["counts"] == {"committed": 1, "failed": 0, "missing": 0}
    assert out["run_header"]["backend"] == "subagent"
    assert out["run_header"]["worker_model"] == "sonnet"

    result = out["results"][0]
    assert result["eval_name"] == "dialogue"
    assert result["metadata"]["backend"] == "subagent"
    assert result["issues"][0]["message"].startswith("[raya-spacing]")

    # Persisted into evaluations/<chunk>.json under judges.dialogue (dashboard badge).
    loaded = load_chunk_evaluation(project, cid)
    assert loaded["judges"]["dialogue"]["eval_name"] == "dialogue"
    assert out["persisted"] and cid in out["persisted"][0]


def test_commit_bad_json_is_failed_not_persisted(tmp_path):
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}")
    _write_draft(tmp_path, cid, "dialogue", "sorry, here is my analysis (no json)")

    out = subagent.commit(project, persist=True)

    assert out["counts"] == {"committed": 0, "failed": 1, "missing": 0}
    assert out["failed"][0]["target_id"] == cid
    assert out["results"] == []
    assert load_chunk_evaluation(project, cid) is None  # nothing persisted


def test_commit_missing_draft(tmp_path):
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}")
    # No draft written.
    out = subagent.commit(project, persist=False)

    assert out["counts"] == {"committed": 0, "failed": 0, "missing": 1}
    assert out["missing"][0]["target_id"] == cid


def test_commit_without_manifest_errors(tmp_path):
    out = subagent.commit(tmp_path, persist=False)
    assert out["status"] == "error"
    assert "manifest" in out["error"]


def test_commit_idempotent_recommit(tmp_path):
    """Re-running commit on the same draft re-parses + re-persists the same result."""
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}")
    _write_draft(tmp_path, cid, "dialogue", _GOOD_VERDICT)

    first = subagent.commit(project, persist=True)
    second = subagent.commit(project, persist=True)
    assert first["counts"] == second["counts"] == {"committed": 1, "failed": 0, "missing": 0}
