"""Tests for chaining a triage wave onto the deterministic rerun.

The button that runs the coded checkers now offers to run triage after them, in
the same background job. What that has to get right is mostly about not lying:

* **The checkers are the main event.** A triage wave that cannot start, fails, or
  raises must never make a completed evaluator pass read as a failed job. The
  operator would have no way to tell which half they still need to redo.
* **A clean scope is not a failure.** ``prepare`` reports "nothing left to
  triage" as an error and exits 1, which is right at a prompt and useless here:
  every book that has been triaged once lands there on the next run.
* **Consent is asked before anything is destroyed.** ``prepare`` unlinks drafts
  and rewrites the manifest, so the popup is built from a read-only status and
  the CLI is preflighted at request time, not from inside the job.
"""

from __future__ import annotations

import json

import pytest

from src.harness import state as hstate
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
    proj = projects_dir / "trproj"
    (proj / "chunks").mkdir(parents=True)
    (proj / "chapters").mkdir(parents=True)
    (proj / "chapters" / "chapter_01.txt").write_text("x", encoding="utf-8")
    _write_chunk(proj, "chapter_01_chunk_000", "El gato negro.")

    import web_ui.app as app_module
    app_module._NESTED_PROJECT_CACHE.clear()
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    monkeypatch.setattr(app_module, "evaluate_and_persist_chunk",
                        lambda project_dir, chunk, **kw: {})
    return proj


@pytest.fixture
def cli_ok(monkeypatch):
    """A CLI that would start. Nothing in these tests spawns a process."""
    import src.harness.headless as headless

    monkeypatch.setattr(headless, "preflight_error", lambda cli, **kw: None)


def post(client, payload):
    """POST the run, then wait for the job thread before returning.

    Not optional. A test that returns while its job is still running leaves the
    thread executing into teardown, where the monkeypatches have been reverted
    and ``evaluate_and_persist_chunk`` is the real one again -- which loads the
    native spellchecker and takes the whole interpreter down with an access
    violation rather than failing a test.
    """
    rv = client.post("/api/project/trproj/review/run-coded", json=payload)
    body = rv.get_json() or {}
    if body.get("job_id"):
        record = jobs.get_job(body["job_id"])
        if record and record.get("thread"):
            record["thread"].join(timeout=30)
    return rv, body


def drain(client, job_id, project_id="trproj"):
    resp = client.get(f"/api/project/{project_id}/jobs/{job_id}/sse")
    events = []
    for raw in resp.get_data(as_text=True).split("\n\n"):
        for line in raw.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def _fake_wave(monkeypatch, *, prepare=None, fanout=None, commit=None, calls=None):
    """Stand in for the three stages, recording the order they were called in."""
    import src.triage.pass_ as tp

    calls = [] if calls is None else calls

    def _prepare(project_dir, **kw):
        calls.append(("prepare", kw))
        return prepare if prepare is not None else {
            "status": "ok", "items": 3, "jobs": 1, "model_source": "repo-default",
            "effective": {"worker_model": "grok-4.6[effort=medium,fast=false]"},
        }

    def _fanout(project_dir, **kw):
        calls.append(("fanout", kw))
        progress = kw.get("progress")
        if progress:
            progress({"id": "job-001", "ok": True, "done": 1, "total": 1})
        return fanout if fanout is not None else {
            "wrote": ["job-001"], "failed": [], "usage": {"prompt_sent": 100},
        }

    def _commit(project_dir, **kw):
        calls.append(("commit", kw))
        return commit if commit is not None else {
            "status": "ok", "written": 3, "suppressed": 2, "kept": 1,
            "floor": 0.85, "report_path": "report.md",
        }

    monkeypatch.setattr(tp, "prepare", _prepare)
    monkeypatch.setattr(tp, "fanout", _fanout)
    monkeypatch.setattr(tp, "commit", _commit)
    return calls


# ── the chain ────────────────────────────────────────────────────────────────

def test_without_the_tick_nothing_triage_shaped_happens(client, project, monkeypatch):
    calls = _fake_wave(monkeypatch)

    rv, body = post(client, {})
    assert rv.status_code == 200
    assert body["triage"] is False

    events = drain(client, body["job_id"])
    assert calls == []
    assert "triage" not in events[-1]


def test_the_wave_runs_after_the_checkers_in_the_same_job(
    client, project, cli_ok, monkeypatch
):
    calls = _fake_wave(monkeypatch)

    rv, body = post(client, {"triage": True})
    assert body["triage"] is True
    events = drain(client, body["job_id"])

    assert [name for name, _ in calls] == ["prepare", "fanout", "commit"]
    # The checkers finished before the wave began.
    kinds = [e.get("event") for e in events]
    assert kinds.index("chunk_done") < kinds.index("phase")

    phases = [e["phase"] for e in events if e.get("event") == "phase"]
    assert phases == ["triage_prepare", "triage_fanout", "triage_commit"]

    done = events[-1]
    assert done["evaluated"] == 1
    assert done["triage"]["status"] == "ok"
    assert done["triage"]["suppressed"] == 2
    assert done["triage"]["kept"] == 1
    assert done["triage"]["model"] == "grok-4.6[effort=medium,fast=false]"
    assert done["triage"]["model_source"] == "repo-default"


