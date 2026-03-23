# Web UI Implementation Plan

## Overview

A browser-based interface for the manual book translation workflow that eliminates command-line complexity while maintaining the copy/paste approach for LLM translation.

**Key Principle**: Keep the manual LLM workflow (no API keys required) but make it easier through a guided web interface.

## User Journey

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. SETUP                                                        │
│    - Upload/paste source chapter                               │
│    - Optional: Upload glossary JSON                            │
│    - Configure chunking parameters                             │
├─────────────────────────────────────────────────────────────────┤
│ 2. CHUNK PREVIEW                                                │
│    - Review auto-generated chunks                              │
│    - See chunk boundaries, overlap regions                     │
│    - Adjust parameters if needed                               │
├─────────────────────────────────────────────────────────────────┤
│ 3. TRANSLATE (for each chunk)                                   │
│    - View chunk with formatting                                │
│    - One-click copy full prompt                                │
│    - Paste LLM translation response                            │
│    - Auto-save progress                                        │
│    - Move to next chunk                                        │
├─────────────────────────────────────────────────────────────────┤
│ 4. EVALUATE                                                     │
│    - Run evaluations on completed chunks                       │
│    - View results inline with issues highlighted              │
│    - Fix and re-evaluate if needed                            │
├─────────────────────────────────────────────────────────────────┤
│ 5. EXPORT                                                       │
│    - Download combined translation                             │
│    - Download individual chunks (JSON)                         │
│    - Download evaluation reports (HTML/JSON)                   │
└─────────────────────────────────────────────────────────────────┘
```

## Architecture

### Technology Stack

**Frontend (Client-Heavy Architecture)**
- **Framework**: React or Vue.js (React recommended for component ecosystem)
- **State Management**: Zustand or React Context (for session state)
- **Storage**: Browser localStorage/IndexedDB for work-in-progress
- **Styling**: Tailwind CSS (rapid development, modern look)
- **Python Runtime**: Pyodide (Python in WebAssembly for browser-based evaluation)

**Backend (Minimal, Optional)**
- **Option A**: Pure client-side (Pyodide for all Python code)
- **Option B**: Lightweight Flask/FastAPI backend for Python evaluation logic
- **Option C**: Hybrid - UI in browser, evaluations via local Python server

**Recommended**: Start with **Option B** - Flask backend for simplicity and reliability.

### Component Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Frontend (React)                                                │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│  │  Setup       │  │  Translate   │  │  Evaluate    │         │
│  │  Component   │─▶│  Component   │─▶│  Component   │         │
│  └──────────────┘  └──────────────┘  └──────────────┘         │
│         │                  │                  │                │
│         └──────────────────┴──────────────────┘                │
│                            │                                   │
│                   ┌────────▼────────┐                          │
│                   │  State Manager  │                          │
│                   │  (Translation   │                          │
│                   │   Session)      │                          │
│                   └────────┬────────┘                          │
│                            │                                   │
│                   ┌────────▼────────┐                          │
│                   │  localStorage   │                          │
│                   └─────────────────┘                          │
└─────────────────────────────┬───────────────────────────────────┘
                              │ HTTP API
┌─────────────────────────────▼───────────────────────────────────┐
│ Backend (Flask/FastAPI)                                         │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│  │  /api/chunk  │  │ /api/evaluate│  │ /api/combine │         │
│  └──────────────┘  └──────────────┘  └──────────────┘         │
│         │                  │                  │                │
│         └──────────────────┴──────────────────┘                │
│                            │                                   │
│                   ┌────────▼────────┐                          │
│                   │  Existing src/  │                          │
│                   │  - chunker.py   │                          │
│                   │  - translator.py│                          │
│                   │  - evaluators/  │                          │
│                   │  - combiner.py  │                          │
│                   └─────────────────┘                          │
└─────────────────────────────────────────────────────────────────┘
```

## Detailed Feature Specifications

### 1. Setup Page

