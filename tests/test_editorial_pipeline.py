"""End-to-end tests for the editorial pipeline's two CLIs, with no LLM anywhere.

``verify_editorial.py`` and ``editorial_metrics.py`` are the parts that touch
real project directories, so these drive them against a synthetic book on
``tmp_path``: judge output goes in, drafts are answered by hand, and the metrics
report reads back what landed.

The properties worth pinning are the ones a caller would notice only after
spending tokens: that ``status`` refuses to re-bill an already-adjudicated chunk,
that ``commit`` persists through the same seam the judges use (so the freshness
ledger and badges follow), and that the precision numbers join marks to findings
by the explicit key rather than by list position.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import editorial_metrics, verify_editorial
from src.judges.scoring import finding_key
from web_ui.evaluations import append_feedback, merge_judge_result

ES_SENTENCES = [
    "Pollyanna subió al ático con su maleta.",
    "El cuarto estaba desnudo y caluroso.",
    "Miró por la ventana y sonrió.",
]
EN_SENTENCES = [
    "Pollyanna climbed to the attic with her trunk.",
    "The room was bare and hot.",
    "She looked out of the window and smiled.",
]
SPANISH = " ".join(ES_SENTENCES)
CHUNK_ID = "chapter_01_chunk_000"


@pytest.fixture
def project(tmp_path):
    """One translated chunk with a chapter alignment, a style guide and a glossary."""
    proj = tmp_path / "projects" / "editorialtest"
    (proj / "chunks").mkdir(parents=True)
    (proj / "alignments").mkdir(parents=True)

    (proj / "chunks" / f"{CHUNK_ID}.json").write_text(
        json.dumps(
            {
                "id": CHUNK_ID,
                "chapter_id": "chapter_01",
                "position": 0,
                "source_text": " ".join(EN_SENTENCES),
                "translated_text": SPANISH,
                "metadata": {
                    "char_start": 0,
                    "char_end": len(" ".join(EN_SENTENCES)),
                    "overlap_start": 0,
                    "overlap_end": 0,
                    "paragraph_count": 1,
                    "word_count": len(" ".join(EN_SENTENCES).split()),
                },
                "status": "translated",
            }
        ),
        encoding="utf-8",
    )
    (proj / "alignments" / "chapter_01.json").write_text(
        json.dumps(
            {
                "chapter_id": "chapter_01",
                "alignments": [
                    {
                        "es_idx": i,
                        "en_idx": i,
                        "es": es,
                        "en": en,
                        "similarity": 0.9,
                        "confidence": "high",
                        "chunk_id": CHUNK_ID,
                    }
                    for i, (es, en) in enumerate(zip(ES_SENTENCES, EN_SENTENCES))
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (proj / "style.json").write_text(
        json.dumps({"content": "Mexican Spanish. Keep names in English form."}),
        encoding="utf-8",
    )
    (proj / "glossary.json").write_text(
        json.dumps(
            {"terms": [{"english": "Pollyanna", "spanish": "Pollyanna", "type": "character"}]}
        ),
        encoding="utf-8",
    )
    return proj


def _candidate(**overrides):
    candidate = {
        "rule": "calque-syntax",
        "category": "NATURALNESS",
        "severity": "warning",
        "confidence": "high",
        "excerpt": "El cuarto estaba desnudo y caluroso.",
        "message": "Reads as a calque of the English.",
        "suggestion": "El cuarto era angosto y sofocante.",
        "source_check": "not_needed",
    }
    candidate.update(overrides)
    return candidate


def _persist_pass_one(project, candidates):
    """Write a pass-1 editorial result the way the judge would."""
    from src.judges.base import JudgeTarget
    from src.judges.registry import get_judge

    target = JudgeTarget(
        id=CHUNK_ID,
        target_type="chunk",
        source_text=" ".join(EN_SENTENCES),
        translated_text=SPANISH,
        context={"chapter_id": "chapter_01"},
    )
    raw = json.dumps({"findings": candidates, "summary": "s"}, ensure_ascii=False)
    result = get_judge("editorial").parse_response(target, raw, {})
    merge_judge_result(project, CHUNK_ID, "editorial", result.model_dump(mode="json"))
    return result


def _run(argv):
    return verify_editorial.main(argv)


# ---------------------------------------------------------------------------
# status


def test_status_reports_nothing_before_the_judge_has_run(project, capsys):
    assert _run(["status", "--project", str(project)]) == 0
    out = json.loads(capsys.readouterr().out)

    assert out["counts"]["chunks"] == 0
    # No evaluation file at all, which is a distinct state from one that exists
    # because the coded evaluators ran but holds no editorial verdict.
    assert out["skipped"] == {"no_evaluation": 1}


def test_status_lists_pending_candidates_and_their_source_requests(project, capsys):
    _persist_pass_one(
        project,
        [
            _candidate(),
            _candidate(
                rule="odd-connector",
                category="FIDELITY_SUSPECT",
                source_check="required",
                excerpt="Miró por la ventana y sonrió.",
            ),
        ],
    )

    assert _run(["status", "--project", str(project)]) == 0
    out = json.loads(capsys.readouterr().out)

    assert out["counts"] == {
        "chunks": 1,
        "candidates": 2,
        "source_requested": 1,
        "source_attached": 1,
    }
    assert out["pending_chunks"] == [CHUNK_ID]


def _second_chapter(project):
    """A second translated chapter, so a multi-scope call has something to union."""
    chunk_id = "chapter_02_chunk_000"
    payload = json.loads((project / "chunks" / f"{CHUNK_ID}.json").read_text(encoding="utf-8"))
    payload["id"] = chunk_id
    payload["chapter_id"] = "chapter_02"
    (project / "chunks" / f"{chunk_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return chunk_id


def _persist_pass_one_for(project, chunk_id, candidates):
    from src.judges.base import JudgeTarget
    from src.judges.registry import get_judge

    target = JudgeTarget(
        id=chunk_id,
        target_type="chunk",
        source_text=" ".join(EN_SENTENCES),
        translated_text=SPANISH,
        context={"chapter_id": chunk_id.rsplit("_chunk_", 1)[0]},
    )
    raw = json.dumps({"findings": candidates, "summary": "s"}, ensure_ascii=False)
    result = get_judge("editorial").parse_response(target, raw, {})
    merge_judge_result(project, chunk_id, "editorial", result.model_dump(mode="json"))
    return result


def test_repeated_scope_flags_union_rather_than_last_wins(project, capsys):
    """The 2026-08-27 friction: seven --scope flags staged the seventh chapter.

    The wave has taken a list since the Review tab started ticking chapters;
    argparse was the only seam that hadn't caught up.
    """
    second = _second_chapter(project)
    _persist_pass_one(project, [_candidate()])
    _persist_pass_one_for(project, second, [_candidate()])

    _run([
        "status", "--project", str(project),
        "--scope", "chapter:chapter_01", "--scope", "chapter:chapter_02",
    ])
    out = json.loads(capsys.readouterr().out)

    assert out["counts"]["chunks"] == 2
    assert sorted(out["pending_chunks"]) == [CHUNK_ID, second]
    assert out["scope"] == ["chapter:chapter_01", "chapter:chapter_02"]


def test_a_chapter_range_reaches_pass_two(project, capsys):
    second = _second_chapter(project)
    _persist_pass_one(project, [_candidate()])
    _persist_pass_one_for(project, second, [_candidate()])

    _run(["status", "--project", str(project), "--scope", "chapter:chapter_01..chapter_02"])
    out = json.loads(capsys.readouterr().out)

    assert sorted(out["pending_chunks"]) == [CHUNK_ID, second]


def test_the_default_scope_is_still_the_whole_book(project, capsys):
    """``append`` with a non-None default would silently prepend 'book'."""
    _persist_pass_one(project, [_candidate()])

    _run(["status", "--project", str(project)])
    assert json.loads(capsys.readouterr().out)["scope"] == "book"


def test_one_bad_scope_beside_a_good_one_warns_instead_of_narrowing_silently(
    project, capsys
):
    """``collect_pending`` demotes an unresolvable scope to a skip.

    Safe while the CLI passed exactly one scope; now that it can pass several,
    the demotion has to be audible or a typo quietly halves the wave.
    """
    _persist_pass_one(project, [_candidate()])

    _run([
        "status", "--project", str(project),
        "--scope", "chapter:chapter_01", "--scope", "chapter:chpater_02",
    ])
    captured = capsys.readouterr()

    assert json.loads(captured.out)["counts"]["chunks"] == 1
    assert "chapter:chpater_02" in captured.err


def test_a_lone_bad_scope_still_fails_loudly(project, capsys):
    _persist_pass_one(project, [_candidate()])
    assert _run(["status", "--project", str(project), "--scope", "chapter:chpater_02"]) == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_a_clean_chunk_is_not_pending(project, capsys):
    """No candidates means nothing to adjudicate — and nothing to bill for."""
    _persist_pass_one(project, [])

    _run(["status", "--project", str(project)])
    out = json.loads(capsys.readouterr().out)

    assert out["counts"]["chunks"] == 0
    assert out["skipped"] == {"no_candidates": 1}


# ---------------------------------------------------------------------------
# prepare / commit


def _prepare(project, capsys, *extra):
    assert _run(["prepare", "--project", str(project), *extra]) == 0
    return json.loads(capsys.readouterr().out)


def test_prepare_writes_a_prompt_a_body_and_a_manifest(project, capsys):
    _persist_pass_one(project, [_candidate()])

    out = _prepare(project, capsys)
    entry = out["entries"][0]
    prompt = (project / ".harness" / "editorial" / f"{CHUNK_ID}.verify.prompt.txt").read_text(
        encoding="utf-8"
    )

    assert out["counts"]["candidates"] == 1
    assert entry["chunk_id"] == CHUNK_ID
    assert "<candidate key=" in prompt
    assert "Mexican Spanish" in prompt
    # The cacheable split: preamble + body reconstruct the prompt byte for byte.
    preamble = (project / ".harness" / "editorial" / "preamble.txt").read_text(encoding="utf-8")
    body = (project / ".harness" / "editorial" / f"{CHUNK_ID}.verify.body.txt").read_text(
        encoding="utf-8"
    )
    assert preamble + body == prompt


def test_prepare_clears_stale_drafts_unless_asked_to_keep_them(project, capsys):
    _persist_pass_one(project, [_candidate()])
    out = _prepare(project, capsys)
    draft = project / ".harness" / "editorial" / f"{CHUNK_ID}.verify.draft.json"
    draft.write_text("{}", encoding="utf-8")

    _prepare(project, capsys)
    assert not draft.exists()

    draft.write_text("{}", encoding="utf-8")
    _prepare(project, capsys, "--keep-drafts")
    assert draft.exists()
    assert out  # the first prepare succeeded


def test_commit_applies_verdicts_and_persists_through_the_judge_seam(project, capsys):
    result = _persist_pass_one(project, [_candidate(), _candidate(rule="agreement")])
    out = _prepare(project, capsys)
    keys = [i.finding_key for i in result.issues]

    draft = project / ".harness" / "editorial" / f"{CHUNK_ID}.verify.draft.json"
    draft.write_text(
        json.dumps(
            {
                "verdicts": {
                    keys[0]: {"verdict": "RETRACT", "reason": "idiomatic after all"},
                    keys[1]: {"verdict": "CONFIRM", "reason": "genuine"},
                }
            }
        ),
        encoding="utf-8",
    )

    assert _run(["commit", "--project", str(project), "--persist"]) == 0
    committed = json.loads(capsys.readouterr().out)
    persisted = json.loads(
        (project / "evaluations" / f"{CHUNK_ID}.json").read_text(encoding="utf-8")
    )["judges"]["editorial"]

    assert out["counts"]["candidates"] == 2
    assert committed["rollup"]["retracted"] == 1
    assert committed["rollup"]["confirmed"] == 1
    assert len(persisted["issues"]) == 1
    assert persisted["metadata"]["verified"] is True
    # merge_judge_result stamps the ledger, so the badge tracks the chunk's text.
    assert "editorial" in json.loads(
        (project / "evaluations" / f"{CHUNK_ID}.json").read_text(encoding="utf-8")
    )["eval_runs"]


def test_commit_names_the_findings_the_verdicts_moved(project, capsys):
    """The rollup counts them; ``verdict_detail`` says which ones.

    2026-08-27: a wave came back 16 confirm / 1 reclassify / 2 retract and the
    only way to answer "which two?" was to open ten evaluation files by hand.
    """
    result = _persist_pass_one(project, [_candidate(), _candidate(rule="agreement")])
    _prepare(project, capsys)
    keys = [i.finding_key for i in result.issues]

    (project / ".harness" / "editorial" / f"{CHUNK_ID}.verify.draft.json").write_text(
        json.dumps(
            {
                "verdicts": {
                    keys[0]: {"verdict": "RETRACT", "reason": "idiomatic after all"},
                    keys[1]: {
                        "verdict": "RECLASSIFY",
                        "severity": "error",
                        "reason": "the English confirms a real omission",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    assert _run(["commit", "--project", str(project), "--persist"]) == 0
    out = json.loads(capsys.readouterr().out)

    detail = {d["verdict"]: d for d in out["verdict_detail"]}
    assert set(detail) == {"RETRACT", "RECLASSIFY"}
    assert detail["RETRACT"]["reason"] == "idiomatic after all"
    assert detail["RETRACT"]["chunk_id"] == CHUNK_ID
    # The severity move, which the survivor itself no longer remembers.
    assert detail["RECLASSIFY"]["severity"] == "warning"
    assert detail["RECLASSIFY"]["new_severity"] == "error"
    assert detail["RECLASSIFY"]["rule"] == "agreement"

    # And it survives on disk, where the skill's post-pass-2 veto reads it.
    persisted = json.loads(
        (project / "evaluations" / f"{CHUNK_ID}.json").read_text(encoding="utf-8")
    )["judges"]["editorial"]["metadata"]
    assert persisted["reclassified"] == 1
    assert persisted["reclassified_findings"][0]["new_severity"] == "error"


def test_commit_has_no_brief_flag(project, capsys):
    """Pass 2's results[] is nine counts per chunk — there was no flood to stop.

    Dropping it only removed the per-chunk breakdown, which ``rollup`` cannot
    reconstruct. Pass 1 keeps its ``--brief``; this CLI never needed one.
    """
    with pytest.raises(SystemExit):
        _run(["commit", "--project", str(project), "--brief"])


def test_commit_reports_a_bad_draft_instead_of_dropping_the_chunk(project, capsys):
    _persist_pass_one(project, [_candidate()])
    _prepare(project, capsys)
    (project / ".harness" / "editorial" / f"{CHUNK_ID}.verify.draft.json").write_text(
        "not json", encoding="utf-8"
    )

    _run(["commit", "--project", str(project), "--persist"])
    out = json.loads(capsys.readouterr().out)

    assert out["committed"] == 0
    assert out["failed"][0]["chunk_id"] == CHUNK_ID
    assert "re-run" in out["instructions"]


def test_commit_reports_a_missing_draft(project, capsys):
    _persist_pass_one(project, [_candidate()])
    _prepare(project, capsys)

    _run(["commit", "--project", str(project), "--persist"])
    out = json.loads(capsys.readouterr().out)

    assert out["missing"] == [CHUNK_ID]


def test_a_verified_chunk_is_skipped_until_forced(project, capsys):
    """Adjudication is not idempotent — a second pass re-decides settled retractions."""
    result = _persist_pass_one(project, [_candidate()])
    _prepare(project, capsys)
    (project / ".harness" / "editorial" / f"{CHUNK_ID}.verify.draft.json").write_text(
        json.dumps({"verdicts": {result.issues[0].finding_key: {"verdict": "CONFIRM"}}}),
        encoding="utf-8",
    )
    _run(["commit", "--project", str(project), "--persist"])
    capsys.readouterr()

    _run(["status", "--project", str(project)])
    assert json.loads(capsys.readouterr().out)["skipped"] == {"already_verified": 1}

    _run(["status", "--project", str(project), "--force"])
    assert json.loads(capsys.readouterr().out)["counts"]["chunks"] == 1


def test_commit_without_a_manifest_is_an_error_not_a_crash(project, capsys):
    assert _run(["commit", "--project", str(project)]) == 1
    assert "prepare" in json.loads(capsys.readouterr().out)["error"]


def test_last_output_mirrors_the_payload(project, capsys):
    """The Windows raya lesson: read the sidecar, never re-parse captured stdout."""
    _persist_pass_one(project, [_candidate()])
    _prepare(project, capsys)

    mirrored = json.loads(
        (project / ".harness" / "editorial" / "last_output.json").read_text(encoding="utf-8")
    )
    assert mirrored["command"] == "prepare"
    assert mirrored["counts"]["candidates"] == 1


# ---------------------------------------------------------------------------
# fanout (job construction only; the launcher itself is stubbed)


def _stub_wave(monkeypatch):
    """Replace the launcher and capture the jobs it was handed."""
    from src.harness import headless

    captured = {}

    def fake(jobs, **kwargs):
        captured["jobs"] = jobs
        captured["kwargs"] = kwargs
        for job in jobs:
            Path(job["output_path"]).write_text('{"verdicts": {}}', encoding="utf-8")
        return {"wrote": [j["id"] for j in jobs], "failed": [], "cwd": ".", "counts": {}}

    monkeypatch.setattr(headless, "run_headless_wave", fake)
    return captured


def test_fanout_hands_the_launcher_one_job_per_prepared_chunk(project, capsys, monkeypatch):
    _persist_pass_one(project, [_candidate()])
    _prepare(project, capsys)
    captured = _stub_wave(monkeypatch)

    assert _run(["fanout", "--project", str(project)]) == 0
    out = json.loads(capsys.readouterr().out)
    job = captured["jobs"][0]

    assert job["id"] == CHUNK_ID
    assert job["output_path"].endswith(".verify.draft.json")
    # The cacheable prefix goes as a system prompt, so it is not re-sent per job.
    assert job["system_prompt_file"].endswith("preamble.txt")
    assert "<candidate key=" in job["input_text"]
    assert "Mexican Spanish" not in job["input_text"]
    # The resolved profile is relayed, so the operator can see what it ran as.
    assert out["profile"]["cli"]
    assert out["profile"]["worker_model"]


def test_fanout_skips_a_chunk_that_already_has_a_draft(project, capsys, monkeypatch):
    """Resume rather than re-spend: a non-empty draft is an answered job."""
    _persist_pass_one(project, [_candidate()])
    _prepare(project, capsys, "--keep-drafts")
    (project / ".harness" / "editorial" / f"{CHUNK_ID}.verify.draft.json").write_text(
        '{"verdicts": {}}', encoding="utf-8"
    )
    captured = _stub_wave(monkeypatch)

    _run(["fanout", "--project", str(project)])
    out = json.loads(capsys.readouterr().out)

    assert out["skipped"] == [CHUNK_ID]
    assert "jobs" not in captured


def test_fanout_without_a_manifest_is_an_error(project, capsys):
    assert _run(["fanout", "--project", str(project)]) == 1
    assert "prepare" in json.loads(capsys.readouterr().out)["error"]


def test_fanout_defaults_its_concurrency_before_the_launcher_sees_it(
    project, capsys, monkeypatch
):
    """The documented bare ``fanout`` used to be a TypeError.

    ``--concurrency`` defaults to None and went straight into
    ``run_headless_wave``, which does ``if concurrency < 1``. Pass 2's first
    fan-out died in ~11s before job 1 (2026-08-26). The launcher is stubbed in
    this file, so only asserting on the value it is *handed* can catch it.
    """
    _persist_pass_one(project, [_candidate()])
    _prepare(project, capsys)
    captured = _stub_wave(monkeypatch)

    assert _run(["fanout", "--project", str(project)]) == 0
    out = json.loads(capsys.readouterr().out)

    assert captured["kwargs"]["concurrency"] == 5
    assert isinstance(captured["kwargs"]["concurrency"], int)
    assert out["concurrency"] == 5


def test_fanout_refuses_a_concurrency_below_one_without_spawning(
    project, capsys, monkeypatch
):
    _persist_pass_one(project, [_candidate()])
    _prepare(project, capsys)
    captured = _stub_wave(monkeypatch)

    assert _run(["fanout", "--project", str(project), "--concurrency", "0"]) == 1
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "error"
    assert "concurrency" in out["error"]
    assert "jobs" not in captured


def test_fanout_inherits_the_profile_prepare_consented_to(project, capsys, monkeypatch):
    """A bare ``fanout`` reproduces the wave whose number was approved."""
    _persist_pass_one(project, [_candidate()])
    _prepare(project, capsys, "--cli", "cursor", "--worker-model", "grok-4.5")
    manifest = json.loads(
        (project / ".harness" / "editorial" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["cli"] == "cursor"
    assert manifest["worker_model"] == "grok-4.5"

    captured = _stub_wave(monkeypatch)
    assert _run(["fanout", "--project", str(project)]) == 0
    out = json.loads(capsys.readouterr().out)

    assert out["profile"]["cli"] == "cursor"
    assert out["profile"]["cli_source"] == "manifest"
    assert captured["kwargs"]["model"] == "grok-4.5"


def test_a_worker_model_override_on_fanout_is_written_back(project, capsys, monkeypatch):
    _persist_pass_one(project, [_candidate()])
    _prepare(project, capsys, "--cli", "cursor")
    _stub_wave(monkeypatch)

    _run(["fanout", "--project", str(project), "--worker-model", "grok-4.6"])
    capsys.readouterr()
    manifest = json.loads(
        (project / ".harness" / "editorial" / "manifest.json").read_text(encoding="utf-8")
    )

    # The resolver may append the effort bracket; what is pinned here is that the
    # override reached the manifest, since `commit` reads it back.
    assert manifest["worker_model"].startswith("grok-4.6")


# ---------------------------------------------------------------------------
# prepare as the consent surface


def test_prepare_quotes_what_the_wave_will_cost(project, capsys):
    """Pass 2 used to start on "yes do pass 2", never on an approved number."""
    _persist_pass_one(project, [_candidate()])

    out = _prepare(project, capsys, "--cli", "cursor")
    usage = out["usage_summary"]

    assert out["effective"]["cli"] == "cursor"
    assert out["effective"]["baseline_tokens"] > 0
    # The per-job fixed overhead is the dominant term, so the headless number
    # must exceed the prompt it is built from — that gap IS the thing being
    # consented to.
    assert usage["estimated_prompt_tokens"] > 0
    assert usage["estimated_headless_tokens"] > usage["estimated_prompt_tokens"]
    assert usage["estimated_headless_tokens"] == (
        usage["estimated_prompt_tokens"] + 1 * usage["headless_baseline_tokens"]
    )
    assert usage["chunks"] == 1 and usage["candidates"] == 1
    assert usage["headless_effort_channel"] in {"argv", "model_bracket", "none"}


def test_prepare_quiet_keeps_the_gate_and_drops_the_echo(project, capsys):
    _persist_pass_one(project, [_candidate()])

    out = _prepare(project, capsys, "--quiet")

    assert "entries" not in out
    assert out["manifest"].endswith("manifest.json")
    assert out["usage_summary"]["estimated_headless_tokens"] > 0


def test_the_baseline_falls_back_to_the_pass_one_log_until_pass_two_has_rows(
    project, capsys
):
    """A first adjudication wave should not quote a probe constant.

    ``.harness/editorial/usage.jsonl`` is empty on wave 1, while the judges log
    next door holds this machine's measured rows for the same CLI. Borrow that
    rather than the 2026-08-10 probe, and say the number is borrowed.
    """
    _persist_pass_one(project, [_candidate()])
    judges_log = project / ".harness" / "judges" / "usage.jsonl"
    judges_log.parent.mkdir(parents=True, exist_ok=True)
    judges_log.write_text(
        "\n".join(
            json.dumps(
                {
                    "cli": "cursor",
                    "rc": 0,
                    "input": 20000,
                    "output": 100,
                    "prompt_sent": 1638,
                }
            )
            for _ in range(6)
        ),
        encoding="utf-8",
    )

    out = _prepare(project, capsys, "--cli", "cursor")
    source = out["usage_summary"]["headless_baseline_source"]

    assert out["usage_summary"]["headless_baseline_tokens"] == 18362
    assert "pass-1 log" in source
    assert out["effective"]["baseline_source"] == source

    # Once adjudication has logged its own jobs, the borrowed number steps aside.
    editorial_log = project / ".harness" / "editorial" / "usage.jsonl"
    editorial_log.write_text(
        "\n".join(
            json.dumps(
                {
                    "cli": "cursor",
                    "rc": 0,
                    "input": 12000,
                    "output": 100,
                    "prompt_sent": 2000,
                }
            )
            for _ in range(6)
        ),
        encoding="utf-8",
    )
    out = _prepare(project, capsys, "--cli", "cursor", "--keep-drafts")

    assert out["usage_summary"]["headless_baseline_tokens"] == 10000
    assert "pass-1 log" not in out["usage_summary"]["headless_baseline_source"]


# ---------------------------------------------------------------------------
# status --drafts, and the nested-slug lookup


def test_status_drafts_answers_whether_the_wave_has_started(project, capsys):
    """The read-only answer to "is it still going?" (2026-08-26, ~8 min lost)."""
    _persist_pass_one(project, [_candidate()])

    assert _run(["status", "--project", str(project), "--drafts"]) == 0
    wave = json.loads(capsys.readouterr().out)["wave"]
    assert wave["manifest_at"] is None
    assert "no manifest" in wave["note"]

    _prepare(project, capsys)
    _run(["status", "--project", str(project), "--drafts"])
    wave = json.loads(capsys.readouterr().out)["wave"]

    assert wave["drafts"] == {"written": 0, "pending": 1}
    assert wave["pending_ids"] == [CHUNK_ID]
    assert wave["manifest_at"]

    (project / ".harness" / "editorial" / f"{CHUNK_ID}.verify.draft.json").write_text(
        '{"verdicts": {}}', encoding="utf-8"
    )
    _run(["status", "--project", str(project), "--drafts"])
    wave = json.loads(capsys.readouterr().out)["wave"]

    assert wave["drafts"] == {"written": 1, "pending": 0}
    assert "commit" in wave["note"]


def test_a_grouped_book_answers_to_its_bare_slug(project, capsys, monkeypatch):
    """``projects/.macdonald/photogen-nycteris`` is addressed as its slug.

    Both CLIs used to check ``projects/<id>`` and stop, so the first command of
    every session on a grouped book returned "Project not found" and cost a
    round-trip of listing the grouping folders by hand.
    """
    from src.harness import state as hstate

    grouped = project.parent / ".macdonald" / "photogen-nycteris"
    grouped.parent.mkdir(parents=True, exist_ok=True)
    project.rename(grouped)
    monkeypatch.setattr(hstate, "REPO_ROOT", project.parent.parent)

    assert _run(["status", "--project", "photogen-nycteris"]) == 0
    assert json.loads(capsys.readouterr().out)["project"] == "photogen-nycteris"

    from scripts import run_judges

    assert run_judges._find_project("photogen-nycteris") == grouped


# ---------------------------------------------------------------------------
# run (API path)


def test_run_adjudicates_and_persists(project, capsys, monkeypatch):
    from src.judges import llm_io

    result = _persist_pass_one(project, [_candidate()])
    key = result.issues[0].finding_key
    seen = {}

    def fake(prompt, **kwargs):
        seen["prompt"] = prompt
        seen["cache_prefix"] = kwargs.get("cache_prefix")
        return json.dumps({"verdicts": {key: {"verdict": "CONFIRM", "reason": "real"}}})

    monkeypatch.setattr(llm_io, "call_judge", fake)

    assert _run(["run", "--project", str(project), "--persist", "--confirm"]) == 0
    out = json.loads(capsys.readouterr().out)
    persisted = json.loads(
        (project / "evaluations" / f"{CHUNK_ID}.json").read_text(encoding="utf-8")
    )["judges"]["editorial"]

    assert out["rollup"]["confirmed"] == 1
    assert out["results"][0]["persisted"] is True
    assert persisted["metadata"]["verified"] is True
    assert seen["prompt"].startswith(seen["cache_prefix"])


def test_run_refuses_to_spend_over_the_cost_limit(project, capsys, monkeypatch):
    """The gate reports without calling an LLM, exactly like run_judges.py run."""
    from src.judges import llm_io

    _persist_pass_one(project, [_candidate()])
    calls = []
    monkeypatch.setattr(llm_io, "call_judge", lambda p, **kw: calls.append(p) or "{}")

    assert _run(["run", "--project", str(project), "--cost-limit", "0"]) == 0
    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "cost_exceeded"
    assert "--confirm" in out["instructions"]
    assert calls == []


def test_run_on_an_empty_scope_calls_nothing(project, capsys, monkeypatch):
    from src.judges import llm_io

    calls = []
    monkeypatch.setattr(llm_io, "call_judge", lambda p, **kw: calls.append(p) or "{}")

    assert _run(["run", "--project", str(project), "--persist", "--confirm"]) == 0
    out = json.loads(capsys.readouterr().out)

    assert out["results"] == []
    assert calls == []


def test_run_leaves_pass_one_intact_when_the_adjudicator_returns_junk(project, capsys, monkeypatch):
    from src.judges import llm_io

    _persist_pass_one(project, [_candidate()])
    monkeypatch.setattr(llm_io, "call_judge", lambda p, **kw: "not json")

    assert _run(["run", "--project", str(project), "--persist", "--confirm"]) == 0
    out = json.loads(capsys.readouterr().out)
    persisted = json.loads(
        (project / "evaluations" / f"{CHUNK_ID}.json").read_text(encoding="utf-8")
    )["judges"]["editorial"]

    assert out["results"][0]["status"] == "parse_error"
    assert out["rollup"]["parse_errors"] == 1
    assert len(persisted["issues"]) == 1
    assert persisted["metadata"]["verified"] is False


# ---------------------------------------------------------------------------
# The reader's dismissal route


def test_the_feedback_route_records_the_stable_key_for_a_judge_finding(project, monkeypatch):
    """The load-bearing join: a reader dismissal must key on rule+excerpt.

    ``_resolve_issue_key`` looks the finding up in ``judges[<name>].issues`` and
    hands the dict to ``issue_key``. If the route fell back to positional
    matching for this judge, every dismissal would re-point at whatever occupied
    the slot after the next run — which is the failure ``finding_key`` exists to
    prevent, and it would be invisible from the UI.
    """
    import web_ui.app as app_module

    result = _persist_pass_one(project, [_candidate()])
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: project.parent)
    app_module.app.config["TESTING"] = True

    with app_module.app.test_client() as client:
        response = client.post(
            f"/api/project/{project.name}/evaluations/{CHUNK_ID}/feedback",
            json={"eval_name": "editorial", "issue_index": 0, "feedback_type": "false_positive"},
        )

    assert response.status_code == 200
    records = [
        json.loads(line)
        for line in (project / "evaluations" / "_feedback.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert records[0]["issue_key"] == result.issues[0].finding_key


# ---------------------------------------------------------------------------
# metrics


def test_metrics_report_volume_and_anchoring(project):
    _persist_pass_one(project, [_candidate()])

    report = editorial_metrics.analyse_project(project)

    assert report["volume"]["chunks_judged"] == 1
    assert report["volume"]["findings"] == 1
    assert report["volume"]["clean_chunk_pct"] == 0.0
    assert report["anchoring"]["anchored"] == 1
    assert report["anchoring"]["anchor_pct"] == 100.0


def test_metrics_count_a_clean_chunk(project):
    _persist_pass_one(project, [])

    report = editorial_metrics.analyse_project(project)

    assert report["volume"]["clean_chunk_pct"] == 100.0
    assert report["volume"]["findings"] == 0


def test_metrics_join_marks_to_findings_by_the_explicit_key(project):
    result = _persist_pass_one(project, [_candidate(), _candidate(rule="agreement")])
    append_feedback(
        project,
        CHUNK_ID,
        "editorial",
        0,
        feedback_type="resolved",
        key=result.issues[0].finding_key,
    )
    append_feedback(
        project,
        CHUNK_ID,
        "editorial",
        1,
        feedback_type="false_positive",
        key=result.issues[1].finding_key,
    )

    report = editorial_metrics.analyse_project(project)

    assert report["precision"]["labelled"] == 2
    assert report["precision"]["accept_pct"] == 50.0
    assert report["by_rule"]["calque-syntax"]["resolved"] == 1
    assert report["by_rule"]["agreement"]["false_positive"] == 1


def test_a_mark_survives_the_judge_rewording_its_message(project):
    """The whole reason for finding_key: a re-judge must not orphan the dismissal."""
    result = _persist_pass_one(project, [_candidate()])
    append_feedback(
        project,
        CHUNK_ID,
        "editorial",
        0,
        feedback_type="false_positive",
        key=result.issues[0].finding_key,
    )

    _persist_pass_one(project, [_candidate(message="The English word order survives here.")])
    report = editorial_metrics.analyse_project(project)

    assert report["precision"]["false_positive"] == 1


def test_examples_bank_separates_accepted_from_dismissed(project):
    result = _persist_pass_one(project, [_candidate(), _candidate(rule="agreement")])
    append_feedback(
        project, CHUNK_ID, "editorial", 0,
        feedback_type="resolved", key=result.issues[0].finding_key,
    )
    append_feedback(
        project, CHUNK_ID, "editorial", 1,
        feedback_type="false_positive", key=result.issues[1].finding_key,
    )

    report = editorial_metrics.analyse_project(project)
    text = editorial_metrics.render_examples(report["_examples"], 8)

    assert "BELOW THE THRESHOLD" in text
    assert "AT OR ABOVE THE THRESHOLD" in text
    assert text.index("BELOW THE THRESHOLD") < text.index("AT OR ABOVE THE THRESHOLD")


def test_examples_bank_is_empty_without_marks(project):
    _persist_pass_one(project, [_candidate()])
    report = editorial_metrics.analyse_project(project)

    assert editorial_metrics.render_examples(report["_examples"], 8) == ""


def test_the_bank_round_trips_into_the_judge_prompt(project):
    """Stage 3's loop closes: written examples reach the next run's cached prefix."""
    from src.judges.base import JudgeTarget
    from src.judges.context import build_judge_context
    from src.judges.registry import get_judge

    (project / "editorial_examples.txt").write_text(
        "BELOW THE THRESHOLD\n- [NATURALNESS/calque-syntax] \"una frase\"", encoding="utf-8"
    )
    ctx, error = build_judge_context(project, ["editorial"], None, None)
    target = JudgeTarget(
        id=CHUNK_ID, target_type="chunk", source_text="x",
        translated_text=SPANISH, context={"chapter_id": "chapter_01"},
    )
    prefix, _ = get_judge("editorial").build_prompt_parts(target, ctx)

    assert error is None
    assert "BELOW THE THRESHOLD" in prefix


def test_adjudication_metrics_come_from_the_verified_metadata(project, capsys):
    result = _persist_pass_one(project, [_candidate(), _candidate(rule="agreement")])
    _prepare(project, capsys)
    keys = [i.finding_key for i in result.issues]
    (project / ".harness" / "editorial" / f"{CHUNK_ID}.verify.draft.json").write_text(
        json.dumps(
            {
                "verdicts": {
                    keys[0]: {"verdict": "RETRACT", "reason": "fine"},
                    keys[1]: {"verdict": "CONFIRM", "reason": "real"},
                }
            }
        ),
        encoding="utf-8",
    )
    _run(["commit", "--project", str(project), "--persist"])
    capsys.readouterr()

    report = editorial_metrics.analyse_project(project)

    assert report["adjudication"]["verified_chunks"] == 1
    assert report["adjudication"]["adjudicated"] == 2
    assert report["adjudication"]["retract_pct"] == 50.0


def test_finding_key_helper_matches_what_metrics_join_on(project):
    result = _persist_pass_one(project, [_candidate()])

    assert result.issues[0].finding_key == finding_key(
        "calque-syntax", "El cuarto estaba desnudo y caluroso."
    )


# ---------------------------------------------------------------------------
# Adjudicating prose that has moved


def _edit_translation(project, text, chunk_id=CHUNK_ID):
    """Rewrite a chunk's Spanish, the way a reader correction or `apply` would."""
    path = project / "chunks" / f"{chunk_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["translated_text"] = text
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_a_chunk_edited_after_pass_one_is_not_adjudicated(project, capsys):
    """Pass 2 never reads the prose, but persisting it re-stamps the ledger.

    So adjudicating a chunk whose Spanish moved after pass 1 cleared ``stale``
    and wrote a ``text_sha`` matching text no judge had seen — findings still
    anchored to the old sentence, badge and anchoring check both reading fresh.
    Edited text needs a new pass 1, not a second opinion on the old one.
    """
    _persist_pass_one(project, [_candidate()])
    _edit_translation(project, "El cuarto era angosto y sofocante. " + SPANISH)

    assert _run(["status", "--project", str(project)]) == 0
    out = json.loads(capsys.readouterr().out)

    assert out["pending_chunks"] == []
    assert out["skipped"] == {"stale": 1}


