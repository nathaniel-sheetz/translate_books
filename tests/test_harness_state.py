"""Tests for harness project-dir resolution, including nested grouping folders.

``resolve_project_dir`` accepts a bare project id or a path. A bare id that
isn't at the flat ``projects/<id>`` root is now searched for inside arbitrary
grouping subfolders so ``translate-harness --project <id>`` keeps working after
a book is moved into a group.
"""

from pathlib import Path

import pytest

from src.harness import state


def _make_project(parent: Path, name: str) -> Path:
    d = parent / name
    (d / "chunks").mkdir(parents=True)
    return d


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "REPO_ROOT", tmp_path)
    (tmp_path / "projects").mkdir()
    return tmp_path


def test_resolve_flat_id(repo):
    flat = _make_project(repo / "projects", "flat-book")
    assert state.resolve_project_dir("flat-book") == flat


def test_resolve_nested_id_one_level(repo):
    nested = _make_project(repo / "projects" / "by-author", "fabre2")
    assert state.resolve_project_dir("fabre2") == nested


def test_resolve_nested_id_two_levels(repo):
    nested = _make_project(repo / "projects" / "experimental" / "drafts", "deep-book")
    assert state.resolve_project_dir("deep-book") == nested


def test_nested_search_ignores_non_project_dirs(repo):
    # A grouping folder that merely shares the id's name is not a project dir
    # (no chunks/ or source.txt) and must not be returned.
    (repo / "projects" / "group" / "decoy").mkdir(parents=True)
    real = _make_project(repo / "projects" / "elsewhere", "decoy")
    assert state.resolve_project_dir("decoy") == real


def test_unknown_id_raises_when_must_exist(repo):
    with pytest.raises(FileNotFoundError):
        state.resolve_project_dir("nope")


def test_unknown_id_returns_flat_candidate_when_optional(repo):
    resolved = state.resolve_project_dir("nope", must_exist=False)
    assert resolved == repo / "projects" / "nope"


def test_explicit_path_passthrough(repo, tmp_path):
    # A path with a separator is treated as a direct path, not a bare id.
    explicit = tmp_path / "somewhere" / "book"
    explicit.mkdir(parents=True)
    assert state.resolve_project_dir(str(explicit)) == Path(str(explicit))


def test_resolve_nested_via_source_txt(repo):
    """A project identified by source.txt (not chunks/) is found in nested search."""
    d = repo / "projects" / "by-genre" / "source-only-book"
    d.mkdir(parents=True)
    (d / "source.txt").write_text("content")
    assert state.resolve_project_dir("source-only-book") == d


def test_resolve_when_projects_root_missing_must_exist_false(tmp_path, monkeypatch):
    """With no projects/ dir at all and must_exist=False, returns flat candidate."""
    monkeypatch.setattr(state, "REPO_ROOT", tmp_path)
    # Don't create tmp_path / "projects" at all.
    result = state.resolve_project_dir("ghost", must_exist=False)
    assert result == tmp_path / "projects" / "ghost"


def test_resolve_when_projects_root_missing_must_exist_true(tmp_path, monkeypatch):
    """With no projects/ dir at all and must_exist=True, FileNotFoundError is raised."""
    monkeypatch.setattr(state, "REPO_ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        state.resolve_project_dir("ghost")


def test_explicit_path_not_exist_raises(repo, tmp_path):
    """An explicit path (with separator) that doesn't exist raises FileNotFoundError."""
    missing = tmp_path / "no-such" / "dir"
    # must_exist=True is the default
    with pytest.raises(FileNotFoundError, match="project path not found"):
        state.resolve_project_dir(str(missing))


def test_duplicate_nested_id_warns(repo, caplog):
    """Two nested project dirs with the same leaf name trigger a warning; first is returned."""
    import logging

    dupe_a = _make_project(repo / "projects" / "group-a", "twin-book")
    _make_project(repo / "projects" / "group-b", "twin-book")

    with caplog.at_level(logging.WARNING, logger="src.harness.state"):
        result = state.resolve_project_dir("twin-book")

    assert result == dupe_a
    assert "Duplicate project id" in caplog.text
