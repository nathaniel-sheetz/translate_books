---
name: book-review
description: |
  Read a book's judge findings together and name what is systemic: glossary
  entries that never reached the translator, a style guide that contradicts the
  address map, rules the translator keeps missing, judges whose findings keep
  being dismissed. Runs every judge over the chapters that owe it (one headless
  wave plus editorial pass 2, subscription only), then coded detectors over every
  finding, and reports each pattern with the evidence behind it. Nothing changes
  in the book unless the user picks a fix. The translate harness routes here by
  default after the first translate wave and again when a finished book has new
  translations.
  Use when asked to "review the book's findings", "run the book review", "why do
  the judges keep flagging", "what's systemic in this book", "check for glossary or
  address-map conflicts", or "book-review".
allowed-tools:
  - Bash
  - Read
  - AskUserQuestion
---

# book-review

One judge finding says one sentence is wrong. Read across a whole book, the
findings say more. The 2026-10-20 cross-book review found most recurring findings
were upstream problems, not translator slips:

- glossary keys that never match the source (`porte cochere` against
  `porte-cochere`, `stalk (mushroom)`, a curly `’Lias`), so the entry never
  reached the translator while the editorial judge, which reads the whole
  glossary, kept flagging the miss;
- a style guide and an address map that disagree, with the address judge and
  the editorial judge pushing the same speaker in opposite directions;
- rules written after the chapters they are judged against;
- rules the translator misses in every chapter, and judges whose findings are
  dismissed every time.

Design and detector details: `docs/BOOK_REVIEW.md`.

**Rules this stage never breaks:**

- **Nothing changes without the user's pick.** The detectors and the report are
  read-only.
- **Subscription only.** The judge sweep is a headless wave; it refuses on a
  metered login and has no override.
- **It never deletes a finding.**

## The CLI

`python -X utf8 scripts/book_review.py <verb> --project <slug>` prints one JSON
object with a `_schema` block.

| verb | spend | what it does |
|---|---|---|
| `status` | none, writes nothing | findings per judge, owed chapters per judge, last report |
| `judge` | none | the plan only: owed chapters per judge, chunks awaiting pass 2 |
| `judge --estimate` | none (clears judge drafts) | prepares the sweep and projects its tokens; spawns nothing |
| `judge --yes` | subscription | dialogue, address (when the book has a map) and editorial over owed chapters, then editorial pass 2 |
| `signals` | none | coded detectors over every finding; writes `.harness/book_review/signals.json` and `reports/book_review_signals_<date>.md` |
| `skip` | none | marks the current translations reviewed so the harness router moves on |

## Flow

1. **`status`.** Lead with what has not been judged (`coverage.judges.*.owed`,
   `unverified_editorial_chunks`). A book with nothing owed goes straight to step 4.

2. **`judge --estimate`.** Relay one consent block: the judges and owed chapters
   (`plan`), pass 1 jobs and tokens (`pass1`), the pass 2 ceiling
   (`adjudication`), and `effective` (CLI, model, effort). If it returns an error
   (another wave holds the lock, the CLI is not logged in), relay it and stop.
   **END your turn.**

3. **On approval, `judge --yes`** with the same flags. Progress goes to stderr. Report
   `summary` (ran, failed, missing, adjudication, errors). A `fatal` means nothing
   was committed: relay it and stop.

4. **`signals`.** Relay `headline` verbatim, worst first, and give the
   `report_path`. Read the report before summarizing it; it carries the findings
   behind each signal. Group what you tell the user by what they would change:
   - **glossary** (`dead_glossary_keys`): `reasons` says why the key missed
     (`punctuation`, `parenthetical`, `slash`, `plural_ies`, `head_noun`), or that
     it is `not_in_logged_prompt` (added after translation).
     `blind_spot_findings` is how many judge findings blamed the translator for it.
   - **forms of address** (`address_style_conflicts`): which artifact says what,
     and which judge pulls which way.
   - **rules** (`recurring_rules`): `resisted` means the translator was told and
     kept missing it, so a coded check or a stronger instruction is the fix.
     `disputed` means the user keeps dismissing it, so the rule or the judge is
     wrong. `unreviewed` means nobody has marked the findings yet.
   - **dismissals** (`false_positive_clusters`): `glossary_stale` or
     `judge_miscalibrated`.

5. **Propose fixes; change nothing yet.** For each pattern, say which artifact
   to change and how. Examples: rekey `porte cochere` to `porte-cochere`; make the
   style guide's FORMS OF ADDRESS agree with the map; add a house rule. Make the
   edits the user approves through the normal paths: the glossary and style-guide
   beats of translate-harness, or the address-map reference. An artifact change
   reaches only chunks translated afterwards; offer `references/retranslate.md`
   for chapters that must pick it up.

## Notes

- A finding's mark comes from `_feedback.jsonl` by `issue_key`. Dialogue and
  address keys hash the judge's message, which a re-judge rewords, so older marks
  on those may not join. The report then counts the finding as unmarked; it does
  not guess.
- "What the translator got" is read from each chunk's logged prompt
  (`last_llm_log` → `prompts/history/*.json`). A chunk with no log falls back to
  recomputing the glossary filter.
- Dialogue-judge clusters on books translated before a dialogue-rule change
  (2026-07-22 » continuation; 2026-09-25 inciso punctuation) usually reflect the
  rule changing, not the translator.
