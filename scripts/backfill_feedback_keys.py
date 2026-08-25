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

Runs in report-only mode by default; pass --write to modify anything. The
original file is copied to ``_feedback.jsonl.bak`` before the first write.

Usage:
    python scripts/backfill_feedback_keys.py
    python scripts/backfill_feedback_keys.py --write
    python scripts/backfill_feedback_keys.py --project the-little-duke --write
"""

from __future__ import annotations

import argparse
import collections
import json
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

            issue = (
                find_issue(evaluation, eval_name, record.get("issue_index"))
                if isinstance(evaluation, dict)
                else None
            )
            if isinstance(issue, dict):
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
        path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        stats["file_written"] += 1

    return out_lines, stats


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
        project_dirs = [projects_root / slug for slug in args.project]
    else:
        project_dirs = sorted(
            p
            for p in projects_root.rglob("evaluations/_feedback.jsonl")
        )
        project_dirs = [p.parent.parent for p in project_dirs]

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

    unresolved_by_eval = {
        k[len("unresolved_") :]: v
        for k, v in totals.items()
        if k.startswith("unresolved_")
    }
    if unresolved_by_eval:
        print("\n  unresolved by evaluator (these keep matching positionally):")
        for name, count in sorted(unresolved_by_eval.items(), key=lambda kv: -kv[1]):
            print(f"    {count:>4}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
