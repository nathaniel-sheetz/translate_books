"""Tests for the book splitter, especially front/back-matter handling."""

import json
from pathlib import Path

import pytest

from src.book_splitter import (
    DetectedChapter,
    build_chapter_manifest,
    detect_pattern_from_text,
    save_chapters_to_files,
    split_book_into_chapters,
    split_sanity_warnings,
)

_BODY_WORDS = "lorem ipsum dolor sit amet " * 40  # ~200-word body


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

    def test_roman_chapter_title_after_header_image(self):
        """Regression (#16): CHAPTER I / [IMAGE] / title on separate line."""
        text = (
            "CHAPTER I\n\n"
            "[IMAGE:images/illus01.jpg]\n\n"
            "The First Signpost\n\n"
            + CHAPTER_BODY + "\n\n"
            "CHAPTER II\n\n"
            + CHAPTER_BODY
        )
        sections = split_book_into_chapters(text, pattern_type="roman")
        assert len(sections) == 2
        ch1 = sections[0]
        assert ch1.chapter_title == "Chapter I\nThe First Signpost"
        assert "[IMAGE:images/illus01.jpg]" in ch1.content
        assert "The First Signpost" not in ch1.content

    def test_roman_chapter_title_with_header_image_before_heading(self):
        """Gaudenzia layout: [IMAGE] / CHAPTER I / title on separate line."""
        text = (
            "[IMAGE:images/illus7.jpg]\n\n"
            "CHAPTER I\n\n"
            "The First Signpost\n\n"
            + CHAPTER_BODY + "\n\n"
            "CHAPTER II\n\n"
            + CHAPTER_BODY
        )
        sections = split_book_into_chapters(text, pattern_type="roman")
        ch1 = sections[0]
        assert ch1.chapter_title == "Chapter I\nThe First Signpost"
        assert "[IMAGE:images/illus7.jpg]" in ch1.content
        assert ch1.content.count("[IMAGE:images/illus7.jpg]") == 1
        assert "The First Signpost" not in ch1.content

    def test_does_not_promote_paragraph_after_header_image(self):
        """A long prose line after the image must not become a subtitle."""
        long_para = (
            "This is the opening paragraph of the chapter and it continues "
            "on the same block without a blank line break after the image."
        )
        text = (
            "CHAPTER I\n\n"
            "[IMAGE:images/illus01.jpg]\n\n"
            f"{long_para}\n\n"
            + CHAPTER_BODY
        )
        sections = split_book_into_chapters(text, pattern_type="roman")
        ch1 = sections[0]
        assert ch1.chapter_title == "Chapter I"
        assert long_para in ch1.content

    def test_plain_roman_does_not_promote_body_paragraph(self):
        """No image at all: a normal long opening paragraph stays in the body."""
        text = (
            "CHAPTER I\n\n"
            + CHAPTER_BODY + "\n\n"
            "CHAPTER II\n\n"
            + CHAPTER_BODY
        )
        sections = split_book_into_chapters(text, pattern_type="roman")
        ch1 = sections[0]
        assert ch1.chapter_title == "Chapter I"
        assert "\n" not in ch1.chapter_title
        assert ch1.content.startswith(CHAPTER_BODY[:20])

    def test_numeric_chapter_title_on_separate_line(self):
        """#16 extends to numeric chapters: Chapter 1 / title on next line."""
        text = (
            "Chapter 1\n\n"
            "The Beginning\n\n"
            + CHAPTER_BODY + "\n\n"
            "Chapter 2\n\n"
            + CHAPTER_BODY
        )
        sections = split_book_into_chapters(text, pattern_type="numeric")
        chapters = [s for s in sections if s.kind == "chapter"]
        assert chapters[0].chapter_title == "Chapter 1\nThe Beginning"
        assert [c.number for c in chapters] == [1, 2]
        assert "The Beginning" not in chapters[0].content

    def test_bare_roman_chapter_title_on_separate_line(self):
        """Bare-roman headings (numbering='roman') also capture a next-line title."""
        text = (
            "I\n\n"
            "Una and the Lion\n\n"
            + CHAPTER_BODY + "\n\n"
            "II\n\n"
            + CHAPTER_BODY
        )
        sections = split_book_into_chapters(text, pattern_type="bare_roman")
        chapters = [s for s in sections if s.kind == "chapter"]
        assert chapters[0].chapter_title == "Chapter I\nUna and the Lion"
        assert [c.number for c in chapters] == [1, 2]
        # The second chapter has no separate title line and must stay clean.
        assert chapters[1].chapter_title == "Chapter II"

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

    def test_declared_boilerplate_is_stripped_not_kept(self):
        """Regression (#13): force-tagging CONTENTS as front matter must NOT
        keep it. Boilerplate is auto-stripped (never written/numbered) and the
        real chapters renumber from 1 with no offset."""
        text = (
            "Contents\n\n"
            + "Foreword .... 1\nChapter I .... 5\n\n"
            + "Foreword\n\n"
            + CHAPTER_BODY + "\n\n"
            + "Chapter I\n\n"
            + CHAPTER_BODY + "\n\n"
            + "Chapter II\n\n"
            + CHAPTER_BODY
        )
        dropped = []
        sections = split_book_into_chapters(
            text,
            pattern_type="roman",
            front_matter_titles=["Contents"],  # the friction-log mistake
            collect_dropped=dropped,
        )
        assert [s.kind for s in sections] == ["front_matter", "chapter", "chapter"]
        assert sections[0].label == "Foreword"  # real front matter kept
        assert [s.number for s in sections if s.kind == "chapter"] == [1, 2]
        assert dropped == [{"label": "Contents", "reason": "boilerplate"}]

    def test_auto_strip_off_keeps_boilerplate(self):
        """The --no-auto-strip escape hatch keeps a declared 'Contents'."""
        text = (
            "Contents\n\n"
            + CHAPTER_BODY + "\n\n"
            + "Chapter I\n\n"
            + CHAPTER_BODY
        )
        dropped = []
        sections = split_book_into_chapters(
            text,
            pattern_type="roman",
            front_matter_titles=["Contents"],
            auto_strip_boilerplate=False,
            collect_dropped=dropped,
        )
        assert [s.kind for s in sections] == ["front_matter", "chapter"]
        assert sections[0].label == "Contents"
        assert dropped == []

    def test_boilerplate_matched_as_chapter_is_dropped(self):
        """An all-caps 'CONTENTS' the chapter regex grabs is still stripped."""
        text = (
            "\n\nCONTENTS\n\n"
            + "Early Days\nLater Days\n\n"
            + "EARLY DAYS\n\n"
            + CHAPTER_BODY + "\n\n"
            + "LATER DAYS\n\n"
            + CHAPTER_BODY
        )
        dropped = []
        sections = split_book_into_chapters(
            text,
            pattern_type="allcaps_heading",
            collect_dropped=dropped,
        )
        assert [s.kind for s in sections] == ["chapter", "chapter"]
        assert [s.chapter_title for s in sections] == ["EARLY DAYS", "LATER DAYS"]
        assert [s.number for s in sections] == [1, 2]
        assert dropped == [{"label": "Contents", "reason": "boilerplate"}]

    def test_undeclared_front_matter_boilerplate_is_recorded(self):
        """Regression (friction log #28): standalone Contents / List of
        Illustrations / Copyright headings before the first chapter — not
        declared as front_matter_titles and not grabbed by the roman pattern —
        must still be recorded in `dropped`, not discarded silently by position.
        Before the fix `dropped` came back empty even though the strip happened,
        so SKILL.md's 'confirm `dropped`' step was impossible to perform."""
        text = (
            "The Red Mustang\n\n"
            + "by William O. Stoddard\n\n"
            + "Contents\n\n"
            + "Chapter I. The Horse .... 5\nChapter II. The Rider .... 20\n\n"
            + "List of Illustrations\n\n"
            + "The Red Mustang ... frontispiece\n\n"
            + "Copyright\n\n"
            + "Copyright 1890 by Harper & Brothers\n\n"
            + "Chapter I\n\n"
            + CHAPTER_BODY + "\n\n"
            + "Chapter II\n\n"
            + CHAPTER_BODY
        )
        dropped = []
        sections = split_book_into_chapters(
            text,
            pattern_type="roman",
            collect_dropped=dropped,
        )
        # The two real chapters survive and renumber from 1; nothing kept as matter.
        assert [s.kind for s in sections] == ["chapter", "chapter"]
        assert [s.number for s in sections] == [1, 2]
        # Every boilerplate heading is accounted for, in document order.
        assert dropped == [
            {"label": "Contents", "reason": "boilerplate"},
            {"label": "List Of Illustrations", "reason": "boilerplate"},
            {"label": "Copyright", "reason": "boilerplate"},
        ]


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


