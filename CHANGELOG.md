# Changelog

All notable changes to this project will be documented in this file.

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
