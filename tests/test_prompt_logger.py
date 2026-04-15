"""Tests for prompt logger utility."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from src.utils.prompt_logger import log_prompt, _short_hash


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
