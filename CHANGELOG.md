# Changelog

All notable changes to this project will be documented in this file.

## [0.25.2.0] - 2026-07-01

### Added
- **`--chapter-pattern auto` is the new default.** The harness detects the best-fit chapter pattern from the source text itself (`detect_pattern_from_text`) — the local `source.txt` analog of the URL path's HTML-heading detection — so a pattern rarely has to be set by hand. Falls back to `roman` when nothing matches confidently.
- **`chapter_numeric_titled` pattern** ("Chapter 1 Some Title" / "Chapter 1"), and `chapter_roman_titled`'s title is now optional so it also matches numeral-only headings ("CHAPTER I"). All named patterns in `split_patterns.json` (`roman`, `numeric`, the titled variants, `allcaps_heading`, `bare_roman`) are now selectable from the CLI, derived from the JSON so a defined pattern is never unreachable.
- **Post-split sanity warnings** (`split_sanity_warnings`). `setup`/`split`/`split-preview` now return `pattern_used`, `suggested_pattern`, a `chapter_report`, and `warnings` on the local `source.txt` path too — flagging a likely mis-split (e.g. one chapter for a large source) instead of silently carrying it to EPUB.

### Changed
- **`detection_order` omits the plain `roman`/`numeric` patterns.** The optional-title variants subsume them and always match first, so they were unreachable during auto-detection; they remain selectable as explicit `--chapter-pattern` choices.

## [0.25.1.0] - 2026-06-30

### Added
- **Claude Sonnet 5 support (`claude-sonnet-5`).** Added to the Anthropic provider model list (GUI dropdown + default config) with introductory pricing ($2/$10 per MTok).

### Fixed
- **Sampling params (`temperature`/`top_p`/`top_k`) no longer sent to Opus 4.7+ generation models.** Sonnet 5 and other models in that generation return HTTP 400 if any sampling params are included. A new `_rejects_sampling_params()` check gates `temperature` out of both the realtime and Anthropic batch call sites for those models.

## [0.25.0.0] - 2026-06-30

### Added
- **Subagent backend for the tailored-judge framework.** Judges now run one of two interchangeable ways: the existing **API backend** (`run_judges.py run`, metered, behind the dollar cost gate) or a new **subagent backend** (`run_judges.py prepare` → spawn `judge-worker` subagents → `run_judges.py commit`) that renders each judge's prompt to a file, has cheap spawned workers answer it, and collects the JSON verdicts — **zero API spend**, runs on the session. Both backends build the same prompt and run the same parser, so the findings and the persisted `evaluations/<chunk>.json` are identical whichever ran; the run header records the `backend` (and `worker_model` for the subagent path). Mirrors the dual-backend pattern translate-harness already uses for translation.
- **`judge-worker` agent (`.claude/agents/judge-worker.md`).** A minimal Read+Write worker (default `model: sonnet`) spawned one-per-`(target × judge)`: it reads a rendered judge prompt and writes only the JSON verdict to a draft file. The orchestrator pins the model via the spawn's `model:` arg.
- **`judge-review` skill backend selection.** The skill now asks API vs subagent (default API), runs the matching flow, and gates the subagent path on a conversational usage check before spawning N workers (the no-dollars analog of the cost gate).

### Changed
- **`run_judges.py` is now subcommand-based** (`run` / `prepare` / `commit`) instead of a single flat command. `run` preserves the previous API-backend behavior verbatim; `prepare`/`commit` drive the subagent backend.
- **The `Judge` interface split into a shared seam.** Every judge now implements `build_prompt(target, context)` and `parse_response(target, raw, context)`; `run()` composes them (build → call LLM → parse) for the API path while the subagent backend reuses the same two methods. Any judge that implements both gets both backends for free. The dialogue judge was refactored onto this seam with no change to its API-path behavior. `parse_response` raises `JudgeParseError` on unparseable output so the API path can retry while the subagent `commit` marks the draft failed for re-spawn.

### Fixed
- **`commit` no longer crashes on a tampered or corrupt manifest.** Non-list `entries`, missing keys in an individual entry, and UnicodeDecodeError from a draft file are all caught and reported as `failed` entries rather than aborting the entire commit with an unhandled exception.
- **Path traversal guard on `draft_path` in `commit`.** Draft file paths from the manifest are now resolved and validated to stay inside `.harness/judges/` before being read, matching the existing guard on `load_template`. Similarly, `target.id` is validated against the safe-id regex in `prepare` before it is used as a filename component.
- **`_PREPARE_SCHEMA` not imported in `run_judges.py`.** The prepare error paths referenced `_PREPARE_SCHEMA` but it was never imported, causing a `NameError` crash whenever `prepare` encountered an invalid scope or suite. The symbol is now imported from `src.judges.subagent`.
- **`commit` exit code.** `run_judges.py commit` now exits with code 1 when the payload status is `"error"` (manifest absent, unreadable, etc.) rather than always exiting 0.
- **`run_header` judge list.** When all entries fail to parse, `judge_names` for the run header is now taken from the stored `judges` key in the manifest rather than being derived from the empty committed list.
- **Performance: judge instances and templates cached.** `get_judge()` is called once per judge name (not once per `target × judge`) in `prepare`, and `load_template()` is memoised with `functools.lru_cache` so the template file is read from disk once per session.

## [0.24.0.0] - 2026-06-29

