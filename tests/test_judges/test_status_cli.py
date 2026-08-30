"""Tests for ``run_judges.py status`` — the judge-coverage report.

The command exists because "an evaluation file exists" was being read as "this
chapter was judged". An ``evaluations/<chunk>.json`` file is written the moment
the *deterministic* evaluators run, so a chapter can carry one while holding
``judges: {}`` — which is how eight of ten fabre2 chapters looked on 2026-08-11
while being reported as already reviewed. The first test pins exactly that.

The rest pin the freshness ladder as the CLI reports it (hash vs. mtime vs. the
apply flag), the scope filter, and parity with the dashboard's Review tab, which
shares the same primitives in ``web_ui.evaluations``.
"""

from __future__ import annotations

import json

import pytest

from src.judges.status import StatusScopeError, build_status
from web_ui.evaluations import (
    JUDGE_STATUS_GROUPS,
    merge_judge_result,
    save_chunk_evaluation,
)


@pytest.fixture
def project(tmp_path):
    """Three one-chunk chapters, all translated, nothing evaluated yet."""
    proj = tmp_path / "projects" / "statustest"
    (proj / "chunks").mkdir(parents=True)
    (proj / "chapters").mkdir(parents=True)
    for n in (1, 2, 3):
        chapter_id = f"chapter_{n:02d}"
        (proj / "chapters" / f"{chapter_id}.txt").write_text("El gato.", encoding="utf-8")
        write_chunk(proj, f"{chapter_id}_chunk_000", "El gato negro.", chapter_id=chapter_id)
    return proj


def write_chunk(proj, chunk_id: str, translated: str, chapter_id: str = "chapter_01"):
    path = proj / "chunks" / f"{chunk_id}.json"
    path.write_text(json.dumps({
        "id": chunk_id, "chapter_id": chapter_id, "position": 0,
        "source_text": "The black cat.", "translated_text": translated,
    }), encoding="utf-8")
    return path


def coded_run(proj, chunk_id: str):
    """Run the deterministic evaluators — and only those — on a chunk."""
    from src.models import EvalResult

    results = [
        EvalResult(eval_name=name, eval_version="1.0.0", target_id=chunk_id,
                   target_type="chunk", passed=True, score=1.0, issues=[])
        for name in ("grammar", "blacklist")
    ]
    save_chunk_evaluation(proj, chunk_id, results, {}, [])


def judge_run(proj, chunk_id: str, judge: str = "dialogue", **metadata):
    merge_judge_result(proj, chunk_id, judge, {
        "eval_name": judge,
        "eval_version": "1.3.0",
        "target_id": chunk_id,
        "target_type": "chunk",
        "passed": True,
        "score": 1.0,
        "issues": [],
        "executed_at": "2026-07-21T10:00:00",
        "metadata": {"backend": "subagent", "worker_model": "grok-4.5", **metadata},
    })


# ── The trap this command exists for ─────────────────────────────────────────


def test_deterministic_only_evaluation_is_not_a_judge_verdict(project):
    """The fabre2 shape: an evaluation file with `judges: {}`.

    `coded` reads done; the judges must read not_run, and the caller must be
    told in words that the file's existence proved nothing about them.
    """
    for n in (1, 2, 3):
        coded_run(project, f"chapter_{n:02d}_chunk_000")

    out = build_status(project)

    assert out["judges"]["coded"]["state"] == "done"
    assert out["judges"]["dialogue"]["state"] == "not_run"
    assert out["judges"]["address"]["state"] == "not_run"
    assert out["judges"]["dialogue"]["chapters"]["not_run"] == [
        "chapter_01", "chapter_02", "chapter_03",
    ]
    assert any("not a judge verdict" in w for w in out["warnings"])


def test_nothing_evaluated_reads_not_run_without_the_trap_warning(project):
    """A chapter with no evaluation file at all is a gap, not a trap."""
    out = build_status(project)

    # Derived from the registry, not spelled out: the group list grows whenever a
    # judge is added, and this assertion is about the *state* of every group, not
    # about which judges happen to exist today.
    assert {g: e["state"] for g, e in out["judges"].items()} == {
        group: "not_run" for group in JUDGE_STATUS_GROUPS
    }
    assert out["warnings"] is None
    assert out["needs"]["dialogue"] == {"not_run": 3}


