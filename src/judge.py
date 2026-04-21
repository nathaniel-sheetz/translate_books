"""
Core LLM-judge primitive for translation evaluation.

Provides two evaluation modes:
- **Absolute**: Score a single translation on a 1-5 rubric (Fluency, Fidelity,
  Regional, Voice) and normalize to [0.0, 1.0].
- **Pairwise**: Compare two translations of the same source and pick a winner
  per dimension.

All LLM calls go through ``call_llm()`` in ``src/api_translator.py`` — never
import the Anthropic/OpenAI SDK directly.  This gives us retry logic, prompt
logging, and provider abstraction for free.
"""

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Literal, Optional

from src.api_translator import call_llm, get_default_provider
from src.models import EvalResult, JudgeScore, PairwiseVerdict

JudgeContextMode = Literal["style", "full_prompt"]

logger = logging.getLogger(__name__)

# Prompt template directory (resolved once)
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Token budget for voice context (approx 4 chars/token)
_VOICE_TOKEN_LIMIT = 4000
_VOICE_CHAR_LIMIT = _VOICE_TOKEN_LIMIT * 4  # rough estimate


class JudgeParseError(Exception):
    """Raised when the judge response cannot be parsed into the expected schema."""
    pass


# ---------------------------------------------------------------------------
# Voice context helpers
# ---------------------------------------------------------------------------

