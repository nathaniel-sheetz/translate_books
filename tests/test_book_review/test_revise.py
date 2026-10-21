"""Revising a recommendation from the user's note, and when one goes stale.

A revision is validated like a commit and only ever proposes: the user still
picks and approves it on the page. A recommendation goes stale only when what
it edits changed, not when anything else in the same file did.
"""

from __future__ import annotations

import json

import pytest

from src.book_review import board, synthesize
from src.book_review.apply import apply
from src.book_review.evidence import artifact_hashes
from src.book_review.revise import revise
from tests.test_book_review.conftest import term
from tests.test_book_review.test_board import _cli, client, reviewed  # noqa: F401
from tests.test_book_review.test_synthesis import book  # noqa: F401

CONTEXT_EDIT = {"type": "glossary.edit", "english": "porte cochere",
                "context": "carriage porch; keep the French form in dialogue"}


def _rec(project_dir, rid):
    doc = json.loads(synthesize.recommendations_path(project_dir).read_text(encoding="utf-8"))
    return next(r for r in doc["recommendations"] if r["id"] == rid)


def _state_rec(project_dir, rid):
    return next(r for r in board.build(project_dir)["recommendations"] if r["id"] == rid)


# --- staleness ---------------------------------------------------------------

def test_an_edit_elsewhere_in_the_same_file_keeps_it_pickable(reviewed):
    reviewed.glossary([term("porte cochere", "la cochera cubierta", context="carriage porch"),
                       term("buggy", "la calesa")])
    reviewed.style("FORMS OF ADDRESS\nEdited.\n\nUNITS\nKeep miles.")
    assert _state_rec(reviewed.dir, reviewed.rid)["stale"] == []
    assert board.save(reviewed.dir, [reviewed.rid])["status"] == "ok"
    assert apply(reviewed.dir, [reviewed.rid])["status"] == "dry_run"


def test_an_edit_to_the_entry_itself_makes_it_stale(reviewed):
    reviewed.glossary([term("porte cochere", "la cochera cubierta", context="edited by hand")])
    stale = _state_rec(reviewed.dir, reviewed.rid)["stale"]
    assert stale == ["change 1: the glossary entry 'porte cochere' changed since the review"]
    assert board.save(reviewed.dir, [reviewed.rid])["status"] == "error"


def test_a_passage_that_is_gone_makes_a_text_patch_stale(reviewed):
    patch = {"type": "style.replace_text", "before": "Keep miles.", "after": "Keep miles, not km."}
    assert revise(reviewed.dir, reviewed.rid, {"patches": [patch], "why": "units"})["status"] == "ok"
    assert _state_rec(reviewed.dir, reviewed.rid)["stale"] == []
    reviewed.style("FORMS OF ADDRESS\nThe owl keeps a familiar tú with Bambi.\n\nUNITS\nUse km.")
    stale = _state_rec(reviewed.dir, reviewed.rid)["stale"]
    assert stale and "no longer there" in stale[0]


def test_a_recommendation_from_before_the_stamps_goes_stale_on_any_edit(reviewed):
    path = synthesize.recommendations_path(reviewed.dir)
    doc = json.loads(path.read_text(encoding="utf-8"))
    for rec in doc["recommendations"]:
        rec.pop("targets")
        rec["precondition"] = {"glossary": artifact_hashes(reviewed.dir)["glossary"]}
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert _state_rec(reviewed.dir, reviewed.rid)["stale"] == []
    reviewed.glossary([term("porte cochere", "la cochera cubierta", context="carriage porch"),
                       term("buggy", "la calesa")])
    assert _state_rec(reviewed.dir, reviewed.rid)["stale"] == ["glossary.json changed since the review"]


# --- asking for a revision on the page ---------------------------------------

def test_a_revision_request_needs_a_note_and_blocks_the_pick(reviewed):
    board.save(reviewed.dir, [], {}, revise=[reviewed.rid])
    assert board.load(reviewed.dir)["revise"] == []  # no note, nothing to revise from

    board.save(reviewed.dir, [], {reviewed.rid: "keep the French"}, revise=[reviewed.rid])
    assert board.load(reviewed.dir)["revise"] == [reviewed.rid]
    assert _state_rec(reviewed.dir, reviewed.rid)["revise_requested"] is True
    refused = board.save(reviewed.dir, [reviewed.rid])
    assert refused["status"] == "error" and "waiting for a revision" in refused["problems"][0]
    assert board.summary(reviewed.dir)["revise"] == {reviewed.rid: "keep the French"}


# --- revise ------------------------------------------------------------------