def test_judged_chapters_are_separated_from_unjudged_ones(project):
    """The 2026-08-11 scope question: which of these ten are already done?"""
    for n in (1, 2, 3):
        coded_run(project, f"chapter_{n:02d}_chunk_000")
    judge_run(project, "chapter_02_chunk_000")

    dialogue = build_status(project)["judges"]["dialogue"]

    assert dialogue["state"] == "partial"
    assert dialogue["chapters"]["done"] == ["chapter_02"]
    assert dialogue["chapters"]["not_run"] == ["chapter_01", "chapter_03"]
    assert dialogue["chunks"] == {"fresh": 1, "stale": 0, "missing": 2}


# ── Freshness, and the evidence behind it ────────────────────────────────────


def test_out_of_band_edit_goes_stale_on_the_content_hash(project):
    """No cooperation from the editing path — the hash alone notices."""
    judge_run(project, "chapter_01_chunk_000")
    write_chunk(project, "chapter_01_chunk_000", "El perro grande y viejo.")

    out = build_status(project, judges=["dialogue"], detail=True)

    assert out["judges"]["dialogue"]["chapters"]["stale"] == ["chapter_01"]
    assert out["judges"]["dialogue"]["stale_basis"] == {"hash": 1}
    row = next(r for r in out["chapters"] if r["id"] == "chapter_01")
    assert row["dialogue"]["basis"] == ["hash"]
    assert row["dialogue"]["chunk_ids"] == ["chapter_01_chunk_000"]
    # A hash verdict is proof, so it must not raise the mtime caveat.
    assert out["warnings"] is None


def test_legacy_evaluation_without_a_ledger_falls_back_to_mtime(project):
    """Most files on disk predate ``eval_runs``; their staleness is a suspicion.

    The report must say so — an operator told "re-judge these nine chapters"
    deserves to know the evidence is a timestamp, not a content comparison.
    """
    judge_run(project, "chapter_01_chunk_000")

    # Strip the ledger, leaving the pre-eval_runs shape, and make the chunk
    # newer than the recorded run.
    eval_path = project / "evaluations" / "chapter_01_chunk_000.json"
    payload = json.loads(eval_path.read_text(encoding="utf-8"))
    payload.pop("eval_runs", None)
    payload["judges_at"] = "2020-01-01T00:00:00"
    eval_path.write_text(json.dumps(payload), encoding="utf-8")

    out = build_status(project, judges=["dialogue"], detail=True)

    assert out["judges"]["dialogue"]["chapters"]["stale"] == ["chapter_01"]
    assert out["judges"]["dialogue"]["stale_basis"] == {"mtime": 1}
    assert any("mtime" in w and "suspicion" in w for w in out["warnings"])


def test_apply_stale_flag_is_reported_with_its_reason(project):
    """A judge apply stale-stamps what it edits; status explains the stamp."""
    from web_ui.evaluations import mark_evaluation_stale

    judge_run(project, "chapter_01_chunk_000")
    mark_evaluation_stale(
        project, "chapter_01_chunk_000",
        "translated_text edited by judge-review apply (address)",
    )

    out = build_status(project, judges=["dialogue"], detail=True)
    row = next(r for r in out["chapters"] if r["id"] == "chapter_01")

    assert out["judges"]["dialogue"]["stale_basis"] == {"flag": 1}
    assert row["dialogue"]["basis"] == ["flag"]
    assert row["dialogue"]["stale_reason"] == [
        "translated_text edited by judge-review apply (address)"
    ]


