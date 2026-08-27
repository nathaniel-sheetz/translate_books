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

Five subcommands:

  * ``status``  — **read-only**. Which chunks carry unverified candidates, and
                  with ``--drafts``, how far a prepared wave has got. No spend,
                  no writes.
  * ``run``     — **API backend**. Adjudicate now, one call per chunk, behind a
                  dollar cost gate.
  * ``prepare`` — **draft backend**, phase 1. Render one prompt file per chunk
                  plus a manifest, for a headless wave or spawned workers. Zero
                  spend, and the wave's **consent gate**: it returns
                  ``effective`` + ``usage_summary``, and takes the ``--cli`` /
                  ``--worker-model`` / ``--effort`` that decide them.
  * ``fanout``  — **draft backend**, opt-in headless wave over the manifest.
                  Inherits the profile ``prepare`` consented to; the flags are
                  overrides, not the primary knobs.
  * ``commit``  — **draft backend**, phase 2. Parse the drafts and persist.

Every command prints exactly one JSON object; every command but ``status``
mirrors it to ``<project>/.harness/editorial/last_output.json`` and prints an
``OUTPUT_JSON:`` pointer to stderr.

Examples:
    python scripts/verify_editorial.py status  --project pollyanna
    python scripts/verify_editorial.py status  --project pollyanna --drafts
    python scripts/verify_editorial.py run     --project pollyanna \\
        --scope chapter:chapter_01 --persist --confirm
    python scripts/verify_editorial.py prepare --project pollyanna --scope book \\
        --cli cursor --quiet
    python scripts/verify_editorial.py fanout  --project pollyanna
    python scripts/verify_editorial.py commit  --project pollyanna --persist
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Windows captured stdout defaults to the locale codec (cp1252), which mangles
# every raya/guillemet/accent in the JSON we print — and pass 2 quotes the same
# Spanish excerpts pass 1 does. ``run_judges.py`` forces UTF-8 for exactly this
# reason; the hasattr guard keeps it safe under pytest's captured streams.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

# Silence the urllib3/chardet version-mismatch warning ``requests`` emits at
# import time. Every pass-2 command used to open with a RequestsDependencyWarning
# — the same "stderr that is not an error" the judges CLI already paid to kill,
# and every such line trains a reader to skim the stream real progress goes to.
warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

from src.harness import state as hstate  # noqa: E402
from src.harness.usage import approx_tokens, baseline_tokens  # noqa: E402
from src.judges import editorial_verify as ev  # noqa: E402
from src.judges import llm_io  # noqa: E402
from src.judges.context import build_judge_context  # noqa: E402
from src.judges.scope import ScopeError, build_targets  # noqa: E402

logger = logging.getLogger(__name__)

JUDGE_NAME = "editorial"
_NO_SIDECAR_COMMANDS = frozenset({"status"})

#: Parallel headless processes when neither ``--concurrency`` nor the manifest
#: says. ``None`` used to be handed straight to ``run_headless_wave``, which
#: does ``if concurrency < 1`` — so the *documented* bare ``fanout`` was a
#: TypeError before job 1. Every sibling launcher resolves this before the call.
_DEFAULT_CONCURRENCY = 5

#: Completion-token allowance per candidate when estimating cost. An
#: adjudication verdict is a decision plus one sentence of reason, occasionally
#: a corrected fix — far shorter than a judge's finding, which carries an
#: excerpt and a suggestion.
_TOKENS_PER_VERDICT = 120


# ---------------------------------------------------------------------------
# Project + IO helpers


def _resolve_project(arg: str) -> Path:
    """Project dir for ``--project``: a path, a flat slug, or a nested one.

    Routed through :func:`harness.state.resolve_project_dir` rather than a local
    lookup, so a book filed under a grouping folder
    (``projects/.macdonald/photogen-nycteris``) answers to its bare slug here
    exactly as it does everywhere else in the harness. The local version globbed
    one level, and ``glob("*")`` skips dot-directories — which is precisely where
    the grouped books live, so the first command of every session 404'd.
    """
    try:
        return hstate.resolve_project_dir(arg)
    except FileNotFoundError as exc:
        raise SystemExit(
            json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
        ) from exc


def work_dir(project_dir: Path) -> Path:
    """Where prompts, drafts and the manifest live for this pipeline."""
    return Path(project_dir) / ".harness" / "editorial"


def _usage_log(project_dir: Path) -> Path:
    """This pipeline's own per-job usage rows."""
    return work_dir(project_dir) / "usage.jsonl"


