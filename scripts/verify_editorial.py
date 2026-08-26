#!/usr/bin/env python3
"""Non-interactive CLI for the editorial judge's adjudication pass.

``run_judges.py`` runs the editorial judge and persists its candidates. This
script gives those candidates a second opinion — CONFIRM, RETRACT or
RECLASSIFY — with the English original attached to the ones that asked for it,
and rewrites ``evaluations/<chunk>.json`` with the adjudicated set.

It is a sibling pipeline rather than an eighth ``run_judges.py`` subcommand, for
the same reason ``review_annotations.py`` is: the unit here is a *chunk's
candidate set*, not a ``JudgeTarget``, and the prompt carries per-candidate
context that the ``Judge`` seams have no slot for. It reuses this repo's
plumbing — ``llm_io`` for the call and the JSON parse, ``harness.headless`` for
the subscription wave, the same draft/commit split — rather than being forced
through a shape that does not fit.

Four subcommands:

  * ``status``  — **read-only**. Which chunks carry unverified candidates. No
                  spend, no writes.
  * ``run``     — **API backend**. Adjudicate now, one call per chunk, behind a
                  dollar cost gate.
  * ``prepare`` — **draft backend**, phase 1. Render one prompt file per chunk
                  plus a manifest, for a headless wave or spawned workers.
                  Zero spend.
  * ``fanout``  — **draft backend**, opt-in headless wave over the manifest.
  * ``commit``  — **draft backend**, phase 2. Parse the drafts and persist.

Every command prints exactly one JSON object and mirrors it to
``<project>/.harness/editorial/last_output.json``.

Examples:
    python scripts/verify_editorial.py status  --project pollyanna
    python scripts/verify_editorial.py run     --project pollyanna \\
        --scope chapter:chapter_01 --persist --confirm
    python scripts/verify_editorial.py prepare --project pollyanna --scope book
    python scripts/verify_editorial.py fanout  --project pollyanna
    python scripts/verify_editorial.py commit  --project pollyanna --persist
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.judges import editorial_verify as ev  # noqa: E402
from src.judges import llm_io  # noqa: E402
from src.judges.context import build_judge_context  # noqa: E402
from src.judges.scope import ScopeError, build_targets  # noqa: E402

logger = logging.getLogger(__name__)

JUDGE_NAME = "editorial"
_NO_SIDECAR_COMMANDS = frozenset({"status"})

#: Completion-token allowance per candidate when estimating cost. An
#: adjudication verdict is a decision plus one sentence of reason, occasionally
#: a corrected fix — far shorter than a judge's finding, which carries an
#: excerpt and a suggestion.
_TOKENS_PER_VERDICT = 120


# ---------------------------------------------------------------------------
# Project + IO helpers


def _find_project(arg: str) -> Optional[Path]:
    path = Path(arg)
    if path.is_dir():
        return path
    candidate = _REPO_ROOT / "projects" / arg
    if candidate.is_dir():
        return candidate
    for parent in sorted((_REPO_ROOT / "projects").glob("*")):
        nested = parent / arg
        if parent.is_dir() and nested.is_dir():
            return nested
    return None


def _resolve_project(arg: str) -> Path:
    found = _find_project(arg)
    if found is not None:
        return found
    raise SystemExit(
        json.dumps(
            {
                "status": "error",
                "error": f"Project not found: {arg!r} (looked for a directory and "
                f"projects/{arg}).",
            },
            ensure_ascii=False,
        )
    )


def work_dir(project_dir: Path) -> Path:
    """Where prompts, drafts and the manifest live for this pipeline."""
    return Path(project_dir) / ".harness" / "editorial"


def _write_output_artifact(project_dir: Optional[Path], payload: dict) -> None:
    """Mirror the result to ``.harness/editorial/last_output.json``.

    Best-effort by design: the artifact must never break a command. Read it back
    from there rather than filtering captured stdout through a second
    interpreter — that is where the raya mojibake comes from on Windows.
    """
    if project_dir is None:
        return
    try:
        directory = work_dir(project_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "last_output.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover - defensive
        logger.debug("Could not write last_output.json: %s", exc)


def _emit(project_dir: Optional[Path], payload: dict) -> int:
    _write_output_artifact(project_dir, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("status") in {"ok", "cost_exceeded"} else 1


# ---------------------------------------------------------------------------
# Loading unverified work


def _load_evaluation(project_dir: Path, chunk_id: str) -> Optional[dict[str, Any]]:
    path = Path(project_dir) / "evaluations" / f"{chunk_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def collect_pending(
    project_dir: Path, scope: str, *, include_verified: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Chunks in ``scope`` carrying editorial candidates, and why others were skipped.

    A chunk is pending when it has an editorial result with at least one
    candidate and ``metadata.verified`` is false. Re-verifying an already
    adjudicated chunk needs ``--force``: the pass is not idempotent in the way
    ``apply`` is — a second adjudication re-decides retractions that the first
    one already removed from ``issues``, and it costs another call.
    """
    targets = build_targets(project_dir, scope)
    pending: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for target in targets:
        payload = _load_evaluation(project_dir, target.id)
        if payload is None:
            skipped.append({"chunk_id": target.id, "reason": "no_evaluation"})
            continue
        result = (payload.get("judges") or {}).get(JUDGE_NAME)
        if not isinstance(result, dict):
            skipped.append({"chunk_id": target.id, "reason": "judge_not_run"})
            continue
        metadata = result.get("metadata") or {}
        if metadata.get("verified") and not include_verified:
            skipped.append({"chunk_id": target.id, "reason": "already_verified"})
            continue

        candidates = ev.attach_context(
            project_dir,
            ev.collect_candidates(result, target.id, target.translated_text),
        )
        if not candidates:
            skipped.append({"chunk_id": target.id, "reason": "no_candidates"})
            continue

        pending.append(
            {
                "chunk_id": target.id,
                "chapter_id": target.context.get("chapter_id"),
                "result": result,
                "candidates": candidates,
            }
        )
    return pending, skipped


