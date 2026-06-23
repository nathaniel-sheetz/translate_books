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

**Reading harness output — Read the artifact, don't grep stdout.** Every JSON-returning command
also mirrors its full result to `projects/<slug>/.harness/last_output.json` (UTF-8) and prints
`OUTPUT_JSON: <path>` to stderr. **Prefer `Read`ing that file** over capturing stdout — it is
always clean UTF-8 regardless of the console. **Never pipe harness output through `grep`**: on
Windows, accented/curly-quote bytes make `grep` treat the stream as binary and truncate it.

**Run logging — automatic timeline + a few beats you log.** Every harness command appends one
event (command, duration, outcome, key counts) to `logs/harness_runs.jsonl`, tied together by a
`run_id` minted at `setup`. That timeline is automatic — you do nothing. But four *conversational*
signals the CLI can't see are yours to record with `log-event` at the beats called out below
(backend choice, style-guide / glossary approval, spawn-mode choice, worker re-spawns):

```bash
python scripts/harness.py log-event --project projects/<slug> \
  --event approval --data '{"beat":"glossary","decision":"approved_first_pass"}'
```

`--data` is a free-form JSON object, so new fields need no new flags. These calls never spend and
must never gate the flow — log and move on; a failed log must not stop a run.

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
  you present a finished draft and ask via AskUserQuestion — **Approve all / Reject & talk it
  through** — with the custom (_Other_) field reserved for specific swaps. Both end your turn and wait.
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
  --target-lang Spanish --locale mx --model claude-sonnet-4-6 \
  --title "<Title>" --author "<Author>"
# add --url <gutenberg-url> if there is no local source.txt yet.
```

Pick the chapter pattern that matches the book: `--chapter-pattern roman` (Chapter I, II …),
`numeric` (Chapter 1, 2 …), or `custom` (with `--custom-regex`). For a Gutenberg `--url`, the
returned `suggested_pattern` and `chapter_report` are read from the book's HTML headings —
relay them and prefer the suggestion when it differs from your guess. Confirm the printed
`chapter_count` looks right and `chunks_dir_exists` is `false`. (The lang/locale/model defaults
are Spanish/mx/sonnet 4.6 — surface them to the user rather than assuming silently.)

**Refine the split if it looks wrong** — the `setup` split misfires, reports "No chapters
detected," or Gutenberg front/back matter (title page, copyright, the CONTENTS listing, a
teacher's note) leaked in as spurious chapters. Don't hand-edit `source.txt`; use the review
beat, which mirrors the web GUI's Stage 2:

```bash
# Dry-run: prints each section tagged front_matter / chapter / back_matter; writes nothing.
python scripts/harness.py split-preview --project projects/<slug> \
  --chapter-pattern custom --custom-regex '(?<=\n---\n\n)[A-Z][^\n]*' \
  --min-chapter-size 500 \
  --front-matter-title "Contents" --back-matter-title "A Word to the Teacher"

