"""Tests for the book splitter, especially front/back-matter handling."""

import json
from pathlib import Path

import pytest

from src.book_splitter import (
    DetectedChapter,
    build_chapter_manifest,
    save_chapters_to_files,
    split_book_into_chapters,
)


# ---------------------------------------------------------------------------
# DetectedChapter backward compatibility
# ---------------------------------------------------------------------------

class TestDetectedChapter:
    def test_accepts_chapter_number_alias(self):
        ch = DetectedChapter(
            chapter_number=3,  # legacy kwarg
            chapter_title="Chapter III",
            content="x" * 100,
            start_line=0,
            end_line=10,
        )
        assert ch.position_index == 3
        assert ch.chapter_number == 3
        assert ch.kind == "chapter"

    def test_explicit_position_index(self):
        ch = DetectedChapter(
            position_index=2,
            chapter_title="Preface",
            content="hello",
            start_line=0,
            end_line=2,
            kind="front_matter",
            label="Preface",
        )
        assert ch.kind == "front_matter"
        assert ch.label == "Preface"
        assert ch.number is None


# ---------------------------------------------------------------------------
# split_book_into_chapters with front/back matter
# ---------------------------------------------------------------------------

CHAPTER_BODY = "lorem ipsum dolor sit amet " * 30


def _book_with_preface_and_epilogue() -> str:
    return (
        "Preface\n\n"
        + CHAPTER_BODY + "\n\n"
        + "Chapter I\n\n"
        + CHAPTER_BODY + "\n\n"
        + "Chapter II\n\n"
        + CHAPTER_BODY + "\n\n"
        + "Epilogue\n\n"
        + CHAPTER_BODY
    )


class TestFrontBackMatterDetection:
    def test_preface_chapter_chapter_epilogue(self):
        text = _book_with_preface_and_epilogue()
        sections = split_book_into_chapters(text, pattern_type="roman")
        kinds = [s.kind for s in sections]
        assert kinds == ["front_matter", "chapter", "chapter", "back_matter"]

        # Display chapter numbers restart at 1 after the preface
        chapter_numbers = [s.number for s in sections if s.kind == "chapter"]
        assert chapter_numbers == [1, 2]

        # Position index is reading-order
        assert [s.position_index for s in sections] == [1, 2, 3, 4]

        # Front/back matter labels are populated
        assert sections[0].label == "Preface"
        assert sections[-1].label == "Epilogue"

    def test_user_supplied_front_matter_title(self):
        text = (
            "To the Teacher\n\n"
            + CHAPTER_BODY + "\n\n"
            + "Chapter I\n\n"
            + CHAPTER_BODY + "\n\n"
            + "Chapter II\n\n"
            + CHAPTER_BODY
        )
        # 'To the Teacher' is NOT in the built-in keyword list, so the user
        # must declare it explicitly.
        sections = split_book_into_chapters(
            text,
            pattern_type="roman",
            front_matter_titles=["To the Teacher"],
        )
        assert sections[0].kind == "front_matter"
        assert sections[0].label == "To the Teacher"
        assert [s.kind for s in sections] == ["front_matter", "chapter", "chapter"]
        assert [s.number for s in sections if s.kind == "chapter"] == [1, 2]

    def test_no_front_matter_when_none_present(self):
        text = (
            "Chapter I\n\n" + CHAPTER_BODY + "\n\n"
            "Chapter II\n\n" + CHAPTER_BODY
        )
        sections = split_book_into_chapters(text, pattern_type="roman")
        assert all(s.kind == "chapter" for s in sections)
        assert [s.number for s in sections] == [1, 2]

    def test_disable_auto_front_matter(self):
        text = "Preface\n\n" + CHAPTER_BODY + "\n\n" + "Chapter I\n\n" + CHAPTER_BODY
        sections = split_book_into_chapters(
            text,
            pattern_type="roman",
            auto_detect_front_matter=False,
        )
        # Preface keyword ignored — only the chapter is detected
        assert all(s.kind == "chapter" for s in sections)

    def test_user_front_matter_overrides_allcaps_chapter_match(self):
        """Regression: with the all-caps pattern, a heading like
        'TO THE CHILDREN' is itself matched by the chapter regex. The user
        declaring it as front matter must take precedence so it isn't
        emitted as chapter 1."""
        text = (
            "TO THE CHILDREN\n\n"
            + CHAPTER_BODY + "\n\n"
            + "THE STORY THAT THE SWALLOW DIDN'T TELL\n\n"
            + CHAPTER_BODY + "\n\n"
            + "THE LAMB WITH THE LONGEST TAIL\n\n"
            + CHAPTER_BODY
        )
        sections = split_book_into_chapters(
            text,
            pattern_type="allcaps_heading",
            front_matter_titles=["TO THE CHILDREN"],
        )
        assert [s.kind for s in sections] == ["front_matter", "chapter", "chapter"]
        assert sections[0].label == "TO THE CHILDREN"
        # Real chapters renumber starting from 1
        assert [s.number for s in sections if s.kind == "chapter"] == [1, 2]

    def test_user_back_matter_overrides_allcaps_chapter_match(self):
        """Same regression for back matter: a user-declared back-matter
        title that the all-caps pattern would otherwise grab as a chapter
        must be demoted to back_matter."""
        # Leading "\n\n" so the all-caps lookbehind matches the very first
        # heading (real books always have something before chapter 1).
        text = (
            "\n\nTHE FIRST STORY\n\n"
            + CHAPTER_BODY + "\n\n"
            + "THE SECOND STORY\n\n"
            + CHAPTER_BODY + "\n\n"
            + "ABOUT THE AUTHOR\n\n"
            + CHAPTER_BODY
        )
        sections = split_book_into_chapters(
            text,
            pattern_type="allcaps_heading",
            back_matter_titles=["ABOUT THE AUTHOR"],
        )
        assert [s.kind for s in sections] == ["chapter", "chapter", "back_matter"]
        assert sections[-1].label == "ABOUT THE AUTHOR"
        assert [s.number for s in sections if s.kind == "chapter"] == [1, 2]


