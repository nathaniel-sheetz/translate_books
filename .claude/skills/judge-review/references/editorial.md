# The editorial judge — running both passes

Read this **before staging `--judge editorial`**. Everything in `SKILL.md`
still applies (status first, profile before you ask, one `AskUserQuestion`, one
`prepare`, end the turn at the gate); this file covers only what is different,
which is that **an editorial run is not finished when `commit` returns**.

Design rationale, prompt internals, the precision targets and the calibration
loop live in [`docs/EDITORIAL_JUDGE.md`](../../../../docs/EDITORIAL_JUDGE.md).
Don't re-derive them here — this is the operating procedure.

## Two passes, one request

| | pass 1 — `run_judges.py --judge editorial` | pass 2 — `verify_editorial.py` |
|---|---|---|
| question | "would a competent editor stop here?" | "and does the English change that?" |
| reads | the **Spanish alone**, plus style guide, glossary, and what the coded evaluators already found | each candidate, plus the English window for the ones that asked for it |
| writes | `judges.editorial.metadata.candidates`, `verified: false` | CONFIRM / RETRACT / RECLASSIFY; `issues[]` becomes the survivors, `verified: true` |
| unit | one `JudgeTarget` per chunk | one chunk's **candidate set** |

Pass 1's `issues[]` are **proposals**, not findings. A dashboard badge lit by
them is un-adjudicated, and the retract rate on a fresh book is the whole reason
pass 2 exists. So:

> **Never offer `apply` on an editorial result whose `metadata.verified` is not
> `true`.** After a pass-1 `commit`, the next step is pass 2 — not §C.

## Pass 1 — the normal flow, with four notes

Stage it exactly as `SKILL.md` describes, with `--judge editorial` (or
`--suite editorial`). It is **not** in `default` or `prose`: it wants a
deliberate "review this book editorially" gesture rather than riding along on a
dialogue wave.

- **`status --judge editorial`** works like any other group. A chunk pass 1 has
  never seen is `not_run`; there is no separate "adjudicated?" state in this
  report — that question is `verify_editorial status`.
- **The prefix is fatter.** Style guide + rule sidecar + glossary + calibration
  examples + the coded findings, versus the dialogue judge's ~1.7k rulebook.
  Expect a higher Cursor `overhead_ratio` — a real 6-chunk wave measured
  **0.837** against dialogue's 0.78. That is the prompt, not a fault.
- **The findings ceiling is not yours to move.** Pass 1 caps findings at 6
  per 1,000 translated words (floored at 2 on very short chunks) — tuned, and
  stated in the prompt as well as enforced in code. **Do not ask about it and
  do not raise it on your own initiative**, including when a chunk comes back
  at its ceiling. Pass `--max-findings-per-1000 N` to `prepare`/`run` only
  when the user named a number, and quote that number back at gate 1 when you
  do. `verify_editorial` has no such flag — pass 2 adjudicates whatever pass 1
  proposed.
- **Say at gate 1 that a second pass follows.** One clause, with a bound:
  *"a second adjudication pass then runs over whatever it finds — at most
  `<chunks> × <baseline_tokens>` more."* The exact number cannot be known until
  pass 1 has proposed something; announcing the shape stops pass 2 from arriving
  as a surprise bill.

## Pass 2 — `scripts/verify_editorial.py`

Five subcommands, the same draft/commit seam as the judges CLI. Every command
but `status` mirrors its JSON to `.harness/editorial/last_output.json` and
prints an `OUTPUT_JSON:` pointer on stderr.

```bash
python scripts/verify_editorial.py status  --project <slug> [--scope …] [--drafts]
python scripts/verify_editorial.py prepare --project <slug> [--scope …] \
    [--cli cursor] [--worker-model …] [--effort …] [--quiet]
python scripts/verify_editorial.py fanout  --project <slug> [--concurrency N]
python scripts/verify_editorial.py commit  --project <slug> --persist
python scripts/verify_editorial.py run     --project <slug> --persist --confirm   # API backend
```

- **`status`** is read-only and free. `counts` is chunks/candidates/
  `source_requested`; `skipped` explains everyone else. Three reasons, none of
  them a failure: `no_candidates` (pass 1 found this chunk clean — **do not**
  report it as a gap), `judge_not_run` (pass 1 never ran here), `already_verified`.
- **`prepare` is the gate**, like pass 1's: it returns `effective` +
  `usage_summary` and takes `--cli` / `--worker-model` / `--effort`. Use
  `--quiet` on the headless path. It is destructive to re-run (it clears the
  drafts it re-renders) unless you pass `--keep-drafts`.
- **`fanout` inherits** the CLI, model and effort `prepare` wrote into the
  manifest — **run it bare**. `--concurrency` defaults to the manifest's
  `batch_size` (5); the flags are the one-flag correction for a bad pin, and a
  model override is written back to the manifest.
- **`commit --persist`** rewrites `evaluations/<chunk>.json` with the
  adjudicated set and stamps `verified: true`. Relay `rollup`: `confirmed` /
  `reclassified` / `retracted` / `source_attached` / `source_used` — **and say
  which findings moved**, from the payload's `verdict_detail` (every retraction
  and reclassification, tagged with its `chunk_id` and its reason; confirmations
  are omitted because they changed nothing). There is **no `--brief` here**: this
  CLI's `results[]` is nine counts per chunk, so nothing floods the turn, and the
  answer to "which two were retracted?" is in the payload rather than ten
  evaluation files.

