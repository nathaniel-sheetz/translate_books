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


# ── The SSE route ────────────────────────────────────────────────────────────


def test_sse_404s_for_an_unknown_or_foreign_job(client, project):
    job_id = jobs.start_job("otherproject", "review-coded", lambda emit: None)
    assert client.get("/api/project/jobproj/jobs/nosuch/sse").status_code == 404
    assert client.get(f"/api/project/jobproj/jobs/{job_id}/sse").status_code == 404
