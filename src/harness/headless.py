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
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

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
    """Return a warning when cursor is paired with a Claude-looking worker_model."""
    if (cli or "").strip().lower() != "cursor":
        return None
    alias = (worker_model or "").strip().lower()
    if not alias:
        return None
    if alias in _CLAUDE_WORKER_ALIASES or alias.startswith("claude-"):
        return (
            f"worker_model={worker_model!r} looks like a Claude alias/id while "
            f"headless_cli=cursor; set --worker-model to a Cursor model "
            f"(e.g. grok-4.5 or auto)"
        )
    return None


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

# Argv that makes a CLI report which credential path it would use. ``None`` means
# the family has no verified equivalent -- it gets the env scrub but not the
# preflight (see the Cursor entry below).
_AUTH_PROBE_ARGV: dict[str, tuple[str, ...] | None] = {
    "claude": ("auth", "status", "--json"),
    # No verified `cursor-agent` auth-status command. Guessing at one risks
    # hard-failing a working setup, so the Cursor profile relies on the
    # CURSOR_API_KEY scrub plus the fact that the harness has no metered Cursor
    # code path at all. See docs/LLM_PROVIDERS.md.
    "cursor": None,
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

    A **routing** probe, not a liveness probe: ``claude auth status`` reports the
    credential path that would be used and reports it identically for a valid and
    a bogus key. It answers "where does the bill go", never "will this call
    succeed".

    Must run with the same scrubbed ``env`` *and* the same ``cwd`` as the workers.
    The CLI reads project-local settings, so probing from the repo root can report
    a different auth path than a worker in the neutral cwd actually gets.

    Fails closed. An unusable probe (missing subcommand, non-zero exit, output
    shape we do not recognise) blocks the wave -- unverifiable means unsafe, and
    ``--backend api`` is the supported way to spend money.
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
    probe = prober if prober is not None else _default_auth_prober
    probe_cwd = cwd if cwd is not None else neutral_claude_cwd()
    try:
        rc, stdout, stderr = probe(argv, env=env, cwd=probe_cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"`{cli_bin} auth status` timed out after {timeout:g}s"
    except OSError as exc:
        return f"could not run `{cli_bin} auth status`: {exc}"

    if rc != 0:
        detail = (stderr or stdout or "").strip()[:300] or f"exit {rc}"
        return (
            f"`{cli_bin} auth status --json` failed ({detail}). Upgrade the CLI "
            f"so the subscription preflight can run, or use `--backend api`."
        )

    try:
        obj = json.loads((stdout or "").strip())
    except json.JSONDecodeError:
        return (
            f"could not parse `{cli_bin} auth status --json` output: "
            f"{(stdout or '').strip()[:200]!r}"
        )
    if not isinstance(obj, dict):
        return f"unexpected `{cli_bin} auth status --json` shape: {type(obj).__name__}"

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


def _build_cmd(
    cli: str,
    cli_bin: str,
    model: str,
    spf: Optional[str],
) -> list[str]:
    """Build argv for one headless job (prompt still goes on stdin)."""
    if cli == "cursor":
        # Ask mode + no --force: answer-only, no applied file edits.
        # --trust: skip workspace-trust prompts in the empty neutral cwd.
        # No --system-prompt-file / --tools (Cursor has neither); callers fold
        # any preamble into stdin via _fold_system_prompt.
        return [
            cli_bin,
            "-p",
            "--trust",
            "--mode",
            "ask",
            "--model",
            model,
            "--output-format",
            "text",
        ]

    # claude profile — today's exact behavior (regression-preserved).
    cmd = [
        cli_bin,
        "-p",
        "--model",
        model,
        "--tools",
        "",
        "--output-format",
        "text",
    ]
    if spf:
        cmd[2:2] = ["--system-prompt-file", str(Path(spf).resolve())]
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


def _cursor_envelope_error(obj: dict[str, Any]) -> str:
    """Build a short failure detail from a Cursor result envelope."""
    result = obj.get("result")
    if isinstance(result, str) and result.strip():
        return result.strip()[:500]
    subtype = obj.get("subtype")
    if isinstance(subtype, str) and subtype.strip():
        return f"cursor result envelope error (subtype={subtype!r})"
    return "cursor result envelope reported is_error"


def _extract_output(cli: str, stdout: str) -> str:
    """Normalize CLI stdout to the answer text written to the draft file.

    For Cursor JSON result envelopes: only unwrap a string ``result``. Error
    envelopes (``is_error`` / ``subtype=error``) and non-string ``result`` values
    raise ``ValueError`` so the job is recorded as failed instead of writing a
    poison draft (Python ``repr`` / nested JSON).
    """
    text = (stdout or "").strip()
    if cli != "cursor" or not text.startswith("{"):
        return text
    # Hardening: if a run accidentally used --output-format json, unwrap the
    # terminal result envelope so commit parsers still see clean prose/JSON.
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not (isinstance(obj, dict) and obj.get("type") == "result" and "result" in obj):
        return text
    if obj.get("is_error") is True or obj.get("subtype") == "error":
        raise ValueError(_cursor_envelope_error(obj))
    result = obj["result"]
    if not isinstance(result, str):
        raise ValueError(
            "cursor result envelope has non-string result "
            f"(type={type(result).__name__}); expected prose/JSON text"
        )
    return result.strip()


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
) -> dict[str, Any]:
    """Run one headless CLI wave for the given jobs.

    Each ``job`` is ``{id, input_text, output_path, system_prompt_file?}``.
    Returns ``{wrote, failed, cwd, counts}``. When the real runner is used and
    the binary is missing from PATH, or the CLI is not on a subscription login,
    returns a top-level ``error`` with empty lists (fail-fast; no per-job wave).

    ``claude_bin`` is a back-compat alias for ``cli_bin`` and is only valid when
    ``cli`` is ``claude`` (mismatch returns a top-level ``error``).

    When the real runner is used, the child env is scrubbed via
    ``subscription_env``. A custom ``runner`` never receives that scrubbed env
    (tests pass their own). The auth preflight runs when ``prober`` is given, or
    when the real runner is in use; a stub ``runner`` with no ``prober`` skips
    it, so unit tests never spawn anything.
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

    cwd = neutral_claude_cwd()
    job_timeout = _CLI_JOB_TIMEOUT_S.get(cli_name)
    # Computed once, so the preflight probes the exact env the workers get.
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

    wrote: list[str] = []
    failed: list[dict[str, str]] = []
    cli_label = "cursor-agent -p" if cli_name == "cursor" else "claude -p"

    def _run_one(job: dict[str, Any]) -> tuple[str, bool, str]:
        job_id = str(job["id"])
        try:
            output_path = Path(job["output_path"])
            input_text = job["input_text"]
            spf = job.get("system_prompt_file")
            spf_for_cmd, stdin_text = _fold_system_prompt(cli_name, input_text, spf)
            cmd = _build_cmd(cli_name, cli_bin, model, spf_for_cmd)
            rc, stdout, stderr = run(cmd, input_text=stdin_text, cwd=cwd)
            if rc != 0:
                detail = (stderr or stdout or f"exit {rc}").strip()
                return job_id, False, detail[:500]
            try:
                prose = _extract_output(cli_name, stdout)
            except ValueError as exc:
                return job_id, False, str(exc)[:500]
            if not prose:
                return job_id, False, f"empty stdout from {cli_label}"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(prose + "\n", encoding="utf-8")
            return job_id, True, "ok"
        except Exception as exc:
            return job_id, False, f"{type(exc).__name__}: {exc}"[:500]

    for i in range(0, len(jobs), concurrency):
        wave = jobs[i : i + concurrency]
        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            futures = {pool.submit(_run_one, j): j for j in wave}
            for fut in as_completed(futures):
                job_id, ok, detail = fut.result()
                if ok:
                    wrote.append(job_id)
                else:
                    failed.append({"id": job_id, "error": detail})

    return {
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
