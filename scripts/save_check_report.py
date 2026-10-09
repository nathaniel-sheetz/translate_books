"""Report what the save-time check has warned about and what became of each warning.

The check (``src/save_check.py``) was sized on 1,759 labelled saves, where it
flagged about 1% of clean ones. This is the same question asked of live use:
per rule, how many warnings were acted on and how many were waved away.

A warning ends up in one of four states:

- ``fixed``      its sentence was edited again and what it flagged went with it
- ``ignored``    its word went on the book's ignore list
- ``dismissed``  the translator dismissed it
- ``open``       still showing, or its sentence was rewritten by a path the
                 check does not see and the flagged word is still in the chapter

A warning that a later one stands in for is not counted: the later one carries
what was still wrong, and its state is the state of both.

``fixed`` is the only state that says the warning was right. A rule whose
warnings are mostly ``dismissed`` or ``ignored`` is noise: switch it off under
``save_check.disabled_rules`` in ``app_config.json``.

The ``model`` row is the language model's verdict. Under the table, each model
that answered is listed with how many saves it scored and how many it flagged
(``save_check_readouts.jsonl``); ``--list`` shows the score beside a warning it
raised. A model that flags much more than one clean save in forty needs its
threshold raised under ``save_check.model.profiles``.

Saves are counted from the existing edit logs (``corrections.jsonl``,
``corrections_applied.jsonl``, ``retranslations.jsonl``) from ``--since``, which
defaults to the day of the first logged warning.

Usage:
    python scripts/save_check_report.py
    python scripts/save_check_report.py --project the-little-duke --since 2026-10-08
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src import save_check, save_check_model  # noqa: E402
from web_ui.evaluations import load_project_ignored_terms  # noqa: E402

STATES = ("fixed", "ignored", "dismissed", "open")


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def count_saves(project_dir: Path, since: str) -> int:
    """Manual writes the check saw: reader Saves and retranslate-modal replaces."""
    saves = _rows(project_dir / "corrections.jsonl")
    # An applied row with a ``source`` was written by a judge, not by hand.
    saves += [r for r in _rows(project_dir / "corrections_applied.jsonl") if not r.get("source")]
    saves += _rows(project_dir / "retranslations.jsonl")
    return sum(1 for r in saves if str(r.get("timestamp", "")) >= since)


def warning_state(warning: dict, outcomes: dict, rows: list[dict], ignored) -> str:
    outcome = outcomes.get(warning.get("id"))
    if outcome:
        return outcome.get("outcome", "dismissed")
    if (save_check.standing_row(warning, rows) is None
            and not save_check.flagged_text_stands(warning, rows)):
        return "fixed"
    left = [
        h for h in warning.get("hits", [])
        if not (h.get("rule") == "spelling" and ignored is not None
                and ignored.matches("dictionary", h.get("text")))
    ]
    return "open" if left else "ignored"


def readout_counts(project_dirs, since: str) -> dict:
    """Per model: ``[saves scored, saves flagged, seconds of every answer]``."""
    counts: dict = collections.defaultdict(lambda: [0, 0, []])
    for project_dir in project_dirs:
        for row in _rows(project_dir / save_check_model.READOUT_LOG_NAME):
            if str(row.get("timestamp", "")) < since:
                continue
            entry = counts[(row.get("model") or "?", row.get("threshold"))]
            entry[0] += 1
            entry[1] += bool(row.get("flagged"))
            if isinstance(row.get("seconds"), (int, float)):
                entry[2].append(row["seconds"])
    return counts


def alignment_rows(project_dir: Path, chapter_id: str, cache: dict) -> list[dict]:
    if chapter_id not in cache:
        try:
            data = json.loads(
                (project_dir / "alignments" / f"{chapter_id}.json").read_text(encoding="utf-8")
            )
            cache[chapter_id] = [r for r in data.get("alignments", []) if isinstance(r, dict)]
        except (OSError, json.JSONDecodeError):
            cache[chapter_id] = []
    return cache[chapter_id]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--projects-dir", type=Path, default=_REPO_ROOT / "projects")
    parser.add_argument("--project", help="only projects whose path contains this")
    parser.add_argument("--since", help="count saves and warnings from this ISO date")
    parser.add_argument("--list", action="store_true", help="print every warning with its state")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    logs = sorted(args.projects_dir.rglob(save_check.LOG_NAME))
    if args.project:
        logs = [p for p in logs if args.project in p.parent.as_posix()]
    books = {p.parent: save_check.load_log(p.parent) for p in logs}
    # Every book that was saved to counts, warned or not: a book the check
    # stayed silent on is the larger part of the rate.
    saved_to = {
        p.parent for name in ("corrections.jsonl", "corrections_applied.jsonl", "retranslations.jsonl")
        for p in args.projects_dir.rglob(name)
        if not args.project or args.project in p.parent.as_posix()
    }
    # A model that has scored saves and flagged none has readouts and no warning.
    stamps = [w.get("timestamp", "") for warnings, _ in books.values() for w in warnings]
    stamps += [
        str(row.get("timestamp", "")) for project_dir in saved_to | set(books)
        for row in _rows(project_dir / save_check_model.READOUT_LOG_NAME)
    ]
    stamps = [s for s in stamps if s]
    if not stamps:
        print("No save-check warnings have been logged.")
        return 0
    since = args.since or min(stamps)[:10]
    saves = sum(count_saves(project_dir, since) for project_dir in saved_to)

    per_rule: dict = collections.defaultdict(collections.Counter)
    total: collections.Counter = collections.Counter()
    for project_dir, (warnings, outcomes) in books.items():
        ignored = load_project_ignored_terms(project_dir)
        cache: dict = {}
        stood_in_for = save_check.superseded(warnings, outcomes)
        for w in warnings:
            if w.get("timestamp", "") < since or w.get("id") in stood_in_for:
                continue
            state = warning_state(
                w, outcomes, alignment_rows(project_dir, w.get("chapter_id", ""), cache), ignored
            )
            total[state] += 1
            for rule in {h.get("rule") for h in w.get("hits", [])}:
                per_rule[rule][state] += 1
            if args.list:
                what = ", ".join(f"{h.get('rule')}:{h.get('text')}" for h in w.get("hits", []))
                asked = w.get("model") or {}
                if asked.get("score") is not None:
                    what += f"  ({asked.get('model')} {asked['score']:+.1f})"
                print(f"{state:9} {project_dir.name[:24]:24} {w.get('chapter_id', ''):12} {what}")

    warned = sum(total.values())
    print(f"Since {since}: {saves} saves across {len(saved_to)} book(s), {warned} warnings in {len(books)} book(s)"
          + (f" (1 in {saves / warned:.0f} saves)" if warned and saves else ""))
    print(f"\n{'rule':24}" + "".join(f"{s:>10}" for s in ("warned",) + STATES))
    for rule in sorted(per_rule, key=lambda r: -sum(per_rule[r].values())):
        counts = per_rule[rule]
        print(f"{rule:24}{sum(counts.values()):>10}" + "".join(f"{counts[s]:>10}" for s in STATES))
    print(f"{'all warnings':24}{warned:>10}" + "".join(f"{total[s]:>10}" for s in STATES))

    readouts = readout_counts(saved_to | set(books), since)
    if readouts:
        print(f"\n{'model':24}{'warns above':>12}{'scored':>10}{'flagged':>10}{'rate':>8}{'median s':>10}")
        for (model, threshold), (scored, flagged, seconds) in sorted(readouts.items(), key=lambda kv: -kv[1][0]):
            median = f"{sorted(seconds)[len(seconds) // 2]:.2f}" if seconds else "-"
            above = "-" if threshold is None else f"{threshold:g}"
            print(f"{model[:24]:24}{above:>12}{scored:>10}{flagged:>10}{100 * flagged / scored:>7.1f}%{median:>10}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
