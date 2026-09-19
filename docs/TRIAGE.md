# Coded-Checker Triage

Filters the noise out of the `dictionary` and `grammar` evaluators before a
human reads their findings, by asking a model whether each flagged word is
really a defect **in the sentence it sits in**.

## Why

Measured over `projects/**/evaluations/_feedback.jsonl`:

| checker | resolved | false positive | accept rate |
|---|---|---|---|
| editorial | 165 | 37 | 82% |
| address | 54 | 23 | 70% |
| dialogue | 85 | 54 | 61% |
| **grammar** | 68 | 363 | **16%** |
| **dictionary** | 37 | 463 | **7%** |

Those two produce roughly 70% of all finding-clearing work and are right about
one time in ten. Nearly all of the noise is one of a few kinds a model can
recognise from one sentence: proper nouns, words correctly left in another
language, archaisms, dialect, source-text artifacts, and grammar rules misfiring
on Spanish dialogue punctuation.

This is Phase 2 of `docs/design/quality-automation-plan.md`, which is design
scratch and deliberately not tracked in the repo.

## What it does and does not do

- **It never deletes a finding.** A verdict is a sidecar record. The finding
  stays in `evaluations/<chunk_id>.json` exactly as the checker wrote it, so
  per-rule precision stays measurable and the filter's own error rate stays
  auditable.
- **It never edits the book.** No prose is touched; there is no apply step.
- **It proposes no rewrites.** It is a filter, not an editor.
- **It runs on its own pinned model**, not the book's default backend.

## The four commands

```bash
python scripts/run_triage.py status  --project my-book
python scripts/run_triage.py prepare --project my-book \
    --worker-model "grok-4.6[effort=medium,fast=false]"
python scripts/run_triage.py fanout  --project my-book
python scripts/run_triage.py commit  --project my-book
```

`status` writes nothing and is the one to open with. It reports the findings in
scope, the jobs they would batch into, the resolved model and **which rung pinned
it**, whether the CLI could start, the floor, and any drafts a `prepare` would
clear. It exists because everything on that list used to be answerable only by
running `prepare`, which answers by clearing the drafts and rewriting the
manifest — so neither a consent dialog nor an agent deciding whether there was
anything to do could ask without destroying something first. A book with nothing
to triage is `ok` with `triageable: 0`, and exits 0; `prepare` reports the same
state as an error carrying `reason: "nothing_to_triage"`, which is right at a
prompt and is why a caller must branch on the code rather than the message.

`prepare` collects every live finding it can anchor to a sentence, batches them
(`--items-per-job`, default 20) and renders prompts under `.harness/triage/`. No
spend. `fanout` runs one headless wave; it skips jobs that already have a draft,
so re-running resumes. `commit` parses the drafts and appends verdicts to
`projects/<slug>/evaluations/_triage.jsonl`, then writes `.harness/triage/report.md`.

`commit` is re-runnable: a finding that already carries a verdict is counted and
skipped rather than written twice.

### Pinning the model

The model is resolved at `prepare` and recorded in the manifest; `fanout`
inherits it rather than reading the book's `worker_model`. That is why the pass
has its own wave type (`COMMAND = "triage"`), which also gives it
`headless_effort_triage` and its own usage log at `.harness/triage/usage.jsonl`.
Pointing triage at a different model — or later at local inference — never
changes how the book is translated or judged.

**The pin has three rungs**, highest first:

| rung | `model_source` |
|---|---|
| `--worker-model` on `prepare` | `cli` |
| the book's `triage_worker_model` | `config` |
| `DEFAULT_TRIAGE_MODEL[cli]`, the model the floor was calibrated on | `repo-default` |

The bottom rung is what makes this pass safe to put behind a button.
`TRIAGE_CONFIDENCE_FLOOR` is one number for the whole corpus and it was swept
against verdicts from one model; before the ladder, a run with no
`--worker-model` fell through to whatever the CLI defaults to — sonnet on Claude,
whatever `~/.cursor/cli-config.json` says on Cursor. Every surface that ran this
pass passed the model by hand, and a button has no hand.

`claude` is deliberately `None` in that table: no Claude model has been through
`replay_triage.py --exam`, and naming one would assert a calibration that does not
exist. `status` reports `calibrated_model` beside `effective.worker_model` so a
caller can compare them, and both the dashboard popup and the skill say so when
they disagree. Overriding is allowed; going quiet about it is not.

