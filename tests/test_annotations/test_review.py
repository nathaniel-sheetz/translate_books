"""prepare / fanout / commit / apply — the pipeline contract."""

from __future__ import annotations

import json

import pytest

from src.annotations import review, store
from src.annotations.review import ai_marker, parse_verdict
from src.judges.llm_io import JudgeParseError

from tests.test_annotations.conftest import write_annotations


def _ann(es_idx=0, **kw):
    base = {
        "project_id": "testbook",
        "chapter_id": "chapter_01",
        "es_idx": es_idx,
        "type": "word_choice",
        "content": "",
        "timestamp": "2026-01-01T00:00:00",
    }
    base.update(kw)
    return base


def _verdict(key, **kw):
    base = {
        "key": key,
        "state": "needs_help",
        "state_reason": "blank note",
        "recommendation": "Usar «marco».",
        "note_text": "«Poyo» es regional; «marco» se entiende mejor.",
        "confidence": "high",
        "evidence": ["style guide"],
    }
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


def _draft_all(prep, **kw):
    from pathlib import Path

    for entry in prep["manifest"]:
        Path(entry["draft_path"]).write_text(_verdict(entry["key"], **kw), encoding="utf-8")


# --- verdict parsing -------------------------------------------------------

def test_parse_verdict_rejects_an_unknown_state():
    with pytest.raises(JudgeParseError, match="state must be one of"):
        parse_verdict(_verdict("k", state="maybe"), key="k")


def test_parse_verdict_requires_the_core_fields():
    with pytest.raises(JudgeParseError, match="Missing fields"):
        parse_verdict(json.dumps({"state": "needs_help"}), key="k")


def test_parse_verdict_tolerates_a_fenced_response():
    raw = "Here you go:\n```json\n" + _verdict("k") + "\n```"
    assert parse_verdict(raw, key="k")["state"] == "needs_help"


def test_parse_verdict_flags_a_key_mismatch():
    parsed = parse_verdict(_verdict("other-key"), key="k")
    assert parsed["key"] == "k"
    assert parsed["key_mismatch"] is True


def test_commit_rejects_a_key_mismatch(project):
    """A mis-routed draft must not become writable apply fodder."""
    from pathlib import Path

    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    prep = review.prepare(project)
    entry = prep["manifest"][0]
    Path(entry["draft_path"]).write_text(_verdict("someone-else"), encoding="utf-8")
    out = review.commit(project)

    assert out["counts"]["failed"] == 1
    assert out["counts"]["committed"] == 0
    assert "key_mismatch" in out["failed"][0]["problem"]
    assert out["results"] == []


def test_run_rejects_a_key_mismatch(project, monkeypatch):
    """API backend must fail the same way commit does on a mis-routed key."""
    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    monkeypatch.setattr(review, "call_judge", lambda *a, **kw: _verdict("someone-else"))
    out = review.run(project, cost_limit=9.99)

    assert out["status"] == "ok"
    assert out["counts"]["failed"] == 1
    assert out["counts"]["committed"] == 0
    assert "key_mismatch" in out["failed"][0]["problem"]
    assert out["results"] == []


def test_run_forwards_the_cache_prefix(project, monkeypatch):
    """The API backend caches the per-type preamble the prepare path already splits.

    Two annotations of the same type must send a byte-identical prefix, or every
    call pays a cache write instead of a read.
    """
    write_annotations(
        project,
        [
            _ann(es_idx=1, content="poyo", sub_id="u1"),
            _ann(es_idx=2, content="zaguan", sub_id="u2"),
        ],
    )
    seen = []

    def fake(prompt, **kwargs):
        seen.append((prompt, kwargs.get("cache_prefix")))
        # Key doesn't matter: these verdicts are never committed, and a mismatch
        # still lets the loop run every target.
        return _verdict("ignored")

    monkeypatch.setattr(review, "call_judge", fake)
    review.run(project, cost_limit=9.99)

    assert len(seen) == 2
    for prompt, prefix in seen:
        assert prefix
        assert prompt.startswith(prefix)
    assert seen[0][1] == seen[1][1]


def test_unknown_confidence_falls_back_to_medium():
    assert parse_verdict(_verdict("k", confidence="certain"), key="k")["confidence"] == "medium"


