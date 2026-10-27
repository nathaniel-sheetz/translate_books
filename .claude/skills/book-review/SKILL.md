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

- **Nothing changes without the user's pick.** The detectors, the report and the
  review page are read-only; only `apply … --yes` writes, and from the board only
  what the user approved.
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
| `judge --estimate` | none (keeps drafts whose prompt is unchanged, clears the rest) | prepares the sweep and projects its tokens and wall time at the chosen width; spawns nothing |
| `judge --yes [--concurrency N] [--job-timeout S]` | subscription | dialogue, address (when the book has a map) and editorial over owed chapters, then editorial pass 2, 10 at a time by default; a re-run after a cut-off resumes from the drafts already written |
| `signals` | none | coded detectors over every finding; writes `.harness/book_review/signals.json` and `reports/book_review_signals_<date>.md` |
| `prepare` | none | detectors, budgeted evidence, and one synthesis job per theme under `.harness/book_review/synthesis/` |
| `fanout` | subscription | runs the synthesis jobs as one headless wave (resumes: skips jobs with a draft) |
| `commit` | none | validates every proposed patch; writes `.harness/book_review/recommendations.json` and `reports/book_review_<date>.md` |
| `run --yes` | subscription | `prepare` → `fanout` → `commit` in one call, after the user approved `prepare`'s gate |
| `board` | none | the review page's `url`, whether the web UI answers, and what the user picked, noted and approved there |
| `revise --id R-xxxxxx --from revised.json` | none | rewrites one recommendation's changes as the user's note asked; validated like `commit`, shown on the page beside the original for the user to pick and approve |
| `apply --from-board` | none | dry run of the picks on the page |
| `apply --from-board --yes` | none | writes the picks the user **approved on the page**; refuses anything else |
| `apply --select R-xxxxxx[,R-yyyyyy.2]` | none | dry run: the diff of every artifact the selection edits (for one or two items picked in the chat) |
| `apply --select … --yes` | none | writes the changes through the normal save paths (history-logged) |
| `skip` | none | marks the current translations reviewed so the harness router moves on |

## Flow

1. **`status`.** Lead with what has not been judged (`coverage.judges.*.owed`,
   `unverified_editorial_chunks`). A book with nothing owed goes straight to step 4.

2. **`judge --estimate`.** Relay one consent block: the judges and owed chapters
   (`plan`), pass 1 jobs and tokens (`pass1`), the pass 2 ceiling
   (`adjudication`) and `effective` (CLI, model, effort). Give the width and the
   length on a line of their own, so the user sees both before anything starts:
   **"`concurrency` at a time (`concurrency_source`) · projected ~Xh Ym"**, from
   `wall.total_s`, with pass 1 and the pass 2 ceiling beside it. If `wall` is null,
   say the length is unknown. If `pass1.resumed_drafts` is above 0, say that many
   jobs are already drafted and will not re-run.
   - **`wall.fits_background_cap` false:** the sweep will not finish inside one
     background command (`background_cap_s`, 2 h). Say so at the gate and offer
     one of two options. The first is a split: one `judge --yes --judge <name>`
     call per judge, sized from `wall.per_judge_s`, with editorial last. The second
     is a wider `--concurrency` (re-run `--estimate` with it to re-quote).
   - If `--estimate` returns an error (another wave holds the lock, the CLI is not
     logged in), relay it and stop.

   **END your turn.**

3. **On approval, `judge --yes`** with the same flags, including `--concurrency` and
   `--job-timeout` if the user changed them. Send stderr to a file, never `$null`. Progress goes there,
   and the first line names the job count and width. Report `summary` (ran, resumed,
   failed, missing, adjudication, errors). A `fatal` means nothing was committed:
   relay it and stop.
   - **If the sweep is cut off** (background limit, a kill, a crash), run the same
     `judge --estimate` again, then the same `judge --yes`. Drafts whose prompt
     has not changed are kept, so only the missing jobs run (`resumed` says how
     many were kept). Do not reach for `run_judges.py` / `verify_editorial.py`.

