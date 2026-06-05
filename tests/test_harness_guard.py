"""Tests for src/harness_guard.py — the translate-harness validation guards.

Covers the two boundaries an agent-authored artifact crosses:
  1. glossary proposals (dicts)  -> guard_glossary_proposals  (parse boundary / KeyError)
  2. written style.json / glossary.json / chunk.json -> validate_*_file (Pydantic loaders)
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from src.harness_guard import (
    HarnessValidationError,
    guard_glossary_proposals,
    validate_chunk_file,
    validate_glossary_file,
    validate_style_guide_file,
)
from src.glossary_bootstrap import glossary_terms_from_proposals
from src.models import Chunk, ChunkMetadata, Glossary, GlossaryTerm, StyleGuide
from src.utils.file_io import save_chunk, save_glossary, save_style_guide


# --------------------------------------------------------------------------- #
# guard_glossary_proposals — the parse boundary before glossary_terms_from_proposals
# --------------------------------------------------------------------------- #

class TestGuardGlossaryProposals:
    def test_valid_proposals_pass_through(self):
        proposals = [
            {"english": "Harry", "translation": "Harry", "type": "character"},
            {"english": "wand", "spanish": "varita", "type": "other"},
        ]
        assert guard_glossary_proposals(proposals) is proposals
        # And the bootstrap helper that previously KeyError'd now succeeds.
        terms = glossary_terms_from_proposals(proposals)
        assert {t.english for t in terms} == {"Harry", "wand"}

    def test_missing_english_raises_not_keyerror(self):
        # Without the guard this dict hits `p["english"]` -> KeyError 500.
        proposals = [{"translation": "varita", "type": "other"}]
        with pytest.raises(HarnessValidationError) as exc:
            guard_glossary_proposals(proposals)
        assert "english" in str(exc.value)
        assert "entry 0" in str(exc.value)

    def test_empty_english_rejected(self):
        with pytest.raises(HarnessValidationError):
            guard_glossary_proposals([{"english": "   ", "translation": "x"}])

    def test_missing_translation_and_spanish_rejected(self):
        with pytest.raises(HarnessValidationError) as exc:
            guard_glossary_proposals([{"english": "Harry"}])
        assert "translation" in str(exc.value)

    def test_reports_every_bad_entry_at_once(self):
        proposals = [
            {"english": "ok", "translation": "ok"},   # good
            {"translation": "missing english"},        # bad: no english
            {"english": "no translation"},             # bad: no translation
        ]
        with pytest.raises(HarnessValidationError) as exc:
            guard_glossary_proposals(proposals)
        msg = str(exc.value)
        assert "entry 1" in msg and "entry 2" in msg

    def test_non_list_rejected(self):
        with pytest.raises(HarnessValidationError):
            guard_glossary_proposals({"english": "Harry"})  # type: ignore[arg-type]

    def test_non_dict_entry_rejected(self):
        with pytest.raises(HarnessValidationError):
            guard_glossary_proposals(["just a string"])  # type: ignore[list-item]


# --------------------------------------------------------------------------- #
# validate_*_file — wrap the existing Pydantic loaders
# --------------------------------------------------------------------------- #

class TestValidateGlossaryFile:
    def test_valid_glossary_file(self, tmp_path: Path):
        path = tmp_path / "glossary.json"
        save_glossary(Glossary(terms=[GlossaryTerm(english="Harry", spanish="Harry")]), path)
        result = validate_glossary_file(path)
        assert result.terms[0].english == "Harry"

    def test_malformed_glossary_raises(self, tmp_path: Path):
        path = tmp_path / "glossary.json"
        path.write_text('{"terms": "not a list"}', encoding="utf-8")
        with pytest.raises(HarnessValidationError) as exc:
            validate_glossary_file(path)
        assert "Glossary" in str(exc.value)

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(HarnessValidationError):
            validate_glossary_file(tmp_path / "nope.json")


class TestValidateStyleGuideFile:
    def test_valid_style_guide_file(self, tmp_path: Path):
        path = tmp_path / "style.json"
        save_style_guide(StyleGuide(content="TONE: formal"), path)
        result = validate_style_guide_file(path)
        assert "formal" in result.content

    def test_malformed_style_guide_raises(self, tmp_path: Path):
        path = tmp_path / "style.json"
        path.write_text('{"version": "1.0"}', encoding="utf-8")  # missing required `content`
        with pytest.raises(HarnessValidationError) as exc:
            validate_style_guide_file(path)
        assert "Style guide" in str(exc.value)


class TestValidateChunkFile:
    def test_valid_chunk_file(self, tmp_path: Path):
        path = tmp_path / "chapter_01_chunk_000.json"
        chunk = Chunk(
            id="chapter_01_chunk_000", chapter_id="chapter_01", position=0,
            source_text="Hello.",
            metadata=ChunkMetadata(char_start=0, char_end=6, overlap_start=0,
                                   overlap_end=0, paragraph_count=1, word_count=1),
        )
        save_chunk(chunk, path)
        result = validate_chunk_file(path)
        assert result.source_text == "Hello."

    def test_malformed_chunk_raises(self, tmp_path: Path):
        path = tmp_path / "bad_chunk.json"
        path.write_text('{"id": 123}', encoding="utf-8")  # wrong types / missing fields
        with pytest.raises(HarnessValidationError):
            validate_chunk_file(path)
