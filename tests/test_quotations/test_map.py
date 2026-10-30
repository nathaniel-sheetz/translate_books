"""Resolve and the mapping validator, on a made-up project and a made-up Bible.

The "verses" here are invented Spanish in the NBLA index's shape (line markers,
reverential capitals); no real Bible text.
"""

import json

import pytest

from src.quotations import map as qmap
from src.quotations import quotations_dir
from src.quotations import resolve as qresolve
from src.quotations import scan as qscan
from src.quotations.index import BibleIndex

VERSES = {
    # "John 8:12", prose.
    "43:008:012": "»Yo soy la luz del camino; el que Me sigue no andará en sombras.",
    # "Psalm 1:1-2", poetry.
    "19:001:001": "\nDichoso el que anda en Su senda,\nY no se sienta con los burladores.",
    "19:001:002": "\nSino que en la ley del SEÑOR medita,\nDe día y de noche.",
}
INDEX = BibleIndex({"version": "nbla", "verses": VERSES, "proper_words": ["dios"]})

SOURCE = (
    "SERMON I.\n\nI am the light of the world.—John 8:12\n\n"
    'Jesus said, "I am the light of the world." Walk in the light, he said.\n\n'
    "Blessed is the man that walketh not in the counsel of the ungodly. (Ps. 1:1)"
)
TRANSLATION = (
    "SERMÓN I.\n\n_Yo soy la luz del mundo._—Juan 8:12\n\n"
    "Jesús dijo: «Yo soy la luz del mundo». Caminen en la luz, dijo.\n\n"
    "Bienaventurado el hombre que no camina en el consejo de los impíos. (Sal 1:1)"
)
ROWS = [
    {"es_idx": 0, "en_idx": 0, "en": "SERMON I.", "es": "SERMÓN I."},
    {"es_idx": 1, "en_idx": 1, "en": "I am the light of the world.—John 8:12", "es": "_Yo soy la luz del mundo._—Juan 8:12"},
    {"es_idx": 2, "en_idx": 2, "en": 'Jesus said, "I am the light of the world."', "es": "Jesús dijo: «Yo soy la luz del mundo»."},
    {"es_idx": 3, "en_idx": 3, "en": "Walk in the light, he said.", "es": "Caminen en la luz, dijo."},
    {"es_idx": 4, "en_idx": 4, "en": "Blessed is the man that walketh not in the counsel of the ungodly.", "es": "Bienaventurado el hombre que no camina en el consejo de los impíos."},
    {"es_idx": 5, "en_idx": 5, "en": "(Ps. 1:1)", "es": "(Sal 1:1)"},
]

CANDIDATES = [
    {"kind": "epigraph", "en_span": "I am the light of the world.", "es_idx": [1], "ref_en": "John 8:12"},
    {"kind": "quotation", "en_span": "I am the light of the world.", "es_idx": [2], "ref_en": "John 8:12"},
    {"kind": "quotation", "en_span": "Blessed is the man that walketh not in the counsel of the ungodly.",
     "es_idx": [4], "ref_en": "Psalm 1:1"},
    {"kind": "not_scripture", "en_span": "Walk in the light, he said.", "es_idx": [3], "ref_en": None},
]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "book"
    (root / "chunks").mkdir(parents=True)
    (root / "alignments").mkdir()
    rows = [{**r, "chunk_id": "chapter_01_chunk_000"} for r in ROWS]
    (root / "chunks" / "chapter_01_chunk_000.json").write_text(json.dumps({
        "id": "chapter_01_chunk_000", "chapter_id": "chapter_01",
        "source_text": SOURCE, "translated_text": TRANSLATION,
    }), encoding="utf-8")
    (root / "alignments" / "chapter_01.json").write_text(
        json.dumps({"chapter_id": "chapter_01", "alignments": rows}), encoding="utf-8")
    qscan.scan_prepare(root, worker_model="claude-sonnet-5-5", cli="claude")
    draft = quotations_dir(root) / "chapter_01.scan.draft.json"
    draft.write_text(json.dumps({"chapter_id": "chapter_01", "candidates": CANDIDATES}), encoding="utf-8")
    assert qscan.scan_commit(root)["status"] == "ok"
    return root


