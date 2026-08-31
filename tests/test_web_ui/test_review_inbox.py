"""The cross-book review inbox (``/review-inbox``).

This page is the fix for the funnel, not for generation: the reviews were always
cheap to produce and expensive to land, one book per chat session. So what these
tests pin is the landing path — that the page lists every in-scope book's
outstanding plan, that applying goes through ``review.apply`` (the only writer,
with its own staleness check), and that a wave holding the book's lock is
reported rather than raced.
"""

from __future__ import annotations

import json

import pytest

from src.annotations import review
from src.harness import locks
from web_ui.app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def books(tmp_path, monkeypatch):
    """A projects root the inbox will walk, with one reviewable book in it."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    import web_ui.app as app_module
    app_module._NESTED_PROJECT_CACHE.clear()
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return projects_dir


def make_book(projects_dir, name="inboxbook", *, group=None):
    parent = projects_dir / group if group else projects_dir
    book = parent / name
    (book / "chunks").mkdir(parents=True)
    (book / "project.json").write_text(
        json.dumps({"title": "Inbox Book"}), encoding="utf-8"
    )
    (book / "annotations.jsonl").write_text(
        json.dumps({
            "project_id": name, "chapter_id": "chapter_01", "es_idx": 0,
            "sub_id": "u1", "type": "word_choice", "content": "poyo",
            "timestamp": "2026-01-01T00:00:00",
        }) + "\n",
        encoding="utf-8",
    )
    return book


def plant_results(book, *, key="chapter_01__0__u1", mode="append", ann_type="word_choice",
                  confidence="high", skipped=None, writable=True):
    """Write the ``results.json`` the inbox renders, as ``commit`` would have."""
    adir = book / ".harness" / "annotations"
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "results.json").write_text(json.dumps({
        "project": book.name,
        "committed_at": "2026-01-02T00:00:00",
        "target_language": "Spanish",
        "marker": "— IA:",
        "results": [{
            "key": key,
            "chapter_id": "chapter_01",
            "es_idx": 0,
            "sub_id": "u1",
            "type": ann_type,
            "content": "poyo",
            "mode": mode,
            "new_content": "poyo\n— IA: usa 'banca'",
            "state": "needs_help",
            "state_reason": "r",
            "recommendation": "usa 'banca'",
            "note_text": "usa 'banca'",
            "confidence": confidence,
            "writable": writable,
            "manual_reason": None if writable else "multi_anchor",
            "prompt_version": "1",
        }],
        "skipped": skipped or [],
    }, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_page_renders_with_nothing_waiting(client, books):
    response = client.get("/review-inbox")
    assert response.status_code == 200
    assert b"Nothing waiting" in response.data


def test_page_lists_a_books_applicable_plan(client, books):
    book = make_book(books)
    plant_results(book)

    response = client.get("/review-inbox")

    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert "Inbox Book" in body
    assert "chapter_01__0__u1" in body
    assert "usa &#39;banca&#39;" in body or "usa 'banca'" in body


def test_nothing_is_pre_ticked(client, books):
    """The page exists because a previous pass applied 9 of ~48 resolutions.

    The fix for that is making each one readable, not defaulting them to yes.
    """
    book = make_book(books)
    plant_results(book)

    body = client.get("/review-inbox").data.decode("utf-8")

    assert 'class="inbox-cb"' in body
    assert "checked" not in body


def test_json_payload_groups_by_book_and_type(client, books):
    book = make_book(books)
    plant_results(book)

    data = client.get("/api/review-inbox").get_json()

    assert data["totals"]["applicable"] == 1
    assert [b["project_id"] for b in data["books"]] == [book.name]
    assert data["books"][0]["applicable_by_type"] == [
        {"type": "word_choice", "entries": data["books"][0]["applicable"]}
    ]


def test_a_footnote_is_flagged_as_published(client, books):
    """Its text goes into the EPUB, which is where an invented fact would print."""
    book = make_book(books)
    plant_results(book, mode="replace", ann_type="footnote")

    data = client.get("/api/review-inbox").get_json()

    flags = data["books"][0]["applicable"][0]["flags"]
    assert any("EPUB" in flag for flag in flags)
    assert data["totals"]["flagged"] == 1


def test_low_confidence_is_flagged(client, books):
    book = make_book(books)
    plant_results(book, confidence="low")

    data = client.get("/api/review-inbox").get_json()

    assert "low confidence" in data["books"][0]["applicable"][0]["flags"]


def test_manual_entries_carry_their_reason(client, books):
    book = make_book(books)
    plant_results(book, writable=False)

    data = client.get("/api/review-inbox").get_json()

    assert data["totals"]["applicable"] == 0
    assert data["books"][0]["manual"][0]["reason"] == "multi_anchor"


def test_orphans_are_listed_so_they_can_be_re_anchored(client, books):
    """No review run will ever reach them; only a human moving the anchor will."""
    book = make_book(books)
    plant_results(book, skipped=[{
        "key": "chapter_01__99__u9", "chapter_id": "chapter_01", "es_idx": 99,
        "sub_id": "u9", "type": "word_choice", "content": "perdida",
        "reason": "orphaned",
    }])

    data = client.get("/api/review-inbox").get_json()

    assert data["totals"]["orphaned"] == 1
    assert data["books"][0]["orphaned"][0]["content"] == "perdida"


def test_excluded_groups_never_reach_the_inbox(client, books):
    """`.backburner` holds backup snapshots whose notes duplicate a live book's."""
    plant_results(make_book(books, "snapshot", group=".backburner"))

    data = client.get("/api/review-inbox").get_json()

    assert data["books"] == []


