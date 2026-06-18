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