**Layout:**
```
┌─────────────────────────────────────────────────┐
│ 📖 Book Translation Workflow                    │
├─────────────────────────────────────────────────┤
│                                                 │
│ Step 1: Prepare Your Translation               │
│                                                 │
│ ┌─────────────────────────────────────────┐   │
│ │ Project Name:                           │   │
│ │ [________________]                      │   │
│ └─────────────────────────────────────────┘   │
│                                                 │
│ ┌─────────────────────────────────────────┐   │
│ │ Source Text (English):                  │   │
│ │ ┌───────────────────────────────────┐   │   │
│ │ │ Paste your chapter here...        │   │   │
│ │ │                                   │   │   │
│ │ │                                   │   │   │
│ │ └───────────────────────────────────┘   │   │
│ │ Or: [Upload .txt file]                  │   │
│ │                                         │   │
│ │ 1,847 words • 42 paragraphs            │   │
│ └─────────────────────────────────────────┘   │
│                                                 │
│ ┌─────────────────────────────────────────┐   │
│ │ Glossary (Optional):                    │   │
│ │ [Upload glossary.json] or [Skip]       │   │
│ │                                         │   │
│ │ ✓ 8 terms loaded                       │   │
│ └─────────────────────────────────────────┘   │
│                                                 │
│ ┌─────────────────────────────────────────┐   │
│ │ Chunking Settings:          [Advanced]  │   │
│ │                                         │   │
│ │ Target size: [1500] words per chunk    │   │
│ │ Overlap:     [2] paragraphs            │   │
│ │                                         │   │
│ │ Estimated chunks: 2                    │   │
│ └─────────────────────────────────────────┘   │
│                                                 │
│           [Continue to Chunk Preview →]        │
│                                                 │
└─────────────────────────────────────────────────┘
```

**Features:**
- Live word/paragraph counter as user types
- Glossary file validation (show term count on upload)
- Collapsible "Advanced" settings for chunking parameters
- Auto-save to localStorage
- Estimated chunk count calculation

**API Endpoints:**
```
POST /api/validate-glossary
  Input: JSON file
  Output: { valid: bool, term_count: int, errors: [] }

POST /api/estimate-chunks
  Input: { text: string, target_size: int, overlap: int }
  Output: { chunk_count: int, avg_chunk_size: int }
```

### 2. Chunk Preview Page

**Layout:**
```
┌─────────────────────────────────────────────────┐
│ Step 2: Review Chunks                           │
├─────────────────────────────────────────────────┤
│                                                 │
│ ┌─────────────────────────────────────────┐   │
│ │ Chunk 1 of 2                            │   │
│ │ ─────────────────────────────────────   │   │
│ │                                         │   │
│ │ Sara stood near her father and          │   │
│ │ listened while he and Miss Minchin...   │   │
│ │                                         │   │
│ │ [Overlap region highlighted]            │   │
│ │                                         │   │
│ │ 1,456 words • 38 paragraphs            │   │
│ └─────────────────────────────────────────┘   │
│                                                 │
│ ┌─────────────────────────────────────────┐   │
│ │ Chunk 2 of 2                            │   │
│ │ ─────────────────────────────────────   │   │
│ │                                         │   │
│ │ [Overlap region highlighted]            │   │
│ │                                         │   │
│ │ ...she gobbles them up as if she        │   │
│ │ were a little wolf instead of a...      │   │
│ │                                         │   │
│ │ 1,523 words • 41 paragraphs            │   │
│ └─────────────────────────────────────────┘   │
│                                                 │
│ [← Adjust Settings]   [Begin Translation →]    │
│                                                 │
└─────────────────────────────────────────────────┘
```

**Features:**
- Expandable/collapsible chunk previews
- Visual highlight of overlap regions (light blue background)
- Stats for each chunk
- Ability to go back and adjust chunking parameters

**API Endpoints:**
```
POST /api/chunk
  Input: {
    text: string,
    chapter_id: string,
    target_size: int,
    overlap: int,
    min_overlap_words: int
  }
  Output: {
    chunks: [
      {
        id: string,
        position: int,
        source_text: string,
        metadata: { word_count, paragraph_count, ... }
      }
    ]
  }
```

### 3. Translation Page (Core Interface)

