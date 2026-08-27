"""The editorial judge's adjudication pass, as callable functions.

:mod:`src.judges.editorial_verify` holds the three backend-agnostic seams
(``build_prompt`` / ``parse_verdicts`` / ``apply_verdicts``). This module holds
the *wave* around them — collecting unverified work, rendering a manifest,
launching a headless fan-out, and landing the drafts — in the same relationship
:mod:`src.judges.subagent` has with ``scripts/run_judges.py``.

It exists because ``scripts/verify_editorial.py`` was the only way to run pass 2,
and its ``cmd_*`` functions take an ``argparse.Namespace``, print JSON and return
an exit code. The web UI never shells out, so it needs the same work as
dict-returning calls. The CLI is now a thin adapter over this module: every
function here returns exactly the payload the CLI prints, so the two paths
cannot drift.

Two differences from the CLI's own shape, both for the GUI's benefit:

* ``scopes`` is a **list**. The Review tab runs a judge over a set of ticked
  chapters, and one untranslated chapter in that set is a reason to leave it out
  rather than to refuse the whole run — the same skip-not-abort rule
  ``web_ui.app.project_review_run_judges`` already applies to pass 1.
* :func:`fanout` threads ``on_job_done`` and ``cache`` into the launcher. The
  first drives a progress bar, the second carries the operator's prompt-cache
  choice to the second wave; ``run_headless_wave`` has always accepted both.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from src.harness.usage import approx_tokens, baseline_tokens
from src.judges import editorial_verify as ev
from src.judges import llm_io
from src.judges.context import build_judge_context
from src.judges.scope import ScopeError, build_targets

logger = logging.getLogger(__name__)

JUDGE_NAME = "editorial"

#: Parallel headless processes when neither the caller nor the manifest says.
#: ``None`` used to be handed straight to ``run_headless_wave``, which does
#: ``if concurrency < 1`` — so the *documented* bare ``fanout`` was a TypeError
#: before job 1. Every sibling launcher resolves this before the call.
DEFAULT_CONCURRENCY = 5

#: Completion-token allowance per candidate when estimating cost. An
#: adjudication verdict is a decision plus one sentence of reason, occasionally
#: a corrected fix — far shorter than a judge's finding, which carries an
#: excerpt and a suggestion.
TOKENS_PER_VERDICT = 120


# ---------------------------------------------------------------------------
# Paths


def work_dir(project_dir: Path) -> Path:
    """Where prompts, drafts and the manifest live for this pipeline."""
    return Path(project_dir) / ".harness" / "editorial"


def usage_log(project_dir: Path) -> Path:
    """This pipeline's own per-job usage rows."""
    return work_dir(project_dir) / "usage.jsonl"


def baseline_for(project_dir: Path, cli: str) -> tuple[int, str]:
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
    tokens, source = baseline_tokens(usage_log(project_dir), cli=cli)
    if source.startswith("default:"):
        alt, alt_source = baseline_tokens(
            Path(project_dir) / ".harness" / "judges" / "usage.jsonl", cli=cli
        )
        if not alt_source.startswith("default:"):
            return alt, f"{alt_source} (pass-1 log; no adjudication rows yet)"
    return tokens, source


