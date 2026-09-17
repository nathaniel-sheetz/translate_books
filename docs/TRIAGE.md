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

## The three commands

```bash
python scripts/run_triage.py prepare --project my-book \
    --worker-model "grok-4.6[effort=medium,fast=false]"
python scripts/run_triage.py fanout  --project my-book
python scripts/run_triage.py commit  --project my-book
```

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

```bash
python scripts/harness.py config-set --project my-book \
    --key headless_effort_triage --value low
```

## What reaches a prompt

Each item carries the checker, the flagged term, the checker's message, the
sentences, the LanguageTool `rule_id` for grammar findings, and any glossary
entries whose Spanish appears in them. A job opens with the book's style guide
and style rules.

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
| Recommendations screen | still listed, as **Filtered out automatically**, with the model's reason — unticked on arrival |

The last row is the point: a filter nobody can question is a filter nobody
should trust. A human mark always beats a machine one.

That row needs a way in, so `auto_suppressed` is its own checkbox in the status
filter, carrying its own total, and each chapter shows a muted **N filtered out**
chip beside the "N dealt with" one. The two are deliberately separate: one is
what you decided, the other is what a model decided for you. Both counts are
occurrence rows, as every chapter chip is, so a repeated word one verdict covers
reads as the number of places it occurs.

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

## Files

| path | what |
|---|---|
| `src/triage/findings.py` | collects findings, joins each to its sentences |
| `src/triage/pass_.py` | prepare / fanout / commit |
| `scripts/run_triage.py` | the CLI |
| `scripts/replay_triage.py` | calibration and the floor |
| `prompts/triage_coded_finding.txt` | the prompt |
| `projects/<slug>/evaluations/_triage.jsonl` | the verdicts |
| `projects/<slug>/.harness/triage/` | manifest, prompts, drafts, usage, report |

See also: [`JUDGES_FRAMEWORK.md`](JUDGES_FRAMEWORK.md) for the LLM judges this
sits beside, and [`EDITORIAL_JUDGE.md`](EDITORIAL_JUDGE.md) for the two-pass
design whose 82% precision is the standard a checker should meet.
