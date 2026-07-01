---
name: judge-review
description: |
  Run tailored LLM judges over a translated chunk or chapter, relay the findings,
  and (with approval) persist them so the dashboard badges update. v1 ships the
  dialogue-compliance judge, which checks Spanish dialogue formatting against the
  house rules in prompts/dialogue.txt. Two interchangeable backends: an API path
  (metered) and a subagent path (no API spend). Use when asked to "run the dialogue
  judge", "check dialogue compliance", "run the judges on chapter N", or "judge-review".
allowed-tools:
  - Bash
  - Read
  - Task
  - AskUserQuestion
---

# judge-review

Drive the tailored-judge framework as a short conversation. The deterministic
surface is one non-interactive CLI — **`scripts/run_judges.py`** — with three
subcommands that each print JSON. You pick a scope, pick a backend, run a judge
(or suite), relay the findings, and offer to persist them.

## Two backends

Judges run one of two interchangeable ways (the same split as translate-harness):

- **API backend** (`run`) — calls the LLM directly, behind a dollar **cost gate**.
  Fast, needs an API key, spends metered money. This skill never calls an LLM
  itself; the CLI does.
- **Subagent backend** (`prepare` → spawn workers → `commit`) — renders each judge
  prompt to a file, you spawn cheap **`judge-worker`** subagents to answer, then
  collect the drafts. **Zero API spend** (runs on the session). The gate here is a
  **usage** check before spawning N workers, not dollars.

Both backends build the *same* prompt and parse with the *same* parser, so the
findings — and the persisted `evaluations/<chunk>.json` — are identical either way.

## The CLI (read first)

`python scripts/run_judges.py <run|prepare|commit>` is non-interactive and prints
one JSON object with a `_schema` block documenting every key.

**Windows / UTF-8 — force it on every Python you run.** `run_judges.py` reconfigures stdout
to UTF-8, but **your own** ad-hoc `python -c` probes default to the Windows console codepage
and will mojibake every raya (—), guillemet («»), and accent — the exact bytes dialogue
findings quote. Always run diagnostics with `python -X utf8 -c "..."` (or set `PYTHONUTF8=1`
for the session). When reading chunk JSON or `evaluations/*.json` in a probe, open with
`encoding="utf-8"` explicitly; prefer the `Read` tool over piping judge stdout into Python.

Shared flags (`run`, `prepare`):
- `--project <id|path>` — required.
- `--judge <name>` **or** `--suite <name>` (`--judge dialogue`, `--suite default`).
  Judges/suites come from `src/judges/registry.py` + `app_config.json`.
- `--scope chunk:<chunk_id>` or `--scope chapter:<chapter_id>` — required.
- `--model` / `--provider` — judge LLM overrides.

`run` (API backend) adds:
- `--cost-limit <usd>` (default 0.50) and `--confirm`.
- `--persist` — write findings into `evaluations/<chunk>.json` (dashboard badges).

`prepare` (subagent backend) adds:
- `--scope` is **repeatable** here — pass it multiple times to stage several
  chapters into one manifest for a single `commit` (see the multi-chapter note in B).
- `--worker-model <tier>` (default `sonnet`) — pins each spawned `judge-worker`.
- `--batch-size <n>` (default 5) — recommended workers per spawn wave.
- `--keep-drafts` — don't clear existing worker drafts. Re-`prepare` is otherwise
  destructive (it wipes the drafts for the pairs it re-renders).

`commit` (subagent backend) takes only `--project` and `--persist`.

`run` returns `status`:
- `"cost_exceeded"` — it refused to spend. Relay the estimate and **stop**; only
  re-run with `--confirm` after the user approves the cost (see gate below).
- `"ok"` — `results[]` carries per-target findings (issues with severity, message,
  location excerpt, suggestion) plus a `summary` rollup.
- `"error"` — relay the `error` string (bad scope, missing translation, etc.).

## Flow

1. **Pick scope.** If the user didn't name one, ask whether to judge a single chunk
   or a whole chapter, and which (use `AskUserQuestion` only if genuinely ambiguous).
