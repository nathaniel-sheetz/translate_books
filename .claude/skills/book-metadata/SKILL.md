---
name: book-metadata
description: |
  Draft the texts a finished translation needs before it is posted on the site or
  published: the book page's description and card summary, «Sobre esta edición»
  (what this edition is and what it changed), «Lo que los padres deben saber» (the
  parent notes), the recommended forms, and the EPUB's «Sobre este libro» page. Reads
  the family-review or theology-review, checks every flag against the translation
  (rewritten, noted or unchanged), finds the silent changes, counts the notes in the
  built EPUB, and lists the gaps a small edit would settle. Saves a draft file and
  shows it in the conversation; after approval, writes project.json, about_book.json
  and the site repo's book folder. Run after translation and after the content review.
  Use when asked to "draft the book metadata", "write the parent notes", "write Sobre
  este libro", "prepare the book page", "what did this edition change", "get this book
  ready for the site", or "book-metadata".
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - WebFetch
  - WebSearch
  - AskUserQuestion
---

# book-metadata

You do the judgment work: what to name, how to word it, which gaps matter.
**`scripts/book_metadata.py`** says what a draft can rest on, checks the drafted texts
and does every write. One STOP gate (G1) sits before anything is written.

**This is not `kdp-publish`.** That skill writes the store listing, the title page and
the copyright page. This one writes what a parent reads before handing the book to a
child. The two don't depend on each other, and the KDP reading age is never derived
from `forms`.

## What this drafts, and where it lands

| Text | Stored in | Shown in | Rendering |
|---|---|---|---|
| `description` | site `libros/<slug>/descripcion.md` | top of the book page | Markdown |
| `resumen` | site `libros/<slug>/libro.toml` | the book's card, the page's meta description | one sentence |
| `forms` | `project.json` | site book page | codes from `book_metadata.FORMS`: `I`, `II`, `III/IV`, `V/VI` |
| `adaptations` | `project.json` → site `adaptaciones.md` | «Sobre esta edición» | Markdown |
| `parent_notes` | `project.json` → site `padres.md` | «Lo que los padres deben saber» | Markdown |
| `about_book` `{heading, body}` | `about_book.json` | both EPUBs, first page after the contents | paragraphs, `_italic_`, `[texto](url)`, `[LIBRE]`/`[KINDLE]` paragraphs, `{{SITIO}}`/`{{COMENTARIOS}}`. **No bold, lists or subheadings** |

**Each text has one job.**
- **Description:** why this book is worth reading. Nothing about the edition.
- **«Sobre esta edición»:** what this edition is and what it changed.
- **«Lo que los padres deben saber»:** what the parent has to judge or act on.
- **«Sobre este libro»:** the short form of the last two, in the EPUB. There is no
  separate short parent-notes field.

On the site the two boxes open folded to about four lines behind «Ver más», so the
first lines carry the most. `forms`, `adaptations` and `parent_notes` never go into
either EPUB.

## When to run it

After the last translated chapter, after **family-review** or **theology-review**, and
after `footnote-pass` and `image-pass`, because the texts count the notes and describe
the images. Before the final build: `about_book.json` shows up only in an EPUB built
after it was written.

## The CLI (read first)

`python -X utf8 scripts/book_metadata.py <command> --project <slug>` prints one JSON
object and mirrors it to `projects/<slug>/.harness/metadata/last_output.json`.

```bash
python -X utf8 scripts/book_metadata.py status --project <slug>
python -X utf8 scripts/book_metadata.py check  --project <slug> [--texts-file <path>] [--epub <path>]
python -X utf8 scripts/book_metadata.py apply  --project <slug> [--texts-file <path>] [--dry-run]
python -X utf8 scripts/book_metadata.py site   --project <slug> [--texts-file <path>] [--site-dir <path>] [--dry-run]
```

Two files are yours:
- **`projects/<slug>/metadata_draft.md`:** the draft the user reads and edits.
- **`projects/<slug>/.harness/metadata/texts.json`:** the same texts as data, which
  `check`, `apply` and `site` read. Write both with the Write tool, never a heredoc.

```json
{
  "forms": ["II"],
  "adaptations": "- …\n- …\n- …",
  "parent_notes": "…\n\n**…** …",
  "about_book": {"heading": "Sobre este libro", "body": "…\n\n…\n\n[LIBRE] En [nuestro sitio]({{SITIO}}) …"},
  "description": "**…**\n\n…",
  "resumen": "…"
}
```

A key left out is left alone wherever it would land.

## Flow

### 1. Status and the guide

```bash
python -X utf8 scripts/book_metadata.py status --project <slug>
```

- **`needs`:** settle each item first. With no content review in the project folder,
  ask the user where it is, or stop: the drafts rest on it. An EPUB older than the last
  edits is rebuilt before any note is counted (`python scripts/harness.py epub --project
  <slug>`).
- **`reviews`:** `family-review.md` (family-review) or `content-review.md`
  (theology-review).
- **`epub.endnotes`:** the note count. It comes from the built EPUB, never from an
  earlier draft: it changes after each footnote-pass.
