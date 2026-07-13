"""Chapter sampling for the address-map drafting beat.

The forms-of-address (usted/tú) map must reflect the **whole book**, because
address relationships evolve as the story progresses, and it should be seeded
from the scenes where register choices actually surface: distinct speakers
addressing and referring to *each other*, not merely a stray line of dialogue.

This module scores each chapter of the **English source** (the map is drafted
before translation, so Spanish tú/usted markers don't exist yet — the signal has
to be language-neutral) by an interpersonal-dialogue concentration metric, then
returns a representative spread across the beginning / middle / end of the book.

The scoring deliberately does **not** reuse ``src/judges/subagent.py``'s
``_DIALOGUE_MARKERS`` (raya/guillemets): those are *Spanish-translation* markers.
English source dialogue is quote-delimited, so we count quotes, attribution
verbs, and second-person address instead.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from src.utils.source_text import load_chapter_source_text

logger = logging.getLogger(__name__)

# Attribution verbs that mark a speaker turn ("...", she said). A dense cluster of
# these is the tell of back-and-forth dialogue rather than a lone quotation.
_ATTRIBUTION_RE = re.compile(
    r"\b(said|asked|replied|answered|cried|exclaimed|whispered|shouted|murmured"
    r"|added|continued|remarked|inquired|responded|called|begged|repeated"
    r"|demanded|retorted|sighed|laughed|declared|insisted|protested|snapped"
    r"|muttered|stammered|urged|warned|scolded|greeted)\b",
    re.IGNORECASE,
)

# Second-person address — the direct signal that one character is speaking *to*
# another (which is exactly what carries tú/usted). Includes archaic thou/thee.
_SECOND_PERSON_RE = re.compile(
    r"\b(you|your|yours|yourself|thou|thee|thy|thine)\b", re.IGNORECASE
)

# A chapter needs at least this many quoted turns to count as real back-and-forth
# dialogue (vs. a single stray quotation or an epigraph).
_MIN_TURNS = 4


class ChapterAddressScore:
    """Per-chapter interpersonal-dialogue metrics used for sampling."""

    __slots__ = ("chapter_id", "turns", "attributions", "second_person", "words", "density")

    def __init__(self, chapter_id: str, turns: int, attributions: int,
                 second_person: int, words: int) -> None:
        self.chapter_id = chapter_id
        self.turns = turns
        self.attributions = attributions
        self.second_person = second_person
        self.words = words
        # Concentration: interpersonal signal per 1k words. Density (not raw
        # volume) is what we want — a short, dialogue-packed confrontation beats
        # a long chapter with a few scattered lines.
        raw = turns + attributions + second_person
        self.density = raw / max(1.0, words / 1000.0)

    def to_dict(self) -> dict:
        return {
            "chapter_id": self.chapter_id,
            "turns": self.turns,
            "attributions": self.attributions,
            "second_person": self.second_person,
            "words": self.words,
            "density": round(self.density, 2),
        }


def score_chapter_text(chapter_id: str, text: str) -> ChapterAddressScore:
    """Score one chapter's English source for interpersonal-dialogue concentration."""
    # Quoted turns: opening curly quotes plus straight-quote pairs. Either style
    # approximates the number of spoken segments in the chapter.
    turns = text.count("“") + text.count('"') // 2
    attributions = len(_ATTRIBUTION_RE.findall(text))
    second_person = len(_SECOND_PERSON_RE.findall(text))
    words = len(text.split())
    return ChapterAddressScore(chapter_id, turns, attributions, second_person, words)


def _chapter_ids_in_order(project_dir: Path) -> list[str]:
    """Return chapter ids to consider, skipping front/back matter when known.

    Prefers project.json's ``chapter_manifest`` (kind == "chapter"); falls back to
    globbing ``chapters/chapter_*.txt`` then ``chunks/`` when no manifest exists.
    """
    manifest_path = project_dir / "project.json"
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = data.get("chapter_manifest") or []
            ids = [
                str(entry["id"])
                for entry in manifest
                if isinstance(entry, dict) and entry.get("id")
                and entry.get("kind", "chapter") == "chapter"
            ]
            if ids:
                return ids
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning("Could not read chapter_manifest from %s: %s", manifest_path, exc)

    # Fallback: derive ids from files on disk, in sorted (natural chapter) order.
    chapters_dir = project_dir / "chapters"
    if chapters_dir.exists():
        ids = sorted(p.stem for p in chapters_dir.glob("chapter_*.txt"))
        if ids:
            return ids
    chunks_dir = project_dir / "chunks"
    if chunks_dir.exists():
        stems = {
            p.stem.rsplit("_chunk_", 1)[0]
            for p in chunks_dir.glob("*_chunk_*.json")
        }
        return sorted(stems)
    return []


def _stratified_pick(
    ranked: list[ChapterAddressScore], max_chapters: int
) -> list[ChapterAddressScore]:
    """Pick a spread across beginning/middle/end, then fill by overall density.

    ``ranked`` is ordered best-density-first. We split the *chapter order* (not
    the ranking) into three sections and take the top scorer(s) from each so the
    sample can't collapse onto the front of the book, then fill any remaining
    slots with the highest-density chapters left.
    """
    if len(ranked) <= max_chapters:
        return sorted(ranked, key=lambda s: s.chapter_id)

    by_chapter = sorted(ranked, key=lambda s: s.chapter_id)
    n = len(by_chapter)
    thirds = [by_chapter[: n // 3], by_chapter[n // 3: 2 * n // 3], by_chapter[2 * n // 3:]]

    chosen: dict[str, ChapterAddressScore] = {}
    per_section = max(1, max_chapters // 3)
    for section in thirds:
        top = sorted(section, key=lambda s: s.density, reverse=True)[:per_section]
        for s in top:
            chosen[s.chapter_id] = s

    # Fill remaining slots with the best density chapters not already chosen.
    for s in ranked:
        if len(chosen) >= max_chapters:
            break
        chosen.setdefault(s.chapter_id, s)

    return sorted(chosen.values(), key=lambda s: s.chapter_id)


def select_address_sample_chapters(
    project_dir: Path,
    *,
    max_chapters: int = 6,
    min_turns: int = _MIN_TURNS,
) -> list[ChapterAddressScore]:
    """Select a whole-book spread of the highest interpersonal-dialogue chapters.

    Scores every eligible chapter's English source, keeps those with real
    back-and-forth dialogue (``turns >= min_turns``), and returns a stratified
    spread (beginning/middle/end) of up to ``max_chapters``. Falls back to the
    densest chapters regardless of the turn gate if nothing clears it (a
    dialogue-light book still gets a sample).
    """
    project_dir = Path(project_dir)
    scores: list[ChapterAddressScore] = []
    for chapter_id in _chapter_ids_in_order(project_dir):
        text, _mtime, _kind = load_chapter_source_text(project_dir, chapter_id)
        if not text.strip():
            continue
        scores.append(score_chapter_text(chapter_id, text))

    if not scores:
        return []

    qualifying = [s for s in scores if s.turns >= min_turns]
    pool = qualifying or scores  # dialogue-light book: fall back to all scored
    ranked = sorted(pool, key=lambda s: s.density, reverse=True)
    return _stratified_pick(ranked, max_chapters)