def test_revise_replaces_the_changes_and_keeps_the_original(reviewed):
    original = _rec(reviewed.dir, reviewed.rid)["patches"]
    board.save(reviewed.dir, ["R-twin00.2"], {reviewed.rid: "keep the French"}, revise=[reviewed.rid])
    board.approve(reviewed.dir, ["R-twin00.2"])
    glossary_before = (reviewed.dir / "glossary.json").read_text(encoding="utf-8")

    out = revise(reviewed.dir, reviewed.rid,
                 {"patches": [CONTEXT_EDIT], "follow_ups": [], "why": "edit the context instead"})
    assert out == {"status": "ok", "id": reviewed.rid, "revision": 1, "patches": 1, "follow_ups": 0}
    rec = _rec(reviewed.dir, reviewed.rid)
    assert rec["patches"] == [CONTEXT_EDIT] and rec["follow_ups"] == []
    assert rec["revision"]["original"]["patches"] == original
    assert rec["revision"]["note"] == "keep the French" and rec["conflicts_with"] == ["R-twin00"]
    assert (reviewed.dir / "glossary.json").read_text(encoding="utf-8") == glossary_before

    saved = board.load(reviewed.dir)
    assert saved["revise"] == [] and saved["approved"] is None
    assert saved["notes"] == {reviewed.rid: "keep the French"}

    view = _state_rec(reviewed.dir, reviewed.rid)
    assert view["revision"]["why"] == "edit the context instead"
    assert view["revision"]["original"][0]["type"] == "glossary.rekey"
    assert board.save(reviewed.dir, [reviewed.rid])["status"] == "ok"
    assert "keep the French form" in board.build(reviewed.dir)["preview"]["diffs"]["glossary"]

    again = revise(reviewed.dir, reviewed.rid, {"patches": original, "why": "back to the rekey"})
    rec = _rec(reviewed.dir, reviewed.rid)
    assert again["revision"] == 2 and rec["revision"]["original"]["patches"] == original
    assert board.load(reviewed.dir)["selection"] == []  # the old pick meant the old changes


def test_an_approval_does_not_survive_a_revision(reviewed):
    board.save(reviewed.dir, [reviewed.rid])
    board.approve(reviewed.dir, [reviewed.rid])
    revise(reviewed.dir, reviewed.rid, {"patches": [CONTEXT_EDIT], "why": "context instead"})
    board.save(reviewed.dir, [reviewed.rid])
    assert board.approved_selection(reviewed.dir)[0] is None


@pytest.mark.parametrize("change, problem", [
    ({"patches": [{"type": "glossary.edit", "english": "nope", "context": "x"}], "why": "w"},
     "no glossary entry"),
    ({"patches": [CONTEXT_EDIT]}, "needs why"),
    ({"why": "w"}, "needs a patches list"),
])
def test_revise_refuses_a_change_that_does_not_validate(reviewed, change, problem):
    before = synthesize.recommendations_path(reviewed.dir).read_text(encoding="utf-8")
    out = revise(reviewed.dir, reviewed.rid, change)
    assert out["status"] == "error" and problem in "; ".join(out["problems"])
    assert synthesize.recommendations_path(reviewed.dir).read_text(encoding="utf-8") == before


def test_revise_refuses_an_applied_or_unknown_recommendation(reviewed):
    assert revise(reviewed.dir, "R-nope00", {"patches": [], "why": "w"})["status"] == "error"
    apply(reviewed.dir, [reviewed.rid], yes=True)
    out = revise(reviewed.dir, reviewed.rid, {"patches": [CONTEXT_EDIT], "why": "w"})
    assert out["problems"] == [f"{reviewed.rid}: already applied"]


def test_the_cli_reads_the_revision_from_a_file(reviewed, capsys, monkeypatch, tmp_path):
    from src.harness import state as hstate

    monkeypatch.setattr(hstate, "resolve_project_dir", lambda arg: reviewed.dir)
    path = tmp_path / "revised.json"
    path.write_text(json.dumps({"patches": [CONTEXT_EDIT], "why": "context instead"}), encoding="utf-8")
    out = _cli(["revise", "--project", "book", "--id", reviewed.rid, "--from", str(path)], capsys)
    assert out["status"] == "ok" and out["revision"] == 1 and "approve" in out["instructions"]
    missing = _cli(["revise", "--project", "book", "--id", reviewed.rid,
                    "--from", str(tmp_path / "none.json")], capsys)
    assert missing["status"] == "error"


def test_the_page_saves_revision_requests(client, reviewed):  # noqa: F811
    state = client.post("/api/project/book/book-review/selection", json={
        "selection": [], "notes": {reviewed.rid: "simpler"}, "revise": [reviewed.rid],
    }).get_json()
    assert state["revise"] == [reviewed.rid]
    rec = next(r for r in state["recommendations"] if r["id"] == reviewed.rid)
    assert rec["revise_requested"] is True
