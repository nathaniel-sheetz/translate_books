#!/usr/bin/env python3
"""
The nightly pass: review every in-scope book's pending work, unattended.

For each book that has work and nothing blocking it, under that book's
``project_lock``:

    detect → prepare(keep_drafts=True) → fanout → commit → auto_apply(policy)

and then a digest at ``reports/nightly/annotations_<YYYYmmdd>.md`` linking each
book's own dated report. Results the policy will not write automatically are
left for ``/review-inbox``; nothing here rebuilds an EPUB, because the policy
never applies a footnote and so never changes published text.

    python scripts/daily_pass.py --dry-run
    python scripts/daily_pass.py --project gaudenzia
    python scripts/daily_pass.py --max-books 3

Design notes worth knowing before changing anything here:

**Each book keeps its own backend.** No ``--cli`` and no ``--worker-model`` is
pushed across books; :func:`src.actions.scope.resolve_book_cli` honours each
book's ``headless_cli`` pin and falls back to ``automation.default_cli`` only for
books that never pinned one. ``--cli`` exists for debugging a single book and the
scheduled task never passes it.

**Preflight is per CLI, up front, and terminal only for that CLI.** A logged-out
``claude`` must not cost the four Cursor books their night. Nothing is prepared
or spawned for a refused CLI, so re-running after ``claude`` + ``/login`` is
idempotent.

**One book's exception ends that book, not the night.** Every per-book step is
wrapped; the failure is recorded in the digest and the run continues.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import warnings
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

from src.actions import registry  # noqa: E402
from src.actions import scope as ascope  # noqa: E402
from src.actions.registry import Budget, Policy  # noqa: E402
from src.harness import locks  # noqa: E402
from src.harness import state as hstate  # noqa: E402
from src.utils.run_logger import log_run_event  # noqa: E402

REPO_ROOT = hstate.REPO_ROOT
DIGEST_DIR = REPO_ROOT / "reports" / "nightly"
NIGHTLY_LOG = REPO_ROOT / "logs" / "nightly.jsonl"

# How long a book waits for a lock somebody else holds before being skipped for
# the night. Long enough to ride out a dashboard click, far short of a wave.
_LOCK_WAIT_S = 60.0


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def _preflight(pairs: set[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """Probe each ``(cli, model)`` the night needs. Returns ``{(cli, model): error}``.

    Keyed by the pair, not by the CLI, because Cursor's third gate validates the
    model id: one book pinning a model that gate rejects must take out that book,
    not every other book on the same family. A failure that really is family-wide
    — a missing binary, a logged-out CLI — fails every pair for that CLI anyway,
    so those books all drop out as before, each carrying the real reason.
    """
    from src.harness.headless import preflight_error

    errors: dict[tuple[str, str], str] = {}
    for cli, model in sorted(pairs):
        try:
            problem = preflight_error(cli, model=model)
        except Exception as exc:  # noqa: BLE001 - a probe crash is a refusal
            problem = f"{type(exc).__name__}: {exc}"
        if problem:
            errors[(cli, model)] = problem
    return errors


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------


def _plan(settings: dict, args) -> tuple[list, list, dict]:
    """Resolve scope, detect work, and resolve each book's backend.

    Returns ``(candidates, skipped_books, preflight_pairs)`` where a candidate is
    ``{"entry", "state", "profile"}`` for a book with work and no blocker.
    """
    result = ascope.in_scope(exclude_groups=settings["exclude_groups"])
    action = registry.get_action(args.action)

    candidates: list[dict] = []
    skipped: list[dict] = []
    pairs: set[tuple[str, str]] = set()

    for entry in result.projects:
        if args.project and entry.project_id != args.project:
            continue
        try:
            state = action.detect(entry.project_dir)
        except Exception as exc:  # noqa: BLE001
            skipped.append({
                "project_id": entry.project_id,
                "reason": "detect_failed",
                "detail": f"{type(exc).__name__}: {exc}",
            })
            continue

        if state.pending == 0:
            continue
        if state.blockers:
            skipped.append({
                "project_id": entry.project_id,
                "reason": "blocked",
                "detail": "; ".join(state.blockers),
                "pending": state.pending,
            })
            continue

        try:
            prof = ascope.book_profile(
                entry.project_dir,
                command=args.action,
                override=args.cli,
                default_cli=settings["default_cli"],
            )
        except Exception as exc:  # noqa: BLE001
            skipped.append({
                "project_id": entry.project_id,
                "reason": "backend_unresolved",
                "detail": f"{type(exc).__name__}: {exc}",
                "pending": state.pending,
            })
            continue

        pairs.add((prof.cli, prof.worker_model))
        candidates.append({"entry": entry, "state": state, "profile": prof})

    for skip in result.skipped:
        if args.project and skip.project_id != args.project:
            continue
        skipped.append({
            "project_id": skip.project_id,
            "reason": skip.reason,
            "detail": skip.detail,
        })

    return candidates, skipped, pairs


def _run_book(
    candidate: dict,
    *,
    action,
    budget: Budget,
    policy: Policy | None,
    run_id: str,
    dry_run: bool,
) -> dict:
    """One book, start to finish, under its lock. Never raises."""
    entry = candidate["entry"]
    prof = candidate["profile"]
    row: dict = {
        "project_id": entry.project_id,
        "pending": candidate["state"].pending,
        "cli": prof.cli,
        "cli_source": prof.cli_source,
        "worker_model": prof.worker_model,
    }

    if dry_run:
        # No lock: a dry run writes nothing, so taking a book's lock would only
        # make `--dry-run` fail against a book the dashboard is working on — the
        # one moment you most want to be able to ask what the plan is.
        #
        # Inside the same handler as the real path all the same: `--dry-run` is
        # what you reach for when a book is already broken, so it is the last
        # mode that should die on one corrupt results.json and take the other
        # fifteen books' plans with it.
        try:
            result = action.run(entry.project_dir, budget)
            row["run"] = result.to_payload()
            if policy is not None and action.auto_apply is not None:
                applied = action.auto_apply(entry.project_dir, policy)
                row["apply"] = applied.to_payload()
        except Exception as exc:  # noqa: BLE001 - one book must not end the night
            row["status"] = "error"
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["traceback"] = traceback.format_exc(limit=6)
        return row

    try:
        with locks.project_lock(
            entry.project_dir, kind=action.name, run_id=run_id, timeout=_LOCK_WAIT_S
        ):
            log_run_event(
                run_id=run_id, project=entry.project_id, event="nightly_book_start",
                action=action.name, pending=candidate["state"].pending,
                cli=prof.cli, cli_source=prof.cli_source,
                worker_model=prof.worker_model,
            )
            started = time.monotonic()
            result = action.run(entry.project_dir, budget)
            row["run"] = result.to_payload()
            row["dur_s"] = round(time.monotonic() - started, 1)

            if policy is not None and action.auto_apply is not None:
                applied = action.auto_apply(entry.project_dir, policy)
                row["apply"] = applied.to_payload()

            log_run_event(
                run_id=run_id, project=entry.project_id, event="nightly_book_done",
                action=action.name, status=result.status, dur_s=row["dur_s"],
                committed=result.committed, failed=result.failed,
                applied=len((row.get("apply") or {}).get("applied") or []),
                held=len((row.get("apply") or {}).get("held") or []),
            )
    except locks.LockBusy as busy:
        row["status"] = "locked"
        row["error"] = str(busy)
        log_run_event(
            run_id=run_id, project=entry.project_id, event="nightly_book_locked",
            action=action.name, holder=busy.holder,
        )
    except Exception as exc:  # noqa: BLE001 - one book must not end the night
        row["status"] = "error"
        row["error"] = f"{type(exc).__name__}: {exc}"
        row["traceback"] = traceback.format_exc(limit=6)
        log_run_event(
            run_id=run_id, project=entry.project_id, event="nightly_book_error",
            action=action.name, error=row["error"],
        )

    return row


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


def _rel(path: str | None) -> str | None:
    """A repo-relative path when possible — the digest is read in the repo."""
    if not path:
        return None
    try:
        return Path(path).resolve().relative_to(REPO_ROOT).as_posix()
    except (OSError, ValueError):
        return path


def _policy_line(payload: dict) -> str:
    """How the digest describes the auto-apply gate, including "not at all"."""
    policy = payload.get("policy")
    if not policy:
        return "review only (--no-apply)"
    return f"{'/'.join(policy['types'])} @ {policy['confidence_floor']}"


def _write_digest(payload: dict) -> Path:
    """One markdown file per run, linking each book's own dated report.

    Named for the run, not the day: a hand-run after fixing a logged-out CLI
    would otherwise overwrite the 06:30 pass's digest, and that digest is the
    only record the other fifteen books produce.
    """
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    run_id = str(payload.get("run_id") or "")
    stamp = run_id[len("nightly_"):] if run_id.startswith("nightly_") else run_id
    path = DIGEST_DIR / f"annotations_{stamp or datetime.now().strftime('%Y%m%d_%H%M%S')}.md"

    totals = payload["totals"]
    lines = [
        f"# Nightly annotation pass — {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"- run_id: `{payload['run_id']}`",
        f"- books attempted: **{totals['books']}** · reviewed: **{totals['committed']}** "
        f"· auto-applied: **{totals['applied']}** · held for review: **{totals['held']}**",
        f"- backend: `default_cli={payload['settings']['default_cli']}` · policy: `{_policy_line(payload)}`",
        f"- stopped: {payload['stopped_because']}",
        "",
    ]
    if totals["held"]:
        lines += [
            f"**{totals['held']} resolution(s) are waiting in [/review-inbox](http://127.0.0.1:5000/review-inbox).**",
            "",
        ]

    lines += ["## Books", ""]
    for row in payload["books"]:
        run = row.get("run") or {}
        apply_ = row.get("apply") or {}
        head = f"### {row['project_id']}"
        lines.append(head)
        if row.get("error"):
            lines += [f"- **{row.get('status', 'error')}**: {row['error']}", ""]
            continue
        lines.append(
            f"- {run.get('committed', 0)} reviewed of {run.get('targets', 0)} sent "
            f"· {len(apply_.get('applied') or [])} applied "
            f"· {len(apply_.get('held') or [])} held "
            f"· {len(apply_.get('stale') or [])} stale"
        )
        lines.append(
            f"- backend: `{row.get('cli')}` ({row.get('cli_source')}), "
            f"model `{row.get('worker_model')}`, {row.get('dur_s', '?')}s"
        )
        report = _rel(run.get("report_path"))
        if report:
            lines.append(f"- report: [{report}]({report})")
        for err in (run.get("errors") or [])[:5]:
            lines.append(f"- error: {err}")
        for warn in (run.get("warnings") or [])[:5]:
            lines.append(f"- note: {warn}")
        lines.append("")

    if payload["skipped_books"]:
        lines += ["## Skipped", ""]
        for skip in payload["skipped_books"]:
            detail = f" — {skip['detail']}" if skip.get("detail") else ""
            lines.append(f"- `{skip['project_id']}`: {skip['reason']}{detail}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _append_nightly_log(payload: dict) -> None:
    """One summary row per run in ``logs/nightly.jsonl``. Best-effort."""
    row = {
        "ts": datetime.now().isoformat(),
        "run_id": payload["run_id"],
        "action": payload["action"],
        "dry_run": payload["dry_run"],
        "totals": payload["totals"],
        "stopped_because": payload["stopped_because"],
        "preflight_errors": payload["preflight_errors"],
        "digest": payload.get("digest"),
    }
    try:
        NIGHTLY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with NIGHTLY_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass  # a summary row must never sink a run that already did the work


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None):
    parser = argparse.ArgumentParser(
        description="Review every in-scope book's pending work, unattended.",
    )
    parser.add_argument(
        "--action", default="annotations", choices=list(registry.action_names()),
        help="which action to run (default: annotations)",
    )
    parser.add_argument("--project", help="limit the pass to one book by id")
    parser.add_argument("--max-books", type=int, help="stop after this many books")
    parser.add_argument(
        "--max-targets", type=int,
        help="stop after this many targets across the whole run "
        "(default: automation.max_targets_per_run)",
    )
    parser.add_argument(
        "--deadline-minutes", type=int,
        help="wall-clock ceiling for the whole run (default: automation.deadline_minutes)",
    )
    parser.add_argument(
        "--concurrency", type=int,
        help="parallel headless jobs per book (default: automation.concurrency)",
    )
    parser.add_argument(
        "--cli", choices=["claude", "cursor"],
        help="DEBUG ONLY: force a family past every book's pin. The scheduled "
        "task never passes this — use --default-cli to move the un-pinned books.",
    )
    parser.add_argument(
        "--default-cli", choices=["claude", "cursor"],
        help="override automation.default_cli for this run",
    )
    parser.add_argument(
        "--no-apply", action="store_true",
        help="review only; leave every resolution for /review-inbox",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="plan only: detect + the apply plan. Writes nothing, spawns nothing.",
    )
    parser.add_argument("--json", action="store_true", help="print the raw JSON summary")
    return parser.parse_args(argv)


def _execute(args, run_id: str) -> dict:
    settings = ascope.automation_config({
        "default_cli": args.default_cli,
        "concurrency": args.concurrency,
        "max_targets_per_run": args.max_targets,
        "deadline_minutes": args.deadline_minutes,
    })
    action = registry.get_action(args.action)

    policy = None if args.no_apply else Policy(
        types=tuple(settings["auto_apply_types"]),
        confidence_floor=settings["confidence_floor"],
        dry_run=args.dry_run,
    )

    candidates, skipped, pairs = _plan(settings, args)

    preflight_errors: dict[tuple[str, str], str] = {}
    if not args.dry_run and candidates:
        preflight_errors = _preflight(pairs)
        if preflight_errors:
            kept = []
            for candidate in candidates:
                prof = candidate["profile"]
                pair = (prof.cli, prof.worker_model)
                if pair in preflight_errors:
                    skipped.append({
                        "project_id": candidate["entry"].project_id,
                        "reason": f"preflight:{prof.cli}",
                        "detail": preflight_errors[pair],
                        "pending": candidate["state"].pending,
                    })
                else:
                    kept.append(candidate)
            candidates = kept

    # `<cli>:<model>`, because a tuple cannot be a JSON key and every consumer
    # below here — the digest, logs/nightly.jsonl, `--json` — serialises.
    preflight_report = {
        f"{cli}:{model}": error for (cli, model), error in preflight_errors.items()
    }

    deadline = time.monotonic() + max(1, int(settings["deadline_minutes"])) * 60
    remaining_targets = int(settings["max_targets_per_run"])
    books: list[dict] = []
    stopped = "finished"

    log_run_event(
        run_id=run_id, project=None, event="nightly_start", action=args.action,
        books=len(candidates), dry_run=args.dry_run,
        default_cli=settings["default_cli"], preflight_errors=preflight_report,
    )

    for index, candidate in enumerate(candidates):
        if args.max_books is not None and index >= args.max_books:
            stopped = "max_books"
            break
        if time.monotonic() >= deadline:
            stopped = "deadline"
            break
        if remaining_targets <= 0:
            stopped = "max_targets"
            break

        budget = Budget(
            concurrency=int(settings["concurrency"]),
            max_targets=remaining_targets,
            deadline=deadline,
            cli=args.cli,
            default_cli=settings["default_cli"],
            dry_run=args.dry_run,
        )
        row = _run_book(
            candidate, action=action, budget=budget, policy=policy,
            run_id=run_id, dry_run=args.dry_run,
        )
        books.append(row)
        remaining_targets -= int((row.get("run") or {}).get("targets") or 0)

    left = len(candidates) - len(books)
    totals = {
        "books": len(books),
        "books_left": left,
        "targets": sum(int((b.get("run") or {}).get("targets") or 0) for b in books),
        "committed": sum(int((b.get("run") or {}).get("committed") or 0) for b in books),
        "applied": sum(len((b.get("apply") or {}).get("applied") or []) for b in books),
        "held": sum(len((b.get("apply") or {}).get("held") or []) for b in books),
        "stale": sum(len((b.get("apply") or {}).get("stale") or []) for b in books),
        "errors": sum(1 for b in books if b.get("error")),
    }

    payload = {
        "run_id": run_id,
        "action": args.action,
        "dry_run": args.dry_run,
        "settings": settings,
        "policy": policy.to_payload() if policy else None,
        "preflight_errors": preflight_report,
        "totals": totals,
        "books": books,
        "skipped_books": skipped,
        "stopped_because": stopped,
    }

    if not args.dry_run:
        # Best-effort, like _append_nightly_log beside it: every book has already
        # been reviewed and written by this point, and losing the run's record
        # over a failed report write would be the expensive way to report it.
        try:
            payload["digest"] = _rel(str(_write_digest(payload)))
        except OSError as exc:
            print(f"could not write the nightly digest: {exc}", file=sys.stderr)
        _append_nightly_log(payload)

    log_run_event(
        run_id=run_id, project=None, event="nightly_done", action=args.action,
        totals=totals, stopped_because=stopped, digest=payload.get("digest"),
    )
    return payload


def _print_human(payload: dict) -> None:
    totals = payload["totals"]
    mode = "DRY RUN — nothing written" if payload["dry_run"] else payload["run_id"]
    print(f"Nightly {payload['action']} pass · {mode}")
    for pair, error in (payload["preflight_errors"] or {}).items():
        print(f"  PREFLIGHT {pair}: {error}")
    for row in payload["books"]:
        run = row.get("run") or {}
        apply_ = row.get("apply") or {}
        if row.get("error"):
            print(f"  {row['project_id']}: {row.get('status')} — {row['error']}")
            continue
        print(
            f"  {row['project_id']}: {run.get('committed', 0)}/{run.get('targets', 0)} reviewed"
            f" · {len(apply_.get('applied') or [])} applied"
            f" · {len(apply_.get('held') or [])} held"
            f"  [{row.get('cli')}/{row.get('worker_model')}]"
        )
    for skip in payload["skipped_books"]:
        if skip["reason"] in ("blocked", "detect_failed", "backend_unresolved") or skip["reason"].startswith("preflight:"):
            print(f"  {skip['project_id']}: SKIPPED ({skip['reason']}) {skip.get('detail', '')}")
    print(
        f"Totals: {totals['books']} book(s), {totals['committed']} reviewed, "
        f"{totals['applied']} applied, {totals['held']} held, "
        f"{totals['errors']} failed · stopped: {payload['stopped_because']}"
    )
    if payload.get("digest"):
        print(f"Digest: {payload['digest']}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    run_id = f"nightly_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    if args.dry_run:
        payload = _execute(args, run_id)
    else:
        try:
            with locks.repo_lock(kind="nightly", run_id=run_id):
                payload = _execute(args, run_id)
        except locks.LockBusy as busy:
            print(f"Another pass is already running: {busy}", file=sys.stderr)
            return 2

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_human(payload)
    return 1 if payload["totals"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
