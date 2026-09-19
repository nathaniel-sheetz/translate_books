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

### `run_triage.py` — filter the noise out of the coded checkers

```bash
python scripts/run_triage.py status  --project my-book
python scripts/run_triage.py prepare --project my-book \
    --worker-model "grok-4.6[effort=medium,fast=false]"
python scripts/run_triage.py fanout  --project my-book
python scripts/run_triage.py commit  --project my-book
```

Sub-verbs: `status`, `prepare`, `fanout`, `commit`. `status` writes nothing and
is the one to open with — it answers how many findings are in scope, which model
would judge them, and whether the CLI can start, none of which `prepare` can be
asked without clearing the drafts. It exits 0 on a book with nothing to triage;
`prepare` treats the same state as an error (`reason: "nothing_to_triage"`).

`dictionary` and `grammar` accept at 7% and 16%, and produce ~70% of all
finding-clearing work. This asks a model whether each flagged word is really a
defect *in its sentence*, and records the answer in
`evaluations/_triage.jsonl` — nothing is deleted and no prose is touched. Only a
`suppress` at or above `TRIAGE_CONFIDENCE_FLOOR` hides a finding; suppressed
ones still appear on the recommendations screen with the model's reason.

The model is pinned at `prepare` and inherited by `fanout` from the manifest, so
the pass never rides the book's default backend. With no `--worker-model` the pin
comes from the book's `triage_worker_model`, and failing that from the model the
floor was calibrated against — so a run started from the dashboard is judged by
the same model the floor was swept on. `commit` is re-runnable.
Full reference: [`TRIAGE.md`](TRIAGE.md).

*Skill equivalent:* `/triage-review`. Also runs as the tail of the dashboard's
**Rerun deterministic** button.

### `replay_triage.py` — score the triage filter and set its cutoff

```bash
python scripts/replay_triage.py --exam
python scripts/replay_triage.py --exam --out report.json
```

Replays recorded verdicts against the human marks in `_feedback.jsonl` and
sweeps the confidence floor, reporting real defects lost (**must be 0** — the
veto) against noise removed at each. `--exam` excludes the three frozen holdout
books, which must never be tuned on. Costs nothing: it calls no model.

### `editorial_metrics.py` — editorial judge precision report

```bash
python scripts/editorial_metrics.py --project my-book
python scripts/editorial_metrics.py --project my-book --write-examples
```

Volume, precision against the human marks, adjudication deltas and excerpt
anchoring. Costs nothing: it scores what is already persisted rather than
re-running the judge. `--write-examples` turns the marked corpus into the
few-shot bank the judge reads back on its next run.

### `ledger_census.py` — reader-edit census and audit export

```bash
python scripts/ledger_census.py
python scripts/ledger_census.py --project my-book --export audit_input.jsonl
python scripts/ledger_census.py --project book-a --project book-b --freeze exam/2026-09-14
```

Per book, from `corrections_applied.jsonl`: reader edits (with repeats and
skipped rows split out), automated rows, unique reader edits per 1,000 aligned
sentences, the `retranslations.jsonl` rows the ledger never sees, and how many
edits are native-confirmed, for every book outside `.backburner`. Read-only. `--export` writes one
`(en, es_before, es_after)` row per unique landed edit with a stable `audit_id`:
the input to the Phase 0 reader-edit audit. `--freeze DIR` writes the exam
snapshot for the `--project` books into a new or empty directory: their edit rows, each
chunk's source and original LLM translation, and a manifest with file hashes.
The snapshot is written whole or not at all. Replays read the snapshot, so the
live books can keep changing. `--export` and `--freeze` refuse a slug that names
two books. `exam/` is gitignored for it.

### `panel_audit.py` — audit the reader's edits with a model panel

```bash
python scripts/panel_audit.py prepare --input audit/run/input.jsonl --run audit/run/rows20
python scripts/panel_audit.py fanout  --run audit/run/rows20
python scripts/panel_audit.py commit  --run audit/run/rows20
```

