"""
Tests for API translation functionality.

Uses mocked API responses to avoid calling real APIs.
"""

import json
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

from src.models import Chunk, ChunkMetadata, ChunkStatus, Glossary, GlossaryTerm, GlossaryTermType, StyleGuide
from src.api_translator import (
    get_api_key,
    estimate_cost,
    translate_chunk_realtime,
    call_anthropic_api,
    call_openai_api,
    submit_batch,
    check_batch_status,
    save_batch_job,
    load_batch_jobs,
    get_batch_job,
    APIError,
    APIKeyError,
    RateLimitError,
)


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
        mock_response.content = [Mock(text="Es una verdad universalmente reconocida...")]
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
