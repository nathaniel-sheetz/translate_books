# Web UI Starter Template

Quick reference for setting up the web UI project structure and starter code.

## Project Structure

```
book_translation/
├── backend/                      # Flask API backend
│   ├── app.py                   # Main Flask application
│   ├── requirements.txt         # Python dependencies
│   ├── Dockerfile              # Docker image for backend
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py           # API endpoint definitions
│   │   ├── schemas.py          # Request/response validation
│   │   └── services.py         # Business logic
│   └── tests/
│       ├── test_routes.py
│       └── test_integration.py
│
├── frontend/                    # React frontend
│   ├── public/
│   │   └── index.html
│   ├── src/
│   │   ├── components/
│   │   │   ├── SetupPage.jsx
│   │   │   ├── ChunkPreview.jsx
│   │   │   ├── TranslatePage.jsx
│   │   │   ├── EvaluatePage.jsx
│   │   │   └── ExportPage.jsx
│   │   ├── store/
│   │   │   └── translationStore.js    # Zustand state management
│   │   ├── utils/
│   │   │   ├── api.js                 # API client functions
│   │   │   └── localStorage.js        # Persistence utilities
│   │   ├── App.jsx                    # Main app with routing
│   │   └── index.js                   # Entry point
│   ├── package.json
│   ├── Dockerfile
│   └── tailwind.config.js
│
├── docker-compose.yml          # Orchestrate backend + frontend
├── WEB_UI_README.md           # Setup and usage guide
└── (existing src/, tests/, etc.)
```

## Backend Starter Code

### `backend/app.py`

```python
"""
Flask backend for book translation web UI.
Wraps existing chunker, evaluator, and combiner functionality.
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
from api.routes import api_bp

def create_app():
    app = Flask(__name__)

    # Enable CORS for frontend (localhost:3000)
    CORS(app, origins=["http://localhost:3000"])

    # Register API blueprint
    app.register_blueprint(api_bp, url_prefix='/api')

    # Health check endpoint
    @app.route('/health')
    def health():
        return jsonify({"status": "healthy"}), 200

    return app

if __name__ == '__main__':
    app = create_app()
    app.run(debug=True, host='0.0.0.0', port=5000)
```

### `backend/api/routes.py`

```python
"""
API route definitions.
"""
from flask import Blueprint, request, jsonify
from marshmallow import ValidationError
from .schemas import ChunkRequestSchema, EvaluateRequestSchema, CombineRequestSchema
from .services import chunk_text, evaluate_chunk, combine_chunks

api_bp = Blueprint('api', __name__)

# Schemas for validation
chunk_schema = ChunkRequestSchema()
evaluate_schema = EvaluateRequestSchema()
combine_schema = CombineRequestSchema()


@api_bp.route('/chunk', methods=['POST'])
def api_chunk():
    """
    Chunk source text into translation segments.

    Request body:
    {
        "text": str,
        "chapter_id": str,
        "target_size": int (default: 1500),
        "overlap": int (default: 2)
    }
    """
    try:
        # Validate request
        data = chunk_schema.load(request.json)

        # Call service
        result = chunk_text(
            text=data['text'],
            chapter_id=data['chapter_id'],
            target_size=data.get('target_size', 1500),
            overlap=data.get('overlap', 2)
        )

        return jsonify({
            "success": True,
            "chunks": result
        }), 200

    except ValidationError as e:
        return jsonify({
            "success": False,
            "error": "Validation error",
            "details": e.messages
        }), 400

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@api_bp.route('/evaluate', methods=['POST'])
def api_evaluate():
    """
    Evaluate translation quality.

    Request body:
    {
        "chunk": {
            "id": str,
            "source_text": str,
            "translated_text": str,
            "metadata": {...}
        },
        "glossary": {...} (optional)
    }
    """
    try:
        data = evaluate_schema.load(request.json)

        result = evaluate_chunk(
            chunk=data['chunk'],
            glossary=data.get('glossary')
        )

        return jsonify({
            "success": True,
            "result": result
        }), 200

    except ValidationError as e:
        return jsonify({
            "success": False,
            "error": "Validation error",
            "details": e.messages
        }), 400

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@api_bp.route('/combine', methods=['POST'])
def api_combine():
    """
    Combine translated chunks into final text.

    Request body:
    {
        "chunks": [{...}, {...}]
    }
    """
    try:
        data = combine_schema.load(request.json)

        result = combine_chunks(chunks=data['chunks'])

        return jsonify({
            "success": True,
            "combined_text": result['text'],
            "metadata": result['metadata']
        }), 200

    except ValidationError as e:
        return jsonify({
            "success": False,
            "error": "Validation error",
            "details": e.messages
        }), 400

    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500
```

