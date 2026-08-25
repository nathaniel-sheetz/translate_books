"""Stamp ``issue_key`` onto feedback records written before the field existed.

Every mark in ``evaluations/_feedback.jsonl`` used to be keyed by
``(eval_name, issue_index)`` -- a position in the evaluator's issue list. This
walks each record, resolves that index against the stored evaluation, and writes
back the finding's content hash so the mark survives the next re-run.

Records whose index no longer resolves are left with ``issue_key: null`` and
flagged ``key_unresolved: true`` rather than guessed at. A dangling mark is
already ambiguous -- the evaluator has re-run and the list moved -- and inventing
a key for it would freeze a wrong answer in place. They keep matching
positionally, exactly as they do today.

The same refusal covers marks *older than the evaluator's last run*, which are
flagged ``key_skipped_stale`` as well: their index still resolves, but to
whatever finding now occupies the slot, so keying them would convert a
recoverable ambiguity into a confident wrong answer.

RUN THIS BEFORE RE-EVALUATING ANYTHING. ``GrammarEvaluator.DEFAULT_IGNORE_RULES``
drops roughly a third of grammar findings, and a re-run renumbers every
``results[].issues`` list; any un-keyed mark left over at that point is stale by
the paragraph above and can no longer be keyed at all.

Runs in report-only mode by default; pass --write to modify anything. The
original file is copied to ``_feedback.jsonl.bak`` before the first write, and
the rewrite goes through a temp file so a concurrent dashboard append or a kill
mid-write cannot truncate it.

Usage:
    python scripts/backfill_feedback_keys.py
    python scripts/backfill_feedback_keys.py --write
    python scripts/backfill_feedback_keys.py --project the-little-duke --write
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from web_ui.evaluations import issue_key  # noqa: E402


def find_issue(
    evaluation: dict[str, Any], eval_name: str, issue_index: Any
) -> Optional[dict]:
    """The stored issue at ``(eval_name, issue_index)``, or None.

    Checks the coded evaluators' ``results[]`` first, then ``judges{}`` -- the
    two places an issue list can live.
    """
    if not isinstance(issue_index, int) or issue_index < 0:
        return None
    for result in evaluation.get("results") or []:
        if isinstance(result, dict) and result.get("eval_name") == eval_name:
            issues = result.get("issues") or []
            return issues[issue_index] if issue_index < len(issues) else None
    judge = (evaluation.get("judges") or {}).get(eval_name)
    if isinstance(judge, dict):
        issues = judge.get("issues") or []
        return issues[issue_index] if issue_index < len(issues) else None
    return None


def eval_ran_at(evaluation: dict[str, Any], eval_name: str) -> Optional[str]:
    """ISO timestamp of the last run of ``eval_name`` on this chunk, or None.

    Coded evaluators record ``eval_runs[name].at``; judges record their own
    ``executed_at``, with ``judges_at`` as the whole-chunk fallback. The strings
    are ISO-8601 from the same clock, so they compare lexicographically.
    """
    run = (evaluation.get("eval_runs") or {}).get(eval_name)
    if isinstance(run, dict) and run.get("at"):
        return str(run["at"])
    judge = (evaluation.get("judges") or {}).get(eval_name)
    if isinstance(judge, dict) and judge.get("executed_at"):
        return str(judge["executed_at"])
    if judge is not None and evaluation.get("judges_at"):
        return str(evaluation["judges_at"])
    return None


def backfill_project(
    project_dir: Path, write: bool
) -> tuple[list[str], collections.Counter]:
    """Return the rewritten lines plus a counter of what happened."""
    stats: collections.Counter = collections.Counter()
    path = project_dir / "evaluations" / "_feedback.jsonl"
    out_lines: list[str] = []
    if not path.exists():
        return out_lines, stats

    cache: dict[str, Optional[dict]] = {}

    with path.open(encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                # Preserve unparseable lines untouched rather than dropping a
                # mark we cannot read.
                stats["malformed_preserved"] += 1
                out_lines.append(stripped)
                continue

            if record.get("issue_key"):
                stats["already_keyed"] += 1
                out_lines.append(json.dumps(record, ensure_ascii=False))
                continue

            chunk_id = record.get("chunk_id")
            eval_name = record.get("eval_name")
            if chunk_id not in cache:
                eval_path = project_dir / "evaluations" / f"{chunk_id}.json"
                try:
                    cache[chunk_id] = json.loads(eval_path.read_text(encoding="utf-8"))
                except Exception:
                    cache[chunk_id] = None
            evaluation = cache[chunk_id]

            # A mark older than the evaluator's last run cannot be trusted to
            # still name the same finding: that re-run is exactly what rewrote
            # the issue list out from under its index. Resolving it anyway would
            # stamp the intruder's hash and freeze a wrong answer, which is
            # strictly worse than the ambiguity it replaces -- so leave it
            # positional, the way a dangling index is left.
            stale = False
            if isinstance(evaluation, dict):
                ran_at = eval_ran_at(evaluation, eval_name)
                ts = record.get("ts")
                if ran_at and isinstance(ts, str) and ts < ran_at:
                    stale = True

            issue = (
                find_issue(evaluation, eval_name, record.get("issue_index"))
                if isinstance(evaluation, dict) and not stale
                else None
            )
            if stale:
                record["issue_key"] = None
                record["key_unresolved"] = True
                record["key_skipped_stale"] = True
                stats["stale"] += 1
                stats[f"unresolved_{eval_name}"] += 1
            elif isinstance(issue, dict):
                record["issue_key"] = issue_key(eval_name, issue)
                stats["keyed"] += 1
                stats[f"keyed_{eval_name}"] += 1
            else:
                record["issue_key"] = None
                record["key_unresolved"] = True
                stats["unresolved"] += 1
                stats[f"unresolved_{eval_name}"] += 1
            out_lines.append(json.dumps(record, ensure_ascii=False))

    if write and out_lines:
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        # Write a sibling and rename over the original. The dashboard is
        # normally running while this executes -- that is how marks are made --
        # and a truncate-in-place would let a mark appended between the read
        # above and the write here vanish, or leave the file half-written if the
        # process dies mid-rewrite.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        stats["file_written"] += 1

    return out_lines, stats


def discover_projects(projects_root: Path) -> list[Path]:
    """Every project directory holding a ``_feedback.jsonl``.

    rglob, not iterdir: books also live one level down under a hidden group
    directory (``projects/.published/``, ``projects/.macdonald/``, ...), and
    skipping those hid 28% of the marked corpus.

    Directories with ``.bak`` in the name are snapshots of another project --
    ``projects/.backburner/the-little-duke.bak-footnote-migration`` is a copy of
    ``the-little-duke`` -- so their marks are duplicates, not evidence.
    """
    return sorted(
        p.parent.parent
        for p in projects_root.rglob("evaluations/_feedback.jsonl")
        if ".bak" not in p.parent.parent.name
    )


def resolve_project(projects_root: Path, slug: str) -> Path:
    """Locate a project by slug, including under a hidden group directory.

    Projects live at ``projects/<slug>`` but also at ``projects/.<group>/<slug>``
    (``.published``, ``.macdonald``, ...), so a bare join misses most of the
    corpus. Falls back to the plain join so the caller's "missing" message still
    names something recognizable.
    """
    direct = projects_root / slug
    if direct.is_dir():
        return direct
    for path in sorted(projects_root.rglob(f"{slug}/evaluations/_feedback.jsonl")):
        return path.parent.parent
    return direct


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", action="append", help="Project slug (repeatable)")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually modify the files (default: report only)",
    )
    args = parser.parse_args()

    projects_root = REPO_ROOT / "projects"
    if args.project:
        project_dirs = [resolve_project(projects_root, slug) for slug in args.project]
    else:
        project_dirs = discover_projects(projects_root)

    totals: collections.Counter = collections.Counter()
    if not args.write:
        print("REPORT ONLY -- pass --write to modify files\n")

    for project_dir in project_dirs:
        if not project_dir.exists():
            print(f"skip (missing): {project_dir}")
            continue
        _, stats = backfill_project(project_dir, args.write)
        if not stats:
            continue
        totals.update(stats)
        print(
            f"  {project_dir.name:<44} keyed {stats['keyed']:>4}   "
            f"unresolved {stats['unresolved']:>3}   "
            f"already {stats['already_keyed']:>4}"
        )

    print()
    print("=== totals ===")
    for key in sorted(totals):
        if key.startswith(("keyed_", "unresolved_")):
            continue
        print(f"  {key}: {totals[key]}")

    for prefix, caption in (
        ("keyed_", "keyed by evaluator:"),
        ("unresolved_", "unresolved by evaluator (these keep matching positionally):"),
    ):
        by_eval = {
            k[len(prefix) :]: v for k, v in totals.items() if k.startswith(prefix)
        }
        if not by_eval:
            continue
        print(f"\n  {caption}")
        for name, count in sorted(by_eval.items(), key=lambda kv: -kv[1]):
            print(f"    {count:>4}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