def test_each_finished_job_reaches_the_progress_bar(
    client, project, cli_ok, monkeypatch
):
    _fake_wave(monkeypatch)

    _, body = post(client, {"triage": True})
    events = drain(client, body["job_id"])

    landed = [e for e in events if e.get("event") == "target_done"]
    assert landed and landed[0]["target_id"] == "job-001"
    assert landed[0]["index"] == 1 and landed[0]["total"] == 1


def test_the_scope_reaches_prepare(client, project, cli_ok, monkeypatch):
    calls = _fake_wave(monkeypatch)

    post(client, {"triage": True, "chapter_ids": ["chapter_01"]})

    assert dict(calls)["prepare"]["chapters"] == ["chapter_01"]


def test_a_whole_book_run_scopes_prepare_to_the_whole_book(
    client, project, cli_ok, monkeypatch
):
    calls = _fake_wave(monkeypatch)

    post(client, {"triage": True})

    assert dict(calls)["prepare"]["chapters"] is None


def test_a_book_with_no_findings_yet_may_still_ask_for_the_wave(
    client, project, cli_ok, monkeypatch
):
    """The count on disk when the run starts decides nothing.

    The popup used to disable its tick when `status` answered `triageable: 0`,
    which made the one run where triage is most obviously wanted -- the first
    one, before any findings exist -- the one run that could not ask for it.
    The count that matters is the one the checkers are about to write, so the
    route must reach the wave without consulting a pre-run total at all. This
    project has no `evaluations/` directory; `status` raising is what says the
    route never asks.
    """
    import src.triage.pass_ as tp

    def _no_asking(*a, **kw):
        raise AssertionError("run-coded must not gate the wave on a pre-run count")

    monkeypatch.setattr(tp, "status", _no_asking)
    calls = _fake_wave(monkeypatch)

    rv, body = post(client, {"triage": True})

    assert rv.status_code == 200
    assert body["triage"] is True
    assert not (project / "evaluations").exists()
    assert [name for name, _ in calls] == ["prepare", "fanout", "commit"]


# ── the ways it can go wrong, none of which may sink the checkers ────────────

def test_a_clean_scope_is_reported_not_raised(client, project, cli_ok, monkeypatch):
    """Every book that has been triaged once lands here on the next run."""
    _fake_wave(monkeypatch, prepare={
        "status": "error", "reason": "nothing_to_triage",
        "error": "no findings left to triage: ...",
        "skipped": {"already_triaged": 12},
    })

    _, body = post(client, {"triage": True})
    done = drain(client, body["job_id"])[-1]

    assert done["triage"]["status"] == "nothing_to_triage"
    assert done["triage"]["skipped"] == {"already_triaged": 12}
    assert done["evaluated"] == 1
    assert done["error_count"] == 0
    assert "fatal" not in done


def test_a_logged_out_cli_still_runs_the_checkers(client, project, monkeypatch):
    """The asymmetry with run-judges: there the wave is the request, here it is
    the tail of one, so it must not cost the operator the evaluator pass."""
    import src.harness.headless as headless

    monkeypatch.setattr(
        headless, "preflight_error", lambda cli, **kw: "cursor-agent: not logged in"
    )
    calls = _fake_wave(monkeypatch)

    rv, body = post(client, {"triage": True})
    assert rv.status_code == 200
    assert body["triage"] is False
    assert "not logged in" in body["triage_skipped"]

    done = drain(client, body["job_id"])[-1]
    assert calls == [], "nothing may be prepared when the CLI cannot start"
    assert done["evaluated"] == 1
    assert done["triage"]["status"] == "skipped"
    assert "not logged in" in done["triage"]["error"]


def test_a_launcher_refusal_is_reported_inside_the_block(
    client, project, cli_ok, monkeypatch
):
    _fake_wave(monkeypatch, fanout={
        "error": "cursor-agent is not on PATH", "wrote": [], "failed": [],
    })

    _, body = post(client, {"triage": True})
    done = drain(client, body["job_id"])[-1]

    assert done["triage"]["status"] == "error"
    assert "not on PATH" in done["triage"]["error"]
    assert done["evaluated"] == 1
    assert done["error_count"] == 0
    assert "fatal" not in done


def test_a_failed_commit_is_reported_inside_the_block(
    client, project, cli_ok, monkeypatch
):
    _fake_wave(monkeypatch, commit={"status": "error", "error": "manifest is gone"})

    _, body = post(client, {"triage": True})
    done = drain(client, body["job_id"])[-1]

    assert done["triage"]["status"] == "error"
    assert "manifest is gone" in done["triage"]["error"]
    assert done["evaluated"] == 1


