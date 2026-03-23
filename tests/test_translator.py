"""
Tests for translation workbook generation and parsing.
"""

import re
from datetime import datetime
from pathlib import Path

import pytest

from src.models import (
    Chunk,
    ChunkMetadata,
    ChunkStatus,
    Glossary,
    GlossaryTerm,
    GlossaryTermType,
    StyleGuide,
)
from src.translator import (
    extract_previous_chapter_context,
    generate_workbook,
    save_workbook,
)


@pytest.fixture
def sample_chunk() -> Chunk:
    """Create a sample chunk for testing."""
    return Chunk(
        id="ch01_chunk_001",
        chapter_id="chapter_01",
        position=1,
        source_text="It is a truth universally acknowledged, that a single man in possession of a good fortune must be in want of a wife.\n\nHowever little known the feelings or views of such a man may be on his first entering a neighbourhood, this truth is so well fixed in the minds of the surrounding families.",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=372,
            overlap_start=0,
            overlap_end=45,
            paragraph_count=2,
            word_count=69,
        ),
        status=ChunkStatus.PENDING,
        created_at=datetime(2025, 1, 28, 10, 0, 0),
    )


@pytest.fixture
def sample_chunk_2() -> Chunk:
    """Create a second sample chunk for testing."""
    return Chunk(
        id="ch01_chunk_002",
        chapter_id="chapter_01",
        position=2,
        source_text="My dear Mr. Bennet, said his lady to him one day, have you heard that Netherfield Park is let at last?",
        metadata=ChunkMetadata(
            char_start=327,
            char_end=432,
            overlap_start=45,
            overlap_end=0,
            paragraph_count=1,
            word_count=21,
        ),
        status=ChunkStatus.PENDING,
        created_at=datetime(2025, 1, 28, 10, 5, 0),
    )


@pytest.fixture
def sample_glossary() -> Glossary:
    """Create a sample glossary for testing."""
    return Glossary(
        terms=[
            GlossaryTerm(
                english="Mr. Bennet",
                spanish="Sr. Bennet",
                type=GlossaryTermType.CHARACTER,
            ),
            GlossaryTerm(
                english="Elizabeth",
                spanish="Elizabeth",
                type=GlossaryTermType.CHARACTER,
            ),
            GlossaryTerm(
                english="Netherfield Park",
                spanish="Netherfield Park",
                type=GlossaryTermType.PLACE,
            ),
        ],
        version="1.0",
        updated_at=datetime(2025, 1, 28, 9, 0, 0),
    )


@pytest.fixture
def sample_style_guide() -> StyleGuide:
    """Create a sample style guide for testing."""
    return StyleGuide(
        content="TONE: Formal but accessible\nFORMALITY: Medium-high (19th century literature)\nDIALECT: Neutral Spanish\nCONVENTIONS: Use 'usted' for formal address",
        version="1.0",
        created_at=datetime(2025, 1, 28, 9, 0, 0),
        updated_at=datetime(2025, 1, 28, 9, 0, 0),
    )


# ============================================================================
# Basic Workbook Generation Tests
# ============================================================================


def test_generate_workbook_single_chunk(sample_chunk):
    """Test generating workbook with a single chunk."""
    workbook = generate_workbook(
        chunks=[sample_chunk],
        project_name="Pride and Prejudice",
    )

    # Verify workbook contains key sections
    assert "Translation Workbook: Pride and Prejudice" in workbook
    assert "How to Use This Workbook" in workbook
    assert "CHUNK 1: ch01_chunk_001" in workbook
    assert "PROMPT TO COPY" in workbook
    assert "PASTE TRANSLATION HERE" in workbook
    assert "METADATA (Do not edit this section)" in workbook