2. **Pick backend.** If the user didn't say, ask: **API** (spends metered $, behind
   the cost gate) or **subagent** (no API spend, uses this session, spawns workers)?
   Default to **API**. Then follow the matching branch below.

### A. API backend

3a. **Dry-run for cost.** Run `run` WITHOUT `--confirm` first. If it returns
   `cost_exceeded`, relay the dollar estimate and ask for approval. **Cost approval
   is a hard stop** — never pass `--confirm` until the user approves in a separate turn.
4a. **Run.** Re-run `run` with `--confirm` (and `--persist` if saving). Relay the
   findings grouped by target: the compliance score, the count of issues, and each
   issue's rule + excerpt + suggestion.

```bash
python scripts/run_judges.py run --project understood-betsy \
    --judge dialogue --scope chapter:chapter_03
python scripts/run_judges.py run --project understood-betsy \
    --suite default --scope chunk:chapter_03_chunk_000 --persist --confirm
```

### B. Subagent backend

3b. **Prepare.** Render the prompts + manifest (no spend). For a multi-chapter
request, stage **every** chapter in one `prepare` by repeating `--scope` — they
land in one manifest, one `commit` collects them all, and `usage_summary` is a
single rollup (no manual summing across calls):
```bash
python scripts/run_judges.py prepare --project understood-betsy --judge dialogue \
    --scope chapter:chapter_05 --scope chapter:chapter_06 [--worker-model sonnet] [--batch-size 5]
```
Relay `usage_summary` (pairs to judge, worker_model, batch_size; `estimated_api_cost`
is the API-equivalent price, shown for context — nothing is spent). The **usage gate**
is the subagent analog of the cost gate: no dollars, but spawning N workers consumes
real session/rate usage. Get approval in a separate turn before spawning.

**Re-`prepare` is destructive.** It clears the drafts for the pairs it re-renders,
so it must never run while you have **uncommitted** worker drafts in flight — that
throws away completed work and forces a re-spawn. Prepare the whole request once
(all `--scope`s together); if you truly must re-prepare with good drafts present,
pass `--keep-drafts`. Don't re-prepare just to "recover" a manifest — stage
everything up front so you never need to.

4b. **Spawn workers.** For each manifest entry, spawn one worker with the **Task** tool:
`subagent_type: judge-worker`, `model:` = the manifest's `worker_model`, in bounded
batches of `batch_size`. Tell each worker its `prompt_path` and `draft_path`: read the
prompt, write ONLY the JSON verdict to the draft, reply `done <target_id>`. On a 529
(overloaded) throttle the batch toward ~1 and continue.

5b. **Commit.** Collect + parse the drafts (and `--persist` if saving):
```bash
python scripts/run_judges.py commit --project understood-betsy --persist
```
Relay `committed` / `failed` / `missing`. **Re-spawn** any `failed` (bad/no JSON) or
`missing` (no draft) entries — same prompt_path/draft_path — then re-run `commit`. Cap
re-spawns at ~3 per entry, then surface for manual review. Then relay findings the same
way the API branch does.

### Feedback (optional, either backend)

If the user says a finding is a false positive, record it so the judge can be tuned
later: `append_feedback(project_dir, chunk_id, "dialogue", issue_index,
"false_positive", note=...)` from `web_ui/evaluations.py`. Allowed types:
`false_positive`, `bad_message`, `missing_context_gap`.

## Notes

- The dialogue judge reads the rules from `prompts/dialogue.txt` automatically.
- Only chunk-scoped results persist (one file per chunk). A `chapter:` scope expands
  to one chunk-target per chunk, so persistence + badges work for both backends.
- Both backends persist via `merge_judge_result`, so the dashboard badge lights up
  identically regardless of which one ran. The persisted result records the backend
  (and `worker_model` for the subagent path) in its metadata / run header.
- Adding a judge later: write a prompt template + a `JudgeSpec`, implement
  `build_prompt` + `parse_response` (you get both backends for free), and register it
  in `src/judges/registry.py`. See `docs/JUDGES_FRAMEWORK.md`.