def test_a_stale_group_reports_the_stale_members_evidence_not_a_fresh_ones(project):
    """A group goes stale on one member while the others stay fresh.

    ``coded`` covers seven evaluators, and the *fresh* ones carry a basis too —
    they were compared and passed. Reading the first basis in the group would
    quote a fresh evaluator's evidence for a stale verdict. Here ``blacklist``
    is fresh by hash and comes first in the group's name order, while
    ``grammar`` is the stale one and its evidence is only a timestamp: the two
    bases differ, so a wrong pick cannot pass.
    """
    coded_run(project, "chapter_01_chunk_000")
    eval_path = project / "evaluations" / "chapter_01_chunk_000.json"
    payload = json.loads(eval_path.read_text(encoding="utf-8"))
    # blacklist keeps its ledger entry (hash still matches -> fresh, basis
    # "hash"); grammar drops out of the ledger and back to the legacy timestamp
    # rule, with a stamp far older than the chunk file -> stale, basis "mtime".
    payload["eval_runs"].pop("grammar")
    payload["evaluated_at"] = "2020-01-01T00:00:00"
    eval_path.write_text(json.dumps(payload), encoding="utf-8")

    out = build_status(project, judges=["coded"], detail=True)
    row = next(r for r in out["chapters"] if r["id"] == "chapter_01")

    assert row["coded"]["state"] == "stale"
    assert row["coded"]["basis"] == ["mtime"]
    assert out["judges"]["coded"]["stale_basis"] == {"mtime": 1}


def test_stale_basis_counts_each_stale_chunk_once_on_its_strongest_evidence(project):
    """``stale_basis`` must sum to ``chunks.stale`` — it is a cross-checkable split.

    One ``coded`` chunk can hold two stale evaluators resting on different
    evidence (here: a hash mismatch and a bare timestamp). Tallying both would
    report more stale verdicts than there are stale chunks.
    """
    coded_run(project, "chapter_01_chunk_000")
    eval_path = project / "evaluations" / "chapter_01_chunk_000.json"
    payload = json.loads(eval_path.read_text(encoding="utf-8"))
    payload["eval_runs"]["blacklist"] = {"at": "2026-01-01T00:00:00", "text_sha": "deadbeef"}
    payload["eval_runs"].pop("grammar")
    payload["evaluated_at"] = "2020-01-01T00:00:00"
    eval_path.write_text(json.dumps(payload), encoding="utf-8")

    out = build_status(project, judges=["coded"], detail=True)
    coded = out["judges"]["coded"]
    row = next(r for r in out["chapters"] if r["id"] == "chapter_01")

    assert coded["chunks"]["stale"] == 1
    assert sum(coded["stale_basis"].values()) == coded["chunks"]["stale"]
    assert coded["stale_basis"] == {"hash": 1}          # proof outranks suspicion
    assert row["coded"]["basis"] == ["hash", "mtime"]   # the detail still names both


def test_untranslated_chunks_are_counted_but_never_owed(project):
    write_chunk(project, "chapter_01_chunk_001", "", chapter_id="chapter_01")

    out = build_status(project, judges=["dialogue"], detail=True)

    assert out["totals"]["chunks"] == 4
    assert out["totals"]["translated"] == 3
    row = next(r for r in out["chapters"] if r["id"] == "chapter_01")
    assert (row["chunks"], row["translated"]) == (2, 1)
    assert out["judges"]["dialogue"]["chunks"]["missing"] == 3


def test_partial_chapter_names_the_chunks_that_still_owe_work(project):
    write_chunk(project, "chapter_01_chunk_001", "El perro.", chapter_id="chapter_01")
    judge_run(project, "chapter_01_chunk_000")

    out = build_status(project, judges=["dialogue"], detail=True)
    row = next(r for r in out["chapters"] if r["id"] == "chapter_01")

    assert row["dialogue"]["state"] == "partial"
    assert row["dialogue"]["chunk_ids"] == ["chapter_01_chunk_001"]


# ── Provenance ───────────────────────────────────────────────────────────────


def test_detail_carries_who_ran_the_judge_and_when(project):
    """`executed_at` / `worker_model` live inside judges[<name>][.metadata]."""
    judge_run(project, "chapter_01_chunk_000")

    out = build_status(project, judges=["dialogue"], detail=True)
    row = next(r for r in out["chapters"] if r["id"] == "chapter_01")

    assert row["dialogue"] == {
        "state": "done",
        "at": "2026-07-21T10:00:00",
        "worker_model": "grok-4.5",
        "backend": "subagent",
    }
    assert out["judges"]["dialogue"]["last_run"] == "2026-07-21T10:00:00"
    assert out["judges"]["dialogue"]["worker_models"] == ["grok-4.5"]


