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
- **Subagent backend** (`prepare` → Task workers *or* headless `fanout` → `commit`) —
  renders each judge prompt to a file, then either spawn cheap **`judge-worker`**
  Task subagents or run a headless `claude -p` wave, then collect the drafts.
  **Zero API spend** (runs on the session). The gate here is a **usage** check
  before spawning N workers / fanning out, not dollars.
  Headless is subscription-only by **enforcement**, not convention: the launcher
  scrubs every metered credential out of the child env and refuses to start the
  wave unless `claude auth status` confirms a subscription. A metered login is
  rejected before job 1, so a wave can never half-complete on API credit.

Both backends build the *same* prompt and parse with the *same* parser, so the
findings — and the persisted `evaluations/<chunk>.json` — are identical either way.

## The CLI (read first)

`python scripts/run_judges.py <run|prepare|fanout|commit|apply>` is non-interactive and prints
one JSON object.

**`_schema` is opt-in.** Every subcommand takes `--schema` to include the block documenting
each output key; **errors always carry it**, successes carry a `_schema_hint` pointer instead.
It is omitted by default because it isn't free — `apply`'s block alone was ~52% of a real
run's payload, re-sent on every invocation. Add `--schema` when you need the key docs.

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
  Solo entries may also carry `preamble_path` / `body_path` (shared per-judge
  rubric cache for headless fan-out). Grouped entries do not — cache OR group,
  don't stack.
- `--quiet` — omit the manifest echo, keeping `manifest_path` + `usage_summary`.
  `fanout` reads the manifest from disk, so use this on the headless path. Task
  workers need the per-entry paths, so don't use it there.

`fanout` (subagent backend, opt-in headless wave) takes `--project`, optional
`--target-ids` (comma-separated solo `target_id` or `batch_id`), `--concurrency`
(default: manifest `batch_size`), `--cli {claude,cursor}` (default: config
`headless_cli`, else `claude`), `--cli-bin` (back-compat alias: `--claude-bin`),
and `--estimate` (project the token cost and print the argv without spawning).

**Cursor headless:** `fanout --cli cursor` (or
`config-set --key headless_cli --value cursor`) drives `cursor-agent` under a
subscription login — pin `--worker-model` / manifest `worker_model` to a Cursor
id (`grok-4.5`, `auto`, …). Cursor uses the full prompt (no cache-split /
`--system-prompt-file`). The **Task-worker** path (`prepare` → spawn
`judge-worker` → `commit`) stays **Claude-only** — the Task tool spawns Claude
subagents; Cursor is offered only on the headless `fanout` path.

`commit` (subagent backend) takes only `--project` and `--persist`.

`apply` (turn approved findings into chunk edits) adds:
- `--judge <name>` (default `dialogue`; **repeatable**) — whose **persisted** findings to consider.
- `--scope chunk:<id>` / `--scope chapter:<id>` / `--scope book` — **repeatable**.
- `--select <id,id,...>` — the `applicable[].id`s (or `qualified_id`s) to apply. **Omit for a plan-only dry-run.**
- `--rebuild-epub` — rebuild the EPUB after applying (implies recombine + realign; incompatible with `--no-realign`).
- `--no-realign` / `--realign-only` — defer or settle the expensive recombine+realign tail.
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
only then run the judge. The beat lives in
`.claude/skills/translate-harness/references/address-map.md` — **Read it first**;
it carries the drafting rules (the `when:"default"` requirement, the cast-naming
rule, and the `style_guide_summary` field) that the raw commands below assume:

```bash
python scripts/harness.py address-map precheck --project understood-betsy   # does the book even have dialogue?
python scripts/harness.py address-map prepare  --project understood-betsy   # samples dialogue-heavy chapters, renders a prompt
# (draft the map JSON to the printed draft_path, refine with the user)
python scripts/harness.py address-map commit   --project understood-betsy   # validates + writes address_map.json
```

Building it here, after translation, means `glossary.json` already exists — so
`prepare` reports `characters_loaded > 0` and the prompt carries the approved
cast. Use those target-language names, not the English source ones.

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

**Ask spawn mode and grouping together, in that order, in ONE `AskUserQuestion` call**
(skip whichever the user already chose). Order matters and the old order was wrong: the
right grouping *depends on* the spawn mode, `prepare` bakes the grouping in, and
re-`prepare` is destructive — so grouping cannot be revised after the fact.

