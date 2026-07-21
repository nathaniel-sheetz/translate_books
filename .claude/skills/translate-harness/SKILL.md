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

**Reading harness output — Read the artifact, NEVER parse stdout.** **Every** command — including
the streaming ones (`chunk`/`cost`/`translate`/`epub`) — mirrors a **fresh** structured result to
`projects/<slug>/.harness/last_output.json` (UTF-8) and prints `OUTPUT_JSON: <path>` to stderr.
**Always `Read` that file** to consume a result — it is the only clean machine-readable surface,
and it is always the *current* command's result (the streaming commands used to leave the previous
command's payload here; they no longer do). Stdout is a *mixed human+JSON stream*: `translate-commit`
/ `chunk` / `epub` print progress lines and the full `terms`/counts dump *around* the JSON, so piping
stdout into a JSON parser (`... | python -c "json.load(sys.stdin)"`) fails immediately with
`Expecting value: line 1 column 1 (char 0)` on the leading non-JSON text. Don't learn this the hard
way — read `last_output.json`. (Same reason **never pipe harness output through `grep`**: on
Windows, accented/curly-quote bytes make `grep` treat the stream as binary and truncate it.)

**Windows / UTF-8 — force it on every Python you run.** The harness CLI and its wrapped
subprocesses already emit UTF-8, but **your own** ad-hoc `python -c` probes and one-off fix
scripts default to the Windows console codepage (cp437/cp1252) and mojibake every
raya (—), guillemet («»), and accent — exactly the characters dialogue judging cares about.
Always run diagnostics with `python -X utf8 -c "..."` (or set `PYTHONUTF8=1` for the session).
When reading project files in a probe, open them with `encoding="utf-8"` explicitly. Prefer
`Read` on `last_output.json` and chunk JSON over parsing harness stdout.

**Don't guess field names — read the `_schema`.** Every `last_output.json` carries a `_schema`
block mapping each result key (and nested shapes) to a one-line description. Read it to learn the
exact keys instead of probing with `python -c` (e.g. `status` chapters use `chunks`/`translated`/
`complete` under a top-level `totals`, *not* `total_chunks`/`pending_chunks`). The `_schema` is the
contract; the sibling keys are the data.

**Resuming a project? Run `status` first.** `python scripts/harness.py status --project <slug>`
reports, in one call, each chapter's translated-vs-pending chunk counts, the saved `spawn_plan`
(`parallelism`/`window`/`batch_size`), which artifacts exist (`style_guide`/`glossary`/`difficulty`/
`chunks`), any built EPUBs, and a `stage` (`pre-chunk`/`untranslated`/`partial`/`fully-translated`)
with a `next` hint. **Never hand-roll a loop over `chunks/*.json` to discover what's left** —
`status` (or, for the work list itself, `translate-prepare`, which already returns *only*
untranslated chunks) answers it directly. To read a past run back, `runs --project <slug>` summarizes
the command timeline + logged beats.

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
 (setup;         agent drafts          agent drafts      (det.; sizes  (det.)   translate ─►
  [FN keep/drop]) + refine + approval   + approval         chunks)               combine ─► align ─►
                                                                    [FN translate+apply beat] ─► epub
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

**Identify the book first, then let `setup` name the folder from its title.** Omit `--project`
and `setup` derives the project slug from `--title` (e.g. `"Understood Betsy"` →
`projects/understood-betsy`), so the folder, reader URLs (`/read/<slug>/chapter_01`), and EPUB
path all say what the book is — don't fall back to a cryptic Gutenberg id like `g5347`. If that
title-slug already exists, the new project gets a `-2`, `-3`, … suffix. Pass `--project <slug>`
explicitly only to **re-run on an existing project** (it's used verbatim and reuses that folder;
relying on `--title` would mint a new numbered folder instead).

**Don't launch a browser to identify the book.** To get the title/author for the slug, make a
single `WebFetch` on the source URL. Never invoke the `/browse` (or any browser) skill here: it
loads its whole ~600-line skill body into context just to read one fact off a static page.
`CLAUDE.md`'s "use `/browse` for all web browsing" rule is about interactive/QA browsing of the app
under development — reading a public page is the carve-out.

