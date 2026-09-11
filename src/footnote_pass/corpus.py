"""
The style-inference corpus: what footnotes this book already carries.

Gate 1 of the skill asks the user to confirm *why* the existing notes were
written, and that question is only answerable from evidence. This module
assembles it: every active ``footnote`` annotation, split into

- **glosses** — notes with display text, i.e. something that actually publishes;
- **placeholders** — notes that are only an ``[anchor]``
  (``anchors.is_effectively_blank``), e.g. the bare ``[Sancerre]`` rows a reader
  leaves as a to-do. They publish *nothing* (``endnotes.build_endnote_artifacts``
  skips empty display text, and logs nothing when it does), so they are evidence
  of intent, never of style.

Each gloss carries its aligned Spanish sentence so the inferred category can be
checked against what the note is actually reacting to.

**Counts go to stdout; the corpus goes to a file.** A real book runs to dozens of
notes, and echoing them into an agent's context is the 29.4KB overflow
``docs/ANNOTATION_REVIEW.md`` records. ``style_corpus.md`` is the relay artifact.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.annotations import store
from src.annotations.anchors import is_effectively_blank
from src.endnotes import parse_endnote_content
from src.footnote_pass import FOOTNOTE_TYPE

logger = logging.getLogger(__name__)


def footnotes_dir(project_dir: Path) -> Path:
    """Working dir: ``<project>/.harness/footnotes/`` (shared .harness root)."""
    return Path(project_dir) / ".harness" / "footnotes"


@dataclass
class ExistingNote:
    """One active footnote annotation, parsed, with its aligned sentence."""

    key: str
    chapter_id: str
    es_idx: Optional[int]
    sub_id: Optional[str]
    content: str
    anchor: Optional[str]
    display_text: str
    es_sentence: str
    origin: Optional[str] = None

    @property
    def is_placeholder(self) -> bool:
        """True when the note publishes nothing — only an anchor, or empty."""
        return not self.display_text

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "chapter_id": self.chapter_id,
            "es_idx": self.es_idx,
            "sub_id": self.sub_id,
            "anchor": self.anchor,
            "display_text": self.display_text,
            "es_sentence": self.es_sentence,
            "origin": self.origin,
        }


@dataclass
class StyleCorpus:
    """The whole book's footnotes, split by whether they publish anything."""

    glosses: list[ExistingNote] = field(default_factory=list)
    placeholders: list[ExistingNote] = field(default_factory=list)

    @property
    def all_notes(self) -> list[ExistingNote]:
        return sorted(
            self.glosses + self.placeholders,
            key=lambda n: (n.chapter_id, n.es_idx or 0, str(n.sub_id)),
        )

    def by_origin(self) -> dict[str, int]:
        return dict(Counter((n.origin or "reader") for n in self.all_notes))

    def by_chapter(self) -> dict[str, int]:
        return dict(Counter(n.chapter_id for n in self.all_notes))


def load_alignment_rows(project_dir: Path, chapter_id: str) -> list[dict]:
    """The chapter's alignment rows, in file order (``[]`` when unusable).

    Tolerant on purpose, matching ``endnotes._load_alignment_es_map``: a corrupt
    or missing alignment yields nothing rather than an exception, because one bad
    chapter must not take down a whole-book audit.
    """
    path = Path(project_dir) / "alignments" / f"{chapter_id}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("unreadable alignment %s: %s", path, exc)
        return []
    return [r for r in (data.get("alignments") or []) if isinstance(r, dict)]


def load_alignment_es_map(project_dir: Path, chapter_id: str) -> dict[int, str]:
    """``{es_idx: es_sentence}`` for one chapter, or ``{}`` when unusable."""
    out: dict[int, str] = {}
    for row in load_alignment_rows(project_dir, chapter_id):
        if "es_idx" in row and "es" in row:
            out[row["es_idx"]] = row["es"]
    return out


def chapter_body_path(project_dir: Path, chapter_id: str) -> Path:
    """Where the translated chapter text lives — what endnotes inject into."""
    return Path(project_dir) / "chapters" / f"{chapter_id}.txt"


def read_chapter_body(project_dir: Path, chapter_id: str) -> Optional[str]:
    """The translated chapter text, or ``None`` when it is not on disk."""
    path = chapter_body_path(project_dir, chapter_id)
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("unreadable chapter body %s: %s", path, exc)
        return None


