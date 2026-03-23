#!/usr/bin/env python3
"""
Manual test script for workbook generation.

Tests the translator module with real fixtures and outputs a sample workbook.
Run with: python3 manual_test_workbook.py (from project root)
"""

from datetime import datetime
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.models import Chunk, ChunkMetadata, ChunkStatus, Glossary, GlossaryTerm, GlossaryTermType, StyleGuide
from src.translator import generate_workbook, save_workbook
from src.utils.file_io import load_chunk, load_glossary


def test_basic_workbook():
    """Test 1: Basic workbook generation with minimal chunk."""
    print("\n" + "=" * 70)
    print("TEST 1: Basic Workbook Generation")
    print("=" * 70)

    chunk = Chunk(
        id="test_chunk_001",
        chapter_id="test_chapter",
        position=1,
        source_text="This is a test chunk.\n\nIt has two paragraphs.",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=47,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=2,
            word_count=9,
        ),
        status=ChunkStatus.PENDING,
    )

    workbook = generate_workbook(
        chunks=[chunk],
        project_name="Test Project",
    )

    # Verify key sections
    assert "Translation Workbook: Test Project" in workbook
    assert "CHUNK 1: test_chunk_001" in workbook
    assert "PROMPT TO COPY" in workbook
    assert "PASTE TRANSLATION HERE" in workbook
    assert "This is a test chunk" in workbook

    print("✓ Basic workbook structure correct")
    print(f"✓ Workbook length: {len(workbook)} characters")


def test_workbook_with_glossary():
    """Test 2: Workbook with glossary."""
    print("\n" + "=" * 70)
    print("TEST 2: Workbook with Glossary")
    print("=" * 70)

    chunk = Chunk(
        id="test_chunk_001",
        chapter_id="test_chapter",
        position=1,
        source_text="Mr. Darcy went to Pemberley.",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=28,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=5,
        ),
    )

    glossary = Glossary(
        terms=[
            GlossaryTerm(
                english="Mr. Darcy",
                spanish="Sr. Darcy",
                type=GlossaryTermType.CHARACTER,
            ),
            GlossaryTerm(
                english="Pemberley",
                spanish="Pemberley",
                type=GlossaryTermType.PLACE,
            ),
        ],
        version="1.0",
    )

    workbook = generate_workbook(
        chunks=[chunk],
        glossary=glossary,
        project_name="Test",
    )

    # Verify glossary section
    assert "Glossary Reference" in workbook
    assert "CHARACTER NAMES:" in workbook
    assert "Mr. Darcy → Sr. Darcy" in workbook
    assert "PLACE NAMES:" in workbook
    assert "Pemberley → Pemberley" in workbook

    print("✓ Glossary section present")
    print("✓ Glossary formatted correctly")


def test_workbook_with_style_guide():
    """Test 3: Workbook with style guide."""
    print("\n" + "=" * 70)
    print("TEST 3: Workbook with Style Guide")
    print("=" * 70)

    chunk = Chunk(
        id="test_chunk_001",
        chapter_id="test_chapter",
        position=1,
        source_text="Test text.",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=10,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=2,
        ),
    )

    style_guide = StyleGuide(
        content="TONE: Formal\nFORMALITY: High\nDIALECT: Neutral Spanish",
        version="1.0",
    )

    workbook = generate_workbook(
        chunks=[chunk],
        style_guide=style_guide,
        project_name="Test",
    )

    # Verify style guide section
    assert "Style Guide" in workbook
    assert "TONE: Formal" in workbook
    assert "FORMALITY: High" in workbook

    print("✓ Style guide section present")
    print("✓ Style guide content included")


