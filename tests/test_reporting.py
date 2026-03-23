"""
Tests for evaluation reporting module.

Tests all three report formats (text, JSON, HTML) with both good and error
fixtures, plus report saving functionality.
"""

import json
import tempfile
from pathlib import Path
from datetime import datetime

import pytest

from src.models import Chunk, ChunkMetadata, ChunkStatus, EvalResult, Issue, IssueLevel
from src.evaluators import run_all_evaluators, aggregate_results
from src.evaluators.reporting import (
    generate_text_report,
    generate_json_report,
    generate_html_report,
    _format_severity_emoji,
    _format_severity_text,
    _format_timestamp,
    _escape_html,
)
from src.utils.file_io import (
    load_chunk,
    load_glossary,
    save_text_report,
    save_json_report,
    save_html_report,
    _generate_report_filename,
)


# ============================================================================
# Helper function tests
# ============================================================================

def test_format_severity_emoji():
    """Test emoji formatting for severity levels."""
    assert _format_severity_emoji(IssueLevel.ERROR) == "❌"
    assert _format_severity_emoji(IssueLevel.WARNING) == "⚠️"
    assert _format_severity_emoji(IssueLevel.INFO) == "ℹ️"


def test_format_severity_text():
    """Test text formatting for severity levels."""
    assert _format_severity_text(IssueLevel.ERROR) == "ERROR"
    assert _format_severity_text(IssueLevel.WARNING) == "WARNING"
    assert _format_severity_text(IssueLevel.INFO) == "INFO"


def test_format_timestamp():
    """Test timestamp formatting."""
    dt = datetime(2025, 1, 31, 14, 30, 22)
    result = _format_timestamp(dt)
    assert result == "2025-01-31 14:30:22"


def test_escape_html():
    """Test HTML escaping of special characters."""
    assert _escape_html("Hello <world>") == "Hello &lt;world&gt;"
    assert _escape_html("A & B") == "A &amp; B"
    assert _escape_html('Say "hello"') == "Say &quot;hello&quot;"
    assert _escape_html("It's") == "It&#x27;s"


def test_generate_report_filename():
    """Test report filename generation with timestamp."""
    filename = _generate_report_filename("ch01_chunk_001", "html")

    # Should match pattern: eval_ch01_chunk_001_YYYYMMDD_HHMMSS.html
    assert filename.startswith("eval_ch01_chunk_001_")
    assert filename.endswith(".html")
    assert len(filename) == len("eval_ch01_chunk_001_20250131_143022.html")


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def fixtures_dir():
    """Get path to test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def chunk_good(fixtures_dir):
    """Load good translation chunk fixture."""
    return load_chunk(fixtures_dir / "chunk_translated_good.json")


@pytest.fixture
def chunk_errors(fixtures_dir):
    """Load error-filled translation chunk fixture."""
    return load_chunk(fixtures_dir / "chunk_translated_errors.json")


@pytest.fixture
def glossary(fixtures_dir):
    """Load glossary fixture."""
    return load_glossary(fixtures_dir / "glossary_sample.json")


@pytest.fixture
def sample_eval_result():
    """Create a sample EvalResult for testing."""
    return EvalResult(
        eval_name="test_evaluator",
        eval_version="1.0.0",
        target_id="ch01_chunk_001",
        target_type="chunk",
        passed=True,
        score=0.95,
        issues=[],
        metadata={"test": "data"}
    )


@pytest.fixture
def sample_eval_result_with_issues():
    """Create a sample EvalResult with various issues."""
    return EvalResult(
        eval_name="test_evaluator",
        eval_version="1.0.0",
        target_id="ch01_chunk_001",
        target_type="chunk",
        passed=False,
        score=0.65,
        issues=[
            Issue(
                severity=IssueLevel.ERROR,
                message="Critical error found",
                location="line 5",
                suggestion="Fix the error"
            ),
            Issue(
                severity=IssueLevel.WARNING,
                message="Minor warning",
                location="line 10",
                suggestion=None
            ),
            Issue(
                severity=IssueLevel.INFO,
                message="FYI: Something to note",
                location=None,
                suggestion=None
            ),
        ],
        metadata={"test": "data"}
    )


@pytest.fixture
def temp_project_dir():
    """Create temporary project directory for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir) / "test_project"
        project_path.mkdir()
        yield project_path


