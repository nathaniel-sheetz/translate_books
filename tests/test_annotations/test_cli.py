"""The CLI contract: one JSON object on stdout, driven through main()."""

from __future__ import annotations

import json

import pytest

from scripts import review_annotations as cli
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


def _run(capsys, argv):
    rc = cli.main(argv)
    out = capsys.readouterr().out
    return rc, json.loads(out)


def test_prepare_prints_one_json_object_with_a_schema(project, capsys):
    write_annotations(project, [_ann(es_idx=1, sub_id="u1")])
    rc, payload = _run(capsys, ["prepare", "--project", str(project)])
    assert rc == 0
    assert payload["status"] == "ok"
    assert "_schema" in payload
    assert payload["usage_summary"]["targets"] == 1


def test_commit_and_apply_round_trip(project, capsys):
    from pathlib import Path

    write_annotations(project, [_ann(es_idx=1, sub_id="u1")])
    _, prep = _run(capsys, ["prepare", "--project", str(project)])
    entry = prep["manifest"][0]
    Path(entry["draft_path"]).write_text(
        json.dumps(
            {
                "key": entry["key"],
                "state": "needs_help",
                "state_reason": "blank",
                "recommendation": "Usar «marco».",
                "note_text": "«Poyo» es regional.",
                "confidence": "high",
                "evidence": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rc, committed = _run(capsys, ["commit", "--project", str(project)])
    assert rc == 0 and committed["counts"]["writable"] == 1

    rc, plan = _run(capsys, ["apply", "--project", str(project), "--dry-run"])
    assert rc == 0 and plan["dry_run"] is True and plan["applied"] == []

    rc, applied = _run(
        capsys, ["apply", "--project", str(project), "--select", entry["key"]]
    )
    assert rc == 0 and applied["applied"] == [entry["key"]]


def test_unknown_type_is_rejected_with_json(project, capsys):
    write_annotations(project, [_ann(es_idx=1, sub_id="u1")])
    with pytest.raises(SystemExit) as exc:
        cli.main(["prepare", "--project", str(project), "--type", "typo"])
    payload = json.loads(str(exc.value))
    assert payload["status"] == "error"
    assert "unknown annotation type" in payload["error"]


def test_malformed_scope_is_rejected_with_json(project, capsys):
    write_annotations(project, [_ann(es_idx=1, sub_id="u1")])
    with pytest.raises(SystemExit) as exc:
        cli.main(["prepare", "--project", str(project), "--scope", "chunk:x"])
    payload = json.loads(str(exc.value))
    assert "malformed scope" in payload["error"]


def test_missing_project_is_rejected_with_json(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["prepare", "--project", "no-such-book"])
    payload = json.loads(str(exc.value))
    assert payload["status"] == "error"
    assert "project not found" in payload["error"]


def test_type_filter_narrows_the_run(project, capsys):
    write_annotations(
        project,
        [
            _ann(es_idx=0, type="word_choice", sub_id="u1"),
            _ann(es_idx=2, type="footnote", content="[Sancerre]", sub_id="u2"),
        ],
    )
    _, payload = _run(
        capsys, ["prepare", "--project", str(project), "--type", "footnote"]
    )
    assert payload["usage_summary"]["by_type"] == {"footnote": 1}


def test_run_refuses_to_spend_over_the_cost_limit(project, capsys, monkeypatch):
    write_annotations(project, [_ann(es_idx=1, sub_id="u1")])

    def _boom(*a, **kw):
        raise AssertionError("the cost gate must run before any LLM call")

    monkeypatch.setattr(review, "call_judge", _boom)
    rc, payload = _run(
        capsys, ["run", "--project", str(project), "--cost-limit", "0.0"]
    )
    assert rc == 2
    assert payload["status"] == "cost_exceeded"
    assert payload["estimated_cost"] > 0
