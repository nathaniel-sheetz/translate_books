"""
Which part of the book a rule covers.

A book rule or an address rule may carry ``since`` / ``until``: chapter ids
(``chapter_13``), read as "from" and "through", both inclusive. The translator
is never told which chapter it is in. Instead a windowed rule reaches only the
chunks of the chapters it covers, in the per-chunk part of the prompt, so the
cached prefix stays byte-identical across the book.

Chapter ids are the split's file ids, not the book's own numbering (bambi's
``chapter_01`` is the FOREWORD), so anything shown to a person also carries the
book's label (:func:`chapter_labels`).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TRAILING_NUMBER_RE = re.compile(r"^(.*?)(\d+)$")


def chapter_key(chapter_id: str) -> tuple[str, int]:
    """Sort key for a chapter id: ``chapter_9`` before ``chapter_10``."""
    match = _TRAILING_NUMBER_RE.match(str(chapter_id or "").strip())
    if not match:
        return (str(chapter_id or ""), -1)
    return (match.group(1), int(match.group(2)))


def rule_window(rule: Any) -> tuple[Optional[str], Optional[str]]:
    """``(since, until)`` of a rule dict or an ``AddressRule``; ``None`` for an open end."""
    if isinstance(rule, dict):
        since, until = rule.get("since"), rule.get("until")
    else:
        since, until = getattr(rule, "since", None), getattr(rule, "until", None)
    return (_end(since), _end(until))


def _end(value: Any) -> Optional[str]:
    text = str(value).strip() if value else ""
    return text or None


def has_window(rule: Any) -> bool:
    since, until = rule_window(rule)
    return bool(since or until)


def in_window(since: Optional[str], until: Optional[str], chapter_id: Optional[str]) -> bool:
    """Whether ``chapter_id`` falls in ``since``..``until`` (both inclusive).

    No window covers every chapter. A window with no chapter to test (a
    sentence retranslation, which knows no chapter) covers nothing: a rule
    for chapters 13 on must not reach a passage that may be from chapter 2.
    """
    if not since and not until:
        return True
    if not chapter_id:
        return False
    key = chapter_key(chapter_id)
    if since and key < chapter_key(since):
        return False
    if until and key > chapter_key(until):
        return False
    return True


def rule_in_scope(rule: Any, chapter_id: Optional[str]) -> bool:
    return in_window(*rule_window(rule), chapter_id)


def chapter_labels(project_dir: Optional[Path]) -> dict[str, str]:
    """``{chapter_id: label}`` from project.json's ``chapter_manifest``.

    "Chapter 12" for a numbered chapter, the manifest's own label ("FOREWORD")
    for front and back matter. Empty when there is no manifest.
    """
    if project_dir is None:
        return {}
    path = Path(project_dir) / "project.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8")).get("chapter_manifest") or []
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}
    labels: dict[str, str] = {}
    for entry in manifest:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        if entry.get("kind", "chapter") == "chapter" and entry.get("number") is not None:
            labels[str(entry["id"])] = f"Chapter {entry['number']}"
        elif entry.get("label"):
            labels[str(entry["id"])] = str(entry["label"])
    return labels


def window_text(since: Optional[str], until: Optional[str], labels: dict[str, str]) -> str:
    """"chapter_13 (Chapter 12) onward", "through chapter_03", "chapter_05 to chapter_09"."""

    def name(chapter_id: str) -> str:
        label = labels.get(chapter_id)
        return f"{chapter_id} ({label})" if label else chapter_id

    if since and until:
        return f"{name(since)} through {name(until)}"
    if since:
        return f"{name(since)} onward"
    if until:
        return f"up to and including {name(until)}"
    return ""


def window_problems(
    rules: list[dict[str, Any]], chapter_ids: list[str]
) -> list[str]:
    """What is wrong with the windows on ``rules``, given the book's chapters.

    An unknown chapter id (a typo, or the book's numbering where the file id was
    meant), and a window that ends before it starts. Both mean the rule would
    reach the wrong chunks, or none.
    """
    known = set(chapter_ids)
    problems: list[str] = []
    for rule in rules:
        since, until = rule_window(rule)
        if not since and not until:
            continue
        rid = str(rule.get("id") or "?")
        for end, value in (("since", since), ("until", until)):
            if value and known and value not in known:
                problems.append(f"rule {rid!r}: {end} {value!r} is not a chapter of this book")
        if since and until and chapter_key(since) > chapter_key(until):
            problems.append(f"rule {rid!r}: since {since!r} is after until {until!r}")
    return problems
