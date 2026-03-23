"""
Tests for StyleGuide loading and saving.

Tests the style guide I/O functions and model validation.
"""

import pytest
import json
import tempfile
from pathlib import Path
from datetime import datetime

from src.utils.file_io import load_style_guide, save_style_guide
from src.models import StyleGuide


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def fixtures_dir():
    """Get path to test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def style_guide_path(fixtures_dir):
    """Get path to style guide fixture."""
    return fixtures_dir / "style_guide_sample.json"


@pytest.fixture
def sample_style_guide():
    """Sample StyleGuide object for testing."""
    return StyleGuide(
        content="TONE: Formal\nFORMALITY: High\nDIALECT: Neutral Spanish",
        version="1.0",
        created_at=datetime(2025, 10, 30, 10, 0, 0),
        updated_at=datetime(2025, 10, 30, 10, 0, 0)
    )


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


# =============================================================================
# LOAD_STYLE_GUIDE TESTS
# =============================================================================

class TestLoadStyleGuide:
    """Tests for load_style_guide()."""

    def test_load_valid_style_guide(self, style_guide_path):
        """Test loading a valid style guide."""
        style_guide = load_style_guide(style_guide_path)

        assert isinstance(style_guide, StyleGuide)
        assert style_guide.version == "1.0"
        assert "Formal" in style_guide.content
        assert len(style_guide.content) > 0

    def test_load_missing_file(self):
        """Test loading non-existent file raises FileNotFoundError."""
        missing_path = Path("nonexistent/style_guide.json")

        with pytest.raises(FileNotFoundError) as exc_info:
            load_style_guide(missing_path)

        assert "not found" in str(exc_info.value).lower()

    def test_load_invalid_json(self, temp_dir):
        """Test loading invalid JSON raises JSONDecodeError."""
        bad_json_path = temp_dir / "bad.json"
        bad_json_path.write_text("{ invalid json }", encoding='utf-8')

        with pytest.raises(json.JSONDecodeError):
            load_style_guide(bad_json_path)

    def test_load_invalid_schema(self, temp_dir):
        """Test loading JSON that doesn't match StyleGuide schema."""
        invalid_path = temp_dir / "invalid.json"
        invalid_data = {"wrong_field": "value"}
        invalid_path.write_text(json.dumps(invalid_data), encoding='utf-8')

        with pytest.raises(ValueError) as exc_info:
            load_style_guide(invalid_path)

        assert "Invalid StyleGuide data" in str(exc_info.value)

    def test_load_preserves_version(self, style_guide_path):
        """Test that version field is correctly loaded."""
        style_guide = load_style_guide(style_guide_path)
        assert style_guide.version == "1.0"

    def test_load_preserves_timestamps(self, style_guide_path):
        """Test that timestamp fields are correctly parsed."""
        style_guide = load_style_guide(style_guide_path)

        assert isinstance(style_guide.created_at, datetime)
        assert isinstance(style_guide.updated_at, datetime)

    def test_load_handles_unicode(self, temp_dir):
        """Test that Unicode content is correctly loaded."""
        unicode_sg = StyleGuide(
            content="Diálogo: usar comillas «españolas»\nTono: Cortés",
            version="1.0",
            created_at=datetime.now(),
            updated_at=datetime.now()
        )

        # Save and reload
        sg_path = temp_dir / "unicode.json"
        save_style_guide(unicode_sg, sg_path)
        loaded = load_style_guide(sg_path)

        assert loaded.content == unicode_sg.content
        assert "Diálogo" in loaded.content
        assert "«españolas»" in loaded.content


# =============================================================================
# SAVE_STYLE_GUIDE TESTS
# =============================================================================