def test_mixed_worker_models_across_a_chapter_are_all_reported(project):
    """Two waves on one chapter must not silently report only the first model."""
    write_chunk(project, "chapter_01_chunk_001", "El perro.", chapter_id="chapter_01")
    judge_run(project, "chapter_01_chunk_000", worker_model="grok-4.5")
    judge_run(project, "chapter_01_chunk_001", worker_model="sonnet")

    out = build_status(project, judges=["dialogue"], detail=True)
    row = next(r for r in out["chapters"] if r["id"] == "chapter_01")

    assert row["dialogue"]["worker_model"] == ["grok-4.5", "sonnet"]
    assert sorted(out["judges"]["dialogue"]["worker_models"]) == ["grok-4.5", "sonnet"]


# ── Scope filtering ──────────────────────────────────────────────────────────


def test_scope_filters_by_chapter_and_by_inclusive_range(project):
    assert [c["id"] for c in build_status(
        project, scopes=["chapter:chapter_02"], detail=True)["chapters"]] == ["chapter_02"]

    ranged = build_status(project, scopes=["chapter:chapter_01..chapter_02"], detail=True)
    assert [c["id"] for c in ranged["chapters"]] == ["chapter_01", "chapter_02"]
    assert ranged["totals"] == {
        "chapters": 3, "chapters_in_scope": 2, "chunks": 2, "translated": 2,
    }


def test_scope_accepts_a_chunk_id_and_the_book_keyword(project):
    assert build_status(
        project, scopes=["chunk:chapter_03_chunk_000"], detail=True
    )["chapters"][0]["id"] == "chapter_03"
    assert build_status(project, scopes=["book"])["totals"]["chapters_in_scope"] == 3


def test_repeated_scopes_union_and_keep_reading_order(project):
    out = build_status(
        project, scopes=["chapter:chapter_03", "chapter:chapter_01"], detail=True
    )
    assert [c["id"] for c in out["chapters"]] == ["chapter_01", "chapter_03"]


def test_a_chapter_with_no_translated_chunks_is_reported_not_an_error(project):
    """The opposite of ``build_targets``, which raises here — by design.

    A status report whose job is to show gaps cannot refuse to describe one.
    """
    (project / "chapters" / "chapter_04.txt").write_text("x", encoding="utf-8")
    write_chunk(project, "chapter_04_chunk_000", "", chapter_id="chapter_04")

    out = build_status(project, scopes=["chapter:chapter_04"], detail=True)

    assert out["chapters"][0]["translated"] == 0
    assert out["judges"]["dialogue"]["state"] == "not_run"
    assert out["next"].startswith("nothing translated in scope yet")


def test_bad_scopes_and_judges_raise_with_an_actionable_message(project):
    with pytest.raises(StatusScopeError, match="Malformed scope"):
        build_status(project, scopes=["chapter_01"])
    with pytest.raises(StatusScopeError, match="Range endpoint"):
        build_status(project, scopes=["chapter:chapter_01..chapter_99"])
    with pytest.raises(StatusScopeError, match="No chapter 'chapter_99'"):
        build_status(project, scopes=["chapter:chapter_99"])
    with pytest.raises(StatusScopeError, match="Unknown scope kind"):
        build_status(project, scopes=["sentences:chapter_01"])
    with pytest.raises(StatusScopeError, match="Unknown judge"):
        build_status(project, judges=["nosuch"])


# ── Group selection ──────────────────────────────────────────────────────────


def test_a_registered_judge_with_no_dashboard_cell_still_appears(project, monkeypatch):
    """JUDGE_STATUS_GROUPS is the Review tab's cell list, not the judge list."""
    import src.judges.status as status_module

    monkeypatch.setattr(
        status_module, "available_judges", lambda: ["dialogue", "address", "brandnew"]
    )
    out = build_status(project)

    assert "brandnew" in out["judges"]
    assert out["judges"]["brandnew"]["state"] == "not_run"


def test_judge_selection_narrows_the_report_and_keeps_the_given_order(project):
    out = build_status(project, judges=["address", "coded"])
    assert list(out["judges"]) == ["address", "coded"]


