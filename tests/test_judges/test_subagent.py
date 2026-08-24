"""Tests for the subagent judge backend (prepare / commit) and the shared seam.

The subagent backend renders prompts to files and parses worker drafts; it must
produce the same EvalResult the API path does, persisted the same way. These tests
drive the deterministic prepare/commit functions directly — no LLM, no Task tool.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.harness import usage
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


def test_prepare_dict_scopes_accept_a_bare_string(tmp_path):
    """A string value is one scope — list('book') would iterate characters."""
    project, cid = _project_with_chunk(tmp_path)
    out = subagent.prepare(project, ["dialogue"], {"dialogue": "book"})

    assert out["status"] == "ok"
    assert out["scopes_by_judge"] == {"dialogue": ["book"]}
    assert {e["target_id"] for e in out["manifest"]} == {cid}


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


def test_commit_records_the_launcher_that_produced_the_drafts(tmp_path):
    """The dashboard's CLI path passes ``backend="headless:<cli>"`` so a persisted
    verdict names the launcher, not a Task spawn that never happened. The default
    (tested above) stays ``subagent`` for the skill path."""
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}")
    _write_draft(tmp_path, cid, "dialogue", _GOOD_VERDICT)

    out = subagent.commit(project, persist=True, backend="headless:cursor")

    assert out["run_header"]["backend"] == "headless:cursor"
    assert out["results"][0]["metadata"]["backend"] == "headless:cursor"


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


# ---------------------------------------------------------------------------
# Gap tests: paths not covered by the above
# ---------------------------------------------------------------------------


def test_commit_corrupt_manifest_errors(tmp_path):
    """A corrupt manifest (invalid JSON) returns status='error' gracefully."""
    jdir = tmp_path / ".harness" / "judges"
    jdir.mkdir(parents=True)
    (jdir / "manifest.json").write_text("not valid json }{", encoding="utf-8")

    out = subagent.commit(tmp_path, persist=False)

    assert out["status"] == "error"
    assert "unreadable" in out["error"] or "manifest" in out["error"]
    assert out["committed"] == []
    assert out["failed"] == []
    assert out["missing"] == []


def test_commit_bare_exception_parse_crash_is_failed(tmp_path, monkeypatch):
    """A non-JudgeParseError exception during parse_response is caught and failed."""
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}")
    _write_draft(tmp_path, cid, "dialogue", _GOOD_VERDICT)

    # Patch parse_response on the class so every get_judge() instance crashes.
    monkeypatch.setattr(
        DialogueComplianceJudge,
        "parse_response",
        lambda self, target, raw, ctx: (_ for _ in ()).throw(RuntimeError("unexpected crash")),
    )
    out = subagent.commit(project, persist=False)

    assert out["counts"] == {"committed": 0, "failed": 1, "missing": 0}
    assert "RuntimeError" in out["failed"][0]["problem"]


def test_commit_non_chunk_target_not_persisted(tmp_path, monkeypatch):
    """A chapter-scoped target is committed but not written to evaluations/ even with --persist."""
    from src.models import Chunk, ChunkMetadata, ChunkStatus
    from src.utils.file_io import save_chunk

    # Build a chapter-level target by directly writing a manifest with target_type='chapter'.
    jdir = tmp_path / ".harness" / "judges"
    jdir.mkdir(parents=True)
    cid = "chapter_01_chunk_000"
    draft_path = jdir / f"{cid}.dialogue.draft.json"
    manifest_doc = {
        "scopes": ["chapter:chapter_01"],
        "judges": ["dialogue"],
        "worker_model": "sonnet",
        "batch_size": 5,
        "model": None,
        "provider": None,
        "entries": [
            {
                "target_id": cid,
                "target_type": "chapter",  # <-- not 'chunk', so persist must be skipped
                "judge": "dialogue",
                "prompt_path": str(jdir / f"{cid}.dialogue.prompt.txt"),
                "draft_path": str(draft_path),
                "source_word_count": 2,
            }
        ],
    }
    (jdir / "manifest.json").write_text(json.dumps(manifest_doc), encoding="utf-8")
    draft_path.write_text(_GOOD_VERDICT, encoding="utf-8")

    out = subagent.commit(tmp_path, persist=True)

    assert out["counts"]["committed"] == 1
    # Nothing persisted because target_type != 'chunk'
    assert out["persisted"] == []


def test_commit_persist_failure_logged_in_persist_errors(tmp_path, monkeypatch):
    """A persist-layer exception is caught and listed in persist_errors (not re-raised)."""
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}")
    _write_draft(tmp_path, cid, "dialogue", _GOOD_VERDICT)

    import web_ui.evaluations as ev_mod

    monkeypatch.setattr(ev_mod, "merge_judge_result", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    out = subagent.commit(project, persist=True)

    assert out["counts"]["committed"] == 1  # parse succeeded
    assert out["persist_errors"] and cid in out["persist_errors"][0]
    assert out["persisted"] == []


def test_prepare_scope_error_propagates(tmp_path):
    """A bad scope string causes prepare to raise ValueError (not silently return ok)."""
    with pytest.raises((ValueError, FileNotFoundError, NotImplementedError)):
        subagent.prepare(tmp_path, ["dialogue"], "chunk:no_such_chunk")


def test_prepare_empty_scope_returns_nothing_to_judge_message(tmp_path, monkeypatch):
    """When a scope resolves to zero targets, instructions say 'Nothing to judge'."""
    # subagent imports build_targets directly so patch it in the subagent module.
    monkeypatch.setattr(subagent, "build_targets", lambda *a, **k: [])

    out = subagent.prepare(tmp_path, ["dialogue"], "chapter:empty")

    assert out["status"] == "ok"
    assert out["manifest"] == []
    assert "Nothing to judge" in out["instructions"]


# ---------------------------------------------------------------------------
# Density-gated target grouping (targets_per_worker > 1)
# ---------------------------------------------------------------------------


def test_dialogue_marker_count_includes_rayas_and_guillemets():
    """The density signal counts the raya (—) AND the guillemets (« »)."""
    assert subagent._dialogue_marker_count("—Hola. —Adiós.") == 2
    assert subagent._dialogue_marker_count("Paul pensó: «Hoy nacerá».") == 2  # « + »
    assert subagent._dialogue_marker_count("—«»—") == 4
    assert subagent._dialogue_marker_count("plain narration, no markers") == 0


def _three_chunk_chapter(tmp_path, dense_markers: int = 61):
    """Two low-density chunks + one dialogue-dense chunk in chapter_01."""
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir(exist_ok=True)
    save_chunk(_chunk("chapter_01_chunk_000", "chapter_01", 0, "—Hola."), chunks_dir / "chapter_01_chunk_000.json")
    save_chunk(_chunk("chapter_01_chunk_001", "chapter_01", 1, "—Adiós."), chunks_dir / "chapter_01_chunk_001.json")
    dense = "—x " * dense_markers  # > _DENSITY_SOLO_THRESHOLD -> judged solo
    save_chunk(_chunk("chapter_01_chunk_002", "chapter_01", 2, dense), chunks_dir / "chapter_01_chunk_002.json")
    return tmp_path


def test_prepare_default_is_solo_no_members(tmp_path):
    """targets_per_worker=1 (default) reproduces today's one-target-per-entry shape."""
    project, cid = _project_with_chunk(tmp_path)
    out = subagent.prepare(project, ["dialogue"], f"chunk:{cid}")

    assert all("members" not in e for e in out["manifest"])
    assert out["usage_summary"]["workers"] == 1
    assert out["usage_summary"]["targets_per_worker"] == 1


