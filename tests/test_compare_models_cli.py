"""
Tests for scripts/compare_models.py CLI wiring.

Covers:
- Chunk-size / overlap flags flow into ChunkingConfig correctly.
- --judge-context normalization (hyphen → underscore).
- _build_translator_context renders the expected slots with a source placeholder.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.compare_models import (
    _SOURCE_PLACEHOLDER,
    _build_translator_context,
    main,
)
from src.models import Chunk, ChunkingConfig, ChunkMetadata


def _make_chunk(text: str = "The beetle crawled across the broad leaf.") -> Chunk:
    words = len(text.split())
    return Chunk(
        id="ch01_chunk_001",
        chapter_id="ch01",
        position=0,
        source_text=text,
        metadata=ChunkMetadata(
            char_start=0,
            char_end=len(text),
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=words,
        ),
    )


# =========================================================================
# Chunking CLI flags → ChunkingConfig
# =========================================================================

class TestChunkingFlags:
    """The --chunk-size and overlap flags must produce the matching ChunkingConfig."""

    def test_defaults_match_chunking_config_defaults(self, monkeypatch):
        """With no flags beyond the required ones, ChunkingConfig defaults are used."""
        captured = {}

        def fake_run_comparison(args):
            captured["args"] = args

        monkeypatch.setattr("scripts.compare_models.run_comparison", fake_run_comparison)
        monkeypatch.setattr(
            sys, "argv",
            [
                "compare_models.py",
                "--source", "dummy.txt",
                "--models", "sonnet,haiku",
                "--project", "projects/dummy",
            ],
        )

        main()

        args = captured["args"]
        assert args.chunk_size == 2000
        assert args.overlap_paragraphs == 0
        assert args.overlap_words == 0
        assert args.judge_context == "style"

        # Build the config the way run_comparison does it; verify it's valid.
        config = ChunkingConfig(
            target_size=args.chunk_size,
            overlap_paragraphs=args.overlap_paragraphs,
            min_overlap_words=args.overlap_words,
        )
        assert config.target_size == 2000
        assert config.overlap_paragraphs == 0
        assert config.min_overlap_words == 0

    def test_custom_chunk_size_and_zero_overlap(self, monkeypatch):
        captured = {}

        def fake_run_comparison(args):
            captured["args"] = args

        monkeypatch.setattr("scripts.compare_models.run_comparison", fake_run_comparison)
        monkeypatch.setattr(
            sys, "argv",
            [
                "compare_models.py",
                "--source", "dummy.txt",
                "--models", "sonnet,haiku",
                "--project", "projects/dummy",
                "--chunk-size", "700",
                "--overlap-paragraphs", "0",
                "--overlap-words", "0",
            ],
        )

        main()

        args = captured["args"]
        assert args.chunk_size == 700
        assert args.overlap_paragraphs == 0
        assert args.overlap_words == 0

        config = ChunkingConfig(
            target_size=args.chunk_size,
            overlap_paragraphs=args.overlap_paragraphs,
            min_overlap_words=args.overlap_words,
        )
        assert config.target_size == 700

    def test_judge_context_full_prompt_flag(self, monkeypatch):
        captured = {}

        def fake_run_comparison(args):
            captured["args"] = args

        monkeypatch.setattr("scripts.compare_models.run_comparison", fake_run_comparison)
        monkeypatch.setattr(
            sys, "argv",
            [
                "compare_models.py",
                "--source", "dummy.txt",
                "--models", "sonnet,haiku",
                "--project", "projects/dummy",
                "--judge-context", "full-prompt",
            ],
        )

        main()

        args = captured["args"]
        assert args.judge_context == "full-prompt"
        # run_comparison normalizes hyphen → underscore for the judge.py API.
        assert args.judge_context.replace("-", "_") == "full_prompt"

    def test_invalid_judge_context_rejected(self, monkeypatch):
        monkeypatch.setattr(
            sys, "argv",
            [
                "compare_models.py",
                "--source", "dummy.txt",
                "--models", "sonnet,haiku",
                "--project", "projects/dummy",
                "--judge-context", "not-a-mode",
            ],
        )
        with pytest.raises(SystemExit):
            main()


# =========================================================================
# _build_translator_context
# =========================================================================

class TestBuildTranslatorContext:
    """The full-prompt context builder must mirror the translator's variables."""

    def test_source_slot_is_placeholder_not_chunk_text(self, monkeypatch):
        """Source text is swapped for a placeholder to avoid duplicating it."""
        # load_prompt_template defaults to prompts/translation.txt; the module's
        # cwd may vary across test runners, so point it at the repo's file.
        from src.utils import file_io
        repo_template = Path(__file__).resolve().parent.parent / "prompts" / "translation.txt"
        monkeypatch.setattr(
            file_io, "load_prompt_template",
            lambda path=None: repo_template.read_text(encoding="utf-8"),
        )
        # compare_models imports load_prompt_template at module load time, so
        # patch its local reference too.
        monkeypatch.setattr(
            "scripts.compare_models.load_prompt_template",
            lambda path=None: repo_template.read_text(encoding="utf-8"),
        )

        chunk = _make_chunk("UNIQUE_SOURCE_SENTINEL text here.")
        context = _build_translator_context(
            chunk, glossary=None, style_guide=None, project_name="Test Book",
        )

        assert _SOURCE_PLACEHOLDER in context
        assert "UNIQUE_SOURCE_SENTINEL" not in context
        assert "Test Book" in context
        assert "No glossary provided." in context
        assert "No style guide provided." in context
