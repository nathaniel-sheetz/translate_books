"""When a book review is due, for the translate-harness router.

A review covers the translations that existed when it ran. ``state.json`` under
``.harness/book_review/`` records the newest ``translated_at`` it saw; any chunk
translated after that makes the next review due again.

Two harness config keys, both ``on`` unless set ``off`` with ``config-set``:

- ``book_review_after_translate`` -- route a fully translated book to the review
  before footnotes and EPUB whenever new translations have landed since the
  last one.
- ``book_review_canary`` -- route a partially translated book to the review once,
  right after its first translate wave, so systemic problems (a dead glossary
  key, a style guide that contradicts the address map) are fixed before the
  rest of the book inherits them.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.book_review import review_dir

AFTER_TRANSLATE_KEY = "book_review_after_translate"
CANARY_KEY = "book_review_canary"


def _state_path(project_dir: Path) -> Path:
    return review_dir(project_dir) / "state.json"


def load_state(project_dir: Path) -> dict[str, Any]:
    try:
        doc = json.loads(_state_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def newest_translation(project_dir: Path) -> Optional[str]:
    """The latest ``translated_at`` across the book's chunks, or ``None``."""
    newest: Optional[str] = None
    for path in (Path(project_dir) / "chunks").glob("*.json"):
        try:
            stamp = json.loads(path.read_text(encoding="utf-8")).get("translated_at")
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(stamp, str) and (newest is None or stamp > newest):
            newest = stamp
    return newest


def record_review(project_dir: Path, *, kind: str, report_path: Optional[str] = None) -> dict[str, Any]:
    """Mark the current translations as reviewed (``kind``: ``signals`` | ``synthesis`` | ``skipped``)."""
    doc = {
        "kind": kind,
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "reviewed_through": newest_translation(project_dir),
        "report_path": report_path,
    }
    path = _state_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc


def due(project_dir: Path, cfg: dict, stage: str) -> Optional[str]:
    """Why the router should send this book to the review now, or ``None``.

    ``stage`` is :func:`src.harness.flow.status`'s: only ``partial`` (the canary)
    and ``fully-translated`` can be due.
    """
    project_dir = Path(project_dir)
    previous = load_state(project_dir)
    newest = newest_translation(project_dir)
    if newest is None:
        return None  # nothing translated on disk, so nothing to review
    if stage == "partial":
        if str(cfg.get(CANARY_KEY) or "on") == "off" or previous:
            return None
        return "canary: the first translate wave has landed and nothing has reviewed it"
    if stage != "fully-translated" or str(cfg.get(AFTER_TRANSLATE_KEY) or "on") == "off":
        return None
    if not previous:
        return "the book is translated and has never been reviewed"
    through = previous.get("reviewed_through")
    if newest and (not through or newest > through):
        return "chunks were translated after the last review"
    return None


def summary(project_dir: Path, cfg: dict, stage: str) -> dict[str, Any]:
    """The ``book_review`` block of ``harness.py status``."""
    previous = load_state(project_dir)
    return {
        "due": due(project_dir, cfg, stage),
        "after_translate": str(cfg.get(AFTER_TRANSLATE_KEY) or "on"),
        "canary": str(cfg.get(CANARY_KEY) or "on"),
        "last_review": previous or None,
    }
