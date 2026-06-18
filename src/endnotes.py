"""
Endnotes: turn sentence-level ``footnote`` annotations into reader-facing
endnotes in the exported EPUB.

A ``footnote`` annotation's note text becomes an endnote. A superscript
reference marker is injected into the body of the chapter, and a "Notas"
section (grouped by chapter, numbered sequentially across the whole book) is
rendered for the back matter.

Marker placement convention:
    The note text may begin with (or contain) a ``[bracketed]`` token. That
    token is matched *verbatim* against the Spanish sentence and the marker is
    inserted immediately after the match -- so trailing punctuation belongs
    inside the brackets (``[by then,]`` -> ``...by then,<sup>N</sup> we'll...``).
    If there is no bracket, or the bracket text is not found in the sentence,
    the marker falls back to the end of the sentence. The bracket token itself
    is stripped from the displayed endnote text.

This module is intentionally free of any ``web_ui`` or ``epub_builder`` import
so it can be called from ``build_epub`` via a local import without a circular
dependency. The in-text ``{{ENDNOTE:N}}`` token it emits is rendered to a
``<sup>`` by ``epub_builder._render_body_blocks``.
"""

import json
import logging
import re
from html import escape
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

logger = logging.getLogger(__name__)

# First [...] token anywhere in the note text. The captured group is the
# verbatim anchor (may include trailing punctuation, e.g. "then,").
_BRACKET_RE = re.compile(r'\[([^\]]*)\]')

# Underscore-wrapped italics, mirroring epub_builder._EM_RE, applied to the
# endnote display text after escaping.
_EM_RE = re.compile(r'(?<![A-Za-z0-9])_([^_\n]+?)_(?![A-Za-z0-9])')


class Endnote(NamedTuple):
    chapter_id: str
    es_idx: int
    number: int
    text: str


def _load_footnote_annotations(project_path: Path, chapter_id: str) -> Dict[int, str]:
    """Return ``{es_idx: content}`` for active ``footnote`` annotations.

    Applies the same append-only / tombstone / latest-wins rule used by the web
    UI (``web_ui/app.py:_load_annotations``): the most recent record per
    ``es_idx`` wins, ``{"removed": true}`` deletes, and the *final* type at an
    index decides whether it is still a footnote (a later non-footnote edit at
    the same sentence supersedes an earlier footnote).
    """
    path = project_path / "annotations.jsonl"
    if not path.exists():
        return {}

    by_idx: Dict[int, dict] = {}
    for line in path.read_text(encoding="utf-8").strip().split("\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("chapter_id") != chapter_id:
            continue
        if record.get("removed"):
            by_idx.pop(record.get("es_idx"), None)
        else:
            by_idx[record["es_idx"]] = record

    return {
        idx: (rec.get("content") or "")
        for idx, rec in by_idx.items()
        if rec.get("type") == "footnote"
    }


def _load_alignment_es_map(project_path: Path, chapter_id: str) -> Dict[int, str]:
    """Return ``{es_idx: es_sentence_text}`` from the chapter's alignment file."""
    path = project_path / "alignments" / f"{chapter_id}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Corrupt alignment file, skipping endnotes for %s", chapter_id)
        return {}
    out: Dict[int, str] = {}
    for a in data.get("alignments", []):
        if "es_idx" in a and "es" in a:
            out[a["es_idx"]] = a["es"]
    return out


def parse_endnote_content(content: str) -> Tuple[Optional[str], str]:
    """Split a note into ``(anchor, display_text)``.

    ``anchor`` is the verbatim text inside the first ``[...]`` token (or
    ``None`` if there is none / it is empty). ``display_text`` is the note with
    that one token removed and whitespace collapsed to single spaces.
    """
    m = _BRACKET_RE.search(content)
    if not m:
        return None, re.sub(r"\s+", " ", content).strip()
    anchor = m.group(1).strip()
    remainder = content[: m.start()] + content[m.end():]
    text = re.sub(r"\s+", " ", remainder).strip()
    return (anchor or None), text


def _injection_point(
    text: str, sent_start: int, sent_end: int, anchor: Optional[str]
) -> int:
    """Position in ``text`` to insert the marker for one endnote.

    After the anchor's first verbatim occurrence within the sentence span, or
    at the end of the sentence when there is no anchor / it is not found.
    """
    if anchor:
        a = text.find(anchor, sent_start, sent_end)
        if a != -1:
            return a + len(anchor)
    return sent_end


