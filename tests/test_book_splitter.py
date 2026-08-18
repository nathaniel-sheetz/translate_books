"""Tests for the book splitter, especially front/back-matter handling."""

import json
from pathlib import Path

import pytest

from src.book_splitter import (
    DetectedChapter,
    build_chapter_manifest,
    detect_pattern_from_text,
    get_chapter_pattern,
    load_heading_outline,
    locate_headings,
    resolve_pattern_type,
    save_chapters_to_files,
    select_heading_level,
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

    def test_header_ornament_caption_travels_into_its_chapter(self):
        """The ornament's caption belongs to the chapter it decorates.

        The boundary moves back past both image and caption, so a caption left
        out of the payload would be deleted from the book entirely.
        """
        text = (
            "CHAPTER I\n\n"
            + CHAPTER_BODY + "\n\n"
            "[IMAGE:images/illus7.jpg]\n\n"
            "[CAPTION] The mare at the gate.\n\n"
            "CHAPTER II\n\n"
            + CHAPTER_BODY
        )
        sections = split_book_into_chapters(text, pattern_type="roman")
        assert len(sections) == 2
        ch1, ch2 = sections
        assert "[CAPTION] The mare at the gate." not in ch1.content
        assert "[IMAGE:images/illus7.jpg]" not in ch1.content
        assert "[IMAGE:images/illus7.jpg]" in ch2.content
        assert "[CAPTION] The mare at the gate." in ch2.content
        # And the caption did not become the chapter's title.
        assert ch2.chapter_title == "Chapter II"

    def test_header_ornament_caption_ending_in_bracket_is_still_a_caption(self):
        """Caption detection is on the leading marker, not the last character."""
        text = (
            "CHAPTER I\n\n"
            + CHAPTER_BODY + "\n\n"
            "[IMAGE:images/illus7.jpg]\n\n"
            "[CAPTION] The mare at the gate [Fig. 1]\n\n"
            "CHAPTER II\n\n"
            + CHAPTER_BODY
        )
        sections = split_book_into_chapters(text, pattern_type="roman")
        ch1, ch2 = sections
        assert "[IMAGE:images/illus7.jpg]" not in ch1.content
        assert "[CAPTION] The mare at the gate [Fig. 1]" not in ch1.content
        assert "[IMAGE:images/illus7.jpg]" in ch2.content
        assert "[CAPTION] The mare at the gate [Fig. 1]" in ch2.content

    def test_caption_before_heading_does_not_become_the_title(self):
        """[IMAGE]/[CAPTION]/CHAPTER I/title: the real title still wins."""
        text = (
            "[IMAGE:images/illus7.jpg]\n\n"
            "[CAPTION] The mare at the gate.\n\n"
            "CHAPTER I\n\n"
            "The First Signpost\n\n"
            + CHAPTER_BODY
        )
        sections = split_book_into_chapters(text, pattern_type="roman")
        ch1 = sections[0]
        assert ch1.chapter_title == "Chapter I\nThe First Signpost"
        assert "[CAPTION] The mare at the gate." in ch1.content
        assert "The First Signpost" not in ch1.content

    def test_caption_after_heading_image_does_not_become_the_title(self):
        """CHAPTER I/[IMAGE]/[CAPTION]: the caption stays in the body."""
        text = (
            "CHAPTER I\n\n"
            "[IMAGE:images/illus01.jpg]\n\n"
            "[CAPTION] The mare at the gate.\n\n"
            + CHAPTER_BODY
        )
        sections = split_book_into_chapters(text, pattern_type="roman")
        ch1 = sections[0]
        assert ch1.chapter_title == "Chapter I"
        assert "[CAPTION] The mare at the gate." in ch1.content

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

    def test_clears_stale_files_from_a_prior_larger_split(self, tmp_path):
        project_dir = tmp_path / "myproj"
        chapters_dir = project_dir / "chapters"
        chapters_dir.mkdir(parents=True)
        (chapters_dir / "chapter_99.txt").write_text("stale", encoding="utf-8")
        (chapters_dir / "notes.txt").write_text("keep", encoding="utf-8")

        sections = split_book_into_chapters(
            "Chapter I\n\n" + CHAPTER_BODY + "\n\n" + "Chapter II\n\n" + CHAPTER_BODY,
            pattern_type="roman",
        )
        save_chapters_to_files(sections, str(chapters_dir))

        names = sorted(p.name for p in chapters_dir.iterdir())
        assert "chapter_99.txt" not in names
        assert "notes.txt" in names
        assert "chapter_01.txt" in names
        assert "chapter_02.txt" in names

    def test_clear_existing_false_keeps_unrelated_chapter_files(self, tmp_path):
        project_dir = tmp_path / "myproj"
        chapters_dir = project_dir / "chapters"
        chapters_dir.mkdir(parents=True)
        (chapters_dir / "chapter_99.txt").write_text("stale", encoding="utf-8")

        sections = split_book_into_chapters(
            "Chapter I\n\n" + CHAPTER_BODY + "\n\n" + "Chapter II\n\n" + CHAPTER_BODY,
            pattern_type="roman",
        )
        save_chapters_to_files(sections, str(chapters_dir), clear_existing=False)
        assert (chapters_dir / "chapter_99.txt").exists()


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


# ---------------------------------------------------------------------------
# Heading-outline splitting
# ---------------------------------------------------------------------------

_STORY = "lorem ipsum dolor sit amet " * 40  # ~1,080 chars, comfortably a chapter


def _outline_book(headings, body=_STORY):
    """Build (source_text, outline) the way the importer emits them.

    ``headings`` is a list of ``(level, text)``; every heading gets ``body``
    under it, mirroring ``_flush_heading``'s ``\n\n{text}\n\n``.
    """
    parts, outline = [], []
    for level, text in headings:
        parts += [text, body]
        outline.append({"level": level, "text": text})
    return "\n\n".join(parts), outline


def _titles(n, prefix="Story"):
    return [(2, f"{prefix} Number {i}") for i in range(1, n + 1)]


class TestLocateHeadings:
    def test_locates_each_heading_as_a_standalone_line(self):
        text, outline = _outline_book(_titles(6))
        anchors, unlocated = locate_headings(text, outline)
        assert unlocated == []
        assert [a["text"] for a in anchors] == [h["text"] for h in outline]
        for a in anchors:
            assert text[a["start"]:a["end"]] == a["text"]

    def test_repeated_title_anchors_on_successive_occurrences(self):
        # A story title that is also an illustration caption appears twice. The
        # scan is sequential, so the second outline entry must not re-anchor on
        # the first occurrence.
        text, outline = _outline_book([
            (2, "A Ravens Funeral"), (2, "Something Else"), (2, "A Ravens Funeral"),
        ])
        anchors, unlocated = locate_headings(text, outline)
        assert unlocated == []
        starts = [a["start"] for a in anchors]
        assert starts == sorted(starts)
        assert len(set(starts)) == 3

    def test_heading_missing_from_source_is_reported_not_skipped_silently(self):
        text, outline = _outline_book(_titles(4))
        outline.insert(2, {"level": 2, "text": "A Heading Nobody Wrote"})
        anchors, unlocated = locate_headings(text, outline)
        assert unlocated == ["A Heading Nobody Wrote"]
        # The surrounding anchors keep their real positions.
        assert len(anchors) == 4
        for a in anchors:
            assert text[a["start"]:a["end"]] == a["text"]

    def test_matches_despite_whitespace_differences(self):
        # The sidecar collapses whitespace; a hand-edited source may not have.
        text, outline = _outline_book([(2, "A Title")] + _titles(4))
        text = text.replace("A Title", "A    Title")
        anchors, unlocated = locate_headings(text, outline)
        assert unlocated == []
        assert anchors[0]["text"] == "A Title"


class TestSelectHeadingLevel:
    def test_picks_the_level_that_partitions_the_text(self):
        text, outline = _outline_book(
            [(1, "The Book")] + _titles(8) + [(4, "Publisher Note")])
        anchors, _ = locate_headings(text, outline)
        report = select_heading_level(anchors, text)
        assert report["selected"] == "h2"
        assert report["levels"]["h2"]["n"] == 8

    def test_bails_when_too_few_headings(self):
        # home-geography: 46 chapters, a handful of headings in the whole file.
        text, outline = _outline_book([(1, "A"), (1, "B"), (1, "C")])
        anchors, _ = locate_headings(text, outline)
        assert select_heading_level(anchors, text)["selected"] is None

    def test_bails_when_sections_are_title_fragments(self):
        # Every "section" is a few chars: this level is typesetting, not chapters.
        text, outline = _outline_book(_titles(8), body="hi")
        anchors, _ = locate_headings(text, outline)
        assert select_heading_level(anchors, text)["selected"] is None

    def test_skew_is_reported_not_vetoed(self):
        # An anthology's stories vary wildly in length; that must not disqualify
        # the level, only earn an advisory.
        parts, outline = [], []
        for i, (level, t) in enumerate(_titles(6)):
            parts += [t, _STORY * (12 if i == 0 else 1)]
            outline.append({"level": level, "text": t})
        text = "\n\n".join(parts)
        anchors, _ = locate_headings(text, outline)
        report = select_heading_level(anchors, text)
        assert report["selected"] == "h2"
        assert report["levels"]["h2"]["skew"] > 4
        warns = split_sanity_warnings(
            split_book_into_chapters(text, pattern_type="headings",
                                     heading_outline=outline),
            text, pattern_used="headings", outline_report=report)
        assert any("median" in w for w in warns)


class TestHeadingOutlineSplit:
    def test_mixed_case_titles_all_become_chapters(self):
        # allcaps_heading silently merges the Title-Case ones into whatever
        # precedes them; the outline knows they are headings.
        headings = [
            (2, "The Lazy Snail"),
            (2, "THE ROBINS BUILD A NEST."),
            (2, "The Cricket School"),
            (2, "The GRASSHOPPER and the MEASURING WORM RUN a RACE"),
            (2, "Mr GREEN FROG AND HIS VISITORS"),
            (2, "The Earthworm Half-Brothers"),
        ]
        text, outline = _outline_book(headings)
        sections = split_book_into_chapters(
            text, pattern_type="auto", heading_outline=outline)
        assert [s.chapter_title for s in sections] == [h[1] for h in headings]
        assert all(s.kind == "chapter" for s in sections)

        regex_sections = split_book_into_chapters(text, pattern_type="allcaps_heading")
        assert len(regex_sections) < len(sections)

    def test_auto_prefers_the_outline_over_a_regex_pattern(self):
        text, outline = _outline_book(_titles(6))
        report: dict = {}
        split_book_into_chapters(text, pattern_type="auto", heading_outline=outline,
                                 collect_outline_report=report)
        assert resolve_pattern_type("auto", text, outline_report=report) == "headings"

    def test_auto_without_an_outline_is_unchanged(self):
        text = _two_sections("CHAPTER I. A", "CHAPTER II. B")
        assert resolve_pattern_type("auto", text) == detect_pattern_from_text(text)

    def test_auto_falls_back_when_the_outline_is_unconvincing(self):
        text = _two_sections("CHAPTER I. A", "CHAPTER II. B")
        outline = [{"level": 1, "text": "CHAPTER I. A"}]
        report: dict = {}
        sections = split_book_into_chapters(
            text, pattern_type="auto", heading_outline=outline,
            collect_outline_report=report)
        assert report["selected"] is None
        assert [s.number for s in sections if s.kind == "chapter"] == [1, 2]

    def test_matter_keywords_are_tagged_in_place_not_by_position(self):
        # A half-title between the dedication and the prologue defeats a
        # positional front-matter scan: it stops there and the prologue is
        # swallowed into its body. Tagging by heading keeps both.
        text, outline = _outline_book(
            [(2, "Dedication"), (2, "Half Title"), (2, "Prologue")]
            + _titles(5) + [(2, "Epilogue")])
        sections = split_book_into_chapters(
            text, pattern_type="headings", heading_outline=outline)
        by_title = {s.chapter_title: s.kind for s in sections}
        assert by_title["Dedication"] == "front_matter"
        assert by_title["Prologue"] == "front_matter"
        assert by_title["Epilogue"] == "back_matter"
        assert by_title["Half Title"] == "chapter"
        # Chapter numbering skips the matter sections.
        assert [s.number for s in sections if s.kind == "chapter"] == [1, 2, 3, 4, 5, 6]

    def test_user_declared_matter_title_is_tagged_in_place(self):
        text, outline = _outline_book([(2, "To The Children")] + _titles(6))
        sections = split_book_into_chapters(
            text, pattern_type="headings", heading_outline=outline,
            front_matter_titles=["To the Children"])
        assert sections[0].kind == "front_matter"
        assert sections[0].label == "To the Children"

    def test_boilerplate_heading_does_not_swallow_following_matter(self):
        text, outline = _outline_book([(2, "CONTENTS"), (2, "PREFACE")] + _titles(5))
        dropped: list = []
        sections = split_book_into_chapters(
            text, pattern_type="headings", heading_outline=outline,
            collect_dropped=dropped)
        assert [d["label"] for d in dropped] == ["Contents"]
        assert sections[0].chapter_title == "PREFACE"
        assert sections[0].kind == "front_matter"

    def test_bare_numeral_heading_merges_into_the_title_that_follows(self):
        # 'Chapter I.' and 'THE HORSE AND HIS RIDER.' as sibling headings.
        parts, outline = [], []
        for i in range(1, 7):
            parts += [f"Chapter {i}.", f"A TITLE FOR {i}", _STORY]
            outline += [{"level": 2, "text": f"Chapter {i}."},
                        {"level": 2, "text": f"A TITLE FOR {i}"}]
        text = "\n\n".join(parts)
        sections = split_book_into_chapters(
            text, pattern_type="headings", heading_outline=outline)
        assert len(sections) == 6
        assert sections[0].chapter_title == "Chapter 1.\nA TITLE FOR 1"
        assert "lorem ipsum" in sections[0].content
        assert "A TITLE FOR 1" not in sections[0].content

    def test_prose_between_merged_headings_opens_the_chapter_body(self):
        """An epigraph under ``Chapter I.`` used to be written nowhere.

        The merge gap is measured start-to-start, so ~190 chars of prose can sit
        inside a group that still merges. The body begins after the *last*
        heading, so that prose was in neither the title nor the body, and never
        reached ``dropped`` either -- the split reported a clean ledger while
        silently discarding it.
        """
        epigraph = "He rode out at dawn, and the dust rose behind him. " * 2
        parts, outline = [], []
        for i in range(1, 7):
            parts += [f"Chapter {i}.", epigraph, f"A TITLE FOR {i}", _STORY]
            outline += [{"level": 2, "text": f"Chapter {i}."},
                        {"level": 2, "text": f"A TITLE FOR {i}"}]
        text = "\n\n".join(parts)
        dropped: list = []
        sections = split_book_into_chapters(
            text, pattern_type="auto", heading_outline=outline,
            collect_dropped=dropped)

        # The merge itself is unchanged: still one section per chapter.
        assert len(sections) == 6
        assert sections[0].chapter_title == "Chapter 1.\nA TITLE FOR 1"
        # The epigraph opens the body, where it appears in the book.
        assert sections[0].content.startswith("He rode out at dawn")
        assert "lorem ipsum" in sections[0].content
        assert "A TITLE FOR 1" not in sections[0].content  # title, not body
        assert all("He rode out at dawn" in s.content for s in sections)
        assert dropped == []

    def test_chained_numeral_merge_keeps_every_body_but_no_heading_lines(self):
        # Consecutive short bare numerals chain into one group. The text between
        # them must survive; the heading lines must not be folded in as prose.
        verse = "A short verse of a handful of lines. "
        parts, outline = [], []
        for n in ("I", "II", "III", "IV", "V", "VI"):
            parts += [f"{n}.", verse]
            outline.append({"level": 2, "text": f"{n}."})
        text = "\n\n".join(parts)
        sections = split_book_into_chapters(
            text, pattern_type="headings", heading_outline=outline,
            heading_level="h2", min_chapter_size=50)

        assert sections[0].content.count("A short verse") == 6
        for n in ("II.", "III.", "IV."):
            assert n not in sections[0].content

    def test_unmerged_headings_get_no_lead_text(self):
        # The regression guard: a book whose headings never merge must have
        # byte-identical bodies to before the lead-text plumbing existed.
        text, outline = _outline_book(_titles(6))
        sections = split_book_into_chapters(
            text, pattern_type="headings", heading_outline=outline)
        for s in sections:
            assert s.content.startswith("lorem ipsum")

    def test_short_standalone_sections_are_not_merged(self):
        # A book of one-page poems: every section is short, but none is a bare
        # numeral, so nothing may collapse into its neighbour.
        text, outline = _outline_book(_titles(8, prefix="Poem"),
                                      body="a short poem. " * 20)
        sections = split_book_into_chapters(
            text, pattern_type="headings", heading_outline=outline,
            heading_level="h2")
        assert len(sections) == 8

    def test_headings_without_an_outline_raises_a_useful_error(self):
        text, _ = _outline_book(_titles(6))
        with pytest.raises(ValueError, match="headings.json"):
            split_book_into_chapters(text, pattern_type="headings")

    def test_headings_with_unlocated_outline_names_the_desync(self):
        # Sidecar present, source.txt rewritten: every title misses. That is
        # not "headings.json was not found" — telling the user to re-ingest
        # for a missing file sends them at the wrong thing.
        text, _ = _outline_book(_titles(6))
        outline = [{"level": 2, "text": f"A Title Nobody Wrote {i}"}
                   for i in range(1, 7)]
        with pytest.raises(ValueError, match="Restore source.txt"):
            split_book_into_chapters(text, pattern_type="headings",
                                     heading_outline=outline)

    def test_explicit_heading_level_overrides_the_selection(self):
        # h2 is denser and wins on its own; h3 is what the caller wants. This is
        # the one-flag fix for an ambiguous book (gaudenzia, stormy-misty).
        parts, outline = [], []
        for i in range(1, 9):
            parts += [f"Scene {i}", _STORY]
            outline.append({"level": 2, "text": f"Scene {i}"})
            if i % 2:
                parts += [f"Chapter {i}", _STORY]
                outline.append({"level": 3, "text": f"Chapter {i}"})
        text = "\n\n".join(parts)

        auto = split_book_into_chapters(text, pattern_type="headings",
                                        heading_outline=outline)
        assert [s.chapter_title for s in auto] == [f"Scene {i}" for i in range(1, 9)]

        forced = split_book_into_chapters(text, pattern_type="headings",
                                          heading_outline=outline, heading_level="h3")
        assert [s.chapter_title for s in forced] == [
            f"Chapter {i}" for i in (1, 3, 5, 7)]

    def test_deeper_level_wins_a_tie(self):
        # A book that numbers at h2 and titles at h3 should split on the more
        # specific level rather than on whichever sorts first.
        parts, outline = [], []
        for i in range(1, 7):
            parts += [f"Volume {i}", f"A Title For {i}", _STORY]
            outline += [{"level": 2, "text": f"Volume {i}"},
                        {"level": 3, "text": f"A Title For {i}"}]
        text = "\n\n".join(parts)
        anchors, _ = locate_headings(text, outline)
        assert select_heading_level(anchors, text)["selected"] == "h3"

    def test_unknown_heading_level_reports_what_is_available(self):
        text, outline = _outline_book(_titles(6))
        with pytest.raises(ValueError, match="h2"):
            split_book_into_chapters(text, pattern_type="headings",
                                     heading_outline=outline, heading_level="h5")

    @pytest.mark.parametrize("junk", ["foo", "h2 extra"])
    def test_garbage_heading_level_is_rejected_not_treated_as_omitted(self, junk):
        # Non-numeric junk used to normalize to None and run the auto-picked
        # level with no error. Out-of-range ints already fail later; this is
        # the case that didn't.
        text, outline = _outline_book(_titles(6))
        with pytest.raises(ValueError, match="invalid heading level"):
            split_book_into_chapters(text, pattern_type="headings",
                                     heading_outline=outline, heading_level=junk)

    def test_unlocated_headings_earn_a_warning(self):
        text, outline = _outline_book(_titles(6))
        outline.append({"level": 2, "text": "A Heading Nobody Wrote"})
        report: dict = {}
        sections = split_book_into_chapters(
            text, pattern_type="headings", heading_outline=outline,
            collect_outline_report=report)
        warns = split_sanity_warnings(sections, text, pattern_used="headings",
                                      outline_report=report)
        assert any("not found in source.txt" in w for w in warns)


class TestLoadHeadingOutline:
    def test_absent_sidecar_returns_none(self, tmp_path):
        assert load_heading_outline(tmp_path) is None

    def test_malformed_sidecar_returns_none(self, tmp_path):
        (tmp_path / "headings.json").write_text("{not json", encoding="utf-8")
        assert load_heading_outline(tmp_path) is None

    def test_round_trips_what_ingest_writes(self, tmp_path):
        (tmp_path / "headings.json").write_text(json.dumps({
            "version": 1,
            "headings": [{"level": 2, "text": "A"}, {"level": 3, "text": " B "},
                         {"level": 2, "text": ""}],
        }), encoding="utf-8")
        assert load_heading_outline(tmp_path) == [
            {"level": 2, "text": "A"}, {"level": 3, "text": "B"}]

    def test_absent_sidecar_reports_no_error(self, tmp_path):
        # Silence is correct here: a project ingested before the sidecar existed
        # is not broken, and must not nag.
        errors: list = []
        assert load_heading_outline(tmp_path, collect_error=errors) is None
        assert errors == []

    @pytest.mark.parametrize("payload,expected", [
        ('{"version": 1, "headings": [{"level": 2, "te', "invalid JSON"),
        ('[{"level": 2, "text": "A"}]', "no 'headings' list"),
        ('{"version": 1, "headings": {"a": 1}}', "no 'headings' list"),
        ('{"version": 1, "headings": [{"level": 2, "text": "  "}]}',
         "none with usable text"),
    ])
    def test_broken_sidecar_is_distinguishable_from_absent(
            self, tmp_path, payload, expected):
        # Returning None for both "absent" and "broken" made a truncated write
        # look like a pre-sidecar project: no warning, and heading_outline null.
        (tmp_path / "headings.json").write_text(payload, encoding="utf-8")
        errors: list = []
        assert load_heading_outline(tmp_path, collect_error=errors) is None
        assert len(errors) == 1
        assert expected in errors[0]
        assert "headings.json" in errors[0]

    def test_broken_sidecar_is_named_when_the_regex_fallback_finds_nothing(self):
        # Otherwise the run dies on "no chapters with pattern 'roman'" and the
        # warning naming the real cause is never reached.
        text, _ = _outline_book([(2, "The Wolf at the Door"),
                                 (2, "The Long Winter")])
        with pytest.raises(ValueError, match="headings.json exists but could not"):
            split_book_into_chapters(
                text, pattern_type="auto",
                outline_error="headings.json exists but could not be used — "
                              "invalid JSON: x")


class TestExplicitHeadingLevelOnAuto:
    """``--heading-level`` must reach the outline path from ``auto``.

    The selector deliberately returns ``selected: None`` for books it isn't
    confident about, and that is exactly when a caller reads the ``levels``
    table and names a level by hand. Resolving ``auto`` without consulting
    ``heading_level`` discarded the flag and ran a regex instead.
    """

    # 4 h2 chapters: under _HEADING_MIN_SECTIONS, so the selector declines. The
    # titles are Title Case, so no regex pattern can find them either.
    def _book(self):
        return _outline_book([(2, "The Wolf at the Door"), (2, "The Long Winter"),
                              (2, "A Letter Home"), (2, "The Bridge at Evening")])

    def test_selector_declines_but_reports_the_level(self):
        text, outline = self._book()
        anchors, _ = locate_headings(text, outline)
        report = select_heading_level(anchors, text)
        assert report["selected"] is None
        assert report["levels"]["h2"]["n"] == 4

    def test_auto_plus_heading_level_uses_the_outline(self):
        text, outline = self._book()
        got: dict = {}
        sections = split_book_into_chapters(
            text, pattern_type="auto", heading_outline=outline,
            heading_level="h2", collect_outline_report=got)
        assert [s.chapter_title for s in sections] == [
            "The Wolf at the Door", "The Long Winter",
            "A Letter Home", "The Bridge at Evening"]
        # The report must name the level actually used, not the declined one.
        assert got["selected"] == "h2"

    def test_auto_without_the_flag_still_falls_back(self):
        text, outline = self._book()
        with pytest.raises(ValueError, match="No chapters detected"):
            split_book_into_chapters(
                text, pattern_type="auto", heading_outline=outline)

    def test_resolve_pattern_type_agrees_with_the_split(self):
        # pattern_used is computed by a second, independent call; if it ignores
        # heading_level it reports a regex pattern for an outline split.
        text, outline = self._book()
        got: dict = {}
        split_book_into_chapters(
            text, pattern_type="auto", heading_outline=outline,
            heading_level="h2", collect_outline_report=got)
        assert resolve_pattern_type(
            "auto", text, outline_report=got, heading_level="h2") == "headings"

    def test_flag_needs_an_outline_to_act_on(self):
        # No sidecar: the flag has nothing to bite on, so the regex patterns
        # still run rather than erroring out on a missing headings.json.
        text, _ = _outline_book(_titles(6))
        assert resolve_pattern_type(
            "auto", text, outline_report=None, heading_level="h2") != "headings"

    def test_ignored_flag_is_warned_about(self):
        # allcaps_heading's class has no digits, so spell the numbers out.
        text, _ = _outline_book([
            (2, f"STORY NUMBER {w}") for w in
            ("ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX")])
        sections = split_book_into_chapters(text, pattern_type="allcaps_heading")
        warnings = split_sanity_warnings(
            sections, text, pattern_used="allcaps_heading",
            outline_report=None, heading_level="h2")
        assert any("--heading-level h2 had no effect" in w for w in warnings)

    def test_no_warning_when_the_flag_was_honored(self):
        text, outline = self._book()
        got: dict = {}
        sections = split_book_into_chapters(
            text, pattern_type="auto", heading_outline=outline,
            heading_level="h2", collect_outline_report=got)
        warnings = split_sanity_warnings(
            sections, text, pattern_used="headings",
            outline_report=got, heading_level="h2")
        assert not any("had no effect" in w for w in warnings)


class TestDroppedSectionsAreReported:
    """Sections used to vanish between detection and the written files."""

    def test_too_short_section_is_recorded_with_its_size(self):
        # A title-page fragment: a real heading with almost nothing under it.
        # It used to be filtered by min_chapter_size and never mentioned again.
        parts, outline = ["A Half Title", "tiny"], [{"level": 2, "text": "A Half Title"}]
        for level, t in _titles(6):
            parts += [t, _STORY]
            outline.append({"level": level, "text": t})
        text = "\n\n".join(parts)

        dropped: list = []
        sections = split_book_into_chapters(
            text, pattern_type="headings", heading_outline=outline,
            collect_dropped=dropped)
        assert len(sections) == 6
        assert [(d["label"], d["reason"]) for d in dropped] == [
            ("A Half Title", "too_short")]
        assert dropped[0]["chars"] == len("tiny")

    def test_section_kept_when_it_clears_min_chapter_size(self):
        text, outline = _outline_book(_titles(6))
        dropped: list = []
        sections = split_book_into_chapters(
            text, pattern_type="headings", heading_outline=outline,
            collect_dropped=dropped)
        assert len(sections) == 6
        assert dropped == []


class TestCustomRegexCaseSensitivity:
    _PROSE = "\n\nPoor Mr. Butterfly! He found his wings so wet and crinkled.\n\n"

    def test_ignorecase_is_the_default(self):
        pat = get_chapter_pattern("custom", r"[A-Z][A-Z ]{4,}")
        assert pat.search(self._PROSE)

    def test_case_sensitive_stops_matching_prose(self):
        pat = get_chapter_pattern("custom", r"[A-Z][A-Z ]{4,}", case_sensitive=True)
        assert not pat.search(self._PROSE)
        assert pat.search("\n\nTHE ROBINS BUILD A NEST\n\n")