# ---------------------------------------------------------------------------
# Text-based pattern detection (local source.txt path — friction #1)
# ---------------------------------------------------------------------------

def _two_sections(h1: str, h2: str) -> str:
    return f"{h1}\n\n{_BODY_WORDS}\n\n{h2}\n\n{_BODY_WORDS}"


class TestDetectPatternFromText:
    def test_same_line_titled_roman_headings(self):
        # The exact Photogen shape that mis-split under the 'roman' pattern.
        text = _two_sections("CHAPTER I. WATHO.", "CHAPTER II. AURORA.")
        assert detect_pattern_from_text(text) == "chapter_roman_titled"

    def test_bare_numeral_roman_headings(self):
        text = _two_sections("CHAPTER I", "CHAPTER II")
        # The titled pattern's title is optional, so it subsumes numeral-only.
        assert detect_pattern_from_text(text) == "chapter_roman_titled"

    def test_numeric_titled_headings(self):
        text = _two_sections("Chapter 1 The Start", "Chapter 2 The End")
        assert detect_pattern_from_text(text) == "chapter_numeric_titled"

    def test_no_headings_returns_none(self):
        assert detect_pattern_from_text("Just prose, no headings at all. " * 50) is None

    def test_empty_returns_none(self):
        assert detect_pattern_from_text("") is None
        assert detect_pattern_from_text("   \n\n  ") is None


