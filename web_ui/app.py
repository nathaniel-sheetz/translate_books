"""
Flask web UI for the book translation pipeline.

Provides the pipeline dashboard and bilingual reader:
- Project list with status cards
- 8-stage pipeline dashboard (Source → Split → Chunk → Style Guide → Glossary → Translate → Review → Export)
- Bilingual sentence-aligned reader with annotations and corrections
"""

import json
import math
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

from src.annotations import REVIEW_ANNOTATION_TYPES, is_effectively_blank, load_active
from src.models import Chunk, ChunkStatus, Glossary, StyleGuide
from src.glossary_bootstrap import glossary_terms_from_proposals, proposals_to_glossary
from src.utils.file_io import (
    format_glossary_for_prompt,
    load_chunk,
    load_glossary,
    load_prompt_template,
    load_style_guide,
    render_prompt,
    save_chunk,
    save_glossary,
    save_style_guide,
)
from src.utils.source_text import load_chapter_source_text
from src.difficulty_scorer import score_book
from src.utils.text_utils import (
    KWIC_WORDS,
    dialogue_instruction,
    find_folded,
    fold,
    fold_with_map,
    image_placeholder_instruction,
    kwic_window,
)
from src.utils.verse import is_verse_block
from web_ui.evaluations import (
    REVIEW_CODED_TYPES,
    REVIEW_JUDGE_TYPES,
    REVIEW_TYPES,
    append_feedback,
    chapter_id_from_chunk_id,
    empty_type_counts,
    evaluate_and_persist_chunk,
    load_all_feedback_by_chunk,
    load_chapter_type_counts,
    load_chunk_evaluation,
    load_feedback_for_chunk,
    load_project_summary,
    merge_llm_judge_result,
    run_coded_evaluators,
)
from web_ui.project_cards import PROJECT_STATUSES, build_project_card

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


def _is_project_dir(p: Path) -> bool:
    """A project dir has chunks/ or source.txt (matches existing discovery filter)."""
    return p.is_dir() and ((p / "chunks").exists() or (p / "source.txt").exists())


def _iter_project_dirs(root: Optional[Path] = None, _depth: int = 0):
    """Yield project dirs under projects/, descending through grouping subfolders
    but never into a project itself. Order is stable (sorted)."""
    root = root or _get_projects_dir()
    if not root.exists() or _depth > 20:
        return
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink():      # skip symlinks to avoid infinite recursion on cycles
            continue
        if not entry.is_dir():
            continue
        if _is_project_dir(entry):
            yield entry
        else:                       # grouping/container folder -> recurse
            yield from _iter_project_dirs(entry, _depth + 1)


_NESTED_PROJECT_CACHE: dict[str, Path] = {}


def _resolve_project_dir(project_id: str) -> Path:
    """Map a project id (leaf folder name) to its actual dir, flat or nested.

    Falls back to the flat path when not found so existing existence checks
    (``if not project_dir.exists(): 404``) behave exactly as before."""
    root = _get_projects_dir()
    flat = root / project_id
    if flat.is_dir():               # fast path: flat layout + newly created projects
        return flat
    cached = _NESTED_PROJECT_CACHE.get(project_id)
    if cached is not None and cached.is_dir():
        return cached
    _found = None
    for proj_dir in _iter_project_dirs(root):
        if proj_dir.name == project_id:
            if _found is None:
                _found = proj_dir
                _NESTED_PROJECT_CACHE[project_id] = proj_dir
            else:
                app.logger.warning(
                    "Duplicate project id %r found at %s and %s; using %s",
                    project_id, _found, proj_dir, _found,
                )
                break
    return _found if _found is not None else flat


def _load_project_config(project_id: str) -> dict:
    """Load per-project config from projects/<id>/project.json."""
    config_path = _resolve_project_dir(project_id) / "project.json"
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_project_config(project_id: str, config: dict) -> None:
    """Save per-project config to projects/<id>/project.json."""
    config_path = _resolve_project_dir(project_id) / "project.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


# Cross-reference: HTML placeholder text (web_ui/templates/dashboard.html) is
# hardcoded to "Nota del traductor" — keep in sync with the backend
# constant src.epub_builder._DEFAULT_TRANSLATOR_HEADING.
#
# Per-user template lives at prompts/translator_note_default.txt (gitignored).
# A repo-tracked example ships at prompts/translator_note_default.example.txt;
# if the per-user file is missing the example is used as a fallback.
_TRANSLATOR_NOTE_TEMPLATE_PATH = _PROJECT_ROOT / "prompts" / "translator_note_default.txt"
_TRANSLATOR_NOTE_TEMPLATE_EXAMPLE_PATH = _PROJECT_ROOT / "prompts" / "translator_note_default.example.txt"
_TRANSLATOR_NOTE_BODY_MAX_BYTES = 100_000


def _read_translator_note_template() -> str:
    """Return the default note body, preferring the per-user file."""
    for path in (_TRANSLATOR_NOTE_TEMPLATE_PATH, _TRANSLATOR_NOTE_TEMPLATE_EXAMPLE_PATH):
        try:
            return path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            continue
    return ""


def _load_translator_note(project_id: str) -> dict:
    """
    Load the per-book translator note.

    Returns dict with keys 'heading' and 'body'. Falls back to the
    repo-tracked default template body (and empty heading so the UI
    placeholder shows the default constant) when no per-book file exists.
    On corrupt JSON, the bad file is moved aside as ``.bak.<unix-ts>`` and
    defaults are returned (ENG REVIEW decision 1B).
    """
    import logging
    import time

    log = logging.getLogger(__name__)
    note_path = _resolve_project_dir(project_id) / "translator_note.json"

    def _defaults() -> dict:
        return {"heading": "", "body": _read_translator_note_template()}

    if not note_path.exists():
        return _defaults()

    try:
        with open(note_path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        try:
            backup = note_path.with_suffix(f".json.bak.{int(time.time())}")
            note_path.rename(backup)
            log.warning(
                "Corrupt translator_note.json renamed to %s; returning defaults",
                backup.name,
            )
        except OSError as exc:
            log.warning("Failed to rename corrupt translator_note.json: %s", exc)
        return _defaults()
    except OSError as exc:
        log.warning("Could not read translator_note.json: %s", exc)
        return _defaults()

    return {
        "heading": str(data.get("heading", "")),
        "body": str(data.get("body", "")),
    }


def _save_translator_note(project_id: str, heading: str, body: str) -> None:
    """Persist the translator note to projects/<id>/translator_note.json."""
    note_path = _resolve_project_dir(project_id) / "translator_note.json"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    with open(note_path, "w", encoding="utf-8") as f:
        json.dump(
            {"heading": heading, "body": body},
            f,
            indent=2,
            ensure_ascii=False,
        )


def _project_title(project_id: str) -> str:
    """Return the display title for a project, falling back to the folder name."""
    return _load_project_config(project_id).get("title") or project_id


def _load_chapter_manifest_for_project(project_id: str) -> dict:
    """Return {chapter_id: entry} from project.json's chapter_manifest, or {}."""
    cfg = _load_project_config(project_id)
    raw = cfg.get("chapter_manifest")
    if not isinstance(raw, list):
        return {}
    out = {}
    for entry in raw:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            out[entry["id"]] = entry
    return out


def _chapter_display_label(chapter_id: str, manifest: dict, chapter_prefix: str) -> str:
    """Compute the display label for a chapter ID using a manifest."""
    entry = manifest.get(chapter_id)
    if entry:
        kind = entry.get("kind", "chapter")
        if kind == "chapter":
            num = entry.get("number")
            if num is not None:
                return f"{chapter_prefix} {num}"
        else:
            label = entry.get("label")
            if label:
                return label
    # Fallback to today's behavior: title-case the filename stem
    return chapter_id.replace("_", " ").title().replace("Chapter", chapter_prefix)


def _safe_id(value: str) -> bool:
    """Return True only for IDs that are safe filesystem names.

    Periods are allowed within the name (real project dirs use them, e.g.
    backup folders like "foo.bak-ch1-restore"), but an id made up entirely
    of dots (".", "..", "...", ...) is rejected so it can't be used for
    path traversal or collide with special directory entries.
    """
    if not value or set(value) == {"."}:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_.\-]+", value))


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


# Reader bottom-sheet UI version. "classic" is the shipped sheet; "v2" is the
# opt-in redesigned sheet (Annotate / Edit / Issues tabs). Mirrors the language
# cookie precedent above: a persistent per-device preference, default classic.
_READER_UI_VERSIONS = ("classic", "v2")


def _get_reader_ui_version() -> str:
    """Read the reader sheet UI version from cookie, default to classic."""
    v = request.cookies.get("reader_ui_version", "classic")
    return v if v in _READER_UI_VERSIONS else "classic"


@app.route("/api/set-ui-version", methods=["POST"])
def set_ui_version():
    """Set the reader sheet UI version via cookie."""
    version = (request.json or {}).get("version", "classic")
    if version not in _READER_UI_VERSIONS:
        version = "classic"
    resp = make_response(jsonify({"version": version}))
    resp.set_cookie("reader_ui_version", version, max_age=365 * 24 * 3600, samesite="Lax")
    return resp


# Which evaluator/judge categories count as "errors I care about". Global, not
# per project: the same six categories mean the same thing in every book, and
# the home page needs the selection before you have picked a book. Mirrors the
# two cookies above. The review-mode *on/off* switch stays per project in
# localStorage — that one is a per-book reading preference.
_REVIEW_TYPES_COOKIE = "reader_review_types"


def _get_review_types() -> list[str]:
    """Read the selected review categories from cookie, default to all six.

    Order follows :data:`REVIEW_TYPES`, not the cookie, so the UI is stable.
    An absent, empty, or fully unrecognized cookie means "show everything" —
    the same thing the checkboxes show on a first visit.
    """
    raw = request.cookies.get(_REVIEW_TYPES_COOKIE, "")
    selected = [t for t in REVIEW_TYPES if t in set(raw.split(","))]
    return selected or list(REVIEW_TYPES)


@app.route("/api/set-review-types", methods=["POST"])
def set_review_types():
    """Persist the selected review categories via cookie."""
    raw = (request.json or {}).get("types")
    if not isinstance(raw, list):
        return jsonify({"error": "types must be a list"}), 400
    unknown = [t for t in raw if t not in REVIEW_TYPES]
    if unknown:
        return jsonify({"error": f"Unknown review types: {', '.join(map(str, unknown))}"}), 400
    types = [t for t in REVIEW_TYPES if t in set(raw)]
    resp = make_response(jsonify({"ok": True, "types": types}))
    resp.set_cookie(
        _REVIEW_TYPES_COOKIE, ",".join(types),
        max_age=365 * 24 * 3600, samesite="Lax",
    )
    return resp


# Which statuses the reader home page shows. Mirrors the review-types cookie
# above, with one difference: an absent cookie is not "everything". A first
# visit hides archived books, so the default has to be distinguishable from a
# deliberately emptied selection (which, like the category picker, means "no
# filter at all").
_STATUS_FILTER_COOKIE = "reader_status_filter"
_DEFAULT_STATUS_FILTER = tuple(s for s in PROJECT_STATUSES if s != "archived")


def _get_status_filter() -> list[str]:
    """Read the ticked statuses from cookie; default to everything but archived.

    Order follows :data:`PROJECT_STATUSES`, not the cookie, so the UI is stable.
    Returns ``[]`` when the cookie is present but empty — the reader unticked
    every box, which the page renders as "show all".
    """
    raw = request.cookies.get(_STATUS_FILTER_COOKIE)
    if raw is None:
        return list(_DEFAULT_STATUS_FILTER)
    return [s for s in PROJECT_STATUSES if s in set(raw.split(","))]


@app.route("/api/set-status-filter", methods=["POST"])
def set_status_filter():
    """Persist the ticked project statuses via cookie."""
    raw = (request.json or {}).get("statuses")
    if not isinstance(raw, list):
        return jsonify({"error": "statuses must be a list"}), 400
    unknown = [s for s in raw if s not in PROJECT_STATUSES]
    if unknown:
        return jsonify({"error": f"Unknown statuses: {', '.join(map(str, unknown))}"}), 400
    statuses = [s for s in PROJECT_STATUSES if s in set(raw)]
    resp = make_response(jsonify({"ok": True, "statuses": statuses}))
    resp.set_cookie(
        _STATUS_FILTER_COOKIE, ",".join(statuses),
        max_age=365 * 24 * 3600, samesite="Lax",
    )
    return resp


@app.route("/api/project/<project_id>/archived", methods=["PATCH"])
def update_project_archived(project_id):
    """Archive or unarchive a project — the one status the reader sets by hand.

    Body: ``{"archived": true|false}``. The other three statuses are derived
    from the files by ``project_cards.derive_status``, so the response hands
    back the freshly derived status: after unarchiving, the caller needs to know
    where the card landed to re-apply the status filter.

    Not to be confused with ``GET /api/project/<id>/status``, which is the
    unrelated dashboard scan of the project's files.
    """
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    archived = (request.json or {}).get("archived")
    if not isinstance(archived, bool):
        return jsonify({"error": "archived must be true or false"}), 400
    config = _load_project_config(project_id)
    config["archived"] = archived
    # Drop the pre-derivation hand-set field so the two can never disagree.
    config.pop("status", None)
    _save_project_config(project_id, config)
    card = build_project_card(_resolve_project_dir(project_id), project_id)
    return jsonify({"ok": True, "archived": archived, "status": card["status"]})


@app.route(
    "/api/project/<project_id>/chapter-manifest/<chapter_id>",
    methods=["PATCH"],
)
def update_chapter_manifest_label(project_id, chapter_id):
    """Override the `label` for a front_matter/back_matter manifest entry.

    Body: ``{"label": "<new label>"}``. Empty string clears the override
    (falls back to existing label/heading behavior in EPUB and reader).
    Numbered chapters (``kind == "chapter"``) are rejected with 400.
    """
    if not _safe_id(project_id) or not _safe_id(chapter_id):
        return jsonify({"error": "Bad request"}), 400

    data = request.get_json(silent=True) or {}
    if "label" not in data:
        return jsonify({"error": "Missing 'label' field"}), 400
    raw = data["label"]
    label = (raw if isinstance(raw, str) else "").strip()
    if len(label) > 500:
        return jsonify({"error": "Label too long (max 500 characters)"}), 400

    config = _load_project_config(project_id)
    manifest = config.get("chapter_manifest")
    if not isinstance(manifest, list):
        return jsonify({"error": "No chapter_manifest for project"}), 404

    entry = next(
        (e for e in manifest
         if isinstance(e, dict) and e.get("id") == chapter_id),
        None,
    )
    if entry is None:
        return jsonify({"error": f"No manifest entry for {chapter_id}"}), 404

    kind = entry.get("kind", "chapter")
    if kind == "chapter":
        return jsonify({
            "error": "Cannot edit label for numbered chapters",
        }), 400

    if label:
        entry["label"] = label
    else:
        entry.pop("label", None)

    _save_project_config(project_id, config)
    return jsonify(entry)


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
    from src.api_translator import load_llm_config, model_supports_thinking

    config = copy.deepcopy(load_llm_config())
    for provider in config.get("providers", []):
        env_var = provider.pop("api_key_env_var", None)
        provider["available"] = bool(os.getenv(env_var)) if env_var else False
        for m in provider.get("models", []):
            m["supports_thinking"] = model_supports_thinking(m["id"])
    return jsonify(config)


# ============================================================================
# Setup routes — style guide wizard + glossary bootstrap
# ============================================================================


@app.route("/api/setup/<project_id>/prompts/questions", methods=["POST"])
def setup_questions_prompt(project_id):
    """Return the full prompt for LLM question generation (for copy/paste)."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)

    from src.style_guide_wizard import (
        build_question_prompt,
        get_active_questions,
        load_source_sample,
    )
    data = request.get_json()
    answers = data.get("answers", {})
    # Convert string indices back to int
    answers = {k: int(v) if isinstance(v, str) and v.isdigit() else v for k, v in answers.items()}

    fixed_questions, conditional_questions, manifest = get_active_questions(project_dir)
    source_text = load_source_sample(project_dir)
    target_lang = data.get("target_lang", "Spanish")
    locale = data.get("locale", "mx")

    all_questions = list(fixed_questions) + list(conditional_questions)
    prompt = build_question_prompt(
        source_text,
        target_lang,
        locale,
        all_questions,
        answers,
        manifest=manifest,
    )
    return jsonify({"prompt": prompt})


@app.route("/api/setup/<project_id>/prompts/style-guide", methods=["POST"])
def setup_style_guide_prompt(project_id):
    """Return the full prompt for style guide generation (for copy/paste)."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)

    from src.style_guide_wizard import (
        build_style_guide_prompt,
        get_active_questions,
        load_source_sample,
    )
    data = request.get_json()
    answers = data.get("answers", {})
    answers = {k: int(v) if isinstance(v, str) and v.isdigit() else v for k, v in answers.items()}
    extra_questions = data.get("extra_questions", [])

    fixed_questions, conditional_questions, _manifest = get_active_questions(project_dir)
    all_questions = list(fixed_questions) + list(conditional_questions) + list(extra_questions)
    source_text = load_source_sample(project_dir)
    target_lang = data.get("target_lang", "Spanish")
    locale = data.get("locale", "mx")

    prompt = build_style_guide_prompt(all_questions, answers, source_text, target_lang, locale)
    return jsonify({"prompt": prompt})


@app.route("/api/setup/<project_id>/style-guide", methods=["POST"])
def setup_save_style_guide(project_id):
    """Save style guide content to style.json. Preserves light_content if set."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)

    data = request.get_json()
    content = data.get("content", "")
    if not content.strip():
        return jsonify({"error": "Empty style guide"}), 400

    output_path = project_dir / "style.json"

    existing_light = None
    created_at = None
    if output_path.exists():
        try:
            existing = load_style_guide(output_path)
            existing_light = existing.light_content
            created_at = existing.created_at
        except Exception as exc:
            app.logger.warning("Could not read existing style.json at %s: %s", output_path, exc)

    now = datetime.now()
    guide = StyleGuide(
        content=content,
        light_content=existing_light,
        version="1.0",
        created_at=created_at or now,
        updated_at=now,
    )
    save_style_guide(guide, output_path)
    return jsonify({"ok": True, "path": str(output_path)})


@app.route("/api/setup/<project_id>/style-guide/light", methods=["POST"])
def setup_save_light_style_guide(project_id):
    """Save the optional light style guide used for sentence retranslation.

    Empty body clears it (falls back to the full guide at retranslate time).
    Requires the main style guide to already exist.
    """
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)
    style_path = project_dir / "style.json"
    if not style_path.exists():
        return jsonify({"error": "Save the main style guide first."}), 404

    data = request.get_json() or {}
    light_raw = (data.get("light_content") or "").strip()
    new_light = light_raw if light_raw else None

    try:
        guide = load_style_guide(style_path)
    except Exception as exc:
        app.logger.exception("Could not load style.json at %s", style_path)
        return jsonify({"error": f"Could not read style.json: {exc}"}), 500

    guide.light_content = new_light
    guide.updated_at = datetime.now()
    save_style_guide(guide, style_path)
    return jsonify({"ok": True, "light_content": new_light or ""})


@app.route("/api/setup/<project_id>/style-guide/fallback", methods=["POST"])
def setup_style_guide_fallback(project_id):
    """Generate style guide from answers using fallback (no LLM)."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400

    from src.style_guide_wizard import get_active_questions, answers_to_style_guide_fallback
    data = request.get_json()
    answers = data.get("answers", {})
    answers = {k: int(v) if isinstance(v, str) and v.isdigit() else v for k, v in answers.items()}
    extra_questions = data.get("extra_questions", [])

    project_dir = _resolve_project_dir(project_id)
    fixed_questions, conditional_questions, _manifest = get_active_questions(project_dir)
    all_questions = list(fixed_questions) + list(conditional_questions) + list(extra_questions)
    content = answers_to_style_guide_fallback(all_questions, answers)
    return jsonify({"content": content})