def test_prepare_groups_low_density_targets_keeps_dense_solo(tmp_path):
    """Low-density targets pack into one batch entry; a dense one stays solo."""
    _three_chunk_chapter(tmp_path)

    out = subagent.prepare(tmp_path, ["dialogue"], "chapter:chapter_01", targets_per_worker=2)

    entries = out["manifest"]
    batch = [e for e in entries if "members" in e]
    solo = [e for e in entries if "members" not in e]
    assert len(batch) == 1
    assert {m["target_id"] for m in batch[0]["members"]} == {
        "chapter_01_chunk_000",
        "chapter_01_chunk_001",
    }
    assert len(solo) == 1
    assert solo[0]["target_id"] == "chapter_01_chunk_002"

    # 3 (target×judge) pairs spread across 2 workers.
    assert out["usage_summary"]["pairs"] == 3
    assert out["usage_summary"]["workers"] == 2
    assert out["usage_summary"]["targets"] == 3
    assert out["usage_summary"]["targets_per_worker"] == 2

    # The batch prompt renders the shared rules block once and one <item> per
    # member. (The rules data block is delimited by a single closing tag; the
    # opening tag also appears in the instruction prose, so count the close tag.)
    batch_prompt = (
        tmp_path / ".harness" / "judges" / f"{batch[0]['batch_id']}.dialogue.prompt.txt"
    ).read_text(encoding="utf-8")
    assert batch_prompt.count("</dialogue_rules>") == 1
    assert batch_prompt.count('<item id="') == 2
    assert 'id="chapter_01_chunk_000"' in batch_prompt
    assert 'id="chapter_01_chunk_001"' in batch_prompt


def test_build_batch_prompt_renders_rules_once_and_all_items():
    """build_batch_prompt reuses the solo tags per item and the rules block once."""
    judge = DialogueComplianceJudge()
    t0 = JudgeTarget("chapter_01_chunk_000", "chunk", '"Hi."', "—Hola.", {})
    t1 = JudgeTarget("chapter_01_chunk_001", "chunk", '"Bye."', "—Adiós.", {})

    prompt = judge.build_batch_prompt([t0, t1], {"dialogue_rules": "Use the raya."})

    assert prompt.count("Use the raya.") == 1  # shared rules block rendered once
    assert prompt.count('<item id="') == 2
    assert "—Hola." in prompt and "—Adiós." in prompt


