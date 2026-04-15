"""
Flask web UI for the book translation pipeline.

Provides the pipeline dashboard and bilingual reader:
- Project list with status cards
- 8-stage pipeline dashboard (Source → Split → Chunk → Style Guide → Glossary → Translate → Review → Export)
- Bilingual sentence-aligned reader with annotations and corrections
"""

import json
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, make_response, redirect, render_template, request, send_from_directory

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

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)  # For session management


def _strip_json_fences(text: str) -> str:
    """Strip markdown code fences from LLM output before JSON parsing."""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()

# Project root is one level up from web_ui/
_PROJECT_ROOT = Path(__file__).parent.parent


# ============================================================================
# Flask Routes
# ============================================================================


@app.route("/")
def index():
    """Redirect to the project list."""
    return redirect("/read/")


# ============================================================================
# Reader Mode Routes
# ============================================================================


def _get_projects_dir() -> Path:
    """Return the projects/ directory relative to project root."""
    return _PROJECT_ROOT / "projects"


def _load_project_config(project_id: str) -> dict:
    """Load per-project config from projects/<id>/project.json."""
    config_path = _get_projects_dir() / project_id / "project.json"
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_project_config(project_id: str, config: dict) -> None:
    """Save per-project config to projects/<id>/project.json."""
    config_path = _get_projects_dir() / project_id / "project.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def _project_title(project_id: str) -> str:
    """Return the display title for a project, falling back to the folder name."""
    return _load_project_config(project_id).get("title") or project_id


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


_VALID_STATUSES = {"pending", "in_progress", "complete", "archived"}


@app.route("/api/project/<project_id>/status", methods=["PATCH"])
def update_project_status(project_id):
    """Update the status field in a project's config."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    status = (request.json or {}).get("status", "")
    if status not in _VALID_STATUSES:
        return jsonify({"error": f"Invalid status. Must be one of: {', '.join(sorted(_VALID_STATUSES))}"}), 400
    config = _load_project_config(project_id)
    config["status"] = status
    _save_project_config(project_id, config)
    return jsonify({"ok": True, "status": status})


# ============================================================================
# LLM config endpoint
# ============================================================================


@app.route("/api/llm-config")
def api_llm_config():
    """Return the LLM provider/model config for the frontend.

    Strips ``api_key_env_var`` for security and adds an ``available``
    flag per provider indicating whether the API key is set.
    """
    import os, copy
    from src.api_translator import load_llm_config

    config = copy.deepcopy(load_llm_config())
    for provider in config.get("providers", []):
        env_var = provider.pop("api_key_env_var", None)
        provider["available"] = bool(os.getenv(env_var)) if env_var else False
    return jsonify(config)


# ============================================================================
# Setup routes — style guide wizard + glossary bootstrap
# ============================================================================


@app.route("/api/setup/<project_id>/prompts/questions", methods=["POST"])
def setup_questions_prompt(project_id):
    """Return the full prompt for LLM question generation (for copy/paste)."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id

    from src.style_guide_wizard import load_fixed_questions, build_question_prompt, load_source_sample
    data = request.get_json()
    answers = data.get("answers", {})
    # Convert string indices back to int
    answers = {k: int(v) if isinstance(v, str) and v.isdigit() else v for k, v in answers.items()}

    fixed_questions = load_fixed_questions()
    source_text = load_source_sample(project_dir)
    target_lang = data.get("target_lang", "Spanish")
    locale = data.get("locale", "mx")

    prompt = build_question_prompt(source_text, target_lang, locale, fixed_questions, answers)
    return jsonify({"prompt": prompt})


@app.route("/api/setup/<project_id>/prompts/style-guide", methods=["POST"])
def setup_style_guide_prompt(project_id):
    """Return the full prompt for style guide generation (for copy/paste)."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id

    from src.style_guide_wizard import (
        load_fixed_questions, build_style_guide_prompt, load_source_sample,
    )
    data = request.get_json()
    answers = data.get("answers", {})
    answers = {k: int(v) if isinstance(v, str) and v.isdigit() else v for k, v in answers.items()}
    extra_questions = data.get("extra_questions", [])

    fixed_questions = load_fixed_questions()
    all_questions = fixed_questions + extra_questions
    source_text = load_source_sample(project_dir)
    target_lang = data.get("target_lang", "Spanish")
    locale = data.get("locale", "mx")

    prompt = build_style_guide_prompt(all_questions, answers, source_text, target_lang, locale)
    return jsonify({"prompt": prompt})


@app.route("/api/setup/<project_id>/style-guide", methods=["POST"])
def setup_save_style_guide(project_id):
    """Save style guide content to style.json."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id

    from src.style_guide_wizard import save_style_guide_json
    data = request.get_json()
    content = data.get("content", "")
    if not content.strip():
        return jsonify({"error": "Empty style guide"}), 400

    output_path = project_dir / "style.json"
    save_style_guide_json(content, output_path)
    return jsonify({"ok": True, "path": str(output_path)})


@app.route("/api/setup/<project_id>/style-guide/fallback", methods=["POST"])
def setup_style_guide_fallback(project_id):
    """Generate style guide from answers using fallback (no LLM)."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400

    from src.style_guide_wizard import load_fixed_questions, answers_to_style_guide_fallback
    data = request.get_json()
    answers = data.get("answers", {})
    answers = {k: int(v) if isinstance(v, str) and v.isdigit() else v for k, v in answers.items()}
    extra_questions = data.get("extra_questions", [])

    fixed_questions = load_fixed_questions()
    all_questions = fixed_questions + extra_questions
    content = answers_to_style_guide_fallback(all_questions, answers)
    return jsonify({"content": content})


@app.route("/api/setup/<project_id>/extract-candidates", methods=["POST"])
def setup_extract_candidates(project_id):
    """Run heuristic glossary extraction and return candidates."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id

    from scripts.extract_glossary_candidates import extract_candidates
    from src.style_guide_wizard import load_source_sample
    text = load_source_sample(project_dir, max_words=200000)  # High cap — use all available chunks
    if not text:
        return jsonify({"error": "No source text found (add chunks/ or source.txt)"}), 404

    glossary = None
    glossary_path = project_dir / "glossary.json"
    if glossary_path.exists():
        glossary = load_glossary(glossary_path)

    report = extract_candidates(text, glossary=glossary)
    candidates = [
        {"term": c.term, "type_guess": c.type_guess.value, "frequency": c.frequency,
         "context_sentence": c.context_sentence}
        for c in report.candidates
    ]
    return jsonify({"candidates": candidates, "total": len(candidates)})


@app.route("/api/setup/<project_id>/prompts/glossary", methods=["POST"])
def setup_glossary_prompt(project_id):
    """Return the full prompt for glossary bootstrap (for copy/paste)."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id

    from src.glossary_bootstrap import build_glossary_prompt
    from src.style_guide_wizard import load_source_sample
    data = request.get_json()
    candidates = data.get("candidates", [])
    target_lang = data.get("target_lang", "Spanish")
    glossary_guidance = data.get("glossary_guidance", "")

    # Load style guide if exists
    style_content = ""
    style_path = project_dir / "style.json"
    if style_path.exists():
        try:
            sg = load_style_guide(style_path)
            style_content = sg.content
        except Exception:
            pass

    source_text = load_source_sample(project_dir)
    prompt = build_glossary_prompt(candidates, source_text, style_content, target_lang, glossary_guidance)
    return jsonify({"prompt": prompt})


@app.route("/api/setup/<project_id>/glossary", methods=["POST"])
def setup_save_glossary(project_id):
    """Save glossary terms to glossary.json."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id

    from src.glossary_bootstrap import glossary_terms_from_proposals, proposals_to_glossary
    from src.utils.file_io import save_glossary as _save_glossary
    data = request.get_json()
    terms_data = data.get("terms", [])
    if not terms_data:
        return jsonify({"error": "No terms provided"}), 400

    terms = glossary_terms_from_proposals(terms_data)
    glossary_path = project_dir / "glossary.json"

    # Merge with existing if present
    if glossary_path.exists():
        existing = load_glossary(glossary_path)
        existing_set = {t.english.lower() for t in existing.terms}
        new_terms = [t for t in terms if t.english.lower() not in existing_set]
        existing.terms.extend(new_terms)
        _save_glossary(existing, glossary_path)
        return jsonify({"ok": True, "total": len(existing.terms), "new": len(new_terms)})
    else:
        glossary = proposals_to_glossary(terms)
        _save_glossary(glossary, glossary_path)
        return jsonify({"ok": True, "total": len(terms), "new": len(terms)})


