"""One case per row of the ``add`` validation table, plus the real contract.

The round-trip test at the bottom is the one that matters: a record can satisfy
every individual check and still publish nothing, so the suite asserts that
``endnotes.build_endnote_artifacts`` actually numbers and injects what ``add``
wrote. That is the failure this whole skill exists to prevent — the other tests
only pin the diagnoses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.annotations import store
from src.endnotes import build_endnote_artifacts
from src.footnote_pass import ORIGIN
from src.footnote_pass.corpus import footnotes_dir
from src.footnote_pass import ledger as fp_ledger
from src.footnote_pass import write as fp_write

from .conftest import CHAPTER_01, footnote_record, write_annotations


def _codes(rows):
    return {p["code"] for row in rows for p in row["problems"]}


def _add(project, **kwargs):
    dry_run = kwargs.pop("dry_run", False)
    return fp_write.add(project, [kwargs], dry_run=dry_run)


# --- the validation table, one test per row -------------------------------

def test_happy_path_appends_one_record(project):
    out = _add(
        project,
        chapter_id="chapter_01",
        es_idx=1,
        anchor="cuernitos",
        note="La gota sale del ano, no de los cuernitos.",
    )
    assert out["status"] == "ok"
    assert out["counts"] == {
        "requested": 1,
        "added": 1,
        "planned": 0,
        "refused": 0,
        "warnings": 0,
        "dropped": 0,
        "invalid": 0,
        "undecided": 0,
    }
    records = store.load_active(project, types=("footnote",))
    assert len(records) == 1
    record = records[0]
    assert record["content"] == "[cuernitos] La gota sale del ano, no de los cuernitos."
    assert record["origin"] == ORIGIN
    assert record["es_text"] == "El pulgón suelta una gota por sus cuernitos."
    assert record["sub_id"].startswith("u") and len(record["sub_id"]) == 9


def test_no_aligned_sentence_is_refused(project):
    out = _add(project, chapter_id="chapter_01", es_idx=99, anchor=None, note="Algo.")
    assert out["status"] == "partial"
    assert _codes(out["refused"]) == {fp_write.NO_ALIGNED_SENTENCE}
    assert not store.annotations_path(project).exists()


def test_sentence_not_in_body_is_refused(project):
    """An aligned sentence the chapter text does not contain — endnotes.py:168."""
    (project / "chapters" / "chapter_01.txt").write_text(
        "Un cuerpo completamente distinto.", encoding="utf-8"
    )
    out = _add(project, chapter_id="chapter_01", es_idx=1, anchor=None, note="Algo.")
    assert out["status"] == "partial"
    assert _codes(out["refused"]) == {fp_write.SENTENCE_NOT_IN_BODY}


def test_empty_gloss_is_refused(project):
    """The silent one: endnotes.py:180 skips this and logs nothing."""
    out = _add(project, chapter_id="chapter_01", es_idx=2, anchor="Sancerre", note="   ")
    assert out["status"] == "partial"
    assert _codes(out["refused"]) == {fp_write.EMPTY_GLOSS}


def test_anchor_not_found_warns_and_still_writes(project):
    """A bad anchor degrades placement; it never orphans the note."""
    out = _add(
        project, chapter_id="chapter_01", es_idx=2, anchor="Burdeos", note="Villa francesa."
    )
    assert out["status"] == "ok"
    assert out["counts"]["added"] == 1
    assert out["warnings"] == [
        {"chapter_id": "chapter_01", "es_idx": 2, "codes": [fp_write.ANCHOR_NOT_FOUND]}
    ]
    assert out["added"][0]["injection_preview"].endswith("‹N›")


def test_ambiguous_anchor_warns_and_suggests_a_unique_one(project):
    """"abeja" occurs twice in es_idx 3, so the marker lands on the first."""
    out = _add(project, chapter_id="chapter_01", es_idx=3, anchor="abeja", note="Insecto.")
    assert out["status"] == "ok"
    row = out["added"][0]
    assert [p["code"] for p in row["problems"]] == [fp_write.AMBIGUOUS_ANCHOR]
    suggested = row["suggested_anchor"]
    assert suggested and suggested != "abeja"
    # The suggestion must be what it claims: unique within the sentence.
    assert CHAPTER_01[3][1].count(suggested) == 1


def test_multi_anchor_is_refused(project):
    """Extra brackets publish verbatim into the book — targets.py:292."""
    out = fp_write.add(
        project,
        [{"chapter_id": "chapter_01", "es_idx": 2, "note": "[Esaú] y [Montélimar] también."}],
    )
    assert out["status"] == "partial"
    assert _codes(out["refused"]) == {fp_write.MULTI_ANCHOR}


def test_missing_chapter_body_is_refused(project):
    (project / "chapters" / "chapter_01.txt").unlink()
    out = _add(project, chapter_id="chapter_01", es_idx=1, anchor=None, note="Algo.")
    assert out["status"] == "partial"
    assert _codes(out["refused"]) == {fp_write.NO_CHAPTER_BODY}


def test_unknown_chapter_is_refused_not_crashed(project):
    out = _add(project, chapter_id="chapter_77", es_idx=0, anchor=None, note="Algo.")
    assert out["status"] == "partial"
    assert _codes(out["refused"]) >= {fp_write.NO_ALIGNED_SENTENCE}


def test_non_integer_es_idx_is_refused(project):
    out = fp_write.add(
        project, [{"chapter_id": "chapter_01", "es_idx": "ocho", "note": "Algo."}]
    )
    assert out["status"] == "partial"
    assert _codes(out["refused"]) == {fp_write.NO_ALIGNED_SENTENCE}


# --- idempotency, dry run, batches ----------------------------------------

def test_re_adding_the_same_note_is_refused_as_a_duplicate(project):
    kwargs = dict(chapter_id="chapter_01", es_idx=2, anchor="Sancerre", note="Villa francesa.")
    assert _add(project, **kwargs)["counts"]["added"] == 1
    again = _add(project, **kwargs)
    assert again["status"] == "partial"
    assert _codes(again["refused"]) == {fp_write.DUPLICATE}
    assert len(store.load_active(project, types=("footnote",))) == 1


def test_duplicate_check_sees_notes_it_did_not_write(project):
    """A reader's own note on the same sentence with the same text still blocks."""
    write_annotations(project, [footnote_record("chapter_01", 2, "[Sancerre] Villa francesa.")])
    out = _add(project, chapter_id="chapter_01", es_idx=2, anchor="Sancerre", note="Villa francesa.")
    assert _codes(out["refused"]) == {fp_write.DUPLICATE}


