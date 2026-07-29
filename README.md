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

Projects live in `projects/`. You can keep them flat or organize them into grouping subfolders (e.g. `projects/by-author/fabre/my-book/`) — the dashboard, all API endpoints, and the CLI all find projects wherever they live. Create one manually or use the Gutenberg ingestor:

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
| 8 | **Export** | Build and download EPUB with images, plus an optional editable "Note from the Translator" appended as the last chapter |

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
- **Remove text…** button prunes a stray sentence (caption, OCR artifact) from both the source and translation, propagating overlap regions and re-aligning the chapter. See [`docs/READER_REMOVE_TEXT.md`](docs/READER_REMOVE_TEXT.md).
- **Retranslate…** button gets a fresh LLM translation of the tapped sentence with a per-call model picker, lets you confirm the source span (alignment isn't always perfect), and replaces the existing translation with one confirm. See [`docs/READER_RETRANSLATE.md`](docs/READER_RETRANSLATE.md).
- **Realign chapter** button appears in the topbar whenever the current chapter has unsaved pending corrections. Clicking it applies the queued corrections to chunk files, then regenerates the sentence alignment in place. Applied corrections are archived to `corrections_applied.jsonl`; unresolvable rows are archived as `skipped`.
- **Find in book** search icon in the topbar opens a full-screen concordance: type any fragment and see every occurrence across the whole book as a source + translation pair, toggling between the Spanish translation and the English source. Matching is accent- and case-insensitive (`habló` = `hablo` = `HABLO`) so you can audit a word, dialect tell, or grammatical pattern for consistency. Tapping a translated result jumps to that exact sentence; untranslated chapters show read-only snippets marked "not translated." Queries are logged to `search_queries.jsonl`.
- Annotation system (word choice, inconsistency, footnote, flag)
- Mark chapters as reviewed
- Correction workflow for batch fixes

---

## Conversational Translation (Claude Code)

The `translate-harness` skill lets you translate a book without leaving your editor. Claude drafts the style guide and glossary in-conversation, pauses for your approval at each stage, then runs the deterministic pipeline (chunk → translate → combine → EPUB).

```
/translate-harness
```

- Every artifact (style guide, glossary, chunk files) is validated before reaching the pipeline — malformed drafts produce a clear error instead of a silent schema failure.
- A hard cost gate always shows the estimate before API spending. The harness runs `--cost-only` first, asks for approval in a separate turn, then passes `--yes` only after you confirm.
- Intermediate state is stored in `.tmp/` and cleared at startup to prevent prior-session contamination.

**Subagent backend (no API key).** Step 4B offers a second translation path that needs no `ANTHROPIC_API_KEY` — translation runs as spawned worker subagents on your Claude subscription. Use `translate-prepare` to render one prompt file per chunk and produce a manifest (no spend), then `translate-commit` to validate each draft and stamp the chunks. Pass `--chapters 1-2` to either command to work in chapter batches. The same prepare/commit seam also drives a **headless** fan-out (`translate-fanout`, a `claude -p` wave), which is subscription-only by enforcement: it strips every metered credential from the CLI it launches and refuses to start unless a subscription login is confirmed. See [`docs/LLM_PROVIDERS.md`](docs/LLM_PROVIDERS.md).

```bash
python scripts/harness.py translate-prepare --project projects/my-book --chapters 1-2
# spawn worker agents per the manifest, then:
python scripts/harness.py translate-commit  --project projects/my-book
```

Worker drafts are validated before stamping — empty output, echo of the source, dropped or hallucinated image tokens, and evaluator-flagged issues all block the commit and report the chunk by name for re-spawn.

Requires Claude Code with the translate-harness skill checked into `.claude/skills/translate-harness/`.

---

## CLI Workflow

All pipeline stages are also available as CLI scripts in `scripts/`:

```bash
# Split a book into chapters
python scripts/split_book.py projects/my-book/source.txt --output projects/my-book/chapters/

# Chunk a chapter
python scripts/chunk_chapter.py projects/my-book/chapters/chapter_01.txt --chapter-id chapter_01

# Score translation difficulty (EN→ES, per chapter)
python scripts/score_difficulty.py my-book            # prints chapter table
python scripts/score_difficulty.py my-book --json     # machine-readable output
python scripts/score_difficulty.py my-book --force    # bypass cache

# API translation
python scripts/translate_api.py chunks/*.json --provider anthropic --output chunks/translated/

# Batch evaluate + combine
python scripts/batch_pipeline.py projects/my-book --stages evaluate,combine

# Build EPUB
python scripts/build_epub.py projects/my-book

# Run LLM judges (dialogue compliance, etc.) — API backend, dry-run by default
python scripts/run_judges.py run --project my-book --judge dialogue --scope chapter:chapter_03
python scripts/run_judges.py run --project my-book --suite default --scope chunk:chapter_03_chunk_000 --persist --confirm

# ...or the subagent backend (no API spend): prepare, spawn judge-workers, commit
python scripts/run_judges.py prepare --project my-book --judge dialogue --scope chapter:chapter_03
python scripts/run_judges.py commit  --project my-book --persist
```

See [`docs/BATCH_PIPELINE.md`](docs/BATCH_PIPELINE.md) for the batch CLI reference.
See [`docs/JUDGES_FRAMEWORK.md`](docs/JUDGES_FRAMEWORK.md) for LLM judge usage and how to add new judges.

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

`ANTHROPIC_API_KEY` is for the metered API path only. The headless backend strips it (and the
other Anthropic / Claude Code / Cursor metered switches it knows about) from the CLI it launches,
so a key sitting in `.env` can never turn a subscription run into a billed one.

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
│       ├── concordance.js/css  # "Find in book" search surface
│       └── setup.js/css        # Setup wizard (used by dashboard)
│
├── src/                        # Core library
│   ├── models.py               # Pydantic data models
│   ├── book_splitter.py        # Chapter detection + splitting
│   ├── chunker.py              # Chapter → translation chunks
│   ├── combiner.py             # Chunks → chapter recombination
│   ├── difficulty_scorer.py    # EN→ES difficulty scoring (sentence length + lexical rarity)
│   ├── api_translator.py       # LLM API translation (Anthropic/OpenAI)
│   ├── sentence_aligner.py     # Bilingual sentence alignment
│   ├── style_guide_wizard.py   # Style guide generation
│   ├── text_feature_detector.py # Heuristic feature scan → conditional questions
│   ├── glossary_bootstrap.py   # Glossary candidate extraction
│   ├── translator.py           # Prompt rendering + workbook generation
│   ├── epub_builder.py         # EPUB export
│   ├── harness/                # translate-harness CLI backend (flow.py beats, state.py paths)
│   ├── harness_guard.py        # Validation guards for translate-harness skill artifacts
│   ├── edit_review_constants.py # EDIT_TAGS vocabulary (shared by report generator + web UI)
│   ├── evaluators/             # Pluggable quality evaluators
│   ├── judges/                 # Tailored LLM judges (verdict / corrector framework)
│   └── utils/                  # File I/O, text utilities, source loaders, glossary context helpers
│
├── scripts/                    # CLI entry points
├── prompts/                    # LLM prompt templates (Jinja2)
├── tests/                      # pytest test suite
├── docs/                       # Documentation
├── projects/                   # Working data (gitignored)
│   ├── my-book/                # flat layout (default)
│   └── by-author/fabre/        # grouping subfolders also supported
│       └── my-book/
│       ├── source.txt          # Raw source text
│       ├── chapters/           # Split chapter files
│       ├── chunks/             # Chunk JSON files
│       ├── style.json          # Style guide
│       ├── text_features.json  # Cached heuristic feature manifest (style wizard)
│       ├── glossary.json       # Glossary
│       ├── translated/         # Combined translated chapters
│       ├── alignments/         # Sentence alignment JSON
│       ├── annotations.jsonl   # Reader annotations
│       ├── reviewed.json       # Chapter review status
│       ├── difficulty.json     # Cached difficulty scores (per-chapter + book level)
│       ├── .chunk_edits/       # Pre-edit chunk backups from the chunk editor
│       └── images/             # Downloaded images
│
├── forced_glossary_terms.example.json  # Template for forced glossary candidates (copy → forced_glossary_terms.json)
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
| [`docs/CHUNKING_GUIDE.md`](docs/CHUNKING_GUIDE.md) | Chunking algorithm, configuration, and per-chapter overrides |
| [`docs/READER_REMOVE_TEXT.md`](docs/READER_REMOVE_TEXT.md) | Reader remove-text gesture |
| [`docs/READER_RETRANSLATE.md`](docs/READER_RETRANSLATE.md) | Reader sentence retranslate flow |
| [`docs/JUDGES_FRAMEWORK.md`](docs/JUDGES_FRAMEWORK.md) | Tailored LLM judges framework: run named judges, add new judges, configure suites |
| [`docs/LLM_JUDGE_EVALUATOR.md`](docs/LLM_JUDGE_EVALUATOR.md) | LLM-judge evaluator + model comparison harness |
| [`docs/BATCH_PIPELINE.md`](docs/BATCH_PIPELINE.md) | CLI batch evaluate + combine |
| [`docs/INGEST_GUTENBERG.md`](docs/INGEST_GUTENBERG.md) | Gutenberg HTML → source.txt |
| [`docs/PROMPT_GUIDE.md`](docs/PROMPT_GUIDE.md) | Prompt template customization |
| [`docs/CHAPTER_DETECTION_GUIDE.md`](docs/CHAPTER_DETECTION_GUIDE.md) | Chapter splitting patterns |
| [`docs/DICTIONARY_SETUP.md`](docs/DICTIONARY_SETUP.md) | Dictionary evaluator setup |
| [`docs/GLOSSARY_CANDIDATES.md`](docs/GLOSSARY_CANDIDATES.md) | Glossary candidate extraction pipeline reference |
| [`docs/EDIT_REVIEW.md`](docs/EDIT_REVIEW.md) | Edit-review report: comparing translations against LLM baselines, tagging hunks |

---

## License

For use with public domain books.