def test_generate_workbook_multiple_chunks(sample_chunk, sample_chunk_2):
    """Test generating workbook with multiple chunks."""
    workbook = generate_workbook(
        chunks=[sample_chunk, sample_chunk_2],
        project_name="Pride and Prejudice",
    )

    # Verify both chunks are present
    assert "CHUNK 1: ch01_chunk_001" in workbook
    assert "CHUNK 2: ch01_chunk_002" in workbook

    # Verify order
    chunk1_pos = workbook.find("CHUNK 1")
    chunk2_pos = workbook.find("CHUNK 2")
    assert chunk1_pos < chunk2_pos, "Chunks should be in position order"


def test_generate_workbook_sorts_chunks(sample_chunk, sample_chunk_2):
    """Test that chunks are sorted by position even if provided out of order."""
    # Provide chunks in reverse order
    workbook = generate_workbook(
        chunks=[sample_chunk_2, sample_chunk],  # 2, 1
        project_name="Test",
    )

    # Verify they appear in correct order
    chunk1_pos = workbook.find("CHUNK 1")
    chunk2_pos = workbook.find("CHUNK 2")
    assert chunk1_pos < chunk2_pos, "Chunks should be sorted by position"


def test_generate_workbook_with_glossary(sample_chunk, sample_glossary):
    """Test generating workbook with glossary reference."""
    workbook = generate_workbook(
        chunks=[sample_chunk],
        glossary=sample_glossary,
        project_name="Test",
    )

    # Verify glossary section is present
    assert "Glossary Reference" in workbook
    assert "CHARACTER NAMES:" in workbook
    assert "Mr. Bennet → Sr. Bennet" in workbook
    assert "PLACE NAMES:" in workbook
    assert "Netherfield Park → Netherfield Park" in workbook
    assert ": 1.0" in workbook  # Version (with markdown formatting)


def test_generate_workbook_with_style_guide(sample_chunk, sample_style_guide):
    """Test generating workbook with style guide reference."""
    workbook = generate_workbook(
        chunks=[sample_chunk],
        style_guide=sample_style_guide,
        project_name="Test",
    )

    # Verify style guide section is present
    assert "Style Guide" in workbook
    assert "TONE: Formal but accessible" in workbook
    assert "FORMALITY: Medium-high" in workbook
    assert ": 1.0" in workbook  # Version (with markdown formatting)


def test_generate_workbook_with_both(sample_chunk, sample_glossary, sample_style_guide):
    """Test generating workbook with both glossary and style guide."""
    workbook = generate_workbook(
        chunks=[sample_chunk],
        glossary=sample_glossary,
        style_guide=sample_style_guide,
        project_name="Test",
    )

    # Verify both sections are present
    assert "Glossary Reference" in workbook
    assert "Mr. Bennet → Sr. Bennet" in workbook
    assert "Style Guide" in workbook
    assert "TONE: Formal but accessible" in workbook


# ============================================================================
# Prompt Rendering Tests
# ============================================================================


def test_workbook_contains_rendered_prompt(sample_chunk, sample_glossary):
    """Test that workbook contains complete rendered prompt for each chunk."""
    workbook = generate_workbook(
        chunks=[sample_chunk],
        glossary=sample_glossary,
        project_name="Pride and Prejudice",
        source_language="English",
        target_language="Spanish",
    )

    # Verify prompt contains expected elements
    assert "You are translating" in workbook
    assert "Pride and Prejudice" in workbook
    assert "from English" in workbook
    assert "to Spanish" in workbook

    # Verify source text is in prompt
    assert "It is a truth universally acknowledged" in workbook

    # Verify glossary is formatted in prompt
    assert "Mr. Bennet → Sr. Bennet" in workbook


def test_workbook_prompt_includes_style_guide(sample_chunk, sample_style_guide):
    """Test that rendered prompt includes style guide content."""
    workbook = generate_workbook(
        chunks=[sample_chunk],
        style_guide=sample_style_guide,
        project_name="Test",
    )

    # Verify style guide content is in the prompt section
    # Search for the actual chunk prompt section (starts with "###")
    prompt_start = workbook.find("### PROMPT TO COPY")
    prompt_end = workbook.find("### PASTE TRANSLATION HERE")
    prompt_section = workbook[prompt_start:prompt_end]

    assert "TONE: Formal but accessible" in prompt_section
    assert "FORMALITY: Medium-high" in prompt_section