- **Q1 — spawn mode.** (1) **Task workers** — spawn `judge-worker` Task subagents
  (Read→Write→`done`). (2) **Headless fan-out** — run `fanout` (`claude -p`, one generate
  turn per entry; uses the per-judge preamble cache when `preamble_path`/`body_path` are on
  the manifest). (3) **Abort**.
- **Q2 — grouping** (subagent-only; the API path always judges one target per call):
  - **Conservative — one chunk per worker** (`--targets-per-worker 1`, the default). Every
    chunk judged in full isolation — the known-good path, and the most spawns.
  - **Grouped — up to 3 chunks per worker** (`--targets-per-worker 3`). Packs up to three
    *low-dialogue-density* chunks into one worker prompt (dialogue-dense chunks always stay
    solo) → **fewer spawns**. Findings track the solo path closely in testing, but the full
    A/B quality gate isn't cleared — a cheaper option with a small bounded quality risk, not
    a free win.

  **Advise from the log, not from theory.** Each `claude -p` process pays a fixed context
  cost before it reads a word of the book — measured at ~3.9k tokens on a solo judge job
  (~37% of that job's billed input), which grouping attacks by cutting the process count.
  But the 2026-07-30 measurements found the bigger cost is on the *output* side: a default
  wave spent 23k output tokens producing a 90-token verdict, i.e. thinking, which grouping
  does not reduce. Both numbers come back in the `usage` rollup `fanout` returns — read
  `usage.overhead_ratio` **and** `usage.output` from the last wave you ran before
  recommending either mode, so this no longer has to be argued from first principles.
  (Per-job rows land in `.harness/judges/usage.jsonl`, but the rollup is computed, not
  stored — do not go looking for `overhead_ratio` in that file.)

Then run `prepare`, passing the chosen `--targets-per-worker` (omit it for conservative).
Add `--quiet` on the headless path — `fanout` reads the manifest from disk, so echoing it
into the conversation is pure duplication:
```bash
python scripts/run_judges.py prepare --project understood-betsy --judge dialogue \
    --scope chapter:chapter_05 --scope chapter:chapter_06 \
    [--targets-per-worker 3] [--worker-model sonnet] [--batch-size 5] [--quiet]
```

**Relay the figure for the backend being chosen — they price different things and neither
bounds the other:**
- **API backend** → `usage_summary.estimated_api_cost` (USD, metered).
- **Headless / Task workers** → `usage_summary.estimated_headless_tokens`, which is
  `estimated_prompt_tokens + workers × headless_baseline_tokens`. Name the split: how much
  is judging content and how much is per-job fixed overhead. `headless_baseline_source`
  says whether that baseline was measured on this machine or is the documented default.
  Also relay `usage_summary.headless_effort` (the resolved `--effort` level; `null` means
  the CLI's own default) so the user consents knowing the level, not just the token count.

Do **not** relay `estimated_api_cost` as "the price, and nothing is spent" when the user is
choosing headless. No *metered dollars* is true and enforced (headless refuses to run on a
metered login); "nothing is spent" is false in the currency that actually binds, and on the
2026-07-30 wave the API figure understated the chosen path by ~2.4× in tokens. Under
grouping also relay `workers` (spawns) alongside `pairs` (target×judge units) so the saving
is visible. Get approval in a separate turn before spawning.

**Want the real number first?** `fanout --estimate` projects the wave from the measured
baseline and prints the argv without spawning anything.

**Worker-model / cache note (fold into the confirmation, not a new gate):** the shared
preamble (dialogue rules ≈1.7k tok; address rubric+map ≈1.3–1.8k) clears **Sonnet's**
1024-token cache minimum but **not** Opus/Haiku's 4096, so prefer a **Sonnet** worker on
headless. The wave runs its first job alone so the rest read the shared prefix from cache
instead of each re-creating it. Task workers get no cross-invocation prompt caching either
way.

**End your turn and wait.** Do not spawn workers or run `fanout` in the same turn that
produced the manifest.

**Re-`prepare` is destructive.** It clears the drafts for the pairs it re-renders,
so it must never run while you have **uncommitted** worker drafts in flight — that
throws away completed work and forces a re-spawn. Prepare the whole request once
(all `--scope`s together); if you truly must re-prepare with good drafts present,
pass `--keep-drafts`. Don't re-prepare just to "recover" a manifest — stage
everything up front so you never need to.

4b. **Spawn workers per the chosen mode, then wait.**

### Option [1] Task workers (default)

For each manifest entry (one target, or — with `--targets-per-worker` — a group of
low-density targets sharing one prompt), spawn one worker with the **Task** tool:
`subagent_type: judge-worker`, `model:` = the manifest's `worker_model`. Tell each
worker its `prompt_path` and `draft_path`: read the prompt, write ONLY the JSON
verdict to the draft, reply `done <id>`.

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

### Option [2] Headless fan-out

Run one wave via the CLI (bounded parallelism, neutral cwd, no Task turns). Pass
`--target-ids` when recovering a subset; omit it to fan out every still-undrafted
manifest entry. `--estimate` projects the cost and spawns nothing:
```bash
python scripts/run_judges.py fanout --project understood-betsy \
  [--target-ids <id1,id2,...>] [--concurrency <batch_size>] [--estimate]
```
Each process is effectively: `claude -p` with the body (or full prompt) on stdin,
optional `--system-prompt-file <preamble_path>`, `--model <worker_model>`,
`--tools ""`, `--output-format json` → `draft_path`. The child env is scrubbed of
every metered credential first, and one `claude auth status --json` preflight runs
per wave; if it cannot confirm a subscription the command returns a top-level
`error`, writes nothing, and spawns no jobs (relay that error — the fix is
`claude` + `/login`, or `--backend api` if metered spend was actually intended).
After the wave, commit as below — the prepare→commit seam is unchanged
(`committed`/`failed`/`missing`). On 529, re-run with a lower `--concurrency`.

**Effort is a first-class knob.** Judge/annotation waves default to `--effort medium`
under `headless_effort_judges: auto` (2026-07-31: output −66%, wall −66%, cost −55%,
quality confirmed by hand). The ladder, cheapest first:

| level | measured trade |
|---|---|
| `low` | output −95%, wall −93%; *missed the only finding* on a 3-chunk sample (2026-07-30) |
| `medium` | output −66%, wall −66%, cost −55%; quality OK on the measured wave; **recall still uncontrolled** |
| CLI default / `high` / `xhigh` | full effort — use when you need maximum recall |

```bash
# Judge waves on this book (persists):
python scripts/harness.py config-set --project <slug> --key headless_effort_judges --value low
# Per-run (does not persist):
python scripts/run_judges.py fanout --project <slug> --effort low
```

`headless_effort_judges` moves judge waves only. Translation and footnotes read
`headless_effort_translate` / `headless_effort_footnotes` and are untouched by
anything you set here — never offer a cheaper judge pass as though it also made
the book cheaper to translate.

Offer a cheaper level only when the user asks for a faster/cheaper pass, and say what it
trades; **never switch silently**. See `docs/LLM_PROVIDERS.md`.

**Relay `usage` after every wave.** `fanout` now reports what the wave actually consumed:
`input`/`output`, the `cache_creation` vs `cache_read` split, `prompt_sent` (the judging
content) and `overhead` / `overhead_ratio` (everything else — the per-process context each
job pays before reading a word of the book). A ratio near 0.5+ means the wave spent more on
being a process than on judging, and the answer is fewer, larger jobs. Per-job detail lands
in `.harness/judges/usage.jsonl`; **read that file only if asked** — it exists so the detail
does *not* have to enter the conversation.

Committing after each wave is fine for recovery; a single final `commit` after all
waves is also fine. Either way, never start wave N+1 until wave N has finished
(Task: workers returned; headless: `fanout` returned).

5b. **Commit.** Collect + parse the drafts (and `--persist` if saving):
```bash
python scripts/run_judges.py commit --project understood-betsy --persist
```
Relay `committed` / `failed` / `missing`. **Re-spawn** any `failed` (bad/no JSON) or
`missing` (no draft) entries — Task: same prompt_path/draft_path; headless:
`fanout --target-ids <ids>` — then re-run `commit`. Trust `commit`'s lists, not a
spawn/fanout error text, as the "no draft" signal. `failed`/`missing` are keyed
**per target** even inside a group, so recovery is per target: re-prepare just the
affected chunk(s) as a **solo** scope (default `--targets-per-worker 1`, with
`--keep-drafts` to protect good drafts still in flight) so one bad chunk never
drags its group-mates. Cap re-spawns at ~3 per entry, then surface for manual
review. Then relay findings the same way the API branch does.

### C. Apply fixes (optional — after relaying findings)

Offer this only **after** findings are relayed and **persisted** (`--persist`). It rewrites
`translated_text` in place, so it is careful and user-gated: it only ever proposes clean,
uniquely-locatable text swaps, and it never applies anything you didn't explicitly select.

`apply` writes its JSON to **stdout only**. The recombine/realign step loads a BERT model and
the EPUB builder prints progress; all of that is redirected to stderr, so stdout still parses
as exactly one JSON object. Aligner chatter on stderr is not an error — read the JSON. A
`[apply] <chunk_id>: N fix(es) written + archived` line lands on stderr per chunk, so an
interrupted run still shows how far it got.

> **Run `apply` backgrounded for anything past a few chapters** (`run_in_background: true`).
> Recombine + realign loads a BERT model **per chapter**, so a whole-book apply runs well over
> the Bash tool's 2-minute foreground limit and gets killed mid-write. That is survivable now
> (see the recovery note in 8c) but it costs a repair round-trip. Scope, then background.

6c. **Dry-run the plan.** Run `apply` with `--dry-run` (or just omit `--select`). Relay the
`applicable[]` fixes as `old → new` previews and list the `manual[]` findings with their
`reason` — those can't be auto-applied. The reasons split into three families: the suggestion
isn't replacement text (`suggestion_not_literal`, `suggestion_placeholder` — a literal `N/A`,
meaning the judge decided the passage was fine, so a swap would delete the line), the excerpt
doesn't locate (`excerpt_not_found`, `excerpt_ambiguous` — the biggest bucket for the address
judge), or the splice itself would damage the text (`suggestion_restates_context` duplicates the
prose around the excerpt, `suggestion_adds_ellipsis` elides text it isn't replacing,
`suggestion_too_long`/`suggestion_too_short` are quoting or dropping prose rather than rewriting
the span, `suggestion_unbalanced_raya` would leave a closing inciso raya with nothing opened, and
`mixed_register_remains` is an `inconsistent-address` fix that leaves the form it replaces standing
elsewhere in the chunk — applying it *creates* the inconsistency it reports). Point the user to the
reader / web chunk editor for all of them.

