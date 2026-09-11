"""The scan wave: the profile gate in code, and candidate validation at commit.

Two claims carry the most weight here.

``scan-prepare`` refuses without a profile. That is how Gate 1 is enforced by the
CLI rather than by prose in a skill file — a wave with no approved taxonomy runs on
the model's own idea of what deserves a note.

``scan-commit`` validates before the user sees anything. A candidate whose quoted
span is not in the aligned sentence cannot become an anchor, so it is reported as
unusable rather than offered as a choice; otherwise research gets spent on a
hallucinated span and the problem surfaces at ``add`` time.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.footnote_pass import scan as fp_scan
from src.footnote_pass.corpus import footnotes_dir

from .conftest import footnote_record, write_annotations


def _prepare(project, profile_file, **kwargs):
    return fp_scan.scan_prepare(project, profile_file=profile_file, **kwargs)


def _draft(project, chapter_id, candidates, *, echo=None):
    path = footnotes_dir(project) / f"{chapter_id}.scan.draft.json"
    path.write_text(
        json.dumps(
            {"chapter_id": echo if echo is not None else chapter_id, "candidates": candidates},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


# --- the profile gate -----------------------------------------------------

def test_prepare_refuses_without_a_profile_file(project, tmp_path):
    out = _prepare(project, tmp_path / "nope.md")
    assert out["status"] == "error"
    assert "profile" in out["error"]
    assert not (footnotes_dir(project) / "manifest.json").exists()


def test_prepare_refuses_an_empty_profile_file(project, tmp_path):
    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")
    out = _prepare(project, empty)
    assert out["status"] == "error"
    assert "empty" in out["error"]


def test_prepare_refuses_a_book_with_no_alignments(tmp_path, profile_file):
    bare = tmp_path / "bare"
    (bare / ".harness").mkdir(parents=True)
    (bare / ".harness" / "config.json").write_text("{}", encoding="utf-8")
    out = _prepare(bare, profile_file)
    assert out["status"] == "error"
    assert "alignment" in out["error"]


def test_prepare_refuses_unknown_chapters(project, profile_file):
    out = _prepare(project, profile_file, chapters=["chapter_99"])
    assert out["status"] == "error"
    assert "chapter_99" in out["error"]


# --- what prepare renders -------------------------------------------------

def test_prepare_renders_one_entry_per_chapter(project, profile_file):
    out = _prepare(project, profile_file)
    assert out["status"] == "ok"
    assert [e["chapter_id"] for e in out["manifest"]] == ["chapter_01", "chapter_02"]
    assert out["usage_summary"]["sentences"] == 6
    assert out["usage_summary"]["headless_effort"] == "medium"
    assert out["usage_summary"]["headless_effort_source"] == "default:footnote_scan"
    for entry in out["manifest"]:
        assert (footnotes_dir(project) / f"{entry['chapter_id']}.scan.prompt.txt").exists()


def test_the_scan_body_carries_the_english_side(project, profile_file):
    """Most candidates are judged against the source, not the translation."""
    _prepare(project, profile_file)
    body = (footnotes_dir(project) / "chapter_01.scan.body.txt").read_text(encoding="utf-8")
    assert "The aphid drips from its cornicles." in body
    assert "El pulgón suelta una gota por sus cuernitos." in body


def test_the_scan_preamble_carries_the_profile_and_no_style_guide(project, profile_file):
    """The wave only detects; style guide and glossary load at drafting time."""
    _prepare(project, profile_file)
    preamble = (footnotes_dir(project) / "preamble.scan.txt").read_text(encoding="utf-8")
    assert "corrects the author's science" in preamble
    assert "Plain, warm, for children." not in preamble
    assert "ostión" not in preamble


def test_already_noted_sentences_are_marked_in_the_body(project, profile_file):
    write_annotations(project, [footnote_record("chapter_01", 1, "[cuernitos] Cornículos.")])
    out = _prepare(project, profile_file)
    entry = next(e for e in out["manifest"] if e["chapter_id"] == "chapter_01")
    assert entry["already_noted"] == [1]
    body = (footnotes_dir(project) / "chapter_01.scan.body.txt").read_text(encoding="utf-8")
    assert "[ALREADY NOTED]" in body
    assert "NEVER propose these" in body


def test_prepare_clears_stale_drafts(project, profile_file):
    _prepare(project, profile_file)
    draft = _draft(project, "chapter_01", [])
    _prepare(project, profile_file)
    assert not draft.exists()


def test_keep_drafts_protects_work_in_flight(project, profile_file):
    _prepare(project, profile_file)
    draft = _draft(project, "chapter_01", [])
    _prepare(project, profile_file, keep_drafts=True)
    assert draft.exists()


def test_prepare_scopes_to_chapters(project, profile_file):
    out = _prepare(project, profile_file, chapters=["chapter_02"])
    assert out["chapters"] == ["chapter_02"]
    assert len(out["manifest"]) == 1


# --- commit validation ----------------------------------------------------

_GOOD = {
    "es_idx": 1,
    "quoted_span": "cuernitos",
    "category": "corrects the author's science",
    "claim": "The author says honeydew comes from the cornicles.",
    "why": "A modern reader would take the claim at face value.",
}


def test_commit_keeps_a_valid_candidate(project, profile_file):
    _prepare(project, profile_file)
    _draft(project, "chapter_01", [dict(_GOOD)])
    _draft(project, "chapter_02", [])

    out = fp_scan.scan_commit(project)
    assert out["status"] == "ok"
    assert out["counts"]["usable"] == 1
    assert out["counts"]["unusable"] == 0
    assert out["by_chapter"] == {"chapter_01": 1}
    assert out["by_category"] == {"corrects the author's science": 1}

    doc = json.loads((footnotes_dir(project) / "candidates.json").read_text(encoding="utf-8"))
    assert doc["candidates"][0]["es_sentence"].startswith("El pulgón")
    assert doc["candidates"][0]["en_sentence"] == "The aphid drips from its cornicles."


def test_commit_rejects_a_span_not_in_the_sentence(project, profile_file):
    """The hallucinated-span case: it cannot be an anchor, so it is never offered."""
    _prepare(project, profile_file)
    _draft(project, "chapter_01", [{**_GOOD, "quoted_span": "tubitos azucarados"}])
    _draft(project, "chapter_02", [])

    out = fp_scan.scan_commit(project)
    assert out["counts"]["usable"] == 0
    assert out["unusable"][0]["reason"] == fp_scan.UNUSABLE_SPAN


def test_commit_rejects_an_unresolvable_es_idx(project, profile_file):
    _prepare(project, profile_file)
    _draft(project, "chapter_01", [{**_GOOD, "es_idx": 99}])
    _draft(project, "chapter_02", [])
    out = fp_scan.scan_commit(project)
    assert out["unusable"][0]["reason"] == fp_scan.UNUSABLE_NO_ROW


def test_commit_rejects_a_non_integer_es_idx(project, profile_file):
    _prepare(project, profile_file)
    _draft(project, "chapter_01", [{**_GOOD, "es_idx": "uno"}])
    _draft(project, "chapter_02", [])
    out = fp_scan.scan_commit(project)
    assert out["unusable"][0]["reason"] == fp_scan.UNUSABLE_NO_ROW


def test_commit_rejects_a_candidate_missing_fields(project, profile_file):
    _prepare(project, profile_file)
    _draft(project, "chapter_01", [{**_GOOD, "claim": ""}])
    _draft(project, "chapter_02", [])
    out = fp_scan.scan_commit(project)
    assert out["unusable"][0]["reason"] == fp_scan.UNUSABLE_FIELDS


def test_commit_rejects_a_re_proposal_of_an_already_noted_sentence(project, profile_file):
    """Belt and braces: the body says not to, and commit enforces it anyway."""
    write_annotations(project, [footnote_record("chapter_01", 1, "[cuernitos] Cornículos.")])
    _prepare(project, profile_file)
    _draft(project, "chapter_01", [dict(_GOOD)])
    _draft(project, "chapter_02", [])
    out = fp_scan.scan_commit(project)
    assert out["counts"]["usable"] == 0
    assert out["unusable"][0]["reason"] == fp_scan.UNUSABLE_ALREADY


def test_commit_reports_a_missing_draft_rather_than_failing(project, profile_file):
    _prepare(project, profile_file)
    _draft(project, "chapter_01", [dict(_GOOD)])
    out = fp_scan.scan_commit(project)
    assert out["missing"] == ["chapter_02"]
    assert out["counts"]["usable"] == 1


def test_commit_reports_a_bad_draft_rather_than_crashing(project, profile_file):
    _prepare(project, profile_file)
    (footnotes_dir(project) / "chapter_01.scan.draft.json").write_text(
        "Here is my analysis: not JSON", encoding="utf-8"
    )
    _draft(project, "chapter_02", [])
    out = fp_scan.scan_commit(project)
    assert out["counts"]["failed"] == 1
    assert out["failed"][0]["chapter_id"] == "chapter_01"


def test_commit_rejects_a_draft_that_echoed_the_wrong_chapter(project, profile_file):
    _prepare(project, profile_file)
    _draft(project, "chapter_01", [dict(_GOOD)], echo="chapter_07")
    _draft(project, "chapter_02", [])
    out = fp_scan.scan_commit(project)
    assert out["counts"]["failed"] == 1
    assert "chapter mismatch" in out["failed"][0]["problem"]


def test_commit_needs_a_manifest(project):
    out = fp_scan.scan_commit(project)
    assert out["status"] == "error"
    assert "scan-prepare" in out["error"]


def test_commit_writes_a_dated_report(project, profile_file):
    _prepare(project, profile_file)
    _draft(project, "chapter_01", [dict(_GOOD)])
    _draft(project, "chapter_02", [{**_GOOD, "quoted_span": "inventado"}])
    out = fp_scan.scan_commit(project)

    body = Path(out["report_path"]).read_text(encoding="utf-8")
    assert "# Footnote candidates" in body
    assert "honeydew comes from the cornicles" in body
    assert "## Unusable" in body
    # The per-candidate claim belongs in the report, not on stdout — for the
    # unusable rows too, which is where it last leaked.
    assert "honeydew" not in json.dumps(out, ensure_ascii=False)
    assert out["unusable"][0]["quoted_span"] == "inventado"


def test_commit_with_no_report_skips_the_file(project, profile_file):
    _prepare(project, profile_file)
    _draft(project, "chapter_01", [])
    _draft(project, "chapter_02", [])
    out = fp_scan.scan_commit(project, report=False)
    assert out["report_path"] is None


def test_an_empty_candidate_list_is_a_valid_answer(project, profile_file):
    _prepare(project, profile_file)
    _draft(project, "chapter_01", [])
    _draft(project, "chapter_02", [])
    out = fp_scan.scan_commit(project)
    assert out["status"] == "ok"
    assert out["counts"] == {
        "chapters": 2,
        "usable": 0,
        "unusable": 0,
        "failed": 0,
        "missing": 0,
    }


# --- fanout (stubbed runner, nothing spawned) -----------------------------

def test_fanout_needs_a_manifest(project):
    out = fp_scan.scan_fanout(project)
    assert "scan-prepare" in out["error"]


def test_fanout_passes_the_preamble_as_a_system_prompt(project, profile_file):
    _prepare(project, profile_file)
    seen = []

    def runner(cmd, *, input_text, cwd, **kwargs):
        seen.append({"cmd": cmd, "input_text": input_text})
        return 0, json.dumps({"chapter_id": "x", "candidates": []}), ""

    out = fp_scan.scan_fanout(project, runner=runner, concurrency=1)
    assert out["counts"]["wrote"] == 2
    assert "--system-prompt-file" in " ".join(seen[0]["cmd"])
    # The body, not the whole prompt, is what goes on stdin.
    assert seen[0]["input_text"].startswith("CHAPTER: ")


def test_fanout_skips_chapters_that_already_have_a_draft(project, profile_file):
    _prepare(project, profile_file)
    _draft(project, "chapter_01", [])

    def runner(cmd, *, input_text, cwd, **kwargs):
        return 0, "{}", ""

    out = fp_scan.scan_fanout(project, runner=runner, concurrency=1)
    assert out["skipped"] == ["chapter_01"]
    assert out["counts"]["wrote"] == 1


def test_fanout_rejects_unknown_target_ids(project, profile_file):
    _prepare(project, profile_file)
    out = fp_scan.scan_fanout(project, target_ids=["chapter_42"])
    assert "chapter_42" in out["error"]


def test_fanout_rejects_a_manifest_pointing_outside_the_footnotes_dir(
    project, profile_file, tmp_path
):
    _prepare(project, profile_file)
    path = footnotes_dir(project) / "manifest.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["entries"][0]["draft_path"] = str(tmp_path / "escape.json")
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    def runner(cmd, *, input_text, cwd, **kwargs):
        return 0, "{}", ""

    out = fp_scan.scan_fanout(project, runner=runner, concurrency=1)
    assert any("escapes footnotes dir" in f["error"] for f in out["failed"])


def test_fanout_rejects_invalid_concurrency(project, profile_file):
    _prepare(project, profile_file)
    out = fp_scan.scan_fanout(project, concurrency=0)
    assert "concurrency" in out["error"]


# --- draft parsing --------------------------------------------------------

def test_parse_scan_draft_tolerates_a_missing_candidates_key():
    assert fp_scan.parse_scan_draft(
        json.dumps({"chapter_id": "chapter_01", "candidates": None}), chapter_id="chapter_01"
    ) == []


def test_parse_scan_draft_strips_a_markdown_fence():
    raw = '```json\n{"chapter_id": "chapter_01", "candidates": []}\n```'
    assert fp_scan.parse_scan_draft(raw, chapter_id="chapter_01") == []