def test_workbook_prompt_separators(sample_chunk):
    """Test that prompt sections have clear separators."""
    workbook = generate_workbook(
        chunks=[sample_chunk],
        project_name="Test",
    )

    # Count separator lines
    separator = "─" * 69  # 69 dashes
    separator_count = workbook.count(separator)

    # Should have 3 separators per chunk (top, bottom of prompt, bottom of metadata)
    assert separator_count >= 3, "Should have separators for prompt boundaries"


# ============================================================================
# Metadata Tests
# ============================================================================


def test_workbook_includes_chunk_metadata(sample_chunk):
    """Test that workbook includes chunk metadata."""
    workbook = generate_workbook(
        chunks=[sample_chunk],
        project_name="Test",
    )

    # Verify metadata is present
    assert ": 1 of 1" in workbook  # Position (with markdown formatting)
    assert ": chapter_01" in workbook  # Chapter (with markdown formatting)
    assert ": 69 words" in workbook  # Word Count (with markdown formatting)
    assert ": 2" in workbook  # Paragraphs (with markdown formatting)

    # Verify metadata footer
    assert "Chunk ID: ch01_chunk_001" in workbook
    assert "Source Word Count: 69" in workbook
    assert "Source Paragraph Count: 2" in workbook


def test_workbook_shows_correct_total_chunks(sample_chunk, sample_chunk_2):
    """Test that position shows correct total (X of Y)."""
    workbook = generate_workbook(
        chunks=[sample_chunk, sample_chunk_2],
        project_name="Test",
    )

    # Verify both chunks show "X of 2"
    assert ": 1 of 2" in workbook  # Position (with markdown formatting)
    assert ": 2 of 2" in workbook  # Position (with markdown formatting)


# ============================================================================
# Instructions and Documentation Tests
# ============================================================================


def test_workbook_includes_instructions(sample_chunk):
    """Test that workbook includes user instructions."""
    workbook = generate_workbook(
        chunks=[sample_chunk],
        project_name="Test",
    )

    # Verify instructions are present
    assert "How to Use This Workbook" in workbook
    assert "Copy everything between the separator lines" in workbook
    assert "Paste into your LLM interface" in workbook
    assert "import_workbook.py" in workbook


def test_workbook_includes_header(sample_chunk):
    """Test that workbook includes proper header."""
    workbook = generate_workbook(
        chunks=[sample_chunk],
        project_name="Test Project",
    )

    # Verify header elements
    assert "Translation Workbook: Test Project" in workbook
    assert "Generated" in workbook
    assert "Total Chunks" in workbook
    assert ": 1" in workbook  # Chunk count


def test_workbook_includes_footer(sample_chunk):
    """Test that workbook includes footer with import instructions."""
    workbook = generate_workbook(
        chunks=[sample_chunk],
        project_name="Test",
    )

    # Verify footer
    assert "End of Translation Workbook" in workbook
    assert "python import_workbook.py" in workbook


# ============================================================================
# Edge Cases and Error Handling
# ============================================================================


def test_generate_workbook_empty_chunks_list():
    """Test that generating workbook with no chunks raises error."""
    with pytest.raises(ValueError, match="Cannot generate workbook with no chunks"):
        generate_workbook(chunks=[], project_name="Test")


def test_generate_workbook_without_glossary(sample_chunk):
    """Test generating workbook without glossary works correctly."""
    workbook = generate_workbook(
        chunks=[sample_chunk],
        project_name="Test",
    )

    # Should not have glossary section
    assert "Glossary Reference" not in workbook

    # But prompt should still render (with "No glossary provided")
    assert "PROMPT TO COPY" in workbook


