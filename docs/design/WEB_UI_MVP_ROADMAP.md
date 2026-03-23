# Web UI MVP Implementation Roadmap

**Goal**: Deliver a working web UI in 3-4 weeks that replaces the command-line workflow.

**Scope**: Docker-based local deployment, single project, upload-only for glossary/prompts.

## Week 1: Backend API Foundation

### Day 1-2: Project Setup
- [ ] Create `backend/` directory structure
- [ ] Initialize Flask application (`backend/app.py`)
- [ ] Setup CORS for frontend communication
- [ ] Create requirements file with additions:
  - flask>=3.0.0
  - flask-cors>=4.0.0
  - marshmallow>=3.20.0
- [ ] Create `backend/api/routes.py` skeleton
- [ ] Test basic Flask server runs

**Deliverable**: Flask app running on `http://localhost:5000` with health check endpoint.

### Day 3-4: Chunking API
- [ ] Create `POST /api/chunk` endpoint
- [ ] Integrate with existing `src/chunker.py`
- [ ] Add request validation (marshmallow schema)
- [ ] Handle errors (file too small, invalid params)
- [ ] Write unit tests for endpoint
- [ ] Test with Postman/curl

**Deliverable**: Working chunk endpoint that returns JSON chunks from text input.

**Example Request:**
```bash
curl -X POST http://localhost:5000/api/chunk \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Sara stood near...",
    "chapter_id": "chapter_01",
    "target_size": 1500,
    "overlap": 2
  }'
```

### Day 5-7: Evaluation & Combine APIs
- [ ] Create `POST /api/evaluate` endpoint
- [ ] Integrate with existing `src/evaluators/`
- [ ] Support optional glossary parameter
- [ ] Create `POST /api/combine` endpoint
- [ ] Integrate with existing `src/combiner.py`
- [ ] Add comprehensive error handling
- [ ] Write unit tests
- [ ] Document API with example requests/responses

**Deliverable**: All 3 core endpoints working and tested.

## Week 2: Frontend Setup & Core Pages

### Day 1-2: React Project Setup
- [ ] Create `frontend/` directory
- [ ] Initialize React app (`npx create-react-app frontend`)
- [ ] Install dependencies:
  - tailwindcss
  - zustand (state management)
  - axios (API calls)
  - react-router-dom (navigation)
  - lucide-react (icons)
- [ ] Configure Tailwind CSS
- [ ] Setup routing structure (5 pages)
- [ ] Create basic layout component
- [ ] Test app runs on `http://localhost:3000`

**Deliverable**: React app with routing and styled layout.

### Day 3-4: State Management & API Layer
- [ ] Create Zustand store (`src/store/translationStore.js`):
  ```javascript
  {
    projectName: string,
    sourceText: string,
    chunks: [],
    currentChunkIndex: number,
    glossary: object | null,
    evaluationResults: {}
  }
  ```
- [ ] Create API utility (`src/utils/api.js`):
  - `chunkText()`
  - `evaluateChunk()`
  - `combineChunks()`
- [ ] Add localStorage persistence hooks
- [ ] Test state updates and persistence

**Deliverable**: Working state management with API integration.

### Day 5-7: Setup & Chunk Preview Pages
- [ ] Build `SetupPage.jsx`:
  - Text area for source text
  - File upload for glossary
  - Chunking parameter inputs
  - Word/paragraph counter (live)
  - "Continue" button
- [ ] Build `ChunkPreview.jsx`:
  - Display all chunks
  - Highlight overlap regions
  - Show chunk stats
  - "Begin Translation" button
- [ ] Connect to backend API
- [ ] Add loading states and error handling
- [ ] Test full flow: paste text → chunk → preview

**Deliverable**: First two pages working end-to-end.

## Week 3: Translation & Evaluation Pages

