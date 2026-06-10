# TODOs

Verified against the codebase on 2026-05-18. Line numbers refreshed from main HEAD.

## P1 — Bugs worth fixing soon

### Make `_apply_chunk_edits` transactional
**What:** `_apply_chunk_edits` (web_ui/app.py:3904-4019) writes chunk JSON first, then recombines/realigns/re-anchors/evaluates. A `.chunk_edits/<chapter_id>/<chunk_id>/<ts>.json` backup is taken before save (line 3938), but nothing restores it if recombine/realign throws. The endpoint returns 500 while chunks/, chapters/<id>.txt, alignments, and annotations are in inconsistent states.
**Why:** Every chunk-edit path (sentence replace, chunk editor, remove-text) shares this. Surfaced by codex adversarial review on the retranslate path.
**How:** On any exception after save_chunk, restore each backup over its chunk_path before re-raising. Optionally extend to also stash `alignments/<chapter_id>.json` and `chapters/<chapter_id>.txt` so all four files revert together.


### Reject `[IMAGE:...]` tokens in retranslate output
**What:** `/api/sentence/replace` accepts any non-empty `new_translation` and splices it into the chunk. `_enrich_alignment` (web_ui/app.py:1092) parses `[IMAGE:...]` tokens as structural references — a hallucinated LLM output containing one would render as a broken image row.
**Why:** LLM trust-boundary on the retranslate path. Codex adversarial review. Low realistic risk on solo local Flask but cheap to fix.
**How:** Scan `new_translation` against `_IMAGE_TOKEN_RE` (web_ui/app.py:3637). If matched, return 422 with a clear error. Regression test with a forged response.

### Build EPUBs from translated chapters only (pipeline parity with web UI)
**What:** The CLI EPUB path emits untranslated chapters as English. `stage_split` writes every chapter's English text to `chapters/chapter_*.txt`; `stage_combine` (scripts/translate_book.py:399-424) only overwrites the `.txt` for fully-translated chapters; then `build_epub` (src/epub_builder.py:483) globs *all* `chapter_*.txt`, so any not-fully-translated chapter ships in English. The web UI's `build_epub_route` (web_ui/app.py:5088) already does it right: it selects chapters where `translated == total`, combines only those into a temp dir, and calls `build_epub(chapters_dir=temp_dir)`.
**Why:** Partial translations produce mixed-language EPUBs from the CLI/agent, while the web UI produces translated-only. The translate-harness skill currently works around this with an inline ~30-line temp-dir rebuild snippet in Step 5; fixing the pipeline lets that step collapse back to a one-line `--start-stage combine` (or a plain `build_epub.py` call) and removes the divergence between the two code paths.
**How:** Extract the web UI's "fully-translated chapters → temp dir → build_epub" logic into a shared helper (e.g. `src/epub_builder.build_epub_from_chunks(project_dir, ...)` or a `translated_only` flag), and have `stage_epub`, `scripts/build_epub.py`, and `build_epub_route` all call it. `stage_combine` should also stop leaving stale English `.txt` for untranslated chapters. Then simplify translate-harness SKILL.md Step 5 to call the script directly. Add a regression test with a partially-translated fixture asserting untranslated chapters are excluded.
**Depends on:** nothing.
**Completed:** v0.15.2.0 (2026-06-07)

## P2 — UX rough edges

### Aggregate book-level difficulty metrics from chapter metrics instead of re-running score_text
**What:** `score_book` in `src/difficulty_scorer.py` joins all chapter texts and calls `score_text` on the concatenation, duplicating all sentence-splitting and per-token Zipf lookups already done per-chapter (O(2N) total work). Fix: derive book metrics by weighted aggregation of per-chapter `DifficultyMetrics` (weighted by `word_count`) instead.
**Why:** Performance optimization — deferred from difficulty-scorer pre-landing review because scoring is cached and only runs once per book.
**How:** In `score_book`, after building `chapters`, compute book metrics from `{c.metrics for c in chapters}` weighted by `c.metrics.word_count`; only fall back to `score_text(book_text)` when `chapters` is empty.

