---
name: triage-review
description: |
  Filter the deterministic checkers before a human reads them. The dictionary and
  grammar evaluators produce roughly 70% of all finding-clearing work and are right
  about one time in ten; this asks a model, for each flagged word, whether it is
  really a defect in the sentence it sits in, and records the answer beside the
  finding. Nothing is ever deleted and no prose is edited. Runs as one headless
  wave on a pinned CLI and model (subscription, no dollars).
  Use when asked to "run triage", "triage the coded checkers", "filter the dictionary
  findings", "filter the grammar noise", "run the triage pass", "clear the false
  positives", or "triage-review".
allowed-tools:
  - Bash
  - Read
  - AskUserQuestion
---

# triage-review

Machine triage of the `dictionary` and `grammar` evaluators. Measured over
`projects/**/evaluations/_feedback.jsonl`, those two are right 7% and 16% of the
time and account for most of the clearing work; nearly all of the noise is a kind
a model can recognise from one sentence — proper nouns, words correctly left in
another language, archaisms, dialect, source-text artifacts, and grammar rules
misfiring on Spanish dialogue punctuation.

Full design: `docs/TRIAGE.md`. Read it before changing anything about the prompt,
the floor, or what reaches a verdict.

**Three rules this pass never breaks**, and you must not describe it as doing
otherwise:

- **It never deletes a finding.** A verdict is a sidecar record; the finding stays
  in `evaluations/<chunk_id>.json` exactly as the checker wrote it, so per-rule
  precision stays measurable and this filter's own error rate stays auditable.
- **It never edits the book.** There is no apply step and it proposes no rewrites.
  It is a filter, not an editor.
- **It runs on its own pinned CLI and model**, not the book's translation backend.

## The CLI

`python scripts/run_triage.py <status|prepare|fanout|commit>` is non-interactive
and prints one JSON object carrying a `_schema` block.

| verb | spend | what it does |
|---|---|---|
| `status` | none, and **writes nothing** | findings in scope, jobs, the model, whether the CLI can start |
| `prepare` | none | renders batched prompts + a manifest under `.harness/triage/` |
| `fanout` | subscription | runs one headless wave over those jobs |
| `commit` | none | parses the drafts, appends verdicts to `evaluations/_triage.jsonl` |

**Open with `status`.** It is the only command that answers "is there anything to
do here, on what model, and can the CLI even start" without touching disk —
`prepare` answers the same questions by clearing the drafts and rewriting the
manifest.

```bash
python -X utf8 scripts/run_triage.py status --project five-little-peppers
```

**Windows / UTF-8.** `run_triage.py` reconfigures its own stdout, but your ad-hoc
`python -c` probes do not: always `python -X utf8`, and open JSON with
`encoding="utf-8"`. A triage reason routinely quotes the prose, rayas and all.

### The CLI and the model are pinned, and the pins are the point

`TRIAGE_CONFIDENCE_FLOOR` (0.85) is one number for the whole corpus, swept against
verdicts from one model on one CLI — **Cursor, `cursor-grok-4.6-medium`**. A wave
on some other pair is scored by a floor nobody tuned for it. Three rungs each,
highest first:

1. `--worker-model` / `--cli` on `prepare`
2. this book's `triage_worker_model` / `triage_headless_cli` (`harness.py config-set`)
3. the calibrated pair (`DEFAULT_TRIAGE_MODEL`, `DEFAULT_TRIAGE_CLI` in `src/triage/pass_.py`)

**The CLI rung ignores the book's `headless_cli`.** A book translated and judged
on Claude still triages on Cursor: this pass filters checker findings, not prose,
and the floor only holds where it was swept. Do not "fix" that by passing
`--cli claude` — `DEFAULT_TRIAGE_MODEL["claude"]` is `None`, so that run is
unpinned and uncalibrated. `triage_headless_cli: auto` un-pins the pass back to
the book if an operator asks for it.

`status` reports `model_source` (which rung answered), `effective.cli_source`, and
`calibrated_model` (what the floor was swept on). **If the model and
`calibrated_model` disagree, or `calibrated_model` is null, say so in your consent
block** — the run is allowed, but the floor applied to it was not measured for it.

A pinned CLI is never swapped for a missing binary, so on a machine without
`cursor-agent` the pass stops at `preflight_error` rather than quietly running
sonnet. Relay that message verbatim; `status`'s `instructions` adds the one way
off the pin.