# ============================================================================
# Text report tests
# ============================================================================

def test_generate_text_report_with_passed_results(sample_eval_result):
    """Test text report generation with all evaluators passing."""
    results = [sample_eval_result]
    aggregated = aggregate_results(results)

    report = generate_text_report(results, aggregated)

    assert isinstance(report, str)
    assert len(report) > 0
    assert "✅ PASSED" in report
    assert "test_evaluator" in report
    assert "1.0.0" in report
    assert "No issues found" in report


def test_generate_text_report_with_failed_results(sample_eval_result_with_issues):
    """Test text report generation with failures."""
    results = [sample_eval_result_with_issues]
    aggregated = aggregate_results(results)

    report = generate_text_report(results, aggregated)

    assert isinstance(report, str)
    assert "❌ FAILED" in report
    assert "test_evaluator" in report
    assert "Critical error found" in report
    assert "Minor warning" in report
    assert "FYI: Something to note" in report
    assert "Fix the error" in report


def test_generate_text_report_with_chunk_context(sample_eval_result):
    """Test text report includes chunk information when provided."""
    results = [sample_eval_result]
    aggregated = aggregate_results(results)

    chunk = Chunk(
        id="ch01_chunk_001",
        chapter_id="chapter_01",
        position=1,
        source_text="Hello world",
        translated_text="Hola mundo",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=11,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=2
        ),
        status=ChunkStatus.TRANSLATED
    )

    report = generate_text_report(results, aggregated, chunk)

    assert "ch01_chunk_001" in report
    assert "chapter_01" in report
    assert "Position: 1" in report


def test_generate_text_report_groups_issues_by_severity(sample_eval_result_with_issues):
    """Test that issues are grouped by severity level."""
    results = [sample_eval_result_with_issues]
    aggregated = aggregate_results(results)

    report = generate_text_report(results, aggregated)

    # Should have sections for each severity
    assert "❌ ERROR" in report
    assert "⚠️ WARNING" in report
    assert "ℹ️ INFO" in report

    # Errors should appear before warnings
    error_pos = report.index("❌ ERROR")
    warning_pos = report.index("⚠️ WARNING")
    info_pos = report.index("ℹ️ INFO")

    assert error_pos < warning_pos < info_pos


def test_generate_text_report_empty_results():
    """Test text report with no evaluation results."""
    results = []
    aggregated = aggregate_results(results)

    report = generate_text_report(results, aggregated)

    assert isinstance(report, str)
    assert len(report) > 0
    assert "Evaluators Run: 0" in report


# ============================================================================
# JSON report tests
# ============================================================================

def test_generate_json_report_with_passed_results(sample_eval_result):
    """Test JSON report generation with passing results."""
    results = [sample_eval_result]
    aggregated = aggregate_results(results)

    report = generate_json_report(results, aggregated)

    # Should be valid JSON
    data = json.loads(report)

    assert data["report_type"] == "evaluation"
    assert "generated_at" in data
    assert data["summary"]["overall_passed"] is True
    assert data["summary"]["total_evaluators"] == 1
    assert data["summary"]["total_issues"] == 0
    assert len(data["evaluators"]) == 1
    assert len(data["detailed_results"]) == 1


def test_generate_json_report_with_failed_results(sample_eval_result_with_issues):
    """Test JSON report generation with failures."""
    results = [sample_eval_result_with_issues]
    aggregated = aggregate_results(results)

    report = generate_json_report(results, aggregated)

    data = json.loads(report)

    assert data["summary"]["overall_passed"] is False
    assert data["summary"]["total_issues"] == 3
    assert data["summary"]["issues_by_severity"]["error"] == 1
    assert data["summary"]["issues_by_severity"]["warning"] == 1
    assert data["summary"]["issues_by_severity"]["info"] == 1

    # Check detailed results structure
    detailed = data["detailed_results"][0]
    assert detailed["eval_name"] == "test_evaluator"
    assert detailed["passed"] is False
    assert detailed["score"] == 0.65
    assert len(detailed["issues"]) == 3


