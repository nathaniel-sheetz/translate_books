"""
Tests for the web UI backend (TranslationSession).

These tests verify the core functionality of the Flask backend
without requiring the full Flask app to run.
"""

import json
import tempfile
from pathlib import Path

import pytest

from src.models import Chunk, ChunkMetadata, ChunkStatus
from src.utils.file_io import load_glossary, load_style_guide, save_chunk

# Import the TranslationSession class from web_ui/app.py
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "web_ui"))
from app import TranslationSession


@pytest.fixture
def temp_chunks_dir(tmp_path):
    """Create a temporary directory with test chunks."""
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()

    # Create 3 test chunks (2 untranslated, 1 translated)
    chunks = [
        Chunk(
            id="test_chunk_001",
            chapter_id="chapter_01",
            position=1,
            source_text="This is the first chunk of English text.",
            translated_text=None,  # Untranslated
            metadata=ChunkMetadata(
                char_start=0,
                char_end=42,
                overlap_start=0,
                overlap_end=0,
                paragraph_count=1,
                word_count=8,
            ),
            status=ChunkStatus.PENDING,
        ),
        Chunk(
            id="test_chunk_002",
            chapter_id="chapter_01",
            position=2,
            source_text="This is the second chunk of English text.",
            translated_text="Este es el segundo fragmento de texto en inglés.",  # Translated
            metadata=ChunkMetadata(
                char_start=42,
                char_end=84,
                overlap_start=0,
                overlap_end=0,
                paragraph_count=1,
                word_count=8,
            ),
            status=ChunkStatus.TRANSLATED,
        ),
        Chunk(
            id="test_chunk_003",
            chapter_id="chapter_01",
            position=3,
            source_text="This is the third chunk of English text.",
            translated_text=None,  # Untranslated
            metadata=ChunkMetadata(
                char_start=84,
                char_end=125,
                overlap_start=0,
                overlap_end=0,
                paragraph_count=1,
                word_count=8,
            ),
            status=ChunkStatus.PENDING,
        ),
    ]

    # Save chunks to directory
    for chunk in chunks:
        save_chunk(chunk, chunks_dir / f"{chunk.id}.json")

    return chunks_dir


@pytest.fixture
def glossary_fixture():
    """Load sample glossary from fixtures."""
    return load_glossary(Path("tests/fixtures/glossary_sample.json"))


@pytest.fixture
def style_guide_fixture():
    """Load sample style guide from fixtures."""
    return load_style_guide(Path("tests/fixtures/style_guide_sample.json"))


def test_session_creation(temp_chunks_dir):
    """Test creating a TranslationSession."""
    session = TranslationSession(chunks_dir=str(temp_chunks_dir))

    assert session.chunks_dir == str(temp_chunks_dir)
    assert len(session.chunks) == 3
    assert session.project_name == "Translation Project"
    assert session.source_language == "English"
    assert session.target_language == "Spanish"


def test_session_with_glossary_and_style(temp_chunks_dir, glossary_fixture, style_guide_fixture):
    """Test creating a session with glossary and style guide."""
    session = TranslationSession(
        chunks_dir=str(temp_chunks_dir),
        glossary=glossary_fixture,
        style_guide=style_guide_fixture,
        project_name="Test Project",
    )

    assert session.glossary == glossary_fixture
    assert session.style_guide == style_guide_fixture
    assert session.project_name == "Test Project"


def test_get_next_untranslated_chunk(temp_chunks_dir):
    """Test finding the next untranslated chunk."""
    session = TranslationSession(chunks_dir=str(temp_chunks_dir))

    # Should return chunk_001 (first untranslated)
    next_chunk = session.get_next_untranslated_chunk()

    assert next_chunk is not None
    assert next_chunk.id == "test_chunk_001"
    assert next_chunk.position == 1
    assert next_chunk.translated_text is None


def test_get_next_untranslated_skips_translated(temp_chunks_dir):
    """Test that get_next_untranslated_chunk skips translated chunks."""
    session = TranslationSession(chunks_dir=str(temp_chunks_dir))

    # Mark chunk_001 as translated
    session.save_translation("test_chunk_001", "Primera traducción")

    # Should now return chunk_003 (skipping chunk_002 which is already translated)
    next_chunk = session.get_next_untranslated_chunk()

    assert next_chunk is not None
    assert next_chunk.id == "test_chunk_003"
    assert next_chunk.position == 3


