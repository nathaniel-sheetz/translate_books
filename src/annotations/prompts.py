"""
Render one annotation target into a prompt.

Both backends go through :func:`build_prompt_parts`, so a verdict is produced from
the same bytes whichever ran — the same seam ``src/judges/base.py`` establishes
for judges.

The split point is ``_CACHE_PREFIX_SPLIT_MARKER``. Everything above it (the type's
rubric, the style guide, the glossary, the target language) is identical for every
annotation of a type, so it is written once per type as ``preamble.<type>.txt``
and passed to the headless CLI via ``--system-prompt-file``. Style guide alone
runs ~400-550 tokens across real books, which would miss Sonnet's 1024-token cache
minimum; including the glossary carries the preamble to ~1.4-2.3k and over the
line — and the book's established terminology is exactly what a word-choice or
inconsistency call needs anyway.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from src.annotations.concordance import format_concordance
from src.annotations.targets import AnnotationTarget
from src.judges.base import _CACHE_PREFIX_SPLIT_MARKER
from src.judges.llm_io import load_template, prompt_version, render

# One template per annotation type. "flag" is "Other" in the reader UI.
TEMPLATES = {
    "word_choice": "annotation_word_choice.txt",
    "inconsistency": "annotation_inconsistency.txt",
    "footnote": "annotation_footnote.txt",
    "flag": "annotation_flag.txt",
}

# Keys every verdict must carry (validated by parse_verdict).
REQUIRED_FIELDS = ("state", "recommendation", "note_text")

# Bound the glossary block so a 900-term book does not push the preamble past
# what is useful. Terms are already ordered as the book's glossary orders them.
MAX_GLOSSARY_TERMS = 400


def template_for(ann_type: str) -> str:
    return TEMPLATES.get(ann_type, TEMPLATES["flag"])


def template_version(ann_type: str) -> str:
    """SHA-256 of the type's template — pins which prompt produced a verdict."""
    return prompt_version(template_for(ann_type))


def load_style_guide_text(project_dir: Path) -> str:
    path = Path(project_dir) / "style.json"
    if not path.exists():
        return "(no style guide recorded for this book)"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "(style guide unreadable)"
    return (data.get("content") or "").strip() or "(style guide is empty)"


def format_glossary(terms: list[dict], limit: int = MAX_GLOSSARY_TERMS) -> str:
    if not terms:
        return "(no glossary recorded for this book)"
    lines = []
    for term in terms[:limit]:
        english = str(term.get("english") or "").strip()
        spanish = str(term.get("spanish") or "").strip()
        if not english and not spanish:
            continue
        line = f"- {english} → {spanish}"
        context = str(term.get("context") or "").strip()
        if context:
            line += f"  ({context})"
        alternatives = [str(a).strip() for a in (term.get("alternatives") or []) if str(a).strip()]
        if alternatives:
            line += f"  [also: {', '.join(alternatives)}]"
        lines.append(line)
    if len(terms) > limit:
        lines.append(f"… and {len(terms) - limit} further terms not listed")
    return "\n".join(lines) if lines else "(no glossary recorded for this book)"


def _format_glossary_hits(hits: list[dict]) -> str:
    if not hits:
        return "(no glossary entry matches this annotation's terms)"
    # Bound by MAX_GLOSSARY_TERMS — never dump an uncapped match list into the body.
    return format_glossary(hits)


def _anchor_line(target: AnnotationTarget) -> str:
    """Describe the bracketed span(s) the note points at, if any."""
    if not target.anchors:
        return (
            "Marked span: none given — the note points at the sentence as a whole."
        )
    if len(target.anchors) == 1:
        return f"Marked span: {target.anchors[0]!r}"
    quoted = ", ".join(repr(a) for a in target.anchors)
    return (
        f"Marked spans ({len(target.anchors)}): {quoted}\n"
        "This note names several spans at once; address all of them."
    )


