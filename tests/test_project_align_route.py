"""Endpoint tests for the dashboard realign route.

Covers POST /api/project/<project_id>/align/<chapter_id>, which combines
chunks, runs sentence alignment, and re-anchors any existing chapter
annotations whose es_idx may have shifted after the new alignment.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.sentence_aligner as sentence_aligner_module
from web_ui.app import app
from src.models import Chunk, ChunkMetadata, ChunkStatus
from src.utils.file_io import save_chunk


# ---------- fixtures ----------


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _make_chunk(chunk_id: str, chapter_id: str, source: str, translated: str) -> Chunk:
    return Chunk(
        id=chunk_id,
        chapter_id=chapter_id,
        position=0,
        source_text=source,
        translated_text=translated,
        metadata=ChunkMetadata(
            char_start=0,
            char_end=len(source),
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=len(source.split()),
        ),
        status=ChunkStatus.TRANSLATED,
    )


def _write_alignment(align_dir: Path, chapter_id: str, pairs: list[dict]) -> Path:
    """Write an alignments/<chapter_id>.json file with the given es/en pairs."""
    payload = {
        "chapter_id": chapter_id,
        "project_id": "test-project",
        "en_count": len(pairs),
        "es_count": len(pairs),
        "high_confidence_pct": 100.0,
        "avg_similarity": 0.95,
        "alignments": pairs,
    }
    path = align_dir / f"{chapter_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Project with one chunk; no alignment/annotations by default."""
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "test-project"
    chunks_dir = proj_dir / "chunks"
    align_dir = proj_dir / "alignments"
    chapters_dir = proj_dir / "chapters"
    for d in (chunks_dir, align_dir, chapters_dir):
        d.mkdir(parents=True)

    chunk = _make_chunk(
        "chapter_01_chunk_000",
        "chapter_01",
        "The cat sat. The dog barked. The bird flew.",
        "El gato se sentó. El perro ladró. El pájaro voló.",
    )
    save_chunk(chunk, chunks_dir / "chapter_01_chunk_000.json")

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


def _patch_aligner(monkeypatch, new_pairs: list[dict]):
    """Patch align_chapter_chunks to write a controlled new alignment file.

    The endpoint calls `from src.sentence_aligner import align_chapter_chunks`
    inside the request handler, which re-resolves the attribute on the module
    each call — so patching the module attribute is sufficient.
    """
    def fake_align(chunk_paths, project_id, chapter_id, source_lang,
                   target_lang, output_path):
        payload = {
            "chapter_id": chapter_id,
            "project_id": project_id,
            "en_count": len(new_pairs),
            "es_count": len(new_pairs),
            "high_confidence_pct": 100.0,
            "avg_similarity": 0.95,
            "alignments": new_pairs,
        }
        Path(output_path).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8",
        )
        return {"pairs": new_pairs}

    monkeypatch.setattr(sentence_aligner_module, "align_chapter_chunks", fake_align)


def _read_annotations(proj_dir: Path) -> list[dict]:
    path = proj_dir / "annotations.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------- input validation ----------


class TestRealignValidation:
    def test_bad_project_id_returns_400(self, client):
        rv = client.post("/api/project/..bad/align/chapter_01")
        assert rv.status_code == 400

    def test_bad_chapter_id_returns_400(self, client):
        rv = client.post("/api/project/test-project/align/..bad")
        assert rv.status_code == 400

    def test_no_chunks_returns_404(self, client, project, monkeypatch):
        # Remove the only chunk so the glob matches nothing.
        for f in (project / "chunks").glob("*.json"):
            f.unlink()
        rv = client.post("/api/project/test-project/align/chapter_01")
        assert rv.status_code == 404


# ---------- happy paths ----------


