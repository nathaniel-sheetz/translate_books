---
name: footnote-pass
description: |
  Author new editorial footnotes for a finished translation: read the notes the book
  already carries to infer why they were written, scan chapters for more spots
  deserving the same treatment, research each surviving candidate, and write the
  gloss into the right place in the text — validated, so a note can never land as a
  silent orphan. Two interchangeable scan backends: headless (default) and Task
  subagents, both subscription-only, no dollars.
  Use when asked to "add editorial footnotes", "add a footnote to chapter N", "scan
  these chapters for footnote candidates", "find more places that need a note",
  "write the gloss for this sentence", or "footnote-pass".
allowed-tools:
  - Bash
  - Read
  - Write
  - WebSearch
  - WebFetch
  - Task
  - AskUserQuestion
---

# footnote-pass

The **create** path for reader footnotes. Everything else in this repo reviews or
publishes notes that already exist; this is the only surface that mints one.

The deterministic surface is one non-interactive CLI —
**`scripts/footnote_pass.py`** — with six subcommands that each print JSON. You read
the book's existing notes, agree a profile, scan, cut, research, draft, and write.
Three STOP gates sit on that path and none of them is optional.

## What this writes

New `type: "footnote"` records in `projects/<slug>/annotations.jsonl`, stamped
`origin: "footnote_pass"`. They become numbered endnotes in the book's back matter
on the next `harness.py epub`, and the reader UI shows them like any other note.

**This is not `annotation-review`.** That skill resolves notes a human already left —
its `apply` replaces the text of an existing annotation and cannot create one.
Reaching for it to add a footnote is how the 2026-09-10 `fabre2` run ended up
reverse-engineering `web_ui/app.py:save_annotation` from source before it could write
a byte.

**This is not `harness.py footnotes`.** That converts `[FOOTNOTE:N]` tokens from a
Gutenberg source into `origin: "gutenberg"` records — the author's own notes,
imported. These are *editorial* notes, written now, about the text.

The three converge only at the EPUB, where `src/endnotes.py` numbers whatever it
finds, whoever wrote it.

## The CLI (read first)

`python scripts/footnote_pass.py <style|scan-prepare|scan-fanout|scan-commit|add|verify>`
is non-interactive and prints one JSON object. Every command also mirrors its payload
to `projects/<slug>/.harness/footnotes/last_output.json` and prints
`OUTPUT_JSON: <path>` to stderr — **Read that file** rather than piping stdout
through a second interpreter.

```bash
# 1. The style-inference corpus. Prints counts; the notes go to a file.
python scripts/footnote_pass.py style --project fabre2 [--chapters 1-20]

# 2. Render one scan prompt per chapter. --profile-file is REQUIRED (see G1).
python scripts/footnote_pass.py scan-prepare --project fabre2 --chapters 1-20 \
    --profile-file projects/fabre2/.harness/footnotes/profile.md \
    [--worker-model sonnet] [--batch-size 5] [--keep-drafts]

# 3a. Headless wave (default), or 3b spawn footnote-scan-worker subagents.
python scripts/footnote_pass.py scan-fanout --project fabre2 \
    [--cli claude|cursor] [--cli-bin <path>] [--concurrency 5] \
    [--target-ids chapter_04,chapter_05] [--effort medium] [--prompt-cache auto]

# 4. Parse drafts, validate every candidate, write candidates.json + the report.
python scripts/footnote_pass.py scan-commit --project fabre2 [--no-report]

# 5. The writer. One note inline, or a whole approved batch from a file.
python scripts/footnote_pass.py add --project fabre2 \
    --chapter chapter_04 --es-idx 40 --anchor "ubres," --note "Hoy sabemos que…" [--dry-run]
python scripts/footnote_pass.py add --project fabre2 \
    --json-file projects/fabre2/.harness/footnotes/approved.json

# 6. Audit every active footnote against the whole validation table.
python scripts/footnote_pass.py verify --project fabre2 [--chapters 1-20]
```

`--chapters` takes `1-20`, `3,7,12`, `1-3,7`, or explicit ids (`chapter_04`).

### Why `add` exists — the three silent failures

`src/endnotes.py` drops a footnote **without saying so**. `add` refuses each case by
name instead, and `verify` applies the same table to notes already on disk:

