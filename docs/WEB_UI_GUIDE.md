# Dashboard & Reader Guide

Complete reference for the web-based pipeline dashboard and bilingual reader.

## Starting the Server

```bash
cd web_ui && python app.py
```

Runs on `http://localhost:5000`. Local use only — no authentication.

## Routes

| Route | Purpose |
|---|---|
| `/` | Redirects to `/read/` |
| `/read/` | Project list (cards with status) |
| `/project/<id>` | Pipeline dashboard (8 stages) |
| `/read/<id>` | Chapter list for a project |
| `/read/<id>/<chapter>` | Bilingual reader view |
| `/read/<id>/<chapter>/chunk/<chunk_id>/edit` | Full-textarea chunk editor |

---

## Pipeline Dashboard

### Layout

Vertical stepper sidebar on the left, main content area on the right. Click any stage to jump to it. Hash-based navigation (`#source`, `#split`, `#chunk`, `#style-guide`, `#glossary`, `#translate`, `#review`, `#export`) keeps stages bookmarkable.

On initial load, the dashboard auto-navigates to the first incomplete stage.

### Status Badges

Each stepper step shows a badge derived from the filesystem:
- Source: checkmark if `source.txt` exists
- Split: chapter count
- Chunk: chunk count
- Style Guide: checkmark if `style.json` exists
- Glossary: term count
- Translate: `X/Y chunks` translated
- Review: count of aligned chapters
- Export: "ready" if EPUB has been built

---

## Stage 1: Source

**Files:** `projects/<id>/source.txt`, `projects/<id>/project.json`

**Book Title field:** At the top of this stage, enter a human-readable title for the book. This replaces the folder name everywhere it was previously shown: the sidebar, browser tab, project cards on `/read/`, the chapter list heading, and the `book_title` variable in all translation prompts. The title is saved to `project.json` and the folder name is only used as an ID in URLs.

**Spanish Title field:** Next to the Book Title, enter the translated title in Spanish. This is saved as `spanish_title` in `project.json`. When building an EPUB (Stage 8), the Spanish title is used as the default EPUB title and filename. If no Spanish title is set, the Book Title is used instead.

**If source exists:** Shows word count and a preview of the first 500 characters. If the source was imported from Gutenberg, a provenance link to the original URL is shown. "Replace" button to upload a new file.

**If no source:** A tab toggle offers two import modes:

### File / Paste (default)

Upload zone (drag-drop or click), paste textarea. Same as before.

### Gutenberg URL

Import directly from a Project Gutenberg HTML page:

1. Paste a Gutenberg URL (e.g. `https://www.gutenberg.org/files/41350/41350-h/41350-h.htm`)
2. Optionally uncheck **Download images** to insert placeholders without fetching image files
3. Click **Import from Gutenberg**

The backend fetches the HTML, strips PG boilerplate (headers/footers), converts to clean plain text, downloads images into `projects/<id>/images/`, and writes `source.txt`. After import, a **Chapter Report** table shows detected chapters with word counts and estimated chunk counts. The detected heading pattern (roman, numeric, etc.) is saved to `project.json` and auto-applied to the Stage 2 pattern selector.

**APIs:**
- `POST /api/project/<id>/ingest` — accepts multipart file upload or JSON `{ "text": "..." }`
- `POST /api/project/<id>/ingest-gutenberg` — `{ "url": "...", "download_images": true }` → `{ "ok": true, "words": N, "chapter_report": [...], "suggested_pattern": "roman", "images_downloaded": N, "images_skipped": N }`
- `GET /api/project/<id>/config` — returns project config JSON (e.g. `{ "title": "..." }`)
- `POST /api/project/<id>/config` — saves project config; accepts `{ "title": "...", "spanish_title": "..." }`

---

## Stage 2: Split into Chapters

**Files:** `projects/<id>/chapters/chapter_*.txt`

**Config options:**
- Pattern type: `roman` (default), `numeric`, `bare_roman`, `custom`
- Custom regex (when pattern is `custom`)
- Minimum chapter size in characters

**Workflow:**
1. Click **Preview** to dry-run detection — shows cards with chapter title, word count, and first 200 characters
2. Click **Confirm & Split** to write files
3. If already split: shows existing chapters with a "Re-split" option (warns about overwrite)

**APIs:**
- `POST /api/project/<id>/split/preview` — `{ "pattern_type": "roman" }` → list of detected chapters
- `POST /api/project/<id>/split` — same body → writes files, returns count

