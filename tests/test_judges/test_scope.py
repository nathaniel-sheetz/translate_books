"""Tests for the judge scope/addressing layer."""

from __future__ import annotations

import pytest

from src.judges.scope import ScopeError, build_targets
from src.models import Chunk, ChunkMetadata, ChunkStatus
from src.utils.file_io import save_chunk


def _chunk(cid: str, chapter: str, pos: int, translated: str | None = "El gato.") -> Chunk:
    return Chunk(
        id=cid,
        chapter_id=chapter,
        position=pos,
        source_text="The cat.",
        translated_text=translated,
        metadata=ChunkMetadata(
            char_start=0,
            char_end=10,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=2,
        ),
        status=ChunkStatus.TRANSLATED if translated else ChunkStatus.PENDING,
    )


def _write(tmp_path, chunk: Chunk):
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir(exist_ok=True)
    save_chunk(chunk, chunks_dir / f"{chunk.id}.json")


def test_chunk_scope(tmp_path):
    _write(tmp_path, _chunk("chapter_01_chunk_000", "chapter_01", 0))
    targets = build_targets(tmp_path, "chunk:chapter_01_chunk_000")
    assert len(targets) == 1
    assert targets[0].id == "chapter_01_chunk_000"
    assert targets[0].target_type == "chunk"
    assert targets[0].translated_text == "El gato."


def test_chapter_scope_sorted_by_position(tmp_path):
    _write(tmp_path, _chunk("chapter_01_chunk_001", "chapter_01", 1))
    _write(tmp_path, _chunk("chapter_01_chunk_000", "chapter_01", 0))
    _write(tmp_path, _chunk("chapter_02_chunk_000", "chapter_02", 0))

    targets = build_targets(tmp_path, "chapter:chapter_01")
    assert [t.id for t in targets] == [
        "chapter_01_chunk_000",
        "chapter_01_chunk_001",
    ]


def test_untranslated_chunk_raises(tmp_path):
    _write(tmp_path, _chunk("chapter_01_chunk_000", "chapter_01", 0, translated=None))
    with pytest.raises(ScopeError):
        build_targets(tmp_path, "chunk:chapter_01_chunk_000")


def test_missing_chunk_raises(tmp_path):
    (tmp_path / "chunks").mkdir()
    with pytest.raises(ScopeError):
        build_targets(tmp_path, "chunk:nope")


def test_empty_chapter_raises(tmp_path):
    (tmp_path / "chunks").mkdir()
    with pytest.raises(ScopeError):
        build_targets(tmp_path, "chapter:chapter_99")


def test_malformed_scope_raises(tmp_path):
    with pytest.raises(ScopeError):
        build_targets(tmp_path, "chapter")  # no colon


def test_unknown_scope_kind_raises(tmp_path):
    with pytest.raises(ScopeError):
        build_targets(tmp_path, "bogus:x")


def test_designed_for_scopes_not_implemented(tmp_path):
    with pytest.raises(NotImplementedError):
        build_targets(tmp_path, "sentences:chapter_01:1,2")
    with pytest.raises(NotImplementedError):
        build_targets(tmp_path, "flags:chapter_01")