| Code | What goes wrong | Refused? |
|---|---|---|
| `no_aligned_sentence` | no alignment row carries that `es_idx` | yes |
| `sentence_not_in_body` | the aligned sentence isn't in `chapters/<id>.txt` | yes |
| `empty_gloss` | nothing left after the `[bracket]` is stripped — **this one logs nothing at all** | yes |
| `multi_anchor` | two brackets: the extras publish verbatim into the book | yes |
| `no_chapter_body` | no `chapters/<id>.txt` | yes |
| `duplicate` | an active note on that sentence already holds this exact text | yes |
| `anchor_not_found` | the marker falls to the end of the sentence | warn |
| `ambiguous_anchor` | the anchor recurs; the marker takes the first hit (a unique anchor is suggested) | warn |
| `sentence_drifted` | `verify` only: `es_idx` now names a different sentence than the note was written against | warn |

`add` is per-note, not per-batch: one bad anchor in a `--json-file` lands the rest and
reports that one. Re-running the identical command is a `duplicate` refusal, not a
second endnote.

**Run `verify` after any `harness.py align`.** `es_idx` is a position in the
alignment, not an identity — re-aligning a chapter moves every note in it.

## Two backends

Both render the same prompts and parse the same drafts, so the candidate list is the
same shape whichever ran. Neither spends dollars.

| | Spend | Cost to your context |
|---|---|---|
| **Headless fan-out** (default) | none (subscription, enforced) | one Bash call; worker output never enters your context |
| **Task subagents** | none (session usage) | one turn per wave |

**Prefer headless, and not marginally.** An 80-chapter book is ~16 wave turns of
spawn ceremony on the Task path against a single `scan-fanout` call. Headless is also
subscription-only *by enforcement*: the launcher scrubs metered credentials from the
child env and refuses to start unless `claude auth status` confirms a subscription.
A top-level `error` with empty `wrote`/`failed` means the wave never started — relay
it verbatim; nothing was written, so re-running after `claude` + `/login` is safe.

The scan wave runs at **medium** effort by default (`footnote_scan` in
`src/harness/state.py:COMMAND_EFFORT_DEFAULTS`), so it does not inherit the CLI's
high band. Override per book with
`harness.py config-set --key headless_effort_footnote_scan`, or per run with
`--effort`.

## Flow

### 1. `style` — read the corpus, don't skim it

```bash
python scripts/footnote_pass.py style --project fabre2
```

`Read` the whole `corpus_path`. For each existing gloss, work out *what problem in
the sentence it is reacting to* — not how it is worded. Placeholders (anchor only, no
text) publish nothing; they are evidence of intent, not of style.

Relay the counts and any `orphaned` notes: those publish nothing today and the user
probably does not know.

### 2. STOP — G1: the profile gate

Present the inferred taxonomy: one line per category, **each with the actual existing
note that evidences it.** On `fabre2` that lands at roughly:

- *corrects the author's science* — a 19th-century claim now known to be wrong
- *corrects an unforced error* — something the author simply got wrong
- *supplies period context a modern child lacks*
- *modernizes an obsolete name* — a place, unit or term since renamed

Use `AskUserQuestion`, multiSelect, ≤4 options, **plus an explicit invitation to
redirect or add categories.** The reason this gate exists is that you may have
misread why a note was written, and the user is the only one who knows.

On approval, `Write` the agreed taxonomy to
`projects/<slug>/.harness/footnotes/profile.md` verbatim — including anything the
user added in their own words.

**END THE TURN before scanning.**

### 3. `scan-prepare`

```bash
python scripts/footnote_pass.py scan-prepare --project fabre2 --chapters 1-20 \
    --profile-file projects/fabre2/.harness/footnotes/profile.md
```

`--profile-file` is required, which is how G1 is enforced in code rather than by this
paragraph. Relay `usage_summary` (chapters, sentences, worker_model, batch_size,
headless_effort).

**Re-`scan-prepare` is destructive** — it clears the drafts for the chapters it
re-renders. Prepare the whole range once; pass `--keep-drafts` if you must re-prepare
with good drafts present.

### 4. STOP — the usage gate

No dollars, but a wave consumes real session/rate usage. Get approval in a **separate
turn** and ask which backend via `AskUserQuestion` unless already chosen.

#### 4a. Headless fan-out (default)

```bash
python scripts/footnote_pass.py scan-fanout --project fabre2
```

On 529, re-run with a lower `--concurrency`. Cursor needs a Cursor model id
(`grok-4.6`, `auto`); `--worker-model sonnet` with `--cli cursor` returns a warning.

#### 4b. Task workers

One `footnote-scan-worker` per manifest entry, `model:` = the manifest's
`worker_model`, told its `prompt_path` and `draft_path`.

