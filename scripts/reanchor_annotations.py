#!/usr/bin/env python3
"""
Put reader annotations back on their sentences without realigning anything.

Every realign now re-anchors a chapter's notes on its own. This script is for
the notes an earlier realign left behind: it reads each note's saved sentence
(``es_text``), finds the row that holds it in the current alignment, and moves
the note there. It prints what it would do and writes nothing unless ``--apply``
is given. Rows are appended to ``annotations.jsonl``, never rewritten.

Notes saved before snapshots existed carry no sentence to match on, so they
cannot be checked. ``--backfill`` stamps each with the row it sits on now, which
is what the next realign would do. A note whose own ``[bracketed]`` word is not
in its row is reported as SUSPECT and never stamped.

Usage:
    python scripts/reanchor_annotations.py projects/five-little-peppers
    python scripts/reanchor_annotations.py projects/five-little-peppers --chapter chapter_06
    python scripts/reanchor_annotations.py projects/five-little-peppers --apply
    python scripts/reanchor_annotations.py projects/five-little-peppers --apply --backfill
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.annotations import reanchor, store  # noqa: E402
from src.annotations.anchors import parse_anchors  # noqa: E402


def _clip(text: str, width: int = 70) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _label(record: dict) -> str:
    return _clip(record.get("content") or "", 32) or "(empty note)"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Move reader annotations onto the rows their sentences now occupy"
    )
    parser.add_argument("project_dir", type=Path, help="Path to project directory")
    parser.add_argument("--chapter", action="append", help="Limit to this chapter id (repeatable)")
    parser.add_argument("--apply", action="store_true", help="Write the changes (default: report only)")
    parser.add_argument(
        "--backfill", action="store_true",
        help="Also stamp notes that have no saved sentence with the row they sit on now",
    )
    args = parser.parse_args()

    project_dir = args.project_dir.resolve()
    if not project_dir.exists():
        print(f"Error: {project_dir} not found", file=sys.stderr)
        return 1
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    chapters = args.chapter or sorted(
        {r.get("chapter_id") for r in store.load_active(project_dir) if r.get("chapter_id")}
    )
    totals = {"kept": 0, "moved": 0, "orphaned": 0, "refreshed": 0, "no_snapshot": 0, "suspect": 0}
    rows_written = 0

    for chapter_id in chapters:
        es_map = reanchor.load_es_map(project_dir, chapter_id)
        # The current alignment stands in for "before": a note with no saved
        # sentence is then read as sitting on the row it has now.
        plan = reanchor.plan_chapter(project_dir, chapter_id, es_map)
        no_snapshot = len(plan.backfilled)
        if not args.backfill:
            plan.backfilled = []

        lines = []
        for move in sorted(plan.moved, key=lambda m: m.old_idx):
            lines.append(
                f"  MOVE    {move.old_idx:>4} -> {move.new_idx:<4} [{move.tier}] {_label(move.record)}\n"
                f"          {_clip(es_map.get(move.new_idx, ''))}"
            )
        for record in plan.orphaned:
            lines.append(
                f"  ORPHAN  {record.get('es_idx'):>4}         {_label(record)}\n"
                f"          saved sentence: {_clip(record.get('es_text') or '') or '(none)'}"
            )
        for record in plan.suspect:
            idx = reanchor.as_es_idx(record.get("es_idx"))
            words = [w.casefold() for w in parse_anchors(record.get("content") or "")]
            elsewhere = sorted(
                i for i, text in es_map.items() if any(w in text.casefold() for w in words)
            )
            lines.append(
                f"  SUSPECT {idx:>4}         {_label(record)}\n"
                f"          its row reads: {_clip(es_map.get(idx, ''))}\n"
                f"          rows with that word: {elsewhere or 'none'}"
            )
        if lines:
            print(f"{chapter_id}")
            print("\n".join(lines))

        # Only a note with a saved sentence that matched its row is confirmed.
        totals["kept"] += plan.kept - no_snapshot - len(plan.suspect)
        totals["moved"] += len(plan.moved)
        totals["orphaned"] += len(plan.orphaned)
        totals["refreshed"] += len(plan.refreshed)
        totals["no_snapshot"] += no_snapshot
        totals["suspect"] += len(plan.suspect)
        if args.apply:
            rows_written += reanchor.apply_plan(project_dir, chapter_id, plan)

    print(
        f"\n{project_dir.name}: {totals['moved']} to move, {totals['orphaned']} orphaned, "
        f"{totals['kept']} confirmed in place "
        f"({totals['refreshed']} with an edited sentence to re-save)."
    )
    if totals["no_snapshot"] or totals["suspect"]:
        print(
            f"  {totals['no_snapshot']} more have no saved sentence to check"
            f"{' (stamped with their current row)' if args.backfill else ' (--backfill stamps them)'}; "
            f"{totals['suspect']} suspect."
        )
    if args.apply:
        print(f"  Wrote {rows_written} row(s) to {store.annotations_path(project_dir)}")
    else:
        print("  Dry run: nothing written. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