### Add TypeError guard for bare-string `chapters` argument in `build_epub_from_chunks`
**What:** `build_epub_from_chunks(chapters="chapter_01")` calls `set("chapter_01")` which produces `{'c','h','a','p','t','e','r','_','0','1'}` — a set of characters. No real chapter ID matches, so the function raises `ValueError('No fully translated chapters found')` instead of a clear `TypeError`. Deferred from pre-landing review on epub-translated-chapters-only (2026-06-07).
**Why:** Confusing error message; the actual mistake (passing a string instead of a list) is invisible.
**How:** Add `if isinstance(chapters, str): raise TypeError("chapters must be a list or set of chapter IDs, not a bare string")` before `set(chapters)` in `src/epub_builder.py`. Add a pytest test in `TestBuildEpubFromChunksEdgeCases`.

### Surface `_attach_text_in_chunk` enrichment failures to the reader client
**What:** `_attach_text_in_chunk` (web_ui/app.py:1019-1086) silently `continue`s when the re-splitter can't map a sentence to a chunk substring (lines 1059-1064, 1078-1080). The client falls back to the normalized aligner text, which then fails the literal `chunk.translated_text.find()` lookup in `/api/sentence/replace` with a false 422.
**Why:** User clicks retranslate on a perfectly good sentence, sees "Cannot locate the original sentence in the chunk." Codex adversarial review.
**How:** Add a `text_in_chunk_status` field ("ok" / "unmapped") to enriched alignment rows. Client disables retranslate for unmapped rows or shows "alignment not anchored — edit the chunk directly."

### Warn before chunk edit overwrites unapplied corrections
**What:** Saving the chunk editor while a chapter has unapplied corrections silently purges them (`_apply_chunk_edits` calls `_purge_chunk_corrections`). The pending-corrections banner in `web_ui/templates/chunk_edit.html:35-39` shows but gives no warning and no way to apply first.
**Why:** Data loss risk; workaround is to apply corrections before opening the editor.
**How:** Update the banner to explicitly warn that saving discards unapplied corrections, and link to the Apply Corrections flow. Optionally require confirmation on Save when `pending_corrections` is true.

### Cache folded text for "Find in book" search
**What:** `search_book` (web_ui/app.py) folds the whole book char-by-char on every request: `_search_alignment_chapter` → `_find_match` → `_fold_with_map` per alignment row, and `_search_source_chapter` → `_fold_with_map` over each untranslated chapter's full text. No mtime-keyed cache despite `load_chapter_source_text` already returning mtime. All work is synchronous in the Flask worker with no result pagination.
**Why:** /ship performance specialist on the reader-concordance branch. Low impact today (single-user localhost) but every query/side-toggle re-reads and re-folds the whole book and ties up the worker; becomes real if multi-user or deployed.
**How:** Cache `(folded, orig_index)` per chapter keyed by (chapter, file mtime). Do a cheap `_fold(haystack).find()` before building the full `orig_index`, and only build the offset map for matching rows. Consider precomputing a folded index at translation time and/or capping results. Shares the cache need with the deferred "Confidence-drift concordance view".

### Add an automated test harness for the reader search JS
**What:** `web_ui/static/concordance.js` (search surface: ES/EN toggle, result rendering, AbortController fetch, focus trap, resume, deep-link nav with `&hl=1&esi=`) and the `scrollToAnchorParam` es_idx logic in `reader.js` have no automated tests. Only the Python endpoint/helpers are covered.
**Why:** /ship testing specialist. The client↔server anchor/es_idx contract and the empty/error/abort branches are only verified by hand.
**How:** Add jsdom + vitest/jest or a Playwright smoke test; cover result rendering, ES/EN toggle, `?anchor=&hl=1&esi=` URL construction, abort-on-toggle, and empty/error states.

