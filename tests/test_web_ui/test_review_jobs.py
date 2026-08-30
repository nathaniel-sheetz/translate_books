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
import re
import threading

import pytest

from web_ui import jobs
from web_ui.app import app
from web_ui.evaluations import REVIEW_JUDGE_TYPES


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


def test_the_judge_picker_offers_every_judge_the_endpoint_accepts(client, project):
    """The modal's checkboxes are the only way the browser names a judge.

    ``selectedJudges()`` reads ``#judges-modal .judge-pick:checked`` and
    ``postJudges`` refuses an empty list, so a judge with no checkbox is
    unreachable from the UI no matter how completely it is wired everywhere
    else — which is exactly how ``editorial`` shipped registered, persisted and
    pipped, but unrunnable. Derived from ``REVIEW_JUDGE_TYPES`` rather than
    spelled out: the next judge added to that tuple fails here until it has a
    box to tick.
    """
    html = client.get("/project/jobproj").data.decode("utf-8")
    picker = html.split('id="judges-modal"')[1].split("</div>")[0]
    offered = set(re.findall(r'class="judge-pick" value="([^"]+)"', picker))

    assert offered == set(REVIEW_JUDGE_TYPES)


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


def _stub_headless(
    monkeypatch, *, cli="claude", preflight=None, fanout_extra=None,
    commit_result=None, preflight_calls=None,
):
    """Stub the three subagent verbs + the preflight; record the call order."""
    from src.harness import headless, profile as profile_mod
    from src.judges import subagent

    calls: list[tuple[str, dict]] = []

    def fake_preflight(c, cli_bin=None, *, model=None):
        if preflight_calls is not None:
            preflight_calls.append({"cli": c, "cli_bin": cli_bin, "model": model})
        return preflight

    monkeypatch.setattr(profile_mod, "resolve_profile", lambda *a, **k: _fake_profile(cli))
    monkeypatch.setattr(headless, "preflight_error", fake_preflight)

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
        if commit_result is not None:
            return commit_result
        return {
            "status": "ok",
            "counts": {"committed": 2, "failed": 0, "missing": 0},
            "failed": [], "missing": [], "persist_errors": [],
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


def test_headless_estimate_drops_an_untranslated_chapter_from_prepare(
    client, project, monkeypatch
):
    """Same skip rule as the API path, but prepare is the consumer here — a
    skipped chapter in ``scopes`` would make it raise (or write empty prompts)
    instead of just dropping out of the estimate."""
    _no_spawn(monkeypatch)
    calls = _stub_headless(monkeypatch)

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

    rv = client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["dialogue"], "backend": "headless", "estimate": True,
        "chapter_ids": ["chapter_01", "chapter_02"],
    })
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["status"] == "estimate"
    assert [s["scope"] for s in body["skipped"]] == ["chapter:chapter_02"]
    assert calls[0][0] == "prepare"
    assert calls[0][1]["scopes"] == ["chapter:chapter_01"]


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


def test_confirm_is_preflighted_too_and_starts_no_job(client, project, monkeypatch):
    """The confirm path used to skip the gate estimate had: it started a job, ran
    the destructive `prepare`, and only then died in fanout — so the operator saw
    a job modal, then "Stopped", with the previous wave's drafts already gone."""
    _no_spawn(monkeypatch)
    calls = _stub_headless(
        monkeypatch, preflight="claude is not logged in — run `claude` and `/login`"
    )
    rv = client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["dialogue"], "backend": "headless", "confirm": True,
    })
    assert rv.status_code == 409
    assert "/login" in rv.get_json()["error"]
    assert calls == []
    assert jobs.active_job("jobproj") is None