def test_get_next_untranslated_all_complete(temp_chunks_dir):
    """Test get_next_untranslated_chunk when all chunks are translated."""
    session = TranslationSession(chunks_dir=str(temp_chunks_dir))

    # Translate all untranslated chunks
    session.save_translation("test_chunk_001", "Primera traducción")
    session.save_translation("test_chunk_003", "Tercera traducción")

    # Should return None (all complete)
    next_chunk = session.get_next_untranslated_chunk()

    assert next_chunk is None


def test_render_chunk_prompt(temp_chunks_dir):
    """Test rendering a complete prompt for a chunk."""
    session = TranslationSession(chunks_dir=str(temp_chunks_dir))

    chunk = session.chunks[0]  # test_chunk_001
    prompt = session.render_chunk_prompt(chunk)

    # Verify prompt contains expected content
    assert "BOOK TRANSLATION TASK" in prompt
    assert "Translation Project" in prompt
    assert chunk.source_text in prompt
    assert "English" in prompt
    assert "Spanish" in prompt

    # Verify header comments are stripped
    assert "# Translation Prompt Template" not in prompt
    assert "# Version: 1.0" not in prompt


def test_render_chunk_prompt_with_glossary(temp_chunks_dir, glossary_fixture):
    """Test rendering prompt with glossary included."""
    session = TranslationSession(
        chunks_dir=str(temp_chunks_dir), glossary=glossary_fixture
    )

    chunk = session.chunks[0]
    prompt = session.render_chunk_prompt(chunk)

    # Verify glossary terms are included
    assert "Harry" in prompt or "CHARACTER" in prompt


def test_render_chunk_prompt_with_previous_context(tmp_path):
    """Test that previous_chapter source text is used as context when no chunk has been translated."""
    from src.utils.file_io import save_chunk
    # Create a chunks dir with only untranslated chunks so last_source_text stays None
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    chunk = Chunk(
        id="fresh_chunk_001",
        chapter_id="chapter_01",
        position=1,
        source_text="First untranslated chunk.",
        translated_text=None,
        metadata=ChunkMetadata(
            char_start=0, char_end=25, overlap_start=0, overlap_end=0,
            paragraph_count=1, word_count=3,
        ),
        status=ChunkStatus.PENDING,
    )
    save_chunk(chunk, chunks_dir / f"{chunk.id}.json")

    prev_chapter_text = "Previous chapter ending.\n\nLast paragraph of chapter."
    session = TranslationSession(
        chunks_dir=str(chunks_dir),
        previous_chapter=prev_chapter_text,
        include_context=True,
        context_paragraphs=1,
    )

    prompt = session.render_chunk_prompt(session.chunks[0])

    # previous_chapter is used as fallback when no chunk has been translated yet
    assert "Previous Section (Original English)" in prompt
    assert "Last paragraph of chapter" in prompt


def test_render_chunk_prompt_context_uses_previous_chunk(temp_chunks_dir):
    """Test that context comes from the sequentially previous chunk, not most recent."""
    session = TranslationSession(chunks_dir=str(temp_chunks_dir))

    # Chunk 0 is first in sequence — no previous chunk context
    prompt_0 = session.render_chunk_prompt(session.chunks[0])
    assert "Previous Section" not in prompt_0

    # Chunk 2 (index 2) should get context from chunk 1 (index 1), not
    # from whatever was most recently translated
    prompt_2 = session.render_chunk_prompt(session.chunks[2])
    assert "Previous Section (Original English)" in prompt_2
    assert "second chunk of English text" in prompt_2


