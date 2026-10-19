"""
Everything already known about a book and this edition, in one file.

``gather`` reads the project's own sidecars (no model, and one network call:
the Gutenberg catalog record, cached) and writes ``.harness/kdp/facts.json``.
That file is the drafting step's only input about the book itself, so it has
to answer the questions a listing asks:

- what to call it (Spanish title, subtitle, original title, author, translator);
- what the source is (catalog subjects, edition year, who is credited, rights);
- what this edition adds (editorial footnotes, a new cover, translated or new
  images) — the "differentiated public domain" case KDP requires;
- what was made with AI, for the KDP disclosure;
- what is still unknown (``needs_input``) and must be asked or researched.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Optional

from src.kdp import examples_path, gutenberg, load_defaults, work_dir

#: KDP's "Illustrated" differentiation needs ten or more original illustrations.
ILLUSTRATED_MIN = 10
#: Below this many editorial notes, "anotado" overstates what the edition adds.
ANNOTATED_MIN = 10

_YEAR_IN_SUMMARY = re.compile(r"\b(?:published|written)\s+(?:in\s+)?(1[5-9]\d\d)\b")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _obj(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    return data if isinstance(data, dict) else {}


def _source_url(project: dict, pipeline: dict) -> Optional[str]:
    # Same order as scripts/image_pass.py: the web UI ingest, then the harness.
    return project.get("gutenberg_url") or pipeline.get("url") or None


def _footnotes(project_dir: Path) -> dict[str, int]:
    from src.annotations.store import load_active

    by_origin: Counter[str] = Counter()
    for record in load_active(project_dir, types=("footnote",)):
        by_origin[record.get("origin") or "manual"] += 1
    total = sum(by_origin.values())
    imported = by_origin.get("gutenberg", 0)
    return {
        "total": total,
        "imported_from_source": imported,
        "editorial": total - imported,
        "by_origin": dict(by_origin),
    }


def _images(project_dir: Path) -> dict[str, Any]:
    from src.image_pass import ledger

    images_dir = project_dir / "images"
    on_disk = sorted(
        p.name for p in images_dir.glob("*")
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    ) if images_dir.is_dir() else []
    state = ledger.current_state(project_dir)
    modes: Counter[str] = Counter()
    models: set[str] = set()
    new_cover = False
    cover_made_from_nothing = False
    for key, row in state.items():
        if ledger.status_of(row) != ledger.STATUS_REPLACED:
            continue
        mode = row.get("mode") or "unknown"
        if row.get("model"):
            models.add(str(row["model"]))
        if Path(key).stem == "cover":
            new_cover = True
            cover_made_from_nothing = bool(row.get("created"))
        else:
            modes[mode] += 1
    return {
        "count": len([n for n in on_disk if Path(n).stem != "cover"]),
        "has_cover": any(Path(n).stem == "cover" for n in on_disk),
        "new_cover": new_cover,
        "cover_made_from_nothing": cover_made_from_nothing,
        "translated_lettering": modes.get("translate", 0),
        "restored": modes.get("restore", 0),
        "new_illustrations": modes.get("replace", 0),
        "models": sorted(models),
        "ledger_rows": len(state),
    }


def _translated_words(project_dir: Path) -> int:
    total = 0
    for path in (project_dir / "chunks").glob("*_chunk_*.json"):
        chunk = _obj(path)
        text = chunk.get("translated_text") or ""
        total += len(str(text).split())
    return total


def _authorship(project_dir: Path) -> dict[str, Any]:
    """How far the final Spanish moved from the model's drafts, and who moved it.

    The draft is ``.harness/translate/<chunk>.draft.txt`` (harness books only; a
    redo replaces it, so this measures from the last draft). ``changed_words``
    counts final words outside the longest matching runs against it. The
    corrections log says who made each sentence edit: no ``source`` is a
    reader-view Save, ``judge``/``harness`` are model fixes.
    """
    import difflib

    drafts = project_dir / ".harness" / "translate"
    final_words = compared = changed = 0
    for path in sorted((project_dir / "chunks").glob("*_chunk_*.json")):
        final = str(_obj(path).get("translated_text") or "").split()
        final_words += len(final)
        draft_path = drafts / f"{path.stem}.draft.txt"
        if not final or not draft_path.exists():
            continue
        draft = draft_path.read_text(encoding="utf-8").split()
        matcher = difflib.SequenceMatcher(None, draft, final, autojunk=False)
        compared += len(final)
        changed += len(final) - sum(b.size for b in matcher.get_matching_blocks())
    saves: Counter[str] = Counter()
    log = project_dir / "corrections_applied.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("status") == "skipped":
                continue
            source = str(row.get("source") or "").split(":")[0]
            if source in ("", "manual"):
                saves["reader_from_finding" if row.get("from_finding") else "reader_typed"] += 1
            else:
                saves[source] += 1
    return {
        "final_words": final_words,
        "words_with_draft": compared,
        "changed_words": changed,
        "changed_fraction": round(changed / compared, 4) if compared else None,
        "reader_saves_typed": saves.get("reader_typed", 0),
        "reader_saves_from_finding": saves.get("reader_from_finding", 0),
        "model_fixes": sum(n for k, n in saves.items() if not k.startswith("reader")),
    }


def descriptor_key(footnotes: dict, images: dict) -> str:
    """Which of KDP's differentiations this edition can claim, as a defaults key."""
    parts = ["translated"]
    if footnotes.get("editorial", 0) >= ANNOTATED_MIN:
        parts.append("annotated")
    if images.get("new_illustrations", 0) >= ILLUSTRATED_MIN:
        parts.append("illustrated")
    return "+".join(parts)