def test_the_preflight_carries_the_resolved_worker_model(client, project, monkeypatch):
    """Cursor rejects a bogus `--model` before it reads a prompt, but only inside
    `run_headless_wave` — i.e. after `prepare`. Hoisting it means the estimate
    never goes green on argv the wave cannot run."""
    _no_spawn(monkeypatch)
    seen: list[dict] = []
    _stub_headless(monkeypatch, cli="cursor", preflight_calls=seen)

    client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["dialogue"], "backend": "headless", "estimate": True,
    })
    assert seen and seen[0]["cli"] == "cursor"
    # `_fake_profile`'s resolved model, not the raw override (which is None here).
    assert seen[0]["model"] == "sonnet"


@pytest.mark.parametrize("gate", [{"estimate": True}, {"confirm": True}])
def test_a_live_job_blocks_both_gates_before_prepare_can_unlink(
    client, project, monkeypatch, gate
):
    """`prepare` unlinks drafts and rewrites manifest.json. Fired while a wave is
    in fanout/commit (a second tab), it deletes work in flight and swaps the
    manifest out from under the running job."""
    _no_spawn(monkeypatch)
    calls = _stub_headless(monkeypatch)

    release = threading.Event()
    running_id = jobs.start_job("jobproj", "review-judges", lambda emit: release.wait(5))
    try:
        body = {"judges": ["dialogue"], "backend": "headless", **gate}
        rv = client.post("/api/project/jobproj/review/run-judges", json=body)
        assert rv.status_code == 409
        assert rv.get_json()["job_id"] == running_id
        assert calls == []  # nothing prepared: no draft was unlinked
    finally:
        release.set()


def test_dry_run_outranks_confirm_on_the_headless_path(client, project, monkeypatch):
    """`dry_run` was read only by the API backend, so `{dry_run, confirm}` here
    started a real wave — silent spend against a flag that says "don't"."""
    _no_spawn(monkeypatch)
    calls = _stub_headless(monkeypatch)

    rv = client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["dialogue"], "backend": "headless",
        "dry_run": True, "confirm": True,
    })
    assert rv.status_code == 200
    assert rv.get_json()["status"] == "estimate"
    assert [name for name, _ in calls] == ["prepare", "fanout"]
    assert calls[1][1]["estimate"] is True
    assert jobs.active_job("jobproj") is None


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


def test_a_failed_commit_stops_the_job_instead_of_reporting_a_clean_wave(
    client, project, monkeypatch
):
    """`commit`'s error return carries no `counts`, so the summary read
    `ran: 0, error_count: 0` and the modal rendered "0 of 2 done" for a commit
    that never happened. False success is worse than a visible stop."""
    _no_spawn(monkeypatch)
    _stub_headless(monkeypatch, commit_result={
        "status": "error",
        "error": "no judge manifest — run `prepare` first",
        "committed": [], "failed": [], "missing": [],
    })
    body = client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["dialogue"], "backend": "headless", "confirm": True,
    }).get_json()
    events = drain(client, body["job_id"])

    assert events[-1]["event"] == "complete"
    assert "no judge manifest" in events[-1]["fatal"]
    assert events[-1].get("ran") is None


def test_missing_drafts_are_counted_as_errors_not_silence(client, project, monkeypatch):
    """A draft the wave never wrote is a failure of this run. Left out of
    `errors`, an all-missing commit reported zero errors and read as a clean
    wave that simply judged nothing."""
    _no_spawn(monkeypatch)
    _stub_headless(monkeypatch, commit_result={
        "status": "ok",
        "counts": {"committed": 0, "failed": 0, "missing": 2},
        "failed": [],
        "missing": [
            {"target_id": "chapter_01_chunk_000", "judge": "dialogue"},
            {"target_id": "chapter_01_chunk_001", "judge": "dialogue"},
        ],
        "persist_errors": [],
    })
    body = client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["dialogue"], "backend": "headless", "confirm": True,
    }).get_json()
    events = drain(client, body["job_id"])

    final = events[-1]
    assert final["event"] == "complete" and final["ran"] == 0
    assert final["missing"] == 2
    assert final["error_count"] == 2
    assert "draft missing" in final["errors"][0]


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


