# Book Translation Pipeline

Translate public-domain books from English to Spanish, well enough to read for pleasure.

Machine translation gets you a draft. This gets you a book: a style guide and glossary
drafted *for this specific text* before a word is translated, per-chunk prompts that carry
the previous chapter's voice forward, LLM judges that check dialogue punctuation and
usted/tú consistency across the whole book, and a bilingual reader where you fix what's
left — sentence by sentence — before exporting an EPUB.

<!-- TODO: screenshot of the bilingual reader goes here -->

---

## Two ways to use it

### 1. Conversationally, in Claude Code

The primary path. Claude drafts the style guide and glossary *in the conversation*, where
you argue with it, then runs the deterministic pipeline (chunk → translate → combine →
EPUB) around your approvals.

```
/translate-harness
```

There is exactly one step that can spend money, and it fails closed until you approve the
estimate in a separate turn. Two of the three translation backends spend nothing at all —
they run on your Claude subscription and physically strip metered credentials from the
processes they launch.

**→ [`docs/TRANSLATE_HARNESS.md`](docs/TRANSLATE_HARNESS.md)**

### 2. In the browser

A pipeline dashboard and a bilingual reader. This is where reviewing happens: tap any
sentence to see its source and edit the translation, search the whole book for a word to
audit its consistency, annotate what needs a second look, then export.

```bash
pip install -r requirements.txt
python -m web_ui.app
```

Open `http://localhost:5000`.

**→ [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) · [`docs/WEB_UI_GUIDE.md`](docs/WEB_UI_GUIDE.md)**

The two surfaces write the same project files, so you can translate in the harness and
review in the reader without exporting anything between them.

---

## The pipeline

| # | Stage | What it does |
|---|-------|-------------|
| 1 | **Source** | Import from a Project Gutenberg URL, upload, or paste → `source.txt` |
| 2 | **Split** | Detect chapter boundaries → individual chapter files |
| 3 | **Chunk** | Break chapters into ~2000-word translation units |
| 4 | **Style Guide** | Dialect, register, forms of address, dialogue formatting — drafted against a scan of the actual text |
| 5 | **Glossary** | Consistent renderings for names, places, and recurring terms |
| 6 | **Translate** | API, Claude subagents, or a headless CLI wave |
| 7 | **Review** | Align sentences, read bilingually, annotate, judge |
| 8 | **Export** | EPUB with images and an optional translator's note |

---

## The bilingual reader

Open any aligned chapter at `/read/<project_id>/<chapter>`:

- **Side-by-side sentences.** Tap a Spanish sentence to see its English source and edit
  the translation in place.
- **Find in book.** Search any fragment and see every occurrence across the whole book as
  a source + translation pair. Accent- and case-insensitive (`habló` = `hablo` = `HABLO`),
  so you can audit a word choice, a dialect tell, or a grammatical pattern for consistency
  in one pass.
- **Retranslate a sentence** with a per-call model picker, confirming the source span
  first because alignment isn't always perfect.
- **Edit the whole chunk** when the problem is a paragraph break rather than a sentence.
  Saves recombine and realign automatically, with backups under `.chunk_edits/`.
- **Remove stray text** — a caption or OCR artifact — from both sides at once, with
  overlap regions propagated and the chapter re-aligned.
- **Annotate** (word choice, inconsistency, footnote, flag) and resolve the notes later
  with `/annotation-review`.

---

## Quality checks

Tailored LLM judges run over translated chapters and report findings you can apply
selectively:

- **Dialogue compliance** — Spanish dialogue punctuation against your own `dialogue.txt`.
- **Forms of address** — usted/tú against a per-book address map built from the text.

Both run on a metered API backend or on your subscription. See
[`docs/JUDGES_FRAMEWORK.md`](docs/JUDGES_FRAMEWORK.md).

---

## Installation

```bash
pip install -r requirements.txt
```

Python 3.11+. For the metered API path, add keys:

```bash
cp .env.example .env    # ANTHROPIC_API_KEY and/or OPENAI_API_KEY
```

`ANTHROPIC_API_KEY` is for the metered path only. The headless backend strips it — and
every other metered switch it knows about — from the CLI it launches, so a key sitting in
`.env` can never turn a subscription run into a billed one.

The optional dictionary evaluator needs system spell-check libraries; see
[`docs/DICTIONARY_SETUP.md`](docs/DICTIONARY_SETUP.md).

### Running as a service

`python scripts/serve.py` runs the app under waitress on loopback, and
`scripts/reader.ps1 install|start|stop|restart|status|dev|log` drives the
`TranslateBooksReader` scheduled task — handy for reading on a phone over Tailscale. See
[`docs/TAILSCALE.md`](docs/TAILSCALE.md).

---

## Command line

Every stage is also a script in `scripts/`, which is what both surfaces call underneath:

```bash
python scripts/ingest_gutenberg.py <url> --output projects/my-book/
python scripts/harness.py status --project my-book
python scripts/run_judges.py status --project my-book
```

Full per-script reference: [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md).

---

## Project structure