# Happy with the preview? Commit it (rewrites chapters/, clearing any stale files).
python scripts/harness.py split --project projects/<slug>  # + the same split flags
```

`--front-matter-title` / `--back-matter-title` are repeatable and force-tag a heading so it
isn't mis-numbered as a chapter; built-in keyword auto-detect (preface, dedication, epilogue …)
stays on unless you pass `--no-auto-front-matter` / `--no-auto-back-matter`. Raising
`--min-chapter-size` (~500) drops short stray front-matter lines a loose pattern would otherwise
capture. The same three controls also work directly on `setup` for a one-shot run.

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
and an `answers_path`. Each option is an `{id, label}` pair — the `id` is a stable slug you pass
straight through, so you never count positions. Nothing here is answered yet — these are
*for the user*. Two notes: (a) the `dialect` question may arrive with a `prefilled` id +
`prefilled_reason` derived from the setup locale (es-mx → `mexican_spanish`) — present it as a
confirm/override default, not a blank ask. (b) `forms_of_address` has a first-class informal-tú
option (id `t_dominates_informal` — the `ú` is dropped by the slug rule); prefer that id over
inventing custom text when the user wants tú to dominate.

**1b. STOP — G1: ask the standard + deterministic questions and WAIT.** Present **every**
question in chat with its options and hint, then **END your turn** and wait for the user's
answers. Do **not** answer them yourself, pick defaults, or run the next command first. (There
are usually more than 4 — 4 fixed + N detected — so ask in chat, optionally batching via
`AskUserQuestion` in groups of ≤ 4; don't assume one `AskUserQuestion` holds them all.) Once the
user has answered (let them revise earlier answers), record each as the chosen option's **`id`**
(or its exact `label`) from the prepare-questions output — or a **custom string** for anything not
among the options — and **Write** the dict to the printed `answers_path`:
`{question_id: option_id_or_label_or_custom_string}`, e.g.
`{"dialect": "mexican_spanish", "forms_of_address": "t_dominates_informal"}` (here `dialect` keeps
the `prefilled` id from setup and `forms_of_address` uses the first-class tú option — reserve a
custom string for genuinely off-menu rules).
(A 0-based numeric index still works for back-compat, but the `id` is safer — no position counting.)
Only then continue to 1c.

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
ones, same `id`/label/custom-string format), then continue to 1d.

**1d. Draft the style guide (you are the LLM), refine, save.**
```bash
python scripts/harness.py style-guide prepare-draft --project projects/<slug>
```
This also reports `resolved_answers` (each tagged `option` or `custom`) and `unanswered` — glance
at it to confirm every answer matched the option you intended (a `custom` tag on a question you
answered by `id` means a typo; fix `answers_path` and re-run). Then read the printed `prompt_path`,
draft the style-guide prose, **Write** it to the printed `draft_path`, and **refine it with the
user in chat** until they sign off. Then:
```bash
python scripts/harness.py style-guide commit --project projects/<slug>
```
This parses, saves `style.json`, and validates it. If it prints a VALIDATION/PARSE error, fix the
draft and re-run `commit` (cap ~3 re-drafts, then hand-edit-or-abort).

**1e. STOP — G3: approval beat.** Present the final style guide, then **AskUserQuestion with exactly
two predefined options** — **"Approve all"** and **"Reject & talk it through"**. **Remind the user in
the question text that to approve *with specific changes* they should pick _Other_ and type the edits
directly** (e.g. "switch register to tú", "keep place names in English"). A custom (_Other_) answer is
approve-with-changes: apply the edits to the draft, re-run `style-guide commit`, and continue. This is
the user's chance to lock in the key decisions (dialect/locale, name conventions, register)
**before** they shape the glossary.

**Log the outcome** once decided: `log-event --event approval --data '{"beat":"style_guide",
"decision":"approved_first_pass"|"reject_then_redraft"|"approve_with_changes","redrafts":<n>}'`
(`redrafts` = how many times you re-ran `commit` after a validation/rejection before this approval).

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
- **AskUserQuestion with exactly two predefined options** — **"Approve all"** (accept the list as-is
  and continue) and **"Reject & talk it through"** (open-ended: END the turn, discuss, then re-draft /
  re-run `commit` and re-present this gate). **In the question text, remind the user that to approve
  *with specific changes* they should pick _Other_ and type the swaps directly** — e.g.
  `Gatito → Minino`, `keep "Granny Gray" untranslated`, or paste corrected JSON. A custom (_Other_)
  answer is approve-with-changes: apply exactly those swaps to the JSON at `draft_path`, re-run
  `commit`, briefly confirm what changed, and continue (the swap submission *is* the approval — don't
  loop back into this gate unless the user asks). Only continue once the user approves.
- **Log the outcome:** `log-event --event approval --data '{"beat":"glossary",
  "decision":"approved_first_pass"|"reject_then_redraft"|"approve_with_changes","redrafts":<n>}'`.

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
prefers one size). Either way this chunks once, prints the estimate, then halts — it runs
`--cost-only` and physically cannot spend. The estimate is **backend-neutral**: it shows the job
size, the metered-API price framed as *conditional* ("If translated via the metered API: ~$X"), and
a reminder that the subagent backend uses your subscription (no API $). Carry the dollar figure into
the Step 4 gate **only if** the user picks the API backend; on the subagent path (Step 4B) ignore it
— that path's gate is the `usage_summary` from `translate-prepare`, not this dollar estimate.

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
   python scripts/harness.py translate --project projects/<slug> --yes --model claude-sonnet-4-6
   ```
   `translate` refuses to run without `--yes`. The model defaults to the one set at `setup`; pass
   `--model` to override, and surface the choice rather than assuming.

## Step 4B — Subagent backend (zero-API-key, model-pinned) — ALTERNATIVE to Step 4

Two translation backends, same downstream pipeline. Pick one with the user:

- **API backend (Step 4):** fast, batchable, but needs an `ANTHROPIC_API_KEY` and spends metered dollars.
- **Subagent backend (this step):** no API key — translation runs as **spawned worker subagents on
  the running subscription**, so a stranger can translate token-free. You (the orchestrator, e.g.
  Opus) stay the smart driver while workers run on a **cheaper pinned model** (default `sonnet`).
  The dollar figure from `chunk`/`cost` is the **metered-API** price and does **not** apply here;
  this path's gate is the `usage_summary` from `translate-prepare` (4B-b), not a cost estimate.

Once the user picks, **log it**: `log-event --event backend --data '{"backend":"api"|"subagent",
"model":"<model>"}'` (records the model/cost path the int-return `translate` wrapper can't).