- **`current`:** what is already written. A book with texts in place is a revision:
  read them, and say in the draft what changes.
- **`site`:** whether the book has a folder in the site repo yet.

Then **Read `guide.path` in full.** It is the user's writing guide: the voice, the
rules for each text and the texts to imitate. Its rules outrank your own sense of what
a good parent note is. A previous `metadata_draft.md` in a sibling project is a second
model when the guide points to one.

### 2. Read the content review in full

List every item the texts may have to name:
- the review's **Cut/rewrite** items,
- its **Preview** and **Skip aloud** items,
- its worldview, theology and accuracy flags,
- anything the user asked to have named.

Note the audience the review gives, and what it rates as needing no action.

### 3. Check every flag against the translation

For each item record one of **rewritten**, **noted**, **unchanged**, with the Spanish
sentence and the note number. This decides the wording: «una nota lo corrige» against
«este pasaje no lleva nota». Never write the first without having seen the note.

- **Cut/rewrite items first.** They were written before translation, and nothing
  guarantees the translation acted on them.
- **Where to look:** the translated chunk for the passage
  (`chunks/*.json`, `source_text` against `translated_text`), then the chapter's XHTML
  in the built EPUB for `href="endnotes.xhtml#en-N"`.
- **Search on distinctive Spanish words near the passage,** not on the flagged English
  word. «negro» is mostly a colour.
- **One chapter at a time.** A whole-book grep with no chapter filter floods the
  context.
- **Chapter numbers.** The review cites the source's numerals, and
  `chapters/chapter_NN.txt` is offset by the front matter. The texts cite the number
  the EPUB heading shows.

### 4. Find the silent changes

The site promises that every kind of change is declared, so report what the record
shows even when it differs from what the user remembers.

- **`corrections_applied.jsonl`:** the hand edits (`original_es` → `corrected_es`,
  `en_reference`). Filter for changed numbers and proper nouns. Most rows are style or
  _tú/usted_ and say nothing here.
- **`chunks/*.json`:** what the translator changed with no hand edit, such as an
  epithet made neutral at translation time.
- **`source.txt`:** stray note tokens, names and dates, compared with the translation.
- **A correction that blames the source** needs support from outside the pipeline: a
  second edition, another translation, a catalogue. Say in the draft what the support
  was, and offer a wording that claims less when there is none.

### 5. Notes, and what sits outside the chapters

- **Notes.** Read the built EPUB's `EPUB/endnotes.xhtml`, split on `<a id="en-N"`, and
  group the notes by purpose: updated science, safety warnings, corrected figures and
  dates, places, money and measures. The «Capítulo X» text after note N is the next
  section's heading: note N belongs to the chapter _before_ it.
- **Front and back matter.** What `source.txt` has that the EPUB does not: a preface,
  a list of illustrations, an advertisement. What was kept, and under what heading.
- **Images.** `images_original/`, `.harness/images/manifest.json` and `cover/`: which
  images were replaced or dropped, whether the captions are translated, whether the
  cover is new. Count image markers in `source.txt` against the chapters.
- **Provenance.** `edition_notice.json` and `kdp_listing.json` (`copyright_basis`) give
  the source edition, an intermediate translation, and the notes the translator wrote.
  Give a year for an intermediate edition only when a catalogue confirms it.

### 6. Draft

**Forms.** Look the book up as the guide says, and map the level to a code. Cross-check
against the KDP reading age and the site's `edades`. Give the evidence in the draft,
not in the texts.

**The texts,** following the guide:
- **`description`** and **`resumen`**, with two or three alternative hooks.
- **`adaptations`**, true as of today's build.
- **`parent_notes`**. Only what the edition does not already handle. Content the user
  decides to keep becomes a parent note, even where the review asked nothing of the
  parent.
- **`about_book`**. The same content as the last two in plain paragraphs: edition
  facts, then translation decisions, then a summary for the parent. A site link goes in
  a `[LIBRE]` paragraph.

**The EPUB page is stricter than the site about spoilers.** A child reading alone sees
«Sobre este libro» before chapter 1. It names no death and no event from the last
chapters; it gives the hard chapters by number and sends the parent to the site. How
the book resolves a religious or worldview thread is not plot: state it there too,
without the scene.

Write `texts.json`, then:

```bash
python -X utf8 scripts/book_metadata.py check --project <slug>
```

Fix every error. `check` refuses markup the EPUB page would show as typed characters,
a note count that differs from the built EPUB, and site-only or EPUB-only markup in
the wrong text. It cannot judge voice, length or spoilers: reread each text against
the guide before the gate.

### 7. Gaps and decisions

A **gap** is something the texts would have to hedge on and that a small edit would
settle: a dangerous passage with no note, an uncorrected error, a leftover epithet, a
Cut/rewrite item that was not done. List each with the line of the draft it would
change. Fixing a gap is the user's call and another skill's work (a hand edit,
`footnote-pass`); this skill only reports it.

List separately what you left out of the parent notes on purpose, and why.

### 8. Write `metadata_draft.md`

One file, in this order. Each text sits in a fenced block so it can be copied whole.