### Added
- **Tailored LLM judges framework (`src/judges/`).** A new `run_judges` CLI lets you run named LLM evaluators over a single chunk or an entire chapter, estimate cost before spending, and optionally persist results into `evaluations/<chunk>.json` so the web dashboard badges pick them up. The first built-in judge — `dialogue` — checks Spanish dialogue formatting against the house rules in `prompts/dialogue.txt` (raya usage, one-turn-one-paragraph, incisos, guillemets for thoughts), assigns a 0–1 compliance score, and returns per-finding issues with severity and suggested fixes. A `.claude/skills/judge-review/SKILL.md` entrypoint exposes the framework as a gstack skill.
- **Dashboard badges now fold in tailored-judge findings.** A chunk judged by a tailored judge (but not yet coded-evaluated) lights up the error/warning/info badge counts alongside coded-evaluator results; the same counts appear for chunks that have both.
- **`judge_suites` in `app_config.json`** lets projects declare named groups of judges for one-shot runs (e.g. `"default": ["dialogue"]`).

### Fixed
- **`extract_json` called `json.JSONDecoder.raw_decode` twice per candidate** — once to probe validity (discarding the result) and again to retrieve it. The second call could theoretically hit a different code path under concurrent modification; collapsed to a single `_, end = decoder.raw_decode(...)` call.
- **`run_judges --persist` loop now surfaces partial failures in the output JSON** instead of only logging them. Individual persist errors appear in a `persist_errors` list alongside the `persisted` paths, so a machine parser can tell "some chunks were not saved" without digging through logs.
- **Glob injection and path traversal guards in the judges layer.** `chapter_id` and `chunk_id` from the CLI are now validated against an alphanumeric+underscore+hyphen regex before being used in filesystem globs or paths, and `load_template()` verifies the resolved path stays inside `prompts/` before reading.

## [0.23.2.0] - 2026-06-27

### Changed
- **Reader chapter list: folded the standalone "flag" badge into the "to review" count and simplified the reviewed/unread badges.** Flag annotations now add to `review_count` (alongside word-choice and inconsistency) instead of rendering a separate `badge-flag`, and a chapter that has been reviewed always shows the "reviewed" badge rather than only when it had zero annotations and high confidence.

### Added
- **Reader chapter-list header now shows the Spanish title (when UI language is Spanish) and a settings/dashboard gear link** to `/project/<id>`, matching the project-card behavior.

## [0.23.1.4] - 2026-06-27

### Fixed
- **The concordance search field no longer clips the ES|EN toggle on narrow phones.** The `.search-input` flex item kept its default `min-width: auto`, so it refused to shrink below its content width and pushed the `flex: 0 0 auto` language toggle off-screen on small viewports. Adding `min-width: 0` lets the field shrink and keeps the toggle visible.

## [0.23.1.3] - 2026-06-26

### Changed
- **Glossary creation no longer surfaces unreliable candidate `type_guess` labels.** The heuristic type guess (character/place/concept/technical/other) was a noisy signal that leaked into the bootstrap prompts, the `/extract-candidates` API response, and the CLI extraction summary, where it risked anchoring the LLM on a wrong guess. The guess is now kept purely as an internal ranking/dedup signal and dropped from all surfaced output; the bootstrap prompts instead instruct the model to infer `type` from how each term is used in the excerpts.

## [0.23.1.2] - 2026-06-26