def draft_written(entry: dict[str, Any]) -> bool:
    """Has this manifest entry been answered? (the test ``fanout`` itself applies)"""
    path = Path(entry.get("draft_path") or "")
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def load_manifest(project_dir: Path) -> Optional[dict[str, Any]]:
    path = work_dir(project_dir) / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _scope_repr(scopes: list[str]) -> Any:
    """What goes in a payload's ``scope`` field.

    A single scope stays a bare string so the CLI's output is byte-identical to
    what it printed before this module existed; a multi-scope run (only the GUI
    makes one) reports the list it was given.
    """
    return scopes[0] if len(scopes) == 1 else list(scopes)


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
    project_dir: Path, scopes: list[str], *, include_verified: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Chunks in ``scopes`` carrying editorial candidates, and why others were skipped.

    A chunk is pending when it has an editorial result with at least one
    candidate and ``metadata.verified`` is false. Re-verifying an already
    adjudicated chunk needs ``force``: the pass is not idempotent in the way
    ``apply`` is — a second adjudication re-decides retractions that the first
    one already removed from ``issues``, and it costs another call.

    A scope that cannot be resolved — an untranslated chapter ticked in the
    Review table — is reported as a skip rather than raising, so one bad entry
    in a multi-chapter selection does not refuse the whole run. But when *every*
    scope fails the error is re-raised: a single-scope caller (the CLI, which
    always passes one) must still fail loudly on a typo rather than report a
    cheerful "nothing to adjudicate".
    """
    pending: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()

    targets = []
    errors: list[Exception] = []
    for scope in scopes:
        try:
            for target in build_targets(project_dir, scope):
                if target.id in seen:
                    continue
                seen.add(target.id)
                targets.append(target)
        except (ScopeError, NotImplementedError, FileNotFoundError, ValueError) as exc:
            errors.append(exc)
            skipped.append({"scope": scope, "reason": "scope_error", "error": str(exc)})
    if errors and len(errors) == len(scopes):
        raise errors[0]

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


def counts(pending: list[dict[str, Any]]) -> dict[str, int]:
    candidates = [c for entry in pending for c in entry["candidates"]]
    return {
        "chunks": len(pending),
        "candidates": len(candidates),
        "source_requested": sum(1 for c in candidates if c.get("_source_requested")),
        "source_attached": sum(1 for c in candidates if c.get("_source_available")),
    }


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


def _context_for(
    project_dir: Path,
    context: Optional[dict[str, Any]],
    model: Optional[str],
    provider: Optional[str],
) -> tuple[dict[str, Any], Optional[str]]:
    """The caller's context, or a freshly built one.

    The GUI already builds a judge context for pass 1 and it carries everything
    :func:`src.judges.editorial_verify.build_prompt` reads (style guide and
    glossary), so re-deriving it there would re-walk ``evaluations/`` for a
    do-not-repeat list pass 2 never renders.
    """
    if context is not None:
        return context, None
    return build_judge_context(project_dir, [JUDGE_NAME], model, provider)


# ---------------------------------------------------------------------------
# status


def draft_progress(project_dir: Path) -> dict[str, Any]:
    """Manifest entries with a draft vs without — "is the wave still going?".

    A read-only answer to the question that otherwise costs a directory listing
    sorted by mtime and a guess. On 2026-08-26 an announced wave had never
    started, and it took eight minutes and a user prompt to notice; the giveaway
    would have been six prepared entries, zero drafts, and a manifest whose
    mtime had not moved.
    """
    manifest = load_manifest(project_dir)
    if manifest is None:
        return {"manifest_at": None, "note": "no manifest — run `prepare` first"}
    entries = manifest.get("entries") or []
    pending = [e["chunk_id"] for e in entries if not draft_written(e)]
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


def status(
    project_dir: Path,
    scopes: list[str],
    *,
    include_verified: bool = False,
    drafts: bool = False,
) -> dict[str, Any]:
    """Read-only: which chunks await adjudication, and optionally the wave's progress."""
    pending, skipped = collect_pending(
        project_dir, scopes, include_verified=include_verified
    )
    reasons: dict[str, int] = {}
    for entry in skipped:
        reasons[entry["reason"]] = reasons.get(entry["reason"], 0) + 1
    payload = {
        "status": "ok",
        "command": "status",
        "project": project_dir.name,
        "scope": _scope_repr(scopes),
        "counts": counts(pending),
        "skipped": reasons,
        "pending_chunks": [e["chunk_id"] for e in pending],
    }
    if drafts:
        payload["wave"] = draft_progress(project_dir)
    return payload


# ---------------------------------------------------------------------------
# API backend


def estimate_cost(
    pending: list[dict[str, Any]],
    context: dict[str, Any],
    model: Optional[str],
    provider: Optional[str],
) -> float:
    total = 0.0
    for entry in pending:
        prompt = ev.build_prompt(entry["candidates"], context)
        completion = TOKENS_PER_VERDICT * max(1, len(entry["candidates"]))
        total += llm_io.estimate_call_cost(
            prompt, provider=provider, model=model, completion_tokens=completion
        )
    return total


