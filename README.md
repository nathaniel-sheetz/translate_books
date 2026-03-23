# Book Translation Workflow

A semi-automated system for translating public domain books (English → Spanish) with quality assurance. Splits books into ~2000-word chunks, provides complete LLM prompts, and validates translation quality through pluggable evaluators.

**410+ passing tests.**

---

## Installation

```bash
pip install -r requirements.txt
```

### Dictionary evaluator (optional)

The dictionary evaluator requires system-level spell-checking libraries:

**Ubuntu/Debian:**
```bash
sudo apt-get install -y libenchant-2-2 hunspell-es aspell-es
```

**macOS:**
```bash
brew install enchant hunspell
# Place es_ES/es_MX .dic/.aff files in ~/Library/Spelling/
# Dictionaries: https://github.com/LibreOffice/dictionaries/tree/master/es
```

**Windows:** `pip install pyenchant` includes bundled dictionaries.

Required dictionaries: `es_ES`, `es_MX`, `en_US`.

---

## Web UI (Recommended)

The web UI is the primary workflow for large projects. It auto-loads the next untranslated chunk, renders the full prompt for one-click copying, and saves translations directly to disk.

**Start the server (run from project root):**
```bash
cd web_ui && python app.py
```

Open `http://localhost:5000`.

### Configuring default form values

Copy `project.example.json` to `project.json` at the project root and fill in your values:

```json
{
    "chunks_dir": "projects/my-book/chunks/",
    "project_name": "My Book Title",
    "source_language": "English",
    "target_language": "Spanish",
    "glossary_path": "projects/my-book/glossary.json",
    "style_guide_path": "projects/my-book/style.json",
    "ui_language": "en"
}
```

`project.json` is gitignored — each user maintains their own. The setup form remains fully editable; these values are only defaults.

See [`docs/WEB_UI_GUIDE.md`](docs/WEB_UI_GUIDE.md) for full documentation.

---

## CLI Workflow

All scripts live in `scripts/` and must be run from the project root:

```bash
# 1. Split a full book file into chapter files
python scripts/split_book.py full_book.txt --output chapters/

# 2. Chunk a chapter into translation-sized pieces
python scripts/chunk_chapter.py chapters/chapter_01.txt --chapter-id chapter_01

# 3. (Option A) Web UI — open http://localhost:5000 and point it at your chunks dir

# 3. (Option B) Generate a workbook for manual LLM translation
python scripts/generate_workbook.py chunks/chapter_01_*.json --output workbook.md
# Edit workbook.md: paste each prompt into your LLM, paste translation back
python scripts/import_workbook.py workbook.md --output chunks/translated/

# 3. (Option C) API translation (Anthropic/OpenAI)
python scripts/translate_api.py chunks/*.json --provider anthropic --output chunks/translated/

# 4. Evaluate translation quality
python scripts/evaluate_chunk.py chunks/translated/*.json --glossary glossary.json

# 5. Combine translated chunks into a final chapter file
python scripts/combine_chunks.py chunks/translated/chapter_01_*.json --output chapter_01.txt
```

### Chunking options

```bash
python scripts/chunk_chapter.py chapter.txt \
    --target-size 1500 \
    --overlap 2 \
    --min-overlap-words 150 \
    --output chunks/original/
```

### Previous chapter context

Include the ending of the previous translated chapter in the prompt for narrative continuity:

```bash
python scripts/generate_workbook.py chunks/chapter_02_*.json \
    --previous-chapter chapters/translated/chapter_01.txt \
    --context-paragraphs 2 \
    --output workbook_ch02.md
```

### Evaluation

```bash
# All evaluators
python scripts/evaluate_chunk.py chunk.json --glossary glossary.json

# Specific evaluators
python scripts/evaluate_chunk.py chunk.json --evaluators length,paragraph

# HTML report
python scripts/evaluate_chunk.py chunk.json --format html --output report.html
```

Available evaluators: `length`, `paragraph`, `dictionary`, `completeness`, `glossary`.

### API translation

```bash
# Real-time
python scripts/translate_api.py chunks/*.json \
    --provider anthropic \
    --model claude-3-5-sonnet-20241022 \
    --glossary glossary.json \
    --output chunks/translated/

# Batch (50% cheaper, ~24h turnaround)
python scripts/translate_api.py chunks/*.json --provider anthropic --batch
python scripts/translate_api.py --check-batch batch_abc123
```

Copy `.env.example` to `.env` and add your API keys.

---

## Project Structure

```
book_translation/
├── src/                        # Core library
│   ├── models.py               # All Pydantic data models
│   ├── chunker.py              # Chapter → chunks (dual-constraint overlap)
│   ├── combiner.py             # Chunks → chapter (use_previous strategy)
│   ├── translator.py           # Workbook generation + parsing
│   ├── evaluators/             # Pluggable evaluation system
│   └── utils/                  # File I/O, text utilities
│
├── scripts/                    # CLI entry points
│   ├── split_book.py
│   ├── chunk_chapter.py
│   ├── combine_chunks.py
│   ├── generate_workbook.py
│   ├── import_workbook.py
│   ├── evaluate_chunk.py       # Unified evaluator (use this one)
│   ├── evaluate_chunk_*.py     # Individual evaluator scripts
│   ├── translate_api.py
│   ├── export_bilingual.py
│   └── search_dictionary.py
│
├── web_ui/                     # Flask web application
│   ├── app.py
│   ├── static/
│   └── templates/
│
├── tests/                      # pytest test suite
│   ├── test_evaluators/
│   ├── fixtures/
│   └── manual/                 # Manual test scripts (not pytest)
│
├── prompts/                    # LLM prompt templates
├── examples/                   # Sample source and translation files
├── docs/                       # User-facing documentation
│   ├── GETTING_STARTED.md
│   ├── USAGE.md
│   ├── WEB_UI_GUIDE.md
│   ├── PROMPT_GUIDE.md
│   ├── DICTIONARY_SETUP.md
│   ├── CHAPTER_DETECTION_GUIDE.md
│   └── design/                 # Architecture and planning docs
│
├── projects/                   # Working data — gitignored
│   └── my-book/
│       ├── source/             # Raw chapter .txt files
│       ├── chunks/             # Processed chunk JSON
│       ├── glossary.json
│       └── style.json
│
├── project.json                # Local defaults for web UI form (gitignored)
├── project.example.json        # Template for project.json
├── requirements.txt
└── .env.example
```

---

## Running Tests

```bash
pytest                              # All tests
pytest tests/test_evaluators/ -v   # Specific module
pytest --cov=src tests/            # With coverage
```

---

## Documentation

| Document | Contents |
|---|---|
| [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) | First-time setup walkthrough |
| [`docs/USAGE.md`](docs/USAGE.md) | Full CLI reference |
| [`docs/WEB_UI_GUIDE.md`](docs/WEB_UI_GUIDE.md) | Web UI features and workflow |
| [`docs/PROMPT_GUIDE.md`](docs/PROMPT_GUIDE.md) | Prompt template customization |
| [`docs/DICTIONARY_SETUP.md`](docs/DICTIONARY_SETUP.md) | Dictionary evaluator setup |
| [`docs/CHAPTER_DETECTION_GUIDE.md`](docs/CHAPTER_DETECTION_GUIDE.md) | Chapter splitting patterns |
| [`docs/design/`](docs/design/) | Architecture and design documents |

---

## License

For use with public domain books.
