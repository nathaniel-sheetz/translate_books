"""Tests for the Review tab's background runs (run-coded / run-judges + jobs).

The invariants worth pinning here are the ones that cost money or leave the UI
lying: the cost gate must not call an LLM, a missing address map must fail with
the fix-up instructions rather than a silent empty judgement, a dead job must
still emit its terminal event, and two runs must not interleave writes into the
same evaluations/*.json.
"""

from __future__ import annotations

import itertools
import json
import threading

import pytest

from web_ui import jobs
from web_ui.app import app


@pytest.fixture(autouse=True)
def clean_jobs():
    jobs.reset_for_tests()
    yield
    jobs.reset_for_tests()


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _write_chunk(proj, chunk_id: str, translated: str, *, position: int = 0):
    """A chunk file complete enough for ``load_chunk`` to validate."""
    source = "The black cat."
    (proj / "chunks" / f"{chunk_id}.json").write_text(json.dumps({
        "id": chunk_id, "chapter_id": "chapter_01", "position": position,
        "source_text": source, "translated_text": translated,
        "status": "translated",
        "metadata": {
            "char_start": 0, "char_end": len(source), "overlap_start": 0,
            "overlap_end": 0, "paragraph_count": 1, "word_count": 3,
        },
    }), encoding="utf-8")