The translate phase is a **review-first, set-by-set** flow: translate a small batch, auto-align it so
it is instantly readable, then translate the rest with the same spawn settings. Do the beats in order
— do **not** improvise parallelism; the spawn mode is the user's call (4B-0b).

**4B-0. STOP — propose a review batch first.** Before translating anything, suggest translating
**10–20% of the book's chapters** as a concrete range (e.g. "chapters 1–6 of 40") so the user can read
a sample in the reader and catch glossary/voice problems before the whole book is spent. They may
accept it, give a different range, or choose **all chapters in one go**. END your turn and wait.
Record the choice as `<set>` (a `--chapters` spec like `1-6`, or "all" = omit `--chapters`).

**4B-0b. STOP — spawn-mode gate (ask once, then save).** *Immediately after* the batch is chosen and
**before any translation**, ask via AskUserQuestion how workers should be spawned. Three options;
**bias toward #2 (the default)**:

  1. **Sequential** — one chunk at a time, in order. Slowest, but every chunk after the first sees the
     previous chunk's **English + Spanish** (max continuity). Pick if continuity beats speed.
  2. **Chapter-parallel (recommended, default)** — run a **window of X chapters (default 8)** at once
     and **finish that window before moving on**. Within a window, spawn **wave by wave on chunk
     position**: first the opening chunk of every chapter in parallel, then each chapter's second
     chunk, etc. First chunks across chapters run concurrently; later chunks within a chapter wait for
     that chapter's previous chunk, so within-chapter EN+Spanish continuity is preserved.
  3. **All-parallel** — every chunk at once (in bounded batches). Fastest; **no** cross-chunk Spanish
     context (nothing is committed when prompts render). Pick only when speed clearly wins.