def _batch_entry(tmp_path):
    """Prepare a two-member batch entry (both chunks low-density) and return it."""
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir(exist_ok=True)
    save_chunk(_chunk("chapter_01_chunk_000", "chapter_01", 0, "—Hola."), chunks_dir / "chapter_01_chunk_000.json")
    save_chunk(_chunk("chapter_01_chunk_001", "chapter_01", 1, "—Adiós."), chunks_dir / "chapter_01_chunk_001.json")
    out = subagent.prepare(tmp_path, ["dialogue"], "chapter:chapter_01", targets_per_worker=2)
    return next(e for e in out["manifest"] if "members" in e)


def test_commit_batch_splits_and_attributes_verdicts(tmp_path):
    """A batch draft's verdicts are split per member and persisted separately."""
    batch = _batch_entry(tmp_path)
    verdicts = {
        "verdicts": {
            "chapter_01_chunk_000": {
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
            },
            "chapter_01_chunk_001": {"compliant": True, "findings": [], "summary": "ok"},
        }
    }
    Path(batch["draft_path"]).write_text(json.dumps(verdicts), encoding="utf-8")

    out = subagent.commit(tmp_path, persist=True)

    assert out["counts"] == {"committed": 2, "failed": 0, "missing": 0}
    assert {c["target_id"] for c in out["committed"]} == {
        "chapter_01_chunk_000",
        "chapter_01_chunk_001",
    }
    # Each verdict attributed to and persisted under its own chunk.
    ev0 = load_chunk_evaluation(tmp_path, "chapter_01_chunk_000")
    ev1 = load_chunk_evaluation(tmp_path, "chapter_01_chunk_001")
    assert ev0["judges"]["dialogue"]["issues"][0]["message"].startswith("[raya-spacing]")
    assert ev1["judges"]["dialogue"]["issues"] == []


def test_commit_batch_missing_member_is_missing_not_dropped(tmp_path):
    """A member id absent from verdicts is reported missing, never silently dropped."""
    batch = _batch_entry(tmp_path)
    verdicts = {"verdicts": {"chapter_01_chunk_000": {"compliant": True, "findings": [], "summary": "ok"}}}
    Path(batch["draft_path"]).write_text(json.dumps(verdicts), encoding="utf-8")

    out = subagent.commit(tmp_path, persist=False)

    assert out["counts"] == {"committed": 1, "failed": 0, "missing": 1}
    assert out["missing"][0]["target_id"] == "chapter_01_chunk_001"


def test_commit_batch_malformed_member_verdict_is_failed(tmp_path):
    """A per-item verdict that fails the parser is failed; its batch-mate still commits."""
    batch = _batch_entry(tmp_path)
    verdicts = {
        "verdicts": {
            "chapter_01_chunk_000": {"compliant": True, "findings": [], "summary": "ok"},
            "chapter_01_chunk_001": {"compliant": True, "summary": "missing findings key"},
        }
    }
    Path(batch["draft_path"]).write_text(json.dumps(verdicts), encoding="utf-8")

    out = subagent.commit(tmp_path, persist=False)

    assert out["counts"] == {"committed": 1, "failed": 1, "missing": 0}
    assert out["failed"][0]["target_id"] == "chapter_01_chunk_001"


def test_commit_batch_unparseable_draft_fails_all_members(tmp_path):
    """An unparseable batch draft fails every member (all re-spawnable per target)."""
    batch = _batch_entry(tmp_path)
    Path(batch["draft_path"]).write_text("sorry, no json here", encoding="utf-8")

    out = subagent.commit(tmp_path, persist=False)

    assert out["counts"] == {"committed": 0, "failed": 2, "missing": 0}
    assert {f["target_id"] for f in out["failed"]} == {
        "chapter_01_chunk_000",
        "chapter_01_chunk_001",
    }


def test_commit_batch_missing_draft_marks_all_members_missing(tmp_path):
    """No draft for a batch entry marks each member missing (per-target recovery)."""
    _batch_entry(tmp_path)  # prepare only; write no draft

    out = subagent.commit(tmp_path, persist=False)

    assert out["counts"] == {"committed": 0, "failed": 0, "missing": 2}
    assert {m["target_id"] for m in out["missing"]} == {
        "chapter_01_chunk_000",
        "chapter_01_chunk_001",
    }


def test_commit_batch_null_verdict_is_failed_not_missing(tmp_path):
    """An explicit null verdict is failed (a bad answer), not missing (an omission)."""
    batch = _batch_entry(tmp_path)
    verdicts = {
        "verdicts": {
            "chapter_01_chunk_000": {"compliant": True, "findings": [], "summary": "ok"},
            "chapter_01_chunk_001": None,
        }
    }
    Path(batch["draft_path"]).write_text(json.dumps(verdicts), encoding="utf-8")

    out = subagent.commit(tmp_path, persist=False)

    assert out["counts"] == {"committed": 1, "failed": 1, "missing": 0}
    assert out["failed"][0]["target_id"] == "chapter_01_chunk_001"
    assert "null" in out["failed"][0]["problem"]