# ============================================================================
# Generate via API endpoints (direct LLM calls from the UI)
# ============================================================================


@app.route("/api/setup/<project_id>/questions/generate", methods=["POST"])
def setup_questions_generate(project_id):
    """Generate additional style-guide questions via LLM (direct API call)."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id

    from src.style_guide_wizard import load_fixed_questions, build_question_prompt, load_source_sample
    from src.api_translator import call_llm

    data = request.get_json()
    answers = data.get("answers", {})
    answers = {k: int(v) if isinstance(v, str) and v.isdigit() else v for k, v in answers.items()}
    provider = data.get("provider", "anthropic")
    model = data.get("model")

    fixed_questions = load_fixed_questions()
    source_text = load_source_sample(project_dir)
    target_lang = data.get("target_lang", "Spanish")
    locale = data.get("locale", "mx")

    prompt = build_question_prompt(source_text, target_lang, locale, fixed_questions, answers)

    try:
        result = call_llm(prompt, provider=provider, model=model, call_type="style_questions")
        # Try to parse as JSON
        questions = json.loads(_strip_json_fences(result))
        return jsonify({"questions": questions})
    except json.JSONDecodeError:
        return jsonify({"raw_text": result, "error": "LLM response was not valid JSON."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/setup/<project_id>/style-guide/generate", methods=["POST"])
def setup_style_guide_generate(project_id):
    """Generate a style guide via LLM (direct API call)."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id

    from src.style_guide_wizard import load_fixed_questions, build_style_guide_prompt, load_source_sample
    from src.api_translator import call_llm

    data = request.get_json()
    answers = data.get("answers", {})
    answers = {k: int(v) if isinstance(v, str) and v.isdigit() else v for k, v in answers.items()}
    extra_questions = data.get("extra_questions", [])
    provider = data.get("provider", "anthropic")
    model = data.get("model")

    fixed_questions = load_fixed_questions()
    all_questions = fixed_questions + extra_questions
    source_text = load_source_sample(project_dir)
    target_lang = data.get("target_lang", "Spanish")
    locale = data.get("locale", "mx")

    prompt = build_style_guide_prompt(all_questions, answers, source_text, target_lang, locale)

    try:
        content = call_llm(prompt, provider=provider, model=model, call_type="style_guide_generate")
        return jsonify({"content": content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/setup/<project_id>/glossary/generate", methods=["POST"])
def setup_glossary_generate(project_id):
    """Generate glossary translations via LLM (direct API call)."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id

    from src.glossary_bootstrap import build_glossary_prompt
    from src.style_guide_wizard import load_source_sample
    from src.api_translator import call_llm

    data = request.get_json()
    candidates = data.get("candidates", [])
    target_lang = data.get("target_lang", "Spanish")
    glossary_guidance = data.get("glossary_guidance", "")
    provider = data.get("provider", "anthropic")
    model = data.get("model")

    # Load style guide if exists
    style_content = ""
    style_path = project_dir / "style.json"
    if style_path.exists():
        try:
            sg = load_style_guide(style_path)
            style_content = sg.content
        except Exception:
            pass

    source_text = load_source_sample(project_dir)
    prompt = build_glossary_prompt(candidates, source_text, style_content, target_lang, glossary_guidance)

    try:
        result = call_llm(prompt, provider=provider, model=model, max_tokens=8192, call_type="glossary")
        # Try to parse as JSON
        terms = json.loads(_strip_json_fences(result))
        return jsonify({"terms": terms})
    except json.JSONDecodeError:
        return jsonify({"raw_text": result, "error": "LLM response was not valid JSON. Showing raw text for manual editing."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/create", methods=["POST"])
def create_project():
    """Create a blank project directory and return its ID."""
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Title is required"}), 400

    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-') or "project"

    projects_dir = _get_projects_dir()
    candidate = slug
    suffix = 2
    while (projects_dir / candidate).exists():
        candidate = f"{slug}-{suffix}"
        suffix += 1
    project_id = candidate

    project_dir = projects_dir / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    _save_project_config(project_id, {"title": title})

    return jsonify({"id": project_id, "redirect": f"/project/{project_id}"})


@app.route("/read/")
def reader_projects():
    """List available projects with status dashboard."""
    t = _reader_strings()
    projects_dir = _get_projects_dir()
    if not projects_dir.exists():
        return render_template("reader.html", mode="no_projects", t=t, lang=_get_ui_lang())

    projects = []
    for proj_dir in sorted(projects_dir.iterdir()):
        if not proj_dir.is_dir():
            continue

        # Include any project dir with chunks/ or source.txt
        has_chunks = (proj_dir / "chunks").exists()
        has_source = (proj_dir / "source.txt").exists()
        if not has_chunks and not has_source:
            continue

        # Count alignment chapters (for Read link)
        align_dir = proj_dir / "alignments"
        alignment_count = len(list(align_dir.glob("*.json"))) if align_dir.exists() else 0

        # Style guide status
        has_style_guide = (proj_dir / "style.json").exists()

        # Glossary status
        glossary_count = 0
        glossary_path = proj_dir / "glossary.json"
        if glossary_path.exists():
            try:
                with open(glossary_path, "r", encoding="utf-8") as f:
                    gdata = json.load(f)
                glossary_count = len(gdata.get("terms", []))
            except (json.JSONDecodeError, OSError):
                pass

        # Translation progress
        total_chunks = 0
        translated_chunks = 0
        chunks_dir = proj_dir / "chunks"
        if chunks_dir.exists():
            for cf in chunks_dir.glob("*_chunk_*.json"):
                total_chunks += 1
                try:
                    with open(cf, "r", encoding="utf-8") as f:
                        cdata = json.load(f)
                    if cdata.get("translated_text"):
                        translated_chunks += 1
                except (json.JSONDecodeError, OSError):
                    pass

        proj_config = _load_project_config(proj_dir.name)
        projects.append({
            "id": proj_dir.name,
            "title": proj_config.get("title") or proj_dir.name,
            "spanish_title": proj_config.get("spanish_title", ""),
            "status": proj_config.get("status", "pending"),
            "chapter_count": alignment_count,
            "has_style_guide": has_style_guide,
            "glossary_count": glossary_count,
            "total_chunks": total_chunks,
            "translated_chunks": translated_chunks,
            "has_alignments": alignment_count > 0,
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

    # Check for pending corrections (file must exist AND have content)
    _corr_path = project_dir / "corrections.jsonl"
    has_corrections = _corr_path.exists() and _corr_path.stat().st_size > 1

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
        project_id=project_id, project_title=_project_title(project_id),
        chapters=chapters,
        has_corrections=has_corrections, t=t, lang=_get_ui_lang(),
    )


@app.route("/read/<project_id>/<chapter>")
def reader_view(project_id, chapter):
    """Render the reader view for a chapter."""
    t = _reader_strings()
    if not _safe_id(project_id) or not _safe_id(chapter):
        return "Bad request", 400
    align_dir = _get_projects_dir() / project_id / "alignments"
    align_path = align_dir / f"{chapter}.json"
    if not align_path.exists():
        return render_template(
            "reader.html", mode="not_found",
            project_id=project_id, chapter=chapter, t=t, lang=_get_ui_lang(),
        ), 404

    # Build prev/next chapter links
    all_chapters = sorted(f.stem for f in align_dir.glob("*.json"))
    idx = all_chapters.index(chapter) if chapter in all_chapters else -1
    prev_chapter = all_chapters[idx - 1] if idx > 0 else None
    next_chapter = all_chapters[idx + 1] if idx < len(all_chapters) - 1 else None

    return render_template(
        "reader.html", mode="read",
        project_id=project_id, project_title=_project_title(project_id),
        chapter=chapter, t=t, lang=_get_ui_lang(),
        prev_chapter=prev_chapter, next_chapter=next_chapter,
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

        # Enrich with paragraph break info from the combined chapter text.
        # chapters/<chapter>.txt is the canonical output of Combine (see
        # project_combine / project_align); align refreshes it before writing
        # alignment JSON, so it should always be in sync here.
        chapter_text_path = projects_dir / project_id / "chapters" / f"{chapter}.txt"
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


@app.route("/api/apply-corrections/<project_id>", methods=["POST"])
def apply_corrections(project_id):
    """Apply pending corrections: patch chunks, recombine, realign."""
    if not _safe_id(project_id):
        return jsonify({"error": "Invalid ID"}), 400

    project_dir = _get_projects_dir() / project_id
    corrections_path = project_dir / "corrections.jsonl"
    if not corrections_path.exists():
        return jsonify({"error": "No corrections to apply"}), 404

    try:
        import time
        from collections import defaultdict

        from scripts.apply_corrections import (
            apply_to_chunk,
            load_corrections,
            realign_chapter,
            recombine_chapter,
        )
        from src.utils.file_io import load_chunk, save_chunk

        corrections = load_corrections(project_dir)
        if not corrections:
            return jsonify({"error": "No corrections found"}), 404

        # Group by chunk
        by_chunk = defaultdict(list)
        for c in corrections:
            chunk_id = c.get("chunk_id", "")
            if chunk_id:
                by_chunk[chunk_id].append(c)

        affected_chapters = set()
        total_applied = 0
        log = []

        # 1. Patch chunks
        for chunk_id, chunk_corrections in sorted(by_chunk.items()):
            chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
            if not chunk_path.exists():
                log.append(f"{chunk_id}: skipped (not found)")
                continue

            chunk = load_chunk(chunk_path)
            updated_chunk, applied = apply_to_chunk(chunk, chunk_corrections)

            chapter_id = chunk_id.rsplit("_chunk_", 1)[0]
            affected_chapters.add(chapter_id)

            if applied > 0:
                save_chunk(updated_chunk, chunk_path)

            total_applied += applied
            log.append(f"{chunk_id}: {applied}/{len(chunk_corrections)}")

        # 2. Recombine affected chapters
        for chapter_id in sorted(affected_chapters):
            recombine_chapter(project_dir, chapter_id)

        # 3. Realign affected chapters
        for chapter_id in sorted(affected_chapters):
            realign_chapter(project_dir, chapter_id)

        # 4. Archive corrections
        archive_path = project_dir / "corrections_applied.jsonl"
        with open(archive_path, "a", encoding="utf-8") as f:
            for corr in corrections:
                corr["applied_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                f.write(json.dumps(corr, ensure_ascii=False) + "\n")

        if total_applied == len(corrections):
            corrections_path.unlink()

        return jsonify({
            "applied": total_applied,
            "total": len(corrections),
            "chapters": sorted(affected_chapters),
            "log": log,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================================
# Dashboard routes — unified project workflow
# ============================================================================


def _get_project_status(project_id: str) -> dict:
    """Scan filesystem to derive full project status."""
    project_dir = _get_projects_dir() / project_id

    status = {
        "project_id": project_id,
        "has_source": False,
        "source_words": 0,
        "source_size": 0,
        "source_preview": "",
        "chapter_count": 0,
        "chapters": [],
        "total_chunks": 0,
        "translated_chunks": 0,
        "has_style_guide": False,
        "style_guide_content": None,
        "glossary_count": 0,
        "alignment_count": 0,
    }

    # Source
    source_path = project_dir / "source.txt"
    if source_path.exists():
        status["has_source"] = True
        status["source_size"] = source_path.stat().st_size
        text = source_path.read_text(encoding="utf-8")
        status["source_words"] = len(text.split())
        status["source_preview"] = text[:500]

    # Gutenberg metadata (for provenance + Stage 2 auto-populate)
    config = _load_project_config(project_id)
    status["gutenberg_url"] = config.get("gutenberg_url")
    status["suggested_split_pattern"] = config.get("suggested_split_pattern")

    # Style guide
    style_path = project_dir / "style.json"
    if style_path.exists():
        status["has_style_guide"] = True
        try:
            sg = load_style_guide(style_path)
            status["style_guide_content"] = sg.content
        except Exception:
            pass

    # Glossary
    glossary_path = project_dir / "glossary.json"
    if glossary_path.exists():
        try:
            g = load_glossary(glossary_path)
            status["glossary_count"] = len(g.terms)
        except Exception:
            pass

    # Chapters + chunks
    chapters_dir = project_dir / "chapters"
    chunks_dir = project_dir / "chunks"
    align_dir = project_dir / "alignments"

    # Load annotations for review info
    annotations_by_chapter = {}
    ann_path = project_dir / "annotations.jsonl"
    if ann_path.exists():
        try:
            with open(ann_path, "r", encoding="utf-8") as f:
                for line in f:
                    ann = json.loads(line)
                    ch = ann.get("chapter_id", "")
                    if ch:
                        annotations_by_chapter[ch] = annotations_by_chapter.get(ch, 0) + 1
        except Exception:
            pass

    # Reviewed chapters
    reviewed_chapters = set()
    reviewed_path = project_dir / "reviewed.json"
    if reviewed_path.exists():
        try:
            with open(reviewed_path, "r", encoding="utf-8") as f:
                reviewed_chapters = set(json.load(f))
        except Exception:
            pass

    # Build chunk index: chapter_id -> {total, translated}
    chunk_index = {}
    if chunks_dir.exists():
        for cf in sorted(chunks_dir.glob("*_chunk_*.json")):
            try:
                with open(cf, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                chapter_id = cdata.get("chapter_id", "")
                if chapter_id not in chunk_index:
                    chunk_index[chapter_id] = {"total": 0, "translated": 0}
                chunk_index[chapter_id]["total"] += 1
                status["total_chunks"] += 1
                if cdata.get("translated_text"):
                    chunk_index[chapter_id]["translated"] += 1
                    status["translated_chunks"] += 1
            except (json.JSONDecodeError, OSError):
                pass

    if chapters_dir.exists():
        for ch_file in sorted(chapters_dir.glob("chapter_*.txt")):
            ch_id = ch_file.stem
            text = ch_file.read_text(encoding="utf-8")
            words = len(text.split())
            chunk_info = chunk_index.get(ch_id, {"total": 0, "translated": 0})

            # Alignment info
            has_alignment = (align_dir / f"{ch_id}.json").exists() if align_dir.exists() else False
            alignment_confidence = None
            if has_alignment:
                status["alignment_count"] += 1
                try:
                    with open(align_dir / f"{ch_id}.json", "r", encoding="utf-8") as f:
                        adata = json.load(f)
                    scores = [p.get("similarity", 1.0) for p in adata.get("alignments", [])]
                    alignment_confidence = round(sum(scores) / len(scores) * 100) if scores else None
                except Exception:
                    pass

            status["chapters"].append({
                "id": ch_id,
                "name": ch_id.replace("_", " ").title(),
                "words": words,
                "preview": text[:200],
                "chunk_count": chunk_info["total"],
                "translated_count": chunk_info["translated"],
                "has_alignment": has_alignment,
                "alignment_confidence": alignment_confidence,
                "annotation_count": annotations_by_chapter.get(ch_id, 0),
                "reviewed": ch_id in reviewed_chapters,
            })

    status["chapter_count"] = len(status["chapters"])
    return status


@app.route("/project/<project_id>")
def dashboard_page(project_id):
    """Render the unified project dashboard."""
    if not _safe_id(project_id):
        return "Bad request", 400
    project_dir = _get_projects_dir() / project_id
    if not project_dir.exists():
        return "Project not found", 404

    from src.style_guide_wizard import load_fixed_questions
    fixed_questions = load_fixed_questions()
    t = _reader_strings()

    return render_template(
        "dashboard.html",
        project_id=project_id,
        project_title=_project_title(project_id),
        project_spanish_title=_load_project_config(project_id).get("spanish_title", ""),
        fixed_questions=fixed_questions,
        t=t,
        lang=_get_ui_lang(),
    )


@app.route("/api/project/<project_id>/status")
def project_status(project_id):
    """Return full project status as JSON."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404
    return jsonify(_get_project_status(project_id))


@app.route("/api/project/<project_id>/config", methods=["GET"])
def project_config_get(project_id):
    """Return per-project configuration."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    return jsonify(_load_project_config(project_id))


@app.route("/api/project/<project_id>/config", methods=["POST"])
def project_config_save(project_id):
    """Save per-project configuration."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404
    data = request.get_json() or {}
    # Merge with existing config so callers can do partial updates
    config = _load_project_config(project_id)
    config.update({k: v for k, v in data.items() if k in ("title", "spanish_title")})
    _save_project_config(project_id, config)
    return jsonify({"ok": True, "config": config})


@app.route("/api/project/<project_id>/ingest", methods=["POST"])
def project_ingest(project_id):
    """Upload/paste source text."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    data = request.json or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400

    source_path = project_dir / "source.txt"
    source_path.write_text(text, encoding="utf-8")
    return jsonify({"ok": True, "words": len(text.split())})


@app.route("/api/project/<project_id>/ingest-gutenberg", methods=["POST"])
def project_ingest_gutenberg(project_id):
    """Import a Project Gutenberg HTML page as source text."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400

    data = request.json or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    download_images = data.get("download_images", True)

    try:
        import importlib.util
        _spec = importlib.util.spec_from_file_location(
            "ingest_gutenberg",
            Path(__file__).parent.parent / "scripts" / "ingest_gutenberg.py",
        )
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        fetch_html = _mod.fetch_html
        find_book_body = _mod.find_book_body
        Converter = _mod.Converter
        build_chapter_report = _mod.build_chapter_report
        suggest_split_pattern = _mod.suggest_split_pattern
        from bs4 import BeautifulSoup

        project_dir = _get_projects_dir() / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        images_dir = project_dir / "images"

        html, base_url = fetch_html(url)
        soup = BeautifulSoup(html, "html.parser")
        body = find_book_body(soup)

        converter = Converter(base_url, images_dir, download_images)
        text = converter.convert(body)
        total_words = len(text.split())

        source_path = project_dir / "source.txt"
        source_path.write_text(text, encoding="utf-8")

        report = build_chapter_report(converter.chapters, total_words)
        pattern = suggest_split_pattern(converter.chapters)

        # Save metadata into project config
        config = _load_project_config(project_id)
        config["gutenberg_url"] = url
        config["suggested_split_pattern"] = pattern
        config["gutenberg_chapter_report"] = report
        _save_project_config(project_id, config)

        return jsonify({
            "ok": True,
            "words": total_words,
            "chapter_report": report,
            "suggested_pattern": pattern,
            "images_downloaded": converter._images_downloaded,
            "images_skipped": converter._images_skipped,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/split-patterns", methods=["GET"])
def get_split_patterns():
    """Return available split pattern definitions for the UI."""
    from src.book_splitter import get_pattern_definitions
    patterns = get_pattern_definitions()
    patterns["custom"] = {
        "label": "Custom regex",
        "numbering": "sequential",
    }
    return jsonify({"patterns": patterns})


@app.route("/api/project/<project_id>/split/preview", methods=["POST"])
def project_split_preview(project_id):
    """Preview chapter splits without writing files."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id
    source_path = project_dir / "source.txt"
    if not source_path.exists():
        return jsonify({"error": "No source.txt found"}), 404

    try:
        from src.book_splitter import split_book_into_chapters
        data = request.json or {}
        text = source_path.read_text(encoding="utf-8")
        chapters = split_book_into_chapters(
            text,
            pattern_type=data.get("pattern_type", "roman"),
            custom_regex=data.get("custom_regex"),
            min_chapter_size=data.get("min_chapter_size", 500),
        )
        result = []
        for ch in chapters:
            result.append({
                "name": ch.chapter_title or f"Chapter {ch.chapter_number}",
                "words": len(ch.content.split()),
                "preview": ch.content[:200],
            })
        return jsonify({"chapters": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/project/<project_id>/split", methods=["POST"])
def project_split(project_id):
    """Execute chapter split and write files."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id
    source_path = project_dir / "source.txt"
    if not source_path.exists():
        return jsonify({"error": "No source.txt found"}), 404

    try:
        from src.book_splitter import save_chapters_to_files, split_book_into_chapters
        data = request.json or {}
        text = source_path.read_text(encoding="utf-8")
        chapters = split_book_into_chapters(
            text,
            pattern_type=data.get("pattern_type", "roman"),
            custom_regex=data.get("custom_regex"),
            min_chapter_size=data.get("min_chapter_size", 500),
        )
        chapters_dir = project_dir / "chapters"
        save_chapters_to_files(chapters, chapters_dir)
        return jsonify({"ok": True, "chapter_count": len(chapters)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/project/<project_id>/chunk-all", methods=["POST"])
def project_chunk_all(project_id):
    """Chunk all (or selected) chapters."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id
    chapters_dir = project_dir / "chapters"
    if not chapters_dir.exists():
        return jsonify({"error": "No chapters directory"}), 404

    try:
        from src.chunker import chunk_chapter
        from src.models import ChunkingConfig
        from src.utils.file_io import save_chunk

        data = request.json or {}
        config = ChunkingConfig(
            target_size=data.get("target_size", 2000),
            min_chunk_size=data.get("min_chunk_size", 500),
            max_chunk_size=data.get("max_chunk_size", 3000),
            overlap_paragraphs=data.get("overlap_paragraphs", 2),
            min_overlap_words=data.get("min_overlap_words", 100),
        )

        chunks_dir = project_dir / "chunks"
        chunks_dir.mkdir(exist_ok=True)

        total_chunks = 0
        for ch_file in sorted(chapters_dir.glob("chapter_*.txt")):
            chapter_id = ch_file.stem
            text = ch_file.read_text(encoding="utf-8")
            chunks = chunk_chapter(text, config, chapter_id)
            for chunk in chunks:
                save_chunk(chunk, chunks_dir / f"{chunk.id}.json")
                total_chunks += 1

        return jsonify({"ok": True, "total_chunks": total_chunks})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/project/<project_id>/chapters/<chapter_id>/rechunk", methods=["POST"])
def project_chapter_rechunk(project_id, chapter_id):
    """Rechunk a single chapter, replacing its existing chunks.

    Destructive: deletes all existing chunk files for this chapter before
    writing new ones. The client is responsible for warning the user when
    the chapter has translated chunks that would be lost.
    """
    if not _safe_id(project_id) or not _safe_id(chapter_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _get_projects_dir() / project_id
    chapter_path = project_dir / "chapters" / f"{chapter_id}.txt"
    if not chapter_path.exists():
        return jsonify({"error": "Chapter not found"}), 404

    try:
        from src.chunker import chunk_chapter
        from src.models import ChunkingConfig
        from src.utils.file_io import save_chunk

        data = request.json or {}
        config = ChunkingConfig(
            target_size=data.get("target_size", 2000),
            min_chunk_size=data.get("min_chunk_size", 500),
            max_chunk_size=data.get("max_chunk_size", 3000),
            overlap_paragraphs=data.get("overlap_paragraphs", 2),
            min_overlap_words=data.get("min_overlap_words", 100),
        )

        chunks_dir = project_dir / "chunks"
        chunks_dir.mkdir(exist_ok=True)

        # Delete existing chunk files for this chapter so we don't leave
        # stale higher-numbered chunks behind if the new chunking produces
        # fewer chunks than before.
        for old in chunks_dir.glob(f"{chapter_id}_chunk_*.json"):
            try:
                old.unlink()
            except OSError:
                pass

        text = chapter_path.read_text(encoding="utf-8")
        chunks = chunk_chapter(text, config, chapter_id)
        for chunk in chunks:
            save_chunk(chunk, chunks_dir / f"{chunk.id}.json")

        return jsonify({"ok": True, "chunk_count": len(chunks)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/project/<project_id>/chapters/<chapter_id>/chunks")
def project_chapter_chunks(project_id, chapter_id):
    """List chunks for a chapter with status."""
    if not _safe_id(project_id) or not _safe_id(chapter_id):
        return jsonify({"error": "Bad request"}), 400

    chunks_dir = _get_projects_dir() / project_id / "chunks"
    if not chunks_dir.exists():
        return jsonify({"error": "No chunks directory"}), 404

    chunks = []
    for cf in sorted(chunks_dir.glob(f"{chapter_id}_chunk_*.json")):
        try:
            chunk = load_chunk(cf)
            chunks.append({
                "id": chunk.id,
                "position": chunk.position,
                "word_count": chunk.metadata.word_count if chunk.metadata else len(chunk.source_text.split()),
                "source_text": chunk.source_text,
                "translated_text": chunk.translated_text or "",
                "has_translation": bool(chunk.translated_text and chunk.translated_text.strip()),
            })
        except Exception:
            pass

    return jsonify({"chunks": chunks})


def _build_previous_context(project_dir: Path, chunk) -> str:
    """
    Build previous_chapter_context for a chunk.

    For non-first chunks within a chapter: uses the previous chunk.
    For the first chunk of a chapter: uses the last chunk of the previous chapter.
    Context window size is read from project.json (context_min_chars,
    context_max_chars, context_min_paragraphs); falls back to defaults.
    """
    from src.translator import extract_previous_chapter_context

    project_id = project_dir.name
    cfg = _load_project_config(project_id)
    min_chars = cfg.get("context_min_chars", 200)
    max_chars = cfg.get("context_max_chars", None)
    min_paragraphs = cfg.get("context_min_paragraphs", 3)

    chunks_dir = project_dir / "chunks"
    chapter_id = chunk.chapter_id
    chapter_chunks = sorted(chunks_dir.glob(f"{chapter_id}_chunk_*.json"))

    prev_chunk = None
    for i, cf in enumerate(chapter_chunks):
        if cf.stem == chunk.id:
            if i > 0:
                prev_chunk = load_chunk(chapter_chunks[i - 1])
            else:
                # First chunk of chapter — look at last chunk of previous chapter
                chapters_dir = project_dir / "chapters"
                all_chapters = sorted(
                    f.stem for f in chapters_dir.glob("chapter_*.txt")
                ) if chapters_dir.exists() else []
                try:
                    ch_idx = all_chapters.index(chapter_id)
                except ValueError:
                    return ""
                if ch_idx == 0:
                    return ""
                prev_chapter_id = all_chapters[ch_idx - 1]
                prev_chapter_chunks = sorted(
                    chunks_dir.glob(f"{prev_chapter_id}_chunk_*.json")
                )
                if not prev_chapter_chunks:
                    return ""
                prev_chunk = load_chunk(prev_chapter_chunks[-1])
            break

    if prev_chunk is None:
        return ""

    return extract_previous_chapter_context(
        prev_chunk.source_text,
        previous_translated_text=prev_chunk.translated_text,
        context_language="both",
        min_paragraphs=min_paragraphs,
        min_chars=min_chars,
        max_chars=max_chars,
        source_language="English",
        target_language="Spanish",
    )


@app.route("/api/project/<project_id>/chunks/<chunk_id>/prompt")
def project_chunk_prompt(project_id, chunk_id):
    """Get the rendered translation prompt for a chunk."""
    if not _safe_id(project_id) or not _safe_id(chunk_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _get_projects_dir() / project_id
    chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
    if not chunk_path.exists():
        return jsonify({"error": "Chunk not found"}), 404

    try:
        chunk = load_chunk(chunk_path)
        template = load_prompt_template()

        # Load glossary and style guide
        glossary = None
        style_guide = None
        glossary_path = project_dir / "glossary.json"
        style_path = project_dir / "style.json"
        if glossary_path.exists():
            try:
                glossary = load_glossary(glossary_path)
            except Exception:
                pass
        if style_path.exists():
            try:
                style_guide = load_style_guide(style_path)
            except Exception:
                pass

        from src.utils.file_io import filter_glossary_for_chunk

        prev_context = _build_previous_context(project_dir, chunk)

        # Filter glossary for this chunk
        chunk_glossary = filter_glossary_for_chunk(glossary, chunk.source_text) if glossary else None

        variables = {
            "book_title": _project_title(project_id),
            "source_text": chunk.source_text,
            "target_language": "Spanish",
            "source_language": "English",
            "glossary": format_glossary_for_prompt(chunk_glossary) if chunk_glossary else "No glossary provided.",
            "style_guide": style_guide.content if style_guide else "No style guide provided.",
            "context": "",
            "chapter_info": f"Chapter {chunk.chapter_id}, Chunk {chunk.position}",
            "previous_chapter_context": prev_context,
        }

        rendered = render_prompt(template, variables)
        separator = "=" * 80
        if separator in rendered:
            parts = rendered.split(separator, 1)
            if len(parts) > 1:
                rendered = separator + parts[1]

        return jsonify({"prompt": rendered})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/project/<project_id>/chunks/<chunk_id>/translate", methods=["POST"])
def project_chunk_translate(project_id, chunk_id):
    """Save a manual translation for a chunk, then recombine + realign."""
    if not _safe_id(project_id) or not _safe_id(chunk_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _get_projects_dir() / project_id
    chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
    if not chunk_path.exists():
        return jsonify({"error": "Chunk not found"}), 404

    data = request.json or {}
    translated_text = data.get("translated_text", "").strip()
    if not translated_text:
        return jsonify({"error": "No translation text"}), 400

    try:
        chunk = load_chunk(chunk_path)
        chunk.status = ChunkStatus.TRANSLATED
        chunk.translated_at = datetime.now()
        result = _replace_chunk_translation(
            project_dir, project_id, chunk_id, chunk_path, chunk, translated_text,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/project/<project_id>/translate/cost-estimate", methods=["POST"])
def project_translate_cost_estimate(project_id):
    """Estimate translation cost for selected chapters."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _get_projects_dir() / project_id
    data = request.json or {}
    chapter_ids = data.get("chapter_ids", [])
    provider = data.get("provider", "anthropic")
    model = data.get("model", "")

    try:
        from src.api_translator import estimate_cost

        chunks_dir = project_dir / "chunks"
        chunks = []
        for ch_id in chapter_ids:
            for cf in sorted(chunks_dir.glob(f"{ch_id}_chunk_*.json")):
                chunk = load_chunk(cf)
                if not chunk.translated_text or not chunk.translated_text.strip():
                    chunks.append(chunk)

        if not chunks:
            return jsonify({"chunk_count": 0, "estimated_cost": 0})

        # Load glossary and style guide for accurate estimation
        glossary = None
        style_guide = None
        glossary_path = project_dir / "glossary.json"
        style_path = project_dir / "style.json"
        if glossary_path.exists():
            try:
                glossary = load_glossary(glossary_path)
            except Exception:
                pass
        if style_path.exists():
            try:
                style_guide = load_style_guide(style_path)
            except Exception:
                pass

        from src.api_translator import DEFAULT_MODEL
        result = estimate_cost(chunks, provider=provider, model=model or DEFAULT_MODEL,
                               glossary=glossary, style_guide=style_guide)
        return jsonify({
            "chunk_count": len(chunks),
            "estimated_cost": result.get("cost_usd", 0),
            "total_tokens": result.get("input_tokens", 0),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/project/<project_id>/translate/realtime", methods=["POST"])
def project_translate_realtime(project_id):
    """Translate a single chunk via API."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _get_projects_dir() / project_id
    data = request.json or {}
    chunk_id = data.get("chunk_id", "")

    chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
    if not chunk_path.exists():
        return jsonify({"error": "Chunk not found"}), 404

    try:
        from src.api_translator import translate_chunk_realtime

        chunk = load_chunk(chunk_path)

        glossary = None
        style_guide = None
        glossary_path = project_dir / "glossary.json"
        style_path = project_dir / "style.json"
        if glossary_path.exists():
            try:
                glossary = load_glossary(glossary_path)
            except Exception:
                pass
        if style_path.exists():
            try:
                style_guide = load_style_guide(style_path)
            except Exception:
                pass

        provider = data.get("provider", "anthropic")
        model = data.get("model", None)
        prev_context = _build_previous_context(project_dir, chunk)

        translated = translate_chunk_realtime(
            chunk=chunk,
            provider=provider,
            model=model,
            glossary=glossary,
            style_guide=style_guide,
            project_name=_project_title(project_id),
            source_language="English",
            target_language="Spanish",
            previous_chapter_context=prev_context,
        )

        new_text = translated.translated_text or ""
        result = _replace_chunk_translation(
            project_dir, project_id, chunk_id, chunk_path, chunk, new_text,
        )
        result["translated_text"] = new_text
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Batch translation with SSE ──

import queue
import threading
import uuid

_batch_jobs = {}  # job_id -> {"queue": Queue, "thread": Thread, "status": str}


@app.route("/api/project/<project_id>/translate/batch", methods=["POST"])
def project_translate_batch(project_id):
    """Start batch translation. Returns job_id for SSE tracking."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _get_projects_dir() / project_id
    data = request.json or {}
    chapter_ids = data.get("chapter_ids", [])
    provider = data.get("provider", "anthropic")
    model = data.get("model", None)

    # Collect untranslated chunks
    chunks_dir = project_dir / "chunks"
    chunk_paths = []
    for ch_id in chapter_ids:
        for cf in sorted(chunks_dir.glob(f"{ch_id}_chunk_*.json")):
            chunk = load_chunk(cf)
            if not chunk.translated_text or not chunk.translated_text.strip():
                chunk_paths.append(cf)

    if not chunk_paths:
        return jsonify({"error": "No untranslated chunks found"}), 400

    # Load glossary and style guide
    glossary = None
    style_guide = None
    glossary_path = project_dir / "glossary.json"
    style_path = project_dir / "style.json"
    if glossary_path.exists():
        try:
            glossary = load_glossary(glossary_path)
        except Exception:
            pass
    if style_path.exists():
        try:
            style_guide = load_style_guide(style_path)
        except Exception:
            pass

    job_id = str(uuid.uuid4())[:8]
    job_queue = queue.Queue()

    def run_batch():
        from src.api_translator import translate_chunk_realtime
        for cp in chunk_paths:
            try:
                chunk = load_chunk(cp)
                job_queue.put(json.dumps({
                    "event": "chunk_started",
                    "chunk_id": chunk.id,
                    "chapter_id": chunk.chapter_id,
                }))
                prev_context = _build_previous_context(project_dir, chunk)
                translated = translate_chunk_realtime(
                    chunk=chunk,
                    provider=provider,
                    model=model,
                    glossary=glossary,
                    style_guide=style_guide,
                    project_name=_project_title(project_id),
                    source_language="English",
                    target_language="Spanish",
                    previous_chapter_context=prev_context,
                )
                save_chunk(translated, cp)
                job_queue.put(json.dumps({
                    "event": "chunk_done",
                    "chunk_id": chunk.id,
                    "chapter_id": chunk.chapter_id,
                }))
            except Exception as e:
                job_queue.put(json.dumps({
                    "event": "chunk_error",
                    "chunk_id": chunk.id if chunk else "",
                    "error": str(e),
                }))
        job_queue.put(json.dumps({"event": "batch_complete"}))

    t = threading.Thread(target=run_batch, daemon=True)
    _batch_jobs[job_id] = {"queue": job_queue, "thread": t, "status": "running"}
    t.start()

    return jsonify({
        "job_id": job_id,
        "total_chunks": len(chunk_paths),
    })


@app.route("/api/project/<project_id>/translate/sse")
def project_translate_sse(project_id):
    """SSE endpoint for batch translation progress."""
    job_id = request.args.get("job_id", "")
    if job_id not in _batch_jobs:
        return "Job not found", 404

    job = _batch_jobs[job_id]

    def generate():
        while True:
            try:
                msg = job["queue"].get(timeout=30)
                data = json.loads(msg)
                event_type = data.get("event", "message")
                yield f"event: {event_type}\ndata: {msg}\n\n"
                if event_type == "batch_complete":
                    job["status"] = "complete"
                    break
            except queue.Empty:
                yield ": keepalive\n\n"

    from flask import Response
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/project/<project_id>/combine/<chapter_id>", methods=["POST"])
def project_combine(project_id, chapter_id):
    """Combine translated chunks back into a chapter file."""
    if not _safe_id(project_id) or not _safe_id(chapter_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _get_projects_dir() / project_id
    chunks_dir = project_dir / "chunks"

    try:
        from src.combiner import combine_chunks

        chunk_files = sorted(chunks_dir.glob(f"{chapter_id}_chunk_*.json"))
        chunks = [load_chunk(cf) for cf in chunk_files]

        if not chunks:
            return jsonify({"error": "No chunks found"}), 404

        combined_text = combine_chunks(chunks)
        chapters_dir = project_dir / "chapters"
        chapters_dir.mkdir(exist_ok=True)
        (chapters_dir / f"{chapter_id}.txt").write_text(combined_text, encoding="utf-8")

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/project/<project_id>/align/<chapter_id>", methods=["POST"])
def project_align(project_id, chapter_id):
    """Combine chunks and run sentence alignment for a chapter."""
    if not _safe_id(project_id) or not _safe_id(chapter_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _get_projects_dir() / project_id
    chunks_dir = project_dir / "chunks"

    try:
        from src.combiner import combine_chunks
        from src.sentence_aligner import align_chapter_chunks

        chunk_files = sorted(chunks_dir.glob(f"{chapter_id}_chunk_*.json"))
        if not chunk_files:
            return jsonify({"error": "No chunks found"}), 404

        # Refresh the combined chapter text before aligning so chapters/ is
        # always in sync with the translated chunks. get_alignment reads this
        # file to enrich alignment data with paragraph breaks.
        chunks = [load_chunk(cf) for cf in chunk_files]
        combined_text = combine_chunks(chunks)
        chapters_dir = project_dir / "chapters"
        chapters_dir.mkdir(exist_ok=True)
        (chapters_dir / f"{chapter_id}.txt").write_text(combined_text, encoding="utf-8")

        align_dir = project_dir / "alignments"
        align_dir.mkdir(exist_ok=True)
        output_path = align_dir / f"{chapter_id}.json"

        result = align_chapter_chunks(
            chunk_paths=[str(cf) for cf in chunk_files],
            project_id=project_id,
            chapter_id=chapter_id,
            source_lang="en",
            target_lang="es",
            output_path=str(output_path),
        )

        return jsonify({"ok": True, "pairs": len(result.get("pairs", []))})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================================
# Chunk editor (full-textarea edit of a single chunk's translated_text)
# ============================================================================


def _chapter_id_from_chunk_id(chunk_id: str) -> Optional[str]:
    """Derive the parent chapter_id from a chunk_id like 'chapter_01_chunk_003'."""
    marker = "_chunk_"
    idx = chunk_id.rfind(marker)
    if idx <= 0:
        return None
    return chunk_id[:idx]


_IMAGE_TOKEN_RE = re.compile(r"\[IMAGE:[^\]]+\]")


def _chapter_has_pending_corrections(project_dir: Path, chapter_id: str) -> bool:
    """True if corrections.jsonl has any unapplied row for this chapter."""
    corrections_path = project_dir / "corrections.jsonl"
    if not corrections_path.exists():
        return False
    try:
        for line in corrections_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("chapter_id") == chapter_id:
                return True
    except OSError:
        return False
    return False


def _load_alignment_es_map(project_dir: Path, chapter_id: str) -> dict[int, str]:
    """Load {es_idx: es_text} for a chapter's current alignment, or {} if none."""
    align_path = project_dir / "alignments" / f"{chapter_id}.json"
    if not align_path.exists():
        return {}
    try:
        with open(align_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    result: dict[int, str] = {}
    for a in data.get("alignments", []):
        idx = a.get("es_idx")
        es_text = a.get("es")
        if idx is not None and isinstance(es_text, str):
            result[int(idx)] = es_text
    return result


def _reanchor_annotations_after_realign(
    project_dir: Path,
    chapter_id: str,
    old_es_map: dict[int, str],
) -> list[dict]:
    """Re-anchor chapter annotations whose es_idx shifted after realign.

    Appends remove+recreate rows to annotations.jsonl for shifted annotations
    and returns a list of orphaned annotation records that couldn't be matched.
    """
    active = _load_annotations(project_dir, chapter_id)
    if not active:
        return []

    new_es_map = _load_alignment_es_map(project_dir, chapter_id)
    # Build reverse lookup from exact es text → new es_idx (first match wins)
    text_to_new_idx: dict[str, int] = {}
    for new_idx, es_text in new_es_map.items():
        text_to_new_idx.setdefault(es_text, new_idx)

    annotations_path = project_dir / "annotations.jsonl"
    orphaned: list[dict] = []
    appended: list[dict] = []

    for old_idx, record in active.items():
        old_es_text = old_es_map.get(old_idx)
        if old_es_text is None:
            # Annotation references a sentence we don't know about — leave it.
            orphaned.append(record)
            continue

        new_idx = text_to_new_idx.get(old_es_text)
        if new_idx is None:
            # Try prefix match (first 30 chars) as a fallback
            prefix = old_es_text[:30]
            for candidate_idx, candidate_text in new_es_map.items():
                if candidate_text.startswith(prefix):
                    new_idx = candidate_idx
                    break
        if new_idx is None:
            orphaned.append(record)
            continue
        if new_idx == old_idx:
            continue

        ts = datetime.now().isoformat()
        # Mark the old index as removed
        appended.append({
            "project_id": record.get("project_id"),
            "chapter_id": chapter_id,
            "es_idx": old_idx,
            "removed": True,
            "timestamp": ts,
        })
        # Re-create at the new index with the same type/content
        appended.append({
            "project_id": record.get("project_id"),
            "chapter_id": chapter_id,
            "es_idx": new_idx,
            "type": record.get("type", "flag"),
            "content": record.get("content", ""),
            "timestamp": ts,
        })

    if appended:
        with open(annotations_path, "a", encoding="utf-8") as f:
            for row in appended:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return orphaned


def _purge_chunk_corrections(project_dir: Path, chunk_id: str) -> int:
    """Remove all pending corrections for a chunk from corrections.jsonl.

    Returns the number of corrections removed.
    """
    corrections_path = project_dir / "corrections.jsonl"
    if not corrections_path.exists():
        return 0
    try:
        lines = corrections_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0

    kept = []
    removed = 0
    for line in lines:
        line_s = line.strip()
        if not line_s:
            continue
        try:
            record = json.loads(line_s)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        if record.get("chunk_id") == chunk_id:
            removed += 1
        else:
            kept.append(line)

    if removed > 0:
        if kept:
            corrections_path.write_text(
                "\n".join(kept) + "\n", encoding="utf-8",
            )
        else:
            corrections_path.unlink(missing_ok=True)

    return removed


def _replace_chunk_translation(
    project_dir: Path,
    project_id: str,
    chunk_id: str,
    chunk_path: Path,
    chunk: "Chunk",
    new_text: str,
) -> dict:
    """Shared pipeline for replacing a chunk's translation.

    Performs: backup → save → purge corrections → recombine → realign →
    re-anchor annotations.

    Returns a result dict with keys: ok, mtime, orphaned_annotations,
    corrections_purged.  On failure raises an exception.
    """
    from src.combiner import combine_chunks
    from src.sentence_aligner import align_chapter_chunks

    chapter_id = _chapter_id_from_chunk_id(chunk_id)

    # 1. Capture old alignment for annotation re-anchoring
    old_es_map = _load_alignment_es_map(project_dir, chapter_id)

    # 2. Backup the pre-edit chunk JSON
    backup_root = project_dir / ".chunk_edits" / chapter_id / chunk_id
    backup_root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup_path = backup_root / f"{ts}.json"
    backup_path.write_text(
        chunk_path.read_text(encoding="utf-8"), encoding="utf-8",
    )
    backups = sorted(backup_root.glob("*.json"))
    for stale in backups[:-10]:
        try:
            stale.unlink()
        except OSError:
            pass

    # 3. Update the chunk and persist
    chunk.translated_text = new_text
    save_chunk(chunk, chunk_path)

    # 4. Purge stale corrections for this chunk
    corrections_purged = _purge_chunk_corrections(project_dir, chunk_id)

    # 5. Recombine + realign the chapter
    chunks_dir = project_dir / "chunks"
    chunk_files = sorted(chunks_dir.glob(f"{chapter_id}_chunk_*.json"))
    chunks = [load_chunk(cf) for cf in chunk_files]
    combined_text = combine_chunks(chunks)
    chapters_dir = project_dir / "chapters"
    chapters_dir.mkdir(exist_ok=True)
    (chapters_dir / f"{chapter_id}.txt").write_text(combined_text, encoding="utf-8")

    align_dir = project_dir / "alignments"
    align_dir.mkdir(exist_ok=True)
    output_path = align_dir / f"{chapter_id}.json"
    align_chapter_chunks(
        chunk_paths=[str(cf) for cf in chunk_files],
        project_id=project_id,
        chapter_id=chapter_id,
        source_lang="en",
        target_lang="es",
        output_path=str(output_path),
    )

    # 6. Re-anchor existing annotations by text match
    try:
        orphaned = _reanchor_annotations_after_realign(
            project_dir, chapter_id, old_es_map,
        )
    except Exception:
        orphaned = []

    try:
        new_mtime = chunk_path.stat().st_mtime
    except OSError:
        new_mtime = 0.0

    return {
        "ok": True,
        "mtime": new_mtime,
        "orphaned_annotations": len(orphaned),
        "corrections_purged": corrections_purged,
    }


@app.route("/read/<project_id>/<chapter>/chunk/<chunk_id>/edit")
def chunk_editor_view(project_id, chapter, chunk_id):
    """Render the full-textarea editor for a single chunk's translated text."""
    t = _reader_strings()
    if not _safe_id(project_id) or not _safe_id(chapter) or not _safe_id(chunk_id):
        return "Bad request", 400

    derived_chapter = _chapter_id_from_chunk_id(chunk_id)
    if derived_chapter != chapter:
        return "Chunk does not belong to chapter", 400

    project_dir = _get_projects_dir() / project_id
    chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
    if not chunk_path.exists():
        return "Chunk not found", 404

    try:
        chunk = load_chunk(chunk_path)
    except Exception as e:
        return f"Failed to load chunk: {e}", 500

    anchor_idx = request.args.get("anchor_idx", "")
    anchor_text = request.args.get("anchor", "")
    pending = _chapter_has_pending_corrections(project_dir, chapter)

    try:
        mtime = chunk_path.stat().st_mtime
    except OSError:
        mtime = 0.0

    return render_template(
        "chunk_edit.html",
        project_id=project_id,
        project_title=_project_title(project_id),
        chapter=chapter,
        chunk_id=chunk_id,
        chunk_position=chunk.position,
        translated_text=chunk.translated_text or "",
        source_text=chunk.source_text,
        overlap_start=chunk.metadata.overlap_start,
        overlap_end=chunk.metadata.overlap_end,
        mtime=mtime,
        anchor_idx=anchor_idx,
        anchor_text=anchor_text,
        pending_corrections=pending,
        t=t,
        lang=_get_ui_lang(),
    )


@app.route("/api/chunk/<project_id>/<chunk_id>/edit", methods=["POST"])
def save_chunk_edit(project_id, chunk_id):
    """Persist a full-chunk text edit: update chunk, recombine, realign."""
    if not _safe_id(project_id) or not _safe_id(chunk_id):
        return jsonify({"error": "Invalid ID"}), 400

    chapter_id = _chapter_id_from_chunk_id(chunk_id)
    if not chapter_id or not _safe_id(chapter_id):
        return jsonify({"error": "Cannot derive chapter from chunk_id"}), 400

    data = request.json or {}
    new_text = data.get("translated_text")
    expected_mtime = data.get("expected_mtime")

    if not isinstance(new_text, str) or not new_text.strip():
        return jsonify({"error": "translated_text is required"}), 400

    project_dir = _get_projects_dir() / project_id
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404

    chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
    if not chunk_path.exists():
        return jsonify({"error": "Chunk not found"}), 404

    # Concurrency check
    try:
        current_mtime = chunk_path.stat().st_mtime
    except OSError as e:
        return jsonify({"error": f"Cannot stat chunk file: {e}"}), 500
    if expected_mtime is not None:
        try:
            if abs(float(expected_mtime) - current_mtime) > 1e-6:
                return jsonify({
                    "error": "Chunk was modified by another process. Reload and try again.",
                }), 409
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid expected_mtime"}), 400

    try:
        chunk = load_chunk(chunk_path)
    except Exception as e:
        return jsonify({"error": f"Failed to load chunk: {e}"}), 500

    old_text = chunk.translated_text or ""

    # Guard: image placeholder set must be unchanged (order-preserving)
    old_tokens = _IMAGE_TOKEN_RE.findall(old_text)
    new_tokens = _IMAGE_TOKEN_RE.findall(new_text)
    if old_tokens != new_tokens:
        return jsonify({
            "error": (
                "[IMAGE:...] placeholders must not be added, removed, or reordered. "
                f"Expected {old_tokens}, got {new_tokens}."
            ),
        }), 400

    # Guard: overlap regions are read-only (combine_chunks would drop them anyway)
    overlap_start = chunk.metadata.overlap_start
    overlap_end = chunk.metadata.overlap_end
    if overlap_start > 0 and new_text[:overlap_start] != old_text[:overlap_start]:
        return jsonify({
            "error": (
                f"The first {overlap_start} characters overlap with the previous "
                "chunk and cannot be edited here."
            ),
        }), 400
    if overlap_end > 0 and new_text[-overlap_end:] != old_text[-overlap_end:]:
        return jsonify({
            "error": (
                f"The last {overlap_end} characters overlap with the next chunk "
                "and cannot be edited here."
            ),
        }), 400

    if new_text == old_text:
        return jsonify({"ok": True, "unchanged": True})

    try:
        result = _replace_chunk_translation(
            project_dir, project_id, chunk_id, chunk_path, chunk, new_text,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/project/<project_id>/epub-status")
def epub_status(project_id):
    """Return epub readiness: which chapters are fully translated."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404

    chunks_dir = project_dir / "chunks"
    chapters_dir = project_dir / "chapters"

    # Build per-chapter translation completeness from chunks
    chunk_index = {}  # chapter_id -> {total, translated}
    if chunks_dir.exists():
        for cf in sorted(chunks_dir.glob("*_chunk_*.json")):
            try:
                with open(cf, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                ch_id = cdata.get("chapter_id", "")
                if ch_id not in chunk_index:
                    chunk_index[ch_id] = {"total": 0, "translated": 0}
                chunk_index[ch_id]["total"] += 1
                if cdata.get("translated_text"):
                    chunk_index[ch_id]["translated"] += 1
            except (json.JSONDecodeError, OSError):
                pass

    # Enumerate chapters from the chapters dir (source files from split)
    chapters = []
    if chapters_dir.exists():
        for ch_file in sorted(chapters_dir.glob("chapter_*.txt")):
            ch_id = ch_file.stem
            info = chunk_index.get(ch_id, {"total": 0, "translated": 0})
            fully_translated = info["total"] > 0 and info["translated"] == info["total"]
            chapters.append({
                "id": ch_id,
                "name": ch_id.replace("_", " ").title(),
                "translated": fully_translated,
            })

    translated_count = sum(1 for c in chapters if c["translated"])

    # Check if epub already exists
    config = _load_project_config(project_id)

    # Find existing epub (filename may be based on title, not folder name)
    epub_files = list(project_dir.glob("*.epub"))
    epub_file = max(epub_files, key=lambda p: p.stat().st_mtime) if epub_files else None

    return jsonify({
        "total_chapters": len(chapters),
        "translated_chapters": translated_count,
        "chapters": chapters,
        "epub_exists": epub_file is not None,
        "epub_filename": epub_file.name if epub_file else None,
        "title": config.get("title", ""),
        "spanish_title": config.get("spanish_title", ""),
        "author": config.get("author", ""),
    })


@app.route("/api/project/<project_id>/build-epub", methods=["POST"])
def build_epub_route(project_id):
    """Build EPUB from translated chapters."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404

    data = request.get_json(silent=True) or {}
    config = _load_project_config(project_id)
    title = data.get("title") or config.get("spanish_title") or config.get("title") or project_id
    author = data.get("author") or config.get("author", "")
    language = data.get("language") or config.get("target_lang_code", "es")

    chunks_dir = project_dir / "chunks"

    # Determine which chapters are fully translated
    chunk_index = {}
    if chunks_dir.exists():
        for cf in sorted(chunks_dir.glob("*_chunk_*.json")):
            try:
                with open(cf, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                ch_id = cdata.get("chapter_id", "")
                if ch_id not in chunk_index:
                    chunk_index[ch_id] = {"total": 0, "translated": 0, "files": []}
                chunk_index[ch_id]["total"] += 1
                chunk_index[ch_id]["files"].append(cf)
                if cdata.get("translated_text"):
                    chunk_index[ch_id]["translated"] += 1
            except (json.JSONDecodeError, OSError):
                pass

    translated_chapter_ids = [
        ch_id for ch_id, info in chunk_index.items()
        if info["total"] > 0 and info["translated"] == info["total"]
    ]

    if not translated_chapter_ids:
        return jsonify({"error": "No fully translated chapters found"}), 400

    # Auto-combine translated chapters into a temp directory for epub building
    import shutil
    import tempfile
    from src.combiner import combine_chunks

    temp_dir = Path(tempfile.mkdtemp(prefix="epub_"))
    try:
        for ch_id in translated_chapter_ids:
            chunk_files = sorted(chunks_dir.glob(f"{ch_id}_chunk_*.json"))
            chunks = [load_chunk(cf) for cf in chunk_files]
            combined_text = combine_chunks(chunks)
            (temp_dir / f"{ch_id}.txt").write_text(combined_text, encoding="utf-8")

        from src.epub_builder import build_epub
        epub_filename = title + ".epub"
        epub_output = project_dir / epub_filename
        epub_path = build_epub(
            project_path=project_dir,
            title=title,
            author=author,
            language=language,
            chapters_dir=temp_dir,
            output_path=epub_output,
        )

        size_bytes = epub_path.stat().st_size

        return jsonify({
            "ok": True,
            "filename": epub_path.name,
            "size_bytes": size_bytes,
            "chapters_included": len(translated_chapter_ids),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.route("/api/project/<project_id>/download-epub")
def download_epub(project_id):
    """Serve the built EPUB file for download."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _get_projects_dir() / project_id
    epub_files = list(project_dir.glob("*.epub"))
    if not epub_files:
        return jsonify({"error": "EPUB not found. Build it first."}), 404
    # Use the most recently modified epub
    epub_file = max(epub_files, key=lambda p: p.stat().st_mtime)
    return send_from_directory(str(project_dir), epub_file.name, as_attachment=True)


if __name__ == "__main__":
    print("=" * 70)
    print("Translation Web UI")
    print("=" * 70)
    print("\nStarting server on http://localhost:5000")
    print("Press Ctrl+C to stop\n")
    app.run(debug=True, host="0.0.0.0", port=5000)