Reads the rows `ledger_census.py --export` writes. `prepare` renders them in
batches (`--rows-per-job`, default 20) into a new or empty run directory for the
panel: Grok 4.6, Gemini 3.8 Flash and GPT-5.6 Terra by default, and `--model`
repeats to change it. A batch holds one book's edits and opens with that book's
own standard: its `style.json` guide, the house rules every book is held to plus
any `style_rules.json` of its own, and `address_map.json`. The guide and the
address map are named as absent when the book lacks them; the house rules are
always there. Each edit carries its glossary
hits, the text around it in both languages (the paragraph before, and the rest
of its own paragraph, where a speaker tag sits) and a `quote_continues` flag
read from the English, so a model can tell a continuing speaker's » from a stray
closing mark. The manifest's `books` records which parts each book had. Successive saves on one
sentence are audited once, as their net change, and a sequence that ends where
it started is left out. `--project`, `--limit` and `--exclude-ids-file` narrow
the rows; excluding any save excludes its net edit. `--projects-root` points at
the directory holding the books (default `projects/`). A rendering failure
returns an error before anything is written, so the same `--run` can be
retried. `fanout` runs one headless wave per model and skips jobs that already
have a draft, so a re-run resumes. `--model` runs one panel model instead of
every one in turn, `--job-ids` (comma-separated) limits it to some jobs,
`--concurrency` caps parallel CLI processes (default 3), and `--cli` picks
`cursor` (the default) or `claude`, with `--cli-bin` when the binary is not on
`PATH`.
`commit` writes `results.jsonl` and `report.md`: verdicts per model, pairwise
agreement, the consensus buckets (silver, taste, regression queue, split), M6
and token usage. It sets an unparseable draft aside as `<job>.rejected.json` so
the next `fanout` re-runs that job. Nothing is written into `projects/`, and
`audit/` is gitignored.

### `review_annotations.py` — resolve reader annotations

```bash
python scripts/review_annotations.py prepare --project my-book
python scripts/review_annotations.py commit  --project my-book
python scripts/review_annotations.py apply   --project my-book
```

Sub-verbs: `prepare`, `fanout`, `commit`, `run`, `apply`. `apply` is the only writer to
`annotations.jsonl`; it takes `--select` to write a resolution and `--reject` to
decline one for good (the note keeps its text, and no later run re-proposes it).
Full reference: [`ANNOTATION_REVIEW.md`](ANNOTATION_REVIEW.md).

*Skill equivalent:* `/annotation-review`

### `footnote_pass.py` — author new editorial footnotes

```bash
python scripts/footnote_pass.py style  --project my-book
python scripts/footnote_pass.py add    --project my-book \
    --chapter chapter_04 --es-idx 40 --anchor "ubres," --note "Hoy sabemos que…" --dry-run
python scripts/footnote_pass.py verify --project my-book
```

Sub-verbs: `style`, `scan-prepare`, `scan-fanout`, `scan-commit`, `add`, `verify`. The
**create** counterpart to `review_annotations.py`, which can only review notes a reader
already left. `add` is the writer and validates against every way `src/endnotes.py`
drops a footnote silently — an `es_idx` with no alignment row, a sentence not findable
in the chapter body, or a note that is empty once its `[anchor]` is stripped. `verify`
applies the same audit to notes already on disk; run it after any `harness.py align`.
Full reference: [`FOOTNOTE_PASS.md`](FOOTNOTE_PASS.md).

*Skill equivalent:* `/footnote-pass`

### `pending_work.py` — what unattended work is available

```bash
python scripts/pending_work.py scan
python scripts/pending_work.py scan --json --project my-book
python scripts/pending_work.py scan --default-cli cursor   # preview the flip
```

Read-only and zero spend: per-book pending counts by type, the resolved CLI and
worker model with their provenance, skip reasons, and blockers (a missing
`.harness/config.json`, a pinned CLI whose binary is not on PATH, orphaned notes no
run will reach). Safe to run mid-wave. See [`NIGHTLY_PASS.md`](NIGHTLY_PASS.md).

### `daily_pass.py` — the nightly unattended pass

```bash
python scripts/daily_pass.py --dry-run
python scripts/daily_pass.py --project my-book
python scripts/daily_pass.py --max-books 3 --no-apply
```

Per in-scope book, under that book's cross-process lock: `prepare(keep_drafts=True)`
→ `fanout` → `commit` → auto-apply the safe subset. Writes a digest to
`reports/nightly/` and a row to `logs/nightly.jsonl`. `--cli` forces a family past
every book's pin and is for debugging only; the scheduled task never passes it.
Full reference: [`NIGHTLY_PASS.md`](NIGHTLY_PASS.md).

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

### `nightly.ps1` — the nightly scheduled task

```powershell
scripts\nightly.ps1 install     # register TranslateBooksNightly, daily at 06:30
scripts\nightly.ps1 status      # audit the live task against this script
scripts\nightly.ps1 run         # force one execution now
scripts\nightly.ps1 log         # last passes + the newest digest
scripts\nightly.ps1 spec        # what install would write, as JSON
```

Sibling to `reader.ps1`, driving `daily_pass.py`. Unlike the reader task it carries
an `ExecutionTimeLimit` (PT2H) — a batch needs a ceiling — and no restart-on-failure,
because a missed night is a night's work, not an outage, and the next run resumes
exactly where it stopped.

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
