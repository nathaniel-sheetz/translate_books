"""Regression coverage for apply_to_chunk's offset-aware span resolution.

The reader has two independent paths that mutate chunk translated_text:

  A) Modal "Retranslate" → /api/sentence/replace → app.sentence_replace
  B) Inline "edit this line" → /api/correction → corrections.jsonl
     → /api/apply-corrections → apply_to_chunk

Both paths suffered from the "twin earlier in body" bug: a body sentence
whose text also lives in an [IMAGE:...] caption alt (or in a quoted
version a few sentences up) would be matched by naive find()/str.replace
at the earlier twin, corrupting it instead of the user's actual target.

Path A was fixed by stamping chunk_offset_start/end onto alignment rows
and threading them through to sentence_replace. This file exercises the
matching fix on Path B: offsets ride along on each correction record and
apply_to_chunk uses them via _resolve_correction_span.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.apply_corrections import (
    _resolve_correction_span,
    apply_to_chunk,
    dedupe_corrections,
)
from src.models import Chunk, ChunkMetadata, ChunkStatus
from src.utils.file_io import load_chunk, save_chunk
from web_ui.app import app


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


# -------- _resolve_correction_span: tier behavior --------


class TestResolveSpan:
    def test_tier1_exact_slice(self):
        text = "Sí. Sí. Sí."
        # Middle "Sí." at offset 4.
        assert _resolve_correction_span(text, "Sí.", 4, 7) == (4, 7)

    def test_tier2_anchored_find_when_slice_misses(self):
        text = "Sí. Sí. Sí."
        # Hint is one byte off — slice [3:6] is " Sí" not "Sí." — Tier 1
        # rejects. Tier 2 runs find("Sí.", 3) → 4.
        assert _resolve_correction_span(text, "Sí.", 3, 6) == (4, 7)

    def test_tier3_legacy_find_when_no_hint(self):
        text = "Sí. Sí. Sí."
        # Without hints, falls through to plain find — first match wins.
        # This preserves pre-fix behavior for legacy queued corrections.
        assert _resolve_correction_span(text, "Sí.", None, None) == (0, 3)

    def test_missing_text_returns_none(self):
        assert _resolve_correction_span("hello world", "absent", None, None) is None

    @pytest.mark.parametrize("bad_start,bad_end", [
        (-1, 3),                # negative
        (9999, 10000),          # out of bounds
        ("0", "3"),             # string-typed
        (True, False),          # bool (isinstance(True, int) is True)
        (None, None),           # missing
    ])
    def test_invalid_hints_fall_through_to_legacy(self, bad_start, bad_end):
        # Bad hints must not raise; they fall through to Tier 3.
        assert _resolve_correction_span("Sí. Sí.", "Sí.", bad_start, bad_end) == (0, 3)


# -------- apply_to_chunk: end-to-end --------


class TestApplyToChunkOffsets:
    def test_image_caption_twin_preserved(self):
        """The the-story-of-nelson scenario, distilled: the body sentence's
        text also appears inside an [IMAGE:...] caption a few characters
        earlier. Without offsets, str.replace would corrupt the caption.
        """
        text = "[IMAGE:images/i010.jpg:Había una fuerte marejada]\n\nHabía una fuerte marejada"
        chunk = _make_chunk("c", "ch", text, text)
        body_start = text.rfind("Había una fuerte marejada")
        body_end = body_start + len("Había una fuerte marejada")
        correction = {
            "original_es": "Había una fuerte marejada",
            "corrected_es": "«Había una fuerte marejada»",
            "chunk_offset_start": body_start,
            "chunk_offset_end": body_end,
        }

        updated, applied, _ = apply_to_chunk(chunk, [correction])

        assert applied == 1
        # Caption alt text MUST stay exactly as written.
        assert "[IMAGE:images/i010.jpg:Había una fuerte marejada]" in updated.translated_text
        # Body sentence wrapped — and ONLY the body sentence.
        assert updated.translated_text.endswith("«Había una fuerte marejada»")
        assert updated.translated_text.count("«Había una fuerte marejada»") == 1

    def test_quoted_twin_preserved(self):
        """Closer to the real user case: an earlier sentence already wraps
        the same text in guillemets. Without offsets, str.replace would
        nest the guillemets («« »»).
        """
        text = (
            "Y dijo: «Había una fuerte marejada» en ese momento.\n\n"
            "Había una fuerte marejada"
        )
        chunk = _make_chunk("c", "ch", text, text)
        body_start = text.rfind("Había una fuerte marejada")
        body_end = body_start + len("Había una fuerte marejada")
        correction = {
            "original_es": "Había una fuerte marejada",
            "corrected_es": "«Había una fuerte marejada»",
            "chunk_offset_start": body_start,
            "chunk_offset_end": body_end,
        }

        updated, applied, _ = apply_to_chunk(chunk, [correction])

        assert applied == 1
        # The earlier quoted version is untouched — no «« »» nesting.
        assert "«Había una fuerte marejada»" in updated.translated_text
        assert "««" not in updated.translated_text
        assert "»»" not in updated.translated_text
        # Both occurrences now wrapped exactly once.
        assert updated.translated_text.count("«Había una fuerte marejada»") == 2

    def test_third_of_three_targets_correctly(self):
        """Triple occurrence: prove offsets pick the THIRD, not 'always the
        first' (Tier 3) and not 'always the second' (anything order-based).
        """
        text = "Sí. Sí. Sí."
        chunk = _make_chunk("c", "ch", text, text)
        third_start = text.rfind("Sí.")
        correction = {
            "original_es": "Sí.",
            "corrected_es": "¡Claro!",
            "chunk_offset_start": third_start,
            "chunk_offset_end": third_start + 3,
        }

        updated, _, _ = apply_to_chunk(chunk, [correction])

        assert updated.translated_text == "Sí. Sí. ¡Claro!"

    def test_legacy_correction_without_offsets_still_works(self):
        """Pre-fix corrections.jsonl entries have no chunk_offset_*. They
        must still apply via Tier 3 (legacy find) for backward compat.
        """
        text = "El gato se sentó."
        chunk = _make_chunk("c", "ch", text, text)
        correction = {
            "original_es": "El gato se sentó.",
            "corrected_es": "El felino se sentó.",
        }

        updated, applied, _ = apply_to_chunk(chunk, [correction])

        assert applied == 1
        assert updated.translated_text == "El felino se sentó."

    def test_multiple_corrections_applied_descending_to_preserve_offsets(self):
        """Two corrections on the same chunk: if we applied them in queue
        order (offset-ascending), the first replacement would shift the
        second correction's offset and the second would land wrong (or
        miss entirely). Descending-by-offset application keeps both valid.
        """
        text = "Hola. Adiós. Hola."
        chunk = _make_chunk("c", "ch", text, text)
        first_hola_start = 0
        second_hola_start = text.rfind("Hola.")
        corrections = [
            # Queued in arbitrary order — apply_to_chunk must sort correctly.
            {
                "original_es": "Hola.",
                "corrected_es": "Buenos días.",
                "chunk_offset_start": first_hola_start,
                "chunk_offset_end": first_hola_start + 5,
            },
            {
                "original_es": "Hola.",
                "corrected_es": "Buenas noches.",
                "chunk_offset_start": second_hola_start,
                "chunk_offset_end": second_hola_start + 5,
            },
        ]

        updated, applied, _ = apply_to_chunk(chunk, corrections)

        assert applied == 2
        # First "Hola." → "Buenos días.", second "Hola." → "Buenas noches."
        assert updated.translated_text == "Buenos días. Adiós. Buenas noches."

    def test_stale_offset_falls_back_to_anchored_find(self):
        """If the chunk shifted between queueing and applying (e.g. a prior
        correction inserted text), the offset may no longer slice exactly.
        Tier 2 anchored find recovers the right occurrence as long as it's
        still at or after hint_start.
        """
        text = "AAA. Target. BBB. Target."
        chunk = _make_chunk("c", "ch", text, text)
        # User edited the second Target at offset 18. Imagine an unrelated
        # change inserted 2 chars before it; the hint now points 2 chars
        # early but the second Target is still findable from hint+2.
        stale_hint = 16  # off by 2
        correction = {
            "original_es": "Target.",
            "corrected_es": "Hit.",
            "chunk_offset_start": stale_hint,
            "chunk_offset_end": stale_hint + len("Target."),
        }

        updated, _, _ = apply_to_chunk(chunk, [correction])

        # Second Target gets replaced, first preserved.
        assert updated.translated_text == "AAA. Target. BBB. Hit."

    def test_dry_run_does_not_mutate(self):
        text = "Sí. Sí."
        chunk = _make_chunk("c", "ch", text, text)
        correction = {
            "original_es": "Sí.",
            "corrected_es": "Claro.",
            "chunk_offset_start": 4,
            "chunk_offset_end": 7,
        }

        updated, applied, _ = apply_to_chunk(chunk, [correction], dry_run=True)

        assert applied == 1
        # Returned chunk is the original (no mutation in dry-run).
        assert updated.translated_text == text


# -------- /api/correction: offsets persisted into corrections.jsonl --------


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def project_with_duplicate_for_correction(tmp_path, monkeypatch):
    """Project mirroring the the-story-of-nelson chapter 2 case: an
    [IMAGE:...] caption alt and a body sentence share the same text.
    Wired with an alignment file whose es_idx points to the body sentence
    so /api/correction can resolve chunk_id properly.
    """
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "test-project"
    chunks_dir = proj_dir / "chunks"
    align_dir = proj_dir / "alignments"
    chapters_dir = proj_dir / "chapters"
    for d in (chunks_dir, align_dir, chapters_dir):
        d.mkdir(parents=True)

    body = "Había una fuerte marejada"
    text = f"[IMAGE:images/i010.jpg:{body}]\n\n{body}"
    chunk = _make_chunk("chapter_01_chunk_000", "chapter_01", text, text)
    save_chunk(chunk, chunks_dir / "chapter_01_chunk_000.json")

    body_start = text.rfind(body)
    body_end = body_start + len(body)
    alignment = {
        "chapter_id": "chapter_01",
        "project_id": "test-project",
        "en_count": 2, "es_count": 2,
        "high_confidence_pct": 100.0, "avg_similarity": 0.95,
        "alignments": [
            {
                "es_idx": 0, "en_idx": 0,
                "es": f"[IMAGE:images/i010.jpg:{body}]",
                "en": "[IMAGE:images/i010.jpg:There was a heavy sea]",
                "similarity": 1.0, "confidence": "high",
                "chunk_id": "chapter_01_chunk_000",
            },
            {
                "es_idx": 1, "en_idx": 1,
                "es": body, "en": "There was a heavy sea running",
                "similarity": 0.95, "confidence": "high",
                "chunk_id": "chapter_01_chunk_000",
            },
        ],
    }
    (align_dir / "chapter_01.json").write_text(
        json.dumps(alignment, ensure_ascii=False), encoding="utf-8"
    )
    (chapters_dir / "chapter_01.txt").write_text(text, encoding="utf-8")

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir, body, body_start, body_end


class TestCorrectionEndpointOffsets:
    def test_offsets_persisted_to_corrections_jsonl(
        self, client, project_with_duplicate_for_correction,
    ):
        """The full round-trip: POST /api/correction with offsets, then
        verify the queued record carries them through to corrections.jsonl
        so apply_to_chunk can find them later.
        """
        proj_dir, body, body_start, body_end = project_with_duplicate_for_correction

        rv = client.post("/api/correction", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "es_idx": 1,
            "original_es": body,
            "corrected_es": f"«{body}»",
            "en_reference": "There was a heavy sea running",
            "chunk_offset_start": body_start,
            "chunk_offset_end": body_end,
        })

        assert rv.status_code == 200, rv.get_json()
        # Read the queued record.
        queued = json.loads(
            (proj_dir / "corrections.jsonl").read_text(encoding="utf-8").strip()
        )
        assert queued["chunk_offset_start"] == body_start
        assert queued["chunk_offset_end"] == body_end

    def test_apply_corrections_targets_body_not_caption(
        self, client, project_with_duplicate_for_correction,
    ):
        """The end-to-end fix proof: queue a correction with offsets, hit
        /api/apply-corrections, verify the body sentence is replaced and
        the [IMAGE:...] caption alt text is preserved.
        """
        proj_dir, body, body_start, body_end = project_with_duplicate_for_correction

        client.post("/api/correction", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "es_idx": 1,
            "original_es": body,
            "corrected_es": f"«{body}»",
            "en_reference": "There was a heavy sea running",
            "chunk_offset_start": body_start,
            "chunk_offset_end": body_end,
        })

        rv = client.post("/api/apply-corrections/test-project")
        assert rv.status_code == 200, rv.get_json()

        chunk_path = proj_dir / "chunks" / "chapter_01_chunk_000.json"
        result_text = load_chunk(chunk_path).translated_text
        # Caption alt MUST be untouched.
        assert f"[IMAGE:images/i010.jpg:{body}]" in result_text
        # Body sentence wrapped — exactly once.
        assert result_text.count(f"«{body}»") == 1
        assert result_text.endswith(f"«{body}»")

    def test_omitted_offsets_persist_no_keys(
        self, client, project_with_duplicate_for_correction,
    ):
        """Backward compat: an older frontend that doesn't send offsets
        must still queue corrections successfully. The record just lacks
        the offset keys, and apply_to_chunk's Tier 3 handles it.
        """
        proj_dir, body, _, _ = project_with_duplicate_for_correction

        rv = client.post("/api/correction", json={
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "es_idx": 1,
            "original_es": body,
            "corrected_es": f"«{body}»",
            "en_reference": "There was a heavy sea running",
        })

        assert rv.status_code == 200
        queued = json.loads(
            (proj_dir / "corrections.jsonl").read_text(encoding="utf-8").strip()
        )
        assert "chunk_offset_start" not in queued
        assert "chunk_offset_end" not in queued


# -------- Duplicate + idempotent apply (TODOS.md P1 lockout fix) --------


class TestDuplicateAndIdempotent:
    """The TODOS.md P1 "corrections.jsonl lockout" — if a correction is
    submitted twice and the first pass mutates the chunk so the duplicate's
    original_es no longer matches, the legacy code left total_applied below
    len(corrections) forever and corrections.jsonl was never unlinked. Fixed
    by (1) deduping before grouping and (2) treating already-applied entries
    as idempotent successes inside apply_to_chunk.
    """

    def test_dedupe_collapses_same_key_keeps_newest(self):
        older = {
            "chunk_id": "c0", "es_idx": 3,
            "original_es": "Hola.", "corrected_es": "Buenos días.",
            "timestamp": "2026-04-01T00:00:00",
        }
        newer = {
            "chunk_id": "c0", "es_idx": 3,
            "original_es": "Hola.", "corrected_es": "Buenos días.",
            "timestamp": "2026-04-02T00:00:00",
        }
        unrelated = {
            "chunk_id": "c0", "es_idx": 4,
            "original_es": "Adiós.", "corrected_es": "Hasta luego.",
            "timestamp": "2026-04-02T00:00:00",
        }
        result = dedupe_corrections([older, newer, unrelated])
        assert len(result) == 2
        # The duplicate (same chunk_id+es_idx+corrected_es) collapses to the
        # newer timestamp; the unrelated row survives.
        deduped_for_idx3 = next(r for r in result if r["es_idx"] == 3)
        assert deduped_for_idx3["timestamp"] == "2026-04-02T00:00:00"

    def test_dedupe_does_not_collapse_different_corrected_es(self):
        """Two corrections that target the same sentence but with different
        ``corrected_es`` are NOT duplicates — they're successive edits and
        both should be preserved (the second supersedes the first when
        applied, but both should be archived).
        """
        first = {
            "chunk_id": "c0", "es_idx": 3,
            "original_es": "Hola.", "corrected_es": "Buenos días.",
            "timestamp": "2026-04-01T00:00:00",
        }
        second = {
            "chunk_id": "c0", "es_idx": 3,
            "original_es": "Hola.", "corrected_es": "Buenas tardes.",
            "timestamp": "2026-04-02T00:00:00",
        }
        result = dedupe_corrections([first, second])
        assert len(result) == 2

    def test_apply_to_chunk_idempotent_when_already_corrected(self):
        """Chunk already has corrected_es; original_es is gone. apply_to_chunk
        must count it as applied (not skip), so the duplicate-pinned-banner
        regression cannot return.
        """
        text = "La hormiga se rompió la pata."
        chunk = _make_chunk("c", "ch", text, text)
        correction = {
            "original_es": "La hormiga se rompió la pierna.",
            "corrected_es": "La hormiga se rompió la pata.",
        }
        updated, applied, _ = apply_to_chunk(chunk, [correction])
        assert applied == 1
        # Text unchanged — it was already correct.
        assert updated.translated_text == text

    def test_apply_to_chunk_truly_missing_still_skips(self):
        """If NEITHER original nor corrected is in the chunk, that's a
        genuinely orphaned correction and applied stays 0.
        """
        text = "Some unrelated translated text."
        chunk = _make_chunk("c", "ch", text, text)
        correction = {
            "original_es": "Nada de esto está aquí.",
            "corrected_es": "Tampoco esto.",
        }
        _, applied, _ = apply_to_chunk(chunk, [correction])
        assert applied == 0

    def test_duplicate_correction_clears_file_after_apply(
        self, client, project_with_duplicate_for_correction,
    ):
        """Full round-trip of the TODOS.md P1 lockout scenario: POST the same
        correction twice, hit /api/apply-corrections, and assert
        corrections.jsonl is removed (banner won't stick) while
        corrections_applied.jsonl gets both rows (audit trail preserved).
        """
        proj_dir, body, body_start, body_end = project_with_duplicate_for_correction

        payload = {
            "project_id": "test-project",
            "chapter_id": "chapter_01",
            "es_idx": 1,
            "original_es": body,
            "corrected_es": f"«{body}»",
            "en_reference": "There was a heavy sea running",
            "chunk_offset_start": body_start,
            "chunk_offset_end": body_end,
        }
        client.post("/api/correction", json=payload)
        client.post("/api/correction", json=payload)

        # Confirm two rows queued — this is the lockout precondition.
        queued_text = (proj_dir / "corrections.jsonl").read_text(encoding="utf-8")
        assert len([ln for ln in queued_text.splitlines() if ln.strip()]) == 2

        rv = client.post("/api/apply-corrections/test-project")
        assert rv.status_code == 200, rv.get_json()

        # File is gone — banner will clear.
        assert not (proj_dir / "corrections.jsonl").exists()

        # Both Save events are recorded in the archive.
        archive_lines = [
            ln for ln in (proj_dir / "corrections_applied.jsonl")
            .read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        assert len(archive_lines) == 2

    def test_dedupe_empty_input(self):
        assert dedupe_corrections([]) == []

    def test_dedupe_tie_or_missing_timestamp_last_wins(self):
        first = {
            "chunk_id": "c0", "es_idx": 0,
            "original_es": "A.", "corrected_es": "B.",
        }
        second = {
            "chunk_id": "c0", "es_idx": 0,
            "original_es": "A.", "corrected_es": "B.",
        }
        result = dedupe_corrections([first, second])
        assert len(result) == 1
        assert result[0] is second

    def test_apply_to_chunk_falsy_corrected_es_is_skipped(self):
        text = "Some text here."
        chunk = _make_chunk("c", "ch", text, text)
        # corrected_es="" — falsy, so the idempotent guard is skipped and
        # the missing original triggers the WARNING path (applied stays 0).
        correction = {"original_es": "Not here.", "corrected_es": ""}
        _, applied, _ = apply_to_chunk(chunk, [correction])
        assert applied == 0

    def test_partial_apply_leaves_corrections_file_intact(
        self, client, project_with_duplicate_for_correction,
    ):
        """If a correction can't be applied (neither original nor corrected
        found in chunk), total_applied < len(deduped corrections) and the
        corrections.jsonl file must NOT be unlinked — the banner stays.
        """
        proj_dir, _, _, _ = project_with_duplicate_for_correction

        # Queue a correction whose original_es doesn't exist in the chunk.
        orphan = {
            "chunk_id": "chapter_01_chunk_000",
            "es_idx": 1,
            "original_es": "This sentence is not in the chunk at all.",
            "corrected_es": "Neither is this replacement.",
            "chapter_id": "chapter_01",
            "project_id": "test-project",
        }
        (proj_dir / "corrections.jsonl").write_text(
            json.dumps(orphan, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        rv = client.post("/api/apply-corrections/test-project")
        assert rv.status_code == 200

        # File stays — banner must not clear on a partial apply.
        assert (proj_dir / "corrections.jsonl").exists()
