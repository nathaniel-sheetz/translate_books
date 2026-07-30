# Annotation review

A post-human-review LLM pass over the notes a reader leaves while reading a
finished translation.

The pipeline has three LLM passes. Translation drafts the prose. The tailored
judges (`docs/JUDGES_FRAMEWORK.md`) check that prose against house rules. Both run
*before* anyone reads the book. Annotation review runs *after*: it reads the
reader's own annotations, researches each one, and drafts a resolution.

It is **note-only** — it never edits translated prose. Rewriting the translation is
`run_judges.py apply`'s job and stays there.

## What it reviews

The four types the reader UI writes into `projects/<slug>/annotations.jsonl`
(see `docs/WEB_UI_GUIDE.md`):

| Type | The reader's question | What the review produces |
|---|---|---|
| `word_choice` | is this word right? | a judgement, weighed against the style guide, glossary, source, and book usage |
| `inconsistency` | are we using several words for one thing? | a unify-or-accept verdict, driven by a book-wide concordance |
| `footnote` | a future reader would want a gloss here | the actual endnote gloss |
| `flag` ("Other") | anything else | the inferred concern plus a concrete next step |

## The CLI

```bash
# Stage one prompt per annotation (no spend). Whole book, all four types, by default.
python scripts/review_annotations.py prepare --project fabre2 \
    [--type word_choice,footnote] [--scope chapter:chapter_04] \
    [--worker-model sonnet] [--batch-size 5] [--keep-drafts]

# Then EITHER a headless wave …
python scripts/review_annotations.py fanout --project fabre2 \
    [--cli claude|cursor] [--cli-bin <path>] [--concurrency 5] [--target-ids k1,k2]
# … OR spawn annotation-worker Task subagents against the manifest's paths.

# Parse the drafts, write results.json and the dated report.
python scripts/review_annotations.py commit --project fabre2 [--no-report] [--full]

# Or skip all of that and use the API backend (metered, cost-gated).
python scripts/review_annotations.py run --project fabre2 [--cost-limit 0.50] [--confirm] [--full]

# Write reviewed notes back. The only writer to annotations.jsonl.
python scripts/review_annotations.py apply --project fabre2 --dry-run
python scripts/review_annotations.py apply --project fabre2 --select <key,key,...> [--full]
```

Every subcommand prints exactly one JSON object with a `_schema` block. Keys are
`<chapter_id>__<es_idx>__<sub_id>`, e.g. `chapter_04__37__u72399176` (`legacy` for
rows with no `sub_id`).

### stdout is a summary

`commit`, `run` and the real `apply` print keys, states and counts. The
per-annotation content lives in two artifacts built for it — the dated markdown
report (what a human or an agent relays) and `results.json` (what `apply` reads) —
and is not echoed a third time. `review.relay_view` / `review.apply_relay_view` do
the projection at the CLI's emit boundary, so the Python functions still return
everything to an in-process caller; `--full` prints that untrimmed payload.

The measured case: a 2-target `commit` on a book with 25 gated annotations went
19.3KB → 3.1KB. The run this came from returned **29.4KB** — `prepare`'s 17
imported-footnote bodies echoed byte-identically, plus every verdict the report was
rendering in prose at that same moment — which overflowed the agent's tool-output
limit, got truncated to a side file, and had to be read back off disk to be relayed
at all. Three exceptions keep the trim from hiding anything: `--no-report` prints
`results[]` (no artifact exists to read), a real `apply` re-prints the full plan
entry for any key that came back `stale`, and `counts` always reports true totals.

The conversational front end is `.claude/skills/annotation-review/SKILL.md`.

## Backends

Three, mirroring the judges. All three build the same prompts through
`prompts.build_prompt_parts` and parse with `review.parse_verdict`, so results are
the same shape whichever ran.

| Backend | Path | Spend | Gate |
|---|---|---|---|
| API | `run` | metered $ on this repo's key | dollar cost gate (`--cost-limit`, `--confirm`) |
| Task subagents | `prepare` → spawn `annotation-worker` → `commit` | none (session usage) | usage gate |
| Headless | `prepare` → `fanout` → `commit` | none (subscription, enforced) | usage gate |

