"""Shared fixtures for footnote-pass tests.

The fixture book is deliberately built the way ``endnotes.build_endnote_artifacts``
reads one — an alignment whose ``es`` strings occur **verbatim** in
``chapters/<id>.txt`` — because every validation in ``src/footnote_pass/write.py``
is a prediction about what that function will do. A fixture whose chapter body did
not contain its own aligned sentences would make every test pass for the wrong
reason.
"""

from __future__ import annotations

import json

import pytest

CHAPTER_01 = [
    (0, "Comimos ostión en el puerto.", "We ate oyster at the harbor."),
    (1, "El pulgón suelta una gota por sus cuernitos.", "The aphid drips from its cornicles."),
    (2, "Nos fuimos a Sancerre.", "We left for Sancerre."),
    (3, "La abeja vio la abeja de nuevo.", "The bee saw the bee again."),
]

CHAPTER_02 = [
    (0, "Tío Pablo abrió la colmena.", "Uncle Paul opened the hive."),
    (1, "La mediana edad llega a los cuarenta.", "Middle age arrives at forty."),
]


def write_alignment(project_dir, chapter_id, pairs):
    """Write ``alignments/<chapter_id>.json`` from ``[(es_idx, es, en), ...]``."""
    align_dir = project_dir / "alignments"
    align_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "chapter_id": chapter_id,
        "alignments": [
            {
                "es_idx": idx,
                "en_idx": idx,
                "es": es,
                "en": en,
                "chunk_id": f"{chapter_id}_chunk_000",
            }
            for idx, es, en in pairs
        ],
    }
    (align_dir / f"{chapter_id}.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8"
    )


def write_chapter(project_dir, chapter_id, pairs):
    """Write ``chapters/<chapter_id>.txt`` as the aligned sentences, joined."""
    chapters = project_dir / "chapters"
    chapters.mkdir(parents=True, exist_ok=True)
    body = " ".join(es for _idx, es, _en in pairs)
    (chapters / f"{chapter_id}.txt").write_text(body, encoding="utf-8")
    return body


def write_annotations(project_dir, records):
    """Write ``annotations.jsonl`` from ``records``, in order."""
    path = project_dir / "annotations.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


def footnote_record(chapter_id, es_idx, content, *, sub_id="u0000dead", **extra):
    """One active ``footnote`` record in the reader's wire shape."""
    return {
        "project_id": "testbook",
        "chapter_id": chapter_id,
        "es_idx": es_idx,
        "sub_id": sub_id,
        "type": "footnote",
        "content": content,
        "timestamp": "2026-09-11T00:00:00",
        **extra,
    }


@pytest.fixture
def project(tmp_path):
    """A two-chapter book whose bodies really contain their aligned sentences."""
    project_dir = tmp_path / "testbook"
    project_dir.mkdir()

    (project_dir / "style.json").write_text(
        json.dumps({"content": "REGISTER\nPlain, warm, for children."}),
        encoding="utf-8",
    )
    (project_dir / "glossary.json").write_text(
        json.dumps({"terms": [{"english": "oyster", "spanish": "ostión"}]}),
        encoding="utf-8",
    )
    harness = project_dir / ".harness"
    harness.mkdir()
    (harness / "config.json").write_text(
        json.dumps({"target_language": "Spanish", "headless_cli": "claude"}),
        encoding="utf-8",
    )

    for chapter_id, pairs in (("chapter_01", CHAPTER_01), ("chapter_02", CHAPTER_02)):
        write_alignment(project_dir, chapter_id, pairs)
        write_chapter(project_dir, chapter_id, pairs)

    return project_dir


@pytest.fixture
def profile_file(tmp_path):
    """An approved Gate 1 profile on disk, which ``scan-prepare`` requires."""
    path = tmp_path / "profile.md"
    path.write_text(
        "# Footnote profile\n\n- corrects the author's science\n- supplies period context\n",
        encoding="utf-8",
    )
    return path