def test_generate_workbook_without_style_guide(sample_chunk):
    """Test generating workbook without style guide works correctly."""
    workbook = generate_workbook(
        chunks=[sample_chunk],
        project_name="Test",
    )

    # Should not have style guide section
    assert "Style Guide" not in workbook

    # But prompt should still render
    assert "PROMPT TO COPY" in workbook


def test_generate_workbook_with_book_context(sample_chunk):
    """Test generating workbook with book context."""
    workbook = generate_workbook(
        chunks=[sample_chunk],
        project_name="Test",
        book_context="A 19th century novel about manners and marriage in England.",
    )

    # Prompt should include context
    assert "PROMPT TO COPY" in workbook
    # Context is in the rendered prompt


# ============================================================================
# File I/O Tests
# ============================================================================


def test_save_workbook(tmp_path, sample_chunk):
    """Test saving workbook to file."""
    workbook = generate_workbook(
        chunks=[sample_chunk],
        project_name="Test",
    )

    output_path = tmp_path / "workbook.md"
    save_workbook(workbook, output_path)

    # Verify file was created
    assert output_path.exists()

    # Verify content
    saved_content = output_path.read_text(encoding="utf-8")
    assert saved_content == workbook


def test_save_workbook_creates_directory(tmp_path, sample_chunk):
    """Test that save_workbook creates parent directories if needed."""
    workbook = generate_workbook(
        chunks=[sample_chunk],
        project_name="Test",
    )

    output_path = tmp_path / "subdir" / "nested" / "workbook.md"
    save_workbook(workbook, output_path)

    # Verify file was created
    assert output_path.exists()


def test_save_workbook_utf8_encoding(tmp_path, sample_chunk):
    """Test that workbook is saved with UTF-8 encoding for Spanish characters."""
    # Create chunk with Spanish text
    chunk = Chunk(
        id="test",
        chapter_id="test",
        position=1,
        source_text="Text with Spanish: ñ, á, é, í, ó, ú, ü",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=50,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=8,
        ),
    )

    workbook = generate_workbook(chunks=[chunk], project_name="Test")
    output_path = tmp_path / "workbook.md"
    save_workbook(workbook, output_path)

    # Verify UTF-8 encoding by reading back
    content = output_path.read_text(encoding="utf-8")
    assert "ñ" in content
    assert "á" in content


# ============================================================================
# Integration Tests with Real Fixtures
# ============================================================================


def test_generate_workbook_with_real_fixture(tmp_path):
    """Test generating workbook with real Pride and Prejudice fixture."""
    from src.utils.file_io import load_chunk, load_glossary

    # Load real fixtures
    chunk_path = Path("tests/fixtures/chunk_english.json")
    glossary_path = Path("tests/fixtures/glossary_sample.json")

    if not chunk_path.exists() or not glossary_path.exists():
        pytest.skip("Fixture files not found")

    chunk = load_chunk(chunk_path)
    glossary = load_glossary(glossary_path)

    # Generate workbook
    workbook = generate_workbook(
        chunks=[chunk],
        glossary=glossary,
        project_name="Pride and Prejudice",
        source_language="English",
        target_language="Spanish",
    )

    # Verify workbook structure
    assert "Translation Workbook: Pride and Prejudice" in workbook
    assert "CHUNK 1" in workbook
    assert "PROMPT TO COPY" in workbook
    assert "It is a truth universally acknowledged" in workbook

    # Save and verify
    output_path = tmp_path / "pride_prejudice_workbook.md"
    save_workbook(workbook, output_path)
    assert output_path.exists()
    assert output_path.stat().st_size > 1000  # Should be substantial


