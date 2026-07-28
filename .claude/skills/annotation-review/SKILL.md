---
name: annotation-review
description: |
  Review the annotations a reader left on a finished book and draft a resolution for
  each: a recommendation for word-choice doubts, a whole-book verdict for suspected
  inconsistencies, an actual endnote gloss for footnote marks, and an investigation
  for "Other" notes. Produces a dated markdown report, then — only with an explicit
  selection — appends a brief version back into the annotation itself. Three
  interchangeable backends: API (metered), Task subagents (no dollars), and headless.
  Use when asked to "review my annotations", "resolve the notes I left", "go through
  my reader annotations", "draft the footnote glosses", or "annotation-review".
allowed-tools:
  - Bash
  - Read
  - Task
  - AskUserQuestion
---

# annotation-review

The third LLM pass over a book, and the only **post**-human-review one. Translation
and `judge-review` both run before anyone reads the book; this runs after, over the
notes the reader left in the reader UI.

The deterministic surface is one non-interactive CLI —
**`scripts/review_annotations.py`** — with five subcommands that each print JSON.
You pick a scope, pick a backend, review the annotations, relay the report, and —
only with an explicit pick — write the resolutions back.

## What it reviews

The reader's four annotation types, from `projects/<slug>/annotations.jsonl`:

- **`word_choice`** — a questionable word. Weighed against the style guide, the
  glossary, the source sentence, and how the book already uses the alternatives.
- **`inconsistency`** — one concept rendered several ways. The book-wide
  concordance is the evidence: every use of the term, plus every Spanish rendering
  the book gave the English source term.
- **`footnote`** — a span the reader wants glossed. Drafts the actual endnote.
- **`flag`** ("Other" in the UI) — anything else. Infers the concern and proposes
  a course of action.

It is **note-only**. It never edits translated prose — that is `judge-review apply`.

## The CLI (read first)

`python scripts/review_annotations.py <prepare|fanout|commit|run|apply>` is
non-interactive and prints one JSON object with a `_schema` block documenting every
key.

**Windows / UTF-8 — force it on every Python you run.** `review_annotations.py`
reconfigures stdout to UTF-8, but **your own** ad-hoc `python -c` probes default to
the Windows console codepage and will mojibake every raya (—), guillemet («»), and
accent — the exact bytes these notes are made of. Always run diagnostics with
`python -X utf8 -c "..."`, open files with `encoding="utf-8"`, and prefer the `Read`
tool over piping CLI stdout into Python.

Scope flags (`prepare`, `run`):
- `--project <id|path>` — required.
- `--type word_choice,footnote` — comma-separated; default is **all four**.
- `--scope chapter:<chapter_id>` — repeatable; default is the **whole book**.
- `--target-language` — overrides `.harness/config.json`.

`prepare` adds `--worker-model` (default `sonnet`), `--batch-size` (default 5), and
`--keep-drafts`.

`fanout` takes `--project`, `--target-ids`, `--concurrency`, `--cli {claude,cursor}`,
`--cli-bin`.

`commit` takes `--project` and `--no-report`.

`run` adds `--model`, `--provider`, `--cost-limit` (default 0.50), `--confirm`.

`apply` takes `--select <key,key,...>` and `--dry-run`.

Keys look like `chapter_04__37__u72399176` (`<chapter>__<es_idx>__<sub_id>`).

## What gets skipped before any LLM call

`prepare` reports these in `skipped[]`, and they cost nothing:

| Reason | Meaning |
|---|---|
| `already_reviewed` | a previous run's text is still intact — **this is what stops notes duplicating across runs** |
| `imported` | a Gutenberg-imported footnote that already carries its body |
| `orphaned` | `es_idx` no longer resolves to an aligned sentence (usually a retranslation) |

`already_reviewed` is exact, not a judgement call: applied records carry an
`ai_review` sidecar recording the text written, and the gate compares it to the live
content. It self-heals — editing the note in the reader drops the sidecar, which
correctly re-opens the annotation.

