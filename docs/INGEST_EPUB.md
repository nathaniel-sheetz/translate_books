# Ingest EPUB

`scripts/ingest_epub.py` converts a publisher EPUB into the same outputs as the
[Gutenberg importer](INGEST_GUTENBERG.md): a clean `source.txt`, the `headings.json`
outline the splitter anchors on, and the book's illustrations in `images/`. Along the
way it drops publisher and edition artifacts.

## Usage

```bash
# Dry run: show every spine document and whether it will be kept, and why
python scripts/ingest_epub.py "projects/mybook/source/Book.epub" --list

# Ingest
python scripts/ingest_epub.py "projects/mybook/source/Book.epub" --output projects/mybook/

# Override a decision (file name, path, or manifest id; repeatable)
python scripts/ingest_epub.py Book.epub --output projects/mybook/ \
    --keep-doc about-the-author.xhtml --drop-doc praise.xhtml

# Other switches
    --no-images              # placeholders only, no image files
    --footnotes import|drop  # default drop, as in the Gutenberg importer
    --no-recase              # keep "THE black yearling" lead-ins verbatim
    --boundary-images next   # plates between chapters open the next chapter
```

Keep the source EPUB **out of the project root** (e.g. in `source/`): a `*.epub` there
is taken to be the built translation by the Export tab, the download endpoint, and
retranslate snapshots.

**Harness:** `python scripts/harness.py setup --epub Book.epub` runs ingest and split in
one step. `--title` and `--author` default to the EPUB's own metadata, and `--keep-doc`
/ `--drop-doc` are accepted. Footnotes default to `import`, matching `--url`.

**Pipeline:** `python scripts/translate_book.py --epub Book.epub ...`

**Dashboard:** Stage 1 → **EPUB file** tab (upload).

## What gets dropped

Each spine document is classified, and the reason is recorded. Structural signals come
first and are trusted outright:

| Signal | Example |
|---|---|
| `linear="no"` in the spine | the cover page |
| The EPUB3 navigation document | `nav.xhtml` |
| OPF guide, nav landmarks, or `epub:type` naming cover, title page, copyright page, TOC, colophon, imprint, list of illustrations | `<reference type="toc">` |
| A TOC/NCX label naming an artifact | `TITLE PAGE`, `COPYRIGHT`, `Contents`, `Also by …` |

Content heuristics come next. They only apply to documents that hold none of the
book's TOC headings, so a short chapter can never be dropped by them:

| Heuristic | Rule |
|---|---|
| Copyright page | At least one strong signal (ISBN, "All rights reserved", ©, Library of Congress) and two signals in total (adds "published by"/"This edition", the `dc:publisher` name, a URL). Under 600 words. |
| Table of contents | At least 60% of text blocks link to other spine documents (minimum 3 links) |
| Title page | Before the first chapter, 50 words or fewer, contains the book title |
| Publisher back matter | After the last chapter, under 800 words, with publisher name, URL or "also available" |

Images are dropped when they appear only in dropped documents (logos, title-page art),
when the file name says spacer (`blank`, `spacer`, `pixel`, `transparent`), or when
either dimension is under 16 px. The book's cover is saved as `images/source_cover.*`.
That name is deliberate: the EPUB builder auto-uses `images/cover.jpg`, and this cover
carries the English title.

## What gets cleaned

- **Headings from the TOC.** Publisher EPUBs rarely use `<h1>`–`<h6>`. A title like
  InDesign's `<p class="cta">` is found by following each NCX/nav entry to its
  element. The fragment id is used when present; otherwise the importer matches the
  label text in the target file. The heading level is the TOC depth, or the h-tag
  level for real headings.
- **Subtitles are not split on.** A heading directly followed by a deeper one ("BUCEPHALUS"
  / "CHARGER OF ALEXANDER THE GREAT") is a title and subtitle. The subtitle stays in
  the text as the chapter's first line but is left out of `headings.json`. Otherwise
  the two levels tie, and the splitter's tie-break (deeper level wins) would split
  on subtitles.
- **Mid-word anchors.** `B<a id="…"></a>UCEPHALUS` becomes `BUCEPHALUS`.
- **Italics from CSS.** Any inline element whose class is `font-style: italic` in the
  book's stylesheet becomes `_underscored_`, as `<i>`/`<em>` do.
- **Paragraphs split across files.** A block with no terminal punctuation at the end
  of one file is rejoined with the next file's opening block when that block starts
  lowercase. A full-page illustration that fell between the halves moves after the
  rejoined paragraph. Verse is never joined.
- **Plates between chapters.** An image-only page that sits after a chapter's last
  page is kept with that chapter (moved above its last paragraph), matching print
  order. Left in place, the splitter would treat it as the next chapter's header
  ornament. Use `--boundary-images next` for books whose plates face the chapter they
  illustrate.
- **Drop-cap lead-ins.** A lead-in stored as literal capitals is recased. Examples:
  `THE black yearling` becomes `The black yearling`, and `JOAN OF ARC stood` becomes
  `Joan of Arc stood`. Each word takes the casing it has mid-sentence elsewhere in the
  book, so proper nouns survive. A word the book only ever writes in capitals (an
  acronym) is kept. Only the first paragraph after each heading is touched.
- **Unheaded front matter.** An epigraph or dedication before the first chapter with
  no heading of its own gets a synthetic one, so the splitter keeps it as front matter
  instead of discarding untitled pre-chapter text. The label comes from `epub:type`
  or class names (`Dedication`, `Epigraph`, `Preface`, …).
- **Page-break markers, noteref residue, soft hyphens, zero-width characters,
  ligature glyphs** are removed.

## Footnotes

The Gutenberg detector (back-linked notes) runs over the whole book at once. It
follows cross-file links such as `notes.xhtml#n1`. EPUB3 `epub:type="noteref"` links
to `footnote`/`endnote` elements are also recognized. A Notes page left empty after
extraction is removed. Import and translation then work exactly as described in
[INGEST_GUTENBERG.md § Footnotes](INGEST_GUTENBERG.md#footnotes---footnotes-import).

## Output

| Path | Contents |
|---|---|
| `source.txt` | Clean text with `[IMAGE:images/…]` placeholders |
| `headings.json` | Heading outline for the splitter |
| `images/` | Extracted illustrations, plus `source_cover.*` |
| `footnotes.json` | Note bodies (`--footnotes import` only) |
| `ingest_report.json` | Every decision: per-document keep/drop and reason, headings, subtitles, synthetic headings, dropped images, paragraph joins, anchored plates, recased lead-ins |

## Validating a new EPUB

1. Run `--list` and read the Keep column. Anything wrong can be fixed with
   `--keep-doc` / `--drop-doc`.
2. Ingest, then skim the end of the report (dropped documents, recased lead-ins).
3. Run `harness.py setup --project …` and check that `heading_outline.selected` is the
   chapter level and `ledger.unlocated` is 0.

Text the publisher got wrong is copied through unchanged, for example the scanning
errors in *Horses of Destiny* ("no hope of ,glory", "Odds … began at I to IO").
