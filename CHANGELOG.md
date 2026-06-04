# Changelog

All notable changes to this project will be documented in this file.

## [0.14.0.0] - 2026-06-03

### Added
- **Find in book (reader concordance)** — while reading, tap the search icon to search the whole book for any fragment and see every occurrence as a source + translation pair, so you can check whether a word, dialect tell, or grammatical pattern (like *vosotros* forms) was rendered consistently. Search runs against the Spanish translation or the English source via a toggle.
- **Accent- and case-insensitive matching** — `habló`, `hablo`, and `HABLO` all match the same occurrences, so accents never split or hide a result. Partial-word fragments work too, letting you sweep morphology patterns yourself.
- **Jump to the exact sentence** — tapping a translated result lands you on that sentence in the reader and briefly flashes it; returning to the search restores your full result list and scroll position, and a "Resume reading" control takes you back to where you were reading.
- **Untranslated-source coverage** — fragments found in chapters that aren't translated yet appear as read-only snippets clearly marked "not translated," so you still see every hit across the book.
- **Query logging** — each search is recorded to `search_queries.jsonl` to inform a later decision on whether lemmatized search is worth adding.

## [0.13.1.0] - 2026-05-29

### Fixed
- **Corrections lockout bug** — double-clicking Save (or any UI glitch that queued the same correction twice) caused `corrections.jsonl` to never be unlinked after apply. The first pass would mutate the chunk, leaving the duplicate's `original_es` unfindable; `total_applied` never reached `len(corrections)`, so the pending-corrections banner stuck forever. Fixed by deduplicating corrections before applying (keeping the newest by timestamp) and treating already-applied corrections as idempotent successes.

## [0.13.0.0] - 2026-05-27

### Added
- **Edit-review report** — `scripts/review_edits.py` generates an HTML side-by-side diff report comparing each translated chunk's current text against the raw LLM baseline that produced it. Hunks are highlighted at the word level, source pane shows a proportionally-mapped context window, and chunks with no recoverable baseline are flagged separately.
- **Edit-tag UI** — the report includes tag buttons so you can label each diff hunk (glossary conflict, missing paragraph break, dialogue punctuation, etc.) and persist tags to `edit_review_tags.jsonl` for later analysis.
- **Baseline provenance stamp (`last_llm_log`)** — every translated chunk now carries a pointer to its submission-time prompt log. Realtime and batch paths both set the stamp; the stamp is preserved across user edits.
- **New API endpoints** — `GET /api/edit-tags` returns the tag vocabulary; `POST /api/project/<id>/edit-tag` persists a tag for a hunk; `GET /reports/<project_id>/<filename>` serves generated reports same-origin with the tag API.
- **Shared edit-review constants** — `src/edit_review_constants.py` holds the `EDIT_TAGS` vocabulary, imported by both the report generator and the web UI to keep them in sync.
- **`chunk_log_map` in batch submission** — `submit_batch` now returns a per-chunk map of submission log paths, letting batch retrieval mutate the exact log file in place rather than writing a separate response log.

### Changed
- Batch submission log tracking renamed from `prompt_map` to `chunk_log_map` across `compare_models.py` and `api_translator.py`.
- `submit_translation_job`, `_translate_via_batch`, and `_translate_via_realtime` all accept `project_slug` so provenance stamps are populated for every translation path.
- `POST /api/edit-tag` moved to `/api/project/<project_id>/edit-tag` to match the existing project-scoped endpoint convention.

### Fixed
- Fallback batch log scan now sorts by filename (newest-first) so the correct log is mutated when multiple null-response logs exist for the same chunk+job.
- `_load_tags` no longer crashes on non-integer `hunk_index` values in the JSONL file.
- Test isolation: `_LAST_LOG_PATH` ContextVar is now reset between tests to prevent cross-test leakage.
- `_attach_batch_response` uses the return value of `log_prompt()` directly instead of the ContextVar side-channel, eliminating a potential race condition under concurrent batch retrieval.

## [0.12.0.0] - 2026-05-21

### Added
- **Front/back matter label editor on dashboard** — front-matter and back-matter chapters now show a text input in the project split view so you can set the heading that appears in the reader and EPUB (e.g. "A los niños" instead of the raw filename). Empty or whitespace-only saves clear the override and fall back to the existing heading. Numbered chapters remain read-only.
- **`PATCH /api/project/<id>/chapter-manifest/<chapter_id>`** — new endpoint persists a custom `label` for front/back matter manifest entries. Returns 400 if the target is a numbered chapter.
- **`kind` and `label` fields in chapter status response** — `/api/project/<id>/status` now includes `kind` (`chapter`, `front_matter`, `back_matter`) and `label` (custom heading if set) for each chapter, enabling the dashboard to render front/back matter distinctly.

## [0.11.1.0] - 2026-05-21