# --- marker ----------------------------------------------------------------

def test_marker_follows_the_target_language():
    assert ai_marker("Spanish") == "— IA:"
    assert ai_marker("German") == "— AI:"


# --- prepare ---------------------------------------------------------------

def test_prepare_writes_a_byte_identical_cache_split(project):
    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    prep = review.prepare(project)
    from pathlib import Path

    entry = prep["manifest"][0]
    preamble = Path(entry["preamble_path"]).read_text(encoding="utf-8")
    body = Path(entry["body_path"]).read_text(encoding="utf-8")
    full = Path(entry["prompt_path"]).read_text(encoding="utf-8")
    assert preamble + body == full


def test_prepare_shares_one_preamble_across_a_type(project):
    write_annotations(
        project,
        [
            _ann(es_idx=0, content="a", sub_id="u1"),
            _ann(es_idx=1, content="b", sub_id="u2"),
        ],
    )
    prep = review.prepare(project)
    paths = {e["preamble_path"] for e in prep["manifest"]}
    assert len(paths) == 1


def test_prepare_clears_stale_drafts_unless_keep_drafts(project):
    from pathlib import Path

    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    prep = review.prepare(project)
    draft = Path(prep["manifest"][0]["draft_path"])
    draft.write_text("stale", encoding="utf-8")

    review.prepare(project)
    assert not draft.exists()

    draft.write_text("in flight", encoding="utf-8")
    review.prepare(project, keep_drafts=True)
    assert draft.read_text(encoding="utf-8") == "in flight"


def test_prepare_carries_skips_into_the_manifest(project):
    write_annotations(project, [_ann(es_idx=999, content="stranded", sub_id="u1")])
    prep = review.prepare(project)
    assert prep["manifest"] == []
    assert [s["reason"] for s in prep["skipped"]] == ["orphaned"]


