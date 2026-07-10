"""
Tests for API translation functionality.

Uses mocked API responses to avoid calling real APIs.
"""

import json
import os
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

from src.models import Chunk, ChunkMetadata, ChunkStatus, Glossary, GlossaryTerm, GlossaryTermType, StyleGuide
from src.api_translator import (
    get_api_key,
    estimate_cost,
    build_translation_prompt,
    build_translation_prompt_parts,
    apply_translation,
    translate_chunk_realtime,
    call_anthropic_api,
    call_openai_api,
    submit_batch,
    submit_translation_job,
    await_translation_job,
    check_batch_status,
    retrieve_batch_results,
    save_batch_job,
    load_batch_jobs,
    get_batch_job,
    update_batch_job_status,
    summarize_chunk_features,
    APIError,
    APIKeyError,
    RateLimitError,
    _rejects_sampling_params,
)
from src.utils.prompt_logger import last_cache_usage


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def sample_chunk():
    """Create a sample chunk for testing."""
    return Chunk(
        id="ch01_chunk_001",
        chapter_id="chapter_01",
        position=1,
        source_text="It is a truth universally acknowledged, that a single man in possession of a good fortune must be in want of a wife.",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=115,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=23
        ),
        status=ChunkStatus.PENDING,
        created_at=datetime(2025, 1, 28, 10, 0, 0)
    )


@pytest.fixture
def sample_glossary():
    """Create a sample glossary for testing."""
    return Glossary(
        terms=[
            GlossaryTerm(
                english="Mr. Bennet",
                spanish="Sr. Bennet",
                type=GlossaryTermType.CHARACTER
            ),
        ],
        version="1.0",
        updated_at=datetime(2025, 1, 28, 9, 0)
    )


@pytest.fixture
def sample_style_guide():
    """Create a sample style guide for testing."""
    return StyleGuide(
        content="TONE: Formal but accessible\nFORMALITY: Medium-high",
        version="1.0",
        created_at=datetime(2025, 1, 28, 9, 0),
        updated_at=datetime(2025, 1, 28, 9, 0)
    )


# ============================================================================
# API Key Tests
# ============================================================================


def test_get_api_key_success():
    """Test getting API key from environment."""
    with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key-123'}):
        key = get_api_key('anthropic')
        assert key == 'test-key-123'


def test_get_api_key_missing():
    """Test error when API key is missing."""
    with patch.dict('os.environ', {}, clear=True):
        with pytest.raises(APIKeyError, match="ANTHROPIC_API_KEY not found"):
            get_api_key('anthropic')


# ============================================================================
# Cost Estimation Tests
# ============================================================================


def test_estimate_cost_single_chunk(sample_chunk):
    """Test cost estimation for a single chunk."""
    cost_info = estimate_cost(
        chunks=[sample_chunk],
        provider='anthropic',
        model='claude-3-5-sonnet-20241022',
        batch_mode=False
    )

    assert 'input_tokens' in cost_info
    assert 'output_tokens_estimate' in cost_info
    assert 'cost_usd' in cost_info
    assert 'cost_per_chunk_usd' in cost_info
    assert cost_info['input_tokens'] > 0
    assert cost_info['cost_usd'] > 0


def test_estimate_cost_batch_discount(sample_chunk):
    """Test that batch mode applies 50% discount."""
    cost_realtime = estimate_cost(
        chunks=[sample_chunk],
        provider='anthropic',
        model='claude-3-5-sonnet-20241022',
        batch_mode=False
    )

    cost_batch = estimate_cost(
        chunks=[sample_chunk],
        provider='anthropic',
        model='claude-3-5-sonnet-20241022',
        batch_mode=True
    )

    # Batch should be roughly 50% of realtime
    assert cost_batch['cost_usd'] < cost_realtime['cost_usd']
    assert abs(cost_batch['cost_usd'] - cost_realtime['cost_usd'] * 0.5) < 0.01


def test_estimate_cost_with_glossary(sample_chunk, sample_glossary):
    """Test cost estimation with glossary (increases prompt size)."""
    cost_without = estimate_cost(
        chunks=[sample_chunk],
        provider='anthropic',
        model='claude-3-5-sonnet-20241022',
        batch_mode=False
    )

    cost_with = estimate_cost(
        chunks=[sample_chunk],
        provider='anthropic',
        model='claude-3-5-sonnet-20241022',
        batch_mode=False,
        glossary=sample_glossary
    )

    # With glossary should cost more (larger prompt)
    assert cost_with['input_tokens'] > cost_without['input_tokens']


# ============================================================================
# Anthropic API Tests
# ============================================================================


def test_call_anthropic_api_success():
    """Test successful Anthropic API call."""
    # Skip if anthropic not installed
    pytest.importorskip("anthropic")

    # Mock the anthropic module's Anthropic class
    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Es una verdad universalmente reconocida...")]
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            result = call_anthropic_api("Translate this text")

        assert result == "Es una verdad universalmente reconocida..."
        mock_client.messages.create.assert_called_once()


def test_call_anthropic_api_rate_limit():
    """Test rate limit error handling."""
    anthropic = pytest.importorskip("anthropic")

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.headers = {}
        mock_client.messages.create.side_effect = anthropic.RateLimitError(
            "Rate limit exceeded", response=mock_response, body={}
        )
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with pytest.raises(RateLimitError, match="rate limit"):
                call_anthropic_api("Test prompt")


def test_call_anthropic_api_auth_error():
    """Test authentication error handling."""
    anthropic = pytest.importorskip("anthropic")

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_response = Mock()
        mock_response.status_code = 401
        mock_response.headers = {}
        mock_client.messages.create.side_effect = anthropic.AuthenticationError(
            "Invalid key", response=mock_response, body={}
        )
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'invalid-key'}):
            with pytest.raises(APIKeyError, match="Invalid Anthropic API key"):
                call_anthropic_api("Test prompt")


def test_rejects_sampling_params():
    """Opus 4.7+ generation models (incl. Sonnet 5) reject sampling params; older ones don't."""
    assert _rejects_sampling_params("claude-sonnet-5") is True
    assert _rejects_sampling_params("claude-sonnet-5-20260615") is True
    assert _rejects_sampling_params("claude-opus-4-7") is True
    assert _rejects_sampling_params("claude-opus-4-8") is True
    assert _rejects_sampling_params("claude-fable-5") is True
    assert _rejects_sampling_params("claude-3-5-sonnet-20241022") is False
    assert _rejects_sampling_params("claude-sonnet-4-6") is False


def test_call_anthropic_api_omits_temperature_for_sonnet_5():
    """call_anthropic_api must not send temperature for a model that 400s on it."""
    pytest.importorskip("anthropic")

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Translated text")]
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            call_anthropic_api("Translate this text", model="claude-sonnet-5")

        _, kwargs = mock_client.messages.create.call_args
        assert "temperature" not in kwargs
        assert kwargs["model"] == "claude-sonnet-5"


def test_call_anthropic_api_disables_thinking_by_default_for_sonnet_5():
    """Sonnet 5 defaults thinking ON when omitted; we must send disabled."""
    pytest.importorskip("anthropic")

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Translated text")]
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}, clear=False):
            os.environ.pop("TRANSLATE_THINKING", None)
            call_anthropic_api("Translate this text", model="claude-sonnet-5")

        _, kwargs = mock_client.messages.create.call_args
        assert kwargs["thinking"] == {"type": "disabled"}


def test_call_anthropic_api_enables_adaptive_thinking_when_opted_in():
    """TRANSLATE_THINKING=1 opts back into adaptive thinking on Sonnet 5."""
    pytest.importorskip("anthropic")

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Translated text")]
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ',
                        {'ANTHROPIC_API_KEY': 'test-key', 'TRANSLATE_THINKING': '1'}):
            call_anthropic_api("Translate this text", model="claude-sonnet-5")

        _, kwargs = mock_client.messages.create.call_args
        assert kwargs["thinking"] == {"type": "adaptive"}


