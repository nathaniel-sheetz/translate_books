# Tailored LLM Judges

A framework for small, single-purpose LLM evaluators ("judges") that you can run
independently, as a suite, or from the **judge-review** skill. The first judge
is a **dialogue-compliance checker** that verifies a Spanish translation follows
the house dialogue rules in `prompts/dialogue.txt`.

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

## Layout

```
src/judges/
  base.py            Judge / VerdictJudge / JudgeSpec / JudgeTarget
  llm_io.py          template load/render/hash, JSON extraction + parse, cost
  scope.py           build_targets(project_dir, scope) -> [JudgeTarget]
  registry.py        _JUDGE_REGISTRY + suite resolution
  runner.py          run_judge (isolated) + run_judge_suite (cost gate + header)
  dialogue_judge.py  DialogueComplianceJudge
prompts/judge_dialogue.txt
scripts/run_judges.py
.claude/skills/judge-review/SKILL.md
```

## CLI

```bash
# Single judge over a whole chapter (cost dry-run; refuses to spend over $0.50)
python scripts/run_judges.py --project understood-betsy \
    --judge dialogue --scope chapter:chapter_03

# A suite over one chunk, persisting findings to evaluations/<chunk>.json
python scripts/run_judges.py --project understood-betsy \
    --suite default --scope chunk:chapter_03_chunk_000 --persist --confirm
```

The command prints one JSON object with a `_schema` block. `status` is `"ok"`,
`"cost_exceeded"` (re-run with `--confirm`), or `"error"`.

| Flag | Default | Description |
|---|---|---|
| `--project` | — | Project id (under `projects/`) or path |
| `--judge` / `--suite` | — | One required; a judge name or a suite name |
| `--scope` | — | `chunk:<chunk_id>` or `chapter:<chapter_id>` |
| `--model` / `--provider` | config | Judge LLM overrides |
| `--cost-limit` | `0.50` | Max estimated USD before `--confirm` is required |
| `--confirm` | false | Proceed past the cost gate |
| `--persist` | false | Write findings into `evaluations/<chunk>.json` |

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

Every suite run is cost-estimated up front and gated by `--cost-limit`. Judge
calls run at `temperature=0`. The run header records each judge's version, the
prompt-template SHA-256, the resolved model/provider, and the git commit — so a
persisted judge result is self-describing and runs are repeatable.

## Adding a judge

1. Write a prompt template in `prompts/judge_<name>.txt` (XML-fence the inputs
   and instruct the model to treat tagged content as data).
2. Subclass `VerdictJudge` in `src/judges/<name>_judge.py`, set a `JudgeSpec`
   (name, version, kind, template, `output_fields`), and implement `run()`:
   render the prompt with `llm_io`, call `llm_io.call_judge`, parse with
   `llm_io.parse_judge_json`, map findings to `Issue`s, return
   `self.make_result(...)`.
3. Register it in `src/judges/registry.py` (`_JUDGE_REGISTRY`), and add it to a
   suite if desired.
4. Add a test under `tests/test_judges/` that mocks `llm_io.call_judge`.

See `src/judges/dialogue_judge.py` as the reference implementation.
