# Getting Started - Manual Translation Workflow

This guide will walk you through translating a book chapter manually using the workbook-based workflow.

## Prerequisites

### 1. Install Python Dependencies

```bash
cd C:\Users\Nathaniel\Cursor\book_translation
pip install -r requirements.txt
```

This installs:
- `pydantic` - Data models
- `pyenchant` - Dictionary checking
- `rich` - Terminal formatting
- `pytest` - Testing (optional)

### 2. Install Spanish Dictionaries

For dictionary evaluation, install Spanish dictionaries:

**Windows:**
```bash
# Download and install enchant from: https://github.com/AbiWord/enchant/releases
# Then install Spanish dictionaries via enchant
```

**macOS/Linux:**
```bash
# Install via package manager
brew install enchant        # macOS
sudo apt install enchant    # Ubuntu/Debian

# Dictionaries are typically auto-installed with enchant
```

See [DICTIONARY_SETUP.md](DICTIONARY_SETUP.md) for detailed instructions.

## Workflows

There are two main workflows depending on your needs:

1. **Single Chapter Workflow** - For translating one chapter at a time
2. **Multi-Chapter Book Workflow** - For translating entire books with automatic chapter detection and context

## Multi-Chapter Book Workflow (NEW!)

If you're translating a full book with multiple chapters (especially large books with 50+ chapters), use this streamlined workflow.

### Step 0: Split Your Book into Chapters

If you have a single book file, automatically detect and split chapters:

```bash
# For books with Roman numeral chapters (Chapter I, Chapter II, etc.)
python split_book.py full_book.txt --output chapters/

# For books with numeric chapters (Chapter 1, Chapter 2, etc.)
python split_book.py book.txt --output chapters/ --pattern numeric

# With custom filename prefix
python split_book.py book.txt --output chapters/ --prefix little_princess
```

**What this does:**
- Automatically detects chapter boundaries
- Creates individual chapter files (chapter_01.txt, chapter_02.txt, etc.)
- Validates chapter sequence (detects gaps or duplicates)
- Saves you from manually creating 100+ files!

**Output:**
```
chapters/
  ├── chapter_01.txt
  ├── chapter_02.txt
  ├── chapter_03.txt
  └── ...
```

### Multi-Chapter Workflow with Context

The key benefit: **previous chapter context**. When translating Chapter 2, the system can include the ending of Chapter 1 (translated) in the prompt, providing continuity for better narrative flow.

**Example: Translating a 100-chapter book**

```bash
# === CHAPTER 1 (no previous context) ===
python chunk_chapter.py chapters/chapter_01.txt --chapter-id chapter_01
python generate_workbook.py chunks/chapter_01_*.json \
    --glossary glossary.json \
    --output workbook_ch01.md

# Translate manually (copy/paste into Claude.ai, etc.)

python import_workbook.py workbook_ch01.md --output chunks/translated/
python combine_chunks.py chunks/translated/chapter_01_*.json \
    --output chapters/translated/chapter_01.txt

# === CHAPTER 2 (with Chapter 1 context!) ===
python chunk_chapter.py chapters/chapter_02.txt --chapter-id chapter_02
python generate_workbook.py chunks/chapter_02_*.json \
    --glossary glossary.json \
    --previous-chapter chapters/translated/chapter_01.txt \
    --context-paragraphs 2 \
    --output workbook_ch02.md

# The workbook prompts now include the last 2 paragraphs from Chapter 1!
# This helps maintain continuity when Chapter 2 references Chapter 1 events.

# Translate, import, combine...

# === CHAPTER 3 (with Chapter 2 context) ===
# Repeat pattern...
```

**Context Options:**
- `--context-paragraphs N` - Include last N paragraphs from previous chapter (default: 2)
- `--context-words N` - Alternative: include last N words from previous chapter

**When to use context:**
- Books with strong continuity between chapters
- Stories where chapters flow into each other
- First sentence of chapter X often references last events of chapter X-1

## Single Chapter Workflow

For translating individual chapters without multi-chapter context:

### Overview

The complete workflow has 5 main steps:

1. **Chunk** - Split chapter into translation-sized pieces
2. **Generate Workbook** - Create prompts for manual translation
3. **Translate** - Copy/paste prompts into any LLM (Claude.ai, ChatGPT, etc.)
4. **Import & Evaluate** - Import translations and check quality
5. **Combine** - Merge chunks into final chapter

### Step 1: Prepare Your Source Text

Create a plain text file (UTF-8) with your English chapter:

```
chapters/chapter_01.txt
```