def test_model_supports_thinking():
    """Only new-gen togglable models report thinking support."""
    from src.api_translator import model_supports_thinking
    assert model_supports_thinking("claude-sonnet-5") is True
    assert model_supports_thinking("claude-opus-4-8") is True
    assert model_supports_thinking("claude-opus-4-7") is True
    assert model_supports_thinking("claude-fable-5") is False       # always-on
    assert model_supports_thinking("claude-sonnet-4-6") is False    # always-off
    assert model_supports_thinking("claude-3-5-sonnet-20241022") is False


def test_call_anthropic_api_enable_thinking_flag_overrides_env():
    """An explicit enable_thinking=True wins even when the env var is unset/off."""
    pytest.importorskip("anthropic")

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Translated text")]
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}, clear=False):
            os.environ.pop("TRANSLATE_THINKING", None)
            call_anthropic_api("Translate", model="claude-sonnet-5", enable_thinking=True)

        _, kwargs = mock_client.messages.create.call_args
        assert kwargs["thinking"] == {"type": "adaptive"}


def test_call_anthropic_api_enable_thinking_false_overrides_env_on():
    """An explicit enable_thinking=False disables thinking even if the env var is on."""
    pytest.importorskip("anthropic")

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Translated text")]
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ',
                        {'ANTHROPIC_API_KEY': 'test-key', 'TRANSLATE_THINKING': '1'}):
            call_anthropic_api("Translate", model="claude-sonnet-5", enable_thinking=False)

        _, kwargs = mock_client.messages.create.call_args
        assert kwargs["thinking"] == {"type": "disabled"}


def test_submit_anthropic_batch_enable_thinking_flag(sample_chunk, tmp_path):
    """submit_batch threads enable_thinking into the batch request params."""
    pytest.importorskip("anthropic")

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_batch = Mock()
        mock_batch.id = "batch_abc123"
        mock_batch.processing_status = "in_progress"
        mock_client.messages.batches.create.return_value = mock_batch
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}, clear=False):
            os.environ.pop("TRANSLATE_THINKING", None)
            submit_batch(
                chunks=[sample_chunk],
                provider='anthropic',
                model='claude-sonnet-5',
                output_dir=tmp_path / "translated",
                enable_thinking=True,
            )

        _, kwargs = mock_client.messages.batches.create.call_args
        params = kwargs["requests"][0]["params"]
        assert params["thinking"] == {"type": "adaptive"}
        # Thinking tokens count against max_tokens, so the batch cap is raised too.
        assert params["max_tokens"] >= 8192


@pytest.mark.parametrize("argv_flag,expected", [
    (["--thinking"], True),
    (["--no-thinking"], False),
    ([], None),
])
def test_translate_api_cli_threads_thinking_realtime(sample_chunk, tmp_path, monkeypatch,
                                                     argv_flag, expected):
    """--thinking / --no-thinking / absent reach translate_chunk_realtime as True/False/None.

    "Absent" stays ``None`` so the resolver falls back to the TRANSLATE_THINKING env default.
    """
    from scripts import translate_api

    monkeypatch.setattr(translate_api, "load_chunks_from_patterns", lambda _p: [sample_chunk])
    monkeypatch.setattr(translate_api, "estimate_cost",
                        lambda *a, **k: {"input_tokens": 1, "output_tokens_estimate": 1,
                                         "cost_usd": 0.0, "cost_per_chunk_usd": 0.0})
    monkeypatch.setattr(translate_api, "save_chunk", lambda *a, **k: None)
    mock_tr = Mock(return_value=sample_chunk)
    monkeypatch.setattr(translate_api, "translate_chunk_realtime", mock_tr)
    monkeypatch.setattr("sys.argv",
                        ["translate_api.py", "chunks/x.json", "--yes",
                         "--output", str(tmp_path / "out"), *argv_flag])

    assert translate_api.main() == 0
    _, kwargs = mock_tr.call_args
    assert kwargs["enable_thinking"] is expected


@pytest.mark.parametrize("argv_flag,expected", [
    (["--thinking"], True),
    (["--no-thinking"], False),
    ([], None),
])
def test_translate_api_cli_threads_thinking_batch(sample_chunk, tmp_path, monkeypatch,
                                                  argv_flag, expected):
    """--thinking / --no-thinking / absent reach submit_batch as True/False/None."""
    from scripts import translate_api

    monkeypatch.setattr(translate_api, "load_chunks_with_paths",
                        lambda _p: [(sample_chunk, "chunks/x.json")])
    monkeypatch.setattr(translate_api, "estimate_cost",
                        lambda *a, **k: {"input_tokens": 1, "output_tokens_estimate": 1,
                                         "cost_usd": 0.0, "cost_per_chunk_usd": 0.0})
    monkeypatch.setattr(translate_api, "save_batch_job", lambda *a, **k: None)
    mock_sb = Mock(return_value={"job_id": "batch_abc", "status": "in_progress", "chunk_count": 1})
    monkeypatch.setattr(translate_api, "submit_batch", mock_sb)
    monkeypatch.setattr("sys.argv",
                        ["translate_api.py", "chunks/x.json", "--batch", "--yes",
                         "--output", str(tmp_path / "out"), *argv_flag])

    assert translate_api.main() == 0
    _, kwargs = mock_sb.call_args
    assert kwargs["enable_thinking"] is expected


def test_max_tokens_with_thinking_floor():
    """When thinking is active max_tokens is raised to the floor; otherwise untouched."""
    from src.api_translator import _max_tokens_with_thinking, _THINKING_MAX_TOKENS_FLOOR

    # Adaptive thinking → raised to the floor.
    assert _max_tokens_with_thinking(
        "claude-sonnet-5", 4096, {"type": "adaptive"}
    ) == _THINKING_MAX_TOKENS_FLOOR
    # A caller cap already above the floor is preserved.
    assert _max_tokens_with_thinking("claude-sonnet-5", 20000, {"type": "adaptive"}) == 20000
    # Disabled / omitted thinking → left alone.
    assert _max_tokens_with_thinking("claude-sonnet-5", 4096, {"type": "disabled"}) == 4096
    assert _max_tokens_with_thinking("claude-opus-4-8", 4096, None) == 4096
    # Fable 5 always thinks, even when the param is omitted.
    assert _max_tokens_with_thinking(
        "claude-fable-5", 4096, None
    ) == _THINKING_MAX_TOKENS_FLOOR


def test_call_anthropic_api_raises_max_tokens_when_thinking_enabled():
    """Enabling thinking bumps the 4096 default so thinking tokens don't truncate output."""
    pytest.importorskip("anthropic")

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Translated text")]
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}, clear=False):
            os.environ.pop("TRANSLATE_THINKING", None)
            call_anthropic_api("Translate", model="claude-sonnet-5", enable_thinking=True)

        _, kwargs = mock_client.messages.create.call_args
        assert kwargs["max_tokens"] >= 8192


def test_call_anthropic_api_keeps_max_tokens_without_thinking():
    """Without thinking, the caller-provided max_tokens is left as-is."""
    pytest.importorskip("anthropic")

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Translated text")]
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}, clear=False):
            os.environ.pop("TRANSLATE_THINKING", None)
            call_anthropic_api(
                "Translate", model="claude-sonnet-5", max_tokens=4096, enable_thinking=False
            )

        _, kwargs = mock_client.messages.create.call_args
        assert kwargs["max_tokens"] == 4096


def test_extract_text_from_content_handles_none():
    """A None/empty content array yields '' rather than raising TypeError."""
    from src.api_translator import _extract_text_from_content
    assert _extract_text_from_content(None) == ""
    assert _extract_text_from_content([]) == ""


