"""Collections: membership, group wiring, stale precedent and the harness commands."""

from __future__ import annotations

import json

import pytest

from src import collection as collection_mod
from src.harness import flow
from src.house import groups
from tests.test_collections.conftest import make_project, write_json


def _coll(slug="plumer-volumen"):
    return collection_mod.load_collection(collection_mod.resolve_collection_dir(slug))


def test_create_writes_membership_and_volume_metadata(library, plumer):
    coll, report = collection_mod.create(
        "plumer-volumen", members=["short-sermons", "plain-thoughts"], title="Sermones y pensamientos")

    assert coll.path == library / "plumer-volumen"
    doc = json.loads((coll.path / "collection.json").read_text(encoding="utf-8"))
    assert doc == {"version": 1, "group": "plumer-volumen",
                   "members": [{"slug": "short-sermons"}, {"slug": "plain-thoughts"}]}
    project = json.loads((coll.path / "project.json").read_text(encoding="utf-8"))
    assert project == {"spanish_title": "Sermones y pensamientos"}
    assert [j["group"] for j in report["joined"]] == ["plumer-volumen", "plumer-volumen"]
    assert report["stale_precedent"] == []


def test_a_collection_is_not_a_project_and_the_walker_still_finds_members_inside(library, plumer):
    from src.harness import state

    cdir = library / ".macdonald" / "cuentos"
    make_project(cdir, "inner-story", chapters=[("Once.", None)])
    coll, _ = collection_mod.create("cuentos", members=["inner-story", "plain-thoughts"],
                                    parent=".macdonald")
    assert coll.path == cdir
    assert not state.is_project_dir(cdir)
    assert collection_mod.is_collection_dir(cdir)
    assert cdir / "inner-story" in list(state.iter_project_dirs(library))
    assert list(collection_mod.iter_collection_dirs(library)) == [cdir]
    assert _coll("cuentos").member("inner-story").path == cdir / "inner-story"


def test_create_rejects_a_slug_that_is_a_project(library, plumer):
    with pytest.raises(ValueError, match="already a project slug"):
        collection_mod.create("plain-thoughts", members=["short-sermons"])


def test_create_rejects_an_unknown_member_and_writes_nothing(library, plumer):
    with pytest.raises(FileNotFoundError, match="setup"):
        collection_mod.create("vol", members=["short-sermons", "no-such-book"])
    assert not (library / "vol").exists()
    assert groups.load_groups() == groups.load_groups(groups.GROUPS_EXAMPLE_FILE)


def test_create_rejects_a_duplicate_collection(library, plumer):
    collection_mod.create("vol", members=["short-sermons"])
    with pytest.raises(ValueError, match="already exists"):
        collection_mod.create("vol", members=["plain-thoughts"])


def test_new_group_defaults_to_a_domain_and_series_is_honored(library, plumer):
    collection_mod.create("vol", members=["short-sermons", "plain-thoughts"])
    assert groups.load_groups()["vol"]["kind"] == groups.DOMAIN

    make_project(library, "hardy-1", chapters=[("One.", None)])
    make_project(library, "hardy-2", chapters=[("Two.", None)])
    collection_mod.create("hardy-volume", members=["hardy-1", "hardy-2"], group_kind="series")
    assert groups.load_groups()["hardy-volume"] == {
        "kind": "series", "about": "", "members": ["hardy-1", "hardy-2"]}


def test_an_existing_group_is_reused(library, plumer):
    groups.join_group("short-sermons", "plumer", kind="domain", about="Plumer's books")
    coll, report = collection_mod.create(
        "plumer-volumen", members=["short-sermons", "plain-thoughts"], group="plumer")

    assert coll.group == "plumer"
    assert groups.load_groups()["plumer"] == {
        "kind": "domain", "about": "Plumer's books",
        "members": ["short-sermons", "plain-thoughts"]}
    assert "plumer-volumen" not in groups.load_groups()
    assert all("warning" not in j for j in report["joined"])


def test_a_conflicting_kind_for_an_existing_group_is_reported(library, plumer):
    groups.join_group("short-sermons", "plumer", kind="domain")
    _, report = collection_mod.create("vol", members=["short-sermons"], group="plumer",
                                      group_kind="series")
    assert "already exists as a domain" in report["joined"][0]["warning"]
    assert groups.load_groups()["plumer"]["kind"] == "domain"


