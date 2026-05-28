"""Edit-preservation contract for the edit-review baseline pointer.

User edits to a chunk MUST NOT touch chunk.last_llm_log. The edit-review
report uses this stamp to anchor every diff against the original LLM
baseline, so orphaning the stamp on every edit would silently collapse the
whole feature into the heuristic fallback path documented in
docs/EDIT_REVIEW.md.

Covers both endpoints that route through _replace_chunk_translation:
  - POST /api/chunk/<p>/<c>/edit                     (chunk editor textarea)
  - POST /api/project/<p>/chunks/<c>/translate       (manual paste / save)
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.models import Chunk, ChunkMetadata, ChunkStatus
from src.utils.file_io import load_chunk, save_chunk
from web_ui.app import app


_PRESET_STAMP = "prompts/history/20260101_000000_translation_abc123.json"


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def project_with_stamped_chunk(tmp_path, monkeypatch):
    """One-chunk project where the chunk already carries an LLM baseline
    stamp. One chunk per chapter so combine_chunks doesn't trip on an
    untranslated sibling during the post-edit pipeline."""
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "proj1"
    chunks_dir = proj_dir / "chunks"
    chunks_dir.mkdir(parents=True)

    src = "It is a truth universally acknowledged. " * 5
    meta = ChunkMetadata(
        char_start=0, char_end=len(src),
        overlap_start=0, overlap_end=0,
        paragraph_count=1, word_count=len(src.split()),
    )
    chunk = Chunk(
        id="chapter_001_chunk_000",
        chapter_id="chapter_001",
        position=0,
        source_text=src,
        translated_text="Original LLM-generated translation.",
        last_llm_log=_PRESET_STAMP,
        metadata=meta,
        status=ChunkStatus.TRANSLATED,
        created_at=datetime.now(),
    )
    save_chunk(chunk, chunks_dir / f"{chunk.id}.json")

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


def test_chunk_edit_endpoint_preserves_last_llm_log(
    client, project_with_stamped_chunk,
):
    """POST /api/chunk/<p>/<c>/edit (chunk-editor textarea) must leave
    last_llm_log untouched after the edit lands."""
    chunk_path = project_with_stamped_chunk / "chunks" / "chapter_001_chunk_000.json"
    assert load_chunk(chunk_path).last_llm_log == _PRESET_STAMP  # fixture sanity

    with patch("src.sentence_aligner.align_chapter_chunks"):
        rv = client.post(
            "/api/chunk/proj1/chapter_001_chunk_000/edit",
            json={"translated_text": "User-edited translation goes here."},
        )

    assert rv.status_code == 200, rv.get_json()
    reloaded = load_chunk(chunk_path)
    assert reloaded.translated_text == "User-edited translation goes here."
    assert reloaded.last_llm_log == _PRESET_STAMP, (
        "Editing a chunk MUST NOT touch last_llm_log — the edit-review "
        "report's baseline pointer must survive arbitrary user edits."
    )


def test_manual_translate_endpoint_preserves_last_llm_log(
    client, project_with_stamped_chunk,
):
    """POST /api/project/<p>/chunks/<c>/translate (the 'Save a manual
    translation' endpoint — pastes from outside, manual saves) routes
    through the same _replace_chunk_translation pipeline as the editor
    and must likewise preserve last_llm_log.

    Note: per docs/EDIT_REVIEW.md Known Limit #1, this also means a paste
    of a wholly new translation over an LLM-stamped chunk currently diffs
    against the now-irrelevant old baseline. That's a UX limit, not a
    correctness bug — the stamp itself is preserved as the contract
    requires."""
    chunk_path = project_with_stamped_chunk / "chunks" / "chapter_001_chunk_000.json"

    with patch("src.sentence_aligner.align_chapter_chunks"):
        rv = client.post(
            "/api/project/proj1/chunks/chapter_001_chunk_000/translate",
            json={"translated_text": "Pasted translation."},
        )

    assert rv.status_code == 200, rv.get_json()
    reloaded = load_chunk(chunk_path)
    assert reloaded.translated_text == "Pasted translation."
    assert reloaded.last_llm_log == _PRESET_STAMP