def test_call_anthropic_api_omits_thinking_for_older_model():
    """Older models default thinking off and may reject the param — omit it."""
    pytest.importorskip("anthropic")

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Translated text")]
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}, clear=False):
            os.environ.pop("TRANSLATE_THINKING", None)
            call_anthropic_api("Translate this text", model="claude-3-5-sonnet-20241022")

        _, kwargs = mock_client.messages.create.call_args
        assert "thinking" not in kwargs


# ============================================================================
# OpenAI API Tests
# ============================================================================


def test_call_openai_api_success():
    """Test successful OpenAI API call."""
    pytest.importorskip("openai")

    with patch('openai.OpenAI') as mock_openai_class:
        mock_client = Mock()
        mock_response = Mock()
        mock_response.choices = [Mock(message=Mock(content="Es una verdad universalmente reconocida..."))]
        mock_client.chat.completions.create.return_value = mock_response
        mock_openai_class.return_value = mock_client

        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}):
            result = call_openai_api("Translate this text")

        assert result == "Es una verdad universalmente reconocida..."
        mock_client.chat.completions.create.assert_called_once()


def test_call_openai_api_rate_limit():
    """Test rate limit error handling."""
    openai = pytest.importorskip("openai")

    with patch('openai.OpenAI') as mock_openai_class:
        mock_client = Mock()
        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.headers = {}
        mock_client.chat.completions.create.side_effect = openai.RateLimitError(
            "Rate limit exceeded", response=mock_response, body={}
        )
        mock_openai_class.return_value = mock_client

        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}):
            with pytest.raises(RateLimitError, match="rate limit"):
                call_openai_api("Test prompt")


# ============================================================================
# Real-time Translation Tests
# ============================================================================


@patch('src.api_translator._dispatch_llm_call')
def test_translate_chunk_realtime_anthropic(mock_dispatch, sample_chunk):
    """Test real-time translation with Anthropic."""
    mock_dispatch.return_value = "Es una verdad universalmente reconocida que un hombre soltero en posesión de una gran fortuna debe estar necesitado de esposa."

    updated_chunk = translate_chunk_realtime(
        chunk=sample_chunk,
        provider='anthropic',
        model='claude-3-5-sonnet-20241022'
    )

    assert updated_chunk.translated_text is not None
    assert "verdad universalmente reconocida" in updated_chunk.translated_text
    assert updated_chunk.status == ChunkStatus.TRANSLATED
    assert updated_chunk.translated_at is not None
    mock_dispatch.assert_called_once()


@patch('src.api_translator._dispatch_llm_call')
def test_translate_chunk_realtime_threads_project_slug_and_chunk_id(mock_dispatch, sample_chunk):
    """The realtime path must forward project_slug and chunk.id to the
    dispatcher so the resulting prompt log is self-identifying."""
    mock_dispatch.return_value = "Una traducción."

    translate_chunk_realtime(
        chunk=sample_chunk,
        provider='anthropic',
        model='claude-3-5-sonnet-20241022',
        project_slug='my-book-slug',
    )

    kwargs = mock_dispatch.call_args.kwargs
    assert kwargs.get("chunk_id") == sample_chunk.id
    assert kwargs.get("project_slug") == "my-book-slug"


@patch('src.api_translator._dispatch_llm_call')
def test_translate_chunk_realtime_openai(mock_dispatch, sample_chunk):
    """Test real-time translation with OpenAI."""
    mock_dispatch.return_value = "Es una verdad universalmente reconocida..."

    updated_chunk = translate_chunk_realtime(
        chunk=sample_chunk,
        provider='openai',
        model='gpt-4o'
    )

    assert updated_chunk.translated_text is not None
    assert updated_chunk.status == ChunkStatus.TRANSLATED
    mock_dispatch.assert_called_once()


@patch('src.api_translator._dispatch_llm_call')
def test_translate_chunk_with_retry(mock_dispatch, sample_chunk):
    """Test retry logic on temporary failure."""
    mock_dispatch.side_effect = [
        RateLimitError("Rate limit"),
        "Es una verdad universalmente reconocida..."
    ]

    with patch('time.sleep'):
        updated_chunk = translate_chunk_realtime(
            chunk=sample_chunk,
            provider='anthropic',
            model='claude-3-5-sonnet-20241022',
            max_retries=3
        )

    assert updated_chunk.translated_text is not None
    assert mock_dispatch.call_count == 2


@patch('src.api_translator._dispatch_llm_call')
def test_translate_chunk_max_retries_exceeded(mock_dispatch, sample_chunk):
    """Test failure after max retries."""
    mock_dispatch.side_effect = RateLimitError("Rate limit")

    with patch('time.sleep'):
        with pytest.raises(RateLimitError):
            translate_chunk_realtime(
                chunk=sample_chunk,
                provider='anthropic',
                model='claude-3-5-sonnet-20241022',
                max_retries=3
            )

    assert mock_dispatch.call_count == 3


# ============================================================================
# Batch Job Tracking Tests
# ============================================================================


def test_save_and_load_batch_jobs(tmp_path):
    """Test saving and loading batch jobs."""
    tracking_file = tmp_path / "batch_jobs.json"

    # Save a job
    job_info = {
        "job_id": "batch_123",
        "provider": "anthropic",
        "model": "claude-3-5-sonnet-20241022",
        "submitted_at": "2025-11-12T10:00:00",
        "status": "in_progress",
        "chunk_count": 5,
        "chunk_ids": ["ch01_001", "ch01_002"],
        "output_dir": "chunks/translated"
    }

    save_batch_job(job_info, tracking_file)

    # Load jobs
    jobs = load_batch_jobs(tracking_file)

    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "batch_123"
    assert jobs[0]["provider"] == "anthropic"


def test_get_batch_job(tmp_path):
    """Test retrieving specific batch job."""
    tracking_file = tmp_path / "batch_jobs.json"

    # Save two jobs
    job1 = {"job_id": "batch_123", "provider": "anthropic", "chunk_count": 5}
    job2 = {"job_id": "batch_456", "provider": "openai", "chunk_count": 10}

    save_batch_job(job1, tracking_file)
    save_batch_job(job2, tracking_file)

    # Get specific job
    job = get_batch_job("batch_456", tracking_file)

    assert job is not None
    assert job["job_id"] == "batch_456"
    assert job["provider"] == "openai"


def test_get_batch_job_not_found(tmp_path):
    """Test getting non-existent batch job."""
    tracking_file = tmp_path / "batch_jobs.json"

    job = get_batch_job("nonexistent", tracking_file)

    assert job is None


def test_load_batch_jobs_empty_file(tmp_path):
    """Test loading from non-existent file."""
    tracking_file = tmp_path / "batch_jobs.json"

    jobs = load_batch_jobs(tracking_file)

    assert jobs == []


# ============================================================================
# Batch API Tests (Mocked)
# ============================================================================


def test_submit_anthropic_batch(sample_chunk, tmp_path):
    """Test submitting batch to Anthropic."""
    pytest.importorskip("anthropic")

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_batch = Mock()
        mock_batch.id = "batch_abc123"
        mock_batch.processing_status = "in_progress"
        mock_client.messages.batches.create.return_value = mock_batch
        mock_anthropic_class.return_value = mock_client

        output_dir = tmp_path / "translated"

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            job_info = submit_batch(
                chunks=[sample_chunk],
                provider='anthropic',
                model='claude-3-5-sonnet-20241022',
                output_dir=output_dir
            )

        assert job_info["job_id"] == "batch_abc123"
        assert job_info["provider"] == "anthropic"
        assert job_info["chunk_count"] == 1
        assert job_info["status"] == "in_progress"
        mock_client.messages.batches.create.assert_called_once()


