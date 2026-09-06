# Dashboard & Reader Guide

Complete reference for the web-based pipeline dashboard and bilingual reader.

> The dashboard and the [harness](TRANSLATE_HARNESS.md) are two front ends over the same
> project files. Most books are faster to *translate* in the harness and faster to
> *review* here; you can switch between them at any point without exporting anything.

## Starting the Server

```bash
python -m web_ui.app
```

Run from the repo root — `web_ui/app.py` imports `web_ui.i18n`, so launching it from
inside `web_ui/` fails. Set `BOOKS_DEBUG=1` for auto-reload and the Werkzeug debugger.

Runs on `http://localhost:5000`. Local use only — no authentication. For an always-on
service, use `python scripts/serve.py` (see [`CLI_REFERENCE.md`](CLI_REFERENCE.md)).

## Routes

| Route | Purpose |
|---|---|
| `/` | Redirects to `/read/` |
| `/read/` | Project list (cards with status) |
| `/project/<id>` | Pipeline dashboard (8 stages) |
| `/read/<id>` | Chapter list for a project |
| `/read/<id>/<chapter>` | Bilingual reader view |
| `/read/<id>/<chapter>/chunk/<chunk_id>/edit` | Full-textarea chunk editor |
| `/review-inbox` | Cross-book annotation resolutions awaiting a decision |
| `/reports/<project_id>/<filename>` | Serves generated edit-review HTML reports (same-origin for tag API) |

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
3. If already split: shows existing chapters with an inline note explaining that confirming a new split will overwrite the saved files and any per-chapter source edits. A confirmation dialog is shown before overwriting.

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

The form pre-fills with the parameters from the last successful chunk run. These are stored in `project.json` under `chunking_config` (including `min_ratio`/`max_ratio` for the Advanced section) and restored automatically on dashboard load. Each chapter also exposes a `chunk_target_override` field (integer or null) from the per-chapter sparse overrides map.

The **Chunk All** button is in the panel header alongside the status indicator.

**Workflow:**
1. Configure chunking parameters (optionally set per-chapter targets in the chapter cards)
2. Click **Chunk All** to process every chapter
3. Shows chapter list with chunk counts after completion

**APIs:**
- `POST /api/project/<id>/chunk-all` — `{ "default": { "target_size": 2000, "overlap_paragraphs": 0, "min_overlap_words": 0, "min_ratio": 0.25, "max_ratio": 1.5 }, "chapters": { "<chapter_id>": { "target_size": 1500 } } }`. Legacy flat payload `{ "target_size": 2000, ... }` is still accepted for backwards compatibility.
- `GET /api/project/<id>/difficulty` — returns cached difficulty scores for the book and all chapters. Query params: `?force=1` (also `true`/`yes`, case-insensitive) to bypass the cache and re-score from source. Response: `{ "book": { "difficulty": 0–1, "label": "easy"|"med"|"hard", "length_score": 0–1, "rarity_score": 0–1, "suggested_target": N }, "chapters": [ { "chapter_id": "...", "difficulty": 0–1, "label": "...", "suggested_target": N }, ... ] }`. Returns `404` if no chapter source files exist, `500` (sanitized) on internal errors.

**Backend:** `chunk_chapter()` from `src/chunker.py`. Each chunk is a `Chunk` Pydantic model serialized to JSON. Difficulty scoring via `score_book()` from `src/difficulty_scorer.py`; results cached to `projects/<id>/difficulty.json`.

### Difficulty Scoring

Click **Analyze difficulty** on the Stage 3 dashboard to score all chapters for EN→ES translation difficulty. Two signals are computed:

- **Sentence length (long-tail-weighted)** — long sentences carry more subordinate clauses and are where LLMs most often drop or mangle content.
- **Lexical rarity** — fraction of tokens below the Zipf frequency threshold, using `wordfreq`. Glossary terms are excluded so recurring proper names don't inflate the score.

Results are shown as **easy / med / hard** color badges on each chapter card. An overall book-level badge appears above the list with a tooltip breaking down the sub-scores.

Each badge includes a **Suggest** link that fills the chapter's Target input with the difficulty-derived recommendation (harder chapters → smaller chunks). The suggestion only fills the input — nothing is applied until you click Rechunk.

Scores are cached to `projects/<id>/difficulty.json` and reused until the source file mtime changes. Click **Analyze difficulty** again (or pass `?force=1` to the API) to force a re-score.

The same scores are available from the CLI: `python scripts/score_difficulty.py <project-id>`.

---

## Stage 4: Style Guide