```bash
python scripts/harness.py setup \
  --target-lang Spanish --locale mx \
  --title "<Title>" --author "<Author>"
# add --url <gutenberg-url> if there is no local source.txt yet.
# add --project <slug> only to re-run on / target a specific existing project folder.
```

`--chapter-pattern` now **defaults to `auto`**, which detects the best-fit pattern from the
source text itself — you rarely need to set it. The named patterns are still selectable:
`roman` (Chapter I, II …), `numeric` (Chapter 1, 2 …), the titled variants
`chapter_roman_titled` / `chapter_numeric_titled` (a title on the *same* line, e.g.
`CHAPTER I. WATHO.` — the common Gutenberg shape), `allcaps_heading`, `bare_roman`, or
`custom` (with `--custom-regex`). On **both** the `--url` and the local `source.txt` path,
`setup`/`split`/`split-preview` now return `pattern_used` (what it split on), `suggested_pattern`
(what the text/HTML implies), and a `chapter_report`. If `pattern_used` ≠ `suggested_pattern`,
or `warnings` is non-empty (e.g. "1 chapter for an 87 KB source"), the split is probably wrong —
re-run with the suggestion. Confirm the printed `chapter_count` looks right and `chunks_dir_exists`
is `false`. (The lang/locale defaults are Spanish/mx — surface them to the user rather than
assuming silently. The model is **not** chosen here; on the API path it is confirmed at the
Step 4 cost gate, and on the subagent path the worker tier is chosen at Step 4B.)

