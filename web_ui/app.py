"""
Flask web UI for chunk-by-chunk translation workflow.

Provides a simple web interface for translating book chunks:
- Auto-loads next untranslated chunk
- Renders complete prompts with one-click copy
- Saves translations directly to JSON files
- Auto-advances to next chunk after save
"""

import glob
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request, session

# Import existing utilities
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import Chunk, ChunkStatus, Glossary, StyleGuide
from src.utils.file_io import (
    format_glossary_for_prompt,
    load_chunk,
    load_glossary,
    load_prompt_template,
    load_style_guide,
    render_prompt,
    save_chunk,
)
from src.translator import extract_previous_chapter_context

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)  # For session management

# In-memory session storage (simple approach for local-only use)
translation_sessions = {}

# Project root is one level up from web_ui/
_PROJECT_ROOT = Path(__file__).parent.parent


def load_project_config() -> dict:
    """Load project.json from project root if it exists, else return empty dict."""
    config_path = _PROJECT_ROOT / "project.json"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


class TranslationSession:
    """Manages state for a translation project session."""

    def __init__(
        self,
        chunks_dir: str,
        glossary: Optional[Glossary] = None,
        style_guide: Optional[StyleGuide] = None,
        previous_chapter: Optional[str] = None,
        include_context: bool = False,
        context_paragraphs: int = 3,
        min_context_chars: int = 200,
        project_name: str = "Translation Project",
        source_language: str = "English",
        target_language: str = "Spanish",
    ):
        self.chunks_dir = chunks_dir
        self.glossary = glossary
        self.style_guide = style_guide
        self.previous_chapter = previous_chapter
        self.include_context = include_context
        self.context_paragraphs = context_paragraphs
        self.min_context_chars = min_context_chars
        self.project_name = project_name
        self.source_language = source_language
        self.target_language = target_language

        # Load all chunks from directory
        self.chunks = self._load_all_chunks()
        self.template = load_prompt_template()

        # Seed from last translated chunk so context survives server restarts
        last_done = next(
            (c for c in reversed(self.chunks) if c.translated_text and c.translated_text.strip()),
            None,
        )
        self.last_source_text: Optional[str] = last_done.source_text if last_done else None

    def _load_all_chunks(self) -> list[Chunk]:
        """Load and sort all chunk JSON files."""
        chunk_files = glob.glob(f"{self.chunks_dir}/*.json")

        if not chunk_files:
            resolved_dir = Path(self.chunks_dir).resolve()
            raise ValueError(
                f"No chunk JSON files found in '{self.chunks_dir}'\n"
                f"Resolved path: {resolved_dir}"
            )

        chunks = []
        for f in chunk_files:
            try:
                chunks.append(load_chunk(Path(f)))
            except Exception as e:
                print(f"Warning: Failed to load chunk {f}: {e}")
                continue

        if not chunks:
            raise ValueError(
                f"Found {len(chunk_files)} JSON files but failed to load any valid chunks from {self.chunks_dir}"
            )

        return sorted(chunks, key=lambda c: (c.chapter_id, c.position))

    def get_next_untranslated_chunk(self) -> Optional[Chunk]:
        """Find first chunk where translated_text is null/empty."""
        for chunk in self.chunks:
            if not chunk.translated_text or chunk.translated_text.strip() == "":
                return chunk
        return None  # All chunks translated

    def render_chunk_prompt(self, chunk: Chunk) -> str:
        """Render complete prompt for a chunk."""
        # Use last chunk's source text as context (auto-threading), or fall back to
        # a static previous chapter source file if configured and no chunk translated yet.
        prev_source = self.last_source_text or (
            self.previous_chapter if self.include_context else None
        )
        prev_context = extract_previous_chapter_context(
            prev_source,
            min_paragraphs=self.context_paragraphs,
            min_chars=self.min_context_chars,
            source_language=self.source_language,
        ) if prev_source else ""

        # Prepare variables
        variables = {
            "book_title": self.project_name,
            "source_text": chunk.source_text,
            "target_language": self.target_language,
            "source_language": self.source_language,
            "glossary": (
                format_glossary_for_prompt(self.glossary)
                if self.glossary
                else "No glossary provided."
            ),
            "style_guide": (
                self.style_guide.content if self.style_guide else "No style guide provided."
            ),
            "context": "",
            "chapter_info": f"Chapter {chunk.chapter_id}, Chunk {chunk.position}",
            "previous_chapter_context": prev_context,
        }

        # Render prompt
        rendered = render_prompt(self.template, variables)

        # Strip header comments (everything before first === separator)
        separator = "=" * 80
        if separator in rendered:
            parts = rendered.split(separator, 1)
            if len(parts) > 1:
                rendered = separator + parts[1]

        return rendered

    def save_translation(self, chunk_id: str, translation: str, source_text: Optional[str] = None) -> bool:
        """Save translation to chunk JSON file."""
        # Find the chunk
        chunk = next((c for c in self.chunks if c.id == chunk_id), None)
        if not chunk:
            return False

        # Update chunk
        chunk_data = chunk.model_dump()
        chunk_data["translated_text"] = translation
        chunk_data["status"] = ChunkStatus.TRANSLATED
        chunk_data["translated_at"] = datetime.now()
        if source_text is not None:
            chunk_data["source_text"] = source_text

        updated_chunk = Chunk(**chunk_data)

        # Save to disk
        chunk_path = Path(self.chunks_dir) / f"{chunk_id}.json"
        save_chunk(updated_chunk, chunk_path)

        # Update in-memory list and track last source text for context threading
        idx = next(i for i, c in enumerate(self.chunks) if c.id == chunk_id)
        self.chunks[idx] = updated_chunk
        self.last_source_text = self.chunks[idx].source_text

        return True

    def get_progress(self) -> dict:
        """Get completion statistics."""
        total = len(self.chunks)
        completed = sum(1 for c in self.chunks if c.translated_text)
        return {
            "total_chunks": total,
            "completed_chunks": completed,
            "remaining_chunks": total - completed,
        }

    def get_chunks_list(self) -> list[dict]:
        """Get list of all chunks with status for navigation."""
        return [
            {
                "chunk_id": chunk.id,
                "chapter_id": chunk.chapter_id,
                "position": chunk.position,
                "has_translation": bool(chunk.translated_text and chunk.translated_text.strip()),
                "status": chunk.status.value,
                "display_status": chunk.display_status,
                "annotation_count": chunk.annotation_count,
                "word_count": chunk.metadata.word_count,
            }
            for chunk in self.chunks
        ]

    def _get_chunk_by_id(self, chunk_id: str) -> Optional[Chunk]:
        """Get chunk by ID."""
        return next((c for c in self.chunks if c.id == chunk_id), None)

    def run_chunk_evaluation(self, chunk_id: str, translation_override: Optional[str] = None) -> dict:
        """
        Run all evaluators on a chunk.

        Args:
            chunk_id: ID of chunk to evaluate
            translation_override: Optional translation text to use instead of saved translation
                                 (useful for evaluating edited text in review mode)

        Returns:
            Dictionary with:
                - results: List of EvalResult objects (as dicts)
                - summary: Aggregated statistics
        """
        from src.evaluators import run_all_evaluators, aggregate_results
        from src.models import EvaluationConfig

        chunk = self._get_chunk_by_id(chunk_id)
        if not chunk:
            raise ValueError(f"Chunk not found: {chunk_id}")

        # Use override if provided (for editing in review mode)
        if translation_override is not None:
            chunk = chunk.model_copy()
            chunk.translated_text = translation_override

        # Configure evaluation
        config = EvaluationConfig(
            enabled_evals=["length", "paragraph", "dictionary", "glossary"],
            fail_on_errors=False
        )

        # Run evaluators
        results = run_all_evaluators(chunk, config, self.glossary)
        summary = aggregate_results(results)

        return {
            "results": [r.model_dump(mode='json') for r in results],
            "summary": summary
        }

    def save_chunk_to_disk(self, chunk: Chunk):
        """Save updated chunk to its JSON file."""
        chunk_path = Path(self.chunks_dir) / f"{chunk.id}.json"
        save_chunk(chunk, chunk_path)

        # Update in-memory list
        for i, c in enumerate(self.chunks):
            if c.id == chunk.id:
                self.chunks[i] = chunk
                break

    def chunk_to_dict(self, chunk: Chunk) -> dict:
        """Convert chunk to dict for JSON response."""
        return {
            "chunk_id": chunk.id,
            "position": chunk.position,
            "total_chunks": len(self.chunks),
            "chapter_id": chunk.chapter_id,
            "source_text": chunk.source_text,
            "translated_text": chunk.translated_text or "",
            "word_count": chunk.metadata.word_count,
            "paragraph_count": chunk.metadata.paragraph_count,
            "rendered_prompt": self.render_chunk_prompt(chunk),
            "has_next": chunk.position < len(self.chunks),
        }