**Don't predict what `manual[]` will hold.** The guards above are *semantic* — they ask
whether a swap would damage the text — not structural, so "this fix merges two paragraphs, it
will surely be withheld" is wrong: a paragraph merge is still a literal, uniquely-locatable
swap. Dialogue findings in particular come back close to 100% `applicable`. Read the actual
plan before telling the user what it will say.

**`applicable` ≠ correct.** `applicable` means *mechanically safe to splice* (unique location,
literal swap, no structural damage the guards catch) — not that the suggestion is the right
fix. The 2026-07-31 stormy-misty dialogue wave was 100% applicable and 78% correct: two of
nine `applicable` fixes were semantically wrong (one deleted source content; one proposed a
fresh dialogue-rule violation). Always skim `old → new` against the source and the rulebook
before presenting the plan as a quality guarantee.
```bash
python scripts/run_judges.py apply --project understood-betsy \
    --judge dialogue --scope chapter:chapter_03 --dry-run
```
`--scope` takes `chunk:<id>`, `chapter:<id>` or **`book`** (the whole project) and is
repeatable. Use `book` for a full-book pass — never a shell loop building 32 `--scope
chapter:` flags, where one missing chapter silently drops its findings out of scope.

`--judge` is **repeatable**, and both judges in one invocation is the right way to run them:
realign is paid once instead of twice, and each judge's excerpts are re-checked against the
text the previous judge left behind. Findings then carry a `qualified_id`
(`<judge>:<chunk_id>#<i>`) as well as the bare `id`; `--select` takes either, but a bare id
both judges have comes back in `ambiguous_ids[]` rather than being guessed at.
```bash
python scripts/run_judges.py apply --project pollyanna \
    --judge dialogue --judge address --scope book --dry-run
```
7c. **Get an explicit selection.** The user picks which `applicable[].id`s to apply — **never
apply without an explicit pick.** Use `AskUserQuestion` (multiSelect, **≤4 options**) when there
are ≤4; for more, present a numbered list and have the user reply with the ids.