Navigation/boilerplate (the title page, a `CONTENTS`/table-of-contents listing, a list of
illustrations, a copyright/transcriber's note) is **auto-stripped** — never written, numbered, or
translated — and each stripped heading is reported back under `dropped` in the `setup` /
`split-preview` / `split` output. Confirm `dropped` matches what you expected. Real front matter
(foreword, preface, prologue, dedication, author's note …) is auto-detected and **kept**, and it
renders its *translated* heading in the EPUB automatically — no manual relabel.

**Footnotes — keep as reader footnotes, or drop?** On the `--url` path, `setup` **imports**
Gutenberg footnotes by default: it captures each note as a survivable `[FOOTNOTE:N]` token in the
body plus a `footnotes.json` sidecar, and reports `footnotes_detected` (count) and `footnotes_mode`
(`import`) in its output. **If `footnotes_detected > 0`, STOP and ask the user** — AskUserQuestion
with two options, *"Keep as translatable reader footnotes"* and *"Drop them"* — since footnotes
noticeably change the reader experience and add a small paid step later (Step 4C). Then:
- **Keep** → nothing to run; the tokens ride through split/chunk/translate untouched. Tell the user
  the note bodies get translated after the chapters, at Step 4C. `log-event` the decision.
- **Drop** → run `python scripts/harness.py footnotes drop --project projects/<slug>`, which strips
  the tokens from `source.txt` + chapters and deletes the sidecar (no re-fetch). `log-event` it.

Footnote detection only happens on the `--url`/HTML path — a project seeded from a local
`source.txt` can't detect or import them (`footnotes_detected` is `0`), so there is nothing to ask.

**Refine the split if it looks wrong** — the `setup` split misfires, reports "No chapters
detected," or a *real* section is mis-numbered as a chapter (or vice-versa). Don't hand-edit
`source.txt`; use the review beat, which mirrors the web GUI's Stage 2:

```bash
# Dry-run: prints each section tagged front_matter / chapter / back_matter,
# plus a `dropped` list of stripped boilerplate. Writes nothing.
python scripts/harness.py split-preview --project projects/<slug> \
  --chapter-pattern custom --custom-regex '(?<=\n---\n\n)[A-Z][^\n]*' \
  --min-chapter-size 500 \
  --front-matter-title "To the Teacher" --back-matter-title "A Word to the Children"

# Happy with the preview? Commit it (rewrites chapters/, clearing any stale files).
python scripts/harness.py split --project projects/<slug>  # + the same split flags
```

**Force-tagging KEEPS a section, it never removes one.** `--front-matter-title` /
`--back-matter-title` are repeatable and force a *real* heading the keyword auto-detect missed
(e.g. "To the Teacher") to be tagged matter so it isn't mis-numbered as a chapter — the section is
still written, translated, and included. **Do not** declare the title page / `CONTENTS` /
boilerplate here: that would un-strip them and push the junk through the whole pipeline. They drop
on their own; leave them alone. Built-in keyword auto-detect (preface, dedication, epilogue …)
stays on unless you pass `--no-auto-front-matter` / `--no-auto-back-matter`; boilerplate
auto-strip stays on unless you pass `--no-auto-strip` (only needed for the rare book with a genuine
chapter literally titled "Contents"). Raising `--min-chapter-size` (~500) drops short stray
front-matter lines a loose pattern would otherwise capture. All of these controls also work
directly on `setup` for a one-shot run.

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
the printed `draft_path`. **Write the target language WITH its diacritics** (Spanish: á é í ó ú ñ
¿ ¡). The `Write` tool is UTF-8 — do **not** ASCII-fold "to be safe"; stripped accents (`Tia` for
`Tía`, `senor` for `señor`) become the canonical forms fed verbatim to every translator worker.
As you draft, **track every term whose translation you were unsure about**
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
- **Surface any `warnings`** — if `commit`'s result carries a non-empty `warnings` array (e.g. an
  accent-stripping smell when the target language carries diacritics), re-read `glossary.json`, fix
  any ASCII-folded terms, re-run `commit`, **then** present the gate.
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

## Step 2B — ADDRESS MAP beat (OPTIONAL — never blocks translation)

A per-book **forms-of-address map** (`projects/<slug>/address_map.json`) records
which pairs of characters use **usted** vs. **tú**, including public/private and
story-stage differences. It powers the later `address` compliance judge
(judge-review); it does **not** affect this translation run.

**This beat is optional and non-blocking. Offer it; do not require it.** Ask the
user (plain question, not a gate) whether they want to build the address map now.
If they decline or don't care, **skip straight to Step 3** — translation proceeds
normally, and the map can be built later (here, or from judge-review's setup
precheck) whenever they want to run the usted/tú judge.

If they opt in:
```bash
python scripts/harness.py address-map prepare --project projects/<slug>
```
This samples the book's highest interpersonal-dialogue chapters (a spread across
the whole book, not just the openers) and renders a prompt at `prompt_path`. Read
it, draft the map JSON (`{content, pairs, global_rules}`) to the printed
`draft_path` — each non-empty direction must end with a `when:"default"` rule —
refine it with the user, then:
```bash
python scripts/harness.py address-map commit --project projects/<slug>
```
**STOP — approval beat** (same shape as glossary): present the committed pairs +
global rules, AskUserQuestion (Approve all / Reject & talk it through; a custom
answer = approve-with-edits → re-commit). Log:
`log-event --event approval --data '{"beat":"address_map","decision":...}'`.
Then continue to Step 3.

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
   it as NOT approved and ask again. **Confirm the model with them here** — this is the only
   place on the API path where the model is chosen; it determines the price shown in the estimate.
   > Cost note (eng review 2026-06-05): the API path does not use prompt caching today, so input
   > tokens are not discounted across chunks. The estimate is the honest figure.
3. **Only once the user has affirmatively approved in a separate turn**, translate:
   ```bash
   python scripts/harness.py translate --project projects/<slug> --yes
   ```
   `translate` refuses to run without `--yes`. The model defaults to Sonnet 5 (or whatever was
   persisted in config); pass `--model` to override, and surface the choice rather than assuming.

**If footnotes were imported, do Step 4C before Step 5** to translate + embed them (the API
`translate` run auto-chains through combine/epub/align, but not the footnote *body* translation).

## Step 4B — No-API-key backends (subagent / headless, model-pinned) — ALTERNATIVE to Step 4

**Three** translation backends share one downstream pipeline. This is a single first-class choice —
pick ONE with the user (bias toward a no-API-key backend when there is no `ANTHROPIC_API_KEY`):

- **API (Step 4):** fast, batchable, but needs an `ANTHROPIC_API_KEY` and spends metered dollars.
- **Subagent — Task workers (this step):** no API key — each chunk is translated by a `translator`
  **Task subagent** you spawn (Read→Write→`done`). You (the orchestrator, e.g. Opus) stay the smart
  driver while workers run on a **cheaper pinned model** (default `sonnet`).