def _rewrite_note(book, content):
    (book / "annotations.jsonl").write_text(
        json.dumps({
            "project_id": book.name, "chapter_id": "chapter_01", "es_idx": 0,
            "sub_id": "u1", "type": "word_choice", "content": content,
            "timestamp": "2026-01-03T00:00:00",
        }, ensure_ascii=False) + chr(10),
        encoding="utf-8",
    )


def test_an_applied_resolution_leaves_the_inbox(client, books):
    """`review.apply(dry_run=True)` plans off results.json, which keeps a
    resolution until the next `prepare` drops it. An inbox that showed those
    would re-offer finished work on every reload."""
    book = make_book(books)
    plant_results(book)

    _apply(client, book.name, ["chapter_01__0__u1"])
    data = client.get("/api/review-inbox").get_json()

    assert data["books"] == []


def test_a_stale_resolution_is_shown_but_not_tickable(client, books):
    """It is real outstanding work — just work whose review is out of date."""
    book = make_book(books)
    plant_results(book)
    _rewrite_note(book, "poyo (ya lo cambié)")

    data = client.get("/api/review-inbox").get_json()
    item = data["books"][0]["applicable"][0]

    assert item["state"] == "stale"
    assert data["totals"]["stale"] == 1
    assert any("edited in the reader" in flag for flag in item["flags"])

    body = client.get("/review-inbox").data.decode("utf-8")
    assert "inbox-item-stale" in body
    assert body.count("disabled") >= 1


def test_a_deleted_annotation_leaves_the_inbox(client, books):
    """No live record means nothing to write to; `apply` would call it stale."""
    book = make_book(books)
    plant_results(book)
    (book / "annotations.jsonl").write_text("", encoding="utf-8")

    assert client.get("/api/review-inbox").get_json()["books"] == []


def test_a_book_with_no_reviewed_results_is_absent(client, books):
    make_book(books)
    data = client.get("/api/review-inbox").get_json()
    assert data["books"] == []


def test_project_filter_narrows_the_page(client, books):
    plant_results(make_book(books, "book-a"))
    plant_results(make_book(books, "book-b"))

    data = client.get("/api/review-inbox?project=book-a").get_json()

    assert [b["project_id"] for b in data["books"]] == ["book-a"]


def test_a_locked_book_says_so(client, books):
    book = make_book(books)
    plant_results(book)

    with locks.project_lock(book, kind="annotations", run_id="nightly"):
        data = client.get("/api/review-inbox").get_json()

    assert data["books"][0]["locked"] is True


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def _apply(client, project_id, keys):
    return client.post(
        "/api/review-inbox/apply",
        json={"project_id": project_id, "keys": keys},
    )