The *mechanical* hazards are the CLI's job now, and it withholds them: placeholder and
instruction-shaped suggestions, ones that restate adjacent prose, elide with `...`, swing far in
length, unbalance the paragraph's rayas, or normalize a register the rest of the chunk still uses
(see the reasons in 6c). Don't re-derive those by hand. What no guard can decide is **semantic**, so
these veto a fix the CLI classified applicable:

**Address judge:**
- **Is the direction right?** Check the fix against `address_map.json` yourself. A blanket clause
  ("tú among peers") can swallow a relationship the map names specifically — two non-intimate adults
  use usted in both directions, and a character the map calls ceremonious keeps usted even with a
  child. The judge reads those as violations; they are not.
- **Is the fix complete?** A speech that mixes registers, where the judge flagged only one clause,
  leaves a half-corrected line if applied. Read the whole turn, not the `old → new` pair.

**Dialogue judge:**
- **Does the suggestion delete content that is in the source?** Read `source_text`, not just
  `translated_text`. A 2026-07-31 fix (`chapter_03_chunk_000#0`) merged two paragraphs by deleting
  the inciso `—continuó, secándose la cara—`, which renders "as he mopped his face". `new` was 89%
  of `old`'s length, so no length guard could ever have caught it.
- **Does the suggestion itself obey `prompts/dialogue.txt`?** Check the rewrite against the
  rulebook, especially rule 25's capitalization for a non-speech inciso between two complete
  sentences. The same wave proposed `—la voz del abuelo retumbó—` (`retumbar` is not in rule 20's
  speech-verb list, so rule 25 demands `La`) while *enforcing* rule 25 twice on another chapter.

