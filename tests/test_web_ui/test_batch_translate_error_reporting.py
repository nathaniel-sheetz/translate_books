"""Tests for failure reporting on the realtime batch endpoint
(``POST /api/project/<id>/translate/batch``).

Regression cover for a run where the Anthropic account had no credit: every
chunk raised, but ``chunk_error`` carried no usable chunk id, ``batch_complete``
looked identical to a clean run, and a first-chunk load failure killed the
worker thread outright — leaving the SSE stream on keepalives and the dashboard
progress bar frozen at 0% with nothing to explain why.

The contract these tests pin down:
  1. Every failure produces a ``chunk_error`` naming the chunk that failed.
  2. ``batch_complete`` reports ``error_count`` / ``translated_count`` so the
     dashboard can tell a failed run from a clean one.
  3. A failure on the *first* chunk still produces events (no dead thread).
  4. Any fatal error still terminates the stream with ``batch_complete``.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.models import Chunk, ChunkMetadata, ChunkStatus
from src.utils.file_io import save_chunk
from web_ui.app import app


def _make_chunk(chapter_id: str, position: int) -> Chunk:
    src = "Hello world. " * 20
    meta = ChunkMetadata(
        char_start=0,
        char_end=len(src),
        overlap_start=0,
        overlap_end=0,
        paragraph_count=1,
        word_count=len(src.split()),
    )
    return Chunk(
        id=f"{chapter_id}_chunk_{position:03d}",
        chapter_id=chapter_id,
        position=position,
        source_text=src,
        translated_text=None,
        metadata=meta,
        status=ChunkStatus.PENDING,
        created_at=datetime.now(),
    )


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Project with two untranslated chunks in one chapter."""
    projects_dir = tmp_path / "projects"
    chunks_dir = projects_dir / "proj1" / "chunks"
    chunks_dir.mkdir(parents=True)
    for pos in (0, 1):
        chunk = _make_chunk("chapter_001", pos)
        save_chunk(chunk, chunks_dir / f"{chunk.id}.json")

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return projects_dir / "proj1"


def _start_batch(client, chapter_ids: list[str]) -> str:
    rv = client.post(
        "/api/project/proj1/translate/batch",
        json={"chapter_ids": chapter_ids, "provider": "anthropic"},
    )
    assert rv.status_code == 200, rv.get_json()
    return rv.get_json()["job_id"]


def _wait_for_job(job_id: str, timeout: float = 5.0) -> None:
    import web_ui.app as app_module
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = app_module._batch_jobs.get(job_id)
        if job and not job["thread"].is_alive():
            return
        time.sleep(0.05)


def _drain(job_id: str) -> list[dict]:
    import web_ui.app as app_module
    job = app_module._batch_jobs[job_id]
    events: list[dict] = []
    while not job["queue"].empty():
        events.append(json.loads(job["queue"].get_nowait()))
    return events