Headless is the better default for throughput: 6–40 annotations is one bounded
wave, and it is the only path that uses the prompt cache.

**Headless is subscription-only by enforcement.** `fanout` shells out to the
user's own `claude` / `cursor-agent` binary, so `src/harness/headless.py` scrubs
every metered credential from the child environment and runs one
`claude auth status --json` preflight per wave. Unless a subscription is
confirmed the command returns a top-level `error`, writes nothing and spawns no
jobs. There is no override flag — metered spend goes through `--backend api`,
which is what that backend is for. Full rationale, and why both layers are
load-bearing, in `docs/LLM_PROVIDERS.md`. **Caveat:** there is no verified
`cursor-agent` auth-status command, so the Cursor profile gets the scrub but not
the preflight.

That replaced a weaker guarantee, and the failure it prevents is worth recording.
A real `stormy-misty-s-foal` run died after 15 of 28 jobs with `Credit balance is
too low`: the wave had been billing metered credit all along. The message arrives
on the CLI's **stdout** while stderr carries an unrelated connectors warning, and
`headless.py` used to report stderr alone — so every failed job showed the red
herring. Both streams are joined now, which keeps any *other* failure cause
legible; the preflight is what stops this particular one from occurring.

### The cache split

Templates split on `src/judges/base.py:_CACHE_PREFIX_SPLIT_MARKER`. Above the
marker sits everything identical across a type's annotations — the rubric, the
style guide, the glossary, the target language — written once to
`preamble.<type>.txt` and passed as `--system-prompt-file`. Below it is the
per-annotation body.

The style guide alone runs ~400–550 tokens on real books, which would **miss**
Sonnet's 1024-token cache minimum. Including the glossary carries the preamble to
~3.2k, over the line — and the book's established terminology is exactly what a
word-choice or inconsistency call needs. That is why the glossary is in the
preamble rather than filtered per annotation. Opus/Haiku need 4096, so pin Sonnet
workers.

`cursor-agent` has no `--system-prompt-file`; `harness/headless.py::_fold_system_prompt`
folds the preamble into stdin for it automatically, so `fanout` builds one job
shape for both CLIs.

## Eligibility gates

Decided in `targets.py`, before any LLM call:

| Reason | Meaning |
|---|---|
| `already_reviewed` | a prior run's text is still intact — the anti-duplication gate |
| `imported` | an `origin: "gutenberg"` footnote that already carries its body |
| `orphaned` | `es_idx` no longer resolves to an aligned sentence |
| `multi_anchor` | a footnote naming several spans — reviewed, but never auto-written |

### How duplicate notes are prevented

Two layers, and the deterministic one does the real work.

**Layer 1.** Records written by `apply` carry an `ai_review` sidecar recording the
exact text written. The next run compares `ai_review.written_content` to the live
`content`; equal means skip, with no LLM call. It self-heals because
`POST /api/annotation` (`web_ui/app.py`) rebuilds records from a fixed key set — a
reader edit drops the sidecar, correctly re-opening the annotation.

**Layer 2.** For records with no sidecar, the model classifies `needs_help` vs
`already_resolved`, catching conclusions the *human* wrote. Reported, never
appended.

### Multi-anchor footnotes

A note like `[Neuve-Celle,]; [Esaú,]; [Montélimar.]` names three spans.
`src/endnotes.py` consumes only the **first** bracket and publishes the remainder as
endnote text, so today that note publishes `; [Esaú,]; [Montélimar.]` into the book.
The review drafts a gloss but withholds the write: the correct fix is splitting one
annotation into three, which renumbers endnotes — a human call. The report prints
the drafted glosses so the reader can split them in the reader UI.

## Write-back

```
footnote        →  REPLACE:  content = "[<first anchor>] <gloss>"
word_choice     →  APPEND:   content = "<original>\n— IA: <note>"
inconsistency   →  APPEND
flag            →  APPEND
```

