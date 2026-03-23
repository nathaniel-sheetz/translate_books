# Chapter Detection & Previous Chapter Context Guide

## Overview

This guide covers the new features for multi-chapter book translation:
1. **Automatic chapter detection** - Split full books into individual chapters
2. **Previous chapter context** - Include ending of previous chapter in translation prompts for continuity

## Feature 1: Automatic Chapter Detection

### What It Does

Instead of manually creating 100 separate chapter files, you can now:
- Upload your full book as a single text file
- Automatically detect chapter boundaries
- Generate individual chapter files with standardized naming

### Supported Chapter Patterns

- **Roman numerals**: Chapter I, Chapter II, Chapter III, ..., Chapter C
- **Numeric**: Chapter 1, Chapter 2, Chapter 3, etc.
- **Custom**: Define your own regex pattern

### Usage

#### Basic Usage (Roman Numerals)

```bash
python split_book.py full_book.txt --output chapters/
```

This will:
- Detect chapters with Roman numeral format (Chapter I, Chapter II, etc.)
- Create files: `chapters/chapter_01.txt`, `chapters/chapter_02.txt`, etc.
- Validate chapter sequence (warns about gaps or duplicates)
- Show detailed statistics

#### Numeric Chapters

```bash
python split_book.py book.txt --output chapters/ --pattern numeric
```

Detects: Chapter 1, Chapter 2, Chapter 3, etc.

#### Custom Chapter Patterns

```bash
python split_book.py book.txt --output chapters/ \
    --pattern custom --custom-regex "^Part \d+"
```

Use any regex pattern for unusual chapter formats.

#### Additional Options

```bash
python split_book.py book.txt --output chapters/ \
    --prefix princesa \          # Custom filename prefix
    --min-size 200 \             # Minimum chapter size (chars)
    --verbose                    # Show detailed output
```

#### Dry Run (Preview Only)

```bash
python split_book.py book.txt --output chapters/ --dry-run
```

Shows what chapters would be created without actually creating files.

### Example Output

```
[OK] Detected 3 chapters

Chapter Details:
--------------------------------------------------------------------------------
  Chapter I            |    107 words | Lines    0-   9
  Chapter II           |    115 words | Lines    9-  20
  Chapter III          |    113 words | Lines   20-  30
--------------------------------------------------------------------------------

[OK] Created 3 chapter files

Output directory: chapters/
  chapters/chapter_01.txt
  chapters/chapter_02.txt
  chapters/chapter_03.txt
```

## Feature 2: Previous Chapter Context

### What It Does

When translating Chapter 2 (or any subsequent chapter), the translation prompt can automatically include the **ending of the previous chapter** (translated text).

### Why This Matters

Books often have continuity between chapters:
- Chapter 2 might start with "The next morning..." referring to events from Chapter 1
- Character states or emotions carry over
- Narrative flow is maintained

By including the last few paragraphs of Chapter 1 in the Chapter 2 prompt, the translator (LLM or human) has better context for maintaining continuity.

### Usage

#### Step 1: Translate Chapter 1 (No Previous Context)

```bash
# Chunk Chapter 1
python chunk_chapter.py chapters/chapter_01.txt --chapter-id chapter_01

# Generate workbook (no previous chapter yet)
python generate_workbook.py chunks/chapter_01_*.json \
    --glossary glossary.json \
    --output workbook_ch01.md

# Translate manually, then import
python import_workbook.py workbook_ch01.md --output chunks/translated/

# Combine into final translated chapter
python combine_chunks.py chunks/translated/chapter_01_*.json \
    --output chapters/translated/chapter_01.txt
```

#### Step 2: Translate Chapter 2 (WITH Chapter 1 Context)

```bash
# Chunk Chapter 2
python chunk_chapter.py chapters/chapter_02.txt --chapter-id chapter_02

# Generate workbook WITH previous chapter context
python generate_workbook.py chunks/chapter_02_*.json \
    --glossary glossary.json \
    --previous-chapter chapters/translated/chapter_01.txt \
    --context-paragraphs 2 \
    --output workbook_ch02.md

# The workbook now includes the last 2 paragraphs from Chapter 1!
```

