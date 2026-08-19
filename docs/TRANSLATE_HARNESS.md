# translate-harness

The harness is the primary way to translate a book. You drive it as a conversation in
Claude Code: the agent drafts the style guide and glossary in-chat, pauses for your
approval at each stage, and calls the deterministic pipeline between drafts. The
dashboard and reader ([`WEB_UI_GUIDE.md`](WEB_UI_GUIDE.md)) are where you review and
finish what the harness produced.

```
/translate-harness
```

That is the whole entry point. Everything below explains what happens behind it — the
stages, the three translation backends, the cost gates, and the CLI the agent calls.

---

## Which surface do I want?

| You want to… | Use |
|---|---|
| Translate a book start to finish | **The harness.** It drafts the style guide and glossary with you and runs every stage. |
| Read, annotate, and correct a translation | **The dashboard + reader.** Sentence-level editing, annotations, concordance search, EPUB export. |
| Re-run one stage in isolation, or script something | **The CLI** ([`CLI_REFERENCE.md`](CLI_REFERENCE.md)). The substrate both surfaces sit on. |

The two surfaces write the same project files, so you can move between them freely —
translate in the harness, review in the reader, come back to the harness to redo a
chapter.

---

## The beat model

Every stage that needs an LLM to *think* — the style guide, the glossary, the address
map, the translation itself — follows the same three-step shape:

1. **`prepare`** gathers inputs, renders a prompt, and prints where it put things:
   a `prompt_path` to read and a `draft_path` to write.
2. **The agent is the LLM.** It reads the prompt, drafts the answer in-conversation
   (this is where you review and push back), and writes the result to `draft_path`.
3. **`commit`** parses and validates the draft, then writes the real artifact. A
   malformed draft fails loudly with a specific error rather than silently producing a
   broken `style.json`.

This is why the harness replaces the old copy/paste-into-an-external-chat loop: the
thinking step happens where you can argue with it, and the validation step happens
before anything reaches the pipeline.

Deterministic stages (`chunk`, `combine`, `align`, `epub`) have no draft step — they
just run.

---

## The pipeline

```
setup ─► [ADDRESS MAP] ─► [STYLE GUIDE] ─► [GLOSSARY] ─► difficulty ─► chunk ─►
(ingest   optional; gated   agent drafts     agent drafts   (suggests    (sizes
 + split)  on dialogue       + approval      + approval      chunk size)  chunks)
                                                                            │
                                                                            ▼
                        [COST] ─► translate ─► combine ─► align ─► [FOOTNOTES] ─► epub
                       (estimate,  the one       (auto)   (reader)   (optional)
                        no spend)  paid step
```

The ordering is not arbitrary:

- **Address map before the style guide.** The map emits a `style_guide_summary` that
  becomes the guide's FORMS OF ADDRESS section, so it has to land first to inform it.
  It is offered only when the book actually has interpersonal dialogue, and it never
  blocks translation — see [`ADDRESS_JUDGE.md`](ADDRESS_JUDGE.md).
- **Style guide before the glossary.** Locale and register steer which translations the
  glossary proposes.
- **Glossary before chunking.** Difficulty scoring excludes glossary terms, and book
  difficulty sets the default chunk size.

`combine` runs automatically — the API path chains it, and on the workers path
`translate-commit` runs it per chapter as each chapter completes. The standalone
`combine` verb is for repair and backfill; `status.combine_stale` tells you when it is
needed.

---

## The three backends

The translation step is the only place real money can be spent, and you pick how it
runs. The choice is stored per-book in `.harness/config.json` under `backend`.

| Backend | How it runs | Cost | Auth |
|---|---|---|---|
| `api` | `harness translate` calls the provider API directly | **Metered** — billed per token | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in `.env` |
| `subagent` | `translate-prepare` renders one prompt per chunk; Task subagents translate them; `translate-commit` validates and stamps | Free (your Claude subscription) | Claude Code session |
| `headless` | Same prepare/commit seam, but `translate-fanout` drives a wave of `claude -p` (or `cursor-agent`) processes | Free (your subscription) | Subscription login on the CLI |

**The headless backend fails closed.** `src/harness/headless.py` scrubs every metered
credential from the child process environment (`ANTHROPIC_API_KEY`, `_AUTH_TOKEN`,
`_BASE_URL`, the Bedrock/Vertex switches) and refuses to start unless the CLI confirms a
subscription login. A key sitting in `.env` cannot turn a subscription run into a billed
one. `CLAUDE_CODE_OAUTH_TOKEN` deliberately survives the scrub, because that *is*
subscription auth.

Full provider and model configuration lives in [`LLM_PROVIDERS.md`](LLM_PROVIDERS.md).

### The prepare/commit seam

Both subscription backends share the same two verbs, which is what makes them
interchangeable:

```bash
# Render one prompt file per chunk + a manifest. No spend.
python scripts/harness.py translate-prepare --project projects/my-book --chapters 1-2 --brief

# ...workers translate each prompt into its .draft.txt...

# Validate every draft and stamp the chunks. Idempotent.
python scripts/harness.py translate-commit --project projects/my-book
```

Prompts and drafts live side by side in `.harness/translate/` as
`<chunk_id>.prompt.txt` and `<chunk_id>.draft.txt`.

`translate-commit` refuses a draft that is empty, that echoes the source instead of
translating it, that drops or invents `[IMAGE:...]` tokens, or that an evaluator flags —
and it names the offending chunk so you can re-spawn just that one. Nothing partial gets
stamped.

---

## Cost and approval gates

- **`chunk` and `cost` cannot spend.** They always pass `--cost-only` to the wrapped CLI.
  Run them freely to see what a translation would cost.
- **`translate` fails closed without `--yes`.** The agent runs the estimate first, shows
  it to you, and only passes `--yes` after you approve in a separate turn.
- **Approvals never cascade.** Approving the style guide or the glossary does not
  authorize translation. There is exactly one paid step, and it needs its own fresh
  approval every time.
- **`retranslate` without `--yes` is a preview.** It prints what it *would* clear and
  changes nothing.

---

## Project state and artifacts

The harness writes into your project directory alongside everything the dashboard uses:

| Path | What it is |
|---|---|
| `.harness/config.json` | Per-book decisions that survive sessions: `backend`, `footnotes_decision`, `address_map_decision`, `headless_cli`, worker model, parallelism |
| `.harness/last_output.json` | Structured result of the most recent command — always read this instead of parsing stdout |
| `.harness/last_output_schema.json` | Per-key documentation for the above |
| `.harness/translate/` | Rendered per-chunk prompts and worker drafts |
| `.harness/*_draft.json`, `*_prompt.txt` | In-flight drafts for the style guide, glossary, and address map beats |
| `logs/harness_runs.jsonl` | Append-only log of every command, at the repo root |

