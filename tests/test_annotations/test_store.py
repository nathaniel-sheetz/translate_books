"""The append-only / tombstone / latest-wins rule shared by reader, endnotes and CLI."""

from __future__ import annotations

import json

from src.annotations import store

from tests.test_annotations.conftest import write_annotations


def _ann(chapter="chapter_01", es_idx=0, **kw):
    base = {
        "project_id": "testbook",
        "chapter_id": chapter,
        "es_idx": es_idx,
        "type": "word_choice",
        "content": "",
        "timestamp": "2026-01-01T00:00:00",
    }
    base.update(kw)
    return base


def test_latest_record_at_a_key_wins(project):
    write_annotations(
        project,
        [
            _ann(content="first", timestamp="2026-01-01T00:00:00", sub_id="u1"),
            _ann(content="second", timestamp="2026-01-02T00:00:00", sub_id="u1"),
        ],
    )
    active = store.load_active(project)
    assert [a["content"] for a in active] == ["second"]


def test_tombstone_removes_only_its_own_sub_id(project):
    write_annotations(
        project,
        [
            _ann(content="keep", sub_id="u1"),
            _ann(content="drop", sub_id="u2"),
            {"chapter_id": "chapter_01", "es_idx": 0, "sub_id": "u2", "removed": True,
             "timestamp": "2026-01-03T00:00:00"},
        ],
    )
    active = store.load_active(project)
    assert [a["content"] for a in active] == ["keep"]


def test_legacy_rows_and_the_legacy_sentinel_share_one_slot(project):
    """A row with no sub_id and one with sub_id "legacy" address the same slot."""
    write_annotations(
        project,
        [
            _ann(content="original", timestamp="2026-01-01T00:00:00"),
            _ann(content="edited", timestamp="2026-01-02T00:00:00", sub_id="legacy"),
        ],
    )
    active = store.load_active(project)
    assert [a["content"] for a in active] == ["edited"]


def test_final_type_at_a_key_decides(project):
    """A later non-footnote edit supersedes an earlier footnote."""
    write_annotations(
        project,
        [
            _ann(content="x", type="footnote", timestamp="2026-01-01T00:00:00", sub_id="u1"),
            _ann(content="x", type="flag", timestamp="2026-01-02T00:00:00", sub_id="u1"),
        ],
    )
    assert store.load_active(project, types=("footnote",)) == []
    assert len(store.load_active(project, types=("flag",))) == 1


def test_unparseable_line_is_skipped_not_fatal(project):
    path = project / "annotations.jsonl"
    path.write_text(
        json.dumps(_ann(content="good", sub_id="u1")) + "\n{ this is not json\n",
        encoding="utf-8",
    )
    assert [a["content"] for a in store.load_active(project)] == ["good"]


def test_records_without_es_idx_are_ignored(project):
    write_annotations(project, [_ann(content="orphan", es_idx=None, sub_id="u1")])
    assert store.load_active(project) == []


def test_chapter_and_type_filters(project):
    write_annotations(
        project,
        [
            _ann(chapter="chapter_01", content="a", sub_id="u1"),
            _ann(chapter="chapter_02", content="b", sub_id="u2"),
            _ann(chapter="chapter_01", content="c", type="footnote", sub_id="u3"),
        ],
    )
    assert len(store.load_active(project, chapter_id="chapter_01")) == 2
    assert len(store.load_active(project, types=("footnote",))) == 1


def test_missing_file_is_empty_not_an_error(project):
    assert store.load_active(project) == []


def test_append_record_never_rewrites(project):
    write_annotations(project, [_ann(content="one", sub_id="u1")])
    before = (project / "annotations.jsonl").read_text(encoding="utf-8")
    store.append_record(project, _ann(content="two", sub_id="u1"))
    after = (project / "annotations.jsonl").read_text(encoding="utf-8")
    assert after.startswith(before)
    assert len(after.strip().splitlines()) == 2


def test_target_key_is_filename_safe(project):
    key = store.target_key(_ann(es_idx=37, sub_id="u72399176"))
    assert key == "chapter_01__37__u72399176"
    # Must satisfy the same id charset the judge scope enforces.
    import re

    assert re.fullmatch(r"[A-Za-z0-9_\-]+", key)


def test_target_key_for_legacy_row(project):
    assert store.target_key(_ann(es_idx=4)) == "chapter_01__4__legacy"
