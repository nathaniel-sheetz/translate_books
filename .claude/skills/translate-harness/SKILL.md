---
name: translate-harness
description: |
  Orchestrate the book-translation pipeline conversationally. The agent acts as the
  thinking-mode LLM that drafts the glossary and style guide in-conversation (replacing
  the copy/paste-into-an-external-chat loops), pauses for your approval, then runs the
  existing deterministic + API-backed pipeline (chunk -> translate -> combine -> epub).
  Use when asked to "translate this book in the harness", "run translate-harness",
  "draft the glossary and style guide with me", or "orchestrate the translation".
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - AskUserQuestion
  - Task
---

# translate-harness

Drive the translation pipeline as a conversation. You (the agent) are the thinking-mode
LLM: you draft the glossary and style guide in-chat, the user approves or edits, and the
existing scripts produce the EPUB. The deterministic steps stay scripts; you call them
through one non-interactive surface — **`scripts/harness.py`**.

Scope (v1, eng review 2026-06-05): **short texts** — a single chapter or a small book.
Long-book robustness (formal resume, batching) is out of scope.

## How the harness CLI works (read first)

Every command is `python scripts/harness.py <command> ...`, run from the repo root, and is
**non-interactive** — it never calls `input()`, so it can never deadlock you. Each beat has
the same shape:

- **`prepare`** commands gather inputs, build the LLM prompt, and print JSON telling you the
  `prompt_path` to read and the `draft_path` to write.
- **You are the LLM:** read the prompt, draft the answer, and **Write it to `draft_path`**
  yourself (the harness does not call an API for these — that is the whole point).
- **`commit`** commands parse + **validate/guard** your draft and write the final artifact
  (`style.json` / `glossary.json`). A bad draft fails loudly with a re-draft message instead
  of poisoning the run; fix what it names and re-run the same `commit` (cap ~3, then
  hand-edit-or-abort).

Commands that produce data for you print a JSON object to stdout. Commands that wrap a
deterministic/paid stage (`chunk`/`cost`/`translate`/`epub`) stream the underlying script's
output through. Per-project working state lives in `projects/<slug>/.harness/`; `setup`
wipes it for a clean run, so there is no global `.tmp/` to manage.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ NEVER invoke an interactive code path — every input() prompt DEADLOCKS you.│
│   ✗ scripts/generate_style_guide.py  (built on input() per question)       │
│ Always go through scripts/harness.py: it is the non-interactive surface,    │
│ and it gates cost via --cost-only + a --yes you only pass after approval.    │
└──────────────────────────────────────────────────────────────────────────┘
```

- **Two kinds of STOP beat — not every stop is a draft approval.** Some STOP beats are
  *question-collection* stops that happen **before any draft exists**: you ask the user
  questions, END your turn, and wait for their real answers. The others are *approval* stops:
  you present a finished draft and ask approve / edit / re-draft. Both end your turn and wait.
- **Step 1 alone has THREE pause points, not one** — do not treat 1e as the only stop:
  - **G1 — standard + deterministic questions (step 1b):** ask, wait for answers.
  - **G2 — LLM-driven follow-up questions (step 1c):** ask, wait for answers.
  - **G3 — style-guide approval (step 1e):** present the draft, wait for sign-off.
  Drafting the style guide (1d) MUST NOT begin until G1 and G2 both hold *real user answers*.
- **Never answer the style-guide questions on the user's behalf.** The whole point of the beat
  is to capture *the user's* decisions. The `options` and `hint` on each question exist to help
  the user choose — they are NOT defaults for you to auto-pick. Inventing answers, picking
  defaults, or skipping ahead to the next command before the user has responded defeats the
  skill. If the user hasn't answered yet, STOP and wait.
- **Approval gates:** on each STOP beat, present the draft and ask. On approve → continue.
  On reject → re-draft with the feedback and re-present. The user may say "change my answer
  to Q3" at any point — honor it.
- **Each gate authorizes ONLY its own stage — approvals never cascade.** Approving the
  style guide does not authorize the glossary. Approving the glossary does **NOT** authorize
  translation. Approving the cost estimate is the *only* thing that authorizes the paid
  translation run, and it authorizes nothing beyond it. There is exactly one paid step
  (translate); it has its own dedicated gate (Step 4) and reaching it requires a fresh,
  explicit "yes, translate" — never inferred from any earlier approval.
- **The cost beat is a hard stop.** Showing the cost estimate and *starting* the
  translation must be two separate turns. After you print the estimate, END your turn with
  the AskUserQuestion and wait. Do NOT run `translate` in the same response that produced the
  estimate, and never bundle "estimate → translate" into one chain. Money moves only after
  the user answers that question affirmatively in a later turn.
- `chunk` and `cost` always run with `--cost-only`: they never spend and never prompt.
  `translate` fails closed unless you pass `--yes`; never pass `--yes` unless the user
  explicitly approved the estimate in a separate turn.

## Pipeline overview

```
ingest/split ─► [STYLE GUIDE beat] ─► [GLOSSARY beat] ─► difficulty ─► chunk ─► [COST beat] ─►
 (setup)         agent drafts          agent drafts      (det.; sizes  (det.)   translate ─►
                 + refine + approval    + approval         chunks)               combine ─► epub
