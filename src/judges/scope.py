"""
Scope / addressing layer for judges.

Turns a ``--scope`` string into a list of :class:`JudgeTarget`. v1 implements
the three scopes the dialogue judge needs:

- ``chunk:<chunk_id>``      — a single chunk.
- ``chapter:<chapter_id>``  — every translated chunk in a chapter, one target
                              each (keeps results keyed per chunk so the
                              existing ``evaluations/<chunk>.json`` persistence
                              and dashboard badges work).
- ``book`` / ``book:``      — every translated chunk in the project, in reading
                              order. Added so a whole-book ``run_judges.py
                              apply`` is one invocation instead of one
                              ``--scope chapter:`` flag per chapter built by a
                              shell loop (a missing chapter there silently drops
                              findings out of scope).

Designed-for, not yet implemented (clear ``NotImplementedError`` stubs):
``sentences:<chapter>:<es_idx,...>`` (alignments), ``flags:<chapter>``
(annotations.jsonl), ``findings:<chapter>:<evaluator>`` (prior eval issues).
These build on ``alignments/chapter_XX.json`` which maps es_idx <-> en/es <->
chunk_id.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from src.judges.base import JudgeTarget
from src.utils.file_io import load_chunk

logger = logging.getLogger(__name__)


class ScopeError(ValueError):
    """Raised when a scope string is malformed or resolves to nothing."""


def _chunks_dir(project_dir: Path) -> Path:
    return Path(project_dir) / "chunks"


def _target_from_chunk_path(path: Path) -> JudgeTarget | None:
    """Load a chunk file into a JudgeTarget, or None if it has no translation."""
    chunk = load_chunk(path)
    if not chunk.has_translation:
        logger.warning("Skipping chunk %s: no translation yet", chunk.id)
        return None
    return JudgeTarget(
        id=chunk.id,
        target_type="chunk",
        source_text=chunk.source_text,
        translated_text=chunk.translated_text or "",
        context={"chapter_id": chunk.chapter_id, "position": chunk.position},
    )


_ID_RE = re.compile(r'^[A-Za-z0-9_\-]+$')


def _build_chunk_targets(project_dir: Path, chunk_id: str) -> list[JudgeTarget]:
    if not _ID_RE.match(chunk_id):
        raise ScopeError(
            f"Invalid chunk_id {chunk_id!r}: only letters, digits, underscores, and hyphens allowed."
        )
    path = _chunks_dir(project_dir) / f"{chunk_id}.json"
    if not path.exists():
        raise ScopeError(f"Chunk file not found: {path}")
    target = _target_from_chunk_path(path)
    if target is None:
        raise ScopeError(f"Chunk {chunk_id} has no translation to judge.")
    return [target]


def _build_chapter_targets(project_dir: Path, chapter_id: str) -> list[JudgeTarget]:
    if not _ID_RE.match(chapter_id):
        raise ScopeError(
            f"Invalid chapter_id {chapter_id!r}: only letters, digits, underscores, and hyphens allowed."
        )
    chunks_dir = _chunks_dir(project_dir)
    if not chunks_dir.exists():
        raise ScopeError(f"No chunks/ directory in {project_dir}")

    paths = sorted(chunks_dir.glob(f"{chapter_id}_chunk_*.json"))
    if not paths:
        raise ScopeError(
            f"No chunks found for chapter {chapter_id!r} in {chunks_dir}"
        )

    targets: list[JudgeTarget] = []
    for path in paths:
        target = _target_from_chunk_path(path)
        if target is not None:
            targets.append(target)

    if not targets:
        raise ScopeError(
            f"Chapter {chapter_id} has chunks but none are translated yet."
        )

    targets.sort(key=lambda t: t.context.get("position", 0))
    return targets


def _build_book_targets(project_dir: Path) -> list[JudgeTarget]:
    """Every translated chunk in the project, in reading order."""
    chunks_dir = _chunks_dir(project_dir)
    if not chunks_dir.exists():
        raise ScopeError(f"No chunks/ directory in {project_dir}")

    paths = sorted(chunks_dir.glob("*_chunk_*.json"))
    if not paths:
        raise ScopeError(f"No chunks found in {chunks_dir}")

    targets: list[JudgeTarget] = []
    for path in paths:
        target = _target_from_chunk_path(path)
        if target is not None:
            targets.append(target)

    if not targets:
        raise ScopeError(f"{project_dir} has chunks but none are translated yet.")

    targets.sort(
        key=lambda t: (t.context.get("chapter_id") or "", t.context.get("position", 0))
    )
    return targets


def build_targets(project_dir: Path, scope: str) -> list[JudgeTarget]:
    """Resolve a ``--scope`` string into JudgeTargets.

    Args:
        project_dir: ``projects/<id>/`` directory.
        scope: e.g. ``"chunk:chapter_01_chunk_000"``, ``"chapter:chapter_03"``,
            or ``"book"`` / ``"book:"`` for the whole project.

    Raises:
        ScopeError: For unknown/malformed scopes or empty resolutions.
    """
    # ``book`` is the one scope with no id after it, so it is matched before the
    # '<kind>:<id>' split — both bare and trailing-colon forms are accepted.
    if scope.strip().lower().rstrip(":") == "book":
        return _build_book_targets(Path(project_dir))

    kind, sep, rest = scope.partition(":")
    if not sep:
        raise ScopeError(
            f"Malformed scope {scope!r}; expected '<kind>:<id>' "
            "(e.g. 'chapter:chapter_03') or 'book'."
        )
    kind = kind.strip().lower()
    rest = rest.strip()

    if kind == "chunk":
        return _build_chunk_targets(Path(project_dir), rest)
    if kind == "chapter":
        return _build_chapter_targets(Path(project_dir), rest)
    if kind == "book":
        raise ScopeError(
            f"Scope 'book' takes no id (got {rest!r}); the project is already "
            "named by --project. Use 'book' or 'book:'."
        )
    if kind in {"sentences", "flags", "findings"}:
        raise NotImplementedError(
            f"Scope {kind!r} is designed for but not implemented in v1. "
            "Use 'chunk:<id>', 'chapter:<id>' or 'book'."
        )
    raise ScopeError(
        f"Unknown scope kind {kind!r}; expected one of: chunk, chapter, book."
    )
