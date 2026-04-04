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
```

## What it does

1. **Fetch** — Downloads the HTML from a URL or reads a local file.
2. **Strip boilerplate** — Removes Project Gutenberg header/footer content. Handles both the newer `<section class="pg-boilerplate">` format and the older `*** START/END OF THE PROJECT GUTENBERG ***` text-marker format.
3. **Convert to plain text** — Walks the HTML tree, dropping navigation, scripts, page-number spans, and other non-content elements. Block elements become double-newline-separated paragraphs; headings are preserved as plain text.
4. **Handle images** — Downloads each image into `<output>/images/` and inserts a `[IMAGE:images/filename.jpg]` placeholder at the same position in the text. These placeholders survive chunking and translation for later re-insertion. Use `--no-images` to skip downloading while keeping placeholders.
5. **Write output** — Saves the cleaned text to `<output>/source.txt`.
6. **Report** — Prints a chapter-by-chapter word count table and estimates how many translation chunks each chapter will produce (at ~2,000 words/chunk). Also suggests an appropriate `--pattern` flag for `split_book.py` based on the detected heading style (numeric, Roman numeral, or bare Roman numeral).

## Output

| Path | Contents |
|------|----------|
| `<output>/source.txt` | Clean plain text, ready for `split_book.py` |
| `<output>/images/` | Downloaded images (unless `--no-images`) |

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
