"""
Scope / addressing layer for judges.

Turns a ``--scope`` string into a list of :class:`JudgeTarget`. v1 implements
the three scopes the dialogue judge needs:

- ``chunk:<chunk_id>``      — a single chunk.
- ``chapter:<chapter_id>``  — every translated chunk in a chapter, one target
                              each (keeps results keyed per chunk so the
                              existing ``evaluations/<chunk>.json`` persistence
                              and dashboard badges work).
- ``chapter:<a>..<b>``      — the inclusive span from chapter ``a`` to chapter
                              ``b``, resolved by position in the enumerated
                              chapter list. The same form ``status`` accepts, so
                              the command an operator reads off a status report
                              also runs on ``prepare`` / ``run`` / ``apply``.
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


def _known_chapters(chunks_dir: Path) -> list[str]:
    """Every chapter that has at least one chunk file, in reading order.

    The ordered universe an inclusive range resolves against. ``status.py`` builds
    the same list from ``iter_chapter_chunks`` unioned with ``chapters/*.txt``,
    because a status report must describe a chapter with nothing translated in it.
    Here the chunk glob is the whole answer — ``build_targets`` only ever judges
    chunks that exist — and it keeps ``src/judges`` from importing ``web_ui``.
    """
    return sorted(
        {path.name.rsplit("_chunk_", 1)[0] for path in chunks_dir.glob("*_chunk_*.json")}
    )


def _build_chapter_range_targets(
    project_dir: Path, first: str, last: str
) -> list[JudgeTarget]:
    """Resolve ``chapter:<first>..<last>`` (inclusive) into JudgeTargets.

    Same semantics as ``status.py::_filter_chapters``, deliberately: the range
    resolves against the *enumerated* chapter list by position rather than by
    comparing ids, so it works on a book whose chapters are not zero-padded, and
    a reversed range (``chapter_09..chapter_03``) is the same span. Keeping the
    two implementations identical is the point — a form that ``status`` accepts
    and ``prepare`` rejects is what cost a turn on 2026-08-27.

    One case ``status`` does not face: a chapter inside the span with chunks but
    no translation is *skipped*, not fatal. A range names a span, not a list the
    caller vouched for, and refusing the other six chapters over one untranslated
    interior chapter is the behaviour ``collect_pending`` already rejected for the
    multi-chapter GUI selection. An empty span is still a ``ScopeError``.
    """
    for endpoint in (first, last):
        if not _ID_RE.match(endpoint):
            raise ScopeError(
                f"Invalid chapter_id {endpoint!r}: only letters, digits, underscores, "
                "and hyphens allowed."
            )

    chunks_dir = _chunks_dir(project_dir)
    if not chunks_dir.exists():
        raise ScopeError(f"No chunks/ directory in {project_dir}")

    known = _known_chapters(chunks_dir)
    absent = [c for c in (first, last) if c not in known]
    if absent:
        span = f" Known chapters run {known[0]}..{known[-1]}." if known else ""
        raise ScopeError(
            f"Range endpoint(s) not in this project: {', '.join(absent)}.{span}"
        )

    lo, hi = sorted((known.index(first), known.index(last)))
    targets: list[JudgeTarget] = []
    for chapter_id in known[lo : hi + 1]:
        for path in sorted(chunks_dir.glob(f"{chapter_id}_chunk_*.json")):
            target = _target_from_chunk_path(path)
            if target is not None:
                targets.append(target)

    if not targets:
        raise ScopeError(f"No translated chunks in chapters {first}..{last}.")

    targets.sort(
        key=lambda t: (t.context.get("chapter_id") or "", t.context.get("position", 0))
    )
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
            the inclusive range ``"chapter:chapter_03..chapter_09"``, or
            ``"book"`` / ``"book:"`` for the whole project.

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
        if ".." in rest:
            first, _, last = rest.partition("..")
            return _build_chapter_range_targets(
                Path(project_dir), first.strip(), last.strip()
            )
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