**File:** `projects/<id>/style.json`

A shared **LLM provider/model selector** appears at the top of the style guide wizard. It controls which model is used for both question generation and style guide generation. See [LLM Providers](LLM_PROVIDERS.md) for configuration.

**Workflow:**
1. Answer fixed questions (register, dialect, era, audience, etc.)
2. Generate additional questions: click **Generate via API** to call the selected LLM directly, or use the copy/paste workflow (Show Prompt to Copy → paste response)
3. Generate style guide: click **Generate via API**, use **Generate from Answers (no LLM)** fallback, or copy/paste
4. Save to `style.json`

**Editing an existing style guide.** Once a guide is saved, the banner shows two buttons:
- **Edit** — opens an inline textarea with the current style guide text for direct edits. Save or Cancel with the buttons below.
- **Rebuild** — discards the current guide and re-runs the full Q&A wizard to regenerate it from scratch.

**Light style guide.** Below the guide sits an optional **Light Style Guide** textarea — at most two sentences (dialect plus high-level tone) that replace the full guide in the reader's single-sentence Retranslate prompt. Books set up through the translate harness arrive with it already filled in; anything typed here overrides that. Clearing it falls back to the full guide. See [Reader Retranslate](READER_RETRANSLATE.md). Editing the main guide never disturbs this field.

**Skipping a question.** Every question (fixed, feature-detected/conditional, or LLM-generated) has a small **Skip** checkbox in the top-right of its block. Ticking it dims the question, disables the radios, and clears any selected answer. Skipped questions are excluded from the style-guide prompt, the no-LLM fallback, the prompt-copy preview, and the Glossary stage's "choose relevant questions" list (the matching row is greyed out and its checkbox disabled). Use this when a question is irrelevant to your book or when an LLM-generated question is off-base. Skip state is session-only — reloading the dashboard clears it.

**APIs:**
- `POST /api/setup/<id>/prompts/questions` — generate additional questions prompt (for copy/paste)
- `POST /api/setup/<id>/questions/generate` — generate questions via direct LLM call; `{ "answers": {...}, "provider": "...", "model": "..." }`
- `POST /api/setup/<id>/prompts/style-guide` — generate style guide prompt (for copy/paste)
- `POST /api/setup/<id>/style-guide/generate` — generate style guide via direct LLM call; `{ "answers": {...}, "extra_questions": [...], "provider": "...", "model": "..." }`
- `POST /api/setup/<id>/style-guide` — save style guide JSON
- `POST /api/setup/<id>/style-guide/fallback` — generate without LLM
- `POST /api/setup/<id>/style-guide/light` — save the light style guide; `{ "light_content": "..." }`, empty clears it

---

## Stage 5: Glossary

**File:** `projects/<id>/glossary.json`

A **LLM provider/model selector** appears in Step 3 ("Bootstrap Translations via LLM").

**Workflow:**
1. Select which style guide Q&A pairs to use as context
2. Click **Extract Candidates** — scans source text for proper nouns and terms
3. Click **Generate via API** to translate candidates using the selected LLM, or use the copy/paste workflow
4. Review proposals table — accept/reject each term, edit `english` / `spanish` / `type` / `context` inline
5. Save glossary

**Edit existing glossary:** Once a glossary is saved, the "Existing glossary loaded (N terms)" banner shows an **Edit** button. Clicking it loads the saved `glossary.json` into the same proposals table. **Drop** a row to remove that term on save; **+ Add row** to insert a new entry; edit any cell inline. Save uses replace semantics — the table contents become the authoritative glossary, so dropped rows are deleted from `glossary.json`. The `alternatives` field on each term is preserved transparently (not shown in the UI). Translation prompts re-read `glossary.json` on each chunk, so changes take effect on the next translate or retranslate.

The **Rare-word sensitivity** slider controls how rare a word must be to surface as a candidate. Higher values include more common words; lower values restrict to the rarest vocabulary. This only affects candidate detection during this scan — existing glossary entries are not changed.

**APIs:**
- `POST /api/setup/<id>/extract-candidates` — extract candidate terms; accepts optional `zipf_offset` (float, ±1.0) to shift both Zipf rarity thresholds simultaneously — positive values surface rarer words, negative values are more permissive
- `POST /api/setup/<id>/prompts/glossary` — generate glossary prompt (for copy/paste); accepts optional `context_mode` (`"full-text"` or `"word"`) — word mode annotates each candidate with 1–2 short in-text fragments instead of a full source dump
- `POST /api/setup/<id>/glossary/generate` — generate glossary via direct LLM call; `{ "candidates": [...], "provider": "...", "model": "..." }`
- `GET /api/setup/<id>/glossary` — return current glossary as `{ "terms": [{english, spanish, type, context, alternatives}, ...] }` for the edit table
- `POST /api/setup/<id>/glossary` — save glossary JSON; accepts optional `mode: "merge" | "replace"` (default `merge`). `replace` overwrites the entire glossary; `merge` only appends terms whose `english` is not already present.