**Backend:** `split_book_into_chapters()` and `save_chapters_to_files()` from `src/book_splitter.py`.

---

## Stage 3: Chunk Chapters

**Files:** `projects/<id>/chunks/<chapter_id>_chunk_*.json`

**Config options:**
- Target size in words (default: 2000)
- Overlap paragraphs (default: 0)
- Minimum overlap words (default: 0)

**Workflow:**
1. Configure chunking parameters
2. Click **Chunk All** to process every chapter
3. Shows chapter list with chunk counts after completion

**API:** `POST /api/project/<id>/chunk-all` — `{ "target_size": 2000, "overlap": 0, "min_overlap_words": 0 }`

**Backend:** `chunk_chapter()` from `src/chunker.py`. Each chunk is a `Chunk` Pydantic model serialized to JSON.

---

## Stage 4: Style Guide

**File:** `projects/<id>/style.json`

A shared **LLM provider/model selector** appears at the top of the style guide wizard. It controls which model is used for both question generation and style guide generation. See [LLM Providers](LLM_PROVIDERS.md) for configuration.

**Workflow:**
1. Answer fixed questions (register, dialect, era, audience, etc.)
2. Generate additional questions: click **Generate via API** to call the selected LLM directly, or use the copy/paste workflow (Show Prompt to Copy → paste response)
3. Generate style guide: click **Generate via API**, use **Generate from Answers (no LLM)** fallback, or copy/paste
4. Save to `style.json`

**APIs:**
- `POST /api/setup/<id>/prompts/questions` — generate additional questions prompt (for copy/paste)
- `POST /api/setup/<id>/questions/generate` — generate questions via direct LLM call; `{ "answers": {...}, "provider": "...", "model": "..." }`
- `POST /api/setup/<id>/prompts/style-guide` — generate style guide prompt (for copy/paste)
- `POST /api/setup/<id>/style-guide/generate` — generate style guide via direct LLM call; `{ "answers": {...}, "extra_questions": [...], "provider": "...", "model": "..." }`
- `POST /api/setup/<id>/style-guide` — save style guide JSON
- `POST /api/setup/<id>/style-guide/fallback` — generate without LLM

---

## Stage 5: Glossary

**File:** `projects/<id>/glossary.json`

A **LLM provider/model selector** appears in Step 3 ("Bootstrap Translations via LLM").

**Workflow:**
1. Select which style guide Q&A pairs to use as context
2. Click **Extract Candidates** — scans source text for proper nouns and terms
3. Click **Generate via API** to translate candidates using the selected LLM, or use the copy/paste workflow
4. Review proposals table — accept/reject each term
5. Save glossary

**APIs:**
- `POST /api/setup/<id>/extract-candidates` — extract candidate terms
- `POST /api/setup/<id>/prompts/glossary` — generate glossary prompt (for copy/paste)
- `POST /api/setup/<id>/glossary/generate` — generate glossary via direct LLM call; `{ "candidates": [...], "provider": "...", "model": "..." }`
- `POST /api/setup/<id>/glossary` — save glossary JSON

**Backend:** `extract_glossary_candidates()` from `src/glossary_bootstrap.py`.

---

## Stage 6: Translate

The most complex stage. Two sub-views: chapter overview and chunk detail.

### Chapter Overview

Table of all chapters with columns: checkbox, chapter name, chunk count, translated count, status, actions.

**Status pills:** `done` (green), `partial` (yellow), `pending` (gray).

**Actions per chapter:**
- Click row to expand → shows chunk detail
- "Read" link (if alignment exists)

### Chunk Detail (Expanded Chapter)

Tabs across the top, one per chunk. Each tab shows:

1. **Source text** — first 500 chars with "Show full" toggle
2. **Prompt** — fully rendered translation prompt (readonly textarea) with **Copy Prompt** button
3. **Translation** — textarea for pasting, with **Save Translation** and **Auto-Translate** buttons

The prompt includes: style guide, filtered glossary (only terms appearing in this chunk), previous chunk context, and source text.

**Chunk tab indicators:** filled dot = translated, empty dot = pending.

**APIs:**
- `GET /api/project/<id>/chapters/<chapter>/chunks` — list chunks with status
- `GET /api/project/<id>/chunks/<chunk_id>/prompt` — rendered translation prompt
- `POST /api/project/<id>/chunks/<chunk_id>/translate` — `{ "translated_text": "..." }` save manual translation. Runs the full post-save pipeline: backs up the chunk, purges stale corrections, recombines the chapter, realigns sentences, and re-anchors any existing annotations.
- `POST /api/project/<id>/translate/realtime` — `{ "chunk_id": "...", "provider": "anthropic", "model": "..." }` single-chunk API translation. Runs the same post-save pipeline as the manual save above.

