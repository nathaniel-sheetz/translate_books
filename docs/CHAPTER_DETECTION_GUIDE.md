# Chapter Detection

Splitting a book into chapters is the first thing that has to go right — every later
stage is scoped per chapter. This document covers how detection works, the shipped
patterns, and what to do when a book doesn't match any of them.

The pattern registry lives in **`src/split_patterns.json`**. Everything below is read
from that file, so it stays true as patterns are added.

---

## Two detection strategies

### Heading outline (preferred)

Books ingested from Gutenberg carry an HTML heading outline, saved as `headings.json` at
ingest. When that outline exists and looks convincing, the splitter anchors on it
directly — no regex guessing. This is the most reliable path and is what `auto` picks
whenever it can.

`--heading-level` chooses which level holds the chapters (`h1`..`h6`, or a bare digit).
The `heading_outline.levels` table in the split output lists every candidate level with
its section count, so a wrong pick is a one-flag fix rather than a hand-written regex.

### Regex patterns (fallback)

For plain-text sources, or HTML whose outline isn't usable, the splitter scores each
named pattern against the text and picks the best fit.

---

## Shipped patterns

| Name | Matches | Numbering |
|---|---|---|
| `roman` | `Chapter I`, `Chapter II` on their own line | roman |
| `numeric` | `Chapter 1`, `Chapter 2` on their own line | numeric |
| `chapter_roman_titled` | `CHAPTER I. WATHO.` — roman numeral plus an optional inline title | roman |
| `chapter_numeric_titled` | `CHAPTER 1 — THE JOURNEY` — numeric plus an optional inline title | numeric |
| `allcaps_heading` | `ST. LOUIS`, `PEACE & WAR` — an all-caps line alone in its own paragraph | sequential |
| `bare_roman` | `I`, `II`, `III` alone on a line, with no "Chapter" | roman |

Plus three meta-values accepted by the `--chapter-pattern` flag:

- **`auto`** (default) — anchor on the heading outline if it looks convincing, otherwise
  score the regex patterns and pick the best.
- **`headings`** — force the outline path even if `auto` wouldn't have chosen it.
- **`custom`** — use your own regex via `--custom-regex`.

The titled variants capture the subtitle separately and write it as a second line in the
chapter file, which the EPUB builder renders as an `<h2>`.

### Detection order and thresholds

`detection_order` in the registry decides which patterns get tried first when scoring:
`chapter_roman_titled` → `chapter_numeric_titled` → `bare_roman` → `allcaps_heading`.
The titled variants come first because they are strictly more specific than the plain
ones — a book matching `chapter_roman_titled` also matches `roman`, but only the former
keeps the subtitles.

Two patterns carry a `detect_min_ratio` of `0.5`: `allcaps_heading` and `bare_roman`.
Both are dangerous generalists — an all-caps line could be a heading or could be emphatic
prose, and a bare `I` is a pronoun as often as a chapter number. The ratio requires that
at least half the candidate matches look structurally right before the pattern is
selected at all.

---

## Front matter, back matter, and boilerplate

The registry also classifies non-chapter sections by heading text:

| Category | Recognized headings |
|---|---|
| **Front matter** | Preface, Foreword, Prologue, Introduction, Note to the Reader, Dedication, Acknowledgments, Author's Note |
| **Back matter** | Epilogue, Afterword, Appendix, Colophon, Bibliography |
| **Dropped** | Contents, Table of Contents, List of Illustrations, Illustrations, Title Page, Copyright, Transcriber's Note |

Dropped sections are stripped by default; pass `--no-auto-strip` to keep them. Front and
back matter are kept but tagged, so the EPUB orders them correctly.

Override the classification per book when a heading doesn't match the patterns:

```bash
python scripts/harness.py split --project projects/my-book \
    --front-matter-title "A Word Before" \
    --back-matter-title "Notes on the Text"
```

`--no-auto-front-matter` / `--no-auto-back-matter` disable the keyword detection
entirely if it is doing more harm than good.

