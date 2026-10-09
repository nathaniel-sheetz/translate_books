"""Shared pytest fixtures.

Isolate every test from the real prompts/history directory. Several tests
exercise code paths that call log_prompt() (e.g. batch submission with
mocked API clients); without isolation those writes leak into the real
prompts/history/ and accumulate as orphan submission stubs over time.

The same applies to the harness run log: harness commands now append to
logs/harness_runs.jsonl, so tests that exercise them are redirected to a
per-test tmp file rather than polluting the real run log.
"""

from __future__ import annotations

import pytest

from src.harness.host import HOST_OVERRIDE_ENV, HOST_UNKNOWN
from src.utils import prompt_logger, run_logger


@pytest.fixture(autouse=True)
def _neutral_host(monkeypatch):
    """Pin host detection to ``unknown`` so results don't depend on who ran pytest.

    ``headless_cli`` defaults to ``auto``, which resolves through
    :func:`src.harness.host.detect_host`. Without this, every assertion about a
    default worker model, effort or token baseline would pass under Claude Code
    and fail under a Cursor-hosted session — the suite would be testing the
    developer's terminal. Tests that care about a host set the variable themselves.
    """
    monkeypatch.setenv(HOST_OVERRIDE_ENV, HOST_UNKNOWN)


@pytest.fixture(autouse=True)
def _isolate_prompt_history(tmp_path, monkeypatch):
    """Redirect prompt_logger writes to a per-test tmp directory."""
    history_dir = tmp_path / "prompts_history"
    history_dir.mkdir()
    monkeypatch.setattr(prompt_logger, "_HISTORY_DIR", history_dir)
    prompt_logger._LAST_LOG_PATH.set(None)


@pytest.fixture(autouse=True)
def _isolate_run_log(tmp_path, monkeypatch):
    """Redirect run_logger writes to a per-test tmp file."""
    monkeypatch.setattr(run_logger, "_RUNS_PATH", tmp_path / "logs" / "harness_runs.jsonl")


@pytest.fixture(autouse=True)
def _no_save_check_config(monkeypatch):
    """Keep the developer's ``save_check`` section out of every test.

    A ``save_check.model`` in app_config.json would send each test Save to a
    real model server. Tests that want a section set their own.
    """
    import src.app_config as app_config

    monkeypatch.setattr(app_config, "get_save_check_config", lambda: {})
