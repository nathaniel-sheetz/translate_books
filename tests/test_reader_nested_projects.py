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
def client(monkeypatch):
    monkeypatch.setattr(app_module, "_NESTED_PROJECT_CACHE", {})
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
    # Only one project card rendered despite two dirs sharing the same id.
    # Count "group-a" and "group-b" occurrences: group-b should be absent (skipped).
    assert b"group-b" not in rv.data


def test_iter_project_dirs_nonexistent_root(tmp_path):
    """_iter_project_dirs returns nothing (no error) when root doesn't exist."""
    from web_ui.app import _iter_project_dirs
    result = list(_iter_project_dirs(tmp_path / "does-not-exist"))
    assert result == []


def test_is_project_dir_via_source_txt(tmp_path):
    """_is_project_dir returns True for a dir that has source.txt (no chunks/)."""
    from web_ui.app import _is_project_dir
    d = tmp_path / "proj"
    d.mkdir()
    (d / "source.txt").write_text("text")
    assert _is_project_dir(d) is True


def test_is_project_dir_neither_marker(tmp_path):
    """_is_project_dir returns False when neither chunks/ nor source.txt present."""
    from web_ui.app import _is_project_dir
    d = tmp_path / "groupdir"
    d.mkdir()
    assert _is_project_dir(d) is False


def test_resolve_stale_cache_falls_through_to_scan(tmp_path, monkeypatch):
    """A stale cache entry (path deleted) causes a fresh scan instead of returning the bad path."""
    projects_dir = tmp_path / "projects"
    # Project must be *nested* (not flat) so the flat fast-path is skipped and the
    # stale-cache branch is actually reached.
    proj = _make_project(projects_dir / "some-group", "stale-book")
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    # Pre-populate cache with a non-existent path to simulate staleness.
    stale_path = tmp_path / "gone" / "stale-book"
    monkeypatch.setattr(app_module, "_NESTED_PROJECT_CACHE", {"stale-book": stale_path})
    # stale_path.is_dir() is False → cache miss → scan finds the nested project.
    result = app_module._resolve_project_dir("stale-book")
    assert result == proj


def test_create_project_dedup_against_nested_id(client, tmp_path, monkeypatch):
    """create_project does not reuse a slug already taken by a nested project."""
    projects_dir = tmp_path / "projects"
    # A nested project "my-book" lives under a grouping folder (not at flat root).
    _make_project(projects_dir / "by-author", "my-book")
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    monkeypatch.setattr(app_module, "_NESTED_PROJECT_CACHE", {})

    rv = client.post(
        "/api/projects/create",
        json={"title": "My Book"},
        content_type="application/json",
    )
    assert rv.status_code == 200
    data = rv.get_json()
    # The slug "my-book" is taken by the nested project; a suffix must be added.
    assert data["id"] != "my-book"
    assert data["id"].startswith("my-book-")


def test_iter_project_dirs_depth_limit(tmp_path):
    """_iter_project_dirs stops at depth > 20 and never yields deeply nested projects."""
    # Build a chain of 22 plain grouping folders; a project at the very bottom
    # must NOT be found because the depth guard fires first.
    current = tmp_path / "projects"
    for i in range(22):
        current = current / f"d{i}"
    target = current / "buried-book"
    (target / "chunks").mkdir(parents=True)

    found = list(_iter_project_dirs(tmp_path / "projects"))
    assert all(p.name != "buried-book" for p in found)


def test_iter_project_dirs_skips_plain_files(tmp_path):
    """Plain files inside the projects root (e.g. .gitkeep) are silently skipped."""
    projects_dir = tmp_path / "projects"
    _make_project(projects_dir, "good-book")
    (projects_dir / "notes.txt").write_text("not a project")

    found = [p.name for p in _iter_project_dirs(projects_dir)]
    assert found == ["good-book"]
    assert "notes.txt" not in found


def test_iter_project_dirs_default_root(tmp_path, monkeypatch):
    """Calling _iter_project_dirs() with no root argument uses _get_projects_dir()."""
    projects_dir = tmp_path / "projects"
    _make_project(projects_dir, "default-root-book")
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    monkeypatch.setattr(app_module, "_NESTED_PROJECT_CACHE", {})

    # root=None triggers the `root or _get_projects_dir()` default path.
    found = [p.name for p in _iter_project_dirs()]
    assert "default-root-book" in found


def test_score_difficulty_resolve_missing_project_exits(tmp_path, monkeypatch, capsys):
    """score_difficulty._resolve_project_dir calls sys.exit(1) for unknown projects."""
    import sys

    # Patch REPO_ROOT in state so the real projects/ dir is not consulted.
    from src.harness import state as state_mod
    monkeypatch.setattr(state_mod, "REPO_ROOT", tmp_path)
    (tmp_path / "projects").mkdir()

    # The monkeypatch on state_mod.REPO_ROOT takes effect at call time (lazy import
    # inside the function body), so no reload is needed or useful here.
    import scripts.score_difficulty as sd

    with pytest.raises(SystemExit) as exc_info:
        sd._resolve_project_dir("no-such-project")
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "no-such-project" in captured.err