### Day 1-3: Translation Page (Core Feature)
- [ ] Build `TranslatePage.jsx`:
  - Display source text (read-only)
  - Display full prompt with glossary
  - "Copy Prompt" button with clipboard API
  - Text area for translation paste
  - Live word counter
  - Progress indicator (chunk X of Y)
  - Navigation: Previous/Next chunk
  - "Save & Evaluate" button
- [ ] Implement auto-save (every 5 seconds)
- [ ] Add keyboard shortcuts (Ctrl+S to save)
- [ ] Style for easy reading (good typography)
- [ ] Test with real chapter text

**Deliverable**: Translation page fully functional with copy/paste workflow.

### Day 4-5: Evaluation Page
- [ ] Build `EvaluatePage.jsx`:
  - Display overall score with visual indicator
  - Show each evaluator result (score, pass/fail)
  - Display issues in expandable cards
  - Color coding (errors red, warnings yellow)
  - "Re-evaluate" button
  - "Edit Translation" button (go back to translate page)
- [ ] Integrate with `/api/evaluate` endpoint
- [ ] Add loading spinner during evaluation
- [ ] Test with good and bad translations

**Deliverable**: Evaluation page showing results clearly.

### Day 6-7: Export Page & Final Integration
- [ ] Build `ExportPage.jsx`:
  - Display completion summary
  - Download buttons:
    - Combined text file
    - Individual chunk JSONs (ZIP)
    - Evaluation report (HTML)
  - Preview of combined translation
  - "Start New Project" button
- [ ] Implement download handlers (Blob/URL.createObjectURL)
- [ ] Test full workflow end-to-end:
  1. Paste chapter
  2. Preview chunks
  3. Translate all chunks
  4. Evaluate
  5. Export combined text
- [ ] Fix any bugs discovered

**Deliverable**: Complete working application (MVP feature-complete).

## Week 4: Polish & Docker Deployment

### Day 1-2: UI Polish
- [ ] Improve visual design (consistent spacing, colors)
- [ ] Add help tooltips on key features
- [ ] Better error messages (user-friendly)
- [ ] Loading states for all async operations
- [ ] Responsive design adjustments
- [ ] Cross-browser testing (Chrome, Firefox, Safari)

### Day 3-4: Docker Setup
- [ ] Create `backend/Dockerfile`:
  ```dockerfile
  FROM python:3.11-slim
  WORKDIR /app
  COPY requirements.txt .
  RUN pip install -r requirements.txt
  COPY . .
  CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app"]
  ```
- [ ] Create `frontend/Dockerfile`:
  ```dockerfile
  FROM node:18-alpine AS build
  WORKDIR /app
  COPY package*.json ./
  RUN npm install
  COPY . .
  RUN npm run build

  FROM nginx:alpine
  COPY --from=build /app/build /usr/share/nginx/html
  EXPOSE 3000
  CMD ["nginx", "-g", "daemon off;"]
  ```
- [ ] Create `docker-compose.yml`:
  ```yaml
  version: '3.8'
  services:
    backend:
      build: ./backend
      ports:
        - "5000:5000"
      volumes:
        - ./src:/app/src
    frontend:
      build: ./frontend
      ports:
        - "3000:3000"
      depends_on:
        - backend
  ```
- [ ] Test Docker build and run
- [ ] Fix any container issues

**Deliverable**: Working Docker Compose setup.

### Day 5: Documentation & Testing
- [ ] Create `WEB_UI_README.md`:
  - Installation instructions (Docker Desktop)
  - Quick start guide
  - Troubleshooting
  - Screenshots
- [ ] Record quick start video (5 minutes):
  - Install Docker
  - Run `docker-compose up`
  - Complete workflow demo
- [ ] Final testing on different machines
- [ ] Fix critical bugs
- [ ] Tag v1.0.0-beta release

**Deliverable**: Production-ready beta release with documentation.

## MVP Feature Checklist

### Must Have (Week 1-3)
- [x] Chunk text into translatable segments
- [x] Generate prompts with glossary
- [x] Copy prompt to clipboard
- [x] Paste translation and save
- [x] Evaluate translation quality
- [x] Combine chunks into final text
- [x] Export combined translation

