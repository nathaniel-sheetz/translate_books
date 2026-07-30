"""Shared fixtures for annotation-review tests."""

from __future__ import annotations

import json

import pytest


def write_annotations(project_dir, records):
    """Write ``records`` as annotations.jsonl lines, in order."""
    path = project_dir / "annotations.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return path


def write_alignment(project_dir, chapter_id, pairs, *, chunk_id="chapter_01_chunk_000"):
    """Write an alignment file from ``[(es_idx, es, en), ...]``."""
    align_dir = project_dir / "alignments"
    align_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "chapter_id": chapter_id,
        "alignments": [
            {"es_idx": idx, "en_idx": idx, "es": es, "en": en, "chunk_id": chunk_id}
            for idx, es, en in pairs
        ],
    }
    (align_dir / f"{chapter_id}.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture
def project(tmp_path):
    """A minimal book project: style guide, glossary, harness config, one chapter."""
    project_dir = tmp_path / "testbook"
    project_dir.mkdir()

    (project_dir / "style.json").write_text(
        json.dumps({"content": "REGISTER\nPlain, warm, for children."}),
        encoding="utf-8",
    )
    (project_dir / "glossary.json").write_text(
        json.dumps(
            {
                "terms": [
                    {
                        "english": "oyster",
                        "spanish": "ostión",
                        "type": "noun",
                        "context": "",
                        "alternatives": ["ostra"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    harness = project_dir / ".harness"
    harness.mkdir()
    (harness / "config.json").write_text(
        json.dumps({"target_language": "Spanish", "locale": "mx"}), encoding="utf-8"
    )

    write_alignment(
        project_dir,
        "chapter_01",
        [
            (0, "Comimos ostión en el puerto.", "We ate oyster at the harbor."),
            (1, "El poyo estaba frío.", "The stone bench was cold."),
            (2, "Nos fuimos a Sancerre.", "We left for Sancerre."),
            (3, "La ostra sabía distinta.", "The oyster tasted different."),
            # A second ostión so a concordance search from es_idx 0 still finds
            # one: the annotated sentence itself is always excluded.
            (4, "Otro ostión llegó después.", "Another oyster came later."),
        ],
    )
    return project_dir