def test_workbook_structure_for_parsing():
    """Test that workbook has consistent structure for parsing."""
    from src.utils.file_io import load_chunk

    chunk_path = Path("tests/fixtures/chunk_english.json")
    if not chunk_path.exists():
        pytest.skip("Fixture file not found")

    chunk = load_chunk(chunk_path)

    workbook = generate_workbook(
        chunks=[chunk],
        project_name="Test",
    )

    # Verify parsing markers are present and in correct order
    assert "## CHUNK 1:" in workbook
    assert "### PASTE TRANSLATION HERE:" in workbook
    assert "### METADATA (Do not edit this section):" in workbook

    # Verify chunk ID is in metadata
    assert f"Chunk ID: {chunk.id}" in workbook

    # Verify separators for parsing
    assert "─" * 69 in workbook


# ============================================================================
# Workbook Parsing Tests
# ============================================================================


def test_parse_workbook_good(tmp_path):
    """Test parsing a valid completed workbook."""
    from src.translator import parse_workbook

    workbook_path = Path("tests/fixtures/workbook_completed_good.md")
    if not workbook_path.exists():
        pytest.skip("Completed workbook fixture not found")

    translations = parse_workbook(workbook_path)

    # Should have exactly one translation
    assert len(translations) == 1
    assert "ch01_chunk_001" in translations

    # Translation should be Spanish text
    translation = translations["ch01_chunk_001"]
    assert "verdad universalmente reconocida" in translation
    assert len(translation) > 100


def test_parse_workbook_missing_translation():
    """Test parsing workbook with missing translation."""
    from src.translator import parse_workbook

    workbook_path = Path("tests/fixtures/workbook_completed_missing.md")
    if not workbook_path.exists():
        pytest.skip("Missing translation fixture not found")

    translations = parse_workbook(workbook_path)

    # Should return empty dict (no translations found)
    assert len(translations) == 0


def test_parse_workbook_placeholder():
    """Test that placeholder text is detected and skipped."""
    from src.translator import parse_workbook

    workbook_path = Path("tests/fixtures/workbook_completed_placeholder.md")
    if not workbook_path.exists():
        pytest.skip("Placeholder fixture not found")

    translations = parse_workbook(workbook_path)

    # Should return empty dict (placeholder detected)
    assert len(translations) == 0


def test_parse_workbook_file_not_found():
    """Test error when workbook file doesn't exist."""
    from src.translator import parse_workbook

    with pytest.raises(FileNotFoundError, match="Workbook file not found"):
        parse_workbook(Path("nonexistent_workbook.md"))


def test_validate_workbook_structure_good():
    """Test validation of good workbook structure."""
    from src.translator import validate_workbook_structure

    workbook_path = Path("tests/fixtures/workbook_completed_good.md")
    if not workbook_path.exists():
        pytest.skip("Workbook fixture not found")

    content = workbook_path.read_text(encoding="utf-8")
    is_valid, errors = validate_workbook_structure(content, ["ch01_chunk_001"])

    assert is_valid
    assert len(errors) == 0


def test_validate_workbook_structure_missing_chunk():
    """Test validation detects missing expected chunks."""
    from src.translator import validate_workbook_structure

    workbook_path = Path("tests/fixtures/workbook_completed_good.md")
    if not workbook_path.exists():
        pytest.skip("Workbook fixture not found")

    content = workbook_path.read_text(encoding="utf-8")

    # Expect a chunk that's not in the workbook
    is_valid, errors = validate_workbook_structure(
        content, ["ch01_chunk_001", "ch01_chunk_002"]
    )

    assert not is_valid
    assert any("Missing expected chunks" in err for err in errors)


def test_validate_workbook_structure_extra_chunk():
    """Test validation detects unexpected chunks."""
    from src.translator import validate_workbook_structure

    workbook_path = Path("tests/fixtures/workbook_completed_good.md")
    if not workbook_path.exists():
        pytest.skip("Workbook fixture not found")

    content = workbook_path.read_text(encoding="utf-8")

    # Expect fewer chunks than in workbook
    is_valid, errors = validate_workbook_structure(content, [])

    assert not is_valid
    assert any("Unexpected chunks" in err for err in errors)