**Backend:** `extract_glossary_candidates()` from `src/glossary_bootstrap.py`; `build_glossary_prompt()` from `src/glossary_bootstrap.py`; context helpers from `src/utils/glossary_context.py`; source loaders from `src/utils/source_text.py`. See [`docs/GLOSSARY_CANDIDATES.md`](GLOSSARY_CANDIDATES.md) for the full extraction pipeline reference.

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

After all chunks finish translating, the dashboard automatically combines each affected chapter and writes its alignment file. The Review tab is ready to read without a manual "Combine + Align" click. A `chapter_aligned` SSE event is emitted for each chapter as it completes alignment.

**APIs:**
- `GET /api/llm-config` — returns available providers/models with availability flags
- `POST /api/project/<id>/translate/cost-estimate` — `{ "chapters": [...], "provider": "anthropic", "model": "..." }` -> `{ "cost_usd": 0.12, "input_tokens": 5000, "chunk_count": 8 }`
- `POST /api/project/<id>/translate/batch` — `{ "chapters": [...], "provider": "...", "model": "..." }` -> `{ "job_id": "abc123" }`
- `GET /api/project/<id>/translate/sse?job_id=abc123` — SSE stream with events: `chunk_started`, `chunk_done`, `chunk_error`, `chapter_aligned`, `batch_complete`

**Available models** are configured in `llm_config.json`. By default: Anthropic (Claude Sonnet 4.6, Claude Haiku 4.5, Claude 3.5 Sonnet, Claude 3.5 Haiku), OpenAI (GPT-4o, GPT-4o Mini), and DeepInfra (Llama 3.3 70B). Any OpenAI-compatible provider can be added.

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

- **Summary row** — severity chips (`✗ errors`, `⚠ warnings`, `ℹ info`), a **Rerun evaluators** button, and a **Run LLM judge** button. There is no aggregate score chip: the evaluators score on incompatible scales, so a mean of them ranked chunks wrongly.
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

One row per translated chapter: **Chapter · Findings · Judges · Notes · Actions**, plus a
checkbox column for selecting a subset.

- **Findings** — one chip per non-zero review category (blacklist, grammar, dictionary,
  completeness, dialogue, address, editorial). Each chip links to `/read/<id>/<chapter>?review=<category>`,
  which opens the reader in Review Mode with just that category lit *for that page load*
  — it writes neither the per-book on/off switch (localStorage) nor the global category
  cookie, so a deep link can't reset your reader settings. The reader stays the place
  findings are read and acted on.
- **Judges** — four status pips: `CD` (the deterministic evaluators, which always run as
  one set), `DL` (dialogue judge), `AD` (address judge), `ED` (editorial judge — see
  [EDITORIAL_JUDGE.md](EDITORIAL_JUDGE.md)). Each is `✓ done`, `◑ partial`,
  `⚠ stale`, or `○ not run`, with a `3/5 chunks fresh` tooltip. "Stale" comes from the
  per-evaluator content hash described in
  [JUDGES_FRAMEWORK.md](JUDGES_FRAMEWORK.md#freshness-ledger-eval_runs): edit a chunk
  anywhere — chunk editor, correction, sentence replace — and its badges go stale by
  themselves. The quality `llm_judge` is not here; it keeps its per-chunk button on the
  Translate stage.
- **Notes** — `N to review · filled/total notes`, plus a `N gaps` badge. Same numbers as
  the reader's chapter list, from the same walk.

There is no alignment percentage. It was a mean of per-row cosine similarity that read
~84% on a perfect chapter, so it painted almost everything as suspect; what matters is
whether a chapter is aligned at all, which the Align/Realign action already says.

**Actions:** Align / Realign, Read, **Rerun** (deterministic evaluators for that chapter),
**Judges…**. The toolbar runs the same two actions over the ticked chapters, or over the
whole book when nothing is ticked. Both run as background jobs with a progress modal.

### The editorial judge runs both of its passes

Ticking **Editorial defects** runs pass 1 *and* its adjudication pass as one job:
`prepare → fanout → commit`, then `adjudicate_prepare → adjudicate → adjudicate_commit`,
with the progress modal naming each. A pass-1 result is a set of *proposals* — the judge
reads the Spanish alone and over-proposes on purpose — and only pass 2 decides which of
them survive against the English. Two reasons the GUI does not stop between them:

- A badge lit by un-adjudicated candidates over-counts by the retract rate, which on a
  fresh book is the whole reason the second pass exists.
- `merge_judge_result` replaces a judge's result wholesale, so "finish it later by
  re-running the judge" would silently discard the adjudication that *had* landed.

Pass 2's size cannot be measured before pass 1 has proposed anything, and you are asked
once, before either wave. So the estimate quotes it as a **ceiling** — one job per chunk
in scope at this pipeline's own measured per-job baseline (borrowed from the pass-1 log
until adjudication has rows of its own), or on the API backend a cost that assumes every
chunk comes back at its full findings budget. Both read high on purpose.

