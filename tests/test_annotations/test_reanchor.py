"""Re-anchoring reader annotations after a realign (src/annotations/reanchor.py).

The notes are matched on their own ``es_text`` snapshot, not on a diff of the
alignment before and after, so a note stranded by an earlier realign is put back
by a later one.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from src.annotations import reanchor, store

from .conftest import write_alignment, write_annotations

REPO_ROOT = Path(__file__).resolve().parents[2]
CH = "chapter_01"


def _note(es_idx, es_text=None, *, sub_id="u1", content="nota", **extra):
    record = {
        "project_id": "testbook",
        "chapter_id": CH,
        "es_idx": es_idx,
        "type": "word_choice",
        "content": content,
        "timestamp": "2026-09-12T15:00:00",
    }
    if sub_id is not None:
        record["sub_id"] = sub_id
    if es_text is not None:
        record["es_text"] = es_text
    record.update(extra)
    return record


def _align(project, sentences):
    """Write an alignment of single-sentence rows numbered from 0."""
    write_alignment(project, CH, [(i, es, "en") for i, es in enumerate(sentences)])


def _write_rows(project, rows):
    path = project / "alignments" / f"{CH}.json"
    path.write_text(
        json.dumps({"chapter_id": CH, "alignments": rows}, ensure_ascii=False),
        encoding="utf-8",
    )


def _live(project):
    return {(r["es_idx"], r.get("sub_id")): r for r in store.load_active(project, chapter_id=CH)}


def _raw_rows(project):
    return list(store.iter_records(project))


class TestStaleNumbers:
    """The chapter-6 case: an earlier realign moved the sentences and no one
    moved the notes, so the 'old' alignment this pass is handed is already wrong."""

    SENTENCES = [
        "Primera oración nueva.",
        "Segunda oración nueva.",
        "—Ven conmigo —le susurró la abuela.",
        "—Ah, tengo que ir a casa de la abuela —dijo Joel.",
    ]

    def test_note_on_a_stale_number_that_still_has_a_row_is_moved(self, project):
        _align(project, self.SENTENCES)
        write_annotations(project, [
            _note(2, "—Ah, tengo que ir a casa de la abuela —dijo Joel."),
        ])
        # The old map is the shifted alignment: number 2 reads the same before
        # and after, which is what used to pass for "nothing moved".
        old_es_map = dict(enumerate(self.SENTENCES))

        result = reanchor.reanchor_chapter(project, CH, old_es_map)

        assert [(m.old_idx, m.new_idx, m.tier) for m in result.moved] == [(2, 3, "exact")]
        assert result.orphaned == []
        assert list(_live(project)) == [(3, "u1")]

    def test_note_on_a_stale_number_with_no_row_is_moved_not_orphaned(self, project):
        # Number 3 is now a later member of row 2 (a speech tag glued on), so
        # neither alignment has a row 3. The sentence itself is at row 5.
        _write_rows(project, [
            {"es_idx": 0, "es": "Uno."},
            {"es_idx": 2, "es_indices": [2, 3], "es": "Dos. —dijo."},
            {"es_idx": 5, "es": "—Bueno, pruébala —dijo la abuela—; anda, corre ya."},
        ])
        write_annotations(project, [
            _note(3, "—Bueno, pruébala —dijo la abuela—; anda, corre ya."),
        ])
        old_es_map = {0: "Uno.", 2: "Dos. —dijo.", 5: "—Bueno, pruébala —dijo la abuela—; anda, corre ya."}

        result = reanchor.reanchor_chapter(project, CH, old_es_map)

        assert result.orphaned == []
        assert list(_live(project)) == [(5, "u1")]

    def test_works_with_no_prior_alignment_at_all(self, project):
        _align(project, self.SENTENCES)
        write_annotations(project, [
            _note(0, "—Ah, tengo que ir a casa de la abuela —dijo Joel."),
        ])
        reanchor.reanchor_chapter(project, CH)
        assert list(_live(project)) == [(3, "u1")]


class TestInPlace:
    def test_a_note_already_on_its_sentence_writes_nothing(self, project):
        _align(project, ["El gato.", "El perro."])
        write_annotations(project, [_note(1, "El perro.")])

        result = reanchor.reanchor_chapter(project, CH, {0: "El gato.", 1: "El perro."})

        assert result.kept == 1 and not result.moved
        assert len(_raw_rows(project)) == 1

    def test_no_annotations_creates_no_file(self, project):
        _align(project, ["El gato."])
        reanchor.reanchor_chapter(project, CH, {0: "El gato."})
        assert not (project / "annotations.jsonl").exists()

    def test_no_alignment_leaves_notes_alone(self, project):
        (project / "alignments" / f"{CH}.json").unlink()
        write_annotations(project, [_note(1, "El perro.")])
        result = reanchor.reanchor_chapter(project, CH)
        assert not result.moved and not result.orphaned
        assert len(_raw_rows(project)) == 1

    def test_a_sentence_edited_in_place_refreshes_the_snapshot(self, project):
        old = "El perro ladró fuerte en el jardín trasero por la mañana."
        new = "El perro ladró fuerte en el jardín delantero a las cinco."
        _align(project, ["El gato.", new])
        write_annotations(project, [_note(1, old)])

        result = reanchor.reanchor_chapter(project, CH)

        assert not result.moved and len(result.refreshed) == 1
        note = _live(project)[(1, "u1")]
        assert note["es_text"] == new
        # Same note, same date: only the snapshot changed.
        assert note["timestamp"] == "2026-09-12T15:00:00"


class TestMatchingTiers:
    def test_note_follows_its_sentence_into_a_merged_row(self, project):
        _write_rows(project, [
            {"es_idx": 0, "es_indices": [0, 1], "es": "—Ven —dijo la abuela. Y se fue."},
        ])
        write_annotations(project, [_note(1, "Y se fue.")])

        result = reanchor.reanchor_chapter(project, CH)

        assert [(m.new_idx, m.tier) for m in result.moved] == [(0, "joined_row")]
        # The snapshot stays the note's own sentence, not the wider row, so the
        # note can still be told apart if the row is cut in two again.
        assert _live(project)[(0, "u1")]["es_text"] == "Y se fue."

    def test_a_repeated_line_goes_to_the_copy_that_moved_with_its_neighbours(self, project):
        sentences = ["—Sí."] + [f"Relleno número {i}." for i in range(9)]
        sentences += ["Una oración única antes.", "—Sí."]
        _align(project, sentences)
        write_annotations(project, [
            # Both notes were written 6 rows earlier than where they are now.
            _note(4, "Una oración única antes.", sub_id="u1"),
            _note(5, "—Sí.", sub_id="u2"),
        ])

        reanchor.reanchor_chapter(project, CH)

        # Nearest to number 5 would be row 0 or row 11 by distance alone (5 vs 6);
        # the neighbour's shift of +6 says row 11.
        assert sorted(_live(project)) == [(10, "u1"), (11, "u2")]

    def test_a_split_sentence_keeps_the_note_on_its_opening_half(self, project):
        _align(project, ["Otra cosa.", "—Ay, caray —gimió su madre.", "Son los ojos."])
        write_annotations(project, [_note(0, "—Ay, caray —gimió su madre. Son los ojos.")])

        result = reanchor.reanchor_chapter(project, CH)

        assert [(m.new_idx, m.tier) for m in result.moved] == [(1, "split")]

    def test_a_reworded_sentence_is_found_by_similarity(self, project):
        old = "Pero, de algún modo, buen número de cosas se coló del viejo calesín a la cocina."
        new = "Y, de algún modo, buen número de cosas se coló de la vieja calesa a la cocina."
        _align(project, ["Otra oración que no se parece en nada a la buscada.", new])
        write_annotations(project, [_note(0, old)])

        result = reanchor.reanchor_chapter(project, CH)

        assert [(m.new_idx, m.tier) for m in result.moved] == [(1, "fuzzy")]
        assert _live(project)[(1, "u1")]["es_text"] == new

    def test_a_short_line_is_never_matched_by_similarity(self, project):
        # "—No —dijo Joel." is 87% similar to "—Sí —dijo Joel." and is not it.
        _align(project, ["Algo.", "—No —dijo Joel."])
        write_annotations(project, [_note(5, "—Sí —dijo Joel.")])

        result = reanchor.reanchor_chapter(project, CH)

        assert not result.moved
        assert [r["sub_id"] for r in result.orphaned] == ["u1"]
        assert len(_raw_rows(project)) == 1

    def test_a_note_is_not_moved_onto_an_image_row(self, project):
        _align(project, ["Algo.", "[IMAGE:images/i001.jpg]"])
        write_annotations(project, [_note(0, "[IMAGE:images/i001.jpg]")])
        result = reanchor.reanchor_chapter(project, CH)
        assert not result.moved and len(result.orphaned) == 1


class TestNotesWithoutASnapshot:
    def test_old_alignment_supplies_the_sentence_and_the_note_gains_a_snapshot(self, project):
        _align(project, ["Nueva.", "El gato.", "El perro."])
        write_annotations(project, [_note(1)])

        result = reanchor.reanchor_chapter(project, CH, {0: "El gato.", 1: "El perro."})

        assert [(m.old_idx, m.new_idx) for m in result.moved] == [(1, 2)]
        assert _live(project)[(2, "u1")]["es_text"] == "El perro."

    def test_confirmed_in_place_is_given_a_snapshot_once(self, project):
        _align(project, ["El gato.", "El perro."])
        write_annotations(project, [_note(1)])
        old_es_map = {0: "El gato.", 1: "El perro."}

        first = reanchor.reanchor_chapter(project, CH, old_es_map)
        second = reanchor.reanchor_chapter(project, CH, old_es_map)

        assert len(first.backfilled) == 1 and not second.backfilled
        assert len(_raw_rows(project)) == 2
        assert _live(project)[(1, "u1")]["es_text"] == "El perro."

    def test_a_note_whose_bracketed_word_is_not_in_its_row_gets_no_snapshot(self, project):
        _align(project, ["El gato.", "El perro."])
        write_annotations(project, [_note(1, content="[calesín] ¿calesa?")])

        result = reanchor.reanchor_chapter(project, CH, {0: "El gato.", 1: "El perro."})

        assert [r["sub_id"] for r in result.suspect] == ["u1"]
        assert not result.backfilled
        assert len(_raw_rows(project)) == 1

    def test_the_bracket_check_ignores_case(self, project):
        _align(project, ["Tenemos una gata con ese nombre."])
        write_annotations(project, [_note(0, content="[Nombre] ¿cuál?")])
        result = reanchor.reanchor_chapter(project, CH, {0: "Tenemos una gata con ese nombre."})
        assert not result.suspect and len(result.backfilled) == 1

    def test_no_snapshot_and_no_old_alignment_is_left_alone(self, project):
        _align(project, ["El gato.", "El perro."])
        write_annotations(project, [_note(1), _note(40, sub_id="u2")])

        result = reanchor.reanchor_chapter(project, CH)

        # Number 1 still resolves, so nothing can be disproved; number 40 does not.
        assert [r["sub_id"] for r in result.unverified] == ["u1"]
        assert [r["sub_id"] for r in result.orphaned] == ["u2"]
        assert len(_raw_rows(project)) == 2


class TestWhatIsWritten:
    def test_the_whole_record_is_carried_including_the_review_sidecar(self, project):
        _align(project, ["Nueva.", "El perro."])
        sidecar = {"run_id": "r1", "written_content": "nota\n— IA: bien."}
        write_annotations(project, [
            _note(0, "El perro.", content="nota\n— IA: bien.", origin="gutenberg",
                  fn_number=3, verified_by="native", ai_review=sidecar),
        ])

        reanchor.reanchor_chapter(project, CH)

        note = _live(project)[(1, "u1")]
        assert note["ai_review"] == sidecar
        assert (note["origin"], note["fn_number"], note["verified_by"]) == ("gutenberg", 3, "native")
        # The annotation-review gate must still see it as reviewed.
        from src.annotations.targets import already_reviewed
        assert already_reviewed(note)

    def test_notes_that_trade_rows_all_survive(self, project):
        # Legacy notes share one sub_id slot per row. 0 → 2 while 2 → 4: a
        # tombstone for row 2 written after the first note arrived would delete it.
        _align(project, ["x.", "y.", "Primera.", "z.", "Segunda."])
        write_annotations(project, [
            _note(0, "Primera.", sub_id=None, content="a"),
            _note(2, "Segunda.", sub_id=None, content="b"),
        ])

        reanchor.reanchor_chapter(project, CH)

        live = _live(project)
        assert {k: v["content"] for k, v in live.items()} == {(2, None): "a", (4, None): "b"}

    def test_two_legacy_notes_landing_on_one_row_both_survive(self, project):
        _write_rows(project, [
            {"es_idx": 0, "es_indices": [0, 1], "es": "—Ven —dijo. Y se fue."},
        ])
        write_annotations(project, [
            _note(0, "—Ven —dijo.", sub_id=None, content="a"),
            _note(1, "Y se fue.", sub_id=None, content="b"),
        ])

        reanchor.reanchor_chapter(project, CH)

        live = _live(project)
        assert sorted(v["content"] for v in live.values()) == ["a", "b"]
        assert {k[0] for k in live} == {0}

    def test_dry_run_writes_nothing(self, project):
        _align(project, ["Nueva.", "El perro."])
        write_annotations(project, [_note(0, "El perro.")])
        result = reanchor.reanchor_chapter(project, CH, dry_run=True)
        assert len(result.moved) == 1
        assert len(_raw_rows(project)) == 1

    def test_other_chapters_are_untouched(self, project):
        _align(project, ["Nueva.", "El perro."])
        other = {**_note(0, "El perro."), "chapter_id": "chapter_02"}
        write_annotations(project, [_note(0, "El perro."), other])

        reanchor.reanchor_chapter(project, CH)

        assert [r["es_idx"] for r in store.load_active(project, chapter_id="chapter_02")] == [0]


class TestRealignChapter:
    def test_realign_chapter_reanchors(self, project, monkeypatch):
        """The realign the judges and the reader's apply-corrections share."""
        import src.sentence_aligner as aligner
        from src.corrections_apply import realign_chapter

        _align(project, ["El gato.", "El perro."])
        write_annotations(project, [_note(1, "El perro.")])
        (project / "chunks").mkdir()

        def fake_align(chunk_paths, project_id, chapter_id, source_lang, target_lang, output_path):
            _align(project, ["Nueva.", "El gato.", "El perro."])
            return {}

        monkeypatch.setattr(aligner, "align_chapter_chunks", fake_align)

        result = realign_chapter(project, CH)

        assert len(result.moved) == 1
        assert list(_live(project)) == [(2, "u1")]

    def test_a_failing_reanchor_does_not_fail_the_realign(self, project, monkeypatch):
        import src.sentence_aligner as aligner
        from src.corrections_apply import realign_chapter

        (project / "chunks").mkdir()
        monkeypatch.setattr(aligner, "align_chapter_chunks", lambda **kw: {})

        def boom(*args, **kwargs):
            raise RuntimeError("synthetic")

        monkeypatch.setattr(reanchor, "reanchor_chapter", boom)
        assert realign_chapter(project, CH) is None


