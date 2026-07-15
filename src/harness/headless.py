"""Shared headless ``claude -p`` wave launcher for translate- and judge-fanout.

Both fan-outs need the same Windows/cwd/absolutize/wave fixes:

- Resolve ``claude`` via ``shutil.which`` (PATHEXT) when using the real runner.
- Run from a neutral empty cwd so project ``CLAUDE.md`` is not auto-loaded.
- Absolutize ``--system-prompt-file`` (worker cwd is neutral, not the project).
- Process jobs in waves of ``concurrency`` (one wave finishes before the next).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Optional

Runner = Callable[..., tuple[int, str, str]]


def neutral_claude_cwd() -> Path:
    """Empty temp dir so ``claude -p`` does not auto-load a project CLAUDE.md."""
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
    cmd: list[str], *, input_text: str, cwd: Path
) -> tuple[int, str, str]:
    """Run ``claude -p`` with the prompt on stdin; return (rc, stdout, stderr)."""
    proc = subprocess.run(
        cmd,
        input=input_text,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def run_headless_wave(
    jobs: list[dict[str, Any]],
    *,
    model: str,
    concurrency: int,
    claude_bin: str = "claude",
    runner: Optional[Runner] = None,
) -> dict[str, Any]:
    """Run one headless ``claude -p`` wave for the given jobs.

    Each ``job`` is ``{id, input_text, output_path, system_prompt_file?}``.
    Returns ``{wrote, failed, cwd, counts}``. When the real runner is used and
    ``claude_bin`` is missing from PATH, returns a top-level ``error`` with empty
    lists (fail-fast; no per-job wave).
    """
    if concurrency < 1:
        return {
            "error": f"invalid concurrency {concurrency!r}; must be >= 1",
            "wrote": [],
            "failed": [],
            "cwd": None,
            "counts": {"wrote": 0, "failed": 0, "todo": 0},
        }

    cwd = neutral_claude_cwd()
    run = runner or default_claude_runner

    # Resolve the launcher to a concrete path. On Windows, ``subprocess`` calls
    # CreateProcess, which does NOT search PATHEXT — a bare ``claude`` matches
    # only the extensionless npm shim (not directly executable) and fails with
    # WinError 2. ``shutil.which`` honors PATHEXT and returns ``claude.cmd`` /
    # ``claude.exe``. Only resolve when using the real runner (tests pass a
    # stub and expect ``claude_bin`` verbatim in the command).
    if runner is None:
        resolved = shutil.which(claude_bin)
        if not resolved:
            return {
                "error": f"claude not found: {claude_bin!r} (not on PATH / PATHEXT)",
                "wrote": [],
                "failed": [],
                "cwd": str(cwd),
                "counts": {"wrote": 0, "failed": 0, "todo": 0},
            }
        claude_bin = resolved

    wrote: list[str] = []
    failed: list[dict[str, str]] = []

    def _run_one(job: dict[str, Any]) -> tuple[str, bool, str]:
        job_id = str(job["id"])
        try:
            output_path = Path(job["output_path"])
            input_text = job["input_text"]
            spf = job.get("system_prompt_file")
            if spf:
                cmd = [
                    claude_bin,
                    "-p",
                    "--system-prompt-file",
                    str(Path(spf).resolve()),
                    "--model",
                    model,
                    "--tools",
                    "",
                    "--output-format",
                    "text",
                ]
            else:
                cmd = [
                    claude_bin,
                    "-p",
                    "--model",
                    model,
                    "--tools",
                    "",
                    "--output-format",
                    "text",
                ]
            rc, stdout, stderr = run(cmd, input_text=input_text, cwd=cwd)
            if rc != 0:
                detail = (stderr or stdout or f"exit {rc}").strip()
                return job_id, False, detail[:500]
            prose = (stdout or "").strip()
            if not prose:
                return job_id, False, "empty stdout from claude -p"
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
        "counts": {
            "wrote": len(wrote),
            "failed": len(failed),
            "todo": len(jobs),
        },
    }
