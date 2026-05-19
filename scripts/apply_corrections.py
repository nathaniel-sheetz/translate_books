#!/usr/bin/env python3
"""
Apply reader corrections back to chunk files, then recombine and realign
affected chapters.

Reads corrections.jsonl from a project directory, patches the translated_text
in each affected chunk JSON, recombines chapters, rebuilds alignments, and
optionally rebuilds the EPUB.

Usage:
    python scripts/apply_corrections.py projects/fabre2
    python scripts/apply_corrections.py projects/fabre2 --rebuild-epub
    python scripts/apply_corrections.py projects/fabre2 --dry-run
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.combiner import combine_chunks
from src.models import Chunk
from src.utils.file_io import load_chunk, save_chunk


def load_corrections(project_dir: Path) -> list[dict]:
    """Load all corrections from corrections.jsonl."""
    corrections_path = project_dir / "corrections.jsonl"
    if not corrections_path.exists():
        return []

    corrections = []
    for line in corrections_path.read_text(encoding="utf-8").strip().split("\n"):
        if line.strip():
            corrections.append(json.loads(line))
    return corrections


def group_by_chunk(corrections: list[dict]) -> dict[str, list[dict]]:
    """Group corrections by chunk_id."""
    by_chunk = defaultdict(list)
    for c in corrections:
        chunk_id = c.get("chunk_id", "")
        if chunk_id:
            by_chunk[chunk_id].append(c)
    return dict(by_chunk)


def _resolve_correction_span(
    text: str, original: str, hint_start, hint_end,
) -> tuple[int, int] | None:
    """Resolve which span of ``text`` a correction targets.

    Mirrors the three-tier logic in ``web_ui.app.sentence_replace``:

      1. Hint slices exactly back to ``original`` — exact span. This is the
         common path when the alignment row's ``chunk_offset_start`` /
         ``chunk_offset_end`` were stamped on by ``_attach_text_in_chunk``
         and the chunk hasn't been touched since.
      2. Hint is present but doesn't slice cleanly (chunk was mutated after
         the offset was stamped, or the user edited ``original_es`` before
         submitting) — anchored ``find()`` from ``hint_start``.
      3. No usable hint — plain ``find()`` from zero. May hit the wrong
         duplicate when ``original`` has a "twin" earlier in the chunk;
         this matches pre-offset-aware behavior for old queued corrections
         that lack offsets.

    Returns ``(start, end)`` or ``None`` if ``original`` is absent.
    """
    has_hint = (
        isinstance(hint_start, int)
        and not isinstance(hint_start, bool)
        and 0 <= hint_start <= len(text)
    )

    if (
        has_hint
        and isinstance(hint_end, int)
        and not isinstance(hint_end, bool)
        and hint_start < hint_end <= len(text)
        and hint_end - hint_start == len(original)
        and text[hint_start:hint_end] == original
    ):
        return hint_start, hint_end

    if has_hint:
        idx = text.find(original, hint_start)
        if idx != -1:
            return idx, idx + len(original)

    idx = text.find(original)
    if idx == -1:
        return None
    return idx, idx + len(original)


def apply_to_chunk(chunk: Chunk, corrections: list[dict], dry_run: bool = False) -> tuple[Chunk, int]:
    """Apply corrections to a chunk's translated_text.

    Each correction is resolved via :func:`_resolve_correction_span` so that
    corrections with ``chunk_offset_start``/``chunk_offset_end`` hints land on
    the exact span the user edited, not the first textual match. This fixes
    the "twin earlier in body" bug: a body sentence whose text also appears
    inside an ``[IMAGE:...]`` caption (or a quoted version a few sentences up)
    would otherwise corrupt the twin when applied via naive ``str.replace``.

    When a chunk has multiple corrections, offset-bearing ones are applied in
    descending ``chunk_offset_start`` order so earlier corrections do not shift
    later corrections' offsets out from under them. Legacy corrections without
    offsets are applied last, in their original queue order.

    Returns the updated chunk and the number of corrections applied.
    """
    text = chunk.translated_text or ""
    applied = 0

    def _sort_key(indexed):
        i, corr = indexed
        start = corr.get("chunk_offset_start")
        if isinstance(start, int) and not isinstance(start, bool) and start >= 0:
            return (0, -start, i)
        return (1, 0, i)

    ordered = [c for _, c in sorted(enumerate(corrections), key=_sort_key)]

    for corr in ordered:
        original = corr["original_es"]
        corrected = corr["corrected_es"]

        span = _resolve_correction_span(
            text,
            original,
            corr.get("chunk_offset_start"),
            corr.get("chunk_offset_end"),
        )
        if span is None:
            print(f"    WARNING: Could not find original text in chunk {chunk.id}:")
            print(f"      Looking for: {original[:60]}...")
            continue

        start, end = span
        text = text[:start] + corrected + text[end:]
        applied += 1

    if not dry_run and applied > 0:
        chunk_data = chunk.model_dump()
        chunk_data["translated_text"] = text
        chunk = Chunk(**chunk_data)

    return chunk, applied


def recombine_chapter(project_dir: Path, chapter_id: str) -> Path:
    """Recombine a chapter from its chunks and write to chapters/ dir."""
    chunks_dir = project_dir / "chunks"
    chunk_paths = sorted(chunks_dir.glob(f"{chapter_id}_chunk_*.json"))
    chunks = [load_chunk(cp) for cp in chunk_paths]

    combined = combine_chunks(chunks)

    chapters_dir = project_dir / "chapters"
    chapters_dir.mkdir(exist_ok=True)
    out_path = chapters_dir / f"{chapter_id}.txt"
    out_path.write_text(combined, encoding="utf-8")

    return out_path


def realign_chapter(project_dir: Path, chapter_id: str, source_lang: str = "en", target_lang: str = "es"):
    """Realign a single chapter."""
    from src.sentence_aligner import align_chapter_chunks

    chunks_dir = project_dir / "chunks"
    chunk_paths = sorted(chunks_dir.glob(f"{chapter_id}_chunk_*.json"))
    align_dir = project_dir / "alignments"
    align_dir.mkdir(exist_ok=True)

    align_chapter_chunks(
        chunk_paths=[str(p) for p in chunk_paths],
        project_id=project_dir.name,
        chapter_id=chapter_id,
        source_lang=source_lang,
        target_lang=target_lang,
        output_path=str(align_dir / f"{chapter_id}.json"),
    )


def rebuild_epub(project_dir: Path):
    """Rebuild the EPUB from chapters/ directory."""
    from src.epub_builder import build_epub

    # Try to load project config for metadata
    config_path = project_dir / f"project.{project_dir.name}.json"
    if not config_path.exists():
        config_path = project_dir / "project.json"

    title = project_dir.name
    author = "Unknown"
    language = "es"

    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        title = config.get("project_name", title)
        author = config.get("author", author)
        language = config.get("target_lang_code", language)

    epub_path = build_epub(
        project_path=project_dir,
        title=title,
        author=author,
        language=language,
    )
    print(f"  EPUB rebuilt: {epub_path}")


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
    corrections = load_corrections(project_dir)
    if not corrections:
        print("No corrections found.")
        return

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
        updated_chunk, applied = apply_to_chunk(chunk, chunk_corrections, dry_run=args.dry_run)

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

    # 6. Archive applied corrections
    corrections_path = project_dir / "corrections.jsonl"
    archive_path = project_dir / "corrections_applied.jsonl"
    with open(archive_path, "a", encoding="utf-8") as f:
        for corr in corrections:
            corr["applied_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            f.write(json.dumps(corr, ensure_ascii=False) + "\n")

    if total_applied == len(corrections):
        corrections_path.unlink()
        print(f"\nDone. Corrections archived to {archive_path.name}, corrections.jsonl cleared.")
    else:
        print(f"\nWARNING: {len(corrections) - total_applied} correction(s) failed to apply.")
        print(f"corrections.jsonl NOT deleted. Review and fix failed corrections manually.")


if __name__ == "__main__":
    main()
