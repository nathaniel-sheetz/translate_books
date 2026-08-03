---
name: translate-harness
description: |
  Orchestrate the book-translation pipeline conversationally. The agent acts as the
  thinking-mode LLM that drafts the glossary and style guide in-conversation (replacing
  the copy/paste-into-an-external-chat loops), pauses for your approval, then runs the
  existing deterministic + API-backed pipeline (chunk -> translate -> combine -> epub).
  Phase-routed: on entry run `status`, then Read only the matching `references/*.md`.
  Use when asked to "translate this book in the harness", "run translate-harness",
  "draft the glossary and style guide with me", or "orchestrate the translation".
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - AskUserQuestion
  - Task
---

# translate-harness

Drive the translation pipeline as a conversation. You (the agent) are the thinking-mode
LLM: you draft the glossary and style guide in-chat, the user approves or edits, and the
existing scripts produce the EPUB. The deterministic steps stay scripts; you call them
through one non-interactive surface — **`scripts/harness.py`**.

Scope (v1, eng review 2026-06-05): **short texts** — a single chapter or a small book.
Long-book robustness (formal resume, batching) is out of scope.

**Phase-routed:** this file is the always-loaded core. On entry, run `status`, read
`config.backend` / `suggested_reference`, consult the ROUTER below, and **Read only the
matching `references/*.md`** — do not load the whole skill tree.

## How the harness CLI works (read first)

Every command is `python scripts/harness.py <command> ...`, run from the repo root, and is
**non-interactive** — it never calls `input()`, so it can never deadlock you. Each beat has
the same shape:

- **`prepare`** → gather inputs, build the LLM prompt, print JSON with `prompt_path` /
  `draft_path`.
- **You are the LLM:** Read the prompt, draft the answer, **Write it to `draft_path`**.
- **`commit`** → parse + validate/guard the draft and write the final artifact. A bad draft
  fails loudly; fix and re-run the same `commit` (cap ~3, then hand-edit-or-abort).

**Reading harness output — Read the artifact, NEVER parse stdout.** Every command mirrors a
fresh structured result to `projects/<slug>/.harness/last_output.json` and prints
`OUTPUT_JSON: <path>` to stderr. **Always `Read` that file** — never pipe stdout into a JSON
parser, and **never `grep` harness output** (Windows treats accented bytes as binary and
truncates).

**Windows / UTF-8:** run ad-hoc probes as `python -X utf8 -c "..."` (or `PYTHONUTF8=1`).
Open project files with `encoding="utf-8"`. Prefer `Read` on `last_output.json`.

**Don't guess field names — read the `_schema`.** Every `last_output.json` carries a
`_schema` block mapping each result key to a one-line description. That is the contract.

**Run logging:** every command appends to `logs/harness_runs.jsonl` automatically. Log
conversational beats the CLI can't see with `log-event` (fire-and-forget — never gate the
flow on a failed log):

```bash
python scripts/harness.py log-event --project projects/<slug> \
  --event approval --data '{"beat":"glossary","decision":"approved_first_pass"}'
```

Persist once-per-book decisions so later sessions stop re-asking:

```bash
python scripts/harness.py config-set --project projects/<slug> \
  --key backend --value subagent   # api | subagent | headless
python scripts/harness.py config-set --project projects/<slug> \
  --key footnotes_decision --value keep   # keep | drop | none
```

```
┌──────────────────────────────────────────────────────────────────────────┐
│ NEVER invoke an interactive code path — every input() prompt DEADLOCKS you.│
│   ✗ scripts/generate_style_guide.py  (built on input() per question)       │
│ Always go through scripts/harness.py: it is the non-interactive surface,    │
│ and it gates cost via --cost-only + a --yes you only pass after approval.    │
└──────────────────────────────────────────────────────────────────────────┘
```

**Universal approval invariant:** approvals never cascade. Approving the style guide or
glossary does **not** authorize translation. There is exactly one paid step (`translate` /
API footnotes translate); it needs its own fresh, explicit `--yes` after a separate-turn
approval. Stage-specific STOP beats (style-guide G1/G2/G3, **4B-backend three-way**, cost
hard-stop, spawn-mode, **redo gate**) live in the reference files — load those when that stage runs.
When `backend` is unset, load `references/translate-workers.md` and run its **three-option**
gate (API / subagent / headless) — never invent a binary "API vs subagent."

## Pipeline overview

