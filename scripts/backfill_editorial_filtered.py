"""Recover the editorial findings pass 1 dropped, from the drafts that still exist.

Pass 1 of the editorial judge drops findings at three gates -- non-issue,
confidence floor, per-passage budget. Until ``metadata.filtered`` existed the
evaluation kept only the counts (``filtered_*``); the findings themselves lived
only in the worker's raw draft, ``.harness/judges/<chunk>.editorial.draft.json``,
which the next ``prepare`` deletes. fabre2 already lost most of its drafts that
way.

For each editorial result without ``metadata.filtered`` this re-runs the same
gates (``EditorialJudge.select_findings``) over the draft, under the confidence
floor and budget the result recorded, and writes the drops back -- but only when
the replay reproduces what is stored: the draft's finding count equals
``proposed_count``, every ``filtered_*`` count matches, and the survivors are the
stored ``candidates`` in order. Anything else came from a different run and is
reported, not guessed at.

Outcomes, per chunk:
- ``recovered``: ``filtered`` written from the draft, ``filtered_backfilled: true``.
- ``zero_drop``: nothing was dropped; ``filtered: []`` written.
- ``lost``: drops were counted but no usable draft remains; ``filtered_lost: true``
  written so the gap is on record rather than silent.
- ``mismatched``: a draft exists but the replay disagrees with the result;
  left alone.
- ``already``: ``filtered`` is present (a run after the field existed).
- ``legacy``: the result predates the ``filtered_*`` counts; left alone.

Only ``judges.editorial.metadata`` is touched, and not through
``merge_judge_result``: that would re-stamp the freshness ledger and clear
``stale`` on chunks whose prose may have moved.

Runs in report-only mode by default; pass --write to modify anything. Each file
is copied to ``.harness/backups/editorial-filtered/`` before its first write,
and the rewrite goes through a temp file so a kill mid-write cannot truncate it.

Usage:
    python scripts/backfill_editorial_filtered.py
    python scripts/backfill_editorial_filtered.py --write
    python scripts/backfill_editorial_filtered.py --project five-little-peppers --write
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

from src.judges import llm_io  # noqa: E402
from src.judges.base import JudgeTarget  # noqa: E402
from src.judges.editorial_judge import EditorialJudge  # noqa: E402
from src.judges.llm_io import JudgeParseError  # noqa: E402

_COUNT_KEYS = ("filtered_nonissues", "filtered_low_confidence", "filtered_over_budget")


def _identity(finding: dict[str, Any]) -> tuple[str, str]:
    return (
        str(finding.get("rule", "other")).strip() or "other",
        " ".join(str(finding.get("excerpt") or "").split()),
    )


def _draft_findings(judge: EditorialJudge, path: Path) -> Optional[list[dict[str, Any]]]:
    """The raw findings in a solo draft, parsed the way ``commit`` parses it."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        data = llm_io.parse_judge_json(raw, judge.spec.output_fields)
    except JudgeParseError:
        return None
    return [f for f in (data.get("findings") or []) if isinstance(f, dict)]


def replay(
    judge: EditorialJudge, chunk_id: str, metadata: dict[str, Any], raw_findings: list[dict]
) -> Optional[list[dict[str, Any]]]:
    """Re-run the gates over ``raw_findings``; the drops, or None on disagreement."""
    if len(raw_findings) != metadata.get("proposed_count"):
        return None
    context = {
        "editorial_min_confidence": metadata.get("min_confidence"),
        "max_findings_override": metadata.get("findings_budget"),
    }
    target = JudgeTarget(id=chunk_id, target_type="chunk", source_text="", translated_text="")
    survivors, filtered, counts = judge.select_findings(raw_findings, target, context)
    if (
        counts["dropped_nonissue"] != metadata.get("filtered_nonissues")
        or counts["dropped_confidence"] != metadata.get("filtered_low_confidence")
        or counts["dropped_budget"] != metadata.get("filtered_over_budget")
    ):
        return None
    stored = [_identity(c) for c in metadata.get("candidates") or [] if isinstance(c, dict)]
    if [_identity(f) for f in survivors] != stored:
        return None
    return [judge._normalize_finding(f) for f in filtered]