A green `ED` pip therefore means both passes ran. When something leaves candidates
unsettled — a job killed between the passes, a fan-out whose drafts never landed, or a
pass 1 run from `run_judges.py` on the command line — the Review toolbar grows a banner
(*"3 chunks carry editorial findings that were never adjudicated"*) whose **Adjudicate…**
button runs pass 2 alone through the same modal. That gate quotes an *exact* number,
because the candidates already exist. The `ED` tooltip carries the same count per chapter.
Adjudication is not idempotent, so an already-verified chunk is skipped rather than
re-decided.

### Ignored terms

Below the table: every term this book has told the reader to stop flagging, stored in
`projects/<id>/ignored_terms.json`. **This is where they are cleared** — the reader can
only add. Each row shows the term, which check it came from (plus the rule id for
grammar), where and when it was added, and **`hides`**, the number of live findings it is
currently suppressing. A term that is also a glossary term is flagged as redundant, since
the glossary already suppresses it before the evaluator ever produces a finding.

`hides` counts **only what clearing the entry would bring back**, so it deliberately
excludes findings you had already dismissed by hand — a dismissal hides those whether or
not the entry survives. That is why the number is usually lower than a plain text search
for the word, and why a term you dismissed everywhere before ignoring it reads as `0`. Those
findings are not lost: a count with dismissals behind it is marked with a `*` and names them
on hover (`6 manually dismissed`). Only `hides` of 0 with **no** asterisk means the entry has
outlived the text it was written against and is safe to remove — a dismissal is keyed on the
finding's message and raw location, so it stops matching the moment the chunk is edited and
re-evaluated, at which point the ignore entry is the only thing still holding those findings
down.

A second, smaller reason the number can trail a text search: the dictionary and grammar
evaluators truncate their position list at three (`Character positions: 378, 1539, 2113,
... (4 total)`), so a word occurring four or more times in one chunk yields at most three
anchored findings to count.

Two things this is deliberately not:

- **Not the glossary.** Nothing here reaches the translator. The glossary is a translation
  contract for the recurring cast (median 14 occurrences per book); these are the hapax
  tail (median 1) — proper nouns, Latin, French place names, with nothing to be
  *consistent* about. Putting them in `glossary.json` does suppress the findings, which is
  why it was the workaround, but it mixes review state into the translation contract and
  leaves no way to see or undo it.
- **Not an evaluator setting.** The filter runs at read time, so adding or removing an
  entry takes effect immediately and symmetrically, with no rerun on either side. The
  stored evaluation still records everything the checker found — which means the
  evaluator's own `score` / `passed` / `metadata` in `evaluations/<chunk_id>.json` do
  **not** move when a term is ignored, only the finding counts and lists do. That matches
  how dismissals already behave, and it keeps the per-rule precision measurement honest.

Removal is a dashboard-only affordance, not a security boundary: the app has no
authentication, so every route is reachable by anything that can reach the port.

**Spelling is keyed on the word; grammar on the `(rule_id, word)` pair.** A grammar
finding is a rule firing on a word, not a fact about the word, and the words reviewers
actually ignore there are function words — `el` occurs 1412 times in one book. A word-only
grammar entry would silence every present and future rule on that token. A grammar finding
with no `rule_id` therefore cannot be ignored at all; evaluations written before rule ids
were persisted stay in that state until the chunk is re-evaluated.

### The LLM judge panel: two backends

The **Backend** picker chooses how the same judges, over the same scope, actually run:

- **API (metered)** — one LLM call per (chunk, judge), behind the dollar gate. A run whose
  estimate exceeds `cost_limit` (default $0.50) comes back `needs_confirm` **without calling
  an LLM**; confirming re-posts it.