**Layout:**
```
┌─────────────────────────────────────────────────────────────────┐
│ Step 3: Translate                                               │
│                                                                 │
│ Progress: Chunk 1 of 2    ●──○                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│ ┌─────────────────────────────────────────────────────────┐   │
│ │ Source Text (English)                                   │   │
│ │ ───────────────────────────────────────────────────     │   │
│ │                                                         │   │
│ │ Sara stood near her father and listened while he and    │   │
│ │ Miss Minchin talked. She had been brought to the        │   │
│ │ seminary because Lady Meredith's two little girls...    │   │
│ │                                                         │   │
│ │ [Full source text scrollable]                          │   │
│ │                                                         │   │
│ │ 1,456 words • 38 paragraphs                            │   │
│ └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│ ┌─────────────────────────────────────────────────────────┐   │
│ │ Translation Prompt                                      │   │
│ │ ───────────────────────────────────────────────────     │   │
│ │                                                         │   │
│ │ You are translating "Little Princess" from English     │   │
│ │ to Spanish. This is a professional translation.         │   │
│ │                                                         │   │
│ │ Key Terms (use consistently):                          │   │
│ │ - Sara Crewe → Sara Crewe                             │   │
│ │ - Miss Minchin → la señorita Minchin                  │   │
│ │                                                         │   │
│ │ Translate this text:                                   │   │
│ │ [Full source text]                                     │   │
│ │                                                         │   │
│ │            [📋 Copy Prompt to Clipboard]               │   │
│ └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│ ┌─────────────────────────────────────────────────────────┐   │
│ │ Paste Translation Here (Spanish)                       │   │
│ │ ───────────────────────────────────────────────────     │   │
│ │ ┌───────────────────────────────────────────────────┐   │   │
│ │ │ Paste the Spanish translation from your LLM...   │   │   │
│ │ │                                                   │   │   │
│ │ │                                                   │   │   │
│ │ │                                                   │   │   │
│ │ │                                                   │   │   │
│ │ └───────────────────────────────────────────────────┘   │   │
│ │                                                         │   │
│ │ ⚠ 0 words (expected ~1,600-1,900 for Spanish)          │   │
│ └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│ [Save & Evaluate]  [Skip to Next Chunk →]                     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Features:**
- **Copy button** - One-click copy of complete prompt
- **Live feedback** - Word count updates as user pastes
- **Auto-save** - Save to localStorage every 5 seconds
- **Progress indicator** - Visual progress through chunks
- **Navigation** - Move between chunks without losing work
- **Expected length indicator** - Show target word count range

**State Management:**
```javascript
{
  projectName: string,
  chunks: [
    {
      id: string,
      sourceText: string,
      translatedText: string | null,
      status: 'pending' | 'in_progress' | 'completed' | 'evaluated'
    }
  ],
  currentChunkIndex: number,
  glossary: {...},
  evaluationResults: {...}
}
```

### 4. Evaluation Page

**Layout:**
```
┌─────────────────────────────────────────────────────────────────┐
│ Step 4: Evaluate Translation Quality                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│ Chunk 1 of 2                                [Evaluate Now]     │
│                                                                 │
│ ┌─────────────────────────────────────────────────────────┐   │
│ │ Overall Score: 0.92 / 1.00                              │   │
│ │                                                         │   │
│ │ Evaluators:                                            │   │
│ │   ✓ Length       1.00  │                              │   │
│ │   ✓ Paragraph    1.00  │                              │   │
│ │   ✓ Dictionary   0.95  │ 1 warning                    │   │
│ │   ✓ Completeness 1.00  │                              │   │
│ │   ⚠ Glossary     0.75  │ 1 error                      │   │
│ │                                                         │   │
│ │ [View Details ▼]                                       │   │
│ └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│ ┌─────────────────────────────────────────────────────────┐   │
│ │ Issues Found:                                           │   │
│ │                                                         │   │
│ │ [dictionary] ⚠ WARNING                                 │   │
│ │   Unknown word: "Gandalf"                              │   │
│ │   Location: Paragraph 3, character position 234        │   │
│ │   → Likely a proper noun. Add to glossary or ignore.   │   │
│ │                                                         │   │
│ │ [glossary] ❌ ERROR                                     │   │
│ │   Inconsistent term usage                              │   │
│ │   Expected: "Sara Crewe"                               │   │
│ │   Found: "Sarah Crewe"                                 │   │
│ │   Location: Paragraph 5                                │   │
│ │   → [View in context] [Fix]                           │   │
│ │                                                         │   │
│ └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│ [Edit Translation]  [Re-evaluate]  [Next Chunk →]             │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Features:**
- **Visual score display** - Progress bars or gauges for each evaluator
- **Expandable details** - Click to see full evaluation report
- **Inline editing** - "Fix" button opens translation for quick edits
- **Re-evaluate** - Run evaluations again after fixes
- **Context viewer** - Highlight issues in translation text
- **Export report** - Download as HTML or JSON

