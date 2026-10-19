"""Re-key triage verdicts and dismissals that an edit orphaned.

The coded checkers write a character offset into ``location``, and
``issue_key`` hashes ``location``, so editing a chunk gave every finding below
the edit a new key. The verdicts and marks standing on the old keys stopped
matching anything, and the findings came back as open work. Re-evaluation now
carries marks forward itself (``web_ui.evaluations._carry_marks_forward``);
this recovers the ones orphaned before it did.

The evaluation those records were written against has been overwritten, so the
match is made from what each record carries:

- a triage record keeps ``term`` and ``rule_id`` (that is why it stores them).
  A dictionary verdict joins on the flagged word; a grammar verdict on the
  matched text, and on ``rule_id`` too when both sides have one.
- a feedback record keeps ``message``. It joins on the message with its
  occurrence count and offsets stripped (``finding_anchor``), and only for the
  labels that say a finding was wrong (``CARRIED_FEEDBACK_TYPES``).

A record is re-keyed only when it names exactly one current finding, that
finding has no record of its own in the same sidecar, and no other orphan
claims it too. Everything else is counted and left alone: a re-ask costs a
glance, a verdict on the wrong finding hides a real defect.

Re-keying appends a copy under the new key with ``carried_from`` set, the same
record re-evaluation writes, so no line is rewritten and there is nothing to
back up. Running it twice writes nothing the second time.

Only records written before carry-forward shipped are considered (--before):
a later one still on a dead key was dropped by carry-forward on purpose, and
the grammar match here, ignoring the sentence context, would put it back.

Runs in report-only mode by default; pass --write to append anything.

Usage:
    python scripts/rekey_orphaned_marks.py
    python scripts/rekey_orphaned_marks.py --project horses-of-destiny
    python scripts/rekey_orphaned_marks.py --project horses-of-destiny --write
"""

from __future__ import annotations

import argparse
import collections
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from web_ui.evaluations import (  # noqa: E402
    CARRIED_FEEDBACK_TYPES,
    CODED_EVAL_NAMES,
    append_feedback,
    append_triage,
    build_dismissed,
    build_triaged,
    finding_anchor,
    issue_key,
    issue_term,
    load_all_feedback_by_chunk,
    load_all_triage_by_chunk,
    load_chunk_evaluation,
)

_SIDECARS = ("_triage.jsonl", "_feedback.jsonl")

# Records from here on were written while re-evaluation carried marks itself,
# so one still on a dead key was dropped on purpose and must stay dropped.
CARRY_FORWARD_SHIPPED = "2026-10-18"