def test_workbook_multiple_chunks():
    """Test 4: Workbook with multiple chunks."""
    print("\n" + "=" * 70)
    print("TEST 4: Multiple Chunks")
    print("=" * 70)

    chunks = [
        Chunk(
            id=f"chunk_{i:03d}",
            chapter_id="chapter_01",
            position=i,
            source_text=f"This is chunk {i}.",
            metadata=ChunkMetadata(
                char_start=i * 20,
                char_end=(i + 1) * 20,
                overlap_start=0,
                overlap_end=0,
                paragraph_count=1,
                word_count=4,
            ),
        )
        for i in range(1, 4)
    ]

    workbook = generate_workbook(
        chunks=chunks,
        project_name="Test",
    )

    # Verify all chunks present
    assert "CHUNK 1: chunk_001" in workbook
    assert "CHUNK 2: chunk_002" in workbook
    assert "CHUNK 3: chunk_003" in workbook

    # Verify order
    pos1 = workbook.find("CHUNK 1")
    pos2 = workbook.find("CHUNK 2")
    pos3 = workbook.find("CHUNK 3")
    assert pos1 < pos2 < pos3

    print("✓ All 3 chunks present")
    print("✓ Chunks in correct order")


def test_workbook_with_real_fixture():
    """Test 5: Real Pride and Prejudice fixture."""
    print("\n" + "=" * 70)
    print("TEST 5: Real Pride and Prejudice Fixture")
    print("=" * 70)

    chunk_path = Path("tests/fixtures/chunk_english.json")
    glossary_path = Path("tests/fixtures/glossary_sample.json")

    if not chunk_path.exists():
        print("⚠ Skipping - fixture not found")
        return

    chunk = load_chunk(chunk_path)
    glossary = load_glossary(glossary_path) if glossary_path.exists() else None

    workbook = generate_workbook(
        chunks=[chunk],
        glossary=glossary,
        project_name="Pride and Prejudice",
        source_language="English",
        target_language="Spanish",
    )

    # Verify content
    assert "Pride and Prejudice" in workbook
    assert "It is a truth universally acknowledged" in workbook
    assert "CHUNK 1" in workbook

    print("✓ Real fixture loaded successfully")
    print(f"✓ Chunk ID: {chunk.id}")
    print(f"✓ Word count: {chunk.metadata.word_count}")

    # Save to file
    output_path = Path("test_output_workbook.md")
    save_workbook(workbook, output_path)
    print(f"✓ Workbook saved to: {output_path}")
    print(f"✓ File size: {output_path.stat().st_size} bytes")


def test_file_saving(tmp_path=Path("test_output")):
    """Test 6: File saving functionality."""
    print("\n" + "=" * 70)
    print("TEST 6: File Saving")
    print("=" * 70)

    chunk = Chunk(
        id="test",
        chapter_id="test",
        position=1,
        source_text="Test with Spanish: ñ, á, é, í, ó, ú",
        metadata=ChunkMetadata(
            char_start=0,
            char_end=40,
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=7,
        ),
    )

    workbook = generate_workbook(chunks=[chunk], project_name="Test")

    # Save to nested directory
    output_path = tmp_path / "nested" / "workbook.md"
    save_workbook(workbook, output_path)

    # Verify file exists and has correct content
    assert output_path.exists()
    content = output_path.read_text(encoding="utf-8")
    assert "ñ" in content
    assert "á" in content

    print(f"✓ File saved to: {output_path}")
    print("✓ UTF-8 encoding preserved Spanish characters")
    print("✓ Nested directories created successfully")


def main():
    """Run all tests."""
    print("\n" + "=" * 70)
    print("WORKBOOK GENERATION MANUAL TESTS")
    print("=" * 70)

    try:
        test_basic_workbook()
        test_workbook_with_glossary()
        test_workbook_with_style_guide()
        test_workbook_multiple_chunks()
        test_workbook_with_real_fixture()
        test_file_saving()

        print("\n" + "=" * 70)
        print("ALL TESTS PASSED ✓")
        print("=" * 70)
        print("\nWorkbook generation is working correctly!")
        print("Check 'test_output_workbook.md' for sample output.")

    except AssertionError as e:
        print(f"\n✗ TEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