def test_update_chunk_with_translation(sample_chunk):
    """Test updating chunk with translation."""
    from src.translator import update_chunk_with_translation

    translation = "Esta es una traducción de prueba."

    updated = update_chunk_with_translation(sample_chunk, translation)

    assert updated.translated_text == translation
    assert updated.status == ChunkStatus.TRANSLATED
    assert updated.translated_at is not None
    assert updated.id == sample_chunk.id  # ID preserved
    assert updated.metadata == sample_chunk.metadata  # Metadata preserved


def test_clean_translation_text_smart_quotes():
    """Test cleaning smart quotes from translation."""
    from src.translator import clean_translation_text

    # Use Unicode escapes for smart quotes: " " ' '
    text = "\u201cHello\u201d and \u2018world\u2019"
    cleaned = clean_translation_text(text)

    assert '"Hello" and \'world\'' == cleaned
    assert "\u201c" not in cleaned  # Smart quote left double
    assert "\u201d" not in cleaned  # Smart quote right double
    assert "\u2018" not in cleaned  # Smart quote left single
    assert "\u2019" not in cleaned  # Smart quote right single


def test_clean_translation_text_line_endings():
    """Test normalizing line endings."""
    from src.translator import clean_translation_text

    # Windows line endings
    text = "Line 1\r\nLine 2\r\nLine 3"
    cleaned = clean_translation_text(text)

    assert "\r" not in cleaned
    assert cleaned == "Line 1\nLine 2\nLine 3"


def test_clean_translation_text_excessive_blank_lines():
    """Test collapsing excessive blank lines."""
    from src.translator import clean_translation_text

    text = "Para 1\n\n\n\n\nPara 2"
    cleaned = clean_translation_text(text)

    # Should collapse to double newline
    assert cleaned == "Para 1\n\nPara 2"
    assert "\n\n\n" not in cleaned


def test_clean_translation_text_preserves_paragraphs():
    """Test that paragraph breaks are preserved."""
    from src.translator import clean_translation_text

    text = "Paragraph 1\n\nParagraph 2\n\nParagraph 3"
    cleaned = clean_translation_text(text)

    assert cleaned == text  # Should be unchanged


def test_is_placeholder_text_detects_placeholders():
    """Test detection of common placeholder patterns."""
    from src.translator import is_placeholder_text

    # Common placeholders
    assert is_placeholder_text("[TRANSLATION]")
    assert is_placeholder_text("PASTE YOUR TRANSLATION HERE")
    assert is_placeholder_text("TODO")
    assert is_placeholder_text("...")
    assert is_placeholder_text("")
    assert is_placeholder_text("   ")


def test_is_placeholder_text_real_translation():
    """Test that real translations are not detected as placeholders."""
    from src.translator import is_placeholder_text

    # Real Spanish text
    assert not is_placeholder_text("Esta es una traducción real.")
    assert not is_placeholder_text("Es una verdad universalmente reconocida...")


def test_extract_chunk_id_from_metadata():
    """Test extracting chunk ID from metadata section."""
    from src.translator import extract_chunk_id_from_metadata

    metadata = """
Chunk ID: ch01_chunk_003
Position: 3
Chapter ID: chapter_01
Source Word Count: 150
"""

    chunk_id = extract_chunk_id_from_metadata(metadata)
    assert chunk_id == "ch01_chunk_003"


def test_extract_chunk_id_from_metadata_not_found():
    """Test error when chunk ID not found in metadata."""
    from src.translator import extract_chunk_id_from_metadata

    metadata = "Position: 3\nChapter ID: chapter_01"

    with pytest.raises(ValueError, match="Could not find 'Chunk ID:'"):
        extract_chunk_id_from_metadata(metadata)


