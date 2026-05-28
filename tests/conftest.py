"""Shared pytest fixtures.

Isolate every test from the real prompts/history directory. Several tests
exercise code paths that call log_prompt() (e.g. batch submission with
mocked API clients); without isolation those writes leak into the real
prompts/history/ and accumulate as orphan submission stubs over time.
"""

from __future__ import annotations

import pytest

from src.utils import prompt_logger


@pytest.fixture(autouse=True)
def _isolate_prompt_history(tmp_path, monkeypatch):
    """Redirect prompt_logger writes to a per-test tmp directory."""
    history_dir = tmp_path / "prompts_history"
    history_dir.mkdir()
    monkeypatch.setattr(prompt_logger, "_HISTORY_DIR", history_dir)
    prompt_logger._LAST_LOG_PATH.set(None)