def test_commit_batch_malformed_member_is_failed_not_dropped(tmp_path):
    """A corrupt member (no target_id) is recorded failed, never silently dropped."""
    jdir = tmp_path / ".harness" / "judges"
    jdir.mkdir(parents=True)
    draft_path = jdir / "batch_x.dialogue.draft.json"
    manifest_doc = {
        "scopes": ["chapter:chapter_01"],
        "judges": ["dialogue"],
        "worker_model": "sonnet",
        "model": None,
        "provider": None,
        "entries": [
            {
                "batch_id": "batch_x",
                "judge": "dialogue",
                "draft_path": str(draft_path),
                "members": [
                    {"target_id": "chapter_01_chunk_000", "target_type": "chunk"},
                    {"target_type": "chunk"},  # <-- malformed: no target_id
                ],
            }
        ],
    }
    (jdir / "manifest.json").write_text(json.dumps(manifest_doc), encoding="utf-8")
    verdicts = {"verdicts": {"chapter_01_chunk_000": {"compliant": True, "findings": [], "summary": "ok"}}}
    draft_path.write_text(json.dumps(verdicts), encoding="utf-8")

    out = subagent.commit(tmp_path, persist=False)

    assert out["counts"]["committed"] == 1
    assert out["counts"]["failed"] == 1
    assert out["failed"][0]["target_id"] == "?"
    assert "malformed batch member" in out["failed"][0]["problem"]


def test_batch_item_block_renders_extra_item_vars(tmp_path):
    """_batch_item_block keeps judge-specific extra per-item vars (no silent drop)."""
    from src.judges.base import _batch_item_block

    block = _batch_item_block(
        "c0",
        {"source_text": "src", "translation_text": "tr", "notes": "extra"},
    )
    assert "<source>\nsrc\n</source>" in block
    assert "<translation>\ntr\n</translation>" in block
    assert "<notes>\nextra\n</notes>" in block

    # With only the two standard vars, output is unchanged from the old shape.
    plain = _batch_item_block("c0", {"source_text": "src", "translation_text": "tr"})
    assert plain == (
        '<item id="c0">\n'
        "<source>\nsrc\n</source>\n"
        "<translation>\ntr\n</translation>\n"
        "</item>"
    )


# ---------------------------------------------------------------------------
# prepare cache split + headless fanout
# ---------------------------------------------------------------------------


def test_prepare_emits_byte_identical_preamble_and_body(tmp_path):
    """Shared preamble + per-target body round-trip to prompt.txt for solo entries."""
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    for i, text in enumerate(["—Hola.", "—Adiós."]):
        cid = f"chapter_01_chunk_{i:03d}"
        save_chunk(_chunk(cid, "chapter_01", i, translated=text), chunks_dir / f"{cid}.json")

    out = subagent.prepare(tmp_path, ["dialogue"], "chapter:chapter_01")
    assert out["status"] == "ok"
    assert len(out["manifest"]) == 2

    preamble_paths = {e.get("preamble_path") for e in out["manifest"]}
    assert len(preamble_paths) == 1 and None not in preamble_paths
    preamble_path = Path(next(iter(preamble_paths)))
    assert preamble_path.name == "preamble.dialogue.txt"
    preamble = preamble_path.read_text(encoding="utf-8")
    assert preamble
    assert "# ---8<--- cache split ---8<---" not in preamble

    for entry in out["manifest"]:
        assert "body_path" in entry and "preamble_path" in entry
        body = Path(entry["body_path"]).read_text(encoding="utf-8")
        prompt = Path(entry["prompt_path"]).read_text(encoding="utf-8")
        assert preamble + body == prompt
        assert body.startswith("# ---8<--- cache split ---8<---")


def test_prepare_grouped_entries_omit_preamble_paths(tmp_path):
    """Grouped (batch) entries keep only prompt_path — no cache split."""
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    # Low-density chunks so they pack together (raya count under solo threshold).
    for i in range(2):
        cid = f"chapter_01_chunk_{i:03d}"
        save_chunk(
            _chunk(cid, "chapter_01", i, translated="—Hola." * 3),
            chunks_dir / f"{cid}.json",
        )

    out = subagent.prepare(
        tmp_path, ["dialogue"], "chapter:chapter_01", targets_per_worker=2
    )
    batched = [e for e in out["manifest"] if "members" in e]
    assert batched, "expected at least one grouped entry"
    for entry in batched:
        assert "preamble_path" not in entry
        assert "body_path" not in entry
        assert Path(entry["prompt_path"]).exists()


def test_build_prompt_parts_round_trips(tmp_path):
    """prefix + suffix == build_prompt; marker lands after shared block."""
    judge = DialogueComplianceJudge()
    target = JudgeTarget("c0", "chunk", '"Hi."', "—Hola.", {})
    ctx = {"dialogue_rules": "Use the raya."}
    prefix, suffix = judge.build_prompt_parts(target, ctx)
    assert prefix + suffix == judge.build_prompt(target, ctx)
    assert prefix
    assert "</dialogue_rules>" in prefix
    assert "\n<source>\n" not in prefix
    assert suffix.startswith("# ---8<--- cache split ---8<---")
    assert "\n<source>\n" in suffix


