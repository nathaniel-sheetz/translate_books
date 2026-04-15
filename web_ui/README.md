# web_ui/

Flask web application providing the pipeline dashboard and bilingual reader.

## Quick Start

```bash
python app.py
# Open http://localhost:5000/project/<project_id>
```

## File Structure

```
web_ui/
├── app.py              # All routes and API endpoints
├── i18n.py             # Server-side EN/ES translations
├── templates/
│   ├── dashboard.html  # Pipeline wizard (8-stage stepper)
│   ├── reader.html     # Bilingual reader + project/chapter lists
│   └── chunk_edit.html # Full-textarea chunk editor
└── static/
    ├── dashboard.js    # Dashboard stage logic, batch SSE, prompts
    ├── dashboard.css   # Dashboard layout and styles
    ├── reader.js       # Reader interactions, annotations, corrections
    ├── reader.css      # Reader serif/reading styles
    ├── chunk_edit.js   # Chunk editor save flow + caret positioning
    ├── chunk_edit.css  # Chunk editor layout
    ├── setup.js        # Style guide + glossary wizard logic
    └── setup.css       # Setup wizard styles
```

## Key Routes

| Route | Template | Purpose |
|---|---|---|
| `/` | — | Redirects to `/read/` |
| `/read/` | reader.html | Project list |
| `/project/<id>` | dashboard.html | Pipeline dashboard |
| `/read/<id>` | reader.html | Chapter list |
| `/read/<id>/<ch>` | reader.html | Bilingual reader |
| `/read/<id>/<ch>/chunk/<chunk_id>/edit` | chunk_edit.html | Full-chunk text editor |

## API Endpoints

### Dashboard (`/api/project/<id>/...`)

- `GET /status` — Full project status from filesystem
- `POST /ingest` — Upload/paste source text
- `POST /ingest-gutenberg` — Import from Gutenberg URL (fetches HTML, strips boilerplate, downloads images)
- `POST /split/preview` — Dry-run chapter detection
- `POST /split` — Execute chapter split
- `POST /chunk-all` — Chunk all chapters
- `GET /chapters/<ch>/chunks` — List chunks with status
- `GET /chunks/<chunk_id>/prompt` — Rendered translation prompt
- `POST /chunks/<chunk_id>/translate` — Save manual translation (recombines + realigns + re-anchors annotations)
- `POST /translate/cost-estimate` — Estimate batch cost
- `POST /translate/realtime` — Single-chunk API translation (recombines + realigns + re-anchors annotations)
- `POST /translate/batch` — Start batch translation (returns job_id)
- `GET /translate/sse?job_id=...` — SSE progress stream
- `POST /combine/<ch>` — Combine chunks into chapter
- `POST /align/<ch>` — Run sentence alignment

### Setup (`/api/setup/<id>/...`)

- `POST /prompts/questions` — Generate LLM questions prompt
- `POST /prompts/style-guide` — Generate style guide prompt
- `POST /style-guide` — Save style guide
- `POST /style-guide/fallback` — Generate without LLM
- `POST /extract-candidates` — Extract glossary candidates
- `POST /prompts/glossary` — Generate glossary prompt
- `POST /glossary` — Save glossary

### Reader

- `GET /api/alignment/<id>/<ch>` — Alignment data
- `POST /api/correction` — Save correction
- `GET/POST/DELETE /api/annotations/<id>/<ch>` — Annotations
- `GET/POST/DELETE /api/reviewed/<id>/<ch>` — Reviewed status
- `POST /api/apply-corrections/<id>` — Batch apply corrections
- `POST /api/chunk/<id>/<chunk_id>/edit` — Save a full-chunk text edit (recombines + realigns)

## Documentation

See [`docs/WEB_UI_GUIDE.md`](../docs/WEB_UI_GUIDE.md) for full reference.

## Tests

```bash
pytest tests/test_web_ui.py -v
```
