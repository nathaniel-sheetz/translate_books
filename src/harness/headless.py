"""Shared headless CLI wave launcher for translate- and judge-fanout.

Both fan-outs need the same Windows/cwd/absolutize/wave fixes:

- Resolve the CLI binary via ``shutil.which`` (PATHEXT) when using the real runner.
- Run from a neutral empty cwd so project ``CLAUDE.md`` / workspace context is not
  auto-loaded.
- Absolutize ``--system-prompt-file`` (worker cwd is neutral, not the project).
- Run jobs in a rolling pool ``concurrency`` wide (a free slot takes the next job).
- Give each Cursor worker its own ``CURSOR_CONFIG_DIR`` so they cannot race it.
- Scrub every metered credential from the child env (``subscription_env``).
- Refuse to start until the CLI confirms a subscription login
  (``subscription_auth_error``).

CLI families are selected with ``cli`` (``claude`` | ``cursor``). The Claude profile
preserves today's ``claude -p`` argv. The Cursor profile drives ``cursor-agent``
under a subscription login (no metered API key).

Headless is the subscription backend; ``--backend api`` is the metered one. That
split used to be a convention and it leaked: the parent process legitimately
holds ``ANTHROPIC_API_KEY`` (``src/api_translator.py`` calls ``load_dotenv()`` at
import, and every ``fanout`` entry point imports it transitively), ``subprocess``
inherits ``os.environ`` by default, and the CLI prefers that key over the
subscription session. Waves silently billed metered credit until the balance ran
out mid-run. Two layers now make that structurally impossible, and neither
subsumes the other:

- The **scrub** is the only thing that catches endpoint redirection --
  ``claude auth status`` reports a clean subscription even with
  ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_CUSTOM_HEADERS`` set.
- The **preflight** is the only thing that catches non-env routing -- an
  ``apiKeyHelper`` in a settings file, or a console-account login, both of which
  an env scrub cannot see.

Fixing this at the spawn instead of at ``load_dotenv()`` is deliberate: the
metered path needs that key, and a boundary scrub is invariant to import order,
shell exports and CI injection, whereas "don't put the key in ``os.environ``" is
a convention that decays.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from src.harness.usage import (
    append_usage,
    approx_tokens,
    baseline_tokens,
    job_record,
    median_wall_s,
    rollup,
    usage_from_envelope,
)

logger = logging.getLogger(__name__)

Runner = Callable[..., tuple[int, str, str]]

_CLI_DEFAULT_BINS = {
    "claude": "claude",
    "cursor": "cursor-agent",
}
_SUPPORTED_CLIS = frozenset(_CLI_DEFAULT_BINS)

# Per-job subprocess timeout (seconds). Cursor -p is known to hang; Claude gets
# a higher ceiling so long Sonnet jobs are not killed mid-flight.
_CLI_JOB_TIMEOUT_S = {
    "claude": 30 * 60,
    "cursor": 15 * 60,
}

# After a timed-out worker's tree is killed, how long to wait for its pipes to
# drain. Bounded on purpose: an *unbounded* drain is the bug this replaces.
_DRAIN_TIMEOUT_S = 10
# Ceiling on the tree-kill call itself, so cleanup cannot become the new hang.
_TREE_KILL_TIMEOUT_S = 15

# Claude Code worker aliases that look wrong when paired with headless_cli=cursor.
_CLAUDE_WORKER_ALIASES = frozenset({"sonnet", "opus", "haiku", "fable"})

# ---------------------------------------------------------------------------
# Subscription enforcement: env scrub
# ---------------------------------------------------------------------------

# Whole ``ANTHROPIC_`` namespace, by prefix rather than by name. The known
# offenders are ANTHROPIC_API_KEY / _AUTH_TOKEN / _BASE_URL / _CUSTOM_HEADERS /
# _BEDROCK_BASE_URL / _VERTEX_BASE_URL, but the failure modes are asymmetric:
# missing one silently bills money, while over-scrubbing at worst drops a model
# default -- and fan-out always passes --model explicitly, so no ANTHROPIC_* var
# is load-bearing here.
_SCRUB_PREFIXES: tuple[str, ...] = ("ANTHROPIC_",)

# One union list for every CLI family, deliberately: `claude` never reads
# CURSOR_API_KEY and `cursor-agent` never reads the Anthropic vars, so merging
# costs nothing -- and a union cannot be under-applied by someone adding a CLI
# family and mis-filing their variable.
_SCRUB_NAMES: frozenset[str] = frozenset({
    # Third-party provider routing: not Anthropic API credit, but not the
    # subscription either -- these bill AWS / GCP / Azure.
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
    # Cursor profile.
    "CURSOR_API_KEY",
})

# CLAUDE_CODE_OAUTH_TOKEN *is* subscription auth (`claude setup-token` requires a
# subscription), so it survives. Named explicitly so nobody widens the scrub to
# the whole CLAUDE_CODE_ prefix and breaks token-based subscription logins.
_SCRUB_KEEP: frozenset[str] = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})

# Not scrubbed, on purpose: AWS_* / GOOGLE_APPLICATION_CREDENTIALS are only read
# when the CLAUDE_CODE_USE_* switch above is set, and that switch is gone.

# ---------------------------------------------------------------------------
# Prompt-cache TTL control (Claude profile only)
# ---------------------------------------------------------------------------
#
# The CLI defaults to a 1-hour ephemeral cache TTL, billed at 2× base input.
# FORCE_PROMPT_CACHING_5M=1 switches to the 5-minute TTL at 1.25×; reads still
# work at 0.1×. DISABLE_PROMPT_CACHING=1 turns caching off entirely (plain 1×
# input, no reads). Every wave in this repo finishes in seconds-to-minutes, so
# the 1-hour premium is usually wasted — see docs/LLM_PROVIDERS.md.
#
# Known-but-unused siblings (per-model DISABLE_PROMPT_CACHING_{SONNET,OPUS,
# HAIKU,FABLE,MYTHOS} and ENABLE_PROMPT_CACHING_1H): the bare names covered
# Sonnet in the 2026-08-01 probe; leave them alone rather than guess.

FORCE_PROMPT_CACHING_5M = "FORCE_PROMPT_CACHING_5M"
DISABLE_PROMPT_CACHING = "DISABLE_PROMPT_CACHING"

CACHE_MODES = frozenset({"auto", "5m", "1h", "off"})
# Concrete modes the CLI env can express (auto resolves to one of these).
CACHE_CONCRETE = frozenset({"5m", "1h", "off"})
# Write multipliers in plain-input-equivalent tokens.
_CACHE_WRITE_MULT = {"1h": 2.0, "5m": 1.25}
_CACHE_READ_MULT = 0.1
# 5-minute TTL with ~30 s margin: a warm-up longer than this risks expiring
# before any follower reads the entry.
CACHE_WARM_RISK_S = 270.0


def prompt_cache_env(
    env: Mapping[str, str], *, mode: str
) -> dict[str, str]:
    """Return ``env`` with the Claude prompt-cache TTL knob for ``mode``.

    Deliberately separate from :func:`subscription_env`: that function is a
    billing-safety boundary and must not grow cost knobs. ``1h`` is the CLI
    default and sets nothing; ``5m`` sets ``FORCE_PROMPT_CACHING_5M=1``; ``off``
    sets ``DISABLE_PROMPT_CACHING=1``.

    Both names are cleared first, so the resolved mode is the one that actually
    takes effect. An inherited ``DISABLE_PROMPT_CACHING`` left over from a probe
    would otherwise silently win over a resolved ``5m`` while every usage row
    recorded ``"cache": "5m"`` — turning the A/B corpus this log exists to be
    into a quietly wrong one.
    """
    out = dict(env)
    out.pop(FORCE_PROMPT_CACHING_5M, None)
    out.pop(DISABLE_PROMPT_CACHING, None)
    if mode == "1h":
        return out
    if mode == "5m":
        out[FORCE_PROMPT_CACHING_5M] = "1"
        return out
    if mode == "off":
        out[DISABLE_PROMPT_CACHING] = "1"
        return out
    raise ValueError(
        f"unknown prompt-cache mode {mode!r}; expected one of "
        f"{sorted(CACHE_CONCRETE)}"
    )


def _cache_group_cost(
    jobs: Sequence[Mapping[str, Any]],
    spf_tokens: Mapping[str, int],
    baseline: int,
    mode: str,
) -> float:
    """Plain-input-equivalent token cost of ``jobs`` under ``mode``.

    Jobs are grouped by ``system_prompt_file`` so a mixed dialogue+address wave
    is priced honestly (first job of each group writes; the rest read). ``P`` is
    the shared prefix (``spf_tokens[spf] + baseline``), including the CLI's own
    fixed context — cacheable even when a job has no ``--system-prompt-file``.
    """
    groups: dict[Any, list[int]] = {}
    for job in jobs:
        spf = job.get("system_prompt_file")
        groups.setdefault(spf, []).append(approx_tokens(job.get("input_text")))

    total = 0.0
    for spf, bodies in groups.items():
        prefix = (spf_tokens.get(spf, 0) if spf else 0) + baseline
        if mode == "off":
            total += sum(prefix + body for body in bodies)
            continue
        write = _CACHE_WRITE_MULT[mode]
        total += write * (prefix + bodies[0])
        for body in bodies[1:]:
            total += _CACHE_READ_MULT * prefix + write * body
    return total


def resolve_cache_mode(
    jobs: Sequence[Mapping[str, Any]],
    spf_tokens: Mapping[str, int],
    baseline: int,
    warm_wall_s: float | None,
) -> str:
    """Pick ``5m`` / ``1h`` / ``off`` from job shapes and warm-up history.

    Pure and unit-testable. Compares the off vs 5m wave totals directly (not a
    U/P ratio heuristic). A measured warm-up longer than
    :data:`CACHE_WARM_RISK_S` keeps ``1h`` reachable — the one case where a
    favorable ratio plus a slow warm-up makes the 1-hour TTL win. No history
    assumes a fast warm-up (``5m``).
    """
    if not jobs:
        return "5m"
    cost_5m = _cache_group_cost(jobs, spf_tokens, baseline, "5m")
    cost_off = _cache_group_cost(jobs, spf_tokens, baseline, "off")
    if cost_off < cost_5m:
        return "off"
    if warm_wall_s is not None and warm_wall_s > CACHE_WARM_RISK_S:
        return "1h"
    return "5m"


def effective_wave_tokens(
    jobs: Sequence[Mapping[str, Any]],
    spf_tokens: Mapping[str, int],
    baseline: int,
    mode: str,
) -> int:
    """Projected plain-input-equivalent tokens for a wave under ``mode``."""
    if mode not in CACHE_CONCRETE:
        raise ValueError(
            f"effective_wave_tokens needs a concrete mode, got {mode!r}"
        )
    return int(round(_cache_group_cost(jobs, spf_tokens, baseline, mode)))


def subscription_env(
    cli: str = "claude", *, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """``os.environ`` minus every var that could bill something other than the sub.

    A denylist, not an allowlist: PATH / PATHEXT / SYSTEMROOT / COMSPEC and the
    rest of the ordinary runtime survive, which Windows ``CreateProcess`` and the
    ``claude.CMD`` npm shim both require.

    ``cli`` is accepted so a future family can declare a *keep* (as Claude does
    for ``CLAUDE_CODE_OAUTH_TOKEN``); the scrub list itself is shared. It never
    raises on an unknown ``cli`` -- this function must not be the thing that
    breaks a spawn.
    """
    del cli  # reserved: per-family keeps, not per-family scrubs
    source = os.environ if base is None else base
    out: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if upper in _SCRUB_KEEP:
            out[key] = value
            continue
        if upper in _SCRUB_NAMES:
            continue
        if any(upper.startswith(prefix) for prefix in _SCRUB_PREFIXES):
            continue
        # Emit the original spelling: env keys are case-sensitive on POSIX.
        out[key] = value
    return out


def warn_cursor_claude_model(cli: str, worker_model: str) -> str | None:
    """Return a warning when cursor is paired with a Claude-looking worker_model.

    Now a check on an *explicit* choice rather than on our own default: an
    un-pinned Cursor wave resolves through :func:`default_worker_model`, which
    never emits a Claude alias.
    """
    if (cli or "").strip().lower() != "cursor":
        return None
    # Strip any ``[effort=…,fast=…]`` suffix before the alias check — same base
    # as ``_cursor_model_base``, so ``sonnet[effort=low]`` still warns.
    alias = _cursor_model_base(worker_model).lower()
    if not alias:
        return None
    if alias in _CLAUDE_WORKER_ALIASES or alias.startswith("claude-"):
        return (
            f"worker_model={worker_model!r} looks like a Claude alias/id while "
            f"headless_cli=cursor; set --worker-model to a Cursor model "
            f"(e.g. grok-4.5 or auto)"
        )
    return None


# The file the interactive Cursor orchestrator itself runs on: whatever model is
# selected there is the one the operator already chose and is already paying for.
CURSOR_CLI_CONFIG = Path.home() / ".cursor" / "cli-config.json"

# `modelId` for "let Cursor pick", spelled two ways across CLI versions. Both mean
# the `auto` the --model flag accepts.
_CURSOR_AUTO_IDS = frozenset({"", "default", "auto"})

CURSOR_FALLBACK_MODEL = "auto"


def cursor_default_model(config_path: Path | str | None = None) -> str:
    """The model ``cursor-agent`` is currently configured to use, in argv form.

    Reads ``selectedModel`` from ``~/.cursor/cli-config.json`` and composes the
    bracket form the CLI accepts (``grok-4.5[effort=medium,fast=false]``). A
    ``modelId`` of ``default`` — Cursor's own "Auto" — becomes ``auto``.

    **Never raises and never blocks**: a missing, unreadable, or unrecognised
    config returns :data:`CURSOR_FALLBACK_MODEL`, because failing to read a
    preferences file must not be the thing that stops a wave.
    """
    path = Path(config_path) if config_path is not None else CURSOR_CLI_CONFIG
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return CURSOR_FALLBACK_MODEL
    if not isinstance(doc, dict):
        return CURSOR_FALLBACK_MODEL

    selected = doc.get("selectedModel")
    if not isinstance(selected, dict):
        return CURSOR_FALLBACK_MODEL
    model_id = str(selected.get("modelId") or "").strip()
    if model_id.lower() in _CURSOR_AUTO_IDS:
        return CURSOR_FALLBACK_MODEL

    params: dict[str, str] = {}
    for param in selected.get("parameters") or []:
        if not isinstance(param, dict):
            continue
        pid = str(param.get("id") or "").strip()
        value = param.get("value")
        if not pid or value is None or isinstance(value, (dict, list)):
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        params[pid] = str(value)
    return compose_cursor_model(model_id, params)


def default_worker_model(cli: str) -> str:
    """The worker model to use when nobody pinned one, for this CLI family.

    Claude keeps ``sonnet``. Cursor inherits whatever the operator already
    selected in the Cursor CLI, so an un-pinned Cursor wave runs the model they
    are already using rather than a Claude alias that only produced a warning.
    """
    if (cli or "").strip().lower() == "cursor":
        return cursor_default_model()
    return "sonnet"


def _cursor_model_base(model: str) -> str:
    """A Cursor model id with any ``[effort=…,fast=…]`` suffix stripped."""
    return parse_cursor_model(model)[0]


# ---------------------------------------------------------------------------
# Cursor model brackets: the CLI's only effort channel
# ---------------------------------------------------------------------------
#
# ``cursor-agent`` takes its knobs inside the model argument
# (``grok-4.5[effort=high,fast=false]``) and accepts no ``--effort`` flag; the
# Claude-argv ``--effort`` is dropped from a Cursor wave by ``_build_cmd``. So on
# Cursor the bracket IS the effort, and it has to be readable and writable rather
# than an opaque string — otherwise the harness reports the Claude answer
# (``medium``) beside a wave running ``effort=high``, which is exactly what the
# 2026-08-11 friction logs caught it doing twice.


def parse_cursor_model(model: str | None) -> tuple[str, dict[str, str]]:
    """Split a Cursor model argument into ``(base_id, params)``.

    ``"grok-4.5[effort=high,fast=false]"`` -> ``("grok-4.5", {"effort": "high",
    "fast": "false"})``. A model with no bracket yields an empty param dict, and
    junk (an unterminated bracket, a bare ``,``, a valueless key) is dropped
    rather than raised on: this parses argv the operator may have typed, and a
    malformed knob must not be able to stop a wave. :func:`compose_cursor_model`
    is its inverse for everything it accepts.
    """
    text = (model or "").strip()
    if not text:
        return "", {}
    base, sep, rest = text.partition("[")
    base = base.strip()
    if not sep:
        return base, {}
    params: dict[str, str] = {}
    for item in rest.rstrip("]").split(","):
        key, eq, value = item.partition("=")
        key = key.strip()
        if not key or not eq:
            continue
        params[key] = value.strip()
    return base, params


def compose_cursor_model(base: str, params: Mapping[str, str] | None = None) -> str:
    """Rebuild a Cursor model argument from ``base`` and ``params``.

    Insertion order is preserved so a round-trip through
    :func:`parse_cursor_model` is byte-identical, which keeps the model string
    stable in manifests, argv and ``usage.jsonl`` across re-resolution.
    """
    base = (base or "").strip()
    pairs = [f"{k}={v}" for k, v in (params or {}).items() if str(k).strip()]
    if not base or not pairs:
        return base
    return f"{base}[{','.join(pairs)}]"


def cursor_model_effort(model: str | None) -> str | None:
    """The effort level carried by a Cursor model argument, if it carries one."""
    value = parse_cursor_model(model)[1].get("effort")
    value = (value or "").strip()
    return value or None


def with_cursor_effort(model: str | None, effort: str | None) -> str:
    """``model`` with its ``effort=`` parameter set to ``effort``.

    Other parameters are preserved (``fast=false`` survives an effort change) and
    a ``None`` effort leaves the model untouched.

    ``auto`` is returned unchanged, deliberately: it is the "let Cursor pick"
    sentinel, there is no evidence ``cursor-agent`` accepts ``auto[effort=…]``,
    and :func:`cursor_model_error` force-probes any bracketed model — so
    synthesizing one here would turn a working default into a live subprocess
    probe that can fail. Callers detect this case by comparing
    :func:`cursor_model_effort` of the result against what they asked for; the
    profile layer reports it as ``effort_channel: "none"`` plus a warning.
    """
    base, params = parse_cursor_model(model)
    if effort is None or not base or base.lower() in _CURSOR_AUTO_IDS:
        return compose_cursor_model(base, params)
    params["effort"] = str(effort).strip()
    return compose_cursor_model(base, params)


def _cursor_known_models(
    cli_bin: str,
    *,
    probe: AuthProber | None = None,
    timeout: float = 30.0,
    env: Mapping[str, str] | None = None,
) -> set[str]:
    """Ids from ``cursor-agent models``; empty set when it cannot be read.

    The list is **incomplete** — see :func:`cursor_model_error` — so an empty
    result and a missing id are treated the same way: not evidence of anything.

    ``env`` defaults to the scrubbed shared environment; pass an isolated one
    wherever a wave's per-worker config dirs are in play.
    """
    runner = probe if probe is not None else _default_auth_prober
    try:
        rc, stdout, _stderr = runner(
            [cli_bin, "models"],
            env=env if env is not None else subscription_env("cursor"),
            cwd=neutral_claude_cwd(),
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if rc != 0:
        return set()
    ids: set[str] = set()
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or " - " not in line:
            continue
        candidate = line.split(" - ", 1)[0].strip()
        if candidate and " " not in candidate:
            ids.add(candidate)
    return ids


def cursor_model_error(
    cli_bin: str,
    model: str,
    *,
    probe: AuthProber | None = None,
    timeout: float = 30.0,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Return why ``cursor-agent`` would reject ``model``, or ``None`` to proceed.

    A bad ``--model`` costs one dead process per job — N identical failures for
    one typo. Both checks here are token-free (``cursor-agent`` validates the
    model id before it reads the prompt, let alone calls a model), so a wave can
    be stopped with one message and zero spawns.

    Two signals, because neither alone is sound:

    - ``cursor-agent models`` lists ids, but **not all of them**: ``grok-4.5``
      is accepted by the CLI and absent from that list (verified 2026-08-11), and
      it is the very id this module's own warning recommends. Membership is
      therefore proof of validity but absence is not proof of invalidity.
    - So for an unlisted id, ask the CLI itself: an empty stdin makes it exit
      non-zero either way, but it reports ``Cannot use this model`` *before* it
      reports the missing prompt. Only that message fails the wave.

    Fails **open** on every other outcome — an unavailable, slow, or restructured
    CLI must never be the thing that blocks a working wave.

    ``env`` is the child environment for both probes. Pass an isolated one: the
    second probe's argv is the same shape a worker job runs, so against the
    operator's own ``~/.cursor`` it can rewrite ``selectedModel`` — the very
    mutation per-worker config dirs exist to prevent. It defaults to the scrubbed
    shared env, which is right for a bare read like
    :func:`worker_model_suggestions`.
    """
    probe_env = env if env is not None else subscription_env("cursor")
    base = _cursor_model_base(model)
    if not base:
        return None
    known = _cursor_known_models(cli_bin, probe=probe, timeout=timeout, env=probe_env)
    # A listed base id proves the *id* is valid, but a bracket suffix can still
    # be rejected (``gpt-5.2[effort=bogus]``). Only skip the CLI probe when the
    # argv has no parameters to validate.
    if base in known and "[" not in (model or ""):
        return None

    runner = probe if probe is not None else _default_auth_prober
    argv = [
        cli_bin, "-p", "--trust", "--mode", "ask",
        "--model", model, "--output-format", "json",
    ]
    try:
        _rc, stdout, stderr = runner(
            argv, env=probe_env, cwd=neutral_claude_cwd(), timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return None
    detail = f"{stderr or ''}\n{stdout or ''}".strip()
    if "cannot use this model" not in detail.lower():
        return None
    listed = f" Known ids: {', '.join(sorted(known))}." if known else ""
    return (
        f"cursor-agent rejected --model {model!r}: {detail.splitlines()[0][:300]}."
        f"{listed}"
    )


def _temp_root() -> Path:
    """The system temp directory, honoring the usual overrides."""
    return Path(
        os.environ.get("TEMP")
        or os.environ.get("TMP")
        or os.environ.get("TMPDIR")
        or tempfile.gettempdir()
    )


def neutral_claude_cwd() -> Path:
    """Empty temp dir so headless CLIs do not auto-load a project CLAUDE.md."""
    cwd = _temp_root() / "claude-headless-empty"
    cwd.mkdir(parents=True, exist_ok=True)
    return cwd


# ---------------------------------------------------------------------------
# Per-worker Cursor config directories
# ---------------------------------------------------------------------------
#
# ``cursor-agent`` saves ``cli-config.json`` and ``statsig-cache.json`` by
# writing a ``<name>.<pid>.<uuid>.tmp`` sibling and renaming it over the
# original. Every worker shared one directory, so concurrent renames collided --
# ``EPERM: operation not permitted, rename '…\.cursor\cli-config.json…'`` -- on
# about 3% of jobs at widths 2-5, each needing a hand re-run (2026-09-11 fabre2
# lost 1/20 in the morning and 3/20 in the afternoon). Verified in the CLI
# bundle (2026.09.10-fd3934a): both files resolve off ``CURSOR_CONFIG_DIR`` ->
# ``XDG_CONFIG_HOME/cursor`` -> ``~/.cursor``, so giving each worker slot its own
# directory removes the collision rather than working around it.
#
# Deliberately NOT relocated:
#
# - **The login.** It lives in ``%APPDATA%\Cursor\auth.json`` (``~/.cursor`` on
#   macOS), computed from the home root and never from the config dir, so a
#   worker with its own config dir stays authenticated. This is the fact the
#   whole feature rests on: had auth lived here, the preflight would pass on the
#   operator's real directory and then *every* worker would run logged out,
#   burning the 15-minute Cursor ceiling each.
# - **Chats and projects.** Those follow ``CURSOR_DATA_DIR``, a different
#   variable, and stay where the operator expects them.
#
# Slots are reused across waves so the ~800 KB statsig cache is refetched once
# per slot rather than once per wave. ``statsig-cache.json`` is never seeded:
# copying a live file the operator's interactive Cursor may be mid-rename on
# would mean handling torn reads, and one cold refetch per slot is cheaper than
# that -- so the first wave after this change looks a little slower, once.

_SLOT_ROOT_NAME = ".cursor-slots"
_CURSOR_CONFIG_DIR_VAR = "CURSOR_CONFIG_DIR"
_SLOT_ROOT_VAR = "HEADLESS_SLOT_ROOT"

# ``cursor-agent`` writes its chat state *inside* CURSOR_CONFIG_DIR, at
# ``chats/<32-hex>/<uuid>/store.db`` (plus ``-wal``, ``-shm``) -- 118 characters
# below the slot, measured. Windows caps a path at 260 unless the writing binary
# opts in via a ``longPathAware`` manifest, and node/sqlite do not for these
# writes, so ``LongPathsEnabled=1`` does not rescue it. Past that budget every
# long job dies with rc=124 and a *Cursor endpoint* reconnect message that reads
# exactly like a provider outage: 4 of 4 long jobs failed under a 261-character
# root and 2 of 2 passed at 139 -- same target, same model, same effort. Short
# jobs pass either way, which is what makes it so misleading; a 10.5 s probe
# succeeded while every real job was failing. 120 leaves margin over the 118.
_SLOT_PATH_BUDGET = 120
_SLOT_CHILD_OVERHEAD = 118

# How many indices a worker may walk past before it gives up on isolation.
_SLOT_SEED_ATTEMPTS = 8

_slot_lock = threading.Lock()
_slot_free: list[int] = []
_slot_high = 0
# Indices whose directory could not be seeded. Never returned to the free list:
# a scheduled task running under another identity leaves ``slot-0..N`` owned by
# someone this user can neither read nor delete, and recycling such an index
# would make every job retry it forever.
_slot_poisoned: set[int] = set()
# Per-wave isolation tally, so a wave that quietly lost isolation says so.
_slot_stats = {"seeded": 0, "unseeded": 0}
_slot_first_error: str | None = None
# The slot the calling thread currently holds. A thread-local rather than an
# argument because the runner seam is ``(cmd, *, input_text, cwd)`` and a job's
# env must not become part of it -- every test stub implements that signature.
_slot_current = threading.local()


def _slot_root() -> Path:
    """Where per-worker Cursor config slots live.

    Under the user's home rather than ``_temp_root()``, which buys two distinct
    properties the temp root could not (2026-09-16 field findings, issues 2-3):

    - **Per-user by construction.** The slot root used to sit under the *shared*
      temp root, so a scheduled task running under another identity created
      ``slot-0..N`` first, with an ACL the interactive user could neither read
      nor delete. Every interactive wave that day then allocated slot-0, failed
      to seed it, and silently ran unisolated.
    - **Short.** See :data:`_SLOT_PATH_BUDGET`. ``%TEMP%`` is already deep on
      Windows and an agent session can point it deeper still -- the root that
      broke this was 261 characters.

    ``HEADLESS_SLOT_ROOT`` overrides it, and is validated like any other root.
    The override is resolved to an absolute path before use: a relative value
    would otherwise pass the character budget as a handful of characters and
    then expand against cwd into the deep path the budget exists to refuse.

    The trade is that slots are no longer swept by the OS. They hold one small
    JSON each and the free list bounds their count to the widest wave this
    process has run, so the cost is fixed and small -- and stable paths are
    exactly what let each slot's statsig cache survive between waves.
    """
    override = (os.environ.get(_SLOT_ROOT_VAR) or "").strip()
    root = Path(override) if override else Path.home() / _SLOT_ROOT_NAME
    return root.expanduser().resolve()


def _slot_path_error(root: Path) -> str | None:
    """Why this slot root would kill every long Cursor job, or ``None``.

    Checked before anything spawns rather than discovered from the failures: the
    symptom names Cursor's own endpoint, so it reads as a provider outage and
    costs hours to tell apart from one.
    """
    length = len(str(root))
    if length <= _SLOT_PATH_BUDGET:
        return None
    return (
        f"slot root is {length} characters, over the {_SLOT_PATH_BUDGET}-character "
        f"budget ({root}). cursor-agent writes chats/<id>/<uuid>/store.db-wal about "
        f"{_SLOT_CHILD_OVERHEAD} characters below it, which would cross the "
        f"260-character Windows path limit and kill every long job with rc=124 and a "
        f"Cursor reconnect message that looks like a provider outage. Set "
        f"{_SLOT_ROOT_VAR} to a shorter path."
    )


def _reset_slot_stats() -> None:
    """Clear the per-wave isolation tally. Called once, just before the pool."""
    global _slot_first_error
    with _slot_lock:
        _slot_stats["seeded"] = 0
        _slot_stats["unseeded"] = 0
        _slot_first_error = None


def _note_slot_error(path: Path, exc: OSError) -> None:
    """Record why a slot could not be seeded, and warn once per wave.

    Failing open is right; failing open *quietly* is what turned a one-line
    problem into a two-hour investigation, because a wave with isolation working
    and one with it disabled were byte-identical from the outside.

    ``path`` is whichever file actually failed -- the destination slot, or the
    operator's source ``cli-config.json``. Naming the slot for a missing *source*
    sent the reader looking at a directory that was never the problem.
    """
    global _slot_first_error
    detail = f"{path}: [{exc.errno}] {exc.strerror or exc}"
    with _slot_lock:
        first = _slot_first_error is None
        if first:
            _slot_first_error = detail
    if first:
        logger.warning("cursor slot could not be seeded (%s); failing open", detail)


def _count_slot(seeded: bool) -> None:
    with _slot_lock:
        _slot_stats["seeded" if seeded else "unseeded"] += 1


def _slot_tally() -> str | None:
    """``"n/N"`` isolated workers for the usage rollup, or ``None`` if no slots."""
    with _slot_lock:
        seeded, unseeded = _slot_stats["seeded"], _slot_stats["unseeded"]
    total = seeded + unseeded
    return f"{seeded}/{total}" if total else None


def _take_slot() -> int:
    global _slot_high
    with _slot_lock:
        if _slot_free:
            return _slot_free.pop()
        index, _slot_high = _slot_high, _slot_high + 1
        return index


def _retire_slot(index: int) -> None:
    """Retire a poisoned index rather than freeing it (see ``_slot_poisoned``)."""
    with _slot_lock:
        _slot_poisoned.add(index)


def _release_slot(index: int) -> None:
    with _slot_lock:
        _slot_free.append(index)


def _cursor_config_dir(env: Mapping[str, str]) -> Path:
    """The config directory ``cursor-agent`` would use under ``env``.

    Mirrors the CLI's own precedence (verified 2026.09.10-fd3934a):
    ``CURSOR_CONFIG_DIR`` -> ``XDG_CONFIG_HOME/cursor`` -> ``~/.cursor``. Read
    out of ``env`` rather than ``os.environ`` so an operator who deliberately
    relocated their Cursor config is seeded *from* it instead of having it
    silently ignored.

    Note this is only the **seed source**. :data:`CURSOR_CLI_CONFIG` still spells
    the plain ``~/.cursor`` path for :func:`cursor_default_model`, which four
    test modules monkeypatch as a module constant.
    """
    explicit = (env.get(_CURSOR_CONFIG_DIR_VAR) or "").strip()
    if explicit:
        return Path(explicit)
    xdg = (env.get("XDG_CONFIG_HOME") or "").strip()
    if xdg:
        return Path(xdg) / "cursor"
    return Path.home() / ".cursor"


def _seed_slot_config(slot_dir: Path, source_dir: Path) -> bool:
    """Copy ``cli-config.json`` into ``slot_dir``; True when the slot is usable.

    That one file and nothing else -- never a recursive copy: ``~/.cursor``
    carries ``extensions/`` and ``chats/``, which run to hundreds of megabytes.

    Returns **False on any failure**, and the caller then leaves
    ``CURSOR_CONFIG_DIR`` unset so the job runs exactly as it did before this
    feature existed. Falling back to an *empty* directory would not be a cold
    cache: ``cli-config.json`` carries ``permissions.allow`` / ``permissions.deny``
    and ``privacyCache.{ghostMode,privacyMode}``, so an unseeded slot would
    quietly change the worker's permissions and data-retention posture.

    Re-seeded whenever the operator's own file is newer -- they changed their
    model picker -- since one ``stat`` per job is free beside a subprocess.
    """
    source = source_dir / "cli-config.json"
    dest = slot_dir / "cli-config.json"
    try:
        src_stat = source.stat()
    except OSError as exc:
        _note_slot_error(source, exc)
        return False
    staged = slot_dir / f"cli-config.json.{os.getpid()}.tmp"
    try:
        # 0o700: cli-config.json carries authInfo.email and userId. Under the
        # home root that is belt-and-braces rather than load-bearing, but this
        # module already withholds that email from probe output and a
        # world-readable copy would widen it straight back.
        slot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            if dest.stat().st_mtime >= src_stat.st_mtime:
                return True
        except OSError:
            pass  # not seeded yet
        # Write a sibling and rename over the target -- the same shape the CLI
        # itself uses, so two processes seeding one slot cannot tear the file.
        shutil.copy2(source, staged)
        os.replace(staged, dest)
        return True
    except OSError as exc:
        _note_slot_error(slot_dir, exc)
        return False
    finally:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def _cursor_slot(source_dir: Path) -> Iterator[None]:
    """Hold one per-worker config slot for the duration of a spawn.

    A free-list keyed on availability alone, rather than on the job index or a
    counter: ``index % concurrency`` is wrong (job N can start while job 0 is
    still running) and a monotonic counter would leak a directory per wave,
    since every wave builds a fresh pool with fresh threads. The free-list
    bounds the directories to the widest wave this process has run and keeps
    their paths stable, which is what lets the statsig cache survive between
    waves.

    On a seed failure the loop **advances** to the next index rather than
    yielding an unisolated slot, and retires the bad one. Scoping the root per
    user (:func:`_slot_root`) fixes the collision that actually reproduced here;
    advancing self-heals whatever it does not catch -- a stale elevated run, a
    changed ACL -- at a cost of one leaked directory per poisoned index, paid
    once per process rather than once per job.

    A missing **source**, though, is not a poisoned slot, and advancing cannot
    help: the file is absent at the same path on every attempt, so the loop would
    burn :data:`_SLOT_SEED_ATTEMPTS` indices per spawn and blame a destination
    that was never at fault. Checked once, up front, and failed open.
    """
    index: int | None = None
    slot_dir: Path | None = None
    source = source_dir / "cli-config.json"
    try:
        source.stat()
    except OSError as exc:
        _note_slot_error(source, exc)
    else:
        for _ in range(_SLOT_SEED_ATTEMPTS):
            candidate = _take_slot()
            candidate_dir = _slot_root() / f"slot-{candidate}"
            if _seed_slot_config(candidate_dir, source_dir):
                index, slot_dir = candidate, candidate_dir
                break
            _retire_slot(candidate)
    try:
        _slot_current.dir = slot_dir
        _count_slot(slot_dir is not None)
        yield
    finally:
        _slot_current.dir = None
        if index is not None:
            _release_slot(index)


def _slot_env(cli: str, env: Mapping[str, str]) -> Mapping[str, str]:
    """``env`` pointed at the calling thread's config slot, if it holds one.

    Derived from the scrubbed wave env, never from ``os.environ``: rebuilding
    the child environment here would reopen the metered-billing hole the scrub
    exists to close.
    """
    slot = getattr(_slot_current, "dir", None)
    if cli != "cursor" or slot is None:
        return env
    return {**env, _CURSOR_CONFIG_DIR_VAR: str(slot)}


def _close_streams(proc: subprocess.Popen) -> None:
    """Drop the pipes without waiting on whoever still holds the other end."""
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Kill the worker *and its children*, not just the wrapper.

    ``cursor-agent`` resolves to ``cursor-agent.CMD``, which spawns ``node``.
    Killing only the wrapper leaves node alive holding the inherited stdout and
    stderr handles -- both halves of the 2026-09-16 findings: the drain then
    blocks for two to three times the job budget, and the worker sits orphaned in
    its reconnect loop with nothing left to collect its output. By then its
    parent chain reads ``powershell.exe:<pid> <- (gone)``, so a later sweep keyed
    on a live parent does not find it. It has to die with the wave.
    """
    try:
        if os.name == "nt":
            # ``/T`` walks the tree from this pid, which is still intact here:
            # the orphaning only happens once the parent is gone.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=_TREE_KILL_TIMEOUT_S,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()  # last resort, and the direct child only
    except OSError:
        pass


# Every live child this process has spawned -- workers and preflight probes alike.
#
# ``KeyboardInterrupt`` is delivered to the **main** thread only, so the
# ``except BaseException`` guarding a spawn can never see the operator's Ctrl-C:
# the ``Popen`` it guards lives in a pool thread. Without a registry the
# interrupting thread holds no handle on the processes it needs to kill, and
# ``ThreadPoolExecutor.__exit__`` then waits out every in-flight job -- up to
# ``concurrency`` x the 900 s Cursor ceiling, spent on a wave nobody is left to
# collect.
_live_procs: set[subprocess.Popen] = set()
_live_procs_lock = threading.Lock()


@contextmanager
def _tracked(proc: subprocess.Popen) -> Iterator[subprocess.Popen]:
    """Register ``proc`` as killable by :func:`_kill_live_processes`."""
    with _live_procs_lock:
        _live_procs.add(proc)
    try:
        yield proc
    finally:
        with _live_procs_lock:
            _live_procs.discard(proc)


def _kill_live_processes() -> int:
    """Tree-kill every spawn still running; returns how many there were.

    Called from the interrupting thread, which is never the thread blocked in
    ``communicate()``. Killing the tree is also what makes the pool's own
    ``shutdown(wait=True)`` cheap: each worker's drain returns at once instead of
    holding the wave open for the rest of its budget.
    """
    with _live_procs_lock:
        procs = list(_live_procs)
    for proc in procs:
        _kill_process_tree(proc)
    return len(procs)


def _communicate_bounded(
    proc: subprocess.Popen, input_text: str | None, timeout: float | None
) -> tuple[int, str, str, bool]:
    """``proc.communicate`` with a post-kill drain that cannot outlive the kill.

    Shared by the worker launcher and the preflight prober, because both hit the
    same stdlib defect: ``subprocess.run`` kills only the direct child and then,
    on Windows, calls ``communicate()`` a second time with **no timeout** (see
    the ``_mswindows`` branch of CPython's ``subprocess.run``). ``cursor-agent``
    resolves to ``cursor-agent.CMD``, which spawns ``node``, so killing the
    wrapper leaves node holding the inherited handles and that second drain
    blocks until node exits on its own -- observed wall times of 1253, 1293 and
    1977 s against a 900 s ceiling.

    Returns ``(rc, stdout, stderr, timed_out)``. The caller decides what a
    timeout means: a 124 row for a worker, a raised ``TimeoutExpired`` for a
    probe whose callers already branch on that.
    """
    try:
        stdout, stderr = proc.communicate(input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        out = err = ""
        try:
            out, err = proc.communicate(timeout=_DRAIN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            # Something the tree kill missed still holds the pipes. Abandon them
            # rather than block the wave: this bound is the whole difference
            # between a 900 s ceiling and a 1977 s one.
            _close_streams(proc)
        return 124, out or "", err or "", True
    except BaseException:
        # An interrupt raised *in this thread* -- never the operator's Ctrl-C,
        # which is why ``_live_procs`` exists -- must not leave the child behind.
        _kill_process_tree(proc)
        raise
    return proc.returncode, stdout or "", stderr or "", False


def default_claude_runner(
    cmd: list[str],
    *,
    input_text: str,
    cwd: Path,
    timeout: float | None = None,
    cli: str = "claude",
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a headless CLI with the prompt on stdin; return (rc, stdout, stderr).

    ``env`` defaults to ``subscription_env(cli)`` rather than to inheritance.
    Callers that pass an explicit ``env=`` own that mapping unchanged.

    Deliberately ``Popen`` rather than ``subprocess.run``: ``run`` enforces a
    timeout that this wave could not rely on -- see :func:`_communicate_bounded`,
    which owns that reasoning and is shared with the preflight prober. The
    ``Popen`` is registered in :data:`_live_procs` for the whole call, so an
    operator's Ctrl-C on the main thread can kill this worker even though the
    interrupt never reaches the thread blocked here.
    """
    child_env = dict(env) if env is not None else subscription_env(cli)
    popen_kwargs: dict[str, Any] = {}
    if os.name != "nt":
        # Its own process group, so the whole tree can be signalled at once.
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
        **popen_kwargs,
    )
    with _tracked(proc):
        rc, stdout, stderr, timed_out = _communicate_bounded(proc, input_text, timeout)
    if timed_out:
        detail = f"timeout after {timeout:g}s"
        return 124, stdout, (stderr.strip() or detail)
    return rc, stdout, stderr


def _normalize_cli(cli: str) -> str:
    name = (cli or "claude").strip().lower()
    if name not in _SUPPORTED_CLIS:
        raise ValueError(
            f"unsupported headless cli {cli!r}; expected one of "
            f"{sorted(_SUPPORTED_CLIS)}"
        )
    return name


def _default_bin(cli: str) -> str:
    return _CLI_DEFAULT_BINS[cli]


def cli_binary(cli: str) -> str | None:
    """The launcher executable a family runs, before PATH resolution.

    Public because two callers outside the wave path need the name-to-family
    mapping and must not reach into ``_CLI_DEFAULT_BINS``:
    :mod:`src.harness.profile` (to tell a bad guess from a good one) and the
    dashboard (to say which backends this machine can offer at all).
    """
    return _CLI_DEFAULT_BINS.get((cli or "").strip().lower())


def cli_binary_present(cli: str) -> bool:
    """True when this family's launcher resolves on PATH.

    ``shutil.which``, not ``os.path.exists``: on Windows the npm shim is
    ``claude.cmd`` and only a PATHEXT-aware lookup finds it — the same
    resolution :func:`run_headless_wave` does before it spawns.
    """
    name = cli_binary(cli)
    return bool(name) and shutil.which(name) is not None


def _bin_missing_error(cli: str, cli_bin: str) -> str:
    if cli == "cursor":
        return (
            f"cursor-agent not found: {cli_bin!r} — install the Cursor CLI and "
            "run `cursor-agent login`"
        )
    return f"claude not found: {cli_bin!r} (not on PATH / PATHEXT)"


def _error_result(message: str, cwd: Path | str | None = None) -> dict[str, Any]:
    """Fail-fast wave result: a top-level error and zero jobs run.

    Every caller of ``run_headless_wave`` branches on
    ``"error" in result and not result["wrote"] and not result["failed"]``.
    """
    return {
        "error": message,
        "wrote": [],
        "failed": [],
        "cwd": str(cwd) if cwd is not None else None,
        "counts": {"wrote": 0, "failed": 0, "todo": 0},
    }


# ---------------------------------------------------------------------------
# Subscription enforcement: auth preflight
# ---------------------------------------------------------------------------

AuthProber = Callable[..., tuple[int, str, str]]

# Argv that makes a CLI report its credential state. ``None`` is still honored --
# a family registered that way gets the env scrub but no preflight -- but no
# family uses it any more: both entries below are verified commands.
_AUTH_PROBE_ARGV: dict[str, tuple[str, ...] | None] = {
    "claude": ("auth", "status", "--json"),
    # Verified 2026-08-10 on cursor-agent 2026.08.04-aaa8809:
    # {"status":"authenticated","isAuthenticated":true,"hasAccessToken":true,
    #  "hasRefreshToken":true,"userInfo":{…}}. See subscription_auth_error for
    # why this probe answers a different question than Claude's.
    "cursor": ("status", "--format", "json"),
}
_AUTH_PROBE_TIMEOUT_S = 30.0


def _default_auth_prober(
    argv: list[str], *, env: Mapping[str, str], cwd: Path | str, timeout: float
) -> tuple[int, str, str]:
    """Run one preflight probe under the same bounded-drain rules as a worker.

    Not ``subprocess.run``, for the reason :func:`_communicate_bounded` gives.
    Three probes run before a wave -- ``status``, ``models``, and the empty-stdin
    ``--model`` check -- and a hung one used to block the wave with no bound at
    all: the ``TimeoutExpired`` handler in :func:`subscription_auth_error` can
    never be reached while ``run`` is stuck inside that unbounded drain, so the
    30 s ceiling those callers document was not real on Windows.

    Raises ``TimeoutExpired`` rather than returning 124, so every caller keeps
    the semantics it already documents: :func:`subscription_auth_error` fails
    closed with a "timed out" message, :func:`cursor_model_error` fails open.

    stdin is an explicitly closed pipe rather than the inherited handle, which is
    what :func:`cursor_model_error` means by "an empty stdin": a probe can no
    longer block reading from whatever the parent happened to be attached to.
    """
    popen_kwargs: dict[str, Any] = {}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(env),
        **popen_kwargs,
    )
    with _tracked(proc):
        rc, stdout, stderr, timed_out = _communicate_bounded(proc, "", timeout)
    if timed_out:
        raise subprocess.TimeoutExpired(
            cmd=argv, timeout=timeout, output=stdout, stderr=stderr
        )
    return rc, stdout, stderr