### Decide whether source-side search should surface read errors
**What:** `_search_alignment_chapter` lets `json.JSONDecodeError`/`OSError` propagate → 500, but `_search_source_chapter` → `load_chapter_source_text` (src/utils/source_text.py:142,156) catches those internally, logs, and returns partial/empty text. A corrupt chunk in an *untranslated* chapter silently drops as a false "No matches" instead of erroring.
**Why:** /ship red-team review. Asymmetric failure mode; "No matches" can be a lie.
**How:** Either have a search-specific source loader signal read failures so the endpoint can 500 consistently, or document the source side as best-effort/silent. Add a source-side malformed-file test once decided.

### Parameterize translate-harness SKILL.md locale (Spanish/mx hardcoded in snippets)
**What:** Steps 1b, 1c, 1d, and 2 of `.claude/skills/translate-harness/SKILL.md` embed `"Spanish"` and `"mx"` literally in Python heredocs. A non-Spanish book silently generates a style guide and glossary tuned for Spanish.
**Why:** Deferred from adversarial review on feat/translate-harness (2026-06-06). v1 is Spanish-only; parameterization adds 10+ snippet edits and a `.tmp/project_config.json` config layer that felt like over-engineering for v1 scope.
**How:** Store `target_lang` and `locale` in `.tmp/project_config.json` at Step 0 and read from it in every snippet. Add a test asserting a non-Spanish target lang flows through correctly.
**Completed:** v0.17.2.0 (2026-06-10) — the harness CLI refactor replaced the hardcoded heredocs with `src/harness/state.py` config (`projects/<slug>/.harness/config.json`, set via `setup --target-lang/--locale`); `flow.py` reads `cfg["target_language"]`/`cfg["locale"]` for every prompt. Caveat: DEFAULTS still default to Spanish/mx (now overridable, not literal), and a dedicated non-Spanish flow-through assertion is not yet in the test suite.

### Add prompt caching to the translate prompt prefix (from harness-mode eng review 2026-06-05)
**What:** `translate_chunk_realtime` (src/api_translator.py:438) builds the entire prompt as one flat string via `render_prompt(template, variables)` and sends it as a single message. There is no `cache_control` anywhere in `src/` or `scripts/` — the API path re-pays full input tokens on every chunk. The shared portion (system instructions + style guide) is identical across all chunks of a book.
**Why:** Surfaced during the harness-mode eng review (2026-06-05). The harness design's cost argument for choosing the API backend over per-chunk subagents assumed this caching already existed; it does not. Prompt caching is the single highest-leverage cost lever on the existing API path, and it is the prerequisite for an honest Approach-A-vs-B cost comparison (subagents amplify the lost-caching cost 2–4x). Note: the glossary is filtered per-chunk (`filter_glossary_for_chunk`, line 472), so it must stay in the variable suffix, not the cached prefix.
**How:** Split the rendered prompt into a stable prefix (system + style guide) carrying `cache_control: {type: "ephemeral"}` and a variable suffix (per-chunk filtered glossary + source text). Confirm cache hits via `usage.cache_read_input_tokens` on the Anthropic response. This is a prompt-structure refactor of the translate path, not a harness-mode deliverable.

### Parallel subagent translation backend for long books (Harness Phase B follow-on)
**What:** Upgrade the sequential subagent translation backend (Phase B v1) to a parallel
fan-out: the orchestrator spawns a capped pool of workers instead of one-at-a-time, with
backoff and partial-failure collation.
**Why:** Sequential per-chunk spawning is fine for the chapter-batch workflow v1 targets, but a
full long book is slow. This is the deferred speed path (eng review 2026-06-10, decision D1 chose
sequential to shrink the riskiest surface first).
**How:** Keep the same `translate-prepare` / `translate-commit` seam; replace the one-at-a-time
spawn loop in `SKILL.md` with a concurrency-capped pool. Continuity must come from the
pre-computed `context_map` (previous-chunk source tail) since parallel workers lose ordering.
Add rate-limit backoff for subscription fair-use caps.
**Depends on:** Phase B v1 (sequential) shipped + a real long-book translation proving latency hurts.

## P3 — Documented but not currently impactful

