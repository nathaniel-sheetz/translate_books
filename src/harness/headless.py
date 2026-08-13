"""Shared headless CLI wave launcher for translate- and judge-fanout.

Both fan-outs need the same Windows/cwd/absolutize/wave fixes:

- Resolve the CLI binary via ``shutil.which`` (PATHEXT) when using the real runner.
- Run from a neutral empty cwd so project ``CLAUDE.md`` / workspace context is not
  auto-loaded.
- Absolutize ``--system-prompt-file`` (worker cwd is neutral, not the project).
- Process jobs in waves of ``concurrency`` (one wave finishes before the next).
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
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

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
    cli_bin: str, *, probe: AuthProber | None = None, timeout: float = 30.0
) -> set[str]:
    """Ids from ``cursor-agent models``; empty set when it cannot be read.

    The list is **incomplete** — see :func:`cursor_model_error` — so an empty
    result and a missing id are treated the same way: not evidence of anything.
    """
    runner = probe if probe is not None else _default_auth_prober
    try:
        rc, stdout, _stderr = runner(
            [cli_bin, "models"],
            env=subscription_env("cursor"),
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
    """
    base = _cursor_model_base(model)
    if not base:
        return None
    known = _cursor_known_models(cli_bin, probe=probe, timeout=timeout)
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
            argv, env=subscription_env("cursor"), cwd=neutral_claude_cwd(), timeout=timeout
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


def neutral_claude_cwd() -> Path:
    """Empty temp dir so headless CLIs do not auto-load a project CLAUDE.md."""
    root = Path(
        os.environ.get("TEMP")
        or os.environ.get("TMP")
        or os.environ.get("TMPDIR")
        or tempfile.gettempdir()
    )
    cwd = root / "claude-headless-empty"
    cwd.mkdir(parents=True, exist_ok=True)
    return cwd


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
    """
    child_env = dict(env) if env is not None else subscription_env(cli)
    try:
        proc = subprocess.run(
            cmd,
            input=input_text,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
            env=child_env,
        )
    except subprocess.TimeoutExpired as exc:
        detail = f"timeout after {timeout:g}s"
        err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return 124, out, (err.strip() or detail)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


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
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
        cwd=str(cwd),
        env=dict(env),
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


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
    # Third gate, same order `run_headless_wave` applies it in (after auth, so a
    # logged-out CLI is reported as logged out rather than as a model problem).
    # Token-free and fails open in every direction, so hoisting it can only turn
    # "the job died after prepare" into "the estimate never went green".
    if cli_name == "cursor" and model:
        return cursor_model_error(resolved, model)
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


def _wave_batches(
    jobs: list[dict[str, Any]], concurrency: int, warm_first: bool
) -> list[list[dict[str, Any]]]:
    """Split ``jobs`` into batches, optionally running the first job alone.

    Every job in a wave shares a prefix — the CLI's own system prompt plus, for
    solo judge entries, the per-judge preamble passed via ``--system-prompt-file``
    — and that prefix is cacheable. Cache *writes* bill at **2×** base input on
    the CLI's default 1-hour TTL, or **1.25×** under ``FORCE_PROMPT_CACHING_5M``;
    cache *reads* bill at **0.1×**. Launching ``concurrency`` jobs simultaneously
    means the whole wave front starts before any of them has written a cache
    entry, so all of them pay ``cache_creation`` and only the stragglers can
    read it. The 2026-07-30 baseline probe put that prefix at ~5.8k tokens per
    job, so on an eight-job wave the difference is most of the overhead, for the
    price of one job's latency.
    """
    if warm_first and len(jobs) > 1 and concurrency > 1:
        rest = jobs[1:]
        return [jobs[:1]] + [
            rest[i : i + concurrency] for i in range(0, len(rest), concurrency)
        ]
    return [jobs[i : i + concurrency] for i in range(0, len(jobs), concurrency)]


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
    across identical-prefix probe calls) while ``cacheWriteTokens`` stayed 0, so
    the client cannot write, pin, or price an entry. Cursor waves therefore record
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
    if runner is None:
        def run(cmd: list[str], *, input_text: str, cwd: Path) -> tuple[int, str, str]:
            return default_claude_runner(
                cmd,
                input_text=input_text,
                cwd=cwd,
                timeout=job_timeout,
                cli=cli_name,
                env=wave_env,
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
    if jobs and (prober is not None or runner is None):
        auth_error = subscription_auth_error(
            cli_name, cli_bin, wave_env, cwd=cwd, prober=prober
        )
        if auth_error:
            return _error_result(f"subscription preflight failed: {auth_error}", cwd)
        # A bad Cursor --model otherwise costs one dead process per job. Both
        # checks are token-free and fail open, so this can only ever convert N
        # identical failures into one message with zero spawns.
        if cli_name == "cursor":
            model_error = cursor_model_error(cli_bin, model, probe=prober)
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
    wave_started = time.monotonic()
    batches = _wave_batches(jobs, concurrency, use_warm_first)
    done_count = 0
    for batch_index, wave in enumerate(batches):
        warm = use_warm_first and batch_index == 0 and len(batches) > 1
        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            futures = {pool.submit(_run_one, j, warm): j for j in wave}
            for fut in as_completed(futures):
                job_id, ok, detail, record = fut.result()
                if ok:
                    wrote.append(job_id)
                else:
                    failed.append({"id": job_id, "error": detail})
                # Written from this thread as each job lands, so a wave killed
                # part-way still leaves the telemetry for the jobs that finished.
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
                        logger.exception(
                            "on_job_done callback failed for job %s", job_id
                        )

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
    if usage_summary is not None:
        usage_summary["wall_s"] = round(time.monotonic() - wave_started, 1)
        out["usage"] = usage_summary
    return out