### Fixed
- **Glossary: I-contractions no longer surface as candidates** — `i'll`, `i'd`, `i'm`, `i've` were rejected by the spell-checker in lowercase but accepted as `I'll` etc. The checker now tries the capitalized form so first-person contractions are correctly filtered from candidate output.
- **Glossary: greetings filtered from dialogue** — `hello`, `hi`, `hey`, `goodbye`, `bye`, `okay`, and `ok` are added to stopwords and sequence-breakers so they no longer appear as "always-capitalized" candidates in dialogue-heavy books.
- **Glossary: dialect possessives no longer collapse to stopwords** — tokens like `so's` (Vermont dialect "so as") were stripped to `so` by the possessive collapser, producing a stopword-keyed candidate. They are now dropped instead. Character names that happen to be stopwords (e.g. `May`, `Will`) are correctly exempted from this filter when they are already confirmed proper nouns.
- **Glossary: protagonist names that often begin sentences are now detected** — sentence-initial capitalized tokens previously counted only toward total occurrences, pushing the capitalized-ratio below the 80% threshold and hiding names like `Betsy`. Both counts are now incremented so protagonist names are reliably surfaced.
- **Glossary: multi-word proper noun n-grams filtered** — n-grams of the form `[function word] + [multi-word name]` (e.g. `like Cousin Ann`) or `[multi-word name] + [common noun]` (e.g. `Aunt Abigail's face`) were not being filtered by the existing single-token name guard. The guard is generalized to match any known proper noun key (single or multi-word) as the name span.
- **Glossary context: plural suffix skipped for title-case s-ending terms** — `_term_pattern("Atlas")` previously generated a regex that matched `atlases` (a different word). Terms that are title-case and already end in `s` now use an exact match; lowercase terms like `dress` still match `dresses` correctly.

## [0.11.0.0] - 2026-05-21

### Added
- **Forced glossary candidates**: create a `forced_glossary_terms.json` file (copy from the new `forced_glossary_terms.example.json`) to list domain words that should always appear in the glossary candidate report when they occur in the source text. Useful for consistently-mistranslated words (e.g. "stall" as *establo* vs *casilla*, "gobbler" as *pavo macho* vs *guajolote*) that extraction heuristics would otherwise bury. Terms match case-insensitively, whole-word, with basic singular/plural handling for single-word entries. The feature bypasses min-frequency, demonym, and contained-term filters while still respecting the existing-glossary exclusion list.

## [0.10.3.0] - 2026-05-20

### Added
- **Realign button in reader**: the reader topbar now shows a realign icon button whenever the current chapter has unsaved pending corrections. After saving a correction via the bottom sheet the button appears automatically; clicking it triggers a full chapter realign in place, preserving scroll position and showing a toast on completion.

### Fixed
- **Pending corrections applied before realign**: running *Realign* from the reader now patches chunk files with any queued bottom-sheet corrections before regenerating the alignment. Previously, chunks held the original text, so realign would silently overwrite the user's edits with a freshly generated alignment. Applied corrections are moved to `corrections_applied.jsonl` with a `status` field (`applied` or `skipped`); rows that could not be applied (missing chunk, stale text, load error) are archived as `skipped` rather than silently stamped as applied. Rows targeting other chapters are preserved in the queue.
- **Empty `chunk_id` corrections no longer re-queue forever**: a correction record with an empty `chunk_id` was previously routed back to `corrections.jsonl` on every realign, keeping the realign button visible indefinitely. Such rows are now archived as `skipped`.
- **`apply_to_chunk` exceptions handled per-chunk**: a malformed correction record (e.g. missing `original_es` key) previously propagated an unhandled exception through the align route. The error is now caught and logged per-chunk so a single bad row does not abort the entire realign.

## [0.10.2.0] - 2026-05-19