- **Headless CLI (subscription)** — one background CLI wave (`claude -p` or `cursor-agent -p`)
  through `prepare` → `fanout` → `commit`, the same three verbs the judge-review skill drives.
  No dollars; what it spends is subscription context.

Whichever runs, the verdicts persist through the same `merge_judge_result` seam, so the pips,
the reader's Review Mode and `run_judges.py status` see one result shape. A CLI wave stamps
`backend: "headless:claude"` / `"headless:cursor"` on each verdict, so you can tell later what
judged the book.

**The CLI fields are not guesses the GUI makes.** Opening the modal fetches
`judges/profile`, which is [`resolve_profile`](LLM_PROVIDERS.md)'s answer verbatim — the same block `python scripts/run_judges.py profile` prints — so the CLI,
worker model, effort and token baseline are resolved once, together, with their provenance:

- The line under the CLI select says *why* ("detected from the session running this
  dashboard", "pinned for this book", "default (no host detected)"). The dashboard's host
  signal is the process that ran `python -m web_ui.app`, not the browser, so a guess is shown
  as a guess.
- **Remember this CLI for this book** writes `headless_cli` into `.harness/config.json` —
  identical to `harness.py config-set --key headless_cli` — which outranks detection from then
  on. Unticking it restores `auto`.
- **Effort** is offered on both CLIs, and the line under it names the channel that carries the
  level: `--effort` on Claude, the model's `[effort=…]` bracket on Cursor, or "nothing on this
  CLI carries it" (an `auto` Cursor model takes no bracket — pin a concrete model).
- **Prompt cache** is Claude-only and hidden on Cursor, which has no cache-TTL lever.
- Any field left alone is sent as `null`, so the server resolves it. That is deliberate: a GUI
  that echoed the resolved values back would make the provenance read "a flag said so" when
  nothing did.

Switching the CLI select re-fetches the whole profile; the worker model, effort, channel and
baseline change together rather than the JS synthesizing the other family's answers.

**Estimate, then confirm.** The CLI path has no threshold — **Estimate tokens** returns process
count, projected tokens with their baseline source, the effective effort and channel, and the
rendered argv. Only then does Confirm start the wave. Changing judges, scope, CLI, worker model,
effort or cache invalidates the estimate; switching the CLI also clears any worker model typed
for the other family.

Both Estimate and Confirm preflight the CLI *before* touching disk: a logged-out or uninstalled
CLI, or a `--model` `cursor-agent` will not run, 409s with the CLI's own instruction. That gate
is on both because `prepare` is destructive (it unlinks drafts and rewrites `manifest.json`) —
failing after it means the operator watched a job modal open, then read "Stopped", with the
previous wave's drafts already gone.

Asking for the address judge on a book with no `address_map.json` 409s with the
`harness.py address-map` commands that fix it, on both backends.

**APIs:**
- `GET /api/project/<id>/review-status` — the whole table: per-chapter `flag_counts`,
  `annotations`, `judges` (state + fresh/stale/missing counts), gaps, plus book-wide
  `totals`. Deliberately separate from `/status`, which every dashboard poll calls: this
  one opens and hashes every chunk, so it is fetched only when the Review stage is opened
  or a run finishes.
- `POST /api/project/<id>/review/run-coded` — `{chapter_ids: [...] | null, evaluators: [...] | null}`.
  No API spend. Returns `{job_id, total}`.
- `GET /api/project/<id>/judges/profile[?cli=claude|cursor]` — `resolve_profile`'s payload
  (`cli`, `cli_source`, `worker_model`, `effort`, `effort_channel`, `baseline_tokens`, `host`,
  `warnings`, …) plus render-only extras: `cli_choices`, `worker_model_suggestions`,
  `prompt_cache`, `prompt_cache_supported`, `binaries`, `default_backend`. Read-only; runs no
  auth probe (that belongs to the estimate step).
- `POST /api/project/<id>/judges/pin-cli` — `{cli: "claude"|"cursor"|"auto"}`. Writes that one
  key to `.harness/config.json`. (The `/config` endpoints manage the *project* `config.json`;
  this is the only harness key the dashboard writes.)
- `POST /api/project/<id>/review/run-judges` — `{chapter_ids, judges, backend, …}`.
  With `backend: "api"` (the default): `{provider, model, confirm, cost_limit, dry_run}` →
  `{status: "estimate" | "needs_confirm", estimated_cost, target_count}` or
  `{status: "started", job_id}`.
  With `backend: "headless"`: `{cli, worker_model, effort, prompt_cache, estimate, confirm}`,
  each knob `null` to let the server resolve it → `{status: "needs_confirm"}` when neither flag
  is set, `{status: "estimate", effective, jobs, prompt_tokens, projected_tokens, argv, cache,
  warnings}` for `estimate: true`, or `{status: "started", job_id, effective}` for
  `confirm: true`. `dry_run: true` means the same thing it means on the API path and outranks
  `confirm`, so a body carrying both spends nothing. A failed CLI preflight is a 409 carrying
  the CLI's own message; so is a request that arrives while a job is already running (both
  flags run `prepare`, so neither may fire mid-wave).
  The Task-subagent backend stays in the judge-review skill — a Flask worker thread cannot
  spawn Task workers.
