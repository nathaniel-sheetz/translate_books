# Footnote pass

Authoring **new** editorial footnotes for a finished translation.

This is the create path. `docs/ANNOTATION_REVIEW.md` covers the review path — reading
notes a human already left and drafting resolutions — and its `apply` is not a creator:
it replaces the text of an existing annotation and has nothing to attach a new one to.
`docs/INGEST_GUTENBERG.md` covers the import path, which turns the *author's* own
`[FOOTNOTE:N]` tokens into `origin: "gutenberg"` records. All three converge at
`src/endnotes.py`, which numbers whatever it finds in `annotations.jsonl` regardless of
who wrote it.

Agent behaviour — the gates, what to ask, what to relay — lives in
`.claude/skills/footnote-pass/SKILL.md` and is not restated here. This document is the
engineering reference: the record shape, the orphan table, the profile contract, the
layout.

## The CLI

```bash
# The style-inference corpus. Prints counts; the notes go to a file.
python scripts/footnote_pass.py style --project fabre2 [--chapters 1-20]

# Render one scan prompt per chapter (no spend). --profile-file is required.
python scripts/footnote_pass.py scan-prepare --project fabre2 [--chapters 1-20] \
    --profile-file projects/fabre2/.harness/footnotes/profile.md \
    [--worker-model sonnet] [--batch-size 5] [--keep-drafts] [--source-text es|both]

# Then EITHER a headless wave …
python scripts/footnote_pass.py scan-fanout --project fabre2 \
    [--cli claude|cursor] [--cli-bin <path>] [--concurrency 5] \
    [--target-ids chapter_04,chapter_05] [--effort medium] [--prompt-cache auto]
# … OR spawn footnote-scan-worker Task subagents against the manifest's paths.

# Parse drafts, validate candidates, write candidates.json + the dated report.
python scripts/footnote_pass.py scan-commit --project fabre2 [--no-report]

# The writer. --json-file takes the decisions document (keeps AND drops).
python scripts/footnote_pass.py add --project fabre2 \
    --chapter chapter_04 --es-idx 40 --anchor "ubres," --note "Hoy sabemos que…" [--dry-run]
python scripts/footnote_pass.py add --project fabre2 --json-file <decisions.json> [--dry-run] [--no-report]

# Audit every active footnote against the whole table.
python scripts/footnote_pass.py verify --project fabre2 [--chapters 1-20]
```

Every subcommand prints exactly one JSON object with a `_schema` block, and mirrors the
payload (minus `_schema`) to `projects/<slug>/.harness/footnotes/last_output.json` with
`OUTPUT_JSON: <path>` on stderr — the same contract `harness.py` and `run_judges.py`
carry, and for the same reason: a file on disk is readable without a second interpreter
mangling the accents on the way through.

`--chapters` accepts `1-20`, `3,7,12`, `1-3,7`, or explicit chapter ids.

## Why `add` exists

`src/endnotes.py:build_endnote_artifacts` drops a footnote **silently** in three ways.
A record can be valid JSON, sit in the file for ever, and publish nothing:

| Code | Condition | What endnotes.py does |
|---|---|---|
| `no_aligned_sentence` | no alignment row carries the `es_idx` | logs a warning, skips (`endnotes.py:161`) |
| `sentence_not_in_body` | the aligned `es` is not findable in `chapters/<id>.txt` | logs a warning, skips (`endnotes.py:168`) |
| `empty_gloss` | no display text once the first `[bracket]` is stripped | **skips, logs nothing** (`endnotes.py:180`) |

Plus three problems that are not silent but are still wrong:

| Code | Condition | Severity |
|---|---|---|
| `multi_anchor` | more than one `[bracket]`; the extras publish verbatim into the book (`targets.py:292`) | refuse |
| `no_chapter_body` | no `chapters/<id>.txt` at all | refuse |
| `duplicate` | an active footnote on that sentence already holds exactly this text | refuse |
| `anchor_not_found` | the anchor is absent; the marker falls to the sentence end (`_injection_point`) | warn |
| `ambiguous_anchor` | the anchor recurs; the marker takes the first hit. A unique anchor grown with `footnote_import._unique_anchor` is suggested | warn |
| `sentence_drifted` | `verify` only: the alignment's `es` at that index no longer matches the record's own `es_text` snapshot | warn |

`WARNING_CODES` is the dividing line in `src/footnote_pass/write.py`: a warning means
*placement* is degraded, a refusal means nothing reaches the book.

`add` is per-note, not per-batch — one bad entry in a `--json-file` lands the rest and
reports that one, so a re-run after a fix is cheap. Re-running an identical `add` is a
`duplicate` refusal rather than a second endnote.

### Run `verify` after any re-align