### Fixed
- **Paragraph break preserved at chunk boundaries**: when a chapter was split into multiple chunks on a paragraph boundary (the chunker's only split rule), the inter-chunk `\n\n` lived *between* the chunks and was never stored on either side. `combine_chunks()` then concatenated chunks with `chapter_text += non_overlap_text`, fusing chunk N's last paragraph with chunk N+1's first in the combined `chapters/<id>.txt`. The reader (which splits the file on `\n\s*\n` to derive paragraph events) and the EPUB renderer (same split) both emitted them as one paragraph. With the project default `overlap_paragraphs=0` this hit every multi-chunk chapter — e.g. `among-the-farmyard-people/chapter_04` joined "…impaciencia." to "Día tras día…". `combine_chunks` now normalizes each boundary to exactly one blank line using `rstrip("\r\n") + "\n\n" + lstrip("\r\n")`, and skips the separator entirely when overlap removal consumes the whole chunk (empty non-overlap). The companion fix in `align_chapter_chunks` flags the first alignment of every non-first chunk as `para_start` (per-chunk `align_chunk` only flags within-chunk paragraph crossings, so the cross-chunk boundary was never marked). Affected chapters need to be recombined and re-aligned.

## [0.10.1.0] - 2026-05-19

### Fixed
- **Sentence replace uses chunk offsets to prevent silent corruption**: `/api/sentence/replace` now accepts `chunk_offset_start`/`chunk_offset_end` from the reader and uses them to locate the exact span to overwrite. When a sentence appears more than once in a chunk (e.g. body text that also appears verbatim inside an `[IMAGE:...]` caption), the previous `str.find()` always replaced the first occurrence, silently corrupting whichever copy came first. The fix uses a three-tier resolution: (1) if the supplied offsets slice back to `current_translation`, that exact span is replaced; (2) if the offsets are stale (user edited the field), an anchored `find()` searches forward from the hint; (3) old clients with no offsets fall back to the original `find()` from position 0. The audit log now records the resolved `chunk_offset_start`/`chunk_offset_end` for forensic debugging.
- **Stale offsets cleared when user edits the current-translation field**: if the reader sends offsets pointing to the original span but the user has edited the "current translation" textbox before applying, the client now omits the offsets entirely so the server falls back to Tier 3 plain-find — preventing a Tier 2 partial-match replacing the wrong fragment.
- **Inline-edit corrections use chunk offsets too**: the bottom-sheet "edit this line" flow (`/api/correction` → `corrections.jsonl` → `apply_to_chunk`) was a separate code path that the sentence-replace fix did not cover. `scripts/apply_corrections.py:apply_to_chunk` still used naive `text.replace(original, corrected, 1)`, which always hit the first match — so a body sentence whose text also appeared in an earlier `[IMAGE:...]` caption or quoted version would corrupt the twin instead of the user's target. The reader now sends `chunk_offset_start`/`chunk_offset_end` on `/api/correction`, `save_correction` persists them into the queued record, and `apply_to_chunk` uses the same three-tier resolution as sentence_replace. Multiple corrections to the same chunk are applied in descending-offset order so each correction's offsets remain valid as text shifts.

## [0.10.0.0] - 2026-05-18

### Added
- **Dublin Core EPUB metadata**: Export tab now has fields for Translator, Original Title, Publisher, Description, and Rights. Values are saved to `project.json` and written into the EPUB's OPF as `dc:contributor` (with MARC relator role `trl`), `dc:source`, `dc:publisher`, `dc:description`, and `dc:rights`.
- **Cover image thumbnail**: Export tab shows a live preview of the cover art from `images/cover.jpg/.jpeg/.png`, matching the image that `build_epub` auto-picks at build time.
- **Auto-align after realtime batch translation**: translating chunks now automatically combines each affected chapter and writes its alignment file. The Review tab is ready without a manual "Align" click; a `chapter_aligned` SSE event signals the dashboard.
- **Chunking config persistence**: the Stage 3 form pre-fills with the parameters from the last successful chunk run, stored in `project.json` under `chunking_config`.
- **Style guide inline edit**: the Style tab now has separate Edit and Rebuild buttons. Edit opens an inline textarea for direct text changes; Rebuild re-runs the Q&A wizard.
- **Glossary rare-word sensitivity label**: slider renamed to "Rare-word sensitivity:" with a clarified tooltip and a hint paragraph explaining that the slider affects candidate detection only, not existing entries.

### Fixed
- **Dashboard annotation counts**: the Review tab now uses the same dedup and tombstone logic as the reader, so superseded edits and removed annotations are not double-counted.
- **N+1 annotation file reads**: `_get_project_status` previously read `annotations.jsonl` once per chapter; replaced with a single-pass bulk read.
- **EPUB `file-as` meta element**: removed spurious `scheme="marc:relators"` attribute from the translator's `file-as` metadata element (the `role` element uses the scheme; `file-as` does not).
- **Chunk All button placement**: moved to the top-right of the Chunk panel header alongside the status indicator.

## [0.9.2.0] - 2026-05-17

### Added
- **Chapter subtitle extraction**: the `chapter_roman_titled` split pattern now captures inline subtitles from roman-numeral headings (e.g. `CHAPTER I EARLY BOYHOOD`). The subtitle is stored separately and written as a second line in the chapter heading, flowing into `<h2>` in the exported EPUB.
- **Broader all-caps heading detection**: `allcaps_heading` character class now allows `.` and `&`, enabling headings like `ST. LOUIS` and `PEACE & WAR` that were previously missed.

### Fixed
- **EPUB italic rendering in verse lines and subtitles**: `_EM_RE.sub()` calls used a double-escaped backreference (`r"<em>\\1</em>"` → literal `\1`). Fixed to `r"<em>\1</em>"` so underscore-marked italics are correctly promoted to `<em>` in verse and subtitle blocks.

### Changed
- **Resplit confirmation dialog**: the UI now checks whether a saved split exists before prompting. If one does, the dialog warns that chapter files and any per-chapter source edits will be replaced.

## [0.9.1.0] - 2026-05-17

### Added
- **Italic preservation end-to-end**: `<i>` and `<em>` tags in the source HTML are now carried through the full pipeline and rendered as `<em>` in the final EPUB.
  - **Ingest** (`scripts/ingest_gutenberg.py`): `<i>` and `<em>` elements are converted to `_word_` underscore markers during HTML-to-text conversion. Adjacent italic tags and `<br>`-separated italic content are handled correctly.
  - **Translation prompt** (`prompts/translation.txt`): the LLM is instructed to preserve underscore markers in the translation, wrapping the translated word(s) in the same `_..._` syntax.
  - **EPUB builder** (`src/epub_builder.py`): underscore markers are promoted to `<em>` in paragraphs, verse lines, and chapter subtitles. Snake_case and URL-style underscores are guarded against via word-boundary lookarounds.

## [0.9.0.0] - 2026-05-17

### Added
- **Verse/poetry line-break preservation end-to-end**: the pipeline now detects stanza blocks and preserves every verse line through translation, alignment, EPUB rendering, and the web reader.
  - `src/utils/verse.py`: new `is_verse_block(block)` heuristic — a block with ≥2 non-empty lines, average line length ≤65 chars, and at least one line of 2–12 words is treated as verse.
  - **EPUB builder**: verse blocks render as `<div class="verse"><p class="verse-line">…</p></div>` with a hanging-indent style (`text-indent: -2em; padding-left: 2em`). Image placeholders embedded within a verse-shaped block are handled correctly (not rendered as literal text).
  - **Sentence aligner**: `_split_sentences_with_para_indices` now splits verse paragraphs on `\n` before pysbd, producing one sentence record per verse line. Verse lines are not passed through pysbd to preserve the 1-record-per-line invariant. `align_chunk` uses this path for both source and target.
  - **Web reader**: `_enrich_alignment` tags alignment records with `verse_line_break=True` for non-first lines of each stanza; `reader.js` renders `<span class="verse-break">` (zero-height block) to produce a visible line break within a stanza without a full paragraph gap.
  - **Translation prompt** (`prompts/translation.txt`, `prompts/translation.example.txt`): added explicit verse line-break preservation instruction under STRUCTURE PRESERVATION.

### Fixed
- EPUB paragraph splitting now uses `\n\s*\n` (matching the reader's regex) instead of `\n{2,}`, so a stanza separator with a stray trailing space is not silently merged with the next stanza.

## [0.8.0.0] - 2026-05-16

### Added
- **Literary-frequency baseline for glossary extraction**: `FrequencyChecker` builds a Zipf-score corpus from NLTK Brown fiction + Gutenberg on first run (cached to `~/.cache/translate_books/literary_freqdist.pkl`) with `wordfreq` as fallback. Gives the extractor a literary-English frequency reference so common English words aren't surfaced as candidates.
- **Rare literary word extractor**: new `extract_rare_literary_words()` pulls out dictionary words whose literary Zipf score falls below a configurable threshold — archaic, domain-specific, or stylistically distinctive vocabulary the translator needs to handle consistently.
- **Span-based tokenization with abbreviation-aware sentence splitting**: `tokenize_with_spans()` returns `(text, start, end)` triples; sentence boundaries now survive `Mr.`, `Lord St. Vincent`, and similar abbreviations via `_ends_with_abbreviation()` / `_rejoin_abbreviation_splits()`.
- **Noise filters and post-merge passes**: `prune_contained_terms()` drops candidates whose every occurrence sits inside a longer candidate (leftmost-longest overlap resolution); `collapse_possessive_keys()` merges `Nelson's` → `Nelson` summing frequencies; `filter_demonyms()` removes nationality words and demonym-led phrases.
- **Word-mode bootstrap prompt**: `build_glossary_prompt()` now supports `context_mode="word"`. In word mode, each candidate is annotated with 1–2 short in-text word-window fragments (via `find_first_word_contexts()`) and candidates are sorted by first appearance, giving the LLM richer context without a bulk source dump.
- **`src/utils/glossary_context.py`**: shared helpers `find_first_contexts()`, `find_first_word_contexts()`, and `precompute_chapter_tokens()` for locating term occurrences across chapter texts with efficient pre-computed tokenization.
- **`src/utils/source_text.py`**: `load_clean_source_text()` and `load_chapter_source_text()` — project-aware source loaders that prefer `chunks/` (guaranteed source-language even after Stage-6 combine overwrites `chapters/`) with overlap stripping, falling back to `chapters/` then raw `source.txt`.
- **Zipf sensitivity slider and word-mode select in the web UI**: `/api/setup/<id>/extract-candidates` now accepts `zipf_offset` (±1.0) to shift both Zipf thresholds simultaneously; `/api/setup/<id>/prompts/glossary` accepts `context_mode` (`"full-text"` or `"word"`).
- **`prompts/glossary_bootstrap_word.example.txt`**: example template for the word-mode bootstrap prompt.

### Changed
- Glossary bootstrap prompt key for the LLM's translated output changed from `"spanish"` to `"translation"` (language-agnostic); parser falls back to `"spanish"` for backward compatibility with existing LLM responses.
- `FrequencyChecker.available` now reflects an actually-loaded backend (NLTK corpus or `wordfreq`) rather than mere importability; the 2.0 sentinel path is not treated as a real frequency source.
- Word-mode context generation pre-computes normalized text and token spans once per request instead of once per candidate.
- Candidates list in word-mode glossary prompt endpoint capped at 1000 items.
- Source text loaded from `chunks/` now strips leading overlap characters from non-first chunks, preventing duplicate passages from inflating extraction frequencies.
- Literary frequency cache write failures are non-fatal (logged as warning, extraction continues in-memory).

### Fixed
- `_read_source_text()` now prefers `chunks/source_text` over `chapters/` to survive Stage-6 translate which overwrites chapter files.

## [0.7.0.0] - 2026-05-14

### Added
- **Conditional image-placeholder instruction in translation prompts**: the STRUCTURE PRESERVATION section of the translation prompt now includes a context-sensitive bullet for `[IMAGE:...]` placeholders only when the source chunk actually contains them. Chunks with no images receive an empty string (no stray instruction). Chunks with filename-only placeholders (`[IMAGE:filename.ext]`) receive the copy-as-is wording; chunks with description placeholders (`[IMAGE:filename.ext:description]`) receive the translate-description wording. The logic is centralised in `src.utils.text_utils.image_placeholder_instruction`.
- **Image placeholder utilities in `text_utils`**: new shared functions `strip_image_placeholders` (equal-length whitespace substitution, preserving character offsets), `image_placeholder_ranges` (returns `(start, end)` spans for filtering tool matches), and `image_placeholder_instruction` (builds the prompt bullet). All callers — `translator.py`, `api_translator.py` (four call sites), `compare_models.py`, and `web_ui/app.py` — now pass `image_placeholder_instructions` when rendering the translation prompt template.
- **In-UI glossary editing (Stage 5)**: the "Existing glossary loaded (N terms)" banner now has an **Edit** button that loads `glossary.json` into the same proposals table used for LLM bootstrap. Each row's `english`, `spanish`, `type`, and `context` fields are editable inline; the `type` column is now a `<select>` over the five `GlossaryTermType` values. A **+ Add row** button appends a blank row that's saved alongside the rest. The existing **Drop** toggle doubles as delete-on-save: dropped rows are excluded from the submitted list. Save in edit mode uses replace semantics; the table contents become the authoritative glossary. Future translation prompts re-read `glossary.json` per chunk, so changes apply on the next translate or retranslate without any cache flush. The `alternatives` list on each term is round-tripped through a `data-alternatives` attribute and preserved on save even though the UI doesn't display it.

### Changed
- `POST /api/setup/<id>/glossary` now accepts an optional `mode` field: `"merge"` (default, prior behavior — only new terms are appended) or `"replace"` (the submitted list becomes the entire glossary). The LLM-proposals save flow continues to send `merge`, so existing behavior is unchanged.
- New `GET /api/setup/<id>/glossary` endpoint returns the current glossary in proposal-row shape (`english`, `spanish`, `type`, `context`, `alternatives`) for the edit table.

### Changed
- **Evaluators use shared image-placeholder utilities**: `DictionaryEvaluator` and `GrammarEvaluator` previously had inline regex to strip `[IMAGE:...]` tokens before tokenising. Both now delegate to `strip_image_placeholders` and (for grammar) `image_placeholder_ranges`, eliminating duplicated patterns and ensuring LanguageTool matches inside replaced placeholder regions are suppressed.
- **Glossary candidate extraction strips image placeholders**: `extract_candidates` now calls `strip_image_placeholders` immediately after normalising newlines, preventing filename fragments (`IMAGE`, `jpg`, path components) and description words from surfacing as candidate glossary terms.

### Fixed
- **XSS in glossary edit table**: `escapeHtml` used a DOM text-node trick that escaped `<`, `>`, `&` but not `"`. Terms injected into `value="..."` HTML attributes could break out of the attribute and execute event handlers. Fixed by additionally replacing `"` with `&quot;`.
- **glossaryEditMode not reset after save**: after a successful replace-mode save, the edit flag stayed `true`, causing any subsequent add-row or LLM-generate save to also use replace semantics.
- **Rejected rows resurrected by "+ Add row"**: clicking "+ Add row" re-rendered the table from a DOM snapshot that included rejected rows, silently un-dropping them before save.
- **POST /api/setup/<id>/glossary** now returns 400 (instead of 500) on a missing or non-JSON body.
- **replace mode with empty terms list** is now explicitly rejected with 400 rather than silently overwriting `glossary.json` with an empty array.

## [0.6.1.0] - 2026-05-13

### Added
- **Per-question Skip flag in the style-guide wizard**: every fixed, conditional, and LLM-generated question now has a "Skip" checkbox in the top-right of its block. Toggling Skip dims the block in place, disables the radios, and clears any selected answer. Skipped questions are excluded from `collectAnswers()`, so they drop out of the style-guide LLM prompt, the no-LLM fallback, the prompt-copy preview, and the glossary guidance derivation. The Glossary stage's "choose relevant questions" list also greys out (with a line-through and disabled checkbox) any row whose source question is skipped, so unanswered questions can't sneak into the glossary prompt either. State is session-only — a reload resets it.

### Fixed
- **Feature detector reads source-language text only**: `load_clean_source_text` now prefers `chunks/*_chunk_*.json` (which carries an immutable `source_text` field) over `chapters/<id>.txt`. Previously, on a partially-translated project, the detector was scanning translated chapter files and matching Spanish words (`lanzamiento de pesos`, `trabajo duro`) as English period-currency tokens. Priority order is now `chunks/` → `chapters/` → `source.txt`. The style-guide wizard's source sample uses the same loader and inherits the fix.
- **`currency_period` detector**: the bare word `crown` was matching `victor's crown`, `crown of gold`, `crown the summit`. The detector now counts `crown` only when at least one strong currency token (`shilling`, `pence`, `peso`, `real`, `maravedí`, etc.) also appears, and bumps the `present` threshold from `count >= 2` to `count >= 3`.
- **`epicene_animal_speakers` detector**: incidental mentions of `spider`, `eagle`, `mouse`, `crow` inside scenes full of human dialogue were tripping the speech-verb-proximity gate. The detector now drops `has_speech` / `has_dialogue` from the trigger gate, requires either a non-pronoun prefix (`Mr.`, `Father`, `old`, …) or proper-noun-style capitalization on the animal mention, and requires `mismatch_pairs >= 1` for `present` (so a single consistent `mother + jirafa` no longer fires).

## [0.6.0.0] - 2026-05-07

### Added
- **Heuristic full-text feature detection drives conditional style-guide questions**: a new `src/text_feature_detector.py` module scans the entire source text (not just the 15 K-character LLM sample) and emits a `FeatureManifest` with one entry per feature (`present`, `count`, `confidence`, `evidence`).
  - 14 detectors: `dialogue`, `verse`, `footnotes`, `epigraphs`, `letters`, `scripture_references`, `archaic_language`, `foreign_passages`, `lists`, `block_quotes`, `dramatic_format`, `measurements_imperial`, `currency_period`, `translator_notes`. The dialogue detector reuses `src/chunker.py` `_is_dialogue()` / `ATTRIBUTION_RE`; the verse detector uses `_is_scene_break()` for stanza delimiters.
  - Manifest cached at `projects/<id>/text_features.json`; re-runs only when the source mtime is newer than the cache or `--force-rescan` is passed. Each entry stores 1–3 short evidence excerpts so detection is auditable.
- **Conditional style-guide question library**: `prompts/style_guide_questions.json` is now a dict with two arrays:
  - `fixed`: 4 always-asked questions (`dialect`, `forms_of_address`, `person_name_handling`, `place_name_handling`).
  - `conditional`: 14 questions, each carrying a `requires` predicate (`{"feature": <name>, "min_count": N, "min_confidence": x}`) evaluated against the manifest. Only matching questions are surfaced.
  - `dialogue_formatting` moved out of the fixed set and re-introduced as a conditional question gated on `dialogue.min_count = 5` — it now fires only when dialogue is actually present.
  - Legacy flat-list configs are still accepted (treated as all-fixed, no conditional).
- **CLI hint** in `scripts/generate_style_guide.py`: prints the list of detected features, where the manifest cache lives, and a one-line "Detected: …" excerpt above each conditional question so the user understands why it is being asked. New `--force-rescan` flag.
- **Manifest-aware LLM prompt**: `prompts/style_guide_questions.txt` now embeds a compact `{{ feature_manifest_summary }}` block instructing the LLM that features marked ✓ already have dedicated questions and not to duplicate them, and to use the manifest as ground-truth evidence about content beyond the 15 K-character sample.
- New tests: `tests/test_text_feature_detector.py` (per-detector tests + manifest caching), `tests/test_style_guide_wizard.py` (config loading, conditional filtering, manifest summary in prompt). New fixtures: `tests/fixtures/verse_sample.txt`, `footnote_sample.txt`, `epistolary_sample.txt`.

### Changed
- `src/style_guide_wizard.py`: new public helpers `load_question_config`, `load_conditional_questions`, `get_active_questions`. `build_question_prompt` now takes an optional `manifest` and renders a summary block. `load_fixed_questions` is preserved as a back-compat shim.

## [0.5.1.0] - 2026-05-07

### Added
- **EPUB / sermon-style chapters**: Chapter heading detection accepts a word prefix plus numeral (for example `SERMÓN I.`, `SERMON III.`). Headings are normalized for display and TOC/NCX (for example `Sermón I`). Optional `toc_format: "heading_only"` in `project.json` omits the subtitle from TOC labels when the subtitle is a short epigraph line.
- **EPUB nav**: `toc.ncx` page inherits `style.css` for nested list indentation; default CSS includes nav list rules.

### Changed
- **Sentence alignment**: After pysbd, post-split sentences that match a run-on quote pattern (period, optional closing quote, space, opening quote or paren) even when short; long-sentence split regex allows a closing quote/bracket before whitespace before the next segment.

### Fixed
- **Web UI reader**: Pending-corrections banner only when `corrections.jsonl` has a non-blank line; whitespace-only files are ignored and removed so they do not keep showing the banner.

## [0.5.0.0] - 2026-05-04

### Added
- **Front and back matter**: pattern-driven detection in `src/book_splitter.py` / `src/split_patterns.json`, CLI flags on `scripts/split_book.py`, `scripts/build_chapter_manifest.py` for existing projects, EPUB spine and nav from `chapter_manifest.json` in `src/epub_builder.py`, and dashboard + reader updates in `web_ui/` (manifest-aware navigation).
- **Stage 8 Export — "Note from the Translator"**: optional editable end-matter chapter for KDP-ready EPUBs.
  - New heading + body fields on the Export panel, auto-saved on blur (Saving… / Saved / click-to-retry indicator, sequence-token guard against stale POST responses).
  - Per-book storage at `projects/<id>/translator_note.json` (`{heading, body}`); empty body → no chapter is appended (EPUB is byte-identical to the legacy build).
  - Default body sourced from `prompts/translator_note_default.txt` (per-user, gitignored), falling back to repo-tracked `prompts/translator_note_default.example.txt`.
  - Default heading constant `Note from the Translator` (`src.epub_builder._DEFAULT_TRANSLATOR_HEADING`).
  - Body capped at 100 KB (HTTP 400); heading uncapped but HTML-escaped (XSS-safe). Inline `[IMAGE:...]` placeholders are stripped (notes don't carry images).
  - Corrupt `translator_note.json` is renamed to `.bak.<unix-ts>` and defaults are returned silently.
- New endpoints: `GET` and `POST /api/project/<id>/translator-note`. `POST /api/project/<id>/build-epub` now accepts `translator_heading` and `translator_note` (persisted to disk first, then read back and passed into `build_epub`).
- New helpers in `src/epub_builder.py`: `_render_body_blocks`, `_strip_image_blocks`, `note_text_to_xhtml`, plus `translator_note_heading` / `translator_note_body` kwargs on `build_epub`.

### Fixed
- **Rechunk**: reconstruct source text from existing chunk files instead of assuming `chapters/<id>.txt` exists.
- **Web UI**: preserve explicit `0` in chunk overlap defaults (avoid dropping user-set zero).

## [0.4.1.0] - 2026-05-01

### Added
- Dashboard **Re-align chapter** action: `POST /api/project/<id>/align/<chapter_id>` recombines chunks, runs sentence alignment, and re-anchors existing chapter annotations when alignment shifts `es_idx`.
- `tests/test_project_align_route.py` — route validation, happy path, and annotation re-anchor / orphan cases for the align endpoint.
- `scripts/fetch_missing_images.py` — CLI to fetch missing Gutenberg images for an ingested project (after `--no-images` or failed downloads); reads `[IMAGE:images/...]` from `source.txt` and writes to `<project>/images/`.
- Reader remove-text confirmation: bilingual preview of the spans about to be removed, plus i18n strings for preview labels (EN + ES).

### Changed
- Remove-text confirm dialog max width increased (`360px` → `460px`) to fit preview content.

### Fixed
- Reader remove-text: avoid silently deleting the untouched default suggestion in one pane when the user diverges from the default in the other pane (preview + `divergeFromDefault` guard).

## [0.4.0.0] - 2026-04-29

### Security / Hardening
- `/api/sentence/retranslate` and `/api/sentence/replace` now reject non-finite `expected_chunk_mtime` (NaN, Inf) with 400 — previously NaN silently bypassed the concurrency guard.
- Size caps on retranslate text inputs: `source_text` ≤ 8KB, `context_text` ≤ 16KB, `current_translation` / `new_translation` ≤ 32KB. Oversize requests return 413.

### Added
- Reader sentence retranslate flow: tap a sentence, confirm the source span, pick a model, get a fresh LLM translation, optionally hand-edit, and replace the existing translation with one confirm. See `docs/READER_RETRANSLATE.md`.
- `src/retranslator.py` — `retranslate_sentence()` primitive (compact prompt, markdown-fence stripping, retry-on-empty, per-call cost estimate).
- `prompts/retranslate_sentence.txt` — lean prompt template (~700–1000 tokens with style guide; reuses `filter_glossary_for_chunk` so the glossary is filtered to terms in the source span).
- `RetranslationResult` Pydantic model in `src/models.py`.
- Endpoints: `GET /api/llm/models`, `POST /api/sentence/retranslate`, `POST /api/sentence/replace`. All carry `expected_chunk_mtime` for concurrency.
- `_attach_text_in_chunk` enriches `/api/alignment/<project_id>/<chapter>` rows with `text_in_chunk`, `chunk_offset_start`, `chunk_offset_end`, and `chunk_mtime` so the reader can round-trip a literal substring back to the chunk file.
- Per-call model picker in the reader modal, populated from `llm_config.json`; last-used model persisted in `localStorage`.
- Audit log: every successful replace appends to `projects/<id>/retranslations.jsonl`.
- `scripts/_smoke_retranslate.py` — CLI smoke for iterating on the prompt without booting the web UI.
- New i18n strings (`retranslate_*` keys) in EN + ES.
- Reader retranslate: source-expansion checkboxes (driven by clicking the alignment badge) fold the previous and/or next English sentence into the span sent to the LLM; new "Context (± sentences)" numeric input sends N sentences before and N after as a read-only `<context>` block (default 1, range 0–5). Bumps prompt template to v1.1.

## [0.3.0.0] - 2026-04-21

### Added
- LLM-judge evaluator primitive (`src/judge.py`) with pairwise and absolute scoring modes, retry-on-parse-failure, and 4-dimension rubric (fluency, fidelity, regional, voice)
- `src/evaluators/llm_judge_eval.py` wrapping the judge primitive for the evaluator pipeline
- `scripts/compare_models.py` model comparison harness CLI with multi-chapter, multi-provider support and translation logs
- `scripts/extract_translations.py` helper to pull translations from logs
- Multi-provider support in `src/api_translator.py` (Anthropic + OpenAI) with split batch submit/await paths
- Six judge prompt templates under `prompts/judge_*.txt` covering absolute/pairwise × default/full-context/no-voice
- Configurable judge context modes (`style` vs `full_prompt`) and chunking in `compare_models`
- `docs/LLM_JUDGE_EVALUATOR.md` usage guide
- LanguageTool JVM instance cache in `src/evaluators/__init__.py` keyed by dialect
- Test coverage: `tests/test_judge.py`, `tests/test_api_translator.py`, `tests/test_compare_models_cli.py`, `tests/test_evaluators/test_jvm_cache.py`

### Changed
- `get_evaluator()` accepts an optional `dialect` argument and caches grammar evaluators per dialect to avoid repeated JVM startups
- `src/models.py` extended for multi-provider/judge workflows

## [0.2.0.0] - 2026-04-17

### Added
- Batch API job management panel: submit, check, retrieve, and dismiss jobs directly from the dashboard
- Per-chunk glossary filtering for batch submissions — only relevant terms are sent with each chunk
- Cross-chapter context map support for batch translation jobs
- `--retrieve-batch` CLI flag to manually retrieve results from a completed batch job
- Auto-retrieve in `--check-batch` when a batch completes, using stored chunk file paths

### Changed
- Batch job submission now stores chunk file paths at submit time, eliminating the need to re-specify files at retrieval
- Retrieved status message has a dismiss (×) button to clear it from the UI
- `chunk_file_map` paths validated against project directory before loading or saving during retrieval

### Fixed
- Double-retrieve race condition: status set to `"retrieving"` inside the lock before network call
- Silent data corruption when a chunk file failed to parse: chunk↔path mapping now built together at load time
- Blank JSONL lines from OpenAI batch output no longer crash the full retrieval
- `batch_api_jobs.json` writes are now atomic (tmp + rename) — crash mid-write no longer zeros the file
- `"ended"` status from provider no longer bypasses the already-retrieved guard on re-check
- `job_id` and `chapter_id` inputs validated on all batch API endpoints
- `prompt_map` (full prompt text) stripped from persisted job tracking files — logged via prompt logger instead
- XSS: server-supplied job fields in the jobs table now escaped; action buttons use event listeners instead of inline onclick

## [0.1.0.0] - 2026-04-14

### Added
- Unified project pipeline dashboard with 7-stage workflow (Ingest, Split, Chunk, Setup, Translate, Review, Export)
- Setup wizard web UI for style guide questionnaire and glossary bootstrap
- Three-phase intelligent chunker with structural split detection and configurable patterns
- Pluggable LLM provider configuration via `llm_config.json` (Anthropic, OpenAI, Google, local)
- Per-chunk LLM provider/model selector in the dashboard
- Style guide wizard and glossary bootstrap modules as importable library code
- Cross-chapter context carry-over for translation continuity
- Prompt/response logging for all LLM calls to `prompts/history/`
- Chapter filter and `--dry-run` mode for translate scripts
- Chunk editor UI with reader integration
- Annotation badge cycling in reader view
- Project status filtering and internationalization (English/Spanish) for dashboard
- `spanish_title` field for project config, Source tab, and EPUB export
- Per-chapter rechunk endpoint and UI controls
- File I/O utility module with atomic writes and glossary filtering
- LLM provider configuration guide and chunking guide documentation
- Example prompt templates for translation, style guide, and glossary bootstrap

### Changed
- Rewrote documentation to reflect pipeline dashboard UI
- Externalized split patterns into `split_patterns.json`
- Updated overlap defaults to 0 and synced docs to `chapters/` path
- Removed legacy `translated/` fallback in favor of canonical `chapters/` directory
- Unified translation save pipeline: backup, purge corrections, recombine, realign, re-anchor
- Archived legacy translation UI, redirected `/` to project list
- Used cross-platform temp directory for OpenAI batch file writes

### Fixed
- Glossary extraction now prefers mixed-case surface forms over all-caps
- Alignment confidence fields and chapters refresh on align
- Test mocks for Anthropic/OpenAI rate limit errors updated for current SDK
