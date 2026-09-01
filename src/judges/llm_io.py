"""
Shared plumbing for tailored LLM judges.

Generalizes the reusable, schema-agnostic pieces that ``src/judge.py`` keeps
private (template load/render/hash, JSON extraction, coded-signal formatting)
so a new judge is mostly a prompt template plus a small spec — not new
boilerplate.

All LLM calls go through ``call_llm()`` in ``src/api_translator.py`` — never
import the Anthropic/OpenAI SDK directly. That gives retry logic, prompt
logging, and provider abstraction for free.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from src.api_translator import (
    call_llm,
    get_default_provider,
    get_model_pricing,
    resolve_provider_for_model,
)
from src.models import EvalResult

logger = logging.getLogger(__name__)

# Prompt templates live at the repo root: src/judges/llm_io.py -> ../../prompts
_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"

# Token estimator used across the codebase: ~4 chars/token.
_CHARS_PER_TOKEN = 4


class JudgeParseError(Exception):
    """Raised when a judge response cannot be parsed into the expected schema."""


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def load_template(name: str) -> str:
    """Load a prompt template by filename from the ``prompts/`` directory."""
    resolved = (_PROMPTS_DIR / name).resolve()
    if not resolved.is_relative_to(_PROMPTS_DIR.resolve()):
        raise ValueError(f"Template path escapes prompts directory: {name!r}")
    return resolved.read_text(encoding="utf-8")


def prompt_version(name: str) -> str:
    """Return the SHA-256 hex digest of a prompt template (reproducibility lock)."""
    return hashlib.sha256(load_template(name).encode("utf-8")).hexdigest()


def render(template: str, variables: dict[str, str]) -> str:
    """Render a template with ``{{double-brace}}`` substitution.

    Unlike ``utils.file_io.render_prompt`` this does not raise on leftover
    placeholders — judge templates may legitimately carry optional blocks that
    a given judge does not fill.
    """
    result = template
    for key, value in variables.items():
        result = result.replace("{{" + key + "}}", str(value))
    return result


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

def extract_json(text: str) -> str:
    """Extract the first complete JSON object/array from an LLM response.

    Handles pure JSON, fenced code blocks, and JSON surrounded by commentary.
    Uses a real decoder (``raw_decode``) so nested braces are handled — the
    regex approach in ``src/judge.py`` only worked for flat objects.

    Each candidate is decoded at its *first* opener only, never at a later one:
    see the comment in the loop for why falling through matters.
    """
    stripped = text.strip()

    # Prefer the contents of a ```json ... ``` fence if present.
    fence = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", stripped, re.DOTALL)
    candidates: list[str] = []
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(stripped)

    decoder = json.JSONDecoder()
    for candidate in candidates:
        # Decode at the FIRST opener only. Trying '[' after '{' has already
        # failed reaches *inside* the broken object: a draft whose
        # "recommendation" string carries an unescaped quote then yields its
        # nested "evidence" array, and parse_judge_json reports "got list"
        # instead of the real syntax error and its line and column.
        positions = [i for i in (candidate.find("{"), candidate.find("[")) if i != -1]
        if not positions:
            continue
        idx = min(positions)
        try:
            _, end = decoder.raw_decode(candidate[idx:])
        except json.JSONDecodeError:
            continue
        return candidate[idx: idx + end]

    return stripped


def parse_judge_json(raw: str, required_fields: Iterable[str]) -> dict[str, Any]:
    """Parse a judge response into a dict, validating required top-level keys.

    Args:
        raw: Raw LLM response text.
        required_fields: Keys that must be present in the parsed object.

    Returns:
        The parsed JSON object.

    Raises:
        JudgeParseError: If the text is not valid JSON, is not an object, or is
            missing a required field.
    """
    json_str = extract_json(raw)
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise JudgeParseError(f"Invalid JSON from judge: {exc}") from exc

    if not isinstance(data, dict):
        raise JudgeParseError(
            f"Expected a JSON object from judge, got {type(data).__name__}"
        )

    missing = set(required_fields) - set(data.keys())
    if missing:
        raise JudgeParseError(f"Missing fields in judge response: {sorted(missing)}")

    return data


# ---------------------------------------------------------------------------
# Coded-signal formatter (shared with src/judge.py's contract)
# ---------------------------------------------------------------------------

def format_signals_for_judge(eval_results: Optional[list[EvalResult]]) -> str:
    """Format coded-evaluator output as natural text for a judge prompt.

    Groups issues by evaluator name with counts. Returns ``'None flagged.'``
    when there are no issues.
    """
    lines: list[str] = []
    for result in eval_results or []:
        if result.issues:
            msgs = "; ".join(issue.message for issue in result.issues)
            lines.append(f"- {result.eval_name} ({len(result.issues)}): {msgs}")
    return "\n".join(lines) if lines else "None flagged."


# ---------------------------------------------------------------------------
# LLM dispatch + cost
# ---------------------------------------------------------------------------

def resolve_provider(provider: Optional[str], model: Optional[str]) -> str:
    """Resolve a provider id from an explicit value, the model, or the default."""
    if provider:
        return provider
    if model:
        try:
            return resolve_provider_for_model(model)
        except ValueError:
            pass
    return get_default_provider()


def call_judge(
    prompt: str,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    call_type: str,
    max_retries: int = 3,
    temperature: float = 0.0,
    cache_prefix: Optional[str] = None,
) -> str:
    """Call the judge LLM at ``temperature`` (0 by default for reproducibility).

    Retries once with a stricter "JSON only" suffix is the caller's job — this
    just dispatches through ``call_llm`` so prompt logging + provider retry are
    handled centrally.

    ``cache_prefix`` is the target-independent head of ``prompt`` (a judge's
    rubric + per-book rules, i.e. the ``build_prompt_parts`` prefix). Passing it
    lets Anthropic's prompt cache serve it on every call after the first, so a
    suite pays for the rubric once per run instead of once per target. Empty is
    normalized to ``None`` — the same convention ``translate_chunk_realtime``
    uses — and no validation happens here: ``call_anthropic_api`` already falls
    back to single-block content when the prefix doesn't match, and the
    OpenAI-compatible path ignores it outright.
    """
    resolved_provider = resolve_provider(provider, model)
    return call_llm(
        prompt,
        provider=resolved_provider,
        model=model,
        temperature=temperature,
        max_retries=max_retries,
        call_type=call_type,
        cache_prefix=cache_prefix or None,
    )


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token), floored at 1."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def estimate_call_cost(
    prompt: str,
    *,
    provider: Optional[str],
    model: Optional[str],
    completion_tokens: int = 600,
) -> float:
    """Estimate the USD cost of one judge call from prompt size + a completion guess."""
    resolved_provider = resolve_provider(provider, model)
    pricing = get_model_pricing(resolved_provider, model or "default")
    prompt_tokens = estimate_tokens(prompt)
    return (
        (prompt_tokens / 1_000_000) * pricing.get("input", 0.0)
        + (completion_tokens / 1_000_000) * pricing.get("output", 0.0)
    )