```
ingest/split ─► [ADDRESS MAP beat] ─► [STYLE GUIDE beat] ─► [GLOSSARY beat] ─► difficulty ─► chunk ─►
 (setup;         OPTIONAL; gated on    agent drafts         agent drafts      (det.; sizes  (det.)
  [FN keep/drop]) a dialogue precheck   + refine + approval  + approval         chunks)
                  + approval                                                                  │
                                                                                              ▼
                                          [COST beat] ─► translate ─► combine ─► align ─►
                                                    [FN translate+apply beat] ─► epub
```

Address map first, and only when the book has dialogue: it emits a `style_guide_summary` that
becomes the guide's FORMS OF ADDRESS section, so it has to precede the guide to inform it.
Style guide next (locale/register steer the glossary). Glossary before chunking (difficulty
excludes glossary terms; book difficulty sets default chunk size). Ingest/split in `setup`
is the only deterministic prep that runs up front.

**The address map never blocks translation.** It is skippable at every step, and the decision
(`built` / `skipped` / `no_dialogue`) is recorded in `address_map_decision` so the router stops
offering it. A project that already has a style guide is never routed backwards to this beat.

**`combine` is automatic**: the API path chains it, and on the workers path `translate-commit` runs
it per chapter as each chapter completes (rewriting `chapters/<id>.txt` from the translated chunks).
`harness combine` is the explicit repair/backfill verb — `status.combine_stale` says when it is
needed. **Redoing chapters that already have translations is NOT a re-run of the forward pipeline**
→ load `references/retranslate.md` first, then re-enter at translate.

## Entry ritual / resume

1. `python scripts/harness.py status --project <slug>` (or omit `--project` only when
   starting fresh — then there is no project yet → `references/setup.md`).
2. `Read` `.harness/last_output.json` — note `stage`, `next`, `artifacts`, `backend`,
   `footnotes_decision`, `address_map_decision`, `suggested_reference`, `spawn_plan`, `epubs`.
3. Consult the ROUTER table; **Read only the matching reference file(s)**.
4. Never hand-roll a loop over `chunks/*.json` — `status` / `translate-prepare` answer
   what's left. Past runs: `runs --project <slug>`.

## ROUTER

| You want to… | Signal (`status` / config / files) | Load |
|---|---|---|
| Start / ingest a book | no `project.json` / no project yet | `references/setup.md` |
| Offer / draft the address map | `style.json` missing **and** `address_map_decision` unset | `references/address-map.md` |
| Draft or revise the style guide | `style.json` missing/stale | `references/style-guide.md` |
| Draft or revise the glossary | `glossary.json` missing | `references/glossary.md` |
| Build the address map **later** (user asks, or judge-review needs it) | `address_map.json` missing, any stage | `references/address-map.md` |
| Chunk / estimate size | `stage: pre-chunk` | `references/chunk.md` |
| Translate (metered) | `config.backend == api` | `references/translate-api.md` |
| Translate (workers) | `config.backend in {subagent, headless}` **or backend unset** (run 4B-backend three-way gate first) | `references/translate-workers.md` |
| Handle footnotes | `footnotes.json` exists and `footnotes_decision` not `none`/`drop` | `references/footnotes.md` |
| Build the EPUB / refresh `chapters/*.txt` | `stage: fully-translated` with no `.epub`, **or `status.combine_stale` non-empty** | `references/epub.md` |
| Review a translated wave | translated chapters exist | `references/reviews.md` |
| Redo / re-translate chapters that **already** have translations | user says redo / re-translate / start over **and** `status` shows `partial`/`fully-translated` | `references/retranslate.md` |
| Add a stage/backend/processor/judge | — | `references/EXTENDING.md` |

Prefer `suggested_reference` from `status` when present; fall back to this table.

## What this skill deliberately does NOT do (v1)

- No `TranslationBackend` Protocol. Backends share `build_translation_prompt` +
  `apply_translation`. Workers path = `translate-prepare` / `translate-commit` (+ optional
  `translate-fanout`); footnotes carry the chosen backend forward. Still deferred: the
  **judge** headless backend and enlarging the translation preamble for Opus/Haiku's
  4096-token cache minimum.
- No long-book resume beyond the pipeline's existing chunk-level idempotency
  (`stage_translate` skips chunks that already have a translation).
- Verse/poetry *rendering* is not unified behind a registry — see
  `references/EXTENDING.md` (known rough edge).
