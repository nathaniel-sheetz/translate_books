"""``docs/TRANSLATE_HARNESS.md`` must describe the CLI that actually exists.

The doc's command reference is a set of tables mapping verbs to what they do,
and a prose line about which verbs accept which scoping flags. Both drift
silently: the ship-lite review on docs/harness-first-revamp found the doc
claiming ``--chapters`` / ``--chunk-ids`` were interchangeable across all five
translate verbs when ``translate-commit`` accepts neither and
``translate-fanout`` accepts only ``--chunk-ids``. Following the doc produced an
argparse error.

Assertions here read the real parser via ``_build_parser`` rather than scraping
``--help`` text, so they stay exact.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from scripts.harness import _build_parser
from src.harness import flow

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "TRANSLATE_HARNESS.md"
DOC = DOC_PATH.read_text(encoding="utf-8")


def _subparser_map() -> dict[str, argparse.ArgumentParser]:
    """Verb name -> its parser, from the real harness argparse tree."""
    parser = _build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("harness._build_parser() exposes no subparsers")


def _option_strings(parser: argparse.ArgumentParser) -> set[str]:
    return {opt for action in parser._actions for opt in action.option_strings}


VERBS = _subparser_map()


def test_the_parser_was_introspected():
    """Guard the guard: an empty verb map would make everything below vacuous."""
    assert len(VERBS) > 15
    assert "translate" in VERBS


# --- #2: documented verbs exist -------------------------------------------

# Verbs appear in the doc's tables as a leading ``| `verb` |`` cell. Sub-verbs
# (``style-guide prepare-draft``) are listed in a separate column and are not
# top-level parser entries, so only the first cell is harvested.
_TABLE_VERB_RE = re.compile(r"^\|\s*`([a-z][a-z0-9-]*)`\s*\|", re.MULTILINE)


def test_every_verb_named_in_a_doc_table_is_a_real_harness_verb():
    documented = set(_TABLE_VERB_RE.findall(DOC))
    assert documented, "no verbs parsed out of TRANSLATE_HARNESS.md — the pattern went stale"
    # The backends table uses the same cell shape for backend names, which are
    # config values rather than verbs.
    documented -= {"api", "subagent", "headless"}
    unknown = sorted(v for v in documented if v not in VERBS)
    assert not unknown, f"TRANSLATE_HARNESS.md documents verbs the CLI does not have: {unknown}"


@pytest.mark.parametrize(
    "verb",
    [
        "setup", "split", "split-preview", "style-guide", "glossary", "address-map",
        "difficulty", "chunk", "cost", "translate", "translate-prepare",
        "translate-commit", "translate-fanout", "retranslate", "combine", "align",
        "epub", "footnotes", "captions", "status", "show-translation", "runs",
        "log-event", "config-set",
    ],
)
def test_documented_verb_exists(verb):
    """Each verb the doc gives a table row to is a real subparser."""
    assert verb in VERBS, f"TRANSLATE_HARNESS.md documents `{verb}`, which the CLI lacks"


# --- #2b: the scoping-flag claim ------------------------------------------

# The corrected prose in "### Translation". Encoded as data so a future edit to
# either side has to touch this table.
SCOPING = {
    "translate": {"--chapters"},
    "translate-prepare": {"--chapters"},
    "translate-commit": set(),
    "translate-fanout": {"--chunk-ids"},
    "retranslate": {"--chapters", "--chunk-ids"},
}


@pytest.mark.parametrize("verb,expected", sorted(SCOPING.items()))
def test_scoping_flags_match_the_documented_claim(verb, expected):
    """``--chapters`` / ``--chunk-ids`` support is per-verb, not universal."""
    actual = _option_strings(VERBS[verb]) & {"--chapters", "--chunk-ids"}
    assert actual == expected, (
        f"`{verb}` accepts {sorted(actual) or 'neither'}, but TRANSLATE_HARNESS.md's "
        f"scoping paragraph says {sorted(expected) or 'neither'}"
    )


def test_translate_commit_takes_no_scoping_flag():
    """The idempotency claim: it commits every draft it finds, so it needs no scope.

    Spelled out separately because this is the specific case the doc got wrong —
    a reader who typed `translate-commit --chapters 1-2` got an argparse error.
    """
    assert not (_option_strings(VERBS["translate-commit"]) & {"--chapters", "--chunk-ids"})


# --- #3: the config-set key list ------------------------------------------

def test_documented_config_set_keys_match_the_registry():
    """The doc enumerates every ``config-set --key`` inline; adding one must update it."""
    # The sentence lists the keys as inline code, with the four effort keys
    # given as a `headless_effort_*` stem plus a parenthesised suffix list.
    para = DOC.split("`config-set --key` accepts exactly:", 1)
    assert len(para) == 2, "the config-set enumeration moved; update this test's anchor"
    sentence = para[1].split("\n\n", 1)[0]

    named = set(re.findall(r"`([a-z_]+)`", sentence))
    suffixes = {"translate", "judges", "annotations", "footnotes"}
    documented = (named - suffixes - {"headless_effort_"}) | {
        f"headless_effort_{s}" for s in suffixes if s in named
    }

    actual = set(flow._CONFIG_SET_KEYS)
    assert documented == actual, (
        "TRANSLATE_HARNESS.md's config-set list disagrees with flow._CONFIG_SET_KEYS.\n"
        f"  documented but not real: {sorted(documented - actual)}\n"
        f"  real but undocumented:   {sorted(actual - documented)}"
    )


def test_config_set_choices_are_the_registry():
    """The CLI's own ``--key`` choices come from the same registry the doc tracks."""
    action = next(a for a in VERBS["config-set"]._actions if "--key" in a.option_strings)
    assert set(action.choices) == set(flow._CONFIG_SET_KEYS)
