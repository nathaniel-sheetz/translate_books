"""The ``annotations`` action: the reader's pending notes, reviewed unattended.

A thin adapter over :mod:`src.annotations.review`. Nothing here re-implements
any part of that pipeline — ``detect`` is :func:`~src.annotations.targets.build_targets`
with the counts rolled up, ``run`` is ``prepare → fanout → commit``, and
``auto_apply`` is ``apply(dry_run=True)`` filtered by a policy and handed back to
``apply(select=…)``.

Two things are this module's own, and both are safety rather than plumbing:

**``prepare`` is always called with ``keep_drafts=True``.** A bare ``prepare``
unlinks the drafts of every entry it re-renders, which is right for a human
starting over and catastrophic for an unattended pass that may be resuming a
wave the previous night's deadline cut short.

**``auto_apply`` never writes a footnote's text.** :func:`~src.annotations.review._planned_content`
gives ``footnote`` ``mode: "replace"`` because its content *is* the published
endnote text, so an automatic write would put a model's gloss into the book.
Every other type is an append after an ``— IA:`` marker into an append-only log,
which the reader can see and undo. The policy checks both the type list and the
mode, so a mistyped ``app_config.json`` cannot promote a footnote.

It does, however, **retire** an ``already_resolved`` note of any type, footnotes
included. That write carries no model prose at all — ``apply`` re-appends the
note's own content byte-for-byte and reports ``stale`` on any drift — so the
policy has nothing to protect against. It exists because the alternative is
worse than harmless: a reader-written gloss the reviewer correctly left alone
carried no record of having been read, so it was re-detected, re-prompted and
re-listed as outstanding work on every run, for the life of the book.
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path
from typing import Any

from src.actions.registry import Action, ActionState, ApplyResult, Budget, Policy, RunResult
from src.annotations import review, store
from src.annotations.targets import SKIP_ORPHANED, already_reviewed, build_targets
from src.harness import state as hstate

logger = logging.getLogger(__name__)

ACTION_NAME = "annotations"


def _settled_keys(project_dir: Path) -> set[str]:
    """Target keys whose live annotation already carries a review stamp.

    Applied, retired or rejected — the gate does not distinguish, and neither
    should any caller: all three mean the note is done and must not be written
    again. One read of ``annotations.jsonl``.
    """
    return {
        store.target_key(record)
        for record in store.load_active(project_dir)
        if already_reviewed(record)
    }


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------


def _cli_blockers(project_dir: Path, cfg: dict[str, Any]) -> list[str]:
    """Fatal, machine-checkable reasons a wave on this book could not start.

    Auth is *not* checked here: it costs a subprocess per CLI and is the
    driver's job to run once for the whole night rather than once per book (see
    ``scripts/daily_pass.py``). What is checked is the pair of conditions that
    are free to test and specific to this book.
    """
    from src.actions.scope import resolve_book_cli
    from src.harness.headless import cli_binary, cli_binary_present

    blockers: list[str] = []
    if not hstate.config_path(project_dir).exists():
        blockers.append(
            "no .harness/config.json — run `python scripts/harness.py setup "
            f"--project {project_dir.name}` first"
        )

    # Only a *pin* is reported as this book's blocker. An un-pinned book that
    # resolves to a missing binary is the machine's problem, not the book's, and
    # the driver reports it once instead of sixteen times.
    cli, source = resolve_book_cli(cfg)
    if source == "config" and not cli_binary_present(cli):
        blockers.append(
            f"headless_cli is pinned to {cli!r} but {cli_binary(cli)!r} is not on "
            f"PATH — install it or re-pin with `config-set --key headless_cli`"
        )
    return blockers


def detect(project_dir: Path) -> ActionState:
    """Count this book's reviewable annotations. No LLM call, no writes.

    ``pending`` is what a run would actually send: ``build_targets`` has already
    dropped imported Gutenberg notes, notes a previous run wrote back, and
    orphans. Those three appear in ``skipped`` with their counts, and the orphans
    also raise an ``attention`` line, because they are the one category no run
    will ever clear on its own.
    """
    project_dir = Path(project_dir)
    cfg = hstate.load_config(project_dir)

    targets, skipped = build_targets(project_dir)
    by_type = Counter(t.ann_type for t in targets)
    by_reason = Counter(s.reason for s in skipped)

    attention: list[str] = []
    orphaned = by_reason.get(SKIP_ORPHANED, 0)
    if orphaned:
        attention.append(
            f"{orphaned} orphaned annotation(s): the sentence they were anchored "
            "to no longer exists, so no review run will reach them — re-anchor "
            "them in the reader"
        )
    manual = sum(1 for t in targets if t.manual_reason)
    if manual:
        attention.append(
            f"{manual} annotation(s) will be reviewed but never written back "
            "automatically (multi-anchor) — resolve them in /review-inbox"
        )

    return ActionState(
        action=ACTION_NAME,
        pending=len(targets),
        by_type=dict(sorted(by_type.items())),
        skipped=dict(sorted(by_reason.items())),
        blockers=_cli_blockers(project_dir, cfg),
        attention=attention,
    )


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def run(project_dir: Path, budget: Budget, *, runner=None) -> RunResult:
    """``prepare → fanout → commit`` for one book, inside one budget.

    The caller is expected to hold this book's
    :func:`~src.harness.locks.project_lock` — every step here is destructive to
    ``.harness/annotations/``.

    Resumable by construction: ``prepare(keep_drafts=True)`` leaves in-flight
    drafts alone, ``fanout`` skips any entry that already has a non-empty draft,
    and ``commit`` merges into ``results.json`` by key. Re-running after a
    deadline cut, a crash or a per-CLI auth failure picks up where it stopped.

    ``runner`` is the same test seam :func:`~src.annotations.review.fanout`
    takes: ``(cmd, *, input_text, cwd) -> (rc, stdout, stderr)``. Passed through
    so the whole action can be exercised without spawning a CLI.
    """
    from src.actions.scope import book_profile

    project_dir = Path(project_dir)
    errors: list[str] = []
    warnings: list[str] = []

    prof = book_profile(
        project_dir,
        command=ACTION_NAME,
        override=budget.cli,
        default_cli=budget.default_cli,
    )
    warnings.extend(prof.warnings)

    # Before `prepare`, because `prepare` writes: it renders a prompt file per
    # annotation and rewrites manifest.json. A dry run has to be able to answer
    # "what would tonight do?" on a book nobody has touched without leaving
    # artifacts behind that a later real run would then treat as prepared.
    if budget.dry_run:
        state = detect(project_dir)
        planned = (
            state.pending if budget.max_targets is None
            else min(state.pending, max(0, budget.max_targets))
        )
        return RunResult(
            action=ACTION_NAME, status="ok", targets=planned, errors=errors,
            warnings=warnings,
            detail={
                "dry_run": True,
                "cli": prof.cli,
                "cli_source": prof.cli_source,
                "worker_model": prof.worker_model,
                "left_over": state.pending - planned,
                "blockers": state.blockers,
            },
        )

    # The worker model has to come from the *resolved* family, not from
    # `prepare`'s own guess: a book with no pin that this pass puts on Cursor
    # would otherwise get `prepare`'s Claude default (`sonnet`) written into its
    # manifest and trip `warn_cursor_claude_model` on every job.
    prep = review.prepare(
        project_dir, keep_drafts=True, worker_model=prof.worker_model
    )
    if prep.get("status") == "error":
        return RunResult(
            action=ACTION_NAME, status="error",
            errors=[*errors, str(prep.get("error") or "prepare failed")],
            warnings=warnings,
        )

    entries = prep.get("manifest") or []
    if not entries:
        return RunResult(
            action=ACTION_NAME, status="ok", targets=0, errors=errors,
            warnings=warnings,
            detail={"cli": prof.cli, "cli_source": prof.cli_source},
        )

    # A ceiling is applied by fanning out a prefix rather than by preparing
    # fewer: the prompts are already written, so tomorrow's run skips straight to
    # the CLI for the ones left over.
    keys = [e["key"] for e in entries if e.get("key")]
    capped = keys if budget.max_targets is None else keys[: max(0, budget.max_targets)]
    left_over = len(keys) - len(capped)

    if not capped:
        return RunResult(
            action=ACTION_NAME, status="partial", targets=0, errors=errors,
            warnings=warnings,
            detail={"left_over": left_over, "reason": "max_targets exhausted"},
        )

    out = review.fanout(
        project_dir,
        target_ids=capped,
        concurrency=budget.concurrency,
        cli=prof.cli,
        runner=runner,
    )
    if out.get("error"):
        errors.append(str(out["error"]))
    if out.get("warning"):
        errors.append(str(out["warning"]))
    errors.extend(
        f"{f.get('id')}: {f.get('error')}" for f in (out.get("failed") or [])
    )

    landed = review.commit(project_dir)
    if landed.get("status") == "error":
        return RunResult(
            action=ACTION_NAME, status="error", targets=len(capped),
            wrote=len(out.get("wrote") or []),
            failed=len(out.get("failed") or []),
            errors=[*errors, str(landed.get("error") or "commit failed")],
            warnings=warnings,
        )

    counts = landed.get("counts") or {}
    errors.extend(
        f"{f.get('key')}: {f.get('problem')}" for f in (landed.get("failed") or [])
    )

    committed = int(counts.get("committed") or 0)
    status = "ok" if (not errors and not left_over and committed == len(capped)) else "partial"
    return RunResult(
        action=ACTION_NAME,
        status=status,
        targets=len(capped),
        wrote=len(out.get("wrote") or []),
        failed=len(out.get("failed") or []) + int(counts.get("failed") or 0),
        committed=committed,
        errors=errors[:20],
        warnings=warnings[:20],
        report_path=landed.get("report_path"),
        detail={
            "cli": prof.cli,
            "cli_source": prof.cli_source,
            "worker_model": prof.worker_model,
            "skipped": len(out.get("skipped") or []),
            "missing": len(landed.get("missing") or []),
            "left_over": left_over,
            "usage": out.get("usage"),
        },
    )


# ---------------------------------------------------------------------------
# auto_apply
# ---------------------------------------------------------------------------


def auto_apply(project_dir: Path, policy: Policy) -> ApplyResult:
    """Write back only the results the policy accepts; hold the rest for a human.

    ``held`` — everything ``apply`` would allow but the policy will not — is the
    queue ``/review-inbox`` renders. It is the deliberate output of this
    function, not a leftover.
    """
    project_dir = Path(project_dir)
    plan = review.apply(project_dir, dry_run=True)
    if plan.get("status") == "error":
        return ApplyResult(
            action=ACTION_NAME, status="error",
            errors=[str(plan.get("error") or "apply plan failed")],
        )

    # Drop anything already settled before the policy ever sees it. `run` normally
    # clears these — a stamped note is skipped by `build_targets` and so drops out
    # of results.json — but `auto_apply` runs even when `run` errored
    # (scripts/daily_pass.py), off whatever plan is on disk. Without this a note
    # you rejected in the inbox would be re-applied by the next night's pass, and
    # a rejected note would keep inflating `held` in the digest, which is the only
    # notification surface there is.
    settled = _settled_keys(project_dir)
    applicable = [
        item for item in (plan.get("applicable") or []) if item["key"] not in settled
    ]
    selected = [item["key"] for item in applicable if policy.accepts(item)]
    held = [item["key"] for item in applicable if not policy.accepts(item)]
    manual = [
        item["key"]
        for item in (plan.get("manual") or [])
        if item["key"] not in settled
    ]

    # Retiring an `already_resolved` note is not governed by the policy, and
    # deliberately so: the policy exists to keep a model's *words* out of the
    # book unwatched, and this write has none — `apply` re-appends byte-identical
    # content and refuses on any drift. What it adds is the sidecar, without
    # which a finished note is re-reviewed every night for the life of the book.
    # That is why footnotes are safe here despite being excluded from the policy.
    retirable = [
        item["key"]
        for item in (plan.get("resolved") or [])
        if item["key"] not in settled
    ]

    if policy.dry_run or not (selected or retirable):
        return ApplyResult(
            action=ACTION_NAME,
            status="ok",
            applied=[],
            held=held,
            errors=[],
            detail={
                "dry_run": policy.dry_run,
                "would_apply": selected,
                "would_retire": retirable,
                "manual": manual,
                "policy": policy.to_payload(),
            },
        )

    written = review.apply(project_dir, select=selected + retirable)
    if written.get("status") == "error":
        return ApplyResult(
            action=ACTION_NAME, status="error", held=held,
            errors=[str(written.get("error") or "apply failed")],
        )

    stale = [s.get("key") for s in (written.get("stale") or []) if s.get("key")]
    unknown = list(written.get("unknown_ids") or [])
    return ApplyResult(
        action=ACTION_NAME,
        status="partial" if (stale or unknown) else "ok",
        applied=list(written.get("applied") or []),
        already_applied=list(written.get("already_applied") or []),
        held=held,
        stale=[str(s) for s in stale],
        errors=[f"unknown key after commit: {k}" for k in unknown],
        detail={
            "retired": list(written.get("retired") or []),
            "manual": manual,
            "policy": policy.to_payload(),
            "annotations_path": written.get("annotations_path"),
        },
    )


annotations_action = Action(
    name=ACTION_NAME,
    detect=detect,
    run=run,
    auto_apply=auto_apply,
    description="Review the reader's pending annotations and append the safe resolutions",
)