Relay `orphaned` skips to the user. Those are notes stranded by a retranslation; no
run will ever reach them until they are re-anchored by hand.

## Three backends

All three build the same prompts and use the same parser, so the report is the same
shape whichever ran.

| | Spend | Gate |
|---|---|---|
| **API** (`run`) | metered $ on this repo's key | dollar cost gate |
| **Task subagents** (`prepare` → spawn → `commit`) | none (session usage) | usage gate |
| **Headless** (`prepare` → `fanout` → `commit`) | whatever the local CLI's auth bills | usage gate |

**Headless is "free" only on a subscription login.** `fanout` shells out to the
user's own `claude` / `cursor-agent`, so it bills whatever that CLI is
authenticated with — on an API-key login it spends metered credit, and the wave
fails mid-run when that balance is exhausted (`Credit balance is too low`, now
surfaced verbatim in `failed[].error`). Don't promise "no spend" for `fanout`
without knowing how their CLI is logged in. Task workers are the genuinely
free-of-dollars path.

**Prefer headless for a whole-book run.** A book carries 6–40 annotations, which is
one bounded wave with no per-wave turn ceremony, and it is the only path that gets
the preamble cache (~3.2k tokens shared per type, comfortably over Sonnet's 1024
minimum). Task workers are Claude-only; Cursor is offered on the headless path only.

## Flow

1. **Pick scope.** Default to the whole book and all four types — that is what the
   feature is for. Narrow only if the user asks.
2. **Pick backend.** If the user didn't say, ask. Default to **headless**.

### A. API backend

3a. **Dry-run for cost.** Run `run` WITHOUT `--confirm`. On `cost_exceeded`, relay
   the estimate and **stop**. Cost approval is a hard stop — never pass `--confirm`
   until the user approves in a separate turn.
4a. **Run.** Re-run with `--confirm`, then relay the report (below).

```bash
python scripts/review_annotations.py run --project fabre2 --confirm
```

### B. Subagent / headless backends

3b. **Prepare.** No spend:
```bash
python scripts/review_annotations.py prepare --project fabre2 \
    [--type word_choice,footnote] [--scope chapter:chapter_04] [--worker-model sonnet]
```
Relay `usage_summary` (targets, by_type, worker_model, batch_size;
`estimated_api_cost` is the API-equivalent price, shown for context — nothing is
spent) and the `skipped` breakdown.

**STOP — usage gate.** No dollars, but spawning N workers or running a wave consumes
real session/rate usage. Get approval in a **separate turn**, and ask which spawn
mode via `AskUserQuestion` unless already chosen:

1. **Headless fan-out (default)** — `fanout`, bounded concurrency, gets the preamble cache.
2. **Task workers** — spawn `annotation-worker` subagents, one wave per turn.
3. **Abort**

**End your turn and wait.** Do not spawn workers or run `fanout` in the same turn
that produced the manifest.

**Re-`prepare` is destructive.** It clears the drafts for the entries it re-renders,
so it must never run while you have uncommitted drafts in flight. Prepare the whole
request once; if you must re-prepare with good drafts present, pass `--keep-drafts`.

4b. **Run the wave.**

#### Option [1] Headless fan-out (default)
```bash
python scripts/review_annotations.py fanout --project fabre2 \
  [--cli claude|cursor] [--target-ids <k1,k2>] [--concurrency 5]
```
Cursor needs a Cursor model id (`grok-4.5`, `auto`) — `--worker-model sonnet` with
`--cli cursor` returns a warning. On 529, re-run with a lower `--concurrency`.

#### Option [2] Task workers
For each manifest entry spawn one worker with the **Task** tool:
`subagent_type: annotation-worker`, `model:` = the manifest's `worker_model`. Tell
each worker its `prompt_path` and `draft_path`: read the prompt, write ONLY the JSON
verdict to the draft, reply `done <key>`.

**Hard rule — never overlap waves.** Spawn at most `batch_size` workers in one turn
(multiple `Task` calls in one message), **end the turn**, confirm the drafts exist,
then spawn the next wave. On 529, step down `batch_size → 3 → 1`.