- **Headless — `claude -p` fan-out (this step):** no API key — the harness runs one bounded wave of
  `claude -p` worker processes (`translate-fanout`) instead of Task subagents. Same pinned model and
  spawn modes; it additionally reuses a shared preamble cache on Sonnet (see the cache note in 4B-b).

Both no-API-key backends run **spawned workers on the running subscription**, so a stranger can
translate token-free; the dollar figure from `chunk`/`cost` is the **metered-API** price and does
**not** apply — their gate is the `usage_summary` from `translate-prepare` (4B-b), not a cost estimate.

Once the user picks, **log it** — this single beat is what every later step reads back to stay on the
same backend, including **footnote translation** (Step 4C carries it forward automatically):
`log-event --event backend --data '{"backend":"api"|"subagent"|"headless","model":"<model>"}'`.

The translate phase is a **review-first, set-by-set** flow: translate a small batch, auto-align it so
it is instantly readable, then translate the rest with the same spawn settings. Do the beats in order
— do **not** improvise parallelism; the spawn mode is the user's call (4B-0b).

**4B-0. STOP — propose a review batch first.** Before translating anything, suggest translating
**10–20% of the book's chapters** as a concrete range (e.g. "chapters 1–6 of 40") so the user can read
a sample in the reader and catch glossary/voice problems before the whole book is spent. They may
accept it, give a different range, or choose **all chapters in one go**. END your turn and wait.
Record the choice as `<set>` (a `--chapters` spec like `1-6`, or "all" = omit `--chapters`).

**4B-0b. STOP — spawn-mode gate (ask once, then save).** *Immediately after* the batch is chosen and
**before any translation**, ask via AskUserQuestion how workers should be spawned.

> **Skip this gate when the spawn mode is moot.** If `status` (or `translate-prepare`) reports
> `spawn_mode_moot: true` — i.e. every chapter is a single chunk — the three modes below are
> **equivalent** (there is no later chunk to inherit a previous chunk's Spanish, so "continuity"
> buys nothing). Don't make the user choose: just use all-parallel in bounded batches of
> `batch_size` and move on. The gate matters only for books with multi-chunk chapters.

Otherwise, three options; **bias toward #2 (the default)**:

  1. **Sequential** — one chunk at a time, in order. Slowest, but every chunk after the first sees the
     previous chunk's **English + Spanish** (max continuity). Pick if continuity beats speed.
  2. **Chapter-parallel (recommended, default)** — run a **window of X chapters (default 3)** at once
     and **finish that window before moving on**. Within a window, spawn **wave by wave on chunk
     position**: first the opening chunk of every chapter in parallel, then each chapter's second
     chunk, etc. First chunks across chapters run concurrently; later chunks within a chapter wait for
     that chapter's previous chunk, so within-chapter EN+Spanish continuity is preserved. Keep
     **`window ≤ batch_size`** so a first-position wave (one worker per chapter in the window) never
     exceeds the fan-out throttle; the defaults (window 3, batch_size 3) match.
  3. **All-parallel** — every chunk at once (in bounded batches). Fastest; **no** cross-chunk Spanish
     context (nothing is committed when prompts render). Pick only when speed clearly wins.

