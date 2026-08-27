# The editorial judge

The dialogue and address judges answer *"does this passage follow rule X?"*. The
editorial judge answers a different question: *"would a competent editor stop
here and fix this?"* — the clarity, naturalness and fidelity defects no rulebook
enumerates.

It is a two-pass judge. Pass one reads the **Spanish alone** and proposes
candidates. Pass two adjudicates every candidate — CONFIRM, RETRACT or
RECLASSIFY — with the English original attached to the ones that asked for it.

```
chunk translation + style guide + glossary + what the coded evaluators already found
        │
        ▼  pass 1 — run_judges.py --judge editorial          one call / chunk
   candidates[]  (budget-capped, confidence-floored, each with a source_check)
        │
        ▼  pass 2 — verify_editorial.py                      one call / chunk
   CONFIRM / RETRACT / RECLASSIFY, English attached where requested
        │
        ▼
   evaluations/<chunk>.json → judges.editorial → Review tab, reader, apply
```

## Precision is the feature

The human marks in `projects/*/evaluations/_feedback.jsonl` (879 rows across
eight books) put the existing checkers here:

| checker | marks | false-positive rate |
|---|---|---|
| `dictionary` | 375 | 90.9% |
| `grammar` | 329 | 80.9% |
| `dialogue` | 109 | 45.0% |
| `address` | 62 | 33.9% |

A judge in that range gets ignored, correctly. So the threshold is defended in
code, not only in the prompt — the dialogue judge already has a carefully worded
prompt-only threshold and still runs 45% false positive. Four gates, in order:

1. **A findings budget.** `FINDINGS_PER_1000_WORDS = 3`, floored at 2 for very
   short chunks. Stated in the prompt and enforced in `select_findings`, which
   truncates by severity so a truncation drops the least serious rather than
   whatever was listed last.
2. **A confidence floor.** The judge returns `high` or `medium` — there is no
   `low`, because a low-confidence finding is not a finding. Stage 1 keeps
   `high` only; relax it with the `editorial_min_confidence` context key once
   the accept rate is known.
3. **The shared non-issue filter.** `scoring.is_nonissue`, for a model that
   narrates its inspection ("this is fine") and tags it with a severity anyway.
4. **Adjudication.** Pass two, below.

A malformed enum never promotes a finding: an unrecognized `confidence` reads as
the *weakest* allowed value, and an unrecognized `source_check` reads as
`not_needed` so a finding cannot conscript an English window it never asked for.

### Targets

| metric | target | for comparison |
|---|---|---|
| findings per chunk | ≤ 2 | dictionary 3.24 · grammar 2.79 · dialogue 1.48 |
| chunks with zero findings | ≥ 40% | dialogue 42.9% · address 68.3% |
| accept rate | ≥ 70% | dialogue 55% · address 66% |
| excerpt anchor rate (fresh chunks) | ≥ 95% | existing judges 98.2% |

`scripts/editorial_metrics.py` reports all four, free.

## Why pass one is blind to the English

`item_prompt_variables` deliberately omits `source_text`. The dialogue judge is
shown the English so it can tell which passages are speech; here the English is
the thing being withheld, because a reader who can see the original stops
evaluating the Spanish as Spanish and starts diffing it against the source.

A candidate that genuinely cannot be settled without the original says so:

| `source_check` | meaning |
|---|---|
| `not_needed` | establishable from the Spanish alone — grammar, a style-rule or glossary violation, an internal inconsistency, a plain calque |
| `recommended` | defective as written, but the original might explain the choice |
| `required` | cannot be asserted without the original; every `FIDELITY_SUSPECT` finding |

## The five categories

`GRAMMAR` · `NATURALNESS` · `STYLE_GUIDE` · `CONSISTENCY` · `FIDELITY_SUSPECT`.

Deliberately five, not eight. `CLARITY` and `READABILITY` overlap `NATURALNESS`
almost entirely, and `OMISSION_ADDITION` collapses into `FIDELITY_SUSPECT` —
both are the single decision *does the English change my judgment*. Fewer
categories means thicker per-category dismissal statistics, which is what tuning
actually needs; at 1–2 findings per chunk, eight categories would take months to
say anything about any of them.

`CONSISTENCY` is **within-window only**, and the prompt says so. Book-wide
terminology drift needs `src/annotations/concordance.py::BookIndex`, which is how
`annotation-review` already answers the reader's `inconsistency` notes. Claiming
a book-wide inconsistency from one chunk is exactly the kind of confident wrong
finding this judge exists to avoid.

## Book inputs

Loaded by `src/judges/context.py::build_judge_context`, and only when
`editorial` is in the requested judges — the glossary and the coded-findings
walk cost real I/O that a dialogue-only wave has no use for.

