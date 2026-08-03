# Tailored LLM Judges

A framework for small, single-purpose LLM evaluators ("judges") that you can run
independently, as a suite, or from the **judge-review** skill. The first judge
is a **dialogue-compliance checker** that verifies a Spanish translation follows
the house dialogue rules in `prompts/dialogue.txt` (the rules document) via
the judge prompt template `prompts/judge_dialogue.txt`.

The second is the **address judge** (`address`), which checks usted/tú (formal
vs. informal address) against a **per-book** `projects/<slug>/address_map.json`
rather than a universal rules file — see [ADDRESS_JUDGE.md](ADDRESS_JUDGE.md). It
is the reference for a judge whose book-specific expectations are injected via
`context` (here, the CLI loads the map), while a universal rubric
(`prompts/address_forms.txt`) stays in the prompt.

This is distinct from the model-comparison **LLM judge** documented in
[LLM_JUDGE_EVALUATOR.md](LLM_JUDGE_EVALUATOR.md): that one answers "is model A
better than model B?"; tailored judges answer "does *this* translation comply
with *this* rule?" and write findings back into the per-chunk evaluation store.

## Two kinds of judge

- **Verdict** (implemented): scores / flags a target and returns an
  `EvalResult` (issues + optional 0–1 score). Reuses the existing
  `evaluations/<chunk>.json` persistence, the dashboard badges, and the
  `_feedback.jsonl` loop.
- **Corrector** (designed-for, not yet built): proposes replacement text. The
  spec leaves a `kind="corrector"` slot; `src/retranslator.py` is the existing
  prototype of that shape.

Judges deliberately do **not** subclass `BaseEvaluator` — that base is
chunk-scoped and lives in the cheap/deterministic coded-evaluator run path.
Judges call the LLM, are cost-gated and reproducibility-locked, and live in
their own registry, but they emit the same `EvalResult` so persistence, badges,
and feedback work unchanged.

## Two backends

A judge runs one of two interchangeable ways (the same split as translate-harness):

- **API backend** — `runner.run_judge_suite`, exposed by `run_judges.py run`.
  Calls the LLM directly behind a dollar cost gate. Metered, needs an API key.
- **Subagent backend** — `subagent.prepare` → spawn `judge-worker` subagents →
  `subagent.commit`, exposed by `run_judges.py prepare` / `commit`. Renders each
  judge's prompt to a file for a spawned worker, then collects + parses the JSON
  verdict. **Zero API spend**; runs on the session. The gate is a usage check
  before spawning N workers, not dollars.
- **Headless fan-out (opt-in)** — `run_judges.py fanout` after `prepare`, then
  `commit`. Same draft/commit seam as Task workers, but the harness shells out
  to a logged-in CLI (`claude -p` or `cursor-agent -p`) with `--cli {claude,cursor}`.
  Cursor uses subscription auth (`cursor-agent login`); no `CURSOR_API_KEY` — now
  enforced by the launcher's env scrub rather than by convention. The shell-out
  runs with every metered credential stripped from the child environment, and on
  the Claude profile a `claude auth status` preflight blocks the wave unless a
  subscription is confirmed (`docs/LLM_PROVIDERS.md`).
  Task workers remain Claude-only.

The two share one seam: every judge implements `build_prompt(target, context)` and
`parse_response(target, raw, context)` on the `Judge` base. The API `run()` does
*build → call LLM → parse*; the subagent path renders the same `build_prompt` output
to a file and runs the same `parse_response` on the worker's draft. So the prompt is
byte-identical and the persisted `EvalResult` is the same whichever backend ran — the
run header records `backend` (`"api"` | `"subagent"`) and, for the subagent path,
`worker_model`. `parse_response` raises `JudgeParseError` on unparseable output so the
API path can retry while the subagent `commit` marks the draft failed for re-spawn.

Subagent files live under `<project>/.harness/judges/` (`<target>.<judge>.prompt.txt`,
`.draft.json`, `manifest.json`, `usage.jsonl`). The `judge-worker` agent is
`.claude/agents/judge-worker.md` (Read+Write only, `model: sonnet`).

### What a headless wave cost

`fanout` returns a `usage` rollup — `input`/`output`, `cache_creation` vs
`cache_read`, `prompt_sent` (the judging content) and `overhead` /
`overhead_ratio` (everything billed beyond it, i.e. the context each child
process loads before reading anything). Per-job rows go to
`.harness/judges/usage.jsonl` and stay out of the orchestrator's context.