`--scope` takes the same `chunk:` / `chapter:` / `chapter:<a>..<b>` / `book`
forms, and it **repeats** — `status`, `run` and `prepare` union every flag, the
way `run_judges prepare` does. Default is `book`, which is usually right — pass
2's scope is "whatever pass 1 just proposed", and `status`'s skip reasons already
exclude the rest. A scope that resolves to nothing is a **warning on stderr and a
`scope_error` skip**, not a failure, so one untranslated chapter cannot refuse
the run — which also means a typo silently narrows it. Read the warnings.

## Do not re-ask the four questions

Backend, CLI, spawn mode and grouping were answered once, before pass 1. **Pass
2 reuses them verbatim.** A second `AskUserQuestion` for the same wave is the
tax this file exists to remove.

- Q1 backend → API is `verify_editorial run`; subagent is
  `prepare` → `fanout` (or `judge-worker` Task subagents on
  `prompt_path`/`draft_path`) → `commit`.
- Q2 CLI → pass it to `prepare --cli`, same as pass 1 — **unless `profile`
  already reported `cli_source: config` for the CLI you want**. `--cli` outranks
  the pin and rewrites `cli_source` to `cli`, so passing it on a pinned book
  changes nothing about the wave and hides the pin from the consent block.
- Q3 spawn mode → same answer; **Cursor is still headless-only**.
- Q4 grouping → pass 2 has no `--targets-per-worker`. The unit is already one
  chunk's whole candidate set, which is the grouping.

## The run, end to end

```bash
# 1. pass 1 — free reads, then the gate
python scripts/run_judges.py status  --project <slug> --judge editorial --detail
python scripts/run_judges.py profile --project <slug>
python scripts/run_judges.py prepare --project <slug> --judge editorial \
    --scope chapter:chapter_01..chapter_06 --cli cursor --quiet \
    [--max-findings-per-1000 N]   # ONLY if the user named a number
#    relay effective + usage_summary + "a second pass follows" → END THE TURN

# 2. the wave — announcement and tool call in the SAME message
python scripts/run_judges.py fanout --project <slug>
python scripts/run_judges.py commit --project <slug> --persist [--brief]
#    relay the findings. They are CANDIDATES. Do not offer apply.

# 3. pass 2 — both free, then the second gate
python scripts/verify_editorial.py status  --project <slug>
python scripts/verify_editorial.py prepare --project <slug> --cli cursor --quiet
#    relay: "N of M chunks carry candidates; ~X tokens, same worker" → END THE TURN

# 4. adjudicate
python scripts/verify_editorial.py fanout --project <slug>
python scripts/verify_editorial.py commit --project <slug> --persist
#    relay the rollup AND name what moved, from `verdict_detail`.
#    NOW §C apply is on the table.
```

Steps 1 and 3 each end the turn on a number. Step 3's is exact, and it is
cheap to obtain: `status` and `prepare` spend nothing, so run both *before*
asking rather than asking for permission to find out.

## The baseline pass 2 quotes

Adjudication keeps its own `.harness/editorial/usage.jsonl`, so the estimate
self-calibrates on verdict-shaped jobs rather than on pass-1's finding-shaped
ones. On a book's **first** adjudication wave that log is empty, and the number
is borrowed from `.harness/judges/usage.jsonl` for the same CLI —
`headless_baseline_source` then reads `… (pass-1 log; no adjudication rows
yet)`. Quote it as-is; the provenance string is the point.

## Adjudication is not idempotent

A verified chunk is skipped until `--force`. Re-running re-decides retractions
the first pass already removed from `issues[]` and costs another wave, so
`--force` is for a deliberate re-adjudication, never for "let's just make sure".
`metadata.candidates` is left exactly as pass 1 wrote it, so the retract rate
stays measurable no matter how often the survivors are edited.

## When something goes wrong

- **"Is the wave still going?"** → `status --drafts` (both CLIs). It reports the
  manifest's mtime and `{written, pending}` drafts. Zero drafts and a stale
  manifest mtime means the wave never started — which is what happened on
  2026-08-26 and cost eight minutes of the operator's time. Do not answer this
  by listing draft files by mtime.
- **`failed` / `missing` on commit** → re-run `fanout` (it skips entries that
  already have a non-empty draft) and `commit` again. Trust `commit`'s lists,
  not a fan-out error string.
- **An unparseable adjudication response** leaves the whole pass-1 result
  untouched with `verified: false` — nothing is silently confirmed.
- **A candidate with no verdict is kept**, and an unreadable verdict reads as
  CONFIRM. Neither ever deletes a finding on evidence we could not read, so a
  partial pass 2 under-retracts rather than over-retracts.

## After pass 2

Relay the rollup, then offer §C `apply` as usual. Two editorial-specific vetoes
before you present a plan:

- **A RECLASSIFIED finding's severity moved but its suggestion did not.** Read
  `old → new` against the reclassified category before treating it as approved.
  Both sides are recorded: `metadata.reclassified_findings` (and the wave's
  `verdict_detail`) carry `severity`/`new_severity` and `category`/`new_category`
  per finding — the survivor in `issues[]` keeps only the new one.
- **`FIDELITY_SUSPECT` survivors are the ones to read against the source**, not
  just against the Spanish — pass 2 attached the English for exactly these, and
  `source_used` says whether it actually changed a verdict.

Editorial findings carry a stable `finding_key` (`sha256(rule ␟ excerpt)`), so a
false-positive mark recorded through `append_feedback` survives the judge
rewording itself on a later run. Recording dismissals is what feeds
`editorial_metrics.py --write-examples`, which is how the threshold gets tuned —
so when the user calls a finding wrong, record it.