def edition_additions(
    footnotes: dict, images: dict, dialect: str, variety_label: str = "",
) -> str:
    """One Spanish sentence on what this edition adds, for the notice and the description.

    ``variety_label`` (defaults ``variety_label``, e.g. "español latino") is how
    the house names the Spanish in public; without it the dialect decides.
    """
    items = []
    if variety_label:
        items.append(f"una traducción nueva al {variety_label}")
    elif dialect == "mexican_spanish":
        items.append("una traducción nueva al español de México")
    else:
        items.append("una traducción nueva al español")
    n = footnotes.get("editorial", 0)
    if n:
        items.append(f"{n} nota{'s' if n != 1 else ''} del traductor")
    if images.get("new_cover"):
        items.append("una portada nueva")
    n = images.get("translated_lettering", 0)
    if n:
        items.append(
            f"{n} ilustraci{'ones' if n != 1 else 'ón'} con el texto traducido al español"
        )
    n = images.get("new_illustrations", 0)
    if n:
        items.append(f"{n} ilustraci{'ones' if n != 1 else 'ón'} nueva{'s' if n != 1 else ''}")
    if len(items) == 1:
        body = items[0]
    else:
        body = ", ".join(items[:-1]) + " y " + items[-1]
    return f"Esta edición incluye {body}."


def render_edition_notice(defaults: dict, values: dict[str, Any]) -> dict[str, str]:
    """Fill the defaults' notice template; a line with an empty placeholder is dropped."""
    template = defaults.get("edition_notice") or {}
    lines = []
    for line in template.get("lines") or []:
        names = re.findall(r"\{(\w+)\}", line)
        if any(not str(values.get(name) or "").strip() for name in names):
            continue
        lines.append(line.format(**{name: values[name] for name in names}))
    return {"heading": template.get("heading") or "Créditos", "body": "\n\n".join(lines)}