def test_members_become_each_others_siblings(library, plumer):
    collection_mod.create("vol", members=["short-sermons", "plain-thoughts"])
    assert [s["slug"] for s in groups.siblings("plain-thoughts")] == ["short-sermons"]


def test_add_remove_reorder(library, plumer):
    make_project(library, "third", chapters=[("Three.", None)])
    coll, _ = collection_mod.create("vol", members=["short-sermons", "plain-thoughts"])

    collection_mod.add_member(coll, "third", position=1)
    assert _coll("vol").member_slugs == ["third", "short-sermons", "plain-thoughts"]
    assert "third" in groups.load_groups()["vol"]["members"]
    with pytest.raises(ValueError, match="already in"):
        collection_mod.add_member(coll, "third")

    collection_mod.reorder(coll, ["plain-thoughts", "third", "short-sermons"])
    assert _coll("vol").member_slugs == ["plain-thoughts", "third", "short-sermons"]
    with pytest.raises(ValueError, match="every member exactly once"):
        collection_mod.reorder(coll, ["plain-thoughts", "third"])
    with pytest.raises(ValueError, match="every member exactly once"):
        collection_mod.reorder(coll, ["plain-thoughts", "third", "third"])

    report = collection_mod.remove_member(coll, "third")
    assert _coll("vol").member_slugs == ["plain-thoughts", "short-sermons"]
    assert "third" not in groups.load_groups()["vol"]["members"]
    assert report["kept_group"] is None
    with pytest.raises(ValueError, match="not in"):
        collection_mod.remove_member(coll, "third")


def test_remove_keeps_the_group_when_another_collection_still_lists_the_member(library, plumer):
    groups.join_group("short-sermons", "plumer", kind="domain")
    first, _ = collection_mod.create("vol-a", members=["short-sermons", "plain-thoughts"],
                                     group="plumer")
    collection_mod.create("vol-b", members=["short-sermons"], group="plumer")

    report = collection_mod.remove_member(first, "short-sermons")
    assert "vol-b" in report["kept_group"]
    assert "short-sermons" in groups.load_groups()["plumer"]["members"]
    assert [c.slug for c in collection_mod.collections_of("short-sermons")] == ["vol-b"]


def test_a_member_may_sit_in_two_collections(library, plumer):
    collection_mod.create("vol-a", members=["short-sermons", "plain-thoughts"])
    collection_mod.create("vol-b", members=["short-sermons"])
    assert [c.slug for c in collection_mod.collections_of("short-sermons")] == ["vol-a", "vol-b"]


def test_stale_precedent_warning(library, plumer):
    sermons, story = plumer
    # plain-thoughts started drafting before it was bound: its snapshot knows no siblings.
    write_json(story / ".harness" / "precedent.json", {
        "schema": 2, "project": "plain-thoughts", "built_at": "2026-11-01T10:00:00",
        "siblings": [], "style": {}, "address": {}, "glossary": {},
    })
    _, report = collection_mod.create("vol", members=["short-sermons", "plain-thoughts"])
    assert report["stale_precedent"] == [{
        "member": "plain-thoughts", "built_at": "2026-11-01T10:00:00",
        "missing_siblings": ["short-sermons"], "next": "precedent --project plain-thoughts",
    }]

    flow.precedent("plain-thoughts")
    assert collection_mod.stale_precedent_warnings(_coll("vol")) == []


def test_part_title_defaults_to_the_spanish_title(library, plumer):
    coll, _ = collection_mod.create("vol", members=["short-sermons", "plain-thoughts"])
    assert collection_mod.part_title(coll.members[0]) == "Sermones cortos"
    doc = json.loads((coll.path / "collection.json").read_text(encoding="utf-8"))
    doc["members"][0]["part_title"] = "Sermones"
    doc["members"][0]["part_page"] = False
    write_json(coll.path / "collection.json", doc)
    member = _coll("vol").members[0]
    assert collection_mod.part_title(member) == "Sermones"
    assert member.part_page is False