def classify(
    judge: EditorialJudge, project_dir: Path, chunk_id: str, metadata: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Decide one chunk's outcome and the metadata fields to add for it."""
    if "filtered" in metadata or metadata.get("filtered_lost"):
        return "already", {}
    if not all(isinstance(metadata.get(k), int) for k in (*_COUNT_KEYS, "proposed_count")):
        return "legacy", {}
    dropped = sum(metadata[k] for k in _COUNT_KEYS)
    if dropped == 0:
        return "zero_drop", {"filtered": []}

    draft = project_dir / ".harness" / "judges" / f"{chunk_id}.editorial.draft.json"
    raw_findings = _draft_findings(judge, draft) if draft.exists() else None
    if raw_findings is None:
        return "lost", {"filtered_lost": True}
    if len(raw_findings) != metadata["proposed_count"]:
        # A later run's draft (or an earlier one's): not this result's record.
        return "lost", {"filtered_lost": True}
    filtered = replay(judge, chunk_id, metadata, raw_findings)
    if filtered is None:
        return "mismatched", {}
    return "recovered", {"filtered": filtered, "filtered_backfilled": True}


def _write(project_dir: Path, path: Path, payload: dict[str, Any]) -> None:
    backup_dir = project_dir / ".harness" / "backups" / "editorial-filtered"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / path.name
    if not backup.exists():
        shutil.copy2(path, backup)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def backfill_project(project_dir: Path, write: bool) -> collections.Counter:
    """Backfill one book; return a counter of outcomes (plus findings recovered)."""
    judge = EditorialJudge()
    stats: collections.Counter = collections.Counter()
    for path in sorted((project_dir / "evaluations").glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            stats["unreadable"] += 1
            continue
        result = (payload.get("judges") or {}).get("editorial") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            continue
        metadata = result.get("metadata")
        if not isinstance(metadata, dict):
            continue

        outcome, additions = classify(judge, project_dir, path.stem, metadata)
        stats[outcome] += 1
        if outcome == "recovered":
            stats["findings_recovered"] += len(additions["filtered"])
        elif outcome == "lost":
            stats["findings_lost"] += sum(metadata[k] for k in _COUNT_KEYS)
        if write and additions:
            metadata.update(additions)
            _write(project_dir, path, payload)
            stats["files_written"] += 1
    return stats


def discover_projects(projects_root: Path) -> list[Path]:
    """Every project directory holding an ``evaluations/`` folder.

    rglob, not iterdir: books also live under hidden group directories
    (``projects/.published/``, ...). ``.bak`` directories are snapshots of
    another project and are skipped.
    """
    return sorted(
        p.parent
        for p in projects_root.rglob("evaluations")
        if p.is_dir() and ".bak" not in p.parent.name and p.parent.parent.name != "evaluations"
    )


def resolve_project(projects_root: Path, slug: str) -> Path:
    direct = projects_root / slug
    if direct.is_dir():
        return direct
    for path in sorted(projects_root.rglob(f"{slug}/evaluations")):
        return path.parent
    return direct


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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

    if not args.write:
        print("REPORT ONLY -- pass --write to modify files\n")

    totals: collections.Counter = collections.Counter()
    for project_dir in project_dirs:
        if not project_dir.exists():
            print(f"skip (missing): {project_dir}")
            continue
        stats = backfill_project(project_dir, args.write)
        if not stats:
            continue
        totals.update(stats)
        print(
            f"  {project_dir.name:<40} recovered {stats['recovered']:>3} "
            f"({stats['findings_recovered']:>3} findings)   zero-drop {stats['zero_drop']:>3}   "
            f"lost {stats['lost']:>3} ({stats['findings_lost']:>3})   "
            f"mismatched {stats['mismatched']:>2}   already {stats['already']:>3}   "
            f"legacy {stats['legacy']:>3}"
        )

    print()
    print("=== totals ===")
    for key in sorted(totals):
        print(f"  {key}: {totals[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
