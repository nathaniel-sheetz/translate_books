# Book review

A judge finding says one sentence is wrong. Read across a whole book, the findings
say more. On 2026-10-20 a manual review of 15 books' editorial, address and dialogue
findings found that most recurring clusters had upstream causes:

| Cause | Example |
|---|---|
| Glossary key never matches the source | pollyanna `porte cochere` vs `porte-cochere`; fabre2 `stalk (mushroom)`; understood-betsy `'Lias` vs `’Lias`; midnight `prairie dog` where the source says "dogs" |
| Glossary entry applied in the wrong sense | fabre2 `scales → balanza` gave «Dos pobres balanzas» for a flower's scales |
| Style guide and address map disagree | pollyanna: the guide gives Aunt Polly usted with her servants, the map gives tú; the address and editorial judges push opposite ways |
| Address map contradicts itself | pollyanna: the pair entry says John Pendleton uses tú with Pollyanna, the global rules say "ceremoniously uses usted" |
| Rule written after translation | the dialogue rules changed 2026-07-22 and 2026-09-25; glossary entries added after the chapters were translated |
| Rule the translator keeps missing | pollyanna glad-game lock (19 findings in 11 chapters, all applied) |
| Judge or glossary the user keeps dismissing | short-sermons: 15 Bible-abbreviation findings marked false positive |

The book review reads every judge finding a book has, runs coded detectors over the
whole set, and reports these patterns with the evidence behind each.

## Pieces

| Module | Role |
|---|---|
| `src/book_review/corpus.py` | Loads chunks, judge findings (kept, retracted by pass 2, filtered by pass 1), human marks, glossary, style guide, address map and artifact history, once and read-only |
| `src/book_review/signals.py` | The detectors |
| `src/book_review/report.py` | The markdown report |
| `src/book_review/judging.py` | The judge sweep: every judge over owed chapters, then editorial pass 2 |
| `src/book_review/progress.py` | When the harness router sends a book here |
| `src/judges/sweep.py` | The headless judge wave, shared with the dashboard's "Run judges" job |
| `scripts/book_review.py` | The CLI |
| `.claude/skills/book-review/SKILL.md` | The operator flow |

## Joins

- **Finding to mark.** `_feedback.jsonl` records are matched by
  `web_ui.evaluations.issue_key`. Editorial findings carry a stable `finding_key`.
  Dialogue and address keys hash the judge's message, which a re-judge rewords, so
  an older mark on those may not join; that finding is then counted as unmarked.
- **Finding to what the translator got.** Each chunk's `last_llm_log` names the
  logged prompt in `prompts/history/`. Its GLOSSARY TERMS block is the authoritative
  record of which entries reached that chunk. A chunk without a readable log falls
  back to recomputing `filter_glossary_for_chunk`.
- **Finding to theme.** Editorial rule ids are invented per run, so `theme_of` folds
  them into a fixed vocabulary (address, glossary, caps_italics, dialogue_punct,
  units, interjection, names, register, agreement, calque, otherwise the category).
  Only the rule id is read; the message is used when there is no rule at all.

## Detectors

**`dead_glossary_keys`**
- For each glossary entry, it counts the chunks the entry reached and the chunks
  whose source contains a near form the key missed.
- It names why the key missed: `punctuation` (hyphens, spaces, curly apostrophes),
  `parenthetical`, `slash`, `plural_ies`, `head_noun`, or `not_in_logged_prompt`
  (the key matches the text, but the prompt the chunk was translated with lacked it,
  so the entry came later).
- A head-noun hit counts only where a finding about the entry fell in that chunk.
- **Blind-spot findings** are judge findings about the entry in a chunk it missed,
  where no sibling entry with the same Spanish reached it either. A finding belongs
  to the entry whose Spanish it quotes longest.
- Entries that match nothing at all are one low-priority signal.

**`address_style_conflicts`**
- A clause in the style guide or the map's `global_rules` that names both parties of
  a pair and states one form the map does not give that direction. It is treated as
  mutual when worded so ("with", "between", "each other"); otherwise the first-named
  party is the speaker. "Paul and Maureen use usted" is a coordinated subject, not a
  pair.
- The address judge and the editorial judge pushing the same pair toward opposite
  forms. The editorial push is read from the pronouns its suggestion adds and
  removes (and the -s of the second-person verb).
- The address judge demanding a form its own map's unconditional pair entry does not
  give, usually a global rule overriding the pair. Time-scoped entries are not checked.

**`recurring_rules`**
- A rule (or a non-generic theme) in at least 3 chapters or 5 findings.
- Labels:
  - `resisted`: a style or glossary rule accepted at least 60% of the time, so the
    translator was told and kept missing it.
  - `disputed`: dismissed at least half the time.
  - `unreviewed`: fewer than 3 marks.
  - `recurring`: anything else.

**`false_positive_clusters`**
- At least 3 dismissed findings on one judge and theme.
- Labels: `glossary_stale`, `missing_context`, or `judge_miscalibrated`.

`coverage` reports the chapters each judge still owes and the editorial chunks
awaiting pass 2, so the report says what it could not see.

## Harness integration

`harness.py status` carries a `book_review` block. `suggested_reference` is
`references/book-review.md`:
- once in a partial book, after its first translate wave (`book_review_canary`);
- in a finished book that was never reviewed, or that has chunks translated after the
  last review (`book_review_after_translate`).

Both keys are `on` unless set `off` with `config-set`. `book_review.py signals`
records the review in `.harness/book_review/state.json` (the newest `translated_at`
it covered); `book_review.py skip` records a deliberate skip the same way.

The judge sweep (`book_review.py judge`) prepares one manifest with per-judge scopes
(`{judge: [chapter:<id>, ...]}`), fans it out headless, commits, and then runs
editorial pass 2 over every chunk still awaiting it. It refuses while another wave
holds the book's lock, or when the CLI preflight fails.
