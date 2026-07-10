"""Light coverage for prompt-caching visibility on the realtime translate path.

Covers:
  - ``/translate/cost-estimate`` returning dialogue/image feature counts
  - ``/translate/batch`` accepting and threading always_include_* flags
"""

from __future__ import annotations

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


def _make_chunk(chapter_id: str, position: int, source: str) -> Chunk:
    meta = ChunkMetadata(
        char_start=0,
        char_end=len(source),
        overlap_start=0,
        overlap_end=0,
        paragraph_count=max(1, source.count("\n\n") + 1),
        word_count=len(source.split()),
    )
    return Chunk(
        id=f"{chapter_id}_chunk_{position:03d}",
        chapter_id=chapter_id,
        position=position,
        source_text=source,
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
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "cacheproj"
    chunks_dir = proj_dir / "chunks"
    chunks_dir.mkdir(parents=True)

    dialogue = (
        chr(34) + "We must leave at once," + chr(34) + " she said.\n\n"
        "He nodded and reached for his coat."
    )
    image = "Para one.\n\n[IMAGE:images/i01.jpg]\n\nPara two."
    plain = "He walked slowly down the lane.\n\nThe trees were silent."

    for i, src in enumerate([dialogue, image, plain]):
        chunk = _make_chunk("chapter_001", i, src)
        save_chunk(chunk, chunks_dir / f"{chunk.id}.json")

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


class TestTranslateCostEstimateFeatures:
    def test_returns_feature_counts(self, client, project):
        rv = client.post(
            "/api/project/cacheproj/translate/cost-estimate",
            json={
                "chapter_ids": ["chapter_001"],
                "provider": "anthropic",
                "model": "claude-3-5-sonnet-20241022",
                "include_translated": True,
            },
        )
        assert rv.status_code == 200, rv.get_json()
        data = rv.get_json()
        assert data["total_chunks"] == 3
        assert data["dialogue_chunk_count"] == 1
        assert data["image_chunk_count"] == 1
        assert "suggested_always_dialogue" in data
        assert data["suggested_always_images"] is True


class TestTranslateBatchFlags:
    def test_threads_always_include_flags(self, client, project):
        captured: list[dict] = []

        def fake_translate(chunk, **kwargs):
            captured.append(kwargs)
            return chunk.model_copy(
                update={
                    "translated_text": "Texto.",
                    "status": ChunkStatus.TRANSLATED,
                }
            )

        with patch("src.api_translator.translate_chunk_realtime", side_effect=fake_translate), \
             patch("src.sentence_aligner.align_chapter_chunks"), \
             patch("web_ui.app.evaluate_and_persist_chunk"):
            rv = client.post(
                "/api/project/cacheproj/translate/batch",
                json={
                    "chapter_ids": ["chapter_001"],
                    "provider": "anthropic",
                    "include_translated": True,
                    "always_include_dialogue": True,
                    "always_include_image_instructions": True,
                },
            )
            assert rv.status_code == 200, rv.get_json()
            job_id = rv.get_json()["job_id"]

            import web_ui.app as app_module
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                job = app_module._batch_jobs.get(job_id)
                if job and not job["thread"].is_alive():
                    break
                time.sleep(0.05)

        assert len(captured) == 3
        for kwargs in captured:
            assert kwargs.get("always_include_dialogue") is True
            assert kwargs.get("always_include_image_instructions") is True