def test_fanout_writes_drafts_via_runner_seam(tmp_path):
    """fanout invokes the runner with --system-prompt-file when split paths exist."""
    project, cid = _project_with_chunk(tmp_path)
    prep = subagent.prepare(project, ["dialogue"], f"chunk:{cid}")
    entry = prep["manifest"][0]
    assert "preamble_path" in entry and "body_path" in entry

    seen_cmds: list[list[str]] = []

    def fake_runner(cmd, *, input_text, cwd):
        seen_cmds.append(list(cmd))
        assert "--system-prompt-file" in cmd
        assert str(Path(entry["preamble_path"]).resolve()) in cmd
        assert input_text == Path(entry["body_path"]).read_text(encoding="utf-8")
        assert Path(cwd).name == "claude-headless-empty"
        assert "--tools" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "json"
        return 0, _GOOD_VERDICT, ""

    out = subagent.fanout(project, runner=fake_runner)
    assert out["counts"]["wrote"] == 1
    assert cid in out["wrote"]
    draft = Path(entry["draft_path"]).read_text(encoding="utf-8").strip()
    assert json.loads(draft)["compliant"] is False
    assert seen_cmds and "--output-format" in seen_cmds[0]

    # Idempotent: existing draft is skipped.
    out2 = subagent.fanout(project, runner=fake_runner)
    assert out2["counts"]["skipped"] == 1
    assert out2["counts"]["wrote"] == 0


def test_fanout_commit_lands_headless_draft(tmp_path):
    """commit parses a draft written by fanout the same as a Task worker draft."""
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}")

    def fake_runner(cmd, *, input_text, cwd):
        return 0, _GOOD_VERDICT, ""

    fan = subagent.fanout(project, runner=fake_runner)
    assert fan["counts"]["wrote"] == 1
    out = subagent.commit(project, persist=False)
    assert out["counts"]["committed"] == 1
    assert out["committed"][0]["target_id"] == cid
    assert out["counts"]["failed"] == 0


def test_fanout_cursor_skips_cache_split_and_commits(tmp_path):
    """cursor fanout uses the full prompt; commit still parses the JSON verdict."""
    project, cid = _project_with_chunk(tmp_path)
    prep = subagent.prepare(project, ["dialogue"], f"chunk:{cid}", worker_model="grok-4.5")
    entry = prep["manifest"][0]
    assert "preamble_path" in entry
    full_prompt = Path(entry["prompt_path"]).read_text(encoding="utf-8")

    seen_cmds: list[list[str]] = []
    seen_inputs: list[str] = []

    def fake_runner(cmd, *, input_text, cwd):
        seen_cmds.append(list(cmd))
        seen_inputs.append(input_text)
        assert "--system-prompt-file" not in cmd
        assert "--tools" not in cmd
        assert "grok-4.5" in cmd
        return 0, _GOOD_VERDICT, ""

    fan = subagent.fanout(project, cli="cursor", runner=fake_runner)
    assert fan["counts"]["wrote"] == 1
    assert fan["cli"] == "cursor"
    assert seen_inputs and seen_inputs[0] == full_prompt
    out = subagent.commit(project, persist=False)
    assert out["counts"]["committed"] == 1
    assert out["committed"][0]["target_id"] == cid


def test_fanout_cursor_warns_on_claude_worker_model(tmp_path, capsys):
    """cursor + Claude-looking worker_model surfaces the same warning as translate."""
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}", worker_model="sonnet")

    def fake_runner(cmd, *, input_text, cwd):
        return 0, _GOOD_VERDICT, ""

    fan = subagent.fanout(project, cli="cursor", runner=fake_runner)
    assert "warning" in fan
    assert "headless_cli=cursor" in fan["warning"]
    err = capsys.readouterr().err
    assert "headless_cli=cursor" in err


# ---------------------------------------------------------------------------
# The usage gate (2026-07-30 friction log, items 0 + 1)
#
# `estimated_api_cost` priced the API backend while the user approved the
# headless one, which consumed ~2.4x the tokens. Both figures are reported now,
# and the headless one self-calibrates off the measured per-job overhead.
# ---------------------------------------------------------------------------


def test_prepare_reports_headless_tokens_beside_the_api_price(tmp_path):
    project, cid = _project_with_chunk(tmp_path)
    out = subagent.prepare(project, ["dialogue"], f"chunk:{cid}")
    summary = out["usage_summary"]

    assert summary["estimated_prompt_tokens"] > 0
    # Untouched machine: the documented baseline probe for this book's CLI
    # (headless_cli defaults to claude), not a silent zero.
    claude_baseline = usage.DEFAULT_BASELINE_TOKENS["claude"]
    assert summary["headless_baseline_tokens"] == claude_baseline
    assert summary["headless_baseline_source"].startswith("default:")
    assert summary["estimated_headless_tokens"] == (
        summary["estimated_prompt_tokens"] + summary["workers"] * claude_baseline
    )
    # The API figure is still there — it just no longer stands alone.
    assert "estimated_api_cost" in summary
    # Effort is visible at the usage gate, not only after a wave.
    assert summary["headless_effort"] == "medium"
    assert summary["headless_effort_source"] == "default:judges"


