"""
Single-sentence retranslation primitive for the reader UI.

The reader exposes a "Retranslate" action on a tapped sentence. This module
takes the user-confirmed source text, the project style guide, and a
filtered glossary slice, sends the prompt to the chosen model via
``llm_mechanisms.complete()`` (a metered API, a subscription CLI or the local
server, whichever the model's provider is), and returns a cleaned-up
replacement translation along with token/cost metadata.

Stays intentionally narrow: no judging, no batch mode, no chunk-level work.
That is all reachable from the same web endpoints if/when needed.

See ``docs/READER_RETRANSLATE.md`` for the user flow, prompt-size budget,
and the persistence/concurrency model.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from src.api_translator import (
    get_default_provider,
    resolve_provider_for_model,
)
from src.llm_mechanisms import complete
from src.models import Glossary, RetranslationResult
from src.utils.file_io import (
    filter_glossary_for_chunk,
    format_glossary_for_prompt,
    render_prompt,
)

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_TEMPLATE_NAME = "retranslate_sentence.txt"

# Hard cap on style-guide chars sent to the LLM. Mirrors src/judge.py's
# voice context limit so a runaway style.json can't blow up the prompt.
_STYLE_TOKEN_LIMIT = 4000
_STYLE_CHAR_LIMIT = _STYLE_TOKEN_LIMIT * 4

# A person is waiting on one sentence. The launcher's own ceilings (15 and 30
# minutes) are sized for a chunk of prose.
_HEADLESS_TIMEOUT_S = 180.0


class RetranslationError(Exception):
    """Raised when the LLM produces an unusable retranslation after retries."""


def _load_template() -> str:
    path = _PROMPTS_DIR / _TEMPLATE_NAME
    return path.read_text(encoding="utf-8")


def _load_style_guide_content(style_json_path: Optional[Path]) -> str:
    """Read style.json's style guide for retranslation.

    Prefers ``light_content`` (a user-curated short guide) when set; falls back
    to the full ``content`` field. Returns "" if missing/empty. Truncates to
    _STYLE_CHAR_LIMIT with a marker if oversized.
    """
    if style_json_path is None or not style_json_path.exists():
        return ""
    try:
        data = json.loads(style_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read style.json at %s: %s", style_json_path, exc)
        return ""
    light = (data.get("light_content") or "").strip()
    if light:
        content = light
        variant = "light_content"
    else:
        content = (data.get("content") or "").strip()
        variant = "content"
    if not content:
        return ""
    logger.info("Retranslate using style guide variant=%s (%d chars)", variant, len(content))
    if len(content) > _STYLE_CHAR_LIMIT:
        logger.warning(
            "Style guide is %d chars (>%d limit); truncating.",
            len(content), _STYLE_CHAR_LIMIT,
        )
        content = content[:_STYLE_CHAR_LIMIT] + "\n[...truncated]"
    return content


def _strip_markdown_fences(text: str) -> str:
    """Strip leading/trailing ``` fences and surrounding whitespace.

    Models occasionally wrap a single-sentence answer in fences despite
    instructions. Also strips matching surrounding quotes.
    """
    s = text.strip()
    fence = re.match(r"^```(?:\w+)?\s*\n?(.*?)\n?```$", s, flags=re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    # Strip a single pair of surrounding double or single quotes
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'", "“", "”"):
        s = s[1:-1].strip()
    return s


def _build_prompt(
    *,
    source_text: str,
    source_language: str,
    target_language: str,
    style_guide_content: str,
    glossary: Optional[Glossary],
    context_text: Optional[str] = None,
) -> str:
    template = _load_template()
    if glossary is not None:
        filtered = filter_glossary_for_chunk(glossary, source_text)
        glossary_str = format_glossary_for_prompt(filtered)
    else:
        glossary_str = "No glossary terms specified."

    context_str = (context_text or "").strip() or "(no surrounding context provided)"

    return render_prompt(template, {
        "source_language": source_language,
        "target_language": target_language,
        "style_guide": style_guide_content or "(no style guide configured)",
        "glossary": glossary_str,
        "context": context_str,
        "source_text": source_text,
    })


def retranslate_sentence(
    source_text: str,
    *,
    style_json_path: Optional[Path] = None,
    glossary: Optional[Glossary] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    source_language: str = "English",
    target_language: str = "Spanish",
    max_retries: int = 3,
    temperature: float = 0.3,
    context_text: Optional[str] = None,
    project_dir: Optional[Path] = None,
) -> RetranslationResult:
    """Produce a fresh translation for a single user-confirmed source span.

    Args:
        source_text: The exact text to translate. Whatever the user
            confirmed in the reader's source textarea is what the LLM sees.
        style_json_path: Path to the project's style.json. Optional.
        glossary: Project glossary; will be filtered to terms appearing in
            ``source_text`` before being included in the prompt.
        model: Model id (e.g. "claude-sonnet-4-6"). Defaults to the system
            default from llm_config.json.
        provider: Provider id. If omitted, resolved from ``model``.
        source_language: Display name only (used in the prompt template).
        target_language: Display name only.
        max_retries: Forwarded to ``call_llm()`` on the API path.
        temperature: Sampling temperature, where the mechanism takes one.
        context_text: Optional surrounding sentences (before + after the
            source span). Rendered into a ``<context>`` block the LLM is
            instructed to read but not translate. ``None`` or whitespace-only
            yields a sentinel placeholder.
        project_dir: The book's directory. A headless call logs its usage
            row under ``.harness/retranslate/`` there; without it the row is
            not kept.

    Returns:
        RetranslationResult with the cleaned translation plus token/cost
        bookkeeping.

    Raises:
        RetranslationError: If the LLM returns empty output after a retry.
        MechanismError: If a headless or local call fails
            (``MechanismUnavailable`` when a local model cannot be asked).
    """
    if not source_text or not source_text.strip():
        raise ValueError("source_text must be non-empty")

    style_guide_content = _load_style_guide_content(style_json_path)
    prompt = _build_prompt(
        source_text=source_text,
        source_language=source_language,
        target_language=target_language,
        style_guide_content=style_guide_content,
        glossary=glossary,
        context_text=context_text,
    )

    if provider is None:
        if model is not None:
            try:
                provider = resolve_provider_for_model(model, api_only=False)
            except ValueError:
                provider = get_default_provider()
        else:
            provider = get_default_provider()

    usage_log = None
    if project_dir is not None:
        usage_log = Path(project_dir) / ".harness" / "retranslate" / "usage.jsonl"

    def ask(text: str, retries: int):
        return complete(
            text,
            provider=provider,
            model=model,
            temperature=temperature,
            max_retries=retries,
            call_type="retranslate_sentence",
            usage_log=usage_log,
            project_slug=Path(project_dir).name if project_dir is not None else None,
            headless_timeout_s=_HEADLESS_TIMEOUT_S,
        )

    answers = [ask(prompt, max_retries)]
    cleaned = _strip_markdown_fences(answers[-1].text)
    if not cleaned:
        logger.warning("Retranslate produced empty output; retrying with stricter suffix.")
        retry_prompt = prompt + (
            "\n\nYour previous response was empty or unusable. "
            f"Respond with ONLY the revised {target_language} translation as "
            "plain text."
        )
        answers.append(ask(retry_prompt, 1))
        cleaned = _strip_markdown_fences(answers[-1].text)
        if not cleaned:
            raise RetranslationError(
                "LLM returned empty output for retranslation after retry."
            )

    last = answers[-1]
    return RetranslationResult(
        new_translation=cleaned,
        model=last.model,
        provider=provider,
        # Both calls were paid for when the first came back unusable.
        prompt_tokens=sum(a.prompt_tokens for a in answers),
        completion_tokens=sum(a.completion_tokens for a in answers),
        cost_usd=round(sum(a.cost_usd for a in answers), 6),
        raw_response=last.text,
        mechanism=last.mechanism,
    )
