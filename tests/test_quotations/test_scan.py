"""The detection wave's prepare and commit on a made-up two-row project."""

import json

import pytest

from src.quotations import quotations_dir
from src.quotations import scan as qscan

SOURCE = (
    "SERMON I.\n\nI am the light.—John 8:12\n\n"
    'Jesus said, "Let your light shine." That is Matt. 5:16. Read also Ps 119:105 tonight.'
)
TRANSLATION = (
    "SERMÓN I.\n\n_Yo soy la luz._—Jn 8:12\n\n"
    "Jesús dijo: «Brille su luz». Eso es Mt 5:16. Lean también Sal 119:105 esta noche."
)
ROWS = [
    {"es_idx": 0, "en_idx": 0, "en": "SERMON I.", "es": "SERMÓN I.", "chunk_id": "chapter_01_chunk_000"},
    # The epigraph's English sentence is dropped by the alignment, as on short-sermons.
    {"es_idx": 1, "en_idx": 2, "en": "—John 8:12", "es": "_Yo soy la luz._—Jn 8:12", "chunk_id": "chapter_01_chunk_000"},
    {"es_idx": 2, "en_idx": 3, "en": 'Jesus said, "Let your light shine."', "es": "Jesús dijo: «Brille su luz».", "chunk_id": "chapter_01_chunk_000"},
    {"es_idx": 3, "en_idx": 4, "en": "That is Matt. 5:16.", "es": "Eso es Mt 5:16.", "chunk_id": "chapter_01_chunk_000"},
    {"es_idx": 4, "en_idx": 5, "en": "Read also Ps 119:105 tonight.", "es": "Lean también Sal 119:105 esta noche.", "chunk_id": "chapter_01_chunk_000"},
]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "book"
    (root / "chunks").mkdir(parents=True)
    (root / "alignments").mkdir()
    (root / "chunks" / "chapter_01_chunk_000.json").write_text(json.dumps({
        "id": "chapter_01_chunk_000", "chapter_id": "chapter_01",
        "source_text": SOURCE, "translated_text": TRANSLATION,
    }), encoding="utf-8")
    (root / "alignments" / "chapter_01.json").write_text(json.dumps({
        "chapter_id": "chapter_01", "alignments": ROWS,
    }), encoding="utf-8")
    return root


def test_interleave_puts_unaligned_english_back(project):
    items = qscan.interleave(qscan.load_rows(project, "chapter_01"), qscan.chunk_sources(project, "chapter_01"))
    en_only = [i["en_only"] for i in items if "en_only" in i]
    assert en_only == ["I am the light."]
    # It sits between row 0 and row 1, where the source has it.
    order = ["en_only" if "en_only" in i else i["es_idx"] for i in items]
    assert order == [0, "en_only", 1, 2, 3, 4]


def test_body_lists_written_refs_and_rows(project):
    out = qscan.scan_prepare(project, worker_model="claude-sonnet-5-5", cli="claude")
    assert out["status"] == "ok"
    assert out["written_refs"] == 3
    body = (quotations_dir(project) / "chapter_01.scan.body.txt").read_text(encoding="utf-8")
    assert "WRITTEN REFERENCES in this chapter's English (3): John 8:12; Matt. 5:16; Ps 119:105" in body
    assert "[-] EN-only (no Spanish row of its own): I am the light." in body
    assert "[2]\n  EN: Jesus said" in body


def _draft(project, candidates):
    path = quotations_dir(project) / "chapter_01.scan.draft.json"
    path.write_text(json.dumps({"chapter_id": "chapter_01", "candidates": candidates}), encoding="utf-8")


def test_commit_validates_and_adds_the_written_reference_floor(project):
    qscan.scan_prepare(project, worker_model="claude-sonnet-5-5", cli="claude")
    _draft(project, [
        {"es_idx": [1], "en_span": "I am the light.", "es_span": "Yo soy la luz.", "kind": "epigraph",
         "ref_en": "John 8:12", "adapted": False, "why": "", "confidence": "high"},
        {"es_idx": [2], "en_span": "Let your light shine.", "es_span": "Brille su luz",
         "kind": "quotation", "ref_en": "Matthew 5:16", "adapted": False, "why": "", "confidence": "high"},
        # Paraphrased English: refused.
        {"es_idx": [2], "en_span": "Let thy light shine", "es_span": "", "kind": "quotation",
         "ref_en": "Matthew 5:16", "confidence": "high"},
        # Spanish that is not in the row: kept, es_span dropped and flagged.
        {"es_idx": [0], "en_span": "SERMON I.", "es_span": "Sermón uno", "kind": "not_scripture",
         "ref_en": None, "confidence": "low"},
    ])
    out = qscan.scan_commit(project)
    assert out["status"] == "ok"
    assert out["counts"]["usable"] == 4  # 3 from the wave + 1 coded
    assert out["written_refs"] == {"found": 3, "covered_by_wave": 2, "coded_cards": 1}
    assert [u["reason"] for u in out["unusable"]] == ["en_span_not_in_chapter"]

    doc = json.loads(qscan.candidates_path(project).read_text(encoding="utf-8"))
    by_kind = {c["kind"]: c for c in doc["candidates"]}
    coded = by_kind["reference_only"]
    assert coded["origin"] == "coded"
    assert coded["ref"] == "Psalms 119:105"
    assert coded["es_idx"] == [4]
    assert coded["es_span"] == "Sal 119:105"
    assert by_kind["epigraph"]["key"].startswith("chapter_01|43 8:12|")
    assert by_kind["epigraph"]["written_ref"] == "John 8:12"
    assert "es_span_not_found" in by_kind["not_scripture"]["flags"]
    assert by_kind["not_scripture"]["ref"] is None


def test_key_is_stable_across_realign(project):
    a = qscan.candidate_key("chapter_01", "43 8:12", "I am the light.")
    b = qscan.candidate_key("chapter_01", "43 8:12", "I  am the light.")
    assert a == b
    assert a != qscan.candidate_key("chapter_02", "43 8:12", "I am the light.")


def test_commit_without_drafts_leaves_candidates_alone(project):
    qscan.scan_prepare(project, worker_model="claude-sonnet-5-5", cli="claude")
    out = qscan.scan_commit(project)
    assert out["status"] == "error"
    assert out["missing"] == ["chapter_01"]
    assert not qscan.candidates_path(project).exists()


def test_fanout_runs_through_the_shared_wave(project):
    qscan.scan_prepare(project, worker_model="claude-sonnet-5-5", cli="claude")
    calls = []

    def runner(cmd, *, input_text, cwd, **_kw):
        calls.append(input_text)
        return 0, json.dumps({"type": "result", "result": json.dumps({"chapter_id": "chapter_01", "candidates": []})}), ""

    out = qscan.scan_fanout(project, runner=runner, cli="claude")
    assert "error" not in out, out
    assert len(calls) == 1 and "CHAPTER: chapter_01" in calls[0]