def _load_voice_context(style_json_path: Optional[Path]) -> tuple[Optional[str], bool]:
    """Load voice context from style.json.

    Returns:
        (voice_text, has_voice) — voice_text is None when style.json is
        missing or content is empty.  has_voice is True when a usable
        voice context was found.
    """
    if style_json_path is None or not style_json_path.exists():
        return None, False

    try:
        data = json.loads(style_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read style.json at %s: %s", style_json_path, exc)
        return None, False

    content = data.get("content", "")
    if not content or not content.strip():
        return None, False

    # Truncate if over token budget
    if len(content) > _VOICE_CHAR_LIMIT:
        logger.warning(
            "Voice context is %d chars (>%d limit), truncating.",
            len(content),
            _VOICE_CHAR_LIMIT,
        )
        content = content[:_VOICE_CHAR_LIMIT] + "\n[...truncated]"

    return content, True


# ---------------------------------------------------------------------------
# Coded-signal formatter (F7)
# ---------------------------------------------------------------------------

def format_signals_for_judge(eval_results: list[EvalResult]) -> str:
    """Format coded evaluator output for the judge prompt.

    Groups issues by evaluator name with counts.
    Returns ``'None flagged.'`` if no issues across all evaluators.
    """
    lines: list[str] = []
    for result in eval_results:
        if result.issues:
            msgs = "; ".join(issue.message for issue in result.issues)
            lines.append(f"- {result.eval_name} ({len(result.issues)}): {msgs}")

    if not lines:
        return "None flagged."

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

def _load_template(name: str) -> str:
    """Load a prompt template by filename from the prompts/ directory."""
    path = _PROMPTS_DIR / name
    return path.read_text(encoding="utf-8")


def _prompt_hash(template_text: str) -> str:
    """SHA-256 hex digest of a prompt template (for reproducibility lock)."""
    return hashlib.sha256(template_text.encode("utf-8")).hexdigest()


def _render(template: str, variables: dict[str, str]) -> str:
    """Render a prompt template with double-brace variable substitution."""
    result = template
    for key, value in variables.items():
        result = result.replace("{{" + key + "}}", value)
    return result


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> str:
    """Extract a JSON object from the LLM response.

    Handles responses that are:
    - Pure JSON
    - Wrapped in markdown code fences
    - Preceded/followed by commentary
    """
    # Try to find JSON inside code fences first
    fence_match = re.search(r"```(?:json)?\s*\n?(\{.*?\})\s*\n?```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1)

    # Try to find a bare JSON object
    brace_match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if brace_match:
        return brace_match.group(0)

    # Fall back to the whole text
    return text.strip()


def _parse_judge_json(raw: str, mode: str) -> dict:
    """Parse judge response JSON, raising JudgeParseError on failure.

    Args:
        raw: Raw LLM response text.
        mode: ``'absolute'`` or ``'pairwise'``.

    Returns:
        Parsed dict ready for Pydantic model construction.

    Raises:
        JudgeParseError: If JSON is invalid or required fields are missing.
    """
    json_str = _extract_json(raw)
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise JudgeParseError(f"Invalid JSON from judge: {exc}") from exc

    if mode == "absolute":
        required = {"fluency", "fidelity", "regional", "rationale"}
    else:
        required = {
            "fluency_winner", "fidelity_winner", "regional_winner",
            "voice_winner", "overall_winner", "rationale",
        }

    missing = required - set(data.keys())
    if missing:
        raise JudgeParseError(f"Missing fields in judge response: {missing}")

    # Clamp absolute scores to [1, 5]
    if mode == "absolute":
        for dim in ("fluency", "fidelity", "regional", "voice"):
            if dim in data and data[dim] is not None:
                try:
                    val = int(data[dim])
                except (TypeError, ValueError):
                    raise JudgeParseError(
                        f"Non-integer value for {dim}: {data[dim]!r}"
                    )
                if val < 1 or val > 5:
                    logger.warning("Clamping %s=%d to [1,5]", dim, val)
                    val = max(1, min(5, val))
                data[dim] = val

    return data


# ---------------------------------------------------------------------------
# Public API — absolute scoring
# ---------------------------------------------------------------------------

def judge_absolute(
    source_text: str,
    translation_text: str,
    *,
    style_json_path: Optional[Path] = None,
    coded_eval_results: Optional[list[EvalResult]] = None,
    judge_provider: Optional[str] = None,
    judge_model: Optional[str] = None,
    max_retries: int = 3,
    judge_context_mode: JudgeContextMode = "style",
    translator_context: Optional[str] = None,
) -> JudgeScore:
    """Score a single translation on the four-dimension rubric.

    Args:
        source_text: Original English text.
        translation_text: Spanish translation.
        style_json_path: Path to the project's style.json (for voice context).
            Ignored when ``judge_context_mode="full_prompt"``.
        coded_eval_results: Results from coded evaluators to feed the judge.
        judge_provider: LLM provider override (default from config).
        judge_model: LLM model override (default from config).
        max_retries: Retry budget for the LLM call.
        judge_context_mode: ``"style"`` (default) uses style.json content as
            voice context; ``"full_prompt"`` passes the full translator prompt
            (glossary, style guide, instructions) as the context block.
        translator_context: Required when ``judge_context_mode="full_prompt"``.
            The rendered translator prompt for this chunk.

    Returns:
        JudgeScore with per-dimension scores and normalized_score.

    Raises:
        JudgeParseError: If the judge response cannot be parsed after retry.
        ValueError: If ``judge_context_mode="full_prompt"`` but no
            ``translator_context`` was supplied.
    """
    coded_signals = format_signals_for_judge(coded_eval_results or [])

    if judge_context_mode == "full_prompt":
        if not translator_context:
            raise ValueError(
                "judge_context_mode='full_prompt' requires translator_context"
            )
        template_name = "judge_absolute_full_context.txt"
        variables = {
            "source_text": source_text,
            "translation_text": translation_text,
            "translator_context": translator_context,
            "coded_signals": coded_signals,
        }
    else:
        voice_context, has_voice = _load_voice_context(style_json_path)
        if has_voice:
            template_name = "judge_absolute.txt"
            variables = {
                "source_text": source_text,
                "translation_text": translation_text,
                "voice_context": voice_context,
                "coded_signals": coded_signals,
            }
        else:
            template_name = "judge_absolute_no_voice.txt"
            variables = {
                "source_text": source_text,
                "translation_text": translation_text,
                "coded_signals": coded_signals,
            }

    template = _load_template(template_name)
    prompt = _render(template, variables)
    provider = judge_provider or get_default_provider()

    raw = call_llm(
        prompt,
        provider=provider,
        model=judge_model,
        temperature=0.0,
        max_retries=max_retries,
        call_type="judge_absolute",
    )

    try:
        data = _parse_judge_json(raw, mode="absolute")
    except JudgeParseError:
        # Retry once with a stricter suffix
        logger.warning("Judge parse failed, retrying with stricter prompt.")
        retry_prompt = prompt + (
            "\n\nYour previous response was not valid JSON. "
            "Respond with ONLY a JSON object, no other text."
        )
        raw = call_llm(
            retry_prompt,
            provider=provider,
            model=judge_model,
            temperature=0.0,
            max_retries=1,
            call_type="judge_absolute",
        )
        data = _parse_judge_json(raw, mode="absolute")

    # Build JudgeScore — voice may be None for no-voice variant
    return JudgeScore(
        fluency=data["fluency"],
        fidelity=data["fidelity"],
        regional=data["regional"],
        voice=data.get("voice"),
        rationale=data["rationale"],
        raw_response=raw,
    )


# ---------------------------------------------------------------------------
# Public API — pairwise comparison
# ---------------------------------------------------------------------------

def judge_pairwise(
    source_text: str,
    translation_a: str,
    translation_b: str,
    *,
    style_json_path: Optional[Path] = None,
    coded_eval_results_a: Optional[list[EvalResult]] = None,
    coded_eval_results_b: Optional[list[EvalResult]] = None,
    judge_provider: Optional[str] = None,
    judge_model: Optional[str] = None,
    max_retries: int = 3,
    judge_context_mode: JudgeContextMode = "style",
    translator_context: Optional[str] = None,
) -> PairwiseVerdict:
    """Compare two translations and return a per-dimension verdict.

    Args:
        source_text: Original English text.
        translation_a: First Spanish translation (position A).
        translation_b: Second Spanish translation (position B).
        style_json_path: Path to style.json for voice context. Ignored when
            ``judge_context_mode="full_prompt"``.
        coded_eval_results_a: Coded evaluator results for translation A.
        coded_eval_results_b: Coded evaluator results for translation B.
        judge_provider: LLM provider override.
        judge_model: LLM model override.
        max_retries: Retry budget for the LLM call.
        judge_context_mode: ``"style"`` (default) uses style.json content as
            voice context; ``"full_prompt"`` passes the full translator prompt
            (glossary, style guide, instructions) as the context block.
        translator_context: Required when ``judge_context_mode="full_prompt"``.
            The rendered translator prompt for this chunk (both translations
            received the same one).

    Returns:
        PairwiseVerdict with per-dimension winners and rationale.

    Raises:
        JudgeParseError: If the judge response cannot be parsed after retry.
        ValueError: If ``judge_context_mode="full_prompt"`` but no
            ``translator_context`` was supplied.
    """
    signals_a = format_signals_for_judge(coded_eval_results_a or [])
    signals_b = format_signals_for_judge(coded_eval_results_b or [])

    if judge_context_mode == "full_prompt":
        if not translator_context:
            raise ValueError(
                "judge_context_mode='full_prompt' requires translator_context"
            )
        template_name = "judge_pairwise_full_context.txt"
        variables = {
            "source_text": source_text,
            "translation_a": translation_a,
            "translation_b": translation_b,
            "translator_context": translator_context,
            "coded_signals_a": signals_a,
            "coded_signals_b": signals_b,
        }
    else:
        voice_context, has_voice = _load_voice_context(style_json_path)
        if has_voice:
            template_name = "judge_pairwise.txt"
            variables = {
                "source_text": source_text,
                "translation_a": translation_a,
                "translation_b": translation_b,
                "voice_context": voice_context,
                "coded_signals_a": signals_a,
                "coded_signals_b": signals_b,
            }
        else:
            template_name = "judge_pairwise_no_voice.txt"
            variables = {
                "source_text": source_text,
                "translation_a": translation_a,
                "translation_b": translation_b,
                "coded_signals_a": signals_a,
                "coded_signals_b": signals_b,
            }

    template = _load_template(template_name)
    prompt = _render(template, variables)
    provider = judge_provider or get_default_provider()

    raw = call_llm(
        prompt,
        provider=provider,
        model=judge_model,
        temperature=0.0,
        max_retries=max_retries,
        call_type="judge_pairwise",
    )

    try:
        data = _parse_judge_json(raw, mode="pairwise")
    except JudgeParseError:
        logger.warning("Judge parse failed, retrying with stricter prompt.")
        retry_prompt = prompt + (
            "\n\nYour previous response was not valid JSON. "
            "Respond with ONLY a JSON object, no other text."
        )
        raw = call_llm(
            retry_prompt,
            provider=provider,
            model=judge_model,
            temperature=0.0,
            max_retries=1,
            call_type="judge_pairwise",
        )
        data = _parse_judge_json(raw, mode="pairwise")

    return PairwiseVerdict(
        fluency_winner=data["fluency_winner"],
        fidelity_winner=data["fidelity_winner"],
        regional_winner=data["regional_winner"],
        voice_winner=data["voice_winner"],
        overall_winner=data["overall_winner"],
        rationale=data["rationale"],
        raw_response=raw,
    )


# ---------------------------------------------------------------------------
# Prompt version helper (for reproducibility lock C7)
# ---------------------------------------------------------------------------

def get_prompt_version(template_name: str) -> str:
    """Return sha256 hash of a prompt template file for reproducibility."""
    template = _load_template(template_name)
    return _prompt_hash(template)
