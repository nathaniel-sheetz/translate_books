"""Shared headless CLI wave launcher for translate- and judge-fanout.

Both fan-outs need the same Windows/cwd/absolutize/wave fixes:

- Resolve the CLI binary via ``shutil.which`` (PATHEXT) when using the real runner.
- Run from a neutral empty cwd so project ``CLAUDE.md`` / workspace context is not
  auto-loaded.
- Absolutize ``--system-prompt-file`` (worker cwd is neutral, not the project).
- Process jobs in waves of ``concurrency`` (one wave finishes before the next).

CLI families are selected with ``cli`` (``claude`` | ``cursor``). The Claude profile
preserves today's ``claude -p`` argv. The Cursor profile drives ``cursor-agent``
under a subscription login (no metered API key).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Optional

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
) -> tuple[int, str, str]:
    """Run a headless CLI with the prompt on stdin; return (rc, stdout, stderr)."""
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
) -> dict[str, Any]:
    """Run one headless CLI wave for the given jobs.

    Each ``job`` is ``{id, input_text, output_path, system_prompt_file?}``.
    Returns ``{wrote, failed, cwd, counts}``. When the real runner is used and
    the binary is missing from PATH, returns a top-level ``error`` with empty
    lists (fail-fast; no per-job wave).

    ``claude_bin`` is a back-compat alias for ``cli_bin`` and is only valid when
    ``cli`` is ``claude`` (mismatch returns a top-level ``error``).
    """
    try:
        cli_name = _normalize_cli(cli)
    except ValueError as exc:
        return {
            "error": str(exc),
            "wrote": [],
            "failed": [],
            "cwd": None,
            "counts": {"wrote": 0, "failed": 0, "todo": 0},
        }

    if claude_bin is not None and cli_bin is None:
        if cli_name != "claude":
            return {
                "error": (
                    f"claude_bin={claude_bin!r} is only valid with cli=claude "
                    f"(got cli={cli_name!r}); use cli_bin for other CLIs"
                ),
                "wrote": [],
                "failed": [],
                "cwd": None,
                "counts": {"wrote": 0, "failed": 0, "todo": 0},
            }
        cli_bin = claude_bin
    if cli_bin is None:
        cli_bin = _default_bin(cli_name)

    if concurrency < 1:
        return {
            "error": f"invalid concurrency {concurrency!r}; must be >= 1",
            "wrote": [],
            "failed": [],
            "cwd": None,
            "counts": {"wrote": 0, "failed": 0, "todo": 0},
        }

    cwd = neutral_claude_cwd()
    job_timeout = _CLI_JOB_TIMEOUT_S.get(cli_name)
    if runner is None:
        def run(cmd: list[str], *, input_text: str, cwd: Path) -> tuple[int, str, str]:
            return default_claude_runner(
                cmd, input_text=input_text, cwd=cwd, timeout=job_timeout
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
            return {
                "error": _bin_missing_error(cli_name, cli_bin),
                "wrote": [],
                "failed": [],
                "cwd": str(cwd),
                "counts": {"wrote": 0, "failed": 0, "todo": 0},
            }
        cli_bin = resolved

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