@pytest.fixture
def project(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    proj = projects_dir / "jobproj"
    (proj / "chunks").mkdir(parents=True)
    (proj / "chapters").mkdir(parents=True)
    (proj / "chapters" / "chapter_01.txt").write_text("x", encoding="utf-8")
    for i, text in enumerate(["El gato negro.", "El perro grande."]):
        _write_chunk(proj, f"chapter_01_chunk_{i:03d}", text, position=i)

    import web_ui.app as app_module
    app_module._NESTED_PROJECT_CACHE.clear()
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj


def drain(client, job_id, project_id="jobproj"):
    """Consume a job's SSE stream and return the parsed events."""
    resp = client.get(f"/api/project/{project_id}/jobs/{job_id}/sse")
    events = []
    for raw in resp.get_data(as_text=True).split("\n\n"):
        for line in raw.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


# ── The generic job runner ───────────────────────────────────────────────────


def test_a_job_that_raises_still_emits_a_terminal_complete():
    """A JVM-backed evaluator has taken this process down before; a dead thread
    must never leave the progress bar spinning with nothing to explain it."""
    def body(emit):
        emit("chunk_done", index=1)
        raise RuntimeError("LanguageTool died")

    job_id = jobs.start_job("p", "review-coded", body)
    jobs.get_job(job_id)["thread"].join(timeout=5)

    events = [json.loads(f.split("data: ", 1)[1]) for f in jobs.stream_job(job_id)
              if "data: " in f]
    assert events[-1]["event"] == "complete"
    assert "LanguageTool died" in events[-1]["fatal"]


def test_second_job_for_the_same_project_is_refused():
    gate = threading.Event()

    def blocking(emit):
        gate.wait(timeout=5)

    first = jobs.start_job("p", "review-coded", blocking)
    with pytest.raises(jobs.JobConflict) as exc:
        jobs.start_job("p", "review-judges", lambda emit: None)
    assert exc.value.job_id == first
    gate.set()
    jobs.get_job(first)["thread"].join(timeout=5)

    # Once it finishes the lock is released.
    jobs.start_job("p", "review-coded", lambda emit: None)


def test_a_different_project_is_not_blocked():
    gate = threading.Event()

    def blocking(emit):
        gate.wait(timeout=5)

    jobs.start_job("p1", "review-coded", blocking)
    jobs.start_job("p2", "review-coded", lambda emit: None)
    gate.set()


def test_a_body_returning_a_non_mapping_still_completes():
    job_id = jobs.start_job("p", "review-coded", lambda emit: "not a dict")
    jobs.get_job(job_id)["thread"].join(timeout=5)
    events = [json.loads(f.split("data: ", 1)[1]) for f in jobs.stream_job(job_id)
              if "data: " in f]
    assert events == [{"event": "complete"}]


def test_a_finished_jobs_stream_can_be_consumed_twice():
    """The queue hands each payload to exactly one consumer, so once the first
    stream drained the terminal frame a second tab (or a reconnect) used to sit
    on `: keepalive` forever — modal stuck on "Starting…", one pinned worker
    thread per hung stream."""
    job_id = jobs.start_job("p", "review-coded", lambda emit: {"ran": 1})
    jobs.get_job(job_id)["thread"].join(timeout=5)

    first = [json.loads(f.split("data: ", 1)[1]) for f in jobs.stream_job(job_id)
             if "data: " in f]
    assert first == [{"event": "complete", "ran": 1}]

    # Bounded: a regression yields keepalives forever, and islice makes that a
    # failed assertion rather than a hung suite.
    frames = list(itertools.islice(jobs.stream_job(job_id, keepalive_seconds=0.01), 5))
    replayed = [json.loads(f.split("data: ", 1)[1]) for f in frames if "data: " in f]
    assert replayed == [{"event": "complete", "ran": 1}]


# ── run-coded ────────────────────────────────────────────────────────────────


def test_run_coded_persists_every_chunk(client, project, monkeypatch):
    import web_ui.app as app_module

    seen = []

    def fake_eval(project_dir, chunk, **kwargs):
        seen.append((chunk.id, kwargs.get("enabled_evals")))
        from web_ui.evaluations import save_chunk_evaluation
        save_chunk_evaluation(project_dir, chunk.id, [], {}, [],
                              enabled_evals=["grammar"])
        return {}

    monkeypatch.setattr(app_module, "evaluate_and_persist_chunk", fake_eval)

    rv = client.post("/api/project/jobproj/review/run-coded",
                     json={"chapter_ids": ["chapter_01"]})
    assert rv.status_code == 200
    job_id = rv.get_json()["job_id"]
    events = drain(client, job_id)

    assert [c for c, _ in seen] == ["chapter_01_chunk_000", "chapter_01_chunk_001"]
    assert events[-1] == {"event": "complete", "evaluated": 2, "total": 2,
                          "error_count": 0, "errors": []}
    assert (project / "evaluations" / "chapter_01_chunk_000.json").exists()


def test_run_coded_reports_a_failing_chunk_without_sinking_the_run(client, project, monkeypatch):
    import web_ui.app as app_module

    def flaky(project_dir, chunk, **kwargs):
        if chunk.id.endswith("000"):
            raise RuntimeError("boom")
        return {}

    monkeypatch.setattr(app_module, "evaluate_and_persist_chunk", flaky)

    job_id = client.post("/api/project/jobproj/review/run-coded",
                         json={"chapter_ids": None}).get_json()["job_id"]
    events = drain(client, job_id)
    assert events[-1]["evaluated"] == 1
    assert events[-1]["error_count"] == 1
    assert "boom" in events[-1]["errors"][0]


def test_run_coded_rejects_unknown_evaluator(client, project):
    rv = client.post("/api/project/jobproj/review/run-coded",
                     json={"evaluators": ["nope"]})
    assert rv.status_code == 400
    assert "nope" in rv.get_json()["error"]


def test_run_coded_rejects_bad_chapter_id(client, project):
    rv = client.post("/api/project/jobproj/review/run-coded",
                     json={"chapter_ids": ["../etc"]})
    assert rv.status_code == 400


def test_run_coded_with_nothing_translated_is_a_400(client, project):
    for path in (project / "chunks").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["translated_text"] = ""
        path.write_text(json.dumps(data), encoding="utf-8")
    rv = client.post("/api/project/jobproj/review/run-coded", json={})
    assert rv.status_code == 400
    assert "No translated chunks" in rv.get_json()["error"]


def test_concurrent_review_job_is_a_409(client, project, monkeypatch):
    gate = threading.Event()
    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "evaluate_and_persist_chunk",
                        lambda *a, **k: gate.wait(timeout=5))

    first = client.post("/api/project/jobproj/review/run-coded", json={})
    assert first.status_code == 200
    second = client.post("/api/project/jobproj/review/run-coded", json={})
    assert second.status_code == 409
    assert second.get_json()["job_id"] == first.get_json()["job_id"]
    gate.set()
    drain(client, first.get_json()["job_id"])


# ── run-judges ───────────────────────────────────────────────────────────────


def _no_llm(monkeypatch):
    """Fail loudly if anything reaches the judge runner."""
    import src.judges.runner as runner

    def explode(*a, **k):
        raise AssertionError("run_judge must not be called")

    monkeypatch.setattr(runner, "run_judge", explode)