# ============================================================================
# Flask Routes
# ============================================================================


@app.route("/")
def index():
    """Serve main UI page."""
    return render_template("index.html", defaults=load_project_config())


@app.route("/api/load-project", methods=["POST"])
def load_project():
    """Load project and initialize session."""
    try:
        data = request.json

        # Required fields
        chunks_dir = data.get("chunks_dir")
        if not chunks_dir:
            return jsonify({"error": "chunks_dir is required"}), 400

        # Normalize and check if directory exists
        chunks_path = Path(chunks_dir).resolve()
        if not chunks_path.exists():
            return jsonify({
                "error": f"Directory not found: {chunks_dir}\nResolved to: {chunks_path}\nCurrent working directory: {Path.cwd()}"
            }), 400

        if not chunks_path.is_dir():
            return jsonify({"error": f"Path is not a directory: {chunks_dir}"}), 400

        # Use the string representation of the original path (not resolved) for session
        # This preserves relative paths for glob patterns

        # Optional fields
        glossary_path = data.get("glossary_path")
        style_guide_path = data.get("style_guide_path")
        include_context = data.get("include_context", True)
        context_paragraphs = data.get("context_paragraphs", 3)
        project_name = data.get("project_name", "Translation Project")
        source_language = data.get("source_language", "English")
        target_language = data.get("target_language", "Spanish")

        # Load optional files
        glossary = None
        if glossary_path and Path(glossary_path).exists():
            try:
                glossary = load_glossary(Path(glossary_path))
            except Exception as e:
                return jsonify({"error": f"Failed to load glossary: {e}"}), 400

        style_guide = None
        if style_guide_path and Path(style_guide_path).exists():
            try:
                style_guide = load_style_guide(Path(style_guide_path))
            except Exception as e:
                return jsonify({"error": f"Failed to load style guide: {e}"}), 400

        # Create session
        try:
            trans_session = TranslationSession(
                chunks_dir=chunks_dir,
                glossary=glossary,
                style_guide=style_guide,
                previous_chapter=None,
                include_context=include_context,
                context_paragraphs=context_paragraphs,
                project_name=project_name,
                source_language=source_language,
                target_language=target_language,
            )
        except Exception as e:
            return jsonify({"error": f"Failed to create session: {e}"}), 500

        # Generate session ID and store
        session_id = secrets.token_hex(16)
        translation_sessions[session_id] = trans_session

        # Get progress and next chunk
        progress = trans_session.get_progress()
        next_chunk = trans_session.get_next_untranslated_chunk()

        # Get all chunks list for navigation
        chunks_list = trans_session.get_chunks_list()

        response = {
            "session_id": session_id,
            "total_chunks": progress["total_chunks"],
            "completed_chunks": progress["completed_chunks"],
            "chunks_list": chunks_list,
        }

        if next_chunk:
            response["next_chunk"] = trans_session.chunk_to_dict(next_chunk)
        else:
            response["all_complete"] = True

        return jsonify(response)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/next-chunk", methods=["GET"])
