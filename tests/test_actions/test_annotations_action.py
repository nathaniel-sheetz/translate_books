"""The ``annotations`` action: detect, run, and the auto-apply policy.

The action is an adapter, so the pipeline itself is covered by
``tests/test_annotations/``. What is pinned here is what the adapter adds:
the blockers ``detect`` raises, that ``run`` never destroys work in flight, and
— the one that matters most — that ``auto_apply`` cannot write a footnote,
because a footnote's write is a *replace* whose text is published into the EPUB.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.actions import annotations as action
from src.actions.registry import Budget, Policy
from src.annotations import review
from tests.test_annotations.conftest import write_annotations


def _ann(es_idx=0, ann_type="word_choice", content="poyo", **kw):
    base = {
        "project_id": "testbook",
        "chapter_id": "chapter_01",
        "es_idx": es_idx,
        "type": ann_type,
        "content": content,
        "timestamp": "2026-01-01T00:00:00",
    }
    base.update(kw)
    return base


def _verdict_runner(*, confidence="high", note_text="texto de la IA"):
    """A runner that answers every job with one well-formed verdict.

    No ``key`` field: ``commit`` treats an echoed key that disagrees with the
    target as a hard failure (a mis-routed gloss must never be applied), and
    only an *echoed* key is checked, so omitting it keeps these tests about the
    action rather than about prompt round-tripping.
    """
    def runner(cmd, *, input_text, cwd):
        return 0, json.dumps({
            "state": "needs_help",
            "state_reason": "r",
            "recommendation": "usa 'banca'",
            "note_text": note_text,
            "confidence": confidence,
            "evidence": [],
        }), ""
    return runner


@pytest.fixture
def budget():
    return Budget(concurrency=2, default_cli="claude")


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------


def test_detect_counts_by_type(project):
    write_annotations(project, [
        _ann(es_idx=0, sub_id="u1"),
        _ann(es_idx=1, ann_type="footnote", content="[1] nota", sub_id="u2"),
        _ann(es_idx=2, ann_type="flag", content="raro", sub_id="u3"),
    ])
    state = action.detect(project)
    assert state.pending == 3
    assert state.by_type == {"flag": 1, "footnote": 1, "word_choice": 1}
    assert state.runnable is True


def test_detect_costs_nothing_and_writes_nothing(project):
    write_annotations(project, [_ann(sub_id="u1")])
    action.detect(project)
    assert not (project / ".harness" / "annotations").exists()


def test_detect_reports_orphans_as_attention_not_a_blocker(project):
    """One un-anchorable note must not stop the eleven beside it."""
    write_annotations(project, [
        _ann(es_idx=0, sub_id="u1"),
        _ann(es_idx=999, sub_id="u2"),      # no such sentence
    ])
    state = action.detect(project)
    assert state.pending == 1
    assert state.skipped == {"orphaned": 1}
    assert state.blockers == []
    assert state.runnable is True
    assert any("orphaned" in note for note in state.attention)


def test_detect_blocks_on_a_missing_harness_config(project):
    write_annotations(project, [_ann(sub_id="u1")])
    (project / ".harness" / "config.json").unlink()
    state = action.detect(project)
    assert state.runnable is False
    assert any("config.json" in b for b in state.blockers)


def test_detect_blocks_on_a_pinned_cli_that_is_not_installed(project, monkeypatch):
    write_annotations(project, [_ann(sub_id="u1")])
    (project / ".harness" / "config.json").write_text(
        json.dumps({"headless_cli": "cursor"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "src.harness.headless.cli_binary_present", lambda cli: False
    )
    state = action.detect(project)
    assert state.runnable is False
    assert any("cursor" in b for b in state.blockers)


def test_detect_does_not_block_on_an_unpinned_missing_cli(project, monkeypatch):
    """That is the machine's problem, reported once by the driver, not per book."""
    write_annotations(project, [_ann(sub_id="u1")])
    monkeypatch.setattr(
        "src.harness.headless.cli_binary_present", lambda cli: False
    )
    assert action.detect(project).blockers == []


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_run_prepares_fans_out_and_commits(project, budget):
    write_annotations(project, [_ann(es_idx=0, sub_id="u1"), _ann(es_idx=1, sub_id="u2")])

    result = action.run(project, budget, runner=_verdict_runner())

    assert result.status == "ok"
    assert result.targets == 2
    assert result.committed == 2
    assert result.report_path
    assert (project / ".harness" / "annotations" / "results.json").exists()


def test_run_dry_writes_nothing(project, budget):
    write_annotations(project, [_ann(sub_id="u1")])

    result = action.run(project, Budget(dry_run=True, default_cli="claude"))

    assert result.targets == 1
    assert result.detail["dry_run"] is True
    # `prepare` renders a prompt per annotation; a dry run must not leave those
    # behind for a later real run to treat as already prepared.
    assert not (project / ".harness" / "annotations").exists()