def _baseline_for(project_dir: Path, cli: str) -> tuple[int, str]:
    """Per-job token overhead for the consent gate, and where the number is from.

    Adjudication keeps its own ``usage.jsonl`` on purpose: a verdict-shaped job
    is a different shape from a pass-1 finding-shaped one, so the estimate
    self-calibrates on its own history rather than on the judge's. But on a
    book's *first* adjudication wave that history is empty, and the constant it
    falls back to is a probe taken on another machine — while
    ``.harness/judges/usage.jsonl`` next door holds dozens of measured rows for
    the same CLI on this box (photogen: 39 rows at 18,362 against a 17,200
    constant). Borrow that rather than quote the constant, and label it as
    borrowed. The fallback stops firing on its own once adjudication has logged
    enough jobs of its own.
    """
    tokens, source = baseline_tokens(_usage_log(project_dir), cli=cli)
    if source.startswith("default:"):
        alt, alt_source = baseline_tokens(
            Path(project_dir) / ".harness" / "judges" / "usage.jsonl", cli=cli
        )
        if not alt_source.startswith("default:"):
            return alt, f"{alt_source} (pass-1 log; no adjudication rows yet)"
    return tokens, source


def _draft_written(entry: dict[str, Any]) -> bool:
    """Has this manifest entry been answered? (the test ``fanout`` itself applies)"""
    path = Path(entry.get("draft_path") or "")
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


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
        out = directory / "last_output.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        # Say where it landed, on stderr, the way ``run_judges.py`` does. Without
        # the pointer a caller has to match on the JSON body to know a command
        # finished, and has to guess the path to read the full payload back.
        print(f"OUTPUT_JSON: {out}", file=sys.stderr)
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


def _draft_progress(project_dir: Path) -> dict[str, Any]:
    """Manifest entries with a draft vs without — "is the wave still going?".

    A read-only answer to the question that otherwise costs a directory listing
    sorted by mtime and a guess. On 2026-08-26 an announced wave had never
    started, and it took eight minutes and a user prompt to notice; the giveaway
    would have been six prepared entries, zero drafts, and a manifest whose
    mtime had not moved.
    """
    manifest = _load_manifest(project_dir)
    if manifest is None:
        return {"manifest_at": None, "note": "no manifest — run `prepare` first"}
    entries = manifest.get("entries") or []
    pending = [e["chunk_id"] for e in entries if not _draft_written(e)]
    path = work_dir(project_dir) / "manifest.json"
    try:
        at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:  # pragma: no cover - defensive
        at = None
    return {
        "manifest_at": at,
        "drafts": {"written": len(entries) - len(pending), "pending": len(pending)},
        "pending_ids": pending,
        "note": (
            "every prepared entry has a draft — run `commit --persist`"
            if not pending
            else f"{len(pending)} of {len(entries)} entries have no draft yet"
        ),
    }


