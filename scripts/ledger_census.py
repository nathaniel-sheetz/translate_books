#!/usr/bin/env python3
"""Count every book's reader corrections, and export them for the ledger audit.

Phase 0 of ``docs/design/quality-automation-plan.md`` audits the reader's own
edits with a panel of models before anything is scored against them. This
script is the step before that: it says exactly how many edits there are, and
writes the rows the audit reads. It reads files and never calls a model.

Everything comes from ``corrections_applied.jsonl``. A row with a ``source`` was
written by an automated applier (``judge:<name>`` today); a row without one is
the reader's bottom-sheet Save, drained into the ledger by Apply or realign.
Per book:

- **reader** — every reader row in the ledger.
- **skip** — reader rows stamped ``status: "skipped"``: the edit never landed.
- **dup** — landed rows repeating an earlier ``(chunk_id, original_es,
  corrected_es)``. Saving twice before Apply writes two rows for one edit.
- **unique** — reader edits that landed, counted once. This is the audit set
  and the numerator of the per-1k rate.
- **auto** — rows with a ``source``.
- **aligned** — sentence rows across ``alignments/*.json``.
- **per 1k** — unique reader edits per 1,000 aligned rows (the shape of M1).
  The corpus total only counts books that have alignments.
- **retrans** — rows in ``retranslations.jsonl``. A retranslate-modal edit
  lands in the chunk without ever reaching the ledger, so these are reader
  edits the audit set does not contain. The chunk editor logs nothing at all.
- **native** — unique edits stamped ``verified_by: "native"``. A reader row
  without the field predates it and counts as ``self``.

``--export`` writes one JSONL row per unique edit, carrying the triple the panel
judges (``en``, ``es_before``, ``es_after``) and an ``audit_id`` hashed from the
edit itself, so verdicts still join back after a re-export.

Usage:
    python scripts/ledger_census.py
    python scripts/ledger_census.py --project bambi-a-life-in-the-woods
    python scripts/ledger_census.py --json census.json --export audit_input.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.corrections_apply import CORRECTIONS_APPLIED_FILENAME  # noqa: E402

RETRANSLATIONS_FILENAME = "retranslations.jsonl"

#: Hex digits kept from the sha1: ~48 bits, collision-free at corpus scale.
_AUDIT_ID_CHARS = 12

#: (column header, stats key) for the printed table.
_COLUMNS = (
    ("reader", "reader_rows"),
    ("skip", "skipped"),
    ("dup", "duplicates"),
    ("unique", "unique_edits"),
    ("auto", "automated_rows"),
    ("aligned", "aligned_rows"),
    ("per 1k", "per_1k"),
    ("retrans", "retranslations"),
    ("native", "native"),
)

_SUMMED = (
    "reader_rows", "skipped", "duplicates", "unique_edits", "automated_rows",
    "aligned_rows", "retranslations", "native", "malformed_lines",
)


def discover_projects(projects_root: Path) -> list[Path]:
    """Every book with a ledger, at ``projects/<slug>`` or ``projects/.<group>/<slug>``.

    Anything deeper is a copy kept inside a book rather than a book of its own,
    and a ``.bak`` directory is a snapshot of another book; counting either would
    count the same edits twice.
    """
    found = []
    for path in projects_root.rglob(CORRECTIONS_APPLIED_FILENAME):
        project_dir = path.parent
        parts = project_dir.relative_to(projects_root).parts
        top_level = len(parts) == 1
        grouped = len(parts) == 2 and parts[0].startswith(".")
        if (top_level or grouped) and ".bak" not in project_dir.name:
            found.append(project_dir)
    return sorted(found)


def _read_jsonl(path: Path) -> tuple[list[dict], int]:
    """The object rows of a JSONL file, and how many non-blank lines were not one."""
    if not path.exists():
        return [], 0
    rows, malformed = [], 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                malformed += 1
    return rows, malformed


def aligned_row_count(project_dir: Path) -> int:
    total = 0
    for path in sorted((project_dir / "alignments").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            total += len(data.get("alignments") or [])
    return total


def edit_key(row: dict) -> tuple[str, str, str]:
    return (
        row.get("chunk_id") or "",
        row.get("original_es") or "",
        row.get("corrected_es") or "",
    )


def audit_id(project_id: str, row: dict) -> str:
    """A hash of the edit, not of its position in the file, so it survives re-exports."""
    key = "\x1f".join((project_id, *edit_key(row)))
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:_AUDIT_ID_CHARS]


def _per_1k(edits: int, aligned: int) -> Optional[float]:
    return round(1000 * edits / aligned, 1) if aligned else None


def census_project(projects_root: Path, project_dir: Path) -> tuple[dict[str, Any], list[dict]]:
    """One book's stats, and its audit rows (one per unique landed reader edit)."""
    project_id = project_dir.name
    ledger, malformed = _read_jsonl(project_dir / CORRECTIONS_APPLIED_FILENAME)
    reader = [row for row in ledger if not row.get("source")]
    landed = [row for row in reader if row.get("status") != "skipped"]
    unique: dict[tuple[str, str, str], dict] = {}
    for row in landed:
        unique.setdefault(edit_key(row), row)
    retranslations, _ = _read_jsonl(project_dir / RETRANSLATIONS_FILENAME)
    aligned = aligned_row_count(project_dir)

    stats = {
        "project_id": project_id,
        "path": project_dir.relative_to(projects_root).as_posix(),
        "reader_rows": len(reader),
        "skipped": len(reader) - len(landed),
        "duplicates": len(landed) - len(unique),
        "unique_edits": len(unique),
        "automated_rows": len(ledger) - len(reader),
        "aligned_rows": aligned,
        "per_1k": _per_1k(len(unique), aligned),
        "retranslations": len(retranslations),
        "native": sum(1 for row in unique.values() if row.get("verified_by") == "native"),
        "malformed_lines": malformed,
    }
    export = [
        {
            "audit_id": audit_id(project_id, row),
            "project_id": project_id,
            "chapter_id": row.get("chapter_id"),
            "chunk_id": row.get("chunk_id") or "",
            "es_idx": row.get("es_idx"),
            "en": row.get("en_reference") or "",
            "es_before": row.get("original_es") or "",
            "es_after": row.get("corrected_es") or "",
            "timestamp": row.get("timestamp"),
            "applied_at": row.get("applied_at"),
            "verified_by": row.get("verified_by") or "self",
        }
        for row in unique.values()
    ]
    return stats, export


