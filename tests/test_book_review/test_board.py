"""The review board: picks, approval and the page's routes.

What matters is that an approval covers exactly the diffs the user saw: a
change of picks, a re-run review or an edited artifact all void it, and
`apply --from-board --yes` refuses anything not approved.
"""

from __future__ import annotations

import json

import pytest

from src.book_review import board, synthesize
from src.book_review.apply import apply
from tests.test_book_review.conftest import term
from tests.test_book_review.test_synthesis import book, draft_payload, write_drafts  # noqa: F401


@pytest.fixture
def reviewed(book):  # noqa: F811
    """The fixture book with one committed recommendation and a conflicting twin."""
    synthesize.prepare(book.dir, cli="claude")
    write_drafts(book.dir, draft_payload())
    synthesize.commit(book.dir)
    path = synthesize.recommendations_path(book.dir)
    doc = json.loads(path.read_text(encoding="utf-8"))
    first = doc["recommendations"][0]
    twin = {**first, "id": "R-twin00", "title": "Another fix for the same key",
            "conflicts_with": [first["id"]],
            "patches": first["patches"] + [{"type": "manual", "description": "think about it"}]}
    first["conflicts_with"] = ["R-twin00"]
    doc["recommendations"].append(twin)
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    book.rid = first["id"]
    return book


def test_build_resolves_evidence_and_shows_the_current_entry(reviewed):
    state = board.build(reviewed.dir)
    rec = next(r for r in state["recommendations"] if r["id"] == reviewed.rid)
    assert rec["conflicts_with"] == ["R-twin00"] and not rec["stale"] and not rec["applied_at"]
    assert rec["patches"][0]["current"]["english"] == "porte cochere"
    finding = next(e for e in rec["evidence"] if e["kind"] == "finding")
    assert finding["english"].startswith("They stopped") and finding["excerpt"]
    assert {"kind": "unknown", "id": "made-up"} not in rec["evidence"]  # dropped at commit
    assert state["preview"] is None and state["selection"] == []


def test_saving_picks_previews_the_diff_and_writes_nothing_else(reviewed):
    before = (reviewed.dir / "glossary.json").read_text(encoding="utf-8")
    assert board.save(reviewed.dir, [reviewed.rid], {reviewed.rid: " keep both "})["status"] == "ok"
    state = board.build(reviewed.dir)
    assert state["preview"]["status"] == "dry_run"
    assert "porte-cochere" in state["preview"]["diffs"]["glossary"]
    assert state["notes"] == {reviewed.rid: "keep both"}
    assert (reviewed.dir / "glossary.json").read_text(encoding="utf-8") == before


def test_conflicting_unknown_and_out_of_range_picks_are_refused(reviewed):
    assert board.save(reviewed.dir, [reviewed.rid, "R-twin00"])["status"] == "error"
    assert board.save(reviewed.dir, ["R-nope00"])["status"] == "error"
    assert board.save(reviewed.dir, ["R-twin00.3"])["status"] == "error"
    assert board.save(reviewed.dir, ["R-twin00.x"])["status"] == "error"
    assert board.save(reviewed.dir, ["R-twin00.2"])["status"] == "ok"


def test_approval_holds_only_for_the_same_picks_and_artifacts(reviewed):
    board.save(reviewed.dir, [reviewed.rid])
    assert board.approve(reviewed.dir, ["R-twin00"])["status"] == "error"  # not what is saved
    assert board.approve(reviewed.dir, [reviewed.rid])["status"] == "ok"
    assert board.approved_selection(reviewed.dir) == ([reviewed.rid], None)
    assert board.build(reviewed.dir)["approved"]

    board.save(reviewed.dir, [reviewed.rid])  # the same picks again keep it
    assert board.approved_selection(reviewed.dir)[0] == [reviewed.rid]

    board.save(reviewed.dir, ["R-twin00.2"])
    selection, why = board.approved_selection(reviewed.dir)
    assert selection is None and "not approved" in why

    board.save(reviewed.dir, [reviewed.rid])
    board.approve(reviewed.dir, [reviewed.rid])
    reviewed.glossary([term("porte cochere", "la cochera cubierta", context="edited by hand")])
    selection, why = board.approved_selection(reviewed.dir)
    assert selection is None and "changed after approval" in why
    assert board.build(reviewed.dir)["approved"] is None


