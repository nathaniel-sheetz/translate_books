"""
Mechanical house rules: fixed in code at chunk commit, never asked of a model.

A rule with ``enforce: "mechanical"`` names a ``fixer`` from :data:`FIXERS`. The
fix runs on every translation as it is committed (API realtime, API batch and
the harness subagent/headless paths all go through :func:`fix_translation`),
each change is logged to the book's :data:`LOG_NAME`, and no prompt carries the
rule. A reader edit that brings the slip back is caught by the save check's
``house_rule`` rule (:func:`introduced_hits`).

A fixer is a word-bounded pattern and its replacement. Word-bounded is the
point: ``rió`` → ``rio`` must leave ``sonrió`` and ``crió`` alone, and the
pattern's ``\\b`` does that because the letters on either side are word
characters.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

logger = logging.getLogger(__name__)

LOG_NAME = "house_fixes.jsonl"


class Fixer(NamedTuple):
    pattern: re.Pattern
    replace: Callable[[re.Match], str]


def _keep_case(word: str) -> Callable[[re.Match], str]:
    """A replacement that keeps the matched word's leading capital."""
    return lambda m: word[0].upper() + word[1:] if m.group()[0].isupper() else word


#: Fixer name → what it matches and what it writes instead.
FIXERS: dict[str, Fixer] = {
    # The preterite of reír, RAE 2010: a monosyllable, so no tilde. The noun
    # río keeps its accent on the i and is a different string.
    "rio_no_tilde": Fixer(re.compile(r"\b[Rr]ió\b"), _keep_case("rio")),
}


def mechanical_rules(rules: list[dict[str, Any]]) -> list[tuple[str, Fixer]]:
    """``(rule id, fixer)`` for each mechanical rule in ``rules`` with a known fixer."""
    from src.judges.context import ENFORCE_MECHANICAL, rule_enforce

    found: list[tuple[str, Fixer]] = []
    for rule in rules:
        if rule_enforce(rule) != ENFORCE_MECHANICAL:
            continue
        name = str(rule.get("fixer") or "").strip()
        fixer = FIXERS.get(name)
        if fixer is None:
            logger.warning("House rule %r names unknown fixer %r; skipped", rule.get("id"), name)
            continue
        found.append((str(rule.get("id") or name), fixer))
    return found


def book_mechanical_rules(project_dir: Optional[Path]) -> list[tuple[str, Fixer]]:
    """The mechanical rules in scope for this book and not turned off for it."""
    from src.judges.context import house_rules_for_book

    return mechanical_rules(house_rules_for_book(project_dir))


def apply_fixes(
    text: str, fixers: list[tuple[str, Fixer]]
) -> tuple[str, list[dict[str, Any]]]:
    """``(fixed text, changes)``; each change is ``{rule_id, before, after, count}``."""
    changes: list[dict[str, Any]] = []
    for rule_id, fixer in fixers:
        seen: dict[tuple[str, str], int] = {}

        def sub(m: re.Match) -> str:
            new = fixer.replace(m)
            seen[(m.group(), new)] = seen.get((m.group(), new), 0) + 1
            return new

        text = fixer.pattern.sub(sub, text)
        changes += [
            {"rule_id": rule_id, "before": old, "after": new, "count": n}
            for (old, new), n in seen.items()
        ]
    return text, changes


def log_fixes(
    project_dir: Optional[Path], chunk_id: str, changes: list[dict[str, Any]], *, writer: str
) -> None:
    """Append one row per change to the book's ``house_fixes.jsonl``.

    With no book to log to (a comparison run), the changes go to the logger.
    A log that cannot be written never stops the commit it describes.
    """
    if not changes:
        return
    ts = datetime.now().isoformat(timespec="seconds")
    if project_dir is None:
        for change in changes:
            logger.info("house fix in %s: %s", chunk_id, change)
        return
    try:
        with (Path(project_dir) / LOG_NAME).open("a", encoding="utf-8") as fh:
            for change in changes:
                row = {"ts": ts, "chunk_id": chunk_id, "writer": writer, **change}
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Could not log house fixes for %s: %s", chunk_id, exc)


def fix_translation(
    text: str, *, project_dir: Optional[Path], chunk_id: str, writer: str
) -> str:
    """Apply this book's mechanical rules to a translation being committed, and log them."""
    try:
        fixers = book_mechanical_rules(project_dir)
    except Exception as exc:  # noqa: BLE001 - a broken house file must not block a commit
        logger.warning("Skipping house fixes for %s: %s", chunk_id, exc)
        return text
    fixed, changes = apply_fixes(text, fixers)
    log_fixes(project_dir, chunk_id, changes, writer=writer)
    return fixed


def introduced_hits(
    before: str, after: str, fixers: list[tuple[str, Fixer]]
) -> list[dict[str, str]]:
    """A mechanical rule's pattern the edit brought into a sentence, as ``{rule, text}``.

    For the save check: a reader edit is not fixed behind the translator's
    back, it is flagged, and only when the edit introduced the match.
    """
    hits: list[dict[str, str]] = []
    for rule_id, fixer in fixers:
        found = fixer.pattern.search(after)
        if found and len(fixer.pattern.findall(after)) > len(fixer.pattern.findall(before)):
            hits.append({"rule": "house_rule", "text": found.group(), "rule_id": rule_id})
    return hits