8c. **Apply the picked ids.** Pass them comma-separated; add `--rebuild-epub` if the user wants
the book rebuilt now (that path recombines + realigns; use `--no-realign` to defer the tail):
```bash
python scripts/run_judges.py apply --project understood-betsy --judge dialogue \
    --scope chapter:chapter_03 --select chapter_03_chunk_000#0,chapter_03_chunk_001#2 [--rebuild-epub]
```
Relay the summary (`applied`, `chapters_realigned`, `archived_to`, `backups`). Each chunk's
pre-edit snapshot (`.chunk_edits/`), its edit, its rows in `corrections_applied.jsonl` (the same
audit log as reader corrections) and its stale stamp are finished **before the next chunk is
touched** (sequential steps; the pre-edit snapshot is the recovery proof if a kill lands
mid-sequence) — so an apply is traceable, and an interrupted one leaves a consistent prefix of
finished chunks rather than edits nothing recorded.

**Re-running the same `--select` is safe, including after an interrupted run.** Ids whose edit is
already in the chunk come back in `already_applied[]` with `status: "ok"` and rc 0; nothing is
re-applied and nothing is re-archived. The proof that an edit is ours is an audit row *or* a
`.chunk_edits/` snapshot, so a run killed before it wrote the audit log still resumes. When the
retry also finds a chapter whose alignment is older than its chunks — the signature of a run that
died before its realign step — it finishes that work: recombine, realign, re-stale-mark, and
recover the missing audit rows (appended with `"recovered": true`). Both repairs are named in
`warnings[]`. An id the plan withheld comes back in `manual_ids[]`; `unknown_ids[]` means only
"no finding with that id in scope".

**Deferring the expensive tail.** `--no-realign` applies the edits and skips recombine+realign,
returning what is owed in `chapters_pending_realign[]`; settle it later with `--realign-only`,
which changes no text and realigns every chapter in scope whose alignment is stale
(`--realign-only --dry-run` just reports). `--realign-only` is also the repair command when an
apply was interrupted and you no longer have its `--select` string:
```bash
python scripts/run_judges.py apply --project pollyanna --scope book --realign-only --rebuild-epub
```

**A plan goes stale the moment anything is applied to its chunks.** Across separate invocations,
re-run the dry run between them — one judge's fix can rewrite the text another judge's excerpt
quotes, and a plan is not revalidated by `--select`. Passing both judges to *one* invocation
avoids the problem entirely; within a run, a fix an earlier judge superseded is reported in
`failed[]` with a warning rather than forced into an approximate span.

9c. **Refresh the badges.** Applying stale-marks each edited chunk's `evaluations/<chunk>.json`
(a fixed finding must not keep asserting a failure). The marker is written at the **top level** of
that file — `stale`, `stale_since`, `stale_reason`, *not* inside `judges[<judge>]` next to the
`score`/`issues` it invalidates — and `stale_reason` is single-valued, so a chunk both judges
edited names only the most recent. Offer to **re-run the judge** on the affected chunks; a fresh
run clears the stale flag.

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
- Only chunk-scoped results persist (one file per chunk). A `chapter:` or `book` scope
  expands to one chunk-target per chunk, so persistence + badges work for both backends.
  `book` works on `run` and `prepare` too — on `run` it is a large metered call, so the
  cost gate will ask for `--confirm`.
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
- Never hand-restore chunks from `.chunk_edits/` to recover an interrupted apply — re-running the
  same `--select` (or `--realign-only`) is the supported path and does it without discarding the
  edits that did land. Those snapshots are still the last resort if the text itself is wrong.
