"""Tests for the reader-mode "Find in book" concordance search.

Covers the lane-A server (GET /api/search/<project_id>) and its folding /
offset / KWIC helpers. The critical correctness risk is the match-offset
mapping: folding can change string length, so offsets computed in folded space
must map back to the ORIGINAL string (design D5). See design 20260603 T1/T2/T7
and the failure-mode table.
"""

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web_ui.app as app_module
from web_ui.app import (
    app,
    _fold,
    _fold_with_map,
    _find_match,
    _kwic_window,
)
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


def _write_alignment(align_dir: Path, chapter_id: str, pairs: list[dict]) -> None:
    payload = {
        "chapter_id": chapter_id,
        "project_id": "test-project",
        "en_count": len(pairs),
        "es_count": len(pairs),
        "high_confidence_pct": 100.0,
        "avg_similarity": 0.95,
        "alignments": pairs,
    }
    (align_dir / f"{chapter_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def search_project(tmp_path, monkeypatch):
    """A book with two translated chapters (alignments) and one untranslated
    chapter (chunk source_text only).

    chapter_01: accented match + an [IMAGE:] row that must be excluded.
    chapter_02: a later translated hit (document-order check).
    chapter_03: NO alignment — source-side scan only (display-only KWIC).
    """
    projects_dir = tmp_path / "projects"
    proj = projects_dir / "test-project"
    chunks_dir = proj / "chunks"
    align_dir = proj / "alignments"
    chunks_dir.mkdir(parents=True)
    align_dir.mkdir(parents=True)

    _write_alignment(align_dir, "chapter_01", [
        {"es_idx": 0, "en_idx": 0,
         "es": "El niño habló con claridad.", "en": "The boy spoke clearly.",
         "confidence": "high", "chunk_id": "chapter_01_chunk_000"},
        {"es_idx": 1, "en_idx": 1,
         "es": "[IMAGE:images/i1.jpg:Un barco]", "en": "[IMAGE:images/i1.jpg:A ship]",
         "confidence": "high", "chunk_id": "chapter_01_chunk_000"},
        {"es_idx": 2, "en_idx": 2,
         "es": "Hablo todos los dias.", "en": "I speak every day.",
         "confidence": "high", "chunk_id": "chapter_01_chunk_000"},
    ])
    _write_alignment(align_dir, "chapter_02", [
        {"es_idx": 0, "en_idx": 0,
         "es": "Otro barco grande.", "en": "Another big ship.",
         "confidence": "high", "chunk_id": "chapter_02_chunk_000"},
    ])

    # Translated chapters need chunks so they aren't mistaken for source-only.
    save_chunk(_make_chunk("chapter_01_chunk_000", "chapter_01",
                           "The boy spoke clearly. A ship. I speak every day.",
                           "El niño habló con claridad. Un barco. Hablo todos los dias."),
               chunks_dir / "chapter_01_chunk_000.json")
    save_chunk(_make_chunk("chapter_02_chunk_000", "chapter_02",
                           "Another big ship.", "Otro barco grande."),
               chunks_dir / "chapter_02_chunk_000.json")

    # Untranslated chapter: chunk source_text only, no alignment file.
    save_chunk(_make_chunk(
        "chapter_03_chunk_000", "chapter_03",
        "Long ago the great ship sailed across the wide ocean toward a distant harbor.",
        ""), chunks_dir / "chapter_03_chunk_000.json")

    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj


def _get(client, q, side="translation", project="test-project"):
    return client.get(f"/api/search/{project}?q={q}&side={side}")


# ---------- folding ----------


def test_fold_accent_and_case_equivalence():
    assert _fold("habló") == _fold("HABLO") == _fold("Habló") == "hablo"
    assert _fold("ñ") == "n"
    assert _fold("ÁÉÍÓÚ") == "aeiou"


def test_fold_with_map_indices_are_monotonic_and_in_range():
    text = "Straße"
    folded, idx = _fold_with_map(text)
    assert folded == "strasse"           # ß -> ss (length grows)
    assert len(idx) == len(folded)
    assert all(0 <= i < len(text) for i in idx)
    assert idx == sorted(idx)            # never decreases


# ---------- offset mapping (CRITICAL) ----------


def test_find_match_offsets_with_length_changing_fold_before_match():
    # ß folds to two chars, so the folded match position is shifted vs the
    # original. The returned offsets must still slice the ORIGINAL exactly.
    s = "Straße hablo claro"
    m = _find_match(s, _fold("hablo"))
    assert m is not None
    assert s[m[0]:m[1]] == "hablo"


def test_find_match_offsets_with_accent_inside_match():
    s = "Él dijo: habló fuerte."
    m = _find_match(s, _fold("hablo"))
    assert m is not None
    assert s[m[0]:m[1]] == "habló"       # accent preserved in the original slice


def test_find_match_no_match_returns_none():
    assert _find_match("hello world", _fold("zzz")) is None


def test_find_match_empty_query_returns_none():
    assert _find_match("anything", "") is None


# ---------- KWIC window ----------


def test_kwic_trims_to_word_boundaries_and_marks_truncation():
    text = "alpha beta gamma delta TARGET epsilon zeta eta theta"
    start = text.index("TARGET")
    snippet, ms, me = _kwic_window(text, start, start + len("TARGET"), words_each_side=2)
    assert snippet[ms:me] == "TARGET"
    assert snippet.startswith("… ")            # left truncated
    assert snippet.endswith(" …")              # right truncated
    assert "gamma delta" in snippet            # 2 words before kept
    assert "epsilon zeta" in snippet           # 2 words after kept
    assert "alpha" not in snippet and "beta" not in snippet


def test_kwic_no_truncation_when_within_window():
    text = "one two TARGET three four"
    start = text.index("TARGET")
    snippet, ms, me = _kwic_window(text, start, start + len("TARGET"), words_each_side=5)
    assert snippet == "one two TARGET three four"
    assert snippet[ms:me] == "TARGET"


# ---------- endpoint: translation side ----------


def test_translation_search_matches_es_with_offsets(client, search_project):
    r = _get(client, "hablo", "translation")
    assert r.status_code == 200
    data = r.get_json()
    assert data["side"] == "translation"
    assert data["n_results"] == 2          # "habló" and "Hablo"
    assert data["n_chapters"] == 1
    first = data["results"][0]
    assert first["translated"] is True
    assert first["match_field"] == "es"
    # Offsets index the ORIGINAL es and land on the accented word.
    assert first["es"][first["match_start"]:first["match_end"]] == "habló"
    assert first["en"] == "The boy spoke clearly."
    # Anchor is an es prefix the reader can match with startsWith.
    assert first["es"].startswith(first["anchor"])


def test_image_rows_excluded(client, search_project):
    # "barco" appears only inside the [IMAGE:...] row in ch01 and in ch02 text.
    r = _get(client, "barco", "translation")
    data = r.get_json()
    chapters = {row["chapter"] for row in data["results"]}
    assert chapters == {"chapter_02"}      # the ch01 [IMAGE:] row is excluded
    assert data["n_results"] == 1


def test_results_in_document_order(client, search_project):
    # "ar" matches "claridad" (ch01) and "barco" (ch02); ch01 must precede ch02.
    chapters = [row["chapter"] for row in _get(client, "ar", "translation").get_json()["results"]]
    assert "chapter_01" in chapters and "chapter_02" in chapters
    assert chapters == sorted(chapters)        # non-decreasing chapter order
    assert chapters.index("chapter_02") == len(chapters) - chapters.count("chapter_02")


def test_default_and_invalid_side_is_translation(client, search_project):
    assert client.get("/api/search/test-project?q=hablo").get_json()["side"] == "translation"
    assert _get(client, "hablo", "bogus").get_json()["side"] == "translation"


def test_short_query_guard(client, search_project):
    data = _get(client, "h", "translation").get_json()
    assert data["n_results"] == 0 and data["results"] == []


# ---------- endpoint: source side ----------


def test_source_search_aligned_and_untranslated(client, search_project):
    r = _get(client, "ship", "source")
    assert r.status_code == 200
    data = r.get_json()
    by_chapter = {}
    for row in data["results"]:
        by_chapter.setdefault(row["chapter"], []).append(row)

    # Aligned chapter: navigable pair, match on en, has anchor.
    ch02 = by_chapter["chapter_02"][0]
    assert ch02["translated"] is True
    assert ch02["match_field"] == "en"
    assert ch02["en"][ch02["match_start"]:ch02["match_end"]].lower() == "ship"
    assert ch02["es"].startswith(ch02["anchor"])

    # Untranslated chapter: display-only KWIC, no anchor, no jump.
    ch03 = by_chapter["chapter_03"][0]
    assert ch03["translated"] is False
    assert "anchor" not in ch03
    assert ch03["match_field"] == "snippet"
    assert ch03["snippet"][ch03["match_start"]:ch03["match_end"]].lower() == "ship"

    assert "chapter_03" in by_chapter      # untranslated chapter is covered


def test_translation_side_skips_untranslated_chapters(client, search_project):
    # "ship" is English; on the translation side it must not appear, and the
    # untranslated chapter_03 has no translation to search.
    data = _get(client, "ship", "translation").get_json()
    assert data["n_results"] == 0


# ---------- validation / failure modes ----------


def test_path_traversal_rejected(client, search_project):
    assert client.get("/api/search/..%2f..%2fetc?q=hablo").status_code in (400, 404)
    assert _get(client, "hablo", project="bad/id").status_code in (400, 404)


def test_missing_project_returns_empty(client, search_project):
    data = _get(client, "hablo", project="no-such-book").get_json()
    assert data["n_results"] == 0 and data["results"] == []


def test_malformed_alignment_returns_500(client, search_project):
    bad = search_project / "alignments" / "chapter_04.json"
    bad.write_text("{not valid json", encoding="utf-8")
    assert _get(client, "hablo", "translation").status_code == 500


# ---------- query log (T7 / D6) ----------


def test_query_is_logged(client, search_project):
    _get(client, "hablo", "translation")
    log = search_project / "search_queries.jsonl"
    assert log.exists()
    rec = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert set(rec) == {"q", "side", "n_results", "ts"}
    assert rec["q"] == "hablo" and rec["side"] == "translation" and rec["n_results"] == 2


def test_query_log_write_failure_keeps_serving(client, search_project, caplog):
    # Make the log path unwritable by occupying it with a directory; the write
    # raises OSError, which must be warned and swallowed (D6).
    (search_project / "search_queries.jsonl").mkdir()
    with caplog.at_level(logging.WARNING):
        r = _get(client, "hablo", "translation")
    assert r.status_code == 200
    assert r.get_json()["n_results"] == 2     # search still served
    assert any("search query" in rec.message.lower() for rec in caplog.records)
