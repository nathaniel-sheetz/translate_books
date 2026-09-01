"""One resolved answer for "what will this headless wave actually run as?".

Four knobs decide a wave — the CLI family, the worker model, the effort, and the
token baseline the consent estimate is quoted in — and until now each was
resolved independently, in a different place, at a different time:

- ``prepare`` read ``headless_cli`` from config (so a Cursor operator got
  ``sonnet``), while ``--cli cursor`` only existed on ``fanout``, one command too
  late to fix the manifest it had already written.
- The baseline was looked up with the *unresolved* CLI, quoting Claude's 3.9k
  per-job overhead for a wave that would pay Cursor's 17.2k — a 4.4x consent
  error, measured at 2.6x on a real book.
- Effort had two channels and only one was reported: ``--effort`` (Claude argv,
  silently dropped by ``cursor-agent``) and the model's ``[effort=…]`` bracket
  (the only one Cursor honours). A wave running ``effort=high`` announced itself
  as ``medium``.

This module answers all four together, records **where each answer came from**,
and is the only place allowed to consult :mod:`src.harness.host`. Callers get a
:class:`HeadlessProfile` they can put in front of an operator whole, instead of
four numbers assembled from four layers that disagree.

**Never import this from :mod:`src.harness.headless`.** That module is the
launcher: the parent env leaks into spawned workers by design (``CLAUDECODE``
survives the credential scrub), so a worker asking "who is my host?" gets its
*parent's* answer. Keeping detection out of the launcher's import graph makes
that a structural fact rather than a rule someone has to remember;
``tests/test_spawn_boundary.py`` pins it.

Resolution costs no subprocess and no LLM call — the heaviest things it does are
``shutil.which`` and reading a few small files (the book's ``config.json``, the
Cursor CLI config, and ``usage.jsonl`` for the overhead baseline). It is safe to
call from a read-only command.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from src.harness import state as hstate
from src.harness.headless import (
    cli_binary,
    cli_binary_present,
    cursor_model_effort,
    default_worker_model,
    warn_cursor_claude_model,
    with_cursor_effort,
)
from src.harness.host import detect_host, host_cli
from src.harness.usage import baseline_tokens, read_recent

# Where each wave type's per-job rows live, relative to the project dir. Kept in
# one place so a caller cannot silently get the *constant* baseline (rather than
# this book's measured one) by forgetting to pass a log path.
USAGE_LOG_RELPATH: dict[str, tuple[str, ...]] = {
    "judges": (".harness", "judges", "usage.jsonl"),
    "annotations": (".harness", "annotations", "usage.jsonl"),
    "translate": (".harness", "translate", "usage.jsonl"),
    "footnotes": (".harness", "footnotes", "usage.jsonl"),
}

# How the resolved effort actually reaches the model.
EFFORT_ARGV = "argv"                  # claude: --effort <level>
EFFORT_MODEL_BRACKET = "model_bracket"  # cursor: grok-4.5[effort=<level>]
EFFORT_NONE = "none"                  # nothing carries it; say so out loud

# ``cli_source`` values that mean someone chose this. A flag (``cli``), a pin
# (``config``) and a prepared manifest (``manifest``) are never second-guessed
# against PATH; inferred sources (``host:*``, the bare fallback) may be — see
# :func:`resolve_profile`.
#
# ``manifest`` belongs here because it is a *record* of a decision, not a guess:
# `prepare` resolves with ``check_binary=True``, so whatever it wrote is already
# post-fallback, and the operator consented to that CLI when the estimate quoted
# its baseline. Leaving it guessable let `prepare --cli cursor` be honoured and
# then silently overturned by a bare `fanout` one command later.
# `automation.default_cli` belongs here for the same reason a flag or a pin does:
# an operator wrote it in `app_config.json` to decide the un-pinned books. Leaving
# it out made it a "guess", so the nightly pass emitted a provenance warning for
# every un-pinned book on every run, and the missing-binary switch would have
# silently flipped the family the operator had just chosen.
_DECIDED_CLI_SOURCES = frozenset({"cli", "config", "manifest", "automation.default_cli"})


def _is_guessed_cli(cli_source: str) -> bool:
    """True when the CLI was inferred (host detection or the bare fallback)."""
    return cli_source not in _DECIDED_CLI_SOURCES


def _other_cli(cli: str) -> str:
    """The launcher family that is not ``cli``."""
    return "claude" if cli == "cursor" else "cursor"


def usage_log_for(project_dir: Path | str, command: str) -> Path | None:
    """This wave type's ``usage.jsonl``, or ``None`` for an unknown command."""
    parts = USAGE_LOG_RELPATH.get(command)
    if not parts:
        return None
    return Path(project_dir).joinpath(*parts)


