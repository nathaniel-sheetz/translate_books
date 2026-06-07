---
name: translate-harness
description: |
  Orchestrate the book-translation pipeline conversationally. The agent acts as the
  thinking-mode LLM that drafts the glossary and style guide in-conversation (replacing
  the copy/paste-into-an-external-chat loops), pauses for your approval, then runs the
  existing deterministic + API-backed pipeline (chunk -> translate -> combine -> epub).
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
---

# translate-harness

Drive the translation pipeline as a conversation. You (the agent) are the thinking-mode
LLM: you draft the glossary and style guide in-chat, the user approves or edits, and the
existing scripts produce the EPUB. The deterministic steps stay scripts; you call them.

Scope (v1, eng review 2026-06-05): **short texts** — a single chapter or a small book.
Long-book robustness (formal resume, batching) is out of scope.

## NON-NEGOTIABLE CONSTRAINTS (read first)

```
┌──────────────────────────────────────────────────────────────────────────┐
│ NEVER invoke an interactive code path — every input() prompt DEADLOCKS you.│
│   ✗ scripts/generate_style_guide.py  (built on input() per question)       │
│ Instead: call the src/ helper FUNCTIONS directly, and gate cost yourself    │
│   via --cost-only + an AskUserQuestion approval before translating.         │
└──────────────────────────────────────────────────────────────────────────┘
```

- **You fill the LLM roles, the helpers don't.** `build_question_prompt`,
  `build_glossary_prompt`, `build_style_guide_prompt` only *build* prompts;
  `parse_*`/`*_to_glossary` only *parse*. The actual LLM step in the scripts is a
  `call_llm(...)` you replace by drafting the answer yourself in-conversation.
- **Validate every draft before the pipeline consumes it** with `src/harness_guard.py`.
  A bad draft must fail loudly and trigger a re-draft, never poison the run.
- **Approval gates:** on each STOP beat, present the draft and ask. On approve → write
  the file and continue. On reject → re-draft with the feedback and re-present. Cap
  automated re-drafts at ~3, then force "hand-edit (use verbatim) or abort". The user
  may say "change my answer to Q3" at any point — honor it.
- **Each gate authorizes ONLY its own stage — approvals never cascade.** Approving the
  style guide does not authorize the glossary. Approving the glossary does **NOT** authorize
  translation. Approving the cost estimate is the *only* thing that authorizes the paid
  translation run, and it authorizes nothing beyond it. There is exactly one paid step
  (translate); it has its own dedicated gate (Step 4) and reaching it requires a fresh,
  explicit "yes, translate" — never inferred from any earlier approval.
- **The cost beat is a hard stop.** Showing the cost estimate and *starting* the
  translation must be two separate turns. After you print the estimate, END your turn with
  the AskUserQuestion and wait. Do NOT run the translate command in the same response that
  produced the estimate, and never bundle "estimate → translate" into one chain. Money
  moves only after the user answers that question affirmatively in a later turn.
- `--cost-only` is a pure estimator: it never spends and never prompts. The paid translate
  run requires `--yes`; never pass `--yes` unless the user explicitly approved the estimate
  in a separate turn.
- Run every Python snippet below from the repo root so `import src...` resolves.

## Pipeline overview

```
ingest/split ─► [STYLE GUIDE beat] ─► [GLOSSARY beat] ─► chunk ─► [COST beat] ─►
 (deterministic)  agent drafts          agent drafts      (det.)   translate ─►
                  + refine + approval    + approval                combine ─► epub
```

The style guide goes **first** on purpose: it captures the user's key decisions
(dialect/locale, name conventions, register, formatting) and those decisions then steer
the glossary — `build_glossary_prompt` takes the approved style guide as an input.

## Step 0 — Set up the project

Identify (or create) the project directory `projects/<slug>/` and get the source text
into `projects/<slug>/source.txt`.