def test_resolve_looks_up_and_fixes_only_the_book_name(project):
    out = qresolve.resolve(project, index=INDEX)
    assert out["status"] == "ok"
    assert out["counts"] == {"resolved": 3, "not_scripture": 1}
    doc = json.loads(qresolve.resolved_path(project).read_text(encoding="utf-8"))
    epigraph = next(c for c in doc["candidates"] if c["kind"] == "epigraph")
    assert epigraph["verses"][0]["key"] == "43:008:012"
    assert epigraph["ref_edit"] == {"es_idx": 1, "old": "Juan 8:12", "new": "Jn 8:12"}
    psalm = next(c for c in doc["candidates"] if c.get("ref_key") == "19 1:1")
    assert psalm["ref_ok"] == {"es_idx": 5, "text": "Sal 1:1"}


def test_resolve_marks_a_verse_the_index_lacks_unresolved(project):
    small = BibleIndex({"version": "nbla", "verses": {"43:008:012": VERSES["43:008:012"]}})
    out = qresolve.resolve(project, index=small)
    assert out["counts"]["unresolved"] == 1
    assert "19:001:001" in out["unresolved"][0]["reason"]


def _cards(project, mappings_by_row, monkeypatch):
    """Commit a draft whose mapping per candidate is looked up by its first row."""
    qresolve.resolve(project, index=INDEX)
    qmap.map_prepare(project, worker_model="claude-sonnet-5-5", cli="claude")
    manifest = json.loads((quotations_dir(project) / "map.manifest.json").read_text(encoding="utf-8"))
    job = manifest["jobs"][0]
    resolved = json.loads(qresolve.resolved_path(project).read_text(encoding="utf-8"))
    by_key = {c["key"]: c for c in resolved["candidates"]}
    mappings = []
    for cid, key in job["candidates"].items():
        row = by_key[key]["es_idx"][0]
        mappings.append({"id": cid, **mappings_by_row.get(row, {"replace": []})})
    (quotations_dir(project) / "chapter_01.map.draft.json").write_text(
        json.dumps({"chapter_id": "chapter_01", "mappings": mappings}), encoding="utf-8")
    monkeypatch.setattr(qmap, "load_index", lambda *_a, **_k: INDEX)
    out = qmap.map_commit(project)
    assert out["status"] == "ok", out
    doc = json.loads(qmap.mapped_path(project).read_text(encoding="utf-8"))
    return out, {c["key"]: c for c in doc["cards"]}, by_key


def test_map_builds_the_nbla_spanish_with_conventions(project, monkeypatch):
    out, cards, by_key = _cards(project, {
        1: {"replace": [{"es_idx": 1, "old": "Yo soy la luz del mundo.", "pieces": ["Yo soy la luz del camino;"]}]},
        2: {"replace": [{"es_idx": 2, "old": "Yo soy la luz del mundo", "pieces": ["Yo soy la luz del camino;"]}]},
    }, monkeypatch)
    epigraph = next(c for c in cards.values() if c["kind"] == "epigraph")
    edit = epigraph["quote_edits"][0]
    # Block (the epigraph fills its paragraph), wrappers kept, ";" gives way to the book's ".".
    assert edit["new"] == "Yo soy la luz del camino."
    assert edit["inline"] is False
    assert edit["verse_keys"] == ["43:008:012"]
    assert epigraph["status"] == "proposed"
    assert epigraph["ref_edit"]["new"] == "Jn 8:12"
    inline = next(c for c in cards.values() if c["kind"] == "quotation" and c["es_idx"] == [2])
    assert inline["quote_edits"][0]["new"] == "Yo soy la luz del camino"
    assert inline["quote_edits"][0]["inline"] is True
    not_scripture = next(c for c in cards.values() if c["kind"] == "not_scripture")
    assert not_scripture["status"] == "not_scripture"