```
book_translation/
├── web_ui/                     # Flask web application
│   ├── app.py                  # All routes (dashboard, reader, setup, APIs)
│   ├── i18n.py                 # Server-side translations (EN/ES)
│   ├── templates/              # dashboard, reader, chunk editor, edit-review report
│   └── static/                 # dashboard / reader / concordance / setup JS + CSS
│
├── src/                        # Core library
│   ├── models.py               # Pydantic data models
│   ├── book_splitter.py        # Chapter detection + splitting
│   ├── split_patterns.json     # Chapter-heading pattern registry
│   ├── chunker.py              # Chapter → translation chunks
│   ├── combiner.py             # Chunks → chapter recombination
│   ├── difficulty_scorer.py    # EN→ES difficulty scoring
│   ├── api_translator.py       # LLM API translation + prompt building
│   ├── sentence_aligner.py     # Bilingual sentence alignment
│   ├── style_guide_wizard.py   # Style guide generation
│   ├── text_feature_detector.py # Heuristic feature scan → conditional questions
│   ├── glossary_bootstrap.py   # Glossary candidate extraction
│   ├── epub_builder.py         # EPUB export
│   ├── harness/                # translate-harness backend (flow.py, state.py, headless.py, locks.py)
│   ├── harness_guard.py        # Validation guards for harness artifacts
│   ├── actions/                # Unattended work units for the nightly pass
│   ├── evaluators/             # Pluggable quality evaluators
│   ├── judges/                 # Tailored LLM judges
│   └── utils/                  # File I/O, text utilities, source loaders
│
├── scripts/                    # CLI entry points (harness.py is the main one)
├── prompts/                    # Prompt templates
├── tests/                      # pytest suite
├── docs/                       # Documentation
└── projects/                   # Working data (gitignored)
    └── my-book/                # flat, or nested under grouping folders
        ├── source.txt          # Raw source text
        ├── chapters/           # Split chapter files
        ├── chunks/             # Chunk JSON files
        ├── style.json          # Style guide
        ├── glossary.json       # Glossary
        ├── address_map.json    # usted/tú map
        ├── translated/         # Combined translated chapters
        ├── alignments/         # Sentence alignment JSON
        ├── annotations.jsonl   # Reader annotations
        ├── difficulty.json     # Cached difficulty scores
        ├── .harness/           # Harness state, prompts, worker drafts, and .lock
        └── images/             # Downloaded images
```

Projects can be flat (`projects/my-book/`) or organized into grouping subfolders
(`projects/by-author/fabre/my-book/`) — the dashboard, the APIs, and the CLI find them
either way.

---

## Tests

```bash
pytest                          # everything
pytest tests/test_web_ui.py -v  # web UI
pytest --cov=src tests/         # with coverage
```

---

## Documentation

**Translating a book**

| Document | Contents |
|---|---|
| [`docs/TRANSLATE_HARNESS.md`](docs/TRANSLATE_HARNESS.md) | The harness: beats, backends, cost gates, full CLI reference |
| [`docs/LLM_PROVIDERS.md`](docs/LLM_PROVIDERS.md) | Providers, models, subscription CLIs |
| [`docs/INGEST_GUTENBERG.md`](docs/INGEST_GUTENBERG.md) | Gutenberg HTML → `source.txt`, footnote handling |
| [`docs/CHAPTER_DETECTION_GUIDE.md`](docs/CHAPTER_DETECTION_GUIDE.md) | Chapter splitting patterns and the pattern registry |
| [`docs/CHUNKING_GUIDE.md`](docs/CHUNKING_GUIDE.md) | Chunking algorithm, configuration, per-chapter overrides |
| [`docs/PROMPT_GUIDE.md`](docs/PROMPT_GUIDE.md) | Prompt templates and how to customize them |

**Dashboard and reader**

| Document | Contents |
|---|---|
| [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md) | Dashboard walkthrough, first book start to finish |
| [`docs/WEB_UI_GUIDE.md`](docs/WEB_UI_GUIDE.md) | Full dashboard + reader reference |
| [`docs/READER_RETRANSLATE.md`](docs/READER_RETRANSLATE.md) | Reader sentence-retranslate flow |
| [`docs/READER_REMOVE_TEXT.md`](docs/READER_REMOVE_TEXT.md) | Reader remove-text gesture |

**Quality and review**

| Document | Contents |
|---|---|
| [`docs/JUDGES_FRAMEWORK.md`](docs/JUDGES_FRAMEWORK.md) | Tailored LLM judges: run them, add them, configure suites |
| [`docs/ADDRESS_JUDGE.md`](docs/ADDRESS_JUDGE.md) | The address map and the usted/tú judge |
| [`docs/EDITORIAL_JUDGE.md`](docs/EDITORIAL_JUDGE.md) | The editorial defect judge and its adjudication pass |
| [`docs/ANNOTATION_REVIEW.md`](docs/ANNOTATION_REVIEW.md) | Resolving reader annotations and drafting footnote glosses |
| [`docs/NIGHTLY_PASS.md`](docs/NIGHTLY_PASS.md) | The scheduled cross-book pass, its locks, and the `/review-inbox` funnel |
| [`docs/EDIT_REVIEW.md`](docs/EDIT_REVIEW.md) | Comparing translations against LLM baselines, tagging hunks |
| [`docs/LLM_JUDGE_EVALUATOR.md`](docs/LLM_JUDGE_EVALUATOR.md) | LLM-judge evaluator + model comparison harness |

**CLI and internals**

| Document | Contents |
|---|---|
| [`docs/CLI_REFERENCE.md`](docs/CLI_REFERENCE.md) | Every script in `scripts/`, live and superseded |
| [`docs/GLOSSARY_CANDIDATES.md`](docs/GLOSSARY_CANDIDATES.md) | Glossary candidate extraction pipeline |
| [`docs/DICTIONARY_SETUP.md`](docs/DICTIONARY_SETUP.md) | Dictionary evaluator setup |

---

## License

MIT — see [LICENSE](LICENSE). That covers the code in this repository.
The books it is built for are public domain, and the translations it produces
are yours; neither is governed by that license.