### Reader realign races an in-flight bottom-sheet save
**What:** `lockChunkMutators(true)` in `reader.js` disables `btn-save` when a realign is triggered, but a save that was *already in flight* when the realign button was clicked is not aborted. The save's own `.finally()` re-enables `btn-save` independently. If the save completes after the align reads `corrections.jsonl` (before the align's `_apply_pending_corrections_for_chapter` call), the new correction is applied correctly. But if the save writes to `corrections.jsonl` after `_apply_pending_corrections_for_chapter` has already read and consumed the file, the correction goes unprocessed: the realign generates a fresh alignment without it, yet the file was cleared during the realign. The user's edit silently vanishes.
**Why:** Requires precise timing (save in-flight at the same moment align fires); surfaced by adversarial review on v0.10.3.0.
**How:** Track a `saving` boolean in `reader.js` and disable the realign button while a save request is outstanding. Or: on the server side, re-read `corrections.jsonl` immediately before the aligner call (after `_apply_pending_corrections_for_chapter`) and apply any newly written rows.

### Crash window between archive write and `corrections.jsonl` rewrite in `_apply_pending_corrections_for_chapter`
**What:** `_apply_pending_corrections_for_chapter` appends to `corrections_applied.jsonl` then rewrites (or deletes) `corrections.jsonl`. A server crash between those two steps leaves `corrections.jsonl` intact, so the next realign re-reads the same rows. `apply_to_chunk` will find `original_es` already replaced and return `applied=0`, archiving the rows again as `skipped` — no double-application, but the archive gets duplicate entries.
**Why:** Low real-world risk (file write + unlink is fast, and `apply_to_chunk` is idempotent in the skip path). Flagged by adversarial review on v0.10.3.0.
**How:** Rewrite `corrections.jsonl` first (or to a `.tmp` then rename), then append to the archive — reversing the order makes a crash leave the corrections file already cleared (re-run would be a no-op) at the cost of a narrow window where the archive is not yet updated.

### Unique temp file names in `save_chunk()` to prevent concurrent-write collision
**What:** `save_chunk()` always writes to `<output_path>.tmp`. Two concurrent `/api/sentence/replace` requests to the same chunk race to write `chapter_01_chunk_000.tmp`; one overwrites the other's temp file before the atomic rename, silently dropping an edit.
**Why:** `tempfile.NamedTemporaryFile(dir=..., delete=False)` gives a unique name per call and is a one-line fix in `save_chunk`. The mtime check is a TOCTOU and doesn't protect against this. Flagged by adversarial review on the chunk-offset fix.
**How:** Replace `output_path.with_suffix('.tmp')` with a unique temp file per call; rename atomically. No API change.

### Log a warning when audit log write fails in `sentence_replace`
**What:** `retranslations.jsonl` write in `sentence_replace` (web_ui/app.py ~2370) has `except OSError: pass`. If the log becomes unwritable (disk full, permissions), replacements succeed but all audit records are silently lost.
**Why:** Forensic debugging was the explicit motivation for recording `chunk_offset_start`/`chunk_offset_end`. Silent loss defeats that. A `app.logger.warning(...)` call is a one-liner.
**How:** Replace `pass` with `app.logger.warning("retranslations.jsonl write failed: %s", e)`.

### Lost-update race on style-guide writes
**What:** `/api/setup/<id>/style-guide` (web_ui/app.py:355) and `/api/setup/<id>/style-guide/light` (web_ui/app.py:391) both load full `style.json` and rewrite it. Concurrent writes can drop one field. `_load_style_guide_content` returns "" on read failure, so retranslation silently proceeds with no style guide.
**Why:** Codex adversarial review. Solo local Flask = essentially never happens.
**How:** When it matters: file lock on load+save, or move `light_content` to `style_light.json`.

### Lost-update race on `translator_note.json` writes
**What:** `POST /api/project/<id>/translator-note` (web_ui/app.py:4475) loads `translator_note.json`, mutates `{heading, body}`, and rewrites the full file. Same shape as the style.json race.
**Why:** Eng review of the Translator Note plan, 2026-04-29.
**How:** Generalize: write a single `_load_save_with_lock(project_id, filename, mutator)` helper and route `glossary.json`, `style.json`, `translator_note.json`, `project.json` writes through it.