def test_run_keeps_drafts_of_work_in_flight(project, budget):
    """A resumed pass must not unlink the drafts the previous one was writing."""
    write_annotations(project, [_ann(es_idx=0, sub_id="u1"), _ann(es_idx=1, sub_id="u2")])
    prep = review.prepare(project)
    entry = prep["manifest"][0]
    draft = Path(entry["draft_path"])
    draft.write_text(json.dumps({
        "key": entry["key"], "state": "already_resolved",
        "state_reason": "the reader fixed it", "recommendation": "", "note_text": "",
        "confidence": "high", "evidence": [],
    }), encoding="utf-8")
    stamp = draft.read_text(encoding="utf-8")

    action.run(project, budget, runner=_verdict_runner())

    assert draft.read_text(encoding="utf-8") == stamp


def test_run_caps_at_max_targets_and_reports_the_remainder(project):
    write_annotations(project, [
        _ann(es_idx=0, sub_id="u1"),
        _ann(es_idx=1, sub_id="u2"),
        _ann(es_idx=2, sub_id="u3"),
    ])

    result = action.run(
        project,
        Budget(concurrency=2, max_targets=2, default_cli="claude"),
        runner=_verdict_runner(),
    )

    assert result.targets == 2
    assert result.status == "partial"
    assert result.detail["left_over"] == 1


def test_run_with_nothing_pending_is_a_clean_no_op(project, budget):
    write_annotations(project, [])
    result = action.run(project, budget, runner=_verdict_runner())
    assert result.status == "ok"
    assert result.targets == 0


# ---------------------------------------------------------------------------
# auto_apply — the policy
# ---------------------------------------------------------------------------


def _reviewed(project, budget, **kw):
    action.run(project, budget, runner=_verdict_runner(**kw))


def test_auto_apply_writes_a_high_confidence_word_choice(project, budget):
    write_annotations(project, [_ann(sub_id="u1")])
    _reviewed(project, budget)

    result = action.auto_apply(project, Policy())

    assert result.status == "ok"
    assert len(result.applied) == 1
    live = [
        json.loads(line)
        for line in (project / "annotations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert live[-1]["content"].startswith("poyo\n")
    assert "texto de la IA" in live[-1]["content"]
    assert live[-1]["ai_review"]["mode"] == "append"


def test_auto_apply_never_writes_a_footnote(project, budget):
    """Its write is a *replace* and ``src/endnotes.py`` publishes that text."""
    write_annotations(
        project, [_ann(ann_type="footnote", content="[1] Sancerre", sub_id="u1")]
    )
    _reviewed(project, budget)

    plan = review.apply(project, dry_run=True)
    assert [item["mode"] for item in plan["applicable"]] == ["replace"]

    result = action.auto_apply(project, Policy())

    assert result.applied == []
    assert len(result.held) == 1


def test_a_footnote_stays_held_even_if_its_type_is_configured_in(project, budget):
    """Belt and braces: a typo in app_config.json must not publish a gloss."""
    write_annotations(
        project, [_ann(ann_type="footnote", content="[1] Sancerre", sub_id="u1")]
    )
    _reviewed(project, budget)

    reckless = Policy(types=("word_choice", "inconsistency", "flag", "footnote"))
    result = action.auto_apply(project, reckless)

    assert result.applied == []
    assert len(result.held) == 1


@pytest.mark.parametrize("confidence", ["low", "medium"])
def test_auto_apply_holds_below_the_confidence_floor(project, budget, confidence):
    write_annotations(project, [_ann(sub_id="u1")])
    _reviewed(project, budget, confidence=confidence)

    result = action.auto_apply(project, Policy(confidence_floor="high"))

    assert result.applied == []
    assert len(result.held) == 1


def test_auto_apply_dry_run_writes_nothing(project, budget):
    write_annotations(project, [_ann(sub_id="u1")])
    _reviewed(project, budget)
    before = (project / "annotations.jsonl").read_text(encoding="utf-8")

    result = action.auto_apply(project, Policy(dry_run=True))

    assert result.applied == []
    assert len(result.detail["would_apply"]) == 1
    assert (project / "annotations.jsonl").read_text(encoding="utf-8") == before


def test_auto_apply_is_idempotent(project, budget):
    write_annotations(project, [_ann(sub_id="u1")])
    _reviewed(project, budget)
    first = action.auto_apply(project, Policy())
    second = action.auto_apply(project, Policy())

    assert len(first.applied) == 1
    assert second.applied == []
    assert second.already_applied == first.applied


def test_auto_apply_skips_a_note_edited_since_the_review(project, budget):
    """The review no longer describes the text on disk, so it must not land."""
    write_annotations(project, [_ann(sub_id="u1")])
    _reviewed(project, budget)
    write_annotations(project, [_ann(sub_id="u1", content="poyo (ya lo cambié)")])

    result = action.auto_apply(project, Policy())

    assert result.status == "partial"
    assert result.applied == []
    assert len(result.stale) == 1


def test_auto_apply_without_results_reports_an_error(project):
    result = action.auto_apply(project, Policy())
    assert result.status == "error"
    assert result.errors
