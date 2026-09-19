"""Score the triage filter against the human marks, and set its cutoff.

This is the only thing that decides whether the triage pass may be trusted, and
the only place ``TRIAGE_CONFIDENCE_FLOOR`` comes from. It replays every recorded
triage verdict against the labelled corpus in ``_feedback.jsonl`` and reports
two numbers at each candidate floor:

1. **Recall guard — the veto.** Of the findings a human marked ``resolved``
   (they agreed it was a real defect and fixed the text), how many would triage
   have suppressed? **This must be 0.** A suppressed real defect is lost
   silently: nobody sees it, nobody learns of it, and no later pass revisits it.
   Any floor with a non-zero count here is not a candidate, whatever it buys.

2. **Benefit.** Of the findings a human marked ``false_positive``, how many
   would triage have suppressed? This is the hand-clearing work the pass saves,
   and it is the only reason to run it at all.

The asymmetry is the whole design. Letting a false positive through costs one
glance; suppressing a real defect costs the defect. So the floor is chosen as
the *lowest* one that still loses nothing, and when in doubt it goes up.

**The exam books stay out of calibration.** ``wonder-book-of-horses``,
``the-little-duke`` and ``bambi-a-life-in-the-woods`` are the frozen holdout
(``docs/design/quality-automation-phase0-progress.md`` §4, rule 3: "Never tune a
judge on an exam book"). Nothing in the code enforces that list, so pass them to
``--exclude-project`` when calibrating; ``--exam`` does it for you.

**What "scoreable" means here.** A finding is scored only when it carries both a
human mark and a triage verdict. ``unscored_labels`` counts the labelled
findings no verdict exists for — it is *not* a count of marks whose pre-edit text
could not be recovered, which nothing in this script checks. Because ``prepare``
skips every finding a human has already marked, a fresh wave and the labelled
corpus begin with zero overlap, so the sweep stays structural until some of the
triaged set has been marked.

Costs nothing to run: it reads verdicts already on disk and calls no model.

Usage:
    python scripts/replay_triage.py --exam
    python scripts/replay_triage.py --project fabre2
    python scripts/replay_triage.py --exam --out report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

from scripts.ledger_census import discover_projects  # noqa: E402
from web_ui.evaluations import (  # noqa: E402
    TRIAGE_CONFIDENCE_FLOOR,
    build_triaged,
    load_all_feedback_by_chunk,
    load_all_triage_by_chunk,
)

#: The frozen holdout. Never tune on these; see the module docstring.
EXAM_BOOKS: tuple[str, ...] = (
    "wonder-book-of-horses",
    "the-little-duke",
    "bambi-a-life-in-the-woods",
)

#: The checkers this pass triages.
EVAL_NAMES: tuple[str, ...] = ("dictionary", "grammar")

#: Only these two labels are ground truth. `bad_message` and
#: `missing_context_gap` say the finding was real but poorly reported — a
#: different axis, and counting them either way would bias the guard.
REAL = "resolved"
NOISE = "false_positive"

#: Floors to report. Fine-grained near the top, where the answer lives.
_FLOORS = (0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 0.99)


def marked_findings(project_dir: Path) -> dict[tuple[str, str, str], str]:
    """``{(chunk_id, eval_name, issue_key): label}`` for the labelled findings.

    Keyed by chunk as well as by content, because ``issue_key`` is only unique
    *within* a chunk: it hashes ``(eval_name, severity, message, location)``, and
    for these two checkers ``location`` is a character offset. The same unknown
    word at the same offset in two chapters therefore hashes identically, and
    flattening the book into one map would let one chunk's label overwrite
    another's. Every other reader of this key — ``app.py`` included — builds the
    map one chunk at a time for exactly this reason.

    Last mark wins *within a chunk*, matching ``build_dismissed``: the file is
    append-only and a finding can be re-marked, so the label standing now is the
    one to score against. Records with no ``issue_key`` (written before the key
    existed) are skipped — they can only be matched positionally, and a position
    means nothing once an evaluator has re-run.
    """
    out: dict[tuple[str, str, str], str] = {}
    for chunk_id, records in load_all_feedback_by_chunk(project_dir).items():
        for record in records:
            eval_name = record.get("eval_name")
            key = record.get("issue_key")
            label = record.get("feedback_type")
            if eval_name in EVAL_NAMES and key and label in (REAL, NOISE):
                out[(chunk_id, eval_name, key)] = label
    return out


def triage_verdicts(project_dir: Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    """``{(chunk_id, eval_name, issue_key): verdict}`` for every triaged finding.

    Chunk-keyed for the reason :func:`marked_findings` gives: ``build_triaged``
    indexes one chunk's records, and ``update``-ing every chunk into one map let
    a verdict in the last chapter silently replace the verdict on an identically
    worded finding in an earlier one.
    """
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for chunk_id, records in load_all_triage_by_chunk(project_dir).items():
        for (eval_name, key), record in build_triaged(records).items():
            out[(chunk_id, eval_name, key)] = record
    return out


def score_project(project_dir: Path) -> dict[str, Any]:
    """Join one book's verdicts onto its labels and tally them per floor."""
    labels = marked_findings(project_dir)
    verdicts = triage_verdicts(project_dir)

    joined = [
        (label, verdicts[identity])
        for identity, label in labels.items()
        if identity in verdicts
    ]
    per_floor: dict[str, dict[str, int]] = {}
    for floor in _FLOORS:
        lost = sum(
            1 for label, v in joined
            if label == REAL and v.get("verdict") == "suppress"
            and float(v.get("confidence") or 0.0) >= floor
        )
        removed = sum(
            1 for label, v in joined
            if label == NOISE and v.get("verdict") == "suppress"
            and float(v.get("confidence") or 0.0) >= floor
        )
        per_floor[f"{floor:.2f}"] = {"lost": lost, "removed": removed}

    return {
        "project": project_dir.name,
        "labelled": len(labels),
        "real": sum(1 for lbl in labels.values() if lbl == REAL),
        "noise": sum(1 for lbl in labels.values() if lbl == NOISE),
        "triaged": len(verdicts),
        "scored": len(joined),
        # The veto's evidence, counted apart from the join: "lost 0" is only a
        # fact about a floor if some scored finding was a real defect to begin
        # with. Without this a sweep over nothing but false positives reads
        # exactly like a sweep that proved the floor safe.
        "scored_real": sum(1 for label, _ in joined if label == REAL),
        "unscored_labels": len(labels) - len(joined),
        "per_floor": per_floor,
    }


