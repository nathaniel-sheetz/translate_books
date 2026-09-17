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

# 5. The writer. One note inline, or a whole decided batch from a file.
python scripts/footnote_pass.py add --project fabre2 \
    --chapter chapter_04 --es-idx 40 --anchor "ubres," --note "Hoy sabemos que…" [--dry-run]
python scripts/footnote_pass.py add --project fabre2 \
    --json-file projects/fabre2/.harness/footnotes/decisions.json [--dry-run] [--no-report]

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

### `add` also writes the record of what you decided

Every `add` appends one row per decision to
`.harness/footnotes/decisions.jsonl` and renders a dated report under
`reports/`. That is the only durable account of the editorial pass: `candidates.json`
is **replaced wholesale** by the next `scan-commit`, and `annotations.jsonl` holds the
notes that landed but nothing about the ones that did not.

- **`--json-file` takes a decisions document**, a superset of the old
  `approved.json`: `[{chapter_id, es_idx, anchor, note, verdict?, reason?, stage?,
  sources?, candidate_key?}, …]`. **A row with no `verdict` is a keep**, so an old
  approved file still works unchanged. A refused row copied back out of
  `decisions.jsonl` (`anchor` plus the composed `content`, no `note`) also works, and
  so does the review page's composed `[anchor] gloss` pasted into `note`. Scan output
  does not: `candidates.json` is refused, and a keep with no `note` field is `invalid`.
- `verdict: "drop"` rows are recorded with their `reason` and `stage`
  (`gate2` | `research` | `gate3`) and never reach the validator. That is how a
  candidate you cut, or one you killed while researching, is recorded. A drop is a
  record, not a filter: the next scan does not read the ledger and can propose the
  same span again.
- `candidate_key` comes off the candidate report. Copy it and the ledger joins back to
  the exact claim; without it the join falls back to the sentence and reports itself as
  a weaker match — two candidates on one sentence resolve to `"none"` rather than a
  guess, and `counts.undecided` keeps listing both. A key naming a different sentence
  than the row's own `chapter_id`/`es_idx` warns `candidate_key_mismatch` and joins to
  nothing — one half is a copy slip, and if it is the `es_idx` the note is on the wrong
  sentence. Fix it before approving.
- `--dry-run` renders the report and writes **nothing** — not the book, not the ledger.
  A proposal is not a decision.
- `--no-report` skips the markdown only; the ledger is still appended.
- `counts.undecided` names candidates in chapters you touched that appear in neither a
  keep nor a drop. It is a **warning, not a refusal** — but record them before the next
  `scan-commit` erases `candidates.json`.

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

**Effort: read it off `effective`, never off the table.** `footnote_scan` sits at
medium in `src/harness/state.py:COMMAND_EFFORT_DEFAULTS`, and on **Claude** that is
what an unpinned wave runs at — the flag is emitted, so it does not inherit the CLI's
high band. On **Cursor there is no flag**: the level rides in the model's own
`[effort=…]` bracket, and `resolve_profile` deliberately honours whatever you chose in
Cursor's model picker rather than overwriting it with a table default you never saw. A
Cursor wave therefore often runs at **high**, and saying "medium by default" about it
is how the 2026-09-11 `fabre2` run sold a 23-minute high wave as a medium one.

`scan-prepare` answers this once, in `effective`. Quote that. Override per book with
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

- *updates the author's science* — what his century believed, since revised
- *clarifies an honest slip* — a figure, date or unit he evidently meant differently
- *supplies period context a modern child lacks*
- *modernizes an obsolete name* — a place, unit or term since renamed

Name the corrective categories the way a friend of the author would. The category
text is written into `profile.md`, stamped on every candidate, and read again as the
frame you draft from at §7.

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
paragraph. Relay the scope from `usage_summary` (chapters, sentences, batch_size) and
what it will run as from `effective` — see the gate below.

**The scanner reads the Spanish alone** (`--source-text es`, the default). The English
is attached to every candidate at `scan-commit` and read at G2 instead — same split as
the editorial judge's two passes. `--source-text both` puts the EN line back in the
prompt and roughly doubles the wave's input; use it deliberately, and say which mode ran
at the gate, because `usage_summary.source_text` is the only field that distinguishes
the two candidate sets afterwards.

**Re-`scan-prepare` is destructive** — it clears the drafts for the chapters it
re-renders. Prepare the whole range once; pass `--keep-drafts` if you must re-prepare
with good drafts present.

### 4. STOP — the usage gate

No dollars, but a wave consumes real session/rate usage. Get approval in a **separate
turn** and ask which backend via `AskUserQuestion` unless already chosen.

**Build the consent block from `effective`, and quote it verbatim.** `scan-prepare`
resolved the whole wave once and reported it with the provenance of every field:

- `cli` + `cli_source` — which launcher, and whether that came from a flag, the book's
  `headless_cli` pin, or host detection