def test_over_the_cost_limit_without_confirm_spends_nothing(client, project, monkeypatch):
    _no_llm(monkeypatch)
    rv = client.post("/api/project/jobproj/review/run-judges",
                     json={"judges": ["dialogue"], "cost_limit": 0.0})
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["status"] == "needs_confirm"
    assert body["target_count"] == 2
    assert body["estimated_cost"] > 0
    assert "job_id" not in body
    assert jobs.active_job("jobproj") is None


def test_address_judge_without_a_map_409s_with_the_harness_fix(client, project, monkeypatch):
    _no_llm(monkeypatch)
    rv = client.post("/api/project/jobproj/review/run-judges",
                     json={"judges": ["address"], "confirm": True})
    assert rv.status_code == 409
    error = rv.get_json()["error"]
    assert "address-map prepare" in error
    assert "address-map commit" in error


def test_run_judges_persists_each_verdict(client, project, monkeypatch):
    import src.judges.runner as runner
    from src.models import EvalResult

    calls = []

    def fake_run_judge(judge_name, target, context):
        calls.append((judge_name, target.id))
        return EvalResult(
            eval_name=judge_name, eval_version="1.0.0", target_id=target.id,
            target_type="chunk", passed=True, score=1.0, issues=[],
        )

    monkeypatch.setattr(runner, "run_judge", fake_run_judge)

    rv = client.post("/api/project/jobproj/review/run-judges",
                     json={"judges": ["dialogue"], "confirm": True})
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["status"] == "started"
    events = drain(client, body["job_id"])

    assert calls == [("dialogue", "chapter_01_chunk_000"),
                     ("dialogue", "chapter_01_chunk_001")]
    assert events[-1]["event"] == "complete"
    assert events[-1]["ran"] == 2
    assert events[-1]["error_count"] == 0
    payload = json.loads(
        (project / "evaluations" / "chapter_01_chunk_000.json").read_text(encoding="utf-8"))
    assert "dialogue" in payload["judges"]
    # Persisting through merge_judge_result means the freshness ledger is
    # stamped the same way a CLI run stamps it.
    assert payload["eval_runs"]["dialogue"]["text_sha"]


def test_one_untranslated_chapter_does_not_abort_the_whole_judges_run(
    client, project, monkeypatch
):
    """The Review table lists untranslated chapters, so ticking one alongside
    ready ones is easy. That chapter must drop out of the run rather than 400
    the whole request and leave nothing judged."""
    import src.judges.runner as runner
    from src.models import EvalResult

    (project / "chapters" / "chapter_02.txt").write_text("y", encoding="utf-8")
    (project / "chunks" / "chapter_02_chunk_000.json").write_text(json.dumps({
        "id": "chapter_02_chunk_000", "chapter_id": "chapter_02", "position": 0,
        "source_text": "The white dog.", "translated_text": "",
        "status": "pending",
        "metadata": {
            "char_start": 0, "char_end": 14, "overlap_start": 0,
            "overlap_end": 0, "paragraph_count": 1, "word_count": 3,
        },
    }), encoding="utf-8")

    judged = []

    def fake_run_judge(judge_name, target, context):
        judged.append(target.id)
        return EvalResult(
            eval_name=judge_name, eval_version="1.0.0", target_id=target.id,
            target_type="chunk", passed=True, score=1.0, issues=[],
        )

    monkeypatch.setattr(runner, "run_judge", fake_run_judge)

    rv = client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["dialogue"],
        "chapter_ids": ["chapter_01", "chapter_02"],
        "confirm": True,
    })
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["status"] == "started"
    assert [s["scope"] for s in body["skipped"]] == ["chapter:chapter_02"]
    drain(client, body["job_id"])
    assert judged == ["chapter_01_chunk_000", "chapter_01_chunk_001"]


def test_every_chapter_untranslated_is_still_a_400(client, project, monkeypatch):
    """Skipping is per chapter, not a licence to start an empty run."""
    _no_llm(monkeypatch)
    for path in (project / "chunks").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["translated_text"] = ""
        path.write_text(json.dumps(data), encoding="utf-8")

    rv = client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["dialogue"], "chapter_ids": ["chapter_01"], "confirm": True,
    })
    assert rv.status_code == 400
    assert jobs.active_job("jobproj") is None