#### Step 3: Repeat for Remaining Chapters

```bash
# Chapter 3 includes Chapter 2 context
python generate_workbook.py chunks/chapter_03_*.json \
    --previous-chapter chapters/translated/chapter_02.txt \
    --output workbook_ch03.md

# And so on...
```

### Context Options

**By Paragraphs (default):**
```bash
--context-paragraphs 2    # Include last 2 paragraphs
--context-paragraphs 3    # Include last 3 paragraphs
```

**By Word Count:**
```bash
--context-words 150       # Include last 150 words
--context-words 200       # Include last 200 words
```

### How Context Appears in Prompts

The previous chapter context is inserted in the **BOOK CONTEXT** section of the translation prompt:

```
================================================================================
BOOK CONTEXT
================================================================================

[Your book context here...]

Previous Chapter Ending (Translated):
─────────────────────────────────────
[Last 2 paragraphs from Chapter 1 translation]
─────────────────────────────────────

This provides continuity context for the current chapter's opening.

================================================================================
```

## Configuration

### Project Configuration (Optional)

You can save chapter detection settings in your project config:

```json
{
  "project_name": "little_princess",
  "source_language": "en",
  "target_language": "es",
  "chapter_detection": {
    "pattern_type": "roman",
    "include_previous_context": true,
    "context_paragraphs": 2
  },
  "chunking": {
    "target_size": 1500,
    "overlap_paragraphs": 2
  }
}
```

## Complete Multi-Chapter Workflow

### For a 100-Chapter Book

```bash
# === STEP 0: Split the full book ===
python split_book.py full_book.txt --output chapters/ --verbose

# This creates:
#   chapters/chapter_01.txt
#   chapters/chapter_02.txt
#   ...
#   chapters/chapter_100.txt

# === CHAPTER 1 ===
python chunk_chapter.py chapters/chapter_01.txt --chapter-id chapter_01
python generate_workbook.py chunks/chapter_01_*.json \
    --glossary glossary.json \
    --output workbook_ch01.md

# [Translate manually using Claude.ai, ChatGPT, etc.]

python import_workbook.py workbook_ch01.md --output chunks/translated/
python combine_chunks.py chunks/translated/chapter_01_*.json \
    --output chapters/translated/chapter_01.txt

# === CHAPTER 2 ===
python chunk_chapter.py chapters/chapter_02.txt --chapter-id chapter_02
python generate_workbook.py chunks/chapter_02_*.json \
    --glossary glossary.json \
    --previous-chapter chapters/translated/chapter_01.txt \
    --output workbook_ch02.md

# [Translate manually]

python import_workbook.py workbook_ch02.md --output chunks/translated/
python combine_chunks.py chunks/translated/chapter_02_*.json \
    --output chapters/translated/chapter_02.txt

# === CHAPTERS 3-100 ===
# Repeat the same pattern, each time using the previous chapter's translation
# for context:
#   python generate_workbook.py chunks/chapter_N_*.json \
#       --previous-chapter chapters/translated/chapter_(N-1).txt \
#       --output workbook_chN.md
```

## Benefits

### Before (Manual Process)
1. ❌ Manually split 100-chapter book into 100 separate files
2. ❌ No context from previous chapter
3. ❌ Potential continuity issues in translation
4. ❌ Time-consuming file management

### After (Automated Process)
1. ✅ One command to split entire book
2. ✅ Automatic chapter detection and validation
3. ✅ Previous chapter context for better continuity
4. ✅ Standardized chapter file naming
5. ✅ Reduced manual work

## Validation & Error Handling

### Chapter Detection Validation

The system automatically checks for:
- **Gaps in sequence**: Warns if chapters are missing (e.g., Chapter 1, 2, 4...)
- **Duplicates**: Warns if same chapter number appears twice
- **Very short chapters**: Flags chapters < 500 chars as potential false positives
- **Non-standard numbering**: Warns if first chapter isn't Chapter 1

### Example Validation Output