def test_two_notes_on_one_sentence_are_allowed_when_they_differ(project):
    """Several footnotes per sentence is a supported shape (sub_id keyed)."""
    _add(project, chapter_id="chapter_01", es_idx=1, anchor="pulgón", note="Insecto chupador.")
    _add(project, chapter_id="chapter_01", es_idx=1, anchor="cuernitos", note="Cornículos.")
    records = store.load_active(project, types=("footnote",))
    assert len(records) == 2
    assert len({r["sub_id"] for r in records}) == 2


def test_dry_run_writes_nothing(project):
    out = _add(
        project, chapter_id="chapter_01", es_idx=1, anchor="cuernitos", note="Algo.", dry_run=True
    )
    assert out["dry_run"] is True
    assert out["counts"] == {
        "requested": 1,
        "added": 0,
        "planned": 1,
        "refused": 0,
        "warnings": 0,
        "dropped": 0,
        "invalid": 0,
        "undecided": 0,
    }
    assert out["planned"][0]["injection_preview"]
    assert not store.annotations_path(project).exists()
    # The proposal report is the one thing a dry run *does* write: it is the
    # review page. The ledger is not touched — a proposal is not a decision.
    assert out["report_path"] and Path(out["report_path"]).exists()
    assert out["ledger_path"] is None
    assert not fp_ledger.decisions_path(project).exists()