def test_dry_run_starts_nothing_even_when_the_estimate_is_zero(
    client, project, monkeypatch
):
    """The estimate button used to post `cost_limit: 0, confirm: false` and rely
    on `estimated_cost > cost_limit` to force `needs_confirm`. A zero-priced
    provider makes `0.0 > 0` false, so the "estimate" started a real run."""
    _no_llm(monkeypatch)
    import src.judges as judges_pkg

    monkeypatch.setattr(judges_pkg, "estimate_suite_cost", lambda *a, **k: 0.0)

    rv = client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["dialogue"], "dry_run": True, "cost_limit": 0,
    })
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["status"] == "estimate"
    assert body["estimated_cost"] == 0.0
    assert body["target_count"] == 2
    assert "job_id" not in body
    assert jobs.active_job("jobproj") is None


def test_run_judges_rejects_unknown_judge(client, project):
    rv = client.post("/api/project/jobproj/review/run-judges", json={"judges": ["vibes"]})
    assert rv.status_code == 400
    assert "vibes" in rv.get_json()["error"]


def test_run_judges_rejects_an_unknown_backend(client, project):
    rv = client.post("/api/project/jobproj/review/run-judges",
                     json={"judges": ["dialogue"], "backend": "telepathy"})
    assert rv.status_code == 400
    assert "backend" in rv.get_json()["error"]


# ── run-judges: the headless CLI backend ─────────────────────────────────────
#
# Nothing below may reach a real CLI: `subagent.*` is stubbed, the preflight is
# stubbed, and `_no_spawn` turns any surviving `subprocess.run` into a failure
# rather than a subscription charge (or a 30 s timeout on a machine with no CLI
# installed at all).


def _no_spawn(monkeypatch):
    from src.harness import headless

    def explode(*a, **k):
        raise AssertionError("this path must not spawn a process")

    monkeypatch.setattr(headless.subprocess, "run", explode)


def _installed(monkeypatch, *names):
    """Pin what ``shutil.which`` finds, so the payload is machine-independent."""
    import shutil

    wanted = set(names)
    monkeypatch.setattr(
        shutil, "which", lambda name: f"/bin/{name}" if name in wanted else None
    )


def _fake_profile(cli: str = "claude"):
    from src.harness.profile import HeadlessProfile

    return HeadlessProfile(
        command="judges",
        cli=cli,
        cli_source="host:claude-code",
        worker_model="sonnet",
        worker_model_source="default:claude",
        effort="medium",
        effort_source="default:judges",
        effort_channel="argv",
        baseline_tokens=3900,
        baseline_source="constant:claude",
        host="claude-code",
        warnings=["heads up"],
    )


def _stub_headless(monkeypatch, *, cli="claude", preflight=None, fanout_extra=None):
    """Stub the three subagent verbs + the preflight; record the call order."""
    from src.harness import headless, profile as profile_mod
    from src.judges import subagent

    calls: list[tuple[str, dict]] = []

    monkeypatch.setattr(profile_mod, "resolve_profile", lambda *a, **k: _fake_profile(cli))
    monkeypatch.setattr(headless, "preflight_error", lambda c, cli_bin=None: preflight)

    def fake_prepare(project_dir, judge_names, scopes, **kwargs):
        calls.append(("prepare", {"scopes": list(scopes), **kwargs}))
        return {"manifest": [{"target_id": "t1"}, {"target_id": "t2"}]}

    def fake_fanout(project_dir, **kwargs):
        calls.append(("fanout", dict(kwargs)))
        if kwargs.get("estimate"):
            return {
                "estimate": {
                    "jobs": 2,
                    "prompt_tokens": 1200,
                    "projected_tokens": 9000,
                    "baseline_tokens": 3900,
                    "baseline_source": "constant:claude",
                    "argv": ["claude", "-p", "--model", "sonnet"],
                    "cache": "5m",
                },
                "effective": _fake_profile(cli).to_payload(),
                "warnings": ["heads up"],
            }
        hook = kwargs.get("on_job_done")
        if hook:
            hook({"id": "t1", "ok": True, "error": None, "done": 1, "total": 2})
            hook({"id": "t2", "ok": True, "error": None, "done": 2, "total": 2})
        out = {
            "wrote": ["t1", "t2"], "failed": [], "cli": cli,
            "usage": {"jobs": 2, "input": 20000, "overhead_ratio": 0.56},
            "effective": _fake_profile(cli).to_payload(),
        }
        out.update(fanout_extra or {})
        return out

    def fake_commit(project_dir, **kwargs):
        calls.append(("commit", dict(kwargs)))
        return {
            "counts": {"committed": 2, "failed": 0, "missing": 0},
            "failed": [], "persist_errors": [],
        }

    monkeypatch.setattr(subagent, "prepare", fake_prepare)
    monkeypatch.setattr(subagent, "fanout", fake_fanout)
    monkeypatch.setattr(subagent, "commit", fake_commit)
    return calls


