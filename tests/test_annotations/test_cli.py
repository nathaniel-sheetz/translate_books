"""The CLI contract: one JSON object on stdout, driven through main()."""

from __future__ import annotations

import json

import pytest

from scripts import review_annotations as cli
from src.annotations import review, store

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


def _run_raw(capsys, argv):
    """Same, but keeps the raw stdout — for asserting what is *not* echoed."""
    rc = cli.main(argv)
    out = capsys.readouterr().out
    return rc, json.loads(out), out


_VERDICT = {
    "state": "needs_help",
    "state_reason": "blank",
    "recommendation": "Usar «marco».",
    "note_text": "«Poyo» es regional.",
    "confidence": "high",
    "evidence": [],
}

# A Gutenberg-imported footnote: gated out before any LLM call, and the shape that
# made a real commit response 29.4KB — 17 of these, bodies and all, echoed a second
# time after `prepare` had already printed them.
_IMPORTED_BODY = (
    "Nota del traductor original: el pastel de ortolanes era un manjar francés "
    "reservado a la mesa de los duques, y su preparación ocupaba páginas enteras "
    "en los recetarios de la época."
)


def _draft(entry, **kw):
    from pathlib import Path

    payload = {"key": entry["key"], **_VERDICT, **kw}
    Path(entry["draft_path"]).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _prepared(project, capsys, extra=()):
    """One drafted word_choice plus one imported (skipped) footnote."""
    write_annotations(
        project,
        [
            _ann(es_idx=1, sub_id="u1"),
            _ann(
                es_idx=2,
                sub_id="u2",
                type="footnote",
                content=f"[Sancerre] {_IMPORTED_BODY}",
                origin="gutenberg",
            ),
            *extra,
        ],
    )
    _, prep = _run(capsys, ["prepare", "--project", str(project)])
    entry = prep["manifest"][0]
    _draft(entry)
    return entry


def test_fanout_forwards_effort_and_prompt_cache(project, capsys, monkeypatch):
    """`--effort` / `--prompt-cache` reach review.fanout as the per-run overrides.

    Argparse-to-callable wiring only — the resolver itself is covered in
    tests/test_harness_usage.py. Without this, a renamed kwarg would go unnoticed
    until a real wave silently ran at the wrong effort.
    """
    seen = {}

    def _capture(project_dir, **kw):
        seen.update(kw)
        return {"wrote": []}

    monkeypatch.setattr(review, "fanout", _capture)

    rc, _ = _run(
        capsys,
        ["fanout", "--project", str(project), "--effort", "low", "--prompt-cache", "off"],
    )
    assert rc == 0
    assert seen["effort"] == "low"
    assert seen["cache"] == "off"

    # Omitted flags forward None so the book's config keys decide.
    seen.clear()
    _run(capsys, ["fanout", "--project", str(project)])
    assert seen["effort"] is None and seen["cache"] is None


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


# --- stdout is a summary, the artifacts are the record ----------------------

def test_commit_does_not_echo_what_the_report_already_holds(project, capsys):
    """The 29.4KB commit: skipped bodies twice, verdicts three times."""
    entry = _prepared(project, capsys)
    rc, payload, raw = _run_raw(capsys, ["commit", "--project", str(project)])

    assert rc == 0
    assert "results" not in payload
    assert _IMPORTED_BODY not in raw
    assert _VERDICT["note_text"] not in raw
    # Both artifacts that do carry it are named.
    assert payload["report_path"] and payload["results_path"]
    assert payload["committed"] == [
        {
            "key": entry["key"],
            "type": "word_choice",
            "state": "needs_help",
            "writable": True,
            "confidence": "high",
        }
    ]


def test_commit_reports_skips_by_reason_without_their_text(project, capsys):
    _prepared(project, capsys)
    _, payload = _run(capsys, ["commit", "--project", str(project)])

    assert list(payload["skipped"]) == ["imported"]
    assert payload["skipped"]["imported"] == ["chapter_01__2__u2"]
    assert payload["counts"]["skipped"] == 1


def test_commit_full_restores_the_untrimmed_payload(project, capsys):
    _prepared(project, capsys)
    _, payload, raw = _run_raw(capsys, ["commit", "--project", str(project), "--full"])

    assert payload["results"][0]["new_content"].endswith(_VERDICT["note_text"])
    assert _IMPORTED_BODY in raw
    assert payload["skipped"][0]["reason"] == "imported"


def test_commit_without_a_report_keeps_the_results(project, capsys):
    """--no-report leaves no artifact to read, so the findings must be printed."""
    _prepared(project, capsys)
    _, payload = _run(capsys, ["commit", "--project", str(project), "--no-report"])

    assert payload["report_path"] is None
    assert payload["results"][0]["note_text"] == _VERDICT["note_text"]


def test_run_stdout_is_a_summary_too(project, capsys, monkeypatch):
    write_annotations(project, [_ann(es_idx=1, sub_id="u1")])
    monkeypatch.setattr(
        review,
        "call_judge",
        lambda *a, **kw: json.dumps(
            {"key": "chapter_01__1__u1", **_VERDICT}, ensure_ascii=False
        ),
    )
    rc, payload, raw = _run_raw(
        capsys, ["run", "--project", str(project), "--cost-limit", "9.99"]
    )

    assert rc == 0 and payload["counts"]["writable"] == 1
    assert "results" not in payload
    assert _VERDICT["note_text"] not in raw
    assert payload["report_path"]


def test_a_real_apply_does_not_repeat_the_dry_run_plan(project, capsys):
    entry = _prepared(project, capsys)
    _run(capsys, ["commit", "--project", str(project)])

    _, plan, plan_raw = _run_raw(
        capsys, ["apply", "--project", str(project), "--dry-run"]
    )
    assert plan["applicable"][0]["old"] == "poyo"
    assert _VERDICT["note_text"] in plan_raw

    _, applied, applied_raw = _run_raw(
        capsys, ["apply", "--project", str(project), "--select", entry["key"]]
    )
    assert applied["applied"] == [entry["key"]]
    assert applied["applicable"] == []
    assert applied["counts"]["applicable"] == 1  # the plan's true size is still there
    assert _VERDICT["note_text"] not in applied_raw


def test_a_real_apply_still_explains_a_key_that_diverged(project, capsys):
    """`stale` is exactly where old/new earns its second showing."""
    entry = _prepared(project, capsys)
    _run(capsys, ["commit", "--project", str(project)])

    # The reader edits the note between the review and the apply.
    store.append_record(
        project,
        _ann(es_idx=1, sub_id="u1", content="poyo — cambié de idea",
             timestamp="2026-03-01T00:00:00"),
    )
    _, payload = _run(
        capsys, ["apply", "--project", str(project), "--select", entry["key"]]
    )

    assert [s["key"] for s in payload["stale"]] == [entry["key"]]
    assert [a["key"] for a in payload["applicable"]] == [entry["key"]]
    assert payload["applicable"][0]["old"] == "poyo"


def test_apply_full_restores_the_untrimmed_payload(project, capsys):
    entry = _prepared(project, capsys)
    _run(capsys, ["commit", "--project", str(project)])
    _, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--select", entry["key"], "--full"],
    )

    assert payload["applied"] == [entry["key"]]
    assert payload["applicable"][0]["new"].endswith(_VERDICT["note_text"])


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
