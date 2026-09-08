"""
Per-item "come back to this" marks for the recommendations screen.

Every other mark this app stores on a recommendation is a *verdict* — ``fixed``,
``not_a_problem``, ``bad_message`` — and all of them mean you are done with the
item. A favorite is the opposite: it means you are not done, and want the item
again later. That is why it lives beside the verdicts rather than among them.

Records are appended to ``projects/<project_id>/favorites.jsonl`` — the project
root, alongside ``annotations.jsonl`` and ``corrections.jsonl``, not the
``evaluations/`` directory that holds ``_feedback.jsonl``. A favorite can be on
a reader *annotation*, whose data lives at the root; filing those under
``evaluations/`` would put annotation state in the evaluator's drawer. Being in
the reader-sidecar family is also what the reader needs to write one itself.

The file is append-only and the last record for an id wins, exactly as
``_feedback.jsonl`` works: unfavoriting appends ``favorite: false`` rather than
rewriting the file, which is the same convention ``annotations.jsonl`` uses for
a deletion. Nothing here locks, because nothing here can conflict — the last
write is the answer by definition.

The module intentionally has no Flask or request-global dependencies so it can
be unit-tested against a bare ``tmp_path`` directory.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from web_ui.evaluations import chapter_id_from_chunk_id

logger = logging.getLogger(__name__)

_FAVORITES_FILENAME = "favorites.jsonl"

# An id is composed here and echoed back by the browser, so it is validated on
# the way in rather than trusted. Both patterns are deliberately narrower than
# "any string" - the id is written to a file we later parse - and they admit the
# same alphabet as `app._safe_id`, periods included: a chapter or project whose
# id carries one is legal everywhere else in this app, and rejecting it here
# would 400 the heart with nothing the reader could do about it. No id is ever
# joined to a path, so the dot buys the caller no reach; `:` stays out because
# it is this scheme's own separator.
_FINDING_ID_RE = re.compile(r"^finding:[A-Za-z0-9_.\-]{1,120}:[A-Za-z0-9_.\-]{1,64}$")
_ANNOTATION_ID_RE = re.compile(r"^annotation:[A-Za-z0-9_.\-]{1,120}$")


def _favorites_file(project_dir: Path) -> Path:
    return project_dir / _FAVORITES_FILENAME


def finding_id(chunk_id: Optional[str], key: Optional[str]) -> Optional[str]:
    """Stable id for one evaluator or judge finding, or ``None``.

    ``key`` is :func:`web_ui.evaluations.issue_key`'s content hash, not the
    finding's ``issue_index``. The index is a *position* in the evaluator's
    issue list, recomputed on every run, so a favorite keyed on it would
    silently re-point at whatever finding later occupied the slot — the bug
    documented at ``evaluations.py:58-65``, which had already mis-aimed 87
    feedback marks in the local corpus before it was found.

    ``chunk_id`` is part of the id because ``issue_key`` hashes only the
    finding's own content: two chunks can hold the same defect with the same
    message and the same quoted text, and those are two findings, not one.

    ``None`` when either half is missing, which is how an item that cannot be
    addressed reaches the browser without a heart on it.
    """
    if not chunk_id or not key:
        return None
    return f"finding:{chunk_id}:{key}"


def annotation_id(key: Optional[str]) -> Optional[str]:
    """Stable id for one reviewed reader annotation, or ``None``.

    ``key`` is :func:`src.annotations.store.target_key`'s
    ``<chapter_id>__<es_idx>__<sub_id>``, which is already the identity the
    inbox and the review sidecar key on.
    """
    if not key:
        return None
    return f"annotation:{key}"


def is_valid_id(fav_id: object) -> bool:
    """True if ``fav_id`` is one this module composed and can parse back."""
    if not isinstance(fav_id, str):
        return False
    return bool(_FINDING_ID_RE.match(fav_id) or _ANNOTATION_ID_RE.match(fav_id))


def chapter_of(fav_id: str) -> Optional[str]:
    """Which chapter an id belongs to, without opening any other file.

    Both id shapes already carry their chapter: a chunk id is
    ``<chapter>_chunk_<n>`` (split by :func:`evaluations.chapter_id_from_chunk_id`,
    the same one the chapter counts use) and an annotation key is
    ``<chapter>__<es_idx>__<sub_id>``. Deriving it beats storing it, because a
    stored chapter would go stale on a re-split while the id itself would not.

    This is only ever used for counting, never for matching: an item's heart is
    decided by the id, so a chapter this guesses wrong costs a miscount and not
    a wrong answer.
    """
    if not is_valid_id(fav_id):
        return None
    kind, _, rest = fav_id.partition(":")
    if kind == "finding":
        chunk_id = rest.rsplit(":", 1)[0]
        return chapter_id_from_chunk_id(chunk_id) or None
    return rest.split("__", 1)[0] or None


def append_favorite(project_dir: Path, fav_id: str, favorite: bool) -> Path:
    """Append one favorite/unfavorite record.

    Raises:
        ValueError: If ``fav_id`` is not a well-formed id.
    """
    if not is_valid_id(fav_id):
        raise ValueError(f"Malformed favorite id: {fav_id!r}")

    record = {
        "ts": datetime.now().isoformat(),
        "id": fav_id,
        "favorite": bool(favorite),
    }
    path = _favorites_file(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def load_favorites(project_dir: Path) -> set[str]:
    """The ids favorited right now.

    Folds the append-only file last-record-wins, then keeps only the ids whose
    standing record says ``favorite``. An id favorited, unfavorited and
    favorited again is in the set once; one unfavorited last is absent.

    Malformed lines and I/O errors are swallowed with a log, the way
    :func:`evaluations.load_feedback_for_chunk` treats its file: this is UI
    state, and a page that renders without hearts beats a page that 500s.
    """
    path = _favorites_file(project_dir)
    if not path.exists():
        return set()

    standing: dict[str, bool] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.debug("Skipping malformed favorite line in %s: %s", path, e)
                    continue
                fav_id = record.get("id")
                if isinstance(fav_id, str) and fav_id:
                    standing[fav_id] = bool(record.get("favorite"))
    except OSError as e:
        logger.warning("Failed to read favorites file %s: %s", path, e)
        return set()

    return {fav_id for fav_id, on in standing.items() if on}


def favorites_by_chapter(project_dir: Path) -> dict[str, int]:
    """``{chapter_id: how many favorites it holds}``.

    The recommendations page fills chapters lazily, so "favorites only" would
    otherwise show a run of empty headings for chapters you never scrolled to.
    These counts tell the page which chapters are worth fetching eagerly and
    which to hide outright.
    """
    counts: dict[str, int] = {}
    for fav_id in load_favorites(project_dir):
        chapter = chapter_of(fav_id)
        if chapter:
            counts[chapter] = counts.get(chapter, 0) + 1
    return counts
