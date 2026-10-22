from __future__ import annotations

import json

import pytest

from src.book_review import judging, progress
from src.harness import flow
from tests.test_book_review.conftest import Book, write_json

ARTIFACTS = {"source": True, "style_guide": True, "glossary": True, "chunks": True,
             "address_map": True}


@pytest.fixture
def book(tmp_path):
    b = Book(tmp_path)
    write_json(b.dir / "project.json", {"name": "book"})
    b.chunk("chapter_01_chunk_000", "text")
    return b


def stamp(book, chunk_id, when):
    path = book.dir / "chunks" / f"{chunk_id}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["translated_at"] = when
    path.write_text(json.dumps(doc), encoding="utf-8")


def route(book, cfg, stage):
    return flow._suggested_reference(book.dir, cfg, ARTIFACTS, stage, ["book.epub"])


def test_canary_routes_a_partial_book_once(book):
    assert progress.due(book.dir, {}, "partial").startswith("canary")
    assert route(book, {}, "partial") == "references/book-review.md"
    progress.record_review(book.dir, kind="signals")
    assert progress.due(book.dir, {}, "partial") is None
    assert route(book, {}, "partial") == "references/translate-workers.md"


def test_canary_off(book):
    assert progress.due(book.dir, {"book_review_canary": "off"}, "partial") is None


def test_finished_book_is_due_until_reviewed_then_again_after_new_chunks(book):
    assert route(book, {}, "fully-translated") == "references/book-review.md"
    progress.record_review(book.dir, kind="signals")
    assert route(book, {}, "fully-translated") == "references/reviews.md"
    stamp(book, "chapter_01_chunk_000", "2099-01-01T00:00:00")
    assert progress.due(book.dir, {}, "fully-translated") == "chunks were translated after the last review"


def test_after_translate_off(book):
    assert route(book, {"book_review_after_translate": "off"}, "fully-translated") == "references/reviews.md"


def test_skip_marks_reviewed(book):
    import scripts.book_review as cli

    assert cli.main(["skip", "--project", str(book.dir)]) == 0
    assert progress.load_state(book.dir)["kind"] == "skipped"
    assert progress.due(book.dir, {}, "fully-translated") is None


def test_config_set_accepts_the_keys():
    assert flow._CONFIG_SET_KEYS["book_review_canary"] == frozenset({"on", "off"})
    assert flow._CONFIG_SET_KEYS["book_review_after_translate"] == frozenset({"on", "off"})


# ---------------------------------------------------------------------------
# judge sweep


def test_plan_owes_unjudged_chapters_and_skips_address_without_a_map(book):
    plan = judging.plan(book.dir)
    assert plan["judges"] == ["dialogue", "editorial"]
    assert plan["owed"] == {"dialogue": ["chapter_01"], "editorial": ["chapter_01"]}
    assert plan["owed_chunks"] == {"dialogue": 1, "editorial": 1}
    assert judging.scopes_for(plan["owed"]) == {
        "dialogue": ["chapter:chapter_01"], "editorial": ["chapter:chapter_01"],
    }


def test_run_refuses_when_gated(book, monkeypatch):
    monkeypatch.setattr(judging, "_gate", lambda project_dir, prof: "not logged in")
    out = judging.run(book.dir, cli="claude")
    assert out["status"] == "error" and out["error"] == "not logged in"


def test_run_sweeps_owed_scopes_then_adjudicates_the_book(book, monkeypatch):
    from src.judges import sweep

    calls = {}
    monkeypatch.setattr(judging, "_gate", lambda project_dir, prof: None)
    monkeypatch.setattr(judging, "_context", lambda project_dir, names: ({"k": 1}, None))

    def fake_sweep(project_dir, names, scopes, emit, **kwargs):
        calls.update(names=names, scopes=scopes, adjudicate=kwargs["adjudicate_scopes"])
        return {"ran": 2, "error_count": 0}

    monkeypatch.setattr(sweep, "run_headless_sweep", fake_sweep)
    out = judging.run(book.dir, cli="claude")
    assert out["status"] == "ok"
    assert calls["names"] == ["dialogue", "editorial"]
    assert calls["scopes"]["editorial"] == ["chapter:chapter_01"]
    assert calls["adjudicate"] == ["book"]
