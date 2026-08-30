# CLI Reference

Every pipeline stage exists as a script in `scripts/`. These are the substrate the
[harness](TRANSLATE_HARNESS.md) and the [dashboard](WEB_UI_GUIDE.md) sit on — both
surfaces ultimately call this code.

**You usually don't want these directly.** The harness gates cost, validates drafts, and
tracks state; the dashboard does the same through a UI. Reach for a raw script when you
need to re-run one stage in isolation, script something, or debug a stage in isolation.

All commands run from the repo root. On Windows, prefix with `python -X utf8` (or set
`PYTHONUTF8=1`) so accented output does not mangle.

> **Never run `scripts/generate_style_guide.py` from an agent.** It is built on `input()`
> per question and will deadlock a non-interactive caller. The harness exists partly to
> provide a non-interactive equivalent.

---

## Ingest and split

### `ingest_gutenberg.py` — Gutenberg HTML → `source.txt`

```bash
python scripts/ingest_gutenberg.py https://www.gutenberg.org/files/41350/41350-h/41350-h.htm \
    --output projects/my-book/
```

Fetches the page, strips boilerplate, converts to clean text, downloads images, and
records the heading outline. `--no-images` skips image download (placeholders still get
inserted). `--footnotes import|drop` controls whether Gutenberg footnotes become
translatable `[FOOTNOTE:N]` tokens. Full detail: [`INGEST_GUTENBERG.md`](INGEST_GUTENBERG.md).

*Harness equivalent:* `harness setup --url ...`
*Dashboard equivalent:* Stage 1 → Gutenberg URL tab

### `split_book.py` — `source.txt` → `chapters/`

```bash
python scripts/split_book.py projects/my-book/source.txt --output projects/my-book/chapters/
```

`--pattern {roman,numeric,custom}` with `--custom-regex` for anything else;
`--min-size` filters short false matches; `--front-matter` / `--back-matter` force-tag
headings.

Note this script exposes only three patterns. The harness's `split` / `split-preview`
verbs reach the full registry in `src/split_patterns.json` (heading-outline anchoring,
titled variants, all-caps headings, bare roman) — see
[`CHAPTER_DETECTION_GUIDE.md`](CHAPTER_DETECTION_GUIDE.md). Prefer the harness for
anything but a plain "Chapter I" book.

*Harness equivalent:* `harness split` (and `split-preview` to dry-run)
*Dashboard equivalent:* Stage 2

### `build_chapter_manifest.py` — retag front/back matter

Rebuilds the chapter manifest with `--front-matter` / `--back-matter` overrides.
`--dry-run` shows the plan; `--yes` applies it.

---

## Chunking and difficulty

### `chunk_chapter.py` — chapter → translation chunks

```bash
python scripts/chunk_chapter.py projects/my-book/chapters/chapter_01.txt --chapter-id chapter_01
```

`--target-size` (words per chunk), `--overlap` paragraphs, `--config` for a per-project
chunk config. Algorithm and tuning: [`CHUNKING_GUIDE.md`](CHUNKING_GUIDE.md).

*Harness equivalent:* `harness chunk --size N` (also prints the cost estimate)
*Dashboard equivalent:* Stage 3

### `score_difficulty.py` — EN→ES difficulty per chapter

```bash
python scripts/score_difficulty.py my-book          # chapter table
python scripts/score_difficulty.py my-book --json   # machine-readable
python scripts/score_difficulty.py my-book --force  # bypass the cache
```

Scores on deterministic signals (long-tail-weighted sentence length, lexical rarity, …)
and caches to `difficulty.json`. Book-level difficulty is what suggests a default chunk
size.

*Harness equivalent:* `harness difficulty`

---

## Glossary and style guide

### `extract_glossary_candidates.py` — proper nouns and recurring terms

```bash
python scripts/extract_glossary_candidates.py projects/my-book/source.txt \
    --output projects/my-book/glossary_candidates.json
```

`--min-frequency`, `--max-candidates`, `--glossary` to exclude already-known terms. The
full extraction pipeline is documented in
[`GLOSSARY_CANDIDATES.md`](GLOSSARY_CANDIDATES.md).

*Harness equivalent:* `harness glossary prepare` (renders the drafting prompt)
*Dashboard equivalent:* Stage 5 → Extract Candidates

### `generate_style_guide.py` — interactive style guide wizard

**Interactive only.** Built on `input()` per question; do not call it from an agent or a
script. Use the dashboard's Stage 4 or the harness `style-guide` beat instead.

---

## Translation

### `translate_book.py` — end-to-end orchestrator

The pipeline engine. `src/harness/flow.py` imports directly from this module, so its
cost-gate semantics are the ones the harness enforces.

