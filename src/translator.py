"""
Translation workbook generation and parsing for manual translation workflow.

This module provides functions to generate formatted workbooks that include
complete prompts for each chunk, ready to copy/paste into any LLM interface,
and to parse completed workbooks to extract translations.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.models import Chunk, ChunkStatus, Glossary, StyleGuide
from src.utils.file_io import (
    load_prompt_template,
    render_prompt,
    format_glossary_for_prompt,
    save_chunk,
)


def _extract_tail_paragraphs(
    text: str,
    min_paragraphs: int = 3,
    min_chars: int = 200,
    max_chars: Optional[int] = None,
) -> str:
    """
    Extract paragraphs from the end of text.

    Rules (applied in this priority order):
    1. Always return complete paragraphs — never split mid-paragraph.
    2. Keep adding paragraphs until BOTH min_paragraphs AND min_chars are met.
       min_paragraphs overrides max_chars: we always include at least that many
       full paragraphs even if their total exceeds max_chars.
    3. Once both minimums are satisfied, stop before adding a paragraph that
       would push the total over max_chars.
    4. If no single full paragraph fits within [min_chars, max_chars] (e.g. all
       paragraphs are larger than max_chars), include the paragraph anyway —
       max_chars is a soft ceiling, not a hard truncation point.

    Args:
        text: Text to extract from.
        min_paragraphs: Minimum number of paragraphs to include (hard lower bound).
        min_chars: Minimum total characters to include (soft lower bound).
        max_chars: Maximum total characters to include (soft upper bound).
                   None means no upper limit.

    Returns:
        Extracted paragraphs joined by double newlines, or "" if text is empty.
    """
    if not text or not text.strip():
        return ""

    paragraphs = [
        p.strip()
        for p in re.split(r'\n\s*\n', text.strip())
        if p.strip()
    ]
    if not paragraphs:
        return ""

    selected = []
    char_count = 0

    for para in reversed(paragraphs):
        both_mins_met = len(selected) >= min_paragraphs and char_count >= min_chars
        would_exceed_max = max_chars is not None and (char_count + len(para)) > max_chars

        if both_mins_met and would_exceed_max:
            break

        selected.insert(0, para)
        char_count += len(para)

        if len(selected) >= min_paragraphs and char_count >= min_chars:
            break

    return '\n\n'.join(selected)


def extract_previous_chapter_context(
    previous_section_text: Optional[str],
    previous_translated_text: Optional[str] = None,
    context_language: str = "both",
    min_paragraphs: int = 3,
    min_chars: int = 200,
    max_chars: Optional[int] = None,
    source_language: str = "English",
    target_language: str = "Spanish",
) -> str:
    """
    Extract context from the end of the previous section for prompt insertion.

    Uses dual-constraint strategy: adds paragraphs from the end until BOTH
    min_paragraphs AND min_chars are satisfied.

    Args:
        previous_section_text: Source-language text from previous chunk/chapter.
        previous_translated_text: Target-language translation of previous section.
        context_language: Which text to include: "both", "source", or "translation".
        min_paragraphs: Minimum number of paragraphs to include (default: 3).
        min_chars: Minimum total characters to include (default: 200).
        source_language: Source language name for labeling.
        target_language: Target language name for labeling.

    Returns:
        Formatted context block for prompt insertion, or "" if no text provided.

    Example:
        >>> prev_text = "Paragraph 1.\\n\\nParagraph 2.\\n\\nParagraph 3."
        >>> context = extract_previous_chapter_context(prev_text, min_paragraphs=2)
        >>> "Paragraph 2" in context
        True
    """
    source_tail = _extract_tail_paragraphs(
        previous_section_text, min_paragraphs, min_chars, max_chars
    ) if previous_section_text else ""

    translated_tail = _extract_tail_paragraphs(
        previous_translated_text, min_paragraphs, min_chars, max_chars
    ) if previous_translated_text else ""

    # Determine what to show based on context_language and what's available
    show_source = context_language in ("both", "source") and source_tail
    show_translation = context_language in ("both", "translation") and translated_tail

    if not show_source and not show_translation:
        # Fall back: show whatever is available
        if source_tail:
            show_source = True
        elif translated_tail:
            show_translation = True
        else:
            return ""

    sections = []
    if show_source:
        sections.append(
            f"Previous Section (Original {source_language}):\n"
            f"─────────────────────────────────────────────\n"
            f"{source_tail}\n"
            f"─────────────────────────────────────────────"
        )
    if show_translation:
        sections.append(
            f"Previous Section ({target_language} Translation):\n"
            f"─────────────────────────────────────────────\n"
            f"{translated_tail}\n"
            f"─────────────────────────────────────────────"
        )

    body = "\n\n".join(sections)

    return f"""
{body}