### Should Have (Week 4)
- [x] Docker deployment
- [x] Basic documentation
- [x] Error handling
- [x] Loading states
- [x] Auto-save progress

### Nice to Have (Post-MVP)
- [ ] Keyboard shortcuts
- [ ] First-time tutorial
- [ ] Session export/import
- [ ] Undo/redo
- [ ] Dark mode
- [ ] Advanced settings

## Development Commands

### Backend Development
```bash
cd backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py  # Runs on http://localhost:5000
```

### Frontend Development
```bash
cd frontend
npm install
npm start  # Runs on http://localhost:3000
```

### Docker (Production)
```bash
# Build and run
docker-compose up --build

# Run in background
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

## Testing Strategy

### Backend Tests
```bash
cd backend
pytest tests/ -v
```

Test coverage:
- Each API endpoint (success cases)
- Error handling (invalid input, missing params)
- Integration with existing src/ modules

### Frontend Tests
```bash
cd frontend
npm test
```

Test coverage:
- Component rendering
- User interactions (button clicks, form input)
- API integration (mocked)
- State management

### Manual Testing Checklist
- [ ] Paste 5000-word chapter → chunks correctly
- [ ] Upload glossary → terms appear in prompt
- [ ] Copy prompt → clipboard contains full text
- [ ] Paste translation → saves to state
- [ ] Navigate between chunks → maintains translations
- [ ] Evaluate → shows correct results
- [ ] Export → downloads correct file
- [ ] Refresh page → restores session from localStorage
- [ ] Clear browser data → starts fresh

## Post-MVP Enhancements (Week 5+)

### Phase 3A: User Experience (1 week)
- Keyboard shortcuts (Ctrl+C, Ctrl+S, Ctrl+→/←)
- Onboarding tutorial (first-time user walkthrough)
- Context-sensitive help tooltips
- Undo/redo for translation edits
- Better mobile support (tablet-friendly)

### Phase 3B: Advanced Features (1 week)
- Session export/import (save project as JSON, load later)
- Multiple prompt templates (technical, children's book, etc.)
- Custom evaluation thresholds via UI
- Batch evaluation (all chunks at once)
- Print-friendly evaluation reports

## Success Criteria

**MVP is complete when:**
1. ✅ User can translate a chapter without touching command line
2. ✅ Docker setup works on fresh machine with just `docker-compose up`
3. ✅ All 5 core workflows functional (setup → chunk → translate → evaluate → export)
4. ✅ No critical bugs in happy path
5. ✅ README has clear setup instructions

**Ready for production when:**
1. ✅ All MVP criteria met
2. ✅ Tested on Windows, macOS, Linux
3. ✅ Documentation includes troubleshooting
4. ✅ Video tutorial available
5. ✅ At least 2 users complete full workflow successfully

## Risk Mitigation

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Docker build fails on Windows | High | Test on Windows early, provide alternative setup |
| Large text (50K+ words) crashes browser | Medium | Add text size limits, warn user |
| Evaluation takes too long | Medium | Add progress indicator, run in background |
| Browser localStorage limit (5MB) | Low | Compress data, warn at 80% capacity |
| Python dependencies conflict | Low | Use isolated Docker containers |

## Questions for Week 1 Review

After Week 1 backend work:
1. Do API response times feel fast enough? (<2 seconds)
2. Are error messages helpful for debugging?
3. Should we add API authentication (simple token)?

## Next Steps After MVP

1. **User Feedback** (1 week)
   - Share with 3-5 beta testers
   - Collect structured feedback
   - Prioritize improvements

2. **Iteration** (2 weeks)
   - Fix reported bugs
   - Add most-requested features
   - Improve documentation

3. **Release v1.0** (1 week)
   - Final testing
   - Create release notes
   - Publish to GitHub
   - Announce to users

---

**Start Date**: TBD
**Target MVP Completion**: 3-4 weeks from start
**Target v1.0 Release**: 6-8 weeks from start
