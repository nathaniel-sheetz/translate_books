"""Integration test for the translate_book `footnotes` stage."""

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import translate_book as tb  # noqa: E402
from src.models import Chunk, ChunkMetadata, ChunkStatus  # noqa: E402
from src.utils.file_io import load_chunk, save_chunk  # noqa: E402


def _make_project(tmp_path: Path) -> Path:
    proj = tmp_path / "mybook"
    (proj / "chunks").mkdir(parents=True)
    (proj / "alignments").mkdir()

    md = ChunkMetadata(char_start=0, char_end=40, overlap_start=0, overlap_end=0,
                       paragraph_count=1, word_count=8)
    chunk = Chunk(
        id="chapter_01_chunk_000", chapter_id="chapter_01", position=0,
        source_text="He paused. Then left.",
        translated_text="Se detuvo.[FOOTNOTE:1] Luego se fue.",
        status=ChunkStatus.TRANSLATED, metadata=md,
    )
    save_chunk(chunk, proj / "chunks" / "chapter_01_chunk_000.json")

    (proj / "alignments" / "chapter_01.json").write_text(json.dumps({"alignments": [
        {"es_idx": 0, "es": "Se detuvo.[FOOTNOTE:1]"},
        {"es_idx": 1, "es": "Luego se fue."},
    ]}), encoding="utf-8")

    (proj / "footnotes.json").write_text(json.dumps([
        {"number": 1, "ref_marker": "[1]", "source_body": "A note.",
         "translated_body": "Una nota.", "detected": "backlink"},
    ]), encoding="utf-8")
    return proj


def test_stage_footnotes_end_to_end(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)

    # Stub the EPUB rebuild so the test needs no ebook toolchain.
    built = {}

    class _Res:
        path = proj / "book.epub"

    def _fake_build(**kwargs):
        built["called"] = True
        return _Res()

    monkeypatch.setattr(tb, "build_epub_from_chunks", _fake_build)

    args = types.SimpleNamespace(project_name="mybook", author="X", target_lang_code="es")
    state = tb.stage_footnotes(args, proj, {})

    assert state["stage_completed"] == "footnotes"
    assert state["footnotes_written"] == 1
    assert built.get("called") is True

    # 1) annotation written, anchored + translated, gutenberg-origin
    records = [
        json.loads(l) for l in (proj / "annotations.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()
    ]
    footnotes = [r for r in records if r.get("type") == "footnote"]
    assert len(footnotes) == 1
    rec = footnotes[0]
    assert rec["es_idx"] == 0
    assert rec["origin"] == "gutenberg"
    assert rec["sub_id"] == "gb1"
    assert "Una nota." in rec["content"]
    assert rec["content"].startswith("[detuvo.]")

    # 2) tokens stripped from the stored translation
    reloaded = load_chunk(proj / "chunks" / "chapter_01_chunk_000.json")
    assert "[FOOTNOTE:" not in (reloaded.translated_text or "")

    # 3) alignment es sentences cleaned of tokens
    align = json.loads((proj / "alignments" / "chapter_01.json").read_text(encoding="utf-8"))
    assert align["alignments"][0]["es"] == "Se detuvo."


def test_stage_footnotes_noop_without_sidecar(tmp_path):
    proj = tmp_path / "book2"
    (proj / "chunks").mkdir(parents=True)
    args = types.SimpleNamespace(project_name="book2", author="X", target_lang_code="es")
    state = tb.stage_footnotes(args, proj, {})
    assert state["stage_completed"] == "footnotes"
    assert not (proj / "annotations.jsonl").exists()


def test_stage_footnotes_reports_partial_token_loss(tmp_path, monkeypatch, capsys):
    """When chapter source had more [FOOTNOTE:N] than the translation, apply logs the gap."""
    proj = _make_project(tmp_path)
    (proj / "chapters").mkdir(exist_ok=True)
    # Source carried notes 1 and 2; translation only kept note 1.
    (proj / "chapters" / "chapter_01.txt").write_text(
        "He paused.[FOOTNOTE:1] Then left.[FOOTNOTE:2]", encoding="utf-8"
    )
    (proj / "footnotes.json").write_text(json.dumps([
        {"number": 1, "ref_marker": "[1]", "source_body": "A note.",
         "translated_body": "Una nota.", "detected": "backlink"},
        {"number": 2, "ref_marker": "[2]", "source_body": "Second.",
         "translated_body": "Segunda.", "detected": "backlink"},
    ]), encoding="utf-8")

    class _Res:
        path = proj / "book.epub"

    monkeypatch.setattr(tb, "build_epub_from_chunks", lambda **kwargs: _Res())
    args = types.SimpleNamespace(project_name="mybook", author="X", target_lang_code="es")
    tb.stage_footnotes(args, proj, {})
    out = capsys.readouterr().out
    assert "expected" in out.lower() or "missing" in out.lower()
    assert "2" in out


def test_stage_footnotes_skips_without_alignment(tmp_path, monkeypatch, capsys):
    proj = _make_project(tmp_path)
    (proj / "alignments" / "chapter_01.json").unlink()
    monkeypatch.setattr(tb, "build_epub_from_chunks", lambda **kwargs: (_ for _ in ()).throw(AssertionError("should not build")))
    args = types.SimpleNamespace(project_name="mybook", author="X", target_lang_code="es")
    state = tb.stage_footnotes(args, proj, {})
    assert state.get("footnotes_written", 0) == 0
    assert "no alignment" in capsys.readouterr().out.lower()
