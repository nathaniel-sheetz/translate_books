# Ingest Gutenberg

`scripts/ingest_gutenberg.py` converts a Project Gutenberg HTML book into a clean `source.txt` ready for the translation pipeline.

## Usage

```bash
# From a URL
python scripts/ingest_gutenberg.py https://www.gutenberg.org/files/41350/41350-h/41350-h.htm \
    --output projects/mybook/

# From a local file
python scripts/ingest_gutenberg.py local_book.htm --output projects/mybook/

# Skip downloading images (placeholders still inserted)
python scripts/ingest_gutenberg.py URL --output projects/mybook/ --no-images

# Import footnotes as translatable reader footnotes (default: drop)
python scripts/ingest_gutenberg.py URL --output projects/mybook/ --footnotes import
```

## What it does

1. **Fetch** — Downloads the HTML from a URL or reads a local file.
2. **Strip boilerplate** — Removes Project Gutenberg header/footer content. Handles both the newer `<section class="pg-boilerplate">` format and the older `*** START/END OF THE PROJECT GUTENBERG ***` text-marker format.
3. **Convert to plain text** — Walks the HTML tree, dropping navigation, scripts, page-number spans, and other non-content elements. Block elements become double-newline-separated paragraphs; headings are preserved as plain text. `<i>` and `<em>` tags are converted to `_word_` underscore markers so italics survive through chunking and LLM translation; the EPUB builder converts these markers back to `<em>` tags at export time.
4. **Handle images** — Downloads each image into `<output>/images/` and inserts a `[IMAGE:images/filename.jpg]` placeholder at the same position in the text. These placeholders survive chunking and translation for later re-insertion. Use `--no-images` to skip downloading while keeping placeholders.
5. **Handle footnotes** — Detects Gutenberg footnotes (an inline reference anchor linked to a definition that back-links to it — the detector keys on that structure, not on class names, so both the modern "images" format and the older `files/` format work). Footnotes are always counted and reported. With `--footnotes import`, each reference becomes a survivable `[FOOTNOTE:N]` token (like `[IMAGE:...]`) at the exact marker position and the note bodies are written to `<output>/footnotes.json`; the tokens ride through translation and are later converted into editable reader footnote annotations. Without it (the default `drop`), references and note bodies are removed cleanly, leaving no `[1]` residue.
6. **Write output** — Saves the cleaned text to `<output>/source.txt`.
7. **Report** — Prints a chapter-by-chapter word count table and estimates how many translation chunks each chapter will produce (at ~2,000 words/chunk). Also suggests an appropriate `--pattern` flag for `split_book.py` based on the detected heading style (numeric, Roman numeral, or bare Roman numeral).

## Footnotes (`--footnotes import`)

Importing footnotes spans three steps:

1. **Ingest** with `--footnotes import` — captures `[FOOTNOTE:N]` tokens + `footnotes.json`.
2. **Translate the note bodies** (whole book, any time after ingest):
   ```bash
   python scripts/translate_footnotes.py --project-dir projects/mybook
   ```
3. **Convert** (the `footnotes` stage of `translate_book.py`, run after `align`) — turns each surviving token into a `type:"footnote"` annotation in `annotations.jsonl`, anchored to the preceding word, then rebuilds the EPUB so the existing endnote machinery embeds them. The full pipeline runs this automatically; footnotes then appear as editable annotations in the reader and as endnotes in the EPUB. `translate_book.py` also accepts `--footnotes import`.

## Output

| Path | Contents |
|------|----------|
| `<output>/source.txt` | Clean plain text, ready for `split_book.py` |
| `<output>/images/` | Downloaded images (unless `--no-images`) |
| `<output>/footnotes.json` | Imported footnote bodies (only with `--footnotes import`) |

## Dependencies

```bash
pip install requests beautifulsoup4
```

## Next step

Feed `source.txt` into `split_book.py`:

```bash
python scripts/split_book.py projects/mybook/source.txt \
    --output projects/mybook/chapters/ --pattern roman
```