def gather(project_dir: Path, *, fetch: bool = True, today: Optional[date] = None) -> dict[str, Any]:
    """Collect the facts and write ``.harness/kdp/facts.json``. Returns the facts."""
    project_dir = Path(project_dir)
    today = today or date.today()
    defaults = load_defaults()
    project = _obj(project_dir / "project.json")
    pipeline = _obj(project_dir / "pipeline_state.json")
    harness = _obj(project_dir / ".harness" / "config.json")
    answers = _obj(project_dir / ".harness" / "style_answers.json")
    ingest = _obj(project_dir / "ingest_report.json").get("metadata") or {}
    cover = _obj(project_dir / "cover" / "spec.json")
    difficulty = _obj(project_dir / "difficulty.json").get("book") or {}
    style = _obj(project_dir / "style.json").get("content") or ""

    url = _source_url(project, pipeline)
    book_id = gutenberg.gutenberg_id(url)
    catalog, catalog_error = (None, None)
    if book_id and fetch:
        catalog, catalog_error = gutenberg.fetch_rdf(book_id, work_dir(project_dir))
    footnotes = _footnotes(project_dir)
    images = _images(project_dir)
    dialect = str(answers.get("dialect") or "")
    if not dialect and harness.get("locale") == "mx":
        dialect = "mexican_spanish"

    translator_default = str(defaults.get("translator") or "").strip()
    translator_project = str(project.get("translator") or "").strip()

    original_year = project.get("original_year")
    year_candidates = []
    if catalog:
        if catalog.get("source_edition_year"):
            year_candidates.append({"year": catalog["source_edition_year"], "from": "source edition (catalog)"})
        match = _YEAR_IN_SUMMARY.search(catalog.get("summary") or "")
        if match:
            year_candidates.append({"year": int(match.group(1)), "from": "catalog summary"})

    needs_input = []
    if not original_year:
        needs_input.append({
            "field": "original_year",
            "why": "First publication of the original work, for the copyright page. "
                   "Candidates below may name a translation's year instead.",
            "candidates": year_candidates,
        })
    if translator_default and translator_project and translator_project != translator_default:
        needs_input.append({
            "field": "translator",
            "why": f"project.json spells it {translator_project!r}; the house default is "
                   f"{translator_default!r}. apply writes the default unless told otherwise.",
        })
    if not (project.get("author") or harness.get("author") or ingest.get("creator")):
        needs_input.append({"field": "author", "why": "No author recorded anywhere."})

    descriptors = defaults.get("descriptors") or {}
    key = descriptor_key(footnotes, images)
    additions = edition_additions(
        footnotes, images, dialect, str(defaults.get("variety_label") or "").strip())
    notice_values = {
        "source_title": project.get("source_title") or (catalog or {}).get("title")
                        or harness.get("title") or "",
        "original_year": original_year or "",
        "translator": translator_default or translator_project,
        "edition_additions": additions,
        "year": today.year,
        "publisher": project.get("publisher") or defaults.get("publisher") or "",
    }

    facts = {
        "slug": project_dir.name,
        "generated_at": today.isoformat(),
        "defaults_source": defaults.get("_source"),
        "titles": {
            "spanish_title": project.get("spanish_title") or "",
            "project_title": project.get("title") or "",
            "subtitle": project.get("subtitle") or "",
            "cover_title": cover.get("title") or "",
            "cover_subtitle": cover.get("subtitle") or "",
            "source_title": project.get("source_title") or "",
            "setup_title": harness.get("title") or "",
            "ingest_title": ingest.get("title") or "",
            "catalog_title": (catalog or {}).get("title") or "",
        },
        "author": project.get("author") or harness.get("author") or ingest.get("creator") or "",
        "translator": {"project": translator_project, "default": translator_default},
        "publisher": project.get("publisher") or defaults.get("publisher") or "",
        "source": {
            "url": url,
            "gutenberg_id": book_id,
            "format": pipeline.get("source_format"),
            "ingest_publisher": ingest.get("publisher") or "",
        },
        "catalog": catalog,
        "catalog_error": catalog_error,
        "rights_flags": gutenberg.rights_flags(catalog, today=today),
        "language": {
            "dialect": dialect,
            "locale": harness.get("locale") or "",
            "style_excerpt": str(style)[:700],
        },
        "size": {
            "source_words": pipeline.get("source_words"),
            "translated_words": _translated_words(project_dir),
            "chapters": len(project.get("chapter_manifest") or []) or pipeline.get("chapter_count"),
        },
        "readability": {
            key_: difficulty.get(key_) for key_ in
            ("difficulty", "mean_sentence_length", "rare_word_fraction", "dialogue_density")
        },
        "footnotes": footnotes,
        "images": images,
        "translator_note": (project_dir / "translator_note.json").exists(),
        "authorship": _authorship(project_dir),
        "publishing_rights_default": str(defaults.get("publishing_rights") or "public_domain"),
        "ai": {
            "translation": {
                "provider": harness.get("provider"),
                "model": harness.get("model"),
                "backend": harness.get("backend"),
                "worker_model": harness.get("worker_model"),
                "headless_cli": harness.get("headless_cli"),
            },
            "images_touched_by_image_pass": images["ledger_rows"] > 0,
            "image_models": images["models"],
        },
        "edition": {
            "descriptor_key": key,
            "descriptor": descriptors.get(key) or "",
            "additions": additions,
            "house_subtitle": str(defaults.get("subtitle") or ""),
            "series": str(defaults.get("series") or ""),
            "variety_label": str(defaults.get("variety_label") or ""),
            "notice_draft": render_edition_notice(defaults, notice_values),
        },
        "existing_listing": (project_dir / "kdp_listing.json").exists(),
        "description_examples": str(examples_path()),
        "needs_input": needs_input,
    }

    out = work_dir(project_dir) / "facts.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    facts["_path"] = str(out)
    return facts
