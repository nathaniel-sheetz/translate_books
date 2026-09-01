# The nightly pass

An unattended, scheduled sweep over every book: review the reader's pending
annotations, apply the safe subset, and leave the rest in a web inbox that can be
cleared in one sitting.

Reviewing was never the bottleneck. The logged annotation jobs median under ten
seconds each, so the whole backlog is minutes of machine time. What stalled was
*landing* the results: `review.apply` was reachable only from a chat session, one
book at a time. The last hand-run pass reviewed ~48 notes and applied 9 of them.
This is the other end of that funnel.

Three pieces, each usable on its own:

| Piece | What it is |
|---|---|
| [`scripts/pending_work.py`](../scripts/pending_work.py) | Read-only scanner: what work exists, what would block it |
| [`scripts/daily_pass.py`](../scripts/daily_pass.py) | The driver: detect → review → auto-apply, per book, under a lock |
| `/review-inbox` | The web funnel: every book's outstanding resolutions on one page |

Plus [`scripts/nightly.ps1`](../scripts/nightly.ps1), which registers the driver as
a Windows scheduled task, and [`src/harness/locks.py`](../src/harness/locks.py),
which stops the pass and the always-on dashboard corrupting each other.

---

## Quick start

```bash
# What is out there? Zero spend, seconds, safe any time.
python scripts/pending_work.py scan

# What would tonight do? Writes nothing, spawns nothing.
python scripts/daily_pass.py --dry-run

# Do it, for real, bounded.
python scripts/daily_pass.py --max-books 3
```

Then open <http://127.0.0.1:5000/review-inbox> — linked from the reader home page —
to work through everything the policy would not apply on its own.

To run it every morning:

```powershell
scripts\nightly.ps1 install     # registers TranslateBooksNightly, daily at 06:30
scripts\nightly.ps1 status      # audits the live task against this script
scripts\nightly.ps1 run         # force one execution now
scripts\nightly.ps1 log         # the last passes plus the newest digest
```

---

## Scope: which books

`projects/` is not a flat list of live books. The canonical walker descends
through grouping folders, which is right for the dashboard — you want
`.macdonald/photogen-nycteris` in the project list — and wrong for a scheduled
job, because the same walk finds `.backburner/the-little-duke.bak-ch1-restore`: a
backup snapshot whose annotations are a copy of another book's.

Two filters, in this order:

1. **Group denylist** — `automation.exclude_groups`, default `.backburner` and
   `.published`. Matches any path component, so a nested `.backburner/older/x`
   is excluded too.
2. **`project.json`'s `archived`** — the reader's own per-book choice.

A third rule is inherited rather than configured: two book folders with the same
leaf name are one addressable id, so the first wins and the second is reported
as `duplicate_id` rather than silently reviewed under the other's name.

---

## Backend: each book keeps its own pin

**The pass never forces a CLI.** It resolves per book, through
[`src.actions.scope.resolve_book_cli`](../src/actions/scope.py), on this ladder:

| Rung | Source | Provenance |
|---|---|---|
| 1 | `--cli` on the driver (debugging only) | `cli` |
| 2 | the book's `headless_cli` pin | `config` |
| 3 | **`automation.default_cli`** | `automation.default_cli` |
| 4 | host detection | `host:*` |
| 5 | `claude` | `fallback` |

Rung 3 is the new one, and it is the main lever rather than an edge case. Of the
sixteen books with pending notes, six carry a real pin; the other ten are
`auto`/`null`. Under a scheduled task there is no driving host, so rung 4 finds
nothing and those ten would silently fall to `claude`. Setting
`"default_cli": "cursor"` moves all ten at once without touching a single book's
config — and never overrides a book that chose for itself.

`pending_work.py scan` prints the provenance per book, so you can see exactly
which rung answered:

```
  27  five-little-peppers   cursor (config) · grok-4.6[effort=medium,fast=false]
   6  gaudenzia             claude (automation.default_cli) · sonnet
```

Three consequences worth knowing:

- **The worker model is per CLI**, so the driver never pushes one
  `--worker-model` across books. `sonnet` on Claude, your selected Cursor model
  on Cursor. Change a book's pin to change its reviewer.
- **Cursor gets no prompt cache.** `cursor-agent` has no `--system-prompt-file`,
  so the ~3.2k-token preamble is folded into stdin per job. Fine at this volume,
  but a note on Cursor costs more than the same note on Claude.
- **Preflight is per CLI, up front.** A logged-out `claude` is terminal for the
  books pinned to Claude and nothing else; the Cursor books still run. Nothing is
  prepared or spawned for a refused CLI, so re-running after `claude` + `/login`
  is a clean start rather than a half-finished resume.

---

## The auto-apply policy

The pass writes back only what is both **recoverable** and **confident**:

```
writable  AND  type ∈ auto_apply_types  AND  mode == "append"  AND  confidence ≥ floor
```

Defaults: `word_choice`, `inconsistency`, `flag`, at `high`.

Those three are *append-only* writes after an `— IA:` marker into an append-only
log — the reader can see them, and the original record survives untouched.

**Footnotes are never auto-applied.** A footnote's content *is* the published
endnote text, so `review._planned_content` gives it `mode: "replace"` and
[`src/endnotes.py`](../src/endnotes.py) publishes that text into the EPUB. The
policy checks the mode as well as the type list, so adding `"footnote"` to
`auto_apply_types` still will not publish a model's gloss — it goes to the inbox
either way.