- `GET /api/project/<id>/jobs/<job_id>/sse` — progress for either run. Always ends in one
  terminal `complete` event, carrying `fatal` if the job died (the grammar evaluator drives
  a JVM that has crashed before; the progress bar must never just stop). A CLI wave also
  emits `phase` events (`prepare` / `fanout` / `commit`), because the two ends of the cycle
  report no per-job progress at all.
- `POST /api/project/<id>/combine/<chapter>` — combine chunks → `chapters/<chapter>.txt`
- `POST /api/project/<id>/align/<chapter>` — applies any pending corrections for the chapter to chunk files first, then refreshes `chapters/<chapter>.txt` (re-combines chunks) and writes sentence alignment → `alignments/<chapter>.json`. Response includes `corrections_applied: N` with the count of queued corrections that were patched into chunks before realigning.

Only one review job runs per project at a time — two would interleave partial writes into
the same `evaluations/<chunk>.json` — so a second start returns 409 with the live `job_id`.
A headless **estimate** is refused the same way: it re-runs `prepare`, which would unlink the
running wave's drafts and swap out the manifest it is fanning out from.

**Backend:** `web_ui/jobs.py` (generic job registry), `web_ui/evaluations.py`
(`evaluator_freshness`, `chunk_group_states`, `rollup_group_state`),
`src/judges/context.py`, `combine_chunks()` from `src/combiner.py`,
`align_chapter_chunks()` from `src/sentence_aligner.py`.

---

## Stage 8: Export

**File:** `projects/<id>/<id>.epub`

Build a downloadable EPUB from all fully-translated chapters. The stage shows how many chapters (out of the total) will be included — only chapters where every chunk has been translated are packaged.

**Workflow:**
1. Title, author, and Dublin Core metadata fields are pre-populated from `project.json`
2. The coverage line shows "X of Y chapters will be included"
3. Optionally fill in the **Translator**, **Original title**, **Publisher**, **Description**, and **Rights** fields — these write Dublin Core metadata into the EPUB's OPF file
4. Optionally edit the **Translator note heading** and **Note from the translator** fields (auto-saved on blur). Leave the body blank to omit the chapter entirely.
5. Click **Build EPUB** — the backend auto-combines translated chunks and calls the epub builder
6. On success, a **Download** link appears and the file is saved to the project folder

**Cover image preview.** If `images/cover.jpg`, `images/cover.jpeg`, or `images/cover.png` exists in the project, a thumbnail is shown in the Export panel so you can confirm the correct image is present before building. The same image is auto-embedded as the EPUB cover.

Images referenced via `[IMAGE:...]` placeholders in translated text are embedded in the EPUB.

### Dublin Core metadata fields

| Field | project.json key | EPUB OPF element |
|---|---|---|
| Translator | `translator` | `dc:contributor` (MARC relator `trl`) |
| Original title | `source_title` | `dc:source` |
| Publisher | `publisher` | `dc:publisher` |
| Description | `description` | `dc:description` |
| Rights | `rights` | `dc:rights` |

All fields are optional. Values are saved to `project.json` by the `POST /api/project/<id>/config` endpoint when Build EPUB is clicked, and read back by `build_epub()` at build time.

### Translator note (optional final chapter)

The Export panel ships with a "Note from the Translator" — Amazon KDP-ready end matter that's appended as the last spine/TOC entry.