The pipeline artifacts themselves (`style.json`, `glossary.json`, `address_map.json`,
`chunks/`, `chapters/`, `alignments/`) are the same files the dashboard reads and
writes — see the project layout in the [README](../README.md#project-structure).

### Reading harness output

Every command mirrors a fresh structured result to `.harness/last_output.json` and
prints `OUTPUT_JSON: <path>` to stderr. Read that file. Do not pipe stdout into a JSON
parser and do not `grep` harness output — on Windows, accented bytes make tools treat it
as binary and truncate.

Successful payloads carry `_schema_path` and `_schema_keys` so you never have to guess a
field name. `_schema_keys` is a superset: conditional and flag-gated keys are listed
whether or not a given run emitted them. Pass `--schema` to inline the full descriptions;
errors always inline them.

---

## Command reference

Every command is `python scripts/harness.py <verb> ...`, run from the repo root. All of
them are non-interactive — none calls `input()`, so none can deadlock an agent. All take
`--project <slug-or-path>` (except `setup`, where it is optional) and `--schema`.

### Setup and splitting

| Verb | Spends? | What it does |
|---|---|---|
| `setup` | no | Create the project, persist config, run ingest + split. `--url` for Gutenberg, `--footnotes import\|drop`, `--chapter-pattern`, `--title` / `--author` |
| `split-preview` | no | Dry-run a split and print detected sections. Writes nothing |
| `split` | no | (Re)write `chapters/` from `source.txt` with the chosen controls |

Both split verbs take the same detection flags: `--chapter-pattern`
(`auto`, `headings`, `roman`, `numeric`, `chapter_roman_titled`,
`chapter_numeric_titled`, `allcaps_heading`, `bare_roman`, `custom`), `--heading-level`,
`--custom-regex` / `--custom-regex-file`, `--min-chapter-size`, and the front/back-matter
overrides. See [`CHAPTER_DETECTION_GUIDE.md`](CHAPTER_DETECTION_GUIDE.md).

### Drafting beats

| Verb | Sub-verbs |
|---|---|
| `style-guide` | `prepare-questions`, `prepare-followups`, `commit-followups`, `prepare-draft`, `commit` |
| `glossary` | `prepare`, `commit` |
| `address-map` | `precheck`, `skip`, `prepare`, `commit`, `rename` |

`address-map precheck` answers "does this book have dialogue at all?" and gates whether
the beat is worth offering; `skip` records that you declined so the router stops asking;
`rename` applies an approved glossary cast to an already-committed map.

### Sizing and cost

| Verb | Spends? | What it does |
|---|---|---|
| `difficulty` | no | Score EN→ES difficulty and suggest a chunk target size |
| `chunk` | **cannot** | Chunk at `--size` and print the cost estimate |
| `cost` | **cannot** | Re-print the estimate without re-chunking |

### Translation

| Verb | Spends? | What it does |
|---|---|---|
| `translate` | **yes, with `--yes`** | The one paid API step |
| `translate-prepare` | no | Render per-chunk prompts + manifest for workers. `--brief` keeps the payload small |
| `translate-commit` | no | Validate worker drafts and stamp the chunks (idempotent) |
| `translate-fanout` | no | Headless `claude -p` / `cursor-agent` wave over prepared drafts |
| `retranslate` | no | Clear translations **and** their stale worker drafts so a redo really re-translates. Preview unless `--yes` |

Scoping differs per verb: `translate`, `translate-prepare`, and `retranslate` take
`--chapters 1-2`; `translate-fanout` and `retranslate` take `--chunk-ids`;
`translate-commit` takes neither — it commits every draft it finds, which is what
makes it idempotent.

### Assembly and output

| Verb | Spends? | What it does |
|---|---|---|
| `combine` | no | Rewrite `chapters/<id>.txt` from translated chunks. Repair/backfill — normally automatic |
| `align` | no | Align translated chapters for the reader and print a reader link |
| `epub` | no | Build the EPUB from translated chunks |
| `footnotes` | depends | `translate` / `translate-prepare` / `translate-commit` / `apply` / `drop`. Follows the book's backend; `apply` and `drop` are free |
| `captions` | no | Find image captions still rendering as body prose and mark them `[CAPTION]`. Dry-run unless `--apply` |

### Inspection and state

| Verb | What it does |
|---|---|
| `status` | Pipeline progress: stage, per-chapter translated/pending, artifacts, backend, `combine_stale`, and a `next` hint. Read-only |
| `show-translation` | Print source + translation straight from `chunks/*.json`. Read-only |
| `runs` | Summarize a run from `logs/harness_runs.jsonl`: command timeline, beats, outcomes |
| `log-event` | Append a conversational beat the CLI cannot see (approvals, friction) |
| `config-set` | Persist a once-per-book decision |

`config-set --key` accepts exactly: `backend`, `footnotes_decision`, `headless_cli`,
`always_include_dialogue`, `always_include_image_instructions`, `headless_extra_flags`,
`headless_prompt_cache`, and the four `headless_effort_*` keys (`translate`, `judges`,
`annotations`, `footnotes`).

---

## Resuming, redoing, repairing

**Resuming** needs nothing special. Run `status` — it reports the stage, what is left per
chapter, and a `next` hint. Chunk-level idempotency means a re-run skips anything already
translated. Never hand-roll a loop over `chunks/*.json`; `status` and `translate-prepare`
already answer what is outstanding.

**Redoing chapters that already have translations is not a re-run of the forward
pipeline.** Running `translate` again will skip them, because they are already done. Use
`retranslate` to clear both the translations and their stale worker drafts first:

```bash
# Preview: prints what would be cleared, changes nothing
python scripts/harness.py retranslate --project projects/my-book --chapters 3-4

# Actually clear it
python scripts/harness.py retranslate --project projects/my-book --chapters 3-4 --yes
```

Clearing the drafts matters. Worker drafts persist across a re-prepare, so a redo that
only clears translations will re-commit the old drafts and look like nothing changed.

**Repairing** a project whose `chapters/*.txt` have drifted from its chunks: run
`combine`. `status.combine_stale` lists exactly which chapters need it.

---

## Where the agent instructions live

This document covers the concepts and the CLI. The agent-facing contract — the phase
ROUTER, the STOP beats, the per-stage reference files — lives in
`.claude/skills/translate-harness/`. That is instruction text written for Claude, not
reference material for you, and it is the source of truth for how the agent behaves.
Nothing here restates it, so the two never disagree.

If you want to extend the harness with a new stage, backend, or judge, start at
`.claude/skills/translate-harness/references/EXTENDING.md`.

---

## Related

| Document | Contents |
|---|---|
| [`LLM_PROVIDERS.md`](LLM_PROVIDERS.md) | Providers, models, subscription CLIs, worker-model pinning |
| [`WEB_UI_GUIDE.md`](WEB_UI_GUIDE.md) | Dashboard and reader reference |
| [`ADDRESS_JUDGE.md`](ADDRESS_JUDGE.md) | The address map and the usted/tú judge |
| [`JUDGES_FRAMEWORK.md`](JUDGES_FRAMEWORK.md) | Tailored LLM judges over translated chunks |
| [`ANNOTATION_REVIEW.md`](ANNOTATION_REVIEW.md) | Resolving reader annotations |
| [`CHUNKING_GUIDE.md`](CHUNKING_GUIDE.md) | Chunking algorithm and tuning |
| [`INGEST_GUTENBERG.md`](INGEST_GUTENBERG.md) | Gutenberg import and footnote handling |
| [`CLI_REFERENCE.md`](CLI_REFERENCE.md) | The underlying scripts |