def test_json_file_batch_partial_success(project):
    """One bad note must not refuse the batch — the rest land."""
    out = fp_write.add(
        project,
        [
            {"chapter_id": "chapter_01", "es_idx": 0, "anchor": "ostión", "note": "Molusco."},
            {"chapter_id": "chapter_01", "es_idx": 99, "note": "Huérfana."},
            {"chapter_id": "chapter_02", "es_idx": 1, "anchor": "cuarenta", "note": "En 1880."},
        ],
    )
    assert out["status"] == "partial"
    assert out["counts"]["added"] == 2
    assert out["counts"]["refused"] == 1
    assert {r["chapter_id"] for r in out["added"]} == {"chapter_01", "chapter_02"}


def test_identical_entries_in_one_batch_land_once(project):
    note = {"chapter_id": "chapter_01", "es_idx": 2, "anchor": "Sancerre", "note": "Villa."}
    out = fp_write.add(project, [dict(note), dict(note)])
    assert out["counts"]["added"] == 1
    assert _codes(out["refused"]) == {fp_write.DUPLICATE}


def test_append_only_never_rewrites_the_file(project):
    write_annotations(project, [footnote_record("chapter_01", 0, "[ostión] Molusco.")])
    before = store.annotations_path(project).read_text(encoding="utf-8")
    _add(project, chapter_id="chapter_01", es_idx=2, anchor="Sancerre", note="Villa.")
    after = store.annotations_path(project).read_text(encoding="utf-8")
    assert after.startswith(before)


# --- the real contract ----------------------------------------------------

def test_added_note_actually_becomes_a_numbered_endnote(project):
    """The contract no individual check can prove: it publishes.

    A record that validates but produces no endnote is exactly the bug this skill
    exists to prevent, so the round trip goes through the real publisher.
    """
    _add(
        project,
        chapter_id="chapter_01",
        es_idx=1,
        anchor="cuernitos",
        note="La gota sale del ano, no de los cuernitos.",
    )
    body = (project / "chapters" / "chapter_01.txt").read_text(encoding="utf-8")
    injected, entries = build_endnote_artifacts(project, [("chapter_01", body)])

    assert len(entries) == 1
    assert entries[0].number == 1
    assert entries[0].text == "La gota sale del ano, no de los cuernitos."
    # The marker sits immediately after the anchor, not at the sentence end.
    assert "cuernitos{{ENDNOTE:1}}." in injected["chapter_01"]


def test_a_refused_note_would_have_published_nothing(project):
    """The negative half: the codes name real silent failures, not style nits."""
    fp_write.add(project, [{"chapter_id": "chapter_01", "es_idx": 2, "note": "[Sancerre]"}])
    assert not store.annotations_path(project).exists()

    # Land it by hand, bypassing add, and confirm endnotes publishes nothing —
    # which is what `empty_gloss` predicts.
    write_annotations(project, [footnote_record("chapter_01", 2, "[Sancerre]")])
    body = (project / "chapters" / "chapter_01.txt").read_text(encoding="utf-8")
    injected, entries = build_endnote_artifacts(project, [("chapter_01", body)])
    assert entries == []
    assert "{{ENDNOTE" not in injected["chapter_01"]


# --- verify ---------------------------------------------------------------

def test_verify_is_clean_on_notes_add_wrote(project):
    _add(project, chapter_id="chapter_01", es_idx=1, anchor="cuernitos", note="Algo.")
    _add(project, chapter_id="chapter_02", es_idx=0, anchor="colmena", note="Caja de abejas.")
    out = fp_write.verify(project)
    assert out["status"] == "ok"
    assert out["counts"] == {"audited": 2, "ok": 2, "broken": 0, "warned": 0}


def test_verify_catches_a_note_it_did_not_write(project):
    """The half that matters: pre-existing notes, not just the new ones."""
    write_annotations(
        project,
        [
            footnote_record("chapter_01", 1, "[cuernitos] Bien.", sub_id="u1"),
            footnote_record("chapter_01", 99, "[x] Huérfana.", sub_id="u2"),
            footnote_record("chapter_01", 2, "[Sancerre]", sub_id="u3"),
        ],
    )
    out = fp_write.verify(project)
    assert out["status"] == "broken"
    assert out["counts"]["broken"] == 2
    assert out["by_code"] == {
        fp_write.NO_ALIGNED_SENTENCE: 1,
        fp_write.EMPTY_GLOSS: 1,
    }


