# Translation Web UI Guide

A simple web interface for translating book chunks one-by-one with automatic progress tracking.

## Overview

The web UI streamlines the manual translation workflow by:
- Auto-loading the next untranslated chunk
- Displaying complete prompts with one-click copy
- Saving translations directly to chunk JSON files
- Auto-advancing to the next chunk after save
- Tracking progress across sessions

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Start the Server

```bash
cd web_ui
python app.py
```

The server will start on `http://localhost:5000`.

### 3. Open in Browser

Navigate to `http://localhost:5000` in your web browser.

## Usage Workflow

### Step 1: Project Setup

When you first load the UI, you'll see the project setup form:

**Required:**
- **Chunks Folder**: Path to folder containing chunk JSON files (e.g., `chunks/`)

**Optional:**
- **Project Name**: Name displayed in the UI (default: "Translation Project")
- **Source Language**: Source language (default: "English")
- **Target Language**: Target language (default: "Spanish")
- **Glossary**: Path to glossary JSON file (e.g., `glossary.json`)
- **Style Guide**: Path to style guide JSON file (e.g., `style_guide.json`)

**Previous Chapter Context** (optional):
- Check "Include previous chapter context" to add continuity context
- Provide path to previous chapter's translated text
- Set number of paragraphs to include from end of previous chapter

Click **Load Project** to initialize the session.

### Step 2: Translate Chunks

Once loaded, the UI shows:

1. **Chunk Information**: Chapter, position, word count, paragraph count
2. **Progress Bar**: Visual indicator of completion (e.g., "Chunk 3 of 10")
3. **Prompt Display**: Complete rendered prompt ready to copy
4. **Translation Input**: Textarea for pasting translations

**Translation Workflow:**

1. Click **Copy to Clipboard** to copy the prompt
2. Paste the prompt into your LLM (Claude.ai, ChatGPT, etc.)
3. Copy the LLM's translation response
4. Paste the translation into the "Paste Translation Here" textarea
5. Click **Save & Continue**

The UI will:
- Save the translation to the chunk's JSON file
- Update the chunk status to "translated"
- Immediately load the next untranslated chunk

### Step 3: Completion

When all chunks are translated, the UI displays a completion message:

```
🎉 All Chunks Completed!
All 10 chunks have been translated.
```

You can click **Start New Project** to reload the page and begin a new translation session.

## Features

### Automatic Progress Tracking

Progress is tracked via the chunk JSON files on disk:
- Chunks with `translated_text` are considered complete
- The UI always loads the first chunk where `translated_text` is null/empty
- If you close the browser and reopen the UI, it picks up where you left off

### Keyboard Shortcuts

- **Ctrl/Cmd + Enter**: Submit translation (when textarea is focused)
- **Ctrl/Cmd + Shift + C**: Copy prompt to clipboard

### Previous Chapter Context

When enabled, the UI includes the last N paragraphs from the previous chapter's translation in each prompt. This provides continuity context for the LLM.

Example use case:
```
Chunks folder: chunks/chapter_02/
Previous chapter: chapters/translated/chapter_01.txt
Context paragraphs: 2
```

The prompt for the first chunk of Chapter 2 will include the last 2 paragraphs from Chapter 1's translation.

## File Structure

```
web_ui/
├── app.py              # Flask backend server
├── templates/
│   └── index.html      # Main UI page
└── static/
    ├── app.js          # Frontend JavaScript logic
    └── style.css       # Styling
```

## Configuration

### Changing the Port

Edit `app.py` and change the port in the last line:

```python
app.run(debug=True, port=5000)  # Change 5000 to your preferred port
```

### Custom Prompt Template

The UI uses the prompt template from `prompts/translation.txt`. To customize:

1. Edit `prompts/translation.txt`
2. Restart the server
3. Reload the project in the UI

Changes to the template take effect immediately for new chunks.

## Troubleshooting

### "No chunk files found in {folder}"

- Verify the chunks folder path is correct
- Ensure the folder contains `.json` files
- Check that the JSON files are valid chunk files (created by chunking scripts)

### "Invalid or expired session"

- This happens if the server restarts while you're translating
- Click "Start New Project" to create a new session
- Your progress is safe - translations are saved to disk immediately

### "Failed to load glossary/style guide"

- Verify the file path is correct
- Check that the file is valid JSON
- Check that the JSON matches the expected schema (Glossary or StyleGuide)

### Browser compatibility issues

- The UI uses modern JavaScript (async/await, fetch)
- Requires a recent browser version (Chrome 55+, Firefox 52+, Safari 11+)
- Clipboard API requires HTTPS or localhost

## Example Session

### Translating Chapter 2 of a Book

1. **Prepare files:**
   ```
   chunks/chapter_02_chunk_000.json
   chunks/chapter_02_chunk_001.json
   chunks/chapter_02_chunk_002.json
   chapters/translated/chapter_01.txt  (previous chapter)
   glossary.json
   style_guide.json
   ```