END your turn and wait. When answered, **save it** by passing it on the next `translate-prepare`
(`--parallelism sequential|chapter|all`, plus `--window <X>` for #2) — it is persisted to the project
config so the "translate the rest" batch reuses it without re-asking. Confirm X for mode 2 (default 8).
**Log the choice:** `log-event --event spawn_mode --data '{"mode":"sequential"|"chapter"|"all",
"window":<X>}'`.

**4B-a. Prepare (no spend).** Render one prompt per untranslated chunk in the set + a manifest, saving
the spawn mode:
```bash
python scripts/harness.py translate-prepare --project projects/<slug> --chapters <set> \
  --parallelism <mode> [--window <X>] [--worker-model sonnet]
```
This prints a `manifest` (each entry: `chunk_id`, `chapter_id`, `prompt_path`, `draft_path`), a
`usage_summary`, the `worker_model`, and the saved `spawn_plan` (`parallelism` + `window`). It does
**not** call an API. (Omit `--chapters` for the whole book.) Re-running only fills chunks that still
need a translation, so resume is free.

**4B-b. STOP — usage gate. END THE TURN.** The subagent analog of the cost gate: no dollars, but
spawning N workers consumes real subscription/rate usage. Show the `usage_summary` ("N workers on
`<model>`, mode `<parallelism>`"), confirm the worker model, and ask via AskUserQuestion: proceed /
abort. **End your turn and wait.** Do not spawn workers in the same turn that produced the manifest.

**4B-c. Spawn workers per the chosen mode, then commit.** Only after the user approves in a later turn.
Each worker uses the **Task** tool with `subagent_type: translator` (`.claude/agents/translator.md`),
`model:` the approved `worker_model` (how the worker is pinned cheaper than you), and the prompt:
*"Translate one chunk. Read `<prompt_path>`. Write ONLY the translated prose to `<draft_path>`. Nothing
else."* The worker writes its file — **do not** have it return the prose to you (that floods your
context). After a wave's drafts are written, commit:
```bash
python scripts/harness.py translate-commit --project projects/<slug>
```
which guards each draft (length / completeness / image-token parity / echo), writes provenance, stamps
the chunks, and prints `committed` / `failed` / `missing` / `skipped` (idempotent — done chunks are
skipped). Spawn according to the saved mode:

- **Sequential:** take the single lowest-position still-untranslated chunk, spawn **one** worker,
  `translate-commit`, then **re-run `translate-prepare`** (so the just-committed Spanish is baked into
  the next chunk's prompt) and repeat until the set is done.
- **Chapter-parallel (default):** work in windows of **X** chapters. For the current window:
  1. From the manifest, group entries by `chapter_id`; the **next wave** is the lowest-position
     still-untranslated chunk of each chapter in the window.
  2. Spawn those workers **in parallel** (multiple `Task` calls in one message), then `translate-commit`.
  3. **Re-run `translate-prepare --chapters <window>`** so each committed chunk's translation flows
     into its chapter's next chunk, and repeat from step 1 until every chunk in the window is committed.
  4. Only then advance to the next window of X chapters. Complete chapters, **not** "all first chunks
     first" — each window is fully finished before the next starts.
- **All-parallel:** spawn workers for **all** manifest entries in bounded batches of ~X (rate limits),
  `translate-commit` after each batch. No re-prepare (this mode has no cross-chunk Spanish context).

**4B-d. Re-spawn the misses.** For any `failed` (the report names the problem per chunk) or `missing`
(no draft written), re-spawn a worker for just those `chunk_id`s — write fresh prose to the same
`draft_path` — and re-run `translate-commit`. Cap re-spawns at ~3 per chunk, then surface the chunk
for a manual edit-or-skip decision rather than looping. **Log each re-spawn:** `log-event
--event respawn --data '{"chunk_id":"<id>","attempt":<n>,"reason":"failed"|"missing"}'`.

**4B-e. Align the set + give a reader link.** Once the set's chunks are all committed, make it readable
with no manual steps:
```bash
python scripts/harness.py align --project projects/<slug> --chapters <set>
```
This writes `alignments/<chapter>.json` for each fully-translated chapter and prints `reader_first` (a
link to the first chapter of the set). Ensure the reader is up — if nothing answers on port 5000,
start it in the background (`python web_ui/app.py`, serving `http://localhost:5000`). Then give the
user the `reader_first` link (e.g. `http://localhost:5000/read/<slug>/chapter_01`) so they can read the
new chapters immediately.

To also show a sample **in chat** (a quick EN→ES gut-check before spending the rest), use the read-back
command — never read `.harness/translate/*.draft.txt` (consumed/empty after commit) or hand-parse the
chunk files:
```bash
python scripts/harness.py show-translation --project projects/<slug> --chapters <set> --max-chunks 4
```
Read the result from `OUTPUT_JSON` (`.harness/last_output.json`). Committed translations live in
`projects/<slug>/chunks/*.json` — **`translated_text`** holds the target-language prose, **`source_text`**
the original. `--max-chunks` caps the sample; add `--no-source` for translation-only.

**4B-f. Translate the rest (if a subset was done).** If you only did a review batch, prompt the user to
translate the **remaining** chapters now, noting the **same spawn mode/window as before** will be used
(it is saved — you can omit `--parallelism`/`--window`). On yes, repeat 4B-a → 4B-e for the remaining
`--chapters` range. When the whole book is translated, continue to Step 5 (combine + EPUB).

Then continue to Step 5 (combine + EPUB) exactly as the API path does.

## Step 5 — Combine + EPUB (translated chapters only)

The API `translate` run chains through combine, epub, and align, building the EPUB from translated
chunks only and reporting exactly which chapters shipped. On the **subagent** path you already aligned
each set in Step 4B-e (the reader reads `alignments/`, not the EPUB), so here you only (re)build the
EPUB — the downloadable deliverable — from whatever chapters are translated so far:
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
  `translate-commit` path (Step 4B), which now supports user-chosen spawn modes (sequential /
  chapter-parallel / all-parallel). Still deferred: the **judge** backend and the portable
  `claude -p --model` worker (Approach C). See TODOS.md.
- No long-book resume beyond the pipeline's existing chunk-level idempotency (`stage_translate`
  skips chunks that already have a translation).
- No prompt caching (tracked separately in TODOS.md).
