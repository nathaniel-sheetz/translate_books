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

``--freeze DIR`` writes the exam snapshot for the books named by ``--project``.
Per book, ``edits.jsonl`` holds the export rows, each marked with whether its
``es_before`` appears in the original translation. ``chunks.jsonl`` holds each
chunk's source, the LLM translation its ``last_llm_log`` recorded, and the text
at freeze time. ``manifest.json`` holds counts and file hashes. The replay reads
the snapshot, never the live book, so the books can keep being improved. The
directory must be new; ``exam/`` is gitignored for it.

Usage:
    python scripts/ledger_census.py
    python scripts/ledger_census.py --project bambi-a-life-in-the-woods
    python scripts/ledger_census.py --json census.json --export audit_input.jsonl
    python scripts/ledger_census.py --project the-little-duke --project bambi-a-life-in-the-woods \\
        --freeze exam/2026-09-14
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.corrections_apply import CORRECTIONS_APPLIED_FILENAME  # noqa: E402

RETRANSLATIONS_FILENAME = "retranslations.jsonl"

#: Group directories left out of the census: parked books, and in one case a
#: pre-redo copy that repeats a live book's edits.
_EXCLUDED_GROUPS = frozenset({".backburner"})

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
    count the same edits twice. Parked groups are not part of the corpus at all.
    """
    found = []
    for path in projects_root.rglob(CORRECTIONS_APPLIED_FILENAME):
        project_dir = path.parent
        parts = project_dir.relative_to(projects_root).parts
        top_level = len(parts) == 1
        grouped = (
            len(parts) == 2
            and parts[0].startswith(".")
            and parts[0] not in _EXCLUDED_GROUPS
        )
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


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


#: Leading characters of a chunk's source its log prompt must carry to count as
#: that chunk's original.
_SOURCE_CHECK_CHARS = 200


def original_translation(chunk: dict) -> tuple[Optional[str], dict[str, Any]]:
    """A chunk's translation as the LLM returned it, from its ``last_llm_log``.

    Edits leave the pointer alone, so this is the text before any reader or judge
    change. The log must be for this chunk: the same ``chunk_id``, and a prompt
    carrying the chunk's source. ``project_slug`` is recorded but not required
    to match, because a chapter translated in a copy of the book and carried over
    is still the text the reader read. Anything else gives ``None`` rather than
    a wrong baseline.
    """
    rel = chunk.get("last_llm_log")
    info: dict[str, Any] = {
        "llm_log": rel, "log_project_slug": None, "model": None, "translated_at": None,
    }
    if not rel:
        return None, info
    try:
        doc = json.loads((_REPO_ROOT / rel).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, info
    meta = doc.get("metadata") or {}
    info["log_project_slug"] = meta.get("project_slug")
    info["model"] = meta.get("model")
    info["translated_at"] = meta.get("timestamp")
    source_head = _norm(chunk.get("source_text"))[:_SOURCE_CHECK_CHARS]
    same_chunk = (
        meta.get("chunk_id") in (None, chunk.get("id"))
        and bool(source_head)
        and source_head in _norm(doc.get("prompt"))
    )
    if doc.get("response") is None or not same_chunk:
        return None, info
    return doc["response"], info


def freeze_book(projects_root: Path, project_dir: Path, out_dir: Path) -> dict[str, Any]:
    """Write one book's exam snapshot under ``out_dir/<slug>/`` and summarize it."""
    project_id = project_dir.name
    _, edits = census_project(projects_root, project_dir)

    chunks, originals = [], {}
    for path in sorted((project_dir / "chunks").glob("*.json")):
        chunk = json.loads(path.read_text(encoding="utf-8"))
        chunk_id = chunk.get("id") or path.stem
        original, info = original_translation(chunk)
        if original is not None:
            originals[chunk_id] = _norm(original)
        chunks.append({
            "chunk_id": chunk_id,
            "chapter_id": chunk.get("chapter_id"),
            "position": chunk.get("position"),
            "source_text": chunk.get("source_text") or "",
            "original_translation": original,
            "translation_at_freeze": chunk.get("translated_text") or "",
            **info,
        })
    # An edit to a sentence that had already changed since translation (an
    # earlier edit, a judge fix, a re-split) cannot be replayed on the original.
    for edit in edits:
        before = _norm(edit["es_before"])
        edit["before_in_original"] = bool(before) and before in originals.get(edit["chunk_id"], "")

    book_dir = out_dir / project_id
    book_dir.mkdir(parents=True)
    written = {"edits.jsonl": edits, "chunks.jsonl": chunks}
    for name, rows in written.items():
        with open(book_dir / name, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {
        "project_id": project_id,
        "path": project_dir.relative_to(projects_root).as_posix(),
        "edits": len(edits),
        "edits_before_in_original": sum(1 for e in edits if e["before_in_original"]),
        "chunks": len(chunks),
        "chunks_missing_original": sum(1 for c in chunks if c["original_translation"] is None),
        "sha256": {name: _sha256(book_dir / name) for name in written},
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--projects-root", type=Path, default=_REPO_ROOT / "projects",
        help="Directory holding the books (default: projects/).",
    )
    parser.add_argument(
        "--project", action="append",
        help="Only this book, by slug (found under hidden group directories too). Repeatable.",
    )
    parser.add_argument("--json", dest="json_out", type=Path, help="Write the report as JSON.")
    parser.add_argument(
        "--export", type=Path,
        help="Write the audit input: one JSONL row per unique landed reader edit.",
    )
    parser.add_argument(
        "--freeze", type=Path,
        help="Write the exam snapshot of the --project books into this new directory.",
    )
    args = parser.parse_args(argv)

    if args.freeze and not args.project:
        parser.error("--freeze needs at least one --project")
    if args.freeze and args.freeze.exists() and (
        not args.freeze.is_dir() or any(args.freeze.iterdir())
    ):
        print(f"{args.freeze} is not an empty directory; a freeze always writes a new one", file=sys.stderr)
        return 1

    projects = discover_projects(args.projects_root)
    if args.project:
        wanted = set(args.project)
        projects = [p for p in projects if p.name in wanted]
        missing = sorted(wanted - {p.name for p in projects})
        if missing:
            print(f"No {CORRECTIONS_APPLIED_FILENAME} for: {', '.join(missing)}", file=sys.stderr)
            return 1
    if not projects:
        print(f"No {CORRECTIONS_APPLIED_FILENAME} under {args.projects_root}", file=sys.stderr)
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
    if args.freeze:
        summaries = [freeze_book(args.projects_root, p, args.freeze) for p in projects]
        version_file = _REPO_ROOT / "VERSION"
        manifest = {
            "frozen_at": datetime.now().isoformat(timespec="seconds"),
            "code_version": (
                version_file.read_text(encoding="utf-8").strip() if version_file.exists() else None
            ),
            "purpose": (
                "Phase 0 exam: replay judges against these books' original translations. "
                "Never tune a judge on these books."
            ),
            "books": summaries,
        }
        (args.freeze / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print(f"\nFroze {len(summaries)} book(s) into {args.freeze}")
        for s in summaries:
            print(
                f"  {s['project_id']}: {s['edits']} edits "
                f"({s['edits_before_in_original']} found in the original), "
                f"{s['chunks']} chunks ({s['chunks_missing_original']} without an original)"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
