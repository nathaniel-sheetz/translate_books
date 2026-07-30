"""
Turn live annotations into reviewable targets, with their evidence attached.

One target = one annotation. Everything a target carries is computed here, in
Python: the anchors, the aligned sentence pair, the surrounding sentences, the
matching glossary terms, and the book-wide concordance. The model reasons over
that evidence; it never goes looking for it. That keeps the two backends
identical and the retrieval reproducible.

This module also owns the **eligibility gates** — the deterministic decisions
made before any LLM call:

``imported``          an ``origin: "gutenberg"`` footnote already carries its body
``already_reviewed``  a prior run's text is still intact (see below)
``orphaned``          ``es_idx`` no longer resolves to an aligned sentence
``multi_anchor``      a footnote naming several spans; reviewed, never auto-written

``already_reviewed`` is what actually stops notes duplicating across runs, and it
is exact rather than a judgement call: a record written by ``apply`` carries an
``ai_review`` sidecar recording the text it wrote, so the gate is a string
comparison against the live ``content``. It self-heals, too — editing the note in
the reader goes through ``POST /api/annotation``, which rebuilds the record from a
fixed key set and so drops ``ai_review``, correctly re-opening the annotation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from src.annotations import store
from src.annotations.anchors import bare_hint, parse_anchors
from src.annotations.concordance import BookIndex, search_terms
from src.utils.text_utils import fold

logger = logging.getLogger(__name__)

# Aligned sentences of context quoted on each side of the annotated one.
CONTEXT_SENTENCES = 3

# Types whose resolution depends on usage elsewhere in the book. Footnote and
# flag targets skip the (comparatively expensive) concordance unless their anchor
# recurs, which is checked separately.
CONCORDANCE_TYPES = frozenset({"inconsistency", "word_choice"})

# Skip reasons.
SKIP_IMPORTED = "imported"
SKIP_ALREADY_REVIEWED = "already_reviewed"
SKIP_ORPHANED = "orphaned"

# Withheld-from-write reason (still reviewed and reported).
MANUAL_MULTI_ANCHOR = "multi_anchor"


@dataclass
class AnnotationTarget:
    """One annotation plus the evidence needed to resolve it."""

    key: str
    chapter_id: str
    es_idx: int
    sub_id: Optional[str]
    ann_type: str
    content: str

    anchors: list[str] = field(default_factory=list)
    hint: str = ""
    hint_in_sentence: bool = False

    es_sentence: str = ""
    en_sentence: str = ""
    context_before: list[str] = field(default_factory=list)
    context_after: list[str] = field(default_factory=list)
    chunk_id: Optional[str] = None

    glossary_hits: list[dict] = field(default_factory=list)
    concordance: list[dict] = field(default_factory=list)

    manual_reason: Optional[str] = None
    record: dict = field(default_factory=dict)

    @property
    def is_writable(self) -> bool:
        """False when a resolution must be applied by hand (see manual_reason)."""
        return self.manual_reason is None


@dataclass
class SkippedAnnotation:
    """An annotation excluded before any LLM call, with the reason why."""

    key: str
    chapter_id: str
    es_idx: Any
    sub_id: Optional[str]
    ann_type: str
    content: str
    reason: str


def _load_alignment(project_dir: Path, chapter_id: str) -> list[dict]:
    path = Path(project_dir) / "alignments" / f"{chapter_id}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("unreadable alignment %s: %s", path, exc)
        return []
    return data.get("alignments") or []


def _glossary_terms(project_dir: Path) -> list[dict]:
    path = Path(project_dir) / "glossary.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [t for t in (data.get("terms") or []) if isinstance(t, dict)]


def _match_glossary(terms: list[dict], needles: Iterable[str]) -> list[dict]:
    """Glossary entries whose English or Spanish side touches any needle.

    Matched on folded substrings in both directions so ``muserola`` hits a
    ``muserón`` entry and vice versa. Needles shorter than
    :data:`concordance.MIN_TERM_LEN` are ignored — ``de`` / ``en`` would
    otherwise substring-match most of the glossary.
    """
    from src.annotations.concordance import MIN_TERM_LEN

    folded_needles = [
        fold(n)
        for n in needles
        if n and n.strip() and len(fold(n)) >= MIN_TERM_LEN
    ]
    if not folded_needles:
        return []
    out: list[dict] = []
    for term in terms:
        haystacks = [fold(str(term.get("english") or "")), fold(str(term.get("spanish") or ""))]
        for alt in term.get("alternatives") or []:
            haystacks.append(fold(str(alt)))
        for needle in folded_needles:
            if any(needle in h or (h and h in needle) for h in haystacks):
                out.append(term)
                break
    return out


def _already_reviewed(record: dict) -> bool:
    """True when this record still holds exactly the text a prior run wrote."""
    sidecar = record.get(store.AI_REVIEW_KEY)
    if not isinstance(sidecar, dict):
        return False
    written = sidecar.get("written_content")
    return isinstance(written, str) and written == (record.get("content") or "")


def build_targets(
    project_dir: Path,
    *,
    types: Optional[Iterable[str]] = None,
    chapters: Optional[Iterable[str]] = None,
    index: Optional[BookIndex] = None,
) -> tuple[list[AnnotationTarget], list[SkippedAnnotation]]:
    """Build review targets for a book's live annotations.

    Args:
        project_dir: ``projects/<slug>/``.
        types: Annotation types to include (``None`` = all four).
        chapters: Chapter ids to include (``None`` = whole book, the default).
        index: A prebuilt :class:`BookIndex`; built on demand when concordance
            is actually needed.

    Returns:
        ``(targets, skipped)`` — targets to review, and the annotations gated out
        before any LLM call, each with its reason.
    """
    project_dir = Path(project_dir)
    wanted_types = set(types) if types else set(store.ANNOTATION_TYPES)
    wanted_chapters = set(chapters) if chapters else None

    records = store.load_active(project_dir, types=wanted_types)
    if wanted_chapters is not None:
        records = [r for r in records if r.get("chapter_id") in wanted_chapters]

    glossary = _glossary_terms(project_dir)
    alignment_cache: dict[str, list[dict]] = {}

    targets: list[AnnotationTarget] = []
    skipped: list[SkippedAnnotation] = []

    def _skip(record: dict, reason: str) -> None:
        chapter_id, es_idx, sub = store.record_key(record)
        skipped.append(
            SkippedAnnotation(
                key=store.target_key(record),
                chapter_id=chapter_id,
                es_idx=es_idx,
                sub_id=sub,
                ann_type=record.get("type") or "flag",
                content=record.get("content") or "",
                reason=reason,
            )
        )

    needs_concordance = False
    for record in records:
        if record.get("origin") == "gutenberg":
            _skip(record, SKIP_IMPORTED)
            continue
        if _already_reviewed(record):
            _skip(record, SKIP_ALREADY_REVIEWED)
            continue

        chapter_id, es_idx, sub = store.record_key(record)
        if chapter_id not in alignment_cache:
            alignment_cache[chapter_id] = _load_alignment(project_dir, chapter_id)
        rows = alignment_cache[chapter_id]

        row_pos = next(
            (i for i, a in enumerate(rows) if a.get("es_idx") == es_idx), None
        )
        if row_pos is None:
            # The sentence this note was anchored to no longer exists — usually a
            # retranslation that re-anchoring could not match.
            _skip(record, SKIP_ORPHANED)
            continue

        row = rows[row_pos]
        content = record.get("content") or ""
        ann_type = record.get("type") or "flag"
        anchors = parse_anchors(content)
        hint = bare_hint(content)
        es_sentence = row.get("es") or ""

        target = AnnotationTarget(
            key=store.target_key(record),
            chapter_id=chapter_id,
            es_idx=es_idx,
            sub_id=sub,
            ann_type=ann_type,
            content=content,
            anchors=anchors,
            hint=hint,
            # Disambiguates the bare-word note: a hint already in the sentence is
            # the word being questioned; one that is absent is a proposed
            # replacement. Folded so accents/case don't decide it.
            hint_in_sentence=bool(hint) and fold(hint) in fold(es_sentence),
            es_sentence=es_sentence,
            en_sentence=row.get("en") or "",
            context_before=[
                (r.get("es") or "")
                for r in rows[max(0, row_pos - CONTEXT_SENTENCES): row_pos]
            ],
            context_after=[
                (r.get("es") or "")
                for r in rows[row_pos + 1: row_pos + 1 + CONTEXT_SENTENCES]
            ],
            chunk_id=row.get("chunk_id"),
            glossary_hits=_match_glossary(glossary, anchors + [hint]),
            record=record,
        )

        # A footnote naming several spans cannot be written back safely: endnotes
        # consume only the first bracket, so one gloss would publish under one
        # anchor and the remaining brackets would print verbatim. The right fix is
        # splitting it into N annotations, which renumbers endnotes — a human call.
        if ann_type == "footnote" and len(anchors) > 1:
            target.manual_reason = MANUAL_MULTI_ANCHOR

        if ann_type in CONCORDANCE_TYPES:
            needs_concordance = True
        targets.append(target)

    if needs_concordance and targets:
        if index is None:
            index = BookIndex(project_dir)
        for target in targets:
            if target.ann_type not in CONCORDANCE_TYPES:
                continue
            # Anchors are Spanish (they are matched against the translation).
            # The hint may be either language — a note like "[Fantasma] The
            # Phantom" names the Spanish word and its English source — so it is
            # searched on both sides. The en-side hits carry their paired es,
            # which is how a competing rendering surfaces.
            results = search_terms(
                index,
                target.anchors,
                sides=("es",),
                skip=(target.chapter_id, target.es_idx),
            )
            if target.hint:
                results.extend(
                    search_terms(
                        index,
                        [target.hint],
                        sides=("es", "en"),
                        skip=(target.chapter_id, target.es_idx),
                    )
                )
            target.concordance = results

    return targets, skipped