This is `src/harness/usage.py`, shared by all four fan-outs and deliberately
isolated so it can be removed in one commit. It exists because the numbers were
being computed and discarded: the launcher asked for `--output-format text`, and
a 2026-07-30 wave paid a large fixed per-process cost that nothing could report.
`prepare`'s `usage_summary` uses the same log to self-calibrate
`estimated_headless_tokens`, so the usage gate quotes the backend being chosen
rather than the API price of the one declined.

## Layout

```
src/judges/
  base.py            Judge / VerdictJudge / JudgeSpec / JudgeTarget
  llm_io.py          template load/render/hash, JSON extraction + parse, cost
  scope.py           build_targets(project_dir, scope) -> [JudgeTarget]
  registry.py        _JUDGE_REGISTRY + suite resolution
  runner.py          run_judge (isolated) + run_judge_suite (cost gate + header)
  subagent.py        prepare() / commit() — the subagent backend
  dialogue_judge.py  DialogueComplianceJudge
prompts/judge_dialogue.txt
scripts/run_judges.py
.claude/agents/judge-worker.md
.claude/skills/judge-review/SKILL.md
```

## CLI

Three subcommands: `run` (API backend), `prepare` + `commit` (subagent backend).
Each prints exactly one JSON object. Pass `--schema` for the block documenting
every output key — it is opt-in on success (`apply`'s alone is ~910 tokens, and
it used to be re-sent on every call) and automatic on any error.

```bash
# API backend — single judge over a chapter (cost dry-run; refuses to spend over $0.50)
python scripts/run_judges.py run --project understood-betsy \
    --judge dialogue --scope chapter:chapter_03

# API backend — a suite over one chunk, persisting findings to evaluations/<chunk>.json
python scripts/run_judges.py run --project understood-betsy \
    --suite default --scope chunk:chapter_03_chunk_000 --persist --confirm

# Subagent backend — render prompts + manifest (no spend), then commit the workers' drafts
python scripts/run_judges.py prepare --project understood-betsy \
    --judge dialogue --scope chapter:chapter_03 [--worker-model sonnet] [--batch-size 5]
python scripts/run_judges.py commit  --project understood-betsy --persist
```

`run` `status` is `"ok"`, `"cost_exceeded"` (re-run with `--confirm`), or `"error"`.

| Subcommand | Flag | Default | Description |
|---|---|---|---|
| all | `--project` | — | Project id (under `projects/`) or path |
| `run`, `prepare` | `--judge` / `--suite` | — | One required; a judge name or suite name |
| `run`, `prepare` | `--scope` | — | `chunk:<chunk_id>`, `chapter:<chapter_id>` or `book` |
| `run`, `prepare` | `--model` / `--provider` | config | Judge LLM overrides |
| `run` | `--cost-limit` | `0.50` | Max estimated USD before `--confirm` is required |
| `run` | `--confirm` | false | Proceed past the cost gate |
| `prepare` | `--worker-model` | `sonnet` | Tier to pin spawned `judge-worker`s to |
| `prepare` | `--batch-size` | `5` | Recommended workers to spawn per wave |
| `run`, `commit` | `--persist` | false | Write findings into `evaluations/<chunk>.json` |
| `apply` | `--judge` | `dialogue` | Repeatable: both judges in one run, realigned once |
| `apply` | `--scope` | — | Repeatable; same three kinds as above |
| `apply` | `--select` | — | Comma-separated `applicable[].id` / `.qualified_id` to apply |
| `apply` | `--dry-run` | false | Preview the plan, change nothing |
| `apply` | `--rebuild-epub` | false | Rebuild the EPUB after applying |
| `apply` | `--no-realign` | false | Apply, but defer recombine+realign (see `chapters_pending_realign`) |
| `apply` | `--realign-only` | false | Change no text; realign chapters in scope whose alignment is stale |
| all | `--verbose` | false | Enable debug logging |

## Scopes

- `chunk:<chunk_id>` — one chunk.
- `chapter:<chapter_id>` — every translated chunk in the chapter, one target
  each (results stay keyed per chunk so persistence + badges work).
- `book` (or `book:`) — every translated chunk in the project, in reading order.
  Added for `apply`, where a full-book pass otherwise meant one `--scope
  chapter:` flag per chapter built by a shell loop, and a chapter missing from
  that loop silently dropped its findings out of scope. Takes no id — `--project`
  already named the book.

Designed-for but not yet implemented (clear `NotImplementedError`):
`sentences:<chapter>:<es_idx,...>`, `flags:<chapter>` (from `annotations.jsonl`),
`findings:<chapter>:<evaluator>` (from prior `evaluations/*.json`). These build
on `alignments/chapter_XX.json` (es_idx ↔ en/es ↔ chunk_id).

