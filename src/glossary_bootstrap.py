"""
Glossary bootstrap: LLM-assisted translation proposals for glossary candidates.

Supports:
1. API mode: send candidates to LLM, get proposed translations
2. Manual mode: export prompt for copy/paste, parse pasted response
"""

import json
import re
from pathlib import Path
from typing import Optional

from src.models import GlossaryTerm, Glossary
from src.utils.file_io import render_prompt
from src.style_guide_wizard import _resolve_prompt_path


def format_candidates_for_prompt(candidates: list[dict]) -> str:
    """Format candidate terms as a list for prompt inclusion.

    If a candidate dict carries a ``contexts`` key (list of
    ``(chapter_label, fragment_or_sentence)`` tuples), emit the candidate as a
    numbered header followed by indented context lines — the layout used by
    the word-mode bootstrap prompt. Otherwise fall back to the legacy
    one-line-per-candidate format used by the full-text prompt.
    """
    has_contexts = any(c.get("contexts") for c in candidates)
    if not has_contexts:
        lines = []
        for c in candidates:
            term = c.get("term", c.get("english", ""))
            term_type = c.get("type_guess", c.get("type", "unknown"))
            freq = c.get("frequency", "?")
            lines.append(f"- {term} (type guess: {term_type}, frequency: {freq})")
        return "\n".join(lines)

    lines: list[str] = []
    for i, c in enumerate(candidates, 1):
        term = c.get("term", c.get("english", ""))
        term_type = c.get("type_guess", c.get("type", "unknown"))
        freq = c.get("frequency", "?")
        lines.append(f"{i}. {term}  [{term_type} | freq={freq}]")
        contexts = c.get("contexts") or []
        if not contexts:
            lines.append("   (no in-text context found)")
        else:
            for label, snippet in contexts:
                clean = re.sub(r"\s+", " ", snippet).strip()
                lines.append(f'   {label}: "{clean}"')
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_glossary_prompt(
    candidates: list[dict],
    source_text_sample: str,
    style_guide_content: str,
    target_lang: str,
    glossary_guidance: str = "",
    context_mode: str = "full-text",
    book_title: str = "",
    context_unit_label: str = "",
) -> str:
    """Build the prompt for LLM to propose glossary translations.

    ``context_mode``:
      * ``"full-text"`` (default, today's behavior): flat candidate list +
        first 10 KB of the source text appended.
      * ``"word"``: candidates carry their own per-term context fragments
        (set by the caller via the ``contexts`` key on each candidate dict);
        no bulk source-text dump is included. ``book_title`` and
        ``context_unit_label`` are interpolated into the word-mode template.
    """
    guidance_block = (
        f"\nGLOSSARY-SPECIFIC GUIDANCE (from style questionnaire):\n{glossary_guidance}\n"
        if glossary_guidance.strip() else ""
    )

    if context_mode == "word":
        template = _resolve_prompt_path("glossary_bootstrap_word.txt").read_text(encoding="utf-8")
        variables = {
            "target_language": target_lang,
            "book_title": book_title or "the book",
            "style_guide": style_guide_content or "No style guide provided.",
            "glossary_guidance": guidance_block,
            "candidates": format_candidates_for_prompt(candidates),
            "context_unit_label": context_unit_label or "excerpts",
        }
        return render_prompt(template, variables)

    template = _resolve_prompt_path("glossary_bootstrap.txt").read_text(encoding="utf-8")
    variables = {
        "target_language": target_lang,
        "style_guide": style_guide_content or "No style guide provided.",
        "glossary_guidance": guidance_block,
        "candidates": format_candidates_for_prompt(candidates),
        "source_text": source_text_sample[:10000],
    }
    return render_prompt(template, variables)


def parse_glossary_response(response: str) -> list[dict]:
    """Parse LLM response into glossary term dicts.

    Expects a JSON array. Handles markdown code fences.
    """
    text = response.strip()
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    terms = json.loads(text)
    if not isinstance(terms, list):
        raise ValueError("Expected a JSON array of glossary terms")
    return terms


def glossary_terms_from_proposals(proposals: list[dict]) -> list[GlossaryTerm]:
    """Convert proposal dicts into GlossaryTerm model objects."""
    terms = []
    for p in proposals:
        term_type = p.get("type", "other").upper()
        # Validate type
        valid_types = {"CHARACTER", "PLACE", "CONCEPT", "TECHNICAL", "OTHER"}
        if term_type not in valid_types:
            term_type = "OTHER"
        terms.append(GlossaryTerm(
            english=p["english"],
            spanish=p["spanish"],
            type=term_type.lower(),
            context=p.get("context", ""),
            alternatives=p.get("alternatives", []),
        ))
    return terms


def proposals_to_glossary(terms: list[GlossaryTerm]) -> Glossary:
    """Create a Glossary from a list of GlossaryTerms."""
    from datetime import datetime
    return Glossary(
        terms=terms,
        version="1.0",
        updated_at=datetime.now(),
    )
