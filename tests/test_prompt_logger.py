"""Tests for prompt logger utility."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from src.utils import prompt_logger
from src.utils.prompt_logger import (
    _short_hash,
    last_log_path,
    log_prompt,
    relative_log_path,
    update_log_response,
)


class TestShortHash:
    def test_deterministic(self):
        assert _short_hash("hello") == _short_hash("hello")

    def test_different_inputs(self):
        assert _short_hash("hello") != _short_hash("world")

    def test_custom_length(self):
        result = _short_hash("test", length=10)
        assert len(result) == 10


class TestLogPrompt:
    def test_creates_log_file(self, tmp_path):
        with patch("src.utils.prompt_logger._HISTORY_DIR", tmp_path):
            path = log_prompt(
                prompt="Translate this",
                response="Traduce esto",
                provider="anthropic",
                model="claude-3-5-sonnet",
                call_type="translation",
            )
            assert path.exists()
            data = json.loads(path.read_text())
            assert data["prompt"] == "Translate this"
            assert data["response"] == "Traduce esto"
            assert data["metadata"]["provider"] == "anthropic"
            assert data["metadata"]["call_type"] == "translation"

    def test_null_response_for_batch(self, tmp_path):
        with patch("src.utils.prompt_logger._HISTORY_DIR", tmp_path):
            path = log_prompt(
                prompt="Batch prompt",
                response=None,
                provider="openai",
                model="gpt-4o",
                mode="batch",
                batch_job_id="batch_123",
            )
            data = json.loads(path.read_text())
            assert data["response"] is None
            assert data["metadata"]["batch_job_id"] == "batch_123"

    def test_includes_duration(self, tmp_path):
        with patch("src.utils.prompt_logger._HISTORY_DIR", tmp_path):
            path = log_prompt(
                prompt="test",
                response="resp",
                provider="anthropic",
                model="claude-3-5-sonnet",
                duration_seconds=1.2345,
            )
            data = json.loads(path.read_text())
            assert data["metadata"]["duration_seconds"] == 1.234

    def test_extra_metadata(self, tmp_path):
        with patch("src.utils.prompt_logger._HISTORY_DIR", tmp_path):
            path = log_prompt(
                prompt="test",
                response="resp",
                provider="anthropic",
                model="claude-3-5-sonnet",
                extra={"custom_field": "value"},
            )
            data = json.loads(path.read_text())
            assert data["metadata"]["custom_field"] == "value"

    def test_project_slug_and_chunk_id_in_metadata(self, tmp_path):
        with patch("src.utils.prompt_logger._HISTORY_DIR", tmp_path):
            path = log_prompt(
                prompt="test",
                response="resp",
                provider="anthropic",
                model="claude-3-5-sonnet",
                call_type="translation",
                chunk_id="chapter_01_chunk_000",
                project_slug="my-book",
            )
            data = json.loads(path.read_text())
            assert data["metadata"]["chunk_id"] == "chapter_01_chunk_000"
            assert data["metadata"]["project_slug"] == "my-book"

    def test_project_slug_omitted_when_none(self, tmp_path):
        with patch("src.utils.prompt_logger._HISTORY_DIR", tmp_path):
            path = log_prompt(
                prompt="test",
                response="resp",
                provider="anthropic",
                model="claude-3-5-sonnet",
            )
            data = json.loads(path.read_text())
            assert "project_slug" not in data["metadata"]


class TestLastLogPathContextVar:
    """The ContextVar-backed peek (last_log_path) is what lets api_translator
    stamp chunk.last_llm_log without threading the log path through every
    layer of call_llm/_dispatch_llm_call. If this plumbing breaks, every LLM
    write site silently fails to stamp and the edit-review report falls back
    to the chunk-id scan (or shows the 'no baseline' banner)."""

    def test_log_prompt_sets_last_log_path_to_written_file(self, tmp_path):
        with patch("src.utils.prompt_logger._HISTORY_DIR", tmp_path):
            written = log_prompt(
                prompt="hola",
                response="hello",
                provider="anthropic",
                model="claude-sonnet-4-6",
                call_type="translation",
            )
        assert last_log_path() == written
        assert written.exists()

    def test_update_log_response_refreshes_last_log_path(self, tmp_path):
        """Batch retrieval uses update_log_response to fill in a submission
        log. That call must also refresh the ContextVar so callers reading
        last_log_path() see the retrieved log, not whatever was last written
        before."""
        with patch("src.utils.prompt_logger._HISTORY_DIR", tmp_path):
            submission_path = log_prompt(
                prompt="batch prompt",
                response=None,
                provider="anthropic",
                model="claude-sonnet-4-6",
                mode="batch",
                batch_job_id="batch_ctx_test",
                chunk_id="ch_001",
            )
            # Pollute the ContextVar so we can prove update_log_response
            # actually refreshes it rather than relying on prior state.
            prompt_logger._LAST_LOG_PATH.set(tmp_path / "unrelated.json")

            update_log_response(submission_path, "filled-in response")

        assert last_log_path() == submission_path
        record = json.loads(submission_path.read_text(encoding="utf-8"))
        assert record["response"] == "filled-in response"
        assert "retrieved_at" in record["metadata"]


class TestRelativeLogPath:
    """relative_log_path converts an absolute prompts/history path to a
    repo-relative POSIX string suitable for chunk.last_llm_log. Paths outside
    the repo (e.g. test tmpdirs) must fall back to the absolute POSIX form
    rather than raising — without this fallback, stamping in any test
    environment would crash with ValueError."""

    def test_absolute_fallback_for_paths_outside_repo(self, tmp_path):
        outside = tmp_path / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        result = relative_log_path(outside)
        assert result == outside.resolve().as_posix()
