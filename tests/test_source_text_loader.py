"""Tests for src/utils/source_text.py priority order."""

from __future__ import annotations

import json
from pathlib import Path

from src.utils.source_text import load_clean_source_text


def _write_chapter(project_dir: Path, name: str, text: str) -> None:
    chapters = project_dir / "chapters"
    chapters.mkdir(exist_ok=True)
    (chapters / name).write_text(text, encoding="utf-8")


def _write_chunk(project_dir: Path, name: str, source_text: str, translated_text: str = "") -> None:
    chunks = project_dir / "chunks"
    chunks.mkdir(exist_ok=True)
    payload = {
        "id": name.replace(".json", ""),
        "source_text": source_text,
        "translated_text": translated_text,
    }
    (chunks / name).write_text(json.dumps(payload), encoding="utf-8")


class TestPriority:
    def test_chunks_preferred_over_chapters(self, tmp_path: Path):
        # When both chunks/ and chapters/ exist, chunks/source_text wins —
        # this protects feature detection / style-guide work from picking
        # up translated text on chapters/ that has been overwritten.
        _write_chunk(tmp_path, "chapter_01_chunk_000.json", "ENGLISH SOURCE")
        _write_chapter(tmp_path, "chapter_01.txt", "TRADUCCIÓN ESPAÑOLA")

        text, _, kind = load_clean_source_text(tmp_path)
        assert kind == "chunks"
        assert "ENGLISH SOURCE" in text
        assert "TRADUCCIÓN" not in text

    def test_chunks_use_source_text_field_not_translated(self, tmp_path: Path):
        # Even when translated_text is populated, only source_text is used.
        _write_chunk(
            tmp_path,
            "chapter_01_chunk_000.json",
            source_text="The ant carried the leaf.",
            translated_text="La hormiga llevaba la hoja.",
        )
        text, _, kind = load_clean_source_text(tmp_path)
        assert kind == "chunks"
        assert "ant carried" in text
        assert "hormiga" not in text

    def test_chapters_used_when_no_chunks(self, tmp_path: Path):
        _write_chapter(tmp_path, "chapter_01.txt", "Plain chapter text.")
        text, _, kind = load_clean_source_text(tmp_path)
        assert kind == "chapters"
        assert text == "Plain chapter text."

    def test_source_txt_fallback(self, tmp_path: Path):
        (tmp_path / "source.txt").write_text("Raw source.", encoding="utf-8")
        text, _, kind = load_clean_source_text(tmp_path)
        assert kind == "source"
        assert text == "Raw source."

    def test_empty_project_returns_empty(self, tmp_path: Path):
        text, mtime, kind = load_clean_source_text(tmp_path)
        assert text == ""
        assert mtime is None
        assert kind == ""