2. **Start server:**
   ```bash
   cd web_ui
   python app.py
   ```

3. **Configure in browser:**
   - Chunks Folder: `chunks/`
   - Project Name: `My Book - Chapter 2`
   - Glossary: `glossary.json`
   - Style Guide: `style_guide.json`
   - ✅ Include previous chapter context
   - Previous Chapter: `chapters/translated/chapter_01.txt`
   - Context Paragraphs: `2`

4. **Translate:**
   - UI loads chunk 000
   - Copy prompt → paste into Claude.ai
   - Copy translation → paste into UI
   - Click "Save & Continue"
   - UI automatically loads chunk 001
   - Repeat until all chunks complete

5. **Result:**
   - All chunk JSON files updated with translations
   - Status set to "translated"
   - Timestamps recorded
   - Ready to combine chunks into final chapter

## Integration with Existing Workflow

The web UI integrates seamlessly with the existing manual workflow:

### Before Web UI

```bash
# 1. Generate workbook
python generate_workbook.py

# 2. Manually copy/paste prompts and translations

# 3. Import completed workbook
python import_workbook.py workbook.md --output chunks/translated/
```

### With Web UI

```bash
# 1. Start web UI
cd web_ui && python app.py

# 2. Open browser and translate (no file juggling!)

# 3. Chunks are already saved - combine directly
python combine_chunks.py chunks/ --output chapters/translated/chapter_02.txt
```

## Comparison: Workbook vs Web UI

| Feature | Workbook | Web UI |
|---------|----------|--------|
| Setup time | Low | Low |
| Copy/paste steps | 2 per chunk | 2 per chunk |
| Progress tracking | Manual | Automatic |
| Context switching | High (file → LLM → file) | Low (same window) |
| Resume capability | Manual (find your place) | Automatic |
| Multi-chapter | One workbook per chapter | Load any chapter |
| Best for | Small projects (1-10 chunks) | Large projects (10+ chunks) |

## Tips for Efficient Translation

1. **Use a second monitor**: Keep the web UI on one screen and your LLM on the other

2. **Keyboard shortcuts**: Learn Ctrl+Enter (submit) and Ctrl+Shift+C (copy) for faster workflow

3. **Browser zoom**: Adjust zoom level (Ctrl/Cmd + +/-) for comfortable reading

4. **Save regularly**: The UI auto-saves, but keep your browser tab open to maintain session

5. **Review before submitting**: Quickly scan the translation for obvious errors before clicking "Save & Continue"

6. **Use previous chapter context**: Enable context for better continuity, especially at chapter boundaries

7. **Batch sessions**: Translate multiple chunks in one sitting to maintain consistency and flow

## Advanced Usage

### Multiple Projects

To work on multiple books/chapters simultaneously:

1. Open multiple browser tabs
2. Load different chunks folders in each tab
3. Each tab maintains its own session

### Custom Session Management

The server stores sessions in memory. To persist sessions across server restarts, you could:

1. Store session data in JSON files
2. Use a lightweight database (SQLite)
3. Implement session recovery from chunk progress

(These features are not implemented by default - the simple in-memory approach works well for local use.)

### API-Only Usage

You can also use the API directly without the web UI:

```python
import requests

# Load project
response = requests.post('http://localhost:5000/api/load-project', json={
    'chunks_dir': 'chunks/',
    'glossary_path': 'glossary.json'
})
session_id = response.json()['session_id']

# Get next chunk
response = requests.get(f'http://localhost:5000/api/next-chunk?session_id={session_id}')
chunk = response.json()

# Save translation
response = requests.post('http://localhost:5000/api/save-translation', json={
    'session_id': session_id,
    'chunk_id': chunk['chunk_id'],
    'translation': 'La traducción...'
})
```

This allows integration with automated translation scripts or custom UIs.

## Security Note

**This web UI is intended for local use only.**

- Runs on localhost (not accessible from network)
- No authentication or authorization
- Session secrets are generated randomly each run
- Not suitable for deployment to public servers

If you need to deploy this for team use:
1. Add authentication (Flask-Login, OAuth)
2. Use a proper session store (Redis, PostgreSQL)
3. Add HTTPS support
4. Implement proper error handling and logging
5. Add rate limiting and input validation

## Support

For issues or questions:
1. Check this guide
2. Review the plan document for implementation details
3. Check the Flask logs in the terminal
4. Inspect browser console for JavaScript errors

## Future Enhancements

Potential improvements (not implemented):

- **Direct LLM API integration**: Call Claude/OpenAI APIs directly from UI
- **Translation memory**: Suggest translations from similar chunks
- **Real-time evaluation**: Show quality scores as you type
- **Collaborative mode**: Multiple translators on same project
- **Export options**: Generate final chapters directly from UI
- **Dark mode**: For late-night translation sessions
- **Undo/redo**: Revert to previous translations
- **Keyboard-only mode**: Full keyboard navigation
