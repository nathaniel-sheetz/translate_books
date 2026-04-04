"""Tests for the pipeline orchestrator (translate_book.py)."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.translate_book import (
    load_pipeline_state,
    save_pipeline_state,
    discover_chapters,
    STAGES,
)


class TestPipelineState:
    """Tests for checkpoint/resume via pipeline_state.json."""

    def test_load_empty_state(self, tmp_path):
        state = load_pipeline_state(tmp_path)
        assert state == {}

    def test_save_and_load_state(self, tmp_path):
        state = {"stage_completed": "chunk", "total_chunks": 12}
        save_pipeline_state(tmp_path, state)

        loaded = load_pipeline_state(tmp_path)
        assert loaded["stage_completed"] == "chunk"
        assert loaded["total_chunks"] == 12
        assert "updated_at" in loaded

    def test_state_overwrites(self, tmp_path):
        save_pipeline_state(tmp_path, {"stage_completed": "ingest"})
        save_pipeline_state(tmp_path, {"stage_completed": "split", "chapter_count": 5})

        loaded = load_pipeline_state(tmp_path)
        assert loaded["stage_completed"] == "split"
        assert loaded["chapter_count"] == 5


class TestDiscoverChapters:
    """Tests for chapter discovery from chunk filenames."""

    def test_discover_basic(self, tmp_path):
        # Create mock chunk files
        for ch in ["chapter_01", "chapter_02"]:
            for i in range(3):
                (tmp_path / f"{ch}_chunk_{i:03d}.json").write_text("{}")

        chapters = discover_chapters(tmp_path)
        assert len(chapters) == 2
        assert "chapter_01" in chapters
        assert "chapter_02" in chapters
        assert len(chapters["chapter_01"]) == 3

    def test_discover_empty_dir(self, tmp_path):
        chapters = discover_chapters(tmp_path)
        assert chapters == {}

    def test_discover_ignores_non_chunk_files(self, tmp_path):
        (tmp_path / "chapter_01_chunk_000.json").write_text("{}")
        (tmp_path / "glossary.json").write_text("{}")
        (tmp_path / "project.json").write_text("{}")

        chapters = discover_chapters(tmp_path)
        assert len(chapters) == 1

    def test_discover_sorted_order(self, tmp_path):
        # Create out of order
        for name in ["chapter_03_chunk_000.json", "chapter_01_chunk_000.json", "chapter_02_chunk_000.json"]:
            (tmp_path / name).write_text("{}")

        chapters = discover_chapters(tmp_path)
        keys = list(chapters.keys())
        assert keys == ["chapter_01", "chapter_02", "chapter_03"]


class TestStageOrder:
    """Tests for pipeline stage ordering."""

    def test_stages_order(self):
        assert STAGES == [
            "ingest", "split", "chunk", "translate",
            "evaluate", "combine", "epub", "align",
        ]

    def test_resume_from_completed_stage(self, tmp_path):
        """Verify resume logic finds correct next stage."""
        state = {"stage_completed": "chunk"}
        completed = state["stage_completed"]
        start_idx = STAGES.index(completed) + 1
        assert STAGES[start_idx] == "translate"

    def test_resume_from_last_stage(self):
        """Completed last stage means pipeline is done."""
        state = {"stage_completed": "align"}
        completed = state["stage_completed"]
        start_idx = STAGES.index(completed) + 1
        assert start_idx >= len(STAGES)


class TestStageIngest:
    """Tests for the ingest stage."""

    def test_ingest_skips_when_source_exists(self, tmp_path):
        """If source.txt exists, ingest should skip."""
        (tmp_path / "source.txt").write_text("Some book text here.")

        args = MagicMock()
        args.url = None

        from scripts.translate_book import stage_ingest
        state = stage_ingest(args, tmp_path, {})
        assert state["stage_completed"] == "ingest"

    def test_ingest_requires_url_when_no_source(self, tmp_path):
        args = MagicMock()
        args.url = None

        from scripts.translate_book import stage_ingest
        with pytest.raises(ValueError, match="--url is required"):
            stage_ingest(args, tmp_path, {})


class TestStageSplit:
    """Tests for the split stage."""

    def test_split_requires_source_file(self, tmp_path):
        args = MagicMock()
        args.chapter_pattern = "roman"
        args.custom_regex = None
        args.min_chapter_size = 100

        from scripts.translate_book import stage_split
        with pytest.raises(FileNotFoundError):
            stage_split(args, tmp_path, {})


class TestStageChunk:
    """Tests for the chunk stage."""

    def test_chunk_requires_chapter_files(self, tmp_path):
        (tmp_path / "chapters").mkdir()

        args = MagicMock()
        args.chunk_size = 2000
        args.overlap_paragraphs = 1
        args.min_overlap_words = 50

        from scripts.translate_book import stage_chunk
        with pytest.raises(FileNotFoundError):
            stage_chunk(args, tmp_path, {})

    def test_chunk_creates_chunks_dir(self, tmp_path):
        """Chunk stage creates chunks/ directory."""
        chapters_dir = tmp_path / "chapters"
        chapters_dir.mkdir()

        # Write a minimal chapter
        (chapters_dir / "chapter_01.txt").write_text(
            "Chapter I\n\n" + " ".join(["word"] * 500)
        )

        args = MagicMock()
        args.chunk_size = 2000
        args.overlap_paragraphs = 0
        args.min_overlap_words = 0

        from scripts.translate_book import stage_chunk
        state = stage_chunk(args, tmp_path, {})

        assert state["stage_completed"] == "chunk"
        assert (tmp_path / "chunks").exists()
        chunk_files = list((tmp_path / "chunks").glob("*.json"))
        assert len(chunk_files) >= 1
