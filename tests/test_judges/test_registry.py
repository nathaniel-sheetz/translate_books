"""Tests for the judge registry + suite resolution."""

from __future__ import annotations

import pytest

from src.judges.dialogue_judge import DialogueComplianceJudge
from src.judges.registry import (
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