### Handle `load_chunk()` failure gracefully inside `build_epub_from_chunks`
**What:** `build_epub_from_chunks` calls `[load_chunk(path) for path in chunk_paths]` with no exception handling. If any chunk file disappears or is corrupt between the glob and the load (race condition, partial flush), the exception propagates raw and that chapter ends up in neither `included` nor `skipped`. Deferred from red-team review on epub-translated-chapters-only (2026-06-07).
**Why:** Theoretical for single-user local tool, but a corrupt chunk file produces a cryptic unhandled exception instead of a clean skip+warning.
**How:** Wrap the list comprehension in try/except; on `OSError` or `json.JSONDecodeError`, append the chapter_id to `skipped`, log a warning, and continue.

### Cross-check filename-derived `chapter_id` against JSON `chapter_id` field in `_discover_chunk_chapters`
**What:** `_discover_chunk_chapters` derives `chapter_id` purely from the filename (via `_CHUNK_FILE_RE`), discarding the authoritative `chapter_id` inside the JSON payload. A manually renamed chunk file would silently produce an EPUB with content filed under the wrong chapter ID. Deferred from red-team review on epub-translated-chapters-only (2026-06-07).
**Why:** Defensive correctness; the previous web UI implementation read `cdata.get("chapter_id")` from the JSON payload.
**How:** After loading each chunk for `has_translation` check in `build_epub_from_chunks`, verify `chunk.chapter_id == filename_chapter_id`. Log a warning and append to `skipped` if they diverge.

### Fix overlap duplication in `align_chapter_chunks`
**What:** `align_chapter_chunks` (src/sentence_aligner.py:459-525) processes each chunk's full text including overlap regions, producing duplicate sentences in the alignment file.
**Why:** Caught by outside voice during eng review. Overlap is disabled for new translations as a workaround.
**How:** Chunking strategy decision. Could move to sentence-based chunk boundaries, or align from combined chapter text via `combine_chunks()` instead of per-chunk when overlap is on.

### Consolidate Stage 8 export options into `export_extras.json`
**What:** Trip-wire — when a 3rd Stage 8 export option lands, refactor the per-feature JSON files (currently `translator_note.json`) into a single `projects/<id>/export_extras.json` with one route pair.
**Why:** Keeps the API surface small and avoids inventing a new route per export-time field.
**How:** Single `GET/POST /api/project/<id>/export-extras` returning `{translator_note: {...}, future_field: {...}}`. Prefer the new file, write only the new file. Drop the legacy route after one cycle.

### Concordance maintainability nits (from /ship review 2026-06-03)
**What:** Small cleanups on the reader-concordance code: (1) `_find_match` and `_search_source_chapter` duplicate the fold + offset-map arithmetic — extract a shared `_iter_matches(haystack, folded_q)` helper. (2) The anchor prefix length `80` is duplicated as a bare literal in `concordance.js` (`es.slice(0, 80)` for the resume anchor) vs `_SEARCH_ANCHOR_LEN = 80` server-side — emit it from the template or a shared constant. (3) The `1800`ms flash-removal timeout in `reader.js` is coupled to the `1.8s` CSS animation — drive removal off `animationend` or a shared duration. (4) `data-side` attributes on the ES/EN toggle buttons are unused — remove or read via `setSide(btn.dataset.side)`.
**Why:** /ship maintainability specialist. None are bugs; they are drift risks.
**How:** As above; all mechanical, no behavior change.

### `search_queries.jsonl` append is not atomic across worker processes
**What:** `_log_search_query` opens in `"a"` mode and writes one JSON line; under a multi-process WSGI server concurrent searches can interleave writes for long unicode lines and tear a record. The `OSError`-only `except` won't catch it, and a later reader hits `JSONDecodeError`.
**Why:** /ship red-team. Won't occur on the current single-worker localhost setup; matters only if deployed multi-process.
**How:** Use a lock or accept-and-document the log as lossy/best-effort so any reader tolerates bad lines.

## Low — nice-to-have