def resolve_cli(
    cfg: Mapping[str, object] | None = None,
    *,
    override: str | None = None,
    override_source: str = "cli",
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """The launcher family to drive, and where that answer came from.

    Precedence, highest first:

    1. ``override`` — an explicit ``--cli`` → ``override_source`` (default ``"cli"``;
       ``fanout`` passes ``"manifest"`` when the value came off disk rather than
       off a flag, so the reported provenance is not a lie)
    2. ``cfg["headless_cli"]`` naming a real family → ``"config"``
    3. the detected host → ``"host:claude-code"`` / ``"host:cursor"``
    4. ``claude`` → ``"fallback"``

    Config outranks detection on purpose: a book pinned to ``claude`` stays on
    Claude no matter who opens it, so pinning is a permanent one-time fix. Tier 3
    only fires for a book whose ``headless_cli`` is ``auto`` — i.e. one that never
    chose — which before this existed silently meant "Claude, and good luck".
    """
    candidate = (override or "").strip().lower()
    if candidate in hstate.HEADLESS_CLIS:
        return candidate, override_source

    configured = str((cfg or {}).get("headless_cli") or "").strip().lower()
    if configured in hstate.HEADLESS_CLIS:
        return configured, "config"

    host = detect_host(env)
    from_host = host_cli(host)
    if from_host:
        return from_host, f"host:{host}"

    return "claude", "fallback"


@dataclass(frozen=True)
class HeadlessProfile:
    """What a wave will run as, with the provenance of every field.

    Every consumer relays this whole block rather than cherry-picking, because
    the fields are only interpretable together: ``baseline_tokens`` means nothing
    without ``cli``, and ``effort`` means nothing without ``effort_channel``.
    """

    command: str
    cli: str
    cli_source: str
    worker_model: str
    worker_model_source: str
    effort: str | None
    effort_source: str
    effort_channel: str
    baseline_tokens: int
    baseline_source: str
    host: str
    warnings: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe form, for a CLI payload the agent relays verbatim."""
        return {
            "command": self.command,
            "cli": self.cli,
            "cli_source": self.cli_source,
            "worker_model": self.worker_model,
            "worker_model_source": self.worker_model_source,
            "effort": self.effort,
            "effort_source": self.effort_source,
            "effort_channel": self.effort_channel,
            "baseline_tokens": self.baseline_tokens,
            "baseline_source": self.baseline_source,
            "host": self.host,
            "warnings": list(self.warnings),
        }


def _prior_clis(usage_log: Path | None) -> set[str]:
    """CLI families this wave type has actually run on before, from its log."""
    if usage_log is None:
        return set()
    seen: set[str] = set()
    for row in read_recent(usage_log):
        name = str(row.get("cli") or "").strip().lower()
        if name:
            seen.add(name)
    return seen


def resolve_profile(
    project_dir: Path | str,
    *,
    command: str,
    cli: str | None = None,
    cli_source: str = "cli",
    worker_model: str | None = None,
    worker_model_source: str = "cli",
    effort: str | None = None,
    effort_source: str = "cli",
    cfg: Mapping[str, object] | None = None,
    env: Mapping[str, str] | None = None,
    usage_log: Path | str | None = None,
    check_binary: bool = True,
) -> HeadlessProfile:
    """Resolve every knob for one wave type of one book, with provenance.

    ``worker_model`` / ``effort`` are the per-run overrides (``--worker-model`` /
    ``--effort``); ``cli`` is ``--cli``. ``cfg`` defaults to the book's config and
    ``usage_log`` to this wave type's own log — pass them only to avoid a re-read
    or in tests.

    ``cli_source`` / ``worker_model_source`` / ``effort_source`` label where a
    passed-in value came from. ``fanout`` sets them to ``"manifest"`` for values
    it read off disk: the provenance strings are the reason this block can be
    relayed to an operator at all, so "a flag said so" and "the consented
    manifest said so" must not print identically.
    """
    project_dir = Path(project_dir)
    if cfg is None:
        cfg = hstate.load_config(project_dir)
    if usage_log is None:
        usage_log = usage_log_for(project_dir, command)
    else:
        usage_log = Path(usage_log)

    warnings: list[str] = []
    host = detect_host(env)
    cli_name, cli_source = resolve_cli(
        cfg, override=cli, override_source=cli_source, env=env
    )

    # A *guess* pointing at a CLI that is not installed is a bad guess, not a
    # decision — quoting Cursor's 17.2k baseline for a wave that will fall back
    # to Claude is the same class of consent error this module exists to kill.
    # Symmetric, because both directions occur: a Cursor host with no
    # `cursor-agent`, and — the dashboard's case — a Flask server launched from a
    # plain shell, where `detect_host` says `unknown`, tier 4 answers `claude`,
    # and a Cursor-only machine would only discover that at the auth preflight.
    #
    # An explicit choice — a flag, a pin, or the manifest a wave was consented to
    # from — is never second-guessed: the launcher already fails closed on a
    # missing binary, and silently running something other than what the operator
    # asked for is worse than a clear error. Neither is a guess with
    # nothing to switch *to* — leaving it in place means the operator is told to
    # install the CLI the host implies rather than the one we happened to pick.
    if check_binary and _is_guessed_cli(cli_source) and not cli_binary_present(cli_name):
        missing_bin = cli_binary(cli_name)
        alternative = _other_cli(cli_name)
        if cli_binary_present(alternative):
            reason = (
                f"the detected {host} host"
                if cli_source.startswith("host:")
                else "the default (no host detected)"
            )
            warnings.append(
                f"{reason} selects {cli_name} but {missing_bin!r} is not on PATH; "
                f"falling back to {alternative} (pass --cli {cli_name} to insist)"
            )
            cli_name, cli_source = alternative, f"fallback:{missing_bin}-missing"
        else:
            warnings.append(
                f"{missing_bin!r} is not on PATH and neither is "
                f"{cli_binary(alternative)!r}; a headless wave cannot run until "
                f"one of them is installed"
            )

    # ── worker model ────────────────────────────────────────────────────────
    pinned_model = (worker_model or "").strip()
    if pinned_model:
        resolved_model = pinned_model
        model_source = worker_model_source
    else:
        resolved_model = default_worker_model(cli_name)
        model_source = (
            "cursor-cli-config" if cli_name == "cursor" else "default:claude"
        )

    # ── effort: one ladder … ────────────────────────────────────────────────
    resolved_effort: str | None
    override = (effort or "").strip() or None
    override_source = effort_source

    if override in hstate.EFFORT_LEVELS:
        resolved_effort, effort_source = override, override_source
    elif override == "default":
        # "emit no flag" on Claude. On Cursor there is no flag to withhold, so it
        # means "leave the model's own bracket alone" rather than "no effort".
        if cli_name == "cursor":
            resolved_effort = cursor_model_effort(resolved_model)
            effort_source = f"{override_source}:default"
        else:
            resolved_effort, effort_source = None, override_source
    else:
        configured = cfg.get(hstate.effort_config_key(command))
        configured = configured if isinstance(configured, str) else "auto"
        pinned_bracket = (
            cursor_model_effort(resolved_model)
            if cli_name == "cursor" and pinned_model
            else None
        )
        if pinned_bracket:
            # A level typed into the pinned model's own bracket is a more
            # specific instruction than the book-level default, so it outranks
            # `headless_effort_<type>` — the ladder docs/LLM_PROVIDERS.md
            # promises. Reading the config first used to silently overwrite the
            # bracket the operator typed on `--worker-model`.
            resolved_effort, effort_source = pinned_bracket, "model-bracket"
        elif configured in hstate.EFFORT_LEVELS:
            resolved_effort, effort_source = configured, "config"
        elif configured == "default":
            resolved_effort, effort_source = None, "config"
        elif cli_name == "cursor" and cursor_model_effort(resolved_model):
            # The operator already chose an effort in Cursor's own model picker
            # (a pinned model's typed bracket is handled above). Honour it
            # rather than overwriting it with a table default they never saw.
            resolved_effort = cursor_model_effort(resolved_model)
            effort_source = "cursor-cli-config"
        elif cli_name == "cursor":
            # Nothing specific asked for an effort, and on Cursor "the default"
            # is not ours to invent: the per-command table was measured on Claude
            # (see CLI_COMMAND_EFFORT_DEFAULTS), and writing a bracket a bare
            # `--model grok-4.5` never had would change argv the operator did not
            # ask to change — and force `cursor_model_error` into a live probe,
            # since any bracket makes it re-validate. Leave the model alone and
            # say plainly that nothing here sets the level.
            resolved_effort, effort_source = None, "cursor-default"
        else:
            resolved_effort = hstate.command_effort_default(command, cli=cli_name)
            effort_source = f"default:{command}"

    # ── … and one channel ───────────────────────────────────────────────────
    if resolved_effort is None:
        # On Claude "no effort" is still delivered by argv (by omitting the flag),
        # which is a decision the wave carries out. On Cursor nothing carries it.
        effort_channel = EFFORT_ARGV if cli_name == "claude" else EFFORT_NONE
    elif cli_name == "cursor":
        resolved_model = with_cursor_effort(resolved_model, resolved_effort)
        if cursor_model_effort(resolved_model) == resolved_effort:
            effort_channel = EFFORT_MODEL_BRACKET
        else:
            # `auto` takes no bracket, so nothing carries the level. Say so
            # rather than reporting an effort the wave will not run at.
            effort_channel = EFFORT_NONE
            warnings.append(
                f"cursor model {resolved_model!r} takes no effort bracket, so "
                f"effort {resolved_effort!r} will not be applied; pin a concrete "
                f"model (e.g. grok-4.5) to control effort"
            )
            resolved_effort, effort_source = None, "unsupported:auto-model"
    else:
        effort_channel = EFFORT_ARGV

    # ── baseline the consent estimate is quoted in ──────────────────────────
    baseline, baseline_source = baseline_tokens(usage_log, cli=cli_name)

    # ── warnings ────────────────────────────────────────────────────────────
    alias_warning = warn_cursor_claude_model(cli_name, resolved_model)
    if alias_warning:
        warnings.append(alias_warning)

    # A book flipping CLI mid-way is legal and sometimes intended, but it must
    # never be silent: on translate/footnotes it changes the model a book's own
    # prose is written by, and detection can flip it just by opening the project
    # from a different agent.
    #
    # Every *inferred* source, not just `host:*` — a `fallback:*` from the
    # missing-binary switch above flips the family exactly as hard, and the
    # dashboard reaches it more often than detection. A `manifest` is exempt for
    # the same reason it is exempt from the switch: `prepare` already emitted this
    # warning when it wrote that manifest, so repeating it on every `fanout` that
    # faithfully reproduces the consented wave is noise, not a flip.
    if _is_guessed_cli(cli_source):
        prior = _prior_clis(usage_log)
        others = sorted(prior - {cli_name})
        if others:
            warnings.append(
                f"this book's previous {command} waves ran on "
                f"{', '.join(others)}; this one resolves to {cli_name} "
                f"({cli_source}). Pin it with `config-set --key headless_cli "
                f"--value {others[0]}` if that is not intended"
            )

    return HeadlessProfile(
        command=command,
        cli=cli_name,
        cli_source=cli_source,
        worker_model=resolved_model,
        worker_model_source=model_source,
        effort=resolved_effort,
        effort_source=effort_source,
        effort_channel=effort_channel,
        baseline_tokens=baseline,
        baseline_source=baseline_source,
        host=host,
        warnings=warnings,
    )