def test_save_translation(temp_chunks_dir):
    """Test saving a translation to a chunk."""
    session = TranslationSession(chunks_dir=str(temp_chunks_dir))

    # Save translation
    translation = "Esta es la primera parte del texto en ingles."
    success = session.save_translation("test_chunk_001", translation)

    assert success is True

    # Verify chunk was updated in memory
    chunk = next(c for c in session.chunks if c.id == "test_chunk_001")
    assert chunk.translated_text == translation
    assert chunk.status == ChunkStatus.TRANSLATED
    assert chunk.translated_at is not None

    # Verify chunk was saved to disk
    saved_chunk = json.loads((temp_chunks_dir / "test_chunk_001.json").read_text())
    assert saved_chunk["translated_text"] == translation
    assert saved_chunk["status"] == "translated"


def test_save_translation_invalid_chunk(temp_chunks_dir):
    """Test saving translation with invalid chunk ID."""
    session = TranslationSession(chunks_dir=str(temp_chunks_dir))

    success = session.save_translation("nonexistent_chunk", "Translation")

    assert success is False


def test_get_progress(temp_chunks_dir):
    """Test getting progress statistics."""
    session = TranslationSession(chunks_dir=str(temp_chunks_dir))

    progress = session.get_progress()

    # 3 total chunks, 1 already translated (chunk_002)
    assert progress["total_chunks"] == 3
    assert progress["completed_chunks"] == 1
    assert progress["remaining_chunks"] == 2


def test_get_progress_after_translation(temp_chunks_dir):
    """Test progress updates after translating a chunk."""
    session = TranslationSession(chunks_dir=str(temp_chunks_dir))

    # Translate one more chunk
    session.save_translation("test_chunk_001", "Traducción")

    progress = session.get_progress()

    # Now 2 chunks should be translated
    assert progress["total_chunks"] == 3
    assert progress["completed_chunks"] == 2
    assert progress["remaining_chunks"] == 1


def test_chunk_to_dict(temp_chunks_dir):
    """Test converting chunk to dictionary for JSON response."""
    session = TranslationSession(chunks_dir=str(temp_chunks_dir))

    chunk = session.chunks[0]
    chunk_dict = session.chunk_to_dict(chunk)

    # Verify all required fields are present
    assert chunk_dict["chunk_id"] == "test_chunk_001"
    assert chunk_dict["position"] == 1
    assert chunk_dict["total_chunks"] == 3
    assert chunk_dict["chapter_id"] == "chapter_01"
    assert chunk_dict["source_text"] == chunk.source_text
    assert chunk_dict["word_count"] == 8
    assert chunk_dict["paragraph_count"] == 1
    assert "rendered_prompt" in chunk_dict
    assert isinstance(chunk_dict["rendered_prompt"], str)
    assert chunk_dict["has_next"] is True


def test_session_with_empty_directory():
    """Test that creating a session with empty directory raises error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with pytest.raises(ValueError, match="No chunk files found"):
            TranslationSession(chunks_dir=tmpdir)


def test_chunks_sorted_by_position(temp_chunks_dir):
    """Test that chunks are sorted by position when loaded."""
    session = TranslationSession(chunks_dir=str(temp_chunks_dir))

    # Verify chunks are in order
    positions = [chunk.position for chunk in session.chunks]
    assert positions == [1, 2, 3]


def test_full_workflow(temp_chunks_dir):
    """Test complete workflow: load → translate → save → next."""
    session = TranslationSession(chunks_dir=str(temp_chunks_dir))

    # Step 1: Get first untranslated chunk
    chunk1 = session.get_next_untranslated_chunk()
    assert chunk1.id == "test_chunk_001"

    # Step 2: Render prompt
    prompt1 = session.render_chunk_prompt(chunk1)
    assert len(prompt1) > 0

    # Step 3: Save translation
    session.save_translation(chunk1.id, "Primera traducción")

    # Step 4: Get next untranslated chunk (should skip translated chunk_002)
    chunk2 = session.get_next_untranslated_chunk()
    assert chunk2.id == "test_chunk_003"

    # Step 5: Translate final chunk
    session.save_translation(chunk2.id, "Tercera traducción")

    # Step 6: Verify all complete
    final_chunk = session.get_next_untranslated_chunk()
    assert final_chunk is None

    # Verify progress
    progress = session.get_progress()
    assert progress["completed_chunks"] == 3
    assert progress["remaining_chunks"] == 0