def test_a_raising_wave_does_not_sink_the_completed_checkers(
    client, project, cli_ok, monkeypatch
):
    """The job runner turns an escaping exception into a fatal `complete`, which
    would report a run that did persist its findings as a dead job."""
    import src.triage.pass_ as tp

    def boom(project_dir, **kw):
        raise RuntimeError("LanguageTool took the process down")

    monkeypatch.setattr(tp, "prepare", boom)

    _, body = post(client, {"triage": True})
    done = drain(client, body["job_id"])[-1]

    assert "fatal" not in done
    assert done["evaluated"] == 1
    assert done["error_count"] == 0
    assert done["triage"]["status"] == "error"
    assert "LanguageTool" in done["triage"]["error"]


# ── remembering the answer ───────────────────────────────────────────────────

@pytest.mark.parametrize("want,expected", [(True, "on"), (False, "off")])
def test_remember_persists_the_tick(client, project, cli_ok, monkeypatch, want, expected):
    _fake_wave(monkeypatch)

    post(client, {"triage": want, "remember": True})

    assert hstate.load_config(project).get("triage_after_coded") == expected


def test_without_remember_the_config_is_untouched(client, project, cli_ok, monkeypatch):
    _fake_wave(monkeypatch)

    post(client, {"triage": True})

    assert "triage_after_coded" not in hstate.load_config(project)


@pytest.mark.parametrize("payload,fragment", [
    ({"evaluators": ["no-such-evaluator"]}, "Unknown evaluators"),
    ({"chapter_ids": ["chapter_99"]}, "No translated chunks in scope"),
])
def test_a_rejected_request_writes_no_preference(
    client, project, cli_ok, monkeypatch, payload, fragment
):
    """A 400 must not leave a decision behind on its way out.

    The write used to happen at request time, ahead of the evaluator and scope
    validation and the 409 lock check, so a request the server then refused
    still mutated `.harness/config.json` -- and did it outside the book lock,
    read-modify-writing the file against whatever held it. It runs inside the
    job body now, which is still before the wave (a CLI that cannot start must
    not cost the operator the tick) but after everything that can say no.
    """
    _fake_wave(monkeypatch)
    payload = dict(payload, triage=True, remember=True)

    rv, body = post(client, payload)

    assert rv.status_code == 400
    assert fragment in body["error"]
    assert "triage_after_coded" not in hstate.load_config(project)


def test_the_tick_is_remembered_even_when_the_wave_cannot_start(
    client, project, monkeypatch
):
    """A preference is not part of the run; losing it because the CLI was
    logged out would make the box look broken."""
    import src.harness.headless as headless

    monkeypatch.setattr(headless, "preflight_error", lambda cli, **kw: "logged out")
    _fake_wave(monkeypatch)

    post(client, {"triage": True, "remember": True})

    assert hstate.load_config(project).get("triage_after_coded") == "on"


# ── the read-only status the popup is built from ─────────────────────────────

def test_status_answers_without_preparing_anything(client, project, cli_ok):
    rv = client.get("/api/project/trproj/triage/status")
    assert rv.status_code == 200
    body = rv.get_json()

    assert body["status"] == "ok"
    assert body["triageable"] == 0
    assert "worker_model" in body["effective"]
    assert body["floor"] == 0.85
    assert not (project / ".harness" / "triage" / "manifest.json").exists()


def test_status_pins_the_calibrated_cli_over_the_books_own_backend(client, project, cli_ok):
    """What the popup will name, on a book that runs on the other family.

    The pass pins the CLI its confidence floor was calibrated on rather than
    following `headless_cli`, so the button judges findings on the calibrated
    model no matter which backend writes this book's prose.
    """
    cfg = hstate.load_config(project)
    cfg["headless_cli"] = "claude"
    hstate.save_config(project, cfg)

    body = client.get("/api/project/trproj/triage/status").get_json()

    assert body["effective"]["cli"] == "cursor"
    assert body["effective"]["cli_source"] == "repo-default"
    assert body["model_source"] == "repo-default"
    assert body["effective"]["worker_model"] == body["calibrated_model"]


def test_status_reports_what_this_book_answered_last_time(client, project, cli_ok):
    assert client.get("/api/project/trproj/triage/status").get_json()["after_coded"] is None

    cfg = hstate.load_config(project)
    cfg["triage_after_coded"] = "off"
    hstate.save_config(project, cfg)

    assert client.get("/api/project/trproj/triage/status").get_json()["after_coded"] == "off"


def test_status_rejects_a_bad_chapter_id(client, project):
    rv = client.get("/api/project/trproj/triage/status?chapters=../etc")
    assert rv.status_code == 400


def test_status_on_a_missing_project_is_a_404(client, project):
    assert client.get("/api/project/nosuchbook/triage/status").status_code == 404