def test_force_does_not_reopen_a_stale_chunk(project, capsys):
    """``force`` means "re-decide what pass 2 settled", not "judge moved prose"."""
    _persist_pass_one(project, [_candidate()])
    _edit_translation(project, "Otra cosa por completo distinta.")

    assert _run(["status", "--project", str(project), "--force"]) == 0
    out = json.loads(capsys.readouterr().out)

    assert out["pending_chunks"] == []
    assert out["skipped"] == {"stale": 1}


def test_an_evaluation_with_no_ledger_is_still_adjudicated(project, capsys):
    """Only hash and flag staleness gate the wave; mtime staleness is a suspicion.

    An evaluation written before ``eval_runs`` existed has no sha to compare, and
    ``evaluator_freshness_detail`` falls back to a timestamp rule that a git
    checkout or a byte-identical rewrite trips. Refusing to adjudicate a legacy
    book on that costs more than the laundering it would prevent.
    """
    _persist_pass_one(project, [_candidate()])
    path = project / "evaluations" / f"{CHUNK_ID}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("eval_runs", None)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _edit_translation(project, "Texto reescrito por completo.")

    assert _run(["status", "--project", str(project)]) == 0
    out = json.loads(capsys.readouterr().out)

    assert out["pending_chunks"] == [CHUNK_ID]