### Batch Translation

1. Select chapters via checkboxes
2. Click **Batch Translate Selected** → opens modal
3. Choose provider and model (dynamically populated from `llm_config.json` — see [LLM Providers](LLM_PROVIDERS.md))
4. Cost estimate auto-calculates (input tokens x model pricing from config)
5. Click **Start** — launches background translation thread
6. Real-time progress via Server-Sent Events (SSE)

**APIs:**
- `GET /api/llm-config` — returns available providers/models with availability flags
- `POST /api/project/<id>/translate/cost-estimate` — `{ "chapters": [...], "provider": "anthropic", "model": "..." }` -> `{ "cost_usd": 0.12, "input_tokens": 5000, "chunk_count": 8 }`
- `POST /api/project/<id>/translate/batch` — `{ "chapters": [...], "provider": "...", "model": "..." }` -> `{ "job_id": "abc123" }`
- `GET /api/project/<id>/translate/sse?job_id=abc123` — SSE stream with events: `chunk_started`, `chunk_done`, `chunk_error`, `batch_complete`

**Available models** are configured in `llm_config.json`. By default: Anthropic (Claude Sonnet 4, Claude Haiku 4.5, Claude 3.5 Sonnet, Claude 3.5 Haiku), OpenAI (GPT-4o, GPT-4o Mini), and DeepInfra (Llama 3.3 70B). Any OpenAI-compatible provider can be added.

**Backend:** `translate_chunk_realtime()` and `estimate_cost()` from `src/api_translator.py`. Glossary filtering via `filter_glossary_for_chunk()` from `src/glossary_bootstrap.py`.

### Evaluator Card

Every chunk save (manual, auto-translate, or edits from the chunk editor) triggers the full evaluator suite as a post-save side effect. Results are persisted per chunk under `projects/<id>/evaluations/<chunk_id>.json` and rendered into the Translate panel as an **evaluator card** directly below the translation textarea.

**Coded evaluators** (always run):

| Evaluator | What it checks |
|---|---|
| `length` | Translation length falls within an acceptable ratio of the source |
| `paragraph` | Paragraph count / break structure matches the source |
| `dictionary` | Flags unknown or suspect Spanish words |
| `glossary` | Enforces project glossary term choices in translated text |
| `completeness` | Detects dropped source content |
| `blacklist` | Surfaces forbidden words / phrases |
| `grammar` | Basic Spanish grammar heuristics |

**Card layout:**

- **Summary row** — severity chips (`✗ errors`, `⚠ warnings`, `ℹ info`), an `average_score` chip, a **Rerun evaluators** button, and a **Run LLM judge** button.
- **Grouped issue list** — one collapsible section per evaluator. Each issue row shows:
  - Severity icon + evaluator tag + `issue.message`
  - Context line with the offending span highlighted via `<mark>` (`…{snippet_before}<mark>{match}</mark>{snippet_after}…`). Falls back to the containing paragraph when the evaluator didn't report a precise location, or `(no location — evaluator gap)` when neither is available.
  - Suggestion (muted text, when the evaluator provides one)
  - Three feedback buttons — **false positive**, **bad message**, **gap** — that append to `projects/<id>/evaluations/_feedback.jsonl`
  - **raw** disclosure → reveals the original `Issue.location` string and a collapsed `<pre>` of the issue's metadata, useful for iterating on the evaluators themselves.
- **Empty state** — "All evaluators passed."

**LLM judge (opt-in):** Click **Run LLM judge** to call a configured LLM with the source text, translation, style guide, and the coded-evaluator results. The judge returns a normalized score (1–5 internal, surfaced as 0.0–1.0) plus optional issues and notes. The result merges into the existing evaluation file under a separate `llm_judge` section and appears below the coded evaluators. Requires an `llm_config.json` at the project root — the button returns `409` if no LLM is configured.

**Chapter-table badges:** Aggregated error/warning counts for each chapter are rendered as small badges next to the chapter name (e.g. `✗ 3` / `⚠ 7`). The rollup reads the persisted evaluation files and refreshes on stage load and after every evaluation run.