def test_submit_anthropic_batch_omits_temperature_for_sonnet_5(sample_chunk, tmp_path):
    """Batch requests for a model that 400s on sampling params must omit temperature."""
    pytest.importorskip("anthropic")

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_batch = Mock()
        mock_batch.id = "batch_abc123"
        mock_batch.processing_status = "in_progress"
        mock_client.messages.batches.create.return_value = mock_batch
        mock_anthropic_class.return_value = mock_client

        output_dir = tmp_path / "translated"

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}, clear=False):
            os.environ.pop("TRANSLATE_THINKING", None)
            submit_batch(
                chunks=[sample_chunk],
                provider='anthropic',
                model='claude-sonnet-5',
                output_dir=output_dir
            )

        _, kwargs = mock_client.messages.batches.create.call_args
        request_params = kwargs["requests"][0]["params"]
        assert "temperature" not in request_params
        assert request_params["model"] == "claude-sonnet-5"
        # Sonnet 5 defaults thinking ON when omitted — batch must disable it.
        assert request_params["thinking"] == {"type": "disabled"}


def test_submit_openai_batch(sample_chunk, tmp_path):
    """Test submitting batch to OpenAI."""
    pytest.importorskip("openai")

    with patch('openai.OpenAI') as mock_openai_class:
        mock_client = Mock()
        mock_file = Mock()
        mock_file.id = "file_xyz789"
        mock_batch = Mock()
        mock_batch.id = "batch_def456"
        mock_batch.status = "validating"

        mock_client.files.create.return_value = mock_file
        mock_client.batches.create.return_value = mock_batch
        mock_openai_class.return_value = mock_client

        output_dir = tmp_path / "translated"

        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}):
            job_info = submit_batch(
                chunks=[sample_chunk],
                provider='openai',
                model='gpt-4o',
                output_dir=output_dir
            )

        assert job_info["job_id"] == "batch_def456"
        assert job_info["provider"] == "openai"
        assert job_info["chunk_count"] == 1
        mock_client.batches.create.assert_called_once()


def test_check_anthropic_batch_status():
    """Test checking Anthropic batch status."""
    pytest.importorskip("anthropic")

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_batch = Mock()
        mock_batch.id = "batch_abc123"
        mock_batch.processing_status = "ended"
        mock_batch.request_counts = Mock(
            processing=0,
            succeeded=10,
            errored=0
        )
        mock_client.messages.batches.retrieve.return_value = mock_batch
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            status_info = check_batch_status("batch_abc123", "anthropic")

        assert status_info["job_id"] == "batch_abc123"
        assert status_info["status"] == "ended"
        assert status_info["succeeded_count"] == 10
        assert status_info["failed_count"] == 0


def test_check_openai_batch_status():
    """Test checking OpenAI batch status."""
    pytest.importorskip("openai")

    with patch('openai.OpenAI') as mock_openai_class:
        mock_client = Mock()
        mock_batch = Mock()
        mock_batch.id = "batch_def456"
        mock_batch.status = "completed"
        mock_batch.completed_at = "2025-11-13T10:00:00"
        mock_batch.request_counts = Mock(
            total=10,
            completed=10,
            failed=0
        )
        mock_client.batches.retrieve.return_value = mock_batch
        mock_openai_class.return_value = mock_client

        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}):
            status_info = check_batch_status("batch_def456", "openai")

        assert status_info["job_id"] == "batch_def456"
        assert status_info["status"] == "completed"
        assert status_info["succeeded_count"] == 10


# ============================================================================
# Batch Result Retrieval Tests (Mocked)
# ============================================================================


def test_retrieve_anthropic_batch_results(sample_chunk, tmp_path):
    """Test retrieving results from Anthropic batch."""
    pytest.importorskip("anthropic")

    # Mock a successful result
    mock_result = Mock()
    mock_result.custom_id = sample_chunk.id
    mock_result.result.type = "succeeded"
    mock_message = Mock()
    mock_content_block = Mock()
    mock_content_block.type = "text"
    mock_content_block.text = "Es una verdad universalmente reconocida..."
    mock_message.content = [mock_content_block]
    mock_result.result.message = mock_message

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        # Status check: return "ended"
        mock_batch = Mock()
        mock_batch.id = "batch_abc123"
        mock_batch.processing_status = "ended"
        mock_batch.request_counts = Mock(processing=0, succeeded=1, errored=0)
        mock_client.messages.batches.retrieve.return_value = mock_batch
        # Results: return our mock
        mock_client.messages.batches.results.return_value = [mock_result]
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with patch('src.api_translator.log_prompt'):
                chunks = retrieve_batch_results(
                    job_id="batch_abc123",
                    provider="anthropic",
                    original_chunks=[sample_chunk],
                    output_dir=tmp_path,
                    model="claude-sonnet-4-6",
                )

    assert len(chunks) == 1
    assert chunks[0].translated_text == "Es una verdad universalmente reconocida..."
    assert chunks[0].status == ChunkStatus.TRANSLATED
    assert chunks[0].translated_at is not None


def test_retrieve_anthropic_batch_results_with_thinking_block(sample_chunk, tmp_path):
    """Sonnet 5 with extended thinking returns a ThinkingBlock before the
    TextBlock. Retrieval must skip it, not crash on
    'ThinkingBlock' object has no attribute 'text'."""
    pytest.importorskip("anthropic")

    # First block is a thinking block with no `.text`; second is the answer.
    thinking_block = Mock(spec=["type", "thinking"])
    thinking_block.type = "thinking"
    thinking_block.thinking = "Let me consider the register and dialogue..."
    text_block = Mock()
    text_block.type = "text"
    text_block.text = "Es una verdad universalmente reconocida..."

    mock_message = Mock()
    mock_message.content = [thinking_block, text_block]
    mock_result = Mock()
    mock_result.custom_id = sample_chunk.id
    mock_result.result.type = "succeeded"
    mock_result.result.message = mock_message

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_batch = Mock()
        mock_batch.id = "batch_thinking"
        mock_batch.processing_status = "ended"
        mock_batch.request_counts = Mock(processing=0, succeeded=1, errored=0)
        mock_client.messages.batches.retrieve.return_value = mock_batch
        mock_client.messages.batches.results.return_value = [mock_result]
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with patch('src.api_translator.log_prompt'):
                chunks = retrieve_batch_results(
                    job_id="batch_thinking",
                    provider="anthropic",
                    original_chunks=[sample_chunk],
                    output_dir=tmp_path,
                    model="claude-sonnet-5",
                )

    assert len(chunks) == 1
    assert chunks[0].translated_text == "Es una verdad universalmente reconocida..."
    assert chunks[0].status == ChunkStatus.TRANSLATED


def test_retrieve_openai_batch_results(sample_chunk, tmp_path):
    """Test retrieving results from OpenAI batch."""
    pytest.importorskip("openai")

    # Build JSONL output content
    result_line = json.dumps({
        "custom_id": sample_chunk.id,
        "response": {
            "status_code": 200,
            "body": {
                "choices": [{
                    "message": {"content": "Es una verdad universalmente reconocida..."}
                }]
            }
        }
    })

    mock_output_content = Mock()
    mock_output_content.text = result_line

    with patch('openai.OpenAI') as mock_openai_class:
        mock_client = Mock()
        # Status check: return "completed"
        mock_batch = Mock()
        mock_batch.id = "batch_def456"
        mock_batch.status = "completed"
        mock_batch.completed_at = "2025-11-13T10:00:00"
        mock_batch.request_counts = Mock(total=1, completed=1, failed=0)
        mock_batch.output_file_id = "file_out_123"
        mock_client.batches.retrieve.return_value = mock_batch
        mock_client.files.content.return_value = mock_output_content
        mock_openai_class.return_value = mock_client

        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}):
            with patch('src.api_translator.log_prompt'):
                chunks = retrieve_batch_results(
                    job_id="batch_def456",
                    provider="openai",
                    original_chunks=[sample_chunk],
                    output_dir=tmp_path,
                    model="gpt-4o",
                )

    assert len(chunks) == 1
    assert chunks[0].translated_text == "Es una verdad universalmente reconocida..."
    assert chunks[0].status == ChunkStatus.TRANSLATED


