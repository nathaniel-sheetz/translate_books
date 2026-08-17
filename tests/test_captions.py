"""Tests for src/captions.py — the [CAPTION] backfill scan/apply.

The tier fixtures mirror shapes taken from the real corpus, including the
false-positive cases the classifier must NOT auto-accept:

  - among-the-farmyard-people : caption repeats the image's alt text     (A)
  - three-little-kittens      : caption is a fully italicized paragraph  (B)
  - kittens-and-cats          : ALL-CAPS short line                      (C)
  - fabre2                    : short noun phrase, no end punctuation    (D)
  - home-geography            : real body prose under an image           (E)
  - gaudenzia                 : chapter title under a header ornament    (excluded)
"""

import json

import pytest

from src.captions import (
    AUTO_TIERS,
    Candidate,
    apply_marks,
    classify,
    iter_blocks,
    scan_project,
    select,
    tier_counts,
)


# ---------------------------------------------------------------------------
# Block scanning
# ---------------------------------------------------------------------------

class TestIterBlocks:
    def test_offsets_point_at_first_character(self):
        text = "One.\n\nTwo.\n\nThree."
        blocks = list(iter_blocks(text))
        assert [b for _s, _e, b in blocks] == ["One.", "Two.", "Three."]
        for start, _end, block in blocks:
            assert text[start:start + len(block)] == block

    def test_blank_runs_and_indentation_are_tolerated(self):
        text = "\n\nOne.\n   \n\n  Two.\n\n"
        blocks = [b for _s, _e, b in iter_blocks(text)]
        assert blocks == ["One.", "Two."]

    def test_empty_text(self):
        assert list(iter_blocks("")) == []


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------

class TestClassify:
    def test_tier_a_exact_alt_match(self):
        assert classify("THE LAMB WITH THE LONGEST TAIL", "THE LAMB WITH THE LONGEST TAIL.") == "A"

    def test_tier_a_translated_alt_still_matches_modulo_punctuation(self):
        assert classify("Las golondrinas están llegando", "LAS GOLONDRINAS ESTÁN LLEGANDO.") == "A"

    def test_short_alt_does_not_swallow_a_paragraph(self):
        # "A COMPASS." must not claim a body paragraph that merely contains it.
        tier = classify("A COMPASS.", "¿Has visto alguna vez una compass? Es una caja pequeña.")
        assert tier != "A"

    def test_tier_b_fully_italicized(self):
        assert classify("", "_La gente gatuna siempre comía muy bien_") == "B"

    def test_partially_italicized_is_not_tier_b(self):
        assert classify("", "_La gente_ gatuna comía muy bien.") != "B"

    def test_tier_c_allcaps(self):
        assert classify("", "ES MI FIESTA") == "C"

    def test_long_allcaps_is_not_tier_c(self):
        long_caps = " ".join(["PALABRA"] * 15)
        assert classify("", long_caps) != "C"

    def test_tier_d_short_noun_phrase(self):
        assert classify("", "Hormiga blanca") == "D"
        assert classify("", "Roble blanco") == "D"

    def test_dialogue_is_not_tier_d(self):
        assert classify("", "—¿Qué haces?") == "E"

    def test_tier_e_body_prose(self):
        prose = (
            "Si salgo al aire libre, ¿cómo puedo encontrar el norte? "
            "¿Cómo puedo encontrarlo dentro del salón de clases?"
        )
        assert classify("", prose) == "E"

    def test_sentence_with_terminal_period_is_not_tier_d(self):
        assert classify("", "Las hojas caen.") == "E"


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def _cand(index, tier):
    return Candidate(
        index=index, chapter_id="chapter_01", image=f"images/i{index}.jpg",
        occurrence=1, alt="", tier=tier, text="x",
        chunk_id="chapter_01_chunk_000", chunk_offset=0,
    )


class TestSelect:
    def test_defaults_to_auto_tiers(self):
        cands = [_cand(1, "A"), _cand(2, "C"), _cand(3, "B"), _cand(4, "E")]
        assert [c.index for c in select(cands)] == [1, 3]
        assert AUTO_TIERS == {"A", "B"}

    def test_accept_tiers(self):
        cands = [_cand(1, "A"), _cand(2, "C"), _cand(3, "E")]
        assert [c.index for c in select(cands, accept_tiers={"A", "C"})] == [1, 2]

    def test_individual_accept_adds_on_top_of_tiers(self):
        cands = [_cand(1, "A"), _cand(2, "E")]
        assert [c.index for c in select(cands, accept={2})] == [1, 2]

    def test_reject_wins_over_tier(self):
        cands = [_cand(1, "A"), _cand(2, "B")]
        assert [c.index for c in select(cands, reject={1})] == [2]

    def test_reject_wins_over_explicit_accept(self):
        cands = [_cand(1, "E")]
        assert select(cands, accept={1}, reject={1}) == []


# ---------------------------------------------------------------------------
# Scanning a project on disk
# ---------------------------------------------------------------------------

