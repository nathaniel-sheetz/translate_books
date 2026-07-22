# Glossary + optional address map

Load this when `glossary.json` is missing or the user wants to revise the glossary / address map.

## Step 2 — GLOSSARY beat (agent drafts, approval gate)

```bash
python scripts/harness.py glossary prepare --project projects/<slug>
```
This extracts candidates, feeds in the approved style guide, and prints `candidate_count`,
`style_guide_loaded` (must be `true` — if not, the style guide isn't saved yet; go back to `references/style-guide.md`),
a `prompt_path`, and a `draft_path`.

**Read the printed `prompt_path`** and draft the glossary proposals yourself — the thinking-mode
step. Produce a JSON array of `{english, translation, type, context}` objects and **Write** it to
the printed `draft_path`. **Write the target language WITH its diacritics** (Spanish: á é í ó ú ñ
¿ ¡). The `Write` tool is UTF-8 — do **not** ASCII-fold "to be safe"; stripped accents (`Tia` for
`Tía`, `senor` for `señor`) become the canonical forms fed verbatim to every translator worker.
As you draft, **track every term whose translation you were unsure about**
(ambiguous sense, multiple valid renderings, dialect/register judgement calls) and why — keep that
running list for the approval beat. Then:
```bash
python scripts/harness.py glossary commit --project projects/<slug>
```
This guards the proposals, builds + saves `glossary.json`, and validates it; it prints the full
`terms` list. If it prints a VALIDATION ERROR, fix the entries it names and re-run `commit` (cap ~3).

**STOP — approval beat.** Do all three, in this order:
- **Show the full list of glossary terms** (use the `terms` the command printed) — render every
  `english → translation` pair with type/context so the user can scan the actual decisions. Do not
  collapse it to "N terms drafted."
- **Call out the uncertain translations** you tracked: name each term, its chosen rendering, the
  alternative(s) you considered, and why you hesitated, so the user can adjudicate the close calls.
- **Surface any `warnings`** — if `commit`'s result carries a non-empty `warnings` array (e.g. an
  accent-stripping smell when the target language carries diacritics), re-read `glossary.json`, fix
  any ASCII-folded terms, re-run `commit`, **then** present the gate.
- **AskUserQuestion with exactly two predefined options** — **"Approve all"** (accept the list as-is
  and continue) and **"Reject & talk it through"** (open-ended: END the turn, discuss, then re-draft /
  re-run `commit` and re-present this gate). **In the question text, remind the user that to approve
  *with specific changes* they should pick _Other_ and type the swaps directly** — e.g.
  `Gatito → Minino`, `keep "Granny Gray" untranslated`, or paste corrected JSON. A custom (_Other_)
  answer is approve-with-changes: apply exactly those swaps to the JSON at `draft_path`, re-run
  `commit`, briefly confirm what changed, and continue (the swap submission *is* the approval — don't
  loop back into this gate unless the user asks). Only continue once the user approves.
- **Log the outcome:** `log-event --event approval --data '{"beat":"glossary",
  "decision":"approved_first_pass"|"reject_then_redraft"|"approve_with_changes","redrafts":<n>}'`.

> Approving the glossary approves **the glossary only**. It does NOT mean "start translating."
> After approval you proceed to difficulty scoring + chunking (`references/chunk.md`) and then **stop again** at
> the cost gate (`references/translate-api.md` / `references/translate-workers.md`). Do not jump to translate here.

## Step 2B — ADDRESS MAP beat (OPTIONAL — never blocks translation)

A per-book **forms-of-address map** (`projects/<slug>/address_map.json`) records
which pairs of characters use **usted** vs. **tú**, including public/private and
story-stage differences. It powers the later `address` compliance judge
(judge-review); it does **not** affect this translation run.

**This beat is optional and non-blocking. Offer it; do not require it.** Ask the
user (plain question, not a gate) whether they want to build the address map now.
If they decline or don't care, **skip straight to `references/chunk.md`** — translation proceeds
normally, and the map can be built later (here, or from judge-review's setup
precheck) whenever they want to run the usted/tú judge.

If they opt in:
```bash
python scripts/harness.py address-map prepare --project projects/<slug>
```
This samples the book's highest interpersonal-dialogue chapters (a spread across
the whole book, not just the openers) and renders a prompt at `prompt_path`. Read
it, draft the map JSON (`{content, pairs, global_rules}`) to the printed
`draft_path` — each non-empty direction must end with a `when:"default"` rule —
refine it with the user, then:
```bash
python scripts/harness.py address-map commit --project projects/<slug>
```
**STOP — approval beat** (same shape as glossary): present the committed pairs +
global rules, AskUserQuestion (Approve all / Reject & talk it through; a custom
answer = approve-with-edits → re-commit). Log:
`log-event --event approval --data '{"beat":"address_map","decision":...}'`.
Then continue to `references/chunk.md`.