The ladder is read *after* the CLI is settled, because `resolve_profile` falls
back to the other CLI when a guessed one's binary is missing — pinning Cursor's
model onto a wave that fell back to Claude would hand the launcher a model id it
cannot parse.

**On Cursor, the effort is part of the model id, not a config key.** The
calibration ran as `grok-4.6[effort=medium,fast=false]`; `cursor-agent` has since
renamed that exact model to `cursor-grok-4.6-medium` and now rejects the bracket
form outright. Nothing appends a bracket to a bracket-less id, so
`headless_effort_triage` has nothing to act on there — to run the pass at a
different effort on Cursor, pin the id that names it (`cursor-grok-4.6-low`)
rather than setting the config key, which would build
`cursor-grok-4.6-medium[effort=low]`, an id the CLI was never asked about.
`headless_effort_triage` remains the lever on Claude, where effort rides in argv.

```bash
# Claude: effort rides in argv, so the config key is the lever.
python scripts/harness.py config-set --project my-book \
    --key headless_effort_triage --value low

# Cursor: effort rides in the model id, so pin the id that names it.
python scripts/harness.py config-set --project my-book \
    --key triage_worker_model --value cursor-grok-4.6-low
```

## Three ways to run it

The pass is one implementation with three front doors. All of them end in the
same `prepare` -> `fanout` -> `commit` over the same manifest, so a wave started
from one is resumable and committable from any other.

| surface | how |
|---|---|
| CLI | `python scripts/run_triage.py status\|prepare\|fanout\|commit` |
| Claude Code | the **`/triage-review`** skill |
| Dashboard | ticked in the popup on **Rerun deterministic** |

### From the dashboard

The Review tab's **Rerun deterministic** button opens a popup first, and its tick
— *Also triage the dictionary and grammar findings* — runs one triage wave as the
tail of the same background job. Ticked by default and remembered per book in
`triage_after_coded`, because the two checkers are right about one finding in ten
and filtering them is the normal end of a rerun rather than a separate thing to
remember.

It is still a tick rather than an automatic step: the wave spends a
subscription's context budget, and that is not consented to by having clicked
something else. So the popup is built from `status` and names what will run — the
findings, the jobs, the model and its rung, the CLI, and the floor — before
anything is prepared. It is the only read of that data that does not destroy the
drafts, which is what `status` exists for.

**Chained inside one job, not by a second request.** `prepare` is destructive, so
two jobs would leave a window where a second request unlinks drafts the first is
still writing. One job also means one book lock, one progress stream, and one
place a failure can be reported. The route emits a `phase` event per stage and
one `target_done` per finished CLI job, so the progress modal has something to say
through the two ends of a wave that emit no per-job progress of their own.

**A triage problem never blocks the checkers.** This is the one place the
behaviour departs from `run-judges`, where the wave *is* the request and a failed
preflight is a 409. Here the checkers are the main event and have already
persisted their findings by the time the wave starts, so a CLI that cannot start
degrades to a deterministic-only run reporting `triage_skipped`, and a wave that
fails reports inside its own block rather than as a fatal job. An operator who
saw "Stopped" would have no way to tell which half they still need to redo.

## What reaches a prompt

Each item carries the checker, the flagged term, the checker's message, the
sentences, the LanguageTool `rule_id` for grammar findings, and any glossary
entries whose Spanish appears in them. A job opens with the book's style guide,
the house rules every book is held to (`prompts/house_style_rules.json`), and
any style rules of its own. A book with no `style_rules.json` sidecar still
carries the house set.

**Items are numbered, and the number is what comes back.** Each item carries
`item`, its 1-based position in the job, and the draft answers with that number
rather than the finding's id. The stored id ends in a 16-hex-character
`issue_key`, and a model asked to echo one gets it wrong: the first real wave
returned `8f3aae189ee24ad1e` against a true key of `8f3b32300d4cf994`, then on a
re-run of the same job returned `8f3c10691e2fdf8c` — the right first three
characters and then invention, twice, on the same finding. Since `parse_draft`
rejects a draft whose ids are not exactly the job's, each attempt cost all 18
findings in that job rather than the one it got wrong. Numbers resolve against
`jobs[].item_ids`, which `prepare` writes in the order it rendered them, so
neither side may ever re-sort a batch.