**API Endpoints:**
```
POST /api/evaluate
  Input: {
    chunk: {
      id: string,
      source_text: string,
      translated_text: string,
      metadata: {...}
    },
    glossary?: {...}
  }
  Output: {
    overall_score: float,
    passed: boolean,
    evaluators: {
      length: { score, passed, issues: [] },
      paragraph: { score, passed, issues: [] },
      dictionary: { score, passed, issues: [] },
      completeness: { score, passed, issues: [] },
      glossary: { score, passed, issues: [] }
    }
  }
```

### 5. Export Page

**Layout:**
```
┌─────────────────────────────────────────────────┐
│ Step 5: Export Your Translation                │
├─────────────────────────────────────────────────┤
│                                                 │
│ ✓ Translation Complete!                        │
│                                                 │
│ Chunks: 2/2 completed                          │
│ Overall score: 0.96 / 1.00                     │
│                                                 │
│ ┌─────────────────────────────────────────┐   │
│ │ Download Options:                       │   │
│ │                                         │   │
│ │ ○ Combined chapter (plain text)        │   │
│ │   chapter_01_translated.txt            │   │
│ │   [Download]                           │   │
│ │                                         │   │
│ │ ○ Individual chunks (JSON)             │   │
│ │   chapter_01_chunk_000.json            │   │
│ │   chapter_01_chunk_001.json            │   │
│ │   [Download All Chunks]                │   │
│ │                                         │   │
│ │ ○ Evaluation reports (HTML)            │   │
│ │   evaluation_report.html               │   │
│ │   [Download Report]                    │   │
│ │                                         │   │
│ │ ○ Complete project (ZIP)               │   │
│ │   Contains: all chunks, combined text, │   │
│ │   evaluation reports, glossary         │   │
│ │   [Download Project ZIP]               │   │
│ └─────────────────────────────────────────┘   │
│                                                 │
│ ┌─────────────────────────────────────────┐   │
│ │ Preview Combined Translation:           │   │
│ │ ─────────────────────────────────────   │   │
│ │                                         │   │
│ │ Sara estaba cerca de su padre y        │   │
│ │ escuchaba mientras él y la señorita... │   │
│ │                                         │   │
│ │ [Show full text]                       │   │
│ └─────────────────────────────────────────┘   │
│                                                 │
│ [← Back to Evaluate]  [Start New Project]     │
│                                                 │
└─────────────────────────────────────────────────┘
```

**Features:**
- **Multiple export formats**
- **Preview** - View combined translation before download
- **ZIP bundle** - All project files in one download
- **Session persistence** - "Continue editing" option

**API Endpoints:**
```
POST /api/combine
  Input: {
    chunks: [...],
    output_format: 'txt' | 'json' | 'html'
  }
  Output: {
    combined_text: string,
    metadata: { word_count, chunk_count, ... }
  }
```

## Implementation Phases

### Phase 1: Backend API (2-3 weeks)

**Week 1: Core API endpoints**
- `POST /api/chunk` - Chunking service
- `POST /api/evaluate` - Evaluation service
- `POST /api/combine` - Combination service
- `POST /api/validate-glossary` - Glossary validation

**Week 2: Integration & Testing**
- Integrate with existing `src/` modules
- Error handling and validation
- Unit tests for all endpoints
- API documentation (Swagger/OpenAPI)

**Week 3: Deployment Setup**
- Docker containerization
- Environment configuration
- CORS setup for frontend
- Local development server script

**Deliverables:**
- Flask/FastAPI application
- Documented REST API
- Docker compose setup
- README for backend setup

### Phase 2: Frontend Core (3-4 weeks)

**Week 1: Project setup & Setup page**
- React project initialization
- Tailwind CSS configuration
- State management setup (Zustand)
- Setup page component
- localStorage integration

**Week 2: Chunk Preview & Translation pages**
- Chunk preview component
- Translation page layout
- Copy-to-clipboard functionality
- Progress indicator
- Auto-save implementation

