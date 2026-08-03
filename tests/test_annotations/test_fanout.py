"""Headless fan-out job construction, driven through an injected runner.

No subprocess is ever spawned: ``run_headless_wave`` takes a ``runner`` seam, so
the argv and stdin each job would produce are asserted directly.
"""

from __future__ import annotations

import json

from src.annotations import review

from tests.test_annotations.conftest import write_annotations


def _ann(es_idx=0, **kw):
    base = {
        "project_id": "testbook",
        "chapter_id": "chapter_01",
        "es_idx": es_idx,
        "type": "word_choice",
        "content": "poyo",
        "timestamp": "2026-01-01T00:00:00",
    }
    base.update(kw)
    return base


def _recording_runner(calls, *, stdout=None):
    def runner(cmd, *, input_text, cwd):
        calls.append({"cmd": cmd, "input_text": input_text, "cwd": str(cwd)})
        return 0, stdout or json.dumps(
            {
                "key": "k",
                "state": "needs_help",
                "state_reason": "r",
                "recommendation": "rec",
                "note_text": "texto",
                "confidence": "high",
                "evidence": [],
            }
        ), ""

    return runner


def test_claude_gets_the_preamble_as_a_system_prompt_file(project):
    write_annotations(project, [_ann(es_idx=1, sub_id="u1")])
    prep = review.prepare(project)
    calls = []
    out = review.fanout(project, cli="claude", runner=_recording_runner(calls))

    assert out["counts"]["wrote"] == 1
    cmd = calls[0]["cmd"]
    assert "--system-prompt-file" in cmd
    # stdin is the body alone; the preamble rides the flag so it can cache.
    preamble = (project / ".harness/annotations/preamble.word_choice.txt").read_text(
        encoding="utf-8"
    )
    assert calls[0]["input_text"] not in ("", preamble)
    assert not calls[0]["input_text"].startswith(preamble)


def test_fanout_records_prompt_cache_mode(project):
    """--prompt-cache / cache= threads through to the usage JSONL row."""
    write_annotations(project, [_ann(es_idx=1, sub_id="u1")])
    review.prepare(project)
    review.fanout(
        project, cli="claude", cache="1h", runner=_recording_runner([]),
    )
    log = project / ".harness" / "annotations" / "usage.jsonl"
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["cache"] == "1h"


def test_cursor_gets_the_preamble_folded_into_stdin(project):
    """cursor-agent has no --system-prompt-file, so the launcher folds it in."""
    write_annotations(project, [_ann(es_idx=1, sub_id="u1")])
    review.prepare(project)
    calls = []
    out = review.fanout(project, cli="cursor", runner=_recording_runner(calls))

    assert out["counts"]["wrote"] == 1
    cmd = calls[0]["cmd"]
    assert "--system-prompt-file" not in cmd
    assert "cursor-agent" in cmd[0]
    preamble = (project / ".harness/annotations/preamble.word_choice.txt").read_text(
        encoding="utf-8"
    )
    assert calls[0]["input_text"].startswith(preamble)


def test_fanout_writes_the_draft_that_commit_then_reads(project):
    write_annotations(project, [_ann(es_idx=1, sub_id="u1")])
    prep = review.prepare(project)
    key = prep["manifest"][0]["key"]
    review.fanout(
        project,
        runner=_recording_runner(
            [],
            stdout=json.dumps(
                {
                    "key": key,
                    "state": "needs_help",
                    "state_reason": "r",
                    "recommendation": "rec",
                    "note_text": "texto",
                    "confidence": "high",
                    "evidence": [],
                }
            ),
        ),
    )
    out = review.commit(project)
    assert out["counts"]["committed"] == 1


def test_fanout_skips_entries_that_already_have_a_draft(project):
    from pathlib import Path

    write_annotations(project, [_ann(es_idx=1, sub_id="u1")])
    prep = review.prepare(project)
    Path(prep["manifest"][0]["draft_path"]).write_text("{}", encoding="utf-8")

    calls = []
    out = review.fanout(project, runner=_recording_runner(calls))
    assert out["counts"]["skipped"] == 1
    assert calls == []


def test_target_ids_restricts_the_wave(project):
    write_annotations(
        project,
        [_ann(es_idx=0, sub_id="u1"), _ann(es_idx=1, sub_id="u2")],
    )
    prep = review.prepare(project)
    key = prep["manifest"][0]["key"]
    calls = []
    out = review.fanout(project, target_ids=[key], runner=_recording_runner(calls))
    assert out["counts"]["todo"] == 1
    assert len(calls) == 1


def test_unknown_target_id_is_an_error(project):
    write_annotations(project, [_ann(es_idx=1, sub_id="u1")])
    review.prepare(project)
    out = review.fanout(project, target_ids=["nope"], runner=_recording_runner([]))
    assert "not in manifest" in out["error"]


def test_fanout_without_a_manifest_errors(project):
    out = review.fanout(project, runner=_recording_runner([]))
    assert "run `prepare` first" in out["error"]


def test_a_nonzero_exit_is_reported_as_failed(project):
    write_annotations(project, [_ann(es_idx=1, sub_id="u1")])
    review.prepare(project)

    def failing(cmd, *, input_text, cwd):
        return 1, "", "boom"

    out = review.fanout(project, runner=failing)
    assert out["counts"]["failed"] == 1
    assert "boom" in out["failed"][0]["error"]


def test_fanout_rejects_paths_outside_the_annotations_dir(project):
    write_annotations(project, [_ann(es_idx=1, sub_id="u1")])
    review.prepare(project)
    manifest_path = project / ".harness/annotations/manifest.json"
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    doc["entries"][0]["draft_path"] = str(project / "escape.draft.json")
    manifest_path.write_text(json.dumps(doc), encoding="utf-8")

    out = review.fanout(project, runner=_recording_runner([]))
    assert out["counts"]["wrote"] == 0
    assert out["counts"]["failed"] == 1
    assert any("escapes annotations dir" in f["error"] for f in out["failed"])


def test_fanout_collects_malformed_entries_instead_of_crashing(project):
    write_annotations(project, [_ann(es_idx=1, sub_id="u1")])
    review.prepare(project)
    manifest_path = project / ".harness/annotations/manifest.json"
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    doc["entries"][0].pop("draft_path", None)
    doc["entries"][0].pop("prompt_path", None)
    manifest_path.write_text(json.dumps(doc), encoding="utf-8")

    out = review.fanout(project, runner=_recording_runner([]))
    assert any("missing draft_path or prompt_path" in f["error"] for f in out["failed"])


def test_cursor_paired_with_a_claude_alias_warns(project):
    write_annotations(project, [_ann(es_idx=1, sub_id="u1")])
    review.prepare(project, worker_model="sonnet")
    out = review.fanout(project, cli="cursor", runner=_recording_runner([]))
    assert "looks like a Claude alias" in out["warning"]