# ============================================================================
# Integration Tests for Import Workflow
# ============================================================================


def test_import_translations_success(tmp_path):
    """Test successful import of translations from workbook."""
    from src.translator import import_translations
    from src.utils.file_io import load_chunk

    # Load original chunk
    chunk_path = Path("tests/fixtures/chunk_english.json")
    if not chunk_path.exists():
        pytest.skip("Chunk fixture not found")

    original_chunk = load_chunk(chunk_path)

    # Load completed workbook
    workbook_path = Path("tests/fixtures/workbook_completed_good.md")
    if not workbook_path.exists():
        pytest.skip("Completed workbook fixture not found")

    # Import translations
    output_dir = tmp_path / "translated"
    updated_chunks, warnings = import_translations(
        workbook_path, [original_chunk], output_dir
    )

    # Should have one updated chunk
    assert len(updated_chunks) == 1
    assert len(warnings) == 0

    # Check updated chunk
    chunk = updated_chunks[0]
    assert chunk.translated_text is not None
    assert "verdad universalmente reconocida" in chunk.translated_text
    assert chunk.status == ChunkStatus.TRANSLATED
    assert chunk.translated_at is not None

    # Check file was saved
    output_file = output_dir / f"{chunk.id}.json"
    assert output_file.exists()


def test_import_translations_missing_translation(tmp_path):
    """Test import with missing translation generates warning."""
    from src.translator import import_translations
    from src.utils.file_io import load_chunk

    # Load original chunk
    chunk_path = Path("tests/fixtures/chunk_english.json")
    if not chunk_path.exists():
        pytest.skip("Chunk fixture not found")

    original_chunk = load_chunk(chunk_path)

    # Load workbook with missing translation
    workbook_path = Path("tests/fixtures/workbook_completed_missing.md")
    if not workbook_path.exists():
        pytest.skip("Missing workbook fixture not found")

    # Import translations
    output_dir = tmp_path / "translated"
    updated_chunks, warnings = import_translations(
        workbook_path, [original_chunk], output_dir
    )

    # Should have no updated chunks
    assert len(updated_chunks) == 0

    # Should have warning
    assert len(warnings) == 1
    assert "No translation found" in warnings[0]


def test_import_translations_workbook_not_found(tmp_path):
    """Test error when workbook file doesn't exist."""
    from src.translator import import_translations
    from src.utils.file_io import load_chunk

    chunk_path = Path("tests/fixtures/chunk_english.json")
    if not chunk_path.exists():
        pytest.skip("Chunk fixture not found")

    original_chunk = load_chunk(chunk_path)

    with pytest.raises(FileNotFoundError):
        import_translations(
            Path("nonexistent.md"), [original_chunk], tmp_path / "translated"
        )


def test_import_translations_validation_fails(tmp_path):
    """Test error when workbook validation fails."""
    from src.translator import import_translations
    from src.models import Chunk, ChunkMetadata

    # Create a chunk with different ID than in workbook
    wrong_chunk = Chunk(
        id="wrong_chunk_id",
        chapter_id="chapter_01",
        position=1,
        source_text="Test",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=10,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=1,
        ),
    )

    workbook_path = Path("tests/fixtures/workbook_completed_good.md")
    if not workbook_path.exists():
        pytest.skip("Workbook fixture not found")

    with pytest.raises(ValueError, match="Workbook validation failed"):
        import_translations(workbook_path, [wrong_chunk], tmp_path / "translated")


# ============================================================================
# extract_previous_chapter_context Tests
# ============================================================================