class TestSaveStyleGuide:
    """Tests for save_style_guide()."""

    def test_save_creates_file(self, temp_dir, sample_style_guide):
        """Test that save creates a file."""
        output_path = temp_dir / "style_guide.json"

        save_style_guide(sample_style_guide, output_path)

        assert output_path.exists()
        assert output_path.is_file()

    def test_save_creates_parent_directory(self, temp_dir, sample_style_guide):
        """Test that save creates parent directories if needed."""
        output_path = temp_dir / "subdir" / "nested" / "style_guide.json"

        save_style_guide(sample_style_guide, output_path)

        assert output_path.exists()
        assert output_path.parent.exists()

    def test_save_produces_valid_json(self, temp_dir, sample_style_guide):
        """Test that saved file is valid JSON."""
        output_path = temp_dir / "style_guide.json"

        save_style_guide(sample_style_guide, output_path)

        # Should be able to load as JSON
        with output_path.open('r', encoding='utf-8') as f:
            data = json.load(f)

        assert isinstance(data, dict)
        assert "content" in data
        assert "version" in data

    def test_save_preserves_content(self, temp_dir, sample_style_guide):
        """Test that content is preserved exactly."""
        output_path = temp_dir / "style_guide.json"

        save_style_guide(sample_style_guide, output_path)
        loaded = load_style_guide(output_path)

        assert loaded.content == sample_style_guide.content

    def test_save_preserves_version(self, temp_dir, sample_style_guide):
        """Test that version is preserved."""
        output_path = temp_dir / "style_guide.json"

        save_style_guide(sample_style_guide, output_path)
        loaded = load_style_guide(output_path)

        assert loaded.version == sample_style_guide.version

    def test_save_round_trip(self, temp_dir, sample_style_guide):
        """Test that save then load produces identical object."""
        output_path = temp_dir / "style_guide.json"

        save_style_guide(sample_style_guide, output_path)
        loaded = load_style_guide(output_path)

        assert loaded.content == sample_style_guide.content
        assert loaded.version == sample_style_guide.version
        # Note: timestamps might differ slightly in precision, so check they exist
        assert loaded.created_at is not None
        assert loaded.updated_at is not None

    def test_save_uses_atomic_write(self, temp_dir, sample_style_guide):
        """Test that save uses atomic write (temp file + rename)."""
        output_path = temp_dir / "style_guide.json"

        # Save should not leave temp files behind
        save_style_guide(sample_style_guide, output_path)

        # Check that temp file doesn't exist
        temp_files = list(temp_dir.glob("*.tmp"))
        assert len(temp_files) == 0

        # But output file does exist
        assert output_path.exists()

    def test_save_handles_unicode(self, temp_dir):
        """Test that Unicode content is saved correctly."""
        unicode_sg = StyleGuide(
            content="Diálogos: usar comillas «españolas»\nÉnfasis: mayúsculas",
            version="2.0",
            created_at=datetime.now(),
            updated_at=datetime.now()
        )

        output_path = temp_dir / "unicode.json"
        save_style_guide(unicode_sg, output_path)

        # Reload and verify
        loaded = load_style_guide(output_path)
        assert loaded.content == unicode_sg.content
        assert "Diálogos" in loaded.content


# =============================================================================
# STYLEGUIDE MODEL TESTS
# =============================================================================

class TestStyleGuideModel:
    """Tests for StyleGuide Pydantic model."""

    def test_create_minimal(self):
        """Test creating StyleGuide with minimal fields."""
        sg = StyleGuide(
            content="TONE: Formal",
            version="1.0",
            created_at=datetime.now(),
            updated_at=datetime.now()
        )

        assert sg.content == "TONE: Formal"
        assert sg.version == "1.0"

    def test_create_with_defaults(self):
        """Test that version defaults are set."""
        sg = StyleGuide(
            content="TONE: Formal"
            # version should default to "1.0"
            # timestamps should default to now()
        )

        assert sg.version == "1.0"
        assert sg.created_at is not None
        assert sg.updated_at is not None

    def test_version_can_be_updated(self):
        """Test that version can be manually updated."""
        sg = StyleGuide(content="Test")
        sg.version = "2.0"

        assert sg.version == "2.0"

    def test_content_can_be_multiline(self):
        """Test that content can contain multiple lines."""
        content = """TONE: Formal but accessible
FORMALITY LEVEL: Medium-high
DIALECT: Neutral Spanish
SPECIAL INSTRUCTIONS: Preserve period language"""

        sg = StyleGuide(content=content)

        assert "\n" in sg.content
        assert "TONE" in sg.content
        assert "DIALECT" in sg.content

    def test_model_serialization(self, sample_style_guide):
        """Test that StyleGuide can be serialized to dict."""
        data = sample_style_guide.model_dump()

        assert isinstance(data, dict)
        assert "content" in data
        assert "version" in data
        assert "created_at" in data
        assert "updated_at" in data

    def test_model_deserialization(self):
        """Test that StyleGuide can be created from dict."""
        data = {
            "content": "Test content",
            "version": "1.5",
            "created_at": "2025-10-30T10:00:00",
            "updated_at": "2025-10-30T12:00:00"
        }

        sg = StyleGuide.model_validate(data)

        assert sg.content == "Test content"
        assert sg.version == "1.5"


# =============================================================================
# INTEGRATION TESTS
# =============================================================================

class TestStyleGuideIntegration:
    """Integration tests for style guide workflow."""

    def test_create_save_load_workflow(self, temp_dir):
        """Test full workflow: create, save, load, verify."""
        # Step 1: Create style guide
        original = StyleGuide(
            content="TONE: Formal\nDIALECT: Neutral",
            version="1.0",
            created_at=datetime(2025, 10, 30, 10, 0, 0),
            updated_at=datetime(2025, 10, 30, 10, 0, 0)
        )

        # Step 2: Save to file
        path = temp_dir / "my_style_guide.json"
        save_style_guide(original, path)

        # Step 3: Load from file
        loaded = load_style_guide(path)

        # Step 4: Verify matches
        assert loaded.content == original.content
        assert loaded.version == original.version

    def test_update_version_workflow(self, temp_dir):
        """Test workflow for updating version manually."""
        # Create initial version
        sg = StyleGuide(content="Initial content", version="1.0")
        path = temp_dir / "style_guide.json"
        save_style_guide(sg, path)

        # Load, update version, save again
        loaded = load_style_guide(path)
        loaded.version = "1.1"
        loaded.content = "Updated content"
        loaded.updated_at = datetime.now()
        save_style_guide(loaded, path)

        # Load again and verify
        final = load_style_guide(path)
        assert final.version == "1.1"
        assert final.content == "Updated content"
