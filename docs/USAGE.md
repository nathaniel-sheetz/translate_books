# How to Evaluate Your Translations

This guide shows you how to use the length evaluator on your text files.

## Quick Start

### Basic Usage

```bash
python evaluate_chunk.py examples/sample_source.txt examples/sample_translation_good.txt
```

This will:
1. Load both text files
2. Run the length evaluation
3. Show you a detailed report with pass/fail status

### With Your Own Files

```bash
python evaluate_chunk.py path/to/your/english.txt path/to/your/spanish.txt
```

## Example Output

### Good Translation (Pass)

```
📖 Loading files...
🔍 Evaluating translation...

======================================================================
📊 EVALUATION RESULTS
======================================================================

Source File:      sample_source.txt
Translation File: sample_translation_good.txt

Counting by: words
  Source:      167 words
  Translation: 173 words
  Ratio:       1.04x

Score: 1.00 / 1.00

----------------------------------------------------------------------
✅ PASSED - Translation length is acceptable
----------------------------------------------------------------------

✨ No issues found! Translation length looks good.

Threshold Configuration:
  Expected range: 1.1x - 1.3x
  Acceptable range: 0.5x - 2.0x

======================================================================
```

### Problem Translation (Fail)

```
📊 EVALUATION RESULTS
======================================================================

Source File:      sample_source.txt
Translation File: sample_translation_short.txt

Counting by: words
  Source:      167 words
  Translation: 41 words
  Ratio:       0.25x

Score: 0.50 / 1.00

----------------------------------------------------------------------
❌ FAILED - Translation has length issues
----------------------------------------------------------------------

⚠️  1 issue(s) found:

1. ❌ ERROR
   Translation is 24.6% the length of the source (41 vs 167 words).
   Expected at least 110.0% (184 words).
   Location: 167 words → 41 words
   💡 Suggestion: Check for missing content, truncated paragraphs,
   or incomplete translation.

======================================================================
```

## Advanced Options

### Count by Characters Instead of Words

```bash
python evaluate_chunk.py source.txt translation.txt --by-chars
```

This is useful for:
- Languages where word boundaries are different
- Very technical text with special terms
- More precise length checking

### File Formats Supported

- Plain text files (.txt)
- UTF-8 encoding (automatically detected)
- Falls back to latin-1 if UTF-8 fails
- Handles Windows (\r\n) and Unix (\n) line endings

## Understanding the Results

### Score (0.0 - 1.0)

- **1.0** = Perfect, within expected range
- **0.8-0.99** = Good, slight deviation but acceptable
- **0.5-0.79** = Warning level deviation
- **< 0.5** = Significant deviation

### Pass/Fail Status

- **PASSED** = No error-level issues (may have warnings)
- **FAILED** = Has error-level issues that need attention

### Issue Severity Levels

1. **❌ ERROR** - Critical issue, translation likely has problems
   - Too short (< 50% of source length)
   - Too long (> 200% of source length)

2. **⚠️ WARNING** - Should review, but may be acceptable
   - Shorter than expected (< 110% for Spanish)
   - Longer than expected (> 130% for Spanish)

3. **ℹ️ INFO** - Informational, usually not a problem
   - Just FYI, no action needed

### Length Ratios

For English → Spanish translations:

- **1.1x - 1.3x** = Expected range (Spanish is typically 10-20% longer)
- **0.5x - 2.0x** = Acceptable range (outside this triggers errors)

Different language pairs have different typical ratios.

## Customizing for Your Needs

If you need different thresholds, you can modify `evaluate_chunk.py` around line 74:

```python
# Example: More lenient for literary translation
context = {
    "length_config": {
        "count_by": "words",
        "min_ratio": 0.7,      # Allow shorter
        "max_ratio": 2.5,      # Allow longer
        "expected_min": 1.0,   # Expect 1.0x-1.5x
        "expected_max": 1.5
    }
}
```

## Testing with Sample Files

We've included sample files you can test with:

### Good Translation (Should Pass)
```bash
python evaluate_chunk.py examples/sample_source.txt examples/sample_translation_good.txt
```

### Short Translation (Should Fail)
```bash
python evaluate_chunk.py examples/sample_source.txt examples/sample_translation_short.txt
```

## Preparing Your Files

### Best Practices

1. **Use UTF-8 encoding** - Most reliable
2. **One chunk per file** - Don't mix multiple chapters
3. **Clean text** - Remove headers, footers, page numbers
4. **Preserve paragraphs** - Keep paragraph breaks (double newlines)

### File Organization

Suggested structure:
```
my_translation/
├── source/
│   ├── chapter_01_en.txt
│   ├── chapter_02_en.txt
│   └── chapter_03_en.txt
└── translated/
    ├── chapter_01_es.txt
    ├── chapter_02_es.txt
    └── chapter_03_es.txt
```

Then evaluate each pair:
```bash
python evaluate_chunk.py my_translation/source/chapter_01_en.txt my_translation/translated/chapter_01_es.txt
```

