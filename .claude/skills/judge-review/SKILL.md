---
name: judge-review
description: |
  Run tailored LLM judges over a translated chunk or chapter, relay the findings,
  and (with approval) persist them so the dashboard badges update. v1 ships the
  dialogue-compliance judge, which checks Spanish dialogue formatting against the
  house rules in prompts/dialogue.txt. Use when asked to "run the dialogue judge",
  "check dialogue compliance", "run the judges on chapter N", or "judge-review".
allowed-tools:
  - Bash
  - Read
  - AskUserQuestion
---

# judge-review

Drive the tailored-judge framework as a short conversation. The deterministic
surface is one non-interactive CLI — **`scripts/run_judges.py`** — that prints
JSON. You pick a scope, run a judge (or suite), relay the findings, and offer to
persist them.

This skill never calls an LLM API itself; the CLI does, behind a cost gate.

## The CLI (read first)

`python scripts/run_judges.py` is non-interactive and prints one JSON object
with a `_schema` block documenting every key. Core flags:

- `--project <id|path>` — required.
- `--judge <name>` **or** `--suite <name>` — what to run (`--judge dialogue`,
  or `--suite default`). Judges/suites come from `src/judges/registry.py` +
  `app_config.json`'s `judge_suites`.
- `--scope chunk:<chunk_id>` or `--scope chapter:<chapter_id>` — required.
- `--cost-limit <usd>` (default 0.50) and `--confirm`.
- `--persist` — write findings into `evaluations/<chunk>.json` (dashboard badges).

The CLI returns `status`:
- `"cost_exceeded"` — it refused to spend. Relay the estimate and **stop**;
  only re-run with `--confirm` after the user approves the cost (see gate below).
- `"ok"` — `results[]` carries per-target findings (issues with severity,
  message, location excerpt, suggestion) plus a `summary` rollup.
- `"error"` — relay the `error` string (bad scope, missing translation, etc.).

## Flow

1. **Pick scope.** If the user didn't name one, ask whether to judge a single
   chunk or a whole chapter, and which (use `AskUserQuestion` only if it's
   genuinely ambiguous; otherwise use what they said).
2. **Dry-run for cost.** Run the CLI WITHOUT `--confirm` first. If it returns
   `cost_exceeded`, relay the dollar estimate and ask for approval.
   **Cost approval is a hard stop** — never pass `--confirm` until the user
   approves in a separate turn.
3. **Run.** Re-run with `--confirm` (and `--persist` if the user wants the
   findings saved). Relay the findings grouped by target: for each, the
   compliance score, the count of issues, and each issue's rule + excerpt +
   suggestion.
4. **Feedback (optional).** If the user says a finding is a false positive,
   record it so the judge can be tuned later:
   `append_feedback(project_dir, chunk_id, "dialogue", issue_index,
   "false_positive", note=...)` from `web_ui/evaluations.py`. Allowed types:
   `false_positive`, `bad_message`, `missing_context_gap`.

## Notes

- The dialogue judge reads the rules from `prompts/dialogue.txt` automatically.
- Only chunk-scoped results persist (one file per chunk). A `chapter:` scope
  expands to one chunk-target per chunk, so persistence + badges still work.
- Adding a judge later: write a prompt template + a `JudgeSpec`, register it in
  `src/judges/registry.py`. See `docs/JUDGES_FRAMEWORK.md`.
