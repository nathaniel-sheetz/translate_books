#!/usr/bin/env python3
"""
Delete coded findings that landed inside an ``[IMAGE:...]`` placeholder.

These are stale data, not a live bug. ``strip_image_placeholders``
(``src/utils/text_utils.py``) blanks image tokens offset-preservingly before
the coded evaluators read the text, and both ``grammar_eval`` and
``dictionary_eval`` have called it since commit ``6b99fc7`` (2026-05-14).
Findings persisted before that date can still quote the token's own characters
("IMAGE", the file stem), and the reader has no sentence to attach them to —
the alignment drops ``[IMAGE:...]`` rows — so they inflate every chapter chip
with items nothing can act on. One pass removes them; a re-run of grammar would
not.

Why deletion is safe here and a rerun is not: a coded finding carries a
**stored** ``issue_index`` (``src/evaluators/location_normalizer.py``) that the
dismissal ledger in ``_feedback.jsonl`` keys on, so removing entries leaves
every survivor's index — and therefore every recorded dismissal — pointing at
the same finding. Judge ``issue_index`` is positional (``enumerate`` over the
issues list at read time), which is exactly why judges are left alone here.

Usage:
    python scripts/purge_image_placeholder_findings.py                 # dry run
    python scripts/purge_image_placeholder_findings.py --apply
    python scripts/purge_image_placeholder_findings.py --project home-geography
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.text_utils import _IMAGE_PLACEHOLDER_RE  # noqa: E402


def image_spans(text: str) -> list[tuple[int, int]]:
    """Half-open ``[start, end)`` spans of every ``[IMAGE:...]`` token."""
    return [(m.start(), m.end()) for m in _IMAGE_PLACEHOLDER_RE.finditer(text)]


def inside_any(spans: list[tuple[int, int]], offset: int) -> bool:
    return any(start <= offset < end for start, end in spans)


def clean_project(project_dir: Path, apply: bool) -> tuple[int, dict[str, int]]:
    """Strip image-placeholder findings from one project.

    Returns ``(total_removed, {eval_name: count})``. With ``apply`` false
    nothing is written.
    """
    eval_dir = project_dir / "evaluations"
    chunks_dir = project_dir / "chunks"
    if not eval_dir.is_dir():
        return 0, {}

    total = 0
    by_eval: dict[str, int] = {}

    for path in sorted(eval_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        issues = payload.get("normalized_issues")
        if not isinstance(issues, list) or not issues:
            continue

        chunk_path = chunks_dir / f"{payload.get('chunk_id') or path.stem}.json"
        if not chunk_path.exists():
            continue
        try:
            chunk = json.loads(chunk_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        spans = image_spans(chunk.get("translated_text") or "")
        if not spans:
            continue

        kept = []
        removed_here = 0
        for issue in issues:
            loc = issue.get("location") or {}
            char_start = loc.get("char_start")
            if (
                loc.get("side") == "target"
                and isinstance(char_start, int)
                and inside_any(spans, char_start)
            ):
                name = issue.get("eval_name") or "?"
                by_eval[name] = by_eval.get(name, 0) + 1
                removed_here += 1
                continue
            kept.append(issue)

        if not removed_here:
            continue
        total += removed_here
        if apply:
            # Survivors keep their stored issue_index verbatim — renumbering
            # would silently re-point every dismissal recorded for this chunk.
            payload["normalized_issues"] = kept
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    return total, by_eval


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--projects-dir", default="projects", help="Root holding project folders"
    )
    parser.add_argument(
        "--project", action="append", help="Limit to this project id (repeatable)"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Write the changes (default: dry run)"
    )
    args = parser.parse_args()

    root = Path(args.projects_dir)
    if not root.is_dir():
        print(f"No such directory: {root}")
        return 1

    wanted = set(args.project or [])
    grand_total = 0
    grand_by_eval: dict[str, int] = {}

    for project_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if wanted and project_dir.name not in wanted:
            continue
        removed, by_eval = clean_project(project_dir, args.apply)
        if not removed:
            continue
        grand_total += removed
        for name, n in by_eval.items():
            grand_by_eval[name] = grand_by_eval.get(name, 0) + n
        detail = ", ".join(f"{n} {name}" for name, n in sorted(by_eval.items()))
        print(f"{project_dir.name:<32} {removed:>4}  ({detail})")

    verb = "Removed" if args.apply else "Would remove"
    detail = ", ".join(f"{n} {name}" for name, n in sorted(grand_by_eval.items()))
    print(f"\n{verb} {grand_total} finding(s){': ' + detail if detail else ''}")
    if not args.apply and grand_total:
        print("Dry run - re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