**One item is one finding, not one occurrence.** A checker reports a repeated
unknown word once — `'pudín': Unknown word ... (found 3 time(s))` — and the
normalizer fans it into an entry per occurrence so the reader can highlight each
span. Those entries share the four fields `issue_key` hashes, so they are a
single identity to the sidecar, to `_feedback.jsonl` and to all three gates: one
verdict is all any of them can store. They are collapsed back into one item
carrying every occurrence's sentence, and the prompt tells the model its single
verdict covers all of them — suppress only if the term is right in every
sentence listed. Emitting them separately would put one id in a job twice, which
`parse_draft` rejects, and would let a verdict formed on one sentence suppress
occurrences that were never judged.

The sentence comes from the alignment, through the same `attach_text_in_chunk` /
`row_containing_offset` pair the reader's Review Mode uses. A finding whose
offset lands on no sentence is **not triaged** — it has no basis for a verdict
and stays live for the human.

Findings are skipped, and counted, when they are already dismissed, name an
ignored term, already carry a verdict, cannot be anchored, or sit on a stale
chunk whose quoted text has moved. `prepare` reports the tally.

## The verdict

```json
{"ts": "...", "chunk_id": "chapter_01_chunk_000", "eval_name": "dictionary",
 "issue_index": 0, "issue_key": "75c7891c57dd31bc", "term": "Sigfridos",
 "rule_id": null, "verdict": "suppress", "confidence": 0.94,
 "reason": "A Norse proper noun the book uses throughout.",
 "model": "grok-4.6[...]", "prompt_version": "...", "run_id": "triage-..."}
```

Only `suppress` **at or above `TRIAGE_CONFIDENCE_FLOOR`** hides anything. A
`keep`, a low-confidence `suppress`, and a malformed confidence all leave the
finding live. The two errors are not symmetric and the prompt says so: letting a
false positive through costs a reader one glance; suppressing a real defect
loses it silently.

`term` and `rule_id` are stored beside `issue_key` deliberately. Neither checker
sets `finding_key`, so `issue_key` hashes `(eval_name, severity, message,
location)` — and `location` is a character offset. Editing the chunk moves it
and orphans the verdict. Keeping the term and the rule makes re-keying a script
rather than a re-run, and they are the join keys the replay scripts already
trust.

### Why not `_feedback.jsonl`

That file is the labelled corpus `replay_dictionary_marks.py` and
`replay_grammar_marks.py` compute per-rule precision from, and its records carry
no author field. A machine verdict written there would be indistinguishable from
a human one and would contaminate the numbers the cutoff is tuned against. The
reader's "Ignore in this book" button already refuses to bulk-write there for
the same reason.

## Where suppression shows

| surface | behaviour |
|---|---|
| Reader Review Mode | suppressed findings are not painted |
| Chapter and project badges | counts drop by the same number |
| Editorial judge's do-not-repeat list | suppressed findings drop off it, as dismissals do |
| Recommendations screen | still listed, as **Filtered out automatically**, with the model's verdict, score and reason — unticked on arrival, and markable in place |

The last row is the point: a filter nobody can question is a filter nobody
should trust. A human mark always beats a machine one.

That row needs a way in, so `auto_suppressed` is its own checkbox in the status
filter, carrying its own total, and each chapter shows a muted **N filtered out**
chip beside the "N dealt with" one. The two are deliberately separate: one is
what you decided, the other is what a model decided for you. Both counts are
occurrence rows, as every chapter chip is, so a repeated word one verdict covers
reads as the number of places it occurs.

### Reading the filter, and overruling it

Every finding a verdict exists for carries a chip naming that verdict and its
confidence — `suppress 0.94`, `keep 0.30`, `suppress 0.60 · below the floor` —
and the model's stated reason below it under **Filter said**. All three show
whether or not the verdict hid anything, because choosing a floor means reading
the scores on both sides of it: a page that showed only the verdicts above the
floor could never justify moving it.

Each finding also carries the reader's own four mark buttons, posting to the
reader's own endpoint (`/api/project/<id>/evaluations/<chunk_id>/feedback`) with
the reader's own four labels. One vocabulary and one writer, because
`_feedback.jsonl` is the corpus both per-rule precision and this pass's floor are
computed from, and a row's meaning must not depend on which screen wrote it.