@app.route("/api/setup/<project_id>/text-features/rescan", methods=["POST"])
def setup_text_features_rescan(project_id):
    """Force a full-text feature rescan for conditional style-guide questions.

    This regenerates (and overwrites) ``text_features.json`` for the project.
    The frontend can call this and then reload to re-render conditional questions.
    """
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)

    from src.style_guide_wizard import get_active_questions

    try:
        fixed, conditional, manifest = get_active_questions(project_dir, force=True)
        return jsonify(
            {
                "ok": True,
                "fixed_count": len(fixed),
                "conditional_count": len(conditional),
                "manifest_path": str(project_dir / "text_features.json"),
            }
        )
    except Exception as exc:
        app.logger.exception("Text feature rescan failed for %s", project_id)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/setup/<project_id>/extract-candidates", methods=["POST"])
def setup_extract_candidates(project_id):
    """Run heuristic glossary extraction and return candidates."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)

    payload = request.get_json(silent=True) or {}
    try:
        zipf_offset = float(payload.get("zipf_offset", 0.0))
    except (TypeError, ValueError):
        zipf_offset = 0.0
    zipf_offset = max(-1.0, min(1.0, zipf_offset))

    from scripts.extract_glossary_candidates import extract_candidates
    from src.style_guide_wizard import load_source_sample
    text = load_source_sample(project_dir, max_words=200000)  # High cap — use all available chunks
    if not text:
        return jsonify({"error": "No source text found (add chunks/ or source.txt)"}), 404

    glossary = None
    glossary_path = project_dir / "glossary.json"
    if glossary_path.exists():
        glossary = load_glossary(glossary_path)

    # Defaults (4.0 / 3.0) mirror extract_candidates() signature; update both spots if changed.
    report = extract_candidates(
        text,
        glossary=glossary,
        max_zipf_capitalized=4.0 + zipf_offset,
        max_zipf_mixed=3.0 + zipf_offset,
    )
    candidates = [
        {"term": c.term, "frequency": c.frequency,
         "context_sentence": c.context_sentence}
        for c in report.candidates
    ]
    return jsonify({"candidates": candidates, "total": len(candidates)})


def _build_glossary_prompt_for_request(project_id, project_dir, data):
    """Shared helper: assemble args for ``build_glossary_prompt`` from request payload.

    Honors ``context_mode`` ("full-text" | "word"). In word mode, enriches each
    candidate with first-appearance contexts and sorts by first appearance, so
    the prompt mirrors what ``scripts/extract_glossary_candidates.py`` produces
    when run with ``--bootstrap-context-mode word`` at default settings.
    """
    from src.glossary_bootstrap import build_glossary_prompt
    from src.style_guide_wizard import load_source_sample

    candidates = list(data.get("candidates", []))[:1000]
    target_lang = data.get("target_lang", "Spanish")
    glossary_guidance = data.get("glossary_guidance", "")
    context_mode = data.get("context_mode", "full-text")
    if context_mode not in ("full-text", "word"):
        context_mode = "full-text"

    style_content = ""
    style_path = project_dir / "style.json"
    if style_path.exists():
        try:
            sg = load_style_guide(style_path)
            style_content = sg.content
        except Exception:
            pass

    source_sample = ""
    book_title = ""
    context_unit_label = ""

    if context_mode == "word":
        from src.utils.glossary_context import find_first_word_contexts, precompute_chapter_tokens
        from src.utils.source_text import load_clean_source_text

        words_before = 10
        words_after = 6
        fragments_per_term = 2

        full_text, _, _ = load_clean_source_text(project_dir)
        chapter_texts = [("source", full_text or "")]
        precomputed = precompute_chapter_tokens(chapter_texts)
        for cand in candidates:
            term = cand.get("term") or cand.get("english") or ""
            pos, ctx = find_first_word_contexts(
                term, chapter_texts,
                max_contexts=fragments_per_term,
                words_before=words_before,
                words_after=words_after,
                _precomputed=precomputed,
            )
            cand["first_position"] = pos
            cand["contexts"] = ctx
        candidates.sort(
            key=lambda c: c.get("first_position") if c.get("first_position") is not None
                          else (10**9, 10**9)
        )
        book_title = _project_title(project_id)
        context_unit_label = (
            f"fragments (~{words_before} words before / {words_after} words after)"
        )
    else:
        source_sample = load_source_sample(project_dir)

    return build_glossary_prompt(
        candidates, source_sample, style_content, target_lang, glossary_guidance,
        context_mode=context_mode,
        book_title=book_title,
        context_unit_label=context_unit_label,
    )


@app.route("/api/setup/<project_id>/prompts/glossary", methods=["POST"])
def setup_glossary_prompt(project_id):
    """Return the full prompt for glossary bootstrap (for copy/paste)."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)

    data = request.get_json() or {}
    prompt = _build_glossary_prompt_for_request(project_id, project_dir, data)
    return jsonify({"prompt": prompt})


