#!/usr/bin/env python3
"""
CLI for translation-difficulty scoring of a project.

Scores English source text on two deterministic signals — long-tail-weighted
sentence length and lexical rarity (wordfreq, glossary terms excluded) — at the
book and per-chapter level, and prints the suggested chunk target sizes. Results
are cached to ``projects/<id>/difficulty.json``.

Usage:
    python scripts/score_difficulty.py understood-betsy
    python scripts/score_difficulty.py understood-betsy --force
    python scripts/score_difficulty.py projects/understood-betsy --json

A higher difficulty (0–1) yields a smaller suggested target_size, so harder
text is chunked more finely. The dashboard "Analyze difficulty" button surfaces
the same numbers and lets you fill the target inputs with one click.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.difficulty_scorer import WORDFREQ_AVAILABLE, manifest_path, score_book


def _resolve_project_dir(project: str) -> Path:
    """Accept either a project id (folder under projects/) or a path."""
    p = Path(project)
    if p.exists():
        return p
    candidate = Path(__file__).parent.parent / "projects" / project
    if candidate.exists():
        return candidate
    print(f"Error: project not found: {project}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("project", help="Project id (under projects/) or path to a project dir")
    ap.add_argument("--force", action="store_true", help="Re-score even if cache is fresh")
    ap.add_argument("--json", action="store_true", help="Print the full manifest as JSON")
    args = ap.parse_args()

    project_dir = _resolve_project_dir(args.project)
    manifest = score_book(project_dir, force=args.force)

    if args.json:
        print(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2))
        return

    if not WORDFREQ_AVAILABLE:
        print("Warning: wordfreq not installed — rarity scores are 0.0 "
              "(install wordfreq for lexical-rarity scoring).", file=sys.stderr)

    b = manifest.book
    print(
        f"Book difficulty: {b.difficulty:.2f}  "
        f"(length {b.length_score:.2f}, rarity {b.rarity_score:.2f})  "
        f"-> suggested target {b.suggested_target_size}w"
    )
    print(
        f"  {b.sentence_length_weighted:.1f} w/sent (weighted), "
        f"{b.rare_word_fraction * 100:.1f}% rare, {b.sentence_count} sentences, "
        f"{b.word_count} words"
    )
    print()

    header = (
        f"{'chapter':<18}{'diff':>6}{'len':>6}{'rar':>6}"
        f"{'w/sent':>8}{'%rare':>7}{'target':>8}"
    )
    print(header)
    print("-" * len(header))
    for cd in manifest.chapters:
        m = cd.metrics
        print(
            f"{cd.chapter_id:<18}{m.difficulty:>6.2f}{m.length_score:>6.2f}"
            f"{m.rarity_score:>6.2f}{m.sentence_length_weighted:>8.1f}"
            f"{m.rare_word_fraction * 100:>7.1f}{m.suggested_target_size:>8}"
        )

    print()
    print(f"Cached to {manifest_relpath(project_dir)}")


def manifest_relpath(project_dir: Path) -> str:
    return str(manifest_path(project_dir))


if __name__ == "__main__":
    main()