**Example:**
```
Sara stood near her father and listened while he and Miss Minchin talked...
```

### Step 2: Chunk Your Chapter

Split the chapter into smaller pieces suitable for translation:

```bash
python chunk_chapter.py chapters/chapter_01.txt --chapter-id chapter_01 --output chunks/
```

**Options:**
- `--target-size 1500` - Target words per chunk (default: 1500)
- `--overlap 2` - Paragraph overlap between chunks (default: 2)
- `--verbose` - Show detailed output

**Output:**
Creates JSON files in `chunks/`:
```
chunks/chapter_01_chunk_000.json
chunks/chapter_01_chunk_001.json
chunks/chapter_01_chunk_002.json
```

Each chunk includes:
- Source text
- Metadata (word count, paragraph count, position)
- Overlap regions for smooth recombination

### Step 3: Generate Translation Workbook

Create a workbook with complete prompts for each chunk:

```bash
python generate_workbook.py chunks/chapter_01_*.json --output workbook.md --project "Your Book Title"
```

**With glossary (recommended):**
```bash
python generate_workbook.py chunks/chapter_01_*.json \
    --glossary glossary.json \
    --project "Little Princess" \
    --output workbook.md
```

**Output:**
Creates `workbook.md` with:
- Complete translation prompts for each chunk
- Glossary terms (if provided)
- Placeholder sections for pasting translations
- Clear instructions

### Step 4: Translate Manually

Open `workbook.md` in any text editor. For each chunk:

1. **Copy the prompt** - Find the "PROMPT TO COPY" section
2. **Paste into LLM** - Use Claude.ai, ChatGPT, or any LLM interface
3. **Copy translation** - Copy the LLM's Spanish translation
4. **Paste into workbook** - Paste into the "PASTE TRANSLATION HERE" section
5. **Save** - Save the workbook regularly

**Example workflow:**
```
## CHUNK 1: chapter_01_chunk_000

### PROMPT TO COPY:
─────────────────────────────────────────
[Complete prompt with glossary and source text]
─────────────────────────────────────────

### PASTE TRANSLATION HERE:
─────────────────────────────────────────
[Paste the Spanish translation here]
─────────────────────────────────────────
```

**Tips:**
- Translate chunks in order for consistency
- Save frequently
- Don't edit the chunk metadata sections
- Keep separator lines intact

### Step 5: Import Translations

Once all chunks are translated, import them back:

```bash
python import_workbook.py workbook.md --output chunks/translated/ --verbose
```

**Output:**
Creates translated chunk files:
```
chunks/translated/chapter_01_chunk_000.json
chunks/translated/chapter_01_chunk_001.json
chunks/translated/chapter_01_chunk_002.json
```

Each file now contains both source and translation.

### Step 6: Evaluate Quality

Run quality checks on translated chunks:

```bash
python evaluate_chunk.py chunks/translated/chapter_01_chunk_000.json
```

**With glossary:**
```bash
python evaluate_chunk.py chunks/translated/chapter_01_chunk_000.json --glossary glossary.json
```

**Generate HTML report:**
```bash
python evaluate_chunk.py chunks/translated/chapter_01_chunk_000.json \
    --format html \
    --output report.html
```

**Evaluators run automatically:**
- **Length** - Checks translation is 1.1x-1.3x source length (typical for Spanish)
- **Paragraph** - Verifies paragraph structure preserved
- **Dictionary** - Flags English words and Spanish misspellings
- **Completeness** - Detects missing content or placeholders
- **Glossary** - Checks term consistency (requires `--glossary`)

### Step 7: Combine Chunks

Merge translated chunks into final chapter:

```bash
python combine_chunks.py chunks/translated/chapter_01_*.json --output chapter_01_translated.txt
```

**Output:**
Creates `chapter_01_translated.txt` with:
- All chunks combined in order
- Overlap regions handled (keeps text from chunk ending, not chunk starting)
- Plain text ready for review

## Example: Complete Workflow

Here's a real example translating Chapter 1:

```bash
# 1. Chunk the source chapter
python chunk_chapter.py chapters/chapter_01.txt \
    --chapter-id chapter_01 \
    --output chunks/

# 2. Generate workbook with glossary
python generate_workbook.py chunks/chapter_01_*.json \
    --glossary glossary.json \
    --project "Little Princess" \
    --output workbook.md

# 3. Translate manually
# Open workbook.md, copy/paste prompts to Claude.ai, paste translations back

# 4. Import translations
python import_workbook.py workbook.md \
    --output chunks/translated/ \
    --verbose

# 5. Evaluate each chunk
python evaluate_chunk.py chunks/translated/chapter_01_chunk_000.json --glossary glossary.json
python evaluate_chunk.py chunks/translated/chapter_01_chunk_001.json --glossary glossary.json

# 6. Combine into final chapter
python combine_chunks.py chunks/translated/chapter_01_*.json \
    --output chapters/translated/chapter_01.txt
```

