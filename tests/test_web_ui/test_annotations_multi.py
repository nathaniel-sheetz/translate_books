"""Tests for multiple annotations per aligned sentence.

The reader keys annotations by ``(es_idx, sub_id)``: user-created notes get a
server-generated ``sub_id`` so several can coexist on one sentence, each
independently editable/deletable, while legacy records without a ``sub_id`` and
imported footnotes (``sub_id`` = ``gb1``, ``gb2``, ...) keep working.
"""

import json
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web_ui.app import app, _load_annotations, _load_annotation_counts


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Minimal project with one aligned chapter, wired to a temp projects dir."""
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "test-project"
    align_dir = proj_dir / "alignments"
    align_dir.mkdir(parents=True)
    (proj_dir / "chunks").mkdir(parents=True, exist_ok=True)

    alignment = {
        "chapter_id": "chapter_01",
        "project_id": "test-project",
        "alignments": [
            {"es_idx": 0, "en_idx": 0, "es": "El gato.", "en": "The cat.",
             "confidence": "high", "chunk_id": "chapter_01_chunk_000"},
            {"es_idx": 1, "en_idx": 1, "es": "El perro.", "en": "The dog.",
             "confidence": "high", "chunk_id": "chapter_01_chunk_000"},
        ],
    }
    with open(align_dir / "chapter_01.json", "w", encoding="utf-8") as f:
        json.dump(alignment, f, ensure_ascii=False)

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


def _post(client, **fields):
    body = {"project_id": "test-project", "chapter_id": "chapter_01"}
    body.update(fields)
    return client.post("/api/annotation", json=body)


def _delete(client, **fields):
    body = {"project_id": "test-project", "chapter_id": "chapter_01"}
    body.update(fields)
    return client.delete("/api/annotation", json=body)


def _get(client):
    rv = client.get("/api/annotations/test-project/chapter_01")
    assert rv.status_code == 200
    return rv.get_json()["annotations"]


def _write_raw(proj_dir, records):
    with open(proj_dir / "annotations.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class TestCreate:
    def test_two_creates_get_distinct_ids_and_both_surface(self, client, project):
        r1 = _post(client, es_idx=0, type="word_choice", content="first")
        r2 = _post(client, es_idx=0, type="inconsistency", content="second")
        sid1 = r1.get_json()["sub_id"]
        sid2 = r2.get_json()["sub_id"]
        assert sid1 and sid2 and sid1 != sid2
        assert sid1.startswith("u") and sid2.startswith("u")

        anns = _get(client)
        assert len(anns) == 2
        by_content = {a["content"]: a for a in anns}
        assert set(by_content) == {"first", "second"}
        assert by_content["first"]["sub_id"] == sid1
        assert by_content["second"]["sub_id"] == sid2

    def test_grouped_loader_returns_list_oldest_first(self, client, project):
        _post(client, es_idx=0, type="word_choice", content="A")
        _post(client, es_idx=0, type="flag", content="B")
        grouped = _load_annotations(project, "chapter_01")
        assert list(grouped.keys()) == [0]
        assert [a["content"] for a in grouped[0]] == ["A", "B"]


class TestEdit:
    def test_edit_by_sub_id_updates_in_place(self, client, project):
        sid = _post(client, es_idx=0, type="word_choice", content="draft").get_json()["sub_id"]
        _post(client, es_idx=0, type="footnote", content="revised", sub_id=sid)

        anns = _get(client)
        assert len(anns) == 1
        assert anns[0]["sub_id"] == sid
        assert anns[0]["content"] == "revised"
        assert anns[0]["type"] == "footnote"


class TestDelete:
    def test_delete_one_keeps_sibling(self, client, project):
        sid1 = _post(client, es_idx=0, type="word_choice", content="keep").get_json()["sub_id"]
        sid2 = _post(client, es_idx=0, type="flag", content="drop").get_json()["sub_id"]

        assert _delete(client, es_idx=0, sub_id=sid2).status_code == 200
        anns = _get(client)
        assert len(anns) == 1
        assert anns[0]["sub_id"] == sid1
        assert anns[0]["content"] == "keep"


class TestLegacyCompat:
    def test_legacy_record_without_sub_id_loads(self, client, project):
        _write_raw(project, [
            {"project_id": "test-project", "chapter_id": "chapter_01",
             "es_idx": 0, "type": "flag", "content": "old note",
             "timestamp": "2026-01-01T00:00:00"},
        ])
        anns = _get(client)
        assert len(anns) == 1
        assert anns[0]["content"] == "old note"
        # Wire protocol: missing storage sub_id surfaces as the "legacy" sentinel
        # so the client can edit/delete without minting a sibling.
        assert anns[0]["sub_id"] == "legacy"

    def test_legacy_edit_via_sentinel_updates_in_place(self, client, project):
        _write_raw(project, [
            {"project_id": "test-project", "chapter_id": "chapter_01",
             "es_idx": 0, "type": "flag", "content": "old note",
             "timestamp": "2026-01-01T00:00:00"},
        ])
        r = _post(client, es_idx=0, type="footnote", content="revised", sub_id="legacy")
        assert r.status_code == 200
        assert r.get_json()["sub_id"] == "legacy"
        anns = _get(client)
        assert len(anns) == 1
        assert anns[0]["content"] == "revised"
        assert anns[0]["type"] == "footnote"
        assert anns[0]["sub_id"] == "legacy"

    def test_legacy_tombstone_without_sub_id_removes_it(self, client, project):
        _write_raw(project, [
            {"project_id": "test-project", "chapter_id": "chapter_01",
             "es_idx": 0, "type": "flag", "content": "old note",
             "timestamp": "2026-01-01T00:00:00"},
        ])
        assert _delete(client, es_idx=0).status_code == 200
        assert _get(client) == []

    def test_legacy_delete_via_sentinel(self, client, project):
        _write_raw(project, [
            {"project_id": "test-project", "chapter_id": "chapter_01",
             "es_idx": 0, "type": "flag", "content": "old note",
             "timestamp": "2026-01-01T00:00:00"},
        ])
        assert _delete(client, es_idx=0, sub_id="legacy").status_code == 200
        assert _get(client) == []

    def test_invalid_sub_id_rejected(self, client, project):
        r = _post(client, es_idx=0, type="flag", content="x", sub_id="bad id!")
        assert r.status_code == 400
        assert _get(client) == []
        _write_raw(project, [
            {"project_id": "test-project", "chapter_id": "chapter_01",
             "es_idx": 0, "sub_id": "uabc", "type": "flag", "content": "keep",
             "timestamp": "2026-01-01T00:00:00"},
        ])
        assert _delete(client, es_idx=0, sub_id="bad id!").status_code == 400
        assert len(_get(client)) == 1


class TestFootnoteSurfacing:
    def test_multiple_imported_footnotes_both_surface(self, client, project):
        """The v1 bug: two footnotes shared an es_idx, so only the second (gb2)
        showed. With (es_idx, sub_id) keying both surface now."""
        _write_raw(project, [
            {"project_id": "test-project", "chapter_id": "chapter_01",
             "es_idx": 0, "sub_id": "gb1", "type": "footnote",
             "content": "[a] first note", "origin": "gutenberg", "fn_number": 1,
             "timestamp": "2026-01-01T00:00:01"},
            {"project_id": "test-project", "chapter_id": "chapter_01",
             "es_idx": 0, "sub_id": "gb2", "type": "footnote",
             "content": "[b] second note", "origin": "gutenberg", "fn_number": 2,
             "timestamp": "2026-01-01T00:00:02"},
        ])
        anns = _get(client)
        assert len(anns) == 2
        assert {a["content"] for a in anns} == {"[a] first note", "[b] second note"}
        assert all(a["type"] == "footnote" for a in anns)


class TestCounts:
    def test_counts_sentences_not_annotations(self, client, project):
        # Two annotations on es_idx 0, one on es_idx 1 → 2 sentences annotated.
        _post(client, es_idx=0, type="word_choice", content="A")
        _post(client, es_idx=0, type="flag", content="B")
        _post(client, es_idx=1, type="inconsistency", content="C")
        counts = _load_annotation_counts(project)
        assert counts == {"chapter_01": 2}
