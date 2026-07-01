#!/usr/bin/env python3
"""
Apply reader corrections back to chunk files, then recombine and realign
affected chapters.

Reads corrections.jsonl from a project directory, patches the translated_text
in each affected chunk JSON, recombines chapters, rebuilds alignments, and
optionally rebuilds the EPUB.

The edit/recombine/realign/archive helpers live in ``src/corrections_apply.py``
so the judge-review ``apply`` verb can reuse the exact same pipeline; they are
re-exported here for backward compatibility with existing importers
(``web_ui/app.py``, ``tests/test_apply_corrections.py``).

Usage:
    python scripts/apply_corrections.py projects/fabre2
    python scripts/apply_corrections.py projects/fabre2 --rebuild-epub
    python scripts/apply_corrections.py projects/fabre2 --dry-run
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.corrections_apply import (  # noqa: E402  (re-exported for importers)
    _resolve_correction_span,
    apply_to_chunk,
    archive_applied_records,
    dedupe_corrections,
    group_by_chunk,
    load_corrections,
    realign_chapter,
    rebuild_epub,
    recombine_chapter,
)
from src.utils.file_io import load_chunk, save_chunk  # noqa: E402

__all__ = [
    "_resolve_correction_span",
    "apply_to_chunk",
    "archive_applied_records",
    "dedupe_corrections",
    "group_by_chunk",
    "load_corrections",
    "realign_chapter",
    "rebuild_epub",
    "recombine_chapter",
]


def main():
    parser = argparse.ArgumentParser(description="Apply reader corrections to chunk files")
    parser.add_argument("project_dir", type=Path, help="Path to project directory")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without modifying files")
    parser.add_argument("--rebuild-epub", action="store_true", help="Rebuild EPUB after applying corrections")
    parser.add_argument("--skip-align", action="store_true", help="Skip realignment (faster, but reader will show stale data)")
    parser.add_argument("--source-lang", default="en", help="Source language code (default: en)")
    parser.add_argument("--target-lang", default="es", help="Target language code (default: es)")
    args = parser.parse_args()

    project_dir = args.project_dir.resolve()
    if not project_dir.exists():
        print(f"Error: Project directory not found: {project_dir}")
        sys.exit(1)

    # 1. Load corrections
    raw_corrections = load_corrections(project_dir)
    if not raw_corrections:
        print("No corrections found.")
        return

    corrections = dedupe_corrections(raw_corrections)
    if len(corrections) != len(raw_corrections):
        print(
            f"Loaded {len(raw_corrections)} correction(s) "
            f"({len(raw_corrections) - len(corrections)} duplicate(s) collapsed)"
        )
    else:
        print(f"Loaded {len(corrections)} correction(s)")

    # 2. Group by chunk and apply
    by_chunk = group_by_chunk(corrections)
    affected_chapters = set()
    total_applied = 0

    print(f"\nApplying to {len(by_chunk)} chunk(s):")
    for chunk_id, chunk_corrections in sorted(by_chunk.items()):
        chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
        if not chunk_path.exists():
            print(f"  {chunk_id}: SKIPPED (chunk file not found)")
            continue

        chunk = load_chunk(chunk_path)
        updated_chunk, applied, _ = apply_to_chunk(chunk, chunk_corrections, dry_run=args.dry_run)

        chapter_id = chunk_id.rsplit("_chunk_", 1)[0]
        affected_chapters.add(chapter_id)

        if applied > 0 and not args.dry_run:
            save_chunk(updated_chunk, chunk_path)

        print(f"  {chunk_id}: {applied}/{len(chunk_corrections)} corrections applied")
        total_applied += applied

    if args.dry_run:
        print(f"\nDry run complete. {total_applied} correction(s) would be applied to {len(affected_chapters)} chapter(s).")
        return

    print(f"\nApplied {total_applied} correction(s)")

    # 3. Recombine affected chapters
    print(f"\nRecombining {len(affected_chapters)} chapter(s):")
    for chapter_id in sorted(affected_chapters):
        out_path = recombine_chapter(project_dir, chapter_id)
        word_count = len(out_path.read_text(encoding="utf-8").split())
        print(f"  {chapter_id}: {word_count:,} words")

    # 4. Realign affected chapters
    if not args.skip_align:
        print(f"\nRealigning {len(affected_chapters)} chapter(s):")
        for chapter_id in sorted(affected_chapters):
            t0 = time.time()
            realign_chapter(project_dir, chapter_id, args.source_lang, args.target_lang)
            elapsed = time.time() - t0
            print(f"  {chapter_id}: {elapsed:.1f}s")
    else:
        print("\nSkipping realignment (--skip-align)")

    # 5. Optionally rebuild EPUB
    if args.rebuild_epub:
        print("\nRebuilding EPUB:")
        rebuild_epub(project_dir)

    # 6. Archive applied corrections — write the full pre-dedupe list so
    # corrections_applied.jsonl keeps a complete audit trail of every Save.
    corrections_path = project_dir / "corrections.jsonl"
    archive_path = archive_applied_records(project_dir, raw_corrections)

    if total_applied == len(corrections):
        corrections_path.unlink()
        print(f"\nDone. Corrections archived to {archive_path.name}, corrections.jsonl cleared.")
    else:
        print(f"\nWARNING: {len(corrections) - total_applied} correction(s) failed to apply.")
        print(f"corrections.jsonl NOT deleted. Review and fix failed corrections manually.")


if __name__ == "__main__":
    main()