### Consolidate `_discover_chunk_chapters` and `discover_chapters` into a shared utility
**What:** `src/epub_builder._discover_chunk_chapters` and `scripts/translate_book.discover_chapters` are near-identical: both glob `*_chunk_*.json`, apply the same regex, sort paths, and return `Dict[str, List[Path]]`. Deferred from maintainability specialist review on epub-translated-chapters-only (2026-06-07).
**Why:** Drift risk — a bug fix to one function won't propagate to the other.
**How:** Extract to `src/utils/chunk_discovery.py` with a single `discover_chunk_chapters(chunks_dir: Path) -> Dict[str, List[Path]]` function. Import it in both callers.

### Consolidate the two `[IMAGE:...]` token regexes
**What:** `src/utils/text_utils.py:21` `_IMAGE_PLACEHOLDER_RE` and `web_ui/app.py:3637`
`_IMAGE_TOKEN_RE` are two regexes for the same `[IMAGE:filename(:desc)?]` token.
**Why:** Drift risk — a change to the token format (or a fix to one) won't reach the other. Surfaced
by the harness Phase B eng review (2026-06-10): the new `guard_translation_draft` reuses the
`text_utils` canonical one, leaving the `web_ui` copy as the odd duplicate.
**How:** Make `text_utils._IMAGE_PLACEHOLDER_RE` the single source and import it in `web_ui/app.py`;
delete `_IMAGE_TOKEN_RE`. Confirm the retranslate-path token check still behaves identically.
**Depends on:** nothing.

### Model comparison in dry-run
**What:** When running cost/dry estimation, translate the same chunk with 2-3 models side by side to compare quality + cost before committing.
**Why:** Currently trusting Sonnet as default; no easy comparison data.
**How:** Add `--compare-models` to `scripts/translate_book.py` (alongside `--cost-only` at line 531). Translate one chunk per model, display side-by-side with cost.

### Migrate `pipeline_state.json` to pydantic `ProjectState`
**What:** Replace the raw dict in `scripts/translate_book.py:55-69` with the existing `ProjectState` model from `src/models.py`.
**Why:** No schema enforcement; fields added ad hoc. Type safety + one source of truth.
**How:** Update `load_pipeline_state` / `save_pipeline_state` to use the pydantic model. Update stage functions to set typed fields.

### Style wizard source-text selection flexibility
**What:** The style wizard reads the first 3 chapters capped at 10K words. Allow `--chapters` and `--max-words` overrides on `scripts/generate_style_guide.py`. Consider token-based cap.
**Why:** 10K works for Fabre (~2500 words/chapter) but may be too little for long chapters or too much for short books.
**How:** Add flags, optionally a token cap matching LLM context.

## Reader Concordance — v2 (deferred from eng review 2026-06-03)

### Confidence-drift concordance view
**What:** Extend the reader concordance ("find in book") to sort results by lowest `confidence` and show a per-chapter frequency sparkline — a "where did the translation drift" detector.
**Why:** `confidence`/`similarity` are already on every alignment row (free signal). Catches a word that "was fine for 8 chapters then went peninsular in chapter 9." This is Approach B in the design doc (`~/.gstack/projects/nathaniel-sheetz-translate_books/Nathaniel-main-design-20260603-113634.md`).
**How:** Needs a cached per-book index → cache-invalidation on every chunk edit (the complexity deliberately deferred from v1). Gate on: v1 concordance shipped + the query-log review below justifying the investment.
**Depends on:** v1 concordance shipped.

### Lemmatizer decision checkpoint
**What:** After ~1-2 weeks of dogfooding the v1 concordance, read `projects/<book>/search_queries.jsonl` and decide: are audits noun/proper-name/dialect-dominant (substring is enough) or verb-dominant (need lemmatization)?
**Why:** This is the data-driven morphology decision the query log exists to inform. Easy to forget once v1 feels fine, which would orphan the log.
**How:** Tally query fragments by part-of-speech intent from the log. Verb-dominant → add a lemmatizer pass; else keep folded substring. Evidence base = the query log itself.
**Depends on:** v1 concordance shipped + query log accumulating.