def test_apply_appends_the_reviewed_note(client, books):
    book = make_book(books)
    plant_results(book)

    response = _apply(client, book.name, ["chapter_01__0__u1"])

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["applied"] == ["chapter_01__0__u1"]

    records = [
        json.loads(line)
        for line in (book / "annotations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 2                      # append-only: the original survives
    assert records[-1]["content"] == "poyo\n— IA: usa 'banca'"
    assert records[-1]["ai_review"]["original_content"] == "poyo"


def test_re_applying_the_same_key_is_a_no_op(client, books):
    book = make_book(books)
    plant_results(book)

    _apply(client, book.name, ["chapter_01__0__u1"])
    payload = _apply(client, book.name, ["chapter_01__0__u1"]).get_json()

    assert payload["applied"] == []
    assert payload["already_applied"] == ["chapter_01__0__u1"]


def test_a_note_edited_since_the_review_comes_back_stale(client, books):
    """It must be reported, never overwritten — the review describes old text."""
    book = make_book(books)
    plant_results(book)
    (book / "annotations.jsonl").write_text(
        json.dumps({
            "project_id": book.name, "chapter_id": "chapter_01", "es_idx": 0,
            "sub_id": "u1", "type": "word_choice", "content": "poyo (ya lo cambié)",
            "timestamp": "2026-01-03T00:00:00",
        }) + "\n",
        encoding="utf-8",
    )

    payload = _apply(client, book.name, ["chapter_01__0__u1"]).get_json()

    assert payload["applied"] == []
    assert payload["stale"][0]["key"] == "chapter_01__0__u1"
    records = [
        json.loads(line)
        for line in (book / "annotations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [r["content"] for r in records] == ["poyo (ya lo cambié)"]


def test_applying_a_footnote_asks_for_an_epub_rebuild(client, books):
    """A replace only reaches the book on the next build."""
    book = make_book(books)
    plant_results(book, mode="replace", ann_type="footnote")

    payload = _apply(client, book.name, ["chapter_01__0__u1"]).get_json()

    assert payload["applied"] == ["chapter_01__0__u1"]
    assert payload["needs_epub_rebuild"] is True


def test_applying_an_append_does_not_ask_for_a_rebuild(client, books):
    book = make_book(books)
    plant_results(book)
    payload = _apply(client, book.name, ["chapter_01__0__u1"]).get_json()
    assert payload["needs_epub_rebuild"] is False


def test_apply_is_refused_while_a_wave_holds_the_lock(client, books):
    """`apply` reads results.json, which a concurrent `prepare` rewrites."""
    book = make_book(books)
    plant_results(book)

    with locks.project_lock(book, kind="annotations", run_id="nightly"):
        response = _apply(client, book.name, ["chapter_01__0__u1"])

    assert response.status_code == 409
    assert response.get_json()["lock"]["kind"] == "annotations"
    # Nothing was written while it was refused.
    assert len((book / "annotations.jsonl").read_text(encoding="utf-8").strip().splitlines()) == 1


def test_apply_releases_the_lock_afterwards(client, books):
    book = make_book(books)
    plant_results(book)
    _apply(client, book.name, ["chapter_01__0__u1"])
    assert locks.holder_of(book) is None


@pytest.mark.parametrize("body", [
    {},
    {"project_id": "inboxbook"},
    {"project_id": "inboxbook", "keys": []},
    {"project_id": "inboxbook", "keys": "chapter_01__0__u1"},
    {"project_id": "../escape", "keys": ["k"]},
])
def test_apply_rejects_a_malformed_body(client, books, body):
    make_book(books)
    response = client.post("/api/review-inbox/apply", json=body)
    assert response.status_code == 400


def test_apply_404s_on_an_unknown_project(client, books):
    response = _apply(client, "no-such-book", ["k"])
    assert response.status_code == 404


def test_apply_errors_when_there_are_no_reviewed_results(client, books):
    make_book(books)
    response = _apply(client, "inboxbook", ["chapter_01__0__u1"])
    assert response.status_code == 400
    assert "commit" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# The link from the home page
# ---------------------------------------------------------------------------


def test_the_reader_home_links_to_the_inbox(client, books):
    """The work chip is the per-book channel; this is the all-books one.

    The repo has no email, webhook or toast of any kind, so if the nightly pass
    is not reachable from here it is not reachable at all.
    """
    body = client.get("/read/").data.decode("utf-8")
    assert 'href="/review-inbox"' in body


def test_results_skipped_tolerates_a_missing_file(tmp_path):
    assert review.results_skipped(tmp_path) == []