def test_applied_recommendations_show_and_cannot_be_picked_again(reviewed):
    assert apply(reviewed.dir, [reviewed.rid], yes=True)["status"] == "ok"
    state = board.build(reviewed.dir)
    rec = next(r for r in state["recommendations"] if r["id"] == reviewed.rid)
    assert rec["applied_at"]
    twin = next(r for r in state["recommendations"] if r["id"] == "R-twin00")
    assert twin["stale"] == [  # the entry it edits moved under it
        "change 1: the glossary entry 'porte cochere' changed since the review"]
    assert board.save(reviewed.dir, [reviewed.rid])["status"] == "error"
    assert board.summary(reviewed.dir)["applied"] == [reviewed.rid]


# --- the CLI -----------------------------------------------------------------

def _cli(argv, capsys):
    from scripts import book_review as cli

    cli.main(argv)
    return json.loads(capsys.readouterr().out)


def test_apply_from_board_needs_an_approval_for_yes(reviewed, capsys, monkeypatch):
    from src.harness import state as hstate

    monkeypatch.setattr(hstate, "resolve_project_dir", lambda arg: reviewed.dir)
    args = ["apply", "--project", "book", "--from-board"]
    assert _cli(args, capsys)["status"] == "error"  # nothing picked

    board.save(reviewed.dir, [reviewed.rid])
    dry = _cli(args, capsys)
    assert dry["status"] == "dry_run" and dry["selection"] == [reviewed.rid]
    refused = _cli(args + ["--yes"], capsys)
    assert refused["status"] == "error" and "not approved" in refused["error"]

    board.approve(reviewed.dir, [reviewed.rid])
    done = _cli(args + ["--yes"], capsys)
    assert done["status"] == "ok" and done["saved"] == ["glossary"]
    assert board.load(reviewed.dir)["selection"] == [] and board.load(reviewed.dir)["approved"] is None

    out = _cli(["board", "--project", "book", "--base-url", "http://127.0.0.1:9"], capsys)
    assert out["url"].endswith("/book-review/book") and out["server_running"] is False
    assert out["applied"] == [reviewed.rid]


# --- the web UI --------------------------------------------------------------

@pytest.fixture
def client(reviewed, monkeypatch):
    import web_ui.app as app_module

    app_module._NESTED_PROJECT_CACHE.clear()
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: reviewed.dir.parent)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def test_the_page_and_its_api(client, reviewed):
    page = client.get("/book-review/book")
    assert page.status_code == 200 and "book_review_board.js" in page.get_data(as_text=True)
    assert client.get("/book-review/nosuchbook").status_code == 404
    assert client.get("/api/project/nosuchbook/book-review").status_code == 404

    state = client.get("/api/project/book/book-review").get_json()
    assert len(state["recommendations"]) == 2 and str(reviewed.dir) not in json.dumps(state)

    bad = client.post("/api/project/book/book-review/selection",
                      json={"selection": [reviewed.rid, "R-twin00"]})
    assert bad.status_code == 400 and "pick one" in bad.get_json()["error"]

    saved = client.post("/api/project/book/book-review/selection",
                        json={"selection": [reviewed.rid], "notes": {}}).get_json()
    assert saved["preview"]["status"] == "dry_run"
    approved = client.post("/api/project/book/book-review/approve",
                           json={"selection": [reviewed.rid]}).get_json()
    assert approved["approved"]["selection"] == [reviewed.rid]
    withdrawn = client.post("/api/project/book/book-review/approve", json={"withdraw": True}).get_json()
    assert withdrawn["approved"] is None


def test_every_string_has_its_spanish_twin():
    from web_ui.i18n import STRINGS

    en, es = STRINGS["en"], STRINGS["es"]
    top = {k for k in en if k.startswith("bookreview_")}
    assert top and top == {k for k in es if k.startswith("bookreview_")}

    def shape(table):
        return {k: sorted(v) if isinstance(v, dict) else None for k, v in table.items()}

    assert shape(en["bookreview_js"]) == shape(es["bookreview_js"])
    from src.book_review.evidence import THEMES

    assert set(en["bookreview_js"]["themes"]) == set(THEMES)
    assert set(en["bookreview_js"]["priorities"]) == {str(p) for p in board.PRIORITIES}