# ── The editorial judge's second pass ────────────────────────────────────────
#
# Pass 1 writes proposals; only pass 2 turns them into findings. The Review tab
# runs both as one job, so what is pinned here is that the second half actually
# happens, that it cannot turn a successful pass 1 into a reported failure, and
# that the repair route which finishes an interrupted one is gated exactly like
# the wave it re-runs.


def _editorial_result(*, verified=False, candidates=1, excerpt="El gato negro."):
    """A persisted pass-1 editorial result, with or without adjudication."""
    metadata = {"verified": verified, "candidates": [
        {
            "rule": "naturalness",
            "category": "NATURALNESS",
            "severity": "warning",
            "confidence": "high",
            "excerpt": excerpt,
            "message": "Reads as translationese.",
            "suggestion": "El gato es negro.",
            "source_check": "not_needed",
        }
        for _ in range(candidates)
    ]}
    return {
        "eval_name": "editorial",
        "passed": False,
        "score": 80.0,
        "issues": [
            {"severity": "warning", "message": "[naturalness] Reads as translationese.",
             "location": excerpt, "finding_key": f"k{i}"}
            for i in range(candidates)
        ],
        "metadata": metadata,
    }


def _write_editorial(proj, chunk_id, **kwargs):
    evals = proj / "evaluations"
    evals.mkdir(parents=True, exist_ok=True)
    (evals / f"{chunk_id}.json").write_text(json.dumps({
        "chunk_id": chunk_id,
        "judges": {"editorial": _editorial_result(**kwargs)},
    }), encoding="utf-8")


def _stub_wave(monkeypatch, *, chunks=2, prepare_error=None, fanout_error=None,
               commit_result=None, boom=None):
    """Stub the pass-2 wave verbs and record the call order."""
    from src.judges import editorial_wave

    calls: list[tuple[str, dict]] = []

    def fake_prepare(project_dir, scopes, **kwargs):
        calls.append(("prepare", {"scopes": list(scopes), **kwargs}))
        if boom == "prepare":
            raise RuntimeError("prepare exploded")
        if prepare_error:
            return {"status": "error", "error": prepare_error}
        return {"status": "ok", "counts": {"chunks": chunks, "candidates": chunks}}

    def fake_fanout(project_dir, **kwargs):
        calls.append(("fanout", dict(kwargs)))
        hook = kwargs.get("on_job_done")
        if hook:
            for i in range(chunks):
                hook({"id": f"c{i}", "ok": True, "done": i + 1, "total": chunks})
        return {"error": fanout_error} if fanout_error else {"wrote": ["c0", "c1"]}

    def fake_commit(project_dir, **kwargs):
        calls.append(("commit", dict(kwargs)))
        if commit_result is not None:
            return commit_result
        return {
            "status": "ok", "committed": chunks, "failed": [], "missing": [],
            "rollup": {"confirmed": 3, "retracted": 1, "reclassified": 0,
                       "source_used": 1},
        }

    monkeypatch.setattr(editorial_wave, "prepare", fake_prepare)
    monkeypatch.setattr(editorial_wave, "fanout", fake_fanout)
    monkeypatch.setattr(editorial_wave, "commit", fake_commit)
    return calls


def _phases(events):
    return [e["phase"] for e in events if "phase" in e]


