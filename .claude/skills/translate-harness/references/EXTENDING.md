# Extending translate-harness

Authoring guide for adding a stage, translation backend, content processor, or
judge. Recipes point at real code; copy the nearest existing surface rather than
inventing a new shape.

## 1. New pipeline stage / reference file

1. Add `references/<x>.md` with a self-contained header + the beat prose
   (commands, STOP gates, what to Read from `last_output.json`).
2. Add a ROUTER row in the core `SKILL.md` (signal → load that file).
3. If it needs a new CLI command: add a subparser in `scripts/harness.py` and a
   function in `src/harness/flow.py`, then register an `OUTPUT_SCHEMAS` entry
   (same as today — only the transport changed: the schema is written to
   `.harness/last_output_schema.json` by default, inlined on `--schema` / errors).
4. Persist any once-per-book decision via
   `python scripts/harness.py config-set --project <p> --key <k> --value <v>`
   (thin wrapper over `state.load_config` / `state.save_config` — no new state
   machinery). Register the key in `flow._CONFIG_SET_KEYS` with the frozenset of
   words it accepts. If the value is not a string on disk, add a coercion table in
   `flow._CONFIG_SET_COERCE` keyed the same way (the prompt-prefix opt-ins do this:
   `on|true|off|false|auto` in, `True|False|None` out) so the file looks identical
   whether it was written by `config-set` or by a `setup` flag. Remember that
   `state.save_config` drops `None`, so a `None` value resolves to the key's
   `DEFAULTS` entry — make that the same "unset" meaning.

**Template to copy:** the footnotes surface —
`scripts/harness.py` `footnotes {translate, apply, drop}` →
`src/harness/flow.py` footnotes helpers → `references/footnotes.md`.

## 2. New content processor (e.g. poetry)

Footnotes is the template end-to-end:

| Layer | Where | What to add |
|---|---|---|
| CLI group | `scripts/harness.py` | `sub.add_parser("<proc>")` + actions |
| Pure logic | `src/<module>.py` | detect / transform / apply helpers |
| Flow | `src/harness/flow.py` | thin wrappers that call the module |
| Skill beat | `references/<proc>.md` | keep/drop (or equivalent) decision + commands |
| Setup ask | `references/setup.md` | once-per-book decision + `config-set` |
| Detection | `src/text_feature_detector.py` | one function + one line in `DETECTORS` (~line 824) |
| Split patterns | `src/split_patterns.json` | data-only, if headings are involved |

**Known rough edge (documented, not fixed here):** verse *rendering* has no
registry. `is_verse_block` (`src/utils/verse.py`) is called directly from
`src/epub_builder.py` and `src/sentence_aligner.py`, so a render-affecting
processor must touch both consumers. A future registry-unification is the clean
path; do not half-fix it in a one-off processor PR.

## 3. New translation backend

1. Add `references/translate-<backend>.md` (commands, gates, spawn/commit shape).
2. Add a ROUTER row keyed on `config.backend == "<backend>"`.
3. Persist the choice: `config-set --key backend --value <backend>` (and
   `log-event --event backend …`).
4. Add harness commands as needed (`translate-prepare` / `translate-fanout`
   pattern for worker-style backends; a single `translate --yes` for metered).
5. Teach `resolve_backend` in `src/harness/flow.py` about the new name (keep it
   in `_BACKENDS`) so footnote translation carries the choice forward.

Shared seam today: `build_translation_prompt` + `apply_translation` — no
`TranslationBackend` Protocol. Prefer that over a class hierarchy unless the
new backend truly cannot share the stamp path.

### 3a. New CLI family inside `headless` (preferred for subscription CLIs)

When the new driver is still "fan out prompts → write drafts → commit" and only
the launcher binary/flags differ (Codex, Gemini CLI, …), **do not** add a fourth
`backend` value. Keep `backend=headless` and extend the CLI selector:

1. Add a profile in `src/harness/headless.py` (`_build_cmd` / `_extract_output` /
   default binary / not-found hint). Fold any Claude-only
   `--system-prompt-file` into stdin when the new CLI lacks that flag.
2. **Add the family's credential and routing env vars to `_SCRUB_NAMES` (or
   `_SCRUB_PREFIXES`) in `src/harness/headless.py`.** A new family with an
   unscrubbed API key silently re-opens metered billing — that is the exact bug
   this enforcement exists to prevent. The scrub list is a union across all
   families, so just add yours to it.
3. **Add an entry to `_AUTH_PROBE_ARGV`:** either the CLI's verified auth-status
   argv plus a branch in `subscription_auth_error`, or `None` with a comment
   naming why no probe exists (the Cursor precedent). Do not guess at a command —
   a wrong one hard-fails a working setup, and the preflight fails closed.
4. Add the name to `_CONFIG_SET_KEYS["headless_cli"]` and `DEFAULTS["headless_cli"]`.
5. Thread `--cli` / `--cli-bin` (already on `translate-fanout`, footnotes
   translate, and `run_judges.py fanout`) — no new fan-out / commit commands.
6. Document the profile in `references/translate-workers.md`,
   `judge-review/SKILL.md`, and the enforcement table in `docs/LLM_PROVIDERS.md`.
   Task-worker paths stay Claude-only (the Task tool spawns Claude subagents).

`tests/test_spawn_boundary.py` holds an inventory of every process spawn in the
repo, so the new family *must* go through `run_headless_wave` — a direct
`subprocess.run` of the CLI fails CI.

Recipe used for Cursor: `headless_cli=cursor` → `cursor-agent -p --trust --mode ask`.

## 4. New review type (judge)

Lives in the **judge-review** skill, not here. Cross-link; do not duplicate.

Recipe (see `docs/JUDGES_FRAMEWORK.md`):

1. Subclass `VerdictJudge` (or the appropriate base) + a `JudgeSpec`.
2. One line in `_JUDGE_REGISTRY` (`src/judges/registry.py`).
3. Optional suite in `app_config.json` `judge_suites`.
4. Per-book context wiring in `scripts/run_judges.py` if the judge needs a
   per-book input (e.g. address map).

From translate-harness, the incremental loop in `references/reviews.md` just
invokes judge-review after align.

## Checklist before merging an extension

- [ ] Core `SKILL.md` ROUTER row points at a real `references/*.md` file
- [ ] Reference file has no "see Step N above" — only file pointers
- [ ] Once-per-book choices written with `config-set` (and echoed by `status`
      via `backend` / `suggested_reference` when relevant)
- [ ] New CLI command has an `OUTPUT_SCHEMAS` entry
- [ ] Pipeline Python behavior unchanged unless the extension intentionally
      adds a stage (this skill refactor itself is docs-only for the pipeline)