- **Heading:** plain text, defaults to `Nota del traductor` if left blank.
- **Body:** plain text. Blank-line-separated paragraphs become `<p>`s; `---` lines become `<hr/>`. `[IMAGE:...]` placeholders are stripped (translator notes don't carry their own images).
- **Empty body → no chapter is appended**, so the EPUB is byte-identical to the legacy build path when the field is left blank.
- **Per-book storage:** `projects/<id>/translator_note.json` (`{heading, body}`). Auto-saved on blur and again when Build EPUB runs (POST body wins → disk → loader → `build_epub`).
- **Default body source:** `prompts/translator_note_default.txt` (per-user, gitignored). Falls back to `prompts/translator_note_default.example.txt` (repo-tracked) on a fresh checkout. Edit either file to change what new books pre-fill.
- **Limits & safety:** body capped at 100 KB (HTTP 400 above that); heading uncapped but HTML-escaped (XSS-safe). Corrupt `translator_note.json` is renamed to `.bak.<unix-ts>` and defaults are returned silently.

**APIs:**
- `GET /api/project/<id>/epub-status` — chapter coverage, existing epub info, title/author from config
- `POST /api/project/<id>/build-epub` — `{ "title": "...", "author": "...", "translator_heading": "...", "translator_note": "..." }` → `{ "ok": true, "filename": "...", "size_bytes": N, "chapters_included": N }`. The two `translator_*` fields, when present, are persisted to `translator_note.json` before the build runs (disk = source of truth).
- `GET /api/project/<id>/translator-note` → `{ "heading": "...", "body": "..." }`
- `POST /api/project/<id>/translator-note` — `{ "heading": "...", "body": "..." }` → `{ "ok": true }`. Body > 100 KB → `400`.
- `GET /api/project/<id>/download-epub` — serves the EPUB file as a download

**Backend:** `build_epub_from_chunks()`, `build_epub()`, `note_text_to_xhtml()`, `_DEFAULT_TRANSLATOR_HEADING` from `src/epub_builder.py`; `_load_translator_note` / `_save_translator_note` / `_read_translator_note_template` from `web_ui/app.py`.

---

## Review Inbox

Served at `/review-inbox`, linked from the header of the project list. Every
in-scope book's outstanding annotation resolutions on one page, grouped by book
and by annotation type, with `old → new`, a checkbox, and a **Reject** button per
resolution.

It renders `review.apply(project_dir, dry_run=True)` — the plan already on disk
from a `commit` — and hands a selection back to `review.apply(select=…)` or
`review.apply(reject=…)`, the only writer to `annotations.jsonl`. Nothing here
reviews anything, and nothing spends.

- **Nothing is pre-ticked.** The page exists because the previous funnel (one book
  per chat session) applied 9 of ~48 resolutions.
- **Reject is per row, and there is no bulk reject.** Applying is a batch decision;
  declining is a judgement about one suggestion, and a "reject all" button is the
  gesture that empties a queue nobody read. A rejected note keeps its own text —
  only the *proposal* is refused — and no future run re-detects it. The row greys
  out with an **Undo** beside it, which lasts until you reload; the write itself is
  durable from the moment it lands.
- **Flagged** — every `low`-confidence resolution, and every footnote, whose text
  is published into the EPUB and so is where an invented fact would print.
- **Needing a hand** — `manual[]` entries with their reason (`multi_anchor`,
  `no_note_text`), which no automatic write will ever land.
- **Orphaned** — notes whose anchor sentence no longer exists. No review run will
  reach them; re-anchor them in the reader.
- **The list is what is still outstanding.** `review.apply(dry_run=True)` plans
  off `results.json`, which keeps a resolution until the next `prepare` drops it
  as `already_reviewed`, so the page compares each entry against the note's live
  *record* first: applied, rejected and deleted entries drop out, and a note edited
  since the review is shown as **stale**, explained, and not tickable. `apply`
  makes the same checks again before writing. Re-applying the same selection is a
  no-op. The record rather than its text, because a rejection changes no text —
  comparing content alone would put every rejected row straight back on the page.
- Applying a footnote reveals a **Rebuild EPUB** button: a replacing write only
  reaches the book on the next build.
- A book a CLI or scheduled wave is working on shows *in use by another run*, and
  applying is refused with a 409 rather than racing it.

Populated by the nightly pass — see [`NIGHTLY_PASS.md`](NIGHTLY_PASS.md).

### Concurrency with background waves

The dashboard's job-starting routes (`review/run-coded`, `review/run-judges`,
`review/adjudicate`) hold the book's cross-process lock
(`projects/<slug>/.harness/.lock`) for the duration of the job, and return a 409
when a CLI or scheduled wave already holds it. Before this, a wave started outside
Flask had no job record, so `jobs.JobConflict` never fired and a click here would
run a `prepare` that unlinked the drafts that wave was still writing.

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