### `backend/api/services.py`

```python
"""
Business logic for API endpoints.
Integrates with existing src/ modules.
"""
import sys
from pathlib import Path

# Add parent directory to path to import from src/
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.chunker import create_chunks
from src.evaluators import run_all_evaluators
from src.combiner import combine_chunks as combine_chunks_impl
from src.models import Chunk, ChunkMetadata, ChunkStatus, Glossary


def chunk_text(text: str, chapter_id: str, target_size: int, overlap: int) -> list:
    """
    Create chunks from source text.

    Returns list of chunk dictionaries ready for JSON serialization.
    """
    # Use existing chunker
    chunks = create_chunks(
        text=text,
        chapter_id=chapter_id,
        target_size=target_size,
        overlap_paragraphs=overlap
    )

    # Convert to dict for JSON response
    return [
        {
            "id": chunk.id,
            "chapter_id": chunk.chapter_id,
            "position": chunk.position,
            "source_text": chunk.source_text,
            "metadata": {
                "word_count": chunk.metadata.word_count,
                "paragraph_count": chunk.metadata.paragraph_count,
                "char_start": chunk.metadata.char_start,
                "char_end": chunk.metadata.char_end,
                "overlap_start": chunk.metadata.overlap_start,
                "overlap_end": chunk.metadata.overlap_end
            }
        }
        for chunk in chunks
    ]


def evaluate_chunk(chunk: dict, glossary: dict = None) -> dict:
    """
    Evaluate a translated chunk.

    Returns evaluation results with scores and issues.
    """
    # Convert dict back to Chunk model
    chunk_obj = Chunk(
        id=chunk['id'],
        chapter_id=chunk['chapter_id'],
        position=chunk['position'],
        source_text=chunk['source_text'],
        translated_text=chunk['translated_text'],
        metadata=ChunkMetadata(**chunk['metadata']),
        status=ChunkStatus.TRANSLATED
    )

    # Convert glossary if provided
    context = {}
    if glossary:
        context['glossary'] = Glossary(**glossary)

    # Run evaluators
    results = run_all_evaluators(chunk_obj, context)

    # Convert to dict
    return {
        "overall_score": results['overall_score'],
        "passed": results['passed'],
        "evaluators": {
            name: {
                "score": result.score,
                "passed": result.passed,
                "issues": [
                    {
                        "severity": issue.severity,
                        "message": issue.message,
                        "location": issue.location,
                        "suggestion": issue.suggestion
                    }
                    for issue in result.issues
                ]
            }
            for name, result in results['evaluators'].items()
        }
    }


def combine_chunks(chunks: list) -> dict:
    """
    Combine chunks into final translation.

    Returns combined text and metadata.
    """
    # Convert dicts to Chunk objects
    chunk_objs = [
        Chunk(
            id=c['id'],
            chapter_id=c['chapter_id'],
            position=c['position'],
            source_text=c['source_text'],
            translated_text=c['translated_text'],
            metadata=ChunkMetadata(**c['metadata']),
            status=ChunkStatus.TRANSLATED
        )
        for c in chunks
    ]

    # Combine using existing implementation
    combined_text = combine_chunks_impl(chunk_objs)

    return {
        "text": combined_text,
        "metadata": {
            "chunk_count": len(chunks),
            "word_count": len(combined_text.split()),
            "char_count": len(combined_text)
        }
    }
```

### `backend/api/schemas.py`