END your turn and wait. When answered, **save it** by passing it on the next `translate-prepare`
(`--parallelism sequential|chapter|all`, plus `--window <X>` for #2) — it is persisted to the project
config so the "translate the rest" batch reuses it without re-asking. Confirm X for mode 2 (default 3).
**Log the choice:** `log-event --event spawn_mode --data '{"mode":"sequential"|"chapter"|"all",
"window":<X>}'`.

**4B-0c. STOP — worker-thinking gate (ask once, then save).** *Only when the chosen `worker_model` is
thinking-capable* (`sonnet`/`opus`/`haiku` — a full-id worker resolves via `model_supports_thinking`),
ask via AskUserQuestion whether workers should engage **extended "think hard" thinking** — **default
No**. Extended thinking gives the worker room to reason through tricky passages, but is slower and burns
more subscription usage per chunk; leave it off unless the book's difficulty warrants it. **Skip this
question entirely for a non-thinking worker** (e.g. `fable`, always-on) — mirroring the GUI hiding the
checkbox — and just proceed with thinking off. Persist the answer by passing `--worker-thinking` (yes)
or `--no-worker-thinking` (no) on the next `translate-prepare`; it is saved to project config and reused
by the "translate the rest" batch (4B-f) without re-asking. **Log the choice:** `log-event --event
thinking --data '{"worker_thinking":true|false}'`.

**4B-a. Prepare (no spend).** Render one prompt per untranslated chunk in the set + a manifest, saving
the spawn mode:
```bash
python scripts/harness.py translate-prepare --project projects/<slug> --chapters <set> \
  --parallelism <mode> [--window <X>] [--worker-model sonnet] [--worker-thinking]
```
This prints a `manifest` (each entry: `chunk_id`, `chapter_id`, `prompt_path`, `draft_path`, and when
the cacheable prefix is stable across chunks also `preamble_path` + `body_path`), a `usage_summary`,
the `worker_model`, and the saved `spawn_plan` (`parallelism` + `window`). It does **not** call an
API. (Omit `--chapters` for the whole book.) Re-running only fills chunks that still need a
translation, so resume is free. The shared preamble lives at `.harness/translate/preamble.txt`;
per-chunk bodies at `.harness/translate/<id>.body.txt` — `preamble + body` is byte-identical to
`<id>.prompt.txt` when those paths are present.

**4B-b. STOP — usage gate. END THE TURN.** The subagent analog of the cost gate: no dollars, but
spawning N workers consumes real subscription/rate usage. Show the `usage_summary` ("N workers on
`<model>`, mode `<parallelism>`, **thinking: on/off**" — read the thinking state from
`usage_summary.worker_thinking`), confirm the worker model **and the backend already chosen** in the
Step 4B intro, and ask via AskUserQuestion: **proceed / abort**. When approved, spawn per that
backend — **Subagent (Task workers)** → Option [1] below; **Headless** → `translate-fanout`
(Option [2]).

**Worker-model / cache note (fold into this confirmation, not a new gate):** the ~2k-token shared
preamble clears **Sonnet's** 1024-token cache minimum (caches for free on the headless path after
chunk 1) but **not** Opus/Haiku's 4096. Prefer a **Sonnet** worker for headless caching; Haiku/Opus
cache only if the preamble is later enlarged (full glossary in the prefix — follow-on). Task workers
do not get cross-invocation prompt caching either way. **Stable prefix:** headless caching needs
`preamble_path`/`body_path` on the manifest — prefer `always_include_dialogue` (and
`always_include_image_instructions` when the book has images) so dialogue/image opt-ins don't make
the prefix diverge across chunks. Without those, mixed chapters silently fall back to the full
`prompt.txt` (correct, just uncached). Headless puts the preamble in `--system-prompt-file`
(system role) for Claude Code caching; Task/API keep prefix+suffix as one user prompt — intentional
divergence, not a bug.

**End your turn and wait.** Do not spawn workers or run `translate-fanout` in the same turn that
produced the manifest. (The backend was already logged in the Step 4B intro; no separate
`fanout_mode` beat is needed.)

**4B-c. Spawn workers per the chosen mode, then commit.** Only after the user approves in a later turn.

### Option [1] Task workers (default)

Each worker uses the **Task** tool with `subagent_type: translator` (`.claude/agents/translator.md`),
`model:` the approved `worker_model` (how the worker is pinned cheaper than you), and the prompt:
*"Translate one chunk. Read `<prompt_path>`. Write ONLY the translated prose to `<draft_path>`. Then
reply with exactly `done <chunk_id>` and nothing else — no summary, no list of choices."* **When the
manifest's `worker_thinking` is `true`, add the "think hard" trigger** so the worker engages extended
thinking: *"Translate one chunk. Read `<prompt_path>`. **Think hard** about the tricky passages, then
write ONLY the translated prose to `<draft_path>`. Then reply with exactly `done <chunk_id>` and nothing
else — no summary, no list of choices."* When `worker_thinking` is `false` (the default), use the plain
prompt above (no keyword → no extended thinking).

