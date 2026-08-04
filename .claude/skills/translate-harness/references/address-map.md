# Address map beat

Load this at **Step 0B** — after `setup`, before the style guide — or any time the user asks
for the forms-of-address map later (e.g. judge-review's precheck says the `address` judge has
no map).

## Step 0B — ADDRESS MAP beat (OPTIONAL — never blocks translation)

A per-book **forms-of-address map** (`projects/<slug>/address_map.json`) records which pairs of
characters use **usted** vs. **tú**, including public/private and story-stage differences.

It has **two** consumers, and they are different audiences:
- `content` — the full prose the later `address` compliance judge reads (judge-review).
- `style_guide_summary` — a 60–90 word condensation that the **style-guide beat injects as its
  FORMS OF ADDRESS section**. That is why this beat runs first: it is the one thing here that
  affects the current translation run.

**This beat is optional and non-blocking at every step.** Record the decision either way; the
router uses `address_map_decision` to stop re-offering it on each resume.

---

**0B-a. Gate on dialogue — do this before you offer anything.**
```bash
python scripts/harness.py address-map precheck --project projects/<slug>
```
An address map describes how characters address *each other*. A book with no interpersonal
dialogue has nothing for it to say, and the judge would have nothing to check.

- `dialogue_present: false` → the command has already recorded `address_map_decision:
  "no_dialogue"`. **Do not offer the beat.** Mention in one line that the book has no dialogue
  so the map is being skipped, then go to `references/style-guide.md`, where `forms_of_address`
  is simply one of the standard questions.
- `dialogue_present: true` → continue to 0B-b. `qualifying_chapters` / `top_chapters` are
  useful colour when you offer it ("32 of 32 chapters carry real dialogue").

**0B-b. Offer it (a plain question, NOT a gate).** Ask whether the user wants to build the map
now. Say what it buys: it powers the usted/tú judge later, and its summary becomes the style
guide's forms-of-address section. Say what skipping costs: nothing for this run — the map can
be built later, here or from judge-review's precheck.

If they decline:
```bash
python scripts/harness.py address-map skip --project projects/<slug>
```
Then go to `references/style-guide.md` and ask `forms_of_address` as one of the standard questions.

**0B-c. STOP — ask the ONE register question and WAIT.** Only if they opted in. The map should
be drafted against the register the user actually wants, so pull that single question forward:
```bash
python scripts/harness.py style-guide prepare-questions --project projects/<slug>
```
From the printed `questions`, present **only `forms_of_address`** with its options and hint.
**END your turn** and wait — do not answer it yourself, and do not ask the other questions yet
(they belong to G1 in `references/style-guide.md`). Once answered, **Write** just that one entry
to the printed `answers_path`: `{"forms_of_address": "<option id>"}`. Note the informal-tú option
id is `t_dominates_informal` (the `ú` is dropped by the slug rule).

> Writing only this key now is correct — the style-guide beat rewrites `answers_path` with the
> full answer set at G1, and will present this question as already-answered for confirmation.

**0B-d. Draft the map.**
```bash
python scripts/harness.py address-map prepare --project projects/<slug>
```
This samples the book's highest interpersonal-dialogue chapters (a spread across the whole book,
not just the openers) and renders a prompt at `prompt_path`. Check `forms_of_address_loaded` is
`true` — if it is `false` the answer never landed; fix `answers_path` and re-run.

Read the prompt and draft the map JSON to the printed `draft_path`:
`{content, style_guide_summary, pairs, global_rules}`. Three things the prompt spells out that
are easy to get wrong:
- Each non-empty direction must **end** with a `when:"default"` rule; specific `when` rules go
  before it.
- **Names:** `characters_loaded` will be `0` here (the glossary does not exist yet). Use the
  **English source names verbatim**. Do not guess target-language forms — the glossary beat
  fixes those later and will flag the drift.
- **`style_guide_summary` has a chunk-local reader.** General rules first, then only the
  high-frequency *exceptions*. No full pair list, no chapter numbers, no "later in the book",
  no reference to the map itself. See 0B-f.