def test_a_headless_editorial_run_adjudicates_in_the_same_job(
    client, project, monkeypatch
):
    """Pass 1's `issues[]` are proposals: `metadata.verified` stays false until
    the adjudication pass has settled them. A GUI that stopped after `commit`
    would light a dashboard badge from un-second-guessed candidates — and because
    `merge_judge_result` replaces a judge's whole result, finishing the job later
    by re-running the judge would discard any adjudication that *had* landed."""
    _no_spawn(monkeypatch)
    _installed(monkeypatch, "claude")
    _stub_headless(monkeypatch)
    wave_calls = _stub_wave(monkeypatch)

    rv = client.post("/api/project/jobproj/review/run-judges", json={
        "backend": "headless", "judges": ["editorial"], "confirm": True,
    })
    assert rv.status_code == 200
    events = drain(client, rv.get_json()["job_id"])

    assert _phases(events) == [
        "prepare", "fanout", "commit",
        "adjudicate_prepare", "adjudicate", "adjudicate_commit",
    ]
    assert [name for name, _ in wave_calls] == ["prepare", "fanout", "commit"]
    # Never `force`: pass 1 has just rewritten these results, so `verified` is
    # false again and the pending set is exactly what it proposed. Forcing would
    # re-decide retractions an *earlier* pass had already made.
    assert wave_calls[0][1].get("force") in (None, False)
    assert wave_calls[2][1]["persist"] is True

    done = events[-1]
    assert done["adjudication"] == {
        "chunks": 2, "confirmed": 3, "reclassified": 0,
        "retracted": 1, "source_used": 1, "unverified": 0,
    }
    assert done["error_count"] == 0


def test_a_wave_without_the_editorial_judge_runs_no_second_pass(
    client, project, monkeypatch
):
    """The other two judges are single-pass. Adjudication must not ride along."""
    _no_spawn(monkeypatch)
    _installed(monkeypatch, "claude")
    _stub_headless(monkeypatch)
    wave_calls = _stub_wave(monkeypatch)

    rv = client.post("/api/project/jobproj/review/run-judges", json={
        "backend": "headless", "judges": ["dialogue"], "confirm": True,
    })
    events = drain(client, rv.get_json()["job_id"])

    assert _phases(events) == ["prepare", "fanout", "commit"]
    assert wave_calls == []
    assert events[-1]["adjudication"] is None


def test_a_clean_pass_one_skips_the_adjudication_wave(client, project, monkeypatch):
    """`no_candidates` is a clean chunk, not a gap. A book pass 1 found nothing
    in must not pay for a second wave to confirm it."""
    _no_spawn(monkeypatch)
    _installed(monkeypatch, "claude")
    _stub_headless(monkeypatch)
    wave_calls = _stub_wave(monkeypatch, chunks=0)

    rv = client.post("/api/project/jobproj/review/run-judges", json={
        "backend": "headless", "judges": ["editorial"], "confirm": True,
    })
    events = drain(client, rv.get_json()["job_id"])

    assert _phases(events) == ["prepare", "fanout", "commit", "adjudicate_prepare"]
    assert [name for name, _ in wave_calls] == ["prepare"]
    assert events[-1]["adjudication"]["chunks"] == 0
    assert events[-1]["error_count"] == 0


def test_a_failed_second_pass_does_not_stop_a_successful_first_one(
    client, project, monkeypatch
):
    """Pass 1's findings are already on disk when adjudication starts. Reporting
    the whole wave as `Stopped` would hide them behind a failure that lost
    nothing — the fix is the Review tab's banner, not a re-run of pass 1."""
    _no_spawn(monkeypatch)
    _installed(monkeypatch, "claude")
    _stub_headless(monkeypatch)
    _stub_wave(monkeypatch, boom="prepare")

    rv = client.post("/api/project/jobproj/review/run-judges", json={
        "backend": "headless", "judges": ["editorial"], "confirm": True,
    })
    done = drain(client, rv.get_json()["job_id"])[-1]

    assert "fatal" not in done
    assert done["ran"] == 2                      # pass 1 still landed
    assert done["error_count"] == 1
    assert "prepare exploded" in done["errors"][0]


def test_the_estimate_quotes_pass_two_as_a_ceiling(client, project, monkeypatch):
    """Pass 2's size cannot be measured before pass 1 has proposed anything, and
    the operator is asked once, before either wave. So the gate quotes a bound:
    one job per chunk in scope at this pipeline's own per-job baseline."""
    _no_spawn(monkeypatch)
    _installed(monkeypatch, "claude")
    _stub_headless(monkeypatch)

    rv = client.post("/api/project/jobproj/review/run-judges", json={
        "backend": "headless", "judges": ["editorial"], "estimate": True,
    })
    body = rv.get_json()
    assert body["status"] == "estimate"
    adj = body["adjudication"]
    assert adj["jobs_max"] == 2                  # two translated chunks in scope
    assert adj["tokens_max"] == 2 * adj["baseline_tokens"]
    assert adj["baseline_source"]

    # ...and a wave without it says nothing about a pass that will not run.
    rv = client.post("/api/project/jobproj/review/run-judges", json={
        "backend": "headless", "judges": ["dialogue"], "estimate": True,
    })
    assert rv.get_json()["adjudication"] is None