def _shift_alignment(project, chapter_id, by=1):
    """Simulate ``harness.py align`` re-numbering every row."""
    path = project / "alignments" / f"{chapter_id}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    for row in doc["alignments"]:
        row["es_idx"] += by
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def test_verify_catches_a_re_align_that_drifted_the_sentence(project):
    """``es_idx`` is a position, not an identity — the reason to re-run verify.

    Shifting the rows leaves the index *resolvable*, so nothing is orphaned: the
    note now publishes against a sentence nobody chose. The ``es_text`` snapshot
    ``add`` stores is the only thing that can tell, which is why it is stored.
    """
    _add(project, chapter_id="chapter_01", es_idx=1, anchor="cuernitos", note="Algo.")
    assert fp_write.verify(project)["status"] == "ok"

    _shift_alignment(project, "chapter_01")
    out = fp_write.verify(project)

    assert out["status"] == "ok"  # it still publishes
    assert out["counts"]["warned"] == 1
    assert fp_write.SENTENCE_DRIFTED in out["by_code"]
    assert "misplaced" in out["instructions"]


def test_verify_catches_a_re_align_that_orphaned_the_note(project):
    """When the shifted index runs off the end, the note publishes nothing."""
    _add(project, chapter_id="chapter_02", es_idx=1, anchor="cuarenta", note="En 1880.")
    _shift_alignment(project, "chapter_02", by=5)

    out = fp_write.verify(project)
    assert out["status"] == "broken"
    assert out["broken"][0]["problems"][0]["code"] == fp_write.NO_ALIGNED_SENTENCE


def test_drift_is_not_reported_for_a_note_with_no_snapshot(project):
    """Reader-written notes carry no ``es_text``; absence is not drift."""
    write_annotations(project, [footnote_record("chapter_01", 1, "[cuernitos] Bien.")])
    _shift_alignment(project, "chapter_01")
    out = fp_write.verify(project)
    assert fp_write.SENTENCE_DRIFTED not in out["by_code"]


def test_verify_scopes_to_chapters(project):
    write_annotations(
        project,
        [
            footnote_record("chapter_01", 99, "[x] Huérfana.", sub_id="u1"),
            footnote_record("chapter_02", 0, "[colmena] Bien.", sub_id="u2"),
        ],
    )
    out = fp_write.verify(project, chapters=["chapter_02"])
    assert out["status"] == "ok"
    assert out["counts"]["audited"] == 1


def test_verify_on_a_book_with_no_footnotes(project):
    out = fp_write.verify(project)
    assert out["status"] == "ok"
    assert out["counts"]["audited"] == 0


# --- composition helpers --------------------------------------------------

@pytest.mark.parametrize(
    "anchor,note,expected",
    [
        ("ubres,", "Hoy sabemos…", "[ubres,] Hoy sabemos…"),
        (None, "Hoy sabemos…", "Hoy sabemos…"),
        ("", "Hoy sabemos…", "Hoy sabemos…"),
        ("  x  ", "  y  ", "[x] y"),
    ],
)
def test_compose_content(anchor, note, expected):
    assert fp_write.compose_content(anchor, note) == expected