| input | source | required |
|---|---|---|
| style guide | `projects/<slug>/style.json` → `content` | no |
| hard rules | `projects/<slug>/style_rules.json` (sidecar) | no |
| glossary | `projects/<slug>/glossary.json`, prompt-formatted | no |
| calibration examples | `projects/<slug>/editorial_examples.txt` | no |
| already reported | live coded findings, per chunk | no |

Every one is optional and has a stated placeholder in the prompt: four of the
five categories need none of them.

### `style_rules.json` — the optional rule sidecar

`style.json` is one free-text blob, the same shape across every book in
`projects/`. Tiering *that* schema into hard rules / preferences / goals would
mean regenerating twenty style guides. So the hard rules live beside it:

```json
{
  "version": 1,
  "rules": [
    {"id": "names-english", "rule": "Keep personal names in their original English form.",
     "note": "Place names too, except where the glossary says otherwise."},
    {"id": "no-iberian", "rule": "No Iberian vocabulary: coger, vosotros, vale."}
  ]
}
```

The judge cites `id` in a finding's `rule`, which becomes `Issue.rule_id` — the
key a rule is suppressed and precision-measured by. A book without the sidecar
still judges; it just emits un-cited `STYLE_GUIDE` findings.

### Already-reported findings

The dictionary and grammar evaluators fire on the same chunks at 3.2 and 2.8
findings apiece. Their live findings — not dismissed, not on the book's ignore
list, not from a stale evaluation — are rendered into the prompt as a
do-not-repeat list. Without it the Review tab counts the same defect twice and
the badge inflates.

## Dismissals must survive a re-judge

`web_ui.evaluations.issue_key` derives a finding's identity from
`(eval_name, severity, message, location)`. That works for a deterministic
checker, which reproduces its message verbatim. **An LLM does not** — it rewords
the same defect every run, so a message-keyed dismissal silently stops matching
its own finding, and the calibration corpus this judge depends on evaporates.

So the editorial judge sets `Issue.finding_key` — `sha256(rule ␟ excerpt)[:16]`,
with whitespace collapsed — and `issue_key` prefers it when present. Rule plus
excerpt is stable across rewordings, and correctly stops matching once the prose
itself changes.

This is opt-in (`scoring.finding_to_issue(..., stable_identity=True)`). The
dialogue and address judges keep the derived key: switching them would orphan
every mark already recorded against them.

## The adjudication pass

`scripts/verify_editorial.py`, backed by `src/judges/editorial_verify.py`.

**It adjudicates everything, not only the source-dependent findings.** The
obvious design fires a bilingual call only when something set `source_check` to
`recommended`/`required`. But the Spanish-only findings are the majority, and
they are where the false positives live — none of the existing judges' 34–45%
is fidelity error. Adjudicating the whole set costs the same one call per chunk
(the English is attached per *candidate*, not per call), and it makes "how often
did the second pass change the outcome" answerable for every finding rather than
for a self-selected slice.

The verifier adjudicates what it is given and nothing else. It does not re-read
the passage for new defects, and it does not go looking for fidelity errors pass
one did not nominate — that would be a different capability, and it would defeat
the threshold.

### English neighbourhood retrieval

`src/judges/neighborhood.py`. No new alignment work:
`alignments/<chapter>.json` already pairs every Spanish sentence with its English
one. Across the 502 alignment files in `projects/` that is 47,091 high-confidence
rows against 1,076 low, with two coverage gaps in the whole corpus.

Retrieval folds accents and case (`text_utils.fold`), matches containment in
both directions (a short excerpt sits inside one sentence; a long one swallows
several), then reduces the hits to a single **contiguous run**, scored by how
much of the excerpt each run accounts for.

That last step is load-bearing. Without it, one short sentence that happens to
appear inside the excerpt anchors the window from somewhere else in the chunk: a
real `gaudenzia` case matched the 12-character row `"El Paragüero"` at index 2
alongside the true span at 176–177, and min-to-max spanned 181 rows — an entire
chapter presented to the verifier as the neighbourhood of one line. A verifier
shown the wrong English adjudicates confidently against it, which is strictly
worse than showing it none.

Failing containment, a single best row by significant-token overlap, requiring
half the excerpt's long tokens to land on it. Failing that, no window — and the
prompt says `<english_context_unavailable>` rather than silently presenting
something unrelated.

Replayed over every persisted judge excerpt on chunks whose text has not
drifted: **163 of 165 located (98.8%)**, median window 7 rows, max 12.

### What is persisted

```json
"metadata": {
  "candidates": [ … pass-1 findings, pre-adjudication … ],
  "retracted": [ {"finding_key": "…", "reason": "…", "used_source": true} ],
  "verified": true,
  "candidates_adjudicated": 4, "confirmed": 2, "reclassified": 1, "retracted_count": 1,
  "source_requested": 2, "source_attached": 2, "source_used": 1
}
```

