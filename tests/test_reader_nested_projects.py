"""Tests for nested-project discovery and resolution (grouping subfolders).

Projects may live directly under ``projects/`` (flat) or inside arbitrary
grouping subfolders (one or more levels deep). Discovery and path resolution
must find a project wherever its folder lives, keyed by the leaf folder name.
"""

import logging
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web_ui.app as app_module
from web_ui.app import app, _iter_project_dirs, _resolve_project_dir


def _make_project(parent: Path, name: str) -> Path:
    """Create a minimal project dir; chunks/ marks it as a project."""
    d = parent / name
    (d / "chunks").mkdir(parents=True)
    return d


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def nested_projects(tmp_path, monkeypatch):
    """Projects laid out flat, one level deep, and two levels deep."""
    projects_dir = tmp_path / "projects"
    flat = _make_project(projects_dir, "flat-book")
    one_deep = _make_project(projects_dir / "by-author", "one-deep-book")
    two_deep = _make_project(projects_dir / "experimental" / "drafts", "two-deep-book")

    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    # Fresh cache per test so a stale leaf->path mapping can't leak across tests.
    monkeypatch.setattr(app_module, "_NESTED_PROJECT_CACHE", {})
    return {
        "projects_dir": projects_dir,
        "flat": flat,
        "one_deep": one_deep,
        "two_deep": two_deep,
    }


def test_iter_finds_projects_at_every_depth(nested_projects):
    found = {p.name: p for p in _iter_project_dirs(nested_projects["projects_dir"])}
    assert set(found) == {"flat-book", "one-deep-book", "two-deep-book"}
    assert found["flat-book"] == nested_projects["flat"]
    assert found["one-deep-book"] == nested_projects["one_deep"]
    assert found["two-deep-book"] == nested_projects["two_deep"]


def test_iter_skips_grouping_folders(nested_projects):
    """Grouping/container folders (no chunks/ or source.txt) are not projects."""
    names = [p.name for p in _iter_project_dirs(nested_projects["projects_dir"])]
    assert "by-author" not in names
    assert "experimental" not in names
    assert "drafts" not in names


def test_iter_does_not_descend_into_a_project(tmp_path, monkeypatch):
    """A subdir inside a project (e.g. chunks/) is never yielded as a project."""
    projects_dir = tmp_path / "projects"
    proj = _make_project(projects_dir, "book")
    # An inner dir that itself looks like a project must not be discovered.
    (proj / "chunks" / "chunks").mkdir(parents=True)
    found = [p.name for p in _iter_project_dirs(projects_dir)]
    assert found == ["book"]


def test_resolve_flat_and_nested(nested_projects):
    assert _resolve_project_dir("flat-book") == nested_projects["flat"]
    assert _resolve_project_dir("one-deep-book") == nested_projects["one_deep"]
    assert _resolve_project_dir("two-deep-book") == nested_projects["two_deep"]


def test_resolve_uses_cache_after_first_lookup(nested_projects):
    # First lookup populates the cache; the cached entry then drives resolution.
    assert _resolve_project_dir("two-deep-book") == nested_projects["two_deep"]
    assert app_module._NESTED_PROJECT_CACHE["two-deep-book"] == nested_projects["two_deep"]


def test_resolve_unknown_falls_back_to_flat(nested_projects):
    """Unknown id falls back to the flat path so `.exists()` 404 checks hold."""
    resolved = _resolve_project_dir("does-not-exist")
    assert resolved == nested_projects["projects_dir"] / "does-not-exist"
    assert not resolved.exists()


def test_reader_lists_projects_at_every_depth(client, nested_projects):
    rv = client.get("/read/")
    assert rv.status_code == 200
    assert b"flat-book" in rv.data
    assert b"one-deep-book" in rv.data
    assert b"two-deep-book" in rv.data


def test_duplicate_id_warns_and_dedups(client, tmp_path, monkeypatch, caplog):
    """Two folders sharing a leaf name: one card shown, a warning logged."""
    projects_dir = tmp_path / "projects"
    _make_project(projects_dir / "group-a", "dup-book")
    _make_project(projects_dir / "group-b", "dup-book")
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    monkeypatch.setattr(app_module, "_NESTED_PROJECT_CACHE", {})

    with caplog.at_level(logging.WARNING):
        rv = client.get("/read/")
    assert rv.status_code == 200
    assert "Duplicate project id" in caplog.text
