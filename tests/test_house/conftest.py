"""A small house on disk: rules, lexicon, groups and sibling books under tmp_path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

RULES = {
    "every_book": [
        {"id": "mexican-spanish", "rule": "Write Mexican Spanish.",
         "settles": {"style:dialect": "mexican_spanish"}},
        {"id": "no-vos", "rule": "Only tú, usted and ustedes."},
        {"id": "rio-no-tilde", "rule": "rio, never rió.", "enforce": "mechanical",
         "fixer": "rio_no_tilde"},
        {"id": "old-surname", "rule": "Old + surname is el viejo + surname.",
         "triggers": ["re:\\bOld [A-Z][a-z]+"]},
        {"id": "the-xes", "rule": "The Xes is los X.", "triggers": ["re:\\bthe [A-Z][a-z]+s\\b"],
         "scope": "group:hardy"},
        {"id": "keep-imperial", "rule": "Keep imperial units.", "enforce": "draft",
         "settles": {"style:measurement_handling": "keep_imperial",
                     "decisions:units-and-culture": "Keep imperial units."}},
        {"id": "adult-usted", "rule": "Adults say usted.", "enforce": "draft",
         "settles": {"address:global_rules": "Two adult strangers say usted both ways."}},
    ],
}

LEXICON = {
    "entries": [
        {"english": "deer", "spanish": "venado"},
        {"english": "stall", "spanish": "casilla", "scope": "group:horses"},
        {"english": "gobbler", "spanish": None},
        {"english": "saucepan", "spanish": "cacerola"},
    ]
}


def write_json(path: Path, doc) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def make_book(
    root: Path,
    slug: str,
    *,
    source: str = "",
    glossary: list[dict] | None = None,
    answers: dict | None = None,
    pairs: list[dict] | None = None,
    global_rules: str = "",
    chunks: list[tuple[str, str]] | None = None,
) -> Path:
    book = root / slug
    book.mkdir(parents=True, exist_ok=True)
    (book / "source.txt").write_text(source, encoding="utf-8")
    if glossary is not None:
        write_json(book / "glossary.json", {"terms": glossary, "version": "1.0"})
    if answers is not None:
        write_json(book / ".harness" / "style_answers.json", answers)
    if pairs is not None or global_rules:
        write_json(book / "address_map.json", {
            "content": "map", "pairs": pairs or [], "global_rules": global_rules,
        })
    for i, (src, tr) in enumerate(chunks or []):
        write_json(book / "chunks" / f"chapter_01_chunk_{i:03d}.json", {
            "id": f"chapter_01_chunk_{i:03d}", "chapter_id": "chapter_01", "position": i,
            "source_text": src, "translated_text": tr,
        })
    return book


def pair(a: str, b: str, ab: str, ba: str) -> dict:
    return {"a": a, "b": b, "directions": {
        "a_to_b": [{"form": ab, "when": "default"}],
        "b_to_a": [{"form": ba, "when": "default"}],
    }}


@pytest.fixture
def house(tmp_path, monkeypatch):
    """Rules, lexicon and groups under tmp_path; projects live in tmp_path/projects."""
    import src.house as house_pkg
    from src.house import check, groups, lexicon, precedent
    from src.judges import context

    projects = tmp_path / "projects"
    projects.mkdir()
    rules = write_json(tmp_path / "prompts" / "house_style_rules.json", RULES)
    monkeypatch.setattr(context, "HOUSE_STYLE_RULES_FILE", rules)
    monkeypatch.setattr(lexicon, "LEXICON_FILE", write_json(tmp_path / "prompts" / "house_lexicon.json", LEXICON))
    monkeypatch.setattr(groups, "GROUPS_FILE", write_json(tmp_path / "prompts" / "book_groups.json", {
        "groups": {
            "horses": {"kind": "domain", "members": ["new-horse", "old-horse", "other-horse"]},
            "hardy": {"kind": "series", "members": ["hardy-2", "hardy-1", "hardy-3"]},
        }
    }))
    for module in (house_pkg, precedent, check):
        monkeypatch.setattr(module, "PROJECTS_DIR", projects)
    return projects