The worker writes its file and reports back only that token — **do not** have it return the prose *or a
recap of its choices* to you (either one floods your context). You learn each worker's success from
`translate-commit`'s `committed`/`failed`/`missing` lists, not from its chat-back.

### Option [2] Headless fan-out

Run one wave via the harness (bounded parallelism, neutral cwd, no Task turns). Pass `--chunk-ids`
when the spawn mode only wants the current wave's entries (chapter-parallel / sequential); omit it to
fan out every still-undrafted manifest entry (all-parallel batches):
```bash
python scripts/harness.py translate-fanout --project projects/<slug> \
  [--chunk-ids <id1,id2,...>] [--concurrency <batch_size>]
```
Each process is effectively:
`claude -p` with the body (or full prompt) on stdin, optional `--system-prompt-file <preamble_path>`,
`--model <worker_model>`, `--tools ""`, `--output-format text` → `draft_path`. The system-prompt
split is used only when `preamble + body` still equals `prompt.txt`; otherwise fan-out falls back
to the full prompt (no cache). Headless does **not**
use extended "think hard" thinking. After the wave, commit as below — the prepare→commit seam is
unchanged (`committed`/`failed`/`missing`).

After a wave's drafts are written (Task or headless), commit:
```bash
python scripts/harness.py translate-commit --project projects/<slug>
```
which guards each draft (length / completeness / image-token parity / echo), writes provenance, stamps
the chunks, and prints `committed` / `failed` / `missing` / `skipped` (idempotent — done chunks are
skipped).

`translate-commit` also **auto-runs and persists the coded evaluators** (`length, paragraph, dictionary, glossary, completeness, blacklist, grammar`) for each newly-committed chunk, so Review-tab badges update without any separate evaluate step.

> **Waiving a confirmed guard false-positive (`--allow-problem`).** Rarely a guard flags a chunk that is
> actually fine — e.g. the placeholder check trips on a legitimate Roman numeral heading. When you have
> *confirmed* the `failed` problem is spurious (read the named problem and the draft), re-commit with
> `--allow-problem <substring>` (repeatable) to drop only that problem:
> ```bash
> python scripts/harness.py translate-commit --project projects/<slug> --allow-problem XXX
> ```
> Every other guard stays enforced (a real defect still lands the chunk in `failed`), and the waive is
> reported under `waived` and recorded in the chunk's provenance log. Use this instead of hand-writing a
> stamping script. Do **not** blanket-waive — match the smallest substring of the specific false-positive.