### Live-debounced concordance search (deferred from design review 2026-06-03)
**What:** Make the "find in book" search update as you type (~250ms debounce) instead of only on Enter.
**Why:** Helps home in on the right fragment — type `vos` and watch vosotros forms appear live. v1 ships Enter-to-submit (DR4) for a deliberate, cheaper model.
**How:** Debounce keystrokes; re-run the scan on pause and on toggle change; guard against list churn. Cheap once Enter-to-search proves the flow.
**Depends on:** v1 concordance shipped.

### Concordance screen-reader depth (deferred from design review 2026-06-03)
**What:** Add `aria-live` announcements (result count + loading/empty/error states) and per-row accessible names to the search surface.
**Why:** v1 ships core a11y (focus-on-open, Esc, 44px targets, contrast) but defers SR niceties (DR8) — fine for a single-user tool, needed if ever navigated by screen reader.
**How:** `aria-live="polite"` region for "Searching…/N results in M chapters/No matches"; navigable rows get accessible names like "Chapter 3: …no le hablo… — go to sentence"; KWIC rows labeled context-only.
**Depends on:** v1 concordance shipped.

### Write a short DESIGN.md (from design review 2026-06-03)
**What:** One-page design-system doc — CSS-variable tokens, type (serif content / sans chrome), the bottom-sheet + full-screen-surface idioms, spacing, 768/480 breakpoints.
**Why:** Future design reviews and any AI mockups could align to it instead of reverse-engineering `reader.css` each time. Low priority — `reader.css` is the de-facto source of truth today.
**How:** Extract the system from `web_ui/static/reader.css`. Keep it short or it drifts from the CSS.
**Depends on:** nothing.

## Recently done

- EPUB now built from translated chapters only across CLI, pipeline, and web UI: `build_epub_from_chunks` shared helper in `src/epub_builder.py`; partial translations no longer produce mixed-language EPUBs. **Completed:** v0.15.2.0 (2026-06-07)
- Realign-chapter button added to reader: clicking it applies queued bottom-sheet corrections to chunks, then regenerates the chapter alignment in place. Corrections are archived with `status: applied | skipped`; empty-chunk_id rows, missing-chunk rows, and stale-text rows are archived as skipped rather than silently lost. Button is hidden when no corrections are pending for the current chapter (v0.10.3.0).
- Paragraph breaks at chunk boundaries now preserved: `combine_chunks()` normalizes each inter-chunk boundary to exactly one blank line (`rstrip`/`lstrip`) and skips the separator when overlap removal empties a chunk; `align_chapter_chunks()` marks the first alignment of every non-first chunk as `para_start=True` so cross-chunk paragraph boundaries are correctly detected. Affected chapters need to be recombined and re-aligned (v0.10.2.0).
- Sentence replace anchored via chunk offsets: three-tier span resolution in `/api/sentence/replace` prevents caption corruption when body text matches image caption text (v0.10.1.0).
- Client clears stale chunk offsets when user edits `retransCurrent` before applying, preventing Tier 2 partial-match on an edited substring (v0.10.1.0).
- Inline-edit corrections (`/api/correction` → `corrections.jsonl` → `apply_to_chunk`) use the same three-tier offset resolution: reader sends `chunk_offset_start`/`chunk_offset_end`, `save_correction` persists them, and `apply_to_chunk` applies corrections in descending-offset order to prevent offset drift (v0.10.1.0).
- Project/chapter browser for reader (`/read/` and `/read/<project_id>` routes).
- Show images in reader (served from `projects/<id>/images/`).
- Sentence-level annotations in reader (`annotations.jsonl`, 4 types, migration script).
- Correction save failure handling (localStorage retry queue).
- Full translation dashboard at `/project/<id>` with 7-stage stepper and batch-API SSE progress.
- `test_combiner.py::test_unsorted_chunks` — was listed as a P0 broken test; it passes against current `src/combiner.py` (verified 2026-05-18). No fix needed.
- `corrections.jsonl` lockout when duplicate corrections can't all apply — dedupe + idempotent apply fix. **Completed:** v0.13.1.0 (2026-05-29)
