"""Tests for the chapter_manifest migration script."""

import json
from pathlib import Path

import pytest

from scripts.build_chapter_manifest import (
    build_manifest_for_project,
    classify_chapter_file,
    write_manifest,
)
from src.book_splitter import _compile_matter_patterns


def _make_project(tmp_path: Path, files: dict) -> Path:
    project = tmp_path / "proj"
    chapters = project / "chapters"
    chapters.mkdir(parents=True)
    for name, body in files.items():
        (chapters / name).write_text(body, encoding="utf-8")
    return project


class TestClassify:
    def setup_method(self):
        self.front = _compile_matter_patterns("front_matter_patterns")
        self.back = _compile_matter_patterns("back_matter_patterns")

    def test_built_in_preface(self):
        info = classify_chapter_file(
            "Preface\n\nBody.", front_titles=[], back_titles=[],
            front_patterns=self.front, back_patterns=self.back,
        )
        assert info["kind"] == "front_matter"
        assert info["label"] == "Preface"

    def test_chapter_default(self):
        info = classify_chapter_file(
            "CHAPTER I\n\nBody.", front_titles=[], back_titles=[],
            front_patterns=self.front, back_patterns=self.back,
        )
        assert info["kind"] == "chapter"

    def test_user_supplied_title(self):
        info = classify_chapter_file(
            "To the Teacher\n\nBody.", front_titles=["To the Teacher"], back_titles=[],
            front_patterns=self.front, back_patterns=self.back,
        )
        assert info["kind"] == "front_matter"
        assert info["label"] == "To the Teacher"

    def test_built_in_epilogue(self):
        info = classify_chapter_file(
            "Epilogue\n\nThe end.", front_titles=[], back_titles=[],
            front_patterns=self.front, back_patterns=self.back,
        )
        assert info["kind"] == "back_matter"
        assert info["label"] == "Epilogue"


class TestBuildManifest:
    def test_renumbers_chapters_starting_at_one(self, tmp_path):
        project = _make_project(tmp_path, {
            "chapter_01.txt": "Preface\n\nBody.",
            "chapter_02.txt": "CHAPTER I\n\nFirst.",
            "chapter_03.txt": "CHAPTER II\n\nSecond.",
            "chapter_04.txt": "Epilogue\n\nClosing.",
        })
        manifest = build_manifest_for_project(
            project, front_titles=[], back_titles=[],
        )
        assert manifest == [
            {"id": "chapter_01", "kind": "front_matter", "label": "Preface"},
            {"id": "chapter_02", "kind": "chapter", "number": 1},
            {"id": "chapter_03", "kind": "chapter", "number": 2},
            {"id": "chapter_04", "kind": "back_matter", "label": "Epilogue"},
        ]

    def test_demotes_misplaced_front_matter(self, tmp_path):
        # A "Preface"-like heading appearing AFTER a chapter is demoted.
        project = _make_project(tmp_path, {
            "chapter_01.txt": "CHAPTER I\n\nFirst.",
            "chapter_02.txt": "Preface\n\nOdd later body.",
        })
        manifest = build_manifest_for_project(
            project, front_titles=[], back_titles=[],
        )
        kinds = [e["kind"] for e in manifest]
        assert kinds == ["chapter", "chapter"]
        assert [e.get("number") for e in manifest] == [1, 2]

    def test_no_chapters_directory_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            build_manifest_for_project(tmp_path, front_titles=[], back_titles=[])

    def test_write_manifest_preserves_other_keys(self, tmp_path):
        project = _make_project(tmp_path, {
            "chapter_01.txt": "CHAPTER I\n\nFirst.",
        })
        (project / "project.json").write_text(
            json.dumps({"title": "MyBook"}), encoding="utf-8"
        )
        manifest = build_manifest_for_project(
            project, front_titles=[], back_titles=[],
        )
        write_manifest(project, manifest)

        data = json.loads((project / "project.json").read_text(encoding="utf-8"))
        assert data["title"] == "MyBook"
        assert data["chapter_manifest"] == manifest