def estimate_cost_bound(
    project_dir: Path,
    chunk_word_counts: dict[str, int],
    context: dict[str, Any],
    model: Optional[str],
    provider: Optional[str],
) -> float:
    """What pass 2 could cost over chunks pass 1 has not judged yet.

    Called *before* pass 1 runs, so there are no candidates to price. The bound
    assumes every chunk comes back carrying its full findings budget — real
    chunks average one or two findings against a floor of two, so this reads
    high, which is the only safe direction for a spend gate.

    Priced off the parts already in hand: ``build_prompt([], context)`` renders
    the shared prefix without an LLM call, and each hypothetical candidate adds
    a fixed block allowance plus its verdict's completion tokens.
    """
    from src.judges.editorial_judge import findings_budget

    prefix = ev.build_prompt([], context)
    total = 0.0
    for words in chunk_word_counts.values():
        budget = findings_budget(words)
        padded = prefix + ("x" * _CANDIDATE_BLOCK_CHARS * budget)
        total += llm_io.estimate_call_cost(
            padded,
            provider=provider,
            model=model,
            completion_tokens=TOKENS_PER_VERDICT * max(1, budget),
        )
    return total


#: Characters a rendered candidate block occupies in the adjudication prompt —
#: the excerpt, the message, the suggestion, and the Spanish (and sometimes
#: English) window around it. Used only by :func:`estimate_cost_bound`, where a
#: generous constant is the point.
_CANDIDATE_BLOCK_CHARS = 1800


