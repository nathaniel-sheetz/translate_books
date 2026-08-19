# web_ui/

Flask application providing the pipeline dashboard and the bilingual reader.

## Quick start

```bash
# From the repo root — not from inside web_ui/
python -m web_ui.app

# With auto-reload and the Werkzeug debugger (never for a service)
BOOKS_DEBUG=1 python -m web_ui.app
```

Open `http://localhost:5000`. `app.py` imports `web_ui.i18n`, so it must run as a module
from the repo root; `cd web_ui && python app.py` fails on that import.

For the always-on service (waitress + rotating logs, driven by the
`TranslateBooksReader` scheduled task) use `python scripts/serve.py` — see
[`scripts/reader.ps1`](../scripts/reader.ps1).

## File structure

```
web_ui/
├── app.py                      # All routes and API endpoints
├── i18n.py                     # Server-side EN/ES translations
├── templates/
│   ├── dashboard.html          # Pipeline wizard (8-stage stepper)
│   ├── reader.html             # Bilingual reader + project/chapter lists
│   ├── chunk_edit.html         # Full-textarea chunk editor
│   └── edit_review_report.html.j2  # Edit-review HTML report (Jinja2)
└── static/
    ├── dashboard.js/.css       # Dashboard stage logic, batch SSE, prompts
    ├── reader.js/.css          # Reader interactions, annotations, corrections
    ├── reader_sheet_v2.js/.css # Bottom-sheet surface
    ├── reader_projects.js      # Project list
    ├── reader_chapters.js      # Chapter list
    ├── concordance.js/.css     # "Find in book" search surface
    ├── chunk_edit.js/.css      # Chunk editor save flow + caret positioning
    ├── setup.js/.css           # Style guide + glossary wizard
    ├── edit_review_report.css  # Styles for the edit-review report
    └── manifest.webmanifest    # PWA manifest
```

## Key routes

| Route | Template | Purpose |
|---|---|---|
| `/` | — | Redirects to `/read/` |
| `/read/` | reader.html | Project list |
| `/project/<id>` | dashboard.html | Pipeline dashboard |
| `/read/<id>` | reader.html | Chapter list |
| `/read/<id>/<ch>` | reader.html | Bilingual reader |
| `/read/<id>/<ch>/chunk/<chunk_id>/edit` | chunk_edit.html | Full-chunk text editor |

## API surface

`app.py` defines 91 routes — 82 under `/api/`, plus the 9 page routes in the table
above. By prefix:

| Prefix | Count | Covers |
|---|---|---|
| `/api/project/<id>/...` | 45 | Dashboard stages (ingest, split, chunk, translate + SSE, combine, align, export), plus judge and review runs |
| `/api/setup/<id>/...` | 13 | Style guide and glossary wizards |
| `/api/alignment`, `/api/annotation(s)`, `/api/reviewed`, `/api/correction`, `/api/apply-corrections` | 9 | Reader data and review state |
| `/api/llm-config`, `/api/llm/models`, `/api/split-patterns`, `/api/edit-tags`, `/api/set-*` | 8 | Config and UI preferences |
| `/api/sentence/...`, `/api/chunk/...`, `/api/remove-text`, `/api/removal-context` | 5 | Per-sentence and per-chunk editing |
| `/api/search/<project_id>` | 1 | Concordance ("Find in book") |
| `/api/projects/create` | 1 | Project creation |

The list is deliberately not enumerated here — it drifts. To see the live surface:

```bash
python -c "from web_ui.app import app; [print(r.rule) for r in sorted(app.url_map.iter_rules(), key=lambda r: r.rule)]"
```

Behavior for each stage is documented in
[`docs/WEB_UI_GUIDE.md`](../docs/WEB_UI_GUIDE.md).

## Tests

```bash
pytest tests/test_web_ui.py -v
```