class TestExtractPreviousChapterContext:
    """Tests for the dual-constraint source-language context extraction."""

    THREE_PARAS = "First paragraph here.\n\nSecond paragraph here.\n\nThird paragraph here."

    def test_returns_empty_for_none(self):
        assert extract_previous_chapter_context(None) == ""

    def test_returns_empty_for_blank(self):
        assert extract_previous_chapter_context("   ") == ""

    def test_includes_source_language_label(self):
        result = extract_previous_chapter_context(self.THREE_PARAS)
        assert "Previous Section (Original English)" in result

    def test_custom_source_language_label(self):
        result = extract_previous_chapter_context(self.THREE_PARAS, source_language="French")
        assert "Previous Section (Original French)" in result

    def test_do_not_retranslate_instruction(self):
        result = extract_previous_chapter_context(self.THREE_PARAS)
        assert "Do not re-translate" in result

    def test_dual_constraint_paragraphs_met_first(self):
        """Long paragraphs — paragraph count is the binding constraint."""
        # Each paragraph is 300 chars, so 1 paragraph already exceeds min_chars=200
        # but we still need min_paragraphs=3
        long_para = "x" * 300
        text = f"{long_para}\n\n{long_para}\n\n{long_para}\n\n{long_para}"
        result = extract_previous_chapter_context(text, min_paragraphs=3, min_chars=200)
        # Should include exactly 3 paragraphs (stops as soon as both met)
        selected = [p for p in result.split("\n\n") if "x" * 10 in p]
        assert len(selected) == 3

    def test_dual_constraint_chars_met_first(self):
        """Short paragraphs — char count is the binding constraint."""
        # 15-char paragraphs; need min_paragraphs=2 and min_chars=200
        # So loop runs until 200 chars accumulated (~14 paragraphs)
        short_para = "Short line.    "  # 15 chars
        paragraphs = [short_para] * 20
        text = "\n\n".join(paragraphs)
        result = extract_previous_chapter_context(text, min_paragraphs=2, min_chars=200)
        # Should include more than 2 paragraphs
        lines = [p for p in result.split("\n\n") if short_para.strip() in p]
        assert len(lines) > 2

    def test_fewer_paragraphs_than_min_returns_all(self):
        """If the text has fewer paragraphs than min_paragraphs, return all."""
        text = "Only one paragraph."
        result = extract_previous_chapter_context(text, min_paragraphs=3, min_chars=200)
        assert "Only one paragraph." in result

    def test_takes_from_end_not_beginning(self):
        """Context must come from the END of the previous section."""
        text = "First para.\n\nMiddle para.\n\nLast para."
        result = extract_previous_chapter_context(text, min_paragraphs=1, min_chars=1)
        assert "Last para." in result

    def test_selects_last_n_paragraphs(self):
        """With min_paragraphs=2 and short text, takes the last 2."""
        text = "Para A.\n\nPara B.\n\nPara C."
        result = extract_previous_chapter_context(text, min_paragraphs=2, min_chars=1)
        assert "Para B." in result
        assert "Para C." in result
        assert "Para A." not in result

    def test_generate_workbook_threads_context_between_chunks(self, sample_chunk, sample_chunk_2):
        """Each chunk in the workbook should receive the previous chunk's source as context."""
        workbook = generate_workbook(
            chunks=[sample_chunk, sample_chunk_2],
            project_name="Test",
            context_paragraphs=1,
            min_context_chars=1,
        )
        # The second chunk's prompt should contain text from the END of the first chunk's source.
        # sample_chunk has two paragraphs; with min_paragraphs=1 and min_chars=1, only the
        # last paragraph is taken.
        second_chunk_start = workbook.find("CHUNK 2:")
        second_chunk_section = workbook[second_chunk_start:]
        assert "However little known the feelings" in second_chunk_section

    def test_generate_workbook_previous_chapter_source_for_first_chunk(self, sample_chunk):
        """First chunk should receive previous_chapter_source as context."""
        prev_source = "End of previous chapter.\n\nFinal paragraph."
        workbook = generate_workbook(
            chunks=[sample_chunk],
            project_name="Test",
            previous_chapter_source=prev_source,
            context_paragraphs=1,
            min_context_chars=1,
        )
        assert "Final paragraph." in workbook
