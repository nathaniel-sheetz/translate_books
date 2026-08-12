"""The resolved headless profile: one answer per knob, with provenance.

These pin the behaviour the 2026-08-11 judge-review friction logs asked for:
a Cursor operator gets a Cursor worker, a Cursor baseline and a truthful effort
without correcting the harness by hand.
"""

from __future__ import annotations

import json

import pytest

from src.harness import state as hstate
from src.harness.profile import (
    EFFORT_ARGV,
    EFFORT_MODEL_BRACKET,
    EFFORT_NONE,
    resolve_cli,
    resolve_profile,
)

_CLAUDE_HOST = {"CLAUDECODE": "1"}
_CURSOR_HOST = {"CURSOR_AGENT": "1"}


@pytest.fixture
def book(tmp_path):
    """A project dir with a config, like every real caller has."""
    (tmp_path / ".harness").mkdir()
    return tmp_path


def _write_cfg(book, **values):
    cfg = dict(hstate.DEFAULTS)
    cfg.update(values)
    (book / ".harness" / "config.json").write_text(
        json.dumps(cfg), encoding="utf-8"
    )
    return cfg


def _cursor_cli_config(tmp_path, model="grok-4.5", effort="high"):
    """A stand-in for ~/.cursor/cli-config.json."""
    path = tmp_path / "cli-config.json"
    path.write_text(
        json.dumps(
            {
                "selectedModel": {
                    "modelId": model,
                    "parameters": [
                        {"id": "effort", "value": effort},
                        {"id": "fast", "value": False},
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def cursor_selected(tmp_path, monkeypatch):
    """Point cursor_default_model at a controlled cli-config."""
    from src.harness import headless

    def _install(model="grok-4.5", effort="high"):
        monkeypatch.setattr(
            headless, "CURSOR_CLI_CONFIG", _cursor_cli_config(tmp_path, model, effort)
        )

    _install()
    return _install


# ── CLI precedence ──────────────────────────────────────────────────────────


def test_cli_precedence_flag_then_config_then_host_then_claude():
    cfg = {"headless_cli": "claude"}
    assert resolve_cli(cfg, override="cursor", env=_CLAUDE_HOST) == ("cursor", "cli")
    assert resolve_cli(cfg, env=_CURSOR_HOST) == ("claude", "config")
    assert resolve_cli({"headless_cli": "auto"}, env=_CURSOR_HOST) == (
        "cursor",
        "host:cursor",
    )
    assert resolve_cli({"headless_cli": "auto"}, env={}) == ("claude", "fallback")


def test_a_book_pinned_to_claude_is_never_flipped_by_a_cursor_host():
    """Pinning is the permanent one-time fix, so it has to outrank detection."""
    assert resolve_cli({"headless_cli": "claude"}, env=_CURSOR_HOST)[0] == "claude"


def test_resolve_cli_never_returns_the_auto_sentinel():
    """`auto` is a config value, not a launcher profile — it must not reach _normalize_cli."""
    for env in (_CLAUDE_HOST, _CURSOR_HOST, {}):
        assert resolve_cli({"headless_cli": "auto"}, env=env)[0] in hstate.HEADLESS_CLIS
    assert resolve_cli({"headless_cli": "nonsense"}, env={})[0] == "claude"


def test_host_detection_reaches_every_wave_type(book, cursor_selected):
    """The decision was 'all headless waves', not judges-only."""
    _write_cfg(book)
    for command in ("judges", "annotations", "translate", "footnotes"):
        prof = resolve_profile(
            book, command=command, env=_CURSOR_HOST, check_binary=False
        )
        assert prof.cli == "cursor", command
        assert prof.cli_source == "host:cursor"


# ── worker model + baseline ─────────────────────────────────────────────────


def test_claude_profile_keeps_sonnet_and_the_claude_baseline(book):
    _write_cfg(book)
    prof = resolve_profile(book, command="judges", env=_CLAUDE_HOST)
    assert (prof.cli, prof.worker_model) == ("claude", "sonnet")
    assert prof.worker_model_source == "default:claude"
    assert prof.baseline_tokens == 3900
    assert prof.effort == "medium" and prof.effort_channel == EFFORT_ARGV


def test_cursor_profile_inherits_the_operators_own_model_and_baseline(
    book, cursor_selected
):
    """The photogen bug: this used to be sonnet on the 3.9k Claude baseline."""
    _write_cfg(book)
    prof = resolve_profile(
        book, command="judges", env=_CURSOR_HOST, check_binary=False
    )
    assert prof.cli == "cursor"
    assert prof.worker_model == "grok-4.5[effort=high,fast=false]"
    assert prof.worker_model_source == "cursor-cli-config"
    assert prof.baseline_tokens == 17200
    assert "cursor" in prof.baseline_source.lower() or "17200" in prof.baseline_source
    # And the effort reported is the one that will actually run.
    assert prof.effort == "high"
    assert prof.effort_source == "cursor-cli-config"
    assert prof.effort_channel == EFFORT_MODEL_BRACKET


def test_the_baseline_follows_the_resolved_cli_not_the_configured_one(book):
    """A ~4.4x consent error lived exactly here."""
    _write_cfg(book, headless_cli="auto")
    claude = resolve_profile(book, command="judges", env=_CLAUDE_HOST)
    cursor = resolve_profile(
        book, command="judges", env=_CURSOR_HOST, check_binary=False
    )
    assert claude.baseline_tokens == 3900
    assert cursor.baseline_tokens == 17200


# ── effort: one ladder, one channel ─────────────────────────────────────────


def test_explicit_effort_is_written_into_the_cursor_bracket(book, cursor_selected):
    cursor_selected(model="grok-4.5", effort="low")
    _write_cfg(book)
    prof = resolve_profile(
        book, command="judges", cli="cursor", effort="high", check_binary=False
    )
    assert prof.effort == "high"
    assert prof.effort_source == "cli"
    assert prof.effort_channel == EFFORT_MODEL_BRACKET
    # The level lands in the model, and the operator's other parameter survives.
    assert prof.worker_model == "grok-4.5[effort=high,fast=false]"


def test_config_effort_now_binds_on_cursor_too(book, cursor_selected):
    """`headless_effort_judges` used to be inert on Cursor — Claude argv, dropped."""
    cursor_selected(model="grok-4.5", effort="low")
    _write_cfg(book, headless_effort_judges="xhigh")
    prof = resolve_profile(book, command="judges", cli="cursor", check_binary=False)
    assert prof.effort == "xhigh" and prof.effort_source == "config"
    assert prof.worker_model == "grok-4.5[effort=xhigh,fast=false]"


def test_a_pinned_bracket_beats_the_cli_config_default(book, cursor_selected):
    _write_cfg(book)
    prof = resolve_profile(
        book,
        command="judges",
        cli="cursor",
        worker_model="gpt-5.2[effort=low]",
        check_binary=False,
    )
    assert prof.effort == "low"
    assert prof.effort_source == "model-bracket"
    assert prof.worker_model == "gpt-5.2[effort=low]"


def test_a_pinned_bracket_outranks_the_config_default(book, cursor_selected):
    """docs/LLM_PROVIDERS.md puts the typed bracket above `headless_effort_<type>`.

    The resolver read the config first, so `with_cursor_effort` overwrote the
    level the operator typed on `--worker-model` with one they never saw.
    """
    _write_cfg(book, headless_effort_judges="high")
    prof = resolve_profile(
        book,
        command="judges",
        cli="cursor",
        worker_model="grok-4.5[effort=xhigh]",
        check_binary=False,
    )
    assert prof.effort == "xhigh"
    assert prof.effort_source == "model-bracket"
    assert prof.worker_model == "grok-4.5[effort=xhigh]"


def test_a_bare_pinned_model_gets_no_invented_bracket(book, cursor_selected):
    """The harness only writes a bracket when something specific asked for one.

    The per-command effort table was measured on Claude; synthesizing it onto a
    bare `--model grok-4.5` would change argv the operator never asked to change
    (and force cursor_model_error into a live probe, since any bracket makes it
    re-validate).
    """
    _write_cfg(book)
    prof = resolve_profile(
        book,
        command="judges",
        cli="cursor",
        worker_model="grok-4.5",
        check_binary=False,
    )
    assert prof.worker_model == "grok-4.5"
    assert prof.effort is None
    assert prof.effort_source == "cursor-default"
    assert prof.effort_channel == EFFORT_NONE


def test_the_auto_model_cannot_carry_an_effort_and_says_so(book, monkeypatch):
    """`auto` takes no bracket, so report no effort rather than one that won't run."""
    from src.harness import headless

    monkeypatch.setattr(headless, "CURSOR_CLI_CONFIG", book / "missing.json")
    _write_cfg(book)
    prof = resolve_profile(
        book, command="judges", cli="cursor", effort="high", check_binary=False
    )
    assert prof.worker_model == "auto"
    assert prof.effort is None
    assert prof.effort_channel == EFFORT_NONE
    assert any("effort bracket" in w for w in prof.warnings)


def test_claude_effort_matches_resolve_headless_argv(book):
    """The two paths must not drift: same ladder, same answer."""
    cfg = _write_cfg(book, headless_effort_judges="low")
    prof = resolve_profile(book, command="judges", cli="claude", cfg=cfg)
    _argv, level, source = hstate.resolve_headless_argv(
        cfg, command="judges", cli="claude"
    )
    assert (prof.effort, prof.effort_source) == (level, source)
    assert prof.effort_channel == EFFORT_ARGV


def test_effort_default_sentinel_emits_nothing_on_claude(book):
    _write_cfg(book)
    prof = resolve_profile(book, command="judges", cli="claude", effort="default")
    assert prof.effort is None and prof.effort_source == "cli"


def test_effort_default_sentinel_leaves_the_cursor_bracket_alone(book, cursor_selected):
    """There is no flag to withhold on Cursor, so 'default' means 'don't touch it'."""
    _write_cfg(book)
    prof = resolve_profile(
        book, command="judges", cli="cursor", effort="default", check_binary=False
    )
    assert prof.worker_model == "grok-4.5[effort=high,fast=false]"
    assert prof.effort == "high" and prof.effort_source == "cli:default"


# ── warnings ────────────────────────────────────────────────────────────────


def test_a_missing_cursor_agent_downgrades_a_guess_but_not_a_choice(book, monkeypatch):
    import src.harness.profile as profile_mod

    monkeypatch.setattr(profile_mod.shutil, "which", lambda _name: None)
    _write_cfg(book)

    guessed = resolve_profile(book, command="judges", env=_CURSOR_HOST)
    assert guessed.cli == "claude"
    assert guessed.cli_source == "fallback:cursor-agent-missing"
    assert any("not on PATH" in w for w in guessed.warnings)

    # An explicit choice is honoured — the launcher already fails closed, and
    # silently running something else is worse than a clear error.
    chosen = resolve_profile(book, command="judges", cli="cursor")
    assert chosen.cli == "cursor" and chosen.cli_source == "cli"


def test_a_claude_alias_on_cursor_still_warns_from_every_entry_point(book):
    """This warning used to exist only on `fanout`, after prepare had baked it in."""
    _write_cfg(book)
    prof = resolve_profile(
        book, command="judges", cli="cursor", worker_model="sonnet",
        check_binary=False,
    )
    assert any("headless_cli=cursor" in w for w in prof.warnings)


def test_a_host_driven_cli_flip_warns_against_this_books_history(book):
    """Detection changing the CLI mid-book is legal, but must never be silent."""
    _write_cfg(book)
    log = book / ".harness" / "judges" / "usage.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(
        json.dumps({"cli": "cursor", "rc": 0, "id": "j1"}) + "\n", encoding="utf-8"
    )

    prof = resolve_profile(book, command="judges", env=_CLAUDE_HOST)
    assert prof.cli == "claude"
    assert any("previous judges waves ran on cursor" in w for w in prof.warnings)

    # An explicitly pinned CLI is a decision, not a drift — no warning.
    pinned = resolve_profile(book, command="judges", cli="claude")
    assert not any("previous judges waves" in w for w in pinned.warnings)


def test_profile_resolves_without_a_config_file(tmp_path):
    """A project mid-setup must not be the thing that raises."""
    prof = resolve_profile(tmp_path, command="judges", env=_CLAUDE_HOST)
    assert prof.cli == "claude" and prof.worker_model == "sonnet"


def test_payload_is_json_safe_and_complete(book):
    _write_cfg(book)
    payload = resolve_profile(book, command="judges", env=_CLAUDE_HOST).to_payload()
    json.dumps(payload)
    assert set(payload) == {
        "command", "cli", "cli_source", "worker_model", "worker_model_source",
        "effort", "effort_source", "effort_channel", "baseline_tokens",
        "baseline_source", "host", "warnings",
    }
