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

from src.footnote_pass import ledger as fp_ledger
from src.footnote_pass import scan as fp_scan
from src.footnote_pass.corpus import footnotes_dir
from src.footnote_pass.scan import _manifest_path

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
    assert not _manifest_path(project).exists()


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


def test_the_scan_body_is_spanish_only_by_default(project, profile_file):
    """The scanner reads what the book's reader reads; the EN rejoins at commit."""
    _prepare(project, profile_file)
    body = (footnotes_dir(project) / "chapter_01.scan.body.txt").read_text(encoding="utf-8")
    assert "El pulgón suelta una gota por sus cuernitos." in body
    assert "The aphid drips from its cornicles." not in body
    assert "EN:" not in body


def test_source_text_both_restores_the_english_side(project, profile_file):
    out = _prepare(project, profile_file, source_text="both")
    assert out["status"] == "ok"
    body = (footnotes_dir(project) / "chapter_01.scan.body.txt").read_text(encoding="utf-8")
    assert "El pulgón suelta una gota por sus cuernitos." in body
    assert "The aphid drips from its cornicles." in body


def test_the_preamble_states_which_languages_the_worker_got(project, profile_file):
    """A bilingual body must never ride under the 'Spanish ALONE' preamble."""
    _prepare(project, profile_file)
    preamble = (footnotes_dir(project) / "preamble.scan.txt").read_text(encoding="utf-8")
    assert "reading the Spanish ALONE" in preamble
    assert "against the **EN** line" not in preamble

    _prepare(project, profile_file, source_text="both")
    preamble = (footnotes_dir(project) / "preamble.scan.txt").read_text(encoding="utf-8")
    assert "against the **EN** line" in preamble
    assert "reading the Spanish ALONE" not in preamble


def test_prepare_records_the_source_mode_everywhere_it_is_read(project, profile_file):
    """`prompt_version` hashes the shared template, so it cannot tell the modes apart."""
    out = _prepare(project, profile_file, source_text="both")
    assert out["source_text"] == "both"
    assert out["usage_summary"]["source_text"] == "both"
    manifest = json.loads(_manifest_path(project).read_text(encoding="utf-8"))
    assert manifest["source_text"] == "both"

    default = _prepare(project, profile_file)
    assert default["source_text"] == "es"
    assert default["usage_summary"]["source_text"] == "es"
    # Same template, same hash — which is exactly why the field has to exist.
    assert default["manifest"][0]["prompt_version"] == out["manifest"][0]["prompt_version"]


def test_prepare_refuses_an_unknown_source_text(project, profile_file):
    out = _prepare(project, profile_file, source_text="english")
    assert out["status"] == "error"
    assert "source_text" in out["error"]


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


def test_a_spanish_only_scan_still_hands_gate_2_the_english(project, profile_file):
    """The whole safety argument for the default. The worker never saw this text.

    ``render_body`` withholds the EN line; ``scan_commit`` attaches it from the
    alignment regardless, so the claim meets the source at G2 rather than nowhere.
    """
    out_prepare = _prepare(project, profile_file, source_text="es")
    body = (footnotes_dir(project) / "chapter_01.scan.body.txt").read_text(encoding="utf-8")
    assert "The aphid drips from its cornicles." not in body

    _draft(project, "chapter_01", [dict(_GOOD)])
    _draft(project, "chapter_02", [])
    out = fp_scan.scan_commit(project)
    assert out["counts"]["usable"] == 1

    doc = json.loads((footnotes_dir(project) / "candidates.json").read_text(encoding="utf-8"))
    assert doc["candidates"][0]["en_sentence"] == "The aphid drips from its cornicles."
    # …and the mode travels with the candidate set, since the prompt hash cannot
    # say which of the two prompts produced it.
    assert doc["source_text"] == "es"
    assert out_prepare["source_text"] == "es"

    report = Path(out["report_path"]).read_text(encoding="utf-8")
    assert "**Scan read:** the Spanish alone" in report
    assert "The aphid drips from its cornicles." in report


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
    path = _manifest_path(project)
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


# --- the consent gate's one binding answer --------------------------------
#
# The 2026-09-11 fabre2 run priced a wave at "headless, medium effort, Claude
# subscription" and then ran Cursor / grok-4.6 at high for 23 minutes. Both
# numbers came out of the same payload: `usage_summary.headless_effort` said
# medium (the footnote_scan table default) while `worker_model` carried an
# `[effort=high]` bracket copied from the session's cli-config. Nothing was
# wrong except that the gate had two true-looking answers and quoted the inert
# one. These tests pin that there is now exactly one.