def test_the_api_gate_covers_both_passes(client, project, monkeypatch):
    """One confirmation has to cover the whole run: the second pass starts inside
    a background job, where there is nobody left to ask. Its exact price is not
    knowable before pass 1 has proposed anything, so the quote assumes every
    chunk comes back at its full findings budget — high, which is the only safe
    direction for a spend gate."""
    _no_llm(monkeypatch)

    with_ed = client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["dialogue", "editorial"], "dry_run": True,
    }).get_json()
    without = client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["dialogue"], "dry_run": True,
    }).get_json()

    assert with_ed["adjudication_cost"] > 0
    assert without["adjudication_cost"] == 0
    # The bound is added to the number the gate compares against, not reported
    # beside it — a limit that only covers pass 1 is not a limit.
    assert with_ed["estimated_cost"] > without["estimated_cost"] + with_ed["adjudication_cost"] * 0.5


def test_the_api_backend_adjudicates_after_its_own_pass_one(
    client, project, monkeypatch
):
    _no_spawn(monkeypatch)
    import src.judges.runner as runner
    from src.judges import editorial_wave
    from src.models import EvalResult

    monkeypatch.setattr(runner, "run_judge", lambda name, target, context: EvalResult(
        eval_name=name, eval_version="1.0.0", target_id=target.id,
        target_type="chunk", passed=True, score=1.0, issues=[],
    ))

    seen = {}

    def fake_run_api(project_dir, scopes, **kwargs):
        seen.update({"scopes": list(scopes), **kwargs})
        return {
            "status": "ok",
            "counts": {"chunks": 1},
            "results": [{"chunk_id": "chapter_01_chunk_000", "status": "ok"}],
            "rollup": {"confirmed": 2, "retracted": 1, "reclassified": 0,
                       "source_used": 0},
        }

    monkeypatch.setattr(editorial_wave, "run_api", fake_run_api)

    rv = client.post("/api/project/jobproj/review/run-judges", json={
        "judges": ["editorial"], "confirm": True,
    })
    events = drain(client, rv.get_json()["job_id"])

    assert _phases(events) == ["adjudicate"]
    assert seen["persist"] is True
    # The caller already gated on an estimate covering both passes; a second
    # dollar gate inside a running job has nobody to ask.
    assert seen["confirm"] is True
    assert events[-1]["adjudication"]["retracted"] == 1


# ── The repair route ─────────────────────────────────────────────────────────


def test_review_status_counts_chunks_awaiting_adjudication(client, project):
    """The banner's number. A chunk pass 1 found clean has nothing to adjudicate
    and must not be counted, or every book reads as permanently half-finished."""
    _write_editorial(project, "chapter_01_chunk_000")
    _write_editorial(project, "chapter_01_chunk_001", verified=True)

    body = client.get("/api/project/jobproj/review-status").get_json()
    assert body["editorial_pending"] == {
        "chunks": 1, "by_chapter": {"chapter_01": 1},
    }
    assert body["chapters"][0]["editorial_pending"] == 1


def test_review_status_ignores_a_chunk_with_no_candidates(client, project):
    _write_editorial(project, "chapter_01_chunk_000", candidates=0)

    body = client.get("/api/project/jobproj/review-status").get_json()
    assert body["editorial_pending"]["chunks"] == 0