```bash
# Full pipeline from a Gutenberg URL
python scripts/translate_book.py --url <gutenberg-url> --project-name my-book --target-lang es

# Cost estimate only — cannot spend
python scripts/translate_book.py --project-dir projects/my-book --cost-only

# Resume an interrupted run
python scripts/translate_book.py --project-dir projects/my-book --resume

# Re-enter at a later stage
python scripts/translate_book.py --project-dir projects/my-book --start-stage evaluate
```

Supports checkpoint/resume. Running it directly bypasses the harness's approval beats —
`--cost-only` first, always.

### `translate_api.py` — translate chunks via a provider API

```bash
python scripts/translate_api.py chunks/*.json --provider anthropic --output chunks/translated/
```

`--dry-run` estimates without spending. `--batch` submits an async batch job;
`--list-batches`, `--check-batch <id>`, and `--retrieve-batch <id>` manage it. `--model`
and `--glossary` override the defaults. Providers and models:
[`LLM_PROVIDERS.md`](LLM_PROVIDERS.md).

*Harness equivalent:* `harness translate --yes` (the one gated paid step)
*Dashboard equivalent:* Stage 6 → Batch Translate Selected

### `translate_footnotes.py` — translate imported footnote bodies

```bash
python scripts/translate_footnotes.py --project-dir projects/my-book
```

Only relevant when the book was ingested with `--footnotes import`.