Everything the policy refuses is `held`, which is not a failure: it is the
inbox's queue. `held` has two exits — apply it, or **reject** it. A rejection is
the same append-only noop write as a retire (the note's own text, plus a stamp),
so the suggestion stops being re-detected without anything being written into the
book. Nothing the pass does can override one: `apply` refuses to write over any
record that already carries a stamp, which matters because `auto_apply` runs even
when the night's `run` errored, off whatever `results.json` is on disk.

---

## Configuration

`app_config.json`, the `automation` block, read through `load_app_config`:

```json
"automation": {
  "exclude_groups": [".backburner", ".published"],
  "default_cli": "claude",
  "auto_apply_types": ["word_choice", "inconsistency", "flag"],
  "confidence_floor": "high",
  "max_targets_per_run": 400,
  "deadline_minutes": 120,
  "concurrency": 5
}
```

Every key has a default in
[`src.actions.scope.AUTOMATION_DEFAULTS`](../src/actions/scope.py), so a fresh
clone with no block behaves identically. Driver flags override both:
`--default-cli`, `--concurrency`, `--max-targets`, `--deadline-minutes`.

Size `deadline_minutes` against the per-job ceilings in `headless._CLI_JOB_TIMEOUT_S`
— claude 1800 s, cursor 900 s — not against the 9.7 s median.

**It is checked between books, not inside one.** A book already under way runs to
completion, so the real worst case is `deadline_minutes` plus one book's full
fan-out. That is why the scheduled task's `ExecutionTimeLimit` is PT3H against a
120-minute deadline rather than matched to it: the driver has to be able to
finish its last book and still write the digest before the scheduler intervenes.
Bounding a single wedged *job* is the per-job CLI timeout's work, not this
setting's.

---

## Locks

Nothing in this repo locked across processes before this. Every wave type follows
the same destructive shape — `prepare` renders prompts and rewrites
`manifest.json`, `fanout` fills the drafts, `commit` reads them — and `prepare`
*unlinks* the drafts of the entries it re-renders. A wave started from the CLI has
no Flask job record, so `jobs.JobConflict` never fires and a click in the
dashboard re-prepares underneath it.

[`src/harness/locks.py`](../src/harness/locks.py) fixes that with one file per
book, `projects/<slug>/.harness/.lock`, created with `O_CREAT | O_EXCL` (atomic on
Windows, no `fcntl`). Its body names the holder, so a blocked caller can say who
is in the way:

- **Same host, dead PID** → break it.
- **Same host, live PID** → hold.
- **Different host** → PID liveness means nothing; only age applies.
- **Older than 3 h** → break it either way.

> Liveness is never probed with `os.kill(pid, 0)`. On Windows CPython implements
> `os.kill` with `TerminateProcess` for any signal that is not a console control
> event, so the POSIX idiom would kill the wave it asks about.

`logs/.nightly.lock` is the driver's own lock, so a hand-run pass and the
scheduled one cannot interleave.

Both sides are wired: the dashboard's four job-starting routes return a 409 when
an outside wave holds the lock, *and* hold the lock themselves for the duration of
the job. The driver waits 60 s for a busy book, then skips it for the night.

---

## `/review-inbox`

Every in-scope book's outstanding plan on one page, grouped by book and by
annotation type, with `old → new`, a checkbox and a Reject button per resolution.

- **Nothing is pre-ticked.** The page exists because the previous funnel applied
  9 of ~48; the fix is making each one readable, not defaulting them to yes.
- **Reject is per row; there is no bulk reject.** It stamps the note without
  changing a character of it, so the suggestion never returns — until you edit the
  note in the reader, which drops the stamp and reopens the question. Undo is
  offered in place until you reload.
- **Flagged**: every `low`-confidence resolution, and every footnote — whose text
  is published, and so is where an invented date or measurement would print.
- **Needing a hand**: `manual[]` with its reason (`multi_anchor`, `no_note_text`).
- **Orphaned**: notes whose anchor sentence no longer exists. No run will ever
  reach them; only re-anchoring in the reader will.
- **The list is what is still outstanding**, not what the last run decided.
  `review.apply(dry_run=True)` plans off `results.json`, which keeps a resolution
  until the next `prepare` drops it as `already_reviewed` — so the page compares
  each entry against the note's *live* text first. Already-applied and deleted
  entries drop out; an entry whose note was edited since the review is shown as
  **stale**, explained, and not tickable, because `apply` would refuse it.
- Applying goes through `review.apply(select=…)` — the only writer — which makes
  the same checks again before writing. Re-applying the same selection is a
  no-op.
- Applying a **footnote** offers a **Rebuild EPUB** button, because a replace only
  reaches the book on the next build.

The reader home page links to it beside the per-book work chips. Those chips plus
the digest are the whole notification surface: the repo has no email, webhook or
toast of any kind, and this does not add one.

---

## Artifacts a pass leaves behind

| Path | What |
|---|---|
| `reports/nightly/annotations_<YYYYmmdd>.md` | The digest, linking each book's own dated report |
| `logs/nightly.jsonl` | One summary row per pass |
| `logs/harness_runs.jsonl` | Per-book `nightly_*` events via `log_run_event` |
| `projects/<slug>/reports/annotations_*.md` | Each book's own report, unchanged |
| `projects/<slug>/.harness/annotations/` | Manifest, prompts, drafts, `results.json` |

No EPUB is rebuilt in the automated path: auto-apply excludes footnotes, so
nothing it writes reaches a book. The inbox owns that.

---

## Adding a second action

The registry has one entry today. `run_judges.py` already has the identical
`prepare / fanout / commit / apply` shape, so the judge pass is a new module in
`src/actions/` and one line in `ACTIONS` — not a refactor:

```python
@dataclass(frozen=True)
class Action:
    name: str
    detect: Callable[[Path], ActionState]          # counts + blockers, no spend
    run:    Callable[[Path, Budget], RunResult]    # do the work
    auto_apply: Callable[[Path, Policy], ApplyResult] | None
```

`auto_apply=None` is legal, and means "run it, stop at the report".

---

## Related

- [`ANNOTATION_REVIEW.md`](ANNOTATION_REVIEW.md) — the pipeline this drives
- [`CLI_REFERENCE.md`](CLI_REFERENCE.md) — every script, one line each
- [`WEB_UI_GUIDE.md`](WEB_UI_GUIDE.md) — the dashboard and reader
- [`LLM_PROVIDERS.md`](LLM_PROVIDERS.md) — CLI, worker model and effort resolution