```python
"""
Request/response validation schemas using marshmallow.
"""
from marshmallow import Schema, fields, validate


class ChunkRequestSchema(Schema):
    text = fields.Str(required=True, validate=validate.Length(min=100))
    chapter_id = fields.Str(required=True)
    target_size = fields.Int(missing=1500, validate=validate.Range(min=500, max=5000))
    overlap = fields.Int(missing=2, validate=validate.Range(min=0, max=10))


class ChunkMetadataSchema(Schema):
    word_count = fields.Int(required=True)
    paragraph_count = fields.Int(required=True)
    char_start = fields.Int(required=True)
    char_end = fields.Int(required=True)
    overlap_start = fields.Int(required=True)
    overlap_end = fields.Int(required=True)


class ChunkDataSchema(Schema):
    id = fields.Str(required=True)
    chapter_id = fields.Str(required=True)
    position = fields.Int(required=True)
    source_text = fields.Str(required=True)
    translated_text = fields.Str(required=True)
    metadata = fields.Nested(ChunkMetadataSchema, required=True)


class EvaluateRequestSchema(Schema):
    chunk = fields.Nested(ChunkDataSchema, required=True)
    glossary = fields.Dict(missing=None)


class CombineRequestSchema(Schema):
    chunks = fields.List(fields.Nested(ChunkDataSchema), required=True, validate=validate.Length(min=1))
```

### `backend/requirements.txt`

```
# Existing dependencies
pydantic>=2.0.0
python-dateutil>=2.8.0
pyenchant>=3.2.0
rich>=13.0.0

# Web UI additions
flask>=3.0.0
flask-cors>=4.0.0
marshmallow>=3.20.0
gunicorn>=21.2.0

# Testing
pytest>=7.0.0
pytest-cov>=4.0.0
```

### `backend/Dockerfile`

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for pyenchant
RUN apt-get update && apt-get install -y \
    enchant-2 \
    aspell-es \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Copy src/ from parent directory
COPY ../src /app/src

# Expose port
EXPOSE 5000

# Run with gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
```

## Frontend Starter Code

### `frontend/src/store/translationStore.js`

```javascript
/**
 * Global state management using Zustand.
 * Handles translation session state and localStorage persistence.
 */
import { create } from 'zustand';
import { persist } from 'zustand/middleware';

const useTranslationStore = create(
  persist(
    (set, get) => ({
      // Project metadata
      projectName: '',

      // Source and chunks
      sourceText: '',
      chunks: [],
      currentChunkIndex: 0,

      // Optional enhancements
      glossary: null,

      // Evaluation results
      evaluationResults: {},

      // Actions
      setProjectName: (name) => set({ projectName: name }),

      setSourceText: (text) => set({ sourceText: text }),

      setChunks: (chunks) => set({ chunks }),

      setCurrentChunkIndex: (index) => set({ currentChunkIndex: index }),

      setGlossary: (glossary) => set({ glossary }),

      updateChunkTranslation: (chunkId, translatedText) => {
        const chunks = get().chunks.map(chunk =>
          chunk.id === chunkId
            ? { ...chunk, translated_text: translatedText, status: 'completed' }
            : chunk
        );
        set({ chunks });
      },

      setEvaluationResult: (chunkId, result) => {
        set({
          evaluationResults: {
            ...get().evaluationResults,
            [chunkId]: result
          }
        });
      },

      reset: () => set({
        projectName: '',
        sourceText: '',
        chunks: [],
        currentChunkIndex: 0,
        glossary: null,
        evaluationResults: {}
      })
    }),
    {
      name: 'translation-storage', // localStorage key
      partialize: (state) => ({
        projectName: state.projectName,
        sourceText: state.sourceText,
        chunks: state.chunks,
        currentChunkIndex: state.currentChunkIndex,
        glossary: state.glossary,
        evaluationResults: state.evaluationResults
      })
    }
  )
);

export default useTranslationStore;
```

### `frontend/src/utils/api.js`

```javascript
/**
 * API client for backend communication.
 */
import axios from 'axios';

