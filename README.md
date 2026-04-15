# Book Translation Pipeline

A semi-automated system for translating public domain books (English → Spanish) with LLM-assisted quality assurance. The primary interface is a **web-based pipeline wizard** that guides you through every stage from raw source text to reviewed bilingual chapters.

---

## Quick Start

```bash
pip install -r requirements.txt

# Start the web server (from project root)
cd web_ui && python app.py
```

Open `http://localhost:5000` to see your projects, or go directly to `http://localhost:5000/project/<project_id>` for the pipeline dashboard.

### Create a project (CLI)

Projects live in `projects/`. Create one manually or use the Gutenberg ingestor:

```bash
# From a Project Gutenberg URL
python scripts/ingest_gutenberg.py https://www.gutenberg.org/files/41350/41350-h/41350-h.htm \
    --output projects/my-book/

# Or just create the directory and add source.txt yourself
mkdir -p projects/my-book
cp my_book.txt projects/my-book/source.txt
```

Then open `http://localhost:5000/project/my-book` to start the pipeline.

---

## Pipeline Overview

The dashboard at `/project/<id>` walks you through 8 stages. All stages are always accessible — nothing is locked.

| # | Stage | What it does |
|---|-------|-------------|
| 1 | **Source** | Upload or paste source text → `source.txt` |
| 2 | **Split** | Detect chapter boundaries → individual chapter files |
| 3 | **Chunk** | Break chapters into ~2000-word translation units |
| 4 | **Style Guide** | Generate translation style rules (LLM-assisted) |
| 5 | **Glossary** | Build consistent term glossary (LLM-assisted) |
| 6 | **Translate** | Translate chunks via API or manual copy/paste |
| 7 | **Review** | Align sentences, read bilingually, annotate |
| 8 | **Export** | Build and download EPUB with images |

The style guide and glossary stages use a **copy/paste LLM pattern**: the dashboard generates a prompt, you copy it into any LLM (Claude, ChatGPT, etc.), paste the response back, and the dashboard parses it.

Translation supports both:
- **Manual**: copy the rendered prompt, paste the LLM response
- **API**: multi-select chapters for batch translation with real-time progress

See [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) for a walkthrough and [`docs/WEB_UI_GUIDE.md`](docs/WEB_UI_GUIDE.md) for full reference.

---

## Bilingual Reader

After translation and alignment, read chapters at `/read/<project_id>/<chapter>`:

- Side-by-side English/Spanish sentences
- Tap any sentence to see the source and edit the translation
- **Edit chunk** button in the bottom sheet opens a full-textarea editor for the surrounding chunk — use it to fix stray whitespace, wrong paragraph breaks, or other edits that don't fit one sentence at a time. Saves recombine and realign the chapter automatically and keep a backup under `.chunk_edits/`.
- Annotation system (word choice, inconsistency, footnote, flag)
- Mark chapters as reviewed
- Correction workflow for batch fixes

---

## CLI Workflow

All pipeline stages are also available as CLI scripts in `scripts/`:

```bash
# Split a book into chapters
python scripts/split_book.py projects/my-book/source.txt --output projects/my-book/chapters/

# Chunk a chapter
python scripts/chunk_chapter.py projects/my-book/chapters/chapter_01.txt --chapter-id chapter_01

# API translation
python scripts/translate_api.py chunks/*.json --provider anthropic --output chunks/translated/

# Batch evaluate + combine
python scripts/batch_pipeline.py projects/my-book --stages evaluate,combine

# Build EPUB
python scripts/build_epub.py projects/my-book
```

See [`docs/BATCH_PIPELINE.md`](docs/BATCH_PIPELINE.md) for the batch CLI reference.

---

## Installation

```bash
pip install -r requirements.txt
```

API keys (optional, for API translation):
```bash
cp .env.example .env
# Add ANTHROPIC_API_KEY and/or OPENAI_API_KEY
```

### Dictionary evaluator (optional)

The dictionary evaluator requires system-level spell-checking libraries. See [`docs/DICTIONARY_SETUP.md`](docs/DICTIONARY_SETUP.md) for setup instructions.

---

## Project Structure

```
book_translation/
├── web_ui/                     # Flask web application
│   ├── app.py                  # All routes (dashboard, reader, setup, APIs)
│   ├── i18n.py                 # Server-side translations (EN/ES)
│   ├── templates/
│   │   ├── dashboard.html      # Pipeline wizard (8 stages)
│   │   ├── reader.html         # Bilingual reader + project/chapter lists
│   │   └── chunk_edit.html     # Full-textarea chunk editor
│   └── static/
│       ├── dashboard.js/css    # Dashboard logic and styles
│       ├── reader.js/css       # Reader logic and styles
│       └── setup.js/css        # Setup wizard (used by dashboard)
│
├── src/                        # Core library
│   ├── models.py               # Pydantic data models
│   ├── book_splitter.py        # Chapter detection + splitting
│   ├── chunker.py              # Chapter → translation chunks
│   ├── combiner.py             # Chunks → chapter recombination
│   ├── api_translator.py       # LLM API translation (Anthropic/OpenAI)
│   ├── sentence_aligner.py     # Bilingual sentence alignment
│   ├── style_guide_wizard.py   # Style guide generation
│   ├── glossary_bootstrap.py   # Glossary candidate extraction
│   ├── translator.py           # Prompt rendering + workbook generation
│   ├── epub_builder.py         # EPUB export
│   ├── evaluators/             # Pluggable quality evaluators
│   └── utils/                  # File I/O, text utilities
│
├── scripts/                    # CLI entry points
├── prompts/                    # LLM prompt templates (Jinja2)
├── tests/                      # pytest test suite
├── docs/                       # Documentation
├── projects/                   # Working data (gitignored)
│   └── my-book/
│       ├── source.txt          # Raw source text
│       ├── chapters/           # Split chapter files
│       ├── chunks/             # Chunk JSON files
│       ├── style.json          # Style guide
│       ├── glossary.json       # Glossary
│       ├── translated/         # Combined translated chapters
│       ├── alignments/         # Sentence alignment JSON
│       ├── annotations.jsonl   # Reader annotations
│       ├── reviewed.json       # Chapter review status
│       ├── .chunk_edits/       # Pre-edit chunk backups from the chunk editor
│       └── images/             # Downloaded images
│
├── requirements.txt
└── .env.example
```

---

## Running Tests

```bash
pytest                              # All tests
pytest tests/test_web_ui.py -v     # Web UI tests
pytest --cov=src tests/            # With coverage
```

---

## Documentation

| Document | Contents |
|---|---|
| [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) | Dashboard walkthrough tutorial |
| [`docs/WEB_UI_GUIDE.md`](docs/WEB_UI_GUIDE.md) | Full dashboard + reader reference |
| [`docs/BATCH_PIPELINE.md`](docs/BATCH_PIPELINE.md) | CLI batch evaluate + combine |
| [`docs/INGEST_GUTENBERG.md`](docs/INGEST_GUTENBERG.md) | Gutenberg HTML → source.txt |
| [`docs/PROMPT_GUIDE.md`](docs/PROMPT_GUIDE.md) | Prompt template customization |
| [`docs/CHAPTER_DETECTION_GUIDE.md`](docs/CHAPTER_DETECTION_GUIDE.md) | Chapter splitting patterns |
| [`docs/DICTIONARY_SETUP.md`](docs/DICTIONARY_SETUP.md) | Dictionary evaluator setup |

---

## License

For use with public domain books.
