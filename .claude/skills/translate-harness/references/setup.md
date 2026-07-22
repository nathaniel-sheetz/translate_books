# Setup (ingest / split)

Load this when starting a book or refining the split. Persist once-per-book decisions with `config-set` after the user picks.

## Step 0 — Set up the project

Get the source text into `projects/<slug>/source.txt` (or pass a Gutenberg `--url`), then run
ingest + split (NOT chunk — chunking is deferred to `references/chunk.md` so it can use the glossary-informed
difficulty score). `setup` also persists `target-lang` / `locale` / `model` / `title` /
`author` so later steps stop repeating them.

**Identify the book first, then let `setup` name the folder from its title.** Omit `--project`
and `setup` derives the project slug from `--title` (e.g. `"Understood Betsy"` →
`projects/understood-betsy`), so the folder, reader URLs (`/read/<slug>/chapter_01`), and EPUB
path all say what the book is — don't fall back to a cryptic Gutenberg id like `g5347`. If that
title-slug already exists, the new project gets a `-2`, `-3`, … suffix. Pass `--project <slug>`
explicitly only to **re-run on an existing project** (it's used verbatim and reuses that folder;
relying on `--title` would mint a new numbered folder instead).

**Don't launch a browser to identify the book.** To get the title/author for the slug, make a
single `WebFetch` on the source URL. Never invoke the `/browse` (or any browser) skill here: it
loads its whole ~600-line skill body into context just to read one fact off a static page.
`CLAUDE.md`'s "use `/browse` for all web browsing" rule is about interactive/QA browsing of the app
under development — reading a public page is the carve-out.

```bash
python scripts/harness.py setup \
  --target-lang Spanish --locale mx \
  --title "<Title>" --author "<Author>"
# add --url <gutenberg-url> if there is no local source.txt yet.
# add --project <slug> only to re-run on / target a specific existing project folder.
```

`--chapter-pattern` now **defaults to `auto`**, which detects the best-fit pattern from the
source text itself — you rarely need to set it. The named patterns are still selectable:
`roman` (Chapter I, II …), `numeric` (Chapter 1, 2 …), the titled variants
`chapter_roman_titled` / `chapter_numeric_titled` (a title on the *same* line, e.g.
`CHAPTER I. WATHO.` — the common Gutenberg shape), `allcaps_heading`, `bare_roman`, or
`custom` (with `--custom-regex`). On **both** the `--url` and the local `source.txt` path,
`setup`/`split`/`split-preview` now return `pattern_used` (what it split on), `suggested_pattern`
(what the text/HTML implies), and a `chapter_report`. If `pattern_used` ≠ `suggested_pattern`,
or `warnings` is non-empty (e.g. "1 chapter for an 87 KB source"), the split is probably wrong —
re-run with the suggestion. Confirm the printed `chapter_count` looks right and `chunks_dir_exists`
is `false`. (The lang/locale defaults are Spanish/mx — surface them to the user rather than
assuming silently. The model is **not** chosen here; on the API path it is confirmed at the
`references/translate-api.md` cost gate, and on the workers path the worker tier is chosen in
`references/translate-workers.md`.)

Navigation/boilerplate (the title page, a `CONTENTS`/table-of-contents listing, a list of
illustrations, a copyright/transcriber's note) is **auto-stripped** — never written, numbered, or
translated — and each stripped heading is reported back under `dropped` in the `setup` /
`split-preview` / `split` output. Confirm `dropped` matches what you expected. Real front matter
(foreword, preface, prologue, dedication, author's note …) is auto-detected and **kept**, and it
renders its *translated* heading in the EPUB automatically — no manual relabel.

**Footnotes — keep as reader footnotes, or drop?** On the `--url` path, `setup` **imports**
Gutenberg footnotes by default: it captures each note as a survivable `[FOOTNOTE:N]` token in the
body plus a `footnotes.json` sidecar, and reports `footnotes_detected` (count) and `footnotes_mode`
(`import`) in its output. **If `footnotes_detected > 0`, STOP and ask the user** — AskUserQuestion
with two options, *"Keep as translatable reader footnotes"* and *"Drop them"* — since footnotes
noticeably change the reader experience and add a small paid step later (`references/footnotes.md`). Then:
- **Keep** → nothing to run; the tokens ride through split/chunk/translate untouched. Tell the user
  the note bodies get translated after the chapters, in `references/footnotes.md`. Persist and log:
  ```bash
  python scripts/harness.py config-set --project projects/<slug> --key footnotes_decision --value keep
  python scripts/harness.py log-event --project projects/<slug> \
    --event footnotes_decision --data '{"decision":"keep"}'
  ```
- **Drop** → run `python scripts/harness.py footnotes drop --project projects/<slug>`, which strips
  the tokens from `source.txt` + chapters and deletes the sidecar (no re-fetch). Persist and log:
  ```bash
  python scripts/harness.py config-set --project projects/<slug> --key footnotes_decision --value drop
  python scripts/harness.py log-event --project projects/<slug> \
    --event footnotes_decision --data '{"decision":"drop"}'
  ```

Footnote detection only happens on the `--url`/HTML path — a project seeded from a local
`source.txt` can't detect or import them (`footnotes_detected` is `0`), so there is nothing to ask.
When none are detected, persist that so later sessions skip the footnotes beat:
```bash
python scripts/harness.py config-set --project projects/<slug> --key footnotes_decision --value none
```

**Refine the split if it looks wrong** — the `setup` split misfires, reports "No chapters
detected," or a *real* section is mis-numbered as a chapter (or vice-versa). Don't hand-edit
`source.txt`; use the review beat, which mirrors the web GUI's Stage 2:

```bash
# Dry-run: prints each section tagged front_matter / chapter / back_matter,
# plus a `dropped` list of stripped boilerplate. Writes nothing.
python scripts/harness.py split-preview --project projects/<slug> \
  --chapter-pattern custom --custom-regex '(?<=\n---\n\n)[A-Z][^\n]*' \
  --min-chapter-size 500 \
  --front-matter-title "To the Teacher" --back-matter-title "A Word to the Children"

# Happy with the preview? Commit it (rewrites chapters/, clearing any stale files).
python scripts/harness.py split --project projects/<slug>  # + the same split flags
```

**Force-tagging KEEPS a section, it never removes one.** `--front-matter-title` /
`--back-matter-title` are repeatable and force a *real* heading the keyword auto-detect missed
(e.g. "To the Teacher") to be tagged matter so it isn't mis-numbered as a chapter — the section is
still written, translated, and included. **Do not** declare the title page / `CONTENTS` /
boilerplate here: that would un-strip them and push the junk through the whole pipeline. They drop
on their own; leave them alone. Built-in keyword auto-detect (preface, dedication, epilogue …)
stays on unless you pass `--no-auto-front-matter` / `--no-auto-back-matter`; boilerplate
auto-strip stays on unless you pass `--no-auto-strip` (only needed for the rare book with a genuine
chapter literally titled "Contents"). Raising `--min-chapter-size` (~500) drops short stray
front-matter lines a loose pattern would otherwise capture. All of these controls also work
directly on `setup` for a one-shot run.
