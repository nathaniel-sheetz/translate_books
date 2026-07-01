"""Shared helpers for applying corrections back to chunk files.

Extracted from ``scripts/apply_corrections.py`` so more than one caller can
reuse the *exact* same edit → recombine → realign → archive pipeline without
duplicating the delicate span-resolution logic:

- ``scripts/apply_corrections.py`` — the reader-corrections CLI (drains
  ``corrections.jsonl``).
- ``scripts/run_judges.py apply`` — the judge-review "apply fixes" verb, which
  turns approved judge findings into the same correction records.

A *correction* is a dict with at least ``chunk_id``, ``original_es`` and
``corrected_es``; optional ``chunk_offset_start``/``chunk_offset_end`` hints let
:func:`apply_to_chunk` land on the exact span the user edited rather than the
first textual match.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

from src.combiner import combine_chunks
from src.models import Chunk
from src.utils.file_io import load_chunk, save_chunk

CORRECTIONS_FILENAME = "corrections.jsonl"
CORRECTIONS_APPLIED_FILENAME = "corrections_applied.jsonl"


def load_corrections(project_dir: Path) -> list[dict]:
    """Load all corrections from corrections.jsonl."""
    corrections_path = project_dir / CORRECTIONS_FILENAME
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


def dedupe_corrections(corrections: list[dict]) -> list[dict]:
    """Collapse corrections that target the same edit, keeping the newest.

    Two corrections collide when they share ``(chunk_id, es_idx, corrected_es)``.
    Without this, a Save click that landed in ``corrections.jsonl`` twice (UI
    glitch, double-click, retry) would leave one copy unable to find its
    ``original_es`` after the first pass mutates the chunk — the file would
    never satisfy ``total_applied == len(corrections)`` and the reader's
    pending-corrections banner would stick forever.

    Stable on order for ties: latest by ``timestamp`` wins; on tie or missing
    timestamps, the last occurrence wins (newest queued entry).
    """
    by_key: dict[tuple, dict] = {}
    for corr in corrections:
        key = (corr.get("chunk_id", ""), corr.get("es_idx"), corr.get("corrected_es"))
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = corr
            continue
        if corr.get("timestamp", "") >= existing.get("timestamp", ""):
            by_key[key] = corr
    return list(by_key.values())


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
        hint_start = corr.get("chunk_offset_start")
        hint_end = corr.get("chunk_offset_end")

        span = _resolve_correction_span(text, original, hint_start, hint_end)
        if span is None:
            # Idempotent path: if corrected_es is already in the chunk, the
            # user's intent is satisfied. Count as applied so a duplicate
            # correction (or a re-click after Realign already consumed the
            # row) doesn't keep corrections.jsonl pinned forever.
            if corrected and _resolve_correction_span(
                text, corrected, hint_start, hint_end,
            ) is not None:
                applied += 1
                continue
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
    return epub_path


def archive_applied_records(
    project_dir: Path, records: list[dict], applied_at: str | None = None,
) -> Path:
    """Append applied corrections to ``corrections_applied.jsonl`` (audit log).

    Each record is written with an ``applied_at`` timestamp added, matching the
    reader-corrections archive format so judge-applied fixes share one audit
    trail with reader corrections. Returns the archive path.
    """
    if applied_at is None:
        applied_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    archive_path = project_dir / CORRECTIONS_APPLIED_FILENAME
    with open(archive_path, "a", encoding="utf-8") as f:
        for corr in records:
            f.write(json.dumps({**corr, "applied_at": applied_at}, ensure_ascii=False) + "\n")
    return archive_path
