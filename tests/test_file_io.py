"""Tests for file I/O utilities."""

import json
import pytest
from pathlib import Path

from src.utils.file_io import (
    load_chunk,
    save_chunk,
    render_prompt,
    filter_glossary_for_chunk,
    format_glossary_for_prompt,
    ensure_project_structure,
    load_state,
    save_state,
)
from src.models import Chunk, ChunkMetadata, ChunkStatus, Glossary, GlossaryTerm, GlossaryTermType, ProjectState


@pytest.fixture
def sample_chunk():
    return Chunk(
        id="ch01_chunk_001",
        chapter_id="chapter_01",
        position=1,
        source_text="It is a truth universally acknowledged.",
        metadata=ChunkMetadata(char_start=0, char_end=100, overlap_start=0, overlap_end=0, paragraph_count=1, word_count=7),
    )


@pytest.fixture
def sample_glossary():
    return Glossary(terms=[
        GlossaryTerm(english="Bennet", spanish="Bennet", type=GlossaryTermType.CHARACTER),
        GlossaryTerm(english="Longbourn", spanish="Longbourn", type=GlossaryTermType.PLACE),
        GlossaryTerm(english="estate", spanish="hacienda", type=GlossaryTermType.CONCEPT),
    ])


class TestLoadSaveChunk:
    def test_roundtrip(self, tmp_path, sample_chunk):
        path = tmp_path / "chunk.json"
        save_chunk(sample_chunk, path)
        loaded = load_chunk(path)
        assert loaded.id == sample_chunk.id
        assert loaded.source_text == sample_chunk.source_text

    def test_load_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_chunk(tmp_path / "nonexistent.json")

    def test_load_invalid_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json at all")
        with pytest.raises(json.JSONDecodeError):
            load_chunk(path)

    def test_save_creates_parent_dirs(self, tmp_path, sample_chunk):
        path = tmp_path / "deep" / "nested" / "chunk.json"
        save_chunk(sample_chunk, path)
        assert path.exists()

    def test_atomic_write_no_temp_left(self, tmp_path, sample_chunk):
        path = tmp_path / "chunk.json"
        save_chunk(sample_chunk, path)
        temp = path.with_suffix('.tmp')
        assert not temp.exists()


class TestRenderPrompt:
    def test_simple_substitution(self):
        result = render_prompt("Hello {{name}}", {"name": "World"})
        assert result == "Hello World"

    def test_multiple_variables(self):
        template = "Translate {{text}} to {{lang}}"
        result = render_prompt(template, {"text": "Hello", "lang": "Spanish"})
        assert result == "Translate Hello to Spanish"

    def test_missing_variable_raises(self):
        with pytest.raises(KeyError, match="missing_var"):
            render_prompt("Hello {{missing_var}}", {})

    def test_no_placeholders(self):
        result = render_prompt("Plain text", {})
        assert result == "Plain text"


class TestFilterGlossaryForChunk:
    def test_filters_to_matching_terms(self, sample_glossary):
        text = "Mr. Bennet's estate was entailed."
        filtered = filter_glossary_for_chunk(sample_glossary, text)
        english_terms = [t.english for t in filtered.terms]
        assert "Bennet" in english_terms
        assert "estate" in english_terms
        assert "Longbourn" not in english_terms

    def test_empty_glossary(self):
        glossary = Glossary(terms=[])
        result = filter_glossary_for_chunk(glossary, "any text")
        assert len(result.terms) == 0

    def test_case_insensitive(self, sample_glossary):
        text = "the bennet family"
        filtered = filter_glossary_for_chunk(sample_glossary, text)
        assert any(t.english == "Bennet" for t in filtered.terms)

    def test_plural_variant(self, sample_glossary):
        text = "the estates were large"
        filtered = filter_glossary_for_chunk(sample_glossary, text)
        assert any(t.english == "estate" for t in filtered.terms)


class TestFormatGlossaryForPrompt:
    def test_groups_by_type(self, sample_glossary):
        result = format_glossary_for_prompt(sample_glossary)
        assert "CHARACTER NAMES:" in result
        assert "PLACE NAMES:" in result
        assert "Bennet" in result

    def test_empty_glossary(self):
        glossary = Glossary(terms=[])
        result = format_glossary_for_prompt(glossary)
        assert "No glossary" in result


class TestEnsureProjectStructure:
    def test_creates_all_subdirs(self, tmp_path):
        project = tmp_path / "my_book"
        ensure_project_structure(project)
        assert (project / "chapters" / "original").is_dir()
        assert (project / "chapters" / "translated").is_dir()
        assert (project / "chunks" / "original").is_dir()
        assert (project / "chunks" / "translated").is_dir()
        assert (project / "reports").is_dir()

    def test_idempotent(self, tmp_path):
        project = tmp_path / "my_book"
        ensure_project_structure(project)
        ensure_project_structure(project)  # should not raise


class TestLoadSaveState:
    def test_load_missing_returns_default(self, tmp_path):
        state = load_state(tmp_path / "nonexistent_project")
        assert isinstance(state, ProjectState)

    def test_roundtrip(self, tmp_path):
        state = ProjectState(project_name="test_book")
        save_state(state, tmp_path)
        loaded = load_state(tmp_path)
        assert loaded.project_name == "test_book"