def test_retrieve_batch_not_complete(sample_chunk, tmp_path):
    """Test that retrieval raises when batch is not complete."""
    pytest.importorskip("anthropic")

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_batch = Mock()
        mock_batch.id = "batch_abc123"
        mock_batch.processing_status = "in_progress"
        mock_batch.request_counts = Mock(processing=5, succeeded=0, errored=0)
        mock_client.messages.batches.retrieve.return_value = mock_batch
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            with pytest.raises(APIError, match="not complete"):
                retrieve_batch_results(
                    job_id="batch_abc123",
                    provider="anthropic",
                    original_chunks=[sample_chunk],
                    output_dir=tmp_path,
                )


def test_retrieve_mutates_submission_log(sample_chunk, tmp_path):
    """Batch retrieval should fill in the response on the existing submission
    log (in place) rather than writing a separate translation_result log."""
    pytest.importorskip("anthropic")

    # Stand up a fake submission log on disk, matching what a real submission
    # would have produced: prompt populated, response=None.
    submission_log = tmp_path / "20260101_000000_translation_abc123.json"
    submission_log.write_text(
        json.dumps({
            "metadata": {
                "timestamp": "2026-01-01T00:00:00",
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "call_type": "translation",
                "mode": "batch",
                "batch_job_id": "batch_log_test",
                "chunk_id": sample_chunk.id,
            },
            "prompt": "The original prompt for this chunk",
            "response": None,
        }),
        encoding="utf-8",
    )

    mock_result = Mock()
    mock_result.custom_id = sample_chunk.id
    mock_result.result.type = "succeeded"
    mock_content_block = Mock()
    mock_content_block.type = "text"
    mock_content_block.text = "Translated text here"
    mock_result.result.message = Mock(content=[mock_content_block])

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_batch = Mock()
        mock_batch.id = "batch_log_test"
        mock_batch.processing_status = "ended"
        mock_batch.request_counts = Mock(processing=0, succeeded=1, errored=0)
        mock_client.messages.batches.retrieve.return_value = mock_batch
        mock_client.messages.batches.results.return_value = [mock_result]
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            # Bypass the repo-relative path resolution by patching the resolver
            # to return our tmp_path log directly.
            with patch(
                'src.api_translator._resolve_submission_log_path',
                return_value=submission_log,
            ), patch('src.api_translator.log_prompt') as mock_log:
                retrieve_batch_results(
                    job_id="batch_log_test",
                    provider="anthropic",
                    original_chunks=[sample_chunk],
                    output_dir=tmp_path,
                    model="claude-sonnet-4-6",
                    chunk_log_map={sample_chunk.id: "fake/path"},
                )

    # No new log file should have been written — the submission log is updated
    # in place instead.
    mock_log.assert_not_called()

    updated = json.loads(submission_log.read_text(encoding="utf-8"))
    assert updated["prompt"] == "The original prompt for this chunk"
    assert updated["response"] == "Translated text here"
    assert "retrieved_at" in updated["metadata"]


def test_update_batch_job_status(tmp_path):
    """Test updating batch job status in tracking file."""
    tracking_file = tmp_path / "batch_jobs.json"

    job_info = {
        "job_id": "batch_123",
        "provider": "anthropic",
        "status": "in_progress",
    }
    save_batch_job(job_info, tracking_file)

    update_batch_job_status("batch_123", "completed", tracking_file)

    jobs = load_batch_jobs(tracking_file)
    assert jobs[0]["status"] == "completed"
    assert "completed_at" in jobs[0]


def test_submit_translation_job_returns_job_info_without_polling(sample_chunk, tmp_path):
    """submit_translation_job should call batches.create but never poll."""
    pytest.importorskip("anthropic")

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_batch = Mock()
        mock_batch.id = "batch_submit_only"
        mock_batch.processing_status = "in_progress"
        mock_client.messages.batches.create.return_value = mock_batch
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            job_info = submit_translation_job(
                work_chunks=[sample_chunk],
                model_id='claude-sonnet-4-6',
                prov='anthropic',
                output_dir=tmp_path,
            )

        assert job_info["job_id"] == "batch_submit_only"
        assert job_info["provider"] == "anthropic"
        assert job_info["model"] == "claude-sonnet-4-6"
        mock_client.messages.batches.create.assert_called_once()
        # Crucially, no status retrieval happened — that's await_translation_job's job.
        mock_client.messages.batches.retrieve.assert_not_called()


def test_await_translation_job_polls_then_retrieves(sample_chunk, tmp_path):
    """await_translation_job should poll until 'ended' then call retrieve_batch_results."""
    job_info = {
        "job_id": "batch_await_test",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "chunk_log_map": {sample_chunk.id: "prompts/history/fake.json"},
    }

    translated_sample = sample_chunk.model_copy(update={"translated_text": "traducción"})

    with patch('src.api_translator.check_batch_status') as mock_check, \
         patch('src.api_translator.retrieve_batch_results') as mock_retrieve, \
         patch('time.sleep'):  # don't actually sleep between polls
        # First poll: still running. Second poll: ended.
        mock_check.side_effect = [
            {"status": "in_progress"},
            {"status": "ended"},
        ]
        mock_retrieve.return_value = [translated_sample]

        result = await_translation_job(
            job_info, [sample_chunk], tmp_path,
            poll_interval_seconds=0,  # explicit no-wait for test speed
            max_polls=5,
        )

        assert result == [translated_sample]
        assert mock_check.call_count == 2
        mock_retrieve.assert_called_once()
        retrieve_kwargs = mock_retrieve.call_args.kwargs
        assert retrieve_kwargs["model"] == "claude-sonnet-4-6"
        assert retrieve_kwargs["chunk_log_map"] == {sample_chunk.id: "prompts/history/fake.json"}


def test_await_translation_job_raises_on_failed_batch(sample_chunk, tmp_path):
    """await_translation_job should raise APIError if the batch ends in a failed state."""
    job_info = {
        "job_id": "batch_failed",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
    }

    with patch('src.api_translator.check_batch_status') as mock_check, \
         patch('src.api_translator.retrieve_batch_results') as mock_retrieve, \
         patch('time.sleep'):
        mock_check.return_value = {"status": "failed"}

        with pytest.raises(APIError, match="failed"):
            await_translation_job(
                job_info, [sample_chunk], tmp_path,
                poll_interval_seconds=0, max_polls=5,
            )
        mock_retrieve.assert_not_called()


def test_batch_submission_filters_glossary(sample_chunk, tmp_path):
    """Test that batch submission filters glossary per chunk."""
    pytest.importorskip("anthropic")

    glossary = Glossary(terms=[
        GlossaryTerm(english="truth", spanish="verdad", type=GlossaryTermType.CONCEPT),
        GlossaryTerm(english="Hogwarts", spanish="Hogwarts", type=GlossaryTermType.PLACE),
    ])

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_batch = Mock()
        mock_batch.id = "batch_filter_test"
        mock_batch.processing_status = "in_progress"
        mock_client.messages.batches.create.return_value = mock_batch
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
            submit_batch(
                chunks=[sample_chunk],
                provider='anthropic',
                model='claude-sonnet-4-6',
                output_dir=tmp_path,
                glossary=glossary,
            )

        # Check the prompt in the batch request
        call_args = mock_client.messages.batches.create.call_args
        requests = call_args[1]["requests"]
        prompt_text = requests[0]["params"]["messages"][0]["content"]

        # "truth" appears in the chunk's source_text, "Hogwarts" does not
        assert "truth" in prompt_text.lower() or "verdad" in prompt_text.lower()
        # Hogwarts should NOT appear in the glossary section since it's not in the source text
        # (The word "Hogwarts" won't be in the filtered glossary)
        # We check that the full unfiltered glossary wasn't used by verifying
        # "Hogwarts" doesn't appear in a glossary context
        assert "Hogwarts" not in prompt_text