def _counts(pending: list[dict[str, Any]]) -> dict[str, int]:
    candidates = [c for entry in pending for c in entry["candidates"]]
    return {
        "chunks": len(pending),
        "candidates": len(candidates),
        "source_requested": sum(1 for c in candidates if c.get("_source_requested")),
        "source_attached": sum(1 for c in candidates if c.get("_source_available")),
    }


# ---------------------------------------------------------------------------
# Commands


def cmd_status(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    pending, skipped = collect_pending(project_dir, args.scope, include_verified=args.force)
    reasons: dict[str, int] = {}
    for entry in skipped:
        reasons[entry["reason"]] = reasons.get(entry["reason"], 0) + 1
    return _emit(
        None,
        {
            "status": "ok",
            "command": "status",
            "project": project_dir.name,
            "scope": args.scope,
            "counts": _counts(pending),
            "skipped": reasons,
            "pending_chunks": [e["chunk_id"] for e in pending],
        },
    )


def cmd_run(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    context, error = build_judge_context(project_dir, [JUDGE_NAME], args.model, args.provider)
    if error:
        return _emit(project_dir, {"status": "error", "command": "run", "error": error})

    pending, skipped = collect_pending(project_dir, args.scope, include_verified=args.force)
    counts = _counts(pending)
    if not pending:
        return _emit(
            project_dir,
            {
                "status": "ok",
                "command": "run",
                "project": project_dir.name,
                "counts": counts,
                "results": [],
                "instructions": "Nothing to adjudicate in this scope.",
            },
        )

    estimate = _estimate_cost(pending, context, args.model, args.provider)
    if estimate > args.cost_limit and not args.confirm:
        return _emit(
            project_dir,
            {
                "status": "cost_exceeded",
                "command": "run",
                "project": project_dir.name,
                "counts": counts,
                "estimated_cost_usd": round(estimate, 4),
                "cost_limit": args.cost_limit,
                "instructions": (
                    f"Estimated ${estimate:.4f} exceeds --cost-limit "
                    f"${args.cost_limit:.2f}. Re-run with --confirm to proceed, or "
                    "use prepare/fanout/commit for a subscription wave."
                ),
            },
        )

    results = []
    for entry in pending:
        patched, info = ev.verify_result(
            project_dir,
            entry["chunk_id"],
            entry["result"],
            "",  # context already attached; verify_result re-derives from candidates
            context,
            candidates=entry["candidates"],
        )
        if info.get("status") == "ok" and args.persist:
            _persist(project_dir, entry["chunk_id"], patched)
            info["persisted"] = True
        results.append(info)

    return _emit(
        project_dir,
        {
            "status": "ok",
            "command": "run",
            "project": project_dir.name,
            "backend": "api",
            "counts": counts,
            "skipped_count": len(skipped),
            "persisted": bool(args.persist),
            "results": results,
            "rollup": _rollup(results),
        },
    )


def _estimate_cost(
    pending: list[dict[str, Any]],
    context: dict[str, Any],
    model: Optional[str],
    provider: Optional[str],
) -> float:
    total = 0.0
    for entry in pending:
        prompt = ev.build_prompt(entry["candidates"], context)
        completion = _TOKENS_PER_VERDICT * max(1, len(entry["candidates"]))
        total += llm_io.estimate_call_cost(
            prompt, provider=provider, model=model, completion_tokens=completion
        )
    return total


def _rollup(results: list[dict[str, Any]]) -> dict[str, int]:
    def total(field: str) -> int:
        return sum(int(r.get(field) or 0) for r in results if r.get("status") == "ok")

    return {
        "adjudicated": total("adjudicated"),
        "confirmed": total("confirmed"),
        "reclassified": total("reclassified"),
        "retracted": total("retracted"),
        "source_attached": total("source_attached"),
        "source_used": total("source_used"),
        "parse_errors": sum(1 for r in results if r.get("status") == "parse_error"),
    }


def _persist(project_dir: Path, chunk_id: str, patched: dict[str, Any]) -> None:
    from web_ui.evaluations import merge_judge_result

    merge_judge_result(project_dir, chunk_id, JUDGE_NAME, patched)


# ---------------------------------------------------------------------------
# Draft backend: prepare / fanout / commit


def cmd_prepare(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    context, error = build_judge_context(project_dir, [JUDGE_NAME], args.model, args.provider)
    if error:
        return _emit(project_dir, {"status": "error", "command": "prepare", "error": error})

    pending, skipped = collect_pending(project_dir, args.scope, include_verified=args.force)
    directory = work_dir(project_dir)
    directory.mkdir(parents=True, exist_ok=True)

    entries = []
    preamble_path = directory / "preamble.txt"
    wrote_preamble = False

    for entry in pending:
        chunk_id = entry["chunk_id"]
        prefix, suffix = ev.build_prompt_parts(entry["candidates"], context)
        prompt_path = directory / f"{chunk_id}.verify.prompt.txt"
        body_path = directory / f"{chunk_id}.verify.body.txt"
        draft_path = directory / f"{chunk_id}.verify.draft.json"

        prompt_path.write_text(prefix + suffix, encoding="utf-8")
        if prefix:
            if not wrote_preamble:
                preamble_path.write_text(prefix, encoding="utf-8")
                wrote_preamble = True
            body_path.write_text(suffix, encoding="utf-8")
        if not args.keep_drafts and draft_path.exists():
            draft_path.unlink()

        entries.append(
            {
                "chunk_id": chunk_id,
                "chapter_id": entry["chapter_id"],
                "candidates": len(entry["candidates"]),
                "prompt_path": str(prompt_path),
                "body_path": str(body_path) if prefix else None,
                "preamble_path": str(preamble_path) if prefix else None,
                "draft_path": str(draft_path),
            }
        )

    manifest = {
        "scope": args.scope,
        "judge": JUDGE_NAME,
        "model": args.model,
        "provider": args.provider,
        "entries": entries,
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return _emit(
        project_dir,
        {
            "status": "ok",
            "command": "prepare",
            "project": project_dir.name,
            "counts": _counts(pending),
            "skipped_count": len(skipped),
            "manifest": str(directory / "manifest.json"),
            "entries": entries,
            "instructions": (
                "Either run `fanout` for a headless wave, or spawn one worker per "
                "entry: read prompt_path, write ONLY the JSON verdict object to "
                "draft_path. Then run `commit --persist`."
            ),
        },
    )


def _load_manifest(project_dir: Path) -> Optional[dict[str, Any]]:
    path = work_dir(project_dir) / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def cmd_fanout(args: argparse.Namespace) -> int:
    from src.harness import state as hstate
    from src.harness.headless import run_headless_wave
    from src.harness.profile import resolve_profile

    project_dir = _resolve_project(args.project)
    manifest = _load_manifest(project_dir)
    if manifest is None:
        return _emit(
            project_dir,
            {
                "status": "error",
                "command": "fanout",
                "error": "no editorial manifest — run `prepare` first",
            },
        )

    # One resolved profile for the wave, through the same seam every other
    # fan-out uses, so a Cursor operator gets Cursor's worker model and its
    # per-process baseline rather than a Claude alias. ``command="judges"``
    # because this is a judge wave for effort-config purposes
    # (``headless_effort_judges``); only the usage log is its own, so the
    # baseline self-calibrates on adjudication jobs rather than on pass-1 ones,
    # which are a different shape.
    cfg = hstate.load_config(project_dir)
    profile = resolve_profile(
        project_dir,
        command="judges",
        cli=args.cli,
        worker_model=args.worker_model,
        effort=args.effort,
        cfg=cfg,
        usage_log=work_dir(project_dir) / "usage.jsonl",
    )
    cli_name = profile.cli
    worker_model = profile.worker_model
    effort = profile.effort
    extra_flags = hstate.compose_headless_argv(
        cfg, effort if profile.effort_channel == "argv" else None
    )
    for warning in profile.warnings:
        print(warning, file=sys.stderr)

    jobs, skipped = [], []
    for entry in manifest.get("entries") or []:
        draft_path = Path(entry["draft_path"])
        if draft_path.exists() and draft_path.read_text(encoding="utf-8").strip():
            skipped.append(entry["chunk_id"])
            continue
        preamble = entry.get("preamble_path")
        body = entry.get("body_path")
        if preamble and body and Path(preamble).exists() and Path(body).exists():
            jobs.append(
                {
                    "id": entry["chunk_id"],
                    "input_text": Path(body).read_text(encoding="utf-8"),
                    "output_path": str(draft_path),
                    "system_prompt_file": preamble,
                }
            )
        else:
            jobs.append(
                {
                    "id": entry["chunk_id"],
                    "input_text": Path(entry["prompt_path"]).read_text(encoding="utf-8"),
                    "output_path": str(draft_path),
                    "system_prompt_file": None,
                }
            )

    if not jobs:
        return _emit(
            project_dir,
            {
                "status": "ok",
                "command": "fanout",
                "project": project_dir.name,
                "profile": profile.to_payload(),
                "wrote": [],
                "skipped": skipped,
                "instructions": "Nothing to fan out — run `commit --persist`.",
            },
        )

    wave = run_headless_wave(
        jobs,
        model=worker_model,
        concurrency=args.concurrency,
        cli=cli_name,
        cli_bin=args.cli_bin,
        usage_log=work_dir(project_dir) / "usage.jsonl",
        extra_flags=extra_flags,
        effort=effort,
    )
    return _emit(
        project_dir,
        {
            "status": "error" if wave.get("error") else "ok",
            "command": "fanout",
            "project": project_dir.name,
            "profile": profile.to_payload(),
            "skipped": skipped,
            **{k: v for k, v in wave.items() if k != "_schema"},
            "instructions": "Run `commit --persist` to land the verdicts.",
        },
    )


def cmd_commit(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    manifest = _load_manifest(project_dir)
    if manifest is None:
        return _emit(
            project_dir,
            {
                "status": "error",
                "command": "commit",
                "error": "no editorial manifest — run `prepare` first",
            },
        )

    context, _ = build_judge_context(project_dir, [JUDGE_NAME], args.model, args.provider)
    results, failed, missing = [], [], []

    for entry in manifest.get("entries") or []:
        chunk_id = entry["chunk_id"]
        draft_path = Path(entry["draft_path"])
        if not draft_path.exists():
            missing.append(chunk_id)
            continue
        raw = draft_path.read_text(encoding="utf-8").strip()
        if not raw:
            missing.append(chunk_id)
            continue

        payload = _load_evaluation(project_dir, chunk_id)
        result = (payload or {}).get("judges", {}).get(JUDGE_NAME)
        if not isinstance(result, dict):
            failed.append({"chunk_id": chunk_id, "error": "editorial result gone"})
            continue

        targets = build_targets(project_dir, f"chunk:{chunk_id}")
        translated = targets[0].translated_text if targets else ""
        candidates = ev.attach_context(
            project_dir, ev.collect_candidates(result, chunk_id, translated)
        )
        try:
            verdicts = ev.parse_verdicts(raw)
        except llm_io.JudgeParseError as exc:
            failed.append({"chunk_id": chunk_id, "error": str(exc)})
            continue

        patched = ev.apply_verdicts(result, candidates, verdicts)
        metadata = patched.get("metadata") or {}
        if args.persist:
            _persist(project_dir, chunk_id, patched)
        results.append(
            {
                "chunk_id": chunk_id,
                "status": "ok",
                "adjudicated": metadata.get("candidates_adjudicated"),
                "confirmed": metadata.get("confirmed"),
                "reclassified": metadata.get("reclassified"),
                "retracted": metadata.get("retracted_count"),
                "source_attached": metadata.get("source_attached"),
                "source_used": metadata.get("source_used"),
                "persisted": bool(args.persist),
            }
        )

    return _emit(
        project_dir,
        {
            "status": "ok",
            "command": "commit",
            "project": project_dir.name,
            "backend": "draft",
            "persisted": bool(args.persist),
            "committed": len(results),
            "failed": failed,
            "missing": missing,
            "results": [] if args.brief else results,
            "rollup": _rollup(results),
            "instructions": (
                "Re-spawn the failed/missing entries and re-run `commit`."
                if (failed or missing)
                else "Done. Findings in evaluations/<chunk>.json are adjudicated."
            ),
        },
    )


# ---------------------------------------------------------------------------
# argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, *, scope: bool = True) -> None:
        p.add_argument("--project", required=True, help="Project id (under projects/) or path")
        if scope:
            p.add_argument("--scope", default="book", help="chunk:<id>, chapter:<id> or book")
        p.add_argument("--model", default=None, help="Adjudicator model override")
        p.add_argument("--provider", default=None, help="Adjudicator provider override")
        p.add_argument("--verbose", action="store_true", help="Enable debug logging")

    p_status = sub.add_parser("status", help="read-only: which chunks await adjudication")
    common(p_status)
    p_status.add_argument("--force", action="store_true", help="Include already-verified chunks")

    p_run = sub.add_parser("run", help="API backend: adjudicate now, one call per chunk")
    common(p_run)
    p_run.add_argument("--persist", action="store_true", help="Write the adjudicated result")
    p_run.add_argument("--confirm", action="store_true", help="Proceed past the cost gate")
    p_run.add_argument("--cost-limit", type=float, default=0.50, help="Max estimated USD")
    p_run.add_argument("--force", action="store_true", help="Re-verify already-verified chunks")

    p_prepare = sub.add_parser("prepare", help="draft backend: render prompts + manifest")
    common(p_prepare)
    p_prepare.add_argument("--keep-drafts", action="store_true", help="Do not clear old drafts")
    p_prepare.add_argument("--force", action="store_true", help="Re-verify verified chunks")

    p_fanout = sub.add_parser("fanout", help="draft backend: headless wave over the manifest")
    common(p_fanout, scope=False)
    p_fanout.add_argument("--cli", default=None, choices=["claude", "cursor"])
    p_fanout.add_argument("--cli-bin", default=None, help="Path to the CLI binary")
    p_fanout.add_argument("--worker-model", default=None, help="Pin the worker model")
    p_fanout.add_argument("--concurrency", type=int, default=None)
    p_fanout.add_argument("--effort", default=None, help="Per-run effort override")

    p_commit = sub.add_parser("commit", help="draft backend: parse drafts and persist")
    common(p_commit, scope=False)
    p_commit.add_argument("--persist", action="store_true", help="Write the adjudicated results")
    p_commit.add_argument("--brief", action="store_true", help="Drop results[] from stdout")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    handlers = {
        "status": cmd_status,
        "run": cmd_run,
        "prepare": cmd_prepare,
        "fanout": cmd_fanout,
        "commit": cmd_commit,
    }
    try:
        return handlers[args.command](args)
    except ScopeError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
