"""
One headless wave over a prepared decisions manifest, shared by the scan and
consolidate steps.

A manifest is ``{preamble_path, worker_model, cli, effort, jobs: [{id, body_path,
draft_path}]}`` with every path inside ``.harness/decisions/``. Each job sends its
body on stdin with the shared preamble as the system prompt; a job whose draft
already exists is skipped, so a re-run picks up only what failed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from src.decisions import decisions_dir

DEFAULT_CONCURRENCY = 5

FANOUT_SCHEMA = {
    "wrote": "job ids whose drafts were written this wave",
    "failed": "list of {id, error} — re-run fanout for these",
    "skipped": "job ids that already had a non-empty draft",
    "counts": "{wrote, failed, skipped, todo}",
    "effective": "what the wave ran as",
    "usage": "what the wave consumed; per-job rows go to the wave's usage log",
    "instructions": "next step",
}


def fanout_error(message: str) -> dict[str, Any]:
    return {
        "error": message,
        "wrote": [],
        "failed": [],
        "skipped": [],
        "counts": {"wrote": 0, "failed": 0, "skipped": 0, "todo": 0},
        "_schema": FANOUT_SCHEMA,
    }


def load_manifest(path: Path, *, missing: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """``(manifest, None)`` or ``(None, error)``; ``missing`` is the not-found message."""
    if not path.exists():
        return None, missing
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"unreadable manifest {path}: {exc}"
    if not isinstance(doc.get("jobs"), list):
        return None, f"manifest {path} has no 'jobs' list"
    return doc, None


def inside(project_dir: Path, raw: str) -> Path:
    """``raw`` resolved, refusing anything outside ``.harness/decisions/``."""
    path = Path(raw).resolve()
    if not path.is_relative_to(decisions_dir(project_dir).resolve()):
        raise ValueError(f"path escapes the decisions dir: {raw}")
    return path


def has_draft(path: Path) -> bool:
    try:
        return path.exists() and bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


def run_wave(
    project_dir: Path,
    manifest: dict[str, Any],
    *,
    command: str,
    job_ids: Optional[list[str]] = None,
    concurrency: Optional[int] = None,
    cli: Optional[str] = None,
    cli_bin: Optional[str] = None,
    effort: Optional[str] = None,
    runner=None,
) -> dict[str, Any]:
    """Run one headless wave over ``manifest``'s jobs as wave type ``command``.

    ``runner`` is a test seam: ``(cmd, *, input_text, cwd) -> (rc, stdout, stderr)``.
    """
    from src.harness import state as hstate
    from src.harness.headless import run_headless_wave
    from src.harness.profile import resolve_profile, usage_log_for

    project_dir = Path(project_dir)
    concurrency = DEFAULT_CONCURRENCY if concurrency is None else int(concurrency)
    if concurrency < 1:
        return fanout_error(f"concurrency must be at least 1, got {concurrency}")

    cfg = hstate.load_config(project_dir)
    # Inherit the consented effort only while the CLI family is unchanged; a
    # level resolved for one CLI is that CLI's table number.
    inherited_effort, inherited_effort_source = effort, "cli"
    if not effort and (cli or manifest.get("cli")) == manifest.get("cli"):
        inherited_effort = manifest.get("effort") or "default"
        inherited_effort_source = "manifest"
    prof = resolve_profile(
        project_dir,
        command=command,
        cli=cli or manifest.get("cli"),
        cli_source="cli" if cli else "manifest",
        worker_model=manifest.get("worker_model"),
        worker_model_source="manifest",
        effort=inherited_effort,
        effort_source=inherited_effort_source,
        cfg=cfg,
        usage_log=usage_log_for(project_dir, command),
    )
    extra_flags = hstate.compose_headless_argv(
        cfg, prof.effort if prof.effort_channel == "argv" else None
    )

    jobs = [j for j in manifest["jobs"] if isinstance(j, dict) and j.get("id")]
    if job_ids is not None:
        unknown = sorted(set(job_ids) - {j["id"] for j in jobs})
        if unknown:
            return fanout_error(f"job ids not in the manifest: {unknown}")
        jobs = [j for j in jobs if j["id"] in set(job_ids)]

    try:
        preamble = inside(project_dir, manifest.get("preamble_path") or "")
    except ValueError as exc:
        return fanout_error(str(exc))

    ready: list[dict[str, Any]] = []
    skipped: list[str] = []
    pre_failed: list[dict[str, str]] = []
    for job in jobs:
        try:
            draft = inside(project_dir, job.get("draft_path") or "")
            if has_draft(draft):
                skipped.append(job["id"])
                continue
            body = inside(project_dir, job.get("body_path") or "").read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            pre_failed.append({"id": job["id"], "error": f"{type(exc).__name__}: {exc}"[:500]})
            continue
        ready.append(
            {
                "id": job["id"],
                "input_text": body,
                "output_path": str(draft),
                "system_prompt_file": str(preamble),
            }
        )

    base = {
        "worker_model": prof.worker_model,
        "cli": prof.cli,
        "concurrency": concurrency,
        "effective": prof.to_payload(),
        "_schema": FANOUT_SCHEMA,
    }
    if prof.warnings:
        base["warnings"] = list(prof.warnings)
    if not ready:
        return {
            **base,
            "wrote": [],
            "failed": pre_failed,
            "skipped": skipped,
            "counts": {
                "wrote": 0, "failed": len(pre_failed),
                "skipped": len(skipped), "todo": len(pre_failed),
            },
            "instructions": (
                "Fix the failed jobs, then re-run fanout." if pre_failed else "Run commit."
            ),
        }

    wave = run_headless_wave(
        ready,
        model=prof.worker_model,
        concurrency=concurrency,
        cli=prof.cli,
        cli_bin=cli_bin,
        runner=runner,
        usage_log=usage_log_for(project_dir, command),
        extra_flags=extra_flags,
        effort=prof.effort,
    )
    if "error" in wave and not wave.get("wrote") and not wave.get("failed"):
        return {
            **fanout_error(wave["error"]),
            **base,
            "skipped": skipped,
            "instructions": "Fix the launcher error, then re-run fanout.",
        }

    failed = pre_failed + list(wave.get("failed") or [])
    wrote = list(wave.get("wrote") or [])
    out = {
        **base,
        "wrote": wrote,
        "failed": failed,
        "skipped": skipped,
        "counts": {
            "wrote": len(wrote), "failed": len(failed),
            "skipped": len(skipped), "todo": len(ready) + len(pre_failed),
        },
        "instructions": (
            "Re-run fanout for the failed jobs, then commit."
            if failed else "Run commit."
        ),
    }
    if wave.get("usage"):
        out["usage"] = wave["usage"]
    return out