const API_BASE_URL = process.env.REACT_APP_API_URL || 'http://localhost:5000/api';

const api = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json'
  }
});

/**
 * Chunk source text into translation segments.
 */
export const chunkText = async (text, chapterId, targetSize = 1500, overlap = 2) => {
  const response = await api.post('/chunk', {
    text,
    chapter_id: chapterId,
    target_size: targetSize,
    overlap
  });
  return response.data;
};

/**
 * Evaluate a translated chunk.
 */
export const evaluateChunk = async (chunk, glossary = null) => {
  const response = await api.post('/evaluate', {
    chunk,
    glossary
  });
  return response.data;
};

/**
 * Combine translated chunks into final text.
 */
export const combineChunks = async (chunks) => {
  const response = await api.post('/combine', {
    chunks
  });
  return response.data;
};

export default api;
```

### `frontend/src/App.jsx`

```javascript
import React from 'react';
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import SetupPage from './components/SetupPage';
import ChunkPreview from './components/ChunkPreview';
import TranslatePage from './components/TranslatePage';
import EvaluatePage from './components/EvaluatePage';
import ExportPage from './components/ExportPage';

function App() {
  return (
    <Router>
      <div className="min-h-screen bg-gray-50">
        <header className="bg-blue-600 text-white p-4 shadow-lg">
          <h1 className="text-2xl font-bold">📖 Book Translation Workflow</h1>
        </header>

        <main className="container mx-auto p-4">
          <Routes>
            <Route path="/" element={<SetupPage />} />
            <Route path="/preview" element={<ChunkPreview />} />
            <Route path="/translate" element={<TranslatePage />} />
            <Route path="/evaluate" element={<EvaluatePage />} />
            <Route path="/export" element={<ExportPage />} />
          </Routes>
        </main>
      </div>
    </Router>
  );
}

export default App;
```

### `frontend/src/components/SetupPage.jsx`

```javascript
import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import useTranslationStore from '../store/translationStore';
import { chunkText } from '../utils/api';