def test_prepare_batch_size_zero_becomes_one(project):
    """``0`` must not collapse to the default via ``or`` — treat it as unset floor."""
    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    prep = review.prepare(project, batch_size=0)
    assert prep["batch_size"] == 1
    manifest = json.loads(
        (project / ".harness/annotations/manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["batch_size"] == 1


# --- commit ----------------------------------------------------------------

def test_commit_plans_append_for_word_choice(project):
    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    prep = review.prepare(project)
    _draft_all(prep)
    out = review.commit(project)

    result = out["results"][0]
    assert result["mode"] == "append"
    assert result["new_content"] == (
        "poyo\n— IA: «Poyo» es regional; «marco» se entiende mejor."
    )


def test_commit_plans_replace_for_footnote_and_keeps_the_anchor(project):
    """A footnote's content IS the published endnote, so an instruction must go."""
    write_annotations(
        project,
        [_ann(es_idx=2, type="footnote", content="[Sancerre] comillas", sub_id="u1")],
    )
    prep = review.prepare(project)
    _draft_all(prep, note_text="Ciudad del centro de Francia.")
    out = review.commit(project)

    result = out["results"][0]
    assert result["mode"] == "replace"
    assert result["new_content"] == "[Sancerre] Ciudad del centro de Francia."
    assert "comillas" not in result["new_content"]
    assert "IA" not in result["new_content"]


def test_replaced_footnote_publishes_as_clean_prose(project):
    """The endnote path must see only the gloss, with the anchor stripped."""
    from src.endnotes import parse_endnote_content

    write_annotations(
        project,
        [_ann(es_idx=2, type="footnote", content="[Sancerre] comillas", sub_id="u1")],
    )
    prep = review.prepare(project)
    _draft_all(prep, note_text="Ciudad del centro de Francia.")
    out = review.commit(project)

    anchor, published = parse_endnote_content(out["results"][0]["new_content"])
    assert anchor == "Sancerre"
    assert published == "Ciudad del centro de Francia."


def test_already_resolved_is_reported_not_written(project):
    write_annotations(
        project, [_ann(es_idx=1, content="poyo — ya decidido: se queda", sub_id="u1")]
    )
    prep = review.prepare(project)
    _draft_all(prep, state="already_resolved", note_text="")
    out = review.commit(project)

    assert out["results"][0]["writable"] is False
    assert out["counts"]["already_resolved"] == 1


def test_needs_help_without_note_text_is_withheld(project):
    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    prep = review.prepare(project)
    _draft_all(prep, note_text="")
    out = review.commit(project)

    assert out["results"][0]["writable"] is False
    assert out["results"][0]["manual_reason"] == review.MANUAL_NO_NOTE_TEXT


def test_multi_anchor_footnote_is_never_writable(project):
    write_annotations(
        project,
        [_ann(es_idx=2, type="footnote", content="[Sancerre]; [Esaú,]", sub_id="u1")],
    )
    prep = review.prepare(project)
    _draft_all(prep, note_text="Una glosa.")
    out = review.commit(project)

    result = out["results"][0]
    assert result["writable"] is False
    assert result["manual_reason"] == "multi_anchor"
    # The drafted gloss is still reported — that is the value for the reader.
    assert result["note_text"] == "Una glosa."


def test_commit_reports_a_missing_draft(project):
    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    review.prepare(project)
    out = review.commit(project)
    assert out["counts"]["missing"] == 1
    assert out["counts"]["committed"] == 0


def test_commit_reports_an_unparseable_draft(project):
    from pathlib import Path

    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    prep = review.prepare(project)
    Path(prep["manifest"][0]["draft_path"]).write_text("not json", encoding="utf-8")
    out = review.commit(project)
    assert out["counts"]["failed"] == 1


def test_commits_accumulate_across_waves(project):
    """Committing after each wave must not discard the previous wave's plan."""
    from pathlib import Path

    write_annotations(
        project,
        [
            _ann(es_idx=0, content="a", sub_id="u1"),
            _ann(es_idx=1, content="b", sub_id="u2"),
        ],
    )
    prep = review.prepare(project)
    first, second = prep["manifest"]

    Path(first["draft_path"]).write_text(_verdict(first["key"]), encoding="utf-8")
    out1 = review.commit(project)
    assert out1["counts"]["committed"] == 1

    Path(second["draft_path"]).write_text(_verdict(second["key"]), encoding="utf-8")
    out2 = review.commit(project)
    assert {r["key"] for r in out2["results"]} == {first["key"], second["key"]}


def test_report_covers_only_this_run_while_the_plan_accumulates(project):
    """A scoped re-run must not re-render an earlier run's results.

    ``results.json`` is the durable apply plan and accumulates by key; the report
    is a dated record of one run. Rendering the merged plan made a 3-annotation
    word_choice run print 15 results carried over from a footnote run hours
    earlier — with each one's content quoted as of *that* run, which is exactly
    the guarantee the report exists to make.
    """
    from pathlib import Path

    write_annotations(
        project,
        [
            _ann(es_idx=1, content="[poyo] ¿banco?", sub_id="u1", type="word_choice"),
            _ann(es_idx=3, content="[ostra] glosa", sub_id="u2", type="footnote"),
        ],
    )

    # Run 1: footnotes only.
    prep1 = review.prepare(project, types=["footnote"])
    _draft_all(prep1)
    out1 = review.commit(project)
    assert out1["counts"]["committed"] == 1

    # Run 2: word_choice only. Re-preparing clears run 1's drafts.
    prep2 = review.prepare(project, types=["word_choice"])
    _draft_all(prep2)
    out2 = review.commit(project)
    assert out2["counts"]["committed"] == 1

    plan = json.loads(
        (project / ".harness" / "annotations" / "results.json").read_text(encoding="utf-8")
    )
    assert {r["type"] for r in plan["results"]} == {"footnote", "word_choice"}

    text = Path(out2["report_path"]).read_text(encoding="utf-8")
    assert "poyo" in text
    assert "ostra" not in text
    assert "Nota al pie" not in text
    assert "| **1** |" in text


def test_report_flags_the_results_it_does_not_cover(project):
    """Scoping the report must not make the rest of the plan invisible."""
    from pathlib import Path

    write_annotations(
        project,
        [
            _ann(es_idx=1, content="[poyo] ¿banco?", sub_id="u1", type="word_choice"),
            _ann(es_idx=3, content="[ostra] glosa", sub_id="u2", type="footnote"),
        ],
    )
    _draft_all(review.prepare(project, types=["footnote"]))
    review.commit(project)
    prep2 = review.prepare(project, types=["word_choice"])
    _draft_all(prep2)
    out2 = review.commit(project)

    text = Path(out2["report_path"]).read_text(encoding="utf-8")
    assert "results.json" in text
    assert "1 anotación(es)" in text


def test_a_single_run_report_has_no_carried_over_notice(project):
    from pathlib import Path

    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    _draft_all(review.prepare(project))
    out = review.commit(project)
    assert "results.json" not in Path(out["report_path"]).read_text(encoding="utf-8")


def test_commit_writes_a_dated_report(project):
    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    prep = review.prepare(project)
    _draft_all(prep)
    out = review.commit(project)

    from pathlib import Path

    report = Path(out["report_path"])
    assert report.exists()
    assert report.name.startswith("annotations_")
    text = report.read_text(encoding="utf-8")
    # The content as of run time must be logged verbatim.
    assert "poyo" in text


# --- apply -----------------------------------------------------------------

def test_apply_without_select_is_plan_only(project):
    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    prep = review.prepare(project)
    _draft_all(prep)
    review.commit(project)

    before = (project / "annotations.jsonl").read_text(encoding="utf-8")
    out = review.apply(project)
    assert out["dry_run"] is True
    assert out["applied"] == []
    assert (project / "annotations.jsonl").read_text(encoding="utf-8") == before


def test_apply_appends_and_never_rewrites(project):
    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    prep = review.prepare(project)
    _draft_all(prep)
    review.commit(project)
    key = prep["manifest"][0]["key"]

    before = (project / "annotations.jsonl").read_text(encoding="utf-8")
    out = review.apply(project, select=[key])
    after = (project / "annotations.jsonl").read_text(encoding="utf-8")

    assert out["applied"] == [key]
    assert after.startswith(before)
    assert len(after.strip().splitlines()) == len(before.strip().splitlines()) + 1


def test_applied_record_carries_the_sidecar(project):
    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    prep = review.prepare(project)
    _draft_all(prep)
    review.commit(project)
    review.apply(project, select=[prep["manifest"][0]["key"]])

    record = store.load_active(project)[0]
    sidecar = record[store.AI_REVIEW_KEY]
    assert sidecar["mode"] == "append"
    assert sidecar["original_content"] == "poyo"
    assert sidecar["written_content"] == record["content"]


def test_applied_notes_are_skipped_on_the_next_run(project):
    """The anti-duplication guarantee, end to end."""
    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    prep = review.prepare(project)
    _draft_all(prep)
    review.commit(project)
    review.apply(project, select=[prep["manifest"][0]["key"]])

    again = review.prepare(project)
    assert again["manifest"] == []
    assert [s["reason"] for s in again["skipped"]] == ["already_reviewed"]


def test_reapplying_the_same_selection_is_a_no_op(project):
    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    prep = review.prepare(project)
    _draft_all(prep)
    review.commit(project)
    key = prep["manifest"][0]["key"]
    review.apply(project, select=[key])

    lines = len((project / "annotations.jsonl").read_text(encoding="utf-8").strip().splitlines())
    out = review.apply(project, select=[key])
    assert out["already_applied"] == [key]
    assert out["applied"] == []
    assert len((project / "annotations.jsonl").read_text(encoding="utf-8").strip().splitlines()) == lines


def test_apply_refuses_a_note_edited_since_the_review(project):
    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    prep = review.prepare(project)
    _draft_all(prep)
    review.commit(project)
    key = prep["manifest"][0]["key"]

    # The reader edits the note after the review but before applying.
    store.append_record(
        project,
        _ann(es_idx=1, content="poyo — cambié de idea", sub_id="u1",
             timestamp="2026-03-01T00:00:00"),
    )
    out = review.apply(project, select=[key])
    assert out["applied"] == []
    assert [s["key"] for s in out["stale"]] == [key]


def test_already_resolved_is_planned_as_resolved_not_manual(project):
    """A finished note is not "needs a hand" — it is its own bucket."""
    write_annotations(
        project, [_ann(es_idx=1, content="poyo — ya decidido: se queda", sub_id="u1")]
    )
    prep = review.prepare(project)
    _draft_all(prep, state="already_resolved", note_text="")
    review.commit(project)
    key = prep["manifest"][0]["key"]

    out = review.apply(project)
    assert [item["key"] for item in out["resolved"]] == [key]
    assert out["manual"] == []
    assert out["applicable"] == []


def test_retiring_stamps_the_sidecar_without_touching_the_text(project):
    content = "poyo — ya decidido: se queda"
    write_annotations(project, [_ann(es_idx=1, content=content, sub_id="u1")])
    prep = review.prepare(project)
    _draft_all(prep, state="already_resolved", note_text="")
    review.commit(project)
    key = prep["manifest"][0]["key"]

    out = review.apply(project, select=[key])
    assert out["retired"] == [key]
    assert out["applied"] == []

    record = store.load_active(project)[0]
    # The reader's words are untouched; only the sidecar is new.
    assert record["content"] == content
    sidecar = record[store.AI_REVIEW_KEY]
    assert sidecar["mode"] == "noop"
    assert sidecar["state"] == "already_resolved"
    assert sidecar["written_content"] == content
    assert sidecar["original_content"] == content


def test_retired_notes_are_skipped_on_the_next_run(project):
    """The whole point: a finished note stops being re-detected forever."""
    write_annotations(
        project, [_ann(es_idx=1, content="poyo — ya decidido: se queda", sub_id="u1")]
    )
    prep = review.prepare(project)
    _draft_all(prep, state="already_resolved", note_text="")
    review.commit(project)
    review.apply(project, select=[prep["manifest"][0]["key"]])

    again = review.prepare(project)
    assert again["manifest"] == []
    assert [s["reason"] for s in again["skipped"]] == ["already_reviewed"]


def test_re_retiring_the_same_note_is_a_no_op(project):
    write_annotations(
        project, [_ann(es_idx=1, content="poyo — ya decidido", sub_id="u1")]
    )
    prep = review.prepare(project)
    _draft_all(prep, state="already_resolved", note_text="")
    review.commit(project)
    key = prep["manifest"][0]["key"]
    review.apply(project, select=[key])

    lines = len((project / "annotations.jsonl").read_text(encoding="utf-8").strip().splitlines())
    out = review.apply(project, select=[key])
    assert out["already_applied"] == [key]
    assert out["retired"] == []
    assert len(
        (project / "annotations.jsonl").read_text(encoding="utf-8").strip().splitlines()
    ) == lines


def test_retiring_refuses_a_note_edited_since_the_review(project):
    """An edit may have turned a finished note back into a question."""
    write_annotations(
        project, [_ann(es_idx=1, content="poyo — ya decidido", sub_id="u1")]
    )
    prep = review.prepare(project)
    _draft_all(prep, state="already_resolved", note_text="")
    review.commit(project)
    key = prep["manifest"][0]["key"]

    store.append_record(
        project,
        _ann(es_idx=1, content="poyo — ¿o poyete?", sub_id="u1",
             timestamp="2026-03-01T00:00:00"),
    )
    out = review.apply(project, select=[key])
    assert out["retired"] == []
    assert [s["key"] for s in out["stale"]] == [key]


def test_multi_anchor_footnote_stays_manual_even_when_resolved(project):
    """Its text may be fine; publishing only its first bracket still is not."""
    write_annotations(
        project,
        [_ann(es_idx=2, type="footnote", content="[Sancerre]; [Esaú,]", sub_id="u1")],
    )
    prep = review.prepare(project)
    _draft_all(prep, state="already_resolved", note_text="")
    review.commit(project)

    out = review.apply(project)
    assert out["resolved"] == []
    assert [item["reason"] for item in out["manual"]] == ["multi_anchor"]


def test_apply_reports_an_unknown_key(project):
    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    prep = review.prepare(project)
    _draft_all(prep)
    review.commit(project)

    out = review.apply(project, select=["chapter_99__1__nope"])
    assert out["unknown_ids"] == ["chapter_99__1__nope"]


def test_apply_without_a_committed_run_errors(project):
    write_annotations(project, [_ann(es_idx=1, content="poyo", sub_id="u1")])
    out = review.apply(project, select=["anything"])
    assert out["status"] == "error"