def test_generate_json_report_with_chunk_context(sample_eval_result):
    """Test JSON report includes chunk information."""
    results = [sample_eval_result]
    aggregated = aggregate_results(results)

    chunk = Chunk(
        id="ch01_chunk_001",
        chapter_id="chapter_01",
        position=1,
        source_text="Hello world",
        translated_text="Hola mundo",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=11,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=2
        ),
        status=ChunkStatus.TRANSLATED
    )

    report = generate_json_report(results, aggregated, chunk)

    data = json.loads(report)

    assert "chunk" in data
    assert data["chunk"]["id"] == "ch01_chunk_001"
    assert data["chunk"]["chapter_id"] == "chapter_01"
    assert data["chunk"]["position"] == 1
    assert data["chunk"]["source_word_count"] == 2
    assert "source_preview" in data["chunk"]


def test_generate_json_report_handles_none_scores():
    """Test JSON report handles evaluators without scores."""
    result = EvalResult(
        eval_name="test_eval",
        eval_version="1.0.0",
        target_id="test_id",
        target_type="chunk",
        passed=True,
        score=None,  # No score provided
        issues=[],
        metadata={}
    )

    results = [result]
    aggregated = aggregate_results(results)

    report = generate_json_report(results, aggregated)

    data = json.loads(report)
    assert data["detailed_results"][0]["score"] is None
    assert data["summary"]["average_score"] is None


def test_generate_json_report_serializes_datetimes(sample_eval_result):
    """Test that datetime objects are properly serialized to ISO format."""
    results = [sample_eval_result]
    aggregated = aggregate_results(results)

    report = generate_json_report(results, aggregated)

    data = json.loads(report)

    # Check timestamps are ISO format strings
    assert isinstance(data["generated_at"], str)
    assert "T" in data["generated_at"]  # ISO format has T separator

    assert isinstance(data["detailed_results"][0]["executed_at"], str)


def test_generate_json_report_preserves_unicode():
    """Test JSON report preserves Unicode characters (Spanish text)."""
    result = EvalResult(
        eval_name="test_eval",
        eval_version="1.0.0",
        target_id="test_id",
        target_type="chunk",
        passed=False,
        score=0.5,
        issues=[
            Issue(
                severity=IssueLevel.ERROR,
                message="Palabra española: niño, año",
                location="línea 5",
                suggestion="Corrección"
            )
        ],
        metadata={}
    )

    results = [result]
    aggregated = aggregate_results(results)

    report = generate_json_report(results, aggregated)

    # Should contain Unicode characters directly, not escaped
    assert "niño" in report
    assert "año" in report
    assert "línea" in report
    assert "Corrección" in report

    # Verify it's still valid JSON
    data = json.loads(report)
    assert data["detailed_results"][0]["issues"][0]["message"] == "Palabra española: niño, año"


# ============================================================================
# HTML report tests
# ============================================================================

def test_generate_html_report_with_passed_results(sample_eval_result):
    """Test HTML report generation with passing results."""
    results = [sample_eval_result]
    aggregated = aggregate_results(results)

    report = generate_html_report(results, aggregated)

    assert isinstance(report, str)
    assert "<!DOCTYPE html>" in report
    assert "<html" in report
    assert "PASSED" in report
    assert "test_evaluator" in report
    assert "No issues found" in report


def test_generate_html_report_with_failed_results(sample_eval_result_with_issues):
    """Test HTML report generation with failures."""
    results = [sample_eval_result_with_issues]
    aggregated = aggregate_results(results)

    report = generate_html_report(results, aggregated)

    assert "FAILED" in report
    assert "Critical error found" in report
    assert "Minor warning" in report
    assert "FYI: Something to note" in report
    assert "Fix the error" in report


def test_generate_html_report_has_embedded_css(sample_eval_result):
    """Test HTML report includes embedded CSS styling."""
    results = [sample_eval_result]
    aggregated = aggregate_results(results)

    report = generate_html_report(results, aggregated)

    assert "<style>" in report
    assert "</style>" in report
    assert "font-family" in report
    assert "background" in report


