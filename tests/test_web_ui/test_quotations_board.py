"""The quotation board routes (/quotations/<id> and its API). The board's logic is
tested in tests/test_quotations; these pin the HTTP surface."""

from __future__ import annotations

import json

import pytest

from web_ui.app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def book(tmp_path, monkeypatch):
    import web_ui.app as webapp

    root = tmp_path / "qbook"
    (root / ".harness" / "quotations").mkdir(parents=True)
    monkeypatch.setattr(webapp, "_resolve_project_dir", lambda pid: tmp_path / pid)
    return root


def test_page_renders_with_its_strings(client, book):
    r = client.get("/quotations/qbook")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "quotations_board.js" in html and 'data-project="qbook"' in html


def test_unknown_project_is_404(client, book):
    assert client.get("/quotations/nope").status_code == 404
    assert client.get("/api/project/nope/quotations").status_code == 404


def test_state_without_cards_is_empty(client, book):
    r = client.get("/api/project/qbook/quotations")
    assert r.status_code == 200 and r.get_json()["status"] == "empty"


def test_choices_for_unknown_cards_are_refused(client, book):
    (book / ".harness" / "quotations" / "mapped.json").write_text(
        json.dumps({"cards": []}), encoding="utf-8")
    r = client.post("/api/project/qbook/quotations/choices", json={"choices": {"x|y|z": {"nbla": True}}})
    assert r.status_code == 400 and "no such card" in r.get_json()["error"]
    assert client.post("/api/project/qbook/quotations/choices", json={}).status_code == 400


def test_approve_and_withdraw(client, book):
    (book / ".harness" / "quotations" / "mapped.json").write_text(
        json.dumps({"cards": []}), encoding="utf-8")
    r = client.post("/api/project/qbook/quotations/approve", json={})
    assert r.status_code == 200 and r.get_json()["approved"]["fingerprint"]
    r = client.post("/api/project/qbook/quotations/approve", json={"withdraw": True})
    assert r.get_json()["approved"] is None


def _chunk(book):
    (book / "chunks").mkdir(exist_ok=True)
    text = "Jesús dijo: «Yo soy la luz del mundo»."
    (book / "chunks" / "chapter_01_chunk_000.json").write_text(json.dumps({
        "id": "chapter_01_chunk_000", "chapter_id": "chapter_01", "position": 0,
        "source_text": "Jesus said.", "translated_text": text,
        "metadata": {"char_start": 0, "char_end": 11, "overlap_start": 0, "overlap_end": 0,
                     "paragraph_count": 1, "word_count": 2},
        "status": "translated",
    }, ensure_ascii=False), encoding="utf-8")


def test_the_editor_returns_to_the_board_card(client, book):
    _chunk(book)
    url = "/read/qbook/chapter_01/chunk/chapter_01_chunk_000/edit"
    back = "/quotations/qbook#card-chapter_01%7C43%208%3A12%7Cabc"
    html = client.get(url, query_string={"anchor": "luz", "return_to": back}).get_data(as_text=True)
    assert f'data-return-to="{back}"' in html


def test_the_editor_ignores_a_return_off_the_board(client, book):
    _chunk(book)
    url = "/read/qbook/chapter_01/chunk/chapter_01_chunk_000/edit"
    for bad in ("https://example.com/", "/quotations/other", "/quotations/qbook//evil.example", "/quotations/qbookx", "/read/qbook"):
        html = client.get(url, query_string={"return_to": bad}).get_data(as_text=True)
        assert 'data-return-to=""' in html, bad


def test_the_reader_arrow_returns_to_the_board_card(client, book):
    (book / "alignments").mkdir()
    (book / "alignments" / "chapter_01.json").write_text('{"alignments": []}', encoding="utf-8")
    url = "/read/qbook/chapter_01"
    back = "/quotations/qbook#card-chapter_01%7C43%208%3A12%7Cabc"
    html = client.get(url, query_string={"return_to": back}).get_data(as_text=True)
    assert f'<a href="{back}" class="back-link"' in html
    for bad in ("", "https://example.com/", "/quotations/qbook//evil.example"):
        html = client.get(url, query_string={"return_to": bad}).get_data(as_text=True)
        assert '<a href="/read/qbook?from=chapter_01" class="back-link"' in html, bad


def test_choices_carry_state_hand_and_ask(client, book):
    card = {"key": "chapter_01|43 8:12|abc", "chapter_id": "chapter_01", "kind": "quotation",
            "status": "proposed", "es_idx": [], "refusals": [], "echo": [],
            "quote_edits": [{"es_idx": 0, "old": "a", "new": "b"}]}
    (book / ".harness" / "quotations" / "mapped.json").write_text(
        json.dumps({"cards": [card]}), encoding="utf-8")
    r = client.post("/api/project/qbook/quotations/choices", json={"choices": {card["key"]: {
        "nbla": True, "state": "approve", "hand": True, "ask": "otra redacción"}}})
    assert r.status_code == 200
    choice = r.get_json()["chapters"][0]["cards"][0]["choice"]
    # The ask holds the card at pending until the agent revises it.
    assert choice["state"] == "pending" and choice["hand"] is True and choice["ask"] == "otra redacción"


def test_a_verdict_reaches_the_page_with_its_band_in_both_languages(client, book):
    from src.quotations import judge as qjudge
    from web_ui.i18n import get_strings

    edits = [{"es_idx": 0, "old": "a", "new": "b"}]
    card = {"key": "chapter_01|43 8:12|abc", "chapter_id": "chapter_01", "kind": "quotation",
            "status": "proposed", "es_idx": [], "refusals": [], "echo": [], "quote_edits": edits}
    (book / ".harness" / "quotations" / "mapped.json").write_text(
        json.dumps({"cards": [card]}), encoding="utf-8")
    qjudge.verdicts_path(book).write_text(json.dumps({"verdicts": {card["key"]: {
        "hash": qjudge.edits_hash(edits), "verdict": "partial", "confidence": 0.92,
        "fix": "su, no Su", "fix_kind": "blend", "reason": "Why."}}}), encoding="utf-8")
    view = client.get("/api/project/qbook/quotations").get_json()["chapters"][0]["cards"][0]
    assert view["judge"]["band"] == "high" and view["judge"]["fix"] == "su, no Su"
    assert view["choice"]["state"] == "pending"
    for lang in ("en", "es"):
        js = get_strings(lang)["quotations_js"]
        assert js["judge_use_ask"] and set(js["judge_verdicts"]) == {"apply", "partial", "reject", "toss_up", "none"}
        assert set(js["judge_bands"]) == {"high", "medium", "low"} and "{confidence}" in js["judge_says"]


def test_the_toss_up_tick_round_trips_and_has_its_label(client, book):
    card = {"key": "chapter_01|43 8:12|abc", "chapter_id": "chapter_01", "kind": "quotation",
            "status": "proposed", "es_idx": [], "refusals": [], "echo": [],
            "quote_edits": [{"es_idx": 0, "old": "a", "new": "b"}]}
    (book / ".harness" / "quotations" / "mapped.json").write_text(
        json.dumps({"cards": [card]}), encoding="utf-8")
    r = client.post("/api/project/qbook/quotations/choices", json={"choices": {card["key"]: {
        "nbla": True, "state": "reject", "toss_up": True}}})
    choice = r.get_json()["chapters"][0]["cards"][0]["choice"]
    assert choice["state"] == "reject" and choice["toss_up"] is True
    assert client.get("/api/project/qbook/quotations").get_json()["chapters"][0]["cards"][0]["choice"]["toss_up"] is True
    assert "toss_up" in client.get("/quotations/qbook").get_data(as_text=True)
