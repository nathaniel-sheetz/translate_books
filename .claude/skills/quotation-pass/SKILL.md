---
name: quotation-pass
description: |
  Put the NBLA (Nueva Biblia de las Américas) into a finished translation's Bible
  quotations: find every quotation, epigraph, allusion and Scripture reference in the
  aligned chapters, look each verse up in the user's own NBLA index, map the words the
  author quotes onto the Spanish, propose echo edits to nearby prose, and show every
  change on one web board to tick and approve. Applies through the judge-apply path and
  records exact verse and word counts in quotations.json for kdp-publish. Three headless
  waves, subscription only, no dollars. Post-translation only.
  Use when asked to "put the NBLA in", "fix the Bible quotes", "map the Scripture
  quotations", "standardize the Bible references", "use the real NBLA text", or
  "quotation-pass".
allowed-tools:
  - Bash
  - Read
  - Write
  - Task
  - AskUserQuestion
---

# quotation-pass

An optional pass for a **finished** translation of a book that quotes Scripture. It
replaces the old user-level `nbla-bible-mapping` skill for books under `projects/`.

The deterministic surface is one CLI, **`scripts/quotation_pass.py`**. Every
subcommand prints one JSON object and mirrors it to
`projects/<slug>/.harness/quotations/last_output.json`.

Decisions that are settled (memory `project_quotation_pass`): Bible only (a quotation
that is not Scripture gets a paste box, nothing more); post-translation only;
copyright is kdp-publish's call. This pass only records the numbers.

## Rules

- **Never edit chunk JSON, `source_text` or `corrections.jsonl` yourself.** Every
  change goes through `apply`, which snapshots, applies, archives, recombines,
  realigns and re-anchors annotations.
- **The NBLA never enters the repo.** The index is `prompts/bible/nbla.json`
  (gitignored, no `.example`). Never paste verse text into tracked files, tests or
  commit messages.
- **The user picks on the board, never by typed ids** (memory
  `feedback_picks_on_a_page_not_ids`).
- **Quote the usage gate before each wave**, with parallel and sequential minutes.

## 0. Status and the index

```bash
python -X utf8 scripts/quotation_pass.py status --project <slug>
```

If `index.ok` is false, ask the user for their NBLA EPUB and build the index once per
machine:

```bash
python -X utf8 scripts/quotation_pass.py index --epub "<path to NBLA.epub>"
```

It refuses an EPUB whose title is not the NBLA, and `load_index` refuses an index
whose EPUB has changed since. Rebuild it then.

## 1. Detect (wave 1)

```bash
python -X utf8 scripts/quotation_pass.py scan-prepare --project <slug> --worker-model claude-sonnet-5-5
```

**Usage gate.** Quote `jobs`, `words`, `written_refs` and `effective` (CLI, model,
effort). Measured on short-sermons (16 chapters, 18.6k English words, Sonnet 5.5 at
medium): **81 s wall at 5 in parallel**, about 5 minutes one job at a time. Scale by
words. Then:

```bash
python -X utf8 scripts/quotation_pass.py scan-fanout --project <slug>
python -X utf8 scripts/quotation_pass.py scan-commit --project <slug>
```

The Task backend is the alternative: spawn one `quotation-scan-worker` per job
(`prompt_path` → `draft_path`), then `scan-commit`.

`scan-commit` reports `written_refs`: every reference written in the English is
attached to a candidate, by the wave or as a coded `reference_only` card. That is
the recall floor. Failed or missing jobs: re-run `scan-fanout` (it skips drafted
jobs), then commit again. A failed job that says "hit your limit" waits for the
reset time it names.

## 2. Resolve (code only)

```bash
python -X utf8 scripts/quotation_pass.py resolve --project <slug>
```

Looks every verse up and works out each reference fix (abbreviation only, the
Spanish's own numbers, the book's range dash). `unresolved` cards are not errors;
they go on the board.

## 3. Map (wave 2)

```bash
python -X utf8 scripts/quotation_pass.py map-prepare --project <slug> --worker-model claude-sonnet-5-5
```

**Usage gate.** Quote `jobs`, `candidates`, `words` and `effective` (effort high by
default). Measured on short-sermons (15 chapters, 311 candidates): **224 s wall at
5 in parallel**, about 15 minutes sequential, roughly 150k output tokens. Then
`map-fanout`, `map-commit` (or one `quotation-map-worker` per job).

`map-commit` validates every string the worker returned: pieces must be verbatim
NBLA, every `old` must occur once in its row, echo edits need English evidence, and
overlaps are refused. Code, never the worker, builds the Spanish with the NBLA's
conventions. Read `refusals`; a pattern there is worth a sentence to the user.

## 3b. Judge (wave 3)

A filter over the mapping wave: one verdict and confidence per card that would change
the text. It writes nothing to the book, `mapped.json` or the fingerprint.

```bash
python -X utf8 scripts/quotation_pass.py judge-prepare --project <slug> --worker-model gpt-6.1-terra --cli codex
```

**Usage gate.** Quote `jobs`, `items` and `effective`. Measured on 168 cards in 12 jobs
at medium, 5 in parallel: **about 42 s and 11k output tokens** on Terra, about 2.5
minutes sequentially; about 60 s and 20-25k on Sonnet 5.5, about 3.5 minutes
sequentially. Grok 4.7 took 7 to 16 minutes in parallel for no better result, so don't
default to it. Then:

```bash
python -X utf8 scripts/quotation_pass.py judge-fanout --project <slug>
python -X utf8 scripts/quotation_pass.py judge-commit --project <slug>
```