def test_judges_profile_relays_the_resolved_payload_and_spawns_nothing(
    client, project, monkeypatch
):
    """The GUI's defaults must be `resolve_profile`'s answer verbatim — the moment
    the dashboard re-derives one of these from config, it is the fifth layer that
    can disagree with the other four."""
    _no_spawn(monkeypatch)
    _installed(monkeypatch, "claude")

    rv = client.get("/api/project/jobproj/judges/profile")
    assert rv.status_code == 200
    body = rv.get_json()

    from src.harness.profile import resolve_profile

    expected = resolve_profile(project, command="judges").to_payload()
    for key, value in expected.items():
        assert body[key] == value, key
    assert body["binaries"] == {"claude": True, "cursor": False}
    assert body["default_backend"] == "headless"
    assert body["prompt_cache_supported"] is True
    assert "sonnet" in body["worker_model_suggestions"]


def test_judges_profile_re_resolves_the_whole_block_for_another_cli(
    client, project, monkeypatch
):
    _no_spawn(monkeypatch)
    _installed(monkeypatch, "claude")
    rv = client.get("/api/project/jobproj/judges/profile?cli=cursor")
    assert rv.status_code == 200
    body = rv.get_json()
    # An explicit pick is never second-guessed against what is installed.
    assert body["cli"] == "cursor" and body["cli_source"] == "cli"
    assert body["prompt_cache_supported"] is False
    # No claude binary implied for a cursor wave, and no API backend forced.
    assert body["default_backend"] == "api"


def test_judges_profile_rejects_an_unknown_cli(client, project):
    assert client.get("/api/project/jobproj/judges/profile?cli=gemini").status_code == 400


def test_pin_cli_writes_the_one_harness_key(client, project):
    rv = client.post("/api/project/jobproj/judges/pin-cli", json={"cli": "cursor"})
    assert rv.status_code == 200
    cfg = json.loads((project / ".harness" / "config.json").read_text(encoding="utf-8"))
    assert cfg["headless_cli"] == "cursor"

    # And unpinning returns the book to detection.
    client.post("/api/project/jobproj/judges/pin-cli", json={"cli": "auto"})
    cfg = json.loads((project / ".harness" / "config.json").read_text(encoding="utf-8"))
    assert cfg["headless_cli"] == "auto"


@pytest.mark.parametrize("value", ["gemini", "", None, 3])
def test_pin_cli_rejects_anything_else(client, project, value):
    rv = client.post("/api/project/jobproj/judges/pin-cli", json={"cli": value})
    assert rv.status_code == 400


def test_an_unconfirmed_headless_request_starts_nothing(client, project, monkeypatch):
    """No threshold on this path: a token estimate must be fetched and confirmed."""
    _no_spawn(monkeypatch)
    calls = _stub_headless(monkeypatch)
    rv = client.post("/api/project/jobproj/review/run-judges",
                     json={"judges": ["dialogue"], "backend": "headless"})
    assert rv.status_code == 200
    assert rv.get_json()["status"] == "needs_confirm"
    assert calls == []
    assert jobs.active_job("jobproj") is None


def test_a_headless_estimate_prepares_but_never_runs(client, project, monkeypatch):
    _no_spawn(monkeypatch)
    calls = _stub_headless(monkeypatch)

    rv = client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["dialogue"], "backend": "headless", "estimate": True,
    })
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["status"] == "estimate"
    assert (body["jobs"], body["projected_tokens"]) == (2, 9000)
    assert body["argv"] == ["claude", "-p", "--model", "sonnet"]
    # Relayed whole: baseline_tokens means nothing without cli, effort nothing
    # without effort_channel.
    assert body["effective"]["cli"] == "claude"
    assert body["effective"]["effort_channel"] == "argv"
    assert body["warnings"] == ["heads up"]

    assert [name for name, _ in calls] == ["prepare", "fanout"]
    assert calls[1][1]["estimate"] is True
    assert jobs.active_job("jobproj") is None


