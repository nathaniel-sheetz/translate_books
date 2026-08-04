# Glossary

Load this when `glossary.json` is missing or the user wants to revise the glossary.
(The address map is its own beat — `references/address-map.md`, Step 0B.)

## Step 2 — GLOSSARY beat (agent drafts, approval gate)

```bash
python scripts/harness.py glossary prepare --project projects/<slug>
```
This extracts candidates, feeds in the approved style guide, and prints `candidate_count`,
`style_guide_loaded` (must be `true` — if not, the style guide isn't saved yet; go back to `references/style-guide.md`),
`carryforward_count`, a `prompt_path`, and a `draft_path`.

`carryforward_count > 0` means the style-guide beat handed terms forward: it surfaced words that
need a fixed translation and — correctly — refused to define them in the guide, which states rules
rather than term pairs. Those terms are already injected as candidates and their rationale is in
the prompt's guidance block. **Define every one of them**; they were flagged precisely because
frequency-ranked extraction can bury a rare-but-critical term.

**Read the printed `prompt_path`** and draft the glossary proposals yourself — the thinking-mode
step. Produce a JSON array of `{english, translation, type, context, alternatives}` objects and
**Write** it to the printed `draft_path`. **Write the target language WITH its diacritics**
(Spanish: á é í ó ú ñ ¿ ¡). The `Write` tool is UTF-8 — do **not** ASCII-fold "to be safe";
stripped accents (`Tia` for `Tía`, `senor` for `señor`) become the canonical forms fed verbatim
to every translator worker.

Two field conventions the prompt spells out and the commit step lints for:
- **`alternatives` is not free.** It lets a worker pick a different rendering per chunk. Places
  and bare personal names take **none**. A title + personal name leads with the **narration form
  including the article** (`el tío Antony`) and offers the **bare vocative** (`tío Antony`) as its
  single alternative — the style guide's word-choice rule tells the worker which to use where.
- **`context` is GUI-only.** It is shown in the dashboard's glossary table and is **not** sent to
  the translator (`format_glossary_for_prompt` emits only `english → spanish (alternatives: …)`).
  Never park a usage rule there expecting it to reach the translation — that belongs in the style
  guide. Use `context` to explain a decision to the human reading the table, including the reason
  for any deliberate exception to the conventions above.

As you draft, **track every term whose translation you were unsure about**
(ambiguous sense, multiple valid renderings, dialect/register judgement calls) and why — keep that
running list for the approval beat. Then:
```bash
python scripts/harness.py glossary commit --project projects/<slug>
```
This guards the proposals, builds + saves `glossary.json`, and validates it; it prints the full
`terms` list. If it prints a VALIDATION ERROR, fix the entries it names and re-run `commit` (cap ~3).

**STOP — approval beat.** Do all of the following, in this order:
- **Show the full list of glossary terms** (use the `terms` the command printed) — render every
  `english → translation` pair with type/context so the user can scan the actual decisions. Do not
  collapse it to "N terms drafted."
- **Call out the uncertain translations** you tracked: name each term, its chosen rendering, the
  alternative(s) you considered, and why you hesitated, so the user can adjudicate the close calls.
- **Triage `warnings`** — `commit` returns one array holding two different kinds of thing. Split
  them:
  - *Draft bugs, fix before presenting.* The accent-stripping smell (all-ASCII translations in a
    diacritic language) means you ASCII-folded. Re-read `glossary.json`, restore the accents,
    re-run `commit`, **then** present the gate.
  - *`REVIEW:` lines — human judgement calls, present them.* These flag the alternatives
    conventions (a place or a bare personal name carrying alternatives; a title + name whose
    primary lacks the article or whose vocative alternative is missing) and a stale address-map
    cast. They are advisory, not errors — a genuine exception like `océano Atlántico` /
    `Atlántico` will trip one legitimately. Show each one and say whether you think it is a real
    fix or a justified exception, and let the user decide.
  - If a `REVIEW:` line says `address_map.json` still uses English cast names, that is the
    reconcile hand-off, and it is mechanical: after this gate, run `address-map rename` (it
    applies every approved form across the whole map and reports each substitution), read the
    draft it writes, then `address-map commit`. Do **not** hand-write the substitutions — the
    ordering is load-bearing (`Bambi's mother` before `Bambi`). See `references/address-map.md`.
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
