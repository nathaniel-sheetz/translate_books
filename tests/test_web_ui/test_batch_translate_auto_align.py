"""Tests for the auto-combine + auto-align step added to the realtime batch
translation endpoint (``POST /api/project/<id>/translate/batch``).

After every successful chunk translation the route now:
  1. Adds the chunk's chapter_id to ``affected_chapters``.
  2. After all chunks finish, combines each affected chapter's chunks into
     a plain-text file in ``chapters/`` (via ``src.combiner.combine_chunks``).
  3. Writes an alignment file in ``alignments/`` (via
     ``src.sentence_aligner.align_chapter_chunks``).
  4. Puts a ``chapter_aligned`` event on the SSE queue.

These tests mock both ``translate_chunk_realtime`` (to avoid real LLM calls)
and ``align_chapter_chunks`` (to avoid real alignment work) and verify the
file-system side-effects and SSE events produced by the thread.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.models import Chunk, ChunkMetadata, ChunkStatus
from src.utils.file_io import save_chunk
from web_ui.app import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chunk(chapter_id: str, position: int, translated: bool = False) -> Chunk:
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
        translated_text="Hola mundo. " * 20 if translated else None,
        metadata=meta,
        status=ChunkStatus.TRANSLATED if translated else ChunkStatus.PENDING,
        created_at=datetime.now(),
    )


def _write_chunk(chunks_dir: Path, chunk: Chunk) -> Path:
    p = chunks_dir / f"{chunk.id}.json"
    save_chunk(chunk, p)
    return p


def _drain_batch(client, project_id: str, job_id: str, timeout: float = 5.0) -> list[dict]:
    """Poll the SSE endpoint and collect all events until ``batch_complete``."""
    events: list[dict] = []
    deadline = time.monotonic() + timeout
    # The SSE stream closes after batch_complete; collect lines until done.
    with client.get(
        f"/api/project/{project_id}/translate/sse?job_id={job_id}",
        buffered=False,
    ) as resp:
        for line in resp.response:
            if time.monotonic() > deadline:
                break
            line_str = line.decode("utf-8").strip() if isinstance(line, bytes) else line.strip()
            if line_str.startswith("data:"):
                try:
                    events.append(json.loads(line_str[5:].strip()))
                except json.JSONDecodeError:
                    pass
            if events and events[-1].get("event") == "batch_complete":
                break
    return events


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Project with two untranslated chunks in one chapter."""
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "proj1"
    chunks_dir = proj_dir / "chunks"
    chunks_dir.mkdir(parents=True)

    chunk1 = _make_chunk("chapter_001", 0)
    chunk2 = _make_chunk("chapter_001", 1)
    _write_chunk(chunks_dir, chunk1)
    _write_chunk(chunks_dir, chunk2)

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBatchTranslateAutoAlign:
    """After a successful realtime batch run, ``chapters/<id>.txt`` and
    ``alignments/<id>.json`` must be written automatically."""

    def _start_batch(self, client, project_name: str, chapter_ids: list[str]) -> str:
        rv = client.post(
            f"/api/project/{project_name}/translate/batch",
            json={"chapter_ids": chapter_ids, "provider": "anthropic"},
        )
        assert rv.status_code == 200, rv.get_json()
        return rv.get_json()["job_id"]

    def _wait_for_job(self, client, project_name: str, job_id: str, timeout: float = 5.0) -> None:
        """Wait for the background thread to finish."""
        import web_ui.app as app_module
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = app_module._batch_jobs.get(job_id)
            if job and not job["thread"].is_alive():
                return
            time.sleep(0.05)

    def test_combined_text_written_after_batch(self, client, project):
        """combine_chunks output must be written to chapters/<id>.txt."""

        def fake_translate(chunk, **kwargs):
            # Return the chunk with a translation added.
            return chunk.model_copy(
                update={"translated_text": "Texto traducido.", "status": ChunkStatus.TRANSLATED}
            )

        with patch("src.api_translator.translate_chunk_realtime", side_effect=fake_translate), \
             patch("src.sentence_aligner.align_chapter_chunks"):
            job_id = self._start_batch(client, "proj1", ["chapter_001"])
            self._wait_for_job(client, "proj1", job_id)

        combined_file = project / "chapters" / "chapter_001.txt"
        assert combined_file.exists(), "chapters/chapter_001.txt was not created"
        assert combined_file.read_text(encoding="utf-8").strip()

    def test_alignment_file_written_after_batch(self, client, project):
        """align_chapter_chunks must be called and alignments dir created."""
        align_calls: list[dict] = []

        def fake_translate(chunk, **kwargs):
            return chunk.model_copy(
                update={"translated_text": "Texto.", "status": ChunkStatus.TRANSLATED}
            )

        def fake_align(**kwargs):
            align_calls.append(kwargs)
            # Write a minimal alignment file so downstream code doesn't fail.
            out = kwargs.get("output_path")
            if out:
                import json as _json
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                Path(out).write_text(_json.dumps({"sentences": []}), encoding="utf-8")

        with patch("src.api_translator.translate_chunk_realtime", side_effect=fake_translate), \
             patch("src.sentence_aligner.align_chapter_chunks", side_effect=fake_align):
            job_id = self._start_batch(client, "proj1", ["chapter_001"])
            self._wait_for_job(client, "proj1", job_id)

        assert len(align_calls) == 1, "align_chapter_chunks must be called once per chapter"
        assert align_calls[0]["chapter_id"] == "chapter_001"
        align_file = project / "alignments" / "chapter_001.json"
        assert align_file.exists(), "alignments/chapter_001.json was not created"

    def test_align_failure_does_not_prevent_batch_complete(self, client, project):
        """If align_chapter_chunks raises, the batch must still complete
        (the except block is non-fatal)."""
        def fake_translate(chunk, **kwargs):
            return chunk.model_copy(
                update={"translated_text": "Texto.", "status": ChunkStatus.TRANSLATED}
            )

        def boom(**kwargs):
            raise RuntimeError("Alignment service unavailable")

        with patch("src.api_translator.translate_chunk_realtime", side_effect=fake_translate), \
             patch("src.sentence_aligner.align_chapter_chunks", side_effect=boom):
            job_id = self._start_batch(client, "proj1", ["chapter_001"])
            self._wait_for_job(client, "proj1", job_id)

        # The job thread should have finished (not hung).
        import web_ui.app as app_module
        job = app_module._batch_jobs.get(job_id)
        assert job is not None
        assert not job["thread"].is_alive(), "Batch thread should have exited after alignment failure"

    def test_only_successfully_translated_chapters_are_realigned(self, client, project):
        """When only some chunks in a chapter succeed, ``affected_chapters``
        still includes the chapter (since at least one save succeeded). However,
        ``combine_chunks`` will fail because not all chunks have translations,
        so the ``except`` block catches that error and ``align_chapter_chunks``
        is never called. The batch must still complete normally."""
        call_count = [0]

        def fake_translate(chunk, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First chunk succeeds.
                return chunk.model_copy(
                    update={"translated_text": "Ok.", "status": ChunkStatus.TRANSLATED}
                )
            else:
                # Second chunk fails — raises so the chunk is not saved.
                raise RuntimeError("LLM error")

        align_chapters: list[str] = []

        def fake_align(**kwargs):
            align_chapters.append(kwargs.get("chapter_id", ""))

        with patch("src.api_translator.translate_chunk_realtime", side_effect=fake_translate), \
             patch("src.sentence_aligner.align_chapter_chunks", side_effect=fake_align):
            job_id = self._start_batch(client, "proj1", ["chapter_001"])
            self._wait_for_job(client, "proj1", job_id)

        # chapter_001 is in affected_chapters (first chunk saved) but
        # combine_chunks raises because chunk_002 is still untranslated.
        # The except block swallows the error, so align is never called.
        assert len(align_chapters) == 0, (
            "align_chapter_chunks must not be called when combine_chunks raises "
            "(untranslated chunks present)"
        )

        # Job thread must have exited cleanly (not hung).
        import web_ui.app as app_module
        job = app_module._batch_jobs.get(job_id)
        assert job is not None
        assert not job["thread"].is_alive()

    def test_chapter_aligned_event_enqueued_for_sse(self, client, project):
        """After a successful batch a ``chapter_aligned`` event must be put on
        the job queue so the SSE stream delivers it to the dashboard."""

        def fake_translate(chunk, **kwargs):
            return chunk.model_copy(
                update={"translated_text": "Texto.", "status": ChunkStatus.TRANSLATED}
            )

        def fake_align(**kwargs):
            out = kwargs.get("output_path")
            if out:
                import json as _json
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                Path(out).write_text(_json.dumps({"sentences": []}), encoding="utf-8")

        with patch("src.api_translator.translate_chunk_realtime", side_effect=fake_translate), \
             patch("src.sentence_aligner.align_chapter_chunks", side_effect=fake_align):
            job_id = self._start_batch(client, "proj1", ["chapter_001"])
            self._wait_for_job(client, "proj1", job_id)

        import web_ui.app as app_module
        job = app_module._batch_jobs.get(job_id)
        assert job is not None

        # Drain the queue and collect all enqueued events.
        queued: list[dict] = []
        while not job["queue"].empty():
            queued.append(json.loads(job["queue"].get_nowait()))

        event_types = [e.get("event") for e in queued]
        assert "chapter_aligned" in event_types, (
            f"Expected 'chapter_aligned' event in job queue; got: {event_types}"
        )
        aligned = [e for e in queued if e.get("event") == "chapter_aligned"]
        assert aligned[0].get("chapter_id") == "chapter_001"