- If the user gives a Gutenberg URL or a local text, run ingest + split (non-interactive):
  ```bash
  python scripts/translate_book.py projects/<slug> --start-stage ingest \
    --url "<url>" --project-name "<Title>" --author "<Author>" \
    --target-lang Spanish --target-lang-code es
  ```
  Stop it before it reaches the translate stage, or just run `--start-stage ingest`
  through `split` by pre-checking. Simplest: run ingest/split/chunk first, do the
  glossary + style-guide beats, then run translate onward (the stages are resumable).
- Confirm `projects/<slug>/source.txt` (or `chapters/chapter_*.txt`) exists before
  continuing.
- Clear intermediate state from any prior harness run for this project:
  ```bash
  python -c "import shutil, pathlib; shutil.rmtree('.tmp', ignore_errors=True); pathlib.Path('.tmp').mkdir()"
  ```

## Step 1 — STYLE GUIDE beat (agent drafts, refine loop, approval gate)

Mirrors the real order in `scripts/generate_style_guide.py` (fixed + conditional Qs →
answers → LLM follow-up Qs → answers → draft guide). `answers` is a dict keyed by
`question["id"]`; the value is the **0-based option index** for choice questions, or the
**custom string** for free-text answers — exactly what the CLI produces.

**1a. Gather questions (fixed + deterministic feature-sweep) and present them:**
```bash
python - <<'PY'
from pathlib import Path
import json, sys; sys.path.insert(0, ".")
from src.style_guide_wizard import get_active_questions, load_source_sample

proj = Path("projects/<slug>")
source = load_source_sample(proj)
fixed, conditional, manifest = get_active_questions(proj)
present = [n for n, r in manifest.features.items() if r.present]
for q in conditional:  # attach detected hints
    f = q.get("requires", {}).get("feature")
    if f and f in manifest.features and manifest.features[f].evidence:
        q["_detected_hint"] = manifest.features[f].evidence[0]
allq = list(fixed) + list(conditional)
Path(".tmp").mkdir(exist_ok=True)
Path(".tmp/style_source.txt").write_text(source, encoding="utf-8")
Path(".tmp/style_fixed.json").write_text(json.dumps(fixed), encoding="utf-8")
Path(".tmp/style_questions.json").write_text(json.dumps(allq), encoding="utf-8")
print(f"detected features: {present or '(none)'}")
print(json.dumps([{"id": q["id"], "question": q["question"],
                   "options": [o["label"] for o in q.get("options", [])],
                   "hint": q.get("_detected_hint", "")} for q in allq],
                 indent=2, ensure_ascii=False))
PY
```

**1b. Collect answers inline, question by question** (D6). Ask each question in chat with
its options and detected hint. Record the chosen **option index** (or custom string)
under the question's `id`. Let the user revise earlier answers ("change my answer to
Q3"). When done, write the answers:
```bash
python - <<'PY'
from pathlib import Path
import json
# Replace the dict below with the answers you collected: {question_id: index_or_string}
answers = {}
Path(".tmp/style_answers.json").write_text(json.dumps(answers), encoding="utf-8")
print(f"{len(answers)} answers recorded")
PY
```

**1c. Generate LLM follow-up questions (you are the LLM).** Build the prompt, draft the
extra questions yourself, write them to `.tmp/style_llm_questions.txt`, then parse + ask
them too (append their answers to `.tmp/style_answers.json`):
```bash
python - <<'PY'
from pathlib import Path
import json, sys; sys.path.insert(0, ".")
from src.style_guide_wizard import build_question_prompt
source = Path(".tmp/style_source.txt").read_text(encoding="utf-8")
fixed = json.loads(Path(".tmp/style_fixed.json").read_text(encoding="utf-8"))
answers = json.loads(Path(".tmp/style_answers.json").read_text(encoding="utf-8"))
# build_question_prompt(source_text, target_lang, locale, fixed_questions, fixed_answers)
prompt = build_question_prompt(source, "Spanish", "mx", fixed, answers)
Path(".tmp/style_qprompt.txt").write_text(prompt, encoding="utf-8")
print("follow-up-question prompt at .tmp/style_qprompt.txt")
PY
```
Read `.tmp/style_qprompt.txt`, draft follow-up questions → `.tmp/style_llm_questions.txt`,
then merge + present them:
```bash
python - <<'PY'
from pathlib import Path
import json, sys; sys.path.insert(0, ".")
from src.style_guide_wizard import parse_llm_questions
extra = parse_llm_questions(Path(".tmp/style_llm_questions.txt").read_text(encoding="utf-8"))
allq = json.loads(Path(".tmp/style_questions.json").read_text(encoding="utf-8")) + extra
Path(".tmp/style_questions.json").write_text(json.dumps(allq), encoding="utf-8")
print(json.dumps([{"id": q["id"], "question": q["question"],
                   "options": [o["label"] for o in q.get("options", [])]} for q in extra],
                 indent=2, ensure_ascii=False))
PY
```
Ask the follow-up questions inline, add their answers to the dict, and rewrite
`.tmp/style_answers.json` (re-run the 1b snippet with the full answer set).