def _make_project(tmp_path, *, source, chapters):
    """chapters: {chapter_id: [chunk_translated_text, ...]}"""
    (tmp_path / "chunks").mkdir(parents=True)
    (tmp_path / "source.txt").write_text(source, encoding="utf-8")
    for chapter_id, chunk_texts in chapters.items():
        for i, text in enumerate(chunk_texts):
            path = tmp_path / "chunks" / f"{chapter_id}_chunk_{i:03d}.json"
            path.write_text(
                json.dumps({"id": f"{chapter_id}_chunk_{i:03d}",
                            "chapter_id": chapter_id,
                            "translated_text": text}, ensure_ascii=False),
                encoding="utf-8",
            )
    return tmp_path


class TestScanProject:
    def test_finds_alt_duplicate_caption(self, tmp_path):
        proj = _make_project(
            tmp_path,
            source=(
                "Body text before the picture here.\n\n"
                "[IMAGE:images/img016.jpg:THE LAMB WITH THE LONGEST TAIL]\n\n"
                "THE LAMB WITH THE LONGEST TAIL.\n\n"
                "Body text after."
            ),
            chapters={"chapter_01": [
                "Texto anterior a la imagen aquí.\n\n"
                "[IMAGE:images/img016.jpg:EL CORDERO DE LA COLA MÁS LARGA]\n\n"
                "EL CORDERO DE LA COLA MÁS LARGA.\n\n"
                "Texto posterior."
            ]},
        )
        cands = scan_project(proj)
        assert len(cands) == 1
        assert cands[0].tier == "A"
        assert cands[0].text == "EL CORDERO DE LA COLA MÁS LARGA."
        assert cands[0].source_text == "THE LAMB WITH THE LONGEST TAIL."
        assert cands[0].source_offset is not None

    def test_body_prose_under_an_image_is_tier_e(self, tmp_path):
        # The home-geography shape: alt carries the caption, the paragraph below
        # is real text. Must not be auto-accepted.
        proj = _make_project(
            tmp_path,
            source="[IMAGE:images/006.jpg:A COMPASS.]\n\nHave you ever seen a compass? It is a box.",
            chapters={"chapter_01": [
                "[IMAGE:images/006.jpg:UNA BRÚJULA.]\n\n"
                "¿Has visto alguna vez una brújula? Es una caja en la que hay una aguja."
            ]},
        )
        cands = scan_project(proj)
        assert len(cands) == 1
        assert cands[0].tier == "E"
        assert select(cands) == []

    def test_chapter_title_under_header_ornament_is_excluded(self, tmp_path):
        # The gaudenzia shape: decorative image, then the chapter title.
        proj = _make_project(
            tmp_path,
            source="[IMAGE:images/illus7.jpg]\n\nThe First Sign\n\nBody of the chapter goes here.",
            chapters={"chapter_03": [
                "[IMAGE:images/illus7.jpg]\n\nEl primer indicio\n\nCuerpo del capítulo aquí."
            ]},
        )
        assert scan_project(proj) == []

    def test_chapter_title_excluded_when_heading_line_precedes_ornament(self, tmp_path):
        # The real gaudenzia shape: the chapter heading is its own block ABOVE
        # the ornament, so "the ornament leads the chapter" must tolerate it.
        proj = _make_project(
            tmp_path,
            source="Chapter I\n\n[IMAGE:images/illus7.jpg]\n\nThe First Sign\n\nBody here.",
            chapters={"chapter_03": [
                "Capítulo I\n\n[IMAGE:images/illus7.jpg]\n\nEl primer indicio\n\nCuerpo aquí."
            ]},
        )
        assert scan_project(proj) == []

    def test_midchapter_allcaps_caption_is_not_treated_as_a_title(self, tmp_path):
        # kittens-and-cats: same shape as a title, but deep in the chapter.
        body = "\n\n".join(f"Párrafo número {n} con texto de relleno." for n in range(6))
        proj = _make_project(
            tmp_path,
            source=f"Chapter I\n\n{body}\n\n[IMAGE:images/illus2.jpg]\n\nIT IS MY PARTY",
            chapters={"chapter_01": [
                f"Capítulo I\n\n{body}\n\n[IMAGE:images/illus2.jpg]\n\nES MI FIESTA"
            ]},
        )
        cands = scan_project(proj)
        assert len(cands) == 1
        assert cands[0].tier == "C"
        assert cands[0].text == "ES MI FIESTA"

    def test_image_and_caption_split_across_chunks(self, tmp_path):
        proj = _make_project(
            tmp_path,
            source="Before.\n\n[IMAGE:images/a.jpg]\n\n_A caption_\n\nAfter.",
            chapters={"chapter_01": [
                "Antes.\n\n[IMAGE:images/a.jpg]",
                "_Un pie de foto_\n\nDespués.",
            ]},
        )
        cands = scan_project(proj)
        assert len(cands) == 1
        # The marker must land in the chunk that actually holds the caption.
        assert cands[0].chunk_id == "chapter_01_chunk_001"
        assert cands[0].tier == "B"

    def test_already_marked_caption_is_not_offered_again(self, tmp_path):
        proj = _make_project(
            tmp_path,
            source="[IMAGE:images/a.jpg]\n\n[CAPTION] A caption",
            chapters={"chapter_01": ["[IMAGE:images/a.jpg]\n\n[CAPTION] Un pie de foto"]},
        )
        assert scan_project(proj) == []

    def test_untranslated_chapter_yields_source_only_candidate(self, tmp_path):
        proj = _make_project(
            tmp_path,
            source="[IMAGE:images/p086.jpg:SHE AND PING]\n\nSHE AND PING.",
            chapters={"chapter_01": [""]},
        )
        cands = scan_project(proj)
        assert len(cands) == 1
        assert cands[0].source_only is True
        assert cands[0].chunk_offset is None
        assert cands[0].tier == "A"

    def test_long_paragraph_is_never_a_candidate(self, tmp_path):
        long_para = " ".join(["palabra"] * 60)
        proj = _make_project(
            tmp_path,
            source="[IMAGE:images/a.jpg]\n\n" + " ".join(["word"] * 60),
            chapters={"chapter_01": ["[IMAGE:images/a.jpg]\n\n" + long_para]},
        )
        assert scan_project(proj) == []

    def test_horizontal_rule_after_image_is_not_a_candidate(self, tmp_path):
        proj = _make_project(
            tmp_path,
            source="[IMAGE:images/a.jpg]\n\n---\n\nBody.",
            chapters={"chapter_01": ["[IMAGE:images/a.jpg]\n\n---\n\nCuerpo."]},
        )
        assert scan_project(proj) == []


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