def _totals(reports: list[dict[str, Any]]) -> dict[str, Any]:
    total: dict[str, Any] = {
        key: sum(r[key] for r in reports)
        for key in ("labelled", "real", "noise", "triaged", "scored",
                    "scored_real", "unscored_labels")
    }
    total["per_floor"] = {
        f"{floor:.2f}": {
            "lost": sum(r["per_floor"][f"{floor:.2f}"]["lost"] for r in reports),
            "removed": sum(r["per_floor"][f"{floor:.2f}"]["removed"] for r in reports),
        }
        for floor in _FLOORS
    }
    return total


def recommended_floor(total: dict[str, Any]) -> Optional[str]:
    """The lowest floor that loses no real defect, or ``None``.

    ``None`` also when there is nothing to choose on. With an empty join every
    floor trivially loses zero real defects, so the old version answered "0.50
    is safe" to a sweep that had scored nothing — and 0.50 is the bottom of the
    table, the largest suppression this pass can be asked to do. That is the
    normal state right after a commit, since ``prepare`` skips already-marked
    findings and the wave it just judged is therefore entirely unmarked.

    Requiring a ``resolved`` row and not merely a join is the same argument one
    step further: the veto is *real defects lost*, and a join made only of
    false positives cannot test it.
    """
    if not total.get("scored") or not total.get("scored_real"):
        return None
    for floor in _FLOORS:
        if total["per_floor"][f"{floor:.2f}"]["lost"] == 0:
            return f"{floor:.2f}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", action="append", help="Project slug (repeatable)")
    parser.add_argument("--exclude-project", action="append", default=[],
                        help="Project slug to leave out (repeatable)")
    parser.add_argument("--exam", action="store_true",
                        help=f"also exclude the exam books: {', '.join(EXAM_BOOKS)}")
    parser.add_argument("--out", help="Write the full report as JSON to this path")
    args = parser.parse_args()

    excluded = set(args.exclude_project)
    if args.exam:
        excluded |= set(EXAM_BOOKS)

    projects = discover_projects(REPO_ROOT / "projects")
    if args.project:
        wanted = set(args.project)
        projects = [p for p in projects if p.name in wanted]
    projects = [p for p in projects if p.name not in excluded]

    reports = [score_project(p) for p in projects]
    reports = [r for r in reports if r["labelled"] or r["triaged"]]
    if not reports:
        print("No labelled or triaged coded findings in the selected books.")
        print("Run `scripts/run_triage.py` on a book first, then re-run this.")
        return 0

    total = _totals(reports)

    print()
    print("=== corpus ===")
    if excluded:
        print(f"  excluded: {', '.join(sorted(excluded))}")
    print(f"  books:                 {len(reports)}")
    print(f"  labelled findings:     {total['labelled']} "
          f"({total['real']} resolved, {total['noise']} false positive)")
    print(f"  triage verdicts:       {total['triaged']}")
    print(f"  joined (scoreable):    {total['scored']} "
          f"({total['scored_real']} resolved — the veto's evidence)")
    print(f"  labelled, not triaged: {total['unscored_labels']}")

    print()
    print("=== floor sweep ===")
    print(f"  {'floor':>6}  {'REAL LOST':>9}  {'noise removed':>13}  {'of noise':>8}")
    for floor in _FLOORS:
        row = total["per_floor"][f"{floor:.2f}"]
        share = f"{100 * row['removed'] / total['noise']:.1f}%" if total["noise"] else "n/a"
        flag = "  <-- VETO" if row["lost"] else ""
        print(f"  {floor:>6.2f}  {row['lost']:>9}  {row['removed']:>13}  {share:>8}{flag}")

    # Why no floor can be recommended comes first, so a reader never meets the
    # recommendation before the reason it is missing.
    print()
    if total["scored"] == 0:
        print("  Nothing was scoreable: no finding carries both a human mark and a")
        print("  triage verdict yet. The sweep above is structural, not evidence.")
    elif total["scored_real"] == 0:
        print("  Nothing scoreable is a real defect: every joined finding is marked")
        print("  `false_positive`, so the sweep measures benefit and not the veto.")

    recommended = recommended_floor(total)
    if recommended is None and not total["scored_real"]:
        print("  No floor is recommended — there is no evidence to choose one on.")
        print(f"  TRIAGE_CONFIDENCE_FLOOR stays at {TRIAGE_CONFIDENCE_FLOOR:.2f}. Mark some")
        print("  of the triaged findings on the recommendations screen, then re-run.")
    elif recommended is None:
        print("  No floor loses zero real defects. Do not ship suppression:")
        print("  tune the prompt until the top floor is clean.")
    else:
        row = total["per_floor"][recommended]
        print(f"  Lowest floor losing no real defect: {recommended} "
              f"(removes {row['removed']} of {total['noise']} false positives)")
        print(f"  TRIAGE_CONFIDENCE_FLOOR is currently {TRIAGE_CONFIDENCE_FLOOR:.2f}")
        if float(recommended) > TRIAGE_CONFIDENCE_FLOOR:
            print("  ! The current floor is BELOW the safe one — it would lose a real defect.")

    print()
    print("=== per book ===")
    for report in sorted(reports, key=lambda r: -r["scored"]):
        print(f"  {report['project']:<36} labelled {report['labelled']:>4}  "
              f"triaged {report['triaged']:>4}  scored {report['scored']:>4}")

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {"total": total, "recommended_floor": recommended,
                 "current_floor": TRIAGE_CONFIDENCE_FLOOR,
                 "excluded": sorted(excluded), "projects": reports},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