This provides continuity context. Do not re-translate it — use it only to
maintain consistent voice, terminology, and narrative flow.
"""


def generate_workbook(
    chunks: list[Chunk],
    glossary: Optional[Glossary] = None,
    style_guide: Optional[StyleGuide] = None,
    project_name: str = "Translation Project",
    source_language: str = "English",
    target_language: str = "Spanish",
    book_context: str = "",
    previous_chapter_source: Optional[str] = None,
    previous_chapter_translated: Optional[str] = None,
    context_paragraphs: int = 3,
    min_context_chars: int = 200,
    context_language: str = "both",
) -> str:
    """
    Generate a formatted workbook for manual translation.

    Creates a document with complete prompts for each chunk that can be
    copy/pasted into any LLM interface (Claude.ai, ChatGPT, etc.).

    Args:
        chunks: List of chunks to include in workbook
        glossary: Optional glossary for translation terms
        style_guide: Optional style guide with translation preferences
        project_name: Name of the translation project
        source_language: Source language (default: English)
        target_language: Target language (default: Spanish)
        book_context: Optional context about the book
        previous_chapter_source: Optional source text from end of previous chapter
        previous_chapter_translated: Optional translated text from end of previous chapter
        context_paragraphs: Minimum paragraphs of context to include (default: 3)
        min_context_chars: Minimum characters of context to include (default: 200)
        context_language: What to include in context: "both", "source", or "translation"

    Returns:
        Formatted workbook content as string

    Example:
        >>> chunks = [chunk1, chunk2, chunk3]
        >>> glossary = load_glossary("glossary.json")
        >>> prev_chapter = Path("chapters/source/chapter_01.txt").read_text()
        >>> workbook = generate_workbook(chunks, glossary, previous_chapter_source=prev_chapter)
        >>> Path("workbook.md").write_text(workbook)
    """
    if not chunks:
        raise ValueError("Cannot generate workbook with no chunks")

    # Sort chunks by position to ensure correct order
    sorted_chunks = sorted(chunks, key=lambda c: c.position)

    # Build workbook sections
    sections = []

    # Header
    sections.append(_generate_header(
        project_name=project_name,
        chunk_count=len(sorted_chunks),
        timestamp=datetime.now()
    ))

    # Instructions
    sections.append(_generate_instructions())

    # Glossary reference (if provided)
    if glossary:
        sections.append(_generate_glossary_section(glossary))

    # Style guide reference (if provided)
    if style_guide:
        sections.append(_generate_style_guide_section(style_guide))

    # Separator before chunks
    sections.append("\n" + "═" * 70 + "\n")

    # Thread context through chunks: each chunk receives the previous chunk's
    # source and translated text (or the previous chapter's for the first chunk).
    prev_source = previous_chapter_source
    prev_translated = previous_chapter_translated
    for chunk in sorted_chunks:
        previous_context = extract_previous_chapter_context(
            prev_source,
            previous_translated_text=prev_translated,
            context_language=context_language,
            min_paragraphs=context_paragraphs,
            min_chars=min_context_chars,
            source_language=source_language,
            target_language=target_language,
        )
        sections.append(_generate_chunk_section(
            chunk=chunk,
            glossary=glossary,
            style_guide=style_guide,
            project_name=project_name,
            source_language=source_language,
            target_language=target_language,
            book_context=book_context,
            total_chunks=len(sorted_chunks),
            previous_chapter_context=previous_context
        ))
        prev_source = chunk.source_text
        prev_translated = chunk.translated_text

    # Footer
    sections.append(_generate_footer(timestamp=datetime.now()))

    return "\n".join(sections)


def save_workbook(
    workbook_content: str,
    output_path: Path,
    encoding: str = "utf-8"
) -> None:
    """
    Save workbook content to file.

    Args:
        workbook_content: Formatted workbook string
        output_path: Path where workbook should be saved
        encoding: Text encoding (default: utf-8 for Spanish characters)

    Raises:
        OSError: If file cannot be written

    Example:
        >>> workbook = generate_workbook(chunks, glossary)
        >>> save_workbook(workbook, Path("workbook.md"))
    """
    # Ensure parent directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write with UTF-8 encoding to support Spanish characters
    output_path.write_text(workbook_content, encoding=encoding)


def _generate_header(
    project_name: str,
    chunk_count: int,
    timestamp: datetime
) -> str:
    """Generate workbook header section."""
    return f"""# Translation Workbook: {project_name}

