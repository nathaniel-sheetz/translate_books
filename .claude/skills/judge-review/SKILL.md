---
name: judge-review
description: |
  Run tailored LLM judges over a translated chunk or chapter, relay the findings,
  and (with approval) persist them so the dashboard badges update. Ships the
  dialogue-compliance judge (Spanish dialogue formatting vs. prompts/dialogue.txt)
  and the address judge (usted/tú vs. the per-book address_map.json). Two
  interchangeable backends: an API path (metered) and a subagent path (no API spend).
  Use when asked to "run the dialogue judge", "check dialogue compliance", "run the
  usted/tú judge", "check forms of address", "run the judges on chapter N", or "judge-review".
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

`python scripts/run_judges.py <run|prepare|commit|apply>` is non-interactive and prints
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
- `--batch-size <n>` (default 5) — workers per spawn wave; wait for the wave to
  finish before launching the next (see 4b).
- `--targets-per-worker <n>` (default 1) — group up to N **low-dialogue-density**
  targets into one worker prompt to amortize per-worker overhead (fewer, cheaper
  spawns). Dialogue-dense chunks are always judged solo. A grouped manifest entry
  has a `batch_id` + `members[]` instead of a top-level `target_id`; `commit`
  splits the one draft back out per member. Leave at 1 for recovery re-prepares
  (see 5b) so a bad chunk never drags its group-mates. `usage_summary` reports
  `workers` (spawns) alongside `pairs` (target×judge units).
- `--keep-drafts` — don't clear existing worker drafts. Re-`prepare` is otherwise
  destructive (it wipes the drafts for the entries it re-renders).

`commit` (subagent backend) takes only `--project` and `--persist`.

`apply` (turn approved findings into chunk edits) adds:
- `--judge <name>` (default `dialogue`) — whose **persisted** findings to consider.
- `--scope chunk:<id>` / `--scope chapter:<id>` — **repeatable**.
- `--select <id,id,...>` — the `applicable[].id`s to apply. **Omit for a plan-only dry-run.**
- `--rebuild-epub` — rebuild the EPUB after applying (recombine + realign always run).
- `--dry-run` — force plan mode even when `--select` is given.

`apply` reads the findings you already persisted, so run/commit with `--persist` **first**.

`run` returns `status`:
- `"cost_exceeded"` — it refused to spend. Relay the estimate and **stop**; only
  re-run with `--confirm` after the user approves the cost (see gate below).
- `"ok"` — `results[]` carries per-target findings (issues with severity, message,
  location excerpt, suggestion) plus a `summary` rollup.
- `"error"` — relay the `error` string (bad scope, missing translation, etc.).

## Available judges

- **`dialogue`** — Spanish dialogue formatting vs. the house rules in
  `prompts/dialogue.txt`. No setup; reads its rules automatically.
- **`address`** — usted/tú (formal vs. informal address) vs. the **per-book
  address map**. **Requires setup** (see below). Run it with `--judge address`
  (or `--suite address`). It is *not* in the `default` suite.

### The `address` judge needs a map first

The correct usted/tú for a line is book-specific, so the address judge checks
against `projects/<slug>/address_map.json` — a reviewed map of who addresses whom
with which form, including public/private and story-stage differences. The CLI
loads it automatically; if it's missing, `run`/`prepare` return
`status: "error"` telling you to build it.

**Before running the address judge, check for the map** (`Read` or
`ls projects/<slug>/address_map.json`). If it's absent, build it with the
translate-harness address-map beat (a short, approval-gated drafting flow) and
only then run the judge:

```bash
python scripts/harness.py address-map prepare --project understood-betsy   # samples dialogue-heavy chapters, renders a prompt
# (draft the map JSON to the printed draft_path, refine with the user)
python scripts/harness.py address-map commit  --project understood-betsy   # validates + writes address_map.json
```

Everything else (scope, backends, persistence, apply) works exactly as for the
dialogue judge — just pass `--judge address`.

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
single rollup (no manual summing across calls).