def cmd_status(args: argparse.Namespace) -> int:
    project_dir = _resolve_project(args.project)
    pending, skipped = collect_pending(project_dir, args.scope, include_verified=args.force)
    reasons: dict[str, int] = {}
    for entry in skipped:
        reasons[entry["reason"]] = reasons.get(entry["reason"], 0) + 1
    payload = {
        "status": "ok",
        "command": "status",
        "project": project_dir.name,
        "scope": args.scope,
        "counts": _counts(pending),
        "skipped": reasons,
        "pending_chunks": [e["chunk_id"] for e in pending],
    }
    if args.drafts:
        payload["wave"] = _draft_progress(project_dir)
    return _emit(None, payload)


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
    from src.harness.profile import resolve_profile

    project_dir = _resolve_project(args.project)
    context, error = build_judge_context(project_dir, [JUDGE_NAME], args.model, args.provider)
    if error:
        return _emit(project_dir, {"status": "error", "command": "prepare", "error": error})

    # The consent gate lives here now, not on ``fanout``. Pass 1 puts ``--cli``
    # on ``prepare``; pass 2 putting it on ``fanout`` meant a Cursor operator had
    # to remember which sibling took which flag — and meant the wave was
    # approved, when it was approved at all, on no number: ``prepare`` returned
    # paths and candidate counts and nothing about what answering them costs.
    cfg = hstate.load_config(project_dir)
    prof = resolve_profile(
        project_dir,
        command="judges",
        cli=args.cli,
        worker_model=args.worker_model,
        effort=args.effort,
        cfg=cfg,
        usage_log=_usage_log(project_dir),
    )

    pending, skipped = collect_pending(project_dir, args.scope, include_verified=args.force)
    directory = work_dir(project_dir)
    directory.mkdir(parents=True, exist_ok=True)

    entries = []
    prompt_tokens = 0
    api_cost = 0.0
    preamble_path = directory / "preamble.txt"
    wrote_preamble = False

    for entry in pending:
        chunk_id = entry["chunk_id"]
        prefix, suffix = ev.build_prompt_parts(entry["candidates"], context)
        prompt_path = directory / f"{chunk_id}.verify.prompt.txt"
        body_path = directory / f"{chunk_id}.verify.body.txt"
        draft_path = directory / f"{chunk_id}.verify.draft.json"

        prompt_path.write_text(prefix + suffix, encoding="utf-8")
        # What a worker actually receives, and what the same call would cost
        # metered. Both come off the parts already in hand: ``prefix + suffix``
        # is byte-identical to ``build_prompt``, so re-rendering to price it
        # would be duplicate work.
        prompt_tokens += approx_tokens(prefix + suffix)
        api_cost += llm_io.estimate_call_cost(
            prefix + suffix,
            provider=args.provider,
            model=args.model,
            completion_tokens=_TOKENS_PER_VERDICT * max(1, len(entry["candidates"])),
        )
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
        # The resolved profile, so a bare ``fanout`` reproduces the wave the
        # operator consented to instead of re-resolving from scratch — and so a
        # wrong pin is visible on disk, not only in a payload that scrolled away.
        "cli": prof.cli,
        "worker_model": prof.worker_model,
        "effort": prof.effort,
        "effort_channel": prof.effort_channel,
        "batch_size": _DEFAULT_CONCURRENCY,
        "entries": entries,
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    baseline, baseline_source = _baseline_for(project_dir, prof.cli)
    effective = prof.to_payload()
    # The profile resolved its baseline against this pipeline's own (usually
    # empty) log; ``_baseline_for`` is allowed to borrow pass 1's. Patch the
    # payload rather than the profile so both halves of the gate quote one
    # number.
    effective["baseline_tokens"] = baseline
    effective["baseline_source"] = baseline_source

    # Also on stderr: a mis-resolved CLI is worth seeing even when the caller
    # only skims stdout, and this is the last moment before the wave.
    for warning in prof.warnings:
        print(f"[prepare] warning: {warning}", file=sys.stderr)

    counts = _counts(pending)
    payload = {
        "status": "ok",
        "command": "prepare",
        "project": project_dir.name,
        "counts": counts,
        "skipped_count": len(skipped),
        "manifest": str(directory / "manifest.json"),
        "effective": effective,
        "usage_summary": {
            "chunks": counts["chunks"],
            "candidates": counts["candidates"],
            "source_requested": counts["source_requested"],
            "cli": prof.cli,
            "worker_model": prof.worker_model,
            "batch_size": _DEFAULT_CONCURRENCY,
            "estimated_api_cost": round(api_cost, 6),
            "estimated_prompt_tokens": prompt_tokens,
            "headless_baseline_tokens": baseline,
            "headless_baseline_source": baseline_source,
            "estimated_headless_tokens": prompt_tokens + len(entries) * baseline,
            "headless_effort": prof.effort,
            "headless_effort_source": prof.effort_source,
            "headless_effort_channel": prof.effort_channel,
        },
        "warnings": list(prof.warnings),
        "instructions": (
            "Relay `effective` + `usage_summary` and get approval, THEN run "
            "`fanout` for a headless wave — or spawn one worker per entry: read "
            "prompt_path, write ONLY the JSON verdict object to draft_path. Then "
            "run `commit --persist`."
            if entries
            else "Nothing to adjudicate in this scope."
        ),
    }
    if not args.quiet:
        payload["entries"] = entries
    return _emit(project_dir, payload)