@app.route("/api/setup/<project_id>/glossary", methods=["GET"])
def setup_load_glossary(project_id):
    """Return existing glossary terms in proposal-shaped rows for the edit table."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    glossary_path = _resolve_project_dir(project_id) / "glossary.json"
    if not glossary_path.exists():
        return jsonify({"terms": []})
    g = load_glossary(glossary_path)
    return jsonify({"terms": [
        {
            "english": t.english,
            "spanish": t.spanish,
            "type": t.type.value if hasattr(t.type, "value") else t.type,
            "context": t.context or "",
            "alternatives": t.alternatives or [],
        }
        for t in g.terms
    ]})


@app.route("/api/setup/<project_id>/glossary", methods=["POST"])
def setupsave_glossary(project_id):
    """Save glossary terms to glossary.json.

    Modes:
    - "merge" (default): only new terms (by english.lower()) are appended to existing file.
    - "replace": submitted list is treated as the authoritative full glossary.
    """
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)

    data = request.get_json() or {}
    terms_data = data.get("terms", [])
    mode = data.get("mode", "merge")

    if mode not in ("merge", "replace"):
        return jsonify({"error": "Invalid mode"}), 400
    if mode == "merge" and not terms_data:
        return jsonify({"error": "No terms provided"}), 400
    if mode == "replace" and not terms_data:
        return jsonify({"error": "Refusing to replace glossary with empty list"}), 400

    terms = glossary_terms_from_proposals(terms_data)
    glossary_path = project_dir / "glossary.json"

    if mode == "replace":
        glossary = proposals_to_glossary(terms)
        save_glossary(glossary, glossary_path)
        return jsonify({"ok": True, "total": len(terms), "mode": "replace"})

    # Merge with existing if present
    if glossary_path.exists():
        existing = load_glossary(glossary_path)
        existing_set = {t.english.lower() for t in existing.terms}
        new_terms = [t for t in terms if t.english.lower() not in existing_set]
        existing.terms.extend(new_terms)
        save_glossary(existing, glossary_path)
        return jsonify({"ok": True, "total": len(existing.terms), "new": len(new_terms)})
    else:
        glossary = proposals_to_glossary(terms)
        save_glossary(glossary, glossary_path)
        return jsonify({"ok": True, "total": len(terms), "new": len(terms)})


# ============================================================================
# Generate via API endpoints (direct LLM calls from the UI)
# ============================================================================


@app.route("/api/setup/<project_id>/questions/generate", methods=["POST"])
def setup_questions_generate(project_id):
    """Generate additional style-guide questions via LLM (direct API call)."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)

    from src.style_guide_wizard import build_question_prompt, get_active_questions, load_source_sample
    from src.api_translator import call_llm

    data = request.get_json()
    answers = data.get("answers", {})
    answers = {k: int(v) if isinstance(v, str) and v.isdigit() else v for k, v in answers.items()}
    provider = data.get("provider", "anthropic")
    model = data.get("model")

    fixed_questions, conditional_questions, manifest = get_active_questions(project_dir)
    source_text = load_source_sample(project_dir)
    target_lang = data.get("target_lang", "Spanish")
    locale = data.get("locale", "mx")

    all_questions = list(fixed_questions) + list(conditional_questions)
    prompt = build_question_prompt(
        source_text,
        target_lang,
        locale,
        all_questions,
        answers,
        manifest=manifest,
    )

    try:
        result = call_llm(prompt, provider=provider, model=model, call_type="style_questions", project_slug=project_id)
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
    project_dir = _resolve_project_dir(project_id)

    from src.style_guide_wizard import get_active_questions, build_style_guide_prompt, load_source_sample
    from src.api_translator import call_llm

    data = request.get_json()
    answers = data.get("answers", {})
    answers = {k: int(v) if isinstance(v, str) and v.isdigit() else v for k, v in answers.items()}
    extra_questions = data.get("extra_questions", [])
    provider = data.get("provider", "anthropic")
    model = data.get("model")

    fixed_questions, conditional_questions, _manifest = get_active_questions(project_dir)
    all_questions = list(fixed_questions) + list(conditional_questions) + list(extra_questions)
    source_text = load_source_sample(project_dir)
    target_lang = data.get("target_lang", "Spanish")
    locale = data.get("locale", "mx")

    prompt = build_style_guide_prompt(all_questions, answers, source_text, target_lang, locale)

    try:
        content = call_llm(prompt, provider=provider, model=model, call_type="style_guide_generate", project_slug=project_id)
        return jsonify({"content": content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/setup/<project_id>/glossary/generate", methods=["POST"])
def setup_glossary_generate(project_id):
    """Generate glossary translations via LLM (direct API call)."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)

    from src.api_translator import call_llm

    data = request.get_json() or {}
    provider = data.get("provider", "anthropic")
    model = data.get("model")

    prompt = _build_glossary_prompt_for_request(project_id, project_dir, data)

    try:
        result = call_llm(prompt, provider=provider, model=model, max_tokens=8192, call_type="glossary", project_slug=project_id)
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
    existing_ids = {p.name for p in _iter_project_dirs(projects_dir)}
    candidate = slug
    suffix = 2
    while candidate in existing_ids or (projects_dir / candidate).exists():
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
    seen_ids: dict[str, Path] = {}
    for proj_dir in _iter_project_dirs(projects_dir):
        if proj_dir.name in seen_ids:
            app.logger.warning(
                "Duplicate project id %r at %s and %s; skipping the latter",
                proj_dir.name, seen_ids[proj_dir.name], proj_dir,
            )
            continue
        seen_ids[proj_dir.name] = proj_dir
        projects.append(build_project_card(proj_dir, proj_dir.name))

    return render_template(
        "reader.html",
        mode="projects",
        projects=projects,
        t=t,
        lang=_get_ui_lang(),
        reader_ui_version=_get_reader_ui_version(),
        review_types=list(REVIEW_TYPES),
        review_types_selected=_get_review_types(),
        statuses=list(PROJECT_STATUSES),
        statuses_selected=_get_status_filter(),
    )


@app.route("/read/<project_id>")
def reader_chapters(project_id):
    """List chapters with alignments for a project."""
    if not _safe_id(project_id):
        return "Bad request", 400
    align_dir = _resolve_project_dir(project_id) / "alignments"
    t = _reader_strings()
    if not align_dir.exists():
        return render_template("reader.html", mode="not_found", project_id=project_id, t=t, lang=_get_ui_lang()), 404

    # Load all annotations for this project. src.annotations.load_active owns the
    # append-only / tombstone / latest-wins rule keyed on
    # (chapter_id, es_idx, sub_id) — keying on sub_id too is what lets one
    # sentence hold both a footnote and a review note — and skips unparseable
    # lines so a half-written record can't 500 the chapter list.
    project_dir = _resolve_project_dir(project_id)
    from collections import defaultdict
    ann_counts = defaultdict(lambda: defaultdict(int))
    empty_fn_counts = defaultdict(int)
    for rec in load_active(project_dir):
        ann_type = rec.get("type", "flag")
        chapter_key = rec.get("chapter_id", "")
        ann_counts[chapter_key][ann_type] += 1
        # A footnote mark with nothing but its [anchor] is silently dropped from
        # the built EPUB (src/endnotes.py), so the row badges written-vs-total
        # rather than a bare count. is_effectively_blank is the same predicate
        # the home card's book-wide rollup uses, so the two cannot disagree.
        if ann_type == "footnote" and is_effectively_blank(rec.get("content") or ""):
            empty_fn_counts[chapter_key] += 1
    all_annotations = dict(ann_counts)  # chapter_id -> {type -> count}

    # Evaluator/judge findings in the six review categories, bucketed by
    # chapter. One extra walk of evaluations/*.json — cheap next to the
    # alignments this route already opens in full.
    flag_counts_by_chapter = load_chapter_type_counts(project_dir)

    # Check for pending corrections (file must exist AND contain at least one
    # non-blank line — a stale file with only whitespace shouldn't trigger the banner).
    _corr_path = project_dir / "corrections.jsonl"
    has_corrections = False
    if _corr_path.exists() and _corr_path.stat().st_size > 1:
        try:
            with open(_corr_path, encoding="utf-8") as _cf:
                has_corrections = any(line.strip() for line in _cf)
        except OSError:
            has_corrections = False
        # Auto-clean stale empty/whitespace-only file so it doesn't keep nagging.
        if not has_corrections:
            try:
                _corr_path.unlink()
            except OSError:
                pass

    # Load reviewed status
    reviewed = _load_reviewed(project_dir)

    # Load chapter manifest for display labels
    manifest = _load_chapter_manifest_for_project(project_id)
    chapter_prefix = t.get("chapter_prefix", "Chapter")

    chapters = []
    for f in sorted(align_dir.glob("*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            ch_id = f.stem
            # Source runs with no translation at all (src/sentence_aligner.py).
            # Distinct from low confidence: those sentences ARE translated, just
            # matched weakly. A gap means the prose is missing outright.
            coverage = data.get("coverage") or {}
            ann = all_annotations.get(ch_id, {})
            # Fold the three review-type annotations (word choice, inconsistency,
            # and "Other"/flag) into one "to review" count; footnotes stay separate
            # because they feed endnotes. Same list the home-page card rolls up
            # (src/annotations/summary.py), so the two can't drift.
            review_count = sum(ann.get(t, 0) for t in REVIEW_ANNOTATION_TYPES)
            footnote_count = ann.get("footnote", 0)
            empty_footnotes = empty_fn_counts.get(ch_id, 0)

            entry = manifest.get(ch_id) or {}
            chapters.append({
                "id": ch_id,
                "display_label": _chapter_display_label(ch_id, manifest, chapter_prefix),
                "kind": entry.get("kind", "chapter"),
                "gap_count": coverage.get("gap_count", 0),
                "gap_chars": coverage.get("en_orphan_chars", 0),
                "review_count": review_count,
                "footnote_count": footnote_count,
                "filled_footnotes": footnote_count - empty_footnotes,
                "empty_footnotes": empty_footnotes,
                "flag_counts": flag_counts_by_chapter.get(ch_id) or empty_type_counts(),
                "reviewed": ch_id in reviewed,
            })
        except (json.JSONDecodeError, OSError):
            continue

    return render_template(
        "reader.html", mode="chapters",
        project_id=project_id, project_title=_project_title(project_id),
        project_spanish_title=_load_project_config(project_id).get("spanish_title", ""),
        chapters=chapters,
        has_corrections=has_corrections, t=t, lang=_get_ui_lang(),
        review_types=list(REVIEW_TYPES),
        review_types_selected=_get_review_types(),
    )


@app.route("/read/<project_id>/<chapter>")
def reader_view(project_id, chapter):
    """Render the reader view for a chapter."""
    t = _reader_strings()
    if not _safe_id(project_id) or not _safe_id(chapter):
        return "Bad request", 400
    align_dir = _resolve_project_dir(project_id) / "alignments"
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

    # Resolve display label using the chapter manifest (if any).
    manifest = _load_chapter_manifest_for_project(project_id)
    chapter_prefix = t.get("chapter_prefix", "Chapter")
    display_label = _chapter_display_label(chapter, manifest, chapter_prefix)

    project_dir = _resolve_project_dir(project_id)
    has_pending_corrections = _chapter_has_pending_corrections(project_dir, chapter)

    # Sheet UI version: a `?ui=` query param overrides (and persists) the cookie
    # so shared "?ui=v2" links open the redesigned sheet; otherwise the cookie
    # preference wins, defaulting to classic.
    ui_override = request.args.get("ui")
    if ui_override in _READER_UI_VERSIONS:
        ui_version = ui_override
    else:
        ui_override = None
        ui_version = _get_reader_ui_version()

    resp = make_response(render_template(
        "reader.html", mode="read",
        project_id=project_id, project_title=_project_title(project_id),
        chapter=chapter, t=t, lang=_get_ui_lang(),
        prev_chapter=prev_chapter, next_chapter=next_chapter,
        display_label=display_label,
        has_pending_corrections=has_pending_corrections,
        reader_ui_version=ui_version,
        review_types=list(REVIEW_TYPES),
        review_types_selected=_get_review_types(),
    ))
    if ui_override is not None:
        resp.set_cookie(
            "reader_ui_version", ui_version,
            max_age=365 * 24 * 3600, samesite="Lax",
        )
    return resp


@app.route("/api/alignment/<project_id>/<chapter>")
def get_alignment(project_id, chapter):
    """Return alignment JSON for a chapter, enriched with paragraph breaks."""
    if not _safe_id(project_id) or not _safe_id(chapter):
        return jsonify({"error": "Invalid ID"}), 400
    project_dir = _resolve_project_dir(project_id)
    align_path = project_dir / "alignments" / f"{chapter}.json"
    if not align_path.exists():
        return jsonify({"error": f"Alignment not found: {project_id}/{chapter}"}), 404

    try:
        with open(align_path, encoding="utf-8") as f:
            data = json.load(f)

        # Attach per-row `text_in_chunk` so the reader's retranslate panel can
        # populate the "current translation" textarea with the literal chunk
        # substring (the aligner's `es` field is normalized and not byte-identical
        # to chunk.translated_text).
        chunks_dir = project_dir / "chunks"
        if chunks_dir.exists():
            _attach_text_in_chunk(data, chunks_dir)

        # Enrich with paragraph break info from the combined chapter text.
        # chapters/<chapter>.txt is the canonical output of Combine (see
        # project_combine / project_align); align refreshes it before writing
        # alignment JSON, so it should always be in sync here.
        chapter_text_path = project_dir / "chapters" / f"{chapter}.txt"
        if chapter_text_path.exists():
            _enrich_alignment(data, chapter_text_path, project_id)

        return jsonify(data)
    except (json.JSONDecodeError, OSError) as e:
        return jsonify({"error": str(e)}), 500


def _attach_text_in_chunk(alignment_data: dict, chunks_dir: Path, target_lang: str = "es") -> None:
    """Mutate alignment rows to include `text_in_chunk` (and chunk char offsets).

    The aligner emits `es` text via pysbd.split + .strip() + " ".join() for N:1
    groups, which is NOT byte-identical to chunk.translated_text. The reader's
    retranslate flow needs the literal chunk substring so /api/sentence/replace
    can find it without fuzzy matching. We re-split each chunk's translated_text
    with the same splitter the aligner uses and walk char positions.
    """
    from collections import defaultdict
    from src.sentence_aligner import _split_sentences_with_para_indices

    rows_by_chunk: dict[str, list[dict]] = defaultdict(list)
    for row in alignment_data.get("alignments", []):
        cid = row.get("chunk_id")
        if cid and "es_idx" in row:
            rows_by_chunk[cid].append(row)

    for chunk_id, rows in rows_by_chunk.items():
        chunk_path = chunks_dir / f"{chunk_id}.json"
        if not chunk_path.exists():
            continue
        try:
            chunk_data = json.loads(chunk_path.read_text(encoding="utf-8"))
            chunk_mtime = chunk_path.stat().st_mtime
        except (json.JSONDecodeError, OSError):
            continue
        chunk_text = chunk_data.get("translated_text") or ""
        if not chunk_text:
            continue

        try:
            sentences, _ = _split_sentences_with_para_indices(chunk_text, target_lang)
        except Exception:
            continue

        ranges: list[Optional[tuple[int, int]]] = []
        cursor = 0
        for sent in sentences:
            idx = chunk_text.find(sent, cursor)
            if idx == -1:
                stripped = sent.strip()
                idx = chunk_text.find(stripped, cursor) if stripped else -1
                if idx == -1:
                    ranges.append(None)
                    continue
                ranges.append((idx, idx + len(stripped)))
                cursor = idx + len(stripped)
            else:
                ranges.append((idx, idx + len(sent)))
                cursor = idx + len(sent)

        # The aligner offsets es_idx by cumulative chunk counts. The minimum
        # es_idx in this chunk's rows is the chunk-local offset.
        es_offset = min(r["es_idx"] for r in rows)

        for row in rows:
            indices = row.get("es_indices") or [row["es_idx"]]
            local = [i - es_offset for i in indices]
            valid = [li for li in local if 0 <= li < len(ranges) and ranges[li] is not None]
            if not valid:
                continue
            start = ranges[valid[0]][0]
            end = ranges[valid[-1]][1]
            row["text_in_chunk"] = chunk_text[start:end]
            row["chunk_offset_start"] = start
            row["chunk_offset_end"] = end
            row["chunk_mtime"] = chunk_mtime


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
    #
    # A single paragraph may contain text AND one or more [IMAGE:...]
    # placeholders glued together by single newlines (e.g. when chunk
    # boundaries clobber the surrounding blank line, or when an LLM
    # retranslate drops one). We split each paragraph on the placeholder
    # regex so the images aren't silently dropped: any leading text
    # contributes a "para" event (matching the aligner's view that this is
    # one paragraph with a single para_start), then each image contributes
    # an "image" event. Trailing text after an in-paragraph image is not
    # emitted as a "para" event because the aligner only flags the
    # paragraph's first sentence as para_start.
    events = []  # list of ("para", first_2_words) or ("image", src, alt)
    for i in range(1, len(paragraphs)):
        para = paragraphs[i]
        matches = list(_IMAGE_PLACEHOLDER_RE.finditer(para))

        if not matches:
            words = para.split()[:2]
            if words:
                events.append(("para", " ".join(words)))
            continue

        leading = para[: matches[0].start()].strip()
        if leading:
            words = leading.split()[:2]
            if words:
                events.append(("para", " ".join(words)))

        for m in matches:
            src = m.group(1)  # e.g. "images/i010.jpg"
            alt = m.group(2) or ""
            events.append(("image", src, alt))

    # Filter out [IMAGE:...] placeholder sentences from alignment records.
    # The aligner treats them as sentences but they're not readable text.
    alignments = [
        a for a in alignment_data.get("alignments", [])
        if not _IMAGE_PLACEHOLDER_RE.fullmatch(a.get("es", "").strip())
    ]
    alignment_data["alignments"] = alignments

    insert_queue = []  # (alignment_list_index, image_record)

    # When the aligner has already set para_start flags, use positional
    # correspondence between chapter-text para events and para_start records
    # for image placement.  This avoids the fragile text-matching that breaks
    # on encoding-corrupted .txt files.
    has_aligner_para_start = any(a.get("para_start") for a in alignments)

    if has_aligner_para_start:
        para_start_positions = [i for i, a in enumerate(alignments) if a.get("para_start")]
        para_event_counter = 0
        pending_images = []
        for event in events:
            if event[0] == "image":
                _, src, alt = event
                pending_images.append({
                    "type": "image",
                    "src": f"/projects/{project_id}/{src}",
                    "alt": alt,
                })
            else:  # "para"
                if para_event_counter < len(para_start_positions):
                    ai = para_start_positions[para_event_counter]
                    for img in pending_images:
                        insert_queue.append((ai, img))
                    pending_images = []
                para_event_counter += 1
        for img in pending_images:
            insert_queue.append((len(alignments), img))

    else:
        # Legacy path: text-matching for alignment files without aligner para_start.
        event_idx = 0
        pending_images = []

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
                    matched = True
                elif len(event_key.split()) >= 2:
                    event_first = event_key.split()[0]
                    es_first = es_text.split()[0] if es_text else ""
                    if event_first and es_first == event_first:
                        has_exact_later = any(
                            " ".join(alignments[j].get("es", "").split()[:2]) == event_key
                            for j in range(ai + 1, len(alignments))
                        )
                        if not has_exact_later:
                            matched = True

                if matched:
                    for img in pending_images:
                        insert_queue.append((ai, img))
                    pending_images = []
                    a["para_start"] = True
                    event_idx += 1

        for img in pending_images:
            insert_queue.append((len(alignments), img))

        while event_idx < len(events):
            if events[event_idx][0] == "image":
                _, src, alt = events[event_idx]
                insert_queue.append((len(alignments), {
                    "type": "image",
                    "src": f"/projects/{project_id}/{src}",
                    "alt": alt,
                }))
            event_idx += 1

    # Mark verse line breaks: for each verse-block paragraph in the source,
    # lines after the first correspond to mid-stanza line breaks. The first
    # line of each stanza is already flagged via para_start above; we tag
    # subsequent verse lines so the reader can render a visible break.
    verse_line_keys: list[str] = []
    for para in paragraphs:
        if not is_verse_block(para):
            continue
        lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
        for ln in lines[1:]:
            words = ln.split()[:2]
            if words:
                verse_line_keys.append(" ".join(words))

    if verse_line_keys:
        key_idx = 0
        for ai, a in enumerate(alignments):
            if key_idx >= len(verse_line_keys):
                break
            if a.get("para_start"):
                continue
            es_text = a.get("es", "").strip()
            if not es_text:
                continue
            es_words = " ".join(es_text.split()[:2])
            target = verse_line_keys[key_idx]
            matched = False
            if es_words == target:
                matched = True
            elif len(target.split()) >= 2:
                target_first = target.split()[0]
                es_first = es_text.split()[0]
                if target_first and es_first == target_first:
                    has_exact_later = any(
                        " ".join(alignments[j].get("es", "").split()[:2]) == target
                        for j in range(ai + 1, len(alignments))
                    )
                    if not has_exact_later:
                        matched = True
            if matched:
                a["verse_line_break"] = True
                key_idx += 1

    # Insert image records (reverse order to preserve indices)
    for insert_idx, img_record in reversed(insert_queue):
        alignments.insert(insert_idx, img_record)


# ============================================================================
# Reader-mode book search ("Find in book")
#
# Folded substring concordance over a single book. side=translation searches
# the aligned Spanish (`es`); side=source searches the English source: the
# aligned `en` for translated chapters (navigable pairs) plus a raw chunk
# `source_text` scan for chapters that have no alignment yet (display-only
# KWIC, "not translated"). See design 20260603-113634 (T1/T2/T7, D1/D4/D5/D7/D9).
# ============================================================================

# Minimum non-space query length; shorter queries return no results (DR4).
_SEARCH_MIN_QUERY = 2
# Anchor prefix length for result-click nav (D2: generous prefix, first-match).
_SEARCH_ANCHOR_LEN = 80
# Words of context on each side of a KWIC source snippet (D4).
_SEARCH_KWIC_WORDS = KWIC_WORDS

# The folding/KWIC primitives live in src/utils/text_utils so the annotation
# concordance can reuse them from a CLI (importing web_ui there would cycle).
# Aliased to their original private names to keep call sites and D5/D4 comments
# in this module untouched.
_fold = fold
_fold_with_map = fold_with_map
_find_match = find_folded
_kwic_window = kwic_window


def _chunk_chapter_ids(project_dir: Path) -> set[str]:
    """Chapter ids that have source chunks (derived from chunk filenames)."""
    ids: set[str] = set()
    chunks_dir = project_dir / "chunks"
    if chunks_dir.exists():
        for f in chunks_dir.glob("*_chunk_*.json"):
            stem = f.stem
            i = stem.rfind("_chunk_")
            if i != -1:
                ids.add(stem[:i])
    return ids


def _search_alignment_chapter(project_dir: Path, chapter: str, field: str,
                              folded_q: str, label: str) -> list[dict]:
    """Folded-substring hits in one chapter's alignment, matching ``field``
    (``es`` or ``en``). Returns navigable source+translation pair rows. Raises
    on a malformed/unreadable alignment file (caller maps to a 500)."""
    align_path = project_dir / "alignments" / f"{chapter}.json"
    data = json.loads(align_path.read_text(encoding="utf-8"))

    rows: list[dict] = []
    for a in data.get("alignments", []):
        es = a.get("es", "") or ""
        en = a.get("en", "") or ""
        # [IMAGE:...] placeholder rows are not readable text (design body).
        if _IMAGE_PLACEHOLDER_RE.fullmatch(es.strip()):
            continue
        # A navigable pair displays the es (primary) and jumps via its es prefix.
        # An en-side match whose es is empty has nothing to show or anchor to, so
        # it would render a dead row (anchor="" -> reader.js no-ops); skip it.
        if not es.strip():
            continue
        haystack = es if field == "es" else en
        m = _find_match(haystack, folded_q)
        if m is None:
            continue
        start, end = m
        rows.append({
            "chapter": chapter,
            "chapter_label": label,
            "translated": True,
            "es": es,
            "en": en,
            "match_field": field,
            "match_start": start,
            "match_end": end,
            # Result-click nav: reader.js matches a.es.startsWith(anchor) (D1/D2)
            # and uses es_idx as a tie-breaker when several es share the prefix.
            "anchor": es[:_SEARCH_ANCHOR_LEN],
            "es_idx": a.get("es_idx"),
        })
    return rows


def _search_source_chapter(project_dir: Path, chapter: str, folded_q: str,
                           label: str) -> list[dict]:
    """Folded-substring hits over a chapter's raw source text, returned as
    display-only KWIC snippets (D4/D7). Used for chapters that have no
    alignment file yet (untranslated). No jump, no segmentation."""
    text, _mtime, _kind = load_chapter_source_text(project_dir, chapter)
    if not text:
        return []
    folded, orig_index = _fold_with_map(text)
    rows: list[dict] = []
    search_from = 0
    while True:
        pos = folded.find(folded_q, search_from)
        if pos == -1:
            break
        start = orig_index[pos]
        end = orig_index[pos + len(folded_q) - 1] + 1
        snippet, ms, me = _kwic_window(text, start, end)
        rows.append({
            "chapter": chapter,
            "chapter_label": label,
            "translated": False,
            "snippet": snippet,
            "match_field": "snippet",
            "match_start": ms,
            "match_end": me,
        })
        search_from = pos + len(folded_q)
    return rows


def _log_search_query(project_dir: Path, q: str, side: str, n_results: int) -> None:
    """Append one query record to search_queries.jsonl. Best-effort: a write
    failure is logged and swallowed so search keeps serving (D6/D10)."""
    record = {
        "q": q,
        "side": side,
        "n_results": n_results,
        "ts": datetime.now().isoformat(),
    }
    try:
        with open(project_dir / "search_queries.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        app.logger.warning("Failed to log search query for %s: %s", project_dir.name, exc)


@app.route("/api/search/<project_id>")
def search_book(project_id):
    """Folded-substring concordance search across one book.

    Query params: ``q`` (fragment), ``side`` (``translation`` | ``source``,
    default ``translation``). Returns results in document order, each row a
    navigable source+translation pair (translated) or a display-only KWIC
    snippet (untranslated source).
    """
    if not _safe_id(project_id):
        return jsonify({"error": "Invalid ID"}), 400

    q = (request.args.get("q") or "").strip()
    side = request.args.get("side", "translation")
    if side not in ("translation", "source"):
        side = "translation"

    folded_q = _fold(q)
    if len(q) < _SEARCH_MIN_QUERY or not folded_q:
        return jsonify({"results": [], "query": q, "side": side,
                        "n_results": 0, "n_chapters": 0})

    project_dir = _resolve_project_dir(project_id)
    manifest = _load_chapter_manifest_for_project(project_id)
    chapter_prefix = _reader_strings().get("chapter_prefix", "Chapter")

    align_dir = project_dir / "alignments"
    aligned = {f.stem for f in align_dir.glob("*.json")} if align_dir.exists() else set()
    # Source side also covers untranslated chapters (chunks but no alignment).
    source_only = (_chunk_chapter_ids(project_dir) - aligned) if side == "source" else set()
    all_chapters = sorted(aligned | source_only)

    field = "es" if side == "translation" else "en"
    results: list[dict] = []
    try:
        for ch in all_chapters:
            label = _chapter_display_label(ch, manifest, chapter_prefix)
            if ch in aligned:
                results.extend(
                    _search_alignment_chapter(project_dir, ch, field, folded_q, label))
            else:  # source-side untranslated chapter
                results.extend(
                    _search_source_chapter(project_dir, ch, folded_q, label))
    except (json.JSONDecodeError, OSError) as e:
        return jsonify({"error": str(e)}), 500

    n_chapters = len({r["chapter"] for r in results})
    _log_search_query(project_dir, q, side, len(results))
    return jsonify({"results": results, "query": q, "side": side,
                    "n_results": len(results), "n_chapters": n_chapters})


@app.route("/projects/<project_id>/images/<path:filename>")
def serve_project_image(project_id, filename):
    """Serve an image file from a project's images/ directory."""
    if not _safe_id(project_id):
        return "Bad request", 400
    images_dir = _resolve_project_dir(project_id) / "images"
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

        project_dir = _resolve_project_dir(project_id)

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

        # Persist client-supplied chunk offsets so apply_to_chunk can target
        # the exact span the user edited, even when original_es has a "twin"
        # earlier in the chunk (e.g. a quoted version of the same line, or an
        # [IMAGE:...] caption whose alt text matches the body sentence).
        # Defensive: only persist if both are well-formed non-negative ints
        # (bool is a subclass of int — exclude it explicitly).
        chunk_offset_start = data.get("chunk_offset_start")
        chunk_offset_end = data.get("chunk_offset_end")
        if (
            isinstance(chunk_offset_start, int)
            and not isinstance(chunk_offset_start, bool)
            and isinstance(chunk_offset_end, int)
            and not isinstance(chunk_offset_end, bool)
            and 0 <= chunk_offset_start < chunk_offset_end
            and chunk_offset_end - chunk_offset_start == len(original_es)
        ):
            correction_record["chunk_offset_start"] = chunk_offset_start
            correction_record["chunk_offset_end"] = chunk_offset_end

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


# Wire-protocol sentinel for pre-multi records that have no sub_id on disk.
# GET emits it so the client can edit/delete; POST/DELETE map it back to the
# (es_idx, None) storage slot without rewriting historical rows.
_LEGACY_SUB_ID = "legacy"
_SUB_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _ann_storage_sub_id(sub_id):
    """Map a wire/API sub_id to the storage key (None = legacy single-slot)."""
    if sub_id is None or sub_id == "" or sub_id == _LEGACY_SUB_ID:
        return None
    return sub_id


def _ann_for_wire(ann: dict) -> dict:
    """Copy a stored annotation, giving legacy rows a round-trippable sub_id."""
    out = dict(ann)
    if _ann_storage_sub_id(out.get("sub_id")) is None:
        out["sub_id"] = _LEGACY_SUB_ID
    return out


def _load_annotations(project_dir: Path, chapter_id: str) -> dict[int, list[dict]]:
    """Load annotations for a chapter, grouped by es_idx.

    Keyed by ``(es_idx, sub_id)`` with append-only tombstone / latest-wins
    semantics, so several annotations can share one aligned sentence.
    Hand-authored legacy records without a ``sub_id`` collapse to the
    ``(es_idx, None)`` slot — one per sentence, exactly as before. Within each
    sentence, records are ordered oldest→newest by ``(timestamp, sub_id)``.
    Returns ``{es_idx: [records]}`` (storage shape; use ``_ann_for_wire`` for APIs).
    """
    annotations_path = project_dir / "annotations.jsonl"
    if not annotations_path.exists():
        return {}

    by_key: dict[tuple, dict] = {}
    for line in annotations_path.read_text(encoding="utf-8").strip().split("\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("chapter_id") != chapter_id:
            continue
        es_idx = record.get("es_idx")
        key = (es_idx, _ann_storage_sub_id(record.get("sub_id")))
        if record.get("removed"):
            by_key.pop(key, None)
        elif es_idx is not None:
            by_key[key] = record

    grouped: dict[int, list[dict]] = {}
    # Sort globally by (timestamp, sub_id) so each per-sentence list is oldest→newest.
    for (es_idx, _sub), rec in sorted(
        by_key.items(), key=lambda kv: (str(kv[1].get("timestamp", "")), str(kv[0][1]))
    ):
        grouped.setdefault(es_idx, []).append(rec)
    return grouped


def _load_annotation_counts(project_dir: Path) -> dict[str, int]:
    """Return the number of sentences with >=1 active annotation, per chapter_id.

    Keyed by ``(es_idx, sub_id)`` so a per-sub_id tombstone doesn't clear a whole
    sentence; counts distinct ``es_idx`` that still carry a live annotation, which
    preserves the existing "sentences annotated" badge meaning under multiples.
    """
    annotations_path = project_dir / "annotations.jsonl"
    if not annotations_path.exists():
        return {}

    by_chapter: dict[str, dict[tuple, dict]] = {}
    for line in annotations_path.read_text(encoding="utf-8").strip().split("\n"):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        ch = record.get("chapter_id", "")
        if not ch:
            continue
        ch_map = by_chapter.setdefault(ch, {})
        key = (record.get("es_idx"), _ann_storage_sub_id(record.get("sub_id")))
        if record.get("removed"):
            ch_map.pop(key, None)
        elif record.get("es_idx") is not None:
            ch_map[key] = record
    return {ch: len({k[0] for k in m}) for ch, m in by_chapter.items()}


@app.route("/api/annotations/<project_id>/<chapter>")
def get_annotations(project_id, chapter):
    """Return annotations for a chapter."""
    if not _safe_id(project_id) or not _safe_id(chapter):
        return jsonify({"error": "Invalid ID"}), 400
    project_dir = _resolve_project_dir(project_id)
    if not project_dir.exists():
        return jsonify({"error": f"Project not found: {project_id}"}), 404

    annotations = _load_annotations(project_dir, chapter)
    flat = [_ann_for_wire(ann) for lst in annotations.values() for ann in lst]
    return jsonify({"annotations": flat})


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
        sub_id = data.get("sub_id")
        _allowed_ann_types = {"word_choice", "inconsistency", "footnote", "flag"}
        if ann_type not in _allowed_ann_types:
            ann_type = "flag"

        if not all([project_id, chapter_id, es_idx is not None]):
            return jsonify({"error": "Missing required fields"}), 400
        if not _safe_id(project_id) or not _safe_id(chapter_id):
            return jsonify({"error": "Invalid ID"}), 400

        project_dir = _resolve_project_dir(project_id)
        if not project_dir.exists():
            return jsonify({"error": f"Project not found: {project_id}"}), 404

        # Edit reuses the client's sub_id (safe charset for DOM data-attrs).
        # "legacy" addresses the pre-multi (es_idx, None) slot without minting.
        # No sub_id → create with a fresh "u…" id (clear of imported "gb…" ids).
        # Invalid sub_id → 400 (do not silently remint; that would fork a duplicate).
        if sub_id == _LEGACY_SUB_ID or sub_id is None or sub_id == "":
            storage_sub = None if sub_id == _LEGACY_SUB_ID else ("u" + secrets.token_hex(4))
        elif not _SUB_ID_RE.fullmatch(str(sub_id)):
            return jsonify({"error": "Invalid sub_id"}), 400
        else:
            storage_sub = str(sub_id)
        wire_sub = _LEGACY_SUB_ID if storage_sub is None else storage_sub

        record = {
            "project_id": project_id,
            "chapter_id": chapter_id,
            "es_idx": es_idx,
            "type": ann_type,
            "content": content or "",
            "timestamp": datetime.now().isoformat(),
        }
        if storage_sub is not None:
            record["sub_id"] = storage_sub

        annotations_path = project_dir / "annotations.jsonl"
        with open(annotations_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        return jsonify({"saved": True, "sub_id": wire_sub})

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
        sub_id = data.get("sub_id")

        if not all([project_id, chapter_id, es_idx is not None]):
            return jsonify({"error": "Missing required fields"}), 400
        if not _safe_id(project_id) or not _safe_id(chapter_id):
            return jsonify({"error": "Invalid ID"}), 400

        project_dir = _resolve_project_dir(project_id)
        if not project_dir.exists():
            return jsonify({"error": f"Project not found: {project_id}"}), 404

        # Tombstone the exact (es_idx, sub_id) key. Missing/"legacy" removes the
        # (es_idx, None) slot. Same charset constraint as POST.
        if sub_id is not None and sub_id != "" and sub_id != _LEGACY_SUB_ID:
            if not _SUB_ID_RE.fullmatch(str(sub_id)):
                return jsonify({"error": "Invalid sub_id"}), 400
            storage_sub = str(sub_id)
        else:
            storage_sub = None
        record = {
            "project_id": project_id,
            "chapter_id": chapter_id,
            "es_idx": es_idx,
            "removed": True,
            "timestamp": datetime.now().isoformat(),
        }
        if storage_sub is not None:
            record["sub_id"] = storage_sub

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
    project_dir = _resolve_project_dir(project_id)
    reviewed = _load_reviewed(project_dir)
    return jsonify({"reviewed": chapter in reviewed})


@app.route("/api/reviewed/<project_id>/<chapter>", methods=["POST"])
def mark_reviewed(project_id, chapter):
    """Mark a chapter as reviewed."""
    if not _safe_id(project_id) or not _safe_id(chapter):
        return jsonify({"error": "Invalid ID"}), 400
    project_dir = _resolve_project_dir(project_id)
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
    project_dir = _resolve_project_dir(project_id)
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

    project_dir = _resolve_project_dir(project_id)
    corrections_path = project_dir / "corrections.jsonl"
    if not corrections_path.exists():
        return jsonify({"error": "No corrections to apply"}), 404

    try:
        import time

        from scripts.apply_corrections import (
            apply_to_chunk,
            dedupe_corrections,
            group_by_chunk,
            load_corrections,
            realign_chapter,
            recombine_chapter,
        )
        from src.utils.file_io import load_chunk, save_chunk

        raw_corrections = load_corrections(project_dir)
        if not raw_corrections:
            return jsonify({"error": "No corrections found"}), 404

        # Collapse duplicates before grouping. Without this, two identical
        # corrections (e.g. an over-eager double Save) leave corrections.jsonl
        # pinned forever: the first pass mutates the chunk so the duplicate's
        # original_es no longer matches, total_applied never reaches
        # len(corrections), and the unlink at the bottom is skipped.
        corrections = dedupe_corrections(raw_corrections)

        by_chunk = group_by_chunk(corrections)

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
            updated_chunk, applied, _ = apply_to_chunk(chunk, chunk_corrections)

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

        # 4. Archive corrections — write the full pre-dedupe list so
        # corrections_applied.jsonl keeps a complete record of every Save.
        archive_path = project_dir / "corrections_applied.jsonl"
        applied_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(archive_path, "a", encoding="utf-8") as f:
            for corr in raw_corrections:
                f.write(json.dumps({**corr, "applied_at": applied_at}, ensure_ascii=False) + "\n")

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
    project_dir = _resolve_project_dir(project_id)

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
        "light_style_guide_content": None,
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
    # Persisted chunking parameters (Stage 3 form remembers what the user
    # last successfully chunked with). May be absent for new projects.
    # Backfill the Advanced ratios so the GUI always has them, even for
    # projects chunked before per-chapter tuning existed.
    cc = config.get("chunking_config")
    if cc:
        changed = not ("min_ratio" in cc and "max_ratio" in cc)
        cc.setdefault("min_ratio", 0.25)
        cc.setdefault("max_ratio", 1.5)
        if changed:
            config["chunking_config"] = cc
            try:
                _save_project_config(project_id, config)
            except Exception as e:
                app.logger.warning("Failed to persist ratio backfill for %s: %s", project_id, e)
    status["chunking_config"] = cc or None
    # Sparse per-chapter overrides ({chapter_id: {target_size, ...}}). Used
    # below to surface each chapter's chunk_target_override (null if none).
    chapter_chunking = config.get("chapter_chunking") or {}

    # Image status: count placeholders in source.txt vs files on disk so the
    # dashboard can warn when the Gutenberg ingester failed to fetch some images.
    status["images_expected"] = 0
    status["images_present"] = 0
    status["images_missing"] = []
    if status["has_source"]:
        try:
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location(
                "fetch_missing_images",
                Path(__file__).parent.parent / "scripts" / "fetch_missing_images.py",
            )
            _fmi = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_fmi)
            placeholders = _fmi.extract_placeholders(source_path)
            missing = _fmi.list_missing_images(project_dir)
            status["images_expected"] = len(placeholders)
            status["images_present"] = len(placeholders) - len(missing)
            status["images_missing"] = missing
        except Exception:
            pass

    # Style guide
    style_path = project_dir / "style.json"
    if style_path.exists():
        status["has_style_guide"] = True
        try:
            sg = load_style_guide(style_path)
            status["style_guide_content"] = sg.content
            status["light_style_guide_content"] = sg.light_content
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

    # Load active annotation counts in one pass. Uses the same dedup/tombstone
    # logic as _load_annotations so Review tab counts match the reader.
    annotation_counts = _load_annotation_counts(project_dir)

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

    manifest_by_id = _load_chapter_manifest_for_project(project_id)

    if chapters_dir.exists():
        for ch_file in sorted(chapters_dir.glob("chapter_*.txt")):
            ch_id = ch_file.stem
            # Prefer chunks/ source_text so post-Stage-6 projects report
            # English word counts and previews instead of the translated
            # text that combine wrote back over chapters/.
            text, _mtime, _source_kind = load_chapter_source_text(project_dir, ch_id)
            if not text:
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

            active_count = annotation_counts.get(ch_id, 0)

            entry = manifest_by_id.get(ch_id) or {}
            ch_override = chapter_chunking.get(ch_id) or {}
            status["chapters"].append({
                "id": ch_id,
                "name": ch_id.replace("_", " ").title(),
                "words": words,
                "preview": text[:200],
                "chunk_count": chunk_info["total"],
                "translated_count": chunk_info["translated"],
                "has_alignment": has_alignment,
                "alignment_confidence": alignment_confidence,
                "annotation_count": active_count,
                "reviewed": ch_id in reviewed_chapters,
                "kind": entry.get("kind", "chapter"),
                "label": entry.get("label"),
                "chunk_target_override": ch_override.get("target_size"),
            })

    status["chapter_count"] = len(status["chapters"])
    return status


# ── Reader: remove-text flow ──────────────────────────────────────────────────


def _find_substring_normalized(haystack: str, needle: str) -> Optional[tuple[int, int]]:
    """Locate ``needle`` inside ``haystack`` ignoring whitespace differences.

    Returns ``(start, end)`` in original ``haystack`` indices, or ``None``.
    """
    idx = haystack.find(needle)
    if idx >= 0:
        return idx, idx + len(needle)
    norm_h, idx_map = _norm_whitespace_with_map(haystack)
    norm_n_full, _ = _norm_whitespace_with_map(needle)
    norm_n = norm_n_full.strip()
    if not norm_n:
        return None
    n_idx = norm_h.find(norm_n)
    if n_idx < 0:
        return None
    n_end = n_idx + len(norm_n)
    if n_idx >= len(idx_map) or n_end - 1 >= len(idx_map):
        return None
    return idx_map[n_idx], idx_map[n_end - 1] + 1


@app.route(
    "/api/removal-context/<project_id>/<chapter_id>/<int:es_idx>",
    methods=["GET"],
)
def removal_context(project_id, chapter_id, es_idx):
    """Return the chunk text + suggested highlight ranges to seed the
    reader's removal modal.
    """
    if not _safe_id(project_id) or not _safe_id(chapter_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _resolve_project_dir(project_id)
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404

    align_path = project_dir / "alignments" / f"{chapter_id}.json"
    if not align_path.exists():
        return jsonify({"error": "Alignment not found"}), 404

    try:
        with open(align_path, encoding="utf-8") as f:
            align_data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return jsonify({"error": f"Failed to read alignment: {e}"}), 500

    record = next(
        (a for a in align_data.get("alignments", [])
         if a.get("es_idx") == es_idx),
        None,
    )
    if record is None:
        return jsonify({"error": f"No alignment record for es_idx={es_idx}"}), 404

    chunk_id = record.get("chunk_id")
    if not chunk_id or not _safe_id(chunk_id):
        return jsonify({"error": "Alignment record missing chunk_id"}), 500

    chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
    if not chunk_path.exists():
        return jsonify({"error": f"Chunk not found: {chunk_id}"}), 404

    try:
        chunk = load_chunk(chunk_path)
    except Exception as e:
        return jsonify({"error": f"Failed to load chunk: {e}"}), 500

    es_full = chunk.translated_text or ""
    en_full = chunk.source_text or ""

    es_match = _find_substring_normalized(es_full, record.get("es") or "")
    en_match = _find_substring_normalized(en_full, record.get("en") or "")

    try:
        chunk_mtime = chunk_path.stat().st_mtime
    except OSError:
        chunk_mtime = 0.0
    try:
        align_mtime = align_path.stat().st_mtime
    except OSError:
        align_mtime = 0.0

    return jsonify({
        "chunk_id": chunk_id,
        "chunk_mtime": chunk_mtime,
        "alignment_mtime": align_mtime,
        "es_full": es_full,
        "en_full": en_full,
        "es_suggested": (
            {"start": es_match[0], "end": es_match[1]} if es_match else None
        ),
        "en_suggested": (
            {"start": en_match[0], "end": en_match[1]} if en_match else None
        ),
        "image_token_ranges_es": _image_token_ranges(es_full),
        "image_token_ranges_en": _image_token_ranges(en_full),
    })


@app.route("/api/remove-text", methods=["POST"])
def remove_text():
    """Remove a substring from both source_text and translated_text of a
    chunk, then recombine + realign the chapter.
    """
    data = request.json or {}
    project_id = (data.get("project_id") or "").strip()
    chapter_id = (data.get("chapter_id") or "").strip()
    chunk_id = (data.get("chunk_id") or "").strip()
    es_remove = data.get("es_remove") or ""
    en_remove = data.get("en_remove") or ""
    es_remove_start = data.get("es_remove_start")
    en_remove_start = data.get("en_remove_start")
    expected_chunk_mtime = data.get("expected_chunk_mtime")

    def _coerce_hint(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    es_hint = _coerce_hint(es_remove_start)
    en_hint = _coerce_hint(en_remove_start)

    if not (
        _safe_id(project_id) and _safe_id(chapter_id) and _safe_id(chunk_id)
    ):
        return jsonify({"error": "Bad request"}), 400
    if not es_remove and not en_remove:
        return jsonify({"error": "No text provided to remove"}), 400

    if _chapter_id_from_chunk_id(chunk_id) != chapter_id:
        return jsonify({"error": "chunk_id does not belong to chapter_id"}), 400

    project_dir = _resolve_project_dir(project_id)
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
    if expected_chunk_mtime is not None:
        try:
            if abs(float(expected_chunk_mtime) - current_mtime) > 1e-6:
                return jsonify({
                    "error": (
                        "Chunk was modified by another process. "
                        "Reload and try again."
                    ),
                }), 409
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid expected_chunk_mtime"}), 400

    try:
        chunk = load_chunk(chunk_path)
    except Exception as e:
        return jsonify({"error": f"Failed to load chunk: {e}"}), 500

    old_es = chunk.translated_text or ""
    old_en = chunk.source_text or ""

    # Apply ES removal
    new_es = old_es
    es_bounds: Optional[tuple[int, int]] = None
    if es_remove:
        new_es, es_bounds, err = _remove_substring(old_es, es_remove, es_hint)
        if err:
            return jsonify({"error": f"Spanish: {err}"}), 400
        guard = _check_no_image_token_overlap(old_es, *es_bounds)
        if guard:
            return jsonify({"error": f"Spanish: {guard}"}), 400
        if not (new_es or "").strip():
            return jsonify({
                "error": "Removing this would empty the chunk's translation.",
            }), 400

    # Apply EN removal
    new_en = old_en
    en_bounds: Optional[tuple[int, int]] = None
    if en_remove:
        new_en, en_bounds, err = _remove_substring(old_en, en_remove, en_hint)
        if err:
            return jsonify({"error": f"English: {err}"}), 400
        guard = _check_no_image_token_overlap(old_en, *en_bounds)
        if guard:
            return jsonify({"error": f"English: {guard}"}), 400
        if not (new_en or "").strip():
            return jsonify({
                "error": "Removing this would empty the chunk's source.",
            }), 400

    # Build the edits list. Start with the seed chunk.
    edits: list[dict] = [{
        "chunk_id": chunk_id,
        "chunk_path": chunk_path,
        "chunk": chunk,
        "new_translated_text": new_es if es_remove else None,
        "new_source_text": new_en if en_remove else None,
    }]

    # Overlap-region duplication: if the removed text sits inside this
    # chunk's overlap region, the same string lives in the adjacent
    # chunk's overlap on the other side. Apply a first-occurrence
    # replace there so the pair stays coherent for re-translation.
    chunks_dir = project_dir / "chunks"
    sibling_paths = sorted(chunks_dir.glob(f"{chapter_id}_chunk_*.json"))
    sibling_ids = [p.stem for p in sibling_paths]
    try:
        my_idx = sibling_ids.index(chunk_id)
    except ValueError:
        my_idx = -1

    def _maybe_propagate(neighbor_idx: int) -> Optional[dict]:
        if not (0 <= neighbor_idx < len(sibling_paths)):
            return None
        n_path = sibling_paths[neighbor_idx]
        n_chunk = load_chunk(n_path)
        n_es = n_chunk.translated_text or ""
        n_en = n_chunk.source_text or ""
        new_n_es: Optional[str] = None
        new_n_en: Optional[str] = None
        if es_remove and es_remove in n_es:
            new_n_es = n_es.replace(es_remove, "", 1)
        if en_remove and en_remove in n_en:
            new_n_en = n_en.replace(en_remove, "", 1)
        if new_n_es is None and new_n_en is None:
            return None
        return {
            "chunk_id": n_chunk.id,
            "chunk_path": n_path,
            "chunk": n_chunk,
            "new_translated_text": new_n_es,
            "new_source_text": new_n_en,
        }

    if my_idx >= 0:
        # Look at both neighbors; the propagation only fires when an
        # exact-match copy of the removed string is present.
        for neighbor in (my_idx - 1, my_idx + 1):
            extra = _maybe_propagate(neighbor)
            if extra is not None:
                edits.append(extra)

    try:
        result = _apply_chunk_edits(
            project_dir, project_id, chapter_id, edits,
        )
    except Exception as e:
        app.logger.exception("remove-text pipeline failed")
        return jsonify({"error": str(e)}), 500

    # Audit log
    try:
        removals_path = project_dir / "removals.jsonl"
        with open(removals_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "project_id": project_id,
                "chapter_id": chapter_id,
                "chunk_id": chunk_id,
                "es_remove": es_remove,
                "en_remove": en_remove,
                "propagated_chunks": [
                    e["chunk_id"] for e in edits if e["chunk_id"] != chunk_id
                ],
                "timestamp": datetime.now().isoformat(),
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass

    align_path = project_dir / "alignments" / f"{chapter_id}.json"
    try:
        new_align_mtime = align_path.stat().st_mtime
    except OSError:
        new_align_mtime = 0.0

    return jsonify({
        "ok": True,
        "chunk_mtime": result["mtimes"].get(chunk_id, 0.0),
        "alignment_mtime": new_align_mtime,
        "orphaned_annotations": result["orphaned_annotations"],
        "corrections_purged": result["corrections_purged"],
        "edited_chunks": [e["chunk_id"] for e in edits],
    })


# ============================================================================
# Reader sentence retranslate (Phase 2)
# ============================================================================

@app.route("/api/llm/models")
def llm_models():
    """Return the model picker payload for the reader's retranslate UI."""
    from src.api_translator import load_llm_config
    config = load_llm_config()
    default_model = config.get("default_model")
    models: list[dict] = []
    for provider in config.get("providers", []):
        for m in provider.get("models", []):
            models.append({
                "id": m["id"],
                "name": m.get("name", m["id"]),
                "provider": provider["id"],
                "pricing": m.get("pricing", {}),
                "is_default": m["id"] == default_model,
            })
    return jsonify({"models": models, "default_model": default_model})


def _validate_chunk_request(project_id: str, chapter_id: str, chunk_id: str):
    """Shared validation for sentence endpoints. Returns (project_dir, chunk_path) or (response, status)."""
    if not (_safe_id(project_id) and _safe_id(chapter_id) and _safe_id(chunk_id)):
        return jsonify({"error": "Bad request"}), 400
    if _chapter_id_from_chunk_id(chunk_id) != chapter_id:
        return jsonify({"error": "chunk_id does not belong to chapter_id"}), 400
    project_dir = _resolve_project_dir(project_id)
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404
    chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
    if not chunk_path.exists():
        return jsonify({"error": "Chunk not found"}), 404
    return project_dir, chunk_path


# Size caps for retranslate text inputs (defensive against accidental megabyte
# pastes; the LLM's own token limit is the implicit upper bound, but rejecting
# early avoids wasted API calls and oversized chunk JSONs).
_RETRANSLATE_SOURCE_MAX = 8 * 1024
_RETRANSLATE_CONTEXT_MAX = 16 * 1024
_REPLACE_TRANSLATION_MAX = 32 * 1024


def _check_chunk_mtime(chunk_path: Path, expected_mtime) -> Optional[tuple]:
    """Return a (response, status) tuple if the mtime mismatches, else None."""
    if expected_mtime is None:
        return None
    try:
        current = chunk_path.stat().st_mtime
    except OSError as e:
        return jsonify({"error": f"Cannot stat chunk file: {e}"}), 500
    try:
        expected_float = float(expected_mtime)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid expected_chunk_mtime"}), 400
    if not math.isfinite(expected_float):
        return jsonify({"error": "Invalid expected_chunk_mtime"}), 400
    if abs(expected_float - current) > 1e-6:
        return jsonify({
            "error": "Chunk was modified by another process. Reload and try again.",
        }), 409
    return None


@app.route("/api/sentence/retranslate", methods=["POST"])
def sentence_retranslate():
    """Generate a fresh translation for a user-confirmed source span."""
    from src.retranslator import retranslate_sentence, RetranslationError

    data = request.json or {}
    project_id = (data.get("project_id") or "").strip()
    chapter_id = (data.get("chapter_id") or "").strip()
    chunk_id = (data.get("chunk_id") or "").strip()
    source_text = (data.get("source_text") or "").strip()
    model = (data.get("model") or "").strip() or None
    provider = (data.get("provider") or "").strip() or None
    context_text = (data.get("context_text") or "").strip() or None
    expected_chunk_mtime = data.get("expected_chunk_mtime")

    if not source_text:
        return jsonify({"error": "source_text is required"}), 400
    if len(source_text) > _RETRANSLATE_SOURCE_MAX:
        return jsonify({
            "error": f"source_text exceeds {_RETRANSLATE_SOURCE_MAX} chars",
        }), 413
    if context_text is not None and len(context_text) > _RETRANSLATE_CONTEXT_MAX:
        return jsonify({
            "error": f"context_text exceeds {_RETRANSLATE_CONTEXT_MAX} chars",
        }), 413

    validation = _validate_chunk_request(project_id, chapter_id, chunk_id)
    if isinstance(validation, tuple) and len(validation) == 2 and not isinstance(validation[0], Path):
        return validation
    project_dir, chunk_path = validation

    mtime_err = _check_chunk_mtime(chunk_path, expected_chunk_mtime)
    if mtime_err is not None:
        return mtime_err

    glossary = None
    glossary_path = project_dir / "glossary.json"
    if glossary_path.exists():
        try:
            glossary = load_glossary(glossary_path)
        except Exception as e:
            app.logger.warning("Could not load glossary at %s: %s", glossary_path, e)

    style_path = project_dir / "style.json"
    style_arg = style_path if style_path.exists() else None

    try:
        result = retranslate_sentence(
            source_text,
            style_json_path=style_arg,
            glossary=glossary,
            model=model,
            provider=provider,
            context_text=context_text,
        )
    except RetranslationError as e:
        return jsonify({"error": f"Retranslation failed: {e}"}), 502
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        app.logger.exception("retranslate failed")
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "ok": True,
        "new_translation": result.new_translation,
        "model": result.model,
        "provider": result.provider,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "cost_usd": result.cost_usd,
    })


@app.route("/api/sentence/replace", methods=["POST"])
def sentence_replace():
    """Replace a sentence (or N:1 span) in a chunk and re-align the chapter."""
    data = request.json or {}
    project_id = (data.get("project_id") or "").strip()
    chapter_id = (data.get("chapter_id") or "").strip()
    chunk_id = (data.get("chunk_id") or "").strip()
    current_translation = data.get("current_translation") or ""
    new_translation = data.get("new_translation") or ""
    expected_chunk_mtime = data.get("expected_chunk_mtime")

    if not current_translation:
        return jsonify({"error": "current_translation is required"}), 400
    if not new_translation.strip():
        return jsonify({"error": "new_translation must be non-empty"}), 400
    if len(current_translation) > _REPLACE_TRANSLATION_MAX:
        return jsonify({
            "error": f"current_translation exceeds {_REPLACE_TRANSLATION_MAX} chars",
        }), 413
    if len(new_translation) > _REPLACE_TRANSLATION_MAX:
        return jsonify({
            "error": f"new_translation exceeds {_REPLACE_TRANSLATION_MAX} chars",
        }), 413

    validation = _validate_chunk_request(project_id, chapter_id, chunk_id)
    if isinstance(validation, tuple) and len(validation) == 2 and not isinstance(validation[0], Path):
        return validation
    project_dir, chunk_path = validation

    mtime_err = _check_chunk_mtime(chunk_path, expected_chunk_mtime)
    if mtime_err is not None:
        return mtime_err

    try:
        chunk = load_chunk(chunk_path)
    except Exception as e:
        return jsonify({"error": f"Failed to load chunk: {e}"}), 500

    old_es = chunk.translated_text or ""

    # Resolve the span to replace. Three tiers, in order:
    #   1. Client offsets that slice back to current_translation — exact span.
    #      This is the always-correct branch when the client clicks an
    #      alignment row without editing the "current" textbox.
    #   2. Client offsets that don't slice cleanly (user edited current, or
    #      offsets are stale) — anchored find() near the hint, then plain
    #      find() if that misses.
    #   3. No offsets at all (old clients) — legacy find() from 0.
    # Tier 1 is the fix for silent corruption when current_translation
    # appears more than once in the chunk (e.g. a body sentence whose text
    # also lives inside an [IMAGE:...] caption).
    chunk_offset_start = data.get("chunk_offset_start")
    chunk_offset_end = data.get("chunk_offset_end")
    has_offset_hint = (
        isinstance(chunk_offset_start, int)
        and not isinstance(chunk_offset_start, bool)
        and 0 <= chunk_offset_start <= len(old_es)
    )

    start: Optional[int] = None
    end: Optional[int] = None

    if (
        has_offset_hint
        and isinstance(chunk_offset_end, int)
        and not isinstance(chunk_offset_end, bool)
        and chunk_offset_start < chunk_offset_end <= len(old_es)
        and chunk_offset_end - chunk_offset_start == len(current_translation)
        and old_es[chunk_offset_start:chunk_offset_end] == current_translation
    ):
        start, end = chunk_offset_start, chunk_offset_end

    if start is None and has_offset_hint:
        idx = old_es.find(current_translation, chunk_offset_start)
        if idx != -1:
            start, end = idx, idx + len(current_translation)

    if start is None:
        idx = old_es.find(current_translation)
        if idx == -1:
            return jsonify({
                "error": (
                    "Cannot locate the original sentence in the chunk. "
                    "Reload the reader and try again."
                ),
            }), 422
        start, end = idx, idx + len(current_translation)

    new_es = old_es[:start] + new_translation + old_es[end:]

    if not new_es.strip():
        return jsonify({"error": "Replacement would empty the chunk's translation."}), 400

    edits = [{
        "chunk_id": chunk_id,
        "chunk_path": chunk_path,
        "chunk": chunk,
        "new_translated_text": new_es,
        "new_source_text": None,
    }]

    try:
        result = _apply_chunk_edits(project_dir, project_id, chapter_id, edits)
    except Exception as e:
        app.logger.exception("sentence replace pipeline failed")
        return jsonify({"error": str(e)}), 500

    # Audit log
    try:
        log_path = project_dir / "retranslations.jsonl"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "project_id": project_id,
                "chapter_id": chapter_id,
                "chunk_id": chunk_id,
                "es_idx": data.get("es_idx"),
                "chunk_offset_start": start,
                "chunk_offset_end": end,
                "current_translation": current_translation,
                "new_translation": new_translation,
                "timestamp": datetime.now().isoformat(),
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass

    align_path = project_dir / "alignments" / f"{chapter_id}.json"
    try:
        new_align_mtime = align_path.stat().st_mtime
    except OSError:
        new_align_mtime = 0.0

    return jsonify({
        "ok": True,
        "chunk_mtime": result["mtimes"].get(chunk_id, 0.0),
        "alignment_mtime": new_align_mtime,
        "orphaned_annotations": result["orphaned_annotations"],
        "corrections_purged": result["corrections_purged"],
    })


@app.route("/project/<project_id>")
def dashboard_page(project_id):
    """Render the unified project dashboard."""
    if not _safe_id(project_id):
        return "Bad request", 400
    project_dir = _resolve_project_dir(project_id)
    if not project_dir.exists():
        return "Project not found", 404

    from src.style_guide_wizard import get_active_questions
    fixed_questions, conditional_questions, manifest = get_active_questions(project_dir)

    # Inject conditional question evidence into the UI so users can see why a
    # question is being asked.
    hydrated_conditional = []
    for q in conditional_questions:
        q2 = dict(q)
        q2["_is_conditional"] = True
        requires = q2.get("requires") or {}
        feat = requires.get("feature")
        if feat:
            r = manifest.get(feat)
            if r.evidence:
                q2["_detected_hint"] = r.evidence[0]
        hydrated_conditional.append(q2)

    all_questions = list(fixed_questions) + hydrated_conditional
    t = _reader_strings()

    return render_template(
        "dashboard.html",
        project_id=project_id,
        project_title=_project_title(project_id),
        project_spanish_title=_load_project_config(project_id).get("spanish_title", ""),
        fixed_questions=all_questions,
        t=t,
        lang=_get_ui_lang(),
    )


@app.route("/api/project/<project_id>/status")
def project_status(project_id):
    """Return full project status as JSON."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)
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
    project_dir = _resolve_project_dir(project_id)
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
    project_dir = _resolve_project_dir(project_id)
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

        project_dir = _resolve_project_dir(project_id)
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


@app.route("/api/project/<project_id>/fetch-missing-images", methods=["POST"])
def project_fetch_missing_images(project_id):
    """Re-download any images missing from a Gutenberg-ingested project."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _resolve_project_dir(project_id)
    if not (project_dir / "source.txt").exists():
        return jsonify({"error": "Project has no source.txt"}), 400

    data = request.json or {}
    base_url_override = (data.get("base_url") or "").strip() or None
    force = bool(data.get("force", False))

    try:
        import importlib.util
        _spec = importlib.util.spec_from_file_location(
            "fetch_missing_images",
            Path(__file__).parent.parent / "scripts" / "fetch_missing_images.py",
        )
        _fmi = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_fmi)

        result = _fmi.fetch_missing_images(
            project_dir, base_url=base_url_override, force=force
        )
        return jsonify({
            "ok": True,
            "downloaded": result["downloaded"],
            "skipped_existing": result["skipped_existing"],
            "failed": [{"url": u, "error": e} for (u, e) in result["failed"]],
            "placeholders": result["placeholders"],
        })
    except (FileNotFoundError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


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
    project_dir = _resolve_project_dir(project_id)
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
            front_matter_titles=data.get("front_matter_titles") or [],
            back_matter_titles=data.get("back_matter_titles") or [],
            auto_detect_front_matter=data.get("auto_detect_front_matter", True),
            auto_detect_back_matter=data.get("auto_detect_back_matter", True),
        )
        result = []
        for ch in chapters:
            if ch.kind == "chapter":
                display_name = ch.chapter_title or f"Chapter {ch.number or ch.position_index}"
            else:
                display_name = ch.label or ch.chapter_title or ch.kind
            result.append({
                "name": display_name,
                "words": len(ch.content.split()),
                "preview": ch.content[:200],
                "kind": ch.kind,
                "label": ch.label,
                "number": ch.number,
            })
        return jsonify({"chapters": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/project/<project_id>/split", methods=["POST"])
def project_split(project_id):
    """Execute chapter split and write files."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)
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
            front_matter_titles=data.get("front_matter_titles") or [],
            back_matter_titles=data.get("back_matter_titles") or [],
            auto_detect_front_matter=data.get("auto_detect_front_matter", True),
            auto_detect_back_matter=data.get("auto_detect_back_matter", True),
        )
        chapters_dir = project_dir / "chapters"
        save_chapters_to_files(chapters, chapters_dir)
        return jsonify({"ok": True, "chapter_count": len(chapters)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _derive_chunk_bounds(target: int, min_ratio: float, max_ratio: float) -> tuple:
    """Derive (min_chunk_size, max_chunk_size) from a target and ratios.

    Thin wrapper over :func:`src.models.derive_chunk_bounds` (the single source
    of truth shared with ``ChunkingConfig.from_target`` and the CLI chunk stage)
    so the two paths can never drift.
    """
    from src.models import derive_chunk_bounds

    return derive_chunk_bounds(target, min_ratio, max_ratio)


def _resolve_chunking(default_cfg: dict, override) -> tuple:
    """Resolve the effective ChunkingConfig (+ optional per-paragraph weights)
    for one chapter from the global default and an optional per-chapter override.

    The override is the chapter's entry from ``chapter_chunking`` (or the
    incoming request). Phase 1 honors only a scalar ``target_size``; min/max are
    derived from the default's ratios, overlap is inherited from the default.

    Returns ``(ChunkingConfig, para_weights | None)``. Phase 1 always yields
    ``para_weights = None`` (uniform sizing). Phase 2 passes the override's
    positional ``weights`` vector straight through here.
    """
    from src.models import ChunkingConfig

    default_cfg = default_cfg or {}
    override = override if isinstance(override, dict) else {}

    target = override.get("target_size")
    if target is None:
        target = default_cfg.get("target_size", 2000)
    target = int(target)

    # from_target validates the ratios and derives the bounds (single source of
    # truth shared with the CLI chunk stage).
    cfg = ChunkingConfig.from_target(
        target,
        min_ratio=float(default_cfg.get("min_ratio", 0.25)),
        max_ratio=float(default_cfg.get("max_ratio", 1.5)),
        overlap_paragraphs=int(default_cfg.get("overlap_paragraphs", 0) or 0),
        min_overlap_words=int(default_cfg.get("min_overlap_words", 0) or 0),
    )

    # Phase 1: scalar override only ⇒ uniform weights (None == today's sizing).
    # Phase 2 will pass override.get("weights") through as para_weights, with a
    # length-mismatch guard in the caller to fall back to uniform.
    para_weights = None
    return cfg, para_weights


def _persist_chunking_config(project_id: str, config) -> None:
    """Save the global default chunking parameters used in a successful chunk
    run to projects/<id>/project.json so the Stage 3 form pre-fills with the
    user's last choices on subsequent visits. Includes the Advanced min/max
    ratios that future per-chapter resolution derives bounds from."""
    try:
        proj_cfg = _load_project_config(project_id)
        proj_cfg["chunking_config"] = {
            "target_size": config.target_size,
            "min_chunk_size": config.min_chunk_size,
            "max_chunk_size": config.max_chunk_size,
            "min_ratio": config.min_ratio,
            "max_ratio": config.max_ratio,
            "overlap_paragraphs": config.overlap_paragraphs,
            "min_overlap_words": config.min_overlap_words,
        }
        _save_project_config(project_id, proj_cfg)
    except Exception as e:
        # Persistence is best-effort; never fail a chunk run because we
        # couldn't update project.json.
        app.logger.warning("Failed to persist chunking config for %s: %s", project_id, e)


def _persist_chapter_chunking(project_id: str, overrides: dict) -> None:
    """Upsert/clear per-chapter chunking overrides in project.json.

    ``overrides`` maps ``chapter_id -> {"target_size": int | None}``. A None
    (or absent) target_size deletes that chapter's entry, reverting it to the
    global default. The stored ``chapter_chunking`` map stays sparse — only
    tuned chapters appear. This is the shared contract future programmatic
    paragraph-scorers write to (extending each entry with a positional
    ``weights`` vector); existing keys on an entry are preserved on update.
    Best-effort: never fails a chunk run."""
    try:
        proj_cfg = _load_project_config(project_id)
        chapter_map = proj_cfg.get("chapter_chunking")
        if not isinstance(chapter_map, dict):
            chapter_map = {}

        for chapter_id, ov in (overrides or {}).items():
            target = ov.get("target_size") if isinstance(ov, dict) else None
            if target is None:
                chapter_map.pop(chapter_id, None)
            else:
                entry = chapter_map.get(chapter_id)
                if not isinstance(entry, dict):
                    entry = {}
                entry["target_size"] = int(target)
                chapter_map[chapter_id] = entry

        if chapter_map:
            proj_cfg["chapter_chunking"] = chapter_map
        else:
            proj_cfg.pop("chapter_chunking", None)
        _save_project_config(project_id, proj_cfg)
    except Exception as e:
        app.logger.warning("Failed to persist chapter chunking for %s: %s", project_id, e)


@app.route("/api/project/<project_id>/chunk-all", methods=["POST"])
def project_chunk_all(project_id):
    """Chunk all (or selected) chapters."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)
    chapters_dir = project_dir / "chapters"
    if not chapters_dir.exists():
        return jsonify({"error": "No chapters directory"}), 404

    try:
        from src.chunker import chunk_chapter
        from src.utils.file_io import save_chunk

        data = request.json or {}
        # New shape: {default: {...}, chapters: {"<id>": {target_size}}}.
        # Back-compat: a flat {target_size, ...} payload is the global default
        # with no per-chapter overrides.
        if "default" in data or "chapters" in data:
            default_cfg = data.get("default")
            if not isinstance(default_cfg, dict):
                default_cfg = {}
            chapter_overrides = data.get("chapters")
            if not isinstance(chapter_overrides, dict):
                chapter_overrides = {}
        else:
            default_cfg = data
            chapter_overrides = {}

        # The global default config (no override) is what we persist for the
        # Stage 3 form to pre-fill from next time.
        default_config, _ = _resolve_chunking(default_cfg, None)

        chunks_dir = project_dir / "chunks"
        chunks_dir.mkdir(exist_ok=True)

        total_chunks = 0
        for ch_file in sorted(chapters_dir.glob("chapter_*.txt")):
            chapter_id = ch_file.stem
            cfg, para_weights = _resolve_chunking(
                default_cfg, chapter_overrides.get(chapter_id)
            )
            # Prefer chunks/ source_text so a re-chunk on a Stage-6'd project
            # preserves English in the new chunks instead of writing the
            # translated text from chapters/ back into chunk.source_text.
            text, _mtime, _kind = load_chapter_source_text(project_dir, chapter_id)
            if not text:
                text = ch_file.read_text(encoding="utf-8")
            chunks = chunk_chapter(text, cfg, chapter_id, para_weights=para_weights)
            for chunk in chunks:
                save_chunk(chunk, chunks_dir / f"{chunk.id}.json")
                total_chunks += 1

        _persist_chunking_config(project_id, default_config)
        _persist_chapter_chunking(project_id, chapter_overrides)
        return jsonify({"ok": True, "total_chunks": total_chunks})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/project/<project_id>/difficulty", methods=["GET"])
def project_difficulty(project_id):
    """Translation-difficulty scores for the book and each chapter.

    Read-only. Returns the combined ``difficulty`` plus the individual
    sub-scores and raw stats (so the dashboard can show a breakdown tooltip) and
    a ``suggested_target_size`` the UI can fill into the chunk target inputs.
    Results are cached to ``difficulty.json``; pass ``?force=1`` to re-score.
    """
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404

    force = (request.args.get("force") or "").lower() in ("1", "true", "yes")
    try:
        manifest = score_book(project_dir, force=force)
        return jsonify({
            "ok": True,
            "book": manifest.book.to_dict(),
            "chapters": [
                {"chapter_id": cd.chapter_id, **cd.metrics.to_dict()}
                for cd in manifest.chapters
            ],
        })
    except Exception:
        app.logger.exception("Difficulty scoring failed for %s", project_id)
        return jsonify({"error": "Difficulty scoring failed"}), 500


def _reconstruct_chapter_source_from_chunks(chunks) -> str:
    """Reconstruct original chapter source text from existing chunks.

    Each chunk's `source_text` preserves the original (untranslated) chapter
    content for that span, including a leading overlap with the previous
    chunk. Strip that overlap and rejoin so we recover the original chapter
    source even when chapters/{id}.txt has been overwritten by Combine with
    the translated text.
    """
    if not chunks:
        return ""
    sorted_chunks = sorted(chunks, key=lambda c: c.position)
    parts = []
    for i, c in enumerate(sorted_chunks):
        text = c.source_text or ""
        if i == 0:
            parts.append(text)
            continue
        ov = (c.metadata.overlap_start if c.metadata else 0) or 0
        # chunk_text = overlap_text + "\n\n" + body_text, so body starts at ov+2
        if ov > 0 and len(text) > ov + 2 and text[ov:ov + 2] == "\n\n":
            parts.append(text[ov + 2:])
        else:
            parts.append(text)
    return "\n\n".join(parts)


@app.route("/api/project/<project_id>/chapters/<chapter_id>/rechunk", methods=["POST"])
def project_chapter_rechunk(project_id, chapter_id):
    """Rechunk a single chapter, replacing its existing chunks.

    Destructive: deletes all existing chunk files for this chapter before
    writing new ones. The client is responsible for warning the user when
    the chapter has translated chunks that would be lost.

    Source-of-truth note: chapters/{id}.txt is overwritten by Combine with the
    translated text, so we cannot rely on it as the source for re-chunking.
    Instead we reconstruct the original source from the existing chunks'
    `source_text` fields (which always hold the original-language text), and
    only fall back to chapters/{id}.txt when no chunks exist yet.
    """
    if not _safe_id(project_id) or not _safe_id(chapter_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _resolve_project_dir(project_id)
    chapter_path = project_dir / "chapters" / f"{chapter_id}.txt"

    try:
        from src.chunker import chunk_chapter
        from src.utils.file_io import save_chunk

        data = request.json or {}
        # Per-chapter scalar target. Blank/None ⇒ revert this chapter to the
        # global default (and clear its chapter_chunking entry below). The
        # default's ratios + overlap come from the persisted global config.
        raw_target = data.get("target_size")
        default_cfg = _load_project_config(project_id).get("chunking_config") or {}
        override = {"target_size": raw_target} if raw_target is not None else None
        config, para_weights = _resolve_chunking(default_cfg, override)

        chunks_dir = project_dir / "chunks"
        chunks_dir.mkdir(exist_ok=True)

        # Reconstruct source from existing chunks BEFORE deleting them, so we
        # don't accidentally rechunk from chapters/{id}.txt (which Combine has
        # overwritten with translated text).
        existing_chunk_paths = sorted(chunks_dir.glob(f"{chapter_id}_chunk_*.json"))
        existing_chunks = []
        for cf in existing_chunk_paths:
            try:
                existing_chunks.append(load_chunk(cf))
            except Exception:
                pass

        if existing_chunks:
            text = _reconstruct_chapter_source_from_chunks(existing_chunks)
        else:
            if not chapter_path.exists():
                return jsonify({"error": "Chapter not found"}), 404
            text = chapter_path.read_text(encoding="utf-8")

        if not text or not text.strip():
            return jsonify({"error": "Chapter source text is empty"}), 400

        # Delete existing chunk files for this chapter so we don't leave
        # stale higher-numbered chunks behind if the new chunking produces
        # fewer chunks than before.
        for old in existing_chunk_paths:
            try:
                old.unlink()
            except OSError:
                pass

        chunks = chunk_chapter(text, config, chapter_id, para_weights=para_weights)
        for chunk in chunks:
            save_chunk(chunk, chunks_dir / f"{chunk.id}.json")

        # Upsert (or clear when target was blank) this chapter's per-chapter
        # override. We don't touch the global chunking_config from a single-
        # chapter rechunk — Advanced defaults are owned by "Chunk All".
        _persist_chapter_chunking(project_id, {chapter_id: {"target_size": raw_target}})
        return jsonify({"ok": True, "chunk_count": len(chunks)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/project/<project_id>/chapters/<chapter_id>/chunks")
def project_chapter_chunks(project_id, chapter_id):
    """List chunks for a chapter with status."""
    if not _safe_id(project_id) or not _safe_id(chapter_id):
        return jsonify({"error": "Bad request"}), 400

    chunks_dir = _resolve_project_dir(project_id) / "chunks"
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

    project_dir = _resolve_project_dir(project_id)
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
            "image_placeholder_instructions": image_placeholder_instruction(chunk.source_text),
            "dialogue_instructions": dialogue_instruction(chunk.source_text, "Spanish"),
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

    project_dir = _resolve_project_dir(project_id)
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


def _load_harness_translate_flags(project_dir: Path) -> dict:
    """Read per-book always_include_* defaults from ``.harness/config.json``.

    Both are tri-state and returned raw: ``None`` means "auto" (derive from the
    selected chunks), so callers must resolve it rather than coercing with
    ``bool()`` — that would report a book running auto-on as off.
    """
    try:
        from src.harness import state as harness_state
        cfg = harness_state.load_config(project_dir)
    except Exception:
        cfg = {}
    return {
        "always_include_dialogue": cfg.get("always_include_dialogue"),
        "always_include_image_instructions": cfg.get("always_include_image_instructions"),
    }


@app.route("/api/project/<project_id>/translate/cost-estimate", methods=["POST"])
def project_translate_cost_estimate(project_id):
    """Estimate translation cost for selected chapters."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _resolve_project_dir(project_id)
    data = request.json or {}
    chapter_ids = data.get("chapter_ids", [])
    provider = data.get("provider", "anthropic")
    model = data.get("model", "")

    include_translated = data.get("include_translated", False)

    try:
        from src.api_translator import estimate_cost, summarize_chunk_features

        chunks_dir = project_dir / "chunks"
        chunks = []
        already_translated_count = 0
        for ch_id in chapter_ids:
            for cf in sorted(chunks_dir.glob(f"{ch_id}_chunk_*.json")):
                chunk = load_chunk(cf)
                has_translation = bool(chunk.translated_text and chunk.translated_text.strip())
                if has_translation:
                    already_translated_count += 1
                if include_translated or not has_translation:
                    chunks.append(chunk)

        if not chunks:
            return jsonify({
                "chunk_count": 0,
                "estimated_cost": 0,
                "already_translated_count": already_translated_count,
                "total_chunks": 0,
                "dialogue_chunk_count": 0,
                "image_chunk_count": 0,
                "suggested_always_dialogue": False,
                "suggested_always_images": False,
            })

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

        flags = _load_harness_translate_flags(project_dir)
        always_dialogue = flags["always_include_dialogue"]
        always_images = flags["always_include_image_instructions"]

        from src.api_translator import DEFAULT_MODEL
        result = estimate_cost(
            chunks,
            provider=provider,
            model=model or DEFAULT_MODEL,
            glossary=glossary,
            style_guide=style_guide,
            always_include_dialogue=always_dialogue,
            always_include_image_instructions=always_images,
        )
        features = summarize_chunk_features(chunks)
        image_count = features["images"]
        suggested_images = (
            bool(always_images) if always_images is not None else image_count > 0
        )
        suggested_dialogue = (
            bool(always_dialogue) if always_dialogue is not None
            else features["dialogue"] > 0
        )
        return jsonify({
            "chunk_count": len(chunks),
            "estimated_cost": result.get("cost_usd", 0),
            "total_tokens": result.get("input_tokens", 0),
            "already_translated_count": already_translated_count,
            "total_chunks": features["total"],
            "dialogue_chunk_count": features["dialogue"],
            "image_chunk_count": image_count,
            "suggested_always_dialogue": suggested_dialogue,
            "suggested_always_images": suggested_images,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/project/<project_id>/translate/realtime", methods=["POST"])
def project_translate_realtime(project_id):
    """Translate a single chunk via API."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _resolve_project_dir(project_id)
    data = request.json or {}
    chunk_id = data.get("chunk_id", "")

    chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
    if not chunk_path.exists():
        return jsonify({"error": "Chunk not found"}), 404

    try:
        from src.api_translator import _book_has_images, translate_chunk_realtime

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

        flags = _load_harness_translate_flags(project_dir)
        always_dialogue = flags["always_include_dialogue"]
        always_images = flags["always_include_image_instructions"]
        if always_images is None:
            always_images = _book_has_images([chunk])
        if always_dialogue is None:
            # Auto over a one-chunk scope is "on iff this chunk has dialogue",
            # which is exactly what dialogue_instruction does when the opt-in is
            # off — so False here renders the same prompt. Kept explicit because
            # the cacheable-prefix reason for the opt-in does not apply to a
            # single realtime call.
            always_dialogue = False

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
            project_slug=project_id,
            always_include_dialogue=always_dialogue,
            always_include_image_instructions=bool(always_images),
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

    project_dir = _resolve_project_dir(project_id)
    data = request.json or {}
    chapter_ids = data.get("chapter_ids", [])
    provider = data.get("provider", "anthropic")
    model = data.get("model", None)
    enable_thinking = bool(data.get("enable_thinking", False))

    if not all(_safe_id(ch_id) for ch_id in chapter_ids):
        return jsonify({"error": "Invalid chapter ID"}), 400

    include_translated = data.get("include_translated", False)

    # Collect chunks to translate
    chunks_dir = project_dir / "chunks"
    chunk_paths = []
    selected_chunks = []
    for ch_id in chapter_ids:
        for cf in sorted(chunks_dir.glob(f"{ch_id}_chunk_*.json")):
            chunk = load_chunk(cf)
            has_translation = bool(chunk.translated_text and chunk.translated_text.strip())
            if include_translated or not has_translation:
                chunk_paths.append(cf)
                selected_chunks.append(chunk)

    if not chunk_paths:
        return jsonify({"error": "No chunks to translate"}), 400

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

    flags = _load_harness_translate_flags(project_dir)
    from src.api_translator import _book_has_images, summarize_chunk_features

    if "always_include_dialogue" in data:
        always_dialogue = bool(data.get("always_include_dialogue"))
    else:
        cfg_dialogue = flags["always_include_dialogue"]
        always_dialogue = (
            bool(cfg_dialogue) if cfg_dialogue is not None
            else summarize_chunk_features(selected_chunks)["dialogue"] > 0
        )

    if "always_include_image_instructions" in data:
        always_images = bool(data.get("always_include_image_instructions"))
    else:
        cfg_images = flags["always_include_image_instructions"]
        always_images = (
            bool(cfg_images) if cfg_images is not None
            else _book_has_images(selected_chunks)
        )

    job_id = str(uuid.uuid4())[:8]
    job_queue = queue.Queue()

    def run_batch():
        from src.api_translator import last_cache_usage, translate_chunk_realtime
        affected_chapters: set[str] = set()
        translated_chunks: list = []
        total_cache_read = 0
        total_cache_created = 0
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
                    project_slug=project_id,
                    enable_thinking=enable_thinking,
                    always_include_dialogue=always_dialogue,
                    always_include_image_instructions=always_images,
                )
                save_chunk(translated, cp)
                affected_chapters.add(chunk.chapter_id)
                translated_chunks.append(translated)
                usage = last_cache_usage() or {}
                cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
                cache_created = int(usage.get("cache_creation_input_tokens", 0) or 0)
                total_cache_read += cache_read
                total_cache_created += cache_created
                job_queue.put(json.dumps({
                    "event": "chunk_done",
                    "chunk_id": chunk.id,
                    "chapter_id": chunk.chapter_id,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_created,
                }))
            except Exception as e:
                job_queue.put(json.dumps({
                    "event": "chunk_error",
                    "chunk_id": chunk.id if chunk else "",
                    "error": str(e),
                }))

        # Run coded evaluators on every chunk we just translated so the Review
        # tab's eval badges are populated without a manual per-chunk rerun.
        # Mirrors the post-translate evaluation done by the async Batch API
        # endpoint below. Non-fatal: evals can always be re-run from Review.
        from web_ui.evaluations import _load_project_blacklist
        blacklist = _load_project_blacklist(project_dir)
        job_queue.put(json.dumps({
            "event": "evals_started",
            "total": len(translated_chunks),
        }))
        evaluated_count = 0
        for tchunk in translated_chunks:
            if (tchunk.translated_text or "").strip():
                try:
                    evaluate_and_persist_chunk(
                        project_dir, tchunk,
                        glossary=glossary, blacklist=blacklist,
                    )
                    evaluated_count += 1
                except Exception:
                    pass  # Non-fatal: evals can be re-run from Review stage
            job_queue.put(json.dumps({
                "event": "chunk_evaluated",
                "chunk_id": tchunk.id,
                "chapter_id": tchunk.chapter_id,
            }))

        # Recombine + realign each affected chapter so the Review tab is
        # immediately usable without a manual "Align" click. Mirrors the
        # post-batch behavior of the async Batch API endpoint above.
        for chapter_id in affected_chapters:
            try:
                from src.combiner import combine_chunks
                from src.sentence_aligner import align_chapter_chunks

                chunk_files = sorted(chunks_dir.glob(f"{chapter_id}_chunk_*.json"))
                ch_chunks = [load_chunk(cf) for cf in chunk_files]
                combined_text = combine_chunks(ch_chunks)
                chapters_dir = project_dir / "chapters"
                chapters_dir.mkdir(exist_ok=True)
                (chapters_dir / f"{chapter_id}.txt").write_text(combined_text, encoding="utf-8")

                align_dir = project_dir / "alignments"
                align_dir.mkdir(exist_ok=True)
                align_chapter_chunks(
                    chunk_paths=[str(cf) for cf in chunk_files],
                    project_id=project_id,
                    chapter_id=chapter_id,
                    source_lang="en",
                    target_lang="es",
                    output_path=str(align_dir / f"{chapter_id}.json"),
                )
                job_queue.put(json.dumps({
                    "event": "chapter_aligned",
                    "chapter_id": chapter_id,
                }))
            except Exception:
                # Non-fatal: alignment can be re-run from the Review stage.
                pass

        job_queue.put(json.dumps({
            "event": "batch_complete",
            "cache_read_input_tokens": total_cache_read,
            "cache_creation_input_tokens": total_cache_created,
            "evaluated_count": evaluated_count,
        }))

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


# ── Batch API (true async batch, 50% discount) ──

_batch_api_tracking_lock = threading.Lock()


def _batch_api_tracking_path(project_dir: Path) -> Path:
    return project_dir / "batch_api_jobs.json"


def _load_batch_api_jobs(project_dir: Path) -> list[dict]:
    path = _batch_api_tracking_path(project_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("jobs", [])
    except Exception:
        return []


def _save_batch_api_jobs(project_dir: Path, jobs: list[dict]):
    import os
    path = _batch_api_tracking_path(project_dir)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"jobs": jobs}, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


@app.route("/api/project/<project_id>/batch-api/submit", methods=["POST"])
def batch_api_submit(project_id):
    """Submit chunks to the provider's async batch API (50% discount)."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _resolve_project_dir(project_id)
    data = request.json or {}
    chapter_ids = data.get("chapter_ids", [])
    provider = data.get("provider", "anthropic")
    model = data.get("model", None)
    enable_thinking = bool(data.get("enable_thinking", False))

    include_translated = data.get("include_translated", False)

    # Validate chapter IDs before using in glob patterns
    if not all(_safe_id(ch_id) for ch_id in chapter_ids):
        return jsonify({"error": "Invalid chapter ID"}), 400

    # Collect chunks to translate
    chunks_dir = project_dir / "chunks"
    chunks = []
    chunk_file_map = {}
    for ch_id in chapter_ids:
        for cf in sorted(chunks_dir.glob(f"{ch_id}_chunk_*.json")):
            chunk = load_chunk(cf)
            has_translation = bool(chunk.translated_text and chunk.translated_text.strip())
            if include_translated or not has_translation:
                chunks.append(chunk)
                chunk_file_map[chunk.id] = str(cf.resolve())

    if not chunks:
        return jsonify({"error": "No chunks to translate"}), 400

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

    # Build context map for previous chapter context
    context_map = {}
    for chunk in chunks:
        ctx = _build_previous_context(project_dir, chunk)
        if ctx:
            context_map[chunk.id] = ctx

    try:
        from src.api_translator import submit_batch, DEFAULT_MODEL

        job_info = submit_batch(
            chunks=chunks,
            provider=provider,
            model=model or DEFAULT_MODEL,
            output_dir=chunks_dir,
            glossary=glossary,
            style_guide=style_guide,
            project_name=_project_title(project_id),
            source_language="English",
            target_language="Spanish",
            context_map=context_map,
            project_slug=project_id,
            enable_thinking=enable_thinking,
        )

        # Store chunk file map for retrieval. chunk_log_map (set by submit_batch)
        # carries the path of each chunk's submission-time prompt log; retrieval
        # mutates that file in place to fill in the response so each completed
        # call ends up as a single self-contained log.
        job_info["chunk_file_map"] = chunk_file_map

        # Save to project-level tracking
        with _batch_api_tracking_lock:
            jobs = _load_batch_api_jobs(project_dir)
            jobs.append(job_info)
            _save_batch_api_jobs(project_dir, jobs)

        return jsonify({
            "job_id": job_info["job_id"],
            "provider": provider,
            "model": job_info["model"],
            "chunk_count": job_info["chunk_count"],
            "status": job_info["status"],
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/project/<project_id>/batch-api/jobs")
def batch_api_list_jobs(project_id):
    """List batch API jobs for this project."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _resolve_project_dir(project_id)
    jobs = _load_batch_api_jobs(project_dir)

    # Return jobs without the chunk_file_map (too verbose for listing)
    summary = []
    for j in jobs:
        summary.append({
            "job_id": j.get("job_id"),
            "provider": j.get("provider"),
            "model": j.get("model"),
            "status": j.get("status"),
            "chunk_count": j.get("chunk_count"),
            "submitted_at": j.get("submitted_at"),
            "completed_at": j.get("completed_at"),
        })

    return jsonify({"jobs": summary})


@app.route("/api/project/<project_id>/batch-api/jobs/<job_id>/check", methods=["POST"])
def batch_api_check_job(project_id, job_id):
    """Check status of a batch API job."""
    if not _safe_id(project_id) or not _safe_id(job_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _resolve_project_dir(project_id)

    # Find job in tracking
    with _batch_api_tracking_lock:
        jobs = _load_batch_api_jobs(project_dir)
        job_info = next((j for j in jobs if j.get("job_id") == job_id), None)

    if not job_info:
        return jsonify({"error": "Job not found"}), 404

    if job_info.get("status") == "completed":
        return jsonify({
            "job_id": job_id,
            "status": "completed",
            "completed_at": job_info.get("completed_at"),
        })

    try:
        from src.api_translator import check_batch_status

        status_info = check_batch_status(job_id, job_info["provider"])

        # Update tracked status (never overwrite "completed" — retrieval already ran)
        with _batch_api_tracking_lock:
            jobs = _load_batch_api_jobs(project_dir)
            for j in jobs:
                if j.get("job_id") == job_id and j.get("status") != "completed":
                    j["status"] = status_info["status"]
                    break
            _save_batch_api_jobs(project_dir, jobs)

        return jsonify(status_info)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/project/<project_id>/batch-api/jobs/<job_id>/retrieve", methods=["POST"])
def batch_api_retrieve_job(project_id, job_id):
    """Retrieve results from a completed batch API job."""
    if not _safe_id(project_id) or not _safe_id(job_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _resolve_project_dir(project_id)

    # Find job and atomically mark it as "retrieving" to prevent double-retrieve
    with _batch_api_tracking_lock:
        jobs = _load_batch_api_jobs(project_dir)
        job_info = next((j for j in jobs if j.get("job_id") == job_id), None)
        if job_info:
            status = job_info.get("status")
            if status in ("completed", "retrieving"):
                return jsonify({"error": "Results already retrieved", "already_done": True}), 400
            job_info["status"] = "retrieving"
            _save_batch_api_jobs(project_dir, jobs)

    if not job_info:
        return jsonify({"error": "Job not found"}), 404

    try:
        from src.api_translator import retrieve_batch_results

        chunk_file_map = job_info.get("chunk_file_map", {})

        # Load original chunks from stored paths (validate paths stay inside project_dir)
        original_chunks = []
        for chunk_id, file_path in chunk_file_map.items():
            try:
                resolved = Path(file_path).resolve()
                if not resolved.is_relative_to(project_dir.resolve()):
                    continue
                original_chunks.append(load_chunk(resolved))
            except Exception:
                pass

        if not original_chunks:
            return jsonify({"error": "Could not load original chunks"}), 500

        chunks_dir = project_dir / "chunks"
        translated = retrieve_batch_results(
            job_id=job_id,
            provider=job_info["provider"],
            original_chunks=original_chunks,
            output_dir=chunks_dir,
            model=job_info.get("model", ""),
            chunk_log_map=job_info.get("chunk_log_map"),
            project_slug=project_id,
        )

        # Save each translated chunk and run post-processing
        affected_chapters = set()
        for chunk in translated:
            save_path = chunk_file_map.get(chunk.id)
            if save_path:
                resolved_save = Path(save_path).resolve()
                if resolved_save.is_relative_to(project_dir.resolve()):
                    save_chunk(chunk, resolved_save)
                chapter_id = chunk.chapter_id
                if chapter_id:
                    affected_chapters.add(chapter_id)

        # Run coded evaluators on every translated chunk
        glossary = None
        blacklist = None
        glossary_path = project_dir / "glossary.json"
        if glossary_path.exists():
            try:
                glossary = load_glossary(glossary_path)
            except Exception:
                pass
        from web_ui.evaluations import _load_project_blacklist
        blacklist = _load_project_blacklist(project_dir)

        evaluated_count = 0
        for chunk in translated:
            if (chunk.translated_text or "").strip():
                try:
                    evaluate_and_persist_chunk(
                        project_dir, chunk,
                        glossary=glossary, blacklist=blacklist,
                    )
                    evaluated_count += 1
                except Exception:
                    pass  # Non-fatal: evals can be re-run from Review stage

        # Recombine + realign affected chapters
        for chapter_id in affected_chapters:
            try:
                from src.combiner import combine_chunks
                from src.sentence_aligner import align_chapter_chunks

                chunk_files = sorted(chunks_dir.glob(f"{chapter_id}_chunk_*.json"))
                ch_chunks = [load_chunk(cf) for cf in chunk_files]
                combined_text = combine_chunks(ch_chunks)
                chapters_dir = project_dir / "chapters"
                chapters_dir.mkdir(exist_ok=True)
                (chapters_dir / f"{chapter_id}.txt").write_text(combined_text, encoding="utf-8")

                align_dir = project_dir / "alignments"
                align_dir.mkdir(exist_ok=True)
                align_chapter_chunks(
                    chunk_paths=[str(cf) for cf in chunk_files],
                    project_id=project_id,
                    chapter_id=chapter_id,
                    source_lang="en",
                    target_lang="es",
                    output_path=str(align_dir / f"{chapter_id}.json"),
                )
            except Exception:
                pass  # Non-fatal: alignment can be re-run from Review stage

        # Update job status
        with _batch_api_tracking_lock:
            jobs = _load_batch_api_jobs(project_dir)
            for j in jobs:
                if j.get("job_id") == job_id:
                    j["status"] = "completed"
                    j["completed_at"] = datetime.now().isoformat()
                    break
            _save_batch_api_jobs(project_dir, jobs)

        return jsonify({
            "ok": True,
            "translated_count": len(translated),
            "evaluated_count": evaluated_count,
            "total_count": len(original_chunks),
            "chapters_affected": list(affected_chapters),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/project/<project_id>/batch-api/jobs/<job_id>", methods=["DELETE"])
def batch_api_delete_job(project_id, job_id):
    if not _safe_id(project_id) or not _safe_id(job_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)
    with _batch_api_tracking_lock:
        jobs = _load_batch_api_jobs(project_dir)
        jobs = [j for j in jobs if j.get("job_id") != job_id]
        _save_batch_api_jobs(project_dir, jobs)
    return jsonify({"ok": True})


@app.route("/api/project/<project_id>/combine/<chapter_id>", methods=["POST"])
def project_combine(project_id, chapter_id):
    """Combine translated chunks back into a chapter file."""
    if not _safe_id(project_id) or not _safe_id(chapter_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _resolve_project_dir(project_id)
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
    """Apply pending corrections for the chapter, then combine + realign.

    Reader's bottom-sheet "Save" queues edits in ``corrections.jsonl`` and
    patches ``alignments/{chapter_id}.json`` in-place, but does not touch
    chunk files. If we realign without first applying those queued rows,
    the chunks (still holding the original Spanish) would regenerate an
    alignment that overwrites the user's edits. Apply per-chapter first
    so realign is always idempotent with respect to the visible reader
    state.
    """
    if not _safe_id(project_id) or not _safe_id(chapter_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _resolve_project_dir(project_id)
    chunks_dir = project_dir / "chunks"

    try:
        from src.combiner import combine_chunks
        from src.sentence_aligner import align_chapter_chunks

        chunk_files = sorted(chunks_dir.glob(f"{chapter_id}_chunk_*.json"))
        if not chunk_files:
            return jsonify({"error": "No chunks found"}), 404

        # Capture the prior alignment (if any) so we can re-anchor any
        # annotations whose es_idx may shift after a fresh align run.
        old_es_map = _load_alignment_es_map(project_dir, chapter_id)

        corrections_applied = _apply_pending_corrections_for_chapter(
            project_dir, chapter_id,
        )

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

        orphaned: list = []
        if old_es_map:
            try:
                orphaned = _reanchor_annotations_after_realign(
                    project_dir, chapter_id, old_es_map,
                )
            except Exception as e:
                app.logger.warning(
                    "Annotation re-anchor failed for %s/%s: %s",
                    project_id, chapter_id, e,
                )
                orphaned = []

        return jsonify({
            "ok": True,
            # align_chapter_chunks returns its rows under "alignments"; reading
            # "pairs" here always reported 0.
            "pairs": len(result.get("alignments", [])),
            "coverage": result.get("coverage"),
            "gaps": result.get("gaps", []),
            "orphaned_annotations": len(orphaned),
            "corrections_applied": corrections_applied,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _apply_pending_corrections_for_chapter(
    project_dir: Path, chapter_id: str,
) -> int:
    """Patch chunks with any queued corrections for this chapter and
    archive the applied rows. Returns the number of corrections applied.

    Corrections targeting other chapters are preserved in
    ``corrections.jsonl``. Rows for this chapter that fail to resolve
    against their chunk are dropped (matching the chunk-editor behavior
    that purges stale corrections after a chunk text changes).
    """
    corrections_path = project_dir / "corrections.jsonl"
    if not corrections_path.exists():
        return 0

    from collections import defaultdict
    from scripts.apply_corrections import apply_to_chunk

    rows: list[dict] = []
    for line in corrections_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    target_rows: list[dict] = []
    other_rows: list[dict] = []
    for record in rows:
        if record.get("chapter_id") == chapter_id:
            target_rows.append(record)
        else:
            other_rows.append(record)

    if not target_rows:
        return 0

    by_chunk: dict[str, list[dict]] = defaultdict(list)
    for record in target_rows:
        by_chunk[record["chunk_id"]].append(record)

    def _correction_record_key(record: dict) -> tuple:
        return (
            record.get("chunk_id"),
            record.get("original_es"),
            record.get("corrected_es"),
            record.get("chunk_offset_start"),
            record.get("chunk_offset_end"),
        )

    chunks_dir = project_dir / "chunks"
    applied_total = 0
    applied_record_keys: set[tuple] = set()
    for chunk_id, chunk_rows in by_chunk.items():
        if not _safe_id(chunk_id):
            continue
        chunk_path = chunks_dir / f"{chunk_id}.json"
        if not chunk_path.exists():
            continue
        try:
            chunk = load_chunk(chunk_path)
        except Exception as e:
            app.logger.warning(
                "Failed to load chunk %s while applying corrections: %s",
                chunk_id, e,
            )
            continue
        try:
            updated_chunk, applied, applied_indices = apply_to_chunk(chunk, chunk_rows)
        except Exception as e:
            app.logger.warning(
                "apply_to_chunk failed for chunk %s: %s", chunk_id, e,
            )
            continue
        if applied > 0:
            save_chunk(updated_chunk, chunk_path)
            applied_total += applied
            for idx in applied_indices:
                applied_record_keys.add(_correction_record_key(chunk_rows[idx]))

    archive_path = project_dir / "corrections_applied.jsonl"
    applied_at = datetime.now().isoformat()
    with open(archive_path, "a", encoding="utf-8") as f:
        for record in target_rows:
            archived = dict(record)
            archived["applied_at"] = applied_at
            archived["status"] = (
                "applied"
                if _correction_record_key(record) in applied_record_keys
                else "skipped"
            )
            f.write(json.dumps(archived, ensure_ascii=False) + "\n")

    if other_rows:
        with open(corrections_path, "w", encoding="utf-8") as f:
            for record in other_rows:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
    else:
        try:
            corrections_path.unlink()
        except OSError:
            pass

    return applied_total


# ============================================================================
# Chunk editor (full-textarea edit of a single chunk's translated_text)
# ============================================================================


def _chapter_id_from_chunk_id(chunk_id: str) -> Optional[str]:
    """Derive the parent chapter_id from a chunk_id like 'chapter_01_chunk_003'.

    Delegates to the evaluations copy so the chapter-list flag badges bucket
    findings exactly the way these callers resolve a chunk to its chapter. The
    two differ only at the edge: this returns None where that returns the
    chunk_id unchanged, because these callers compare against a real chapter.
    """
    chapter_id = chapter_id_from_chunk_id(chunk_id)
    return chapter_id if chapter_id != chunk_id else None


_IMAGE_TOKEN_RE = re.compile(r"\[IMAGE:[^\]]+\]")


def _image_token_ranges(text: str) -> list[tuple[int, int]]:
    """Return [(start, end), ...] character ranges of [IMAGE:...] tokens."""
    return [(m.start(), m.end()) for m in _IMAGE_TOKEN_RE.finditer(text)]


def _spans_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def _check_no_image_token_overlap(
    text: str, start: int, end: int,
) -> Optional[str]:
    """Return an error message if [start:end] overlaps any [IMAGE:...]
    token in ``text``; ``None`` if the range is clear.
    """
    for tok_start, tok_end in _image_token_ranges(text):
        if _spans_overlap(start, end, tok_start, tok_end):
            return (
                f"Selection overlaps an [IMAGE:...] token at characters "
                f"{tok_start}-{tok_end}. Adjust the highlight."
            )
    return None


def _norm_whitespace_with_map(s: str) -> tuple[str, list[int]]:
    """Collapse runs of whitespace to single spaces and return both the
    normalized string and a per-output-character mapping back to the
    original index in ``s``.
    """
    out_chars: list[str] = []
    out_map: list[int] = []
    prev_ws = False
    for i, ch in enumerate(s):
        if ch.isspace():
            if not prev_ws:
                out_chars.append(" ")
                out_map.append(i)
            prev_ws = True
        else:
            out_chars.append(ch)
            out_map.append(i)
            prev_ws = False
    return "".join(out_chars), out_map


def _remove_substring(
    text: str, substring: str, hint_start: Optional[int] = None,
) -> tuple[Optional[str], Optional[tuple[int, int]], Optional[str]]:
    """Remove the first occurrence of ``substring`` from ``text``.

    If ``hint_start`` is provided and ``text[hint_start:]`` begins with
    ``substring``, that occurrence is used directly. Otherwise tries an
    exact match first; on miss, tries a whitespace-normalized match and
    remaps the bounds back to the original string.

    Returns ``(new_text, (orig_start, orig_end), error)``. On success the
    error is ``None``; on failure ``new_text`` and the bounds are ``None``.
    Tidies a single adjoining ASCII space to avoid double-spaces, but
    preserves paragraph breaks (``\\n\\n``).
    """
    if not substring:
        return None, None, "Empty selection."

    # 0. Hint match — client knows the exact offset of the user's
    # selection; prefer it over a substring search to avoid false-first
    # matches when the same string also appears (e.g.) inside an
    # [IMAGE:...] caption earlier in the chunk.
    if hint_start is not None and 0 <= hint_start <= len(text):
        end = hint_start + len(substring)
        if end <= len(text) and text[hint_start:end] == substring:
            new_text = _tidy_after_removal(text, hint_start, end)
            return new_text, (hint_start, end), None

    # 1. Exact match (fast path)
    idx = text.find(substring)
    if idx >= 0:
        end = idx + len(substring)
        new_text = _tidy_after_removal(text, idx, end)
        return new_text, (idx, end), None

    # 2. Whitespace-normalized match
    norm_text, idx_map = _norm_whitespace_with_map(text)
    norm_sub_full, _ = _norm_whitespace_with_map(substring)
    norm_sub = norm_sub_full.strip()
    if not norm_sub:
        return None, None, "Selection is whitespace only."
    n_idx = norm_text.find(norm_sub)
    if n_idx < 0:
        return (
            None, None,
            "Could not locate the selected text in the chunk.",
        )
    n_end = n_idx + len(norm_sub)
    if n_end - 1 >= len(idx_map) or n_idx >= len(idx_map):
        return None, None, "Internal error remapping selection bounds."
    orig_start = idx_map[n_idx]
    orig_end = idx_map[n_end - 1] + 1
    new_text = _tidy_after_removal(text, orig_start, orig_end)
    return new_text, (orig_start, orig_end), None


def _tidy_after_removal(text: str, start: int, end: int) -> str:
    """Remove ``text[start:end]`` and collapse a single adjoining ASCII
    space, while preserving newline-based paragraph boundaries.
    """
    before = text[:start]
    after = text[end:]
    if before.endswith(" ") and after.startswith(" "):
        after = after[1:]
    return before + after


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

    for old_idx, records in active.items():
        old_es_text = old_es_map.get(old_idx)
        if old_es_text is None:
            # Annotation references a sentence we don't know about — leave it.
            orphaned.extend(records)
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
            orphaned.extend(records)
            continue
        if new_idx == old_idx:
            continue

        ts = datetime.now().isoformat()
        # Re-anchor every annotation on this sentence, preserving each one's
        # identity (sub_id) and any imported-footnote provenance.
        for record in records:
            remove_row = {
                "project_id": record.get("project_id"),
                "chapter_id": chapter_id,
                "es_idx": old_idx,
                "removed": True,
                "timestamp": ts,
            }
            recreate_row = {
                "project_id": record.get("project_id"),
                "chapter_id": chapter_id,
                "es_idx": new_idx,
                "type": record.get("type", "flag"),
                "content": record.get("content", ""),
                "timestamp": ts,
            }
            if record.get("sub_id") is not None:
                remove_row["sub_id"] = record["sub_id"]
                recreate_row["sub_id"] = record["sub_id"]
            for extra in ("origin", "fn_number"):
                if record.get(extra) is not None:
                    recreate_row[extra] = record[extra]
            appended.append(remove_row)
            appended.append(recreate_row)

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


def _apply_chunk_edits(
    project_dir: Path,
    project_id: str,
    chapter_id: str,
    edits: list[dict],
) -> dict:
    """Apply a batch of chunk edits in one chapter and rerun the post-edit
    pipeline once.

    Each ``edit`` is a dict with keys: ``chunk_id``, ``chunk_path``,
    ``chunk``, and optionally ``new_source_text`` and/or
    ``new_translated_text``. Fields not provided are left as-is.

    Performs (in order): backup → save chunks → purge corrections →
    recombine chapter → realign chapter → re-anchor annotations →
    evaluate edited chunks. Recombine/realign run once for the chapter,
    not per chunk.
    """
    from src.combiner import combine_chunks
    from src.sentence_aligner import align_chapter_chunks

    # 1. Capture old alignment for annotation re-anchoring
    old_es_map = _load_alignment_es_map(project_dir, chapter_id)

    corrections_purged_total = 0
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    for edit in edits:
        chunk_id = edit["chunk_id"]
        chunk_path: Path = edit["chunk_path"]
        chunk: "Chunk" = edit["chunk"]

        # 2. Backup the pre-edit chunk JSON (last 10)
        backup_root = project_dir / ".chunk_edits" / chapter_id / chunk_id
        backup_root.mkdir(parents=True, exist_ok=True)
        backup_path = backup_root / f"{ts}.json"
        backup_path.write_text(
            chunk_path.read_text(encoding="utf-8"), encoding="utf-8",
        )
        for stale in sorted(backup_root.glob("*.json"))[:-10]:
            try:
                stale.unlink()
            except OSError:
                pass

        # 3. Apply the edit and persist
        if edit.get("new_source_text") is not None:
            chunk.source_text = edit["new_source_text"]
        if edit.get("new_translated_text") is not None:
            chunk.translated_text = edit["new_translated_text"]
        save_chunk(chunk, chunk_path)

        # 4. Purge stale corrections for this chunk
        corrections_purged_total += _purge_chunk_corrections(
            project_dir, chunk_id,
        )

    # 5. Recombine + realign the chapter (once)
    chunks_dir = project_dir / "chunks"
    chunk_files = sorted(chunks_dir.glob(f"{chapter_id}_chunk_*.json"))
    chunks = [load_chunk(cf) for cf in chunk_files]
    combined_text = combine_chunks(chunks)
    chapters_dir = project_dir / "chapters"
    chapters_dir.mkdir(exist_ok=True)
    (chapters_dir / f"{chapter_id}.txt").write_text(
        combined_text, encoding="utf-8",
    )

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

    # 7. Re-evaluate each edited chunk; reload from disk so the evaluator
    # sees the saved bytes rather than the in-memory object.
    evaluations: dict[str, Optional[dict]] = {}
    mtimes: dict[str, float] = {}
    for edit in edits:
        chunk_id = edit["chunk_id"]
        chunk_path = edit["chunk_path"]
        try:
            edited_chunk = load_chunk(chunk_path)
            evaluations[chunk_id] = evaluate_and_persist_chunk(
                project_dir, edited_chunk,
            )
        except Exception as e:
            app.logger.warning(
                "Evaluator pipeline failed for chunk %s: %s", chunk_id, e,
            )
            evaluations[chunk_id] = {"error": str(e)}
        try:
            mtimes[chunk_id] = chunk_path.stat().st_mtime
        except OSError:
            mtimes[chunk_id] = 0.0

    return {
        "ok": True,
        "mtimes": mtimes,
        "orphaned_annotations": len(orphaned),
        "corrections_purged": corrections_purged_total,
        "evaluations": evaluations,
    }


def _replace_chunk_translation(
    project_dir: Path,
    project_id: str,
    chunk_id: str,
    chunk_path: Path,
    chunk: "Chunk",
    new_text: str,
) -> dict:
    """Translation-only single-chunk edit — wraps :func:`_apply_chunk_edits`
    and reshapes the result to the legacy chunk-editor response.
    """
    chapter_id = _chapter_id_from_chunk_id(chunk_id)
    result = _apply_chunk_edits(
        project_dir,
        project_id,
        chapter_id,
        [{
            "chunk_id": chunk_id,
            "chunk_path": chunk_path,
            "chunk": chunk,
            "new_translated_text": new_text,
        }],
    )
    return {
        "ok": True,
        "mtime": result["mtimes"].get(chunk_id, 0.0),
        "orphaned_annotations": result["orphaned_annotations"],
        "corrections_purged": result["corrections_purged"],
        "evaluation": result["evaluations"].get(chunk_id),
    }


# ── Evaluation endpoints ──────────────────────────────────────────────────────


def _llm_judge_is_configured() -> bool:
    """Return True when ``llm_config.json`` exists at the project root."""
    cfg = Path(__file__).resolve().parent.parent / "llm_config.json"
    return cfg.exists()


@app.route(
    "/api/project/<project_id>/evaluations/<chunk_id>",
    methods=["GET"],
)
def project_chunk_evaluation_get(project_id, chunk_id):
    """Return the persisted evaluation JSON for ``chunk_id``.

    Returns 404 when the chunk has never been evaluated — the frontend treats
    404 as "no card yet" rather than an error.
    """
    if not _safe_id(project_id) or not _safe_id(chunk_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _resolve_project_dir(project_id)
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404

    payload = load_chunk_evaluation(project_dir, chunk_id)
    if payload is None:
        return jsonify({"error": "No evaluation yet"}), 404
    payload["feedback"] = load_feedback_for_chunk(project_dir, chunk_id)
    return jsonify(payload)


@app.route(
    "/api/project/<project_id>/evaluations/<chunk_id>/rerun",
    methods=["POST"],
)
def project_chunk_evaluation_rerun(project_id, chunk_id):
    """Rerun the 7 coded evaluators on a chunk without re-translating."""
    if not _safe_id(project_id) or not _safe_id(chunk_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _resolve_project_dir(project_id)
    chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
    if not chunk_path.exists():
        return jsonify({"error": "Chunk not found"}), 404

    try:
        chunk = load_chunk(chunk_path)
    except Exception as e:
        return jsonify({"error": f"Failed to load chunk: {e}"}), 500

    if not (chunk.translated_text or "").strip():
        return jsonify({"error": "Chunk has no translation yet"}), 409

    try:
        evaluation = evaluate_and_persist_chunk(project_dir, chunk)
    except Exception as e:
        app.logger.exception("Rerun evaluators failed for %s", chunk_id)
        return jsonify({"error": str(e)}), 500

    evaluation["feedback"] = load_feedback_for_chunk(project_dir, chunk_id)
    return jsonify({"ok": True, "evaluation": evaluation})


@app.route(
    "/api/project/<project_id>/evaluations/<chunk_id>/llm_judge",
    methods=["POST"],
)
def project_chunk_evaluation_llm_judge(project_id, chunk_id):
    """Run only the opt-in LLM judge evaluator against a chunk."""
    if not _safe_id(project_id) or not _safe_id(chunk_id):
        return jsonify({"error": "Bad request"}), 400

    if not _llm_judge_is_configured():
        return jsonify({"error": "LLM judge is not configured"}), 409

    project_dir = _resolve_project_dir(project_id)
    chunk_path = project_dir / "chunks" / f"{chunk_id}.json"
    if not chunk_path.exists():
        return jsonify({"error": "Chunk not found"}), 404

    try:
        chunk = load_chunk(chunk_path)
    except Exception as e:
        return jsonify({"error": f"Failed to load chunk: {e}"}), 500

    if not (chunk.translated_text or "").strip():
        return jsonify({"error": "Chunk has no translation yet"}), 409

    data = request.json or {}
    judge_provider = data.get("provider") or None
    judge_model = data.get("model") or None

    try:
        from src.evaluators.llm_judge_eval import LLMJudgeEvaluator

        # Build fresh coded-evaluator context so the judge can weigh the
        # objective checks. Reloading the glossary here mirrors the post-save
        # hook's behavior.
        coded_results, _, _ = run_coded_evaluators(chunk)

        style_path = project_dir / "style.json"
        context = {
            "style_json_path": style_path if style_path.exists() else None,
            "coded_eval_results": coded_results,
            "judge_provider": judge_provider,
            "judge_model": judge_model,
        }

        judge = LLMJudgeEvaluator()
        result = judge.evaluate(chunk, context)
    except Exception as e:
        app.logger.exception("LLM judge failed for %s", chunk_id)
        return jsonify({"error": str(e)}), 500

    try:
        result_dict = result.model_dump(mode="json")
    except Exception:
        import json as _json

        result_dict = _json.loads(result.model_dump_json())

    try:
        merge_llm_judge_result(project_dir, chunk_id, result_dict)
    except Exception as e:
        app.logger.exception("Failed to persist LLM judge for %s", chunk_id)
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True, "llm_judge": result_dict})


@app.route(
    "/api/project/<project_id>/evaluations/<chunk_id>/feedback",
    methods=["POST"],
)
def project_chunk_evaluation_feedback(project_id, chunk_id):
    """Record user feedback on a specific evaluator issue."""
    if not _safe_id(project_id) or not _safe_id(chunk_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _resolve_project_dir(project_id)
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404

    data = request.json or {}
    eval_name = (data.get("eval_name") or "").strip()
    feedback_type = (data.get("feedback_type") or "").strip()
    raw_index = data.get("issue_index")
    note = data.get("note")
    message = data.get("message")

    if not eval_name or not feedback_type:
        return jsonify({"error": "eval_name and feedback_type are required"}), 400

    try:
        issue_index = int(raw_index)
    except (TypeError, ValueError):
        return jsonify({"error": "issue_index must be an integer"}), 400

    try:
        append_feedback(
            project_dir,
            chunk_id,
            eval_name=eval_name,
            issue_index=issue_index,
            feedback_type=feedback_type,
            message=message,
            note=note,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        app.logger.exception("Failed to append feedback for %s", chunk_id)
        return jsonify({"error": str(e)}), 500

    return jsonify({"ok": True})


# ── Reader "Review Mode" — overlay evaluator findings on the reader ───────────

# Membership-test forms of the shared category lists (web_ui/evaluations.py).
_REVIEW_CODED_TYPES = frozenset(REVIEW_CODED_TYPES)
_REVIEW_JUDGE_TYPES = frozenset(REVIEW_JUDGE_TYPES)


def _row_containing_offset(rows_sorted: list[dict], offset: int) -> Optional[dict]:
    """Return the alignment row whose chunk char span contains ``offset``.

    ``rows_sorted`` must be sorted by ``chunk_offset_start``. Uses a half-open
    ``[start, end)`` test — the same coordinate space as the coded evaluators'
    ``char_start`` (both index into the chunk's ``translated_text``).
    """
    for row in rows_sorted:
        start = row.get("chunk_offset_start")
        end = row.get("chunk_offset_end")
        if start is None or end is None:
            continue
        if start <= offset < end:
            return row
    return None


def _locate_match(text_in_chunk: str, match_text: str) -> tuple[Optional[int], Optional[int]]:
    """Find ``match_text`` inside ``text_in_chunk`` → sentence-relative span.

    Returns ``(None, None)`` when the match is empty, not found, or spans the
    whole sentence (in which case the caller falls back to a sentence-level
    tint rather than wrapping the entire sentence as a word highlight).
    """
    if not match_text:
        return None, None
    idx = text_in_chunk.find(match_text)
    if idx == -1:
        return None, None
    # A span covering (essentially) the whole sentence reads better as a
    # sentence tint than as a word highlight around everything.
    if idx == 0 and len(match_text) >= len(text_in_chunk.strip()):
        return None, None
    return idx, idx + len(match_text)


def _anchor_judge_excerpt(
    excerpt: Optional[str], translated_text: str, rows_sorted: list[dict]
) -> Optional[int]:
    """Anchor a judge issue (raw excerpt, no offsets) to an ``es_idx``.

    Judges only report an excerpt string, so we locate it inside the chunk's
    ``translated_text`` by searching a short probe (its first non-empty line),
    then map that char offset to the alignment row that contains it. Returns
    ``None`` when the excerpt can't be located (finding is dropped, not
    force-attached — see the v1 known limits).
    """
    if not excerpt or not isinstance(excerpt, str) or not translated_text:
        return None
    probe = ""
    for line in excerpt.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if stripped:
            probe = stripped[:60]
            break
    if not probe:
        return None
    idx = translated_text.find(probe)
    if idx == -1:
        short = probe[:25]
        idx = translated_text.find(short) if len(short) >= 8 else -1
        if idx == -1:
            return None
    row = _row_containing_offset(rows_sorted, idx)
    return row["es_idx"] if row else None


@app.route(
    "/api/project/<project_id>/review/<chapter>",
    methods=["GET"],
)
def project_chapter_review(project_id, chapter):
    """Return evaluator findings for a chapter, anchored to reader sentences.

    Powers the reader's opt-in Review Mode. Reuses the alignment builder and
    the persisted per-chunk evaluations — no new persistence format. Findings
    that already have feedback are treated as dismissed and omitted; chunks
    marked ``stale`` (edited after the run) are skipped and only counted.

    Response::

        { ok, by_es_idx: { "<es_idx>": [finding, ...] },
          type_counts: { blacklist: N, ... }, stale_chunks: N }

    Each finding: ``{eval_name, issue_index, chunk_id, severity, message,
    suggestion, excerpt, match, match_start, match_end}`` where
    ``match_start is None`` ⇒ paint a whole-sentence tint.
    """
    from collections import defaultdict

    if not _safe_id(project_id) or not _safe_id(chapter):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _resolve_project_dir(project_id)
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404

    align_path = project_dir / "alignments" / f"{chapter}.json"
    if not align_path.exists():
        return jsonify({"error": f"Alignment not found: {project_id}/{chapter}"}), 404

    try:
        with open(align_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return jsonify({"error": str(e)}), 500

    # Enrich rows with chunk_id + char offsets + text_in_chunk (same builder
    # /api/alignment uses). No paragraph/image enrichment needed here.
    chunks_dir = project_dir / "chunks"
    if chunks_dir.exists():
        _attach_text_in_chunk(data, chunks_dir)

    rows_by_chunk: dict[str, list[dict]] = defaultdict(list)
    for row in data.get("alignments", []):
        if not isinstance(row, dict) or "es_idx" not in row:
            continue
        cid = row.get("chunk_id")
        if not cid:
            continue
        if row.get("chunk_offset_start") is None or row.get("text_in_chunk") is None:
            continue
        # Skip [IMAGE:...] placeholder sentences — the reader filters these out
        # (see _enrich_alignment), so a finding anchored to one would have no
        # sentence to highlight. Their char span is left uncovered, so any
        # coded finding that lands inside the token is dropped, not misattached.
        if _IMAGE_PLACEHOLDER_RE.fullmatch((row.get("es") or "").strip()):
            continue
        rows_by_chunk[cid].append(row)

    by_es_idx: dict[str, list[dict]] = defaultdict(list)
    type_counts: dict[str, int] = defaultdict(int)
    stale_chunks = 0

    from src.utils.text_utils import normalize_newlines

    feedback_by_chunk = load_all_feedback_by_chunk(project_dir)
    chunk_text_cache: dict[str, str] = {}

    for chunk_id, crows in rows_by_chunk.items():
        payload = load_chunk_evaluation(project_dir, chunk_id)
        if not payload:
            continue
        if payload.get("stale"):
            stale_chunks += 1
            continue

        feedback = feedback_by_chunk.get(chunk_id, [])
        dismissed = {
            (fb.get("eval_name"), fb.get("issue_index")) for fb in feedback
        }
        crows_sorted = sorted(crows, key=lambda r: r["chunk_offset_start"])

        # Coded evaluators → target-side normalized_issues with a char span.
        for ni in payload.get("normalized_issues") or []:
            eval_name = ni.get("eval_name")
            if eval_name not in _REVIEW_CODED_TYPES:
                continue
            loc = ni.get("location") or {}
            if loc.get("side") != "target":
                continue
            char_start = loc.get("char_start")
            if char_start is None:
                continue
            issue_index = ni.get("issue_index")
            if (eval_name, issue_index) in dismissed:
                continue
            row = _row_containing_offset(crows_sorted, char_start)
            if row is None:
                continue
            match_text = loc.get("match") or ""
            match_start, match_end = _locate_match(row["text_in_chunk"], match_text)
            excerpt = match_text or (
                (loc.get("snippet_before") or "")
                + (loc.get("match") or "")
                + (loc.get("snippet_after") or "")
            )
            by_es_idx[str(row["es_idx"])].append({
                "eval_name": eval_name,
                "issue_index": issue_index,
                "chunk_id": chunk_id,
                "severity": ni.get("severity"),
                "message": ni.get("message"),
                "suggestion": ni.get("suggestion"),
                "excerpt": excerpt,
                "match": match_text,
                "match_start": match_start,
                "match_end": match_end,
            })
            type_counts[eval_name] += 1

        # Judges → issues carry only a raw excerpt string; anchor by text.
        judges = payload.get("judges")
        if isinstance(judges, dict) and judges:
            if chunk_id not in chunk_text_cache:
                translated_text = ""
                chunk_path = chunks_dir / f"{chunk_id}.json"
                if chunk_path.exists():
                    try:
                        cdata = json.loads(chunk_path.read_text(encoding="utf-8"))
                        translated_text = normalize_newlines(
                            cdata.get("translated_text") or ""
                        )
                    except (json.JSONDecodeError, OSError):
                        translated_text = ""
                chunk_text_cache[chunk_id] = translated_text
            translated_text = chunk_text_cache[chunk_id]
            for judge_name, jres in judges.items():
                if judge_name not in _REVIEW_JUDGE_TYPES or not isinstance(jres, dict):
                    continue
                for issue_index, issue in enumerate(jres.get("issues") or []):
                    if not isinstance(issue, dict):
                        continue
                    if (judge_name, issue_index) in dismissed:
                        continue
                    excerpt = issue.get("location")
                    es_idx = _anchor_judge_excerpt(excerpt, translated_text, crows_sorted)
                    if es_idx is None:
                        continue
                    by_es_idx[str(es_idx)].append({
                        "eval_name": judge_name,
                        "issue_index": issue_index,
                        "chunk_id": chunk_id,
                        "severity": issue.get("severity"),
                        "message": issue.get("message"),
                        "suggestion": issue.get("suggestion"),
                        "excerpt": excerpt or "",
                        "match": "",
                        "match_start": None,
                        "match_end": None,
                    })
                    type_counts[judge_name] += 1

    return jsonify({
        "ok": True,
        "by_es_idx": dict(by_es_idx),
        "type_counts": dict(type_counts),
        "stale_chunks": stale_chunks,
    })


@app.route(
    "/api/project/<project_id>/evaluations/summary",
    methods=["GET"],
)
def project_evaluations_summary(project_id):
    """Return a per-chunk summary map for badge rendering."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400

    project_dir = _resolve_project_dir(project_id)
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404

    try:
        summary = load_project_summary(project_dir)
    except Exception as e:
        app.logger.exception("Summary walk failed for project %s", project_id)
        return jsonify({"error": str(e)}), 500

    by_chapter: dict[str, dict[str, int]] = {}
    for chunk_id, counts in summary.items():
        chapter_id = _chapter_id_from_chunk_id(chunk_id)
        if not chapter_id:
            continue
        bucket = by_chapter.setdefault(chapter_id, {"errors": 0, "warnings": 0, "info": 0, "stale": 0})
        bucket["errors"] += counts.get("errors", 0) or 0
        bucket["warnings"] += counts.get("warnings", 0) or 0
        bucket["info"] += counts.get("info", 0) or 0
        bucket["stale"] += counts.get("stale", 0) or 0

    return jsonify({"ok": True, "summary": summary, "by_chapter": by_chapter})


@app.route("/read/<project_id>/<chapter>/chunk/<chunk_id>/edit")
def chunk_editor_view(project_id, chapter, chunk_id):
    """Render the full-textarea editor for a single chunk's translated text."""
    t = _reader_strings()
    if not _safe_id(project_id) or not _safe_id(chapter) or not _safe_id(chunk_id):
        return "Bad request", 400

    derived_chapter = _chapter_id_from_chunk_id(chunk_id)
    if derived_chapter != chapter:
        return "Chunk does not belong to chapter", 400

    project_dir = _resolve_project_dir(project_id)
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

    project_dir = _resolve_project_dir(project_id)
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
    project_dir = _resolve_project_dir(project_id)
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

    # Surface the cover image that ``src.epub_builder._resolve_cover`` will
    # auto-pick at build time, so the Export tab can render a thumbnail
    # preview. Mirrors the precedence used in epub_builder.
    images_dir = project_dir / "images"
    cover_filename = None
    cover_mtime = None
    if images_dir.exists():
        for name in ("cover.jpg", "cover.jpeg", "cover.png"):
            candidate = images_dir / name
            if candidate.exists():
                cover_filename = name
                try:
                    cover_mtime = int(candidate.stat().st_mtime)
                except OSError:
                    cover_mtime = None
                break

    return jsonify({
        "total_chapters": len(chapters),
        "translated_chapters": translated_count,
        "chapters": chapters,
        "epub_exists": epub_file is not None,
        "epub_filename": epub_file.name if epub_file else None,
        "title": config.get("title", ""),
        "spanish_title": config.get("spanish_title", ""),
        "author": config.get("author", ""),
        "translator": config.get("translator", ""),
        "description": config.get("description", ""),
        "rights": config.get("rights", ""),
        "source_title": config.get("source_title", ""),
        "publisher": config.get("publisher", ""),
        "cover_filename": cover_filename,
        "cover_mtime": cover_mtime,
    })


@app.route("/api/project/<project_id>/translator-note", methods=["GET"])
def get_translator_note(project_id):
    """Return the per-book translator note (heading + body)."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404
    return jsonify(_load_translator_note(project_id))


@app.route("/api/project/<project_id>/translator-note", methods=["POST"])
def save_translator_note(project_id):
    """Persist the per-book translator note (heading + body)."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404

    data = request.get_json(silent=True) or {}
    heading_raw = data.get("heading", "")
    body_raw = data.get("body", "")
    heading = "" if heading_raw is None else str(heading_raw)
    body = "" if body_raw is None else str(body_raw)

    if len(body.encode("utf-8")) > _TRANSLATOR_NOTE_BODY_MAX_BYTES:
        return jsonify({"error": "Body exceeds 100KB limit"}), 400

    _save_translator_note(project_id, heading, body)
    return jsonify({"ok": True})


@app.route("/api/project/<project_id>/build-epub", methods=["POST"])
def build_epub_route(project_id):
    """Build EPUB from translated chapters.

    Translator-note data flow (per ENG REVIEW decision 1C):
        request.body.{translator_heading, translator_note}
            -> _save_translator_note(...)            # disk = source of truth
            -> _load_translator_note(...)            # round-trip back
            -> build_epub(..., translator_note_*)    # appended as final chapter
    """
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404

    data = request.get_json(silent=True) or {}
    config = _load_project_config(project_id)
    title = data.get("title") or config.get("spanish_title") or config.get("title") or project_id
    author = data.get("author") or config.get("author", "")
    language = data.get("language") or config.get("target_lang_code", "es")

    # Optional Dublin Core metadata: request body wins, otherwise fall back to
    # project.json. Persist whatever the form submitted so re-exports keep the
    # same metadata without retyping (mirrors the translator-note pattern).
    metadata_keys = ("translator", "description", "rights", "source_title", "publisher")
    metadata: dict[str, str] = {}
    config_dirty = False
    for key in metadata_keys:
        if key in data:
            val = "" if data.get(key) is None else str(data.get(key)).strip()
            if config.get(key, "") != val:
                config[key] = val
                config_dirty = True
            metadata[key] = val
        else:
            metadata[key] = str(config.get(key, "") or "").strip()
    # Also persist author/title when supplied so the form is the source of truth.
    for key, val in (("author", author), ("title", data.get("title") or "")):
        if val and config.get(key, "") != val:
            config[key] = val
            config_dirty = True
    if config_dirty:
        _save_project_config(project_id, config)
    # Optional chapter heading synthesis config; request body wins, otherwise
    # build_epub will read it from project.json.
    chapter_heading_config = data.get("chapter_heading")
    if not isinstance(chapter_heading_config, dict):
        chapter_heading_config = None

    # Persist any translator-note edits sent with this build request so disk
    # is always the source of truth (ENG REVIEW decision 1A).
    if "translator_heading" in data or "translator_note" in data:
        h_raw = data.get("translator_heading", "")
        b_raw = data.get("translator_note", "")
        h = "" if h_raw is None else str(h_raw)
        b = "" if b_raw is None else str(b_raw)
        if len(b.encode("utf-8")) > _TRANSLATOR_NOTE_BODY_MAX_BYTES:
            return jsonify({"error": "Translator note body exceeds 100KB limit"}), 400
        _save_translator_note(project_id, h, b)

    note = _load_translator_note(project_id)

    try:
        from src.epub_builder import build_epub_from_chunks
        epub_filename = (Path(title).name or project_id) + ".epub"
        epub_output = project_dir / epub_filename
        result = build_epub_from_chunks(
            project_path=project_dir,
            title=title,
            author=author,
            language=language,
            output_path=epub_output,
            chapter_heading_config=chapter_heading_config,
            translator_note_heading=note.get("heading", ""),
            translator_note_body=note.get("body", ""),
            translator=metadata["translator"] or None,
            description=metadata["description"] or None,
            rights=metadata["rights"] or None,
            source_title=metadata["source_title"] or None,
            publisher=metadata["publisher"] or None,
        )
        epub_path = result.path

        size_bytes = epub_path.stat().st_size

        return jsonify({
            "ok": True,
            "filename": epub_path.name,
            "size_bytes": size_bytes,
            "chapters_included": len(result.included),
        })
    except json.JSONDecodeError as e:
        return jsonify({"error": f"Corrupt chunk file: {e}"}), 500
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/project/<project_id>/download-epub")
def download_epub(project_id):
    """Serve the built EPUB file for download."""
    if not _safe_id(project_id):
        return jsonify({"error": "Bad request"}), 400
    project_dir = _resolve_project_dir(project_id)
    epub_files = list(project_dir.glob("*.epub"))
    if not epub_files:
        return jsonify({"error": "EPUB not found. Build it first."}), 404
    # Use the most recently modified epub
    epub_file = max(epub_files, key=lambda p: p.stat().st_mtime)
    return send_from_directory(str(project_dir), epub_file.name, as_attachment=True)


# ============================================================================
# Edit-review tagging
# ============================================================================

from src.edit_review_constants import EDIT_TAGS  # noqa: E402


@app.route("/api/edit-tags", methods=["GET"])
def list_edit_tags():
    """Return the predefined edit-tag vocabulary used by the review report."""
    return jsonify({"tags": EDIT_TAGS})


@app.route("/api/project/<project_id>/edit-tag", methods=["POST"])
def post_edit_tag(project_id):
    """Append a tag (with optional free-text note) for a chunk's diff hunk.

    Tags are persisted as JSONL at projects/<project_id>/edit_review_tags.jsonl.
    Multiple tags per (chunk_id, hunk_index) are allowed — readers can collapse
    duplicates at render time.
    """
    if not _safe_id(project_id):
        return jsonify({"error": "Bad project id"}), 400
    data = request.json or {}
    chunk_id = (data.get("chunk_id") or "").strip()
    tag = (data.get("tag") or "").strip()
    note = (data.get("note") or "").strip()
    hunk_index = data.get("hunk_index")

    if not _safe_id(chunk_id):
        return jsonify({"error": "Bad chunk id"}), 400
    if tag not in EDIT_TAGS:
        return jsonify({"error": f"Unknown tag '{tag}'"}), 400
    if not isinstance(hunk_index, int) or hunk_index < 0:
        return jsonify({"error": "hunk_index must be a non-negative integer"}), 400

    project_dir = _resolve_project_dir(project_id)
    if not project_dir.exists():
        return jsonify({"error": "Project not found"}), 404

    row = {
        "timestamp": datetime.now().isoformat(),
        "project_id": project_id,
        "chunk_id": chunk_id,
        "hunk_index": hunk_index,
        "tag": tag,
        "note": note,
    }
    tags_path = project_dir / "edit_review_tags.jsonl"
    try:
        with tags_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        return jsonify({"error": f"Failed to write tag: {e}"}), 500

    return jsonify({"ok": True, "row": row})


@app.route("/reports/<project_id>/<path:filename>")
def serve_edit_report(project_id, filename):
    """Serve generated HTML reports from projects/<id>/reports/.

    Same-origin with /api/edit-tag so the report's fetch() calls don't need CORS.
    """
    if not _safe_id(project_id):
        return jsonify({"error": "Bad project id"}), 400
    # Reject path traversal in filename
    if ".." in filename or filename.startswith("/") or "\\" in filename:
        return jsonify({"error": "Bad filename"}), 400
    reports_dir = _resolve_project_dir(project_id) / "reports"
    if not reports_dir.exists():
        return jsonify({"error": "No reports for this project"}), 404
    return send_from_directory(str(reports_dir), filename)


def _print_access_urls(port: int) -> None:
    import os, socket
    # Werkzeug's reloader runs this block twice (parent + child). Only print in
    # the child, identified by WERKZEUG_RUN_MAIN=true.
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return
    try:
        addrs = sorted(set(socket.gethostbyname_ex(socket.gethostname())[2]))
    except socket.gaierror:
        addrs = []
    print("=" * 70)
    print(f"  Translation Web UI — running on port {port}")
    print("=" * 70)
    print(f"  Local:    http://localhost:{port}")
    for addr in addrs:
        print(f"  Network:  http://{addr}:{port}")
    print("  (Phones/tablets on the same Wi-Fi: pick a Network URL above.)")
    print("=" * 70)


if __name__ == "__main__":
    _print_access_urls(5000)
    app.run(debug=True, host="0.0.0.0", port=5000)