**APIs:**
- `GET  /api/project/<id>/evaluations/<chunk_id>` — load the most recent persisted evaluation for a chunk
- `POST /api/project/<id>/evaluations/<chunk_id>/rerun` — re-run all coded evaluators against the current translation (preserves any existing `llm_judge` result by default)
- `POST /api/project/<id>/evaluations/<chunk_id>/llm_judge` — run the LLM judge and merge the result into the stored evaluation; `409` if unconfigured, `500` on LLM error
- `POST /api/project/<id>/evaluations/<chunk_id>/feedback` — append a feedback entry; body `{ "type": "false_positive" | "bad_message" | "missing_context_gap", "eval_name": "...", "issue_index": N, ... }`
- `GET  /api/project/<id>/evaluations/summary` — returns `{ summary: {chunk_id: {errors, warnings, info}}, by_chapter: {chapter_id: {errors, warnings, info}} }` for badge rendering

**Backend:**
- `src/evaluators/` — the seven coded evaluators, the registry, and `aggregate_results()`
- `src/evaluators/location_normalizer.py` — parses every `Issue.location` format into a `NormalizedLocation` and fans multi-location issues into per-location rows for rendering
- `web_ui/evaluations.py` — orchestration and persistence (`run_coded_evaluators()`, `evaluate_and_persist_chunk()`, atomic JSON writes, LLM-judge merging, feedback append, per-project summary walk)
- Hooked from `_replace_chunk_translation()` in `web_ui/app.py`, so every save path (manual, auto-translate, chunk editor) produces fresh results.

The evaluator card lives only in the dashboard (`#chunk-detail-container` in `dashboard.html`). It is not rendered in the bilingual reader or the chunk editor.

---

## Stage 7: Review

Table of translated chapters with columns: chapter, alignment status, annotation count, reviewed status, actions.

**Actions:**
- **Combine + Align** — merges chunks into full chapter text, then runs sentence alignment
- **Read** — opens bilingual reader in a new tab

**APIs:**
- `POST /api/project/<id>/combine/<chapter>` — combine chunks → `chapters/<chapter>.txt`
- `POST /api/project/<id>/align/<chapter>` — refreshes `chapters/<chapter>.txt` (re-combines chunks), then writes sentence alignment → `alignments/<chapter>.json`

**Backend:** `combine_chunks()` from `src/combiner.py`, `align_chapter_chunks()` from `src/sentence_aligner.py`.

---

## Stage 8: Export

**File:** `projects/<id>/<id>.epub`

Build a downloadable EPUB from all fully-translated chapters. The stage shows how many chapters (out of the total) will be included — only chapters where every chunk has been translated are packaged.

**Workflow:**
1. Title and author fields are pre-populated from `project.json`
2. The coverage line shows "X of Y chapters will be included"
3. Click **Build EPUB** — the backend auto-combines translated chunks and calls the epub builder
4. On success, a **Download** link appears and the file is saved to the project folder

Images referenced via `[IMAGE:...]` placeholders in translated text are embedded in the EPUB. A cover image is auto-detected from `images/cover.jpg` (or `.png`) if present.

**APIs:**
- `GET /api/project/<id>/epub-status` — chapter coverage, existing epub info, title/author from config
- `POST /api/project/<id>/build-epub` — `{ "title": "...", "author": "..." }` → `{ "ok": true, "filename": "...", "size_bytes": N, "chapters_included": N }`
- `GET /api/project/<id>/download-epub` — serves the EPUB file as a download

**Backend:** `build_epub()` from `src/epub_builder.py`, `combine_chunks()` from `src/combiner.py`.

---

## Bilingual Reader

Served at `/read/<project_id>/<chapter>`. Separate from the dashboard — uses serif, reading-optimized CSS.

### Navigation

- `/read/` — project cards with style guide, glossary, and translation status
- `/read/<id>` — chapter list with badges (annotation counts, confidence, reviewed status)
- `/read/<id>/<chapter>` — reading view with prev/next chapter navigation

### Reading View

Sentences are displayed as a vertical list of Spanish text. Tap any sentence to open the bottom sheet showing:

1. **English source** — the aligned original sentence
2. **Edit chunk button** — opens the full chunk editor (see below) scrolled to the tapped sentence
3. **Edit area** — textarea pre-filled with Spanish text, save button to persist changes
4. **Annotation controls** — 4 types:
   - Word choice (question mark icon)
   - Inconsistency (zigzag icon)
   - Footnote (superscript icon)
   - Flag/other (ellipsis icon)

Annotated sentences get a subtle colored background tint. Each annotation has an optional note field.

### Chunk Editor

