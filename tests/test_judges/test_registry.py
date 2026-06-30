"""Tests for the judge registry + suite resolution."""

from __future__ import annotations

import pytest

from src.app_config import get_judge_suites, load_app_config
from src.judges.dialogue_judge import DialogueComplianceJudge
from src.judges.registry import (
    all_suites,
    available_judges,
    get_judge,
    resolve_suite,
)


def test_dialogue_registered():
    assert "dialogue" in available_judges()


def test_get_judge_returns_instance():
    assert isinstance(get_judge("dialogue"), DialogueComplianceJudge)


def test_get_unknown_judge_raises():
    with pytest.raises(ValueError, match="Unknown judge"):
        get_judge("does_not_exist")


def test_default_suite_resolves():
    assert resolve_suite("default") == ["dialogue"]


def test_unknown_suite_raises():
    with pytest.raises(ValueError, match="Unknown suite"):
        resolve_suite("does_not_exist")


def test_suite_with_unregistered_judge_raises(monkeypatch):
    """resolve_suite raises ValueError when a suite member is not in the registry."""
    import src.judges.registry as reg
    monkeypatch.setitem(reg._BUILTIN_SUITES, "bad_suite", ["dialogue", "nonexistent"])
    with pytest.raises(ValueError, match="unregistered judges"):
        resolve_suite("bad_suite")


# ---------------------------------------------------------------------------
# get_judge_suites (app_config integration)
# ---------------------------------------------------------------------------


def test_get_judge_suites_absent_returns_empty(monkeypatch):
    """When judge_suites is not in app_config, get_judge_suites returns {}."""
    monkeypatch.setattr(
        "src.app_config.load_app_config", lambda **kw: {}
    )
    assert get_judge_suites() == {}


def test_get_judge_suites_malformed_dict_value_skipped(monkeypatch):
    """Suite entries whose member list is not a list[str] are silently skipped."""
    monkeypatch.setattr(
        "src.app_config.load_app_config",
        lambda **kw: {"judge_suites": {"good": ["dialogue"], "bad": "not-a-list"}},
    )
    suites = get_judge_suites()
    assert "good" in suites
    assert "bad" not in suites


def test_get_judge_suites_non_dict_value_returns_empty(monkeypatch):
    """When judge_suites is not a dict (e.g. a list), get_judge_suites returns {}."""
    monkeypatch.setattr(
        "src.app_config.load_app_config",
        lambda **kw: {"judge_suites": ["dialogue"]},
    )
    assert get_judge_suites() == {}


def test_all_suites_config_overrides_builtin(monkeypatch):
    """Config-defined suites win over built-in suites with the same name."""
    monkeypatch.setattr(
        "src.app_config.load_app_config",
        lambda **kw: {"judge_suites": {"default": ["dialogue", "dialogue"]}},
    )
    suites = all_suites()
    assert suites["default"] == ["dialogue", "dialogue"]
