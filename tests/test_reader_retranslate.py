"""Endpoint tests for the reader sentence-retranslate flow."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web_ui.app import app, _attach_text_in_chunk
from src.models import (
    Chunk,
    ChunkMetadata,
    ChunkStatus,
    StyleGuide,
)
from src.utils.file_io import save_chunk, save_style_guide


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


@pytest.fixture
def project_with_chunk(tmp_path, monkeypatch):
    """Create a project with one chapter, one chunk, and an alignment file."""
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
        "The cat sat. The dog barked.",
        "El gato se sentó. El perro ladró.",
    )
    save_chunk(chunk, chunks_dir / "chapter_01_chunk_000.json")

    alignment = {
        "chapter_id": "chapter_01",
        "project_id": "test-project",
        "en_count": 2,
        "es_count": 2,
        "high_confidence_pct": 100.0,
        "avg_similarity": 0.9,
        "alignments": [
            {
                "es_idx": 0, "en_idx": 0,
                "es": "El gato se sentó.", "en": "The cat sat.",
                "similarity": 0.95, "confidence": "high",
                "chunk_id": "chapter_01_chunk_000",
            },
            {
                "es_idx": 1, "en_idx": 1,
                "es": "El perro ladró.", "en": "The dog barked.",
                "similarity": 0.92, "confidence": "high",
                "chunk_id": "chapter_01_chunk_000",
            },
        ],
    }
    (align_dir / "chapter_01.json").write_text(
        json.dumps(alignment, ensure_ascii=False), encoding="utf-8"
    )
    (chapters_dir / "chapter_01.txt").write_text(
        "The cat sat. The dog barked.", encoding="utf-8"
    )

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


@pytest.fixture
def project_with_style_guide(project_with_chunk):
    style = StyleGuide(content="Use formal Spanish.", version="1.0")
    save_style_guide(style, project_with_chunk / "style.json")
    return project_with_chunk


# -------- /api/sentence/retranslate --------

class TestRetranslateEndpoint:
    def test_missing_source_text_returns_400(self, client, project_with_chunk):
        rv = client.post("/api/sentence/retranslate", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
        })
        assert rv.status_code == 400

    def test_missing_project_returns_404(self, client, project_with_chunk):
        rv = client.post("/api/sentence/retranslate", json={
            "project_id": "nonexistent",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "source_text": "The cat sat.",
        })
        assert rv.status_code == 404

    def test_chunk_chapter_mismatch_returns_400(self, client, project_with_chunk):
        rv = client.post("/api/sentence/retranslate", json={
            "project_id": "test-project",
            "chapter_id": "chapter_99",
            "chunk_id": "chapter_01_chunk_000",
            "source_text": "The cat sat.",
        })
        assert rv.status_code == 400

    def test_mtime_mismatch_returns_409(self, client, project_with_chunk):
        rv = client.post("/api/sentence/retranslate", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "source_text": "The cat sat.",
            "expected_chunk_mtime": 1.0,  # stale
        })
        assert rv.status_code == 409

    def test_invalid_mtime_returns_400(self, client, project_with_chunk):
        rv = client.post("/api/sentence/retranslate", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "source_text": "The cat sat.",
            "expected_chunk_mtime": "not-a-number",
        })
        assert rv.status_code == 400

    def test_nan_mtime_rejected_400(self, client, project_with_chunk):
        # Regression for codex finding #1: NaN bypassed the mtime check because
        # abs(nan - current) > 1e-6 is always False.
        rv = client.post("/api/sentence/retranslate", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "source_text": "The cat sat.",
            "expected_chunk_mtime": float("nan"),
        })
        assert rv.status_code == 400

    def test_inf_mtime_rejected_400(self, client, project_with_chunk):
        rv = client.post("/api/sentence/retranslate", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "source_text": "The cat sat.",
            "expected_chunk_mtime": float("inf"),
        })
        assert rv.status_code == 400

    def test_oversized_source_text_returns_413(self, client, project_with_chunk):
        # Regression for codex finding #8: cap source_text at 8KB.
        rv = client.post("/api/sentence/retranslate", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "source_text": "x" * (8 * 1024 + 1),
        })
        assert rv.status_code == 413

    def test_oversized_context_text_returns_413(self, client, project_with_chunk):
        rv = client.post("/api/sentence/retranslate", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "source_text": "The cat sat.",
            "context_text": "y" * (16 * 1024 + 1),
        })
        assert rv.status_code == 413

    def test_retranslation_error_returns_502(self, client, project_with_style_guide, monkeypatch):
        from src import retranslator

        def boom(*a, **kw):
            raise retranslator.RetranslationError("LLM empty")

        import web_ui.app as app_module
        monkeypatch.setattr(app_module, "retranslate_sentence", boom, raising=False)
        # also patch the import-time symbol used inside the endpoint
        monkeypatch.setattr(retranslator, "retranslate_sentence", boom)

        rv = client.post("/api/sentence/retranslate", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "source_text": "The cat sat.",
        })
        assert rv.status_code == 502

    def test_value_error_returns_400(self, client, project_with_style_guide, monkeypatch):
        from src import retranslator
        monkeypatch.setattr(retranslator, "retranslate_sentence",
                            lambda *a, **kw: (_ for _ in ()).throw(ValueError("bad input")))

        rv = client.post("/api/sentence/retranslate", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "source_text": "The cat sat.",
        })
        assert rv.status_code == 400

    def test_success_returns_translation(self, client, project_with_style_guide, monkeypatch):
        from src import retranslator
        from src.models import RetranslationResult

        def fake(*a, **kw):
            return RetranslationResult(
                new_translation="El felino se sentó.",
                model="claude-sonnet-4-6",
                provider="anthropic",
                prompt_tokens=120,
                completion_tokens=8,
                cost_usd=0.001,
                raw_response="El felino se sentó.",
            )

        monkeypatch.setattr(retranslator, "retranslate_sentence", fake)

        rv = client.post("/api/sentence/retranslate", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "source_text": "The cat sat.",
        })
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert data["new_translation"] == "El felino se sentó."
        assert data["provider"] == "anthropic"


# -------- /api/sentence/replace --------

class TestReplaceEndpoint:
    def test_missing_current_returns_400(self, client, project_with_chunk):
        rv = client.post("/api/sentence/replace", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "new_translation": "El felino se sentó.",
        })
        assert rv.status_code == 400

    def test_empty_new_returns_400(self, client, project_with_chunk):
        rv = client.post("/api/sentence/replace", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "current_translation": "El gato se sentó.",
            "new_translation": "   ",
        })
        assert rv.status_code == 400

    def test_oversized_new_translation_returns_413(self, client, project_with_chunk):
        # Regression for codex finding #8: cap new_translation at 32KB.
        rv = client.post("/api/sentence/replace", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "current_translation": "El gato se sentó.",
            "new_translation": "z" * (32 * 1024 + 1),
        })
        assert rv.status_code == 413

    def test_source_not_found_returns_422(self, client, project_with_chunk):
        rv = client.post("/api/sentence/replace", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "current_translation": "Sentence that is not in the chunk.",
            "new_translation": "Una nueva traducción.",
        })
        assert rv.status_code == 422

    def test_success_writes_audit_log(self, client, project_with_chunk):
        rv = client.post("/api/sentence/replace", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "current_translation": "El gato se sentó.",
            "new_translation": "El felino se sentó.",
            "es_idx": 0,
        })
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert "chunk_mtime" in data
        log_path = project_with_chunk / "retranslations.jsonl"
        assert log_path.exists()
        record = json.loads(log_path.read_text(encoding="utf-8").strip().split("\n")[-1])
        assert record["new_translation"] == "El felino se sentó."
        assert record["es_idx"] == 0


# -------- /api/setup/<id>/style-guide/light --------

class TestLightStyleGuideEndpoint:
    def test_no_main_style_returns_404(self, client, project_with_chunk):
        rv = client.post(
            "/api/setup/test-project/style-guide/light",
            json={"light_content": "Short guide."},
        )
        assert rv.status_code == 404

    def test_set_and_clear(self, client, project_with_style_guide):
        rv = client.post(
            "/api/setup/test-project/style-guide/light",
            json={"light_content": "Use vos in dialogue."},
        )
        assert rv.status_code == 200
        assert rv.get_json()["light_content"] == "Use vos in dialogue."

        # Clear with empty content
        rv2 = client.post(
            "/api/setup/test-project/style-guide/light",
            json={"light_content": ""},
        )
        assert rv2.status_code == 200
        assert rv2.get_json()["light_content"] == ""

    def test_bad_project_id_returns_400(self, client, project_with_style_guide):
        rv = client.post(
            "/api/setup/..%2F..%2Fetc/style-guide/light",
            json={"light_content": "x"},
        )
        # _safe_id rejects path-traversal-style ids
        assert rv.status_code in (400, 404)


# -------- /api/llm/models --------

class TestLLMModelsEndpoint:
    def test_returns_models_payload(self, client):
        rv = client.get("/api/llm/models")
        assert rv.status_code == 200
        data = rv.get_json()
        assert "models" in data
        assert "default_model" in data
        assert isinstance(data["models"], list)


# -------- _attach_text_in_chunk enricher --------

class TestAttachTextInChunk:
    def test_split_match_attaches_text_and_offsets(self, tmp_path):
        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        chunk = _make_chunk(
            "chapter_01_chunk_000",
            "chapter_01",
            "The cat sat. The dog barked.",
            "El gato se sentó. El perro ladró.",
        )
        save_chunk(chunk, chunks_dir / "chapter_01_chunk_000.json")

        alignment = {
            "alignments": [
                {
                    "es_idx": 0, "en_idx": 0,
                    "es": "El gato se sentó.", "en": "The cat sat.",
                    "chunk_id": "chapter_01_chunk_000",
                },
                {
                    "es_idx": 1, "en_idx": 1,
                    "es": "El perro ladró.", "en": "The dog barked.",
                    "chunk_id": "chapter_01_chunk_000",
                },
            ],
        }
        _attach_text_in_chunk(alignment, chunks_dir, target_lang="es")

        rows = alignment["alignments"]
        assert all("text_in_chunk" in r for r in rows)
        assert all("chunk_offset_start" in r and "chunk_offset_end" in r for r in rows)
        assert all("chunk_mtime" in r for r in rows)
        # Verify offsets actually slice back to text_in_chunk
        chunk_text = (chunks_dir / "chapter_01_chunk_000.json").read_text(encoding="utf-8")
        # We compare against the chunk's translated_text instead, since offsets are
        # against translated_text not the JSON file.
        from src.utils.file_io import load_chunk
        loaded = load_chunk(chunks_dir / "chapter_01_chunk_000.json")
        for r in rows:
            assert loaded.translated_text[r["chunk_offset_start"]:r["chunk_offset_end"]] == r["text_in_chunk"]

    def test_missing_chunk_file_skips_silently(self, tmp_path):
        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        alignment = {
            "alignments": [
                {
                    "es_idx": 0, "en_idx": 0,
                    "es": "El gato.", "en": "The cat.",
                    "chunk_id": "chapter_01_chunk_999",
                },
            ],
        }
        # No chunk file present — should not raise
        _attach_text_in_chunk(alignment, chunks_dir, target_lang="es")
        # Row should be unchanged or have None offsets
        row = alignment["alignments"][0]
        assert row.get("text_in_chunk") in (None, "")
