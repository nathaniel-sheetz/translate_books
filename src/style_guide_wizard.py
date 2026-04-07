"""
Style guide wizard: generates translation style guides from questionnaire answers.

Supports three modes:
1. Fixed-only: answer hardcoded questions, generate style guide from config effects
2. LLM-assisted: fixed questions + LLM-generated questions, LLM generates style guide
3. Manual: export prompts for copy/paste into external LLM
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.models import StyleGuide
from src.utils.file_io import save_style_guide, render_prompt, load_prompt_template

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _resolve_prompt_path(name: str) -> Path:
    """Return user's copy if it exists, otherwise fall back to .example file."""
    user_path = _PROMPTS_DIR / name
    if user_path.exists():
        return user_path
    example_path = _PROMPTS_DIR / (name.rsplit(".", 1)[0] + ".example." + name.rsplit(".", 1)[1])
    if example_path.exists():
        return example_path
    raise FileNotFoundError(f"Neither {user_path} nor {example_path} found")


def load_fixed_questions(path: Optional[Path] = None) -> list[dict]:
    """Load fixed questions from config JSON."""
    config_path = path or _resolve_prompt_path("style_guide_questions.json")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def format_answered_questions(
    questions: list[dict],
    answers: dict[str, int | str],
    include_effects: bool = False,
) -> str:
    """Format questions and answers as readable text for prompt inclusion.

    Args:
        questions: list of question dicts (fixed or LLM-generated)
        answers: map of question id -> selected option index (int) or custom text (str)
        include_effects: if True, append style_guide_effect text after each answer label
    """
    lines = []
    for q in questions:
        qid = q["id"]
        if qid not in answers:
            continue
        answer = answers[qid]
        if isinstance(answer, int) and 0 <= answer < len(q["options"]):
            option = q["options"][answer]
            label = option["label"]
            effect = option.get("style_guide_effect", "") if include_effects else ""
        else:
            label = str(answer)
            effect = ""
        lines.append(f"- {q['question']} -> {label}")
        if effect:
            lines.append(f"  {effect}")
    return "\n".join(lines)


def build_question_prompt(
    source_text: str,
    target_lang: str,
    locale: str,
    fixed_questions: list[dict],
    fixed_answers: dict[str, int | str],
) -> str:
    """Build the prompt for LLM to generate additional questions."""
    template = _resolve_prompt_path("style_guide_questions.txt").read_text(encoding="utf-8")
    answered = format_answered_questions(fixed_questions, fixed_answers)
    variables = {
        "target_language": target_lang,
        "locale": locale,
        "answered_questions": answered,
        "source_text": source_text[:15000],  # Cap at ~15K chars
    }
    return render_prompt(template, variables)


def parse_llm_questions(response: str) -> list[dict]:
    """Parse LLM response into question dicts.

    Expects a JSON array. Handles responses wrapped in markdown code fences.
    """
    text = response.strip()
    # Strip markdown code fences
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    questions = json.loads(text)
    if not isinstance(questions, list):
        raise ValueError("Expected a JSON array of questions")
    # Validate structure
    for q in questions:
        if "id" not in q or "question" not in q or "options" not in q:
            raise ValueError(f"Question missing required fields: {q}")
    return questions


def build_style_guide_prompt(
    questions: list[dict],
    answers: dict[str, int | str],
    source_text: str,
    target_lang: str,
    locale: str,
) -> str:
    """Build the prompt for LLM to generate a style guide from Q&A."""
    template = _resolve_prompt_path("style_guide_generate.txt").read_text(encoding="utf-8")
    qa_text = format_answered_questions(questions, answers, include_effects=True)
    variables = {
        "target_language": target_lang,
        "locale": locale,
        "questions_and_answers": qa_text,
        "source_text": source_text[:10000],
    }
    return render_prompt(template, variables)


def parse_style_guide_response(response: str) -> str:
    """Extract style guide text from LLM response.

    Strips markdown fences if present, returns clean text.
    """
    text = response.strip()
    match = re.search(r"```(?:markdown|text)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    return text


def answers_to_style_guide_fallback(
    questions: list[dict],
    answers: dict[str, int | str],
) -> str:
    """Generate a style guide from answers WITHOUT LLM.

    Concatenates the style_guide_effect text of each selected option.
    For custom text answers, uses the custom text directly under the question's section.
    """
    sections = []
    for q in questions:
        qid = q["id"]
        if qid not in answers:
            continue
        answer = answers[qid]
        if isinstance(answer, int) and 0 <= answer < len(q["options"]):
            effect = q["options"][answer].get("style_guide_effect", "")
            if effect:
                sections.append(effect)
        elif isinstance(answer, str) and answer.strip():
            # Custom text answer — use the question id as section header
            header = qid.upper().replace("_", " ")
            sections.append(f"{header}\n{answer.strip()}")
    return "\n\n".join(sections)


def load_source_sample(project_dir: Path, max_words: int = 10000) -> str:
    """Load a source text sample from a project directory.

    Prefers processed chapter data (chunks) over raw source.txt, since chunks
    contain clean chapter text without TOC, publisher info, etc.

    Priority: chunk source_text → source.txt fallback.
    Returns first ~max_words words.
    """
    # Prefer chunks — these are post-chapter-splitting, clean source text
    chunks_dir = project_dir / "chunks"
    if chunks_dir.exists():
        chunk_files = sorted(chunks_dir.glob("*_chunk_*.json"))
        if chunk_files:
            texts = []
            word_count = 0
            for chunk_file in chunk_files:
                with open(chunk_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                src = data.get("source_text", "")
                texts.append(src)
                word_count += len(src.split())
                if word_count >= max_words:
                    break
            return "\n\n".join(texts)

    # Fallback: raw source.txt (pre-splitting, may include TOC/front matter)
    source_path = project_dir / "source.txt"
    if source_path.exists():
        text = source_path.read_text(encoding="utf-8")
        words = text.split()
        return " ".join(words[:max_words])

    return ""


def save_style_guide_json(content: str, output_path: Path) -> None:
    """Save a style guide to JSON file."""
    now = datetime.now()
    guide = StyleGuide(
        content=content,
        version="1.0",
        created_at=now,
        updated_at=now,
    )
    save_style_guide(guide, output_path)