**Generated**: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}
**Total Chunks**: {chunk_count}

This workbook contains complete prompts for manual translation.
Follow the instructions below to translate each chunk.
"""


def _generate_instructions() -> str:
    """Generate user instructions section."""
    return """
## How to Use This Workbook

1. **For each chunk below**:
   - Locate the "PROMPT TO COPY" section
   - Copy everything between the separator lines (─────)
   - Paste into your LLM interface (Claude.ai, ChatGPT, etc.)
   - Copy the LLM's translation response
   - Paste it into the "PASTE TRANSLATION HERE" section

2. **Save your work** regularly as you translate

3. **When all chunks are complete**:
   - Save this workbook
   - Import translations using: `python import_workbook.py workbook.md --output chunks/translated/`

4. **Important notes**:
   - Do NOT edit the chunk metadata sections
   - Keep the separator lines intact
   - Preserve paragraph structure in translations
   - Use the glossary terms consistently
"""


def _generate_glossary_section(glossary: Glossary) -> str:
    """Generate glossary reference section."""
    formatted = format_glossary_for_prompt(glossary)
    return f"""
## Glossary Reference

Use these translations consistently throughout all chunks:

{formatted}

**Version**: {glossary.version}
"""


def _generate_style_guide_section(style_guide: StyleGuide) -> str:
    """Generate style guide reference section."""
    return f"""
## Style Guide

{style_guide.content}

**Version**: {style_guide.version}
"""


def _generate_chunk_section(
    chunk: Chunk,
    glossary: Optional[Glossary],
    style_guide: Optional[StyleGuide],
    project_name: str,
    source_language: str,
    target_language: str,
    book_context: str,
    total_chunks: int,
    previous_chapter_context: str = ""
) -> str:
    """Generate complete section for a single chunk."""
    # Load and render the prompt
    template = load_prompt_template()

    # Prepare variables for prompt rendering
    variables = {
        "book_title": project_name,
        "source_text": chunk.source_text,
        "target_language": target_language,
        "source_language": source_language,
        "glossary": format_glossary_for_prompt(glossary) if glossary else "No glossary provided.",
        "style_guide": style_guide.content if style_guide else "No style guide provided.",
        "context": book_context if book_context else "",
        "chapter_info": f"Chapter {chunk.chapter_id}, Chunk {chunk.position} of {total_chunks}",
        "previous_chapter_context": previous_chapter_context
    }

    # Render the complete prompt
    rendered_prompt = render_prompt(template, variables)

    # Strip header comments (everything before first === separator)
    # These are documentation for template editors, not LLM instructions
    separator = "=" * 80
    if separator in rendered_prompt:
        parts = rendered_prompt.split(separator, 1)
        if len(parts) > 1:
            # Keep the separator and everything after it
            rendered_prompt = separator + parts[1]

    # Build chunk section
    return f"""
## CHUNK {chunk.position}: {chunk.id}