`es_idx` is a **position** in `alignments/<id>.json`, not an identity. `harness.py
align` renumbers rows, so a note can go from correct to orphaned (`no_aligned_sentence`)
or to attached-to-the-wrong-sentence (`sentence_drifted`) without anything being
written. `sentence_drifted` is only detectable because `add` snapshots `es_text` into
the record; reader-written notes have no snapshot and cannot be checked this way.

## Record shape

```json
{
  "project_id": "fabre2",
  "chapter_id": "chapter_04",
  "es_idx": 40,
  "sub_id": "u48138bfb",
  "type": "footnote",
  "content": "[ubres,] Hoy sabemos que esa gota azucarada no sale de esos dos tubitos…",
  "es_text": "…se ordeñan las ubres, y la gota sale.",
  "origin": "footnote_pass",
  "timestamp": "2026-09-11T07:04:11.233412"
}
```

Mirrors what `web_ui/app.py:save_annotation` writes — the reader has to be able to open,
edit and delete one of these like any other note — plus `origin`.

- **`sub_id`** is `"u" + secrets.token_hex(4)`, the reader's own convention, and clear
  of the `gb<n>` namespace `src/footnote_import.py` owns. Several footnotes may share one
  sentence; `endnotes.py` numbers them by position in the text, not by `es_idx` order.
- **`origin: "footnote_pass"`** is provenance only. Downstream special-cases
  `"gutenberg"` alone (`src/annotations/targets.py:232`, the `imported` eligibility
  gate), so this value is inert by design — these notes stay reviewable by
  `annotation-review` like a reader's.
- **`content`** is `[anchor] gloss`, composed by `write.compose_content`, the same shape
  `review._planned_content` writes for a footnote. The anchor is optional.
- Writes go through `src.annotations.store.append_record`. The file is append-only by
  contract; nothing here ever rewrites a line.

## The profile contract

`scan-prepare` **requires** `--profile-file`. That is the Gate 1 enforcement point: the
file holds the taxonomy of what counts as a footnote candidate in this book, inferred
from the notes it already carries and then approved by a human. Without it a wave would
run on the model's own idea of what deserves a note.

The file is plain markdown, written by the agent after the gate, conventionally at
`projects/<slug>/.harness/footnotes/profile.md`. Its whole text is interpolated into the
scan preamble. There is no schema — a bulleted list of categories is what the prompt
expects, and anything the user added in their own words is carried verbatim.

### The scan is detection-only

The scan prompt gets the profile and nothing else about the book's voice. **No style
guide, no glossary.** Those govern how a gloss is *worded*, and a scanner given them
starts writing notes — the wrong output, and the wave is wasted. They load later, in the
conversation, when the gloss is actually drafted.

A candidate is `{chapter_id, es_idx, quoted_span, category, claim, why}`. Never a gloss.

One consequence worth recording: `docs/ANNOTATION_REVIEW.md`'s cache-split section notes
that the glossary is what carried *that* preamble over Sonnet's 1024-token cache
minimum, and this preamble has no glossary to lean on. Measured on `fabre2` with a
two-category profile it comes out at **~1.1k estimated tokens** (`estimate_tokens`, the
rough 4-chars/token rule) against a chapter body of ~2.7k — so it sits barely over the
line, close enough that a terser profile would fall under it and the wave would simply
not cache.

That is a cost detail, not a correctness one, and the body dominates the job either way.
**Do not pad the prompt to hit the threshold.** If caching on the scan turns out to
matter, measure it from `usage.jsonl`'s `cache_read` column and fix it in the template,
not by padding the profile.

### The scan reads the Spanish alone

`scan.render_body` emits numbered `es_idx | ES` rows. No English, by default.

This is the shape `src/judges/editorial_judge.py` already uses, for the reason
`docs/EDITORIAL_JUDGE.md` ("Why pass one is blind to the English") gives: *a reader who
can see the original stops evaluating the Spanish as Spanish and starts diffing it
against the source.* It applies here with one extra argument of its own — a footnote is
written for someone holding the translation and nothing else, so the pass deciding
whether a sentence needs one should be reading what that reader reads.

**The English is moved, not dropped.** `scan_commit` stamps `en_sentence` onto every
usable candidate from the alignment, unconditionally, and `report.py` prints it under
each one. So the source rejoins the claim at Gate 2 — in front of a human and an agent
who can act on the difference, rather than a detector that can only avoid proposing.
That is the `editorial_verify` division of labour, and it is deliberately *not* gated on
a per-candidate `source_check` field the way that module's is: the
`.claude/skill-friction-logs/judge-review/2026-08-27-five-little-peppers-editorial-ch12-16-claude-switch.md`
run recorded `source_requested: 0` on a set where four of five findings turned on the
English, and pass two confirmed everything. Attaching it to all of them costs nothing
here, because it is already on the row.