Footnotes replace because their content **is** the published endnote text. Appending
a gloss after an instruction word would print that word into the book: a note reading
`[Aragón,] comillas` would publish as *"comillas Región del noreste de España…"*.
Nothing is lost — `annotations.jsonl` is append-only, so the original record stays on
disk and the report logs it verbatim.

The `— IA:` marker (`— AI:` outside Romance target languages) makes model text
visible in the reader. Footnotes get no marker; that text is published.

Applied records look like:

```json
{"project_id": "fabre2", "chapter_id": "chapter_09", "es_idx": 2,
 "type": "footnote", "content": "[Sancerre] Ciudad del centro de Francia…",
 "timestamp": "…", "sub_id": "u1",
 "ai_review": {"run_id": "…", "at": "…", "mode": "replace",
               "prompt_version": "<sha256 of the template>",
               "original_content": "[Sancerre]",
               "written_content": "[Sancerre] Ciudad del centro de Francia…"}}
```

`apply` requires an explicit `--select`; omitting it is a plan-only dry run. Before
writing, it compares each annotation's live content to what the review saw and
reports a mismatch as `stale` rather than overwriting it.

## Reports

`projects/<slug>/reports/annotations_<YYYYmmdd_HHMMSS>.md`, dated so a book
accumulates one per run. Written in the book's target language (English fallback for
languages without a string table). Sections: header, summary table by type, one
subsection per annotation — **content as of run time, verbatim** — and `Omitidas`
listing every skip with its reason.

## Layout

```
src/annotations/
  store.py         annotations.jsonl access; the (es_idx, sub_id) tombstone rule, shared
  anchors.py       [bracket] parsing — front, trailing, multiple, absent
  targets.py       AnnotationTarget + the eligibility gates
  concordance.py   book-wide whole-word folded search over alignments/
  prompts.py       template rendering + the cache split
  review.py        prepare / fanout / commit / run / apply
  report.py        the dated markdown report
scripts/review_annotations.py
prompts/annotation_{word_choice,inconsistency,footnote,flag}.txt
.claude/skills/annotation-review/SKILL.md
.claude/agents/annotation-worker.md

projects/<slug>/.harness/annotations/
  manifest.json           what prepare staged
  preamble.<type>.txt     shared, cacheable
  <key>.<type>.body.txt   per-annotation half
  <key>.<type>.prompt.txt preamble + body (Task workers, API)
  <key>.<type>.draft.json worker output
  results.json            the apply plan; commit merges into it by key
```

`store.py` deliberately imports nothing from `web_ui`, so `src/endnotes.py` — which
`epub_builder` calls — can share it without a circular dependency. The folded-search
primitives live in `src/utils/text_utils.py` for the same reason; `web_ui/app.py`
imports them back under their original private names for the reader's "Find in
book".

## Adding an annotation type

1. `web_ui/app.py:save_annotation` — add it to `_allowed_ann_types` (unknown types
   are silently coerced to `flag`).
2. Reader front end — `ANN_RANK` in `static/reader.js`, the CSS tints in
   `static/reader.css`, the i18n strings in `web_ui/i18n.py`, and both sheet
   templates.
3. `src/annotations/store.py` — `ANNOTATION_TYPES`.
4. `prompts/annotation_<type>.txt` — with the cache-split marker.
5. `src/annotations/prompts.py` — `TEMPLATES`.
6. Decide append vs replace in `review._planned_content`. Replace only if something
   downstream publishes the content.
7. `src/annotations/report.py` — the `types` label maps and `_TYPE_ORDER`.
8. Tests in `tests/test_annotations/`.

## Related

- `docs/JUDGES_FRAMEWORK.md` — the pre-review pass. Separate persistence: annotation
  results never touch `evaluations/*.json` or the dashboard badges.
- `docs/WEB_UI_GUIDE.md` — where annotations come from.
- `docs/INGEST_GUTENBERG.md` — imported footnotes, which this pass skips.