def test_untouched_fields_reach_prepare_as_none(client, project, monkeypatch):
    """Pre-filling the resolved values and sending them back would make
    `cli_source` read "a flag said so" when nothing did."""
    _no_spawn(monkeypatch)
    calls = _stub_headless(monkeypatch)

    client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["dialogue"], "backend": "headless", "estimate": True,
    })
    prepared = calls[0][1]
    assert prepared["cli"] is None
    assert prepared["worker_model"] is None
    assert prepared["effort"] is None


def test_explicit_overrides_reach_prepare_verbatim(client, project, monkeypatch):
    _no_spawn(monkeypatch)
    calls = _stub_headless(monkeypatch, cli="cursor")

    client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["dialogue"], "backend": "headless", "estimate": True,
        "cli": "cursor", "worker_model": "grok-4.5[effort=high]", "effort": "high",
        "prompt_cache": "1h",
    })
    prepared = calls[0][1]
    assert prepared["cli"] == "cursor"
    assert prepared["worker_model"] == "grok-4.5[effort=high]"
    assert prepared["effort"] == "high"
    assert calls[1][1]["cache"] == "1h"


def test_a_failed_preflight_is_a_409_carrying_the_clis_own_fix(
    client, project, monkeypatch
):
    _no_spawn(monkeypatch)
    calls = _stub_headless(
        monkeypatch, preflight="claude is not logged in — run `claude` and `/login`"
    )
    rv = client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["dialogue"], "backend": "headless", "estimate": True,
    })
    assert rv.status_code == 409
    assert "/login" in rv.get_json()["error"]
    assert calls == []  # nothing prepared, nothing spawned


def test_a_confirmed_headless_run_prepares_fans_out_and_commits(
    client, project, monkeypatch
):
    _no_spawn(monkeypatch)
    calls = _stub_headless(monkeypatch, cli="cursor")

    rv = client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["dialogue"], "backend": "headless", "confirm": True,
    })
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["status"] == "started"
    events = drain(client, body["job_id"])

    assert [name for name, _ in calls] == ["prepare", "fanout", "commit"]
    # The persisted verdict says which launcher judged the book, not a Task
    # spawn that never happened.
    assert calls[2][1] == {"persist": True, "backend": "headless:cursor"}

    phases = [e.get("phase") for e in events if e.get("event") == "phase"]
    assert phases == ["prepare", "fanout", "commit"]
    assert [e["index"] for e in events if e.get("event") == "target_done"] == [1, 2]
    assert events[-1]["event"] == "complete"
    assert events[-1]["ran"] == 2
    assert events[-1]["usage"]["overhead_ratio"] == 0.56


def test_a_launcher_error_ends_the_job_instead_of_committing(
    client, project, monkeypatch
):
    """A wave that never launched has nothing to commit, and saying "0 of 2 done"
    would hide the one line that explains why."""
    _no_spawn(monkeypatch)
    calls = _stub_headless(
        monkeypatch,
        fanout_extra={"error": "subscription preflight failed: not logged in",
                      "wrote": [], "usage": None},
    )
    body = client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["dialogue"], "backend": "headless", "confirm": True,
    }).get_json()
    events = drain(client, body["job_id"])

    assert [name for name, _ in calls] == ["prepare", "fanout"]
    assert events[-1]["event"] == "complete"
    assert "not logged in" in events[-1]["fatal"]


@pytest.mark.parametrize("payload", [
    {"cli": "auto"},
    {"cli": "gemini"},
    {"effort": "turbo"},
    {"prompt_cache": "10m"},
    {"worker_model": "   "},
    {"worker_model": 7},
])
def test_bad_headless_knobs_are_400(client, project, monkeypatch, payload):
    _no_spawn(monkeypatch)
    calls = _stub_headless(monkeypatch)
    body = {"judges": ["dialogue"], "backend": "headless", "estimate": True}
    body.update(payload)
    rv = client.post("/api/project/jobproj/review/run-judges", json=body)
    assert rv.status_code == 400
    assert calls == []


# ── The SSE route ────────────────────────────────────────────────────────────


def test_sse_404s_for_an_unknown_or_foreign_job(client, project):
    job_id = jobs.start_job("otherproject", "review-coded", lambda emit: None)
    assert client.get("/api/project/jobproj/jobs/nosuch/sse").status_code == 404
    assert client.get(f"/api/project/jobproj/jobs/{job_id}/sse").status_code == 404
