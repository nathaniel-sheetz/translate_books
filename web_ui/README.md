# Translation Web UI

Simple web interface for translating book chunks one-by-one.

## Quick Start

```bash
# Start the server
python app.py
```

Then open your browser to `http://localhost:5000`.

## Features

- 🎯 Auto-loads next untranslated chunk
- 📋 One-click prompt copying
- ⚡ Auto-advance after saving
- 📊 Visual progress tracking
- 🔄 Resume capability (progress saved to disk)
- ⌨️ Keyboard shortcuts (Ctrl+Enter, Ctrl+Shift+C)

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

## Documentation

See [WEB_UI_GUIDE.md](../WEB_UI_GUIDE.md) for complete documentation including:
- Detailed usage instructions
- Configuration options
- Troubleshooting guide
- Integration with existing workflow

## API Endpoints

### `POST /api/load-project`
Initialize translation session with chunks folder.

**Request:**
```json
{
  "chunks_dir": "chunks/",
  "glossary_path": "glossary.json",
  "style_guide_path": "style_guide.json",
  "include_context": true,
  "context_paragraphs": 2
}
```

**Response:**
```json
{
  "session_id": "abc123...",
  "total_chunks": 10,
  "completed_chunks": 3,
  "next_chunk": { ... }
}
```

### `GET /api/next-chunk?session_id=abc123`
Get next untranslated chunk with rendered prompt.

**Response:**
```json
{
  "chunk_id": "chapter_01_chunk_003",
  "position": 3,
  "total_chunks": 10,
  "chapter_id": "chapter_01",
  "source_text": "...",
  "word_count": 150,
  "paragraph_count": 3,
  "rendered_prompt": "...",
  "has_next": true
}
```

### `POST /api/save-translation`
Save translation and get next chunk.

**Request:**
```json
{
  "session_id": "abc123...",
  "chunk_id": "chapter_01_chunk_003",
  "translation": "Spanish translation..."
}
```

**Response:**
```json
{
  "saved": true,
  "next_chunk": { ... }
}
```

Or if all complete:
```json
{
  "saved": true,
  "all_complete": true,
  "total_chunks": 10
}
```

## Testing

Run the web UI backend tests:

```bash
pytest tests/test_web_ui.py -v
```

## Security Note

**This web UI is for local use only.**
- Runs on localhost (not accessible from network)
- No authentication
- Not suitable for public deployment

## Requirements

- Python 3.9+
- Flask 3.0+
- All dependencies from main `requirements.txt`