`scan-prepare --source-text both` restores the EN line and the old instruction with it
(one `{{source_rule}}` variable above the cache split selects the paragraph, so a
bilingual body can never be served under a preamble that says the English is not in
front of you). Make it a real choice: measured across the 40 prepared `fabre2` bodies
the EN lines are **48.1%** of the body against the ES lines' 48.8% — EN/ES = 0.98, so
carrying the source very nearly doubles the wave's input.

`prompt_version` hashes the template, which both modes share, so it cannot tell them
apart. `source_text` is recorded on the manifest, in `scan-prepare`'s payload and
`usage_summary`, echoed by `scan-fanout`, and carried into `candidates.json` — that
field is the only thing that says which prompt a candidate set came from.

Sentences that already carry a footnote are marked `[ALREADY NOTED]` inline, which is
cheaper than a cross-referenced exclusion list — and `scan-commit` rejects a
re-proposal anyway.

### Candidates are validated before anyone sees them

`scan-commit` refuses a candidate whose `es_idx` has no alignment row, or whose
`quoted_span` does not occur verbatim in that aligned sentence. Those arrive as
`unusable` with a reason and are never offered as choices. `span_not_in_sentence` is the
one to watch — it means the worker paraphrased instead of quoting, and a list that is
mostly that indicates the wrong model tier rather than a problem in the book.

## Effort

The scan registers as its own wave type, `footnote_scan`, in
`src/harness/state.py:COMMAND_EFFORT_DEFAULTS` at **medium**, and
`headless_effort_footnote_scan` comes with it through `harness.py config-set`.
`--effort` overrides per run.

The band is a measurement, not a preference. Medium here is named by analogy with the
judge waves (detection against a fixed rubric, where medium measured out at no quality
loss) rather than from its own sweep. Re-measure and move the row, with a comment.

**The table binds on Claude. On Cursor it usually does not**, and saying otherwise is
what the 2026-09-11 `fabre2` friction log is about. Claude takes the level as a
`--effort` flag, so withholding the CLI's high band is a thing this table can do. Cursor
has no flag: the level rides in the model's own `[effort=…]` bracket, and
`profile.resolve_profile` (`src/harness/profile.py:318-323`) deliberately honours
whatever the operator already selected in Cursor's model picker rather than rewriting
argv they never asked to change. A Cursor scan therefore commonly runs at **high**.

That is a reporting problem, not a correctness one, and it is solved by reporting:
`scan-prepare` and `scan-fanout` both resolve through `resolve_profile` and emit one
`effective` block — `cli`, `worker_model`, `effort`, `effort_channel`, each with its
provenance — and `usage_summary.headless_effort` is derived from the same profile, so
the two cannot tell different stories. Quote `effective` at the consent gate. Before
this, `usage_summary` said `medium`/`default:footnote_scan` beside a
`grok-4.6[effort=high,fast=false]` worker, and an agent relaying "the summary" could
truthfully sell a 23-minute high wave as a medium one.

## Layout

```
src/footnote_pass/
  __init__.py      FOOTNOTE_TYPE / ORIGIN, and why the package exists
  corpus.py        the style corpus: gloss vs placeholder, alignment/body readers
  scan.py          scan_prepare / scan_fanout / scan_commit, the candidate validator
  write.py         add / verify, the validation table, sub_id minting
  ledger.py        the decision ledger: the candidate <-> note join
  report.py        the dated candidate and decision reports
scripts/footnote_pass.py
prompts/footnote_scan.txt
.claude/skills/footnote-pass/SKILL.md
.claude/agents/footnote-scan-worker.md

projects/<slug>/.harness/footnotes/
  style_corpus.md              every existing footnote, split gloss/placeholder
  profile.md                   the approved taxonomy (by convention; any path works)
  scan.manifest.json           what scan-prepare staged
  preamble.scan.txt            shared, cacheable
  <chapter_id>.scan.body.txt   one chapter's sentence rows (ES; +EN under --source-text both)
  <chapter_id>.scan.prompt.txt preamble + body (Task workers)
  <chapter_id>.scan.draft.json worker output
  candidates.json              the validated shortlist (REPLACED each scan-commit)
  decisions.json               keeps and drops (by convention; add --json-file)
  decisions.jsonl              the append-only decision ledger
  scan.usage.jsonl             per-job headless usage
  last_output.json             the OUTPUT_JSON sidecar
projects/<slug>/reports/footnote_candidates_<YYYYmmdd_HHMMSS>.md
projects/<slug>/reports/footnote_decisions_<YYYYmmdd_HHMMSS>[_proposal].md
```

