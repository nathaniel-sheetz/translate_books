# Style guide beat

Load this when `style.json` is missing or the user wants to revise the style guide. Three STOP gates (G1/G2/G3).

## Step 1 — STYLE GUIDE beat (two question gates, then draft + approval)

This beat has **three STOP points**: G1 (1b) and G2 (1c) collect the user's answers *before*
any draft exists; G3 (1e) approves the finished guide. Do not reach the draft (1d) until the
user has answered both question gates.

**1a. Gather the standard + deterministic questions:**
```bash
python scripts/harness.py style-guide prepare-questions --project projects/<slug>
```
This prints `detected_features`, the `questions` (the 4 **standard** fixed questions plus the
**deterministic** feature-detected ones, each with `id`, `question`, `options`, and a `hint`),
and an `answers_path`. Each option is an `{id, label}` pair — the `id` is a stable slug you pass
straight through, so you never count positions. Nothing here is answered yet — these are
*for the user*. Two notes: (a) the `dialect` question may arrive with a `prefilled` id +
`prefilled_reason` derived from the setup locale (es-mx → `mexican_spanish`) — present it as a
confirm/override default, not a blank ask. (b) `forms_of_address` has a first-class informal-tú
option (id `t_dominates_informal` — the `ú` is dropped by the slug rule); prefer that id over
inventing custom text when the user wants tú to dominate.

(c) If the address-map beat ran (`references/address-map.md`, Step 0B), **`forms_of_address` is
already answered** — it was pulled forward so the map could be drafted against the right register,
and the answer is already in `answers_path`. Present it as answered, for confirmation, rather than
asking it blind; only re-ask if the user wants to change it (changing it means the committed map
should be revisited too).

**1b. STOP — G1: ask the standard + deterministic questions and WAIT.** Present **every**
unanswered question in chat with its options and hint, then **END your turn** and wait for the
user's answers. Do **not** answer them yourself, pick defaults, or run the next command first. (There
are usually more than 4 — 4 fixed + N detected — so ask in chat, optionally batching via
`AskUserQuestion` in groups of ≤ 4; don't assume one `AskUserQuestion` holds them all.) Once the
user has answered (let them revise earlier answers), record each as the chosen option's **`id`**
(or its exact `label`) from the prepare-questions output — or a **custom string** for anything not
among the options — and **Write** the dict to the printed `answers_path`:
`{question_id: option_id_or_label_or_custom_string}`, e.g.
`{"dialect": "mexican_spanish", "forms_of_address": "t_dominates_informal"}` (here `dialect` keeps
the `prefilled` id from setup and `forms_of_address` uses the first-class tú option — reserve a
custom string for genuinely off-menu rules).
(A 0-based numeric index still works for back-compat, but the `id` is safer — no position counting.)
Only then continue to 1c.

**1c. Generate the LLM-driven follow-up questions, then STOP to ask them.** First draft them —
you are the LLM:
```bash
python scripts/harness.py style-guide prepare-followups --project projects/<slug>
```
Read the printed `prompt_path`, draft the follow-up questions as a JSON array, **Write** them to
the printed `draft_path`, then:
```bash
python scripts/harness.py style-guide commit-followups --project projects/<slug>
```
**STOP — G2: ask the printed `new_questions` and WAIT.** Present them in chat, **END your turn**,
and wait for the user's answers (same rule — do not answer them for the user). Only after the
user responds, **rewrite `answers_path` with the full answer set** (prior answers + the new
ones, same `id`/label/custom-string format), then continue to 1d.

**1d. Draft the style guide (you are the LLM), refine, save.**
```bash
python scripts/harness.py style-guide prepare-draft --project projects/<slug>
```
This also reports `resolved_answers` (each tagged `option` or `custom`) and `unanswered` — glance
at it to confirm every answer matched the option you intended (a `custom` tag on a question you
answered by `id` means a typo; fix `answers_path` and re-run). It also reports
`address_summary_loaded`: `true` means an approved address map supplied the FORMS OF ADDRESS
section, so **reproduce that summary rather than writing your own** — it was drafted against the
whole-book dialogue sample and the user already signed off on it.

Then read the printed `prompt_path`, draft the style-guide prose, **Write** it to the printed
`draft_path`, and **refine it with the user in chat** until they sign off.

Two rules the prompt states and you must hold yourself to while drafting:

- **The reader sees ONE chunk and nothing else.** No chapter identity, no book context, no
  glossary, no address map, no other books — the rendered translation prompt genuinely carries
  none of it. So never write "see the address map", "as established elsewhere", "after chapter N",
  "if the relationship shifts later", or "consistent with the other books". Restate such a rule as
  something decidable from one excerpt (usually: the form that holds for most of the book), or drop it.
- **No term→translation pairs in the guide.** Terms ship separately as the glossary; a guide that
  also names terms creates two sources of truth. When drafting surfaces a term that needs a fixed
  translation, **Write** it to the printed `carryforward_path` as a JSON array of
  `{term, why, type_guess}` — the glossary beat injects those as candidates and shows your `why`
  in its prompt. What *does* belong in the guide is the generic rule that tells a worker which
  glossary entry or alternative to reach for, e.g. *"for a title + personal name, use the form
  with the definite article in narration (el tío Manuel llegó tarde) and the bare vocative in
  direct address (Oye, tío Manuel, quiero…)"*.

Then:
```bash
python scripts/harness.py style-guide commit --project projects/<slug>
```
This parses, saves `style.json`, and validates it. If it prints a VALIDATION/PARSE error, fix the
draft and re-run `commit` (cap ~3 re-drafts, then hand-edit-or-abort).

**1e. STOP — G3: approval beat.** Present the final style guide, then **AskUserQuestion with exactly
two predefined options** — **"Approve all"** and **"Reject & talk it through"**. **Remind the user in
the question text that to approve *with specific changes* they should pick _Other_ and type the edits
directly** (e.g. "switch register to tú", "keep place names in English"). A custom (_Other_) answer is
approve-with-changes: apply the edits to the draft, re-run `style-guide commit`, and continue. This is
the user's chance to lock in the key decisions (dialect/locale, name conventions, register)
**before** they shape the glossary.

**Log the outcome** once decided: `log-event --event approval --data '{"beat":"style_guide",
"decision":"approved_first_pass"|"reject_then_redraft"|"approve_with_changes","redrafts":<n>}'`
(`redrafts` = how many times you re-ran `commit` after a validation/rejection before this approval).