def test_volume_metadata_derives_author_and_translator(library, plumer):
    from src.book_metadata import load_book_metadata

    coll, _ = collection_mod.create("vol", members=["short-sermons", "plain-thoughts"],
                                    title="Sermones y pensamientos")
    meta = load_book_metadata(coll.path)
    assert meta["title"] == "Sermones y pensamientos"
    assert meta["author"] == "William S. Plumer"
    assert meta["translator"] == "N. C. Sheetz"
    assert meta["language"] == "es"

    make_project(library, "other-author", chapters=[("X.", None)],
                 project={"author": "George MacDonald"})
    collection_mod.add_member(coll, "other-author")
    assert load_book_metadata(coll.path)["author"] == "William S. Plumer y George MacDonald"

    write_json(coll.path / "project.json", {"spanish_title": "V", "author": "Varios"})
    assert load_book_metadata(coll.path)["author"] == "Varios"


# ── harness commands ──────────────────────────────────────────────────────


def test_collection_status_reports_each_member(library, plumer):
    from web_ui.project_cards import clear_card_cache

    clear_card_cache()
    make_project(library, "half-done", chapters=[("One.", "Uno."), ("Two.", None)])
    flow.collection_create("vol", members=["short-sermons", "plain-thoughts", "half-done"])
    out = flow.collection_status("vol")

    rows = {r["slug"]: r for r in out["works"]}
    assert rows["short-sermons"]["stage"] == "fully-translated"
    assert rows["short-sermons"]["translated_pct"] == 100
    assert rows["short-sermons"]["chapters"] == "3/3"
    assert rows["short-sermons"]["part_title"] == "Sermones cortos"
    assert rows["half-done"]["stage"] == "partial"
    assert rows["half-done"]["translated_pct"] == 50
    assert set(rows["half-done"]["open_findings"]) == {
        "judge", "annotations_awaiting_review", "empty_footnotes"}
    assert out["ready_to_build"] is False
    assert "half-done" in out["next"]
    assert set(out) <= set(flow.OUTPUT_SCHEMAS["collection-status"])


def test_collection_status_flags_a_member_that_no_longer_resolves(library, plumer):
    coll, _ = collection_mod.create("vol", members=["short-sermons"])
    doc = json.loads((coll.path / "collection.json").read_text(encoding="utf-8"))
    doc["members"].append({"slug": "gone"})
    write_json(coll.path / "collection.json", doc)
    rows = flow.collection_status("vol")["works"]
    assert rows[-1] == {"slug": "gone", "missing": True, "stage": None}


def test_flow_payload_keys_are_documented(library, plumer):
    make_project(library, "third", chapters=[("Three.", None)])
    created = flow.collection_create("vol", members=["short-sermons", "plain-thoughts"])
    added = flow.collection_add("vol", member="third")
    reordered = flow.collection_reorder("vol", members=["third", "short-sermons", "plain-thoughts"])
    removed = flow.collection_remove("vol", member="third")
    for name, payload in [("collection-create", created), ("collection-add", added),
                          ("collection-reorder", reordered), ("collection-remove", removed)]:
        assert set(payload) <= set(flow.OUTPUT_SCHEMAS[name]), name


def test_the_cli_parses_the_collection_verbs():
    from scripts.harness import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["collection-create", "plumer-volumen", "--members", "a", "b",
                              "--group", "plumer", "--group-kind", "series"])
    assert (args.collection, args.members, args.group, args.group_kind) == (
        "plumer-volumen", ["a", "b"], "plumer", "series")
    args = parser.parse_args(["collection-epub", "plumer-volumen", "--allow-partial"])
    assert args.allow_partial is True


def test_a_blank_group_falls_back_to_the_slug(library, plumer):
    coll, report = collection_mod.create("vol", members=["short-sermons"], group="   ")
    assert coll.group == "vol"
    assert [j["group"] for j in report["joined"]] == ["vol"]


def test_a_corrupt_collection_json_resolves_like_a_book(library, plumer):
    from src.book_metadata import load_book_metadata

    coll, _ = collection_mod.create("vol", members=["short-sermons"], title="Volumen")
    (coll.path / "collection.json").write_text("{not json", encoding="utf-8")
    meta = load_book_metadata(coll.path)
    assert meta["title"] == "Volumen"
    assert meta["author"] == ""
