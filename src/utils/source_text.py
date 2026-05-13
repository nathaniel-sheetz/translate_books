"""
Source text loader for project-aware tools.

Style-guide, glossary, and feature-detection paths all need to feed an LLM
(or a heuristic detector) with sample source-language text. The raw
``source.txt`` typically contains TOC, copyright, publisher info, and other
front matter that is irrelevant (and noisy).

The chapter-splitting step already strips most of that and writes clean
per-chapter files to ``chapters/``. The chunking step then re-writes the
same content into ``chunks/*.json`` with both a ``source_text`` field
(immutable, source-language) and a ``translated_text`` field (filled in
later by the translator).

Priority order:
    1. ``chunks/*_chunk_*.json`` ``source_text``  — guaranteed source-language,
       front matter already excluded, immutable even after translation. Used
       first because ``chapters/`` may be overwritten with translated text
       on partially-translated projects.
    2. ``chapters/chapter_*.txt``  — clean plain text right after splitting,
       but a downstream workflow can replace these with translations.
    3. ``source.txt``              — raw, may include TOC / publisher / copyright.

Returns the source kind so callers can warn when they fall back to raw
``source.txt``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SourceKind = str  # one of: "chapters", "chunks", "source", ""


def load_clean_source_text(
    project_dir: Path,
) -> tuple[str, Optional[float], SourceKind]:
    """Load the cleanest available source text for a project.

    Returns ``(text, mtime, source_kind)``.

    ``source_kind`` is ``"chapters"``, ``"chunks"``, ``"source"``, or ``""``
    if no source could be found. ``mtime`` is the newest file mtime among
    the files contributing to ``text`` (useful for cache invalidation), or
    ``None`` if no source exists.
    """
    project_dir = Path(project_dir)

    # 1. Prefer chunks/ — the ``source_text`` field is guaranteed to be the
    #    original source language, even on projects whose ``chapters/``
    #    files have been replaced with translated text.
    chunks_dir = project_dir / "chunks"
    if chunks_dir.exists():
        chunk_files = sorted(chunks_dir.glob("*_chunk_*.json"))
        if chunk_files:
            mtime = max(f.stat().st_mtime for f in chunk_files)
            texts: list[str] = []
            for cf in chunk_files:
                try:
                    with open(cf, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    texts.append(data.get("source_text", ""))
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("Failed to read chunk %s: %s", cf, exc)
            return "\n\n".join(texts), mtime, "chunks"

    # 2. Fall back to chapters/ — clean plain text right after splitting,
    #    but may have been overwritten with translated text downstream.
    chapters_dir = project_dir / "chapters"
    if chapters_dir.exists():
        chapter_files = sorted(chapters_dir.glob("chapter_*.txt"))
        if chapter_files:
            mtime = max(f.stat().st_mtime for f in chapter_files)
            texts = []
            for cf in chapter_files:
                try:
                    texts.append(cf.read_text(encoding="utf-8"))
                except OSError as exc:
                    logger.warning("Failed to read chapter %s: %s", cf, exc)
            return "\n\n".join(texts), mtime, "chapters"

    # 3. Last resort: raw source.txt — likely contains front matter.
    source_path = project_dir / "source.txt"
    if source_path.exists():
        logger.warning(
            "Falling back to raw source.txt for %s — chapter splitting has not "
            "run yet, so style-guide / glossary input may include TOC, "
            "copyright, and other front matter.",
            project_dir,
        )
        return source_path.read_text(encoding="utf-8"), source_path.stat().st_mtime, "source"

    return "", None, ""