class TestApplyMarks:
    @pytest.fixture
    def project(self, tmp_path):
        return _make_project(
            tmp_path,
            source=(
                "Before.\n\n[IMAGE:images/a.jpg:A LAMB]\n\nA LAMB.\n\nAfter."
            ),
            chapters={"chapter_01": [
                "Antes.\n\n[IMAGE:images/a.jpg:UN CORDERO]\n\nUN CORDERO.\n\nDespués."
            ]},
        )

    def test_marks_both_chunk_and_source(self, project):
        cands = scan_project(project)
        report = apply_marks(project, select(cands))

        assert report["chunk_marks"] == 1
        assert report["source_marks"] == 1
        assert report["source_written"] is True

        chunk = json.loads(
            (project / "chunks" / "chapter_01_chunk_000.json").read_text(encoding="utf-8")
        )
        assert "[CAPTION] UN CORDERO." in chunk["translated_text"]
        assert "[CAPTION] Antes." not in chunk["translated_text"]

        source = (project / "source.txt").read_text(encoding="utf-8")
        assert "[CAPTION] A LAMB." in source
        assert "[CAPTION] Before." not in source

    def test_chapters_dir_is_never_written(self, project):
        # chapters/*.txt is regenerated from the chunks; writing there would be
        # silently erased by the next combine.
        (project / "chapters").mkdir()
        chapter_file = project / "chapters" / "chapter_01.txt"
        chapter_file.write_text("untouched", encoding="utf-8")

        apply_marks(project, select(scan_project(project)))
        assert chapter_file.read_text(encoding="utf-8") == "untouched"

    def test_is_idempotent(self, project):
        first = apply_marks(project, select(scan_project(project)))
        assert first["chunk_marks"] == 1

        text_after_first = (project / "source.txt").read_text(encoding="utf-8")
        # Re-scanning finds nothing left to do, and re-applying the stale
        # selection changes nothing.
        assert scan_project(project) == []
        second = apply_marks(project, select(scan_project(project)))
        assert second["chunk_marks"] == 0
        assert (project / "source.txt").read_text(encoding="utf-8") == text_after_first

    def test_empty_selection_writes_nothing(self, project):
        before = (project / "source.txt").read_text(encoding="utf-8")
        report = apply_marks(project, [])
        assert report["chunk_marks"] == 0
        assert (project / "source.txt").read_text(encoding="utf-8") == before

    def test_surrounding_text_is_preserved_byte_for_byte(self, project):
        original = (project / "source.txt").read_text(encoding="utf-8")
        apply_marks(project, select(scan_project(project)))
        updated = (project / "source.txt").read_text(encoding="utf-8")
        assert updated.replace("[CAPTION] ", "") == original

    def test_multiple_marks_in_one_chunk(self, tmp_path):
        proj = _make_project(
            tmp_path,
            source="[IMAGE:images/a.jpg]\n\n_One_\n\n[IMAGE:images/b.jpg]\n\n_Two_",
            chapters={"chapter_01": [
                "[IMAGE:images/a.jpg]\n\n_Uno_\n\n[IMAGE:images/b.jpg]\n\n_Dos_"
            ]},
        )
        report = apply_marks(proj, select(scan_project(proj)))
        assert report["chunk_marks"] == 2
        chunk = json.loads(
            (proj / "chunks" / "chapter_01_chunk_000.json").read_text(encoding="utf-8")
        )
        assert "[CAPTION] _Uno_" in chunk["translated_text"]
        assert "[CAPTION] _Dos_" in chunk["translated_text"]


class TestTierCounts:
    def test_reports_every_tier(self):
        counts = tier_counts([_cand(1, "A"), _cand(2, "A"), _cand(3, "E")])
        assert counts == {"A": 2, "B": 0, "C": 0, "D": 0, "E": 1}