- `worker_model` — **as prepare printed it**, bracket included
- `effort` **and** `effort_channel` — `argv` means a `--effort` flag carries it,
  `model_bracket` means the Cursor model string does, `none` means nothing does

Those four are only interpretable together, which is why they are relayed as a block
and not cherry-picked. Two rules, both learned the expensive way:

- **Do not quote `usage_summary.headless_effort` on a Cursor wave** — use
  `effective.effort`. (They are derived from the same profile now, so they agree; but
  `effective` is the field that carries its own provenance.)
- **Do not describe headless as "Claude"** when prepare already named a Cursor model.
  "Headless" is a backend, not a vendor. On 2026-09-11 the gate offered
  "headless — medium effort, Claude subscription" against a wave that ran Cursor /
  `grok-4.6` at high for 23 minutes; the consent was for something that never ran.

An honest line reads: *"Cursor, `grok-4.6[effort=high,fast=false]`, effort **high**
(from your Cursor model picker; the command default is medium) — 20 chapters, ~23 min.
Run it?"*

#### 4a. Headless fan-out (default)

```bash
python scripts/footnote_pass.py scan-fanout --project fabre2
```

On 529, re-run with a lower `--concurrency`. Cursor needs a Cursor model id
(`grok-4.6`, `auto`); `--worker-model sonnet` with `--cli cursor` returns a warning.

On **Cursor + Windows**, a job failing with
`EPERM: operation not permitted, rename '…\.cursor\cli-config.json…'` is concurrent
`cursor-agent` processes racing that one file, not a rate limit. **Since 0.59.3.0 this
should no longer happen** — each worker gets its own `CURSOR_CONFIG_DIR` — so treat a
recurrence as a bug worth reporting rather than something to work around. The wave's
usage rollup reports `slots_seeded: "n/N"`; anything below `N/N` means workers lost
that isolation and the race is possible again. The recovery,
if you ever need it: re-run just the failed ids at `--concurrency 1`
(`scan-fanout --target-ids chapter_34 --concurrency 1`). Never re-`scan-prepare` to
recover — that is destructive.

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

`Read` `report_path` and relay it **in full** — the per-candidate text is there, not on
stdout, and G2 below is a decision the user cannot make from counts. Each candidate
carries a `candidate_key`; keep it, it is what joins a decision back to its claim.
Re-run `scan-fanout --target-ids <ids>` for anything in `failed`/`missing`, cap at ~3
attempts per chapter, then surface it. `status: partial` means those chapters are
missing but the rest landed. `status: error` with nothing parsed means **nothing was
replaced** — the previous `candidates.json` is untouched, so re-fan; never re-prepare.
`scan-fanout` exits 1 when every job failed: do not commit over that.

Raise the `unusable` count explicitly, and `span_not_in_sentence` in particular: that
means a worker paraphrased instead of quoting, and if it is most of the list the
profile or the model tier is wrong, not the book.

**The report's `EN:` line is your job, not the scanner's.** Under the default scan the
worker never saw the source, so a candidate's `claim` is what the *Spanish* asserts. The
English is attached here from the alignment so the check happens once, at G2, in front
of someone who can act on it: if the source does not support the claim, that is a cut
with the reason "the English does not say this" — not a note, and not research to spend.

An empty candidate list is a valid answer. Relay it as one.

### 6. STOP — G2: the shortlist gate

Cut the list **before** research is spent on it. Research is the expensive step and it
is all in your own context.

**Print the candidates in the chat, then ask.** Grouped by chapter: `es_idx`,
category, the claim, and the ES sentence. `report_path` is the source — relay it, do
not compress it into options.

`AskUserQuestion` is allowed **only as an id-picker once the content is already on
screen.** It must never be the place the content lives: the widget truncates, and an
option label cannot hold a sentence plus a claim. A picker whose labels *are* the
evidence is asking the user to cut a list they have not read.

Record the cut. Every candidate the user drops becomes a `verdict: "drop"` row with
`stage: "gate2"` and their reason, in the decisions file you write at §8. Undecided
candidates are lost when the next `scan-commit` replaces `candidates.json`.

### 7. Research and draft, inline

Per surviving candidate:

1. `WebSearch` the specific claim; `WebFetch` a source to confirm it.
2. **Kill anything that turns out pedantic, trivial, or already explained by the
   sentence.** A candidate that survives detection and dies here is the system
   working, not a waste. But **a kill is reported at G3 with its reason, never
   silently** — it goes into the decisions file as `stage: "research"`. A borderline
   call goes on the list *flagged as borderline* rather than decided for the user: on
   2026-09-11 the ch. 29 cobras note was killed as pedantic, and the operator restored
   it as a sidenote the moment they saw it.
3. **Only now load the drafting inputs**: the book's `style.json` and
   `glossary.json`, and two sections of `prompts/annotation_footnote.txt` — "What a
   good gloss is" (1–2 sentences, ~30 words, concrete, self-contained, no hedging
   about facts, register matched to the book, no "aquí significa…") and **"Two kinds
   of note, one voice"**.