4. **`signals`.** Relay `headline` verbatim, worst first, and give the
   `report_path`. Read the report before summarizing it; it carries the findings
   behind each signal. Group what you tell the user by what they would change:
   - **glossary** (`dead_glossary_keys`): `reasons` says why the key missed
     (`punctuation`, `parenthetical`, `slash`, `plural_ies`, `head_noun`), or that
     it is `not_in_logged_prompt` (added after translation).
     `blind_spot_findings` is how many judge findings blamed the translator for it.
   - **wrong sense** (`glossary_sense`): judges replacing an entry's Spanish where
     the English uses the word another way. The concordance shows the senses.
   - **forms of address** (`address_style_conflicts`): which artifact says what,
     and which judge pulls which way.
   - **drift** (`rule_drift`): findings judged against rules the chunk's prompt
     never carried (older or missing dialogue rules, a style guide or address map
     written after translation, a judge prompt that changed mid-book). These are
     usually not the translator's fault.
   - **undoing** (`opposite_directions`): one replacement pushed both ways across
     chapters.
   - **rules** (`recurring_rules`): `resisted` means the translator was told and
     kept missing it, so a coded check or a stronger instruction is the fix.
     `disputed` means the user keeps dismissing it, so the rule or the judge is
     wrong. `unreviewed` means nobody has marked the findings yet.
   - **dismissals** (`false_positive_clusters`): `glossary_stale` or
     `judge_miscalibrated`.

5. **Synthesis.** Run `prepare` (free). Relay one consent block: `jobs`,
   `by_theme`, `input_tokens`, `effective`, and `omitted` (signals shown in less
   detail to fit the budget). **END your turn.** On approval, run `fanout`, then
   `commit`. Re-run `fanout` for any `failed` job before committing. If the user
   only wants the coded report, stop after step 4.

6. **Hand over the review page.** The user picks on a page, not by typing ids.
   Run `board` and give them its `url`, with two or three lines on what stands
   out (the priority-1 items, any `conflicts_with` pair, a retranslate the review
   wants). **END your turn.** If `server_running` is false, ask them to start the
   web UI (`python web_ui/app.py`) instead of handing over a dead link.

   The page (`/book-review/<slug>`, also in the reader's card menu) shows every
   recommendation by priority with its rationale, each change in full (the passage
   a style patch rewrites, the glossary entry a patch edits, the findings a
   dismissal marks) and the evidence behind it, English included. Ticking one, or
   single changes inside one, saves at once and redraws the dry-run diffs at the
   bottom; a conflicting pair unpicks its other half. A note box on each card
   carries anything the picks cannot ("yes, but keep the old key"); ticking "Ask
   the agent to rewrite this from my note" under it turns the note into a
   revision request and blocks the pick until the revision is there. The user
   approves the diffs there.

   Only with **one or two** recommendations, or when the user asks for the chat,
   present them inline instead and use `apply --select`.

7. **When they come back, `board` again.** Handle `revise` first, then answer
   or act on each of `notes`: a note can change what they want applied.
   - `revise` (`{id: note}`): for each, write the changes the note asks for as a
     JSON file in the scratchpad, `{"patches": [...], "why": "one line on what
     changed", "follow_ups": [...]}` (patches in the recommendation's own shape;
     leave out `follow_ups` to keep its own, `[]` to drop them; `rationale` is
     optional), and run `revise --id … --from <file>`. A refusal lists the
     `problems`: fix the file and run it again. Read the note against the
     evidence: drop a part the note makes wrong (a house rule it contradicts, a
     retranslate it makes moot) and say so in `why`. When every request is
     revised, tell the user in a line each what changed and send them back to
     the `url` to pick and approve. **END your turn**; nothing is applied yet.
     Never apply a note's wording yourself outside the page.
   - `approved: true`: run `apply --from-board --yes`. Their approval on the page
     is the gate; it covers exactly the diffs they saw.
   - not approved: `not_approved_because` says why (nothing picked, picks changed
     after approval, an artifact or the review changed). Relay it and give the
     `url` again.

   Relay `actions`:
   - `saved` rows: edits to the glossary, style guide or address map. They reach
     chunks translated from now on.
   - `done` rows: dismissals, re-judge flags, house proposals.
   - `offer` rows: `retranslate` carries a preview command. Run it only through
     `.claude/skills/translate-harness/references/retranslate.md`, which has its own gate. `manual` needs a person
     or a code change.

   The page shows applied recommendations as applied. Another one goes stale
   only when something it edits changed: the passage it rewrites is gone, or the
   glossary entry or address pair it edits by name is no longer as it was at the
   review. The card says which change and why; re-run from step 5 to pick it, or
   `revise` it if the user asks. (Reviews committed before these per-change
   checks go stale on any edit to a file they touch.)

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