# ---------------------------------------------------------------------------
# save_chapters_to_files writes manifest
# ---------------------------------------------------------------------------

class TestSaveChaptersWritesManifest:
    def test_writes_manifest_to_project_json(self, tmp_path):
        text = _book_with_preface_and_epilogue()
        sections = split_book_into_chapters(text, pattern_type="roman")

        project_dir = tmp_path / "myproj"
        chapters_dir = project_dir / "chapters"
        save_chapters_to_files(sections, str(chapters_dir))

        # Files exist with reading-order names
        names = sorted(p.name for p in chapters_dir.iterdir())
        assert names == [
            "chapter_01.txt", "chapter_02.txt",
            "chapter_03.txt", "chapter_04.txt",
        ]

        # Manifest written into project.json
        proj_json = project_dir / "project.json"
        assert proj_json.exists()
        manifest = json.loads(proj_json.read_text(encoding="utf-8"))["chapter_manifest"]
        assert manifest == [
            {"id": "chapter_01", "kind": "front_matter", "label": "Preface"},
            {"id": "chapter_02", "kind": "chapter", "number": 1},
            {"id": "chapter_03", "kind": "chapter", "number": 2},
            {"id": "chapter_04", "kind": "back_matter", "label": "Epilogue"},
        ]

    def test_does_not_clobber_other_keys(self, tmp_path):
        project_dir = tmp_path / "myproj"
        chapters_dir = project_dir / "chapters"
        project_dir.mkdir()
        (project_dir / "project.json").write_text(
            json.dumps({"title": "My Book", "chapter_heading": {"label": "Capítulo"}}),
            encoding="utf-8",
        )

        sections = split_book_into_chapters(
            "Chapter I\n\n" + CHAPTER_BODY + "\n\n" + "Chapter II\n\n" + CHAPTER_BODY,
            pattern_type="roman",
        )
        save_chapters_to_files(sections, str(chapters_dir))

        data = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        assert data["title"] == "My Book"
        assert data["chapter_heading"] == {"label": "Capítulo"}
        assert "chapter_manifest" in data


class TestBuildChapterManifest:
    def test_basic(self):
        sections = [
            DetectedChapter(position_index=1, chapter_title="Preface",
                            content="x"*10, start_line=0, end_line=1,
                            kind="front_matter", label="Preface"),
            DetectedChapter(position_index=2, chapter_title="Chapter I",
                            content="x"*10, start_line=2, end_line=3,
                            kind="chapter", number=1),
        ]
        m = build_chapter_manifest(sections)
        assert m == [
            {"id": "chapter_01", "kind": "front_matter", "label": "Preface"},
            {"id": "chapter_02", "kind": "chapter", "number": 1},
        ]