4. **Decide what kind of note it is.** *Explanatory*: the author refers in passing to
   something a modern reader may not know. *Corrective*: the author said something
   that is not so. Either way the note is written as a friend of the author would
   write it — an error is an honest mistake, never a point scored against him. Kind is
   not vague: a corrective note still makes clear which version to believe.
5. **Pick the voice sample.** Kindness toward the author is the floor on every book.
   Above it, count the published glosses *of the same kind* in `style_corpus.md`:
   with **three or more**, match their phrasing — how they name the author or
   narrator, how long they run; with fewer, the examples in the prompt are the
   sample. A corpus note that reads against the author does not license another. A
   merely terse one is a fine model: bluntness is not unkindness, least of all in an
   explanatory note. On the 2026-09-11 `fabre2` ch. 41–60 slice, four of five
   published glosses were rewritten at G3 for voice (*«no seis mil»*, *«El pino no:
   tiene piñas»*).
6. Write the gloss in the book's target language.

Sources go in your relay to the user and **never** into the gloss. The endnote is 1–2
sentences for the book's reader, not a citation.

> The `/browse` rule in `CLAUDE.md` is about interactive QA of the app under
> development. Reading a public page to check a fact is the carve-out `WebSearch` /
> `WebFetch` exist for here.

### 8. STOP — G3: the notes gate

**The standing rule: anything going into a published book is read by a human in full
before it is written. Never ask for approval of text that is not on screen.**

1. Write `.harness/footnotes/decisions.json` — every keep *and* every drop, with the
   reasons from §6 and §7:

   ```json
   [
     {"chapter_id": "chapter_22", "es_idx": 61, "anchor": "todos nuestros viñedos.",
      "note": "Tío Paul no exageraba: …", "sources": ["https://…"],
      "candidate_key": "chapter_22__61__7f3ab19c"},
     {"chapter_id": "chapter_30", "es_idx": 12, "verdict": "drop", "stage": "research",
      "reason": "the first-aid note later in the chapter already covers it"}
   ]
   ```

2. `add --json-file … --dry-run`. It writes nothing and renders
   `reports/footnote_decisions_<stamp>_proposal.md` — the review page, with each
   gloss verbatim and the marker shown where it will actually fall
   (`…ubres,‹N› sino del ano…`).

3. **Print the notes in the chat**, from that file: the ES sentence, the marker
   preview, the exact `--anchor`, the note's kind (explanatory / corrective), the
   gloss **verbatim**, the finding, the sources.
   Then the drops with their reasons, so a kill can be reversed.

4. Ask for approval or edits **in the conversation**. An id-picker afterwards is
   optional; `AskUserQuestion` whose labels *are* the notes is not. One call cannot
   hold an 80-word Spanish sentence plus a two-sentence gloss — on 2026-09-11 the
   operator cancelled exactly that dialogue with *"I need to see the full notes."*

5. Apply their wording edits to `decisions.json` and re-run the dry run if the text
   changed.

### 9. Write, verify, publish

```bash
python scripts/footnote_pass.py add --project fabre2 --json-file <decisions.json> --dry-run
python scripts/footnote_pass.py add --project fabre2 --json-file <decisions.json>
python scripts/footnote_pass.py verify --project fabre2
python scripts/harness.py epub --project fabre2
```

`--dry-run` first, always — G3 already ran it, so this is the re-run after any wording
edit. Then report the endnote count delta: `Endnotes section appended (N notes)` must
increase by exactly the number added. That log line is the only end-to-end proof the
notes landed.

The live `add` returns `ledger_path` and `report_path`. Relay both — that report is
the account of the pass that survives the next scan — and raise `counts.undecided` if
it is nonzero: it is the last chance to record a candidate before `scan-commit`
replaces `candidates.json`.

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
  rather than replaces. `decisions.jsonl` follows the same rule: a second decision on
  one candidate appends and supersedes, so the refusal-then-fix sequence stays readable.
- **`.harness/footnotes/` holds two waves.** `harness.py footnotes` (the Gutenberg
  translation wave) owns `manifest.json` and `usage.jsonl` there; this one owns
  `scan.manifest.json` and `scan.usage.jsonl`. Do not read the unprefixed pair
  expecting a scan.
- "Durable" means *survives the next `scan-commit`*, not *survives a re-clone* —
  `projects/` is gitignored.
- Reviewing notes a reader left is `.claude/skills/annotation-review/SKILL.md`.
  Engineering reference: `docs/FOOTNOTE_PASS.md`.

## Friction logs

Runs of this skill are logged in `.claude/skill-friction-logs/footnote-pass/`. When the user asks
for a friction log — or a run wasted significant tokens or operator time and they would
plausibly want one — invoke the **`friction-log`** skill rather than hand-rolling the
file; it owns the location, naming, and section skeleton. Those prior logs are also the
best standing record of this skill's known rough edges: read the latest one or two when
a run hits something that feels familiar.
