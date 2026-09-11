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
    [--worker-model sonnet] [--batch-size 5] [--keep-drafts]

# Then EITHER a headless wave …
python scripts/footnote_pass.py scan-fanout --project fabre2 \
    [--cli claude|cursor] [--cli-bin <path>] [--concurrency 5] \
    [--target-ids chapter_04,chapter_05] [--effort medium] [--prompt-cache auto]
# … OR spawn footnote-scan-worker Task subagents against the manifest's paths.

# Parse drafts, validate candidates, write candidates.json + the dated report.
python scripts/footnote_pass.py scan-commit --project fabre2 [--no-report]

# The writer.
python scripts/footnote_pass.py add --project fabre2 \
    --chapter chapter_04 --es-idx 40 --anchor "ubres," --note "Hoy sabemos que…" [--dry-run]
python scripts/footnote_pass.py add --project fabre2 --json-file <approved.json> [--dry-run]

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

### The body carries both languages

`scan.render_body` emits numbered `es_idx | ES | EN` rows. The English side is
load-bearing: most candidates are of the form "the author asserts X", and X is judged
against the source, not the translation. Sentences that already carry a footnote are
marked `[ALREADY NOTED]` inline, which is cheaper than a cross-referenced exclusion list
— and `scan-commit` rejects a re-proposal anyway.

### Candidates are validated before anyone sees them

`scan-commit` refuses a candidate whose `es_idx` has no alignment row, or whose
`quoted_span` does not occur verbatim in that aligned sentence. Those arrive as
`unusable` with a reason and are never offered as choices. `span_not_in_sentence` is the
one to watch — it means the worker paraphrased instead of quoting, and a list that is
mostly that indicates the wrong model tier rather than a problem in the book.

## Effort

The scan registers as its own wave type, `footnote_scan`, in
`src/harness/state.py:COMMAND_EFFORT_DEFAULTS` at **medium** — so an unpinned wave does
not inherit the Claude CLI's high band. That also gives `headless_effort_footnote_scan`
through `harness.py config-set` for free, and `--effort` overrides per run.

The band is a measurement, not a preference. Medium here is named by analogy with the
judge waves (detection against a fixed rubric, where medium measured out at no quality
loss) rather than from its own sweep. Re-measure and move the row, with a comment.

## Layout

```
src/footnote_pass/
  __init__.py      FOOTNOTE_TYPE / ORIGIN, and why the package exists
  corpus.py        the style corpus: gloss vs placeholder, alignment/body readers
  scan.py          scan_prepare / scan_fanout / scan_commit, the candidate validator
  write.py         add / verify, the validation table, sub_id minting
  report.py        the dated candidate report
scripts/footnote_pass.py
prompts/footnote_scan.txt
.claude/skills/footnote-pass/SKILL.md
.claude/agents/footnote-scan-worker.md

projects/<slug>/.harness/footnotes/
  style_corpus.md              every existing footnote, split gloss/placeholder
  profile.md                   the approved taxonomy (by convention; any path works)
  manifest.json                what scan-prepare staged
  preamble.scan.txt            shared, cacheable
  <chapter_id>.scan.body.txt   one chapter's bilingual rows
  <chapter_id>.scan.prompt.txt preamble + body (Task workers)
  <chapter_id>.scan.draft.json worker output
  candidates.json              the validated shortlist
  approved.json                what the user approved (by convention; add --json-file)
  usage.jsonl                  per-job headless usage
  last_output.json             the OUTPUT_JSON sidecar
projects/<slug>/reports/footnote_candidates_<YYYYmmdd_HHMMSS>.md
```

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