**Week 3: Evaluation page**
- Evaluation results display
- Issue highlighting
- Re-evaluation flow
- Context viewer for issues

**Week 4: Export page & Polish**
- Export functionality
- File download handlers
- Preview components
- Overall UI polish

**Deliverables:**
- Fully functional React application
- Responsive design (desktop + tablet)
- localStorage persistence
- Basic error handling

### Phase 3: Polish & Enhancement (1-2 weeks)

**Week 1: User Experience**
- Keyboard shortcuts (Ctrl+C for copy, etc.)
- Tutorial/onboarding flow (first-time walkthrough)
- Help tooltips on key features
- Better error messages and validation
- Loading states and animations
- Cross-browser testing (Chrome, Firefox, Safari, Edge)

**Week 2: Polish & Testing**
- Performance optimization (large text handling)
- Accessibility improvements (ARIA labels, keyboard navigation)
- Session export/import (save/load project JSON)
- User testing and feedback
- Bug fixes and edge cases
- Final UI polish

**Deliverables:**
- Production-ready UI
- Docker deployment guide
- User documentation
- Quick start video

### Phase 4: Deployment & Documentation (1 week)

- Production deployment configuration
- User guide / documentation
- Video walkthrough
- Feedback mechanism
- Analytics (optional, privacy-respecting)

## Technical Specifications

### Frontend Stack Details

```json
{
  "dependencies": {
    "react": "^18.2.0",
    "react-dom": "^18.2.0",
    "zustand": "^4.4.0",
    "tailwindcss": "^3.3.0",
    "axios": "^1.5.0",
    "react-router-dom": "^6.16.0",
    "react-hot-toast": "^2.4.1",
    "lucide-react": "^0.284.0"
  }
}
```

### Backend Stack Details

```python
# requirements.txt (additions for web UI)
flask>=3.0.0
flask-cors>=4.0.0
flask-restful>=0.3.10
marshmallow>=3.20.0      # Request/response validation
gunicorn>=21.2.0         # Production server

# Or with FastAPI:
fastapi>=0.104.0
uvicorn[standard]>=0.24.0
pydantic>=2.4.0
python-multipart>=0.0.6  # File uploads
```

### File Structure

```
book_translation/
├── backend/
│   ├── app.py                 # Flask app entry point
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py          # API route definitions
│   │   ├── schemas.py         # Request/response schemas
│   │   └── services.py        # Business logic
│   ├── requirements.txt
│   └── Dockerfile
│
├── frontend/
│   ├── public/
│   ├── src/
│   │   ├── components/
│   │   │   ├── SetupPage.jsx
│   │   │   ├── ChunkPreview.jsx
│   │   │   ├── TranslatePage.jsx
│   │   │   ├── EvaluatePage.jsx
│   │   │   └── ExportPage.jsx
│   │   ├── hooks/
│   │   │   ├── useLocalStorage.js
│   │   │   └── useAPI.js
│   │   ├── store/
│   │   │   └── translationStore.js
│   │   ├── utils/
│   │   │   └── api.js
│   │   ├── App.jsx
│   │   └── main.jsx
│   ├── package.json
│   └── Dockerfile
│
├── docker-compose.yml         # Run both backend + frontend
└── WEB_UI_README.md          # Setup and usage guide
```

### API Request/Response Examples

**Chunk Endpoint:**
```python
# Request
POST /api/chunk
{
  "text": "Sara stood near her father...",
  "chapter_id": "chapter_01",
  "target_size": 1500,
  "overlap": 2
}

# Response
{
  "success": true,
  "chunks": [
    {
      "id": "chapter_01_chunk_000",
      "position": 0,
      "source_text": "Sara stood near her father...",
      "metadata": {
        "word_count": 1456,
        "paragraph_count": 38,
        "char_start": 0,
        "char_end": 7234,
        "overlap_start": 6800,
        "overlap_end": 7234
      }
    }
  ]
}
```