```

The style guide goes **first** on purpose: it captures the user's key decisions
(dialect/locale, name conventions, register, formatting) and those decisions then steer the
glossary — `glossary prepare` feeds the approved style guide into the proposal prompt.

The glossary goes **before chunking** on purpose too: the difficulty scorer excludes glossary
terms from its lexical-rarity signal, and the book-level difficulty sets the **default chunk
target size** (harder text → smaller chunks). So chunking is deferred until after the glossary
is finalized — ingest/split (in `setup`) is the only deterministic prep that runs up front.

## Step 0 — Set up the project

Get the source text into `projects/<slug>/source.txt` (or pass a Gutenberg `--url`), then run
ingest + split (NOT chunk — chunking is deferred to Step 3 so it can use the glossary-informed
difficulty score). `setup` also persists `target-lang` / `locale` / `model` / `title` /
`author` so later steps stop repeating them.

```bash
python scripts/harness.py setup --project projects/<slug> \
  --target-lang Spanish --locale mx --model claude-sonnet-4-20250514 \
  --title "<Title>" --author "<Author>"
# add --url <gutenberg-url> if there is no local source.txt yet.
```

Pick the chapter pattern that matches the book: `--chapter-pattern roman` (Chapter I, II …),
`numeric` (Chapter 1, 2 …), or `custom` (with `--custom-regex`). If split reports "No chapters
detected," re-run with a different pattern. Confirm the printed `chapter_count` looks right and
`chunks_dir_exists` is `false`. (The lang/locale/model defaults are Spanish/mx/sonnet — surface
them to the user rather than assuming silently.)

## Step 1 — STYLE GUIDE beat (two question gates, then draft + approval)

This beat has **three STOP points**: G1 (1b) and G2 (1c) collect the user's answers *before*
any draft exists; G3 (1e) approves the finished guide. Do not reach the draft (1d) until the
user has answered both question gates.

**1a. Gather the standard + deterministic questions:**
```bash
python scripts/harness.py style-guide prepare-questions --project projects/<slug>
```
This prints `detected_features`, the `questions` (the 4 **standard** fixed questions plus the
**deterministic** feature-detected ones, each with `id`, `question`, `options`, and a `hint`),
and an `answers_path`. Nothing here is answered yet — these are *for the user*.

**1b. STOP — G1: ask the standard + deterministic questions and WAIT.** Present **every**
question in chat with its options and hint, then **END your turn** and wait for the user's
answers. Do **not** answer them yourself, pick defaults, or run the next command first. (There
are usually more than 4 — 4 fixed + N detected — so ask in chat, optionally batching via
`AskUserQuestion` in groups of ≤ 4; don't assume one `AskUserQuestion` holds them all.) Once the
user has answered (let them revise earlier answers), record each as a 0-based **option index**
or a **custom string** and **Write** the dict to the printed `answers_path`:
`{question_id: index_or_string}`. Only then continue to 1c.

**1c. Generate the LLM-driven follow-up questions, then STOP to ask them.** First draft them —
you are the LLM:
```bash
python scripts/harness.py style-guide prepare-followups --project projects/<slug>
```
Read the printed `prompt_path`, draft the follow-up questions as a JSON array, **Write** them to
the printed `draft_path`, then:
```bash
python scripts/harness.py style-guide commit-followups --project projects/<slug>
```
**STOP — G2: ask the printed `new_questions` and WAIT.** Present them in chat, **END your turn**,
and wait for the user's answers (same rule — do not answer them for the user). Only after the
user responds, **rewrite `answers_path` with the full answer set** (prior answers + the new
ones), then continue to 1d.

**1d. Draft the style guide (you are the LLM), refine, save.**
```bash
python scripts/harness.py style-guide prepare-draft --project projects/<slug>
```
Read the printed `prompt_path`, draft the style-guide prose, **Write** it to the printed
`draft_path`, and **refine it with the user in chat** until they sign off. Then:
```bash
python scripts/harness.py style-guide commit --project projects/<slug>
```
This parses, saves `style.json`, and validates it. If it prints a VALIDATION/PARSE error, fix the
draft and re-run `commit` (cap ~3 re-drafts, then hand-edit-or-abort).

**1e. STOP — G3: approval beat.** Present the final style guide. Approve / edit / re-draft. This is
the user's chance to lock in the key decisions (dialect/locale, name conventions, register)
**before** they shape the glossary.

## Step 2 — GLOSSARY beat (agent drafts, approval gate)

```bash
python scripts/harness.py glossary prepare --project projects/<slug>
```
This extracts candidates, feeds in the approved style guide, and prints `candidate_count`,
`style_guide_loaded` (must be `true` — if not, the style guide isn't saved yet; go back to Step 1),
a `prompt_path`, and a `draft_path`.

**Read the printed `prompt_path`** and draft the glossary proposals yourself — the thinking-mode
step. Produce a JSON array of `{english, translation, type, context}` objects and **Write** it to
the printed `draft_path`. As you draft, **track every term whose translation you were unsure about**
(ambiguous sense, multiple valid renderings, dialect/register judgement calls) and why — keep that
running list for the approval beat. Then:
```bash
python scripts/harness.py glossary commit --project projects/<slug>
```
This guards the proposals, builds + saves `glossary.json`, and validates it; it prints the full
`terms` list. If it prints a VALIDATION ERROR, fix the entries it names and re-run `commit` (cap ~3).

**STOP — approval beat.** Do all three, in this order:
- **Show the full list of glossary terms** (use the `terms` the command printed) — render every
  `english → translation` pair with type/context so the user can scan the actual decisions. Do not
  collapse it to "N terms drafted."
- **Call out the uncertain translations** you tracked: name each term, its chosen rendering, the
  alternative(s) you considered, and why you hesitated, so the user can adjudicate the close calls.
- **AskUserQuestion: approve / edit / re-draft.** On edit, let the user hand corrected JSON; Write
  it to the `draft_path` and re-run `commit`. Only continue once approved.

> Approving the glossary approves **the glossary only**. It does NOT mean "start translating."
> After approval you proceed to difficulty scoring + chunking (Step 3) and then **stop again** at
> the cost gate (Step 4). Do not jump to translate here.

## Step 3 — Difficulty-aware chunk (deterministic) — estimator only

**3a — Score difficulty → default chunk target size.** The glossary now exists, so the scorer
reflects it:
```bash
python scripts/harness.py difficulty --project projects/<slug>
```
This prints `book_difficulty` and a book-level `suggested_target_size` (N), plus a **per-chapter
table** (each chapter's `difficulty` and its own `suggested_target_size`). Present the book
difficulty and `N`, and surface the per-chapter spread when chapters differ markedly (e.g. a
dialect-heavy chapter scored harder/smaller). Treat the suggestions as **defaults**: honor a user
override; otherwise chunk at them. (If `wordfreq` isn't installed, `wordfreq_available` is `false`
and suggestions lean toward 2000 — still usable.)

**3b — Chunk (estimator only).** Default to per-chapter sizing so each chapter uses its own
`suggested_target_size`:
```bash
python scripts/harness.py chunk --project projects/<slug> --size <N> --per-chapter
```
With `--per-chapter`, the chunker reads the per-chapter suggestions from `difficulty.json` (so run
`difficulty` first) and `--size <N>` is the **fallback** for any chapter not in the manifest. Each
chapter's target also rescales its min/max bounds, so a harder/smaller target actually splits more.
Drop `--per-chapter` to chunk the whole book uniformly at `--size <N>` instead (e.g. if the user
prefers one size). Either way this chunks once, prints the cost estimate, then halts — it runs
`--cost-only` and physically cannot spend. Carry that estimate straight into the Step 4 gate below.

## Step 4 — COST beat, then translate

```
┌──────────────────────────────────────────────────────────────────────────┐
│ THIS IS THE ONLY PAID STEP AND THE ONLY HARD STOP THAT COSTS MONEY.        │
│ Estimating cost and starting translation are TWO SEPARATE TURNS.           │
│ Print the estimate, ask, and END YOUR TURN. Never run `chunk`/`cost` and   │
│ `translate` in the same response. No earlier approval (style guide,        │
│ glossary, chunk) authorizes this — only the answer to the question below.  │
└──────────────────────────────────────────────────────────────────────────┘
```

1. You already have the estimate from Step 3. To recompute it (still WITHOUT spending):
   ```bash
   python scripts/harness.py cost --project projects/<slug>
   ```
2. **STOP — approval beat. END THE TURN HERE.** Show the estimate, then ask via AskUserQuestion:
   proceed / abort — and stop. Do not call any further tool in this response. Resume ONLY after the
   user has, in a *later* turn, explicitly chosen to proceed. If unsure whether they approved, treat
   it as NOT approved and ask again. Confirm the model with them here too (it determines the price).
   > Cost note (eng review 2026-06-05): the API path does not use prompt caching today, so input
   > tokens are not discounted across chunks. The estimate is the honest figure.
3. **Only once the user has affirmatively approved in a separate turn**, translate:
   ```bash
   python scripts/harness.py translate --project projects/<slug> --yes --model claude-sonnet-4-20250514
   ```
   `translate` refuses to run without `--yes`. The model defaults to the one set at `setup`; pass
   `--model` to override, and surface the choice rather than assuming.

## Step 4B — Subagent backend (zero-API-key, model-pinned) — ALTERNATIVE to Step 4

Two translation backends, same downstream pipeline. Pick one with the user:

- **API backend (Step 4):** fast, batchable, but needs an `ANTHROPIC_API_KEY` and spends metered dollars.
- **Subagent backend (this step):** no API key — translation runs as **spawned worker subagents on
  the running subscription**, so a stranger can translate token-free. You (the orchestrator, e.g.
  Opus) stay the smart driver while workers run on a **cheaper pinned model** (default `sonnet`). v1
  is **sequential** (one worker at a time) and best for short texts or a chapter batch.

**Chapter-at-a-time:** pass `--chapters 1-2` (or `3,7`) to translate just those chapters; read them in
the reader, then come back and translate `3-4`. Re-running only fills chunks that still need a
translation, so resume is free. Works on Step 4 too.

**4B-a. Prepare (no spend).** Renders one prompt file per untranslated chunk + a manifest:
```bash
python scripts/harness.py translate-prepare --project projects/<slug> [--chapters 1-2] [--worker-model sonnet]
```
This prints a `manifest` (each entry has `chunk_id`, `prompt_path`, `draft_path`), a `usage_summary`
(`chunks`, `source_words`, `worker_model`), and the `worker_model`. It does **not** call an API.

**4B-b. STOP — usage gate. END THE TURN.** This is the subagent analog of the cost gate. There is no
dollar cost, but spawning N workers consumes real subscription/rate usage. Show the `usage_summary`
("N workers on `<model>`"), confirm the worker model, and ask via AskUserQuestion: proceed / abort.
**End your turn and wait.** Do not spawn workers in the same turn that produced the manifest.

**4B-c. Spawn workers (sequential), then commit.** Only after the user approves in a later turn, loop
the manifest **one entry at a time**. For each entry, spawn a worker with the **Task** tool:
- `subagent_type`: `translator` (defined in `.claude/agents/translator.md`)
- `model`: the approved `worker_model` (e.g. `sonnet` / `haiku`) — this is how the worker is pinned
  cheaper than you, the orchestrator.
- prompt: "Translate one chunk. Read `<prompt_path>`. Write ONLY the translated prose to
  `<draft_path>`. Nothing else." (Pass the entry's real `prompt_path` and `draft_path`.)

The worker writes its prose to `draft_path` — **do not** have it return the translation to you (that
floods your context; the whole point is the worker writes the file). Once all workers in the batch
have written their drafts:
```bash
python scripts/harness.py translate-commit --project projects/<slug>
```
This guards each draft (length / completeness / image-token filename parity / echo), writes a
provenance log, stamps the chunk, and prints `committed` / `failed` / `missing` / `skipped`. It is
idempotent — already-translated chunks are skipped.

**4B-d. Re-spawn the misses.** For any `failed` (the report names the problem per chunk) or `missing`
(no draft written), re-spawn a worker for just those `chunk_id`s — write fresh prose to the same
`draft_path` — and re-run `translate-commit`. Cap re-spawns at ~3 per chunk, then surface the chunk
for a manual edit-or-skip decision rather than looping.

Then continue to Step 5 (combine + EPUB) exactly as the API path does.

## Step 5 — Combine + EPUB (translated chapters only)

The `translate` run chains through combine, epub, and align, building the EPUB from translated
chunks only and reporting exactly which chapters shipped. To (re)build explicitly:
```bash
python scripts/harness.py epub --project projects/<slug>
# --title / --author / --language default to what you set at setup; pass them to override.
```
Report the included/skipped chapter lists so a partial translation is never mistaken for a complete
book. Confirm the EPUB landed:
```bash
ls projects/<slug>/*.epub
```

## Done

Report: glossary terms count, style-guide length, chunk count, EPUB path. Confirm the copy-paste
loop is gone — the user drafted nothing in an external chat.

## What this skill deliberately does NOT do (v1)

- No `TranslationBackend` Protocol. Both backends share one prompt builder
  (`build_translation_prompt`) and one stamp (`apply_translation`), so the seam is two functions,
  not a class hierarchy. The subagent backend (Phase B) is the `translate-prepare` /
  `translate-commit` path (Step 4B). Still deferred: **parallel** worker fan-out (v1 is sequential),
  the **judge** backend, and the portable `claude -p --model` worker (Approach C). See TODOS.md.
- No long-book resume beyond the pipeline's existing chunk-level idempotency (`stage_translate`
  skips chunks that already have a translation).
- No prompt caching (tracked separately in TODOS.md).