def _hint_line(target: AnnotationTarget) -> str:
    """Describe the reader's own words, and whether they are in the sentence.

    The framing is per type, because the same fact means different things.

    For ``word_choice`` / ``inconsistency`` a bare word is ambiguous on its face —
    ``humilde`` could be the word being questioned or the replacement being
    proposed — and whether it occurs in the translated sentence settles it. That
    is computed in Python rather than left for the model to guess.

    For ``footnote`` the same absence means nothing of the sort: a gloss is
    *expected* not to appear in the sentence, so "not in the sentence" must not be
    read as "a proposed replacement". Telling a footnote worker otherwise made it
    classify finished glosses (``Himno cristiano por George Washington Doane
    (1848)``, ``[Meadow»] Prado de Misty``) as instructions and rewrite them.
    """
    if not target.hint:
        return "Reader's note text: (none — only the marked span, or nothing at all)."

    if target.ann_type == "footnote":
        return (
            f"Reader's note text: {target.hint!r}\n"
            "For a footnote this is EITHER an already-written gloss (in which case "
            "the annotation is already_resolved and you must leave it alone) OR a "
            "short instruction naming what to gloss. Decide which. A brief gloss "
            "still counts as a gloss: translating or identifying the marked span — "
            "«Pequeño sin nombre», «Prado de Misty», «Himno cristiano de 1848» — is "
            "a finished note, not an instruction. Treat it as an instruction only "
            "when it names a topic without saying anything about it (biblia, "
            "comillas, poesía)."
        )

    if target.ann_type == "flag":
        return (
            f"Reader's note text: {target.hint!r}\n"
            "This may name a topic, state a concern, or record a conclusion the "
            "reader already reached. Decide which before answering."
        )

    if target.hint_in_sentence:
        return (
            f"Reader's note text: {target.hint!r}\n"
            "This text DOES appear in the translated sentence, so it is most "
            "likely the wording the reader is questioning."
        )
    return (
        f"Reader's note text: {target.hint!r}\n"
        "This text does NOT appear in the translated sentence, so it is most "
        "likely a replacement the reader is proposing, a question, or an "
        "instruction to you."
    )


def _join_context(sentences: list[str]) -> str:
    cleaned = [s.strip() for s in sentences if s and s.strip()]
    return " ".join(cleaned) if cleaned else "(none)"


def build_prompt_parts(
    target: AnnotationTarget,
    context: dict[str, Any],
) -> tuple[str, str]:
    """Render ``(preamble, body)`` for one target.

    ``preamble + body`` is the full prompt; the preamble is byte-identical across
    every target of the same type, which is what makes it cacheable.
    """
    template = load_template(template_for(target.ann_type))
    chapter_ref = f"{target.chapter_id}, sentence {target.es_idx}"

    variables = {
        "target_language": context.get("target_language") or "the target language",
        "style_guide": context.get("style_guide") or "(none)",
        "glossary": context.get("glossary") or "(none)",
        "key": target.key,
        "chapter_ref": chapter_ref,
        "annotation_content": target.content or "(empty)",
        "anchor_line": _anchor_line(target),
        "hint_line": _hint_line(target),
        "es_sentence": target.es_sentence or "(missing)",
        "en_sentence": target.en_sentence or "(missing)",
        "context_before": _join_context(target.context_before),
        "context_after": _join_context(target.context_after),
        "glossary_hits": _format_glossary_hits(target.glossary_hits),
        "concordance": format_concordance(target.concordance),
    }

    rendered = render(template, variables)
    prefix, marker, suffix = rendered.partition(_CACHE_PREFIX_SPLIT_MARKER)
    if not marker:
        # No split marker: the whole prompt is the body, and fan-out uses the
        # full prompt file (exactly how judges degrade).
        return "", rendered
    return prefix, suffix.lstrip("\n")


def build_prompt(target: AnnotationTarget, context: dict[str, Any]) -> str:
    prefix, suffix = build_prompt_parts(target, context)
    return prefix + suffix


def build_context(project_dir: Path, *, target_language: Optional[str] = None) -> dict[str, Any]:
    """Assemble the per-book shared prompt inputs.

    ``target_language`` falls back to ``.harness/config.json``; a book with no
    harness config (older projects) defaults to Spanish, which is what every
    project in the repo targets.
    """
    from src.annotations.targets import _glossary_terms
    from src.harness import state as hstate

    project_dir = Path(project_dir)
    cfg = hstate.load_config(project_dir)
    language = target_language or cfg.get("target_language") or "Spanish"

    return {
        "target_language": language,
        "style_guide": load_style_guide_text(project_dir),
        "glossary": format_glossary(_glossary_terms(project_dir)),
        "locale": cfg.get("locale"),
    }