**Evaluate Endpoint:**
```python
# Request
POST /api/evaluate
{
  "chunk": {
    "id": "chapter_01_chunk_000",
    "source_text": "Sara stood...",
    "translated_text": "Sara estaba...",
    "metadata": { ... }
  },
  "glossary": { ... }  # Optional
}

# Response
{
  "success": true,
  "result": {
    "overall_score": 0.92,
    "passed": true,
    "evaluators": {
      "length": {
        "score": 1.0,
        "passed": true,
        "issues": []
      },
      "glossary": {
        "score": 0.75,
        "passed": false,
        "issues": [
          {
            "severity": "error",
            "message": "Inconsistent term usage",
            "location": "Paragraph 5",
            "suggestion": "Use 'Sara Crewe' not 'Sarah Crewe'",
            "context": {
              "expected": "Sara Crewe",
              "found": "Sarah Crewe"
            }
          }
        ]
      }
    }
  }
}
```

## Deployment: Docker Compose (Local Web Server)

**Technology**: Docker Compose running Flask backend + React frontend

**Setup:**
```bash
# Clone or navigate to project
cd book_translation

# Start the web UI (builds and runs both services)
docker-compose up

# Access in browser
# Opens automatically to http://localhost:3000
```

**Architecture:**
```
┌─────────────────────────────────────────┐
│ User's Browser (http://localhost:3000)  │
└───────────────┬─────────────────────────┘
                │
    ┌───────────▼────────────┐
    │ Docker Compose Network │
    │                        │
    │  ┌──────────────────┐  │
    │  │ Frontend         │  │
    │  │ (React + Nginx)  │  │
    │  │ :3000            │  │
    │  └────────┬─────────┘  │
    │           │            │
    │  ┌────────▼─────────┐  │
    │  │ Backend          │  │
    │  │ (Flask + Python) │  │
    │  │ :5000            │  │
    │  └──────────────────┘  │
    │                        │
    └────────────────────────┘
```

**Pros:**
- Cross-platform (runs on Windows, macOS, Linux)
- Isolated environment (no dependency conflicts)
- Easy updates (`docker-compose pull && docker-compose up`)
- Works offline after initial setup
- Single command to start

**Cons:**
- Requires Docker Desktop installation (~500MB)
- First-time build takes 2-3 minutes

**Production Readiness:**
- Add volume mounts for persistent storage (optional)
- Environment variables for configuration
- Health checks for both services
- Auto-restart on failure

## Security & Privacy Considerations

1. **Data Storage**
   - All data in localStorage (never sent to external servers)
   - Option to export/import project files
   - Clear data button in settings

2. **Backend Security**
   - CORS restrictions
   - Request size limits (prevent abuse)
   - Input validation on all endpoints
   - No persistent storage (stateless API)

3. **Privacy**
   - No analytics by default
   - No external API calls (except user's LLM of choice)
   - Open source (users can verify)

## Success Metrics

**Primary Goals:**
- Users can complete full workflow without touching command line
- Average time to translate first chunk: < 5 minutes
- Evaluation results easy to understand and act on

**Metrics to Track:**
- Time from project start to first translation
- Completion rate (% who finish full chapter)
- Error rate (evaluation failures)
- User feedback scores

## Design Decisions (Finalized)

✅ **Deployment**: Docker Compose (local web server)
✅ **Glossary management**: Upload JSON only (no UI editor)
✅ **Custom prompts**: File-based only (no UI editor)
✅ **Session management**: Single project at a time
✅ **Collaboration**: Not needed (removed from scope)

## Next Steps

1. **Review this plan** - Gather feedback on priorities
2. **Spike/Prototype** - Build minimal version of Translation page (core flow)
3. **User testing** - Get feedback from 2-3 users on prototype
4. **Iterate** - Refine based on feedback
5. **Build Phase 1** - Implement backend API
6. **Build Phase 2** - Implement frontend
7. **Beta release** - Docker-based local deployment
8. **Production release** - Electron app or hosted service

## Estimated Timeline (Revised)

- **Phase 1 (Backend API)**: 2 weeks
- **Phase 2 (Frontend Core)**: 3 weeks
- **Phase 3 (Polish & Enhancement)**: 1-2 weeks
- **Phase 4 (Deployment & Docs)**: 1 week

**Total: 7-8 weeks** for full production-ready release

**Minimum Viable Product (MVP)**: 3-4 weeks (Phase 1 + basic Phase 2)

*Timeline reduced due to simplified scope: no glossary editor, no prompt editor, no multi-user features, Docker-only deployment.*

---

*Document created: 2025-01-07*
*Status: Planning - Awaiting Feedback*
