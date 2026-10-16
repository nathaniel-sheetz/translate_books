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

import os

import pytest

from src.harness import headless
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
def _isolate_codex_home(tmp_path, monkeypatch):
    """Send an unset ``CODEX_HOME`` to an empty tmp directory, never ``~/.codex``.

    The Codex preflight reads ``.env`` there, and the model and config readers
    fall back to it. Without this a metered key in the developer's own Codex home
    would fail every test that reaches the preflight. A test that sets
    ``CODEX_HOME``, in the process or in the ``env`` it passes, still gets it.
    """
    fallback = tmp_path / "codex-home"
    real = headless.codex_home

    def codex_home(env=None):
        source = os.environ if env is None else env
        return real(env) if source.get("CODEX_HOME") else fallback

    monkeypatch.setattr(headless, "codex_home", codex_home)


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
def _house_files_are_the_examples(tmp_path, monkeypatch):
    """Every house file reads as its tracked ``.example`` twin.

    The operator's own house rules, lexicon and book groups are per-user and
    change what prompts carry; a test must see the same house on every machine.
    Proposals are written to a per-test file. Tests that want a different house
    point these at their own files.
    """
    from src.house import groups, lexicon, proposals
    from src.judges import context

    absent = tmp_path / "_house_absent"
    monkeypatch.setattr(context, "HOUSE_STYLE_RULES_FILE", absent / "house_style_rules.json")
    monkeypatch.setattr(lexicon, "LEXICON_FILE", absent / "house_lexicon.json")
    monkeypatch.setattr(lexicon, "LEGACY_FORCED_FILE", absent / "forced_glossary_terms.json")
    monkeypatch.setattr(groups, "GROUPS_FILE", absent / "book_groups.json")
    monkeypatch.setattr(proposals, "PROPOSALS_FILE", tmp_path / "house_rules.proposals.jsonl")


@pytest.fixture
def no_house_rules(tmp_path, monkeypatch):
    """A house with no rules at all, for tests of what happens without one."""
    from src.judges import context

    empty = tmp_path / "empty_house_rules.json"
    empty.write_text('{"every_book": []}', encoding="utf-8")
    monkeypatch.setattr(context, "HOUSE_STYLE_RULES_FILE", empty)
    return empty


@pytest.fixture(autouse=True)
def _no_save_check_config(monkeypatch):
    """Keep the developer's ``save_check`` section out of every test.

    A ``save_check.model`` in app_config.json would send each test Save to a
    real model server. Tests that want a section set their own.
    """
    import src.app_config as app_config

    monkeypatch.setattr(app_config, "get_save_check_config", lambda: {})


@pytest.fixture(autouse=True)
def _no_local_llm_server(monkeypatch):
    """Keep the developer's local model server out of every test.

    A ``local`` provider in llm_config.json would have the model picker ask a
    real ``llama-server`` what it has loaded. Tests that want a server pass
    their own ``session``.
    """
    import src.local_llm as local_llm

    class _Offline:
        def get(self, *args, **kwargs):
            raise ConnectionError("no model server in tests")

        post = get

    monkeypatch.setattr(local_llm, "_default_session", lambda: _Offline())
    monkeypatch.setattr(local_llm, "_loaded_cache", {})