## Try It With Sample Files

Test the workflow with included examples:

### Simple Evaluation Test

```bash
# Should PASS - good translation
python evaluate_chunk.py tests/fixtures/chunk_translated_good.json

# Should FAIL - has errors
python evaluate_chunk.py tests/fixtures/chunk_translated_errors.json
```

### Understanding Evaluation Results

#### Example: Good Translation (PASS)

```
======================================================================
EVALUATION RESULTS
======================================================================

Chunk ID: chapter_01_chunk_000
Chapter: chapter_01

Evaluators Run: 5
  ✓ length        Score: 1.00
  ✓ paragraph     Score: 1.00
  ✓ dictionary    Score: 1.00
  ✓ completeness  Score: 1.00
  ✓ glossary      Score: 1.00

Overall Score: 1.00 / 1.00

----------------------------------------------------------------------
[PASSED] All evaluations passed
----------------------------------------------------------------------

No issues found!
======================================================================
```

**What this means:**
- All 5 evaluators passed
- Translation is complete, properly formatted, and uses correct Spanish
- Glossary terms used correctly
- Ready to combine

#### Example: Translation With Issues (FAIL)

```
======================================================================
EVALUATION RESULTS
======================================================================

Overall Score: 0.72 / 1.00

----------------------------------------------------------------------
[FAILED] Translation has issues
----------------------------------------------------------------------

3 issue(s) found:

[dictionary] ERROR: English word in translation: 'friend'
  Location: Character position 234
  Suggestion: Translate to Spanish or add to glossary

[completeness] ERROR: Placeholder detected
  Location: Line 5
  Suggestion: Replace "[TODO: translate]" with actual translation

[glossary] WARNING: Inconsistent term usage
  Expected: "Sara"
  Found: "Sarah"
  Location: Paragraph 3
======================================================================
```

**What this means:**
- Translation needs fixes before combining
- Fix the 2 errors (untranslated word, placeholder)
- Review the warning (name spelling inconsistency)

## Creating a Glossary

A glossary ensures consistent translation of character names, places, and special terms.

### Basic Glossary Structure

Create `glossary.json`:

```json
{
  "terms": [
    {
      "english": "Sara Crewe",
      "spanish": "Sara Crewe",
      "term_type": "character",
      "context": "Main character, young girl",
      "alternatives": []
    },
    {
      "english": "Miss Minchin",
      "spanish": "la señorita Minchin",
      "term_type": "character",
      "context": "Headmistress of the seminary",
      "alternatives": []
    },
    {
      "english": "Emily",
      "spanish": "Emily",
      "term_type": "character",
      "context": "Sara's doll",
      "alternatives": []
    }
  ],
  "version": "1.0.0",
  "updated_at": "2025-01-07T00:00:00"
}
```

### Using the Glossary

The glossary is automatically included in workbook prompts:

```bash
python generate_workbook.py chunks/*.json --glossary glossary.json --output workbook.md
```

And used for evaluation:

```bash
python evaluate_chunk.py chunk.json --glossary glossary.json
```

## Common Questions

**Q: Do I need to use an LLM API?**
No! The manual workflow uses copy/paste with any LLM interface (Claude.ai, ChatGPT, etc.). No API key required.

**Q: What's a good chunk size?**
Default is 1500 words (~3-5 pages). Larger chunks give more context but cost more per LLM request.

**Q: Can I edit translations after import?**
Yes! Edit the JSON files directly or re-import from an edited workbook.

**Q: How do I handle footnotes or special formatting?**
Include them in the source text. Add instructions in a custom translation prompt if needed.

**Q: What if evaluation fails?**
Review the specific issues reported. Common fixes:
- Re-translate sections with placeholders
- Fix untranslated English words
- Adjust length if significantly off (may indicate missing content)

**Q: Can I translate to other languages besides Spanish?**
Yes, but you'll need appropriate dictionaries and may need to adjust length ratio thresholds.

## Next Steps

- Read [USAGE.md](USAGE.md) for detailed command reference
- See [PROMPT_GUIDE.md](PROMPT_GUIDE.md) for customizing translation prompts
- Check [README.md](README.md) for architecture and development info
- Review [DESIGN.md](DESIGN.md) for technical details

---

Happy translating!