function SetupPage() {
  const navigate = useNavigate();
  const { setProjectName, setSourceText, setChunks, setGlossary } = useTranslationStore();

  const [projectName, setProjectNameLocal] = useState('');
  const [text, setText] = useState('');
  const [glossaryFile, setGlossaryFile] = useState(null);
  const [targetSize, setTargetSize] = useState(1500);
  const [overlap, setOverlap] = useState(2);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const wordCount = text.trim().split(/\s+/).length;
  const paragraphCount = text.split('\n\n').filter(p => p.trim()).length;

  const handleGlossaryUpload = (e) => {
    const file = e.target.files[0];
    if (file) {
      const reader = new FileReader();
      reader.onload = (event) => {
        try {
          const glossary = JSON.parse(event.target.result);
          setGlossaryFile(glossary);
        } catch (err) {
          setError('Invalid glossary file. Must be valid JSON.');
        }
      };
      reader.readAsText(file);
    }
  };

  const handleContinue = async () => {
    if (!text.trim()) {
      setError('Please enter source text');
      return;
    }

    setLoading(true);
    setError(null);

    try {
      // Save to store
      setProjectName(projectName);
      setSourceText(text);
      if (glossaryFile) {
        setGlossary(glossaryFile);
      }

      // Chunk the text
      const result = await chunkText(text, 'chapter_01', targetSize, overlap);

      if (result.success) {
        setChunks(result.chunks);
        navigate('/preview');
      } else {
        setError(result.error || 'Failed to chunk text');
      }
    } catch (err) {
      setError(err.message || 'An error occurred');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="max-w-4xl mx-auto p-6">
      <h2 className="text-3xl font-bold mb-6">Step 1: Prepare Your Translation</h2>

      {/* Project Name */}
      <div className="mb-6">
        <label className="block text-sm font-medium mb-2">
          Project Name:
        </label>
        <input
          type="text"
          value={projectName}
          onChange={(e) => setProjectNameLocal(e.target.value)}
          placeholder="e.g., Little Princess - Chapter 1"
          className="w-full p-3 border rounded-lg"
        />
      </div>

      {/* Source Text */}
      <div className="mb-6">
        <label className="block text-sm font-medium mb-2">
          Source Text (English):
        </label>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Paste your chapter here..."
          rows={12}
          className="w-full p-3 border rounded-lg font-mono text-sm"
        />
        <div className="mt-2 text-sm text-gray-600">
          {wordCount} words • {paragraphCount} paragraphs
        </div>
      </div>

      {/* Glossary Upload */}
      <div className="mb-6">
        <label className="block text-sm font-medium mb-2">
          Glossary (Optional):
        </label>
        <input
          type="file"
          accept=".json"
          onChange={handleGlossaryUpload}
          className="block w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-semibold file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100"
        />
        {glossaryFile && (
          <div className="mt-2 text-sm text-green-600">
            ✓ {glossaryFile.terms?.length || 0} terms loaded
          </div>
        )}
      </div>

      {/* Chunking Settings */}
      <div className="mb-6">
        <label className="block text-sm font-medium mb-2">
          Chunking Settings:
        </label>
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-xs text-gray-600 mb-1">
              Target size (words per chunk):
            </label>
            <input
              type="number"
              value={targetSize}
              onChange={(e) => setTargetSize(parseInt(e.target.value))}
              className="w-full p-2 border rounded"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-600 mb-1">
              Overlap (paragraphs):
            </label>
            <input
              type="number"
              value={overlap}
              onChange={(e) => setOverlap(parseInt(e.target.value))}
              className="w-full p-2 border rounded"
            />
          </div>
        </div>
      </div>

      {/* Error Display */}
      {error && (
        <div className="mb-6 p-4 bg-red-50 border border-red-200 rounded-lg text-red-700">
          {error}
        </div>
      )}

      {/* Continue Button */}
      <button
        onClick={handleContinue}
        disabled={loading || !text.trim()}
        className="w-full bg-blue-600 text-white py-3 px-6 rounded-lg font-semibold hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed"
      >
        {loading ? 'Processing...' : 'Continue to Chunk Preview →'}
      </button>
    </div>
  );
}

export default SetupPage;
```

## Docker Configuration

### `docker-compose.yml`

```yaml
version: '3.8'

services:
  backend:
    build:
      context: ./backend
      dockerfile: Dockerfile
    ports:
      - "5000:5000"
    volumes:
      # Mount source code for development
      - ./src:/app/src:ro
      - ./tests:/app/tests:ro
    environment:
      - FLASK_ENV=production
      - PYTHONUNBUFFERED=1
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:5000/health"]
      interval: 30s
      timeout: 10s
      retries: 3

  frontend:
    build:
      context: ./frontend
      dockerfile: Dockerfile
    ports:
      - "3000:80"
    depends_on:
      - backend
    environment:
      - REACT_APP_API_URL=http://localhost:5000/api
```

### `frontend/Dockerfile`

```dockerfile
# Build stage
FROM node:18-alpine AS build

WORKDIR /app

COPY package*.json ./
RUN npm ci --silent

COPY . .
RUN npm run build

# Production stage
FROM nginx:alpine

COPY --from=build /app/build /usr/share/nginx/html

# Custom nginx config for React Router
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
```

### `frontend/nginx.conf`

```nginx
server {
    listen 80;
    server_name localhost;

    root /usr/share/nginx/html;
    index index.html;

    # React Router support
    location / {
        try_files $uri $uri/ /index.html;
    }

    # Proxy API requests to backend
    location /api {
        proxy_pass http://backend:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }
}
```

## Quick Start Commands

### Initialize Backend
```bash
mkdir -p backend/api backend/tests
cd backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

### Initialize Frontend
```bash
npx create-react-app frontend
cd frontend
npm install zustand axios react-router-dom lucide-react
npm install -D tailwindcss postcss autoprefixer
npx tailwindcss init
npm start
```

### Run with Docker
```bash
docker-compose up --build
# Access at http://localhost:3000
```

---

**Next**: Follow the MVP Roadmap week-by-week to build each component.
