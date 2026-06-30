# Tailored LLM Judges

A framework for small, single-purpose LLM evaluators ("judges") that you can run
independently, as a suite, or from the **judge-review** skill. The first judge
is a **dialogue-compliance checker** that verifies a Spanish translation follows
the house dialogue rules in `prompts/dialogue.txt` (the rules document) via
the judge prompt template `prompts/judge_dialogue.txt`.

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

The two share one seam: every judge implements `build_prompt(target, context)` and
`parse_response(target, raw, context)` on the `Judge` base. The API `run()` does
*build → call LLM → parse*; the subagent path renders the same `build_prompt` output
to a file and runs the same `parse_response` on the worker's draft. So the prompt is
byte-identical and the persisted `EvalResult` is the same whichever backend ran — the
run header records `backend` (`"api"` | `"subagent"`) and, for the subagent path,
`worker_model`. `parse_response` raises `JudgeParseError` on unparseable output so the
API path can retry while the subagent `commit` marks the draft failed for re-spawn.

Subagent files live under `<project>/.harness/judges/` (`<target>.<judge>.prompt.txt`,
`.draft.json`, `manifest.json`). The `judge-worker` agent is
`.claude/agents/judge-worker.md` (Read+Write only, `model: sonnet`).

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
Each prints one JSON object with a `_schema` block.

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
| `run`, `prepare` | `--scope` | — | `chunk:<chunk_id>` or `chapter:<chapter_id>` |
| `run`, `prepare` | `--model` / `--provider` | config | Judge LLM overrides |
| `run` | `--cost-limit` | `0.50` | Max estimated USD before `--confirm` is required |
| `run` | `--confirm` | false | Proceed past the cost gate |
| `prepare` | `--worker-model` | `sonnet` | Tier to pin spawned `judge-worker`s to |
| `prepare` | `--batch-size` | `5` | Recommended workers to spawn per wave |
| `run`, `commit` | `--persist` | false | Write findings into `evaluations/<chunk>.json` |
| all | `--verbose` | false | Enable debug logging |

## Scopes

- `chunk:<chunk_id>` — one chunk.
- `chapter:<chapter_id>` — every translated chunk in the chapter, one target
  each (results stay keyed per chunk so persistence + badges work).

Designed-for but not yet implemented (clear `NotImplementedError`):
`sentences:<chapter>:<es_idx,...>`, `flags:<chapter>` (from `annotations.jsonl`),
`findings:<chapter>:<evaluator>` (from prior `evaluations/*.json`). These build
on `alignments/chapter_XX.json` (es_idx ↔ en/es ↔ chunk_id).

## Suites

A suite is a named list of judges. Built-in: `default = ["dialogue"]`. Override
or add suites in `app_config.json`:

```json
{ "judge_suites": { "default": ["dialogue"], "prose": ["dialogue"] } }
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
