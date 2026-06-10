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

## Step 1 — STYLE GUIDE beat (agent drafts, refine loop, approval gate)

**1a. Gather questions and present them:**
```bash
python scripts/harness.py style-guide prepare-questions --project projects/<slug>
```
This prints `detected_features`, the `questions` (each with `id`, `question`, `options`, and a
`hint`), and an `answers_path`.

**1b. Collect answers inline, question by question.** Ask each question in chat with its options
and hint. Record the chosen **option index** (0-based) or **custom string** under the question's
`id`. Let the user revise earlier answers. Then **Write** the dict to the printed `answers_path`:
`{question_id: index_or_string}`.

**1c. Generate follow-up questions (you are the LLM).**
```bash
python scripts/harness.py style-guide prepare-followups --project projects/<slug>
```
Read the printed `prompt_path`, draft the follow-up questions as a JSON array, **Write** them to
the printed `draft_path`, then:
```bash
python scripts/harness.py style-guide commit-followups --project projects/<slug>
```
Ask the printed `new_questions` inline, then **rewrite `answers_path` with the full answer set**
(prior answers + the new ones).

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

**1e. STOP — approval beat.** Present the final style guide. Approve / edit / re-draft. This is the
user's chance to lock in the key decisions (dialect/locale, name conventions, register) **before**
they shape the glossary.

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
This prints `book_difficulty` and a `suggested_target_size` (N), plus a per-chapter table.
Present the book difficulty and that suggested `N`. Treat `N` as the **default**: honor a user
override; otherwise chunk at `N`. (If `wordfreq` isn't installed, `wordfreq_available` is `false`
and the suggestion leans toward 2000 — still usable.)

**3b — Chunk at that size (estimator only).**
```bash
python scripts/harness.py chunk --project projects/<slug> --size <N>
```
This chunks once and prints the cost estimate, then halts — it runs `--cost-only` and physically
cannot spend. Carry that estimate straight into the Step 4 gate below.

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

- No `TranslationBackend` abstraction — translation goes straight through the existing API path.
  The subagent backend + a backend Protocol are deferred until Approach B is scheduled (eng review
  D9). `scripts/harness.py` is orchestration only; it adds no new translation business logic.
- No long-book resume beyond the pipeline's existing chunk-level idempotency (`stage_translate`
  skips chunks that already have a translation).
- No prompt caching (tracked separately in TODOS.md).
