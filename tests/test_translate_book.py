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
    parse_chapter_range,
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


class TestParseChapterRange:
    """Tests for --chapters argument parsing."""

    def test_single_chapter(self):
        assert parse_chapter_range("3") == {"chapter_03"}

    def test_range(self):
        assert parse_chapter_range("1-3") == {"chapter_01", "chapter_02", "chapter_03"}

    def test_comma_separated(self):
        assert parse_chapter_range("3,7,12") == {"chapter_03", "chapter_07", "chapter_12"}

    def test_mixed(self):
        result = parse_chapter_range("1-3,7,10-12")
        assert result == {"chapter_01", "chapter_02", "chapter_03", "chapter_07",
                          "chapter_10", "chapter_11", "chapter_12"}

    def test_large_chapter_number(self):
        assert parse_chapter_range("99") == {"chapter_99"}


class TestStageOrder:
    """Tests for pipeline stage ordering."""

    def test_stages_order(self):
        assert STAGES == [
            "ingest", "split", "chunk", "translate",
            "evaluate", "combine", "epub", "align", "footnotes",
        ]

    def test_resume_from_completed_stage(self, tmp_path):
        """Verify resume logic finds correct next stage."""
        state = {"stage_completed": "chunk"}
        completed = state["stage_completed"]
        start_idx = STAGES.index(completed) + 1
        assert STAGES[start_idx] == "translate"

    def test_resume_from_last_stage(self):
        """Completed last stage means pipeline is done."""
        state = {"stage_completed": STAGES[-1]}
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

    def test_ingest_stashes_heading_report_and_pattern(self, tmp_path):
        """The URL/HTML path records heading-derived hints (a per-chapter report
        and an auto-suggested split pattern) so the harness can relay them."""
        html = (
            "<html><body>"
            "<h2>Chapter I</h2><p>" + ("word " * 60) + "</p>"
            "<h2>Chapter II</h2><p>" + ("word " * 60) + "</p>"
            "</body></html>"
        )
        src = tmp_path / "book.html"
        src.write_text(html, encoding="utf-8")

        args = MagicMock()
        args.url = str(src)  # fetch_html reads a local path

        from scripts.translate_book import stage_ingest
        state = stage_ingest(args, tmp_path, {})

        assert state["stage_completed"] == "ingest"
        assert [c["heading"] for c in state["chapter_report"]] == ["Chapter I", "Chapter II"]
        assert state["suggested_pattern"] is not None


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
        args.overlap_paragraphs = 0
        args.min_overlap_words = 0

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


# ---------------------------------------------------------------------------
# TestStageEpub
# ---------------------------------------------------------------------------