- **Run it before the user reviews a chapter.** A verdict moves the default of any card
  with no saved choice, and a card the user read and left alone has none. On a book
  already partly reviewed that way, pass `--chapters` for the unreviewed chapters only.
- **Only a confident apply sets a default:** `apply` at 0.90 or above starts on Apply,
  and everything else starts on Not yet with its verdict showing. On the held-out score
  (2026-11-02, 97 cards), applies at that floor were 34 with none wrong. Rejects were 3
  wrong of 26, and 14 of the 30 rejects at any confidence were cards the user partly kept.
- **A revised card's verdict goes stale** and it falls back to the coded rules.
- **Don't tune on a set you've scored.** The replay is
  `scripts/quotation_judge_gold.py`, and chapters shown with verdicts are no longer blind.

## 4. Board

```bash
python -X utf8 scripts/quotation_pass.py board --project <slug>
```

Give the user the `url` (restart the reader with `scripts/reader.ps1 restart` if it
predates this branch).

- **What the change is:** the ticks. Each card has **Fix the reference** and **Use
  the NBLA wording**, both on by default wherever the card has one. Echo edits are
  separate ticks, off by default. Not-Scripture and unresolved cards have a paste
  box.
- **Whether it happens:** each card's state, **Apply / Not yet / Reject**. It starts
  from a coded recommendation (approve, not sure or decline), shown with the rule
  that gave it. Drift, adapted cards, allusions, low confidence, refusals and
  echo-only cards are "not sure" and start at Not yet. A judged card starts from its
  verdict instead (section 3b).
- **The judge's verdict** shows on the card with its band and reason, and there is a
  chip row to filter by it. A verdict with a fix offers **Use as ask**, which copies the
  fix into the ask box.
- **The diffs** are computed on the current text and say when an edit no longer
  lands.
- **Filters:** chips by decision, recommendation and kind. Applied and declined
  cards keep their history.
- **Approval** is the page's single button.

Two ways to get wording other than the proposed diff:

- **Edit in the book.** The card's link opens the reader at the sentence, where
  the sheet's Edit tab (or Edit chunk) changes it; the browser's Back returns to
  the card. The user then ticks **Fixed by hand**: nothing is
  written, and the card counts as NBLA in the record. This also works on an
  applied card, and is the only way to redo one.
- **Ask.** The user types what they want in the card's ask box, which holds the
  card at Not yet. Then:

  ```bash
  python -X utf8 scripts/quotation_pass.py asks --project <slug>
  ```

  For each ask, write a mapping in the wave's format to a scratch file:
  `{"replace": [{"es_idx", "old", "pieces"}], "echo": [...], "drift": "", "note": ""}`.
  `pieces` are verbatim NBLA, and code builds the Spanish. Add `"new"` to a
  `replace` item only when the user wants words that are not the NBLA's. Every
  piece must still be verbatim NBLA and appear in `new`, and the card is then
  marked adapted. A `new` that differs from `old` only by quotation marks may
  have empty `pieces`, for a paraphrase taken out of the marks; it counts no verses
  and no NBLA words. Then run:

  ```bash
  python -X utf8 scripts/quotation_pass.py revise --project <slug> --card "<key>" --file <mapping.json>
  ```

  A refused revision writes nothing; read `refusals` and try again. On success
  the card is back on the board at Not yet, with the new diff. Tell the user to
  review it. Revisions live in `revisions.json` and survive a `map-commit`.

**Another verse.** When the user says a card names the wrong verse (the author
quotes the KJV of one verse, and the NBLA says it better in another), the
reference is part of the card's key, so it takes a new card. This works on an
applied card too:

```bash
python -X utf8 scripts/quotation_pass.py reref --project <slug> --card "<key>" --ref "Jeremiah 12:3" --why "<the user's words>"
```

- The new card sits on the same sentence, with `replaces` set to the old key.
  `in_book` is the text its mapping must replace. Write that mapping and run
  `revise` on the **new** key, exactly as for an ask.
- An unapplied old card is set to Reject. An applied one stays as it is until
  the new card is applied. `apply` then marks the old card `superseded` in the
  same write: the record drops its verse, `verify` stops checking it, and the
  board shows "Replaced by …".
- Asking again before the new card is applied swaps it for the newer verse.
- A reference the author wrote in the text is not changed; fix it by hand.

The requests live in `rerefs.json`, which `resolve` reads on every run.

## 5. Apply, verify, record

When the user says the board is approved:

```bash
python -X utf8 scripts/quotation_pass.py apply --project <slug>
python -X utf8 scripts/quotation_pass.py verify --project <slug>
```

`apply` refuses a board that changed since approval (cards recomputed, or a chapter
edited). It is resumable: re-run it after a kill.

- **It applies only cards set to Apply.**
- **Rejected cards** are recorded as declined, once.
- **Not-yet cards** are left for a later round. The board can be approved and
  applied again as the user works through them.
- **A card fixed by hand** gets an applied ledger row with no edits.

It ends by writing
`projects/<slug>/quotations.json`: verses (unique and with repeats), quoted words,
share of the book, the NBLA's limits and `within_limits`, plus one accounting row per
accepted card. Every card with NBLA wording counts, edited or not, adapted ones
included and labelled. Relay those numbers; kdp-publish reads the file.

`verify` checks that every applied quotation is still in its chunk. Run it again
before publishing if the book was edited after the pass.

## Handoff

Relay: cards by status, edits applied, chapters realigned, the record's
`verses_unique` / `verse_occurrences` / `share` / `within_limits`, and anything
`skipped`. Then route to kdp-publish when the user is ready to publish.