def next_chunk():
    """Get next untranslated chunk with rendered prompt."""
    session_id = request.args.get("session_id")

    if not session_id or session_id not in translation_sessions:
        return jsonify({"error": "Invalid or expired session"}), 400

    trans_session = translation_sessions[session_id]
    chunk = trans_session.get_next_untranslated_chunk()

    if not chunk:
        return jsonify({"all_complete": True})

    return jsonify(trans_session.chunk_to_dict(chunk))


@app.route("/api/save-translation", methods=["POST"])
def save_translation():
    """Save translation and get next chunk."""
    try:
        data = request.json
        session_id = data.get("session_id")
        chunk_id = data.get("chunk_id")
        translation = data.get("translation")

        if not session_id or session_id not in translation_sessions:
            return jsonify({"error": "Invalid or expired session"}), 400

        if not chunk_id or not translation:
            return jsonify({"error": "chunk_id and translation are required"}), 400

        trans_session = translation_sessions[session_id]
        source_text = data.get("source_text")  # optional, sent by review mode when source paragraphs are edited

        # Save translation
        success = trans_session.save_translation(chunk_id, translation, source_text)

        if not success:
            return jsonify({"error": f"Failed to save translation for {chunk_id}"}), 500

        # Get next chunk
        next_chunk = trans_session.get_next_untranslated_chunk()

        response = {"saved": True}

        if next_chunk:
            response["next_chunk"] = trans_session.chunk_to_dict(next_chunk)
        else:
            response["all_complete"] = True
            progress = trans_session.get_progress()
            response["total_chunks"] = progress["total_chunks"]

        return jsonify(response)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/evaluate-chunk", methods=["POST"])
