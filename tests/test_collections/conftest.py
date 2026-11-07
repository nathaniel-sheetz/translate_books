"""A small library on disk: projects (and collections of them) under tmp_path/projects."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest

# A 1x1 PNG; two members carry an image of the same name with different bytes.
PNG_A = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92"
    b"\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)
PNG_B = PNG_A + b"\x00"


def write_json(path: Path, doc) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_chunk(project: Path, chapter_id: str, position: int, source: str,
                translated: Optional[str]) -> None:
    chunk_id = f"{chapter_id}_chunk_{position:03d}"
    write_json(project / "chunks" / f"{chunk_id}.json", {
        "id": chunk_id,
        "chapter_id": chapter_id,
        "position": position,
        "source_text": source,
        "translated_text": translated,
        "metadata": {
            "char_start": 0, "char_end": len(source), "overlap_start": 0,
            "overlap_end": 0, "paragraph_count": 1, "word_count": len(source.split()),
        },
    })


def make_project(
    root: Path,
    slug: str,
    *,
    chapters: list[tuple[str, Optional[str]]],
    manifest: Optional[list[dict]] = None,
    project: Optional[dict] = None,
    harness: Optional[dict] = None,
    images: Optional[dict[str, bytes]] = None,
    footnotes: Optional[dict[str, list[tuple[int, str, str]]]] = None,
) -> Path:
    """A project with one chunk per chapter.

    ``chapters`` is ``[(english, spanish or None), ...]`` for chapter_01, 02, ...
    ``footnotes`` maps a chapter id to ``[(es_idx, sentence, note), ...]``:
    the sentence goes in the alignment and the note in annotations.jsonl.
    """
    book = root / slug
    book.mkdir(parents=True, exist_ok=True)
    (book / "source.txt").write_text("\n\n".join(en for en, _ in chapters), encoding="utf-8")
    for i, (en, es) in enumerate(chapters, 1):
        write_chunk(book, f"chapter_{i:02d}", 0, en, es)
    doc = dict(project or {})
    if manifest is not None:
        doc["chapter_manifest"] = manifest
    if doc:
        write_json(book / "project.json", doc)
    if harness:
        write_json(book / ".harness" / "config.json", harness)
    for name, data in (images or {}).items():
        (book / "images").mkdir(exist_ok=True)
        (book / "images" / name).write_bytes(data)
    records = []
    for chapter_id, notes in (footnotes or {}).items():
        write_json(book / "alignments" / f"{chapter_id}.json", {
            "chapter_id": chapter_id,
            "alignments": [{"es_idx": idx, "es": sentence} for idx, sentence, _ in notes],
        })
        for idx, _, note in notes:
            records.append({
                "id": f"{chapter_id}-{idx}", "chapter_id": chapter_id, "es_idx": idx,
                "type": "footnote", "content": note, "timestamp": "2026-11-01T00:00:00",
            })
    if records:
        (book / "annotations.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8")
    return book


@pytest.fixture
def library(tmp_path, monkeypatch):
    """``projects/`` under tmp_path, seen by the harness, the house layer and the groups file."""
    import src.house as house_pkg
    from src.harness import state

    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(state, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(house_pkg, "PROJECTS_DIR", projects)
    return projects


@pytest.fixture
def plumer(library):
    """Two works by one author: a sermon book with a preface, and a one-chapter story."""
    sermons = make_project(
        library, "short-sermons",
        chapters=[
            ("Preface\n\nA word to parents.", "Prefacio\n\nUna palabra a los padres."),
            ("SERMON I.\n\nThere is a God.", "SERMÓN I.\n\nHay un Dios.\n\n[IMAGE:images/illus01.jpg]\n\nDios hizo el mundo."),
            ("SERMON II.\n\nGod is good.", "SERMÓN II.\n\nDios es bueno."),
        ],
        manifest=[
            {"id": "chapter_01", "kind": "front_matter", "label": "Preface"},
            {"id": "chapter_02", "kind": "chapter", "number": 1},
            {"id": "chapter_03", "kind": "chapter", "number": 2},
        ],
        project={"spanish_title": "Sermones cortos", "author": "William S. Plumer",
                 "translator": "N. C. Sheetz", "original_year": 1848},
        harness={"title": "Short Sermons to Little Children"},
        images={"illus01.jpg": PNG_A},
        footnotes={"chapter_02": [(1, "Dios hizo el mundo.", "[Dios] Nota del sermón.")]},
    )
    story = make_project(
        library, "plain-thoughts",
        chapters=[
            ("PLAIN THOUGHTS\n\nA short word.",
             "PENSAMIENTOS SENCILLOS\n\nUna palabra breve.\n\n[IMAGE:images/illus01.jpg]\n\nFin."),
        ],
        project={"spanish_title": "Pensamientos sencillos", "author": "William S. Plumer",
                 "translator": "N. C. Sheetz"},
        harness={"title": "Plain Thoughts for Children"},
        images={"illus01.jpg": PNG_B},
        footnotes={"chapter_01": [(0, "Una palabra breve.", "Nota del cuento.")]},
    )
    return sermons, story
