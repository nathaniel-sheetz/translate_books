"""
Read/write access to ``projects/<slug>/annotations.jsonl``.

The reader writes annotations append-only: every create, edit and delete appends
a new record. The active set is derived, not stored — keyed by
``(es_idx, sub_id)``, latest record wins, and ``{"removed": true}`` is a
tombstone. Three copies of that rule existed (``web_ui/app.py:_load_annotations``,
``web_ui/app.py:_load_annotation_counts``, ``src/endnotes.py``); this module is
the one implementation the non-web callers share.

Deliberately free of any ``web_ui`` import so ``src/endnotes.py`` (which
``epub_builder`` calls) and the annotation-review CLI can both use it without a
circular dependency.

A hand-authored annotation has no ``sub_id`` (``None``), giving one note per
sentence exactly as before the multi-annotation change; imported footnotes carry
a stable ``sub_id`` (``gb<n>``) and reader-created ones a minted ``u<hex>`` so
several can coexist on one sentence.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

ANNOTATIONS_FILENAME = "annotations.jsonl"

# The four types the reader UI writes (web_ui/app.py:save_annotation). "flag" is
# labelled "Other" in the UI.
ANNOTATION_TYPES = ("word_choice", "inconsistency", "footnote", "flag")

# Sidecar key stamped on records written by annotation-review. It is not part of
# the reader's wire shape, so a later edit through POST /api/annotation rebuilds
# the record without it — which correctly re-opens the annotation for review.
AI_REVIEW_KEY = "ai_review"


def annotations_path(project_dir: Path) -> Path:
    return Path(project_dir) / ANNOTATIONS_FILENAME


def storage_sub_id(sub_id: Any) -> Optional[str]:
    """Map a wire/API sub_id to the storage key (``None`` = legacy single slot).

    Mirrors ``web_ui/app.py:_ann_storage_sub_id`` — the ``"legacy"`` sentinel the
    API emits addresses the same ``(es_idx, None)`` slot as an absent sub_id.
    """
    if sub_id is None or sub_id == "" or sub_id == "legacy":
        return None
    return str(sub_id)


def iter_records(project_dir: Path) -> Iterable[dict]:
    """Yield every parseable record in the file, oldest first.

    Unparseable lines are skipped (matching the reader), because a partially
    written line must never take down the whole book's annotations.
    """
    path = annotations_path(project_dir)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("skipping unparseable annotation line in %s", path)
            continue
        if isinstance(record, dict):
            yield record


def load_active(
    project_dir: Path,
    *,
    chapter_id: Optional[str] = None,
    types: Optional[Iterable[str]] = None,
) -> list[dict]:
    """Return the live annotations, oldest→newest by ``(timestamp, sub_id)``.

    Applies the append-only / tombstone / latest-wins rule keyed by
    ``(chapter_id, es_idx, sub_id)``. The *final* record at a key decides the
    type, so a later non-footnote edit supersedes an earlier footnote.

    Args:
        project_dir: ``projects/<slug>/``.
        chapter_id: Restrict to one chapter (``None`` = whole book).
        types: Restrict to these annotation types (``None`` = all).

    Returns:
        The stored records, unmodified. Each carries ``chapter_id``, ``es_idx``,
        ``type``, ``content``, ``timestamp`` and optionally ``sub_id``,
        ``origin`` and ``ai_review``.
    """
    by_key: dict[tuple, dict] = {}
    for record in iter_records(project_dir):
        ch = record.get("chapter_id")
        if chapter_id is not None and ch != chapter_id:
            continue
        key = (ch, record.get("es_idx"), storage_sub_id(record.get("sub_id")))
        if record.get("removed"):
            by_key.pop(key, None)
        elif record.get("es_idx") is not None:
            by_key[key] = record

    wanted = set(types) if types is not None else None
    records = [
        rec
        for rec in by_key.values()
        if wanted is None or rec.get("type") in wanted
    ]
    # Stable, deterministic order. str() keeps None (legacy rows) comparable.
    records.sort(
        key=lambda r: (
            str(r.get("chapter_id") or ""),
            r.get("es_idx") or 0,
            str(r.get("timestamp") or ""),
            str(storage_sub_id(r.get("sub_id"))),
        )
    )
    return records


def append_record(project_dir: Path, record: dict) -> Path:
    """Append one record to ``annotations.jsonl`` and return the file path.

    The file is append-only by contract — never rewrite it. Callers that "edit"
    an annotation append a full replacement record at the same
    ``(es_idx, sub_id)`` key; the previous one stays on disk as history.
    """
    path = annotations_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def record_key(record: dict) -> tuple:
    """The identity of an annotation: ``(chapter_id, es_idx, storage sub_id)``."""
    return (
        record.get("chapter_id"),
        record.get("es_idx"),
        storage_sub_id(record.get("sub_id")),
    )


def target_key(record: dict) -> str:
    """A filename- and id-safe key for one annotation.

    ``<chapter_id>__<es_idx>__<sub_id>``, e.g. ``chapter_04__37__u72399176``;
    legacy rows (no sub_id) use ``legacy``. Uses ``__`` rather than ``#`` so the
    key satisfies ``^[A-Za-z0-9_\\-]+$`` and can be both a manifest id and part
    of a prompt/draft filename.
    """
    chapter_id, es_idx, sub = record_key(record)
    return f"{chapter_id}__{es_idx}__{sub or 'legacy'}"