`issues[]` holds the survivors, so the badges and the reader see the adjudicated
set. `metadata.candidates` is left exactly as pass one wrote it — the
pre-adjudication set is the only record of what was proposed, and the retract
rate is measured against it.

Two conservative defaults: a candidate with **no** verdict is kept (an omitted
key has not retracted anything), and an **unparseable** verdict value is read as
CONFIRM. Neither ever deletes a finding on evidence we could not read. An
unparseable adjudication response leaves the whole pass-1 result untouched with
`verified: false`, rather than silently confirming everything.

## Cost

Pass 1's cacheable prefix is the style guide, rule list, glossary and examples;
the suffix is the passage. `run_judges.py run --cost-limit 0` quotes the real
number for a given book — around **$0.04 per chunk**, so a 40-chunk book is a
few dollars on the API backend and needs `--confirm` past the $0.50 default gate.
The subagent and headless backends spend nothing.

## Commands

```bash
# Pass 1 — read-only coverage first
python scripts/run_judges.py status --project pollyanna --judge editorial --detail

# Pass 1 — API (cost-gated; --cost-limit 0 quotes without calling anything)
python scripts/run_judges.py run --project pollyanna --judge editorial \
    --scope chapter:chapter_01 --persist --confirm

# Pass 1 — headless (no spend)
python scripts/run_judges.py prepare --project pollyanna --judge editorial \
    --scope chapter:chapter_01
python scripts/run_judges.py fanout  --project pollyanna
python scripts/run_judges.py commit  --project pollyanna --persist

# Pass 2 — what is waiting (read-only); --drafts adds "has the wave started?"
python scripts/verify_editorial.py status --project pollyanna [--drafts]

# Pass 2 — API
python scripts/verify_editorial.py run --project pollyanna --persist --confirm

# Pass 2 — headless. `prepare` is the consent gate: it returns `effective` +
# `usage_summary` and takes the --cli/--worker-model/--effort that decide them.
# `fanout` inherits that profile from the manifest — run it bare.
python scripts/verify_editorial.py prepare --project pollyanna --cli cursor --quiet
python scripts/verify_editorial.py fanout  --project pollyanna
python scripts/verify_editorial.py commit  --project pollyanna --persist

# Turn confirmed findings into edits (plan first, always)
python scripts/run_judges.py apply --project pollyanna --judge editorial \
    --scope chapter:chapter_01 --dry-run

# Measure. Costs nothing — it scores what is persisted, it never re-runs the judge.
python scripts/editorial_metrics.py --project pollyanna
python scripts/editorial_metrics.py --project pollyanna --write-examples
```

Adjudication is **not** idempotent the way `apply` is: a second pass re-decides
retractions the first already removed from `issues`, and costs another call. So
a verified chunk is skipped until `--force`.

## Calibration

`--write-examples` turns the marked corpus into
`projects/<slug>/editorial_examples.txt`, which the judge reads back as
`<calibration_examples>` inside its cached prefix. Dismissed examples come first
and are the more valuable half: they state the threshold in the reviewer's own
decisions rather than in adjectives.

Record marks through the existing loop — the reader's four feedback buttons, or
`web_ui.evaluations.append_feedback`. **Do not add a `feedback_type` value.** The
four in `_ALLOWED_FEEDBACK_TYPES` are load-bearing for 879 rows, `is_dismissed`,
and both replay scripts; a finer dismissal reason belongs as a sub-field on a
`false_positive` record.

The decision gate for pass two: if source checking fires rarely and rarely
changes a verdict, tighten `source_check` or drop it for some categories. If it
retracts often, it has paid for itself. `source_used_pct` in the metrics report
is that number.

## Files

```
prompts/judge_editorial.txt              pass 1, solo
prompts/judge_editorial_batch.txt        pass 1, several chunks per worker
prompts/judge_editorial_verify.txt       pass 2
src/judges/editorial_judge.py            EditorialJudge (registered as "editorial")
src/judges/editorial_verify.py           build / parse / apply seams for pass 2
src/judges/neighborhood.py               English window from alignments/
scripts/verify_editorial.py              pass 2 CLI (run | prepare | fanout | commit | status)
.claude/skills/judge-review/references/editorial.md   how the agent drives both passes
scripts/editorial_metrics.py             precision, volume, adjudication, anchoring
tests/test_judges/test_editorial_judge.py
tests/test_judges/test_editorial_verify.py
tests/test_editorial_pipeline.py
```

See [JUDGES_FRAMEWORK.md](JUDGES_FRAMEWORK.md) for the shared judge plumbing and
[ADDRESS_JUDGE.md](ADDRESS_JUDGE.md) for the other per-book judge. The operating
procedure an agent follows to run both passes — which questions to ask, where the
two consent gates are, and why `apply` waits for `verified: true` — is
[`.claude/skills/judge-review/references/editorial.md`](../.claude/skills/judge-review/references/editorial.md).
