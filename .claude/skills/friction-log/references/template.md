# Friction log template

The canonical section skeleton, distilled from the logs already in
`.claude/skill-friction-logs/`. Sections 4, 6 and 9 are **dropped when empty** (a session
with no fanout has no usage table); the rest are always present.

Richest exemplars, if you want to see the shape at full size:

- `judge-review/2026-08-26-photogen-nycteris-editorial-first-run.md` — prior-item table,
  two-pass usage table, a self-inflicted item at position 0
- `annotation-review/2026-07-29-the-little-duke-word-choice-and-one-footnote.md` —
  per-issue body shape, struck-through closed follow-ups
- `translate-harness/2026-08-20-the-story-of-nelson-backburner-resume-epub-metadata.md` —
  prior-log status as a bullet list, "what I would do differently as the agent"

---

## 1. Title

```markdown
# <skill-name> friction log — <book / scope, in a few words>
```

## 2. Header block

Bullets, in this order. Omit a line only if it truly does not apply.

```markdown
- **Date:** YYYY-MM-DD
- **Skill(s):** `<skill>` (CLI: `scripts/<x>.py`), <backend>, worker `<model>`, effort `<e>`.
  Name secondary skills/CLIs here too, and say if the skill doesn't document one.
- **Repo state:** branch `<name>` @ `<sha>` (VERSION `<x.y.z.w>`), <clean / uncommitted work in tree>.
- **Book:** <Author, *Title*> (`projects/<slug>`) — <n> chapters / <n> chunks.
- **Scope:** what this run actually covered, in chunks/chapters and any conservatism flags.
- **Outcome:** what succeeded, what failed, in numbers. This is the one-line verdict.
- **Prior logs this compares against:** [<date> <short name>](<relative-file.md>), ...
```

## 3. TL;DR — ranked

```markdown
## TL;DR — friction ranked by impact

0. **<Worst item, bolded lead clause.>** What happened in two sentences. **<Measured
   cost — minutes, tokens, re-runs.>** Numbering may start at 0 when the top item is
   process rather than tooling.
1. **<Next item.>** ...
```

Follow with a short **What is still true, not a regression** paragraph when the run
re-confirmed known-good behaviour worth not re-litigating.

## 4. Prior items — status *(drop if there are no prior logs)*

Either a table:

```markdown
| Prior item | This run |
|---|---|
| **<log> N. <finding>** | **Did not fire.** / **Recurred.** / **CLOSED** — <how>. |
```

or bullets:

```markdown
- **<log> §2 (<finding>): still open, recurred.** Fix remains no-code.
- **<log> §3 (<finding>): CLOSED** — now documented at `flow.py:5035`.
```

## 5. Run transcript (short)

Numbered beats — one line each, the commands actually issued and what came back. This is
the evidence the ranked findings point at, not a narrative.

## 6. Usage *(drop if no wave ran)*

```markdown
| | <pass 1 / wave 1> | <pass 2 / wave 2> |
|---|---|---|
| jobs | | |
| input / output | | |
| prompt_sent | | |
| overhead / ratio | | |
| cache_creation / cache_read | | |
| wall_s | | |
| cost_equiv_usd | | |
| worker | | |
| baseline at gate | measured <n> / default <n> | |
```

A sentence under the table explaining *why* the ratio looks the way it does is worth
more than the table alone.

## 7. Friction, ranked

One `###` per item, ordered worst-first, matching the TL;DR order.

```markdown
### N. <Short title> · severity: HIGH|MEDIUM|LOW (<cost class>)

**What happened.** Concrete: the command, the error text or the omission, the artifact
state. Paste short stack traces or payload keys verbatim.

**Why it matters.** What it cost and what it will cost again. Name the recurrence if
this has fired before.

**Suggested fix.** Name the file and function — `verify_editorial.py::cmd_fanout`
should default `concurrency` before `run_headless_wave`. Say "none in code" when the fix
is procedural, and say what the procedure is.

Wasted: <the number>.
```

Cost classes: `operator time`, `token waste`, `blocks the run`, `quality`,
`entry friction`, `framing`.

## 8. What went well (keep)

Bullets. Specific behaviours worth preserving, so the next run does not "fix" them.
Include the deliberate choices that paid off (one `AskUserQuestion`, `--quiet` prepare,
no re-prepare after a switch).

Optionally follow with **What I would do differently as the agent (no CLI change
needed)** — the procedural half, separated from the tooling half.

## 9. Numbers for the next run *(drop if no wave ran)*

```markdown
| | <this run's shape> |
|---|---|
| billed input / output | |
| prompt_sent | |
| overhead_ratio | |
| prepare baseline | |
| prepares / fanouts / commits | |
| wall | |
```

The number the *next* consent gate should quote.

## 10. Open follow-ups

Ranked by value, not by section order. Closed items stay in the list, struck through:

```markdown
1. <Highest-value leftover> (item N) — will recur any time <trigger>.
2. ~~<Closed item>~~ — **done, in <version>.** <What changed, and the measured effect.>
```

Close with one short paragraph: what this version's path did right, and where the
session paid an old tax again.
