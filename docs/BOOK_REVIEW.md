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
| `src/book_review/report.py` | The markdown reports (coded signals; recommendations) |
| `src/book_review/evidence.py` | Tiered, budgeted evidence for the synthesis model |
| `src/book_review/synthesize.py` | The synthesis wave: prepare, fanout, commit |
| `src/book_review/schema.py` | Validation of proposed patches against the book as it is |
| `src/book_review/apply.py` | Applying picked recommendations through the normal save paths |
| `src/book_review/board.py` | The review page's state, the user's picks, notes, revision requests and approval (`board.json`) |
| `src/book_review/revise.py` | Rewrites one recommendation's changes from the user's note, keeping the original |
| `web_ui/templates/book_review_board.html`, `web_ui/static/book_review_board.*` | The review page, `/book-review/<slug>` |
| `prompts/book_review_synthesize.txt` | The synthesis prompt |
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

**`glossary_sense`**
- A finding in a chunk the entry reached, whose suggestion removes the entry's
  Spanish. That is a sign the key is a homonym applied in the wrong sense (fabre2
  `scales → balanza`, `locust → langosta` for the locust tree).
- It carries the entry's `context`, which the translator never gets, and an
  English concordance (±60 characters, up to 12 uses), so the reader can see the
  senses.

**`rule_drift`**
- Each chunk's logged prompt is fingerprinted on its DIALOGUE FORMATTING block,
  folding whitespace, and compared with the rules in `prompts/dialogue.txt` today.
  Dialogue findings on chunks translated under older rules, or with no dialogue
  block at all, are judged against rules the translator never had. On 2026-10-20
  no reviewed book had been translated with the current rules, and
  among-the-farmyard-people had none.
- A judge's `prompt_version` that changes partway through a book.
- The style guide (`updated_at`) or the address map (`created_at`) postdating
  translated chunks, counted against the findings on those chunks.

**`opposite_directions`**
- A one- or two-word replacement that appears in some chapters and is reversed
  in others (pollyanna «sabe» → «sabes» 5×, and back 14×).

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

## Synthesis

`prepare` builds the evidence (`.harness/book_review/evidence.json`) and renders one
job per theme: `glossary`, `address_style`, `rules`, `dismissals`. A theme over the
job cap is split. The preamble is shared and cacheable: the instructions, the full
style guide, and the address map's content, global rules and pairs.

Each signal gets one of three tiers:
- **A (full):** every finding, up to 12. Each carries its message, the Spanish
  excerpt and suggestion, the English paragraph it translates (found by paragraph
  position, since the translation prompt keeps paragraph breaks), the mark, and
  when it was judged and translated. The item also carries the artifacts the
  signal names: the full glossary entry including its unshipped `context`, the
  style-guide lines around the clause, the address pair, and the relevant
  history rows.
- **B (compact):** three full exemplars, the rest as id and message.
- **C (count):** counts and two one-line samples.

Budget: about 40k input tokens per job, 200k per book. Past either, the
lowest-scoring signals drop a tier. Each demotion is recorded in `omitted` and
shown in the report.

The model returns `recommendations[]` with typed patches (see the prompt for the
table) and `no_action[]`. `commit` validates each patch (`schema.check_patch`):
- a glossary key must exist, and any key it introduces must occur in the source,
  so a fix cannot add another dead key;
- a `before` text must be in the artifact verbatim, exactly once;
- an address pair must exist (a swapped `a`/`b` is normalized);
- dismissals and follow-ups must cite findings and chapters that exist;
- unknown evidence ids are dropped.

A recommendation with no valid patch is rejected and listed in the report.
Recommendations that touch the same target are cross-marked `conflicts_with`.
Each patch that edits a glossary entry or an address pair by name is stamped
with that target's state (`targets`, one per patch; `patches.stamp`). A patch
that quotes the text it replaces needs no stamp: replaying it fails once the
text is gone.

## The review page

The user picks recommendations on `/book-review/<slug>` in the web UI rather
than by typing ids. The page draws `board.build`: every recommendation by
priority, each change in full (the passage a style patch rewrites, the glossary
entry a patch edits, the findings a dismissal marks), the evidence behind it with
the English, and which ones are already applied or went stale. Ticking a
recommendation, or single changes inside one, saves the selection to
`.harness/book_review/board.json` and returns the dry run of exactly that
selection, which the page shows as diffs with the changed words marked. Each card
has a note box for what a pick cannot say.

A note can also be a revision request ("Ask the agent to rewrite this from my
note"). The recommendation then cannot be picked until the agent has written
the changes the note asks for and stored them with `revise`. That runs the same
validation as `commit`, stamps the new patches against the book as it is now,
keeps the first version under `revision.original`, and clears the request and
any pick of the old version. The card shows the revision, the note and what
changed, with the original changes folded below; the user picks and approves it
like any other.

A recommendation is stale when one of its patches no longer fits the book
(`apply.stale_reasons`): its replaced text is gone, or a stamped target
changed. An edit elsewhere in the same file leaves it pickable. Recommendations
committed before the per-patch stamps carry whole-file hashes (`precondition`)
and go stale on any edit to a file they touch.

Approval is per selection. `approve` stores the selection with a fingerprint of
`recommendations.json` and the three artifacts' hashes; saving different picks,
re-running the review, revising a recommendation or editing an artifact voids
it. `apply --from-board --yes` applies
only a selection whose approval still holds, then clears the picks. `board`
reports the url, the picks, the notes and why nothing is approved yet. Saving
refuses an unknown id, a patch number out of range, an applied or stale
recommendation, and both halves of a `conflicts_with` pair.

## Apply

`apply --select` is a dry run that returns unified diffs. It first checks every
selected recommendation against the artifacts now (`stale_reasons`). If any is
stale, nothing is written. Otherwise it saves through `save_glossary`,
`save_style_guide` and `save_address_map` (`writer="book_review"`, reason = the
recommendation ids), so each change is in `decisions_history.jsonl`. Other
patches:

| Patch | What `--yes` does |
|---|---|
| `judge.dismiss` | appends keyed `_feedback.jsonl` marks |
| `rejudge` | flags the chapters' evaluations stale |
| `house.rule_proposal` | records a house proposal (adoption stays separate) |
| `retranslate` | returns a preview command and runs nothing |
| `manual` | returned to the operator, nothing run |

Every apply is appended to `.harness/book_review/applied.jsonl`.