> **Spawning into a flaky API — probe, throttle, commit-then-check.** Worker spawns can fail when the
> API is degraded. Handle it deterministically instead of hammering:
> - **Probe before a big wave.** After *any* spawn failure (or a known incident), spawn **ONE** worker
>   (Task) or `translate-fanout --chunk-ids <one_id>` first and confirm it writes a draft before
>   fanning out. A 1-worker probe discovers an outage at a fraction of the context/usage cost of a
>   failed full wave.
> - **`500` vs `529` are opposite signals.** A **500** is a server outage — concurrency is irrelevant;
>   **wait / back off**, don't change batch size, don't spam retries (pause and tell the user if it
>   persists). A **529** is *overloaded* — **reduce concurrency**: step the wave down the ladder
>   `batch_size → 3 → 1` until drafts land, then ramp back up toward `batch_size`.
> - **Commit-then-check, regardless of the Agent / claude -p error.** A Task worker often `529`s on
>   its final *wrap-up* turn **after** it already wrote a valid draft (you'll see `tool_uses: 2`). A
>   killed or partial `claude -p` leaves `missing` instead. So an error is **not** a reliable "no draft"
>   signal: after every wave (success or error) run `translate-commit` and trust its
>   `missing`/`failed` lists — not the spawn error text — to decide what to re-spawn. This avoids
>   re-translating chunks that already landed.

Spawn according to the saved mode (each wave is `batch_size` workers wide unless throttling down).
For **headless**, replace each "spawn Task workers" step with `translate-fanout` (pass `--chunk-ids`
for the wave's entries; pass `--concurrency` when throttling):

- **Sequential:** take the single lowest-position still-untranslated chunk, spawn **one** worker,
  `translate-commit`, then **re-run `translate-prepare`** (so the just-committed Spanish is baked into
  the next chunk's prompt) and repeat until the set is done.
- **Chapter-parallel (default):** work in windows of **X** chapters. For the current window:
  1. From the manifest, group entries by `chapter_id`; the **next wave** is the lowest-position
     still-untranslated chunk of each chapter in the window.
  2. Spawn those workers **in parallel** (multiple `Task` calls in one message, or one
     `translate-fanout --chunk-ids ...`), then `translate-commit`.
  3. **Re-run `translate-prepare --chapters <window>`** so each committed chunk's translation flows
     into its chapter's next chunk, and repeat from step 1 until every chunk in the window is committed.
  4. Only then advance to the next window of X chapters. Complete chapters, **not** "all first chunks
     first" — each window is fully finished before the next starts.

  Re-preparing a **narrower** scope no longer wipes a just-finished wave: `translate-prepare`
  keeps any non-empty `.draft.txt` on disk and **rescues** mappable uncommitted drafts into
  the new manifest (reported as `rescued_prior_drafts`), so `translate-commit` can still land
  them. Prefer committing a wave before re-preparing; unmappable or unreadable drafts stay on
  disk untouched. `window` is clamped to `batch_size` when it would exceed the fan-out throttle.
- **All-parallel:** spawn workers for **all** manifest entries in bounded batches of `batch_size`
  (the saved fan-out width; rate limits), `translate-commit` after each batch. No re-prepare (this mode
  has no cross-chunk Spanish context). This is also the mode to use whenever `spawn_mode_moot` is true.
  Headless: one `translate-fanout` call already waves at `batch_size`; then commit.

**4B-d. Re-spawn the misses.** For any `failed` (the report names the problem per chunk) or `missing`
(no draft written), re-spawn a worker for just those `chunk_id`s — Task spawn, or
`translate-fanout --chunk-ids <ids>` — write fresh prose to the same `draft_path` — and re-run
`translate-commit`. Cap re-spawns at ~3 per chunk, then surface the chunk for a manual edit-or-skip
decision rather than looping. **Log each re-spawn:** `log-event --event respawn --data
'{"chunk_id":"<id>","attempt":<n>,"reason":"failed"|"missing"}'`.

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
Read the result from `OUTPUT_JSON` (`.harness/last_output.json`) — do **not** write a `python` probe that
guesses the shape (it is **not** a top-level `chunks`/`items`/`samples` list). The structure nests
`chapters[] → chunks[] → translated_text`: the first chunk's prose is
`result["chapters"][0]["chunks"][0]["translated_text"]`, and its English is the sibling `["source_text"]`.
Committed translations also live in `projects/<slug>/chunks/*.json`. `--max-chunks` caps the sample; add
`--no-source` for translation-only.

**4B-f. Translate the rest (if a subset was done).** If you only did a review batch, prompt the user to
translate the **remaining** chapters now, noting the **same spawn mode/window as before** will be used
(it is saved — you can omit `--parallelism`/`--window`). On yes, repeat 4B-a → 4B-e for the remaining
`--chapters` range. When the whole book is translated, continue (Step 4C if footnotes were imported,
then Step 5 — combine + EPUB).

Then continue exactly as the API path does — **Step 4C if footnotes were imported**, then Step 5
(combine + EPUB).

## Step 4C — Footnotes: translate the notes, then embed them (only if imported)

**Skip this whole step unless footnotes were imported** (`projects/<slug>/footnotes.json` exists /
`setup` reported `footnotes_mode: import` and the user didn't drop them). This runs **after** the
chapters are translated **and aligned** (the embed reads `alignments/`), and **before** the final
EPUB.

Footnotes translate on the **same backend you chose for the chapters** (Step 4/4B) — the note bodies
never force a different path. `footnotes translate` resolves it automatically from the `backend`
run-log beat (`--backend {auto,api,headless,subagent}`, default `auto`; pass an explicit value only to
override). Once the chapters are done, **STOP and ask the user (its own beat): "Translate the
footnotes now?"** — a separate-turn go-ahead, never folded into an earlier approval. Notes are few and
short.

- **Yes** → in a later turn, translate on the resolved backend, then embed. Pick the matching path:

  - **API backend** — a metered step (like Step 4): confirm cost in this beat, then
    ```bash
    python scripts/harness.py footnotes translate --project projects/<slug> --yes
    ```
    `footnotes translate` refuses without `--yes` on the API backend.
  - **Headless backend** — no dollars (subscription usage), so no `--yes`. One command runs a
    `claude -p` wave and writes the bodies back:
    ```bash
    python scripts/harness.py footnotes translate --project projects/<slug>
    ```
  - **Subagent (Task) backend** — spawn workers exactly like the chapters:
    ```bash
    python scripts/harness.py footnotes translate-prepare --project projects/<slug>
    ```
    Then spawn one `translator` Task subagent per manifest `entries[]` item (`.claude/agents/translator.md`,
    pinned `worker_model`): *"Read `<prompt_path>`. Write ONLY the `N| <translation>` lines to
    `<draft_path>`. Reply `done <batch_id>`."* Then land them:
    ```bash
    python scripts/harness.py footnotes translate-commit --project projects/<slug>
    ```
    `translate-commit` reports `committed` / `pending`; re-prepare + re-spawn any `pending` notes.

  In every case, once the bodies are filled, embed them:
  ```bash
  python scripts/harness.py footnotes apply --project projects/<slug>
  ```
  `footnotes apply` converts every surviving `[FOOTNOTE:N]` token into an anchored reader footnote,
  strips the raw tokens from the stored translation, and **rebuilds the EPUB** with a numbered
  back-matter section.
- **No** → still run **`footnotes apply`** alone, so the raw `[FOOTNOTE:N]` tokens don't leak into
  the EPUB as literal text. Warn the user the notes will appear in the **source language**
  (untranslated); they can translate them later (`footnotes translate` [`--yes` on the API backend],
  then `footnotes apply`).

`footnotes apply` is idempotent — on the **API path**, `translate` already auto-ran the footnotes
stage once (with source text, since the bodies weren't translated yet), and re-applying simply
re-converts with the translated bodies (prior imported-footnote annotations are replaced). Running
Step 5 `epub` afterward stays consistent — it renders the persisted footnote annotations.

## Step 5 — Combine + EPUB (translated chapters only)

The API `translate` run chains through combine, epub, and align, building the EPUB from translated
chunks only and reporting exactly which chapters shipped. On the **subagent** path you already aligned
each set in Step 4B-e (the reader reads `alignments/`, not the EPUB), so here you only (re)build the
EPUB — the downloadable deliverable — from whatever chapters are translated so far:

> **Stitching contract.** Chunks are created with **zero overlap**, so `combine` is a plain
> concatenation of each chunk's translation (one blank line at every boundary) — there is no
> overlap de-dup, on either backend. The prompt's "previous section" block is **continuity context
> only and is never re-combined.** Overlap/combine de-dup is disabled (known-broken): `combine`
> hard-fails if a chunk ever carries overlap. So a worker must translate its **whole** chunk and
> never drop content that also appears in the previous-section block. See
> `docs/design/TRANSLATE_HARNESS_FRICTION_LOG_4.md` #20.

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

- No `TranslationBackend` Protocol. The backends share one prompt builder
  (`build_translation_prompt`) and one stamp (`apply_translation`), so the seam is a few functions,
  not a class hierarchy. The no-API-key backends (Phase B) are the `translate-prepare` /
  `translate-commit` path (Step 4B), with user-chosen spawn modes (sequential /
  chapter-parallel / all-parallel) and a first-class choice between Task workers and headless
  `translate-fanout` (`claude -p`). Footnote translation (Step 4C) carries the chosen backend forward
  through the same seam (`footnotes translate` for api/headless; `footnotes translate-prepare` /
  `translate-commit` for Task workers). Still deferred: the **judge** headless backend (see
  `docs/design/headless-judge-review-backend.md` when present) and enlarging the translation
  preamble so Opus/Haiku clear the 4096-token cache minimum.
- No long-book resume beyond the pipeline's existing chunk-level idempotency (`stage_translate`
  skips chunks that already have a translation).