# ============================================================================
# Provenance stamp (chunk.last_llm_log) — see docs/EDIT_REVIEW.md
# ============================================================================
#
# These tests exist because the edit-review report uses chunk.last_llm_log
# as its O(1) lookup from a chunk to the LLM call that produced its
# translation. If any LLM-write site stops stamping, the feature silently
# degrades to the heuristic chunk-id scan (or the "no baseline" banner) and
# nothing else fails. So each write site needs an explicit guard test.


def test_translate_chunk_realtime_stamps_last_llm_log(sample_chunk):
    """The realtime path must stamp chunk.last_llm_log with the path of the
    prompt log it just wrote. Patches the underlying SDK call (not
    _dispatch_llm_call) so the real log_prompt + last_log_path plumbing runs.
    """
    pytest.importorskip("anthropic")

    with patch('src.api_translator.call_anthropic_api',
               return_value="Una traducción cualquiera."), \
         patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}):
        updated = translate_chunk_realtime(
            chunk=sample_chunk,
            provider='anthropic',
            model='claude-sonnet-4-6',
            project_slug='my-book',
        )

    assert updated.last_llm_log, "Realtime translation must stamp last_llm_log"
    stamp_path = Path(updated.last_llm_log)
    assert stamp_path.exists(), (
        f"Stamped log {stamp_path} should exist on disk "
        "(isolated tmp history from conftest)"
    )
    record = json.loads(stamp_path.read_text(encoding="utf-8"))
    assert record["response"] == "Una traducción cualquiera."
    assert record["metadata"]["chunk_id"] == sample_chunk.id
    assert record["metadata"]["project_slug"] == "my-book"
    assert record["metadata"]["call_type"] == "translation"


def _write_submission_log(path: Path, *, provider: str, model: str,
                          job_id: str, chunk_id: str) -> None:
    """Helper: stand up a submission-time prompt log on disk, matching the
    shape submit_batch would write (response=None, batch_job_id set)."""
    path.write_text(
        json.dumps({
            "metadata": {
                "timestamp": "2026-01-01T00:00:00",
                "provider": provider,
                "model": model,
                "call_type": "translation",
                "mode": "batch",
                "batch_job_id": job_id,
                "chunk_id": chunk_id,
            },
            "prompt": "The original prompt for this chunk",
            "response": None,
        }),
        encoding="utf-8",
    )


def test_retrieve_anthropic_batch_stamps_last_llm_log(sample_chunk, tmp_path):
    """Anthropic batch retrieval must stamp chunk.last_llm_log on every
    successfully translated chunk — and the stamp must point at the
    submission log that retrieval mutated in place."""
    pytest.importorskip("anthropic")

    submission_log = tmp_path / "20260101_000000_translation_anth_stamp.json"
    _write_submission_log(
        submission_log,
        provider="anthropic", model="claude-sonnet-4-6",
        job_id="batch_stamp_anthropic", chunk_id=sample_chunk.id,
    )

    mock_content_block = Mock()
    mock_content_block.type = "text"
    mock_content_block.text = "Translated text via Anthropic batch."
    mock_result = Mock()
    mock_result.custom_id = sample_chunk.id
    mock_result.result.type = "succeeded"
    mock_result.result.message = Mock(content=[mock_content_block])

    with patch('anthropic.Anthropic') as mock_anthropic_class:
        mock_client = Mock()
        mock_batch = Mock()
        mock_batch.id = "batch_stamp_anthropic"
        mock_batch.processing_status = "ended"
        mock_batch.request_counts = Mock(processing=0, succeeded=1, errored=0)
        mock_client.messages.batches.retrieve.return_value = mock_batch
        mock_client.messages.batches.results.return_value = [mock_result]
        mock_anthropic_class.return_value = mock_client

        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'test-key'}), \
             patch('src.api_translator._resolve_submission_log_path',
                   return_value=submission_log):
            chunks = retrieve_batch_results(
                job_id="batch_stamp_anthropic",
                provider="anthropic",
                original_chunks=[sample_chunk],
                output_dir=tmp_path,
                model="claude-sonnet-4-6",
                chunk_log_map={sample_chunk.id: "fake/path"},
            )

    assert len(chunks) == 1
    assert chunks[0].last_llm_log, "Anthropic batch retrieval must stamp last_llm_log"
    assert Path(chunks[0].last_llm_log).name == submission_log.name


def test_retrieve_openai_batch_stamps_last_llm_log(sample_chunk, tmp_path):
    """OpenAI batch retrieval must stamp chunk.last_llm_log identically to
    the Anthropic path. Same contract; symmetric SDK shape."""
    pytest.importorskip("openai")

    submission_log = tmp_path / "20260101_000000_translation_oai_stamp.json"
    _write_submission_log(
        submission_log,
        provider="openai", model="gpt-4o",
        job_id="batch_stamp_openai", chunk_id=sample_chunk.id,
    )

    result_line = json.dumps({
        "custom_id": sample_chunk.id,
        "response": {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"content": "Translated via OpenAI batch."}}],
            },
        },
    })
    mock_output_content = Mock()
    mock_output_content.text = result_line

    with patch('openai.OpenAI') as mock_openai_class:
        mock_client = Mock()
        mock_batch = Mock()
        mock_batch.id = "batch_stamp_openai"
        mock_batch.status = "completed"
        mock_batch.completed_at = "2026-01-01T01:00:00"
        mock_batch.request_counts = Mock(total=1, completed=1, failed=0)
        mock_batch.output_file_id = "file_out_stamp"
        mock_client.batches.retrieve.return_value = mock_batch
        mock_client.files.content.return_value = mock_output_content
        mock_openai_class.return_value = mock_client

        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}), \
             patch('src.api_translator._resolve_submission_log_path',
                   return_value=submission_log):
            chunks = retrieve_batch_results(
                job_id="batch_stamp_openai",
                provider="openai",
                original_chunks=[sample_chunk],
                output_dir=tmp_path,
                model="gpt-4o",
                chunk_log_map={sample_chunk.id: "fake/path"},
            )

    assert len(chunks) == 1
    assert chunks[0].last_llm_log, "OpenAI batch retrieval must stamp last_llm_log"
    assert Path(chunks[0].last_llm_log).name == submission_log.name


# ============================================================================
# Translation seam extract — regression guard (harness Phase B eng review A1)
# ============================================================================
#
# build_translation_prompt + apply_translation were factored OUT of
# translate_chunk_realtime so the harness subagent backend can render the SAME
# prompt and stamp chunks the SAME way. These tests pin the extract: the
# realtime path must send byte-identical output to build_translation_prompt,
# and the stamp must match the inline behavior it replaced. If either drifts,
# every existing translation silently changes.


def _seam_glossary():
    return Glossary(terms=[
        GlossaryTerm(english="truth", spanish="verdad", type=GlossaryTermType.CONCEPT),
        GlossaryTerm(english="Hogwarts", spanish="Hogwarts", type=GlossaryTermType.PLACE),
    ])


def _seam_style_guide():
    return StyleGuide(content="TONE: formal. DIALECT: neutral Latin American Spanish.")


def test_realtime_sends_exactly_build_translation_prompt(sample_chunk):
    """REGRESSION: the realtime path must send byte-identical output to what
    build_translation_prompt produces for the same inputs."""
    glossary = _seam_glossary()
    style_guide = _seam_style_guide()
    prev = "El capitulo anterior termino asi."

    expected = build_translation_prompt(
        sample_chunk,
        glossary=glossary,
        style_guide=style_guide,
        project_name="Pride and Prejudice",
        source_language="English",
        target_language="Spanish",
        previous_chapter_context=prev,
    )

    with patch("src.api_translator.call_llm", return_value="una traduccion") as mock_call:
        translate_chunk_realtime(
            chunk=sample_chunk,
            provider="anthropic",
            model="claude-sonnet-4-6",
            glossary=glossary,
            style_guide=style_guide,
            project_name="Pride and Prejudice",
            previous_chapter_context=prev,
        )

    sent_prompt = mock_call.call_args.args[0]
    assert sent_prompt == expected, "realtime path drifted from build_translation_prompt"


