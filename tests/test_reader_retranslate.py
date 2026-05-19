"""Endpoint tests for the reader sentence-retranslate flow."""

import json
import sys
from pathlib import Path
from typing import Optional

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


def _project_with_translated_text(
    tmp_path,
    monkeypatch,
    *,
    source_text: str,
    translated_text: str,
    chapter_text: Optional[str] = None,
):
    """Factory for /api/sentence/replace tests that need custom chunk text.

    Builds a single-chapter, single-chunk project with the given source +
    translated text. ``chapter_text`` defaults to ``source_text`` (matches the
    existing fixture's convention). Returns the project dir.
    """
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
        source_text,
        translated_text,
    )
    save_chunk(chunk, chunks_dir / "chapter_01_chunk_000.json")

    # Minimal alignment file — endpoint doesn't read it for /replace, but
    # _apply_chunk_edits' pipeline expects the file to exist.
    (align_dir / "chapter_01.json").write_text(
        json.dumps({
            "chapter_id": "chapter_01",
            "project_id": "test-project",
            "en_count": 0, "es_count": 0,
            "high_confidence_pct": 0.0, "avg_similarity": 0.0,
            "alignments": [],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (chapters_dir / "chapter_01.txt").write_text(
        chapter_text if chapter_text is not None else source_text,
        encoding="utf-8",
    )

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


@pytest.fixture
def project_with_duplicate_translation(tmp_path, monkeypatch):
    """Project whose chunk has 'Está bien.' twice — exercises the find()
    ambiguity at the heart of the codex finding.
    """
    return _project_with_translated_text(
        tmp_path, monkeypatch,
        source_text="Okay. Okay.",
        translated_text="Está bien. Está bien.",
    )


@pytest.fixture
def project_with_image_alt_duplicate(tmp_path, monkeypatch):
    """Project whose chunk has an [IMAGE:...] caption whose alt text matches
    the body sentence — the the-story-of-nelson scenario.
    """
    # Note: [IMAGE:...] placeholder sits inside translated_text in real
    # projects, embedded by the source-text builder when extracting from
    # epub. Source and translated mirror the structure.
    return _project_with_translated_text(
        tmp_path, monkeypatch,
        source_text="[IMAGE:images/i010.jpg:Okay.]\n\nOkay.",
        translated_text="[IMAGE:images/i010.jpg:Está bien.]\n\nEstá bien.",
    )


@pytest.fixture
def project_with_triple_translation(tmp_path, monkeypatch):
    """Project whose chunk has 'Sí.' three times — proves offsets pick the
    exact occurrence, not just 'always the second'.
    """
    return _project_with_translated_text(
        tmp_path, monkeypatch,
        source_text="Yes. Yes. Yes.",
        translated_text="Sí. Sí. Sí.",
    )


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
            "chunk_offset_start": 0,
            "chunk_offset_end": len("El gato se sentó."),
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
        # Offsets recorded for forensic debugging of bad replacements.
        assert record["chunk_offset_start"] == 0
        assert record["chunk_offset_end"] == len("El gato se sentó.")


# -------- /api/sentence/replace: chunk-offset resolution --------

def _load_translated(project_dir: Path, chunk_id: str = "chapter_01_chunk_000") -> str:
    from src.utils.file_io import load_chunk
    return load_chunk(project_dir / "chunks" / f"{chunk_id}.json").translated_text


class TestReplaceUsesChunkOffsets:
    """Regression coverage for the codex finding and the the-story-of-nelson
    image-caption corruption: /api/sentence/replace must anchor the span via
    client-supplied chunk offsets when current_translation appears more than
    once in the chunk.
    """

    def test_offsets_select_exact_span(self, client, project_with_duplicate_translation):
        """Tier 1: duplicate body sentence; offsets pick the second one."""
        # translated_text = "Está bien. Está bien." — len 10 of "Está bien.",
        # second occurrence starts at index 11 (after ". ").
        second_start = len("Está bien. ")
        second_end = second_start + len("Está bien.")
        rv = client.post("/api/sentence/replace", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "current_translation": "Está bien.",
            "new_translation": "Vale.",
            "es_idx": 1,
            "chunk_offset_start": second_start,
            "chunk_offset_end": second_end,
        })
        assert rv.status_code == 200, rv.get_json()
        result = _load_translated(project_with_duplicate_translation)
        # First occurrence untouched, second replaced.
        assert result == "Está bien. Vale."

    def test_image_alt_text_preserved(self, client, project_with_image_alt_duplicate):
        """The the-story-of-nelson scenario: image caption alt text matches
        the body sentence. Without offsets, find() would corrupt the caption.
        """
        original = "[IMAGE:images/i010.jpg:Está bien.]\n\nEstá bien."
        # The body sentence is the *last* "Está bien." in the chunk.
        body_start = original.rfind("Está bien.")
        # Sanity: there really are two occurrences and the body one is later.
        assert original.find("Está bien.") < body_start
        body_end = body_start + len("Está bien.")

        rv = client.post("/api/sentence/replace", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "current_translation": "Está bien.",
            "new_translation": "« Está bien. »",
            "chunk_offset_start": body_start,
            "chunk_offset_end": body_end,
        })
        assert rv.status_code == 200, rv.get_json()
        result = _load_translated(project_with_image_alt_duplicate)
        # Caption alt text MUST stay as "Está bien." — no «» bleed.
        assert "[IMAGE:images/i010.jpg:Está bien.]" in result
        # Body sentence now wrapped in guillemets.
        assert result.endswith("« Está bien. »")

    def test_three_occurrences_offsets_pick_middle(self, client, project_with_triple_translation):
        """Strong correctness test: 'Sí. Sí. Sí.' — pick the middle one."""
        # offsets for the middle "Sí."
        middle_start = len("Sí. ")
        middle_end = middle_start + len("Sí.")
        rv = client.post("/api/sentence/replace", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "current_translation": "Sí.",
            "new_translation": "Claro.",
            "chunk_offset_start": middle_start,
            "chunk_offset_end": middle_end,
        })
        assert rv.status_code == 200, rv.get_json()
        assert _load_translated(project_with_triple_translation) == "Sí. Claro. Sí."

    @pytest.mark.parametrize("bad_offsets", [
        # Out-of-bounds: past end of chunk.
        {"chunk_offset_start": 9999, "chunk_offset_end": 10009},
        # Negative start.
        {"chunk_offset_start": -1, "chunk_offset_end": 9},
        # Non-integer (e.g. JSON sent strings by mistake).
        {"chunk_offset_start": "0", "chunk_offset_end": "10"},
        # Length mismatch: span doesn't match current_translation length.
        {"chunk_offset_start": 0, "chunk_offset_end": 5},
        # Booleans masquerading as ints (isinstance(True, int) is True).
        {"chunk_offset_start": True, "chunk_offset_end": False},
    ])
    def test_invalid_offsets_fall_through_cleanly(
        self, client, project_with_chunk, bad_offsets,
    ):
        """Bad offsets must not raise — they fall through to Tier 2 or Tier 3
        without error. Cases where has_offset_hint is False (out-of-bounds,
        negative, non-int, bool) go directly to Tier 3. The length-mismatch
        case has a valid start so has_offset_hint=True and goes to Tier 2
        (anchored find), then Tier 3 if that misses. Either way the fixture's
        chunk has exactly one match for 'El gato se sentó.', so the
        replacement succeeds regardless of which tier handles it.
        """
        payload = {
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "current_translation": "El gato se sentó.",
            "new_translation": "El felino se sentó.",
        }
        payload.update(bad_offsets)
        rv = client.post("/api/sentence/replace", json=payload)
        assert rv.status_code == 200, (bad_offsets, rv.get_json())
        result = _load_translated(project_with_chunk)
        assert result.startswith("El felino se sentó.")

    def test_offset_mismatch_uses_anchored_find(self, client, project_with_duplicate_translation):
        """Tier 2: hint is off (straddles separator) so the Tier 1 slice
        check fails, but anchored find() from hint_start recovers the correct
        second occurrence.
        """
        # The hint points one byte before the second "Está bien." — the slice
        # [10:20] spans ". Está bie" which doesn't equal "Está bien.", so
        # Tier 1 rejects it. Tier 2 runs find("Está bien.", 10) and lands at
        # index 11 (the second occurrence).
        rv = client.post("/api/sentence/replace", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "current_translation": "Está bien.",
            "new_translation": "Vale.",
            # Offset points midway into ". " separator — slice won't match
            # current_translation, but Tier 2 anchored find will recover.
            "chunk_offset_start": len("Está bien. ") - 1,
            "chunk_offset_end": len("Está bien. ") - 1 + len("Está bien."),
        })
        assert rv.status_code == 200, rv.get_json()
        # Anchored find from hint_start=10 lands at index 11 (the second
        # occurrence). First "Está bien." preserved.
        assert _load_translated(project_with_duplicate_translation) == "Está bien. Vale."

    def test_valid_offsets_string_not_found_returns_422(self, client, project_with_chunk):
        """Tier 1 fails, Tier 2 anchored find misses, Tier 3 plain find misses
        → 422. Offsets are well-formed ints but the target string is absent.
        """
        rv = client.post("/api/sentence/replace", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "current_translation": "Esta frase no existe.",
            "new_translation": "Reemplazo.",
            "chunk_offset_start": 0,
            "chunk_offset_end": len("Esta frase no existe."),
        })
        assert rv.status_code == 422
        assert "Cannot locate" in rv.get_json()["error"]

    def test_tier3_audit_log_records_server_resolved_offsets(self, client, project_with_chunk):
        """Tier 3 (no client offsets): audit log records the start/end the
        server resolved via plain find(), not None.
        """
        rv = client.post("/api/sentence/replace", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "current_translation": "El gato se sentó.",
            "new_translation": "El felino se sentó.",
            "es_idx": 0,
        })
        assert rv.status_code == 200
        log_path = project_with_chunk / "retranslations.jsonl"
        record = json.loads(log_path.read_text(encoding="utf-8").strip().split("\n")[-1])
        assert record["chunk_offset_start"] == 0
        assert record["chunk_offset_end"] == len("El gato se sentó.")

    def test_no_offsets_legacy_behavior(self, client, project_with_chunk):
        """Tier 3: old clients that don't send offsets still work."""
        rv = client.post("/api/sentence/replace", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "current_translation": "El gato se sentó.",
            "new_translation": "El felino se sentó.",
        })
        assert rv.status_code == 200, rv.get_json()
        result = _load_translated(project_with_chunk)
        assert result.startswith("El felino se sentó.")


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
