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
| `/read/` | Project list (cards with status) |
| `/project/<id>` | Pipeline dashboard (8 stages) |
| `/read/<id>` | Chapter list for a project |
| `/read/<id>/<chapter>` | Bilingual reader view |

The old `/setup/<id>` route redirects to `/project/<id>#style-guide`.

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
- `POST /api/project/<id>/config` — saves project config; currently accepts `{ "title": "..." }`

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
- Minimum overlap words (default: 150)

**Workflow:**
1. Configure chunking parameters
2. Click **Chunk All** to process every chapter
3. Shows chapter list with chunk counts after completion

**API:** `POST /api/project/<id>/chunk-all` — `{ "target_size": 2000, "overlap": 0, "min_overlap_words": 150 }`

**Backend:** `chunk_chapter()` from `src/chunker.py`. Each chunk is a `Chunk` Pydantic model serialized to JSON.

---

## Stage 4: Style Guide

**File:** `projects/<id>/style.json`

Uses the same wizard as the old setup page — all existing `/api/setup/<id>/*` endpoints are reused.

**Workflow:**
1. Answer fixed questions (register, dialect, era, audience, etc.)
2. Optionally generate LLM questions: copies a prompt → paste response → parses additional questions
3. Generate style guide: either via LLM (copy/paste) or built-in fallback
4. Save to `style.json`

**APIs (existing):**
- `POST /api/setup/<id>/prompts/questions` — generate additional questions prompt
- `POST /api/setup/<id>/prompts/style-guide` — generate style guide prompt
- `POST /api/setup/<id>/style-guide` — save style guide JSON
- `POST /api/setup/<id>/style-guide/fallback` — generate without LLM

---

## Stage 5: Glossary

**File:** `projects/<id>/glossary.json`

**Workflow:**
1. Select which style guide Q&A pairs to use as context
2. Click **Extract Candidates** — scans source text for proper nouns and terms
3. Copy the generated glossary prompt, paste into LLM, paste response back
4. Review proposals table — accept/reject each term
5. Save glossary

**APIs (existing):**
- `POST /api/setup/<id>/extract-candidates` — extract candidate terms
- `POST /api/setup/<id>/prompts/glossary` — generate glossary prompt
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
- `POST /api/project/<id>/chunks/<chunk_id>/translate` — `{ "translated_text": "..." }` save manual translation
- `POST /api/project/<id>/translate/realtime` — `{ "chunk_id": "...", "provider": "anthropic", "model": "..." }` single-chunk API translation

### Batch Translation

1. Select chapters via checkboxes
2. Click **Batch Translate Selected** → opens modal
3. Choose provider (Anthropic / OpenAI) and model
4. Cost estimate auto-calculates (input tokens × model pricing)
5. Click **Start** → launches background translation thread
6. Real-time progress via Server-Sent Events (SSE)

**APIs:**
- `POST /api/project/<id>/translate/cost-estimate` — `{ "chapters": [...], "provider": "anthropic", "model": "..." }` → `{ "cost_usd": 0.12, "input_tokens": 5000, "chunk_count": 8 }`
- `POST /api/project/<id>/translate/batch` — `{ "chapters": [...], "provider": "...", "model": "..." }` → `{ "job_id": "abc123" }`
- `GET /api/project/<id>/translate/sse?job_id=abc123` — SSE stream with events: `chunk_started`, `chunk_done`, `chunk_error`, `batch_complete`

**Available models:**
- Anthropic: Claude Sonnet 4, Claude Haiku 4.5, Claude 3.5 Sonnet, Claude 3.5 Haiku
- OpenAI: GPT-4o, GPT-4o Mini

**Backend:** `translate_chunk_realtime()` and `estimate_cost()` from `src/api_translator.py`. Glossary filtering via `filter_glossary_for_chunk()` from `src/glossary_bootstrap.py`.

---

## Stage 7: Review

Table of translated chapters with columns: chapter, alignment status, annotation count, reviewed status, actions.

**Actions:**
- **Combine + Align** — merges chunks into full chapter text, then runs sentence alignment
- **Read** — opens bilingual reader in a new tab

**APIs:**
- `POST /api/project/<id>/combine/<chapter>` — combine chunks → `translated/<chapter>.txt`
- `POST /api/project/<id>/align/<chapter>` — sentence alignment → `alignments/<chapter>.json`

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
2. **Edit area** — textarea pre-filled with Spanish text, save button to persist changes
3. **Annotation controls** — 4 types:
   - Word choice (question mark icon)
   - Inconsistency (zigzag icon)
   - Footnote (superscript icon)
   - Flag/other (ellipsis icon)

Annotated sentences get a subtle colored background tint. Each annotation has an optional note field.

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
├── project.json            # Project config (title, gutenberg_url, suggested_split_pattern)
├── source.txt              # Raw source text
├── chapters/               # Split chapter .txt files
│   ├── chapter_01.txt
│   └── ...
├── chunks/                 # Chunk JSON files
│   ├── chapter_01_chunk_000.json
│   └── ...
├── style.json              # Style guide
├── glossary.json           # Term glossary
├── translated/             # Combined translated chapter .txt files
├── alignments/             # Sentence alignment JSON
├── annotations.jsonl       # Reader annotations (append-only)
├── reviewed.json           # Chapter reviewed status
├── corrections/            # Pending corrections
├── images/                 # Downloaded images (Gutenberg)
└── <id>.epub               # Built EPUB (Stage 8 Export)
```
