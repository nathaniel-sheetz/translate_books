"""What a model id says about itself (``src/harness/model_ids.py``).

Nothing here names a model the code knows about — that is the point. These pin
the two id *shapes* the harness reads, so the next release needs no edit.
"""

from __future__ import annotations

import pytest

from src.harness import state as hstate
from src.harness.model_ids import (
    join_effort_suffix,
    model_family,
    split_effort_suffix,
)


@pytest.mark.parametrize("base,expected", [
    ("grok-4.7-medium", ("grok-4.7", "medium", False)),
    ("grok-4.7-xhigh", ("grok-4.7", "xhigh", False)),
    ("grok-4.7-low-fast", ("grok-4.7", "low", True)),
    ("cursor-grok-4.6-medium", ("cursor-grok-4.6", "medium", False)),
    ("gemini-3.8-flash-medium", ("gemini-3.8-flash", "medium", False)),
    ("GROK-4.7-HIGH", ("GROK-4.7", "high", False)),
    # No effort in the id: returned whole.
    ("grok-4.5", ("grok-4.5", None, False)),
    ("auto", ("auto", None, False)),
    ("claude-sonnet-5-5", ("claude-sonnet-5-5", None, False)),
    # `-fast` alone is not an effort, and a level needs a stem in front of it.
    ("composer-2-fast", ("composer-2-fast", None, False)),
    ("medium", ("medium", None, False)),
    ("", ("", None, False)),
    (None, ("", None, False)),
])
def test_split_effort_suffix(base, expected):
    assert split_effort_suffix(base) == expected


def test_join_is_the_inverse_of_split():
    for model in ("grok-4.7-medium", "grok-4.7-xhigh-fast", "cursor-grok-4.6-low"):
        stem, effort, fast = split_effort_suffix(model)
        assert join_effort_suffix(stem, effort, fast) == model


def test_every_effort_level_the_harness_knows_is_a_suffix():
    """The suffix pattern is built from EFFORT_LEVELS, so the two cannot drift."""
    for level in hstate.EFFORT_LEVELS:
        assert split_effort_suffix(f"some-model-{level}")[1] == level


@pytest.mark.parametrize("model,family", [
    # One model, however its effort is spelled or which listing named it.
    ("grok-4.7-medium", "grok-4.7"),
    ("grok-4.7-high-fast", "grok-4.7"),
    ("grok-4.7[effort=low,fast=false]", "grok-4.7"),
    ("cursor-grok-4.6-medium", "grok-4.6"),
    ("grok-4.6[effort=medium,fast=false]", "grok-4.6"),
    ("Grok-4.6", "grok-4.6"),
    # Claude ids and aliases pass through; an alias is its own family.
    ("sonnet", "sonnet"),
    ("claude-sonnet-5-5", "claude-sonnet-5-5"),
    ("auto", "auto"),
    ("", ""),
    (None, ""),
])
def test_model_family(model, family):
    assert model_family(model) == family


def test_different_models_never_share_a_family():
    assert model_family("grok-4.6-medium") != model_family("grok-4.7-medium")
    assert model_family("claude-sonnet-5") != model_family("claude-sonnet-5-5")