## The decision ledger

`candidates.json` records what a wave proposed and `annotations.jsonl` records what
reached the book. Everything in between — which candidates the human cut at Gate 2,
which the agent killed while researching, what a gloss said before it was rewritten,
and which claim any landed note came from — used to exist only in a chat transcript.
And `scan-commit` **replaces `candidates.json` wholesale**, so the proposals went with
it.

`src/footnote_pass/ledger.py` closes that. Every `add` appends one row per decision to
`.harness/footnotes/decisions.jsonl` and renders
`reports/footnote_decisions_<stamp>.md`.

**Append-only, not replace-in-place.** `src/annotations/review.py`'s `results.json` is
rewritten because it is a *plan* that `apply` still has to execute, so a second commit
must merge rather than clobber owed work. Nothing reads this as a plan, so merge logic
would be liability without a payer. It inherits `store.append_record`'s superseding
rule instead: a later row at the same key wins and the earlier stays as history — which
is what makes a refusal, a fix, and the landing readable as one sequence.

**Snapshots, not references.** Each row embeds the candidate's category, claim and span
and the model that proposed it. A row that merely pointed at `candidates.json` would be
worthless the moment the next commit rewrote it, which is the exact failure this
module exists to prevent.

**The join is `candidate_key`, not `(chapter_id, es_idx)`.** That pair is not unique —
`scan_commit` never dedupes on it and `write.py` deliberately allows several notes on
one sentence — and not stable, since `es_idx` is a position, which is the whole reason
`sentence_drifted` exists. `candidate_key` is
`<chapter_id>__<es_idx>__<sha1(quoted_span)[:8]>`, stamped onto every usable row at
commit time and printed in the candidate report. Resolution is two-tier and reports its
own quality in `join`: `exact` (the decision row carried the key), `sentence` (matched
the pair, and exactly one candidate had it), `none`. **An ambiguous sentence degrades to
`none` rather than guessing** — a ledger that claims a claim it cannot prove belongs to
a note is worse than one that says it does not know. The join to `annotations.jsonl` is
`sub_id`.

**`--dry-run` renders the report and appends nothing.** A proposal is not a decision,
and a ledger that cannot tell "what we considered" from "what was chosen" is the
confusion it exists to end. The pre-edit gloss is not lost: the dated *proposal* report
holds it verbatim, and no later run deletes it. That proposal render is also the Gate 3
review page — it carries the gloss in full and the injection preview showing where the
marker falls, because the `AskUserQuestion` widget cannot hold an 80-word Spanish
sentence plus a two-sentence gloss, and asking through it is asking a human to approve
copy they have not seen.

`counts.undecided` names usable candidates, in chapters this run touched, that appear
in neither a keep nor a drop. It is a **warning that does not change `status`**: an
omission is not a malformed instruction, and a routine `add` that exits nonzero makes
the "then run `verify`" flow read as a failure. An unknown `verdict` *is* an error and
does flip `status` to `partial` — guessing which way the operator meant it is how
unapproved copy lands in a published book.

### Two waves, one directory

`.harness/footnotes/` is shared with `harness.py footnotes`, the *translation* wave for
imported Gutenberg notes (`src/harness/flow.py:_footnote_work_dir`). Both used to write
`manifest.json` there and append to the same `usage.jsonl`: whichever prepared last
silently owned the manifest, and `profile.baseline_tokens` read a median across two
wave types with very different prompt sizes, which described neither. This wave now
owns `scan.manifest.json` and `scan.usage.jsonl`, registered under `footnote_scan` in
`profile.USAGE_LOG_RELPATH`. Stale unprefixed files from before the split are simply
orphaned; nothing migrates.

Reports are in **English**, unlike the annotation-review reports: a candidate is a
detection note addressed to the editor, not prose for the book's reader. The gloss that
does get published is drafted in the conversation and never appears in the report.

## Publishing

```bash
python scripts/harness.py epub --project fabre2
```

The log line `Endnotes section appended (N notes)` is the end-to-end proof. It must
increase by exactly the number of notes added — no individual validation can establish
that, which is why `tests/test_footnote_pass/test_write.py` round-trips through
`endnotes.build_endnote_artifacts` rather than stopping at a well-formed record.

## Related

- [`ANNOTATION_REVIEW.md`](ANNOTATION_REVIEW.md) — the review path over the same file.
- [`INGEST_GUTENBERG.md`](INGEST_GUTENBERG.md) — imported author footnotes.
- [`WEB_UI_GUIDE.md`](WEB_UI_GUIDE.md) — the reader, which shows and edits these notes.
- [`TRANSLATE_HARNESS.md`](TRANSLATE_HARNESS.md) — `config-set`, `align`, `epub`.