That control is what makes calibration reachable. A suppressed finding is
invisible in Review Mode by design, so until it existed the only surface that
could show you one was the only surface that could not let you rule on it.

**Marking is how the floor stops being a guess.** `replay_triage.py` scores
recorded verdicts against human marks, and `prepare` skips every finding a human
has already marked — so a fresh wave and the labelled corpus start with *zero*
overlap, and the wave is unscoreable until someone marks the set it just judged.
The row the exam most needs is the one a model hid and a human then called a
real defect: that, and only that, is how "real defects lost" is ever measured.
So a card's status and its triage chip are deliberately independent — a human
mark changes the status and leaves the machine's verdict on display beside it,
rather than overwriting the evidence of the disagreement.

## Calibration — the part that decides whether to trust it

```bash
python scripts/replay_triage.py --exam
```

Scores recorded verdicts against the human labels and sweeps the floor,
reporting two numbers at each:

1. **Real defects lost** — `resolved` findings triage would have suppressed.
   **This must be 0.** It is the veto, whatever the benefit.
2. **Noise removed** — `false_positive` findings suppressed. The only reason to
   run the pass.

`TRIAGE_CONFIDENCE_FLOOR` (`web_ui/evaluations.py`) is the lowest floor that
still loses nothing, and it moves only with a number from this script attached.

**Keep the exam books out.** `wonder-book-of-horses`, `the-little-duke` and
`bambi-a-life-in-the-woods` are the frozen holdout
(`docs/design/quality-automation-phase0-progress.md` §4, rule 3 — design scratch,
not tracked). Nothing in the code enforces that list; `--exam` excludes them for you.

A `resolved` finding's word has been fixed out of the chunk, so it no longer
reproduces against current text. The pre-edit translation is recovered through
`ledger_census.original_translation`, which validates the chunk's `last_llm_log`
is really that chunk's before returning it. A mark whose original cannot be
verified is reported as unscoreable rather than guessed at.

## Where this is heading

The destination is one harness quality stage — deterministic checkers, then
triage, then the LLM judges — run as a single command. That is Phase 6 of the
quality-automation plan (design scratch, not tracked).

What blocks it today is that **step one has no CLI at all**. The persisting entry
point is `evaluate_and_persist_chunk` in `web_ui/evaluations.py`; it is per-chunk,
and its only callers are the dashboard and `translate_commit`. There is no
book-wide "run the deterministic checkers" command for a harness stage to call,
which is why the chain above lives in a Flask route rather than in
`src/harness/flow.py`.

The shape of the work, when it is picked up:

1. Lift a book-wide evaluator runner out of `web_ui/evaluations.py` into `src/`,
   leaving the route and `translate_commit` calling the same code.
2. Compose the three passes as a `harness.py` subcommand through `flow.py`'s
   existing `_run_script` pattern, which already wraps `chunk`, `cost`,
   `translate`, `epub` and `footnotes`.
3. Teach `flow.status` about evaluation coverage and `_triage.jsonl`, so
   `suggested_reference` can route into a quality phase — today its last branch
   returns `references/reviews.md` and knows nothing about any of this.

Until then, the dashboard chain and `/triage-review` are the two composed
surfaces, and neither duplicates the pass: both call `src/triage/pass_.py`.

## Files

| path | what |
|---|---|
| `src/triage/findings.py` | collects findings, joins each to its sentences |
| `src/triage/pass_.py` | prepare / fanout / commit |
| `scripts/run_triage.py` | the CLI |
| `scripts/replay_triage.py` | calibration and the floor |
| `.claude/skills/triage-review/SKILL.md` | the skill (`/triage-review`) |
| `prompts/triage_coded_finding.txt` | the prompt |
| `projects/<slug>/evaluations/_triage.jsonl` | the verdicts |
| `projects/<slug>/.harness/triage/` | manifest, prompts, drafts, usage, report |

See also: [`JUDGES_FRAMEWORK.md`](JUDGES_FRAMEWORK.md) for the LLM judges this
sits beside, and [`EDITORIAL_JUDGE.md`](EDITORIAL_JUDGE.md) for the two-pass
design whose 82% precision is the standard a checker should meet.
