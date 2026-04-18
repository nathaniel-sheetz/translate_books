"""
API-based translation using Anthropic Claude, OpenAI GPT, or any
OpenAI-compatible provider (DeepInfra, Together, Groq, etc.).

This module provides functions to translate chunks using AI APIs in both
real-time and batch modes.  Provider and model configuration is loaded from
``llm_config.json`` at the project root.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from src.models import Chunk, ChunkStatus, Glossary, StyleGuide
from src.utils.file_io import render_prompt, load_prompt_template, format_glossary_for_prompt, filter_glossary_for_chunk
from src.utils.prompt_logger import log_prompt

# Load environment variables from .env file
load_dotenv()

# Provider is now a plain string (validated against llm_config.json)
Provider = str

# ---------------------------------------------------------------------------
# LLM config loading
# ---------------------------------------------------------------------------

_LLM_CONFIG_CACHE: dict | None = None

_FALLBACK_CONFIG = {
    "default_provider": "anthropic",
    "default_model": "claude-sonnet-4-20250514",
    "providers": [
        {
            "id": "anthropic",
            "name": "Anthropic (Claude)",
            "type": "anthropic",
            "api_key_env_var": "ANTHROPIC_API_KEY",
            "models": [
                {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4", "pricing": {"input": 3.00, "output": 15.00}},
                {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5", "pricing": {"input": 1.00, "output": 5.00}},
            ],
        },
        {
            "id": "openai",
            "name": "OpenAI (GPT)",
            "type": "openai-compatible",
            "api_key_env_var": "OPENAI_API_KEY",
            "base_url": None,
            "models": [
                {"id": "gpt-4o", "name": "GPT-4o", "pricing": {"input": 2.50, "output": 10.00}},
                {"id": "gpt-4o-mini", "name": "GPT-4o Mini", "pricing": {"input": 0.15, "output": 0.60}},
            ],
        },
    ],
}


def load_llm_config(*, force_reload: bool = False) -> dict:
    """Load LLM provider/model configuration from ``llm_config.json``."""
    global _LLM_CONFIG_CACHE
    if _LLM_CONFIG_CACHE is not None and not force_reload:
        return _LLM_CONFIG_CACHE

    config_path = Path(__file__).resolve().parent.parent / "llm_config.json"
    if config_path.exists():
        _LLM_CONFIG_CACHE = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        _LLM_CONFIG_CACHE = _FALLBACK_CONFIG
    return _LLM_CONFIG_CACHE


def get_provider_config(provider_id: str) -> dict:
    """Return the config dict for a specific provider, or raise ValueError."""
    config = load_llm_config()
    for p in config["providers"]:
        if p["id"] == provider_id:
            return p
    raise ValueError(f"Unknown provider '{provider_id}'. Check llm_config.json.")


def get_default_model() -> str:
    return load_llm_config().get("default_model", "claude-sonnet-4-20250514")


def get_default_provider() -> str:
    return load_llm_config().get("default_provider", "anthropic")


def get_model_pricing(provider_id: str, model_id: str) -> dict:
    """Return ``{"input": ..., "output": ...}`` for the given provider/model."""
    try:
        pconfig = get_provider_config(provider_id)
    except ValueError:
        return {"input": 5.00, "output": 15.00}
    for m in pconfig.get("models", []):
        if m["id"] == model_id:
            return m.get("pricing", {"input": 5.00, "output": 15.00})
    return {"input": 5.00, "output": 15.00}


def get_pricing_table() -> dict:
    """Build and return a ``PRICING_TABLE``-shaped dict from config."""
    config = load_llm_config()
    table: dict = {}
    for p in config["providers"]:
        table[p["id"]] = {}
        for m in p.get("models", []):
            table[p["id"]][m["id"]] = m.get("pricing", {"input": 5.00, "output": 15.00})
    return table


# Keep module-level constant for backward compat in scripts that import it
DEFAULT_MODEL = "claude-sonnet-4-20250514"


class APIError(Exception):
    """Base exception for API-related errors."""
    pass


class APIKeyError(APIError):
    """Raised when API key is missing or invalid."""
    pass


class RateLimitError(APIError):
    """Raised when API rate limit is exceeded."""
    pass


class CostLimitError(APIError):
    """Raised when cost limit is exceeded."""
    pass


def get_api_key(provider: Provider) -> str:
    """Get API key for *provider* from environment variables.

    The env-var name is read from ``llm_config.json`` (field
    ``api_key_env_var``).  Falls back to ``<PROVIDER>_API_KEY``.
    """
    try:
        pconfig = get_provider_config(provider)
        key_var = pconfig.get("api_key_env_var", f"{provider.upper()}_API_KEY")
    except ValueError:
        key_var = f"{provider.upper()}_API_KEY"

    api_key = os.getenv(key_var)
    if not api_key:
        raise APIKeyError(
            f"{key_var} not found in environment. "
            f"Please set it in your .env file or environment variables."
        )
    return api_key


def estimate_cost(
    chunks: list[Chunk],
    provider: Provider,
    model: str,
    batch_mode: bool = False,
    glossary: Optional[Glossary] = None,
    style_guide: Optional[StyleGuide] = None,
) -> dict:
    """
    Estimate the cost of translating chunks with the specified provider and model.

    Args:
        chunks: List of chunks to translate
        provider: API provider
        model: Model identifier
        batch_mode: Whether batch API will be used (50% discount)
        glossary: Optional glossary (affects prompt length)
        style_guide: Optional style guide (affects prompt length)

    Returns:
        Dictionary with cost breakdown:
        {
            "input_tokens": int,
            "output_tokens_estimate": int,
            "cost_usd": float,
            "cost_per_chunk_usd": float
        }
    """
    # Load template to estimate prompt size
    template = load_prompt_template()

    # Estimate tokens per chunk
    total_input_tokens = 0
    total_output_tokens_estimate = 0

    for chunk in chunks:
        # Filter glossary to terms relevant to this chunk
        chunk_glossary = filter_glossary_for_chunk(glossary, chunk.source_text) if glossary else None

        # Build prompt variables
        variables = {
            "book_title": "Sample Book",
            "source_text": chunk.source_text,
            "target_language": "Spanish",
            "source_language": "English",
            "glossary": format_glossary_for_prompt(chunk_glossary) if chunk_glossary else "No glossary provided.",
            "style_guide": style_guide.content if style_guide else "No style guide provided.",
            "context": "",
            "chapter_info": f"Chapter {chunk.chapter_id}, Chunk {chunk.position}",
            "previous_chapter_context": "",
        }

        # Render prompt
        prompt = render_prompt(template, variables)

        # Rough token estimate: ~4 characters per token
        input_tokens = len(prompt) // 4
        # Output roughly same size as source text
        output_tokens = len(chunk.source_text) // 4

        total_input_tokens += input_tokens
        total_output_tokens_estimate += output_tokens

    # Get pricing for this model
    model_pricing = get_model_pricing(provider, model)

    # Calculate cost
    input_cost = (total_input_tokens / 1_000_000) * model_pricing["input"]
    output_cost = (total_output_tokens_estimate / 1_000_000) * model_pricing["output"]
    total_cost = input_cost + output_cost

    # Apply batch discount (50% off)
    if batch_mode:
        total_cost *= 0.5

    return {
        "input_tokens": total_input_tokens,
        "output_tokens_estimate": total_output_tokens_estimate,
        "cost_usd": round(total_cost, 4),
        "cost_per_chunk_usd": round(total_cost / len(chunks), 4) if chunks else 0,
        "batch_discount_applied": batch_mode
    }


def call_anthropic_api(
    prompt: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
    temperature: float = 0.3,
    api_key: str | None = None,
) -> str:
    """Call the Anthropic Claude API and return the text response."""
    try:
        import anthropic
    except ImportError:
        raise APIError(
            "anthropic package not installed. "
            "Install it with: pip install anthropic"
        )

    if api_key is None:
        api_key = get_api_key("anthropic")
    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        # Extract text from response
        if response.content and len(response.content) > 0:
            return response.content[0].text
        else:
            raise APIError("Empty response from Claude API")

    except anthropic.RateLimitError as e:
        raise RateLimitError(f"Claude API rate limit exceeded: {e}")
    except anthropic.AuthenticationError as e:
        raise APIKeyError(f"Invalid Anthropic API key: {e}")
    except anthropic.APIError as e:
        raise APIError(f"Claude API error: {e}")


def call_openai_api(
    prompt: str,
    model: str = "gpt-4o",
    max_tokens: int = 4096,
    temperature: float = 0.3,
    api_key: str | None = None,
    base_url: str | None = None,
) -> str:
    """Call an OpenAI-compatible API and return the text response.

    Works with native OpenAI, DeepInfra, Together, Groq, or any endpoint
    that speaks the OpenAI chat-completions protocol.
    """
    try:
        import openai
    except ImportError:
        raise APIError(
            "openai package not installed. "
            "Install it with: pip install openai"
        )

    if api_key is None:
        api_key = get_api_key("openai")
    client = openai.OpenAI(api_key=api_key, base_url=base_url)

    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        # Extract text from response
        if response.choices and len(response.choices) > 0:
            return response.choices[0].message.content
        else:
            raise APIError("Empty response from OpenAI API")

    except openai.RateLimitError as e:
        raise RateLimitError(f"OpenAI API rate limit exceeded: {e}")
    except openai.AuthenticationError as e:
        raise APIKeyError(f"Invalid OpenAI API key: {e}")
    except openai.APIError as e:
        raise APIError(f"OpenAI API error: {e}")


def _dispatch_llm_call(
    prompt: str,
    provider: str,
    model: str,
    max_tokens: int = 4096,
    temperature: float = 0.3,
    call_type: str = "unknown",
) -> str:
    """Low-level dispatcher — routes a single LLM call to the right SDK."""
    pconfig = get_provider_config(provider)
    ptype = pconfig.get("type", provider)
    api_key = get_api_key(provider)

    t0 = time.time()
    if ptype == "anthropic":
        response = call_anthropic_api(prompt, model, max_tokens, temperature, api_key=api_key)
    elif ptype == "openai-compatible":
        response = call_openai_api(
            prompt, model, max_tokens, temperature,
            api_key=api_key, base_url=pconfig.get("base_url"),
        )
    else:
        raise ValueError(f"Unknown provider type '{ptype}' for provider '{provider}'")

    duration = time.time() - t0
    log_prompt(
        prompt=prompt,
        response=response,
        provider=provider,
        model=model,
        call_type=call_type,
        mode="realtime",
        temperature=temperature,
        max_tokens=max_tokens,
        duration_seconds=duration,
    )
    return response


def call_llm(
    prompt: str,
    provider: Provider = "anthropic",
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.3,
    max_retries: int = 3,
    call_type: str = "unknown",
) -> str:
    """Call an LLM and return the text response.

    Generic wrapper with retry logic.  Dispatches to the correct SDK based
    on the provider's ``type`` field in ``llm_config.json``.
    """
    if model is None:
        model = get_default_model()

    last_error = None
    for attempt in range(max_retries):
        try:
            return _dispatch_llm_call(prompt, provider, model, max_tokens, temperature, call_type=call_type)
        except RateLimitError as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(2 ** (attempt + 1))
            else:
                raise
        except APIError as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                raise
    raise last_error or APIError("LLM call failed")


def translate_chunk_realtime(
    chunk: Chunk,
    provider: Provider,
    model: str,
    glossary: Optional[Glossary] = None,
    style_guide: Optional[StyleGuide] = None,
    project_name: str = "Translation Project",
    source_language: str = "English",
    target_language: str = "Spanish",
    max_retries: int = 3,
    previous_chapter_context: str = "",
) -> Chunk:
    """
    Translate a single chunk using real-time API.

    Args:
        chunk: Chunk to translate
        provider: API provider ("anthropic" or "openai")
        model: Model identifier
        glossary: Optional glossary
        style_guide: Optional style guide
        project_name: Project name for context
        source_language: Source language
        target_language: Target language
        max_retries: Maximum retry attempts on failure

    Returns:
        Updated chunk with translation

    Raises:
        APIError: If translation fails after retries
    """
    # Filter glossary to terms relevant to this chunk
    chunk_glossary = filter_glossary_for_chunk(glossary, chunk.source_text) if glossary else None

    # Load and render prompt
    template = load_prompt_template()
    variables = {
        "book_title": project_name,
        "source_text": chunk.source_text,
        "target_language": target_language,
        "source_language": source_language,
        "glossary": format_glossary_for_prompt(chunk_glossary) if chunk_glossary else "No glossary provided.",
        "style_guide": style_guide.content if style_guide else "No style guide provided.",
        "context": "",
        "chapter_info": f"Chapter {chunk.chapter_id}, Chunk {chunk.position}",
        "previous_chapter_context": previous_chapter_context,
    }

    prompt = render_prompt(template, variables)

    # Strip header comments (like in workbook generation)
    separator = "=" * 80
    if separator in prompt:
        parts = prompt.split(separator, 1)
        if len(parts) > 1:
            prompt = separator + parts[1]

    # Use call_llm which handles retry + config-based dispatch
    translation = call_llm(prompt, provider=provider, model=model, max_retries=max_retries, call_type="translation")

    chunk.translated_text = translation.strip()
    chunk.status = ChunkStatus.TRANSLATED
    chunk.translated_at = datetime.now()
    return chunk


# ============================================================================
# Batch API Functions
# ============================================================================


def submit_batch(
    chunks: list[Chunk],
    provider: Provider,
    model: str,
    output_dir: Path,
    glossary: Optional[Glossary] = None,
    style_guide: Optional[StyleGuide] = None,
    project_name: str = "Translation Project",
    source_language: str = "English",
    target_language: str = "Spanish",
    context_map: Optional[dict[str, str]] = None,
) -> dict:
    """
    Submit a batch translation job to the API.

    Args:
        chunks: List of chunks to translate
        provider: API provider ("anthropic" or "openai")
        model: Model identifier
        output_dir: Directory where translated chunks will be saved
        glossary: Optional glossary
        style_guide: Optional style guide
        project_name: Project name for context
        source_language: Source language
        target_language: Target language
        context_map: Optional mapping of chunk ID to previous chapter context

    Returns:
        Dictionary with batch job info

    Raises:
        APIError: If batch submission fails
    """
    if context_map is None:
        context_map = {}

    if provider == "anthropic":
        return _submit_anthropic_batch(
            chunks, model, output_dir, glossary, style_guide,
            project_name, source_language, target_language, context_map
        )
    elif provider == "openai":
        return _submit_openai_batch(
            chunks, model, output_dir, glossary, style_guide,
            project_name, source_language, target_language, context_map
        )
    else:
        raise ValueError(f"Unknown provider: {provider}")


def _submit_anthropic_batch(
    chunks: list[Chunk],
    model: str,
    output_dir: Path,
    glossary: Optional[Glossary],
    style_guide: Optional[StyleGuide],
    project_name: str,
    source_language: str,
    target_language: str,
    context_map: dict[str, str] | None = None,
) -> dict:
    """Submit batch to Anthropic Message Batches API."""
    try:
        import anthropic
    except ImportError:
        raise APIError("anthropic package not installed")

    if context_map is None:
        context_map = {}

    api_key = get_api_key("anthropic")
    client = anthropic.Anthropic(api_key=api_key)

    # Build requests for each chunk
    template = load_prompt_template()
    requests = []

    for chunk in chunks:
        # Filter glossary to terms relevant to this chunk
        chunk_glossary = filter_glossary_for_chunk(glossary, chunk.source_text) if glossary else None

        # Render prompt for this chunk
        variables = {
            "book_title": project_name,
            "source_text": chunk.source_text,
            "target_language": target_language,
            "source_language": source_language,
            "glossary": format_glossary_for_prompt(chunk_glossary) if chunk_glossary else "No glossary provided.",
            "style_guide": style_guide.content if style_guide else "No style guide provided.",
            "context": "",
            "chapter_info": f"Chapter {chunk.chapter_id}, Chunk {chunk.position}",
            "previous_chapter_context": context_map.get(chunk.id, ""),
        }

        prompt = render_prompt(template, variables)

        # Strip header comments
        separator = "=" * 80
        if separator in prompt:
            parts = prompt.split(separator, 1)
            if len(parts) > 1:
                prompt = separator + parts[1]

        # Create batch request
        requests.append({
            "custom_id": chunk.id,
            "params": {
                "model": model,
                "max_tokens": 4096,
                "temperature": 0.3,
                "messages": [
                    {"role": "user", "content": prompt}
                ]
            }
        })

    # Build prompt map for later retrieval logging
    prompt_map = {}
    for req, chunk in zip(requests, chunks):
        prompt_map[chunk.id] = req["params"]["messages"][0]["content"]

    try:
        # Submit batch
        batch = client.messages.batches.create(requests=requests)

        # Log each prompt (response will arrive later)
        for chunk_id, prompt_text in prompt_map.items():
            log_prompt(
                prompt=prompt_text,
                response=None,
                provider="anthropic",
                model=model,
                call_type="translation",
                mode="batch",
                batch_job_id=batch.id,
                chunk_id=chunk_id,
            )

        # Return job info
        return {
            "job_id": batch.id,
            "provider": "anthropic",
            "model": model,
            "submitted_at": datetime.now().isoformat(),
            "status": batch.processing_status,
            "chunk_count": len(chunks),
            "chunk_ids": [chunk.id for chunk in chunks],
            "output_dir": str(output_dir),
            "prompt_map": prompt_map,
        }

    except anthropic.AuthenticationError as e:
        raise APIKeyError(f"Invalid Anthropic API key: {e}")
    except anthropic.APIError as e:
        raise APIError(f"Anthropic batch submission failed: {e}")


def _submit_openai_batch(
    chunks: list[Chunk],
    model: str,
    output_dir: Path,
    glossary: Optional[Glossary],
    style_guide: Optional[StyleGuide],
    project_name: str,
    source_language: str,
    target_language: str,
    context_map: dict[str, str] | None = None,
) -> dict:
    """Submit batch to OpenAI Batch API."""
    try:
        import openai
    except ImportError:
        raise APIError("openai package not installed")

    if context_map is None:
        context_map = {}

    api_key = get_api_key("openai")
    client = openai.OpenAI(api_key=api_key)

    # Build JSONL file with requests
    template = load_prompt_template()
    jsonl_lines = []

    for chunk in chunks:
        # Filter glossary to terms relevant to this chunk
        chunk_glossary = filter_glossary_for_chunk(glossary, chunk.source_text) if glossary else None

        # Render prompt for this chunk
        variables = {
            "book_title": project_name,
            "source_text": chunk.source_text,
            "target_language": target_language,
            "source_language": source_language,
            "glossary": format_glossary_for_prompt(chunk_glossary) if chunk_glossary else "No glossary provided.",
            "style_guide": style_guide.content if style_guide else "No style guide provided.",
            "context": "",
            "chapter_info": f"Chapter {chunk.chapter_id}, Chunk {chunk.position}",
            "previous_chapter_context": context_map.get(chunk.id, ""),
        }

        prompt = render_prompt(template, variables)

        # Strip header comments
        separator = "=" * 80
        if separator in prompt:
            parts = prompt.split(separator, 1)
            if len(parts) > 1:
                prompt = separator + parts[1]

        # Create batch request
        request = {
            "custom_id": chunk.id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": model,
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 4096,
                "temperature": 0.3
            }
        }

        jsonl_lines.append(json.dumps(request))

    # Build prompt map for later retrieval logging
    prompt_map = {}
    for req_line, chunk in zip(jsonl_lines, chunks):
        req_obj = json.loads(req_line)
        prompt_map[chunk.id] = req_obj["body"]["messages"][0]["content"]

    try:
        # Write JSONL to temporary file
        import tempfile
        jsonl_path = Path(tempfile.gettempdir()) / f"openai_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
        jsonl_path.write_text("\n".join(jsonl_lines))

        # Upload file
        with open(jsonl_path, "rb") as f:
            batch_file = client.files.create(file=f, purpose="batch")

        # Create batch
        batch = client.batches.create(
            input_file_id=batch_file.id,
            endpoint="/v1/chat/completions",
            completion_window="24h"
        )

        # Clean up temp file
        jsonl_path.unlink()

        # Log each prompt (response will arrive later)
        for chunk_id, prompt_text in prompt_map.items():
            log_prompt(
                prompt=prompt_text,
                response=None,
                provider="openai",
                model=model,
                call_type="translation",
                mode="batch",
                batch_job_id=batch.id,
                chunk_id=chunk_id,
            )

        # Return job info
        return {
            "job_id": batch.id,
            "provider": "openai",
            "model": model,
            "submitted_at": datetime.now().isoformat(),
            "status": batch.status,
            "chunk_count": len(chunks),
            "chunk_ids": [chunk.id for chunk in chunks],
            "output_dir": str(output_dir),
            "prompt_map": prompt_map,
        }

    except openai.AuthenticationError as e:
        raise APIKeyError(f"Invalid OpenAI API key: {e}")
    except openai.APIError as e:
        raise APIError(f"OpenAI batch submission failed: {e}")


def check_batch_status(
    job_id: str,
    provider: Provider,
) -> dict:
    """
    Check the status of a batch translation job.

    Args:
        job_id: Batch job ID
        provider: API provider

    Returns:
        Dictionary with status info:
        {
            "job_id": str,
            "status": str,  # "in_progress", "completed", "failed", etc.
            "completed_at": Optional[str],
            "failed_count": int,
            "succeeded_count": int,
            "total_count": int
        }

    Raises:
        APIError: If status check fails
    """
    if provider == "anthropic":
        return _check_anthropic_batch(job_id)
    elif provider == "openai":
        return _check_openai_batch(job_id)
    else:
        raise ValueError(f"Unknown provider: {provider}")


def _check_anthropic_batch(job_id: str) -> dict:
    """Check Anthropic batch status."""
    try:
        import anthropic
    except ImportError:
        raise APIError("anthropic package not installed")

    api_key = get_api_key("anthropic")
    client = anthropic.Anthropic(api_key=api_key)

    try:
        batch = client.messages.batches.retrieve(job_id)

        return {
            "job_id": batch.id,
            "status": batch.processing_status,
            "completed_at": batch.ended_at if hasattr(batch, 'ended_at') else None,
            "failed_count": batch.request_counts.errored if hasattr(batch, 'request_counts') else 0,
            "succeeded_count": batch.request_counts.succeeded if hasattr(batch, 'request_counts') else 0,
            "total_count": batch.request_counts.processing + batch.request_counts.succeeded + batch.request_counts.errored if hasattr(batch, 'request_counts') else 0
        }

    except anthropic.APIError as e:
        raise APIError(f"Failed to check Anthropic batch status: {e}")


def _check_openai_batch(job_id: str) -> dict:
    """Check OpenAI batch status."""
    try:
        import openai
    except ImportError:
        raise APIError("openai package not installed")

    api_key = get_api_key("openai")
    client = openai.OpenAI(api_key=api_key)

    try:
        batch = client.batches.retrieve(job_id)

        return {
            "job_id": batch.id,
            "status": batch.status,
            "completed_at": batch.completed_at,
            "failed_count": batch.request_counts.failed if hasattr(batch, 'request_counts') else 0,
            "succeeded_count": batch.request_counts.completed if hasattr(batch, 'request_counts') else 0,
            "total_count": batch.request_counts.total if hasattr(batch, 'request_counts') else 0
        }

    except openai.APIError as e:
        raise APIError(f"Failed to check OpenAI batch status: {e}")


def retrieve_batch_results(
    job_id: str,
    provider: Provider,
    original_chunks: list[Chunk],
    output_dir: Path,
    model: str = "",
    prompt_map: dict[str, str] | None = None,
) -> list[Chunk]:
    """
    Retrieve and process results from a completed batch job.

    Args:
        job_id: Batch job ID
        provider: API provider
        original_chunks: Original chunks (to get IDs and metadata)
        output_dir: Directory to save translated chunks
        model: Model used (for logging)
        prompt_map: Mapping of chunk ID to the original prompt sent at submission

    Returns:
        List of updated chunks with translations

    Raises:
        APIError: If retrieval fails or batch is not complete
    """
    # Check status first
    status = check_batch_status(job_id, provider)

    if status["status"] not in ["completed", "ended"]:
        raise APIError(
            f"Batch {job_id} is not complete yet. "
            f"Status: {status['status']}"
        )

    if prompt_map is None:
        prompt_map = {}

    if provider == "anthropic":
        return _retrieve_anthropic_results(job_id, original_chunks, output_dir, model, prompt_map)
    elif provider == "openai":
        return _retrieve_openai_results(job_id, original_chunks, output_dir, model, prompt_map)
    else:
        raise ValueError(f"Unknown provider: {provider}")


def _retrieve_anthropic_results(
    job_id: str,
    original_chunks: list[Chunk],
    output_dir: Path,
    model: str = "",
    prompt_map: dict[str, str] | None = None,
) -> list[Chunk]:
    """Retrieve results from Anthropic batch."""
    try:
        import anthropic
    except ImportError:
        raise APIError("anthropic package not installed")

    api_key = get_api_key("anthropic")
    client = anthropic.Anthropic(api_key=api_key)

    try:
        # Get results
        results = client.messages.batches.results(job_id)

        # Create mapping from chunk ID to chunk
        chunk_map = {chunk.id: chunk for chunk in original_chunks}
        updated_chunks = []

        # Process each result
        for result in results:
            chunk_id = result.custom_id

            if chunk_id not in chunk_map:
                continue

            chunk = chunk_map[chunk_id]

            # Check if successful
            if result.result.type == "succeeded":
                message = result.result.message
                if message.content and len(message.content) > 0:
                    translation = message.content[0].text
                    chunk.translated_text = translation.strip()
                    chunk.status = ChunkStatus.TRANSLATED
                    chunk.translated_at = datetime.now()
                    updated_chunks.append(chunk)

                    # Log the complete prompt + response pair
                    original_prompt = (prompt_map or {}).get(chunk_id, "")
                    log_prompt(
                        prompt=original_prompt,
                        response=translation.strip(),
                        provider="anthropic",
                        model=model,
                        call_type="translation_result",
                        mode="batch",
                        batch_job_id=job_id,
                        chunk_id=chunk_id,
                    )

        return updated_chunks

    except anthropic.APIError as e:
        raise APIError(f"Failed to retrieve Anthropic batch results: {e}")


def _retrieve_openai_results(
    job_id: str,
    original_chunks: list[Chunk],
    output_dir: Path,
    model: str = "",
    prompt_map: dict[str, str] | None = None,
) -> list[Chunk]:
    """Retrieve results from OpenAI batch."""
    try:
        import openai
    except ImportError:
        raise APIError("openai package not installed")

    api_key = get_api_key("openai")
    client = openai.OpenAI(api_key=api_key)

    try:
        # Get batch info to get output file ID
        batch = client.batches.retrieve(job_id)

        if not batch.output_file_id:
            raise APIError("Batch has no output file")

        # Download output file
        output_content = client.files.content(batch.output_file_id)
        output_lines = output_content.text.strip().split("\n")

        # Create mapping from chunk ID to chunk
        chunk_map = {chunk.id: chunk for chunk in original_chunks}
        updated_chunks = []

        # Process each result
        for line in output_lines:
            if not line.strip():
                continue
            result = json.loads(line)
            chunk_id = result.get("custom_id")

            if chunk_id not in chunk_map:
                continue

            chunk = chunk_map[chunk_id]

            # Check if successful
            response = result.get("response", {})
            if response.get("status_code") == 200:
                body = response.get("body", {})
                choices = body.get("choices", [])
                if choices and len(choices) > 0:
                    translation = choices[0].get("message", {}).get("content", "")
                    chunk.translated_text = translation.strip()
                    chunk.status = ChunkStatus.TRANSLATED
                    chunk.translated_at = datetime.now()
                    updated_chunks.append(chunk)

                    # Log the complete prompt + response pair
                    original_prompt = (prompt_map or {}).get(chunk_id, "")
                    log_prompt(
                        prompt=original_prompt,
                        response=translation.strip(),
                        provider="openai",
                        model=model,
                        call_type="translation_result",
                        mode="batch",
                        batch_job_id=job_id,
                        chunk_id=chunk_id,
                    )

        return updated_chunks

    except openai.APIError as e:
        raise APIError(f"Failed to retrieve OpenAI batch results: {e}")


# ============================================================================
# Batch Job Tracking
# ============================================================================


def translate_chapter_with_model(
    chunks: list[Chunk],
    model_id: str,
    project_path: Path,
    provider: Provider | None = None,
    glossary: Optional[Glossary] = None,
    style_guide: Optional[StyleGuide] = None,
    project_name: str = "Translation Project",
    source_language: str = "English",
    target_language: str = "Spanish",
) -> list[Chunk]:
    """Translate all chunks for a chapter using a specific model via batch API.

    Thin wrapper around ``submit_batch`` + polling ``check_batch_status`` +
    ``retrieve_batch_results``.  Surfaces per-model batch failure explicitly
    (raises ``APIError`` rather than silently returning an empty list).

    Args:
        chunks: Source chunks to translate.
        model_id: Model identifier (e.g. ``"claude-sonnet-4-6"``).
        project_path: Path to the project directory (used for output_dir).
        provider: LLM provider; defaults to ``get_default_provider()``.
        glossary: Optional glossary.
        style_guide: Optional style guide.
        project_name: Project name for prompt context.
        source_language: Source language name.
        target_language: Target language name.

    Returns:
        List of chunks with ``translated_text`` populated.

    Raises:
        APIError: If batch submission, polling, or retrieval fails.
    """
    import copy

    prov = provider or get_default_provider()
    output_dir = project_path / "comparisons" / "_tmp_translations"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Deep-copy chunks so the caller's originals stay untouched
    work_chunks = [copy.deepcopy(c) for c in chunks]

    job_info = submit_batch(
        work_chunks, prov, model_id, output_dir,
        glossary=glossary, style_guide=style_guide,
        project_name=project_name,
        source_language=source_language,
        target_language=target_language,
    )
    job_id = job_info["job_id"]

    # Poll until complete (30s intervals, ~30min max)
    import time as _time
    for _ in range(60):
        status = check_batch_status(job_id, prov)
        if status["status"] in ("completed", "ended"):
            break
        if status["status"] in ("failed", "expired", "cancelled"):
            raise APIError(
                f"Batch {job_id} for model {model_id} "
                f"ended with status: {status['status']}"
            )
        _time.sleep(30)
    else:
        raise APIError(
            f"Batch {job_id} for model {model_id} timed out after 30 minutes"
        )

    translated = retrieve_batch_results(
        job_id, prov, work_chunks, output_dir,
        model=model_id, prompt_map=job_info.get("prompt_map"),
    )

    if not translated:
        raise APIError(
            f"Batch {job_id} for model {model_id} returned zero translations"
        )

    return translated


def save_batch_job(job_info: dict, tracking_file: Path = Path("batch_jobs.json")):
    """
    Save batch job information to tracking file.

    Args:
        job_info: Dictionary with batch job info
        tracking_file: Path to tracking file (default: batch_jobs.json)
    """
    # Load existing jobs
    if tracking_file.exists():
        data = json.loads(tracking_file.read_text())
    else:
        data = {"jobs": []}

    # Add new job
    data["jobs"].append(job_info)

    # Save
    tracking_file.write_text(json.dumps(data, indent=2))


def load_batch_jobs(tracking_file: Path = Path("batch_jobs.json")) -> list[dict]:
    """
    Load all batch jobs from tracking file.

    Args:
        tracking_file: Path to tracking file

    Returns:
        List of batch job dictionaries
    """
    if not tracking_file.exists():
        return []

    data = json.loads(tracking_file.read_text())
    return data.get("jobs", [])


def get_batch_job(job_id: str, tracking_file: Path = Path("batch_jobs.json")) -> Optional[dict]:
    """
    Get specific batch job by ID.

    Args:
        job_id: Batch job ID
        tracking_file: Path to tracking file

    Returns:
        Job dictionary or None if not found
    """
    jobs = load_batch_jobs(tracking_file)
    for job in jobs:
        if job["job_id"] == job_id:
            return job
    return None


def update_batch_job_status(
    job_id: str,
    status: str,
    tracking_file: Path = Path("batch_jobs.json")
):
    """
    Update status of a batch job in tracking file.

    Args:
        job_id: Batch job ID
        status: New status
        tracking_file: Path to tracking file
    """
    if not tracking_file.exists():
        return

    data = json.loads(tracking_file.read_text())
    jobs = data.get("jobs", [])

    for job in jobs:
        if job["job_id"] == job_id:
            job["status"] = status
            if status == "completed":
                job["completed_at"] = datetime.now().isoformat()
            break

    tracking_file.write_text(json.dumps(data, indent=2))