**1d. Draft the style guide (you are the LLM), refine, save.** Build the prompt:
```bash
python - <<'PY'
from pathlib import Path
import json, sys; sys.path.insert(0, ".")
from src.style_guide_wizard import build_style_guide_prompt
allq = json.loads(Path(".tmp/style_questions.json").read_text(encoding="utf-8"))
answers = json.loads(Path(".tmp/style_answers.json").read_text(encoding="utf-8"))
source = Path(".tmp/style_source.txt").read_text(encoding="utf-8")
# build_style_guide_prompt(questions, answers, source_text, target_lang, locale)
prompt = build_style_guide_prompt(allq, answers, source, "Spanish", "mx")
Path(".tmp/style_guide_prompt.txt").write_text(prompt, encoding="utf-8")
print("style-guide prompt at .tmp/style_guide_prompt.txt")
PY
```
Read it, draft the style-guide prose to `.tmp/style_guide_draft.txt`, and **refine it with
the user in chat** until they sign off. Then save + validate:
```bash
python - <<'PY'
from pathlib import Path
import sys; sys.path.insert(0, ".")
from src.style_guide_wizard import parse_style_guide_response, save_style_guide_json
from src.harness_guard import validate_style_guide_file
content = parse_style_guide_response(Path(".tmp/style_guide_draft.txt").read_text(encoding="utf-8"))
out = Path("projects/<slug>/style.json")
save_style_guide_json(content, out)
validate_style_guide_file(out)
print(f"style.json written ({len(content)} chars)")
PY
```

**1e. STOP — approval beat.** Present the final style guide. Approve / edit / re-draft
(cap ~3 re-drafts, then hand-edit-or-abort). This is the user's chance to lock in the key
decisions (dialect/locale, name conventions, register) **before** they shape the glossary.

## Step 2 — GLOSSARY beat (agent drafts, approval gate)

The approved style guide now steers term choices — read it back in so name conventions,
dialect, and register carry into the glossary.

1. Extract candidates deterministically and build the proposal prompt, feeding in the
   approved style guide. Run:
   ```bash
   python - <<'PY'
   from pathlib import Path
   import sys; sys.path.insert(0, ".")
   from scripts.extract_glossary_candidates import extract_candidates
   from src.glossary_bootstrap import build_glossary_prompt
   from src.style_guide_wizard import load_source_sample

   proj = Path("projects/<slug>")
   source = (proj / "source.txt").read_text(encoding="utf-8")
   report = extract_candidates(source, verbose=False)
   candidates = [c.model_dump() for c in report.candidates[:200]]
   sample = load_source_sample(proj)
   style_path = proj / "style.json"
   style_guide = style_path.read_text(encoding="utf-8") if style_path.exists() else ""
   # build_glossary_prompt(candidates, source_text_sample, style_guide_content, target_lang)
   prompt = build_glossary_prompt(candidates, sample, style_guide, "Spanish")
   Path(".tmp/glossary_prompt.txt").parent.mkdir(exist_ok=True)
   Path(".tmp/glossary_prompt.txt").write_text(prompt, encoding="utf-8")
   print(f"{len(candidates)} candidates; style guide {'loaded' if style_guide else 'MISSING'};"
         f" prompt written to .tmp/glossary_prompt.txt")
   PY
   ```