```
Warnings:
  [!] Gap in sequence: Missing chapter(s) [3]
  [!] Chapter 5 is very short (245 chars). May be a false positive.
  [!] First chapter is 2, not 1. Book may have prologue or preface.
```

## Edge Cases

### First Chapter
- No previous chapter context (empty string)
- Works normally

### Previous Chapter Not Yet Translated
- Just skip the `--previous-chapter` flag
- Or system will return empty context if file doesn't exist

### Large Books
- Chapter detection handles 100+ chapters
- Memory efficient (processes line by line)
- Progress tracking for validation

## Testing

Test with the included sample file:

```bash
# Test chapter detection
python split_book.py test_book_sample.txt --output test_chapters/ --verbose

# Test previous chapter context
python chunk_chapter.py test_chapters/chapter_02.txt --chapter-id chapter_02
python generate_workbook.py chunks/chapter_02_*.json \
    --previous-chapter test_chapters/translated/chapter_01.txt \
    --output test_workbook.md

# Verify context appears in workbook
grep -A 10 "Previous Chapter" test_workbook.md
```

## Troubleshooting

### No Chapters Detected

**Problem**: `Error: No chapters detected with pattern type 'roman'`

**Solutions**:
1. Check your book's chapter format:
   ```bash
   head -50 your_book.txt  # Look at first 50 lines
   ```
2. Try different pattern:
   ```bash
   python split_book.py book.txt --output chapters/ --pattern numeric
   ```
3. Use custom regex:
   ```bash
   python split_book.py book.txt --output chapters/ \
       --pattern custom --custom-regex "^CAPÍTULO \d+"
   ```

### Chapters Too Short

**Problem**: Many warnings about short chapters

**Solutions**:
1. Adjust minimum size:
   ```bash
   python split_book.py book.txt --output chapters/ --min-size 50
   ```
2. Check for false positives (e.g., "Chapter" in dialogue)

### Previous Chapter Context Not Appearing

**Problem**: Workbook doesn't show previous chapter context

**Solutions**:
1. Verify previous chapter file exists:
   ```bash
   ls chapters/translated/chapter_01.txt
   ```
2. Check file is not empty
3. Verify you used `--previous-chapter` flag

## Implementation Details

### Files Modified/Created

1. **`src/book_splitter.py`** (NEW)
   - Chapter detection logic
   - Roman numeral conversion
   - Chapter validation

2. **`split_book.py`** (NEW)
   - CLI interface for book splitting

3. **`src/models.py`** (UPDATED)
   - Added `ChapterDetectionConfig`
   - Integrated into `ProjectConfig`

4. **`src/translator.py`** (UPDATED)
   - Added `extract_previous_chapter_context()` function
   - Updated `generate_workbook()` to accept previous chapter

5. **`generate_workbook.py`** (UPDATED)
   - Added `--previous-chapter` flag
   - Added `--context-paragraphs` and `--context-words` options

6. **`prompts/translation.txt`** (UPDATED)
   - Added `{{previous_chapter_context}}` variable
   - Integrated into BOOK CONTEXT section

### Data Flow

```
Full Book (single file)
    ↓
split_book.py
    ↓
Individual Chapters (chapter_01.txt, chapter_02.txt, ...)
    ↓
chunk_chapter.py (for each chapter)
    ↓
Chunks (chapter_01_chunk_000.json, ...)
    ↓
generate_workbook.py (with --previous-chapter)
    ↓
Workbook with Context (workbook_ch02.md)
    ↓
Manual Translation
    ↓
import_workbook.py
    ↓
Translated Chunks
    ↓
combine_chunks.py
    ↓
Translated Chapter (chapter_02_translated.txt)
```

## Related Documentation

- [GETTING_STARTED.md](GETTING_STARTED.md) - Complete workflow guide
- [README.md](README.md) - Project overview
- [USAGE.md](USAGE.md) - Detailed command reference
- [PROMPT_GUIDE.md](PROMPT_GUIDE.md) - Customizing translation prompts

---

**Last Updated**: 2026-02-16
