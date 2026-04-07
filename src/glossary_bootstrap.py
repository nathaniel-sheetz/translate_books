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
    """Format candidate terms as a simple list for prompt inclusion."""
    lines = []
    for c in candidates:
        term = c.get("term", c.get("english", ""))
        term_type = c.get("type_guess", c.get("type", "unknown"))
        freq = c.get("frequency", "?")
        lines.append(f"- {term} (type guess: {term_type}, frequency: {freq})")
    return "\n".join(lines)


def build_glossary_prompt(
    candidates: list[dict],
    source_text_sample: str,
    style_guide_content: str,
    target_lang: str,
    glossary_guidance: str = "",
) -> str:
    """Build the prompt for LLM to propose glossary translations."""
    template = _resolve_prompt_path("glossary_bootstrap.txt").read_text(encoding="utf-8")
    guidance_block = (
        f"\nGLOSSARY-SPECIFIC GUIDANCE (from style questionnaire):\n{glossary_guidance}\n"
        if glossary_guidance.strip() else ""
    )
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