*Harness equivalent:* `harness footnotes translate` (follows the book's backend)

---

## Assembly and export

### `combine_chunks.py` — chunks → chapter text

```bash
python scripts/combine_chunks.py chunks/chapter_01_chunk_*.json --output translated/chapter_01.txt
```

*Harness equivalent:* `harness combine` (normally automatic after `translate-commit`)

### `align_sentences.py` — bilingual sentence alignment

```bash
python scripts/align_sentences.py --project-id my-book --chapter-id chapter_01
```

Produces the `alignments/` JSON the reader needs. Without this, a chapter is translated
but not readable side by side.

*Harness equivalent:* `harness align`
*Dashboard equivalent:* Stage 7 → Combine + Align

### `build_epub.py` — EPUB export

```bash
python scripts/build_epub.py projects/my-book --title "My Book" --author "An Author"
```

`--cover`, `--language`, `--chapters-dir`, `--output`.

*Harness equivalent:* `harness epub`
*Dashboard equivalent:* Stage 8

### `export_bilingual.py` — plain-text bilingual export

Writes a side-by-side source/translation text file for offline review. Run it as a module
so `src/` resolves:

```bash
python -m scripts.export_bilingual chunks/chapter_01_chunk_000.json --output review_ch01.txt
```

### `fetch_missing_images.py` — backfill images

```bash
python scripts/fetch_missing_images.py projects/my-book
```

Re-downloads images referenced by `[IMAGE:...]` tokens that never made it in.
`--base-url` when the source host differs, `--force` to re-fetch everything.

---

## Quality and review

### `run_judges.py` — tailored LLM judges

```bash
# Read-only: what has a current verdict, what is stale, what was never judged
python scripts/run_judges.py status --project my-book

# API backend (cost dry-run by default)
python scripts/run_judges.py run --project my-book --judge dialogue --scope chapter:chapter_03

# Subscription backends: prepare → (fanout | spawn subagents) → commit
python scripts/run_judges.py prepare --project my-book --judge dialogue --scope chapter:chapter_03
python scripts/run_judges.py commit  --project my-book --persist
```

Sub-verbs: `profile`, `status`, `run`, `prepare`, `fanout`, `commit`, `apply`. Full
reference including how to add a judge: [`JUDGES_FRAMEWORK.md`](JUDGES_FRAMEWORK.md).
The usted/tú judge has its own guide: [`ADDRESS_JUDGE.md`](ADDRESS_JUDGE.md), and so
does the editorial judge: [`EDITORIAL_JUDGE.md`](EDITORIAL_JUDGE.md).

*Skill equivalent:* `/judge-review`

### `verify_editorial.py` — adjudicate the editorial judge's candidates

```bash
# Read-only: which chunks carry candidates nobody has second-guessed
python scripts/verify_editorial.py status --project my-book

# API backend (cost-gated), one call per chunk
python scripts/verify_editorial.py run --project my-book --persist --confirm

# Subscription backend: prepare → (fanout | spawn workers) → commit
python scripts/verify_editorial.py prepare --project my-book
python scripts/verify_editorial.py commit  --project my-book --persist
```

Pass two of the editorial judge: CONFIRM / RETRACT / RECLASSIFY over every
candidate, with the English original attached to the ones whose `source_check`
asked for it. Not idempotent — a verified chunk is skipped until `--force`.
Full reference: [`EDITORIAL_JUDGE.md`](EDITORIAL_JUDGE.md).

### `editorial_metrics.py` — editorial judge precision report

```bash
python scripts/editorial_metrics.py --project my-book
python scripts/editorial_metrics.py --project my-book --write-examples
```

Volume, precision against the human marks, adjudication deltas and excerpt
anchoring. Costs nothing: it scores what is already persisted rather than
re-running the judge. `--write-examples` turns the marked corpus into the
few-shot bank the judge reads back on its next run.

### `review_annotations.py` — resolve reader annotations

```bash
python scripts/review_annotations.py prepare --project my-book
python scripts/review_annotations.py commit  --project my-book
python scripts/review_annotations.py apply   --project my-book
```

Sub-verbs: `prepare`, `fanout`, `commit`, `run`, `apply`. `apply` is the only writer to
`annotations.jsonl`. Full reference: [`ANNOTATION_REVIEW.md`](ANNOTATION_REVIEW.md).

*Skill equivalent:* `/annotation-review`

### `review_edits.py` — edit-review diff report

```bash
python scripts/review_edits.py --project my-book --chapter chapter_01 --open
```

Generates an HTML report diffing each chunk's current translation against an LLM
baseline, with a tag vocabulary for classifying hunks. See
[`EDIT_REVIEW.md`](EDIT_REVIEW.md).

### `evaluate_chunk.py` — run evaluators over a chunk

```bash
python scripts/evaluate_chunk.py chunks/chapter_01_chunk_000.json --format text
```

`--evaluators` selects which to run (`length`, `paragraph`, `dictionary`, `glossary`,
`completeness`, `blacklist`, `grammar`; default is all available); `--format {text,json,html,all}`. The specialized variants
(`evaluate_chunk_dictionary.py`, `evaluate_chunk_glossary.py`,
`evaluate_chunk_length.py`, `evaluate_chunk_paragraph.py`) run one evaluator each.
The dictionary evaluator needs system spell-check libraries —
[`DICTIONARY_SETUP.md`](DICTIONARY_SETUP.md).

### `compare_models.py` — model comparison harness

```bash
python scripts/compare_models.py --source chunks/*.json --models claude-sonnet-5,gpt-4o --project my-book
```

Translates the same source with several models and scores them with a judge. See
[`LLM_JUDGE_EVALUATOR.md`](LLM_JUDGE_EVALUATOR.md).

### `apply_corrections.py` — apply queued reader corrections

```bash
python scripts/apply_corrections.py projects/my-book --dry-run
python scripts/apply_corrections.py projects/my-book --rebuild-epub
```

Applies corrections queued from the reader to the chunk files, re-aligns, and optionally
rebuilds the EPUB. `--skip-align` when you will align separately.

---

## Serving the app

### `serve.py` — production reader server

```bash
python scripts/serve.py
```

Runs the app under waitress on loopback with rotating logs to `logs/web_ui.log`. This is
what the `TranslateBooksReader` scheduled task runs; `scripts/reader.ps1
install|start|stop|restart|status|dev|log` drives that task. For development use
`python -m web_ui.app` instead (set `BOOKS_DEBUG=1` for auto-reload).

---

## Utilities

| Script | What it does |
|---|---|
| `search_dictionary.py` | Interactive Spanish dictionary lookup |
| `migrate_annotations.py` | One-off migration of word-level annotations to sentence-level |
| `extract_translations.py` | Pull translations out of chunk JSON |
| `run_eval_test.py` | Evaluator smoke test |

---

## Superseded

These still run and still have tests, but nothing in the harness, the dashboard, or
`src/` calls them. They predate the current pipeline and are kept for reference.

### `generate_workbook.py` / `import_workbook.py`

The original copy/paste translation loop: render a markdown workbook containing every
chunk plus glossary and previous-chapter context, translate it by hand in an external
chat, then import the result back into chunk JSON.

```bash
python scripts/generate_workbook.py chunks/chapter_01_*.json --output workbook_ch01.md
python scripts/import_workbook.py workbook_ch01.md --output chunks/translated/
```

Superseded by the harness's prepare/commit seam, which renders one prompt per chunk and
validates each draft before stamping it. Use `harness translate-prepare` /
`translate-commit` instead.

### `batch_pipeline.py`

Batch evaluate-and-combine across a whole project. Discovers chapters by scanning
`{project}/chunks/` for `*_chunk_*.json` and grouping by chapter ID.

```bash
python scripts/batch_pipeline.py projects/my-book --stages evaluate,combine
```

| Argument | Default | Description |
|---|---|---|
| `project_dir` | required | Path to the project directory |
| `--stages` | `combine` | Comma-separated: `evaluate`, `combine` |
| `--glossary` | auto-discover | Path to glossary JSON |
| `--evaluators` | `length,paragraph,completeness` | Comma-separated evaluator names |
| `--output-dir` | `{project}/translated/` | Where combined chapters go |
| `--chapters` | all | Comma-separated chapter IDs |
| `--dry-run` | off | Show the plan without changing anything |
| `--verbose` | off | Per-chunk detail |

Chapters with missing translations are skipped with an error rather than halting the run.
Superseded by `harness combine` (which `translate-commit` now runs automatically per
chapter) and by the evaluator wiring in the translation path.