2. **Read `.tmp/glossary_prompt.txt`** and draft the glossary proposals yourself — this
   is the thinking-mode step. Produce a JSON array of
   `{english, translation, type, context}` objects. Write it to `.tmp/glossary_draft.json`.
   As you draft, **track every term whose translation you were unsure about** (ambiguous
   sense, multiple valid renderings, dialect/register judgement calls) and note why — keep
   that running list for the approval beat below.
3. Validate + build the glossary, guarding the parse boundary:
   ```bash
   python - <<'PY'
   from pathlib import Path
   import json, sys; sys.path.insert(0, ".")
   from src.harness_guard import guard_glossary_proposals, validate_glossary_file
   from src.glossary_bootstrap import glossary_terms_from_proposals, proposals_to_glossary
   from src.utils.file_io import save_glossary

   proposals = json.loads(Path(".tmp/glossary_draft.json").read_text(encoding="utf-8"))
   guard_glossary_proposals(proposals)          # raises HarnessValidationError -> re-draft
   glossary = proposals_to_glossary(glossary_terms_from_proposals(proposals))
   out = Path("projects/<slug>/glossary.json")
   save_glossary(glossary, out)
   validate_glossary_file(out)                  # belt-and-suspenders
   print(f"glossary.json written: {len(glossary.terms)} terms")
   PY
   ```
   If the guard raises, fix the offending entries it names and re-draft (cap ~3).
4. **STOP — approval beat.** Do all three, in this order:
   - **Show the full list of glossary terms**, not just a count or summary — render every
     `english → translation` pair (with type/context) so the user can scan the actual
     decisions. Offer it as a table or list; do not collapse it to "N terms drafted."
   - **Call out the uncertain translations** you tracked in step 2: name each term, its
     chosen rendering, the alternative(s) you considered, and why you hesitated, so the
     user can adjudicate the close calls deliberately.
   - **AskUserQuestion: approve / edit / re-draft.** On edit, let the user hand the
     corrected JSON; write it verbatim and re-validate. Only continue once approved.
   > Approving the glossary approves **the glossary only**. It does NOT mean "start
   > translating." After approval you proceed to chunking (Step 3) and then **stop again**
   > at the cost gate (Step 4). Do not jump to the translate command here.

## Step 3 — Chunk (deterministic) — run as estimator only

Run chunk **with `--cost-only`** so the run chunks and then halts at the cost estimate
(`--cost-only` exits before a single chunk is translated — it physically cannot spend):
```bash
python scripts/translate_book.py projects/<slug> --start-stage chunk --cost-only
```
This produces the chunks and prints the estimate, then stops. Carry that estimate straight
into the Step 4 gate below — do not run any command without `--cost-only` until the user
has approved.

## Step 4 — COST beat, then translate (no input() ever)

```
┌──────────────────────────────────────────────────────────────────────────┐
│ THIS IS THE ONLY PAID STEP AND THE ONLY HARD STOP THAT COSTS MONEY.        │
│ Estimating cost and starting translation are TWO SEPARATE TURNS.           │
│ Print the estimate, ask, and END YOUR TURN. Never run --cost-only and the  │
│ translate command in the same response. No earlier approval (style guide,  │
│ glossary, chunk) authorizes this — only the answer to the question below.  │
└──────────────────────────────────────────────────────────────────────────┘
```

1. You already have the estimate from Step 3 (the `--cost-only` chunk run). If you need to
   recompute it, re-run the cost estimate — still WITHOUT translating, so it never spends
   money:
   ```bash
   python scripts/translate_book.py projects/<slug> --start-stage translate --cost-only
   ```