def test_build_translation_prompt_strips_header(sample_chunk):
    """The builder strips any header before the first 80-'=' separator and keeps
    the source text."""
    prompt = build_translation_prompt(sample_chunk)
    separator = "=" * 80
    if separator in prompt:
        assert prompt.startswith(separator), "header before the separator must be stripped"
    assert "truth" in prompt.lower()


def test_build_translation_prompt_includes_dialogue_block_when_source_has_dialogue():
    """build_translation_prompt must embed the framed DIALOGUE FORMATTING block
    when the source chunk contains dialogue and the target language is Spanish."""
    # Build a chunk whose source starts with a straight-quote dialogue opener
    # so that _source_has_dialogue returns True.
    dialogue_text = (
        chr(34) + "We must leave at once," + chr(34) + " she said.\n\n"
        "He nodded and reached for his coat."
    )
    chunk = Chunk(
        id="ch01_chunk_dlg",
        chapter_id="chapter_01",
        position=1,
        source_text=dialogue_text,
        metadata=ChunkMetadata(
            char_start=0,
            char_end=len(dialogue_text),
            overlap_start=0,
            overlap_end=0,
            paragraph_count=2,
            word_count=len(dialogue_text.split()),
        ),
        status=ChunkStatus.PENDING,
        created_at=datetime(2025, 1, 28, 10, 0, 0),
    )
    prompt = build_translation_prompt(chunk, target_language="Spanish")
    # The injected block is framed with "=" * 80 + "\nDIALOGUE FORMATTING\n" + "=" * 80.
    framed_marker = "=" * 80 + "\nDIALOGUE FORMATTING\n" + "=" * 80
    assert framed_marker in prompt, (
        "Prompt for a dialogue chunk must contain the framed DIALOGUE FORMATTING section"
    )


def test_build_translation_prompt_omits_dialogue_block_for_narration():
    """build_translation_prompt must NOT embed the framed DIALOGUE FORMATTING
    section when the source is pure narration (no dialogue paragraphs).

    Note: the template body references "DIALOGUE FORMATTING" in prose, so we
    look for the distinctive 80-char separator that frames the injected block,
    not the bare string.
    """
    narration = "He walked slowly down the lane.\n\nThe trees were silent."
    chunk = Chunk(
        id="ch01_chunk_nar",
        chapter_id="chapter_01",
        position=2,
        source_text=narration,
        metadata=ChunkMetadata(
            char_start=0,
            char_end=len(narration),
            overlap_start=0,
            overlap_end=0,
            paragraph_count=2,
            word_count=len(narration.split()),
        ),
        status=ChunkStatus.PENDING,
        created_at=datetime(2025, 1, 28, 10, 0, 0),
    )
    prompt = build_translation_prompt(chunk, target_language="Spanish")
    # The framed block starts with "=" * 80 + "\nDIALOGUE FORMATTING\n" + "=" * 80.
    # The template itself also contains "=" * 80 separators but NOT immediately
    # followed by "DIALOGUE FORMATTING\n" + "=" * 80 — that pattern only appears
    # when the injected block is non-empty.
    framed_marker = "=" * 80 + "\nDIALOGUE FORMATTING\n" + "=" * 80
    assert framed_marker not in prompt, (
        "Prompt for a narration-only chunk must NOT contain the framed DIALOGUE FORMATTING block"
    )


# ----------------------------------------------------------------------------
# Cache-prefix opt-ins: always_include_dialogue / always_include_image_instructions.
# The reorder puts the fixed sections up front and per-chunk variable content last;
# these flags force the (otherwise conditional) dialogue block and image bullet into
# that fixed prefix so it is byte-identical across a book and stays cacheable.
# ----------------------------------------------------------------------------

_DIALOGUE_FRAMED_MARKER = "=" * 80 + "\nDIALOGUE FORMATTING\n" + "=" * 80
# The GLOSSARY TERMS separator marks the boundary between the fixed prefix (A/B)
# and the per-chunk variable suffix (C) after the reorder.
_GLOSSARY_MARKER = "=" * 80 + "\nGLOSSARY TERMS\n" + "=" * 80


def _mk_chunk(cid: str, source: str) -> Chunk:
    return Chunk(
        id=cid,
        chapter_id="chapter_01",
        position=1,
        source_text=source,
        metadata=ChunkMetadata(
            char_start=0,
            char_end=len(source),
            overlap_start=0,
            overlap_end=0,
            paragraph_count=max(1, source.count("\n\n") + 1),
            word_count=len(source.split()),
        ),
        status=ChunkStatus.PENDING,
        created_at=datetime(2025, 1, 28, 10, 0, 0),
    )


def test_build_translation_prompt_always_include_dialogue_narration():
    """With always_include_dialogue, a narration-only Spanish chunk carries the
    framed DIALOGUE FORMATTING block (it moves into the fixed cacheable prefix)."""
    chunk = _mk_chunk("ch01_nar", "He walked slowly down the lane.\n\nThe trees were silent.")
    prompt = build_translation_prompt(
        chunk, target_language="Spanish", always_include_dialogue=True
    )
    assert _DIALOGUE_FRAMED_MARKER in prompt


def test_build_translation_prompt_always_include_dialogue_non_spanish_still_omits():
    """The Spanish gate wins: always_include_dialogue does NOT inject the block for
    a non-Spanish target."""
    chunk = _mk_chunk("ch01_nar", "He walked slowly down the lane.\n\nThe trees were silent.")
    prompt = build_translation_prompt(
        chunk, target_language="French", always_include_dialogue=True
    )
    assert _DIALOGUE_FRAMED_MARKER not in prompt


def test_build_translation_prompt_image_bullet_makes_prefix_byte_identical():
    """always_include_image_instructions makes the fixed prefix (everything before
    the GLOSSARY TERMS section) byte-identical across an image-bearing chunk and a
    plain chunk of the same book — the whole point of the book-level constant."""
    img_chunk = _mk_chunk("ch01_img", "Para one.\n\n[IMAGE:images/i01.jpg]\n\nPara two.")
    plain_chunk = _mk_chunk("ch01_plain", "Para one.\n\nPara two, no images at all.")

    def prefix(chunk):
        # Both chunks are pure narration → no dialogue block in either; isolate the
        # image effect. Source text lives in the variable suffix, not the prefix.
        p = build_translation_prompt(
            chunk, target_language="Spanish", always_include_image_instructions=True
        )
        return p.split(_GLOSSARY_MARKER)[0]

    assert prefix(img_chunk) == prefix(plain_chunk)
    # And the constant image bullet is actually present in that shared prefix.
    assert "[IMAGE:filename.ext:image description]" in prefix(img_chunk)


def test_build_translation_prompt_image_bullet_off_fragments_prefix():
    """Contrast: WITHOUT the flag the image bullet is per-chunk, so the fixed prefix
    differs between an image chunk and a plain chunk — the fragmentation the flag fixes."""
    img_chunk = _mk_chunk("ch01_img", "Para one.\n\n[IMAGE:images/i01.jpg]\n\nPara two.")
    plain_chunk = _mk_chunk("ch01_plain", "Para one.\n\nPara two, no images at all.")

    def prefix(chunk):
        p = build_translation_prompt(chunk, target_language="Spanish")
        return p.split(_GLOSSARY_MARKER)[0]

    assert prefix(img_chunk) != prefix(plain_chunk)