### Fixed
- **`setup` now reports stripped boilerplate in `dropped` instead of silently discarding it (#28).** Standalone front/back-matter headings (Contents, List of Illustrations, Copyright) that sit before the first chapter and aren't declared as `front_matter_titles` were dropped by position and never reached `collect_dropped`, so `setup` returned `dropped: []` despite the strip — making the harness's "confirm `dropped`" step impossible. The drop-matter patterns are now scanned alongside real front/back matter so these headings become recorded, strippable sections.

### Changed
- **`show-translation` output schema is documented concretely.** The harness `OUTPUT_SCHEMAS` entry and SKILL.md now spell out the nested `chapters[] → chunks[] → translated_text` shape (with `source_text` sibling), steering readers away from writing a `python` probe that guesses a top-level `chunks`/`items` list.

## [0.23.1.1] - 2026-06-26

### Changed
- **Browsing guidance: read a fact off a static public page with a one-shot `WebFetch`, not the `/browse` skill.** `CLAUDE.md`'s "use `/browse` for all web browsing" rule is scoped to interactive/QA browsing of the app under development; the translate-harness skill now tells the book-identification beat to `WebFetch` the source URL rather than loading the ~600-line browser skill to read one fact.

## [0.23.1.0] - 2026-06-26

### Fixed
- **Glossary extraction no longer emits underscore-twin junk candidates (#27).** Project Gutenberg's paired-underscore italic markers (`_Gaudenzia_`) are stripped before tokenizing, so an italicized name no longer becomes a separate candidate from its plain spelling. The fix lives in the shared extractor, covering both the GUI and harness paths.

### Changed
- **The harness glossary beat extracts from the cleanest available text (chunks/ → chapters/), not raw `source.txt` (#27).** Front matter (TOC, copyright, chapter-title fragments) is excluded from candidate extraction, mirroring the GUI route; `glossary_prepare` reports the chosen `source_kind` and falls back to `source.txt` only when neither chunks nor chapters exist.

## [0.23.0.0] - 2026-06-25

### Added
- **Project folders are named from the book title, not a generic slug (#22).** `setup` derives a readable `projects/<title-slug>/` from the book's title, with a new `harness_guard` collision-resolution path that picks a free slug when the name is already taken.
- **The splitter auto-strips boilerplate front matter and translates front-matter headings (#13, #17).** Navigation/boilerplate sections (Contents, Title Page, …) are dropped before numbering/translation, and a front/back-matter section's own first line is preferred over the source-language manifest label so a Spanish `Prólogo` is no longer stacked under an English `Foreword`.
- **Chapter subtitles are lifted onto the heading line (#16).** An inline or standalone subtitle following a `Capítulo XXX` heading is detected and folded into the heading, with the remaining body carried through as a `body_override`.
- **Streaming commands always write a fresh, self-documenting `last_output.json` (#18, #19).** `_stream_result` returns a dict (never a bare int) so each command refreshes the artifact even when the wrapped script emits no sentinel, closing the stale-artifact trap.

### Changed
- **Chunk overlap is disabled and combine de-duplication is hard-blocked (#20).** Chunks are paragraph-aligned by construction and stitched by plain concatenation; the obsolete start-overlap removal helper and its tests were deleted.
- **Translator workers are silenced down to a single `done <chunk_id>` token (#14).** Worker agents no longer recap their glossary/formatting choices, saving orchestrator context; the orchestrator reads the draft file directly.
- **The glossary draft warns when accents were stripped (#21).** A draft missing expected accented characters is surfaced rather than silently committed.

### Fixed
- **`_first_nonempty_line` no longer promotes an `[IMAGE:...]` placeholder to a front-matter heading.** A front/back-matter body beginning with a prepended image line now skips the image token when choosing the section label/TOC entry.
- **A wrapped script's `HARNESS_RESULT` sentinel can no longer overwrite the harness-authoritative `command`/`exit_code`** recorded for a streaming run.
- **The `XXX` placeholder guard no longer false-positives on the Roman numeral for chapter 30.** A bare/trailing `XXX` (or one preceded by a chapter keyword) is treated as a heading numeral, not an incomplete-translation marker.

## [0.22.7.0] - 2026-06-23

### Added
- **`harness status` reports a project's pipeline progress at a glance (read-only; no spend).** On a resume it answers "where is this project and what's left?" in one call instead of hand-rolling a loop over `chunks/*.json`: per-chapter translated-vs-pending chunk counts (via `chunk.has_translation`), the saved spawn plan (`parallelism`/`window`/`batch_size`), which artifacts exist (`source`/`chapters`/`style_guide`/`glossary`/`difficulty`/`chunks`), any built EPUBs, and a one-word `stage` (`pre-chunk`/`untranslated`/`partial`/`fully-translated`) plus a `next` hint and `pending_chapters` list. `spawn_mode_moot` reflects chunk **structure** (single-chunk-per-chapter), not translation state.
- **`harness runs` reads `logs/harness_runs.jsonl` back into a per-run retro (read-only).** The run log was previously write-only; `runs` summarizes a single run into a command timeline (durations + outcomes), the qualitative beats (`approval`/`backend`/`spawn_mode`/`respawn`), status tallies, and total command seconds. Defaults to the project's most recent run; `--run-id` selects a specific one and `available_run_ids` lists the rest. Backed by the new `read_run_events()` reader in `src/utils/run_logger.py`, which tolerates a missing/corrupt log (`[]`) and skips unparseable lines.
- **`translate-prepare --batch-size` persists a recommended fan-out width.** A saved number (default 5) the agent ramps from and throttles back toward ~1 on a `529` (overloaded), echoed under `spawn_plan`/`usage_summary`; invalid values are reported, not raised, and never corrupt the saved config. `translate-prepare` also now returns `spawn_mode_moot` (True when every in-scope chapter is a single chunk) so the agent can skip the spawn-mode gate when the continuity modes are equivalent.
- **SKILL guidance for the above:** resume-with-`status` first, the spawn-mode-moot gate skip, a 500-vs-529 backoff playbook (probe → throttle → commit-then-check), and a hardened "never parse stdout — always `Read` `last_output.json`" instruction.

## [0.22.6.1] - 2026-06-23

### Changed
- **Default Anthropic model is now `claude-sonnet-4-6` (was `claude-sonnet-4-20250514`).** The new id is applied uniformly across the harness state default (`src/harness/state.py`), the `api_translator` fallback config + `DEFAULT_MODEL` constant, every CLI `--model` default (`extract_glossary_candidates.py`, `generate_style_guide.py`, `translate_api.py`, `translate_book.py`, `harness.py`), the example config (`llm_config.example.json`), and the docs (`LLM_PROVIDERS.md`, `GLOSSARY_CANDIDATES.md`, `WEB_UI_GUIDE.md`, the translate-harness SKILL). Display names updated `Claude Sonnet 4` → `Claude Sonnet 4.6`; pricing unchanged. This also resolves a pre-existing inconsistency where the judge/retranslate/compare tooling already referenced `claude-sonnet-4-6` while the defaults lagged on the dated id.

## [0.22.6.0] - 2026-06-22

### Added
- **Every harness command now writes a timestamped JSONL event to `logs/harness_runs.jsonl`.** Each entry carries `run_id`, `project`, `event`, `cmd`, `status`, `dur_s`, and result-summary counts — giving every run a queryable timeline. The `run_id` (`<slug>_YYYYMMDD_HHMMSS_ffffff`) is minted at `setup` and stamped on every subsequent command for the same run, so a full session can be replayed or diffed later.
- **`harness log-event` lets the agent write qualitative beats** (e.g. `approval`, `spawn_mode`, `backend`, `respawn`) with arbitrary `--data` JSON, tying the agent's decisions into the same timeline as the CLI commands. The SKILL now instructs the agent to call `log-event` after each key gate (style-guide/glossary approval, backend choice, spawn-mode choice, worker re-spawns). Writes are best-effort and never fail a command.

## [0.22.5.4] - 2026-06-22

### Added
- **`harness show-translation` reads committed translations back out for review (friction-log #7).** A read-only command that returns `source_text` + `translated_text` per chunk straight from `projects/<slug>/chunks/*.json` (the field that actually holds the translation, which the agent previously had to guess by trying `translation`/`translated_text`/`translated`). `--chapters` scopes it, `--max-chunks` caps the sample so a gut-check can't flood context, and `--no-source` returns translation-only; the output names the keys under `fields` and mirrors to `last_output.json` like the other JSON commands. The translate-harness SKILL Step 4B-e now points the agent here for an in-chat sample and states that the per-worker `.harness/translate/*.draft.txt` files are consumed at `translate-commit` (empty afterward) — so neither the agent nor a human reads internal files or guesses the schema. Covered by `test_show_translation_returns_committed_text`.

## [0.22.5.3] - 2026-06-22

### Changed
- **The setup `locale` auto-answers the redundant `dialect` question (friction-log #8).** `style-guide prepare-questions` now maps the locale chosen at setup to a `dialect` option (`es-MX` → `mexican_spanish`, etc.) and returns it as a `prefilled` id + `prefilled_reason` with instructions to present it as a confirm/override default rather than a blank re-ask. `prepare-draft` adds defense-in-depth: if the agent confirms the prefill by leaving `dialect` out of the answers, it backfills from the locale so the dialect section is never blank. The new `dialect_id_from_locale` helper validates every mapped id against the dialect question's actual options, so it stays correct if labels are reworded and never names a non-selectable option. Region-less bare `es` deliberately resolves to `generic_latin_america` (not Castilian); asserting Castilian requires an explicit Spain token (`es-ES`/`spain`/`castilian`). The SKILL also documents the first-class informal-tú `forms_of_address` option (`t_dominates_informal`).

## [0.22.5.2] - 2026-06-22

### Changed
- **The `chunk`/`cost` estimate is backend-neutral (friction-log #9).** When `stage_translate` runs with `--cost-only` (the harness `chunk`/`cost` commands), it no longer prints an unconditional `Estimated cost: $...` — at that point the translation backend isn't chosen yet, and the subagent backend has no metered-API spend. The estimator now prints the job size, frames the API price as conditional (`If translated via the metered API: ~$X`), and reminds that the subagent backend uses the subscription (no API $). The unconditional cost lead-in is reserved for the real paid translate run. The translate-harness SKILL clarifies that the dollar figure feeds the Step 4 gate **only** on the API backend; the subagent path (Step 4B) gates on the `usage_summary` from `translate-prepare`, not the estimate.

## [0.22.5.1] - 2026-06-22

### Changed
- **Approval gates capture specific swaps via the custom field.** The translate-harness style-guide (G3) and glossary approval beats now present `AskUserQuestion` with exactly two predefined options — **"Approve all"** and **"Reject & talk it through"** — and instruct the user to pick **_Other_** to approve *with specific changes* (e.g. `Gatito → Minino`, "keep place names in English", or pasted corrected JSON). A custom answer is treated as approve-with-changes: apply the edits, re-run `commit`, briefly confirm, and continue without looping back into the gate. The two-kinds-of-STOP overview is updated to match.

## [0.22.5.0] - 2026-06-21

### Added
- **Style-guide answers can be recorded by option `id` or label, not just a positional index.** `style-guide prepare-questions` and `commit-followups` now surface each option as an `{id, label}` pair, where `id` is a stable slug derived from the label, so the agent passes the user's pick straight through to `style_answers.json` without counting positions. `resolve_answer` accepts (in priority order) a 0-based index (back-compat), an option id, the exact label (case- and whitespace-insensitive), or free text as a custom answer. `prepare-draft` now echoes `resolved_answers` (each tagged `option` or `custom`) plus `unanswered`, so a mistyped id that silently became a custom answer is caught before the draft is generated.

### Fixed
- **Whitespace-symmetric label matching and label-less options.** Label matching now collapses internal whitespace the same way slug ids do, so a label is reachable by typing it regardless of irregular spacing; options missing a `label` key resolve to an empty string instead of raising `KeyError`.

## [0.22.4.0] - 2026-06-21

### Fixed
- **Harness output is now reliably UTF-8 (friction-log #4).** `harness.py` reconfigures stdout/stderr to UTF-8 and `_run_script` passes `PYTHONUTF8=1`/`PYTHONIOENCODING=utf-8` to the wrapped `chunk`/`cost`/`translate`/`epub` subprocesses, so accented/curly-quote JSON no longer mojibakes or fails to decode on a cp1252 Windows console. The `RequestsDependencyWarning` that forced a lossy `grep` is filtered at import, so the CLI emits zero stderr noise. Every JSON-returning command also mirrors its result to `projects/<slug>/.harness/last_output.json` (UTF-8) and prints `OUTPUT_JSON: <path>`; the skill now tells the agent to Read that artifact instead of grepping stdout. Covered by tests asserting UTF-8 stdout + artifact mirror and that `_run_script` injects `PYTHONUTF8`/`PYTHONIOENCODING` into the child environment.

## [0.22.3.0] - 2026-06-20

### Added
- **User-chosen spawn modes for the subagent translate backend.** `harness.py translate-prepare` accepts `--parallelism sequential|chapter|all` and `--window <X>`, persisted to the project config and echoed back under `spawn_plan` (and `usage_summary`) so the later "translate the rest" batch reuses the choice without re-asking. The `chapter` mode (default, window 8) runs a window of chapters wave-by-wave on chunk position; `sequential` maximizes continuity; `all` is fastest with no cross-chunk context.
- **Committed-predecessor EN+ES context injection.** When a chunk's preceding chunk is already committed, `translate-prepare` now renders BOTH the predecessor's source tail and its Spanish translation into the prompt (the same `extract_previous_chapter_context` block the reader uses), so re-running prepare after each commit flows finished Spanish into the next chunk. An uncommitted predecessor degrades to source-only context — never blocking.
- **`harness.py align` command.** Aligns fully-translated chapters for the web reader and prints a `reader_first` link, the per-set finisher run after each `translate-commit` batch. Partially-translated chapters are skipped; `--chapters` limits the work; `--reader-host`/`--reader-port` shape the printed link.

### Fixed
- **`--window` below 1 is rejected** with the documented "must be a positive integer" error instead of being silently clamped to 1.
- **`align` reports a per-chapter aligner failure** (e.g. embedding model unavailable) as a partial result with an `error` key rather than crashing the batch and breaking the clean-JSON-on-stdout contract.

## [0.22.2.0] - 2026-06-19

### Added
- **Chapter-split review beat in the harness (GUI parity).** New `harness.py split-preview` dry-runs a chapter split and prints every detected section tagged `front_matter` / `chapter` / `back_matter` without writing files; `harness.py split` then commits the split, rewriting `chapters/` from `source.txt`. Both accept the full set of split controls (`--chapter-pattern`, `--custom-regex`, `--min-chapter-size`, repeatable `--front-matter-title` / `--back-matter-title`, and `--no-auto-front-matter` / `--no-auto-back-matter`). This mirrors the web GUI's Stage 2 so a misfired `setup` split can be refined without hand-editing `source.txt`.
- **Heading-derived split hints from ingest.** A Gutenberg `--url` ingest now records a per-chapter `chapter_report` and an auto-suggested `suggested_pattern` (both computed from the book's HTML headings) and surfaces them on the `setup` result so the agent can spot a wrong pattern or stray front/back matter. The keys are always present (null on the no-URL path).
- **`setup` split controls.** `harness.py setup` accepts `--front-matter-title`, `--back-matter-title`, and `--min-chapter-size` for a one-shot run that matches the review-beat behavior.

### Fixed
- **`split` clears stale chapter files.** `split` removes existing `chapter_*.txt` before rewriting so a smaller re-split never leaves orphaned higher-numbered files behind.
- **Paired-API symmetry.** `split` now returns `files_written: True`, matching `split-preview`'s `files_written: False`, so a consumer keying off that flag no longer hits a `KeyError` on the commit path.

## [0.22.1.0] - 2026-06-19

### Added
- **Per-chapter chunk sizing.** `harness.py chunk --per-chapter` reads each chapter's `suggested_target_size` from the cached `difficulty.json` (produced by `harness.py difficulty`) and sizes chunks independently per chapter. Harder chapters get smaller targets, which actually bite because min/max bounds now scale with the target via the new `ChunkingConfig.from_target()` factory. `--size` remains the fallback for any chapter not in the manifest.
- **`ChunkingConfig.from_target()` factory.** Centralizes the derivation of `min_chunk_size` / `max_chunk_size` from a target and ratio pair. Used by both the CLI chunk stage and the web UI split endpoint so the two paths cannot drift. With default ratios (0.25 / 1.5) and `target=2000` it reproduces the historical 500 / 3000 bounds exactly.
- **`translate_book.py --chunk-sizes`** argument accepts a JSON map `{chapter_id: target_size}` for per-chapter sizing without going through the harness flow wrapper.

### Fixed
- **Web UI split bounds now match CLI.** `_resolve_chunking` in `web_ui/app.py` now delegates to `derive_chunk_bounds` (the same function used by `ChunkingConfig.from_target`) instead of a local copy, eliminating the risk of the two paths drifting.

## [0.22.0.0] - 2026-06-18

### Added
- **Project grouping subfolders.** Books in `projects/` can now be organized into arbitrary subfolders (e.g. `projects/by-author/fabre/`, `projects/experimental/drafts/my-book/`) at any nesting depth. The reader listing, all API endpoints, and the `translate-harness` CLI all find projects wherever they live — you no longer need to keep every book at the flat root.
- **Automatic slug deduplication against nested projects.** When creating a new project, if the generated slug is already in use by a book in a grouping subfolder (not just the flat root), a numeric suffix is appended to avoid collision.

### Fixed
- **Duplicate project id warning.** If two folders at different paths share the same leaf name, a `WARNING` is logged and the first one wins — consistent between the web UI and CLI.
- **Stale project cache recovery.** If a cached project path is deleted or moved, the resolver now re-scans the tree to find the current location instead of returning a non-existent path.
- **Recursion depth guard.** Project discovery stops at 20 levels of nesting so a pathologically deep directory tree can never cause a stack overflow.
- **Symlink safety.** Directory traversal during project discovery never follows symlinks.

## [0.21.0.0] - 2026-06-18

### Added
- **Endnotes in exported EPUBs.** Footnote annotations created in the reader are now collected into a numbered "Notas" back-matter section when you export an EPUB. Each in-text superscript links forward to the endnote; each endnote links back to its position in the chapter. Notes are grouped by chapter and numbered sequentially across the book, ordered by their position in the translated text (not the source-language alignment index, so reorganized sentences are handled correctly).

### Fixed
- **Endnote ordering when Spanish sentences are reorganized.** Endnotes are now numbered by their text position in the chapter body rather than by alignment `es_idx` order. Previously, if a translator reorganized sentences relative to the English alignment, a footnote on an out-of-order sentence could be silently skipped.
- **Crash on malformed annotation records.** `_load_footnote_annotations` now safely skips records missing the `es_idx` key instead of raising `KeyError` and aborting the EPUB build.
- **Crash when alignment JSON has `"alignments": null`.** `_load_alignment_es_map` now treats a `null` alignments field the same as an empty list instead of raising `TypeError`.

## [0.20.1.0] - 2026-06-17

### Fixed
- **Stale batch-translate "Complete!" popup.** The batch-translation modal's progress UI is now reset to its pristine state (progress bar hidden, status text back to "Translating...", fill at 0%, Start re-enabled, Cancel hidden) whenever the modal opens or closes, so a previous run's completed progress bar can no longer linger into the next translation. On batch completion, the modal close is now scheduled before `loadStatus()` and that call is wrapped in a guard, so a rebuild error can't strand the modal open.

### Changed
- `TODOS.md` removed from version control (the file is gitignored and user-local).

## [0.20.0.0] - 2026-06-15

### Added
- **Dialect-density signal in translation-difficulty scorer.** A third orthogonal dimension — eye-dialect marker density — now contributes to the difficulty score alongside sentence length and lexical rarity. `dialect_marker_count()` counts deterministic markers: internal-apostrophe tokens outside the standard contraction whitelist (`ain't`, `young'un`, `off'n`), g-drop trailing apostrophes (`comin'`, `standin'`), curated trailing reductions (`o'`, `jes'`), leading-apostrophe elisions (`'twas`, `'em`), `a-`prefixed progressives (`a-thinkin'`, `a-walking`), and a small apostrophe-free lexicon (`reckon`, `yonder`, `nacherel`, `acrost`). Score is additive — non-dialect books are byte-for-byte unchanged; dialect can only raise difficulty. Standard contractions, possessives (`horse's`, `horses'`), and Irish/Scottish surname prefixes (`O'Brien`) are excluded. Both straight and typographic apostrophes (U+2019, U+2018, U+02BC) are normalized before whitelist comparison.
- **`DifficultyMetrics` dialect fields.** `dialect_marker_count`, `dialect_density`, `dialect_score` added to the dataclass (and serialized to `difficulty.json`). `DifficultyMetrics.from_dict` / `to_dict` round-trips include the new fields; the cache is automatically invalidated when calibration constants change.
- **Calibration constants.** `DIALECT_EASY`, `DIALECT_HARD`, `WEIGHT_DIALECT` added alongside existing length/rarity constants; all now returned by `calibration()` and stored in the manifest for traceability.
- **Dashboard dialect tooltip.** Difficulty badges now include dialect score and marker count in their hover tooltip when dialect is non-zero.
- **`/api/project/<id>/difficulty` API fields.** `dialect_score` and `dialect_marker_count` included in both book and chapter metric responses.

### Changed
- `TARGET_HARD` reduced from 1260 → 1200 (the hardest dialect-saturated chapters now reach a smaller target chunk size).
- `score_book()` cache invalidation extended: the manifest is also re-scored when calibration constants change, not only when source mtime is newer.

## [0.19.0.0] - 2026-06-13

### Added
- **Dialogue-conditional prompt injection.** Spanish translation prompts now automatically include a DIALOGUE FORMATTING block whenever the source chunk contains dialogue (as detected by the chunker's `_is_dialogue` rules). Non-dialogue chunks receive no injection, so token cost is only incurred when the block is relevant. The same conditional-wildcard pattern as image placeholders: a `{{dialogue_instructions}}` variable in the translation template renders to the framed block or `""`. The house-style rules live in `prompts/dialogue.txt` (user-local, gitignored) with a committed `prompts/dialogue.example.txt` fallback — edit your local copy without touching the repo.
- **`dialogue_instruction(source_text, target_language)` utility** in `src/utils/text_utils.py`. Returns the framed DIALOGUE FORMATTING section when the chunk has dialogue and the target is Spanish; otherwise `""`. Gated to Spanish to avoid injecting Spanish raya/guillemet rules into other-language pipelines.
- **`prompts/dialogue.example.txt`** — committed example dialogue style guide covering raya conversion, one-turn-one-paragraph, interrupted speech, and attribution patterns.

### Changed
- `prompts/translation.example.txt` updated to include the `{{dialogue_instructions}}` wildcard and updated STRUCTURE PRESERVATION wording that defers to the injected DIALOGUE FORMATTING section when present.
- `prompts/style_guide_questions.example.json` — removed `dialogue_formatting` conditional question (superseded by inline injection).
- `prompts/translation.txt` removed from git (it is per-user and gitignored; a fresh checkout uses `translation.example.txt` as the fallback).

## [0.18.0.0] - 2026-06-10

### Added
- **Subagent translation backend (`translate-prepare` / `translate-commit`).** You can now translate a book — or any chapter subset — without an API key. `translate-prepare` renders one prompt file per untranslated chunk and writes a manifest (no spend); you then spawn one lightweight worker agent per entry; `translate-commit` guards each draft (length, echo detection, image-token parity), stamps the chunk, and writes a provenance log. The backend is idempotent: re-running commit resumes from where workers left off, and chunks already translated are skipped. Pass `--chapters 1-2` or `3,7,12` to either command to work in chapter batches — translate a batch, read it in the reader, then translate the next.
- **`guard_translation_draft` validation.** Worker prose is validated before any chunk is stamped: empty or whitespace-only output, verbatim/near-verbatim echo of the English source (exact match and ≥85% token-overlap), image-token filename parity (dropped, hallucinated, or duplicated tokens all caught), and evaluator-flagged issues (placeholder text, wildly-off length) block the commit. Failed chunks are reported by name so you can re-spawn or edit them directly.
- **`image_filename_counts` utility.** The image-parity check now uses a `Counter` rather than a set, so a worker that emits an image token twice is correctly caught even though the filename itself is not new.
- **Translator agent definition (`.claude/agents/translator.md`).** Defines the `translator` agent type that the harness spawns as a worker: read a prompt file, write translated prose to a draft file, stop. The agent is tracked in git so the subagent backend is self-contained in the repo.
- **`--chapters` scope on all pipeline subcommands.** `chunk`, `cost`, `translate`, `translate-prepare`, and `translate-commit` all now accept `--chapters` so you can target a chapter range at any stage.

### Changed
- **Shared prompt/stamp seam.** `build_translation_prompt` and `apply_translation` are now the single source of truth for building the translation prompt and recording a translated chunk. The realtime path, both batch-API paths, and the subagent backend all call the same functions — no more prompt-rendering drift between backends.
- Manifest rescue: if `translate-prepare` is re-run before `translate-commit`, any worker drafts from the prior run that haven't been committed are carried forward into the new manifest rather than silently orphaned.
- `translate_prepare` now handles malformed `--chapters` values gracefully (returns an error dict instead of propagating a `ValueError`).
- Corrupted or partially-written `manifest.json` returns a descriptive error dict from `translate-commit` instead of crashing.

## [0.17.2.0] - 2026-06-10

### Changed
- **translate-harness: one non-interactive CLI surface.** The skill's pipeline orchestration moved out of ~nine inline-Python heredocs in SKILL.md (and a repo-global `.tmp/` scratch dir) into a tested CLI, `scripts/harness.py`, backed by a new `src/harness/` package (`flow.py` — one function per beat; `state.py` — per-project `.harness/` paths + `config.json`). No new business logic: each beat reuses the existing `style_guide_wizard` / `glossary_bootstrap` / `harness_guard` / `translate_book` / `difficulty_scorer` primitives. Working state is now per-project (`projects/<slug>/.harness/`) instead of a single global `.tmp/` shared across books, and `setup` wipes it for a clean run.
- The cost gate semantics are unchanged but now live in one place: `chunk` and `cost` always pass `--cost-only` (they cannot spend), and `translate` fails closed (exit 2) unless `--yes` is supplied after a separate-turn approval. SKILL.md is rewritten to drive the prepare → (agent drafts) → commit contract through the CLI.
- **Style-guide beat: explicit question gates.** SKILL.md and the `flow.py` instruction strings now spell out that Step 1 has three STOP points — G1 (standard + deterministic questions) and G2 (LLM follow-ups) collect the *user's* answers before any draft exists, and G3 approves the finished guide. The agent must ask each question and wait, never auto-pick option defaults or answer on the user's behalf.

### Added
- **`tests/test_harness_flow.py`.** Offline coverage for the harness orchestration that previously lived untested in markdown: the style-guide and glossary prepare/commit beats (with stubbed agent drafts), malformed-draft rejection (fails loudly, writes nothing), difficulty scoring, and the `translate` fail-closed-without-`--yes` guard.

## [0.17.1.0] - 2026-06-09

### Changed
- **translate-harness skill: difficulty-aware chunking.** The `translate-harness` SKILL.md now wires the difficulty scorer into the interactive pipeline. Step 0 runs ingest + split only (chunking deferred), Step 3a scores difficulty with `--force` after the glossary is approved (so glossary terms are excluded from the lexical-rarity signal), and Step 3b chunks at the scorer's suggested target size. The pipeline diagram is updated to show the new ordering: glossary → score difficulty → chunk.
- Step 0 ingest+split uses an inline Python helper instead of the `--start-stage ingest` CLI flag, matching the actual API (no "stop after split" flag exists).
- `translate_book.py` invocations in Step 3 and Step 4 updated to use the `--project-dir` flag consistently.

## [0.17.0.0] - 2026-06-09

### Added
- **Translation difficulty scoring.** Click "Analyze difficulty" on the Stage 3 dashboard to score the book and each chapter for EN→ES translation difficulty. Two signals: long-tail sentence length (dense sentences surface subordinate clauses that LLMs mangle) and lexical rarity (Zipf frequency via `wordfreq`, glossary terms excluded so recurring proper names don't inflate the score). Results show as easy/med/hard colour badges on each chapter card.
- **"Suggest" button per chapter.** Each difficulty badge includes a "Suggest" link that fills the chapter's Target input with the difficulty-derived recommendation (harder chapters → smaller chunks). Nothing is applied until you click Rechunk — the suggestion only fills the input.
- **Book-level difficulty summary.** An overall book difficulty badge appears above the chapter list after scoring, with a tooltip breaking down length and rarity sub-scores.
- **Difficulty manifest caching.** Scores are cached to `{project}/difficulty.json` and reused until the source mtime changes. Pass `?force=1` (or `true`/`yes`, case-insensitive) to re-score unconditionally.
- **`scripts/score_difficulty.py` CLI.** Score any project from the command line: `python scripts/score_difficulty.py <project-id>`. Prints a chapter table with difficulty, length score, rarity, and suggested target. Add `--json` for machine-readable output and `--force` to bypass the cache.

### Fixed
- `_safe_id` input validation switched from a blocklist (`..`, `/`, `\`) to an allowlist (`[A-Za-z0-9_-]+`), closing a Windows drive-relative path escape via project IDs containing `:`.
- Difficulty manifest writes are now atomic (`os.replace(tmp, dest)`) — no torn JSON if two requests score simultaneously.
- Chapters whose English source is unavailable (e.g. the `chapters/` file was overwritten with translated text before chunking) are now skipped with a warning rather than scored using the translated Spanish text, which produced misleading difficulty badges.
- `GET /api/project/<id>/difficulty` 500 responses no longer include the raw exception message; the internal detail is logged server-side only.

## [0.16.0.0] - 2026-06-08

### Added
- **Per-chapter chunk target overrides.** Each chapter card in the Stage 3 dashboard now has a Target field. Set a different word-count target for a single chapter (dense chapters, opening chapters, etc.) and click its Rechunk button — all other chapters stay at the global default. Clear the field and rechunk to revert the chapter back to the default.
- **Ratio-based chunk bounds.** The Advanced section replaces the old absolute Min/Max size inputs with Min Ratio and Max Ratio fields (defaults: 0.25 and 1.5). Bounds are derived from the target on every chunk run, so the Advanced section rarely needs touching. The historical 500/3000 bounds are reproduced exactly at target 2000 with the defaults.
- **`ChunkingConfig` gains `min_ratio` and `max_ratio` fields** (Pydantic, `gt=0.0`, defaults 0.25/1.5). Legacy project configs are back-filled automatically on first status load and persisted to disk.
- **Weighted-DP chunker scaffold.** `chunk_chapter` accepts an optional `para_weights` list for paragraph-density-aware sizing (Phase 2). Phase 1 always passes `None` (uniform sizing); the infrastructure is in place for a future paragraph-scorer to provide weights without changing the API.

### Changed
- The `chunk-all` API now accepts `{ "default": {...}, "chapters": {"<id>": {"target_size": N}} }`. The old flat `{ "target_size": N, ... }` shape is still accepted for backwards compatibility.
- The `rechunk` API now accepts only `{ "target_size": N }` per-chapter; global overlap/bounds come from the persisted default config rather than being re-sent each time.
- The `/api/project/<id>/status` response now includes `chunk_target_override` (integer or null) on each chapter entry, and `min_ratio`/`max_ratio` on `chunking_config`.

### Fixed
- Non-finite or inverted ratio values (`max_ratio ≤ min_ratio`, `inf`, `NaN`) now raise a `ValueError` rather than silently producing degenerate chunk bounds that get persisted.
- Non-dict `default` or `chapters` fields in the chunk-all payload no longer cause an `AttributeError` — they are coerced to `{}`.
- Persistence failures in chunking config helpers now log a warning instead of swallowing the error silently.

## [0.15.2.0] - 2026-06-07

### Changed
- **EPUB now built from translated chapters only — everywhere.** The CLI (`scripts/build_epub.py`), the translate pipeline (`stage_epub`), and the web UI (`build_epub_route`) all share a single `build_epub_from_chunks` helper that discovers fully-translated chapters from chunk JSON files, combines them, and builds the EPUB from that set only. Partially-translated chapters are skipped rather than included as English. The translate-harness Step 5 is now a one-line `build_epub.py` call instead of a 30-line inline snippet.
- `scripts/build_epub.py` reports included and skipped chapter lists after each build, and accepts `--chapters-dir` as a legacy escape hatch for pre-chunk workflows.

### Fixed
- Partial translations no longer produce mixed-language EPUBs from the CLI or pipeline; untranslated chapters are excluded automatically.
- `build_epub_route` now returns HTTP 400 (with a clear message) when no chapters are fully translated, 500 when a chunk file is corrupt (previously `json.JSONDecodeError` was incorrectly caught as a 400 client error).
- EPUB filename generation in the web route falls back to `project_id + ".epub"` when the user-supplied title resolves to an empty `Path.name` (e.g. a title of `/`).
- `build_epub_from_chunks` handles a missing `chunks/` directory gracefully (returns empty, raises `ValueError`) on all platforms, rather than letting `OSError` escape on Linux.
- `translate_book.stage_epub` now records `epub_included_chapters` and `epub_skipped_chapters` in pipeline state.

## [0.15.1.0] - 2026-06-07

### Changed
- **Cost gate simplified** — `--cost-only` is now a pure estimator that always exits cleanly before any prompt or spend, regardless of the estimated amount. The old `--cost-limit` threshold flag is removed; the translate-harness skill now uses `--yes` to record the human's explicit approval from its own AskUserQuestion gate before passing it to the non-interactive run.

### Fixed
- Non-interactive translate runs without `--yes` now exit with code 1 and a recovery message ("re-run with --yes once you've approved the estimate") instead of silently deadlocking on `input()`.
- translate-harness SKILL.md updated to reflect the new `--cost-only` / `--yes` API; removed the `--cost-limit 999999` workaround that was required in v0.15.0.0.

## [0.15.0.0] - 2026-06-06

### Added
- **translate-harness skill** — translate a book conversationally without copy-pasting into an external chat. The agent drafts the style guide and glossary in-conversation, pauses for your approval at each stage, then runs the deterministic pipeline (chunk → translate → combine → EPUB). Each approval gate authorizes only its own stage; approving the glossary never triggers translation.
- **Cost gate** — before any API spending, the skill runs `--cost-only` to produce the estimate, presents it via AskUserQuestion, and ends its turn. Translation only starts with `--yes` after you explicitly approve in a separate turn — no earlier approval (style guide, glossary, chunk) can authorize it.
- **Validation guard (`src/harness_guard.py`)** — every agent-produced artifact (glossary proposals, style.json, glossary.json, chunk files) passes a validation guard before reaching the pipeline. Malformed drafts raise a clear re-draft-friendly error instead of poisoning the run with a KeyError or silent schema mismatch.
- **Pipeline spine tests** — offline tests cover all validation guard branches and the full chunk → translate → combine → EPUB path with a stubbed LLM seam. Dedicated tests assert the approved path, `--cost-only`, and unapproved non-interactive runs never deadlock by calling `input()`.
- **Skill tracked in version control** — `.gitignore` now tracks `.claude/skills/` so the translate-harness skill ships with the repo.

### Fixed
- `guard_glossary_proposals` no longer raises `AttributeError` when an LLM produces a numeric `translation` value; it coerces to string before validation.
- `scripts/translate_book.py` now treats `--cost-only` as a pure estimator that exits before confirmation, and unapproved non-interactive translation exits with instructions to re-run using `--yes` after approval.
- Intermediate harness state (`.tmp/` files) is cleared at Step 0 to prevent stale data from a prior session contaminating the next run.

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