def run_api(
    project_dir: Path,
    scopes: list[str],
    *,
    context: Optional[dict[str, Any]] = None,
    persist: bool = False,
    confirm: bool = False,
    force: bool = False,
    cost_limit: float = 0.50,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> dict[str, Any]:
    """Metered backend: adjudicate now, one call per chunk, behind a dollar gate."""
    context, error = _context_for(project_dir, context, model, provider)
    if error:
        return {"status": "error", "command": "run", "error": error}

    pending, skipped = collect_pending(project_dir, scopes, include_verified=force)
    totals = counts(pending)
    if not pending:
        return {
            "status": "ok",
            "command": "run",
            "project": project_dir.name,
            "counts": totals,
            "results": [],
            "instructions": "Nothing to adjudicate in this scope.",
        }

    estimate = estimate_cost(pending, context, model, provider)
    if estimate > cost_limit and not confirm:
        return {
            "status": "cost_exceeded",
            "command": "run",
            "project": project_dir.name,
            "counts": totals,
            "estimated_cost_usd": round(estimate, 4),
            "cost_limit": cost_limit,
            "instructions": (
                f"Estimated ${estimate:.4f} exceeds --cost-limit "
                f"${cost_limit:.2f}. Re-run with --confirm to proceed, or "
                "use prepare/fanout/commit for a subscription wave."
            ),
        }

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
        if info.get("status") == "ok" and persist:
            _persist(project_dir, entry["chunk_id"], patched)
            info["persisted"] = True
        results.append(info)

    return {
        "status": "ok",
        "command": "run",
        "project": project_dir.name,
        "backend": "api",
        "counts": totals,
        "skipped_count": len(skipped),
        "persisted": bool(persist),
        "results": results,
        "rollup": _rollup(results),
    }


# ---------------------------------------------------------------------------
# Draft backend: prepare / fanout / commit


def prepare(
    project_dir: Path,
    scopes: list[str],
    *,
    context: Optional[dict[str, Any]] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    cli: Optional[str] = None,
    worker_model: Optional[str] = None,
    effort: Optional[str] = None,
    force: bool = False,
    keep_drafts: bool = False,
    quiet: bool = False,
) -> dict[str, Any]:
    """Render one prompt file per pending chunk, plus a manifest. Zero spend.

    This is the wave's **consent gate**. Pass 1 puts ``--cli`` on ``prepare``;
    pass 2 putting it on ``fanout`` meant a Cursor operator had to remember which
    sibling took which flag — and meant the wave was approved, when it was
    approved at all, on no number: ``prepare`` returned paths and candidate
    counts and nothing about what answering them costs.

    Destructive: it clears the drafts it re-renders unless ``keep_drafts``.
    """
    from src.harness import state as hstate
    from src.harness.profile import resolve_profile

    context, error = _context_for(project_dir, context, model, provider)
    if error:
        return {"status": "error", "command": "prepare", "error": error}

    cfg = hstate.load_config(project_dir)
    prof = resolve_profile(
        project_dir,
        command="judges",
        cli=cli,
        worker_model=worker_model,
        effort=effort,
        cfg=cfg,
        usage_log=usage_log(project_dir),
    )

    pending, skipped = collect_pending(project_dir, scopes, include_verified=force)
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
            provider=provider,
            model=model,
            completion_tokens=TOKENS_PER_VERDICT * max(1, len(entry["candidates"])),
        )
        if prefix:
            if not wrote_preamble:
                preamble_path.write_text(prefix, encoding="utf-8")
                wrote_preamble = True
            body_path.write_text(suffix, encoding="utf-8")
        if not keep_drafts and draft_path.exists():
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
        "scope": _scope_repr(scopes),
        "judge": JUDGE_NAME,
        "model": model,
        "provider": provider,
        # The resolved profile, so a bare ``fanout`` reproduces the wave the
        # operator consented to instead of re-resolving from scratch — and so a
        # wrong pin is visible on disk, not only in a payload that scrolled away.
        "cli": prof.cli,
        "worker_model": prof.worker_model,
        "effort": prof.effort,
        "effort_channel": prof.effort_channel,
        "batch_size": DEFAULT_CONCURRENCY,
        "entries": entries,
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    baseline, baseline_source = baseline_for(project_dir, prof.cli)
    effective = prof.to_payload()
    # The profile resolved its baseline against this pipeline's own (usually
    # empty) log; ``baseline_for`` is allowed to borrow pass 1's. Patch the
    # payload rather than the profile so both halves of the gate quote one
    # number.
    effective["baseline_tokens"] = baseline
    effective["baseline_source"] = baseline_source

    totals = counts(pending)
    payload = {
        "status": "ok",
        "command": "prepare",
        "project": project_dir.name,
        "counts": totals,
        "skipped_count": len(skipped),
        "manifest": str(directory / "manifest.json"),
        "effective": effective,
        "usage_summary": {
            "chunks": totals["chunks"],
            "candidates": totals["candidates"],
            "source_requested": totals["source_requested"],
            "cli": prof.cli,
            "worker_model": prof.worker_model,
            "batch_size": DEFAULT_CONCURRENCY,
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
    if not quiet:
        payload["entries"] = entries
    return payload


def fanout(
    project_dir: Path,
    *,
    cli: Optional[str] = None,
    cli_bin: Optional[str] = None,
    worker_model: Optional[str] = None,
    effort: Optional[str] = None,
    concurrency: Optional[int] = None,
    cache: str = "auto",
    on_job_done: Optional[Callable[[dict[str, Any]], None]] = None,
    on_profile: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    """Headless wave over the prepared manifest.

    Inherits the CLI, worker model and effort ``prepare`` wrote into the
    manifest, so a bare call reproduces the wave that was consented to. The
    arguments are the one-flag correction for a bad pin, not the primary knobs.

    ``on_profile`` fires the moment the profile resolves, before any process is
    launched, so a caller can surface its warnings while they are still
    actionable — a wave on the wrong CLI runs for minutes, and a warning
    delivered with the result arrived too late to be one.
    """
    from src.harness import state as hstate
    from src.harness.headless import run_headless_wave
    from src.harness.profile import resolve_profile

    manifest = load_manifest(project_dir)
    if manifest is None:
        return {
            "status": "error",
            "command": "fanout",
            "error": "no editorial manifest — run `prepare` first",
        }

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
    inherited_effort, inherited_effort_source = effort, "cli"
    cli_unchanged = (cli or manifest.get("cli")) == manifest.get("cli")
    if not effort and "effort" in manifest and cli_unchanged:
        inherited_effort = manifest["effort"] or "default"
        inherited_effort_source = "manifest"
    profile = resolve_profile(
        project_dir,
        command="judges",
        cli=cli or manifest.get("cli"),
        cli_source="cli" if cli else "manifest",
        worker_model=worker_model or manifest.get("worker_model"),
        worker_model_source="cli" if worker_model else "manifest",
        effort=inherited_effort,
        effort_source=inherited_effort_source,
        cfg=cfg,
        usage_log=usage_log(project_dir),
    )
    cli_name = profile.cli
    resolved_model = profile.worker_model
    resolved_effort = profile.effort
    extra_flags = hstate.compose_headless_argv(
        cfg, resolved_effort if profile.effort_channel == "argv" else None
    )
    if on_profile is not None:
        on_profile(profile.to_payload())

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
            logger.warning("could not update the editorial manifest: %s", exc)

    # Resolve the wave width BEFORE the launcher sees it. ``run_headless_wave``
    # does ``if concurrency < 1``, so handing it the argparse default of ``None``
    # is a TypeError before job 1 — which is exactly what the documented bare
    # ``fanout`` did. Every sibling launcher resolves this first; this one did
    # not, and its test stubs the launcher, so CI never ran the comparison.
    if concurrency is None:
        try:
            concurrency = int(manifest.get("batch_size") or DEFAULT_CONCURRENCY)
        except (TypeError, ValueError):
            concurrency = DEFAULT_CONCURRENCY
    if concurrency < 1:
        return {
            "status": "error",
            "command": "fanout",
            "project": project_dir.name,
            "error": f"invalid concurrency {concurrency!r}; must be >= 1",
        }

    jobs, skipped = [], []
    for entry in manifest.get("entries") or []:
        draft_path = Path(entry["draft_path"])
        if draft_written(entry):
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
        return {
            "status": "ok",
            "command": "fanout",
            "project": project_dir.name,
            "profile": profile.to_payload(),
            "concurrency": concurrency,
            "wrote": [],
            "skipped": skipped,
            "instructions": "Nothing to fan out — run `commit --persist`.",
        }

    wave = run_headless_wave(
        jobs,
        model=resolved_model,
        concurrency=concurrency,
        cli=cli_name,
        cli_bin=cli_bin,
        usage_log=usage_log(project_dir),
        extra_flags=extra_flags,
        effort=resolved_effort,
        cache=cache,
        on_job_done=on_job_done,
    )
    return {
        "status": "error" if wave.get("error") else "ok",
        "command": "fanout",
        "project": project_dir.name,
        "profile": profile.to_payload(),
        "concurrency": concurrency,
        "skipped": skipped,
        **{k: v for k, v in wave.items() if k != "_schema"},
        "instructions": "Run `commit --persist` to land the verdicts.",
    }


def commit(
    project_dir: Path, *, persist: bool = False, brief: bool = False
) -> dict[str, Any]:
    """Parse every written draft and fold its verdicts into the persisted result.

    Takes no judge context, unlike its siblings. The verdicts were rendered
    against one at ``prepare`` time and the drafts answer *those* prompts;
    landing them needs the candidate set and the parsed response and nothing
    else. (The command this was lifted from built a context here and never read
    it — a walk of ``evaluations/`` per commit for a variable nothing used.)
    """
    manifest = load_manifest(project_dir)
    if manifest is None:
        return {
            "status": "error",
            "command": "commit",
            "error": "no editorial manifest — run `prepare` first",
        }

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
        if persist:
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
                "persisted": bool(persist),
            }
        )

    return {
        "status": "ok",
        "command": "commit",
        "project": project_dir.name,
        "backend": "draft",
        "persisted": bool(persist),
        "committed": len(results),
        "failed": failed,
        "missing": missing,
        "results": [] if brief else results,
        "rollup": _rollup(results),
        "instructions": (
            "Re-spawn the failed/missing entries and re-run `commit`."
            if (failed or missing)
            else "Done. Findings in evaluations/<chunk>.json are adjudicated."
        ),
    }