def test_prepare_baseline_self_calibrates_from_the_usage_log(tmp_path):
    project, cid = _project_with_chunk(tmp_path)
    log = subagent.usage_log_path(project)
    log.parent.mkdir(parents=True, exist_ok=True)
    row = json.dumps(
        {"cli": "claude", "input": 0, "cache_creation": 4200, "cache_read": 0,
         "prompt_sent": 0, "rc": 0}
    )
    log.write_text((row + "\n") * 4, encoding="utf-8")

    summary = subagent.prepare(project, ["dialogue"], f"chunk:{cid}")["usage_summary"]
    assert summary["headless_baseline_tokens"] == 4200
    assert summary["headless_baseline_source"].startswith("measured:")


def test_prepare_baseline_ignores_the_other_cli_s_rows(tmp_path):
    """One usage.jsonl, two families ~4.4x apart: a mixed median describes neither.

    A claude book that has also run cursor waves must still quote the claude
    number — before the per-cli filter, four cursor rows dragged its estimate
    from ~3.9k to ~19k and the gate quoted that at the moment consent was given.
    """
    project, cid = _project_with_chunk(tmp_path)
    log = subagent.usage_log_path(project)
    log.parent.mkdir(parents=True, exist_ok=True)

    def _row(cli: str, overhead: int) -> str:
        return json.dumps({
            "cli": cli, "input": 0, "cache_creation": overhead, "cache_read": 0,
            "prompt_sent": 0, "rc": 0,
        })

    log.write_text(
        "".join([_row("cursor", 19_000) + "\n"] * 4 + [_row("claude", 4_200) + "\n"] * 4),
        encoding="utf-8",
    )
    summary = subagent.prepare(project, ["dialogue"], f"chunk:{cid}")["usage_summary"]
    assert summary["headless_baseline_tokens"] == 4200
    assert "claude" in summary["headless_baseline_source"]


def test_prepare_on_a_cursor_book_quotes_the_cursor_baseline(tmp_path):
    """~17.2k per process, not the ~3.9k that priced every past Cursor wave."""
    project, cid = _project_with_chunk(tmp_path)
    from src.harness import state as hstate

    cfg = hstate.load_config(project)
    cfg["headless_cli"] = "cursor"
    hstate.save_config(project, cfg)

    summary = subagent.prepare(project, ["dialogue"], f"chunk:{cid}")["usage_summary"]
    assert summary["headless_baseline_tokens"] == usage.DEFAULT_BASELINE_TOKENS["cursor"]
    assert "2026-08-10" in summary["headless_baseline_source"]
    assert summary["estimated_headless_tokens"] == (
        summary["estimated_prompt_tokens"]
        + summary["workers"] * usage.DEFAULT_BASELINE_TOKENS["cursor"]
    )


# ---------------------------------------------------------------------------
# CLI-aware prepare / fanout (2026-08-11 friction logs)
#
# `prepare` had no --cli, so a Cursor operator's first manifest pinned `sonnet`
# and quoted the Claude baseline; the only fix was a destructive re-prepare. And
# effort had two channels with only the Claude one reported, so a wave running
# `effort=high` announced itself as `medium`.
# ---------------------------------------------------------------------------


def test_prepare_cli_flag_writes_the_cursor_profile_into_the_manifest(tmp_path):
    project, cid = _project_with_chunk(tmp_path)
    out = subagent.prepare(
        project, ["dialogue"], f"chunk:{cid}",
        cli="cursor", worker_model="grok-4.5[effort=high,fast=false]",
    )

    eff = out["effective"]
    assert eff["cli"] == "cursor" and eff["cli_source"] == "cli"
    assert eff["worker_model"] == "grok-4.5[effort=high,fast=false]"
    assert eff["baseline_tokens"] == usage.DEFAULT_BASELINE_TOKENS["cursor"]

    # …and it is on disk, so `fanout` reproduces the consented wave with no flags.
    doc = json.loads((subagent._judges_dir(project) / "manifest.json").read_text("utf-8"))
    assert doc["cli"] == "cursor"
    assert doc["worker_model"] == "grok-4.5[effort=high,fast=false]"
    assert doc["effort"] == "high"
    assert doc["effort_channel"] == "model_bracket"


def test_prepare_effort_and_the_model_bracket_can_no_longer_disagree(tmp_path):
    """The photogen bug, as an invariant.

    After pinning `effort=high` in the bracket — the only knob Cursor honours —
    prepare still advertised `headless_effort: medium` from the Claude ladder. An
    agent relaying usage_summary faithfully told the operator the wave was medium
    while argv said high.
    """
    project, cid = _project_with_chunk(tmp_path)
    out = subagent.prepare(
        project, ["dialogue"], f"chunk:{cid}",
        cli="cursor", worker_model="grok-4.5[effort=high,fast=false]",
    )
    summary, eff = out["usage_summary"], out["effective"]

    assert summary["headless_effort"] == "high"
    assert summary["headless_effort_channel"] == "model_bracket"
    assert summary["cli"] == "cursor"
    # The reported level and the level in the argv-bound model are the same fact.
    from src.harness.headless import cursor_model_effort
    assert cursor_model_effort(eff["worker_model"]) == summary["headless_effort"]
    # And nothing anywhere claims the Claude default for a Cursor wave.
    assert "medium" not in json.dumps(eff)