5b. **Commit.**
```bash
python scripts/review_annotations.py commit --project fabre2
```
Relay `committed` / `failed` / `missing`. Re-spawn any `failed` (bad JSON) or
`missing` (no draft) — headless: `fanout --target-ids <keys>` — then re-run
`commit`. Trust `commit`'s lists, not a spawn or fanout error string. Cap re-spawns
at ~3 per entry, then surface for manual review.

Committing after each wave is fine: `commit` **merges** into `results.json` by key,
so wave 2's commit does not discard wave 1's plan.

### Relaying the report

`commit` and `run` write a dated report to
`projects/<slug>/reports/annotations_<timestamp>.md` and print the same data as
`results[]`. `Read` the report and relay, grouped by type:

- the recommendation for each annotation, with its confidence;
- for footnotes, the drafted gloss itself — that text gets published, so the user
  should see it before approving;
- the `already_resolved` ones (nothing will be written to them);
- the `manual` ones and why, especially `multi_anchor`;
- the `Omitidas` section.

Flag every **`low` confidence** result explicitly, and every footnote gloss
asserting a hard fact (a date, a place, a person) — those are what a human most
needs to check.

### C. Apply (optional — after relaying the report)

Only after the report is relayed. `apply` is the only writer to `annotations.jsonl`.

6c. **Dry-run the plan.**
```bash
python scripts/review_annotations.py apply --project fabre2 --dry-run
```
Relay `applicable[]` as `old → new` previews and `manual[]` with each `reason`.

Two write modes, and the difference matters:
- **`footnote` → replace.** Its content *is* the published endnote text
  (`src/endnotes.py` strips the first bracket and publishes the rest), so the
  instruction word is dropped and the gloss takes its place under the same anchor.
  Nothing is lost: the original stays in the append-only log and in the report.
- **everything else → append**, after a `— IA:` marker.

7c. **Get an explicit selection.** The user picks which keys to apply — **never
apply without an explicit pick.** Use `AskUserQuestion` (multiSelect, ≤4 options)
when there are ≤4; otherwise present a numbered list and have the user reply with
keys. Hand-check each footnote gloss you offer: it is going into the book.

8c. **Apply the picked keys.**
```bash
python scripts/review_annotations.py apply --project fabre2 --select chapter_09__2__legacy,chapter_02__48__legacy
```
Relay `applied`, `already_applied`, `stale`, `unknown_ids`.

`stale` means the note changed between the review and the apply — the review no
longer describes what is on disk, so it was skipped rather than overwritten.
Re-`prepare` those. Re-running the same `--select` is a no-op (`already_applied`),
not a double write.

9c. **Rebuild the EPUB if footnotes changed.** Applied glosses only reach the book
on the next build:
```bash
python scripts/harness.py epub --project fabre2
```

## Notes

- Every annotation type reads the style guide (`style.json`) and glossary
  (`glossary.json`) automatically; `inconsistency` and `word_choice` also get a
  book-wide concordance built from `alignments/*.json`. Retrieval is deterministic
  Python at prepare time — the model reasons over evidence, it never goes looking.
- **Multi-anchor footnotes** (`[Neuve-Celle,]; [Esaú,]; [Montélimar.]`) are reviewed
  but never auto-written: endnotes consume only the first bracket, so the right fix
  is splitting the note into several, which renumbers endnotes — a human call. The
  report drafts a gloss per anchor so the user can split them by hand in the reader.
- `apply` writes append-only records with an `ai_review` sidecar. Nothing is ever
  rewritten in place, so every run is recoverable from the log.
- Adding a fifth annotation type means touching `web_ui/app.py:save_annotation`'s
  allowlist, the reader JS/CSS/i18n, `store.ANNOTATION_TYPES`, and a new
  `prompts/annotation_<type>.txt`. See `docs/ANNOTATION_REVIEW.md`.
- Judges are the *pre*-review pass over the same book; see
  `.claude/skills/judge-review/SKILL.md`. The two do not share persistence:
  annotation results never touch `evaluations/*.json` or the dashboard badges.