def evaluate_chunk():
    """Run evaluators on a chunk and return results."""
    try:
        data = request.json
        session_id = data.get("session_id")
        chunk_id = data.get("chunk_id")
        translation_override = data.get("translation_override")

        if not session_id or session_id not in translation_sessions:
            return jsonify({"error": "Invalid or expired session"}), 400

        if not chunk_id:
            return jsonify({"error": "chunk_id is required"}), 400

        trans_session = translation_sessions[session_id]

        # Run evaluation
        results = trans_session.run_chunk_evaluation(chunk_id, translation_override)

        return jsonify(results)

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/save-annotations", methods=["POST"])
def save_annotations():
    """Save annotations to chunk JSON."""
    try:
        data = request.json
        session_id = data.get("session_id")
        chunk_id = data.get("chunk_id")
        annotations_data = data.get("annotations", [])

        if not session_id or session_id not in translation_sessions:
            return jsonify({"error": "Invalid or expired session"}), 400

        if not chunk_id:
            return jsonify({"error": "chunk_id is required"}), 400

        trans_session = translation_sessions[session_id]
        chunk = trans_session._get_chunk_by_id(chunk_id)

        if not chunk:
            return jsonify({"error": f"Chunk not found: {chunk_id}"}), 400

        # Import models
        from src.models import Annotation, ChunkReviewData

        # Initialize review_data if needed
        if not chunk.review_data:
            chunk.review_data = ChunkReviewData()

        # Replace annotations
        chunk.review_data.annotations = [
            Annotation.model_validate(a) for a in annotations_data
        ]

        # Save chunk to disk
        trans_session.save_chunk_to_disk(chunk)

        return jsonify({"saved": True, "count": len(chunk.review_data.annotations)})

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/get-chunk", methods=["GET"])
def get_chunk():
    """Get a specific chunk by ID (for review mode or navigation)."""
    try:
        session_id = request.args.get("session_id")
        chunk_id = request.args.get("chunk_id")

        if not session_id or session_id not in translation_sessions:
            return jsonify({"error": "Invalid or expired session"}), 400

        if not chunk_id:
            return jsonify({"error": "chunk_id is required"}), 400

        trans_session = translation_sessions[session_id]
        chunk = trans_session._get_chunk_by_id(chunk_id)

        if not chunk:
            return jsonify({"error": f"Chunk not found: {chunk_id}"}), 400

        # Convert to dict with all fields including review_data
        chunk_dict = trans_session.chunk_to_dict(chunk)

        # Add source and translated text for review mode
        chunk_dict["source_text"] = chunk.source_text
        chunk_dict["translated_text"] = chunk.translated_text

        # Add review data if present
        if chunk.review_data:
            chunk_dict["review_data"] = chunk.review_data.model_dump(mode='json')

        return jsonify(chunk_dict)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/load-chunk", methods=["GET"])
def api_load_chunk():
    """Load a specific chunk for translation (for navigation)."""
    try:
        session_id = request.args.get("session_id")
        chunk_id = request.args.get("chunk_id")

        if not session_id or session_id not in translation_sessions:
            return jsonify({"error": "Invalid or expired session"}), 400

        if not chunk_id:
            return jsonify({"error": "chunk_id is required"}), 400

        trans_session = translation_sessions[session_id]
        chunk = trans_session._get_chunk_by_id(chunk_id)

        if not chunk:
            return jsonify({"error": f"Chunk not found: {chunk_id}"}), 400

        return jsonify(trans_session.chunk_to_dict(chunk))

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("=" * 70)
    print("Translation Web UI")
    print("=" * 70)
    print("\nStarting server on http://localhost:5000")
    print("Press Ctrl+C to stop\n")
    app.run(debug=True, port=5000)