def build_endnote_artifacts(
    project_path: Path,
    ordered_chapters: List[Tuple[str, str]],
) -> Tuple[Dict[str, str], List[Endnote]]:
    """Compute endnote markers and the numbered endnote list.

    Args:
        project_path: project root (contains ``annotations.jsonl`` / ``alignments/``).
        ordered_chapters: ``[(chapter_id, chapter_text), ...]`` in book order.

    Returns:
        ``(injected_texts, entries)`` where ``injected_texts`` maps every input
        ``chapter_id`` to its text with ``{{ENDNOTE:N}}`` tokens inserted
        (unchanged when the chapter has no endnotes), and ``entries`` is the
        globally-numbered, book-ordered list of endnotes.

    Numbering and marker placement are computed together so the in-text markers
    and the rendered list can never drift: a note is numbered only when it has
    non-empty display text *and* its sentence is locatable in the body.
    """
    injected_texts: Dict[str, str] = {}
    entries: List[Endnote] = []
    counter = 0

    for chapter_id, text in ordered_chapters:
        annotations = _load_footnote_annotations(project_path, chapter_id)
        if not annotations:
            injected_texts[chapter_id] = text
            continue

        es_map = _load_alignment_es_map(project_path, chapter_id)
        insertions: List[Tuple[int, int]] = []  # (position, number)
        cursor = 0

        for es_idx in sorted(annotations):
            anchor, display_text = parse_endnote_content(annotations[es_idx])
            if not display_text:
                continue  # legacy placeholder (e.g. bare "[Sancerre]")

            sentence = es_map.get(es_idx)
            if not sentence:
                logger.warning(
                    "Endnote skipped: no aligned sentence for %s es_idx=%s",
                    chapter_id, es_idx,
                )
                continue

            sent_start = text.find(sentence, cursor)
            if sent_start == -1:
                logger.warning(
                    "Endnote skipped: sentence not found in body for %s es_idx=%s",
                    chapter_id, es_idx,
                )
                continue
            sent_end = sent_start + len(sentence)

            counter += 1
            insertions.append(
                (_injection_point(text, sent_start, sent_end, anchor), counter)
            )
            entries.append(Endnote(chapter_id, es_idx, counter, display_text))
            cursor = sent_end

        if not insertions:
            injected_texts[chapter_id] = text
            continue

        # Splice tokens into the original text by ascending position.
        parts: List[str] = []
        last = 0
        for pos, number in sorted(insertions):
            parts.append(text[last:pos])
            parts.append(f"{{{{ENDNOTE:{number}}}}}")
            last = pos
        parts.append(text[last:])
        injected_texts[chapter_id] = "".join(parts)

    return injected_texts, entries


def render_endnotes_xhtml(
    entries: List[Endnote],
    chapter_headings: Dict[str, str],
    chapter_files: Dict[str, str],
    section_heading: str = "Notas",
) -> str:
    """Render the back-matter "Notas" section as XHTML.

    Grouped by chapter (in book order), one ``<p class="endnote">`` per note.
    Each number links back to its in-text marker (``#enref-N``) and carries an
    ``id="en-N"`` so the marker can link forward to it.
    """
    body: List[str] = [f"<h1>{escape(section_heading)}</h1>"]

    # entries are already book-ordered with chapters contiguous; dict preserves
    # first-seen order for the per-chapter grouping.
    grouped: Dict[str, List[Endnote]] = {}
    for e in entries:
        grouped.setdefault(e.chapter_id, []).append(e)

    for chapter_id, notes in grouped.items():
        heading = chapter_headings.get(chapter_id, chapter_id)
        body.append(f"<h2>{escape(heading)}</h2>")
        href_base = chapter_files.get(chapter_id, "")
        for e in notes:
            text_html = _EM_RE.sub(r"<em>\1</em>", escape(e.text))
            back = f"{href_base}#enref-{e.number}"
            body.append(
                f'<p class="endnote">'
                f'<a id="en-{e.number}" href="{escape(back)}">{e.number}.</a> '
                f"{text_html}</p>"
            )

    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<!DOCTYPE html>",
        '<html xmlns="http://www.w3.org/1999/xhtml">',
        "<head><title>{}</title>"
        '<link rel="stylesheet" type="text/css" href="style.css"/>'
        "</head>".format(escape(section_heading)),
        "<body>",
        *body,
        "</body>",
        "</html>",
    ]
    return "\n".join(parts)
