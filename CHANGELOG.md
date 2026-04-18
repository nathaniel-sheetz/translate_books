# Changelog

All notable changes to this project will be documented in this file.

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
