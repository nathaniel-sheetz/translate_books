"""Tests for src/triage/pass_.py — prepare, fanout and commit of a triage wave.

Nothing here spawns a process: ``fanout`` takes a ``runner`` seam, the same one
``tests/test_audit/test_panel.py`` uses, so the whole flow runs offline.

The assertions concentrate on the three ways this pass could do real harm:

* **A draft joined to the wrong findings.** ``parse_draft`` demands the job's
  exact id set, because a batch answering about other findings would file
  verdicts against them.
* **A verdict that hides a finding it should not.** Only ``suppress`` at or above
  the floor suppresses; everything else must leave the finding live.
* **A second commit doubling the sidecar.** Commit has to be re-runnable, since
  a partly-failed wave is the normal case.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.triage import pass_ as tp
from web_ui.evaluations import (
    TRIAGE_CONFIDENCE_FLOOR,
    build_triaged,
    is_triaged,
    issue_key,
    load_all_triage_by_chunk,
)

ES = (
    "Fru Astrida cantaba de los Sigfridos antiguos.\n\n"
    "El niño escuchaba en silencio junto al fuego.\n"
)
CHUNK = "chapter_01_chunk_000"


def _ni(term: str, *, index: int = 0, eval_name: str = "dictionary") -> dict:
    start = ES.index(term)
    return {
        "eval_name": eval_name, "eval_version": "1.0.0", "issue_index": index,
        "severity": "warning",
        "message": f"'{term}': Unknown word (found 1 time(s))",
        "suggestion": None,
        "location": {
            "raw": f"Character position {start}", "side": "target",
            "paragraph_index": 0, "char_start": start, "char_end": start + len(term),
            "snippet_before": "", "match": term, "snippet_after": "",
        },
        "rule_id": None, "category": None, "term": term,
    }


@pytest.fixture
def book(tmp_path: Path) -> Path:
    project = tmp_path / "bk"
    (project / "chunks").mkdir(parents=True)
    (project / "chunks" / f"{CHUNK}.json").write_text(
        json.dumps({"id": CHUNK, "chunk_id": CHUNK, "translated_text": ES,
                    "source_text": "Fru Astrida sang of the old Sigfrids."}),
        encoding="utf-8",
    )
    (project / "alignments").mkdir(parents=True)
    rows = [
        {"es_idx": i, "en_idx": i, "es": p.strip(), "en": "", "chunk_id": CHUNK}
        for i, p in enumerate(p for p in ES.split("\n\n") if p.strip())
    ]
    (project / "alignments" / "chapter_01.json").write_text(
        json.dumps({"alignments": rows}), encoding="utf-8",
    )
    (project / "evaluations").mkdir(parents=True)
    (project / "evaluations" / f"{CHUNK}.json").write_text(
        json.dumps({"chunk_id": CHUNK, "normalized_issues": [
            _ni("Sigfridos", index=0),
            _ni("escuchaba", index=1, eval_name="grammar"),
        ]}),
        encoding="utf-8",
    )
    return project


def _items_in_body(body: str) -> list[dict]:
    """The items array out of a rendered job body.

    Anchored on the ``Items (N):`` line the template emits, and decoded with
    ``raw_decode`` from there. Deliberately not "first ``[`` to last ``]``": the
    body opens with the book context and closes with a return-shape line that
    itself contains braces and brackets, so a bracket-hunting slice spans the
    instruction text and fails to parse.
    """
    marker = body.index("Items (")
    start = body.index("[", marker)
    return json.JSONDecoder().raw_decode(body[start:])[0]


def _answering_runner(verdict="suppress", confidence=0.95, seen=None):
    """A runner that answers every item in the body it is handed."""
    def runner(cmd, *, input_text, cwd, **kwargs):
        if seen is not None:
            seen.append(input_text)
        return 0, json.dumps([
            {"item": it["item"], "verdict": verdict, "confidence": confidence,
             "reason": "a proper noun"}
            for it in _items_in_body(input_text)
        ]), ""
    return runner


def _run(book: Path, **kw):
    assert tp.prepare(book, worker_model="m", cli="cursor")["status"] == "ok"
    out = tp.fanout(book, runner=_answering_runner(**kw), concurrency=1)
    assert not out["failed"], out
    return tp.commit(book)


# --- prepare -----------------------------------------------------------------

def test_prepare_collects_both_checkers_and_pins_the_model(book: Path):
    out = tp.prepare(book, worker_model="grok-4.6", cli="cursor")
    assert out["status"] == "ok"
    assert out["items"] == 2
    assert out["by_eval"] == {"dictionary": 1, "grammar": 1}
    assert out["effective"]["worker_model"] == "grok-4.6"
    assert out["effective"]["command"] == "triage"


def test_prepare_writes_a_manifest_fanout_can_read(book: Path):
    tp.prepare(book, worker_model="m", cli="cursor")
    manifest, error = tp.load_manifest(book)
    assert error is None
    assert manifest["model"] == "m"
    assert len(manifest["items"]) == 2
    assert manifest["run_id"].startswith("triage-")


def test_prepare_batches_by_items_per_job(book: Path):
    out = tp.prepare(book, worker_model="m", cli="cursor", items_per_job=1)
    assert out["jobs"] == 2


def test_prepare_refuses_to_render_over_existing_drafts(book: Path):
    tp.prepare(book, worker_model="m", cli="cursor")
    draft = tp.triage_dir(book) / "drafts" / "m" / "job-001.json"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("[]", encoding="utf-8")
    out = tp.prepare(book, worker_model="m", cli="cursor")
    assert out["status"] == "error"
    assert "drafts" in out["error"]


def test_prepare_errors_when_there_is_nothing_to_triage(tmp_path: Path):
    empty = tmp_path / "bk"
    (empty / "evaluations").mkdir(parents=True)
    out = tp.prepare(empty, worker_model="m", cli="cursor")
    assert out["status"] == "error"
    assert "no findings" in out["error"]


def test_the_preamble_is_shared_and_carries_no_items(book: Path):
    """It must be byte-identical across jobs or the prompt cache never hits."""
    tp.prepare(book, worker_model="m", cli="cursor", items_per_job=1)
    preamble = (tp.triage_dir(book) / tp.PREAMBLE_FILENAME).read_text(encoding="utf-8")
    assert "Sigfridos" not in preamble
    bodies = sorted((tp.triage_dir(book) / "jobs").glob("*.body.txt"))
    assert len(bodies) == 2
    assert all("Sigfridos" in b.read_text(encoding="utf-8")
               or "escuchaba" in b.read_text(encoding="utf-8") for b in bodies)


# --- fanout ------------------------------------------------------------------

def test_fanout_folds_the_preamble_in_and_writes_a_draft(book: Path):
    tp.prepare(book, worker_model="m", cli="cursor")
    seen: list[str] = []
    out = tp.fanout(book, runner=_answering_runner(seen=seen), concurrency=1)
    assert out["counts"]["wrote"] == 1
    assert not out["failed"]
    assert seen and "Sigfridos" in seen[0]


def test_fanout_resumes_past_written_drafts(book: Path):
    tp.prepare(book, worker_model="m", cli="cursor")
    tp.fanout(book, runner=_answering_runner(), concurrency=1)
    seen: list[str] = []
    out = tp.fanout(book, runner=_answering_runner(seen=seen), concurrency=1)
    assert out["counts"]["skipped"] == 1
    assert seen == []


def test_fanout_refuses_unknown_job_ids(book: Path):
    tp.prepare(book, worker_model="m", cli="cursor")
    out = tp.fanout(book, job_ids=["job-999"], runner=_answering_runner())
    assert "not in the manifest" in out["error"]


def test_fanout_without_a_manifest_is_an_error(tmp_path: Path):
    out = tp.fanout(tmp_path / "nope", runner=_answering_runner())
    assert "prepare" in out["error"]


# --- commit ------------------------------------------------------------------

def test_commit_writes_verdicts_that_actually_suppress(book: Path):
    out = _run(book, verdict="suppress", confidence=0.95)
    assert out["status"] == "ok"
    assert out["written"] == 2
    assert out["suppressed"] == 2 and out["kept"] == 0

    records = load_all_triage_by_chunk(book)[CHUNK]
    by_key = build_triaged(records)
    assert is_triaged(by_key, "dictionary", _ni("Sigfridos")) is True


def test_a_low_confidence_suppress_hides_nothing(book: Path):
    out = _run(book, verdict="suppress", confidence=TRIAGE_CONFIDENCE_FLOOR - 0.1)
    assert out["written"] == 2
    assert out["suppressed"] == 0 and out["kept"] == 2

    by_key = build_triaged(load_all_triage_by_chunk(book)[CHUNK])
    assert is_triaged(by_key, "dictionary", _ni("Sigfridos")) is False


def test_a_keep_hides_nothing_however_confident(book: Path):
    out = _run(book, verdict="keep", confidence=1.0)
    assert out["suppressed"] == 0 and out["kept"] == 2
    by_key = build_triaged(load_all_triage_by_chunk(book)[CHUNK])
    assert is_triaged(by_key, "dictionary", _ni("Sigfridos")) is False


def test_the_verdict_records_the_term_and_the_run(book: Path):
    """Both are what make a re-key a script rather than a re-run."""
    _run(book)
    records = load_all_triage_by_chunk(book)[CHUNK]
    dictionary = next(r for r in records if r["eval_name"] == "dictionary")
    assert dictionary["term"] == "Sigfridos"
    assert dictionary["issue_key"] == issue_key("dictionary", _ni("Sigfridos"))
    assert dictionary["run_id"].startswith("triage-")
    assert dictionary["model"] == "m"
    assert dictionary["prompt_version"]


def test_commit_is_re_runnable_and_does_not_double_write(book: Path):
    _run(book)
    before = len(load_all_triage_by_chunk(book)[CHUNK])
    again = tp.commit(book)
    assert again["written"] == 0
    assert again["already_recorded"] == 2
    assert len(load_all_triage_by_chunk(book)[CHUNK]) == before


def test_commit_sets_a_bad_draft_aside_and_reports_it(book: Path):
    tp.prepare(book, worker_model="m", cli="cursor")
    draft = tp.triage_dir(book) / "drafts" / "m" / "job-001.json"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("not json at all", encoding="utf-8")
    out = tp.commit(book)
    assert out["failed"] and out["failed"][0]["job"] == "job-001"
    assert (draft.with_name("job-001.rejected.json")).exists()
    assert not draft.exists()


def test_commit_reports_a_job_with_no_draft_as_missing(book: Path):
    tp.prepare(book, worker_model="m", cli="cursor")
    out = tp.commit(book)
    assert out["missing"] == ["job-001"]
    assert out["written"] == 0


def _write_draft(book: Path, rows: list[dict]) -> Path:
    draft = tp.triage_dir(book) / "drafts" / "m" / "job-001.json"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(json.dumps(rows), encoding="utf-8")
    return draft


def test_a_draft_numbering_items_the_job_does_not_have_is_rejected(book: Path):
    """The guard that stops verdicts being filed against the wrong findings.

    An out-of-range number is what is left of that failure once the opaque key
    is off the wire: the model can no longer name a finding in another book, only
    a position this job does not hold.
    """
    tp.prepare(book, worker_model="m", cli="cursor")
    manifest, _ = tp.load_manifest(book)
    count = len(manifest["jobs"][0]["item_ids"])
    _write_draft(book, [
        {"item": 900 + n, "verdict": "suppress", "confidence": 0.99, "reason": "x"}
        for n in range(count)
    ])
    out = tp.commit(book)
    assert out["failed"]
    assert "do not match" in out["failed"][0]["problem"]
    assert load_all_triage_by_chunk(book) == {}


def test_a_draft_repeating_one_item_number_is_rejected(book: Path):
    """The sibling guard: two verdicts for one finding, and no way to pick."""
    tp.prepare(book, worker_model="m", cli="cursor")
    _write_draft(book, [
        {"item": 1, "verdict": "suppress", "confidence": 0.99, "reason": "x"},
        {"item": 1, "verdict": "keep", "confidence": 0.10, "reason": "y"},
    ])
    out = tp.commit(book)
    assert out["failed"]
    assert "more than once" in out["failed"][0]["problem"]
    assert load_all_triage_by_chunk(book) == {}


def test_a_draft_missing_an_item_is_rejected(book: Path):
    """A short answer is not a partial success: the job re-runs whole."""
    tp.prepare(book, worker_model="m", cli="cursor")
    _write_draft(book, [
        {"item": 1, "verdict": "suppress", "confidence": 0.99, "reason": "x"},
    ])
    out = tp.commit(book)
    assert out["failed"]
    assert "do not match" in out["failed"][0]["problem"]
    assert load_all_triage_by_chunk(book) == {}


def test_item_numbers_map_back_to_the_right_findings(book: Path):
    """The risk the ordinal scheme introduces, in exchange for the one it removes.

    A number carries no evidence of which finding it means, so a mapping that
    silently shifted by one would file every verdict against its neighbour and
    nothing would look wrong. The two items here take opposite verdicts, so a
    swap cannot pass.
    """
    tp.prepare(book, worker_model="m", cli="cursor")
    manifest, _ = tp.load_manifest(book)
    item_ids = manifest["jobs"][0]["item_ids"]
    wanted = {
        manifest["items"][iid]["eval_name"]: n
        for n, iid in enumerate(item_ids, 1)
    }
    _write_draft(book, [
        {"item": wanted["dictionary"], "verdict": "suppress",
         "confidence": 0.99, "reason": "a proper noun"},
        {"item": wanted["grammar"], "verdict": "keep",
         "confidence": 0.99, "reason": "a real defect"},
    ])
    out = tp.commit(book)
    assert not out["failed"], out
    assert out["written"] == 2

    records = load_all_triage_by_chunk(book)[CHUNK]
    by_eval = {r["eval_name"]: r for r in records}
    assert by_eval["dictionary"]["verdict"] == "suppress"
    assert by_eval["dictionary"]["term"] == "Sigfridos"
    assert by_eval["grammar"]["verdict"] == "keep"
    assert by_eval["grammar"]["term"] == "escuchaba"


def test_commit_writes_a_report_listing_what_it_suppressed(book: Path):
    out = _run(book, verdict="suppress", confidence=0.95)
    report = Path(out["report_path"]).read_text(encoding="utf-8")
    assert "Sigfridos" in report
    assert "2 suppressed" in report
    assert "Nothing was deleted" in report