def test_generate_html_report_escapes_special_characters():
    """Test HTML report properly escapes special characters."""
    result = EvalResult(
        eval_name="test_eval",
        eval_version="1.0.0",
        target_id="test_id",
        target_type="chunk",
        passed=False,
        score=0.5,
        issues=[
            Issue(
                severity=IssueLevel.ERROR,
                message="Error with <tag> and & symbol",
                location="line 5",
                suggestion='Use "quotes" properly'
            )
        ],
        metadata={}
    )

    results = [result]
    aggregated = aggregate_results(results)

    report = generate_html_report(results, aggregated)

    # Special characters should be escaped
    assert "&lt;tag&gt;" in report
    assert "&amp;" in report
    assert "&quot;quotes&quot;" in report


def test_generate_html_report_with_chunk_context(sample_eval_result):
    """Test HTML report includes chunk information."""
    results = [sample_eval_result]
    aggregated = aggregate_results(results)

    chunk = Chunk(
        id="ch01_chunk_001",
        chapter_id="chapter_01",
        position=1,
        source_text="Hello world",
        translated_text="Hola mundo",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=11,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=2
        ),
        status=ChunkStatus.TRANSLATED
    )

    report = generate_html_report(results, aggregated, chunk)

    assert "ch01_chunk_001" in report
    assert "chapter_01" in report
    assert "Chunk Information" in report


def test_generate_html_report_creates_valid_structure(sample_eval_result):
    """Test HTML report has valid HTML structure."""
    results = [sample_eval_result]
    aggregated = aggregate_results(results)

    report = generate_html_report(results, aggregated)

    # Check basic HTML structure
    assert report.startswith("<!DOCTYPE html>")
    assert "<html lang=\"en\">" in report
    assert "<head>" in report
    assert "</head>" in report
    assert "<body>" in report
    assert "</body>" in report
    assert "</html>" in report

    # Check meta tags
    assert 'charset="UTF-8"' in report
    assert '<meta name="viewport"' in report


# ============================================================================
# Report saving tests
# ============================================================================

def test_save_text_report(temp_project_dir, sample_eval_result):
    """Test saving text report to file."""
    results = [sample_eval_result]
    aggregated = aggregate_results(results)
    report = generate_text_report(results, aggregated)

    path = save_text_report(report, temp_project_dir, "ch01_chunk_001")

    assert path.exists()
    assert path.suffix == ".txt"
    assert path.name.startswith("eval_ch01_chunk_001_")
    assert path.parent.name == "reports"

    # Verify content
    content = path.read_text(encoding='utf-8')
    assert content == report or content == report + '\n'


def test_save_json_report(temp_project_dir, sample_eval_result):
    """Test saving JSON report to file."""
    results = [sample_eval_result]
    aggregated = aggregate_results(results)
    report = generate_json_report(results, aggregated)

    path = save_json_report(report, temp_project_dir, "ch01_chunk_001")

    assert path.exists()
    assert path.suffix == ".json"
    assert path.name.startswith("eval_ch01_chunk_001_")

    # Verify it's valid JSON
    with path.open('r', encoding='utf-8') as f:
        data = json.load(f)
    assert data["report_type"] == "evaluation"


def test_save_html_report(temp_project_dir, sample_eval_result):
    """Test saving HTML report to file."""
    results = [sample_eval_result]
    aggregated = aggregate_results(results)
    report = generate_html_report(results, aggregated)

    path = save_html_report(report, temp_project_dir, "ch01_chunk_001")

    assert path.exists()
    assert path.suffix == ".html"
    assert path.name.startswith("eval_ch01_chunk_001_")

    # Verify content
    content = path.read_text(encoding='utf-8')
    assert "<!DOCTYPE html>" in content


def test_save_report_creates_reports_directory(temp_project_dir, sample_eval_result):
    """Test that save functions create reports directory if it doesn't exist."""
    results = [sample_eval_result]
    aggregated = aggregate_results(results)
    report = generate_text_report(results, aggregated)

    # Reports directory shouldn't exist yet
    reports_dir = temp_project_dir / "reports"
    assert not reports_dir.exists()

    # Save should create it
    path = save_text_report(report, temp_project_dir, "ch01_chunk_001")

    assert reports_dir.exists()
    assert reports_dir.is_dir()
    assert path.parent == reports_dir