def test_minted_sub_ids_are_unique_and_clear_of_the_gutenberg_namespace():
    ids = {fp_write.mint_sub_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(i.startswith("u") and not i.startswith("gb") for i in ids)


# --- the decision ledger ---------------------------------------------------
#
# Before this existed, everything between "the scanner proposed N candidates"
# and "N notes are in annotations.jsonl" lived only in a chat transcript: which
# candidates the human cut, which the agent killed during research, what a gloss
# said before it was rewritten, and which claim any landed note came from.
# `candidates.json` is replaced wholesale by the next `scan-commit`, so the
# proposals went with it.


def _candidates(project, rows, *, worker_model="grok-4.6[effort=high,fast=false]"):
    """Write a candidates.json shaped the way `scan_commit` writes one."""
    out = []
    for row in rows:
        out.append(
            {
                **row,
                "candidate_key": fp_ledger.candidate_key(
                    row["chapter_id"], row["es_idx"], row["quoted_span"]
                ),
            }
        )
    doc = {
        "project": project.name,
        "committed_at": "2026-09-11T09:10:59",
        "profile_path": "profile.md",
        "worker_model": worker_model,
        "prompt_version": "abc123",
        "candidates": out,
        "unusable": [],
        "failed": [],
        "missing": [],
    }
    path = footnotes_dir(project) / "candidates.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return doc


_APHID = {
    "chapter_id": "chapter_01",
    "es_idx": 1,
    "quoted_span": "cuernitos",
    "category": "corrects the author's science",
    "claim": "The author says the drop comes from the cornicles.",
    "why": "It comes from the anus.",
    "es_sentence": "El pulgón suelta una gota por sus cuernitos.",
    "en_sentence": "The aphid drips from its cornicles.",
}


def test_a_bare_list_of_keeps_still_works(project):
    """The old approved.json shape: no verdict anywhere. It is all keeps."""
    out = fp_write.add(
        project,
        [{"chapter_id": "chapter_01", "es_idx": 1, "anchor": "cuernitos", "note": "Del ano."}],
    )
    assert out["counts"]["added"] == 1
    assert out["counts"]["dropped"] == 0
    rows = fp_ledger.read_decisions(project)
    assert [r["verdict"] for r in rows] == ["keep"]
    assert rows[0]["outcome"] == "added"


def test_a_drop_row_is_never_validated_or_written(project):
    """A drop is a decision, not a proposal — the validator must not see it.

    The es_idx here would be a hard refusal if it went through validation.
    """
    out = fp_write.add(
        project,
        [
            {"chapter_id": "chapter_01", "es_idx": 0, "anchor": "ostión", "note": "Molusco."},
            {"chapter_id": "chapter_01", "es_idx": 999, "verdict": "drop",
             "stage": "research", "reason": "pedantic once you read the next sentence"},
        ],
        decided=True,
    )
    assert out["status"] == "ok"
    assert out["counts"]["added"] == 1
    assert out["counts"]["refused"] == 0
    assert out["counts"]["dropped"] == 1

    drop = [r for r in fp_ledger.read_decisions(project) if r["verdict"] == "drop"][0]
    assert drop["outcome"] == "dropped"
    assert drop["stage"] == "research"
    assert drop["reason"] == "pedantic once you read the next sentence"


def test_the_ledger_records_the_sub_id_of_every_landed_note(project):
    _candidates(project, [_APHID])
    out = fp_write.add(
        project,
        [{"chapter_id": "chapter_01", "es_idx": 1, "anchor": "cuernitos",
          "note": "La gota sale del ano.", "sources": ["https://example.org/aphids"]}],
        decided=True,
    )
    sub_id = out["added"][0]["sub_id"]
    row = fp_ledger.read_decisions(project)[0]
    assert row["sub_id"] == sub_id
    assert row["annotation_key"] == f"chapter_01__1__{sub_id}"
    assert row["sources"] == ["https://example.org/aphids"]
    # And the join back to the claim that proposed it.
    assert row["join"] == "sentence"
    assert row["candidate"]["claim"].startswith("The author says")
    assert row["proposed_by"]["worker_model"] == "grok-4.6[effort=high,fast=false]"


def test_the_ledger_records_a_refused_note_with_its_codes_and_its_text(project):
    """A refusal is the case where the gloss exists nowhere else on disk."""
    out = fp_write.add(
        project,
        [{"chapter_id": "chapter_01", "es_idx": 99, "note": "Una nota cara de investigar."}],
    )
    assert out["counts"]["refused"] == 1
    row = fp_ledger.read_decisions(project)[0]
    assert row["outcome"] == "refused"
    assert row["sub_id"] is None
    assert [p["code"] for p in row["problems"]] == ["no_aligned_sentence"]
    assert row["content"] == "Una nota cara de investigar."


def test_a_warning_note_lands_and_the_ledger_carries_the_warning_codes(project):
    out = fp_write.add(
        project,
        [{"chapter_id": "chapter_01", "es_idx": 1, "anchor": "Burdeos", "note": "Ciudad."}],
    )
    assert out["counts"]["added"] == 1
    row = fp_ledger.read_decisions(project)[0]
    assert row["outcome"] == "added"
    assert row["warning_codes"] == ["anchor_not_found"]


def test_a_duplicate_refusal_points_at_the_existing_sub_id(project):
    """A duplicate is not work destroyed — the gloss is on the book already."""
    note = {"chapter_id": "chapter_01", "es_idx": 1, "anchor": "cuernitos", "note": "Del ano."}
    first = fp_write.add(project, [note])
    sub_id = first["added"][0]["sub_id"]

    second = fp_write.add(project, [note])
    assert second["counts"]["refused"] == 1
    row = [r for r in fp_ledger.read_decisions(project) if r["outcome"] == "duplicate"]
    assert len(row) == 1
    assert row[0]["existing_sub_id"] == sub_id


def test_the_ledger_is_append_only_across_runs(project):
    fp_write.add(
        project, [{"chapter_id": "chapter_01", "es_idx": 0, "anchor": "ostión", "note": "A."}]
    )
    before = fp_ledger.decisions_path(project).read_text(encoding="utf-8")
    fp_write.add(
        project, [{"chapter_id": "chapter_02", "es_idx": 1, "anchor": "cuarenta", "note": "B."}]
    )
    after = fp_ledger.decisions_path(project).read_text(encoding="utf-8")
    assert after.startswith(before)
    assert len(fp_ledger.read_decisions(project)) == 2


def test_the_proposal_report_carries_the_gloss_and_the_marker_verbatim(project):
    """The Gate 3 review page. A picker cannot hold this; a file can."""
    _candidates(project, [_APHID])
    gloss = "La gota no sale de los cuernitos, sino del ano — hoy lo sabemos."
    out = fp_write.add(
        project,
        [{"chapter_id": "chapter_01", "es_idx": 1, "anchor": "cuernitos", "note": gloss}],
        dry_run=True,
        decided=True,
    )
    text = Path(out["report_path"]).read_text(encoding="utf-8")
    assert "proposed" in text
    assert gloss in text
    assert "‹N›" in text
    assert "El pulgón suelta una gota por sus cuernitos." in text
    assert "corrects the author's science" in text
    assert Path(out["report_path"]).name.endswith("_proposal.md")


def test_undecided_candidates_are_warned_not_refused(project):
    """Exit code stays 0: an omission is not a malformed instruction."""
    other = {**_APHID, "es_idx": 2, "quoted_span": "Sancerre",
             "es_sentence": "Nos fuimos a Sancerre.", "claim": "Sancerre is a place."}
    _candidates(project, [_APHID, other])
    out = fp_write.add(
        project,
        [{"chapter_id": "chapter_01", "es_idx": 1, "anchor": "cuernitos", "note": "Del ano."}],
        decided=True,
    )
    assert out["status"] == "ok"
    assert out["counts"]["undecided"] == 1
    assert out["undecided"][0]["es_idx"] == 2
    assert "neither a keep nor a drop" in out["instructions"]


def test_undecided_is_silent_for_chapters_this_run_did_not_touch(project):
    """candidates.json covers a range; an add often lands one note in one chapter."""
    elsewhere = {**_APHID, "chapter_id": "chapter_02", "es_idx": 1,
                 "quoted_span": "cuarenta", "es_sentence": "La mediana edad llega a los cuarenta."}
    _candidates(project, [_APHID, elsewhere])
    out = fp_write.add(
        project,
        [{"chapter_id": "chapter_01", "es_idx": 1, "anchor": "cuernitos", "note": "Del ano."}],
        decided=True,
    )
    assert out["counts"]["undecided"] == 0


def test_an_inline_single_note_add_does_not_warn_about_candidates(project):
    """No decisions document means no set to be complete against."""
    _candidates(project, [_APHID, {**_APHID, "es_idx": 2, "quoted_span": "Sancerre",
                                   "es_sentence": "Nos fuimos a Sancerre."}])
    out = fp_write.add(
        project,
        [{"chapter_id": "chapter_01", "es_idx": 1, "anchor": "cuernitos", "note": "Del ano."}],
    )
    assert out["counts"]["undecided"] == 0


def test_an_unknown_verdict_is_recorded_not_coerced(project):
    """Guessing which way the operator meant it is how unapproved copy lands."""
    out = fp_write.add(
        project,
        [{"chapter_id": "chapter_01", "es_idx": 1, "anchor": "cuernitos",
          "note": "Del ano.", "verdict": "maybe"}],
        decided=True,
    )
    assert out["status"] == "partial"
    assert out["counts"]["invalid"] == 1
    assert out["counts"]["added"] == 0
    assert not store.annotations_path(project).exists()
    row = fp_ledger.read_decisions(project)[0]
    assert row["verdict"] == "invalid"
    assert "maybe" in row["problem"]


def test_a_decisions_file_of_only_drops_is_a_valid_run(project):
    out = fp_write.add(
        project,
        [{"chapter_id": "chapter_01", "es_idx": 1, "verdict": "drop",
          "stage": "gate2", "reason": "the sentence already explains it"}],
        decided=True,
    )
    assert out["status"] == "ok"
    assert out["counts"]["requested"] == 0
    assert out["counts"]["dropped"] == 1
    assert not store.annotations_path(project).exists()
    assert Path(out["report_path"]).exists()


def test_the_ledger_survives_a_scan_commit_that_replaced_candidates_json(project):
    """The regression this whole module exists to prevent."""
    _candidates(project, [_APHID])
    fp_write.add(
        project,
        [{"chapter_id": "chapter_01", "es_idx": 1, "anchor": "cuernitos", "note": "Del ano."}],
        decided=True,
    )
    # A later scan of a different range replaces the file wholesale.
    _candidates(project, [{**_APHID, "chapter_id": "chapter_02", "es_idx": 1,
                           "quoted_span": "cuarenta", "claim": "Something else entirely.",
                           "es_sentence": "La mediana edad llega a los cuarenta."}])

    row = fp_ledger.read_decisions(project)[0]
    assert row["candidate"]["claim"] == "The author says the drop comes from the cornicles."
    assert row["proposed_by"]["worker_model"] == "grok-4.6[effort=high,fast=false]"


def test_candidate_key_disambiguates_two_candidates_on_one_sentence(project):
    a = fp_ledger.candidate_key("chapter_01", 1, "cuernitos")
    b = fp_ledger.candidate_key("chapter_01", 1, "una gota")
    assert a and b and a != b
    assert a.startswith("chapter_01__1__")
    assert fp_ledger.candidate_key("chapter_01", 1, "") is None


def test_the_join_degrades_to_none_when_two_candidates_share_a_sentence(project):
    """Never guess which claim a note came from."""
    _candidates(
        project,
        [_APHID, {**_APHID, "quoted_span": "una gota", "claim": "A different claim."}],
    )
    fp_write.add(
        project,
        [{"chapter_id": "chapter_01", "es_idx": 1, "anchor": "cuernitos", "note": "Del ano."}],
        decided=True,
    )
    row = fp_ledger.read_decisions(project)[0]
    assert row["join"] == "none"
    assert row["candidate"] is None


def test_an_exact_candidate_key_beats_an_ambiguous_sentence(project):
    doc = _candidates(
        project,
        [_APHID, {**_APHID, "quoted_span": "una gota", "claim": "A different claim."}],
    )
    key = doc["candidates"][0]["candidate_key"]
    fp_write.add(
        project,
        [{"chapter_id": "chapter_01", "es_idx": 1, "anchor": "cuernitos",
          "note": "Del ano.", "candidate_key": key}],
        decided=True,
    )
    row = fp_ledger.read_decisions(project)[0]
    assert row["join"] == "exact"
    assert row["candidate"]["claim"] == "The author says the drop comes from the cornicles."


def test_an_unwritable_report_does_not_lose_the_note(project, monkeypatch):
    """A footnote that landed must never be reported as a failure."""
    from src.footnote_pass import report as fp_report

    def _boom(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(fp_report, "write_decision_report", _boom)
    out = fp_write.add(
        project, [{"chapter_id": "chapter_01", "es_idx": 1, "anchor": "cuernitos", "note": "X."}]
    )
    assert out["status"] == "ok"
    assert out["counts"]["added"] == 1
    assert "read-only file system" in out["ledger_error"]
    assert len(store.load_active(project, types=("footnote",))) == 1