def build_corpus(
    project_dir: Path, *, chapters: Optional[list[str]] = None
) -> StyleCorpus:
    """Read the book's active footnotes into a :class:`StyleCorpus`.

    ``chapters`` restricts the scan; the default is the whole book, which is what
    style inference wants — a three-chapter slice is not a style.
    """
    project_dir = Path(project_dir)
    wanted = set(chapters) if chapters else None

    records = store.load_active(project_dir, types=(FOOTNOTE_TYPE,))
    es_maps: dict[str, dict[int, str]] = {}

    corpus = StyleCorpus()
    for record in records:
        chapter_id = record.get("chapter_id") or ""
        if wanted is not None and chapter_id not in wanted:
            continue
        content = record.get("content") or ""
        anchor, display_text = parse_endnote_content(content)
        if chapter_id not in es_maps:
            es_maps[chapter_id] = load_alignment_es_map(project_dir, chapter_id)
        note = ExistingNote(
            key=store.target_key(record),
            chapter_id=chapter_id,
            es_idx=record.get("es_idx"),
            sub_id=store.storage_sub_id(record.get("sub_id")),
            content=content,
            anchor=anchor,
            display_text=display_text,
            es_sentence=es_maps[chapter_id].get(record.get("es_idx"), ""),
            origin=record.get("origin"),
        )
        # ``is_effectively_blank`` and an empty ``display_text`` answer the same
        # question from opposite ends (every bracket stripped vs. only the first);
        # a note is a placeholder when either says so, because either is enough
        # to make the published endnote empty.
        if note.is_placeholder or is_effectively_blank(content):
            corpus.placeholders.append(note)
        else:
            corpus.glosses.append(note)
    return corpus


def already_noted_keys(
    project_dir: Path, *, chapters: Optional[list[str]] = None
) -> set[tuple[str, Optional[int]]]:
    """``{(chapter_id, es_idx)}`` for every active footnote, placeholders included.

    The dedupe set handed to the scanner. Placeholders count: a sentence the
    reader already marked is a sentence the scanner must not re-propose, even
    though nothing publishes there yet.
    """
    wanted = set(chapters) if chapters else None
    return {
        (r.get("chapter_id") or "", r.get("es_idx"))
        for r in store.load_active(project_dir, types=(FOOTNOTE_TYPE,))
        if r.get("es_idx") is not None
        and (wanted is None or r.get("chapter_id") in wanted)
    }


def render_style_corpus(corpus: StyleCorpus, project_name: str) -> str:
    """Render ``style_corpus.md`` — the document Gate 1 is argued from."""
    lines = [
        f"# Existing footnotes — {project_name}",
        "",
        f"- Glosses (publish text): **{len(corpus.glosses)}**",
        f"- Placeholders (anchor only, publish nothing): **{len(corpus.placeholders)}**",
        "",
        "Read every gloss below and infer *why* it was written — what problem in the",
        "sentence it reacts to. Those reasons are the taxonomy the scan looks for; the",
        "wording of the notes is not the point at this stage.",
        "",
        "## Glosses",
        "",
    ]
    if not corpus.glosses:
        lines += ["_None. This book has no footnotes to infer a style from._", ""]
    for note in sorted(corpus.glosses, key=lambda n: (n.chapter_id, n.es_idx or 0)):
        anchor_line = (
            f"- **Anchor:** `{note.anchor}`"
            if note.anchor
            else "- **Anchor:** none (the marker falls to the end of the sentence)"
        )
        sentence = note.es_sentence or (
            "(no aligned sentence — this note is an orphan and publishes nothing)"
        )
        lines += [
            f"### {note.chapter_id} · es_idx {note.es_idx}"
            + (f" · origin `{note.origin}`" if note.origin else ""),
            "",
            anchor_line,
            f"- **Note:** {note.display_text}",
            f"- **Sentence:** {sentence}",
            "",
        ]

    lines += ["## Placeholders", ""]
    if not corpus.placeholders:
        lines += ["_None._", ""]
    else:
        lines += [
            "These publish nothing today. They are spots someone marked and never",
            "wrote up — intent, not style.",
            "",
        ]
        for note in sorted(
            corpus.placeholders, key=lambda n: (n.chapter_id, n.es_idx or 0)
        ):
            lines.append(
                f"- `{note.chapter_id}` es_idx {note.es_idx} — content {note.content!r}"
            )
        lines.append("")
    return "\n".join(lines)


def style(
    project_dir: Path, *, chapters: Optional[list[str]] = None
) -> dict:
    """The ``style`` subcommand: write ``style_corpus.md``, return counts only."""
    project_dir = Path(project_dir)
    corpus = build_corpus(project_dir, chapters=chapters)
    out_dir = footnotes_dir(project_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    corpus_path = out_dir / "style_corpus.md"
    corpus_path.write_text(
        render_style_corpus(corpus, project_dir.name), encoding="utf-8"
    )

    orphans = [n.key for n in corpus.all_notes if not n.es_sentence]
    return {
        "status": "ok",
        "corpus_path": str(corpus_path),
        "counts": {
            "glosses": len(corpus.glosses),
            "placeholders": len(corpus.placeholders),
            "total": len(corpus.all_notes),
            "orphaned": len(orphans),
        },
        "by_origin": corpus.by_origin(),
        "by_chapter": corpus.by_chapter(),
        "orphaned": orphans,
        "chapters": sorted(chapters) if chapters else None,
        "instructions": (
            "Read corpus_path in full — do not skim it. Infer one category per "
            "reason a note exists, name the note that evidences each, and take "
            "those to the user as Gate 1. Nothing here is a style to imitate yet."
            if corpus.glosses
            else "This book has no footnote glosses, so there is no house style to "
            "infer. Ask the user what kind of note they want before scanning."
        ),
    }
