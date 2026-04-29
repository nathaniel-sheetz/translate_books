"""
Single-sentence retranslation primitive for the reader UI.

The reader exposes a "Retranslate" action on a tapped sentence. This module
takes the user-confirmed source text, the project style guide, and a
filtered glossary slice, calls the chosen LLM via ``call_llm()``, and returns
a cleaned-up replacement translation along with token/cost metadata.

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
    call_llm,
    get_default_provider,
    get_model_pricing,
    resolve_provider_for_model,
)
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

# Token estimator used across the codebase: ~4 chars/token.
_CHARS_PER_TOKEN = 4


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


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


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
        max_retries: Forwarded to ``call_llm()``.
        temperature: Sampling temperature for the LLM.
        context_text: Optional surrounding sentences (before + after the
            source span). Rendered into a ``<context>`` block the LLM is
            instructed to read but not translate. ``None`` or whitespace-only
            yields a sentinel placeholder.

    Returns:
        RetranslationResult with the cleaned translation plus token/cost
        bookkeeping.

    Raises:
        RetranslationError: If the LLM returns empty output after a retry.
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
                provider = resolve_provider_for_model(model)
            except ValueError:
                provider = get_default_provider()
        else:
            provider = get_default_provider()

    raw = call_llm(
        prompt,
        provider=provider,
        model=model,
        temperature=temperature,
        max_retries=max_retries,
        call_type="retranslate_sentence",
    )

    cleaned = _strip_markdown_fences(raw)
    if not cleaned:
        logger.warning("Retranslate produced empty output; retrying with stricter suffix.")
        retry_prompt = prompt + (
            "\n\nYour previous response was empty or unusable. "
            f"Respond with ONLY the revised {target_language} translation as "
            "plain text."
        )
        raw = call_llm(
            retry_prompt,
            provider=provider,
            model=model,
            temperature=temperature,
            max_retries=1,
            call_type="retranslate_sentence",
        )
        cleaned = _strip_markdown_fences(raw)
        if not cleaned:
            raise RetranslationError(
                "LLM returned empty output for retranslation after retry."
            )

    resolved_model = model or "default"
    prompt_tokens = _estimate_tokens(prompt)
    completion_tokens = _estimate_tokens(raw)
    pricing = get_model_pricing(provider, resolved_model)
    cost_usd = (
        (prompt_tokens / 1_000_000) * pricing.get("input", 0.0)
        + (completion_tokens / 1_000_000) * pricing.get("output", 0.0)
    )

    return RetranslationResult(
        new_translation=cleaned,
        model=resolved_model,
        provider=provider,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=round(cost_usd, 6),
        raw_response=raw,
    )