class TestStageEpub:
    """Tests for stage_epub in translate_book.py."""

    def _write_chunk(self, chunks_dir, chapter_id, position, source, translated):
        chunk_id = f"{chapter_id}_chunk_{position:03d}"
        payload = {
            "id": chunk_id,
            "chapter_id": chapter_id,
            "position": position,
            "source_text": source,
            "translated_text": translated,
            "metadata": {
                "char_start": 0,
                "char_end": len(source),
                "overlap_start": 0,
                "overlap_end": 0,
                "paragraph_count": 1,
                "word_count": len(source.split()),
            },
        }
        (chunks_dir / f"{chunk_id}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_stage_epub_builds_epub_and_updates_state(self, tmp_path):
        (tmp_path / "images").mkdir()
        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        self._write_chunk(chunks_dir, "chapter_01", 0, "Hello.", "CAPÍTULO I\n\nHola.")

        from scripts.translate_book import stage_epub
        args = MagicMock()
        args.project_name = "My Book"
        args.author = "Author"
        args.target_lang_code = "es"
        args.chapters = None

        state = stage_epub(args, tmp_path, {})

        assert state["stage_completed"] == "epub"
        assert "epub_path" in state
        assert Path(state["epub_path"]).exists()
        assert state["epub_included_chapters"] == ["chapter_01"]
        assert state["epub_skipped_chapters"] == []

    def test_stage_epub_with_chapters_filter(self, tmp_path):
        (tmp_path / "images").mkdir()
        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        self._write_chunk(chunks_dir, "chapter_01", 0, "Hello.", "CAPÍTULO I\n\nHola.")
        self._write_chunk(chunks_dir, "chapter_02", 0, "World.", "CAPÍTULO II\n\nMundo.")

        from scripts.translate_book import stage_epub
        args = MagicMock()
        args.project_name = "Filtered Book"
        args.author = "Author"
        args.target_lang_code = "es"
        args.chapters = "1"  # parse_chapter_range("1") == {"chapter_01"}

        state = stage_epub(args, tmp_path, {})

        assert state["epub_included_chapters"] == ["chapter_01"]
        assert "chapter_02" not in state["epub_included_chapters"]

    def test_stage_epub_prints_skipped_when_present(self, tmp_path, capsys):
        (tmp_path / "images").mkdir()
        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        self._write_chunk(chunks_dir, "chapter_01", 0, "Hello.", "CAPÍTULO I\n\nHola.")
        self._write_chunk(chunks_dir, "chapter_02", 0, "World.", None)  # untranslated

        from scripts.translate_book import stage_epub
        args = MagicMock()
        args.project_name = "Book"
        args.author = "Author"
        args.target_lang_code = "es"
        args.chapters = None

        stage_epub(args, tmp_path, {})
        captured = capsys.readouterr()
        assert "Skipped" in captured.out
        assert "chapter_02" in captured.out

    def test_stage_epub_no_skipped_line_when_all_translated(self, tmp_path, capsys):
        (tmp_path / "images").mkdir()
        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        self._write_chunk(chunks_dir, "chapter_01", 0, "Hello.", "CAPÍTULO I\n\nHola.")

        from scripts.translate_book import stage_epub
        args = MagicMock()
        args.project_name = "Book"
        args.author = "Author"
        args.target_lang_code = "es"
        args.chapters = None

        stage_epub(args, tmp_path, {})
        captured = capsys.readouterr()
        assert "Skipped" not in captured.out


# ---------------------------------------------------------------------------
# TestStageAlign
# ---------------------------------------------------------------------------

class TestStageAlign:
    """API auto-chain: stage_align must put coverage_warnings on the final sentinel."""

    def _write_chunk(self, chunks_dir, chapter_id, position, source, translated):
        chunk_id = f"{chapter_id}_chunk_{position:03d}"
        payload = {
            "id": chunk_id,
            "chapter_id": chapter_id,
            "position": position,
            "source_text": source,
            "translated_text": translated,
            "metadata": {
                "char_start": 0,
                "char_end": len(source),
                "overlap_start": 0,
                "overlap_end": 0,
                "paragraph_count": 1,
                "word_count": len(source.split()),
            },
        }
        (chunks_dir / f"{chunk_id}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_stage_align_emits_coverage_warnings_on_translate_result(
        self, tmp_path, monkeypatch, capsys,
    ):
        """Last HARNESS_RESULT must carry gaps so API translate last_output sees them."""
        from scripts import translate_book as tb

        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        self._write_chunk(chunks_dir, "chapter_01", 0, "Hello.", "Hola.")

        gap = {
            "position": "tail", "en_start": 45, "en_end": 47, "sentences": 3,
            "chars": 749, "preview": "Richard was bidden…",
            "chunk_id": "chapter_01_chunk_000",
        }

        def fake_align(chunk_paths, project_id, chapter_id, source_lang="en",
                       target_lang="es", output_path=None):
            if output_path:
                Path(output_path).write_text(
                    json.dumps({"chapter_id": chapter_id, "alignments": []}),
                    encoding="utf-8",
                )
            return {
                "chapter_id": chapter_id, "es_count": 1, "high_confidence_pct": 100.0,
                "gaps": [gap],
            }

        monkeypatch.setattr(tb, "align_chapter_chunks", fake_align)

        args = MagicMock()
        args.source_lang_code = "en"
        args.target_lang_code = "es"
        translate_payload = {
            "stage": "translate",
            "translated": 1,
            "chapters_done": ["chapter_01"],
            "estimated_cost_usd": 0.01,
            "remaining_untranslated": 0,
        }
        state = {"_harness_translate_result": translate_payload}

        out = tb.stage_align(args, tmp_path, state)
        assert out["stage_completed"] == "align"

        captured = capsys.readouterr().out
        sentinel_lines = [
            line for line in captured.splitlines()
            if line.startswith("HARNESS_RESULT:")
        ]
        assert len(sentinel_lines) == 1
        payload = json.loads(sentinel_lines[0].removeprefix("HARNESS_RESULT:").strip())
        assert payload["stage"] == "translate"
        assert payload["translated"] == 1
        assert payload["coverage_warnings"] == [{"chapter_id": "chapter_01", **gap}]
        assert "COVERAGE WARNING" in payload["instructions"]

    def test_stage_align_emits_empty_warnings_when_clean(
        self, tmp_path, monkeypatch, capsys,
    ):
        from scripts import translate_book as tb

        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        self._write_chunk(chunks_dir, "chapter_01", 0, "Hello.", "Hola.")

        def fake_align(chunk_paths, project_id, chapter_id, source_lang="en",
                       target_lang="es", output_path=None):
            if output_path:
                Path(output_path).write_text("{}", encoding="utf-8")
            return {
                "chapter_id": chapter_id, "es_count": 1, "high_confidence_pct": 100.0,
                "gaps": [],
            }

        monkeypatch.setattr(tb, "align_chapter_chunks", fake_align)

        args = MagicMock()
        args.source_lang_code = "en"
        args.target_lang_code = "es"
        tb.stage_align(args, tmp_path, {})

        captured = capsys.readouterr().out
        sentinel_lines = [
            line for line in captured.splitlines()
            if line.startswith("HARNESS_RESULT:")
        ]
        payload = json.loads(sentinel_lines[0].removeprefix("HARNESS_RESULT:").strip())
        assert payload["stage"] == "align"
        assert payload["coverage_warnings"] == []
        assert "instructions" not in payload

    def test_stage_align_preserves_early_exit_translate_note(
        self, tmp_path, monkeypatch, capsys,
    ):
        """Already-translated early exit must keep its note after align re-emits."""
        from scripts import translate_book as tb

        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        self._write_chunk(chunks_dir, "chapter_01", 0, "Hello.", "Hola.")

        def fake_align(chunk_paths, project_id, chapter_id, source_lang="en",
                       target_lang="es", output_path=None):
            if output_path:
                Path(output_path).write_text("{}", encoding="utf-8")
            return {
                "chapter_id": chapter_id, "es_count": 1, "high_confidence_pct": 100.0,
                "gaps": [],
            }

        monkeypatch.setattr(tb, "align_chapter_chunks", fake_align)

        args = MagicMock()
        args.source_lang_code = "en"
        args.target_lang_code = "es"
        state = {
            "_harness_translate_result": {
                "stage": "translate",
                "translated": 0,
                "total_chunks_in_scope": 1,
                "note": "all chunks already translated",
            },
        }
        tb.stage_align(args, tmp_path, state)

        captured = capsys.readouterr().out
        sentinel_lines = [
            line for line in captured.splitlines()
            if line.startswith("HARNESS_RESULT:")
        ]
        payload = json.loads(sentinel_lines[0].removeprefix("HARNESS_RESULT:").strip())
        assert payload["stage"] == "translate"
        assert payload["note"] == "all chunks already translated"
        assert payload["coverage_warnings"] == []
        assert "instructions" not in payload