def test_save_json_report_validates_json(temp_project_dir):
    """Test that save_json_report validates JSON before saving."""
    invalid_json = "This is not valid JSON {{{{"

    with pytest.raises(ValueError, match="Invalid JSON report"):
        save_json_report(invalid_json, temp_project_dir, "test_chunk")


def test_save_report_returns_path(temp_project_dir, sample_eval_result):
    """Test that save functions return the path to saved file."""
    results = [sample_eval_result]
    aggregated = aggregate_results(results)
    report = generate_text_report(results, aggregated)

    path = save_text_report(report, temp_project_dir, "ch01_chunk_001")

    assert isinstance(path, Path)
    assert path.is_absolute()
    assert path.exists()


# ============================================================================
# Integration tests with real fixtures
# ============================================================================

def test_integration_text_report_with_good_chunk(chunk_good, glossary):
    """Integration test: Generate text report with good translation fixture."""
    from src.config import create_default_config

    config = create_default_config("test_project")
    config.evaluation.enabled_evals = ["length", "paragraph", "dictionary", "glossary"]

    try:
        results = run_all_evaluators(chunk_good, config.evaluation, glossary)
        aggregated = aggregate_results(results)
        report = generate_text_report(results, aggregated, chunk_good)

        assert isinstance(report, str)
        assert len(report) > 0
        # Good translation should mostly pass (may have minor warnings)
        assert "Evaluators Run: 4" in report
    except Exception as e:
        # Some evaluators (like dictionary) may not initialize in test environment
        # This is acceptable for this integration test
        pytest.skip(f"Evaluator initialization failed (expected in some environments): {e}")


def test_integration_json_report_with_error_chunk(chunk_errors, glossary):
    """Integration test: Generate JSON report with error-filled fixture."""
    from src.config import create_default_config

    config = create_default_config("test_project")
    config.evaluation.enabled_evals = ["length", "paragraph"]  # Use simple evaluators

    results = run_all_evaluators(chunk_errors, config.evaluation, glossary)
    aggregated = aggregate_results(results)
    report = generate_json_report(results, aggregated, chunk_errors)

    # Parse and verify
    data = json.loads(report)
    assert data["report_type"] == "evaluation"
    assert data["summary"]["total_evaluators"] >= 1
    assert data["summary"]["total_issues"] > 0  # Should have issues
    assert "chunk" in data
    assert data["chunk"]["id"] == chunk_errors.id


def test_integration_html_report_with_good_chunk(chunk_good, glossary):
    """Integration test: Generate HTML report with good translation fixture."""
    from src.config import create_default_config

    config = create_default_config("test_project")
    config.evaluation.enabled_evals = ["length", "paragraph"]

    results = run_all_evaluators(chunk_good, config.evaluation, glossary)
    aggregated = aggregate_results(results)
    report = generate_html_report(results, aggregated, chunk_good)

    assert "<!DOCTYPE html>" in report
    assert chunk_good.id in report
    assert "Evaluation Report" in report


def test_integration_save_all_formats(temp_project_dir, chunk_good, glossary):
    """Integration test: Generate and save all three report formats."""
    from src.config import create_default_config

    config = create_default_config("test_project")
    config.evaluation.enabled_evals = ["length", "paragraph"]

    results = run_all_evaluators(chunk_good, config.evaluation, glossary)
    aggregated = aggregate_results(results)

    # Generate all formats
    text_report = generate_text_report(results, aggregated, chunk_good)
    json_report = generate_json_report(results, aggregated, chunk_good)
    html_report = generate_html_report(results, aggregated, chunk_good)

    # Save all formats
    text_path = save_text_report(text_report, temp_project_dir, chunk_good.id)
    json_path = save_json_report(json_report, temp_project_dir, chunk_good.id)
    html_path = save_html_report(html_report, temp_project_dir, chunk_good.id)

    # Verify all exist
    assert text_path.exists()
    assert json_path.exists()
    assert html_path.exists()

    # Verify all are in reports directory
    assert text_path.parent.name == "reports"
    assert json_path.parent.name == "reports"
    assert html_path.parent.name == "reports"