For edits that don't fit the one-sentence-at-a-time flow — stray whitespace, wrong paragraph breaks, multi-sentence rewording — tap a sentence in the reader and click **Edit chunk** in the bottom sheet. That opens a full-textarea editor (`/read/<id>/<chapter>/chunk/<chunk_id>/edit`) for the chunk containing the tapped sentence, with the caret pre-positioned near that sentence.

On save, the endpoint:

1. Guards the edit: rejects if the chunk's file mtime has changed since the editor opened, if any `[IMAGE:...]` placeholder was added/removed/reordered, or if the edit touches a non-zero overlap region.
2. Delegates to the shared `_replace_chunk_translation` pipeline (same pipeline used by the dashboard's manual save and auto-translate), which:
   - Backs up the pre-edit chunk JSON to `projects/<id>/.chunk_edits/<chapter>/<chunk_id>/<timestamp>.json` (last 10 per chunk retained).
   - Writes the new `translated_text` to the chunk file.
   - Purges any stale corrections for this chunk from `corrections.jsonl` (they reference old text that no longer exists).
   - Recombines the chapter into `chapters/<chapter>.txt` via `combine_chunks()`.
   - Realigns the chapter via `align_chapter_chunks()`.
   - Re-anchors annotations for this chapter: any annotation whose sentence still exists (matched by exact text, then by 30-char prefix) is rewritten to the new `es_idx`. Unmatched ones are reported in the response as orphaned and left in place.

After a successful save the reader reopens scrolled to the same sentence via a text-prefix anchor (so the scroll point survives any index shift from realign).

**Limitations:**

- Edits that straddle a chunk boundary aren't possible — pick whichever chunk contains the issue.
- Chunks with non-zero `overlap_start`/`overlap_end` have those regions locked (the server refuses to save any change inside them, since `combine_chunks()` would drop them anyway). Projects chunked with the current `overlap_paragraphs=0` default are unaffected.
- Annotations that can't be re-anchored by text are left at their old `es_idx` and surfaced as orphaned in the API response.

### Chapter Status

- **Unread** — no annotations, not reviewed
- **Reviewed** — marked complete (checkmark badge)
- **Badge counts** — review annotations, footnotes, flags, low-confidence alignment

### Corrections

When corrections are saved from the reader, a banner appears on the chapter list page with an **Apply Corrections** button that batch-applies all pending edits.

### Reader APIs

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/alignment/<id>/<chapter>` | GET | Alignment data with enrichments |
| `/api/correction` | POST | Save a sentence correction |
| `/api/annotations/<id>/<chapter>` | GET | Get chapter annotations |
| `/api/annotation` | POST | Save annotation |
| `/api/annotation` | DELETE | Remove annotation |
| `/api/reviewed/<id>/<chapter>` | GET/POST/DELETE | Reviewed status |
| `/api/apply-corrections/<id>` | POST | Batch apply corrections |
| `/api/chunk/<id>/<chunk_id>/edit` | POST | Save a full-chunk text edit (recombines + realigns the chapter) |

---

## Internationalization

The UI supports English and Spanish. Toggle via the language buttons on the project list page. Reader strings are managed server-side in `web_ui/i18n.py`; dashboard strings are in-page.

---

## Security

This is a **local-only** application. No authentication, no HTTPS, no rate limiting. Not suitable for public deployment.

---

## Project Data Layout

All state is derived from the filesystem — no database.

```
projects/<id>/
├── project.json            # Project config (title, spanish_title, gutenberg_url, suggested_split_pattern)
├── source.txt              # Raw source text
├── chapters/               # Chapter .txt files (combined translated output)
│   ├── chapter_01.txt
│   └── ...
├── chunks/                 # Chunk JSON files
│   ├── chapter_01_chunk_000.json
│   └── ...
├── style.json              # Style guide
├── glossary.json           # Term glossary
├── alignments/             # Sentence alignment JSON
├── annotations.jsonl       # Reader annotations (append-only)
├── reviewed.json           # Chapter reviewed status
├── corrections.jsonl       # Pending corrections (purged automatically when a chunk's translation is replaced)
├── corrections_applied.jsonl # Archive of applied corrections
├── .chunk_edits/           # Pre-edit chunk backups (last 10 per chunk, created by any translation save)
├── evaluations/            # Per-chunk evaluator output
│   ├── <chunk_id>.json     # Aggregated coded-evaluator + optional LLM-judge result
│   └── _feedback.jsonl     # Append-only user feedback on individual issues
├── images/                 # Downloaded images (Gutenberg)
└── <id>.epub               # Built EPUB (Stage 8 Export)
```