Refine with the user, then:
```bash
python scripts/harness.py address-map commit --project projects/<slug>
```
This validates against the `AddressMap` model and records `address_map_decision: "built"`. If it
returns a `warnings` entry about a missing `style_guide_summary`, add it and re-commit — without
it the style guide falls back to the single questionnaire answer and this beat informed nothing.

**0B-e. STOP — approval beat.** Present, in this order:
- the committed **pairs** and **global rules**;
- the **`style_guide_summary`** verbatim, flagged as *"this text goes into the style guide and is
  what every translator worker will actually see"* — it is the part with production impact, so
  it deserves its own look rather than being buried in the pair list;
- **AskUserQuestion with exactly two predefined options** — **"Approve all"** and **"Reject &
  talk it through"**. **Remind the user in the question text that to approve *with specific
  changes* they should pick _Other_ and type the edits directly.** A custom (_Other_) answer is
  approve-with-changes: apply the edits to `draft_path`, re-run `commit`, confirm what changed,
  and continue.
- **Log the outcome:** `log-event --event approval --data '{"beat":"address_map",
  "decision":"approved_first_pass"|"reject_then_redraft"|"approve_with_changes","redrafts":<n>}'`.

Then continue to `references/style-guide.md`.

**0B-f. Sanity-check the summary before you present it.** Reject your own draft and rewrite if it:
- names more than two or three pairs (the general rules are not general enough);
- says "chapter", "later", "by the end", "once they marry", "as the story progresses";
- mentions the address map, the glossary, or any other book;
- describes the book instead of instructing the translator.

The reader of that text sees **one excerpt** and does not know where in the book it falls — the
rendered translation prompt carries no chapter identity at all. A rule they cannot apply from
the excerpt in front of them is worse than no rule.

---

## Reconciling the cast after the glossary (Step 2 hand-off)

The map is drafted here with English names; the glossary fixes the target-language forms three
beats later. When `glossary commit` prints a `REVIEW:` line saying the map still uses English
cast names, come back and run:

```bash
python scripts/harness.py address-map rename --project projects/<slug>
```

This applies every approved `character` form across all nine text fields of the map — the judge
prose, the global rules, the style-guide summary, the pair names, and the rule
`when`/`after_event`/`notes` — in one deterministic pass. It writes a **draft** and leaves
`address_map.json` untouched, so read `draft_path` before committing. What to check:

- **`renamed`** — one entry per term actually substituted, with a count and the fields it
  touched. The **first** `remaining_warnings` line (stale English cast names) must be gone; that
  is the confirmation the substitution pass is complete, so you never need to re-run
  `glossary commit` just to see the warning clear.
- **`flags`** — the sites needing a human, each with a `context` snippet. Only one of the three
  kinds was rewritten:
  - `possessive` — **substituted**, but an English `'s` now trails a target-language name
    (`la señorita Polly's coldness`). Reword the phrase.
  - `compound` — **left in English**: the match is edged by a hyphen or apostrophe, so it is
    usually a **quoted vocative** (`'I'm sorry, Uncle Dock'`) wanting the bare vocative from the
    glossary's `alternatives` rather than the article-led narration form, part of a longer name
    (`Great-aunt Harriet`), or a possessive adjective (`a 1920s boys' adventure`) that should not
    be translated at all. Type the right form in yourself.
  - `shadowed` — **left in English**: the name sits inside *another* term's approved form, which
    means the glossary contradicts itself (`Aunt Harriet` → `la tía Harriet` beside `Harriet` →
    `Enriqueta`). Fix the glossary, or pick a form in the map by hand.
- **A second `remaining_warnings` line**, when those flagged sites exist. It names them and says
  plainly that re-running the rename will not clear them — that is expected, not a failure. Edit
  `draft_path` directly (nothing is committed yet), or accept them as written.

Then re-commit — it re-checks the cast, and the stale-names warning is gone when the reconcile is
done (a hand-edit line may remain if you accepted a flagged site as written):

```bash
python scripts/harness.py address-map commit --project projects/<slug>
```

## Building the map later (out of band)

Same commands, one difference: if `glossary.json` already exists, `prepare` reports
`characters_loaded > 0` and the prompt carries the approved cast — use those target-language
names, not the English ones, and no rename is needed afterwards.