def _cursor_auth_error(cli_bin: str, obj: dict[str, Any]) -> str | None:
    """Verdict for a parsed ``cursor-agent status --format json`` payload.

    Passes on ``isAuthenticated is True`` or ``status == "authenticated"`` and
    fails closed on anything else. The payload also carries a ``userInfo`` block
    with the account's email, id and real name; **only the two decision keys are
    ever echoed**, mirroring how the Claude branch withholds email/orgId/orgName.
    """
    if obj.get("isAuthenticated") is True:
        return None
    if str(obj.get("status") or "").strip().lower() == "authenticated":
        return None
    safe = {k: obj.get(k) for k in ("status", "isAuthenticated") if k in obj}
    detail = json.dumps(safe, sort_keys=True)[:300] if safe else "no status/isAuthenticated key"
    return (
        f"{cli_bin} is not logged in ({detail}) — run `{cli_bin} login`, "
        f"or use `--backend api` if metered spend is what you want."
    )


def subscription_auth_error(
    cli: str,
    cli_bin: str,
    env: Mapping[str, str],
    *,
    cwd: Path | str | None = None,
    prober: AuthProber | None = None,
    timeout: float = _AUTH_PROBE_TIMEOUT_S,
) -> str | None:
    """Return why this CLI would *not* bill the subscription, or None if it would.

    **The two families answer different questions, deliberately.**

    - ``claude auth status --json`` is a **routing** probe: it reports the
      credential path that would be used, identically for a valid and a bogus
      key. It answers "where does the bill go", never "will this call succeed" —
      which is what matters, because the Claude path has a metered twin.
    - ``cursor-agent status --format json`` is a **liveness** probe: it reports
      whether a login session exists. There is no metered Cursor code path in
      this repo to route to, so the billing-safety half is already carried by the
      ``CURSOR_API_KEY`` scrub; what this adds is failing before N jobs each
      discover a logged-out CLI on their own.

    Must run with the same scrubbed ``env`` *and* the same ``cwd`` as the workers.
    The CLI reads project-local settings, so probing from the repo root can report
    a different auth path than a worker in the neutral cwd actually gets.

    Fails closed for both. An unusable probe (missing subcommand, non-zero exit,
    output shape we do not recognise) blocks the wave -- unverifiable means
    unsafe, and ``--backend api`` is the supported way to spend money.
    """
    if cli not in _AUTH_PROBE_ARGV:
        return (
            f"no auth probe registered for cli {cli!r}; "
            f"register it in _AUTH_PROBE_ARGV (or use `--backend api`)"
        )
    argv_tail = _AUTH_PROBE_ARGV[cli]
    if argv_tail is None:
        return None  # family explicitly has no probe; the scrub is its only guarantee

    argv = [cli_bin, *argv_tail]
    label = " ".join(argv)
    probe = prober if prober is not None else _default_auth_prober
    probe_cwd = cwd if cwd is not None else neutral_claude_cwd()
    try:
        rc, stdout, stderr = probe(argv, env=env, cwd=probe_cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"`{label}` timed out after {timeout:g}s"
    except OSError as exc:
        return f"could not run `{label}`: {exc}"

    if rc != 0:
        detail = (stderr or stdout or "").strip()[:300] or f"exit {rc}"
        return (
            f"`{label}` failed ({detail}). Upgrade the CLI "
            f"so the subscription preflight can run, or use `--backend api`."
        )

    try:
        obj = json.loads((stdout or "").strip())
    except json.JSONDecodeError:
        return (
            f"could not parse `{label}` output: "
            f"{(stdout or '').strip()[:200]!r}"
        )
    if not isinstance(obj, dict):
        return f"unexpected `{label}` shape: {type(obj).__name__}"

    if cli == "cursor":
        return _cursor_auth_error(cli_bin, obj)

    if obj.get("loggedIn") is not True:
        return f"{cli_bin} is not logged in — run `{cli_bin}` and `/login`"

    # Positive detection of metered routing. Unconditional; nothing bypasses this.
    api_key_source = obj.get("apiKeySource")
    if api_key_source:
        return (
            f"{cli_bin} would bill metered API credit via {api_key_source}. "
            f"Headless fan-out is subscription-only — unset it, or use "
            f"`--backend api` if metered spend is what you want."
        )
    provider = obj.get("apiProvider")
    if provider is not None and provider != "firstParty":
        return (
            f"{cli_bin} is routed to the third-party provider {provider!r}, which "
            f"bills that provider rather than your subscription."
        )

    # A Pro/Max session reports subscriptionType directly.
    subscription = obj.get("subscriptionType")
    if isinstance(subscription, str) and subscription.strip():
        return None
    # `claude setup-token` auth reports authMethod=oauth_token with no
    # subscriptionType. ANTHROPIC_AUTH_TOKEN produces a byte-identical response,
    # so the probe alone cannot tell them apart -- the scrubbed env is the
    # tiebreaker: with ANTHROPIC_AUTH_TOKEN removed, only the setup-token
    # (subscription) path can still report oauth_token.
    # Case-insensitive: subscription_env preserves original key spelling, and
    # Windows env lookups are case-insensitive — an exact `"…TOKEN" in env`
    # would fail-close a valid setup-token login whose key survived as
    # ``Claude_Code_Oauth_Token``.
    if obj.get("authMethod") == "oauth_token" and any(
        key.upper() == "CLAUDE_CODE_OAUTH_TOKEN" for key in env
    ):
        return None

    # Diagnostic keys only — never dump email/orgId/orgName from auth status.
    safe = {
        k: obj.get(k)
        for k in (
            "loggedIn",
            "authMethod",
            "apiProvider",
            "apiKeySource",
            "subscriptionType",
        )
        if k in obj
    }
    return (
        f"could not confirm a subscription login for {cli_bin}: "
        f"{json.dumps(safe, sort_keys=True)[:300]}"
    )


def preflight_error(
    cli: str, cli_bin: str | None = None, *, model: str | None = None
) -> str | None:
    """Why a wave on ``cli`` would refuse to start, or ``None`` if it would run.

    The same three gates :func:`run_headless_wave` applies before its first job --
    binary resolution, the subscription probe, and (on Cursor) the ``--model``
    check -- hoisted so a caller can fail closed *before* it prepares anything.
    ``fanout(estimate=True)`` returns before the launcher is reached, so without
    this an estimate on a logged-out or uninstalled CLI reads as a green light
    and the failure only lands after the operator has consented to a run.

    ``model`` is the *resolved* worker model (``prof.worker_model``, brackets and
    all). Optional and keyword-only so the binary/auth gates stay callable before
    a model has been resolved — but pass it whenever one is known: without it a
    bogus Cursor id survives a green estimate and only kills the wave later, from
    inside the job, after the destructive ``prepare`` has already run.

    Returns the CLI's own message verbatim: it already names the fix (``claude``
    + ``/login``, ``cursor-agent login``, install the Cursor CLI), and
    paraphrasing it is how a caller ends up telling someone to run the wrong
    command.
    """
    try:
        cli_name = _normalize_cli(cli)
    except ValueError as exc:
        return str(exc)
    name = cli_bin or _default_bin(cli_name)
    resolved = shutil.which(name)
    if not resolved:
        return _bin_missing_error(cli_name, name)
    auth_error = subscription_auth_error(
        cli_name,
        resolved,
        subscription_env(cli_name),
        cwd=neutral_claude_cwd(),
    )
    if auth_error:
        return auth_error
    # Same gate the wave applies, hoisted for the same reason as the others: a
    # slot root over budget kills every long job with a message that reads as a
    # provider outage, and the dashboard should catch that before it spends.
    if cli_name == "cursor":
        path_error = _slot_path_error(_slot_root())
        if path_error is not None:
            return path_error
    # Third gate, same order `run_headless_wave` applies it in (after auth, so a
    # logged-out CLI is reported as logged out rather than as a model problem).
    # Token-free and fails open in every direction, so hoisting it can only turn
    # "the job died after prepare" into "the estimate never went green".
    if cli_name == "cursor" and model:
        # Through an isolated config dir, for the reason the wave gives at its
        # own model gate: this probe spawns a worker-shaped `-p --model` argv,
        # and the dashboard calls it against the operator's live Cursor config.
        probe_env = subscription_env(cli_name)
        with _cursor_slot(_cursor_config_dir(probe_env)):
            return cursor_model_error(
                resolved, model, env=dict(_slot_env(cli_name, probe_env))
            )
    return None


def worker_model_suggestions(cli: str, *, timeout: float = 10.0) -> list[str]:
    """Worker-model ids worth *offering* for a family — never an exhaustive list.

    Claude returns its subscription aliases. Cursor asks the CLI (its list is
    known-incomplete, see :func:`cursor_model_error`) and unions in whatever the
    operator already selected in Cursor's own picker, so the model a bare wave
    would actually run is always among the suggestions.

    Fails open in every direction: a missing or unreadable CLI yields whatever is
    known without it. Callers must treat the result as a suggestion list (a
    datalist, not a select) — Cursor models take the bracket form
    ``grok-4.5[effort=medium]``, which no fixed list can enumerate.
    """
    if (cli or "").strip().lower() != "cursor":
        return sorted(_CLAUDE_WORKER_ALIASES)
    ids: set[str] = set()
    resolved = shutil.which(_CLI_DEFAULT_BINS["cursor"])
    if resolved:
        ids |= _cursor_known_models(resolved, timeout=timeout)
    selected = cursor_default_model()
    if selected:
        ids.add(selected)
    return sorted(ids)


def _build_cmd(
    cli: str,
    cli_bin: str,
    model: str,
    spf: Optional[str],
    *,
    extra_flags: Sequence[str] = (),
) -> list[str]:
    """Build argv for one headless job (prompt still goes on stdin).

    ``extra_flags`` is the Claude-profile experiment seam: argv that trims what
    the child loads into its system prompt (``--strict-mcp-config``,
    ``--setting-sources ""``, ``--safe-mode`` …). It is recorded per job in the
    usage log, so which flags a wave ran under is a property of the measurement
    rather than something to remember. Claude-only on purpose — a Cursor wave
    would reject Claude argv, and silently ignoring it beats failing the wave.
    """
    if cli == "cursor":
        # Ask mode + no --force: answer-only, no applied file edits.
        # --trust: skip workspace-trust prompts in the empty neutral cwd.
        # No --system-prompt-file / --tools (Cursor has neither); callers fold
        # any preamble into stdin via _fold_system_prompt.
        # --output-format json: verified 2026-08-10 on cursor-agent
        # 2026.08.04-aaa8809 to emit {"type":"result","subtype","is_error",
        # "result","duration_ms","session_id","usage":{"inputTokens",
        # "outputTokens","cacheReadTokens","cacheWriteTokens"}} — the same
        # envelope _extract_result already unwraps. Before this, Cursor was the
        # only family whose per-process overhead (~17.2k tokens, ~4.4x Claude's)
        # nothing could report.
        return [
            cli_bin,
            "-p",
            "--trust",
            "--mode",
            "ask",
            "--model",
            model,
            "--output-format",
            "json",
        ]

    # claude profile. ``--output-format json`` costs nothing and is the only way
    # the parent can see what a job was billed: with ``text`` the CLI computes
    # the whole ``usage`` block and then throws it away. ``_extract_result``
    # unwraps the envelope back to the same prose ``text`` would have produced,
    # and falls back to raw stdout if a build ever ignores the flag.
    cmd = [
        cli_bin,
        "-p",
        "--model",
        model,
        "--tools",
        "",
        "--output-format",
        "json",
    ]
    if spf:
        cmd[2:2] = ["--system-prompt-file", str(Path(spf).resolve())]
    cmd.extend(str(flag) for flag in extra_flags)
    return cmd


def _fold_system_prompt(
    cli: str, input_text: str, spf: Optional[str]
) -> tuple[Optional[str], str]:
    """Return (spf_for_cmd, stdin_text).

    Claude keeps the cache split (``spf`` flag + body on stdin). Cursor has no
    ``--system-prompt-file``, so fold the preamble into stdin and drop the flag.
    """
    if not spf:
        return None, input_text
    if cli != "cursor":
        return spf, input_text
    preamble = Path(spf).read_text(encoding="utf-8")
    if preamble and not preamble.endswith("\n") and input_text:
        preamble = preamble + "\n"
    return None, preamble + input_text


def _envelope_error(cli: str, obj: dict[str, Any]) -> str:
    """Build a short failure detail from a CLI result envelope."""
    result = obj.get("result")
    if isinstance(result, str) and result.strip():
        return result.strip()[:500]
    subtype = obj.get("subtype")
    if isinstance(subtype, str) and subtype.strip():
        return f"{cli} result envelope error (subtype={subtype!r})"
    return f"{cli} result envelope reported is_error"


def _extract_result(
    cli: str, stdout: str, *, model: str | None = None
) -> tuple[str, dict[str, Any] | None]:
    """Normalize CLI stdout to ``(draft_text, usage)``.

    A terminal ``{"type": "result", …}`` envelope is unwrapped to its string
    ``result`` and its ``usage`` block is kept. Error envelopes (``is_error`` /
    ``subtype=error``) and non-string ``result`` values raise ``ValueError`` so
    the job is recorded as failed instead of writing a poison draft (Python
    ``repr`` / nested JSON).

    **Anything that is not an envelope passes through as prose with no usage.**
    That is what lets a stubbed test runner, or a CLI build of either family that
    ignores ``--output-format json``, keep working exactly as before — the
    telemetry degrades, the wave does not. A judge verdict is itself JSON but has
    no ``type: "result"`` key, so it is never mistaken for an envelope.
    """
    text = (stdout or "").strip()
    if not text.startswith("{"):
        return text, None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text, None
    if not (isinstance(obj, dict) and obj.get("type") == "result" and "result" in obj):
        return text, None
    if obj.get("is_error") is True or obj.get("subtype") == "error":
        raise ValueError(_envelope_error(cli, obj))
    result = obj["result"]
    if not isinstance(result, str):
        raise ValueError(
            f"{cli} result envelope has non-string result "
            f"(type={type(result).__name__}); expected prose/JSON text"
        )
    return result.strip(), usage_from_envelope(obj, model=model)


def _extract_output(cli: str, stdout: str) -> str:
    """``_extract_result`` without the usage half (kept for tests)."""
    return _extract_result(cli, stdout)[0]


def _usage_from_stdout(stdout: str, *, model: str | None = None) -> dict[str, Any] | None:
    """The usage block from a job's stdout, or ``None``. Never raises.

    A job that failed still consumed tokens, and ``--output-format json`` reports
    them on the error envelope exactly as on a successful one. Reading them here
    is what stops a wave whose jobs died mid-flight from reporting no spend at
    all — the failure case is precisely when the operator wants the number.
    """
    text = (stdout or "").strip()
    if not text.startswith("{"):
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return usage_from_envelope(obj, model=model)


def _failure_detail(cli: str, stdout: str) -> str:
    """The human-readable cause from a non-zero job's stdout.

    ``--output-format json`` wraps the reason a job failed ("Credit balance is
    too low") inside a result envelope. Reporting the envelope verbatim would
    bury the one line the operator needs in a blob of session metadata, so pull
    the message out; non-envelope stdout is returned unchanged.
    """
    text = (stdout or "").strip()
    if not text.startswith("{"):
        return text
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(obj, dict) and obj.get("type") == "result":
        return _envelope_error(cli, obj)
    return text


def run_headless_wave(
    jobs: list[dict[str, Any]],
    *,
    model: str,
    concurrency: int,
    cli: str = "claude",
    cli_bin: Optional[str] = None,
    claude_bin: Optional[str] = None,
    runner: Optional[Runner] = None,
    prober: Optional[AuthProber] = None,
    usage_log: Path | str | None = None,
    extra_flags: Sequence[str] = (),
    effort: str | None = None,
    warm_first: bool = True,
    cache: str = "auto",
    on_job_done: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run one headless CLI wave for the given jobs.

    Each ``job`` is ``{id, input_text, output_path, system_prompt_file?}``.
    Returns ``{wrote, failed, cwd, counts}``, plus ``usage`` when the CLI
    reported any (see :mod:`src.harness.usage`). When the real runner is used and
    the binary is missing from PATH, or the CLI is not on a subscription login,
    returns a top-level ``error`` with empty lists (fail-fast; no per-job wave).

    Jobs run in a **rolling pool** ``concurrency`` wide: a free slot takes the
    next job the moment it frees, rather than the whole wave front waiting on
    the slowest job of a fixed group (31% of worker time, measured across 230
    rebuilt Cursor judge groups). ``wrote`` and ``failed`` are consequently in
    completion order; no caller pins that, they match by id. On Cursor each
    concurrent worker additionally gets its **own** ``CURSOR_CONFIG_DIR``, so
    they can no longer race ``cli-config.json`` — see ``_cursor_slot``.

    ``claude_bin`` is a back-compat alias for ``cli_bin`` and is only valid when
    ``cli`` is ``claude`` (mismatch returns a top-level ``error``).

    When the real runner is used, the child env is scrubbed via
    ``subscription_env``. A custom ``runner`` never receives that scrubbed env
    (tests pass their own). The auth preflight — and, on Cursor, the token-free
    ``--model`` validation — runs when ``prober`` is given, or when the real
    runner is in use; a stub ``runner`` with no ``prober`` skips both, so unit
    tests never spawn anything.

    ``usage_log`` (a JSONL path) receives one row per job — the detail stays on
    disk rather than in the orchestrator's context. ``extra_flags`` is appended
    to the Claude argv and recorded in each row. ``effort`` is a telemetry label
    only (the flag is already composed into ``extra_flags`` by
    :func:`~src.harness.state.resolve_headless_argv`) and applies to the Claude
    profile; on Cursor it is **ignored**, and the level recorded in each row is
    read back from the model's own ``[effort=…]`` bracket instead — the only
    effort channel that CLI has. ``warm_first`` runs job 1 alone so the rest read
    the shared prefix from cache instead of each re-creating it.

    ``cache`` (``auto`` | ``5m`` | ``1h`` | ``off``) selects the Claude
    prompt-cache TTL; ``auto`` resolves from job shapes and prior wall times.
    **Cursor has no controllable cache** — not "no cache": its server-side prefix
    caching fires opportunistically (``cacheReadTokens`` of 0 / 256 / 1664 / 7680
    across identical-prefix probe calls on Grok 4.5), and what it reports varies
    by model: GPT-5.6 Terra bills its whole prefix as ``cacheWriteTokens`` on
    every job and never reads, while Claude Sonnet 5 writes once and reads back
    (2026-09-14 probe). Either way the client cannot write, pin, or price an
    entry. Cursor waves therefore record
    ``cache=None`` and keep ``warm_first``: with ``cache_read`` now logged beside
    ``warm``, two waves of ``usage.jsonl`` settle empirically whether serializing
    job 1 raises cache reads. When the resolved Claude mode is ``off``, warm-up is
    forced off — nothing to warm.

    ``on_job_done`` is called from the collecting thread as each job lands, with
    ``{"id", "ok", "error", "done", "total"}`` — the seam a UI needs, because this
    function returns only when the *whole* wave ends and a 16-job Cursor wave is
    several minutes of silence. It is called after the usage row is appended, so
    the log and the callback can never disagree about what finished. A callback
    that raises is logged and swallowed: progress reporting must not be able to
    kill a wave that is spending real tokens.
    """
    try:
        cli_name = _normalize_cli(cli)
    except ValueError as exc:
        return _error_result(str(exc))

    if claude_bin is not None and cli_bin is None:
        if cli_name != "claude":
            return _error_result(
                f"claude_bin={claude_bin!r} is only valid with cli=claude "
                f"(got cli={cli_name!r}); use cli_bin for other CLIs"
            )
        cli_bin = claude_bin
    if cli_bin is None:
        cli_bin = _default_bin(cli_name)

    if concurrency < 1:
        return _error_result(f"invalid concurrency {concurrency!r}; must be >= 1")

    requested_cache = (cache or "auto").strip().lower()
    if requested_cache not in CACHE_MODES:
        return _error_result(
            f"invalid cache mode {cache!r}; expected one of {sorted(CACHE_MODES)}"
        )

    cwd = neutral_claude_cwd()
    job_timeout = _CLI_JOB_TIMEOUT_S.get(cli_name)
    # Computed once, so the preflight probes the exact env the workers get.
    # Cache TTL knobs are applied after the spf_tokens pass below — auth does
    # not read them, and the run() closure looks up wave_env by name at call
    # time, so the reassignment is visible to every worker.
    wave_env = subscription_env(cli_name)
    # Only a real spawn needs a per-worker config dir. A stub runner must never
    # touch the operator's Cursor directory or the temp root, so unit tests stay
    # hermetic.
    slot_source = (
        _cursor_config_dir(wave_env)
        if cli_name == "cursor" and runner is None
        else None
    )
    # Refuse a root that would kill every long job, before anything spawns. The
    # symptom names Cursor's own endpoint, so left to the jobs it reads as a
    # provider outage rather than as a path length.
    if slot_source is not None:
        slot_path_error = _slot_path_error(_slot_root())
        if slot_path_error is not None:
            return _error_result(slot_path_error, cwd)
    if runner is None:
        def run(cmd: list[str], *, input_text: str, cwd: Path) -> tuple[int, str, str]:
            # The slot is held only across the subprocess call -- precisely the
            # window in which two workers could collide on the config file.
            # ``wave_env`` is looked up by name at call time on purpose: it is
            # reassigned below once the prompt-cache mode resolves, and freezing
            # it here would drop FORCE_PROMPT_CACHING_5M from every job.
            with (
                _cursor_slot(slot_source) if slot_source is not None else nullcontext()
            ):
                return default_claude_runner(
                    cmd,
                    input_text=input_text,
                    cwd=cwd,
                    timeout=job_timeout,
                    cli=cli_name,
                    env=_slot_env(cli_name, wave_env),
                )
    else:
        run = runner

    # Resolve the launcher to a concrete path. On Windows, ``subprocess`` calls
    # CreateProcess, which does NOT search PATHEXT — a bare ``claude`` matches
    # only the extensionless npm shim (not directly executable) and fails with
    # WinError 2. ``shutil.which`` honors PATHEXT and returns ``claude.cmd`` /
    # ``claude.exe``. Only resolve when using the real runner (tests pass a
    # stub and expect ``cli_bin`` verbatim in the command).
    if runner is None:
        resolved = shutil.which(cli_bin)
        if not resolved:
            return _error_result(_bin_missing_error(cli_name, cli_bin), cwd)
        cli_bin = resolved

    # Subscription preflight, after binary resolution (so it probes the same
    # absolute path the workers launch) and before any job runs. Skipped for an
    # empty fan-out so an idempotent re-run with nothing to do stays a no-op.
    #
    # ``subscription_auth_error`` documents that it must run with the same env
    # the workers get. Per-worker config dirs would quietly make that false, so
    # the probe takes a real seeded slot -- which also proves a seeded directory
    # still authenticates *before* N jobs depend on it.
    # Guarded on ``jobs`` for the same reason the probe below is: an empty
    # fan-out must stay a pure no-op, right down to not creating a slot.
    probe_env = wave_env
    if jobs and slot_source is not None:
        # Snapshot a seeded slot's env the same way a worker would, then release
        # the slot back to the free list *before* the probe runs. The directory
        # stays on disk, so auth still sees the seeded copy; holding the index
        # through a 30s probe would just pin it for no reason. It used to seed
        # ``slot-0`` by hand and fall through on failure -- and slot-0 is
        # precisely the index a foreign-owned directory poisons, so the probe
        # silently ran on the shared config exactly when isolation was broken,
        # proving nothing about the directory the workers would actually use.
        with _cursor_slot(slot_source):
            probe_env = dict(_slot_env(cli_name, wave_env))
    if jobs and (prober is not None or runner is None):
        auth_error = subscription_auth_error(
            cli_name, cli_bin, probe_env, cwd=cwd, prober=prober
        )
        if auth_error:
            return _error_result(f"subscription preflight failed: {auth_error}", cwd)
        # A bad Cursor --model otherwise costs one dead process per job. Both
        # checks are token-free and fail open, so this can only ever convert N
        # identical failures into one message with zero spawns.
        if cli_name == "cursor":
            # Through the same seeded slot the auth probe used: this spawn's argv
            # has a worker job's shape, so on the shared config it could rewrite
            # the operator's own selectedModel.
            model_error = cursor_model_error(
                cli_bin, model, probe=prober, env=probe_env
            )
            if model_error:
                return _error_result(f"model preflight failed: {model_error}", cwd)

    wrote: list[str] = []
    failed: list[dict[str, str]] = []
    cli_label = "cursor-agent -p" if cli_name == "cursor" else "claude -p"

    # Size every distinct preamble once, before anything spawns: the whole point
    # of the cache split is that one file serves the wave, so re-reading it per
    # job would be the measurement paying its own overhead.
    spf_tokens: dict[str, int] = {}
    for job in jobs:
        spf_path = job.get("system_prompt_file")
        if not spf_path or spf_path in spf_tokens:
            continue
        try:
            spf_tokens[spf_path] = approx_tokens(
                Path(spf_path).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError):
            spf_tokens[spf_path] = 0

    # Resolve the prompt-cache mode and (for Claude) apply it to the child env.
    # Cursor has no equivalent knob — record None so A/Bs stay honest.
    use_warm_first = warm_first
    if cli_name == "claude":
        if requested_cache == "auto":
            baseline, _ = baseline_tokens(usage_log, cli=cli_name)
            resolved_cache = resolve_cache_mode(
                jobs, spf_tokens, baseline, median_wall_s(usage_log, cli=cli_name)
            )
        else:
            resolved_cache = requested_cache
        wave_env = prompt_cache_env(wave_env, mode=resolved_cache)
        if resolved_cache == "off":
            # A warm-up with nothing to warm is pure serialized latency
            # (90–330 s on judge waves).
            use_warm_first = False
        cache_label: str | None = resolved_cache
    else:
        cache_label = None

    # What the log should say this wave ran at. On Claude that is the ``--effort``
    # argv; on Cursor that flag is dropped by ``_build_cmd`` and the live channel
    # is the model's own ``[effort=…]`` bracket — so read it back from the model
    # rather than logging ``null`` for a wave that plainly ran at some effort.
    effort_label = effort if cli_name == "claude" else cursor_model_effort(model)

    def _run_one(job: dict[str, Any], warm: bool) -> tuple[str, bool, str, dict[str, Any]]:
        job_id = str(job["id"])
        started = time.monotonic()
        prompt_sent = 0
        usage: dict[str, Any] | None = None
        rc = -1

        def _record(detail: str | None) -> dict[str, Any]:
            return job_record(
                job_id=job_id,
                cli=cli_name,
                model=model,
                prompt_sent=prompt_sent,
                wall_s=time.monotonic() - started,
                rc=rc,
                flags=extra_flags if cli_name == "claude" else (),
                warm=warm,
                usage=usage,
                error=detail,
                effort=effort_label,
                cache=cache_label,
                timed_out=rc == 124,
            )

        try:
            output_path = Path(job["output_path"])
            input_text = job["input_text"]
            spf = job.get("system_prompt_file")
            spf_for_cmd, stdin_text = _fold_system_prompt(cli_name, input_text, spf)
            # What we meant to send. Cursor has the preamble folded into stdin
            # already, so counting spf again there would double-count it.
            prompt_sent = approx_tokens(stdin_text) + (
                spf_tokens.get(spf, 0) if spf_for_cmd else 0
            )
            cmd = _build_cmd(cli_name, cli_bin, model, spf_for_cmd, extra_flags=extra_flags)
            rc, stdout, stderr = run(cmd, input_text=stdin_text, cwd=cwd)
            if rc != 0:
                # Report BOTH streams. These CLIs put the actual cause on stdout
                # ("Credit balance is too low") while stderr carries unrelated
                # warnings ("claude.ai connectors are disabled…"), so preferring
                # stderr reported the warning and hid the reason every job failed.
                # Under --output-format json that cause arrives wrapped, so unwrap
                # it rather than reporting a JSON blob as the failure reason.
                parts = [_failure_detail(cli_name, stdout), (stderr or "").strip()]
                detail = " | ".join(p for p in parts if p) or f"exit {rc}"
                usage = _usage_from_stdout(stdout, model=model)
                return job_id, False, detail[:500], _record(detail)
            try:
                prose, usage = _extract_result(cli_name, stdout, model=model)
            except ValueError as exc:
                usage = _usage_from_stdout(stdout, model=model)
                return job_id, False, str(exc)[:500], _record(str(exc))
            if not prose:
                detail = f"empty stdout from {cli_label}"
                return job_id, False, detail, _record(detail)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(prose + "\n", encoding="utf-8")
            return job_id, True, "ok", _record(None)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"[:500]
            return job_id, False, detail, _record(detail)

    records: list[dict[str, Any]] = []
    booked: set[str] = set()
    wave_started = time.monotonic()
    done_count = 0

    def _warm_label(index: int) -> bool:
        """Whether job ``index`` is the wave's cache warm-up, for its usage row.

        Every job in a wave shares a prefix — the CLI's own system prompt plus,
        for solo judge entries, the per-judge preamble passed via
        ``--system-prompt-file`` — and that prefix is cacheable. Cache *writes*
        bill at **2×** base input on the CLI's default 1-hour TTL, or **1.25×**
        under ``FORCE_PROMPT_CACHING_5M``; cache *reads* bill at **0.1×**.
        Starting the whole pool at once means every job pays ``cache_creation``
        and only stragglers can read, so job 1 runs alone first. The 2026-07-30
        baseline probe put that prefix at ~5.8k tokens per job, so on an
        eight-job wave that is most of the overhead for one job's latency.

        The gate below additionally requires ``concurrency > 1``; this label
        deliberately does **not**. At ``concurrency == 1`` the old batching still
        marked job 0 warm even though nothing was serialized for its benefit,
        and ``usage.jsonl`` is an A/B corpus — a relabelled row is a corrupted
        one. Verified equivalent to the old per-row label for (8,5,warm),
        (8,5,cold), (1,5,warm), (3,1,warm) and (3,2,warm).
        """
        return use_warm_first and len(jobs) > 1 and index == 0

    def _collect(outcome: tuple[str, bool, str, dict[str, Any]]) -> None:
        nonlocal done_count
        job_id, ok, detail, record = outcome
        # Idempotent: the interrupt path re-walks every done future, including
        # ones ``as_completed`` already booked. A second pass would duplicate
        # ``wrote``/``failed`` and punch a second row into ``usage.jsonl``.
        if job_id in booked:
            return
        booked.add(job_id)
        if ok:
            wrote.append(job_id)
        else:
            failed.append({"id": job_id, "error": detail})
        # Written from this thread as each job lands, so a wave killed part-way
        # still leaves the telemetry for the jobs that finished.
        records.append(record)
        append_usage(usage_log, record)
        done_count += 1
        if on_job_done is not None:
            try:
                on_job_done({
                    "id": job_id,
                    "ok": ok,
                    "error": None if ok else detail,
                    "done": done_count,
                    "total": len(jobs),
                })
            except Exception:  # noqa: BLE001 - a progress hook is not the wave
                logger.exception("on_job_done callback failed for job %s", job_id)

    # A rolling pool, not fixed groups: a free slot takes the next job the
    # moment it frees. Groups made every job wait on the slowest in its group,
    # which was 31% of worker time across 230 rebuilt Cursor judge groups.
    # Guarded on ``jobs`` because ``max_workers=0`` is a ValueError, and an
    # empty fan-out must stay the idempotent no-op callers rely on.
    # After the preflight, which takes a slot of its own and must not count as a
    # worker in the tally below.
    _reset_slot_stats()
    if jobs:
        with ThreadPoolExecutor(max_workers=min(concurrency, len(jobs))) as pool:
            # Everything submitted but not yet handed to ``_collect``. The except
            # path below reads it, so the warm-up belongs inside the ``try`` too:
            # an interrupt during that one job used to reach no cleanup at all.
            futures: list[Any] = []
            try:
                start = 0
                if use_warm_first and len(jobs) > 1 and concurrency > 1:
                    warm = pool.submit(_run_one, jobs[0], _warm_label(0))
                    futures.append(warm)
                    outcome = warm.result()
                    futures.clear()  # collected here, not by the except path
                    _collect(outcome)
                    start = 1
                futures = [
                    pool.submit(_run_one, job, _warm_label(start + offset))
                    for offset, job in enumerate(jobs[start:])
                ]
                for fut in as_completed(futures):
                    _collect(fut.result())
            except BaseException:
                # Three steps, in this order, and none of them is optional.
                #
                # Cancelling drops the queued backlog -- the whole wave is queued
                # up front, so without it an interrupt lets every remaining job
                # run on spending tokens. Killing the live children is the part
                # the operator's Ctrl-C cannot do for itself: KeyboardInterrupt
                # reaches only this thread, while the Popens live in the pool's,
                # so the per-spawn cleanup never fires. And the explicit wait is
                # then cheap, where ``__exit__``'s own ``shutdown(wait=True)``
                # would otherwise sit out every in-flight job -- measured at a
                # full extra job's wall, and up to the CLI's 900 s ceiling each.
                pool.shutdown(wait=False, cancel_futures=True)
                _kill_live_processes()
                pool.shutdown(wait=True)
                # A job that finished already wrote its draft. Collect what
                # landed rather than dropping it from `wrote`/`failed` and from
                # usage.jsonl, which is an A/B corpus a killed wave must not
                # silently punch holes in.
                for fut in futures:
                    if not fut.done() or fut.cancelled():
                        continue
                    # ``exception()`` rather than a try around ``result()``: a
                    # job that died of the interrupt itself raises BaseException,
                    # which would escape an ``except Exception`` here and abandon
                    # every future after it -- losing the rows this loop exists
                    # to save.
                    if fut.exception() is not None:
                        continue
                    try:
                        _collect(fut.result())
                    except Exception:  # noqa: BLE001 - never mask the interrupt
                        logger.exception("could not collect a job after an interrupt")
                raise

    out: dict[str, Any] = {
        "wrote": wrote,
        "failed": failed,
        "cwd": str(cwd),
        "cli": cli_name,
        "counts": {
            "wrote": len(wrote),
            "failed": len(failed),
            "todo": len(jobs),
        },
    }
    usage_summary = rollup(records)
    tally = _slot_tally() if slot_source is not None else None
    if usage_summary is None and tally is not None:
        # ``rollup`` is None when no job reported tokens -- an all-failed or
        # all-timed-out wave. That is precisely the wave whose isolation tally
        # matters most, so give it a summary of its own rather than dropping the
        # signal in exactly the failure shape it was added to make audible.
        usage_summary = {}
    if usage_summary is not None:
        usage_summary["wall_s"] = round(time.monotonic() - wave_started, 1)
        # How many workers actually got isolation. Without this a wave that lost
        # it is indistinguishable from one that kept it.
        if tally is not None:
            usage_summary["slots_seeded"] = tally
        out["usage"] = usage_summary
    return out