class TestBatchTranslateErrorReporting:

    @pytest.fixture(autouse=True)
    def _mock_evals(self):
        with patch("web_ui.app.evaluate_and_persist_chunk"):
            yield

    def test_chunk_error_names_the_chunk_that_failed(self, client, project):
        """The billing failure that started this: every chunk raises. Each
        ``chunk_error`` must identify its own chunk, not "" and not the
        previous chunk's id."""

        def boom(chunk, **kwargs):
            raise RuntimeError("Claude API error: credit balance is too low")

        with patch("src.api_translator.translate_chunk_realtime", side_effect=boom), \
             patch("src.sentence_aligner.align_chapter_chunks"):
            job_id = _start_batch(client, ["chapter_001"])
            _wait_for_job(job_id)

        errors = [e for e in _drain(job_id) if e.get("event") == "chunk_error"]
        assert len(errors) == 2, f"expected one chunk_error per chunk; got {errors}"
        assert [e["chunk_id"] for e in errors] == [
            "chapter_001_chunk_000",
            "chapter_001_chunk_001",
        ]
        assert all("credit balance" in e["error"] for e in errors)

    def test_batch_complete_reports_failure_counts(self, client, project):
        """A run where everything failed must not be indistinguishable from a
        clean run — that is what made the dashboard say "Complete!"."""

        def boom(chunk, **kwargs):
            raise RuntimeError("Claude API error: credit balance is too low")

        with patch("src.api_translator.translate_chunk_realtime", side_effect=boom), \
             patch("src.sentence_aligner.align_chapter_chunks"):
            job_id = _start_batch(client, ["chapter_001"])
            _wait_for_job(job_id)

        complete = [e for e in _drain(job_id) if e.get("event") == "batch_complete"]
        assert len(complete) == 1
        assert complete[0]["error_count"] == 2
        assert complete[0]["translated_count"] == 0
        assert complete[0]["errors"], "batch_complete must carry the reason"
        assert "credit balance" in complete[0]["errors"][0]

    def test_batch_complete_reports_partial_success(self, client, project):
        """Mixed run: one ok, one failed."""
        calls = {"n": 0}

        def flaky(chunk, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return chunk.model_copy(
                    update={"translated_text": "Texto.", "status": ChunkStatus.TRANSLATED}
                )
            raise RuntimeError("overloaded")

        with patch("src.api_translator.translate_chunk_realtime", side_effect=flaky), \
             patch("src.sentence_aligner.align_chapter_chunks"):
            job_id = _start_batch(client, ["chapter_001"])
            _wait_for_job(job_id)

        complete = [e for e in _drain(job_id) if e.get("event") == "batch_complete"][0]
        assert complete["translated_count"] == 1
        assert complete["error_count"] == 1

    def test_first_chunk_load_failure_does_not_kill_the_thread(self, client, project):
        """``load_chunk`` blowing up on the *first* chunk used to raise
        UnboundLocalError inside the error handler, killing the worker with an
        empty queue: the stream never terminated and the bar never moved."""

        import web_ui.app as app_module
        real_load = app_module.load_chunk
        # The route loads every chunk synchronously to build the work list
        # before starting the thread. Let those succeed; fail only the reloads
        # the worker does, which is where the crash lived.
        selection_calls = 2
        state = {"n": 0}

        def boom_load(path, *a, **k):
            state["n"] += 1
            if state["n"] <= selection_calls:
                return real_load(path, *a, **k)
            raise ValueError("corrupt chunk JSON")

        with patch("web_ui.app.load_chunk", side_effect=boom_load), \
             patch("src.sentence_aligner.align_chapter_chunks"):
            job_id = _start_batch(client, ["chapter_001"])
            _wait_for_job(job_id)

        events = _drain(job_id)
        types = [e.get("event") for e in events]
        assert "chunk_error" in types, (
            f"first-chunk load failure must still report an error; got {types}"
        )
        errors = [e for e in events if e.get("event") == "chunk_error"]
        # Falls back to the filename stem when the chunk never loaded.
        assert errors[0]["chunk_id"] == "chapter_001_chunk_000"
        assert "batch_complete" in types, "the stream must always terminate"
        assert [e for e in events if e.get("event") == "batch_complete"][0]["error_count"] == 2

    def test_fatal_error_still_terminates_the_stream(self, client, project):
        """An error outside the per-chunk handler must still close out the job,
        otherwise the SSE stream sits on keepalives forever."""

        def boom(*a, **k):
            raise RuntimeError("blacklist store unavailable")

        with patch("src.api_translator.translate_chunk_realtime",
                   side_effect=lambda chunk, **kw: chunk.model_copy(
                       update={"translated_text": "Texto.",
                               "status": ChunkStatus.TRANSLATED})), \
             patch("web_ui.evaluations._load_project_blacklist", side_effect=boom):
            job_id = _start_batch(client, ["chapter_001"])
            _wait_for_job(job_id)

        import web_ui.app as app_module
        assert not app_module._batch_jobs[job_id]["thread"].is_alive()
        events = _drain(job_id)
        types = [e.get("event") for e in events]
        assert "batch_complete" in types, (
            f"a fatal error must still emit batch_complete; got {types}"
        )

        # Both chunks translated before the crash and both are on disk. Calling
        # them failures is the same lie the terminal event exists to stop.
        complete = [e for e in events if e.get("event") == "batch_complete"][0]
        assert complete["translated_count"] == 2
        assert complete["error_count"] == 0
        # Counts alone read as a clean run here, so the abort needs its own
        # signal or the modal reports "Complete!" of a job that died.
        assert "blacklist store unavailable" in complete["fatal"]
        assert "blacklist store unavailable" in complete["errors"][0]

    def test_fatal_error_after_partial_success_keeps_the_real_tally(self, client, project):
        """A crash mid-batch must report the chunks already on disk as
        translated, not fold every one of them into the failure count."""
        calls = {"n": 0}

        def flaky(chunk, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return chunk.model_copy(
                    update={"translated_text": "Texto.", "status": ChunkStatus.TRANSLATED}
                )
            raise RuntimeError("overloaded")

        def boom(*a, **k):
            raise RuntimeError("blacklist store unavailable")

        with patch("src.api_translator.translate_chunk_realtime", side_effect=flaky), \
             patch("web_ui.evaluations._load_project_blacklist", side_effect=boom):
            job_id = _start_batch(client, ["chapter_001"])
            _wait_for_job(job_id)

        complete = [e for e in _drain(job_id) if e.get("event") == "batch_complete"][0]
        assert complete["translated_count"] == 1
        assert complete["error_count"] == 1
        # The per-chunk failure survives alongside the fatal one.
        assert any("overloaded" in msg for msg in complete["errors"])