class TestRealignHappyPath:
    def test_fresh_align_no_prior_alignment_no_orphans(
        self, client, project, monkeypatch,
    ):
        """First-ever align: no old map, no annotations → no re-anchor work."""
        new_pairs = [
            {"es_idx": 0, "en_idx": 0, "es": "El gato se sentó.",
             "en": "The cat sat.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
            {"es_idx": 1, "en_idx": 1, "es": "El perro ladró.",
             "en": "The dog barked.", "similarity": 0.92, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
        ]
        _patch_aligner(monkeypatch, new_pairs)

        rv = client.post("/api/project/test-project/align/chapter_01")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert data["pairs"] == 2
        assert data["orphaned_annotations"] == 0
        # No annotations.jsonl should be created when there are no annotations.
        assert not (project / "annotations.jsonl").exists()

    def test_realign_with_no_annotations_no_orphans(
        self, client, project, monkeypatch,
    ):
        """Prior alignment exists but no annotations → no re-anchor work."""
        _write_alignment(project / "alignments", "chapter_01", [
            {"es_idx": 0, "en_idx": 0, "es": "El gato se sentó.",
             "en": "The cat sat.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
        ])
        _patch_aligner(monkeypatch, [
            {"es_idx": 0, "en_idx": 0, "es": "El gato se sentó.",
             "en": "The cat sat.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
        ])

        rv = client.post("/api/project/test-project/align/chapter_01")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert data["orphaned_annotations"] == 0
        assert not (project / "annotations.jsonl").exists()

    def test_realign_stable_idx_does_not_append_rows(
        self, client, project, monkeypatch,
    ):
        """When es_idx is unchanged after realign, no new annotation rows are written."""
        _write_alignment(project / "alignments", "chapter_01", [
            {"es_idx": 0, "en_idx": 0, "es": "El gato se sentó.",
             "en": "The cat sat.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
            {"es_idx": 1, "en_idx": 1, "es": "El perro ladró.",
             "en": "The dog barked.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
        ])
        # Pre-existing annotation on es_idx 1
        annotation = {
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "es_idx": 1,
            "type": "flag",
            "content": "needs review",
            "timestamp": "2026-04-30T12:00:00",
        }
        (project / "annotations.jsonl").write_text(
            json.dumps(annotation, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        # New alignment keeps the same es_idx → es text mapping
        _patch_aligner(monkeypatch, [
            {"es_idx": 0, "en_idx": 0, "es": "El gato se sentó.",
             "en": "The cat sat.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
            {"es_idx": 1, "en_idx": 1, "es": "El perro ladró.",
             "en": "The dog barked.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
        ])

        rv = client.post("/api/project/test-project/align/chapter_01")
        assert rv.status_code == 200
        assert rv.get_json()["orphaned_annotations"] == 0

        rows = _read_annotations(project)
        assert len(rows) == 1, "no new rows should be appended when idx is stable"
        assert rows[0]["es_idx"] == 1
        assert rows[0]["content"] == "needs review"


# ---------- re-anchor / orphan paths ----------


class TestRealignReanchor:
    def test_shifted_idx_reanchored_to_new_idx(
        self, client, project, monkeypatch,
    ):
        """When the same es text moves to a new es_idx, the annotation
        is re-anchored: a removed=True row is appended for the old idx and a
        recreate row is appended for the new idx."""
        _write_alignment(project / "alignments", "chapter_01", [
            {"es_idx": 0, "en_idx": 0, "es": "El gato se sentó.",
             "en": "The cat sat.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
            {"es_idx": 1, "en_idx": 1, "es": "El perro ladró.",
             "en": "The dog barked.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
        ])
        # Annotate "El perro ladró." at old idx 1
        annotation = {
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "es_idx": 1,
            "type": "flag",
            "content": "loud dog",
            "timestamp": "2026-04-30T12:00:00",
        }
        (project / "annotations.jsonl").write_text(
            json.dumps(annotation, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        # Realigner inserts a new sentence at idx 0, shifting "El perro ladró."
        # from idx 1 → idx 2.
        _patch_aligner(monkeypatch, [
            {"es_idx": 0, "en_idx": 0, "es": "Era de noche.",
             "en": "It was night.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
            {"es_idx": 1, "en_idx": 1, "es": "El gato se sentó.",
             "en": "The cat sat.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
            {"es_idx": 2, "en_idx": 2, "es": "El perro ladró.",
             "en": "The dog barked.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
        ])

        rv = client.post("/api/project/test-project/align/chapter_01")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["pairs"] == 3
        assert data["orphaned_annotations"] == 0

        # Original row + remove-old + recreate-new
        rows = _read_annotations(project)
        assert len(rows) == 3
        assert rows[0]["es_idx"] == 1 and rows[0].get("removed") is not True
        assert rows[1]["es_idx"] == 1 and rows[1]["removed"] is True
        assert rows[2]["es_idx"] == 2
        assert rows[2]["content"] == "loud dog"
        assert rows[2]["type"] == "flag"

        # Effective view (latest per es_idx, with removals applied) should
        # show exactly one annotation, anchored at the new idx 2.
        from web_ui.app import _load_annotations
        effective = _load_annotations(project, "chapter_01")
        assert list(effective.keys()) == [2]
        assert effective[2]["content"] == "loud dog"

    def test_prefix_fallback_match_reanchors(
        self, client, project, monkeypatch,
    ):
        """When the exact es text changed slightly, the 30-char prefix fallback
        still anchors the annotation to the rewritten sentence."""
        old_text = "El perro ladró fuerte en el jardín trasero por la mañana."
        new_text = "El perro ladró fuerte en el jardín delantero a las cinco."
        # Sanity check: the 30-char prefix used by the fallback must match.
        assert new_text.startswith(old_text[:30])

        _write_alignment(project / "alignments", "chapter_01", [
            {"es_idx": 0, "en_idx": 0, "es": old_text,
             "en": "The dog barked loudly in the backyard.",
             "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
        ])
        annotation = {
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "es_idx": 0,
            "type": "comment",
            "content": "tone check",
            "timestamp": "2026-04-30T12:00:00",
        }
        (project / "annotations.jsonl").write_text(
            json.dumps(annotation, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _patch_aligner(monkeypatch, [
            {"es_idx": 0, "en_idx": 0, "es": "Otra oración inicial.",
             "en": "Another opening sentence.",
             "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
            {"es_idx": 1, "en_idx": 1, "es": new_text,
             "en": "The dog barked loudly in the garden.",
             "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
        ])

        rv = client.post("/api/project/test-project/align/chapter_01")
        assert rv.status_code == 200
        assert rv.get_json()["orphaned_annotations"] == 0

        from web_ui.app import _load_annotations
        effective = _load_annotations(project, "chapter_01")
        assert list(effective.keys()) == [1]
        assert effective[1]["content"] == "tone check"

    def test_unmatchable_annotation_is_orphaned(
        self, client, project, monkeypatch,
    ):
        """When neither exact match nor prefix fallback finds the old sentence
        in the new alignment, the annotation is reported as orphaned and
        no recreate row is written."""
        _write_alignment(project / "alignments", "chapter_01", [
            {"es_idx": 0, "en_idx": 0, "es": "El gato se sentó.",
             "en": "The cat sat.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
            {"es_idx": 1, "en_idx": 1,
             "es": "Una oración completamente única que ya no existe.",
             "en": "A completely unique sentence that no longer exists.",
             "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
        ])
        annotation = {
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "es_idx": 1,
            "type": "flag",
            "content": "this will be orphaned",
            "timestamp": "2026-04-30T12:00:00",
        }
        (project / "annotations.jsonl").write_text(
            json.dumps(annotation, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        # New alignment drops the annotated sentence entirely.
        _patch_aligner(monkeypatch, [
            {"es_idx": 0, "en_idx": 0, "es": "El gato se sentó.",
             "en": "The cat sat.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
        ])

        rv = client.post("/api/project/test-project/align/chapter_01")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["pairs"] == 1
        assert data["orphaned_annotations"] == 1

        # Only the original row should remain — no remove/recreate appended.
        rows = _read_annotations(project)
        assert len(rows) == 1
        assert rows[0]["content"] == "this will be orphaned"

    def test_reanchor_failure_swallowed_returns_zero_orphans(
        self, client, project, monkeypatch,
    ):
        """If the re-anchor helper raises, the endpoint logs a warning,
        reports orphaned_annotations=0, and still returns 200."""
        _write_alignment(project / "alignments", "chapter_01", [
            {"es_idx": 0, "en_idx": 0, "es": "El gato se sentó.",
             "en": "The cat sat.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
        ])
        _patch_aligner(monkeypatch, [
            {"es_idx": 0, "en_idx": 0, "es": "El gato se sentó.",
             "en": "The cat sat.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
        ])

        import web_ui.app as app_module

        def boom(*args, **kwargs):
            raise RuntimeError("synthetic re-anchor failure")

        monkeypatch.setattr(
            app_module, "_reanchor_annotations_after_realign", boom,
        )

        rv = client.post("/api/project/test-project/align/chapter_01")
        assert rv.status_code == 200
        assert rv.get_json()["orphaned_annotations"] == 0


# ---------- error path ----------


class TestRealignErrors:
    def test_aligner_exception_returns_500(self, client, project, monkeypatch):
        def fake(*a, **kw):
            raise RuntimeError("alignment exploded")

        monkeypatch.setattr(
            sentence_aligner_module, "align_chapter_chunks", fake,
        )

        rv = client.post("/api/project/test-project/align/chapter_01")
        assert rv.status_code == 500
        assert "alignment exploded" in rv.get_json()["error"]
