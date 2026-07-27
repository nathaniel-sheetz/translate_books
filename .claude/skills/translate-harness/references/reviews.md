# Incremental review loop

Load this when translated chapters exist and the user wants to review quality
(after a wave, after a full book, or mid-run). Thin — **delegates** to the
**judge-review** skill; do not duplicate judge logic here.

## When to enter

After a workers-path wave lands (see `references/translate-workers.md` 4B-e), or
whenever `status` shows translated chapters and the user asks to review. The
API path auto-aligns; still offer review once chapters are readable.

## Loop

1. **Align + reader link** (if not already done for this set):
   ```bash
   python scripts/harness.py align --project projects/<slug> --chapters <set>
   ```
   Read `.harness/last_output.json` from that `align` call. **Report any
   `coverage_warnings`** before reviewing anything else: each one is a run of
   source sentences the translation never covered (dropped prose). No judge or
   evaluator catches these — the Spanish reads perfectly without them.
   Re-translate the named chunk and re-align.

   Ensure the reader is up (`python web_ui/app.py` → `http://localhost:5000`) and
   hand the user `reader_first` from that same align output. Optional in-chat sample:
   ```bash
   python scripts/harness.py show-translation --project projects/<slug> \
     --chapters <set> --max-chunks 4
   ```
   That command overwrites `last_output.json` with a different schema
   (`chapters[] → chunks[] → translated_text`); do not look for
   `coverage_warnings` there.

2. **Invoke the judge-review skill.** Do not re-implement dialogue/address/etc.
   judges here. Hand it the project slug and the chapter set just translated;
   follow that skill's ROUTER / setup precheck (address map, etc.).

3. **Apply fixes** the user approves (retranslate / edit / glossary tweak), then
   re-align affected chapters if needed.

4. **Repeat** for the next wave, or continue the pipeline
   (`references/footnotes.md` if footnotes were kept, then `references/epub.md`).

## What this file deliberately does NOT do

- No judge prompt drafting, no `run_judges` CLI recipes, no suite definitions —
  those live in **judge-review** and `docs/JUDGES_FRAMEWORK.md`.
- No re-asking the translation backend or spawn mode — those are already in
  `.harness/config.json` (`backend`, spawn knobs).