**Position**: {chunk.position} of {total_chunks}
**Chapter**: {chunk.chapter_id}
**Word Count**: {chunk.metadata.word_count} words
**Paragraphs**: {chunk.metadata.paragraph_count}

### PROMPT TO COPY (Paste this entire section into your LLM):
─────────────────────────────────────────────────────────────────────
{rendered_prompt}
─────────────────────────────────────────────────────────────────────

### PASTE TRANSLATION HERE:




### METADATA (Do not edit this section):
```
Chunk ID: {chunk.id}
Position: {chunk.position}
Chapter ID: {chunk.chapter_id}
Source Word Count: {chunk.metadata.word_count}
Source Paragraph Count: {chunk.metadata.paragraph_count}
Created: {chunk.created_at.strftime('%Y-%m-%d %H:%M:%S')}
```
─────────────────────────────────────────────────────────────────────
"""


def _generate_footer(timestamp: datetime) -> str:
    """Generate workbook footer."""
    return f"""

═══════════════════════════════════════════════════════════════════

End of Translation Workbook
Generated: {timestamp.strftime('%Y-%m-%d %H:%M:%S')}

After completing all translations, import using:
    python import_workbook.py workbook.md --output chunks/translated/

═══════════════════════════════════════════════════════════════════
"""

# ============================================================================
# Workbook Parsing Functions
# ============================================================================


def parse_workbook(workbook_path: Path) -> dict[str, str]:
    """
    Parse a completed workbook to extract translations.

    Extracts the translated text for each chunk from the workbook's
    "PASTE TRANSLATION HERE" sections.

    Args:
        workbook_path: Path to completed workbook file

    Returns:
        Dictionary mapping chunk IDs to translation text

    Raises:
        FileNotFoundError: If workbook file doesn't exist
        ValueError: If workbook is malformed or has no chunks

    Example:
        >>> translations = parse_workbook(Path("workbook_completed.md"))
        >>> print(translations["ch01_chunk_001"])
        "Es una verdad universalmente reconocida..."
    """
    if not workbook_path.exists():
        raise FileNotFoundError(f"Workbook file not found: {workbook_path}")

    # Read workbook with UTF-8 encoding
    content = workbook_path.read_text(encoding="utf-8")

    # Extract translations using regex
    # Pattern: ## CHUNK N: chunk_id ... ### PASTE TRANSLATION HERE:\n(text)### METADATA
    pattern = r'## CHUNK \d+: ([\w_]+).*?### PASTE TRANSLATION HERE:\s*\n(.*?)\n### METADATA'

    matches = re.findall(pattern, content, re.DOTALL)

    if not matches:
        raise ValueError("No chunks found in workbook. Check workbook format.")

    translations = {}

    for chunk_id, translation_text in matches:
        # Clean the translation text
        cleaned = clean_translation_text(translation_text)

        # Check for empty or placeholder translations
        if not cleaned:
            continue  # Skip empty translations (will warn later)

        if is_placeholder_text(cleaned):
            continue  # Skip placeholder text (will warn later)

        translations[chunk_id] = cleaned

    return translations


def validate_workbook_structure(
    workbook_content: str,
    expected_chunk_ids: list[str]
) -> tuple[bool, list[str]]:
    """
    Validate workbook structure before parsing.

    Checks that the workbook has all required sections and that chunk IDs
    match the expected list.

    Args:
        workbook_content: The workbook file content
        expected_chunk_ids: List of chunk IDs that should be present

    Returns:
        Tuple of (is_valid, list_of_error_messages)

    Example:
        >>> content = Path("workbook.md").read_text()
        >>> is_valid, errors = validate_workbook_structure(content, ["ch01_chunk_001"])
        >>> if not is_valid:
        ...     print("\\n".join(errors))
    """
    errors = []

    # Check for header
    if "Translation Workbook:" not in workbook_content:
        errors.append("Missing workbook header")

    # Check for instructions
    if "How to Use This Workbook" not in workbook_content:
        errors.append("Missing usage instructions")

    # Find all chunk sections
    chunk_pattern = r'## CHUNK \d+: ([\w_]+)'
    found_chunk_ids = re.findall(chunk_pattern, workbook_content)

    if not found_chunk_ids:
        errors.append("No chunk sections found in workbook")
        return (False, errors)

    # Check for required sections in each chunk
    for chunk_id in found_chunk_ids:
        chunk_section_pattern = f'## CHUNK \\d+: {re.escape(chunk_id)}(.*?)(?=## CHUNK|═════|$)'
        chunk_match = re.search(chunk_section_pattern, workbook_content, re.DOTALL)

        if chunk_match:
            chunk_section = chunk_match.group(1)

            if "### PROMPT TO COPY" not in chunk_section:
                errors.append(f"Chunk '{chunk_id}' missing PROMPT TO COPY section")

            if "### PASTE TRANSLATION HERE:" not in chunk_section:
                errors.append(f"Chunk '{chunk_id}' missing PASTE TRANSLATION HERE section")

            if "### METADATA" not in chunk_section:
                errors.append(f"Chunk '{chunk_id}' missing METADATA section")

    # Check for chunk ID mismatches
    expected_set = set(expected_chunk_ids)
    found_set = set(found_chunk_ids)

    missing = expected_set - found_set
    extra = found_set - expected_set

    if missing:
        errors.append(f"Missing expected chunks: {', '.join(sorted(missing))}")

    if extra:
        errors.append(f"Unexpected chunks in workbook: {', '.join(sorted(extra))}")

    # Check for duplicates
    if len(found_chunk_ids) != len(found_set):
        duplicates = [cid for cid in found_set if found_chunk_ids.count(cid) > 1]
        errors.append(f"Duplicate chunk IDs: {', '.join(duplicates)}")

    is_valid = len(errors) == 0
    return (is_valid, errors)


def import_translations(
    workbook_path: Path,
    original_chunks: list[Chunk],
    output_dir: Path
) -> tuple[list[Chunk], list[str]]:
    """
    Import translations from completed workbook and save updated chunks.

    Main orchestration function that parses the workbook, validates it,
    updates chunks with translations, and saves them to the output directory.

    Args:
        workbook_path: Path to completed workbook file
        original_chunks: List of original (untranslated) chunks
        output_dir: Directory to save translated chunk JSON files

    Returns:
        Tuple of (updated_chunks, warnings)
        - updated_chunks: List of chunks with translations
        - warnings: List of warning messages (non-fatal issues)

    Raises:
        FileNotFoundError: If workbook file doesn't exist
        ValueError: If workbook validation fails

    Example:
        >>> chunks = [load_chunk("ch01_chunk_001.json")]
        >>> updated, warnings = import_translations(
        ...     Path("workbook.md"),
        ...     chunks,
        ...     Path("chunks/translated/")
        ... )
        >>> for warning in warnings:
        ...     print(f"Warning: {warning}")
    """
    warnings = []

    # Validate workbook exists
    if not workbook_path.exists():
        raise FileNotFoundError(f"Workbook file not found: {workbook_path}")

    # Read workbook
    workbook_content = workbook_path.read_text(encoding="utf-8")

    # Validate structure
    expected_ids = [chunk.id for chunk in original_chunks]
    is_valid, errors = validate_workbook_structure(workbook_content, expected_ids)

    if not is_valid:
        error_msg = "Workbook validation failed:\n  " + "\n  ".join(errors)
        raise ValueError(error_msg)

    # Parse translations
    translations = parse_workbook(workbook_path)

    # Create output directory if needed
    output_dir.mkdir(parents=True, exist_ok=True)

    # Update chunks with translations
    updated_chunks = []

    for chunk in original_chunks:
        if chunk.id not in translations:
            warnings.append(f"No translation found for chunk '{chunk.id}'")
            continue

        # Update chunk with translation
        updated_chunk = update_chunk_with_translation(chunk, translations[chunk.id])

        # Save to output directory
        output_path = output_dir / f"{chunk.id}.json"
        save_chunk(updated_chunk, output_path)

        updated_chunks.append(updated_chunk)

    return (updated_chunks, warnings)


def update_chunk_with_translation(chunk: Chunk, translation: str) -> Chunk:
    """
    Update a chunk with translation text and metadata.

    Creates a copy of the chunk with translation text, updated status,
    and timestamp.

    Args:
        chunk: Original chunk to update
        translation: Translated text

    Returns:
        Updated chunk with translation

    Example:
        >>> chunk = Chunk(id="test", ...)
        >>> updated = update_chunk_with_translation(chunk, "Traducción...")
        >>> print(updated.status)
        ChunkStatus.TRANSLATED
    """
    # Create a copy of the chunk data
    chunk_data = chunk.model_dump()

    # Update with translation
    chunk_data["translated_text"] = translation
    chunk_data["status"] = ChunkStatus.TRANSLATED
    chunk_data["translated_at"] = datetime.now()

    # Create new chunk with updated data
    return Chunk(**chunk_data)


def extract_chunk_id_from_metadata(metadata_section: str) -> str:
    """
    Extract chunk ID from metadata section.

    Parses the metadata block to find the "Chunk ID:" line and extracts
    the ID value.

    Args:
        metadata_section: The metadata section text

    Returns:
        Chunk ID string

    Raises:
        ValueError: If chunk ID line not found

    Example:
        >>> metadata = "Chunk ID: ch01_chunk_003\\nPosition: 3\\n..."
        >>> chunk_id = extract_chunk_id_from_metadata(metadata)
        >>> print(chunk_id)
        "ch01_chunk_003"
    """
    # Find line starting with "Chunk ID:"
    pattern = r'Chunk ID:\s*([\w_]+)'
    match = re.search(pattern, metadata_section)

    if not match:
        raise ValueError("Could not find 'Chunk ID:' in metadata section")

    return match.group(1).strip()


def clean_translation_text(text: str) -> str:
    """
    Clean translation text of common text editor artifacts.

    Removes smart quotes, normalizes line endings, and strips excess
    whitespace while preserving paragraph structure.

    Args:
        text: Raw translation text from workbook

    Returns:
        Cleaned translation text

    Example:
        >>> raw = ""This is text" with smart quotes\\r\\n"
        >>> clean = clean_translation_text(raw)
        >>> print(clean)
        '"This is text" with smart quotes'
    """
    # Convert smart quotes to straight quotes
    # Left and right double quotes: " (U+201C) and " (U+201D) -> "
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    # Left and right single quotes: ' (U+2018) and ' (U+2019) -> '
    text = text.replace('\u2018', "'").replace('\u2019', "'")

    # Normalize line endings
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    # Strip trailing spaces on each line
    lines = text.split('\n')
    lines = [line.rstrip() for line in lines]
    text = '\n'.join(lines)

    # Collapse excessive blank lines (more than 2 consecutive) to double newline
    while '\n\n\n' in text:
        text = text.replace('\n\n\n', '\n\n')

    # Strip leading/trailing whitespace from entire text
    text = text.strip()

    return text


def is_placeholder_text(text: str) -> bool:
    """
    Check if text appears to be placeholder/template text.

    Detects common placeholder patterns that indicate the user hasn't
    actually translated the text yet.

    Args:
        text: Text to check

    Returns:
        True if text appears to be a placeholder

    Example:
        >>> is_placeholder_text("[TRANSLATION]")
        True
        >>> is_placeholder_text("Esta es la traducción.")
        False
    """
    text_lower = text.lower().strip()

    # Common placeholder patterns
    placeholders = [
        "[translation]",
        "paste your translation here",
        "paste translation here",
        "todo",
        "[insert translation]",
        "...",
        "[traducción]",
        "pegar traducción aquí",
    ]

    # Check for exact matches
    if text_lower in placeholders:
        return True

    # Check if text is just whitespace
    if not text.strip():
        return True

    # Check if text is just punctuation and whitespace
    if re.match(r'^[\s\.\,\;\:\!\?\-]+$', text):
        return True

    return False