def test_prepare_never_pins_a_claude_alias_on_a_cursor_wave(tmp_path, capsys):
    """The default is per-CLI now, and a bad explicit pin warns at prepare, not fanout."""
    project, cid = _project_with_chunk(tmp_path)
    out = subagent.prepare(
        project, ["dialogue"], f"chunk:{cid}", cli="cursor", worker_model="sonnet"
    )
    assert any("headless_cli=cursor" in w for w in out["warnings"])
    assert "headless_cli=cursor" in capsys.readouterr().err


def test_fanout_inherits_the_manifest_cli_without_re_passing_it(tmp_path):
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(
        project, ["dialogue"], f"chunk:{cid}",
        cli="cursor", worker_model="grok-4.5[effort=high,fast=false]",
    )

    seen: list[list[str]] = []

    def fake_runner(cmd, *, input_text, cwd):
        seen.append(list(cmd))
        return 0, _GOOD_VERDICT, ""

    fan = subagent.fanout(project, runner=fake_runner)  # no --cli, no --worker-model
    assert fan["effective"]["cli"] == "cursor"
    # …and it says so honestly: this came off the consented manifest, not a flag.
    assert fan["effective"]["cli_source"] == "manifest"
    assert fan["effective"]["worker_model_source"] == "manifest"
    assert fan["counts"]["wrote"] == 1
    assert "grok-4.5[effort=high,fast=false]" in seen[0]
    # cursor-agent takes no --effort; the level rides in the model instead.
    assert "--effort" not in seen[0]


def test_fanout_worker_model_overrides_the_manifest_and_writes_it_back(tmp_path):
    """A bad pin costs one flag, not a destructive re-prepare.

    The write-back is load-bearing: `commit` stamps the manifest's worker_model
    into each result's metadata, and `status` reports that back as what judged the
    book — so an un-persisted override would misreport the run forever.
    """
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}", cli="cursor",
                     worker_model="sonnet")

    seen: list[list[str]] = []

    def fake_runner(cmd, *, input_text, cwd):
        seen.append(list(cmd))
        return 0, _GOOD_VERDICT, ""

    fan = subagent.fanout(
        project, worker_model="grok-4.5[effort=high,fast=false]", runner=fake_runner
    )
    assert fan["counts"]["wrote"] == 1
    assert "grok-4.5[effort=high,fast=false]" in seen[0]
    assert "sonnet" not in seen[0]

    doc = json.loads((subagent._judges_dir(project) / "manifest.json").read_text("utf-8"))
    assert doc["worker_model"] == "grok-4.5[effort=high,fast=false]"

    committed = subagent.commit(project, persist=False)
    assert committed["run_header"]["worker_model"] == "grok-4.5[effort=high,fast=false]"


def test_fanout_estimate_on_cursor_reports_the_bracket_effort(tmp_path):
    """`estimate.effort` used to contradict `estimate.argv` in adjacent keys."""
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(
        project, ["dialogue"], f"chunk:{cid}",
        cli="cursor", worker_model="grok-4.5[effort=high,fast=false]",
    )

    def exploding_runner(*args, **kwargs):
        raise AssertionError("--estimate must not spawn")

    est = subagent.fanout(project, estimate=True, runner=exploding_runner)["estimate"]
    assert est["effort"] == "high"
    assert est["effort_channel"] == "model_bracket"
    assert est["baseline_tokens"] == usage.DEFAULT_BASELINE_TOKENS["cursor"]
    assert est["projected_tokens"] == (
        est["prompt_tokens"] + est["jobs"] * usage.DEFAULT_BASELINE_TOKENS["cursor"]
    )
    # No --effort in the argv, and that is now consistent with what is reported.
    assert "--effort" not in est["argv"]
    assert "grok-4.5[effort=high,fast=false]" in est["argv"]


def test_prepare_survives_an_unreadable_cursor_cli_config(tmp_path, monkeypatch):
    """Failing to read a preferences file must never be what stops a wave."""
    from src.harness import headless

    monkeypatch.setattr(headless, "CURSOR_CLI_CONFIG", tmp_path / "nope.json")
    project, cid = _project_with_chunk(tmp_path)
    out = subagent.prepare(project, ["dialogue"], f"chunk:{cid}", cli="cursor")
    assert out["status"] == "ok"
    assert out["effective"]["worker_model"] == "auto"