## Troubleshooting

### "File not found" error
- Check the file path is correct
- Use quotes if path has spaces: `"my file.txt"`
- Use absolute paths if relative doesn't work

### "UnicodeDecodeError"
- The script should handle this automatically
- If it persists, save your file as UTF-8
- In Notepad: File → Save As → Encoding: UTF-8

### Unexpected results
- Check that you're comparing the right files
- Verify source is English and translation is Spanish
- Make sure files aren't empty
- Check for extra spaces or hidden characters

## Dictionary Evaluator

The dictionary evaluator checks your Spanish translation for spelling errors, English words that weren't translated, and unknown words.

### Basic Usage

```bash
python evaluate_chunk_dictionary.py path/to/translation.txt
```

**Note:** Dictionary check only needs the translation file, not the source.

### Example Output

#### Clean Translation (Pass)

```
>> Loading translation file...
>> Evaluating translation dictionary...

======================================================================
DICTIONARY EVALUATION RESULTS
======================================================================

Translation File: chapter1_es.txt
Dictionaries: es_ES (Spain Spanish), es_MX (Mexican Spanish)

Statistics:
  Total words:      285
  Unique words:     156
  Glossary words:   0
  English words:    0 [ERROR]
  Unknown words:    0 [WARNING]
  Flagged instances: 0

Score: 1.00 / 1.00

----------------------------------------------------------------------
[PASSED] Dictionary check passed
----------------------------------------------------------------------

No issues found! All words are valid Spanish.

======================================================================
```

#### Translation with Issues (Fail)

```
======================================================================
DICTIONARY EVALUATION RESULTS
======================================================================

Translation File: chapter1_es.txt
Dictionaries: es_ES (Spain Spanish), es_MX (Mexican Spanish)

Statistics:
  Total words:      1,762
  Unique words:     749
  Glossary words:   0
  English words:    3 [ERROR]
  Unknown words:    12 [WARNING]
  Flagged instances: 47

Score: 0.97 / 1.00

----------------------------------------------------------------------
[FAILED] Dictionary check found errors
----------------------------------------------------------------------

15 issue(s) found:

[ERRORS] - English words in translation:
----------------------------------------------------------------------
1. 'house': English word in translation (found 2 time(s))
   Location: Character positions: 145, 389
   Suggestion: Translate 'house' to Spanish or add to glossary if it's a proper noun

2. 'friend': English word in translation (found 1 time(s))
   Location: Character position 567
   Suggestion: Translate 'friend' to Spanish or add to glossary if it's a proper noun

[WARNINGS] - Unknown words (not in Spanish or English dictionaries):
----------------------------------------------------------------------
1. 'preuba': Unknown word (not in Spanish or English dictionary) (found 1 time(s))
   Location: Character position 234
   Suggestion: Possible misspelling. Suggestions: prueba, preñaba, preñada

2. 'animalillo': Unknown word (not in Spanish or English dictionary) (found 1 time(s))
   Location: Character position 1456
   Suggestion: Possible misspelling. Suggestions: animalizo, animaliza, animarlo

======================================================================
```

### Understanding the Results

#### Statistics Explained

- **Total words**: All words in the translation
- **Unique words**: Number of distinct words
- **Glossary words**: Words found in provided glossary (excluded from checks)
- **English words**: Words in English dictionary (ERRORS)
- **Unknown words**: Words not in Spanish or English dictionaries (WARNINGS)
- **Flagged instances**: Total occurrences of flagged words

#### Issue Types

1. **ERRORS - English Words**
   - Words found in English dictionary
   - Should have been translated to Spanish
   - **Action required**: Translate these words or add to glossary if proper nouns

2. **WARNINGS - Unknown Words**
   - Not in Spanish or English dictionaries
   - Could be:
     - Misspellings (check suggestions)
     - Proper nouns (character names, places)
     - Diminutives or uncommon forms
     - Regional words not in standard dictionaries
   - **Action**: Review each one - fix typos, add proper nouns to glossary

#### Score (0.0 - 1.0)

- **1.0** = All words are valid Spanish
- **0.95-0.99** = Minor issues (few unknown words)
- **0.90-0.94** = Some issues to review
- **< 0.90** = Significant issues or many English words

### Advanced Options

#### Using with a Glossary

Create a glossary file for proper nouns and technical terms:

**glossary.json:**
```json
{
  "terms": [
    {
      "english": "Hogwarts",
      "spanish": "Hogwarts",
      "term_type": "place",
      "context": "School name - keep in original",
      "alternatives": []
    },
    {
      "english": "Gandalf",
      "spanish": "Gandalf",
      "term_type": "character",
      "context": "Wizard name",
      "alternatives": []
    },
    {
      "english": "API",
      "spanish": "API",
      "term_type": "technical",
      "context": "Technical term",
      "alternatives": []
    }
  ],
  "version": "1.0.0",
  "updated_at": "2025-10-29T00:00:00"
}
```

