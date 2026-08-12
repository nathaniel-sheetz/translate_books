"""Which coding agent is driving this harness invocation.

The harness is run by a *host agent* — Claude Code or the Cursor agent — and the
right headless worker differs by host: a Claude Code session already holds a
Claude subscription, a Cursor session already holds a Cursor one. Until now
nothing detected that, so ``headless_cli`` defaulted to ``claude`` for everyone
and a Cursor operator paid 2-4 turns per wave correcting the worker model, the
effort and the token estimate by hand (see the 2026-08-11 judge-review friction
logs).

Detection is **env-only**: no file reads, no subprocess, no network. It answers a
question about the *current process's* parent, so anything slower than a dict
lookup is the wrong shape.

Two hard rules:

1. **Never call this from inside a spawned worker.** ``CLAUDECODE`` survives the
   child-env scrub in :mod:`src.harness.headless` (deliberately — see
   ``_SCRUB_KEEP`` there), so a ``cursor-agent`` child spawned by a Claude Code
   parent inherits ``CLAUDECODE=1`` and would detect its *parent's* host. The
   structural guard is that :mod:`src.harness.headless` — the launcher — must
   never import this module; ``tests/test_spawn_boundary.py`` pins that.
2. **Detection only ever supplies a default.** An explicit flag and an explicitly
   configured ``headless_cli`` both outrank it (see
   :func:`src.harness.profile.resolve_profile`), and the resolved answer is
   always reported with its source so an operator can see it was a guess.
"""

from __future__ import annotations

import os
from typing import Mapping

HOST_CLAUDE_CODE = "claude-code"
HOST_CURSOR = "cursor"
HOST_UNKNOWN = "unknown"

HOSTS = (HOST_CLAUDE_CODE, HOST_CURSOR, HOST_UNKNOWN)

# Escape hatch and test seam. Set it to any value in HOSTS to pin the answer;
# anything else is ignored rather than trusted, so a typo degrades to real
# detection instead of to a wrong host. The test suite sets it to "unknown"
# (autouse fixture) so results never depend on who ran pytest.
HOST_OVERRIDE_ENV = "HARNESS_HOST"

# Claude Code marks its own child processes. ``CLAUDECODE=1`` is the canonical
# one; ``CLAUDE_CODE_ENTRYPOINT`` is set alongside it and survives as a second
# witness if the first is ever dropped.
_CLAUDE_CODE_MARKERS: tuple[str, ...] = (
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
)

# cursor-agent's own process markers. CURSOR_API_KEY is deliberately NOT here:
# it is a credential a user can export in any shell, so it says nothing about who
# is driving — and it is scrubbed from worker envs anyway.
_CURSOR_AGENT_MARKERS: tuple[str, ...] = (
    "CURSOR_AGENT",
    "CURSOR_CONVERSATION_ID",
    "CURSOR_INVOKED_AS",
    "CURSOR_AGENT_STORE",
)

# Signals that look tempting and are wrong. Verified 2026-08-11 in a Claude Code
# session running inside Cursor's integrated terminal, which had ALL of these
# set while the host was Claude Code:
#
#   TERM_PROGRAM=vscode          (Cursor is a VS Code fork)
#   VSCODE_GIT_ASKPASS_NODE=…\cursor\Cursor.exe
#   PATH=…\Programs\cursor\…;…\cursor-agent
#   AI_AGENT=claude-code_2-1-227_agent
#
# The editor a terminal is docked in is not the agent driving the terminal, and
# an installed CLI on PATH is not a running one. Only per-process markers count.
# (AI_AGENT happens to be right here, but it is set by the Claude Code build and
# has no documented Cursor counterpart, so it is not load-bearing.)


def _has_marker(env: Mapping[str, str], names: tuple[str, ...]) -> bool:
    """True when any of ``names`` is present in ``env`` with a non-empty value.

    Presence is the signal, not the value: ``CLAUDECODE=1`` and
    ``CURSOR_INVOKED_AS=cursor-agent`` carry different payloads and neither is
    worth parsing. An empty string is treated as absent — an exported-but-blank
    variable is how a shell profile unsets one in practice.
    """
    for name in names:
        try:
            value = env.get(name)
        except Exception:  # pragma: no cover - a Mapping that raises on get
            return False
        if value is None:
            continue
        if str(value).strip():
            return True
    return False


def detect_host(env: Mapping[str, str] | None = None) -> str:
    """The agent hosting this process: ``claude-code`` / ``cursor`` / ``unknown``.

    ``env`` defaults to :data:`os.environ`; passing one explicitly is the test
    seam (same shape as ``subscription_env(base=…)`` in
    :mod:`src.harness.headless`).

    Claude Code is checked first. That ordering is the safe one for the case that
    actually occurs: a ``cursor-agent`` worker spawned from a Claude Code parent
    carries *both* families of marker, and resolving it to the parent's host is
    the conservative answer — though the real protection is rule 1 in the module
    docstring, which says not to ask from inside a worker at all.

    Never raises: a hostile ``env`` mapping degrades to :data:`HOST_UNKNOWN`.
    """
    source: Mapping[str, str] = os.environ if env is None else env

    try:
        override = str(source.get(HOST_OVERRIDE_ENV) or "").strip().lower()
    except Exception:  # pragma: no cover - a Mapping that raises on get
        return HOST_UNKNOWN
    if override in HOSTS:
        return override

    if _has_marker(source, _CLAUDE_CODE_MARKERS):
        return HOST_CLAUDE_CODE
    if _has_marker(source, _CURSOR_AGENT_MARKERS):
        return HOST_CURSOR
    return HOST_UNKNOWN


def host_cli(host: str) -> str | None:
    """The headless CLI family a host implies, or ``None`` when it implies none.

    ``None`` is a real answer and means "detection has no opinion" — the caller
    then falls back to its own default rather than to a coin flip. Kept separate
    from :func:`detect_host` so the host stays reportable in its own right: an
    operator seeing ``cli_source: "host:cursor"`` learns *why* the wave chose
    what it chose.
    """
    normalized = (host or "").strip().lower()
    if normalized == HOST_CLAUDE_CODE:
        return "claude"
    if normalized == HOST_CURSOR:
        return "cursor"
    return None