def test_inline_poetry_lowers_line_starts_and_keeps_senor(project, monkeypatch):
    _out, cards, _ = _cards(project, {
        4: {"replace": [{
            "es_idx": 4,
            "old": "Bienaventurado el hombre que no camina en el consejo de los impíos.",
            "pieces": ["Dichoso el que anda en Su senda, Y no se sienta con los burladores."],
        }]},
    }, monkeypatch)
    psalm = next(c for c in cards.values() if c.get("ref_key") == "19 1:1")
    # "(Sal 1:1)" is the only other text in the paragraph, so it is a block and keeps "Y".
    assert psalm["quote_edits"][0]["new"] == "Dichoso el que anda en Su senda, Y no se sienta con los burladores."


def test_validator_refuses_paraphrased_pieces_missing_old_and_bad_evidence(project, monkeypatch):
    out, cards, _ = _cards(project, {
        1: {
            "replace": [{"es_idx": 1, "old": "Yo soy la luz del mundo.", "pieces": ["Yo soy la luz del mundo"]}],
            "echo": [
                {"es_idx": 3, "old": "Caminen", "new": "Anden", "en_evidence": "Walk"},
                {"es_idx": 3, "old": "luz", "new": "lumbre", "en_evidence": "lamp"},
                {"es_idx": 2, "old": "No está", "new": "x", "en_evidence": "Jesus"},
            ],
        },
        2: {
            "replace": [{"es_idx": 2, "old": "Yo soy la luz del mundo", "pieces": ["Yo soy la luz del camino"]}],
        },
    }, monkeypatch)
    epigraph = next(c for c in cards.values() if c["kind"] == "epigraph")
    reasons = sorted(r["reason"] for r in epigraph["refusals"])
    assert reasons == ["en_evidence_not_in_row", "old_not_in_row", "piece_not_verbatim"]
    assert epigraph["echo"] == [{
        "es_idx": 3, "chunk_id": "chapter_01_chunk_000", "old": "Caminen", "new": "Anden",
        "en_evidence": "Walk",
    }]
    assert epigraph["status"] == "proposed"  # the echo edit still stands


def test_overlapping_echo_inside_a_quotation_is_refused(project, monkeypatch):
    _out, cards, _ = _cards(project, {
        2: {
            "replace": [{"es_idx": 2, "old": "Yo soy la luz del mundo", "pieces": ["Yo soy la luz del camino"]}],
            "echo": [{"es_idx": 2, "old": "luz del mundo", "new": "luz del camino", "en_evidence": "light"}],
        },
    }, monkeypatch)
    card = next(c for c in cards.values() if c["kind"] == "quotation" and c["es_idx"] == [2])
    assert [r["reason"] for r in card["refusals"]] == ["overlaps_another_edit"]
    assert card["echo"] == []


def test_already_nbla_is_matches_not_proposed(project, monkeypatch):
    _out, cards, _ = _cards(project, {
        2: {"replace": [{"es_idx": 2, "old": "Yo soy la luz del", "pieces": ["Yo soy la luz del"]}]},
    }, monkeypatch)
    card = next(c for c in cards.values() if c["kind"] == "quotation" and c["es_idx"] == [2])
    assert card["status"] == "matches"


def test_missing_draft_leaves_cards_unmapped(project, monkeypatch):
    qresolve.resolve(project, index=INDEX)
    qmap.map_prepare(project, worker_model="claude-sonnet-5-5", cli="claude")
    monkeypatch.setattr(qmap, "load_index", lambda *_a, **_k: INDEX)
    out = qmap.map_commit(project)
    assert out["status"] == "partial"
    assert out["missing"] == ["chapter_01"]
    assert out["counts"]["by_status"]["unmapped"] == 3
