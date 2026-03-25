# Glossary Candidate Extraction

Extract candidate glossary terms from a source text before translation. This script analyzes a book's full text to find character names, place names, technical language, and uncommon words that should be consistently translated -- without any LLM calls.

## Quick Start

```bash
python scripts/extract_glossary_candidates.py source.txt -o candidates.json
```

## Usage

```bash
# Basic extraction
python scripts/extract_glossary_candidates.py projects/mybook/source.txt -o candidates.json

# Exclude terms already in an existing glossary
python scripts/extract_glossary_candidates.py source.txt -o candidates.json -g glossary.json

# Require terms to appear at least 3 times
python scripts/extract_glossary_candidates.py source.txt -o candidates.json --min-frequency 3

# Limit output and see progress
python scripts/extract_glossary_candidates.py source.txt -o candidates.json --max-candidates 100 -v
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `source_file` | required | Plain text file to analyze |
| `-o`, `--output` | required | Output JSON path |
| `-g`, `--glossary` | none | Existing `glossary.json` to exclude known terms |
| `--min-frequency` | 2 | Minimum occurrences for a term to qualify |
| `--max-candidates` | 500 | Cap on total candidates returned |
| `-v`, `--verbose` | off | Show per-extractor counts during analysis |
| `--dry-run` | off | Print summary without writing the output file |

## How It Works

The script runs four independent heuristic extractors, then merges and ranks their results.

### 1. Proper Noun Extractor

Finds capitalized words that appear mid-sentence (not at the start where any word would be capitalized). Consecutive capitalized words are combined into multi-word candidates like "Uncle Paul" or "Mont Ventoux".

Type guesses are based on context:
- Preceded by a title word (Mr., Uncle, Dr., etc.) -> CHARACTER
- Preceded by a geographic word (Mount, Lake, Cape, etc.) -> PLACE
- Other multi-word capitalized sequences -> CHARACTER by default

A term must appear capitalized in >80% of its total occurrences to qualify, which filters out common words that only look capitalized because they start a sentence.

### 2. Uncommon Word Extractor

Uses PyEnchant's English dictionary to find words that aren't standard English. These are often domain-specific terms, foreign words, or archaic vocabulary -- exactly the kind of terms that need glossary entries for consistent translation.

### 3. N-gram Extractor

Finds repeated two- and three-word phrases where at least one word is not in the English dictionary. This catches compound technical terms like "Processionary Caterpillars" or "June bug" that wouldn't be found by single-word analysis.

Filters out dialogue attributions ("said Jules"), stopword-only phrases, and conjoined name patterns ("Jules and Emile").

### 4. Repeated Capitalized Extractor

A safety net that catches proper nouns which only appear at sentence starts (where the main extractor can't distinguish them from regular words). If a word is always capitalized and not in the English dictionary, it's likely a name.

### Scoring

Each candidate receives a score from 0 to 1 based on:
- **Frequency** (40%) -- how often the term appears in the text
- **Dictionary novelty** (30%) -- not in the English dictionary
- **Multi-word** (20%) -- multi-word terms score higher
- **Detection breadth** (10%) -- flagged by multiple extractors

Candidates are sorted by score descending, then capped at `--max-candidates`.

## Output Format

```json
{
  "source_file": "projects/mybook/source.txt",
  "total_words": 91342,
  "total_unique_words": 7900,
  "candidates": [
    {
      "term": "Uncle Paul",
      "type_guess": "character",
      "frequency": 96,
      "score": 0.935,
      "context_sentence": "The children gathered around Uncle Paul in the garden.",
      "detection_reasons": ["capitalized_sequence", "title_word_prefix"]
    }
  ],
  "excluded_glossary_terms": 11,
  "generated_at": "2026-03-25T10:30:00"
}
```

Each candidate includes:
- **term** -- the surface form as it appears in the text
- **type_guess** -- `character`, `place`, `technical`, or `other`
- **frequency** -- number of occurrences in the source
- **score** -- ranking score (higher = stronger candidate)
- **context_sentence** -- first sentence where the term appears
- **detection_reasons** -- why the term was flagged

## Typical Workflow

1. Run the script against your source text
2. Review the output JSON (or feed it to an LLM for prioritization)
3. Use the results to build or supplement your `glossary.json`

The goal is to replace sending the entire book to an LLM for term extraction. Instead, this script narrows down to a focused list of candidates, which can then be reviewed much more cheaply.

## Requirements

- **PyEnchant** with an English (`en_US`) dictionary installed. Without it, only the proper noun and repeated capitalized extractors will run (dictionary-based extraction is skipped).
- See [DICTIONARY_SETUP.md](DICTIONARY_SETUP.md) for installation instructions.