---

## Previewing before you commit

Always preview. `split-preview` runs the full detection and prints what it found without
writing a single file:

```bash
python scripts/harness.py split-preview --project projects/my-book
python scripts/harness.py split-preview --project projects/my-book --chapter-pattern bare_roman
```

When it looks right, run the same flags through `split`:

```bash
python scripts/harness.py split --project projects/my-book --chapter-pattern bare_roman
```

In the dashboard, Stage 2 does the same thing: **Preview** shows detected chapters with
word counts, **Confirm & Split** writes the files.

---

## When nothing matches

### Raise the minimum chapter size

Short false matches — a stray heading-looking line in the front matter — are usually
fixed by requiring more content per chapter:

```bash
python scripts/harness.py split-preview --project projects/my-book --min-chapter-size 500
```

Default is 100 characters.

### Write a custom regex

```bash
python scripts/harness.py split-preview --project projects/my-book \
    --chapter-pattern custom --custom-regex "^\s*PART\s+([IVX]+)\s*$"
```

Two traps worth knowing:

- **Custom regexes are compiled with `IGNORECASE` by default.** That means a class like
  `[A-Z][A-Z ]+` silently means "any run of letters and spaces" and will happily match
  ordinary prose paragraphs. Pass `--custom-regex-case-sensitive` (or wrap the pattern in
  `(?-i:...)`) when case is the whole point of the match.
- **The shell will mangle long alternations** containing quotes, `$`, `|`, or accented
  characters. Put the pattern in a file and use `--custom-regex-file` instead.

### Add a permanent pattern

If a shape recurs across books, add it to `src/split_patterns.json` rather than retyping
a custom regex. Each entry needs:

| Key | Purpose |
|---|---|
| `label` | Human-readable name shown in the dashboard's pattern dropdown |
| `regex` | The splitting pattern; capture group 1 is the number, group 2 (optional) the title |
| `flags` | List of `re` flag names, e.g. `["IGNORECASE", "MULTILINE"]` |
| `numbering` | `roman`, `numeric`, or `sequential` |
| `detect_regex` | Cheaper pattern used only for scoring during auto-detection |
| `detect_min_ratio` | `null`, or a threshold for generalist patterns that need proof |

Add the name to `detection_order` only if `auto` should consider it — specific patterns
before general ones. The dashboard's dropdown and the harness's `--chapter-pattern`
choices are both generated from this file, so no other change is needed.

---

## Re-splitting a book you already started

`split` rewrites `chapters/` from `source.txt`. That is safe before chunking, and
destructive after: chunks and translations are keyed to chapter IDs, so re-splitting a
partially-translated book will orphan work.

If you need to re-split after translating, treat it as a redo — see the retranslate
section of [`TRANSLATE_HARNESS.md`](TRANSLATE_HARNESS.md#resuming-redoing-repairing).

---

## Previous-chapter context

Once chapters exist, each chunk's translation prompt automatically receives the tail of
the previous chapter as `{{previous_chapter_context}}`, so pronouns, running jokes, and
character voice carry across a chapter boundary. This is handled for you on every backend
— there is nothing to pass.

---

## The standalone CLI

`scripts/split_book.py` predates the registry and exposes only `roman`, `numeric`, and
`custom`. It has no heading-outline support and no titled variants. Prefer the harness or
the dashboard for anything but a plain "Chapter I" book. See
[`CLI_REFERENCE.md`](CLI_REFERENCE.md).

---

## Related

| Document | Contents |
|---|---|
| [`INGEST_GUTENBERG.md`](INGEST_GUTENBERG.md) | Where `headings.json` comes from |
| [`TRANSLATE_HARNESS.md`](TRANSLATE_HARNESS.md) | `split-preview` / `split` in the pipeline |
| [`WEB_UI_GUIDE.md`](WEB_UI_GUIDE.md) | Stage 2 in the dashboard |
| [`CHUNKING_GUIDE.md`](CHUNKING_GUIDE.md) | What happens to chapters next |