class TestAutoSplit:
    def test_auto_resolves_titled_and_keeps_all_chapters(self):
        # Mixed: one heading carries its title inline, one is numeral-only with
        # the title on the next line — auto must catch both (20-vs-19 bug).
        text = (
            "CHAPTER I. WATHO.\n\n" + _BODY_WORDS + "\n\n"
            "CHAPTER II\nAURORA.\n\n" + _BODY_WORDS
        )
        chapters = split_book_into_chapters(text, pattern_type="auto")
        chaps = [c for c in chapters if c.kind == "chapter"]
        assert len(chaps) == 2
        assert "WATHO" in chaps[0].chapter_title
        assert "AURORA" in chaps[1].chapter_title  # pulled from the next line

    def test_auto_falls_back_to_roman_when_unconfident(self):
        # No recognizable headings: auto falls back to 'roman' rather than raising.
        text = "CHAPTER I\n\n" + _BODY_WORDS  # single heading, below the >=2 floor
        chapters = split_book_into_chapters(text, pattern_type="auto")
        assert [c for c in chapters if c.kind == "chapter"]


class TestSplitSanityWarnings:
    def test_flags_single_chapter_large_source(self):
        text = "x" * 30_000
        chapters = [DetectedChapter(position_index=1, chapter_title="Chapter XVII",
                                    content=text, start_line=0, end_line=1,
                                    kind="chapter", number=17)]
        warns = split_sanity_warnings(chapters, text, pattern_used="roman",
                                      detected="chapter_roman_titled")
        assert warns and "chapter_roman_titled" in warns[0]

    def test_clean_when_multiple_chapters(self):
        text = _two_sections("CHAPTER I. A", "CHAPTER II. B")
        chapters = split_book_into_chapters(text, pattern_type="auto")
        assert split_sanity_warnings(chapters, text, pattern_used="chapter_roman_titled",
                                     detected="chapter_roman_titled") == []

    def test_pure_fallback_emits_only_the_fallback_message(self):
        # auto found nothing (detected is None) -> resolved to 'roman' on a large
        # source that under-splits. Exactly one advisory fires: the actionable
        # "fell back to roman" message, not the generic under-split one too.
        text = "x" * 30_000
        chapters = [DetectedChapter(position_index=1, chapter_title="",
                                    content=text, start_line=0, end_line=1,
                                    kind="chapter", number=1)]
        warns = split_sanity_warnings(chapters, text, pattern_used="roman",
                                      detected=None)
        assert len(warns) == 1
        assert "fell back to" in warns[0]
        assert "may be wrong" not in warns[0]
