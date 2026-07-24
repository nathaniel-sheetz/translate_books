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
from web_ui.app import app, _apply_pending_corrections_for_chapter
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
    def test_bad_project_id_returns_404(self, client):
        # "..bad" isn't the traversal segment ".." — just an ordinary (if
        # unusual) name — so it now passes _safe_id and 404s as not-found,
        # same as any other nonexistent project id.
        rv = client.post("/api/project/..bad/align/chapter_01")
        assert rv.status_code == 404

    def test_bad_chapter_id_returns_404(self, client):
        rv = client.post("/api/project/test-project/align/..bad")
        assert rv.status_code == 404

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
        assert len(effective[2]) == 1
        assert effective[2][0]["content"] == "loud dog"

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
        assert len(effective[1]) == 1
        assert effective[1][0]["content"] == "tone check"

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


# ---------- pending corrections ----------


class TestRealignAppliesPendingCorrections:
    """Realign must apply queued bottom-sheet corrections to chunks first.

    Bottom-sheet "Save" writes to corrections.jsonl and patches the
    alignment in-place but does not touch chunks. If realign runs against
    unpatched chunks, the regenerated alignment overwrites the user's
    edit. Realign must apply pending corrections to the chunks first.
    """

    def test_pending_correction_applied_to_chunk_then_realigned(
        self, client, project, monkeypatch,
    ):
        # Existing alignment (matches the unedited chunk).
        _write_alignment(project / "alignments", "chapter_01", [
            {"es_idx": 0, "en_idx": 0, "es": "El gato se sentó.",
             "en": "The cat sat.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000",
             "chunk_offset_start": 0, "chunk_offset_end": 17},
        ])
        # Queue a correction (what bottom-sheet Save would write).
        chunk_text = "El gato se sentó. El perro ladró. El pájaro voló."
        original_es = "El gato se sentó."
        corrected_es = "El gato se sentó tranquilo."
        correction = {
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "es_idx": 0,
            "original_es": original_es,
            "corrected_es": corrected_es,
            "en_reference": "The cat sat.",
            "chunk_offset_start": 0,
            "chunk_offset_end": len(original_es),
            "timestamp": "2026-05-20T12:00:00",
        }
        (project / "corrections.jsonl").write_text(
            json.dumps(correction, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        # Aligner echoes back whatever's in the chunk now; check it sees
        # the corrected text.
        captured: dict = {}

        def fake_align(chunk_paths, project_id, chapter_id, source_lang,
                       target_lang, output_path):
            from src.utils.file_io import load_chunk as _lc
            ch = _lc(Path(chunk_paths[0]))
            captured["translated_text"] = ch.translated_text
            payload = {
                "chapter_id": chapter_id,
                "project_id": project_id,
                "en_count": 1,
                "es_count": 1,
                "high_confidence_pct": 100.0,
                "avg_similarity": 0.95,
                "alignments": [{"es_idx": 0, "en_idx": 0,
                                "es": corrected_es,
                                "en": "The cat sat.",
                                "similarity": 0.95, "confidence": "high",
                                "chunk_id": "chapter_01_chunk_000"}],
            }
            Path(output_path).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8",
            )
            return {"pairs": payload["alignments"]}

        monkeypatch.setattr(sentence_aligner_module, "align_chapter_chunks", fake_align)

        rv = client.post("/api/project/test-project/align/chapter_01")
        assert rv.status_code == 200, rv.get_json()
        data = rv.get_json()
        assert data["corrections_applied"] == 1

        # Chunk on disk now has the corrected text.
        from src.utils.file_io import load_chunk
        updated = load_chunk(project / "chunks" / "chapter_01_chunk_000.json")
        assert corrected_es in updated.translated_text
        assert original_es not in updated.translated_text
        # Aligner observed the corrected text.
        assert corrected_es in captured["translated_text"]
        # corrections.jsonl is cleared; archive has the row with applied_at.
        assert not (project / "corrections.jsonl").exists()
        archive = project / "corrections_applied.jsonl"
        assert archive.exists()
        archived_rows = [
            json.loads(line)
            for line in archive.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(archived_rows) == 1
        assert archived_rows[0]["corrected_es"] == corrected_es
        assert "applied_at" in archived_rows[0]
        assert archived_rows[0]["status"] == "applied"
        # Untouched chunk text in other positions should be preserved.
        assert "El perro ladró" in updated.translated_text

    def test_other_chapter_corrections_preserved(
        self, client, project, monkeypatch,
    ):
        """Corrections targeting other chapters must remain in
        corrections.jsonl after a per-chapter realign."""
        rows = [
            {
                "project_id": "test-project",
                "chapter_id": "chapter_01",
                "chunk_id": "chapter_01_chunk_000",
                "es_idx": 0,
                "original_es": "El gato se sentó.",
                "corrected_es": "El gato se sentó tranquilo.",
                "en_reference": "The cat sat.",
                "chunk_offset_start": 0,
                "chunk_offset_end": 17,
                "timestamp": "2026-05-20T12:00:00",
            },
            {
                "project_id": "test-project",
                "chapter_id": "chapter_02",
                "chunk_id": "chapter_02_chunk_000",
                "es_idx": 0,
                "original_es": "Otra frase.",
                "corrected_es": "Otra frase mejor.",
                "en_reference": "Another sentence.",
                "timestamp": "2026-05-20T12:00:00",
            },
        ]
        (project / "corrections.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8",
        )

        _patch_aligner(monkeypatch, [
            {"es_idx": 0, "en_idx": 0, "es": "El gato se sentó tranquilo.",
             "en": "The cat sat.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
        ])

        rv = client.post("/api/project/test-project/align/chapter_01")
        assert rv.status_code == 200
        assert rv.get_json()["corrections_applied"] == 1

        # chapter_02 row is still queued.
        remaining = [
            json.loads(line)
            for line in (project / "corrections.jsonl")
            .read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(remaining) == 1
        assert remaining[0]["chapter_id"] == "chapter_02"

    def test_no_pending_corrections_reports_zero(
        self, client, project, monkeypatch,
    ):
        """No corrections.jsonl → corrections_applied is 0; no archive
        file is created."""
        _patch_aligner(monkeypatch, [
            {"es_idx": 0, "en_idx": 0, "es": "El gato se sentó.",
             "en": "The cat sat.", "similarity": 0.95, "confidence": "high",
             "chunk_id": "chapter_01_chunk_000"},
        ])

        rv = client.post("/api/project/test-project/align/chapter_01")
        assert rv.status_code == 200
        assert rv.get_json()["corrections_applied"] == 0
        assert not (project / "corrections_applied.jsonl").exists()


# ---------- pending corrections edge cases (direct function tests) ----------


class TestApplyPendingCorrectionsEdgeCases:
    """Edge-case paths in _apply_pending_corrections_for_chapter.

    Tested via direct function calls so error paths don't collide with the
    surrounding project_align route logic (e.g. load_chunk also used later
    in the same route handler).
    """

    def _setup_project(self, tmp_path):
        proj_dir = tmp_path / "projects" / "test-project"
        chunks_dir = proj_dir / "chunks"
        chunks_dir.mkdir(parents=True)
        chunk = _make_chunk(
            "chapter_01_chunk_000", "chapter_01",
            "The cat sat.",
            "El gato se sentó.",
        )
        save_chunk(chunk, chunks_dir / "chapter_01_chunk_000.json")
        return proj_dir

    def test_malformed_json_line_is_skipped(self, tmp_path):
        """A corrupt line in corrections.jsonl is ignored; valid rows still apply."""
        proj_dir = self._setup_project(tmp_path)
        valid = {
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "es_idx": 0,
            "original_es": "El gato se sentó.",
            "corrected_es": "El gato tranquilo.",
            "en_reference": "The cat sat.",
            "chunk_offset_start": 0,
            "chunk_offset_end": 17,
            "timestamp": "2026-05-20T12:00:00",
        }
        (proj_dir / "corrections.jsonl").write_text(
            "NOT_VALID_JSON\n" + json.dumps(valid, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        result = _apply_pending_corrections_for_chapter(proj_dir, "chapter_01")
        assert result == 1

    def test_missing_chunk_file_skipped_gracefully(self, tmp_path):
        """A correction targeting a non-existent chunk returns 0 without crashing."""
        proj_dir = self._setup_project(tmp_path)
        correction = {
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_999",  # does not exist
            "es_idx": 0,
            "original_es": "El gato se sentó.",
            "corrected_es": "El gato.",
            "en_reference": "The cat sat.",
            "timestamp": "2026-05-20T12:00:00",
        }
        (proj_dir / "corrections.jsonl").write_text(
            json.dumps(correction, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        result = _apply_pending_corrections_for_chapter(proj_dir, "chapter_01")
        assert result == 0
        # Row is archived but marked skipped — not silently lost as "applied".
        archived = [
            json.loads(line)
            for line in (proj_dir / "corrections_applied.jsonl")
            .read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        assert len(archived) == 1
        assert archived[0]["status"] == "skipped"

    def test_load_chunk_exception_is_handled(self, tmp_path, monkeypatch):
        """An exception loading a chunk is swallowed; function still returns cleanly."""
        proj_dir = self._setup_project(tmp_path)
        correction = {
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "es_idx": 0,
            "original_es": "El gato se sentó.",
            "corrected_es": "El gato tranquilo.",
            "en_reference": "The cat sat.",
            "chunk_offset_start": 0,
            "chunk_offset_end": 17,
            "timestamp": "2026-05-20T12:00:00",
        }
        (proj_dir / "corrections.jsonl").write_text(
            json.dumps(correction, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        import web_ui.app as app_module
        def _raise(path):
            raise OSError("corrupt chunk file")
        monkeypatch.setattr(app_module, "load_chunk", _raise)
        result = _apply_pending_corrections_for_chapter(proj_dir, "chapter_01")
        assert result == 0
        archived = [
            json.loads(line)
            for line in (proj_dir / "corrections_applied.jsonl")
            .read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        assert len(archived) == 1
        assert archived[0]["status"] == "skipped"

    def test_stale_correction_no_match_returns_zero(self, tmp_path):
        """A correction whose original_es no longer appears in chunk text returns 0."""
        proj_dir = self._setup_project(tmp_path)
        correction = {
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "chunk_id": "chapter_01_chunk_000",
            "es_idx": 0,
            "original_es": "Este texto ya no existe en el fragmento.",
            "corrected_es": "Texto corregido.",
            "en_reference": "The cat sat.",
            "chunk_offset_start": 0,
            "chunk_offset_end": 40,
            "timestamp": "2026-05-20T12:00:00",
        }
        (proj_dir / "corrections.jsonl").write_text(
            json.dumps(correction, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        result = _apply_pending_corrections_for_chapter(proj_dir, "chapter_01")
        assert result == 0
        archived = [
            json.loads(line)
            for line in (proj_dir / "corrections_applied.jsonl")
            .read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        assert len(archived) == 1
        assert archived[0]["status"] == "skipped"


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