**First, pick a worker-grouping mode** (subagent-only — the API path always judges
one target per call). Offer the choice with `AskUserQuestion` unless the user already
said which they want:
- **Conservative — one chunk per worker** (`--targets-per-worker 1`, the default).
  Every chunk is judged in full isolation — the known-good path. Most spawns, so the
  highest session/rate usage. Recommend this when in doubt.
- **Grouped — up to 3 chunks per worker** (`--targets-per-worker 3`, cheaper). Packs
  up to three *low-dialogue-density* chunks into one worker prompt (dialogue-dense
  chunks are always judged solo), amortizing per-worker overhead → **fewer, cheaper
  spawns**. In testing the findings track the solo path closely, but the full A/B
  quality gate isn't cleared yet — present it as the cheaper option with a small,
  bounded quality risk, not a free win.

Then run `prepare`, passing the chosen `--targets-per-worker` (omit it for conservative):
```bash
python scripts/run_judges.py prepare --project understood-betsy --judge dialogue \
    --scope chapter:chapter_05 --scope chapter:chapter_06 \
    [--targets-per-worker 3] [--worker-model sonnet] [--batch-size 5]
```
Relay `usage_summary` (pairs to judge, worker_model, batch_size; `estimated_api_cost`
is the API-equivalent price, shown for context — nothing is spent). Under grouping,
`usage_summary.workers` (actual spawns) is fewer than `pairs` (target×judge units) —
relay both so the saving is visible. The **usage gate** is the subagent analog of the
cost gate: no dollars, but spawning N workers consumes real session/rate usage. Get
approval in a separate turn before spawning.

**Re-`prepare` is destructive.** It clears the drafts for the pairs it re-renders,
so it must never run while you have **uncommitted** worker drafts in flight — that
throws away completed work and forces a re-spawn. Prepare the whole request once
(all `--scope`s together); if you truly must re-prepare with good drafts present,
pass `--keep-drafts`. Don't re-prepare just to "recover" a manifest — stage
everything up front so you never need to.

4b. **Spawn workers — one wave at a time, then wait.** For each manifest entry (one
target, or — with `--targets-per-worker` — a group of low-density targets sharing one
prompt), spawn one worker with the **Task** tool: `subagent_type: judge-worker`,
`model:` = the manifest's `worker_model`. Tell each worker its `prompt_path` and
`draft_path`: read the prompt, write ONLY the JSON verdict to the draft, reply
`done <id>`.

**Hard rule — never overlap waves.** Never launch wave N+1 until every worker from
wave N has finished. Spawning multiple waves without waiting is a usage-limit failure
mode and leaves workers in an indeterminate state.

Wave contract (fan-out width = `batch_size`, default 5; throttle down on 529):

1. **One wave per turn.** In a single assistant turn, spawn at most `batch_size`
   workers in parallel (multiple `Task` calls in one message). Do not queue the next
   wave in that same turn.
2. **END THE TURN and wait.** Wait until every worker in the current wave has
   returned (draft written / `done <id>`). No overlapping waves.
3. **Wave-complete check before the next spawn.** Confirm drafts exist for that
   wave's entries (`Read` / `ls` on each `draft_path`, or a partial `commit` if you
   want early `missing` detection). An Agent error on wrap-up is **not** proof the
   draft is missing — check the file (same as translate-harness commit-then-check).
4. **Next wave only after the wait.** Then spawn the next ≤ `batch_size` workers.
   Repeat until the manifest is exhausted.
5. **529 throttle.** On overload, step the wave down `batch_size → 3 → 1`, still
   waiting between waves; ramp back up toward `batch_size` when drafts land cleanly.

Committing after each wave is fine for recovery; a single final `commit` after all
waves is also fine. Either way, never start wave N+1 until wave N's workers have
finished.