def test_adjudicate_route_walks_the_three_gate_states(client, project, monkeypatch):
    """Same ladder as the pass-1 gate — nothing spends without an explicit
    confirmation — except that this one can quote an exact number, because the
    candidates it would adjudicate already exist."""
    _no_spawn(monkeypatch)
    _installed(monkeypatch, "claude")
    _stub_headless(monkeypatch)
    _write_editorial(project, "chapter_01_chunk_000")

    def post(**body):
        return client.post(
            "/api/project/jobproj/review/adjudicate-editorial",
            json={"backend": "headless", **body},
        ).get_json()

    assert post()["status"] == "needs_confirm"

    wave_calls = _stub_wave(monkeypatch, chunks=1)
    estimate = post(estimate=True)
    assert estimate["status"] == "estimate"
    assert [name for name, _ in wave_calls] == ["prepare"]

    started = post(confirm=True)
    assert started["status"] == "started"
    events = drain(client, started["job_id"])
    assert _phases(events) == [
        "adjudicate_prepare", "adjudicate", "adjudicate_commit",
    ]


def test_adjudicate_route_reports_nothing_pending_rather_than_failing(
    client, project, monkeypatch
):
    """The banner can be a poll behind, or another session may have finished the
    work. A request that is correct and merely unnecessary is not an error."""
    _no_spawn(monkeypatch)
    _write_editorial(project, "chapter_01_chunk_000", verified=True)

    rv = client.post("/api/project/jobproj/review/adjudicate-editorial",
                     json={"backend": "api", "confirm": True})
    assert rv.status_code == 200
    assert rv.get_json()["status"] == "nothing_pending"


def test_adjudicate_route_refuses_while_a_job_is_running(
    client, project, monkeypatch
):
    """`prepare` unlinks the drafts it re-renders, so firing one while a wave is
    in fan-out deletes work in flight. `start_job` refuses a second run only
    after this request would already have done the damage."""
    _no_spawn(monkeypatch)
    _installed(monkeypatch, "claude")
    _stub_headless(monkeypatch)
    _write_editorial(project, "chapter_01_chunk_000")
    wave_calls = _stub_wave(monkeypatch, chunks=1)

    release = threading.Event()
    jobs.start_job("jobproj", "review-judges", lambda emit: release.wait(30))
    try:
        rv = client.post("/api/project/jobproj/review/adjudicate-editorial",
                         json={"backend": "headless", "estimate": True})
        assert rv.status_code == 409
        assert wave_calls == []
    finally:
        release.set()


# ── The SSE route ────────────────────────────────────────────────────────────


def test_sse_404s_for_an_unknown_or_foreign_job(client, project):
    job_id = jobs.start_job("otherproject", "review-coded", lambda emit: None)
    assert client.get("/api/project/jobproj/jobs/nosuch/sse").status_code == 404
    assert client.get(f"/api/project/jobproj/jobs/{job_id}/sse").status_code == 404


def test_the_default_judge_set_leaves_the_editorial_judge_out(client, project, monkeypatch):
    """A caller that names no judges must not buy the two-pass editorial wave.

    The route defaulted to ``REVIEW_JUDGE_TYPES``, which is the reader's
    *display* tuple — adding ``editorial`` to it for the pips silently widened
    this default to the most expensive judge plus its adjudication pass, which
    ``registry._BUILTIN_SUITES`` keeps out of ``default`` and ``prose`` on
    purpose. The dashboard always sends ``judges``; a script does not.
    """
    _no_llm(monkeypatch)
    # The default set includes the address judge, which refuses to build a
    # context without this — a 409 would hide what the set actually was.
    (project / "address_map.json").write_text(
        json.dumps({
            "content": "Betsy->Frances usted; Frances->Betsy tú.",
            "pairs": [],
            "global_rules": "usted between non-intimate adults.",
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    rv = client.post("/api/project/jobproj/review/run-judges", json={"dry_run": True})

    assert rv.status_code == 200
    body = rv.get_json()
    assert "editorial" not in body["judges"]
    assert body["adjudication_cost"] == 0.0