`flags:` stays unimplemented on purpose. Reviewing reader annotations turned out
to need a different target shape (one annotation, not one chunk) and a different
persistence path (the annotation itself, not `evaluations/*.json` and the badges),
so it lives in its own pipeline — see `docs/ANNOTATION_REVIEW.md`. That pipeline
reuses this one's plumbing (`llm_io`, the cache-split marker, the headless wave
launcher) rather than being forced through `JudgeTarget`.

## Suites

A suite is a named list of judges. Built-in: `default = ["dialogue"]` and
`address = ["address"]`. The address judge is deliberately kept out of `default`
because it needs the per-book `address_map.json` prerequisite and is metered — run
it via `--judge address` or `--suite address`. Override or add suites in
`app_config.json`:

```json
{ "judge_suites": { "default": ["dialogue"], "prose": ["dialogue", "address"] } }
```

## Persistence & feedback

With `--persist`, each judge's `EvalResult` is written under
`evaluations/<chunk>.json` → `judges.<judge_name>` (via
`web_ui.evaluations.merge_judge_result`), preserving coded results and the
quality `llm_judge` block. `load_project_summary` folds judge issue counts into
the same per-chunk badge totals, so a judged chunk lights up in the dashboard.

False positives can be recorded with `web_ui.evaluations.append_feedback`
(`feedback_type` ∈ `false_positive` | `bad_message` | `missing_context_gap`),
the same loop the coded evaluators use — useful for tuning a judge's prompt.

`apply` stale-stamps every chunk whose text it rewrote: `stale`, `stale_since`
and `stale_reason` at the **top level** of `evaluations/<chunk>.json`, not inside
`judges.<judge_name>` — a fixed finding must not keep asserting a failure, and
`merge_judge_result` clears the stamp on the next run.

## Crash consistency in `apply`

`apply` finishes each chunk's pre-edit snapshot (`.chunk_edits/`), its edit, its
`corrections_applied.jsonl` rows and its stale stamp **before the next chunk is
touched** — sequential steps, not one atomic write; the snapshot (first) is the
recovery proof if a kill lands mid-sequence. A kill after a chunk finishes
leaves a consistent prefix, and the only thing a finished chunk can still skip
is the per-chapter tail (recombine → realign → EPUB).

Re-running the same `--select` resumes rather than fails. Two independent proofs
are accepted that a selected edit is already ours: a matching audit row, or a
`.chunk_edits/` snapshot holding the excerpt but not the suggestion. The snapshot
is written *before* the chunk is saved and the audit row after, so the snapshot is
the proof that survives a kill — the case where the archive is exactly what is
missing. Rows missing for such an edit are re-appended with `"recovered": true`.

`alignments/<chapter>.json` is written last by `realign_chapter`, so its mtime is
the receipt that the tail ran. An already-applied edit in a chapter whose
alignment is older than its chunks means an earlier run died before realigning,
and the retry finishes it. That same signal is what `--realign-only` acts on, and
what makes `--no-realign` safe to defer.

## Reproducibility & cost

The API backend cost-estimates every suite run up front and gates it by
`--cost-limit`. Judge calls run at `temperature=0`. The run header records each
judge's version, the prompt-template SHA-256, the resolved model/provider, the
`backend`, and the git commit — so a persisted judge result is self-describing and
runs are repeatable. The subagent backend spends no dollars; its gate is the
conversational usage check the skill makes before spawning workers.

## Adding a judge

1. Write a prompt template in `prompts/judge_<name>.txt` (XML-fence the inputs and
   instruct the model to treat tagged content as data, and to return JSON only).
2. Subclass `VerdictJudge` in `src/judges/<name>_judge.py`, set a `JudgeSpec`
   (name, version, kind, template, `output_fields`), and implement the two seam
   methods — this is what gives you **both** backends for free:
   - `prompt_variables()` (or override `build_prompt()`) — the template variables.
   - `parse_response(target, raw, context)` — parse `raw` with
     `llm_io.parse_judge_json` (raising `JudgeParseError` on bad output), map
     findings to `Issue`s, and return `self.make_result(...)`.
   Then `run()` is just *build_prompt → llm_io.call_judge → parse_response* (plus
   any retry); see the dialogue judge.
3. Register it in `src/judges/registry.py` (`_JUDGE_REGISTRY`), and add it to a
   suite if desired.
4. Add a test under `tests/test_judges/` that mocks `llm_io.call_judge` for the API
   path, and (optionally) drives `parse_response` directly for the subagent path.

See `src/judges/dialogue_judge.py` as the reference implementation.