def test_narrowing_to_one_judge_still_warns_about_deterministic_only_files(project):
    """Whether a chapter is the trap shape is a fact about the chapter.

    Asking only about dialogue must not hide that these chapters have an
    evaluation file and no judge verdict — that is precisely the reading the
    command exists to correct.
    """
    for n in (1, 2, 3):
        coded_run(project, f"chapter_{n:02d}_chunk_000")

    out = build_status(project, judges=["dialogue"])

    assert list(out["judges"]) == ["dialogue"]
    assert any("not a judge verdict" in w for w in out["warnings"])


def test_a_chapter_judged_by_another_judge_is_not_reported_as_the_trap(project):
    """Address-judged but not dialogue-judged is a gap, not a misleading file."""
    for n in (1, 2, 3):
        coded_run(project, f"chapter_{n:02d}_chunk_000")
        judge_run(project, f"chapter_{n:02d}_chunk_000", judge="address")

    out = build_status(project, judges=["dialogue"])

    assert out["judges"]["dialogue"]["state"] == "not_run"
    assert out["warnings"] is None


# ── Parity with the dashboard ────────────────────────────────────────────────


def test_cli_and_review_tab_agree_on_every_chapter_state(project, monkeypatch):
    """Both compose the same primitives; this is the guard on that staying true."""
    import web_ui.app as app_module

    coded_run(project, "chapter_01_chunk_000")
    judge_run(project, "chapter_01_chunk_000")
    judge_run(project, "chapter_02_chunk_000")
    write_chunk(project, "chapter_02_chunk_000", "Texto reescrito.", chapter_id="chapter_02")

    app_module._NESTED_PROJECT_CACHE.clear()
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: project.parent)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        body = client.get("/api/project/statustest/review-status").get_json()

    cli = build_status(project)
    for chapter in body["chapters"]:
        for group in ("coded", "dialogue", "address"):
            expected = chapter["judges"][group]["state"]
            buckets = cli["judges"][group]["chapters"]
            actual = next(s for s in buckets if chapter["id"] in buckets[s])
            assert actual == expected, f"{chapter['id']}/{group}"


def test_tagged_scope_is_rejected_with_a_prepare_pointer(project, capsys):
    """Tags are a prepare feature; status must not parse 'dialogue' as a kind."""
    run_judges = pytest.importorskip("scripts.run_judges")
    rc = run_judges.main(
        ["status", "--project", str(project), "--scope", "dialogue:book"]
    )
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert "prepare" in out["error"]
    assert not (project / ".harness" / "judges").exists()


def test_drafts_reports_whether_a_prepared_wave_has_started(project, capsys):
    """The read-only "is it still going?" answer.

    On 2026-08-26 an announced fan-out had never been launched and it took eight
    minutes to notice. One prepared entry with no draft is the signal; a grouped
    entry counts once, since ``batch_id`` is the unit ``fanout`` takes.
    """
    run_judges = pytest.importorskip("scripts.run_judges")
    jdir = project / ".harness" / "judges"
    jdir.mkdir(parents=True)
    (jdir / "chapter_02.draft.json").write_text('{"issues": []}', encoding="utf-8")
    (jdir / "manifest.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "target_id": "chapter_01_chunk_000",
                        "judge": "dialogue",
                        "draft_path": str(jdir / "chapter_01.draft.json"),
                    },
                    {
                        "batch_id": "batch_000",
                        "judge": "dialogue",
                        "draft_path": str(jdir / "chapter_02.draft.json"),
                        "members": [
                            {"target_id": "chapter_02_chunk_000"},
                            {"target_id": "chapter_03_chunk_000"},
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    assert run_judges.main(["status", "--project", str(project), "--drafts"]) == 0
    wave = json.loads(capsys.readouterr().out)["wave"]

    assert wave["drafts"] == {"written": 1, "pending": 1}
    assert wave["pending_ids"] == ["chapter_01_chunk_000"]
    assert wave["manifest_at"]


def test_drafts_says_so_when_nothing_was_ever_prepared(project, capsys):
    run_judges = pytest.importorskip("scripts.run_judges")

    assert run_judges.main(["status", "--project", str(project), "--drafts"]) == 0
    wave = json.loads(capsys.readouterr().out)["wave"]

    assert wave["manifest_at"] is None
    assert "prepare" in wave["note"]