**Never overlap waves.** At most `batch_size` workers in one turn (several `Task`
calls in one message), **end the turn**, confirm the drafts exist, then the next
wave. On 529, step down `batch_size → 3 → 1`.

### 5. `scan-commit`, then relay the report

```bash
python scripts/footnote_pass.py scan-commit --project fabre2
```

`Read` `report_path` and relay it — the per-candidate text is there, not on stdout.
Re-run `scan-fanout --target-ids <ids>` for anything in `failed`/`missing`, cap at ~3
attempts per chapter, then surface it.

Raise the `unusable` count explicitly, and `span_not_in_sentence` in particular: that
means a worker paraphrased instead of quoting, and if it is most of the list the
profile or the model tier is wrong, not the book.

An empty candidate list is a valid answer. Relay it as one.

### 6. STOP — G2: the shortlist gate

Cut the list **before** research is spent on it. Present the candidates grouped by
category with the claim and the sentence, and get an explicit pick. Research is the
expensive step and it is all in your own context.

### 7. Research and draft, inline

Per surviving candidate:

1. `WebSearch` the specific claim; `WebFetch` a source to confirm it.
2. **Kill anything that turns out pedantic, trivial, or already explained by the
   sentence.** A candidate that survives detection and dies here is the system
   working, not a waste.
3. **Only now load the drafting inputs**: the book's `style.json` and
   `glossary.json`, and the gloss-voice rules in `prompts/annotation_footnote.txt` —
   1–2 sentences, ~30 words, concrete, self-contained, no hedging, register matched
   to the book, no "aquí significa…".
4. Write the gloss in the book's target language.

Sources go in your relay to the user and **never** into the gloss. The endnote is 1–2
sentences for the book's reader, not a citation.

> The `/browse` rule in `CLAUDE.md` is about interactive QA of the app under
> development. Reading a public page to check a fact is the carve-out `WebSearch` /
> `WebFetch` exist for here.

### 8. STOP — G3: the notes gate

Present each drafted note with its finding, its sources, and the exact `--anchor`.
Approve **individually** — `AskUserQuestion` in groups of ≤4; one call does not hold
more. Anything going into a published book gets looked at by a human first.

Write the approved set to `.harness/footnotes/approved.json` as
`[{chapter_id, es_idx, anchor, note}, ...]`.

### 9. Write, verify, publish

```bash
python scripts/footnote_pass.py add --project fabre2 --json-file <approved.json> --dry-run
python scripts/footnote_pass.py add --project fabre2 --json-file <approved.json>
python scripts/footnote_pass.py verify --project fabre2
python scripts/harness.py epub --project fabre2
```

`--dry-run` first, always: it prints the resolved anchor and an injection preview
(`…ubres,‹N› sino del ano…`) and writes nothing. Then report the endnote count delta —
`Endnotes section appended (N notes)` must increase by exactly the number added.
That log line is the only end-to-end proof the notes landed.

## Notes

- **`projects/` is gitignored, so Glob and Grep return nothing there.** Use Bash or
  `Read` with absolute paths for anything under a book.
- **`python -X utf8` on every Python you run**, and `encoding="utf-8"` on every open.
  Windows stdout defaults to cp1252, which mangles every raya and accent — the exact
  bytes a Spanish gloss is made of.
- **On PowerShell a `python -c` must be one physical line**, or a temp file. A
  newline inside `-c` returns `ScriptBlock solo se debe especificar…`.
- **A script under `.tmp/` cannot `import src`** — `sys.path[0]` is the script's
  directory, not the repo. Set `PYTHONPATH` to the repo root, or just use the CLI,
  which is the point of its existing.
- Several footnotes on one sentence are fine (`sub_id`-keyed, numbered by position in
  the text). Several `[brackets]` in one note are not.
- `annotations.jsonl` is append-only. Nothing here ever rewrites a line, so every run
  is recoverable from the log, and a note the user later edits in the reader supersedes
  rather than replaces.
- Reviewing notes a reader left is `.claude/skills/annotation-review/SKILL.md`.
  Engineering reference: `docs/FOOTNOTE_PASS.md`.

## Friction logs

Runs of this skill are logged in `.claude/skill-friction-logs/footnote-pass/`. When the user asks
for a friction log — or a run wasted significant tokens or operator time and they would
plausibly want one — invoke the **`friction-log`** skill rather than hand-rolling the
file; it owns the location, naming, and section skeleton. Those prior logs are also the
best standing record of this skill's known rough edges: read the latest one or two when
a run hits something that feels familiar.
