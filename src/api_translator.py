"""
API-based translation using Anthropic Claude or OpenAI GPT models.

This module provides functions to translate chunks using AI APIs in both
real-time and batch modes.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Literal

from dotenv import load_dotenv

from src.models import Chunk, ChunkStatus, Glossary, StyleGuide
from src.utils.file_io import render_prompt, load_prompt_template, format_glossary_for_prompt, filter_glossary_for_chunk

# Load environment variables from .env file
load_dotenv()

# API Provider types
Provider = Literal["anthropic", "openai"]


# Pricing per 1M tokens (updated April 2026)
PRICING_TABLE = {
    "anthropic": {
        "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
        "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
        "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
        "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.00},
    },
    "openai": {
        "gpt-4o": {"input": 2.50, "output": 10.00},
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    },
}

DEFAULT_MODEL = "claude-sonnet-4-20250514"


def get_pricing_table() -> dict:
    """Return the pricing table for all known models."""
    return PRICING_TABLE


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
    """
    Get API key for the specified provider from environment variables.

    Args:
        provider: API provider ("anthropic" or "openai")

    Returns:
        API key string

    Raises:
        APIKeyError: If API key is not found in environment
    """
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
    if provider not in PRICING_TABLE or model not in PRICING_TABLE[provider]:
        # Unknown model, use conservative estimate
        model_pricing = {"input": 5.00, "output": 15.00}
    else:
        model_pricing = PRICING_TABLE[provider][model]

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
) -> str:
    """
    Call Anthropic Claude API for real-time translation.

    Args:
        prompt: The translation prompt
        model: Claude model identifier
        max_tokens: Maximum tokens to generate
        temperature: Sampling temperature (0-1)

    Returns:
        Translated text from Claude

    Raises:
        APIKeyError: If API key is missing
        RateLimitError: If rate limit is exceeded
        APIError: For other API errors
    """
    try:
        import anthropic
    except ImportError:
        raise APIError(
            "anthropic package not installed. "
            "Install it with: pip install anthropic"
        )

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
) -> str:
    """
    Call OpenAI API for real-time translation.

    Args:
        prompt: The translation prompt
        model: OpenAI model identifier
        max_tokens: Maximum tokens to generate
        temperature: Sampling temperature (0-1)

    Returns:
        Translated text from OpenAI

    Raises:
        APIKeyError: If API key is missing
        RateLimitError: If rate limit is exceeded
        APIError: For other API errors
    """
    try:
        import openai
    except ImportError:
        raise APIError(
            "openai package not installed. "
            "Install it with: pip install openai"
        )

    api_key = get_api_key("openai")
    client = openai.OpenAI(api_key=api_key)

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


def call_llm(
    prompt: str,
    provider: Provider = "anthropic",
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
    temperature: float = 0.3,
    max_retries: int = 3,
) -> str:
    """Call an LLM and return the text response.

    Generic wrapper around call_anthropic_api/call_openai_api with retry logic.
    Use this for non-translation LLM calls (style wizard, glossary bootstrap, etc.).
    """
    last_error = None
    for attempt in range(max_retries):
        try:
            if provider == "anthropic":
                return call_anthropic_api(prompt, model, max_tokens, temperature)
            elif provider == "openai":
                return call_openai_api(prompt, model, max_tokens, temperature)
            else:
                raise ValueError(f"Unknown provider: {provider}")
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

    # Retry logic with exponential backoff
    last_error = None
    for attempt in range(max_retries):
        try:
            # Call appropriate API
            if provider == "anthropic":
                translation = call_anthropic_api(prompt, model)
            elif provider == "openai":
                translation = call_openai_api(prompt, model)
            else:
                raise ValueError(f"Unknown provider: {provider}")

            # Update chunk with translation
            chunk.translated_text = translation.strip()
            chunk.status = ChunkStatus.TRANSLATED
            chunk.translated_at = datetime.now()

            return chunk

        except RateLimitError as e:
            last_error = e
            if attempt < max_retries - 1:
                # Exponential backoff: 2s, 4s, 8s
                wait_time = 2 ** (attempt + 1)
                time.sleep(wait_time)
            else:
                raise

        except APIError as e:
            last_error = e
            if attempt < max_retries - 1:
                # Brief wait before retry
                time.sleep(2)
            else:
                raise

    # Should not reach here, but just in case
    raise last_error or APIError("Translation failed")


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

    Returns:
        Dictionary with batch job info:
        {
            "job_id": str,
            "provider": str,
            "model": str,
            "submitted_at": str,
            "status": str,
            "chunk_count": int,
            "chunk_ids": list[str],
            "output_dir": str
        }

    Raises:
        APIError: If batch submission fails
    """
    if provider == "anthropic":
        return _submit_anthropic_batch(
            chunks, model, output_dir, glossary, style_guide,
            project_name, source_language, target_language
        )
    elif provider == "openai":
        return _submit_openai_batch(
            chunks, model, output_dir, glossary, style_guide,
            project_name, source_language, target_language
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
) -> dict:
    """Submit batch to Anthropic Message Batches API."""
    try:
        import anthropic
    except ImportError:
        raise APIError("anthropic package not installed")

    api_key = get_api_key("anthropic")
    client = anthropic.Anthropic(api_key=api_key)

    # Build requests for each chunk
    template = load_prompt_template()
    requests = []

    for chunk in chunks:
        # Render prompt for this chunk
        variables = {
            "book_title": project_name,
            "source_text": chunk.source_text,
            "target_language": target_language,
            "source_language": source_language,
            "glossary": format_glossary_for_prompt(glossary) if glossary else "No glossary provided.",
            "style_guide": style_guide.content if style_guide else "No style guide provided.",
            "context": "",
            "chapter_info": f"Chapter {chunk.chapter_id}, Chunk {chunk.position}",
            "previous_chapter_context": "",
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

    try:
        # Submit batch
        batch = client.messages.batches.create(requests=requests)

        # Return job info
        return {
            "job_id": batch.id,
            "provider": "anthropic",
            "model": model,
            "submitted_at": datetime.now().isoformat(),
            "status": batch.processing_status,
            "chunk_count": len(chunks),
            "chunk_ids": [chunk.id for chunk in chunks],
            "output_dir": str(output_dir)
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
) -> dict:
    """Submit batch to OpenAI Batch API."""
    try:
        import openai
    except ImportError:
        raise APIError("openai package not installed")

    api_key = get_api_key("openai")
    client = openai.OpenAI(api_key=api_key)

    # Build JSONL file with requests
    template = load_prompt_template()
    jsonl_lines = []

    for chunk in chunks:
        # Render prompt for this chunk
        variables = {
            "book_title": project_name,
            "source_text": chunk.source_text,
            "target_language": target_language,
            "source_language": source_language,
            "glossary": format_glossary_for_prompt(glossary) if glossary else "No glossary provided.",
            "style_guide": style_guide.content if style_guide else "No style guide provided.",
            "context": "",
            "chapter_info": f"Chapter {chunk.chapter_id}, Chunk {chunk.position}",
            "previous_chapter_context": "",
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

    try:
        # Write JSONL to temporary file
        jsonl_path = Path(f"/tmp/openai_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl")
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

        # Return job info
        return {
            "job_id": batch.id,
            "provider": "openai",
            "model": model,
            "submitted_at": datetime.now().isoformat(),
            "status": batch.status,
            "chunk_count": len(chunks),
            "chunk_ids": [chunk.id for chunk in chunks],
            "output_dir": str(output_dir)
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
) -> list[Chunk]:
    """
    Retrieve and process results from a completed batch job.

    Args:
        job_id: Batch job ID
        provider: API provider
        original_chunks: Original chunks (to get IDs and metadata)
        output_dir: Directory to save translated chunks

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

    if provider == "anthropic":
        return _retrieve_anthropic_results(job_id, original_chunks, output_dir)
    elif provider == "openai":
        return _retrieve_openai_results(job_id, original_chunks, output_dir)
    else:
        raise ValueError(f"Unknown provider: {provider}")


def _retrieve_anthropic_results(
    job_id: str,
    original_chunks: list[Chunk],
    output_dir: Path,
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

        return updated_chunks

    except anthropic.APIError as e:
        raise APIError(f"Failed to retrieve Anthropic batch results: {e}")


def _retrieve_openai_results(
    job_id: str,
    original_chunks: list[Chunk],
    output_dir: Path,
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

        return updated_chunks

    except openai.APIError as e:
        raise APIError(f"Failed to retrieve OpenAI batch results: {e}")


# ============================================================================
# Batch Job Tracking
# ============================================================================


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