def _load_manifest(project_dir: Path) -> Optional[dict[str, Any]]:
    path = work_dir(project_dir) / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def cmd_fanout(args: argparse.Namespace) -> int:
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
    #
    # Resolved against the *manifest* first: ``prepare`` wrote the CLI, model and
    # effort the operator consented to, so a bare ``fanout`` reproduces that wave
    # exactly. Flags still win, for the one-flag correction of a bad pin. Effort
    # is inherited only when the CLI has not flipped — a level resolved for
    # Claude is a Claude-table number, and carrying it onto Cursor would write an
    # ``[effort=…]`` bracket the resolver deliberately refuses to invent.
    cfg = hstate.load_config(project_dir)
    inherited_effort, inherited_effort_source = args.effort, "cli"
    cli_unchanged = (args.cli or manifest.get("cli")) == manifest.get("cli")
    if not args.effort and "effort" in manifest and cli_unchanged:
        inherited_effort = manifest["effort"] or "default"
        inherited_effort_source = "manifest"
    profile = resolve_profile(
        project_dir,
        command="judges",
        cli=args.cli or manifest.get("cli"),
        cli_source="cli" if args.cli else "manifest",
        worker_model=args.worker_model or manifest.get("worker_model"),
        worker_model_source="cli" if args.worker_model else "manifest",
        effort=inherited_effort,
        effort_source=inherited_effort_source,
        cfg=cfg,
        usage_log=_usage_log(project_dir),
    )
    cli_name = profile.cli
    worker_model = profile.worker_model
    effort = profile.effort
    extra_flags = hstate.compose_headless_argv(
        cfg, effort if profile.effort_channel == "argv" else None
    )
    for warning in profile.warnings:
        print(warning, file=sys.stderr)

    # An override that changes the wave must reach the manifest — ``commit``
    # and any later bare ``fanout`` both read it back. Profile fields only:
    # entries and drafts are untouched, so this is not the destructive
    # re-``prepare``.
    if (
        manifest.get("cli") != profile.cli
        or manifest.get("worker_model") != profile.worker_model
        or manifest.get("effort") != profile.effort
    ):
        manifest["cli"] = profile.cli
        manifest["worker_model"] = profile.worker_model
        manifest["effort"] = profile.effort
        manifest["effort_channel"] = profile.effort_channel
        try:
            (work_dir(project_dir) / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:  # pragma: no cover - disk failure
            print(f"could not update the editorial manifest: {exc}", file=sys.stderr)

    # Resolve the wave width BEFORE the launcher sees it. ``run_headless_wave``
    # does ``if concurrency < 1``, so handing it the argparse default of ``None``
    # is a TypeError before job 1 — which is exactly what the documented bare
    # ``fanout`` did. Every sibling launcher resolves this first; this one did
    # not, and its test stubs the launcher, so CI never ran the comparison.
    concurrency = args.concurrency
    if concurrency is None:
        try:
            concurrency = int(manifest.get("batch_size") or _DEFAULT_CONCURRENCY)
        except (TypeError, ValueError):
            concurrency = _DEFAULT_CONCURRENCY
    if concurrency < 1:
        return _emit(
            project_dir,
            {
                "status": "error",
                "command": "fanout",
                "project": project_dir.name,
                "error": f"invalid concurrency {concurrency!r}; must be >= 1",
            },
        )

    jobs, skipped = [], []
    for entry in manifest.get("entries") or []:
        draft_path = Path(entry["draft_path"])
        if _draft_written(entry):
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
                "concurrency": concurrency,
                "wrote": [],
                "skipped": skipped,
                "instructions": "Nothing to fan out — run `commit --persist`.",
            },
        )

    wave = run_headless_wave(
        jobs,
        model=worker_model,
        concurrency=concurrency,
        cli=cli_name,
        cli_bin=args.cli_bin,
        usage_log=_usage_log(project_dir),
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
            "concurrency": concurrency,
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
    p_status.add_argument(
        "--drafts",
        action="store_true",
        help="Also report manifest entries with/without a draft (is the wave still going?)",
    )

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
    # The same four ``run_judges.py prepare`` carries. This is the consent
    # surface: whichever CLI is going to run the wave has to be known here, or
    # the tokens quoted are for a wave that never happens.
    p_prepare.add_argument("--cli", default=None, choices=["claude", "cursor"])
    p_prepare.add_argument("--worker-model", default=None, help="Pin the worker model")
    p_prepare.add_argument("--effort", default=None, help="Per-run effort override")
    p_prepare.add_argument(
        "--quiet",
        action="store_true",
        help="Omit entries[] from stdout, keeping manifest + effective + usage_summary",
    )

    p_fanout = sub.add_parser("fanout", help="draft backend: headless wave over the manifest")
    common(p_fanout, scope=False)
    p_fanout.add_argument("--cli", default=None, choices=["claude", "cursor"])
    p_fanout.add_argument("--cli-bin", default=None, help="Path to the CLI binary")
    p_fanout.add_argument("--worker-model", default=None, help="Pin the worker model")
    p_fanout.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Max parallel headless processes "
        f"(default: the manifest's batch_size, else {_DEFAULT_CONCURRENCY})",
    )
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
