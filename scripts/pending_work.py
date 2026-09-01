#!/usr/bin/env python3
"""
What unattended work is available across every book, and what would block it.

Read-only and zero spend: it reads ``annotations.jsonl``, each book's
``.harness/config.json``, and PATH. Nothing is written, nothing is spawned, no
LLM is called. Seconds to run, safe at any time — including while the nightly
pass or the dashboard is mid-wave.

    python scripts/pending_work.py scan
    python scripts/pending_work.py scan --json
    python scripts/pending_work.py scan --action annotations --project gaudenzia

The JSON object it prints is the same shape ``daily_pass.py`` plans from, so
"what would tonight do?" and "what did tonight do?" are answered from one
description of the work rather than two.

**CLI availability is resolved with ``shutil.which``**, via
:func:`src.harness.headless.cli_binary_present` — never a shell ``which``. Git
Bash's ``which`` does not honour ``PATHEXT`` and so misses the ``.CMD`` shims
both CLIs install on Windows, which would report every book as blocked.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows captured stdout defaults to cp1252, which mangles every accent in an
# annotation preview. Same guard the other CLIs use.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

from src.actions import registry  # noqa: E402
from src.actions import scope as ascope  # noqa: E402
from src.harness import locks  # noqa: E402

_SCHEMA = {
    "scanned_at": "ISO timestamp of the scan",
    "settings": "the resolved `automation` block the scan (and a run) would use",
    "totals": "{books, books_with_work, pending, blocked, by_type}",
    "books": "one row per in-scope book: {project_id, project_dir, group, lock, "
    "backend: {cli, cli_source, worker_model, worker_model_source, effort, "
    "effort_channel, host, warnings}, actions: {<name>: ActionState payload}}",
    "skipped_books": "books the scope rules excluded: {project_id, reason, detail}",
    "_note": "read-only; no spend, no writes, nothing spawned",
}


def _scan(
    *,
    action_names: list[str],
    only_project: str | None,
    settings: dict,
) -> dict:
    result = ascope.in_scope(exclude_groups=settings["exclude_groups"])

    books: list[dict] = []
    totals_pending = 0
    totals_blocked = 0
    by_type: dict[str, int] = {}
    with_work = 0

    for entry in result.projects:
        if only_project and entry.project_id != only_project:
            continue

        row: dict = {
            **entry.to_payload(),
            "actions": {},
        }

        # Resolution is per book and never cached across books: the whole point
        # of respecting each book's pin is that two books in one scan can
        # legitimately report different families and different worker models.
        try:
            prof = ascope.book_profile(
                entry.project_dir, default_cli=settings["default_cli"]
            )
            row["backend"] = {
                "cli": prof.cli,
                "cli_source": prof.cli_source,
                "worker_model": prof.worker_model,
                "worker_model_source": prof.worker_model_source,
                "effort": prof.effort,
                "effort_channel": prof.effort_channel,
                "host": prof.host,
                "warnings": list(prof.warnings),
            }
        except Exception as exc:  # noqa: BLE001 - one bad book must not sink the scan
            row["backend"] = {"error": f"{type(exc).__name__}: {exc}"}

        holder = locks.holder_of(entry.project_dir)
        if holder:
            row["lock"] = {
                "kind": holder.get("kind"),
                "pid": holder.get("pid"),
                "host": holder.get("host"),
                "run_id": holder.get("run_id"),
                "started_at": holder.get("started_at"),
            }

        book_pending = 0
        for name in action_names:
            action = registry.get_action(name)
            try:
                state = action.detect(entry.project_dir)
            except Exception as exc:  # noqa: BLE001
                row["actions"][name] = {
                    "action": name,
                    "pending": 0,
                    "blockers": [f"detect failed: {type(exc).__name__}: {exc}"],
                    "runnable": False,
                }
                totals_blocked += 1
                continue
            row["actions"][name] = state.to_payload()
            book_pending += state.pending
            totals_pending += state.pending
            for key, count in state.by_type.items():
                by_type[key] = by_type.get(key, 0) + count
            if state.blockers:
                totals_blocked += 1

        if book_pending:
            with_work += 1
        books.append(row)

    from datetime import datetime

    return {
        "scanned_at": datetime.now().isoformat(),
        "settings": settings,
        "totals": {
            "books": len(books),
            "books_with_work": with_work,
            "pending": totals_pending,
            "blocked": totals_blocked,
            "by_type": dict(sorted(by_type.items())),
        },
        "books": books,
        "skipped_books": [s.to_payload() for s in result.skipped],
        "_schema": _SCHEMA,
    }


def _print_human(payload: dict) -> None:
    """A per-book table. The JSON is authoritative; this is the reading copy."""
    totals = payload["totals"]
    settings = payload["settings"]
    print(
        f"{totals['books']} book(s) in scope · {totals['books_with_work']} with work "
        f"· {totals['pending']} pending annotation(s) · {totals['blocked']} blocked"
    )
    print(
        f"  default_cli={settings['default_cli']}  "
        f"exclude_groups={','.join(settings['exclude_groups']) or '(none)'}  "
        f"auto_apply={','.join(settings['auto_apply_types'])}@{settings['confidence_floor']}"
    )
    print()

    for book in payload["books"]:
        counts = {
            name: state.get("pending", 0)
            for name, state in book["actions"].items()
        }
        total = sum(counts.values())
        backend = book.get("backend") or {}
        where = book["group"] + "/" if book.get("group") else ""
        head = f"{total:>4}  {where}{book['project_id']}"
        if "error" in backend:
            print(f"{head}   [backend: {backend['error']}]")
        else:
            print(
                f"{head}   {backend.get('cli')} ({backend.get('cli_source')})"
                f" · {backend.get('worker_model')}"
            )

        if book.get("lock"):
            lock = book["lock"]
            print(f"        held: {lock.get('kind')} pid {lock.get('pid')} since {lock.get('started_at')}")
        for warning in backend.get("warnings") or []:
            print(f"        ! {warning}")
        for name, state in book["actions"].items():
            if state.get("by_type"):
                detail = ", ".join(f"{k} {v}" for k, v in state["by_type"].items())
                print(f"        {name}: {detail}")
            for blocker in state.get("blockers") or []:
                print(f"        BLOCKED  {blocker}")
            for note in state.get("attention") or []:
                print(f"        attn     {note}")

    if payload["skipped_books"]:
        print()
        print(f"Skipped {len(payload['skipped_books'])} book(s):")
        for skip in payload["skipped_books"]:
            detail = f" ({skip['detail']})" if skip.get("detail") else ""
            print(f"  {skip['project_id']}: {skip['reason']}{detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report available unattended work across every in-scope book.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="report pending work (read-only, no spend)")
    scan.add_argument(
        "--action", action="append", dest="actions",
        choices=list(registry.action_names()),
        help="limit to one action (repeatable); default is all registered actions",
    )
    scan.add_argument("--project", help="limit to one book by id")
    scan.add_argument(
        "--default-cli", choices=["claude", "cursor"],
        help="preview a different automation.default_cli without editing app_config.json",
    )
    scan.add_argument("--json", action="store_true", help="print the raw JSON object")

    args = parser.parse_args(argv)

    settings = ascope.automation_config({"default_cli": args.default_cli})
    payload = _scan(
        action_names=args.actions or list(registry.action_names()),
        only_project=args.project,
        settings=settings,
    )

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_human(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