**Run with glossary:**
```bash
python evaluate_chunk_dictionary.py translation.txt --glossary glossary.json
```

Words in the glossary won't be flagged as unknown.

#### Case-Sensitive Mode

By default, checking is case-insensitive ("Casa" and "casa" treated the same).

Enable case-sensitive checking:
```bash
python evaluate_chunk_dictionary.py translation.txt --case-sensitive
```

### Interpreting Character Positions

The evaluator reports exact character positions for each flagged word:

```
Location: Character position 234
```

This is the position in the file where the word starts. You can:
- Use your text editor's "Go to position" feature
- Search for the word in context
- Use the position to locate the sentence

For repeated words:
```
Location: Character positions: 145, 389, 567 (3 total)
```

### Handling Common Cases

#### Proper Nouns (Character Names, Places)

If you see warnings for proper nouns:

**Option 1:** Add to glossary (recommended)
```bash
python evaluate_chunk_dictionary.py translation.txt --glossary glossary.json
```

**Option 2:** Ignore the warnings if you've verified they're correct

#### Diminutives and Regional Words

Spanish has many diminutive forms (animalillo, monito, bracito) that may not be in standard dictionaries:

- **Check context**: Is it a natural diminutive?
- **Verify spelling**: Use suggestions if available
- **Accept if correct**: These warnings can be safely ignored if the form is correct

#### Mixed Vocabulary (Spain vs. Latin America)

The evaluator uses both es_ES (Spain) and es_MX (Mexican) dictionaries with OR logic, accepting words valid in either:

- "ordenador" (Spain) ✓ Accepted
- "computadora" (Latin America) ✓ Accepted
- Both are valid Spanish

#### Spelling Suggestions

For unknown words, the evaluator provides suggestions:

```
Suggestion: Possible misspelling. Suggestions: prueba, preñaba, preñada
```

- Check if the first suggestion is what you meant
- Consider context when choosing correction
- Sometimes the word is correct but rare

### File Formats Supported

- Plain text files (.txt)
- UTF-8 encoding (recommended)
- Falls back to latin-1 if UTF-8 fails
- Handles Windows (\r\n) and Unix (\n) line endings

### Testing with Sample Files

Test the dictionary evaluator with real translation files:

```bash
# Your own translation
python evaluate_chunk_dictionary.py my_translation/chapter1_es.txt

# With glossary
python evaluate_chunk_dictionary.py my_translation/chapter1_es.txt --glossary my_glossary.json
```

### Troubleshooting

#### "Dictionary not found" error

This means Spanish dictionaries aren't installed. See `DICTIONARY_SETUP.md` for installation instructions.

#### Too many unknown words

Possible causes:
- Translation contains many proper nouns → Use glossary
- Non-standard Spanish (regional, archaic) → Warnings are okay
- File encoding issues → Ensure UTF-8 encoding
- Actually misspelled → Review carefully

#### False positives

Sometimes valid words are flagged:
- Very rare or archaic Spanish words
- Regional variants not in either dictionary
- Technical terminology
- Recent loanwords

**Solution:** Use glossary for known valid terms, or accept warnings after verification.

## Next Steps

### Recommended: Unified Evaluation Script

The **best way** to evaluate your translations is using the unified evaluation script that runs all evaluators at once:

```bash
# Run all evaluators with a single command
python evaluate_chunk.py chunk.json --glossary glossary.json

# Or with text files (runs all except glossary)
python evaluate_chunk.py chunk.json
```

This gives you:
- All evaluators run together (length, paragraph, dictionary, glossary, completeness)
- Comprehensive reports in text, JSON, or HTML format
- Single command for complete quality assessment

See the **"Evaluating Your Translations"** section in [README.md](README.md) for full details.

### Alternative: Standalone Evaluator Scripts

For **standalone testing** of individual evaluators, you can use these scripts:

```bash
# Length check only (completeness)
python evaluate_chunk_length.py source.txt translation.txt

# Paragraph check only (structure)
python evaluate_chunk_paragraph.py source.txt translation.txt

# Dictionary check only (spelling/English)
python evaluate_chunk_dictionary.py translation.txt --glossary glossary.json

# Glossary check only (term consistency)
python evaluate_chunk_glossary.py chunk.json --glossary glossary.json
```

**Use these when:**
- Testing a single evaluator during development
- Quick check of one specific aspect
- Learning how each evaluator works

**For production use:** Use the unified `evaluate_chunk.py` script instead.

### Best Practice Workflow

For each translation chunk:

```bash
# Option 1 (Recommended): Run all evaluators at once
python evaluate_chunk.py chunk.json --glossary glossary.json --format html --output report.html

# Option 2: Run individual checks (for testing)
python evaluate_chunk_length.py source.txt translation.txt
python evaluate_chunk_paragraph.py source.txt translation.txt
python evaluate_chunk_dictionary.py translation.txt --glossary glossary.json
```

If all evaluators pass, your translation quality is good!

---

Need help? Check the [DESIGN.md](DESIGN.md) for technical details or open an issue.
