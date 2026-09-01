"""The action contract itself: the registry, and the auto-apply policy gates.

``Policy.accepts`` is the single decision that separates "a machine may write
this into the book's notes while nobody is watching" from "a human must look at
it", so it gets tested on its own rather than only through the action.
"""

from __future__ import annotations

import pytest

from src.actions import registry
from src.actions.registry import Policy, confidence_rank


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_annotations_is_registered():
    assert "annotations" in registry.action_names()


def test_every_action_satisfies_the_contract():
    for action in registry.ACTIONS:
        assert callable(action.detect)
        assert callable(action.run)
        assert action.auto_apply is None or callable(action.auto_apply)
        assert action.name


def test_get_action_names_the_alternatives():
    with pytest.raises(ValueError) as excinfo:
        registry.get_action("judges")
    assert "annotations" in str(excinfo.value)


def test_the_package_re_exports_the_contract():
    """A caller should not have to know which submodule holds which type."""
    import src.actions as actions

    for name in ("Action", "ActionState", "ApplyResult", "Budget", "Policy", "RunResult"):
        assert hasattr(actions, name)


# ---------------------------------------------------------------------------
# Confidence ladder
# ---------------------------------------------------------------------------


def test_confidence_is_ordered():
    assert confidence_rank("low") < confidence_rank("medium") < confidence_rank("high")


@pytest.mark.parametrize("value", ["", None, "very high", "HIGH-ish"])
def test_an_unrecognised_confidence_ranks_lowest(value):
    """The safe direction: the rank only ever decides whether to write."""
    assert confidence_rank(value) == confidence_rank("low")


def test_confidence_is_case_insensitive():
    assert confidence_rank("HIGH") == confidence_rank("high")


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def _item(**kw):
    base = {"mode": "append", "type": "word_choice", "confidence": "high"}
    base.update(kw)
    return base


def test_accepts_a_high_confidence_append():
    assert Policy().accepts(_item()) is True


@pytest.mark.parametrize("confidence", ["low", "medium", "", None])
def test_rejects_below_the_floor(confidence):
    assert Policy().accepts(_item(confidence=confidence)) is False


def test_a_lower_floor_lets_medium_through():
    assert Policy(confidence_floor="medium").accepts(_item(confidence="medium")) is True


@pytest.mark.parametrize("floor", ["very high", "hgih", "", "HIGH!", None])
def test_an_unrecognised_floor_falls_back_to_the_strictest(floor):
    """The floor has to fail closed; an item label may fail open.

    ``confidence_rank`` returns 0 for anything it does not know, which is the
    safe direction for an item — it clears fewer floors. On the floor side the
    same 0 admits *everything*, so one typo in ``app_config.json`` would have
    the nightly pass auto-writing low-confidence prose into every book.
    """
    policy = Policy(confidence_floor=floor)
    assert policy.confidence_floor == "high"
    assert policy.accepts(_item(confidence="low")) is False
    assert policy.accepts(_item(confidence="high")) is True


def test_a_recognised_floor_is_kept_verbatim():
    assert Policy(confidence_floor="  Medium  ").confidence_floor == "medium"


def test_rejects_a_type_outside_the_list():
    assert Policy().accepts(_item(type="footnote")) is False


def test_rejects_a_replace_whatever_its_type():
    """The mode gate is the belt to the type list's braces.

    ``footnote`` is the only type that produces ``mode: "replace"`` today, and
    its text is published into the EPUB by :mod:`src.endnotes`. Checking the
    mode as well means a typo in ``app_config.json``'s ``auto_apply_types``
    cannot put a model's gloss into a book.
    """
    assert Policy(types=("footnote",)).accepts(_item(type="footnote", mode="replace")) is False


def test_rejects_an_unknown_mode():
    assert Policy().accepts(_item(mode="overwrite")) is False
    assert Policy().accepts(_item(mode=None)) is False


def test_the_default_policy_excludes_footnotes():
    assert "footnote" not in Policy().types
