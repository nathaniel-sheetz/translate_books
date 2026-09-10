---
name: friction-log
description: |
  Write a post-run friction log for a skill session — the durable record of where a run
  wasted tokens or operator time, ranked by impact, quantified, and cross-referenced
  against the prior logs for that skill so recurring rough edges are visible.
  Files it in the right directory under the right skill with the right sections, and
  gathers the real repo/usage numbers instead of writing from recollection.
  Use when asked to "write a friction log", "log the friction from this run", "friction
  log for this session", "write up what went wrong", "log what wasted time here", or
  "friction-log".
allowed-tools:
  - Bash
  - Read
  - Write
  - Glob
  - Grep
  - AskUserQuestion
---

# friction-log

Turn a finished skill session into a friction log: a ranked, quantified account of what
this run cost that it should not have. The audience is the next session of the same
skill — and whoever is deciding what to fix next.

These logs are the reason the skills improve. Later logs open with a **Prior items —
status** table saying which earlier findings recurred and which were closed in a named
version. That continuity is the highest-value part of the format and the part that dies
first if a log is written from memory. Do the gathering step.

## What belongs in one

**The bar is significant waste of tokens or operator time.** Not a tidy diary of the
run.

- **Every ranked item carries a cost.** Tokens, wall-clock or operator minutes, extra
  turns, dead processes, re-runs, re-translations. "~8 minutes of operator wait, zero
  tokens spent." "5 chunks of avoidable re-translation." An item with no cost attached
  does not belong in the ranked list — move it to *What went well* or drop it.
- **Rank by impact, not chronology.** The worst thing goes first, even if it happened
  last.
- **Severity is `HIGH` / `MEDIUM` / `LOW` plus a cost class** in parentheses:
  `operator time`, `token waste`, `blocks the run`, `quality`, `entry friction`,
  `framing`. e.g. `· severity: HIGH (operator time)`.
- **Include self-inflicted agent errors, prominently.** The existing logs do this
  unflinchingly — *"Announced spawn with no process"*, *"Self-inflicted: Bash `find`
  instead of `Glob`"* — and they are frequently the top item. State plainly what the
  agent did wrong and what it cost. No hedging, no burying it at position 5.
- **Suggested fixes name the file and function** that would change, or say "none in
  code" when the fix is procedural.
- **Things that worked go in `What went well (keep)`.** Keeping that section honest is
  what stops the next run from "fixing" something that was already right.

## Where it goes

Resolve the target directory, first hit wins:

1. an explicit path in the user's ask,
2. `$SKILL_FRICTION_LOG_DIR`,
3. `<repo-root>/.claude/skill-friction-logs/` — the default.

The file is always:

```
<resolved-dir>/<skill-name>/<YYYY-MM-DD>-<slug>.md
```

- Create the per-skill subdirectory if it does not exist. (The few loose files at the
  top level predate the convention — do not add to them.)
- `<slug>` is kebab-case `<project-slug>-<what-happened>`, e.g.
  `2026-08-26-photogen-nycteris-editorial-first-run`,
  `2026-07-19-the-little-duke-dropped-chunk-tail-paragraph`.
- Use today's date, not the date of the run's first command.
- **Never overwrite.** Check first — `ls <resolved-dir>/<skill-name>/` in the gather step
  already shows you the directory; a shell redirect (`cat > path`) clobbers silently and
  these logs are gitignored, so a clobbered one is gone. If the path exists, lengthen the
  descriptor to something distinguishing (`-second-run`, `-apply`, `-ch12-16`) rather
  than clobbering.

The default directory is gitignored, so writing a log is a local action — it is never
part of a commit and should not be offered as one.

**Which skill** the log belongs to is inferred from the session. If more than one ran,
file it under the primary one and name the rest on the header's `Skills:` line. Ask only
if that is genuinely ambiguous.

## How to write one

### 1. Gather (read-only, batch it)

Do not write from recollection. One Bash call for the repo state:

```bash
git branch --show-current; git rev-parse --short HEAD; git status --short | head -20; cat VERSION
```

Then, as they apply to the session:

- the project's `.harness/last_output.json` (and `.harness/editorial/last_output.json`
  for a pass-2 editorial run) — stage, backend, worker model, effort, artifacts
- `logs/harness_runs.jsonl`, filtered to this session's project — the actual command
  sequence and timings
- the fanout / commit payloads already in this conversation — jobs, input/output tokens,
  `prompt_sent`, `overhead_ratio`, cache creation/read, `wall_s`, `cost_equiv_usd`

### 2. Read the prior logs

```bash
ls <resolved-dir>/<skill-name>/
```

Read the **2–3 most recent** for that skill, plus any earlier one for the same book.
They give you:

- which findings are recurrences (say so, and link them) versus new,
- which were closed — strike those through and mark `**done, in <version>.**`,
- the baseline numbers this run should be compared against.

Link prior logs relatively — they are siblings in the same per-skill directory, so the
target is a bare filename: `[<date> <short name>](<log-filename.md>)`. For instance, link
text `2026-08-11 photogen effort-consent` pointing at
`2026-08-11-photogen-nycteris-cursor-effort-consent.md`.

### 3. Write it

`Read` `references/template.md` for the section skeleton, then write the file. Sections
that would be empty for this session (no wave → no usage table) are dropped, not left as
empty headings.

### 4. Report back

Give the path and the ranked headline items in two or three lines. Do not paste the log
body back into the conversation — it is on disk and the user asked for a file.