When Review Mode is on, the sheet's Issues tab lists that sentence's findings with the four
feedback labels (resolved / false positive / bad message / missing context), each of which
marks **that one finding**. Spelling and grammar findings also get an **Ignore in this
book** button below them, which silences the term everywhere in the book at once — see
[Ignored terms](#ignored-terms). It writes one `false_positive` mark for the finding in
hand and one ignore-list entry; it deliberately does not synthesize a mark per hidden
finding, because `_feedback.jsonl` is the corpus per-rule precision is measured from. The
reader cannot undo an ignore — that lives on the dashboard's Review stage.

Once you have read a book and left annotations, `scripts/review_annotations.py`
resolves them in bulk: it researches each note against the style guide, glossary and
the whole book, writes a dated report, and — with your explicit selection — appends
its finding back into the annotation (for `footnote` notes, it drafts the actual
published gloss). See `docs/ANNOTATION_REVIEW.md`.

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

When corrections are saved from the reader, a banner appears on the chapter list page with an **Apply Corrections** button that batch-applies all pending edits. The reader passes `chunk_offset_start`/`chunk_offset_end` with each correction so `apply_corrections.py` can locate the exact span to replace — even when the corrected sentence also appears in an `[IMAGE:...]` caption or elsewhere in the same chunk. Multiple corrections to the same chunk are applied in descending-offset order to keep earlier offsets valid as text shifts.

The reader also shows a **Realign** button (topbar icon, right of chapter navigation) whenever the current chapter has unsaved pending corrections. After saving a correction via the bottom sheet the button appears automatically. Clicking it applies all queued corrections to the underlying chunk files, then regenerates the sentence alignment for the chapter in place, preserving scroll position and showing a toast on completion. Applied correction records are archived to `corrections_applied.jsonl` with a `status` field (`applied` or `skipped`); rows that could not be matched (missing chunk file, stale source text, empty `chunk_id`, load error) are archived as `skipped` rather than silently dropped. Corrections targeting other chapters are left in `corrections.jsonl`.

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
| `/api/project/<id>/align/<chapter>` | POST | Apply pending corrections to chunks, then recombine + realign (used by the reader Realign button) |
| `/api/edit-tags` | GET | Returns the `EDIT_TAGS` vocabulary list for the edit-review report tag UI |
| `/api/project/<id>/edit-tag` | POST | Persist a tag for a diff hunk; appends to `projects/<id>/edit_review_tags.jsonl` |
| `/api/project/<id>/ignored-terms` | GET | The book's ignore list, each row with a live `hides` count, a `dismissed` count, and an `in_glossary` flag |
| `/api/project/<id>/ignored-terms` | POST | Add a term (`{term, eval_name, rule_id?, added_from?, note?}`); idempotent, and `rule_id` is required for `grammar` |
| `/api/project/<id>/ignored-terms` | DELETE | Remove one entry; the term travels in the JSON body, not the path |

Both write routes answer **409** when `ignored_terms.json` exists but cannot be
read — malformed JSON, or a schema `version` this build does not understand.
The file is rewritten wholesale on every add and remove, so treating an
unreadable list as an empty one would replace it with the single entry in hand;
repair or delete the file to proceed. The read path is deliberately more
forgiving: it degrades to "nothing is ignored" so a malformed file cannot blank
the review queue.

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
├── project.json            # Project config (title, spanish_title, gutenberg_url, suggested_split_pattern, chunking_config, translator, source_title, publisher, description, rights)
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
├── difficulty.json         # Cached difficulty scores (per-chapter + book level; invalidated by source mtime)
├── corrections.jsonl       # Pending corrections (purged automatically when a chunk's translation is replaced)
├── corrections_applied.jsonl # Archive of applied corrections
├── .chunk_edits/           # Pre-edit chunk backups (last 10 per chunk, created by any translation save)
├── evaluations/            # Per-chunk evaluator output
│   ├── <chunk_id>.json     # Aggregated coded-evaluator + optional LLM-judge result
│   └── _feedback.jsonl     # Append-only user feedback on individual issues
├── ignored_terms.json      # Per-book ignore list (spelling/grammar findings the reader silenced; rewritten in place, not append-only)
├── images/                 # Downloaded images (Gutenberg)
├── translator_note.json    # Optional "Note from the Translator" (heading + body, Stage 8)
├── reports/                # Generated edit-review HTML reports (review_edits.py output)
├── edit_review_tags.jsonl  # Accumulated hunk tags from the edit-review report (append-only)
└── <id>.epub               # Built EPUB (Stage 8 Export)
```