2. **STOP — approval beat. END THE TURN HERE.** Show the estimate, then ask via
   AskUserQuestion: proceed / abort — and stop. Do not call any further tool in this
   response. Resume to step 3 ONLY after the user has, in a *later* turn, explicitly chosen
   to proceed. If you are unsure whether they approved, treat it as NOT approved and ask
   again. Confirm the model with them here too (it determines the price).
   > Cost note (eng review 2026-06-05): the API path does not use prompt caching today,
   > so input tokens are not discounted across chunks. The estimate is the honest figure.
3. **Only once the user has affirmatively approved in a separate turn**, translate with
   `--yes` to record that approval for the non-interactive CLI run:
   ```bash
   python scripts/translate_book.py projects/<slug> --start-stage translate \
     --yes --provider anthropic --model claude-sonnet-4-20250514
   ```
   (Pick the model the user wants; default is sonnet. Surface model choice rather than
   assuming.)

## Step 5 — Combine + EPUB (rebuild translated-only, like the web UI)

The translate run above already chained through combine, epub, and align — but **do not
ship that auto-built EPUB.** It is wrong for any partially-translated book.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ WHY: `stage_split` writes the ENGLISH text of every chapter into           │
│ chapters/chapter_*.txt. `stage_combine` only overwrites the .txt for       │
│ chapters that are FULLY translated, so untranslated chapters keep their     │
│ English text — and `build_epub` globs ALL chapter_*.txt, so the EPUB ends   │
│ up with English chapters mixed in. The web UI avoids this by combining only │
│ fully-translated chapters into a temp dir and building from that.          │
└──────────────────────────────────────────────────────────────────────────┘
```

Rebuild the EPUB the web-UI way — translated chapters only — overwriting the polluted one:
```bash
python - <<'PY'
from pathlib import Path
import sys, tempfile, shutil; sys.path.insert(0, ".")
from scripts.translate_book import discover_chapters
from src.utils.file_io import load_chunk
from src.combiner import combine_chunks
from src.epub_builder import build_epub

proj = Path("projects/<slug>")
chunks_dir = proj / "chunks"

# Include a chapter only if EVERY one of its chunks has a translation (mirrors
# the web UI's total==translated rule).
chapters = discover_chapters(chunks_dir)
translated, skipped = {}, []
for ch_id, paths in chapters.items():
    chunks = [load_chunk(p) for p in paths]
    if chunks and all(c.has_translation for c in chunks):
        translated[ch_id] = chunks
    else:
        skipped.append(ch_id)

if not translated:
    raise SystemExit("No fully translated chapters — nothing to build.")

tmp = Path(tempfile.mkdtemp(prefix="epub_"))
try:
    for ch_id, chunks in translated.items():
        (tmp / f"{ch_id}.txt").write_text(combine_chunks(chunks), encoding="utf-8")
    # Overwrite the pipeline's default output path: projects/<slug>/<slug>.epub
    out = build_epub(
        project_path=proj, title="<Title>", author="<Author>", language="es",
        chapters_dir=tmp, output_path=proj / f"{proj.name}.epub",
    )
    print(f"EPUB rebuilt: {out}")
    print(f"  included {len(translated)} translated chapter(s): {sorted(translated)}")
    print(f"  skipped {len(skipped)} untranslated chapter(s): {sorted(skipped)}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
PY
```
Report the included/skipped chapter lists to the user so a partial translation is never
mistaken for a complete book. (Use the project's real `<Title>`/`<Author>` and target
language code.) Confirm the EPUB landed:
```bash
ls projects/<slug>/*.epub
```

## Done

Report: glossary terms count, style-guide length, chunk count, EPUB path. Confirm the
copy-paste loop is gone — the user drafted nothing in an external chat.

## What this skill deliberately does NOT do (v1)

- No `TranslationBackend` abstraction — translation goes straight through the existing
  API path (`translate_chunk_realtime`). The subagent backend + a backend Protocol are
  deferred until Approach B is scheduled (eng review D9).
- No long-book resume beyond the pipeline's existing chunk-level idempotency
  (`stage_translate` skips chunks that already have a translation).
- No prompt caching (tracked separately in TODOS.md).
