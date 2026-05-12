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
from src.text_feature_detector import (
    FeatureManifest,
    detect_all_features,
    filter_conditional_questions,
    manifest_summary,
)
from src.utils.file_io import save_style_guide, render_prompt, load_prompt_template
from src.utils.source_text import load_clean_source_text

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


def load_question_config(path: Optional[Path] = None) -> dict[str, list[dict]]:
    """Load question config as ``{"fixed": [...], "conditional": [...]}``.

    Accepts both the new dict-shaped config (``{"fixed": [...], "conditional": [...]}``)
    and the legacy flat-list format (treated as all-fixed, no conditional).
    """
    config_path = path or _resolve_prompt_path("style_guide_questions.json")
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {"fixed": data, "conditional": []}
    if not isinstance(data, dict):
        raise ValueError(
            f"Question config at {config_path} must be a list or dict, got {type(data).__name__}"
        )
    return {
        "fixed": list(data.get("fixed", [])),
        "conditional": list(data.get("conditional", [])),
    }


def load_fixed_questions(path: Optional[Path] = None) -> list[dict]:
    """Load only the fixed questions (back-compat shim)."""
    return load_question_config(path)["fixed"]


def load_conditional_questions(path: Optional[Path] = None) -> list[dict]:
    """Load only the conditional questions."""
    return load_question_config(path)["conditional"]


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
    manifest: Optional[FeatureManifest] = None,
) -> str:
    """Build the prompt for LLM to generate additional questions.

    Note: we intentionally do NOT include the heuristic feature manifest in
    this prompt. The LLM should base additional question suggestions on the
    user-answered questions plus the source text sample.
    """
    template = _resolve_prompt_path("style_guide_questions.txt").read_text(encoding="utf-8")
    answered = format_answered_questions(fixed_questions, fixed_answers)
    variables = {
        "target_language": target_lang,
        "locale": locale,
        "answered_questions": answered,
        "source_text": source_text[:15000],  # Cap at ~15K chars
    }
    return render_prompt(template, variables)


def get_active_questions(
    project_dir: Optional[Path],
    *,
    config_path: Optional[Path] = None,
    manifest: Optional[FeatureManifest] = None,
    force: bool = False,
) -> tuple[list[dict], list[dict], FeatureManifest]:
    """Return (fixed_questions, active_conditional_questions, manifest).

    Loads the question config, runs / loads the feature manifest, then filters
    the conditional questions against it.
    """
    config = load_question_config(config_path)
    if manifest is None:
        if project_dir is None:
            manifest = FeatureManifest(features={}, generated_at="")
        else:
            manifest = detect_all_features(Path(project_dir), force=force)
    active_conditional = filter_conditional_questions(config["conditional"], manifest)
    return config["fixed"], active_conditional, manifest


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

    Returns the first ``max_words`` words of the cleanest available source
    text. See ``src.utils.source_text.load_clean_source_text`` for the
    priority order (chapters → chunks → source.txt).
    """
    text, _, _ = load_clean_source_text(project_dir)
    if not text:
        return ""
    words = text.split()
    return " ".join(words[:max_words])


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