def test_commit_lands_the_other_drafts_when_one_chunk_has_vanished(project, capsys):
    """A chunk re-chunked between ``prepare`` and ``commit`` abandoned the wave.

    ``build_targets`` raised straight out of the loop, so every draft after it in
    the manifest went uncommitted and the CLI exited 1 having landed nothing —
    a fan-out already paid for, discarded because one chunk moved. The verdicts
    answer ``metadata.candidates``, which is still on disk, so they still land.
    """
    second = _second_chapter(project)
    first_result = _persist_pass_one(project, [_candidate()])
    second_result = _persist_pass_one_for(project, second, [_candidate()])
    _prepare(project, capsys)

    drafts = project / ".harness" / "editorial"
    for chunk_id, result in ((CHUNK_ID, first_result), (second, second_result)):
        (drafts / f"{chunk_id}.verify.draft.json").write_text(
            json.dumps({"verdicts": {result.issues[0].finding_key: {"verdict": "CONFIRM"}}}),
            encoding="utf-8",
        )
    (project / "chunks" / f"{CHUNK_ID}.json").unlink()

    assert _run(["commit", "--project", str(project), "--persist"]) == 0
    out = json.loads(capsys.readouterr().out)

    assert out["committed"] == 2
    assert out["failed"] == []
    assert sorted(r["chunk_id"] for r in out["results"]) == sorted([CHUNK_ID, second])