def _cursor_cli_config(tmp_path, model="grok-4.6", effort="high"):
    """A stand-in for ~/.cursor/cli-config.json."""
    path = tmp_path / "cli-config.json"
    path.write_text(
        json.dumps(
            {
                "selectedModel": {
                    "modelId": model,
                    "parameters": [
                        {"id": "effort", "value": effort},
                        {"id": "fast", "value": False},
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _pin_cursor(project, tmp_path, monkeypatch, *, effort="high"):
    from src.harness import headless

    (project / ".harness" / "config.json").write_text(
        json.dumps({"target_language": "Spanish", "headless_cli": "cursor"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        headless, "CURSOR_CLI_CONFIG", _cursor_cli_config(tmp_path, effort=effort)
    )
    monkeypatch.setattr(headless, "cli_binary_present", lambda _cli: True)


def test_prepare_reports_one_effective_block(project, profile_file):
    out = _prepare(project, profile_file)
    effective = out["effective"]
    assert effective["cli"] == "claude"
    assert effective["effort"] == "medium"
    assert effective["effort_source"] == "default:footnote_scan"
    # On Claude the level is delivered by argv, by emitting --effort.
    assert effective["effort_channel"] == "argv"
    # The summary is derived from the same profile, so the two cannot disagree.
    assert out["usage_summary"]["headless_effort"] == effective["effort"]
    assert out["usage_summary"]["cli"] == effective["cli"]
    assert out["usage_summary"]["worker_model"] == effective["worker_model"]


def test_prepare_never_reports_an_effort_the_bracket_contradicts(
    project, profile_file, tmp_path, monkeypatch
):
    """The regression the 2026-09-11 friction log is about.

    A book pinned to Cursor inherits the level from the operator's own model
    picker — `resolve_profile` honours that deliberately rather than overwriting
    it with a table default nobody saw. What must never happen again is the
    payload reporting the table default *beside* the bracket that overrides it.
    """
    _pin_cursor(project, tmp_path, monkeypatch, effort="high")
    out = _prepare(project, profile_file)
    effective = out["effective"]

    assert effective["cli"] == "cursor"
    assert "[effort=high" in effective["worker_model"]
    assert effective["effort"] == "high"
    assert effective["effort_source"] == "cursor-cli-config"
    # On Cursor there is no --effort flag; the model's own bracket carries it.
    assert effective["effort_channel"] == "model_bracket"

    # The field the agent used to quote at the gate now says high too. Medium is
    # never printed for this wave.
    assert out["usage_summary"]["headless_effort"] == "high"
    assert out["usage_summary"]["headless_effort_channel"] == "model_bracket"


def test_prepare_persists_the_profile_into_the_manifest(
    project, profile_file, tmp_path, monkeypatch
):
    """So fanout reproduces the consented wave without re-passing --cli."""
    _pin_cursor(project, tmp_path, monkeypatch)
    _prepare(project, profile_file)
    doc = json.loads(_manifest_path(project).read_text(encoding="utf-8"))
    assert doc["cli"] == "cursor"
    assert doc["effort"] == "high"
    assert doc["effort_channel"] == "model_bracket"
    assert "host" in doc


def test_the_scan_manifest_does_not_collide_with_the_translation_wave(
    project, profile_file
):
    """`harness.py footnotes` owns .harness/footnotes/manifest.json.

    Two waves share that directory. Before the rename, whichever prepared last
    silently owned the filename.
    """
    _prepare(project, profile_file)
    assert _manifest_path(project).name == "scan.manifest.json"
    assert not (footnotes_dir(project) / "manifest.json").exists()


def test_commit_stamps_a_candidate_key_on_every_usable_row(project, profile_file):
    _prepare(project, profile_file)
    _draft(
        project,
        "chapter_01",
        [{"es_idx": 1, "quoted_span": "cuernitos", "category": "science",
          "claim": "Aphids drip from their cornicles.", "why": "They do not."}],
    )
    _draft(project, "chapter_02", [])
    out = fp_scan.scan_commit(project)
    doc = json.loads((footnotes_dir(project) / "candidates.json").read_text(encoding="utf-8"))
    key = doc["candidates"][0]["candidate_key"]
    assert key and key.startswith("chapter_01__1__")
    assert out["counts"]["usable"] == 1

    # And the same candidate is in the ledger as `proposed`, so it survives the
    # next commit replacing candidates.json.
    rows = fp_ledger.read_decisions(project)
    assert [r["candidate_key"] for r in rows] == [key]
    assert rows[0]["verdict"] == "proposed"
    assert rows[0]["candidate"]["claim"] == "Aphids drip from their cornicles."