def summarize(stats: list[dict]) -> dict[str, Any]:
    totals: dict[str, Any] = {key: sum(s[key] for s in stats) for key in _SUMMED}
    rated_edits = sum(s["unique_edits"] for s in stats if s["aligned_rows"])
    totals["per_1k"] = _per_1k(rated_edits, totals["aligned_rows"])
    totals["projects"] = len(stats)
    return totals


def format_table(stats: list[dict], totals: dict) -> str:
    rows = sorted(stats, key=lambda s: (s["per_1k"] is None, -(s["per_1k"] or 0), s["path"]))
    width = max([len("TOTAL")] + [len(s["path"]) for s in rows]) + 2

    def line(label: str, values: dict) -> str:
        cells = "".join(
            f"{'-' if values[key] is None else values[key]:>9}" for _, key in _COLUMNS
        )
        return f"{label:<{width}}{cells}"

    header = f"{'book':<{width}}" + "".join(f"{title:>9}" for title, _ in _COLUMNS)
    out = [header, "-" * len(header)]
    out.extend(line(s["path"], s) for s in rows)
    out.append("-" * len(header))
    out.append(line("TOTAL", totals))
    return "\n".join(out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--projects-root", type=Path, default=_REPO_ROOT / "projects",
        help="Directory holding the books (default: projects/).",
    )
    parser.add_argument(
        "--project",
        help="Only this book, by slug (found under hidden group directories too).",
    )
    parser.add_argument("--json", dest="json_out", type=Path, help="Write the report as JSON.")
    parser.add_argument(
        "--export", type=Path,
        help="Write the audit input: one JSONL row per unique landed reader edit.",
    )
    args = parser.parse_args(argv)

    projects = discover_projects(args.projects_root)
    if args.project:
        projects = [p for p in projects if p.name == args.project]
    if not projects:
        scope = f" for {args.project}" if args.project else ""
        print(f"No {CORRECTIONS_APPLIED_FILENAME} under {args.projects_root}{scope}", file=sys.stderr)
        return 1

    stats, export = [], []
    for project_dir in projects:
        book_stats, rows = census_project(args.projects_root, project_dir)
        stats.append(book_stats)
        export.extend(rows)
        if book_stats["malformed_lines"]:
            print(
                f"warning: {book_stats['path']}: skipped "
                f"{book_stats['malformed_lines']} unparseable ledger line(s)",
                file=sys.stderr,
            )
    totals = summarize(stats)
    print(format_table(stats, totals))

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps({"projects": stats, "totals": totals}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.export:
        args.export.parent.mkdir(parents=True, exist_ok=True)
        with open(args.export, "w", encoding="utf-8") as f:
            for row in export:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"\nWrote {len(export)} audit rows to {args.export}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
