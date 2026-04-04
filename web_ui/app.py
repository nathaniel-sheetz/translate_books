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
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, make_response, render_template, request, send_from_directory, session

from web_ui.i18n import get_strings

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
        context_language: str = "both",
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
        self.context_language = context_language

        # Load all chunks from directory
        self.chunks = self._load_all_chunks()
        self.template = load_prompt_template()

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
        # Look up the sequentially previous chunk (by sorted position, not by
        # most-recently-translated) so context always comes from the right place.
        idx = self.chunks.index(chunk)
        if idx > 0:
            prev_chunk = self.chunks[idx - 1]
            prev_source = prev_chunk.source_text
            prev_translated = prev_chunk.translated_text
        else:
            # First chunk — use previous chapter file if configured
            prev_source = self.previous_chapter if self.include_context else None
            prev_translated = None

        prev_context = extract_previous_chapter_context(
            prev_source,
            previous_translated_text=prev_translated,
            context_language=self.context_language,
            min_paragraphs=self.context_paragraphs,
            min_chars=self.min_context_chars,
            source_language=self.source_language,
            target_language=self.target_language,
        ) if prev_source or prev_translated else ""

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

        # Update in-memory list
        idx = next(i for i, c in enumerate(self.chunks) if c.id == chunk_id)
        self.chunks[idx] = updated_chunk

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
        previous_chapter_path = data.get("previous_chapter_path")
        include_context = data.get("include_context", True)
        context_paragraphs = data.get("context_paragraphs", 3)
        min_context_chars = data.get("min_context_chars", 200)
        context_language = data.get("context_language", "both")
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

        # Load previous chapter text if provided
        previous_chapter = None
        if previous_chapter_path and Path(previous_chapter_path).exists():
            try:
                previous_chapter = Path(previous_chapter_path).read_text(encoding='utf-8')
            except Exception as e:
                return jsonify({"error": f"Failed to load previous chapter: {e}"}), 400

        # Create session
        try:
            trans_session = TranslationSession(
                chunks_dir=chunks_dir,
                glossary=glossary,
                style_guide=style_guide,
                previous_chapter=previous_chapter,
                include_context=include_context,
                context_paragraphs=context_paragraphs,
                min_context_chars=min_context_chars,
                project_name=project_name,
                source_language=source_language,
                target_language=target_language,
                context_language=context_language,
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


# ============================================================================
# Reader Mode Routes
# ============================================================================


def _get_projects_dir() -> Path:
    """Return the projects/ directory relative to project root."""
    return _PROJECT_ROOT / "projects"


def _safe_id(value: str) -> bool:
    """Return False if ID contains path traversal characters."""
    return bool(value) and ".." not in value and "/" not in value and "\\" not in value


def _get_ui_lang() -> str:
    """Read UI language from cookie, default to English."""
    return request.cookies.get("reader_lang", "en")


def _reader_strings() -> dict:
    """Get i18n strings for the current request."""
    return get_strings(_get_ui_lang())


@app.route("/api/set-lang", methods=["POST"])
def set_language():
    """Set the UI language via cookie."""
    lang = (request.json or {}).get("lang", "en")
    if lang not in ("en", "es"):
        lang = "en"
    resp = make_response(jsonify({"lang": lang}))
    resp.set_cookie("reader_lang", lang, max_age=365 * 24 * 3600, samesite="Lax")
    return resp


@app.route("/read/")
def reader_projects():
    """List available projects that have alignment data."""
    t = _reader_strings()
    projects_dir = _get_projects_dir()
    if not projects_dir.exists():
        return render_template("reader.html", mode="no_projects", t=t, lang=_get_ui_lang())

    projects = []
    for proj_dir in sorted(projects_dir.iterdir()):
        if not proj_dir.is_dir():
            continue
        align_dir = proj_dir / "alignments"
        if align_dir.exists():
            alignment_files = sorted(align_dir.glob("*.json"))
            if alignment_files:
                projects.append({
                    "id": proj_dir.name,
                    "chapter_count": len(alignment_files),
                })

    return render_template("reader.html", mode="projects", projects=projects, t=t, lang=_get_ui_lang())


@app.route("/read/<project_id>")
def reader_chapters(project_id):
    """List chapters with alignments for a project."""
    if not _safe_id(project_id):
        return "Bad request", 400
    align_dir = _get_projects_dir() / project_id / "alignments"
    t = _reader_strings()
    if not align_dir.exists():
        return render_template("reader.html", mode="not_found", project_id=project_id, t=t, lang=_get_ui_lang()), 404

    # Load all annotations for this project
    project_dir = _get_projects_dir() / project_id
    all_annotations = {}  # chapter_id -> {type -> count}
    ann_path = project_dir / "annotations.jsonl"
    if ann_path.exists():
        live = {}  # es_idx -> record (latest wins, removed deletes)
        for line in ann_path.read_text(encoding="utf-8").strip().split("\n"):
            if not line.strip():
                continue
            r = json.loads(line)
            ch = r.get("chapter_id", "")
            key = (ch, r.get("es_idx"))
            if r.get("removed"):
                live.pop(key, None)
            else:
                live[key] = r
        from collections import defaultdict
        ann_counts = defaultdict(lambda: defaultdict(int))
        for (ch, _), r in live.items():
            ann_counts[ch][r.get("type", "flag")] += 1
        all_annotations = dict(ann_counts)

    # Check for pending corrections
    has_corrections = (project_dir / "corrections.jsonl").exists()

    # Load reviewed status
    reviewed = _load_reviewed(project_dir)

    chapters = []
    for f in sorted(align_dir.glob("*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            ch_id = f.stem
            confidence = data.get("high_confidence_pct", 0)
            ann = all_annotations.get(ch_id, {})
            review_count = ann.get("word_choice", 0) + ann.get("inconsistency", 0)
            footnote_count = ann.get("footnote", 0)
            flag_count = ann.get("flag", 0)
            total_ann = sum(ann.values())

            chapters.append({
                "id": ch_id,
                "confidence": confidence,
                "low_confidence": confidence < 90,
                "review_count": review_count,
                "footnote_count": footnote_count,
                "flag_count": flag_count,
                "total_ann": total_ann,
                "reviewed": ch_id in reviewed,
            })
        except (json.JSONDecodeError, OSError):
            continue

    return render_template(
        "reader.html", mode="chapters",
        project_id=project_id, chapters=chapters,
        has_corrections=has_corrections, t=t, lang=_get_ui_lang(),
    )


@app.route("/read/<project_id>/<chapter>")
def reader_view(project_id, chapter):
    """Render the reader view for a chapter."""
    t = _reader_strings()
    if not _safe_id(project_id) or not _safe_id(chapter):
        return "Bad request", 400
    align_path = _get_projects_dir() / project_id / "alignments" / f"{chapter}.json"
    if not align_path.exists():
        return render_template(
            "reader.html", mode="not_found",
            project_id=project_id, chapter=chapter, t=t, lang=_get_ui_lang(),
        ), 404

    return render_template(
        "reader.html", mode="read",
        project_id=project_id, chapter=chapter, t=t, lang=_get_ui_lang(),
    )


@app.route("/api/alignment/<project_id>/<chapter>")
def get_alignment(project_id, chapter):
    """Return alignment JSON for a chapter, enriched with paragraph breaks."""
    if not _safe_id(project_id) or not _safe_id(chapter):
        return jsonify({"error": "Invalid ID"}), 400
    projects_dir = _get_projects_dir()
    align_path = projects_dir / project_id / "alignments" / f"{chapter}.json"
    if not align_path.exists():
        return jsonify({"error": f"Alignment not found: {project_id}/{chapter}"}), 404

    try:
        with open(align_path, encoding="utf-8") as f:
            data = json.load(f)

        # Enrich with paragraph break info from combined chapter text
        # Check chapters/ (orchestrator output) first, then translated/ (legacy)
        chapter_text_path = projects_dir / project_id / "chapters" / f"{chapter}.txt"
        if not chapter_text_path.exists():
            chapter_text_path = projects_dir / project_id / "translated" / f"{chapter}.txt"
        if chapter_text_path.exists():
            _enrich_alignment(data, chapter_text_path, project_id)

        return jsonify(data)
    except (json.JSONDecodeError, OSError) as e:
        return jsonify({"error": str(e)}), 500


_IMAGE_PLACEHOLDER_RE = re.compile(r"\[IMAGE:(images/[^:\]]+)(?::([^\]]*))?\]")


def _enrich_alignment(alignment_data: dict, chapter_text_path: Path, project_id: str):
    """Enrich alignment records with paragraph breaks and inline images.

    Parses the combined chapter text to detect paragraph boundaries and
    [IMAGE:...] placeholders, then tags alignment records with para_start
    and inserts image records at the correct positions.
    """
    text = chapter_text_path.read_text(encoding="utf-8")

    # Split into paragraphs (separated by blank lines)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) <= 1:
        return

    # Build ordered list of paragraph events: either a text paragraph
    # (with its first-2-words key) or an image placeholder.
    # Skip the first paragraph since it never gets a para_start marker.
    events = []  # list of ("para", first_2_words) or ("image", src, alt)
    for i in range(1, len(paragraphs)):
        para = paragraphs[i]
        img_match = _IMAGE_PLACEHOLDER_RE.fullmatch(para)
        if img_match:
            src = img_match.group(1)  # e.g. "images/i010.jpg"
            alt = img_match.group(2) or ""
            events.append(("image", src, alt))
        else:
            words = para.split()[:2]
            if words:
                events.append(("para", " ".join(words)))

    # Filter out [IMAGE:...] placeholder sentences from alignment records.
    # The aligner treats them as sentences but they're not readable text.
    alignments = [
        a for a in alignment_data.get("alignments", [])
        if not _IMAGE_PLACEHOLDER_RE.fullmatch(a.get("es", "").strip())
    ]
    alignment_data["alignments"] = alignments

    # Walk alignment records and match events in order.
    # For "para" events, tag the matching sentence with para_start.
    # Pending images are flushed just before the next para_start match
    # so they render between paragraphs.
    event_idx = 0
    insert_queue = []  # (alignment_list_index, image_record)
    pending_images = []  # image records waiting for next para match

    for ai, a in enumerate(alignments):
        if event_idx >= len(events):
            break

        es_text = a.get("es", "").strip()
        if not es_text:
            continue
        es_words = " ".join(es_text.split()[:2])

        # Drain leading image events
        while event_idx < len(events) and events[event_idx][0] == "image":
            _, src, alt = events[event_idx]
            pending_images.append({
                "type": "image",
                "src": f"/projects/{project_id}/{src}",
                "alt": alt,
            })
            event_idx += 1

        # Check for paragraph match
        if event_idx < len(events) and events[event_idx][0] == "para":
            event_key = events[event_idx][1]
            matched = False

            if es_words == event_key:
                # Exact 2-word match
                matched = True
            elif len(event_key.split()) >= 2:
                # Try first-word fallback only when the 2-word key has no
                # exact match anywhere ahead (avoids grabbing wrong sentence)
                event_first = event_key.split()[0]
                es_first = es_text.split()[0] if es_text else ""
                if event_first and es_first == event_first:
                    # Check if an exact 2-word match exists later
                    has_exact_later = any(
                        " ".join(alignments[j].get("es", "").split()[:2]) == event_key
                        for j in range(ai + 1, len(alignments))
                    )
                    if not has_exact_later:
                        matched = True

            if matched:
                # Flush pending images before this paragraph starts
                for img in pending_images:
                    insert_queue.append((ai, img))
                pending_images = []

                a["para_start"] = True
                event_idx += 1

    # Flush any remaining pending images at the end
    for img in pending_images:
        insert_queue.append((len(alignments), img))

    # Drain any remaining image events
    while event_idx < len(events):
        if events[event_idx][0] == "image":
            _, src, alt = events[event_idx]
            insert_queue.append((len(alignments), {
                "type": "image",
                "src": f"/projects/{project_id}/{src}",
                "alt": alt,
            }))
        event_idx += 1

    # Insert image records (reverse order to preserve indices)
    for insert_idx, img_record in reversed(insert_queue):
        alignments.insert(insert_idx, img_record)


@app.route("/projects/<project_id>/images/<path:filename>")
def serve_project_image(project_id, filename):
    """Serve an image file from a project's images/ directory."""
    if not _safe_id(project_id):
        return "Bad request", 400
    images_dir = _get_projects_dir() / project_id / "images"
    if not images_dir.exists():
        return jsonify({"error": "Images directory not found"}), 404
    return send_from_directory(str(images_dir), filename)


@app.route("/api/correction", methods=["POST"])
def save_correction():
    """Save a correction and patch the alignment file."""
    try:
        data = request.json
        project_id = data.get("project_id")
        chapter_id = data.get("chapter_id")
        es_idx = data.get("es_idx")
        original_es = data.get("original_es")
        corrected_es = data.get("corrected_es")
        en_reference = data.get("en_reference")

        if not all([project_id, chapter_id, es_idx is not None, original_es, corrected_es]):
            return jsonify({"error": "Missing required fields"}), 400
        es_idx = int(es_idx)
        if not _safe_id(project_id) or not _safe_id(chapter_id):
            return jsonify({"error": "Invalid ID"}), 400

        projects_dir = _get_projects_dir()
        project_dir = projects_dir / project_id

        if not project_dir.exists():
            return jsonify({"error": f"Project not found: {project_id}"}), 404

        # 1. Append to corrections.jsonl
        corrections_path = project_dir / "corrections.jsonl"
        correction_record = {
            "project_id": project_id,
            "chapter_id": chapter_id,
            "es_idx": es_idx,
            "original_es": original_es,
            "corrected_es": corrected_es,
            "en_reference": en_reference or "",
            "timestamp": datetime.now().isoformat(),
        }

        # Read alignment to get chunk_id for this es_idx
        align_path = project_dir / "alignments" / f"{chapter_id}.json"
        chunk_id = None
        if align_path.exists():
            with open(align_path, encoding="utf-8") as f:
                alignment = json.load(f)

            # Find the alignment record and get chunk_id
            for a in alignment.get("alignments", []):
                if a.get("es_idx") == es_idx:
                    chunk_id = a.get("chunk_id")
                    break

            # 2. Patch the alignment JSON in-place
            patched = False
            for a in alignment.get("alignments", []):
                if a.get("es_idx") == es_idx:
                    a["es"] = corrected_es
                    a["corrected"] = True
                    patched = True
                    break

            if patched:
                with open(align_path, "w", encoding="utf-8") as f:
                    json.dump(alignment, f, ensure_ascii=False, indent=2)

        correction_record["chunk_id"] = chunk_id or ""

        with open(corrections_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(correction_record, ensure_ascii=False) + "\n")

        return jsonify({"saved": True, "chunk_id": chunk_id})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _load_annotations(project_dir: Path, chapter_id: str) -> dict[int, dict]:
    """Load annotations for a chapter, keyed by es_idx. Latest per es_idx wins."""
    annotations_path = project_dir / "annotations.jsonl"
    if not annotations_path.exists():
        return {}

    by_idx: dict[int, dict] = {}
    for line in annotations_path.read_text(encoding="utf-8").strip().split("\n"):
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("chapter_id") != chapter_id:
            continue
        if record.get("removed"):
            by_idx.pop(record.get("es_idx"), None)
        else:
            by_idx[record["es_idx"]] = record
    return by_idx


@app.route("/api/annotations/<project_id>/<chapter>")
def get_annotations(project_id, chapter):
    """Return annotations for a chapter."""
    if not _safe_id(project_id) or not _safe_id(chapter):
        return jsonify({"error": "Invalid ID"}), 400
    projects_dir = _get_projects_dir()
    project_dir = projects_dir / project_id
    if not project_dir.exists():
        return jsonify({"error": f"Project not found: {project_id}"}), 404

    annotations = _load_annotations(project_dir, chapter)
    return jsonify({"annotations": list(annotations.values())})


@app.route("/api/annotation", methods=["POST"])
def save_annotation():
    """Create or update a sentence-level annotation."""
    try:
        data = request.json
        project_id = data.get("project_id")
        chapter_id = data.get("chapter_id")
        es_idx = data.get("es_idx")
        ann_type = data.get("type", "flag")
        content = data.get("content", "")

        if not all([project_id, chapter_id, es_idx is not None]):
            return jsonify({"error": "Missing required fields"}), 400
        if not _safe_id(project_id) or not _safe_id(chapter_id):
            return jsonify({"error": "Invalid ID"}), 400

        projects_dir = _get_projects_dir()
        project_dir = projects_dir / project_id
        if not project_dir.exists():
            return jsonify({"error": f"Project not found: {project_id}"}), 404

        record = {
            "project_id": project_id,
            "chapter_id": chapter_id,
            "es_idx": es_idx,
            "type": ann_type,
            "content": content or "",
            "timestamp": datetime.now().isoformat(),
        }

        annotations_path = project_dir / "annotations.jsonl"
        with open(annotations_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        return jsonify({"saved": True})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/annotation", methods=["DELETE"])
def remove_annotation():
    """Remove an annotation by marking it as removed."""
    try:
        data = request.json
        project_id = data.get("project_id")
        chapter_id = data.get("chapter_id")
        es_idx = data.get("es_idx")

        if not all([project_id, chapter_id, es_idx is not None]):
            return jsonify({"error": "Missing required fields"}), 400
        if not _safe_id(project_id) or not _safe_id(chapter_id):
            return jsonify({"error": "Invalid ID"}), 400

        projects_dir = _get_projects_dir()
        project_dir = projects_dir / project_id
        if not project_dir.exists():
            return jsonify({"error": f"Project not found: {project_id}"}), 404

        record = {
            "project_id": project_id,
            "chapter_id": chapter_id,
            "es_idx": es_idx,
            "removed": True,
            "timestamp": datetime.now().isoformat(),
        }

        annotations_path = project_dir / "annotations.jsonl"
        with open(annotations_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        return jsonify({"removed": True})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _load_reviewed(project_dir: Path) -> dict:
    """Load reviewed.json → {chapter_id: timestamp}."""
    p = project_dir / "reviewed.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


@app.route("/api/reviewed/<project_id>/<chapter>", methods=["GET"])
def get_reviewed(project_id, chapter):
    """Check if a chapter is reviewed."""
    if not _safe_id(project_id) or not _safe_id(chapter):
        return jsonify({"error": "Invalid ID"}), 400
    project_dir = _get_projects_dir() / project_id
    reviewed = _load_reviewed(project_dir)
    return jsonify({"reviewed": chapter in reviewed})


@app.route("/api/reviewed/<project_id>/<chapter>", methods=["POST"])
def mark_reviewed(project_id, chapter):
    """Mark a chapter as reviewed."""
    if not _safe_id(project_id) or not _safe_id(chapter):
        return jsonify({"error": "Invalid ID"}), 400
    project_dir = _get_projects_dir() / project_id
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404

    reviewed = _load_reviewed(project_dir)
    reviewed[chapter] = datetime.now().isoformat()
    (project_dir / "reviewed.json").write_text(
        json.dumps(reviewed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return jsonify({"marked": True})


@app.route("/api/reviewed/<project_id>/<chapter>", methods=["DELETE"])
def unmark_reviewed(project_id, chapter):
    """Unmark a chapter as reviewed."""
    if not _safe_id(project_id) or not _safe_id(chapter):
        return jsonify({"error": "Invalid ID"}), 400
    project_dir = _get_projects_dir() / project_id
    reviewed = _load_reviewed(project_dir)
    reviewed.pop(chapter, None)
    (project_dir / "reviewed.json").write_text(
        json.dumps(reviewed, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return jsonify({"unmarked": True})


if __name__ == "__main__":
    print("=" * 70)
    print("Translation Web UI")
    print("=" * 70)
    print("\nStarting server on http://localhost:5000")
    print("Press Ctrl+C to stop\n")
    app.run(debug=True, host="0.0.0.0", port=5000)