1. **Header:** the date, what the draft was made from (the review, the EPUB and its
   date), and that nothing has been written yet.
2. **Site description**, the alternative hooks, the `resumen`, and, when the book has
   no folder in the site repo, a proposed `libro.toml` modelled on an existing book's.
3. **`forms`**, with the evidence.
4. **`adaptations`**.
5. **`parent_notes`**.
6. **`about_book`**.
7. **What the record shows:** a table of finding, where, evidence. Every claim in the
   texts traces to a row.
8. **Decisions:** the gaps, each with the draft line it would change, and what was left
   out on purpose.

### 9. STOP, G1: approve the texts

End the turn with the texts in your message: the description and hooks, `resumen`,
`forms`, `adaptations`, `parent_notes`, the `about_book` body, then the gaps and open
decisions, then the draft's path. Show the texts verbatim, not summarized.

**Ask in plain text, at the end of that message.** Do not open an AskUserQuestion
dialog here: text printed just before a dialog is hidden behind it.

Then iterate:
- **The user answers in the conversation:** change `metadata_draft.md` and
  `texts.json` together, re-run `check`, and show the changed texts again.
- **The user edits the draft file, or a copy of it:** read their file. Their wording is
  final: carry it into `texts.json` verbatim, smooth nothing, and tell them about
  anything `check` then refuses.
- **A gap gets fixed** (a note added, a passage edited): rebuild the EPUB, re-run
  `status` for the new count, and update the line that hedged.

Write nothing outside those two files until the user approves.

### 10. Write

```bash
python -X utf8 scripts/book_metadata.py apply --project <slug> --dry-run
python -X utf8 scripts/book_metadata.py apply --project <slug>
```

Relay the dry-run's `changes` before the live run, above all any `replaced` row: that
is a text the user wrote or approved earlier. `apply` writes `forms`, `adaptations`
and `parent_notes` to `project.json` and the page to `about_book.json`. It refuses
while `check` has errors.

Then the site, when `status.site.book_dir_exists` is true:

```bash
python -X utf8 scripts/book_metadata.py site --project <slug> --dry-run
python -X utf8 scripts/book_metadata.py site --project <slug>
```

`site` writes `adaptaciones.md` and `padres.md` from `project.json`, `descripcion.md`
from the description, and the `resumen` line of `libro.toml`. It touches nothing else
in `libro.toml`, and it never commits or pushes: that stays with the user, in the site
repo.

**When the book has no folder in the site repo,** `site` stops and says so. Creating a
book page (cover, release, ages, order) is a deliberate step there. Leave the
description, the `resumen` and the proposed `libro.toml` in the draft, and say that
`site` is ready to run once the folder exists.

### 11. Rebuild and hand off

Rebuild so the EPUB carries «Sobre este libro», unless another pass is still to come:

```bash
python scripts/harness.py epub --project <slug>
python scripts/harness.py epub --project <slug> --target libre
python -X utf8 scripts/book_metadata.py status --project <slug>
```

`status.epub.has_about_book` must now be true. If the book is headed for KDP, the
Kindle build still has to pass `kdp_publish.py verify-epub`.

Tell the user:
- what was written, and where;
- what is left to them: the commit and push in the site repo, a book folder to create,
  an EPUB to upload;
- the gaps still open.

**Update the guide.** When the user's feedback on this book changed how a text is
written, name each rule in one line and, with their yes, add it to
`prompts/book_metadata_guide.md` under the text it governs, with the date and the
book. That file is how the next book starts from where this one ended. When the user
has no copy of their own yet (`status.guide.kind` is `example`), offer to start one
from the example and this book's approved texts.

## Notes

- **`projects/` is gitignored, so Glob and Grep return nothing there.** Use Bash or
  `Read` with absolute paths.
- **Python from Git Bash cannot open `/c/Users/...` paths.** Pass `C:/Users/...`.
- **Temp files go in the scratchpad,** not under `../`, which lands outside the repo.
- **Never `cat > file` without a heredoc:** it waits on stdin until the timeout.
- **Rerunning is safe.** `status` and `check` write only `last_output.json`. `apply`
  and `site` report `unchanged` for a text already in place.
- **The Export tab's Website box** edits `forms`, `adaptations` and `parent_notes`
  too, through `/api/project/<id>/site-metadata`. A text the user changed there after
  the draft shows up as a `replaced` row in the dry-run: ask before overwriting it.
- **`harness.py site-export`** writes only `adaptaciones.md` and `padres.md`. `site`
  here does the same and adds the description and the `resumen`.
- **A book of several works** (a collection) has no flow here yet: draft per member.
- The per-user guide is `prompts/book_metadata_guide.md` (gitignored; tracked twin
  `book_metadata_guide.example.md`).

## Friction logs

Runs of this skill are logged in `.claude/skill-friction-logs/book-metadata/`. When the
user asks for a friction log, or a run wasted significant tokens or operator time and
they would plausibly want one, invoke the **`friction-log`** skill rather than
hand-rolling the file. It owns the location, naming and section skeleton. Those prior
logs are also the best standing record of this skill's known rough edges: read the
latest one or two when a run hits something that feels familiar.
