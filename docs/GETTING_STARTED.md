# Getting Started

This guide walks you through translating your first book using the web dashboard.

## Prerequisites

```bash
pip install -r requirements.txt
```

For API translation, copy `.env.example` to `.env` and add your `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.

## Step 0: Create a Project

Projects are directories under `projects/`. You can create one ahead of time or let the dashboard do it:

```bash
# Option A: Start from scratch — the dashboard will create the directory
# Just navigate to http://localhost:5000/project/my-book

# Option B: CLI — Gutenberg ingestor (downloads + cleans HTML, extracts images)
python scripts/ingest_gutenberg.py https://www.gutenberg.org/files/41350/41350-h/41350-h.htm \
    --output projects/my-book/

# Option C: Manual
mkdir -p projects/my-book
cp my_book.txt projects/my-book/source.txt
```

## Step 1: Start the Server

```bash
cd web_ui && python app.py
```

Open `http://localhost:5000/project/my-book` in your browser. You'll see the pipeline dashboard with 8 stages in a vertical stepper on the left.

## Step 2: Source

If you already have `source.txt` in your project directory, this stage shows a preview and word count. Otherwise, the Source stage offers two import modes via a tab toggle:

### File / Paste

- **Upload** a `.txt` file via drag-and-drop
- **Paste** text into the textarea

Click "Upload Source" and the source is written to `projects/my-book/source.txt`.

### Gutenberg URL

Import directly from Project Gutenberg without leaving the dashboard:

1. Click the **Gutenberg URL** tab
2. Paste a Gutenberg HTML URL (e.g. `https://www.gutenberg.org/files/41350/41350-h/41350-h.htm`)
3. Click **Import from Gutenberg**

The system fetches the page, strips boilerplate, converts to clean text, and downloads images. After import you'll see a chapter report with word counts, and the detected heading pattern is automatically applied to the Split stage.

## Step 3: Split into Chapters

Configure the chapter detection pattern:

- **Roman** — "Chapter I", "Chapter II", etc. (default)
- **Numeric** — "Chapter 1", "Chapter 2", etc.
- **Bare Roman** — Just "I", "II" on their own line
- **Custom** — Your own regex

Click **Preview** to see detected chapters with word counts. If it looks right, click **Confirm & Split** to write the chapter files.

## Step 4: Chunk Chapters

Configure chunking parameters (defaults are usually fine):

- **Target size**: ~2000 words per chunk
- **Overlap paragraphs**: 0 (overlap disabled by default)

Click **Chunk All** to break every chapter into translation-sized JSON chunks.

## Step 5: Style Guide

The style guide tells the LLM how to translate — formality, tone, regional variant, etc.

1. Answer the fixed questions (radio buttons for register, dialect, era, etc.)
2. Optionally generate additional LLM-powered questions: click **Generate Questions**, copy the prompt into your LLM, paste the response back
3. Click **Generate Style Guide** — this creates a prompt you copy/paste through your LLM, or uses a built-in fallback if you prefer not to use an LLM for this step
4. The style guide is saved to `projects/my-book/style.json`

## Step 6: Glossary

The glossary ensures consistent translation of names, places, and terms.

1. Select which style guide Q&A pairs to include as context
2. Click **Extract Candidates** — the system scans your source text for proper nouns and recurring terms
3. Copy the generated prompt, paste into your LLM, paste the response back
4. Review the proposals table — accept or reject each term
5. Click **Save Glossary** to write `projects/my-book/glossary.json`

## Step 7: Translate

This is the main work stage. You'll see a table of all chapters with translation status.

### Manual translation (per chunk)

1. Click a chapter row to expand it
2. Click a chunk tab to see its content
3. Click **Copy Prompt** to copy the fully rendered translation prompt (includes style guide, glossary, previous chunk context)
4. Paste into your LLM, copy the response
5. Paste the translation and click **Save**

### Batch API translation

1. Check the boxes next to chapters you want to translate
2. Click **Batch Translate Selected**
3. Choose provider (Anthropic/OpenAI) and model
4. Review the cost estimate
5. Click **Start** — progress updates in real-time via SSE

## Step 8: Review

After translation, align and review:

1. Click **Combine + Align** on a translated chapter — this merges chunks and runs sentence alignment
2. Click **Read** to open the bilingual reader in a new tab

In the reader (`/read/my-book/chapter_01`):
- Tap any Spanish sentence to see the English source below
- Edit translations inline
- Add annotations (word choice, inconsistency, footnote, flag)
- Mark chapters as reviewed when you're satisfied

## Step 9: Export

Build a downloadable EPUB from your translated chapters.

1. The stage shows how many chapters will be included (only fully-translated chapters are packaged)
2. Title and author are pre-filled from your project config — edit if needed
3. Click **Build EPUB** — the system auto-combines chunks and builds the EPUB with embedded images
4. Click **Download** to save the file, or find it at `projects/my-book/my-book.epub`

## What's Next

- Translate remaining chapters (batch or manual)
- Use the reader to review and annotate
- Apply corrections if needed (banner appears on chapter list)
- Re-export the EPUB after making changes

## CLI Alternative

Every dashboard stage has a CLI equivalent. See the [README](../README.md#cli-workflow) for commands and [`docs/BATCH_PIPELINE.md`](BATCH_PIPELINE.md) for batch processing.