def current_findings(evaluation: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """``{(eval_name, issue_key): first occurrence}`` for the coded findings."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for issue in evaluation.get("normalized_issues") or []:
        if not isinstance(issue, dict) or issue.get("eval_name") not in CODED_EVAL_NAMES:
            continue
        name = issue["eval_name"]
        out.setdefault((name, issue_key(name, issue)), issue)
    return out


def _match_text(issue: dict[str, Any]) -> str:
    location = issue.get("location")
    return (location.get("match") if isinstance(location, dict) else None) or ""


def triage_candidates(
    record: dict[str, Any], findings: dict[tuple[str, str], dict[str, Any]]
) -> list[tuple[str, str]]:
    """The current findings a triage record's term (and rule) names."""
    name = record.get("eval_name")
    term = record.get("term")
    if not term:
        return []
    rule = record.get("rule_id")
    out = []
    for key, issue in findings.items():
        if key[0] != name:
            continue
        if name == "dictionary":
            if issue_term(name, issue) != term:
                continue
        else:
            if (issue_term(name, issue) or _match_text(issue)) != term:
                continue
            if rule and issue.get("rule_id") and issue.get("rule_id") != rule:
                continue
        out.append(key)
    return out


def feedback_candidates(
    record: dict[str, Any], findings: dict[tuple[str, str], dict[str, Any]]
) -> list[tuple[str, str]]:
    """The current findings a feedback record's message names, offsets aside."""
    name = record.get("eval_name")
    if not record.get("message"):
        return []
    wanted = finding_anchor(name, {"message": record["message"]})[3]
    return [
        key for key, issue in findings.items()
        if key[0] == name and finding_anchor(name, issue)[3] == wanted
    ]


def _written_before(record: dict[str, Any], before: Optional[datetime]) -> bool:
    if before is None:
        return True
    try:
        return datetime.fromisoformat(str(record.get("ts"))) < before
    except ValueError:
        # No usable stamp: older than anything that stamps, so old enough.
        return True


def plan_chunk(
    movable: dict[tuple[str, str], dict[str, Any]],
    occupied: dict[tuple[str, str], dict[str, Any]],
    findings: dict[tuple[str, str], dict[str, Any]],
    candidates,
    stats: collections.Counter,
    label: str,
    before: Optional[datetime] = None,
) -> list[tuple[dict[str, Any], tuple[str, str]]]:
    """Pair each orphaned record in one sidecar with the finding it now names.

    ``movable`` is the standing records that may be re-keyed; ``occupied`` is
    every standing record in the sidecar, since a finding holding any record of
    its own must not gain another. A record already carried to a newer key is
    not an orphan, and one written on or after ``before`` was left behind by
    carry-forward on purpose (an edit inside a grammar sentence), so neither is
    re-keyed.
    """
    carried = {
        (r.get("eval_name"), r["carried_from"])
        for r in occupied.values() if r.get("carried_from")
    }
    claims: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for key, record in movable.items():
        if key[0] not in CODED_EVAL_NAMES or key in findings:
            continue
        if key in carried:
            stats[f"{label}_already_carried"] += 1
            continue
        if not _written_before(record, before):
            stats[f"{label}_after_cutoff"] += 1
            continue
        stats[f"{label}_orphaned"] += 1
        found = candidates(record, findings)
        if not found:
            stats[f"{label}_no_match"] += 1
        elif len(found) > 1:
            stats[f"{label}_ambiguous"] += 1
        elif found[0] in occupied:
            stats[f"{label}_target_has_record"] += 1
        else:
            claims[found[0]].append(record)
    pairs = []
    for target, records in claims.items():
        if len(records) > 1:
            stats[f"{label}_contested"] += len(records)
            continue
        pairs.append((records[0], target))
    return pairs


def rekey_project(
    project_dir: Path, write: bool, before: Optional[datetime] = None,
) -> collections.Counter:
    stats: collections.Counter = collections.Counter()
    triage_by_chunk = load_all_triage_by_chunk(project_dir)
    feedback_by_chunk = load_all_feedback_by_chunk(project_dir)

    for chunk_id in sorted(set(triage_by_chunk) | set(feedback_by_chunk)):
        evaluation = load_chunk_evaluation(project_dir, chunk_id)
        if not evaluation:
            continue
        findings = current_findings(evaluation)

        triaged = build_triaged(triage_by_chunk.get(chunk_id, []))
        for record, (name, key) in plan_chunk(
            triaged, triaged, findings, triage_candidates, stats, "triage", before,
        ):
            stats["triage_rekeyed"] += 1
            print(f"    {chunk_id}  triage  {name:<10} {record.get('term')!r} "
                  f"{record.get('verdict')}  {record.get('issue_key')} -> {key}")
            if write:
                append_triage(
                    project_dir, chunk_id, name, findings[(name, key)].get("issue_index"),
                    record.get("verdict"),
                    key=key,
                    confidence=record.get("confidence"),
                    reason=record.get("reason"),
                    term=record.get("term"),
                    rule_id=record.get("rule_id"),
                    model=record.get("model"),
                    prompt_version=record.get("prompt_version"),
                    run_id=record.get("run_id"),
                    carried_from=record.get("issue_key"),
                )

        by_key, _ = build_dismissed(feedback_by_chunk.get(chunk_id, []))
        carryable = {
            k: r for k, r in by_key.items()
            if r.get("feedback_type") in CARRIED_FEEDBACK_TYPES
        }
        stats["feedback_not_carryable"] += sum(
            1 for k in by_key
            if k not in carryable and k[0] in CODED_EVAL_NAMES and k not in findings
        )
        for record, (name, key) in plan_chunk(
            carryable, by_key, findings, feedback_candidates, stats, "feedback", before,
        ):
            stats["feedback_rekeyed"] += 1
            print(f"    {chunk_id}  mark    {name:<10} {record.get('feedback_type')}  "
                  f"{record.get('issue_key')} -> {key}")
            if write:
                append_feedback(
                    project_dir, chunk_id, name, findings[(name, key)].get("issue_index"),
                    record.get("feedback_type"),
                    message=findings[(name, key)].get("message"),
                    note=record.get("note"),
                    key=key,
                    carried_from=record.get("issue_key"),
                )
    return stats


def discover_projects(projects_root: Path) -> list[Path]:
    """Every project holding either sidecar, group directories included.

    Same walk as ``backfill_feedback_keys.discover_projects``, widened to the
    triage sidecar: a book can be triaged before anyone marks a finding in it.
    """
    found = {
        p.parent.parent
        for name in _SIDECARS
        for p in projects_root.rglob(f"evaluations/{name}")
        if ".bak" not in p.parent.parent.name
    }
    return sorted(found)


def resolve_project(projects_root: Path, slug: str) -> Path:
    direct = projects_root / slug
    if direct.is_dir():
        return direct
    for name in _SIDECARS:
        for path in sorted(projects_root.rglob(f"{slug}/evaluations/{name}")):
            return path.parent.parent
    return direct


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", action="append", help="Project slug (repeatable)")
    parser.add_argument("--write", action="store_true",
                        help="Append the re-keyed records (default: report only)")
    parser.add_argument("--before", default=CARRY_FORWARD_SHIPPED,
                        help="Only re-key records written before this ISO date/time "
                             f"(default {CARRY_FORWARD_SHIPPED}, when re-evaluation "
                             "began carrying marks itself)")
    args = parser.parse_args(argv)
    try:
        before = datetime.fromisoformat(args.before)
    except ValueError:
        parser.error(f"--before is not an ISO date/time: {args.before!r}")

    projects_root = REPO_ROOT / "projects"
    if args.project:
        project_dirs = [resolve_project(projects_root, slug) for slug in args.project]
    else:
        project_dirs = discover_projects(projects_root)

    if not args.write:
        print("REPORT ONLY -- pass --write to append records\n")

    totals: collections.Counter = collections.Counter()
    for project_dir in project_dirs:
        if not project_dir.exists():
            print(f"skip (missing): {project_dir}")
            continue
        print(f"  {project_dir.name}")
        stats = rekey_project(project_dir, args.write, before)
        totals.update(stats)
        if stats:
            print("      " + "  ".join(f"{k}={v}" for k, v in sorted(stats.items())))

    print("\n=== totals ===")
    for key in sorted(totals):
        print(f"  {key}: {totals[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