def test_build_translation_prompt_section_order_is_canonical():
    """Pin the cache-load-bearing section order so the runtime prompts/translation.txt
    and the committed prompts/translation.example.txt cannot silently diverge again.
    In particular the fixed OUTPUT FORMAT section must TRAIL after the variable suffix
    (glossary/context/source), not sit in the fixed prefix — the exact divergence that
    otherwise slips past the byte-identical prefix checks above. always_include_dialogue
    is set so the DIALOGUE FORMATTING block is present and its position is pinned too."""
    chunk = _mk_chunk("ch01_ord", "He walked slowly down the lane.\n\nThe trees were silent.")
    prompt = build_translation_prompt(
        chunk, target_language="Spanish", always_include_dialogue=True
    )

    def sep(name: str) -> str:
        return "=" * 80 + f"\n{name}\n" + "=" * 80

    order = [
        "BOOK TRANSLATION TASK",
        "STYLE GUIDE",
        "TRANSLATION INSTRUCTIONS",
        "DIALOGUE FORMATTING",
        "GLOSSARY TERMS",
        "BOOK CONTEXT",
        "SOURCE TEXT TO TRANSLATE",
        "OUTPUT FORMAT",
        "TRANSLATION",
    ]
    positions = {name: prompt.find(sep(name)) for name in order}
    assert all(pos != -1 for pos in positions.values()), positions
    assert list(positions.values()) == sorted(positions.values()), positions
    # The load-bearing invariant: OUTPUT FORMAT (fixed text) trails the variable source.
    assert positions["OUTPUT FORMAT"] > positions["SOURCE TEXT TO TRANSLATE"], positions


def test_build_translation_prompt_parts_round_trip():
    """prefix + suffix must equal the flat prompt; prefix ends right before GLOSSARY TERMS."""
    chunk = _mk_chunk("ch01_parts", "He walked slowly down the lane.\n\nThe trees were silent.")
    flat = build_translation_prompt(
        chunk,
        target_language="Spanish",
        always_include_dialogue=True,
        always_include_image_instructions=True,
    )
    prefix, suffix = build_translation_prompt_parts(
        chunk,
        target_language="Spanish",
        always_include_dialogue=True,
        always_include_image_instructions=True,
    )
    assert prefix + suffix == flat
    assert suffix.startswith("=" * 80 + "\nGLOSSARY TERMS")
    assert "GLOSSARY TERMS" not in prefix


def test_build_translation_prompt_parts_byte_identical_with_flags():
    """With both always_include flags on, two different chunks share a byte-identical prefix."""
    dialogue_text = (
        chr(34) + "We must leave at once," + chr(34) + " she said.\n\n"
        "He nodded and reached for his coat."
    )
    dialogue = _mk_chunk("ch01_dlg", dialogue_text)
    plain = _mk_chunk("ch01_plain", "He walked slowly down the lane.\n\nThe trees were silent.")
    p1, _ = build_translation_prompt_parts(
        dialogue,
        target_language="Spanish",
        always_include_dialogue=True,
        always_include_image_instructions=True,
    )
    p2, _ = build_translation_prompt_parts(
        plain,
        target_language="Spanish",
        always_include_dialogue=True,
        always_include_image_instructions=True,
    )
    assert p1 == p2
    assert p1  # non-empty cacheable prefix


def test_build_translation_prompt_parts_diverges_without_flags():
    """Without always_include_dialogue, a dialogue chunk and a plain chunk diverge in prefix."""
    dialogue_text = (
        chr(34) + "We must leave at once," + chr(34) + " she said.\n\n"
        "He nodded and reached for his coat."
    )
    dialogue = _mk_chunk("ch01_dlg", dialogue_text)
    plain = _mk_chunk("ch01_plain", "He walked slowly down the lane.\n\nThe trees were silent.")
    p1, _ = build_translation_prompt_parts(dialogue, target_language="Spanish")
    p2, _ = build_translation_prompt_parts(plain, target_language="Spanish")
    assert p1 != p2


def test_call_anthropic_api_cache_prefix_two_blocks():
    """cache_prefix yields two-block content with cache_control on block 0."""
    pytest.importorskip("anthropic")

    full = "PREFIX_STABLE\nVARIABLE_SUFFIX"
    prefix = "PREFIX_STABLE\n"

    with patch("anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="ok")]
        mock_usage = Mock()
        mock_usage.cache_read_input_tokens = 100
        mock_usage.cache_creation_input_tokens = 50
        mock_response.usage = mock_usage
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_class.return_value = mock_client

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            result = call_anthropic_api(full, cache_prefix=prefix)

        assert result == "ok"
        _, kwargs = mock_client.messages.create.call_args
        content = kwargs["messages"][0]["content"]
        assert isinstance(content, list)
        assert len(content) == 2
        assert content[0]["text"] == prefix
        assert content[0]["cache_control"] == {"type": "ephemeral"}
        assert content[1]["text"] == "VARIABLE_SUFFIX"
        usage = last_cache_usage()
        assert usage is not None
        assert usage["cache_read_input_tokens"] == 100
        assert usage["cache_creation_input_tokens"] == 50


def test_call_anthropic_api_without_cache_prefix_is_string():
    """No cache_prefix keeps the historical single-string content."""
    pytest.importorskip("anthropic")

    with patch("anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="ok")]
        mock_response.usage = Mock(
            cache_read_input_tokens=0, cache_creation_input_tokens=0
        )
        mock_client.messages.create.return_value = mock_response
        mock_anthropic_class.return_value = mock_client

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            call_anthropic_api("plain prompt")

        _, kwargs = mock_client.messages.create.call_args
        assert kwargs["messages"][0]["content"] == "plain prompt"


def test_summarize_chunk_features_counts():
    dialogue_text = (
        chr(34) + "We must leave at once," + chr(34) + " she said.\n\n"
        "He nodded and reached for his coat."
    )
    dialogue = _mk_chunk("c_dlg", dialogue_text)
    image = _mk_chunk("c_img", "Para one.\n\n[IMAGE:images/i01.jpg]\n\nPara two.")
    plain = _mk_chunk("c_plain", "He walked slowly down the lane.\n\nThe trees were silent.")
    summary = summarize_chunk_features([dialogue, image, plain])
    assert summary == {"total": 3, "dialogue": 1, "images": 1}


def test_estimate_cost_image_auto_and_feature_counts():
    image = _mk_chunk("c_img", "Para one.\n\n[IMAGE:images/i01.jpg]\n\nPara two.")
    plain = _mk_chunk("c_plain", "He walked slowly down the lane.\n\nThe trees were silent.")
    info = estimate_cost(
        [image, plain],
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
    )
    assert info["total_chunks"] == 2
    assert info["image_chunk_count"] == 1
    assert info["dialogue_chunk_count"] == 0
    # Auto image flag enlarges prompts vs forcing images off.
    info_off = estimate_cost(
        [image, plain],
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        always_include_image_instructions=False,
    )
    assert info["input_tokens"] >= info_off["input_tokens"]


def test_apply_translation_stamps(sample_chunk):
    """apply_translation strips + stamps text/status/timestamp; last_llm_log is
    set only when a log path is given and preserved (not cleared) when None."""
    assert sample_chunk.status == ChunkStatus.PENDING
    out = apply_translation(sample_chunk, "  una traduccion  ")
    assert out.translated_text == "una traduccion"   # stripped
    assert out.status == ChunkStatus.TRANSLATED
    assert out.translated_at is not None
    assert out.last_llm_log is None                  # no log path -> preserved (was None)

    with patch("src.api_translator.relative_log_path", return_value="prompts/history/x.json"):
        apply_translation(sample_chunk, "y", log_path=Path("whatever.json"))
    assert sample_chunk.last_llm_log == "prompts/history/x.json"

    # log_path=None with an existing value: must NOT clear the field (preserved).
    apply_translation(sample_chunk, "z", log_path=None)
    assert sample_chunk.last_llm_log == "prompts/history/x.json"  # still the prior value