Effort depends on the CLI. On Claude it rides in argv, so
`headless_effort_triage` (default `medium`) is the lever. On Cursor it is part of
the model id — the calibrated `cursor-grok-4.6-medium` names its own — so pin a
different id (`cursor-grok-4.6-low`) rather than setting the config key, which
would build a bracketed id the CLI was never asked about. `status` reports
`effective.effort_channel`, which says which of the two is live.

## Flow

### 1. Scope and status, before you ask anything

Default to the whole book. `--chapters chapter_01,chapter_02` narrows it, and the
ids are **alignment stems** — a chapter with no alignment cannot be triaged at all
and is counted in `skipped`.

### 2. Relay one consent block, then ask once

A wave spends a subscription's context budget, so it is consented to, not assumed.
Put all of it in one message and ask everything in a single `AskUserQuestion`:

- findings in scope and how many jobs they batch into (`triageable`, `jobs`)
- per-checker split (`by_eval`)
- the model, its rung, and whether it is the calibrated one
- the CLI and its rung — name it even when it is the default, because this pass
  pins a family the book may not otherwise use
- `preflight_error` if it is set — **stop here if it is**, and relay the CLI's own
  message verbatim; it already names the fix
- the floor, and that only a `suppress` at or above it hides anything
- `pending_drafts`, if non-zero: preparing clears them

Ask: scope (if not already settled), and whether to override the model. Do not ask
about backends — there is one.

### 3. Prepare

```bash
python -X utf8 scripts/run_triage.py prepare --project <slug> [--chapters ...] [--worker-model ...]
```

Once. It is destructive to re-run: drafts are cleared because the draft-to-finding
join is **positional**, so a surviving `job-001.json` would answer a fresh
`job-001` about entirely different findings.

### 4. Announce the wave and run it in the same message

Say what is about to run and make the `fanout` Bash call **in that same message** —
not a message announcing it followed by a turn that runs it. (The rule
`judge-review` records from 2026-08-26: the split wastes a full turn per wave.)

```bash
python -X utf8 scripts/run_triage.py fanout --project <slug>
```

It blocks until the wave drains, skips jobs that already have a draft (so
re-running resumes rather than re-spends), and inherits the model from the
manifest.

### 5. Commit, and say what the filter did

```bash
python -X utf8 scripts/run_triage.py commit --project <slug>
```

Re-runnable: a finding that already carries a verdict is counted and skipped, not
written twice. Relay `suppressed` / `kept` / `already_recorded` and the
`report_path`.

Close by pointing at the **recommendations screen**, where every filtered finding
is still listed under *Filtered out automatically* with the model's verdict, score
and reason — and can be overruled in place. A human mark always beats a machine
one. That screen is also the only way a suppressed finding can ever be looked at,
since Review Mode does not paint them.

## Gotchas

Four things that will otherwise cost you a wave:

- **`--keep-drafts` means refuse, not resume.** It is the inverse of what the same
  flag means in `run_judges.py`: it makes `prepare` *refuse to run* when drafts
  exist, for use while a wave is in flight. The safe default for a re-prepare is
  to omit it.
- **`prepare` exits 1 on a clean scope.** "Nothing left to triage" is a normal
  outcome on any book triaged before. It carries `reason: "nothing_to_triage"` —
  branch on that, never on the message, and do not report it as a failure.
- **`fanout` exits 0 even when jobs failed.** Only a launcher refusal sets a
  top-level `error`. Check `counts.failed` and `counts.todo`, not the return code.
  A top-level `error` with empty `wrote` and `failed` means the wave never started.
- **One manifest per book.** Never overlap waves, and never re-prepare while one is
  running — including from the dashboard, which runs this same pass as the tail of
  a deterministic rerun.

## The floor moves only with a number

```bash
python -X utf8 scripts/replay_triage.py --exam
```

Scores recorded verdicts against human marks and sweeps the floor, reporting **real
defects lost** (must be 0 — the veto, whatever the benefit) and **noise removed**.
`--exam` excludes the frozen holdout books for you. Do not propose a floor change
without output from this script attached, and do not edit
`TRIAGE_CONFIDENCE_FLOOR` in `web_ui/evaluations.py` on a hunch.

A fresh wave is unscoreable until someone marks the set it just judged: `prepare`
skips every finding a human already marked, so a wave and the labelled corpus start
with zero overlap. The row the exam most needs is the one a model hid and a human
then called a real defect.

## Friction logs

Runs of this skill are logged in `.claude/skill-friction-logs/triage-review/`. When
the user asks for a friction log — or a run wasted significant tokens or operator
time and they would plausibly want one — invoke the **`friction-log`** skill rather
than hand-rolling the file; it owns the location, naming, and section skeleton.
Read the latest one or two when a run hits something that feels familiar.