5b. **Commit.** Collect + parse the drafts (and `--persist` if saving):
```bash
python scripts/run_judges.py commit --project understood-betsy --persist
```
Relay `committed` / `failed` / `missing`. **Re-spawn** any `failed` (bad/no JSON) or
`missing` (no draft) entries — same prompt_path/draft_path — then re-run `commit`.
`failed`/`missing` are keyed **per target** even inside a group, so recovery is per
target: re-prepare just the affected chunk(s) as a **solo** scope (default
`--targets-per-worker 1`, with `--keep-drafts` to protect good drafts still in flight)
so one bad chunk never drags its group-mates. Cap re-spawns at ~3 per entry, then
surface for manual review. Then relay findings the same way the API branch does.

### C. Apply fixes (optional — after relaying findings)

Offer this only **after** findings are relayed and **persisted** (`--persist`). It rewrites
`translated_text` in place, so it is careful and user-gated: it only ever proposes clean,
uniquely-locatable text swaps, and it never applies anything you didn't explicitly select.

6c. **Dry-run the plan.** Run `apply` with `--dry-run` (or just omit `--select`). Relay the
`applicable[]` fixes as `old → new` previews and list the `manual[]` findings with their
`reason` — those can't be auto-applied (instruction-type suggestion, or an absent/ambiguous
excerpt); point the user to the reader / web chunk editor for them.
```bash
python scripts/run_judges.py apply --project understood-betsy \
    --judge dialogue --scope chapter:chapter_03 --dry-run
```
7c. **Get an explicit selection.** The user picks which `applicable[].id`s to apply — **never
apply without an explicit pick.** Use `AskUserQuestion` (multiSelect, **≤4 options**) when there
are ≤4; for more, present a numbered list and have the user reply with the ids. If a listed
suggestion still reads to you as an *instruction* rather than literal replacement text, flag it
and leave it for manual editing even though the CLI classified it applicable.

8c. **Apply the picked ids.** Pass them comma-separated; add `--rebuild-epub` if the user wants
the book rebuilt now (recombine + realign always run either way):
```bash
python scripts/run_judges.py apply --project understood-betsy --judge dialogue \
    --scope chapter:chapter_03 --select chapter_03_chunk_000#0,chapter_03_chunk_001#2 [--rebuild-epub]
```
Relay the summary (`applied`, `chapters_realigned`, `archived_to`, `backups`). Every edit is
logged to `corrections_applied.jsonl` (the same audit log as reader corrections) and a pre-edit
chunk snapshot is kept under `.chunk_edits/` — so an apply is recoverable and traceable.

9c. **Refresh the badges.** Applying stale-marks each edited chunk's `evaluations/<chunk>.json`
(a fixed finding must not keep asserting a failure). Offer to **re-run the judge** on the
affected chunks; a fresh run clears the stale flag.

### Feedback (optional, either backend)

If the user says a finding is a false positive, record it so the judge can be tuned
later: `append_feedback(project_dir, chunk_id, "dialogue", issue_index,
"false_positive", note=...)` from `web_ui/evaluations.py`. Allowed types:
`false_positive`, `bad_message`, `missing_context_gap`.

## Notes

- The dialogue judge reads the rules from `prompts/dialogue.txt` automatically.
- The address judge reads the per-book expectations from `address_map.json` (the
  CLI injects them) plus the universal detection rubric in
  `prompts/address_forms.txt`. It needs the address-map setup beat first; the CLI
  errors clearly if the map is missing. `apply` also works on it (`--judge address`).
- Only chunk-scoped results persist (one file per chunk). A `chapter:` scope expands
  to one chunk-target per chunk, so persistence + badges work for both backends.
- Both backends persist via `merge_judge_result`, so the dashboard badge lights up
  identically regardless of which one ran. The persisted result records the backend
  (and `worker_model` for the subagent path) in its metadata / run header.
- Adding a judge later: write a prompt template + a `JudgeSpec`, implement
  `build_prompt` + `parse_response` (you get both backends for free), and register it
  in `src/judges/registry.py`. See `docs/JUDGES_FRAMEWORK.md`.
- `apply` reuses the reader-corrections pipeline (`src/corrections_apply.py`) but never touches
  the reader's own `corrections.jsonl` queue — it applies only the ids you selected, archives to
  `corrections_applied.jsonl`, and stale-marks the edited evaluations. Re-running the judge with
  `--persist` clears that stale marker.