def test_fanout_estimate_spawns_nothing(tmp_path):
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}")

    def exploding_runner(*args, **kwargs):
        raise AssertionError("--estimate must not spawn")

    out = subagent.fanout(project, estimate=True, runner=exploding_runner)
    assert out["wrote"] == []
    assert out["estimate"]["jobs"] == 1
    assert out["estimate"]["projected_tokens"] == (
        out["estimate"]["prompt_tokens"] + usage.DEFAULT_BASELINE_TOKENS["claude"]
    )
    # The argv is included so a bad headless_extra_flags entry is visible before
    # a wave commits to it, not after N jobs have failed.
    assert "--output-format" in out["estimate"]["argv"]
    # Auto default for judges is medium — exactly one --effort, plus the fields.
    # `prepare` resolved that medium and recorded it, so a bare fanout inherits
    # it *from the manifest*: same level, and the provenance says which.
    assert out["estimate"]["effort"] == "medium"
    assert out["estimate"]["effort_source"] == "manifest"
    argv = out["estimate"]["argv"]
    assert argv.count("--effort") == 1
    assert "--effort" in argv and argv[argv.index("--effort") + 1] == "medium"
    # Solo dialogue entry: N=1 → auto picks off (1.0× beats 1.25× with no followers).
    assert out["estimate"]["cache"] == "off"
    # And no draft was written.
    draft = json.loads((subagent._judges_dir(project) / "manifest.json").read_text("utf-8"))
    assert not Path(draft["entries"][0]["draft_path"]).exists()


def test_fanout_inherits_the_manifest_effort(tmp_path):
    """`prepare --effort xhigh` quotes the consent estimate for xhigh; a bare
    `fanout` used to drop it and fall through to the judges default (medium),
    so the wave ran at a level nobody was shown."""
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}", effort="xhigh")

    manifest = json.loads(
        (subagent._judges_dir(project) / "manifest.json").read_text("utf-8"))
    assert manifest["effort"] == "xhigh"

    out = subagent.fanout(
        project, estimate=True,
        runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no spawn")),
    )
    assert out["estimate"]["effort"] == "xhigh"
    assert out["estimate"]["effort_source"] == "manifest"
    argv = out["estimate"]["argv"]
    assert argv[argv.index("--effort") + 1] == "xhigh"


def test_fanout_estimate_cli_effort_override(tmp_path):
    """--effort on fanout flips the estimate without touching config."""
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}")

    out = subagent.fanout(
        project, estimate=True, effort="low",
        runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no spawn")),
    )
    assert out["estimate"]["effort"] == "low"
    assert out["estimate"]["effort_source"] == "cli"
    argv = out["estimate"]["argv"]
    assert argv.count("--effort") == 1
    assert argv[argv.index("--effort") + 1] == "low"


def test_fanout_estimate_reports_resolved_cache_mode(tmp_path):
    """--estimate prices the projection under the resolved prompt-cache mode."""
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}")

    out = subagent.fanout(
        project, estimate=True, cache="off",
        runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no spawn")),
    )
    assert out["estimate"]["cache"] == "off"
    # off = prompt + N*baseline (same as the pre-cache formula for a single job).
    assert out["estimate"]["projected_tokens"] == (
        out["estimate"]["prompt_tokens"] + out["estimate"]["baseline_tokens"]
    )

    pinned = subagent.fanout(
        project, estimate=True, cache="1h",
        runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no spawn")),
    )
    assert pinned["estimate"]["cache"] == "1h"
    # 1h writes at 2×, so a single-job wave costs 2*(P+U) > off's (P+U).
    assert pinned["estimate"]["projected_tokens"] > out["estimate"]["projected_tokens"]

def test_fanout_records_usage_and_writes_the_job_log(tmp_path):
    project, cid = _project_with_chunk(tmp_path)
    subagent.prepare(project, ["dialogue"], f"chunk:{cid}")
    envelope = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": _GOOD_VERDICT, "total_cost_usd": 0.02,
        "usage": {"input_tokens": 3, "output_tokens": 40,
                  "cache_creation_input_tokens": 5778, "cache_read_input_tokens": 3289},
    })

    out = subagent.fanout(project, runner=lambda *a, **k: (0, envelope, ""))
    assert out["counts"]["wrote"] == 1
    assert out["usage"]["cache_creation"] == 5778
    assert 0 < out["usage"]["overhead_ratio"] < 1

    rows = subagent.usage_log_path(project).read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["id"] == cid

    # The draft is the verdict, not the envelope — commit must still parse it.
    committed = subagent.commit(project)
    assert committed["counts"]["committed"] == 1


def test_commit_drops_the_unattributable_evaluator_results_array(tmp_path):
    """N same-named entries with no target_id could never be used for reporting."""
    project, cid = _project_with_chunk(tmp_path)
    out = subagent.prepare(project, ["dialogue"], f"chunk:{cid}")
    Path(out["manifest"][0]["draft_path"]).write_text(_GOOD_VERDICT, encoding="utf-8")

    committed = subagent.commit(project, persist=True)
    assert "evaluator_results" not in committed["summary"]
    assert committed["summary"]["total_issues"] >= 1  # the usable numbers stay
    # Paths collapse to one directory plus basenames.
    assert committed["persisted"] == [f"{cid}.json"]
    assert committed["persisted_dir"].endswith("evaluations")