# Writes its alignments to a temp directory to compare against a baseline; no
# project's alignment changes, so there are no notes to move.
_ALIGN_WITHOUT_REANCHOR = {"scripts/_benchmark_alignment.py"}
_REANCHOR_CALLS = {
    "reanchor_chapter",
    "reanchor_chapter_quietly",
    "_reanchor_annotations_after_realign",
}


def _called_names(node: ast.AST) -> set[str]:
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            names.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    return names


def _functions_that_align() -> list[tuple[str, str]]:
    found = []
    for folder in ("src", "scripts", "web_ui"):
        for path in sorted((REPO_ROOT / folder).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in _ALIGN_WITHOUT_REANCHOR or rel == "src/sentence_aligner.py":
                continue
            source = path.read_text(encoding="utf-8-sig")
            if "align_chapter_chunks(" not in source:
                continue
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                # Only the innermost function that makes the call.
                inner = [
                    n for n in ast.walk(node)
                    if n is not node and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                own_calls = _called_names(node) - set().union(*map(_called_names, inner), set())
                if "align_chapter_chunks" in own_calls:
                    found.append((rel, node.name))
    return found


def test_the_guard_finds_the_known_realign_paths():
    found = set(_functions_that_align())
    assert ("src/corrections_apply.py", "realign_chapter") in found
    assert ("web_ui/app.py", "project_align") in found
    assert len(found) >= 7


@pytest.mark.parametrize("rel,func", _functions_that_align())
def test_every_path_that_realigns_a_chapter_reanchors_its_notes(rel, func):
    """A function that rewrites a chapter's alignment and leaves the notes
    behind is how they came to sit on the wrong sentence. New callers of
    ``align_chapter_chunks`` must re-anchor, or be listed above with a reason."""
    tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8-sig"))
    node = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func
        and "align_chapter_chunks" in _called_names(n)
    )
    assert _called_names(node) & _REANCHOR_CALLS, (
        f"{rel}:{func} calls align_chapter_chunks without re-anchoring annotations"
    )
