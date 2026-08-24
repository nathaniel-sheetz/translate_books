"""Integration coverage for `run_judges.py apply` — judge-review's fix path.

Exercises the plan-first flow end to end on a tiny throwaway project: preview
(nothing changes), select-and-apply (edit + backup + recombine/realign + archive
+ stale-guard), the reader-queue is left alone, and re-judging clears the stale
marker. Realignment (BERT) is stubbed — this test is about the wiring, not the
aligner.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import run_judges
from src.models import Chunk, ChunkMetadata, ChunkStatus
from src.utils.file_io import load_chunk, save_chunk
from web_ui.evaluations import load_chunk_evaluation, merge_judge_result

CHUNK_ID = "chapter_01_chunk_000"
# One clean swap (— Hola), one instruction suggestion (dijo él), one ambiguous
# excerpt ("bien bien" twice), one whose suggestion restates the words that
# already precede the excerpt, and one whose suggestion is the placeholder "N/A".
# Only the first should be applicable.
TRANSLATED = "— Hola, dijo él. Aquí hay bien bien y más bien bien al final."


def _make_chunk(source: str, translated: str) -> Chunk:
    return Chunk(
        id=CHUNK_ID,
        chapter_id="chapter_01",
        position=0,
        source_text=source,
        translated_text=translated,
        metadata=ChunkMetadata(
            char_start=0,
            char_end=len(source),
            overlap_start=0,
            overlap_end=0,
            paragraph_count=1,
            word_count=len(source.split()),
        ),
        status=ChunkStatus.TRANSLATED,
    )


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "demo"
    (proj / "chunks").mkdir(parents=True)
    save_chunk(_make_chunk("Hi, he said.", TRANSLATED), proj / "chunks" / f"{CHUNK_ID}.json")
    merge_judge_result(
        proj,
        CHUNK_ID,
        "dialogue",
        {
            "eval_name": "dialogue",
            "issues": [
                {"severity": "error", "message": "[raya-spacing] space after raya",
                 "location": "— Hola", "suggestion": "—Hola"},
                {"severity": "warning", "message": "[one-turn-one-paragraph] turn",
                 "location": "dijo él", "suggestion": "move to its own line"},
                {"severity": "warning", "message": "[other] duplicate",
                 "location": "bien bien", "suggestion": "bien"},
                # Keyed to "al final" but rewrites the whole tail, restating the
                # "más bien bien" that already precedes it — splicing this in
                # would duplicate those words in the book.
                {"severity": "warning", "message": "[narration-separation] turn",
                 "location": "al final", "suggestion": "más bien bien al final."},
                # The judge decided this passage was actually fine and hung a note
                # on it instead of a fix. Applying it would splice "N/A" over the
                # line and delete it.
                {"severity": "error", "message": "[other] note, not a fix",
                 "location": "Aquí hay", "suggestion": "N/A"},
            ],
        },
    )
    return proj


def _run(capsys, argv):
    rc = run_judges.main(argv)
    return rc, json.loads(capsys.readouterr().out)


def _stub_realign(monkeypatch, *, noisy: bool = False) -> list[str]:
    """Replace the BERT aligner with a stub that still leaves its artifact.

    The real ``realign_chapter`` writes ``alignments/<chapter>.json`` last, and
    ``apply`` now reads that file's mtime as the receipt that an earlier run
    finished its recombine/realign tail. A stub that writes nothing looks exactly
    like an interrupted run, so it would send every later apply into the repair
    path. Returns the list of chapters the aligner was asked for.
    """
    calls: list[str] = []

    def _fake(project_dir, chapter_id, *args, **kwargs):
        calls.append(chapter_id)
        if noisy:
            print("Downloading model shards: 100%|##########| 3/3")
        align_dir = Path(project_dir) / "alignments"
        align_dir.mkdir(exist_ok=True)
        (align_dir / f"{chapter_id}.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr("src.corrections_apply.realign_chapter", _fake)
    return calls


def test_plan_lists_applicable_and_manual(project, capsys):
    rc, payload = _run(capsys, ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}"])
    assert rc == 0
    assert payload["mode"] == "plan"
    assert {a["id"] for a in payload["applicable"]} == {f"{CHUNK_ID}#0"}
    assert payload["applicable"][0]["old"] == "— Hola"
    assert payload["applicable"][0]["new"] == "—Hola"
    assert {m["reason"] for m in payload["manual"]} == {
        "suggestion_not_literal", "excerpt_ambiguous", "suggestion_restates_context",
        "suggestion_placeholder",
    }
    # Plan mode writes nothing.
    assert not (project / "corrections_applied.jsonl").exists()
    assert "stale" not in load_chunk_evaluation(project, CHUNK_ID)


def test_restating_suggestion_is_withheld_and_cannot_be_selected(project, capsys):
    """A suggestion that repeats adjacent prose never reaches the applicable set."""
    rc, payload = _run(capsys, ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}"])
    assert rc == 0
    restating = [m for m in payload["manual"] if m["id"] == f"{CHUNK_ID}#3"]
    assert len(restating) == 1
    assert restating[0]["reason"] == "suggestion_restates_context"

    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#3"],
    )
    assert rc == 1
    assert payload["manual_ids"] == [f"{CHUNK_ID}#3"]
    # The book is untouched — no duplicated "más bien bien".
    chunk = load_chunk(project / "chunks" / f"{CHUNK_ID}.json")
    assert chunk.translated_text == TRANSLATED


def test_placeholder_suggestion_is_withheld_and_cannot_be_selected(project, capsys):
    """A judge note dressed as a fix ("N/A") must never reach the applicable set.

    Three of these were classified applicable on the 2026-07-29 pollyanna
    whole-book apply; a swap deletes the line it is keyed to. Short enough that the
    `old → new` preview does not make it obvious, so the CLI has to refuse it.
    """
    rc, payload = _run(capsys, ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}"])
    assert rc == 0
    placeholder = [m for m in payload["manual"] if m["id"] == f"{CHUNK_ID}#4"]
    assert len(placeholder) == 1
    assert placeholder[0]["reason"] == "suggestion_placeholder"
    # The operator still sees what the judge actually said.
    assert placeholder[0]["suggestion"] == "N/A"

    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#4"],
    )
    assert rc == 1
    assert payload["manual_ids"] == [f"{CHUNK_ID}#4"]
    chunk = load_chunk(project / "chunks" / f"{CHUNK_ID}.json")
    assert chunk.translated_text == TRANSLATED
    assert "N/A" not in chunk.translated_text


def test_reapplying_an_already_applied_id_is_a_no_op(project, capsys, monkeypatch):
    _stub_realign(monkeypatch)
    argv = ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#0"]
    rc, payload = _run(capsys, argv)
    assert rc == 0
    assert payload["applied"] == [f"{CHUNK_ID}#0"]
    archive_before = (project / "corrections_applied.jsonl").read_text(encoding="utf-8")

    # Same selection again: the desired state already holds.
    rc, payload = _run(capsys, argv)
    assert rc == 0
    assert payload["status"] == "ok"
    assert payload["mode"] == "applied"
    assert payload["applied"] == []
    assert payload["already_applied"] == [f"{CHUNK_ID}#0"]
    assert payload["chapters_realigned"] == []
    # No duplicate audit record, no second backup.
    assert (project / "corrections_applied.jsonl").read_text(encoding="utf-8") == archive_before
    assert len(list((project / ".chunk_edits" / "chapter_01" / CHUNK_ID).glob("*.json"))) == 1


def test_interrupted_run_is_resumed_by_the_same_select(project, capsys, monkeypatch):
    """The 2026-07-29 pollyanna failure: a killed apply must be re-runnable.

    A whole-book apply that is killed mid-run (2-minute foreground limit) leaves
    the edits in the chunks with no audit log and no realign. Re-running the same
    ``--select`` used to report every applied id as ``manual``/``excerpt_not_found``
    with ``already_applied: []`` and rc 1 — the exact inverse of the documented
    contract, and with no path back to a consistent state. It must instead see the
    edits, recover the lost audit rows, and finish the tail.
    """
    calls = _stub_realign(monkeypatch)
    argv = ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#0"]
    rc, _ = _run(capsys, argv)
    assert rc == 0

    # Roll back to exactly what a kill between the edit and the tail leaves: the
    # chunk edited, the snapshot written, and nothing else.
    (project / "corrections_applied.jsonl").unlink()
    (project / "alignments" / "chapter_01.json").unlink()
    evaluation = load_chunk_evaluation(project, CHUNK_ID)
    for key in ("stale", "stale_since", "stale_reason"):
        evaluation.pop(key, None)
    (project / "evaluations" / f"{CHUNK_ID}.json").write_text(
        json.dumps(evaluation, ensure_ascii=False), encoding="utf-8"
    )
    calls.clear()

    rc, payload = _run(capsys, argv)
    assert rc == 0
    assert payload["status"] == "ok"
    assert payload["applied"] == []
    assert payload["already_applied"] == [f"{CHUNK_ID}#0"]
    # The tail the killed run never reached now runs.
    assert calls == ["chapter_01"]
    assert payload["chapters_realigned"] == ["chapter_01"]
    assert payload["stale_marked"] == [CHUNK_ID]
    assert load_chunk_evaluation(project, CHUNK_ID)["stale"] is True
    assert any("never ran for this chapter" in w for w in payload["warnings"])

    # The audit row is back, exactly once, and marked as recovered.
    rows = [
        json.loads(line)
        for line in (project / "corrections_applied.jsonl")
        .read_text(encoding="utf-8").strip().splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["original_es"] == "— Hola"
    assert rows[0]["corrected_es"] == "—Hola"
    assert rows[0]["recovered"] is True
    assert rows[0]["source"] == "judge:dialogue"
    assert any("recovered" in w for w in payload["warnings"])

    # The edit itself is untouched — resuming never re-applies.
    assert load_chunk(project / "chunks" / f"{CHUNK_ID}.json").translated_text.count("—Hola") == 1
    assert len(list((project / ".chunk_edits" / "chapter_01" / CHUNK_ID).glob("*.json"))) == 1


def _simulate_kill_after_edit(project, chunk_id=CHUNK_ID):
    """Leave the post-edit / pre-tail state a killed apply leaves on disk."""
    archive = project / "corrections_applied.jsonl"
    if archive.exists():
        archive.unlink()
    align = project / "alignments" / "chapter_01.json"
    if align.exists():
        align.unlink()
    evaluation = load_chunk_evaluation(project, chunk_id)
    for key in ("stale", "stale_since", "stale_reason"):
        evaluation.pop(key, None)
    (project / "evaluations" / f"{chunk_id}.json").write_text(
        json.dumps(evaluation, ensure_ascii=False), encoding="utf-8"
    )


def test_mixed_select_still_repairs_already_applied(project, capsys, monkeypatch):
    """A bad id in --select must not skip audit recovery / resume realign.

    Mixed ``already_applied`` + ``manual`` used to early-return before the repair
    path, so the recovery contract only held on a perfectly clean select.
    """
    calls = _stub_realign(monkeypatch)
    argv_ok = ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#0"]
    assert _run(capsys, argv_ok)[0] == 0
    _simulate_kill_after_edit(project)
    calls.clear()

    rc, payload = _run(
        capsys,
        [
            "apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}",
            "--select", f"{CHUNK_ID}#0,{CHUNK_ID}#1",
        ],
    )
    assert rc == 1
    assert payload["status"] == "error"
    assert payload["manual_ids"] == [f"{CHUNK_ID}#1"]
    assert payload["already_applied"] == [f"{CHUNK_ID}#0"]
    assert payload["chapters_realigned"] == ["chapter_01"]
    assert calls == ["chapter_01"]
    rows = [
        json.loads(line)
        for line in (project / "corrections_applied.jsonl")
        .read_text(encoding="utf-8").strip().splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["recovered"] is True


def test_resume_with_no_realign_defers_honestly(project, capsys, monkeypatch):
    """Resume under --no-realign must not claim it is finishing the tail now."""
    calls = _stub_realign(monkeypatch)
    argv = ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#0"]
    assert _run(capsys, argv)[0] == 0
    _simulate_kill_after_edit(project)
    calls.clear()

    rc, payload = _run(
        capsys,
        [
            "apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}",
            "--select", f"{CHUNK_ID}#0", "--no-realign",
        ],
    )
    assert rc == 0
    assert payload["status"] == "ok"
    assert payload["already_applied"] == [f"{CHUNK_ID}#0"]
    assert payload["chapters_realigned"] == []
    assert payload["chapters_pending_realign"] == ["chapter_01"]
    assert calls == []
    assert any("Deferred again under --no-realign" in w for w in payload["warnings"])
    assert not any("Finishing it now" in w for w in payload["warnings"])


def test_already_applied_plus_all_pending_fail_is_partial_not_error(
    project, capsys, monkeypatch
):
    """Resume-worthy already_applied work must not be reported as a blank failure.

    When every still-pending id fails to locate, but at least one selected id was
    already applied, the run is ``partial`` (rc 0) — not ``status: error``.
    """
    _stub_realign(monkeypatch)
    assert _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#0"],
    )[0] == 0

    # Keep #0 as the already-applied finding; add a second applicable span.
    merge_judge_result(
        project,
        CHUNK_ID,
        "dialogue",
        {
            "eval_name": "dialogue",
            "issues": [
                {"severity": "error", "message": "[raya-spacing] space after raya",
                 "location": "— Hola", "suggestion": "—Hola"},
                {"severity": "error", "message": "[other] rename",
                 "location": "dijo él", "suggestion": "dijo ella"},
            ],
        },
    )

    def _apply_none(chunk, records):
        return chunk, 0, []

    monkeypatch.setattr("src.corrections_apply.apply_to_chunk", _apply_none)

    rc, payload = _run(
        capsys,
        [
            "apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}",
            "--select", f"{CHUNK_ID}#0,{CHUNK_ID}#1",
        ],
    )
    assert rc == 0
    assert payload["status"] == "partial"
    assert payload["already_applied"] == [f"{CHUNK_ID}#0"]
    assert payload["applied"] == []
    assert payload["failed"] == [f"{CHUNK_ID}#1"]


def test_resume_needs_snapshot_proof_not_just_the_desired_state(project, capsys):
    """Without proof the excerpt ever existed, a missing excerpt stays manual.

    The address judge paraphrases excerpts often enough (29% of pollyanna's
    findings) that "the old text isn't there and the new text occurs once" cannot
    on its own mean "we already applied this" — otherwise a never-applicable
    finding would be reported as done.
    """
    chunk_path = project / "chunks" / f"{CHUNK_ID}.json"
    chunk = load_chunk(chunk_path)
    # Desired state holds for #0 (excerpt gone, suggestion present once), but no
    # archive row and no snapshot exist.
    chunk.translated_text = TRANSLATED.replace("— Hola", "—Hola")
    save_chunk(chunk, chunk_path)

    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#0"],
    )
    assert rc == 1
    assert payload["already_applied"] == []
    assert payload["manual_ids"] == [f"{CHUNK_ID}#0"]


def test_archive_is_written_per_chunk_not_once_at_the_end(project, capsys, monkeypatch):
    """A kill part-way through must leave a consistent prefix, not zero rows.

    The audit log used to be written in one batch after every chunk had been
    edited, so the window in which a long apply gets killed was exactly the window
    in which the edits exist and nothing records them.
    """
    _stub_realign(monkeypatch)
    second_chunk_id = "chapter_01_chunk_001"
    second = _make_chunk("Hi again.", "— Adiós, dijo ella.")
    second.id = second_chunk_id
    second.position = 1
    save_chunk(second, project / "chunks" / f"{second_chunk_id}.json")
    merge_judge_result(
        project,
        second_chunk_id,
        "dialogue",
        {
            "eval_name": "dialogue",
            "issues": [
                {"severity": "error", "message": "[raya-spacing] space after raya",
                 "location": "— Adiós", "suggestion": "—Adiós"},
            ],
        },
    )

    calls: list[int] = []
    from src import corrections_apply

    original = corrections_apply.archive_applied_records

    def _die_on_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt("killed mid-apply")
        return original(*args, **kwargs)

    monkeypatch.setattr("src.corrections_apply.archive_applied_records", _die_on_second)

    with pytest.raises(KeyboardInterrupt):
        run_judges.main(
            [
                "apply", "--project", str(project), "--scope", "chapter:chapter_01",
                "--select", f"{CHUNK_ID}#0,{second_chunk_id}#0",
            ]
        )

    # The first chunk is edited *and* archived; the second is neither realigned
    # nor recorded, and the run stopped before touching anything else.
    assert len(calls) == 2
    rows = [
        json.loads(line)
        for line in (project / "corrections_applied.jsonl")
        .read_text(encoding="utf-8").strip().splitlines()
    ]
    assert [r["chunk_id"] for r in rows] == [CHUNK_ID]
    assert "—Hola" in load_chunk(project / "chunks" / f"{CHUNK_ID}.json").translated_text


def test_already_applied_requires_unique_suggestion_not_a_short_substring(
    project, capsys, monkeypatch
):
    """An archive hit plus a common substring must not report already_applied.

    Short ``corrected_es`` values (``Él``, ``dijo``) appear many times; treating
    any ``in`` hit as "edit present" would green-light a no-op while the real
    swap never landed (excerpt already gone for other reasons).
    """
    _stub_realign(monkeypatch)
    # Excerpt gone; suggestion "Él" still appears twice. Archive claims the swap.
    chunk_path = project / "chunks" / f"{CHUNK_ID}.json"
    chunk = load_chunk(chunk_path)
    chunk.translated_text = "— Él dijo. Él respondió. Aquí hay bien bien al final."
    save_chunk(chunk, chunk_path)
    (project / "corrections_applied.jsonl").write_text(
        json.dumps(
            {
                "chunk_id": CHUNK_ID,
                "original_es": "MISSING_EXCERPT",
                "corrected_es": "Él",
                "source": "judge:dialogue",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    merge_judge_result(
        project,
        CHUNK_ID,
        "dialogue",
        {
            "eval_name": "dialogue",
            "issues": [
                {
                    "severity": "warning",
                    "message": "[other] short",
                    "location": "MISSING_EXCERPT",
                    "suggestion": "Él",
                },
            ],
        },
    )
    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#0"],
    )
    assert rc == 1
    assert payload["status"] == "error"
    assert f"{CHUNK_ID}#0" not in (payload.get("already_applied") or [])
    assert payload["manual_ids"] == [f"{CHUNK_ID}#0"]


def test_already_applied_rejects_when_unique_suggestion_but_excerpt_remains():
    """Unique ``corrected_es`` is not enough if ``original_es`` is still in the chunk."""
    archived = [
        {
            "chunk_id": CHUNK_ID,
            "original_es": "OLD_UNIQUE_PHRASE",
            "corrected_es": "NEW_UNIQUE_PHRASE",
        }
    ]
    chunk_text = "lead OLD_UNIQUE_PHRASE mid NEW_UNIQUE_PHRASE tail"
    assert chunk_text.count("NEW_UNIQUE_PHRASE") == 1
    assert "OLD_UNIQUE_PHRASE" in chunk_text
    # Desired state requires the excerpt gone; unique suggestion alone is not enough.
    assert not run_judges._desired_state_holds(
        chunk_text, "OLD_UNIQUE_PHRASE", "NEW_UNIQUE_PHRASE"
    )
    assert run_judges._archive_has_edit(
        archived, CHUNK_ID, "OLD_UNIQUE_PHRASE", "NEW_UNIQUE_PHRASE"
    )


def test_restated_after_splice_checks_every_occurrence_not_nearest_hint():
    """A clean twin near the pre-edit hint must not mask a restating occurrence."""
    new = "más bien bien al final."
    # First hit is clean (nearest the hint); second restates the three words before it.
    text = (
        "Aquí hay X. más bien bien al final. "
        "Y luego más bien bien más bien bien al final."
    )
    first = text.find(new)
    second = text.find(new, first + 1)
    assert first != -1 and second != -1 and first != second
    records = [
        {
            "corrected_es": new,
            "original_es": "al final",
            "chunk_offset_start": first,  # old logic would only check this hit
        }
    ]
    repeated = run_judges._restated_after_splice(text, records)
    assert repeated is not None
    assert "más" in repeated


def test_refuse_to_save_when_post_splice_backstop_fires(project, capsys, monkeypatch):
    """If classify lets a restating suggestion through, apply must not write it.

    Forces the post-splice refuse-to-save path that is normally unreachable
    after classify_fix's restatement gate.
    """
    from src.judges.fixes import ProposedFix, ManualFinding, classify_fix as real_classify

    def allow_restating(issue, text):
        result = real_classify(issue, text)
        if (
            isinstance(result, ManualFinding)
            and result.reason == "suggestion_restates_context"
            and result.excerpt
            and result.suggestion
        ):
            start = text.find(result.excerpt)
            assert start != -1
            return ProposedFix(
                excerpt=result.excerpt,
                suggestion=result.suggestion,
                char_start=start,
                char_end=start + len(result.excerpt),
                rule=result.rule,
                severity=result.severity,
                message=result.message,
            )
        return result

    monkeypatch.setattr("src.judges.fixes.classify_fix", allow_restating)
    _stub_realign(monkeypatch)

    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#3"],
    )
    assert rc == 1
    assert payload["status"] == "error"
    assert payload["failed"] == [f"{CHUNK_ID}#3"]
    assert payload["applied"] == []
    assert any("refused to save" in (w or "") for w in (payload.get("warnings") or []))
    # Book untouched — no duplicated "más bien bien", no archive, no backup.
    assert load_chunk(project / "chunks" / f"{CHUNK_ID}.json").translated_text == TRANSLATED
    assert not (project / "corrections_applied.jsonl").exists()
    assert not list((project / ".chunk_edits").rglob("*.json"))


def test_apply_stdout_is_exactly_one_json_object(project, capsys, monkeypatch):
    """The realign/EPUB block's chatter must not land in the JSON contract."""
    _stub_realign(monkeypatch, noisy=True)
    rc = run_judges.main(
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#0"]
    )
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)  # would raise if the banner leaked to stdout
    assert payload["applied"] == [f"{CHUNK_ID}#0"]
    assert "Downloading model shards" not in captured.out
    assert "Downloading model shards" in captured.err
    # The per-chunk progress line goes to stderr too, so an interrupted run shows
    # how far it got instead of leaving zero bytes of output.
    assert f"[apply] {CHUNK_ID}: 1 fix(es) written + archived" in captured.err


def test_apply_selected_edits_backs_up_and_logs(project, capsys, monkeypatch):
    _stub_realign(monkeypatch)
    # A pending reader correction must survive untouched.
    (project / "corrections.jsonl").write_text('{"chunk_id":"x"}\n', encoding="utf-8")

    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#0"],
    )
    assert rc == 0
    assert payload["mode"] == "applied"
    assert payload["applied"] == [f"{CHUNK_ID}#0"]
    assert payload["chapters_realigned"] == ["chapter_01"]
    assert payload["stale_marked"] == [CHUNK_ID]

    # Only the applicable swap landed; manual findings' text is untouched.
    chunk = load_chunk(project / "chunks" / f"{CHUNK_ID}.json")
    assert "—Hola" in chunk.translated_text
    assert "— Hola" not in chunk.translated_text
    assert "dijo él" in chunk.translated_text
    assert chunk.translated_text.count("bien bien") == 2

    # Pre-edit backup written under .chunk_edits/.
    backups = list((project / ".chunk_edits" / "chapter_01" / CHUNK_ID).glob("*.json"))
    assert len(backups) == 1

    # Archived to the shared audit log with judge provenance.
    lines = (project / "corrections_applied.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["source"] == "judge:dialogue"
    assert rec["original_es"] == "— Hola"
    assert rec["corrected_es"] == "—Hola"
    assert "applied_at" in rec

    # Reader queue untouched.
    assert (project / "corrections.jsonl").read_text(encoding="utf-8") == '{"chunk_id":"x"}\n'

    # Evaluation stale-guarded.
    ev = load_chunk_evaluation(project, CHUNK_ID)
    assert ev["stale"] is True
    assert "judge-review apply" in ev["stale_reason"]


def test_rejudge_clears_stale(project, capsys, monkeypatch):
    _stub_realign(monkeypatch)
    _run(capsys, ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#0"])
    assert load_chunk_evaluation(project, CHUNK_ID)["stale"] is True

    # A fresh judge run supersedes the stale marker.
    merge_judge_result(project, CHUNK_ID, "dialogue", {"eval_name": "dialogue", "issues": []})
    ev = load_chunk_evaluation(project, CHUNK_ID)
    assert "stale" not in ev
    assert "stale_since" not in ev
    assert "stale_reason" not in ev


def test_selecting_a_manual_id_is_rejected(project, capsys):
    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#1"],
    )
    assert rc == 1
    assert payload["status"] == "error"
    # Withheld-by-the-classifier is its own bucket: unknown_ids means "no such id".
    assert payload["manual_ids"] == [f"{CHUNK_ID}#1"]
    assert payload["unknown_ids"] == []
    assert not (project / "corrections_applied.jsonl").exists()


def test_selecting_an_id_that_does_not_exist_is_unknown(project, capsys):
    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#99"],
    )
    assert rc == 1
    assert payload["status"] == "error"
    assert payload["unknown_ids"] == [f"{CHUNK_ID}#99"]
    assert payload["manual_ids"] == []


def test_apply_rejects_selected_id_when_text_changed_before_apply(project, capsys):
    """If chunk text changed since the plan, a previously-applicable id is rejected.

    It is re-classified against the *current* text, so it comes back as manual
    (``excerpt_not_found``) rather than as an unrecognized id.
    """
    chunk_path = project / "chunks" / f"{CHUNK_ID}.json"
    chunk = load_chunk(chunk_path)
    chunk.translated_text = chunk.translated_text.replace("— Hola", "XXX")
    save_chunk(chunk, chunk_path)

    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--select", f"{CHUNK_ID}#0"],
    )
    assert rc == 1
    assert payload["status"] == "error"
    assert payload["manual_ids"] == [f"{CHUNK_ID}#0"]
    assert not (project / "corrections_applied.jsonl").exists()


def test_apply_partial_success_archives_only_located_fixes(project, capsys, monkeypatch):
    """When one of two applicable fixes on a chunk cannot locate, report partial apply."""
    _stub_realign(monkeypatch)
    merge_judge_result(
        project,
        CHUNK_ID,
        "dialogue",
        {
            "eval_name": "dialogue",
            "issues": [
                {"severity": "error", "message": "[a] raya", "location": "— Hola", "suggestion": "—Hola"},
                {
                    "severity": "error",
                    "message": "[b] comma",
                    "location": " Hola,",
                    "suggestion": " X,",
                },
            ],
        },
    )
    rc, payload = _run(
        capsys,
        [
            "apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}",
            "--select", f"{CHUNK_ID}#0,{CHUNK_ID}#1",
        ],
    )
    assert rc == 0
    assert payload["status"] == "partial"
    assert payload["applied"] == [f"{CHUNK_ID}#1"]
    assert payload["failed"] == [f"{CHUNK_ID}#0"]
    lines = (project / "corrections_applied.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["source"] == "judge:dialogue"


def test_apply_errors_without_persisted_findings(tmp_path, capsys):
    proj = tmp_path / "bare"
    (proj / "chunks").mkdir(parents=True)
    save_chunk(_make_chunk("Hi", "— Hola x"), proj / "chunks" / f"{CHUNK_ID}.json")
    rc, payload = _run(capsys, ["apply", "--project", str(proj), "--scope", f"chunk:{CHUNK_ID}"])
    assert rc == 1
    assert payload["status"] == "error"
    assert "persist" in payload["error"].lower()


def test_apply_rejects_a_tagged_scope(project, capsys):
    """Tags are a prepare feature; apply must not parse 'address' as a kind."""
    before = load_chunk(project / "chunks" / f"{CHUNK_ID}.json").translated_text
    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", "address:chapter:chapter_01"],
    )
    assert rc == 1
    assert payload["status"] == "error"
    assert "prepare" in payload["error"]
    assert load_chunk(project / "chunks" / f"{CHUNK_ID}.json").translated_text == before


# --- deferring the expensive tail -------------------------------------------


def test_no_realign_defers_the_tail_and_realign_only_settles_it(project, capsys, monkeypatch):
    """Recombine+realign loads a BERT model per chapter; it must be deferrable."""
    calls = _stub_realign(monkeypatch)
    rc, payload = _run(
        capsys,
        [
            "apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}",
            "--select", f"{CHUNK_ID}#0", "--no-realign",
        ],
    )
    assert rc == 0
    assert payload["applied"] == [f"{CHUNK_ID}#0"]
    assert payload["chapters_realigned"] == []
    assert payload["chapters_pending_realign"] == ["chapter_01"]
    assert calls == []
    # The edit and its audit row landed all the same.
    assert "—Hola" in load_chunk(project / "chunks" / f"{CHUNK_ID}.json").translated_text
    assert (project / "corrections_applied.jsonl").exists()

    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--realign-only"],
    )
    assert rc == 0
    assert payload["mode"] == "realign"
    assert payload["chapters_realigned"] == ["chapter_01"]
    assert calls == ["chapter_01"]

    # Nothing left owed: a second repair pass finds every chapter aligned.
    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--realign-only"],
    )
    assert rc == 0
    assert payload["chapters_realigned"] == []
    assert calls == ["chapter_01"]


def test_realign_only_dry_run_reports_without_realigning(project, capsys, monkeypatch):
    calls = _stub_realign(monkeypatch)
    rc, payload = _run(
        capsys,
        [
            "apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}",
            "--realign-only", "--dry-run",
        ],
    )
    assert rc == 0
    assert payload["mode"] == "realign"
    assert payload["chapters_pending_realign"] == ["chapter_01"]
    assert payload["chapters_realigned"] == []
    assert calls == []


def test_realign_only_needs_no_persisted_findings(tmp_path, capsys, monkeypatch):
    """The repair verb must work on a project whose judges were never run."""
    calls = _stub_realign(monkeypatch)
    proj = tmp_path / "bare"
    (proj / "chunks").mkdir(parents=True)
    save_chunk(_make_chunk("Hi", "— Hola x"), proj / "chunks" / f"{CHUNK_ID}.json")
    rc, payload = _run(
        capsys, ["apply", "--project", str(proj), "--scope", "book", "--realign-only"]
    )
    assert rc == 0
    assert payload["chapters_realigned"] == ["chapter_01"]
    assert calls == ["chapter_01"]


@pytest.mark.parametrize(
    "flags, expect",
    [
        (["--no-realign", "--rebuild-epub"], "--no-realign cannot be combined"),
        (["--realign-only", "--select", f"{CHUNK_ID}#0"], "cannot take --select"),
        (["--realign-only", "--no-realign"], "opposites"),
    ],
)
def test_contradictory_flags_error_as_json(project, capsys, flags, expect):
    """Rejected on stdout as JSON — argparse would print usage and exit 2."""
    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", *flags],
    )
    assert rc == 1
    assert payload["status"] == "error"
    assert expect in payload["error"]


# --- both judges in one run -------------------------------------------------


def _add_address_findings(project, issues):
    merge_judge_result(project, CHUNK_ID, "address", {"eval_name": "address", "issues": issues})


def test_two_judges_in_one_run_apply_in_order_and_realign_once(project, capsys, monkeypatch):
    """One user intent = one invocation = one realign (and one snapshot)."""
    calls = _stub_realign(monkeypatch)
    _add_address_findings(
        project,
        [{"severity": "error", "message": "[wrong-form-tu-expected] usted expected",
          "location": "dijo él", "suggestion": "dijo usted"}],
    )
    rc, payload = _run(
        capsys,
        [
            "apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}",
            "--judge", "dialogue", "--judge", "address",
            "--select", f"dialogue:{CHUNK_ID}#0,address:{CHUNK_ID}#0",
        ],
    )
    assert rc == 0
    assert payload["status"] == "ok"
    assert payload["judges"] == ["dialogue", "address"]
    assert payload["judge"] is None
    # With two judges in play, ids echo back qualified — the only unambiguous form.
    assert payload["applied"] == [f"dialogue:{CHUNK_ID}#0", f"address:{CHUNK_ID}#0"]
    assert calls == ["chapter_01"]

    text = load_chunk(project / "chunks" / f"{CHUNK_ID}.json").translated_text
    assert text.startswith("—Hola, dijo usted.")
    # One pre-edit snapshot for the chunk, both judges' rows in the audit log.
    assert len(list((project / ".chunk_edits" / "chapter_01" / CHUNK_ID).glob("*.json"))) == 1
    sources = [
        json.loads(line)["source"]
        for line in (project / "corrections_applied.jsonl")
        .read_text(encoding="utf-8").strip().splitlines()
    ]
    assert sources == ["judge:dialogue", "judge:address"]
    # The stale reason names both judges that touched the chunk.
    assert "dialogue, address" in load_chunk_evaluation(project, CHUNK_ID)["stale_reason"]


def test_a_bare_id_shared_by_two_judges_is_ambiguous(project, capsys):
    """Both judges have a `#0` on this chunk; guessing is not an option."""
    _add_address_findings(
        project,
        [{"severity": "error", "message": "[wrong-form-tu-expected] usted expected",
          "location": "dijo él", "suggestion": "dijo usted"}],
    )
    rc, payload = _run(
        capsys,
        [
            "apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}",
            "--judge", "dialogue", "--judge", "address", "--select", f"{CHUNK_ID}#0",
        ],
    )
    assert rc == 1
    assert payload["ambiguous_ids"] == [f"{CHUNK_ID}#0"]
    assert payload["manual_ids"] == []
    assert payload["unknown_ids"] == []
    assert not (project / "corrections_applied.jsonl").exists()
    assert load_chunk(project / "chunks" / f"{CHUNK_ID}.json").translated_text == TRANSLATED


def test_a_bare_id_only_one_judge_has_still_resolves(project, capsys, monkeypatch):
    """Ambiguity is per id, not per run — an unshared bare id needs no prefix."""
    _stub_realign(monkeypatch)
    other_id = "chapter_01_chunk_001"
    other = _make_chunk("He said.", "— Adiós, dijo él.")
    other.id = other_id
    other.position = 1
    save_chunk(other, project / "chunks" / f"{other_id}.json")
    # Only the address judge has findings on this chunk.
    merge_judge_result(
        project,
        other_id,
        "address",
        {
            "eval_name": "address",
            "issues": [
                {"severity": "error", "message": "[wrong-form-tu-expected] usted expected",
                 "location": "dijo él", "suggestion": "dijo usted"},
            ],
        },
    )
    rc, payload = _run(
        capsys,
        [
            "apply", "--project", str(project), "--scope", "chapter:chapter_01",
            "--judge", "dialogue", "--judge", "address", "--select", f"{other_id}#0",
        ],
    )
    assert rc == 0
    assert payload["applied"] == [f"address:{other_id}#0"]
    assert "dijo usted" in load_chunk(project / "chunks" / f"{other_id}.json").translated_text


def test_second_judge_fix_superseded_by_the_first_is_reported_not_forced(
    project, capsys, monkeypatch
):
    """Apply order between judges matters, and the loser must not be guessed at.

    The address excerpt quotes text the dialogue fix rewrites (pollyanna item 7).
    Re-classified against what dialogue left behind it no longer locates, and
    ``_resolve_correction_span``'s first-match fallback must never get a chance to
    put it somewhere else.
    """
    _stub_realign(monkeypatch)
    _add_address_findings(
        project,
        [{"severity": "error", "message": "[wrong-form-tu-expected] usted expected",
          "location": "— Hola, dijo él", "suggestion": "— Hola, dijo usted"}],
    )
    rc, payload = _run(
        capsys,
        [
            "apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}",
            "--judge", "dialogue", "--judge", "address",
            "--select", f"dialogue:{CHUNK_ID}#0,address:{CHUNK_ID}#0",
        ],
    )
    assert rc == 0
    assert payload["status"] == "partial"
    assert payload["applied"] == [f"dialogue:{CHUNK_ID}#0"]
    assert payload["failed"] == [f"address:{CHUNK_ID}#0"]
    assert any("superseded by another edit" in w for w in payload["warnings"])

    text = load_chunk(project / "chunks" / f"{CHUNK_ID}.json").translated_text
    assert text == TRANSLATED.replace("— Hola", "—Hola")
    assert "dijo usted" not in text
    rows = (project / "corrections_applied.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["source"] == "judge:dialogue"


def test_judge_order_comes_from_the_flags_not_the_select_order(project, capsys, monkeypatch):
    """Which judge edits first decides whose excerpt can be superseded.

    So it must follow ``--judge``, not the order the ids happened to be pasted
    into ``--select``. Here the address fix quotes text the dialogue fix rewrites:
    with dialogue first, address loses — regardless of how --select is written.
    """
    _stub_realign(monkeypatch)
    _add_address_findings(
        project,
        [{"severity": "error", "message": "[wrong-form-tu-expected] usted expected",
          "location": "— Hola, dijo él", "suggestion": "— Hola, dijo usted"}],
    )
    rc, payload = _run(
        capsys,
        [
            "apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}",
            "--judge", "dialogue", "--judge", "address",
            # Address listed first on purpose.
            "--select", f"address:{CHUNK_ID}#0,dialogue:{CHUNK_ID}#0",
        ],
    )
    assert rc == 0
    assert payload["applied"] == [f"dialogue:{CHUNK_ID}#0"]
    assert payload["failed"] == [f"address:{CHUNK_ID}#0"]


def test_plan_carries_judge_and_qualified_id(project, capsys):
    _add_address_findings(
        project,
        [{"severity": "error", "message": "[wrong-form-tu-expected] usted expected",
          "location": "dijo él", "suggestion": "dijo usted"}],
    )
    rc, payload = _run(
        capsys,
        [
            "apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}",
            "--judge", "dialogue", "--judge", "address",
        ],
    )
    assert rc == 0
    assert payload["judges"] == ["dialogue", "address"]
    by_qid = {a["qualified_id"]: a for a in payload["applicable"]}
    assert by_qid[f"address:{CHUNK_ID}#0"]["judge"] == "address"
    assert by_qid[f"address:{CHUNK_ID}#0"]["id"] == f"{CHUNK_ID}#0"
    assert by_qid[f"dialogue:{CHUNK_ID}#0"]["new"] == "—Hola"


# ---------------------------------------------------------------------------
# Payload economy (2026-07-30 friction log, section 6)
#
# The CLI's own output was a measurable share of what an agent had to read:
# _APPLY_SCHEMA alone is ~910 tokens and was re-sent on every invocation, twice
# per apply session, where it was ~52% of the real run's payload.
# ---------------------------------------------------------------------------


def test_schema_is_omitted_from_successful_output(project, capsys):
    rc, payload = _run(capsys, ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}"])
    assert rc == 0
    assert "_schema" not in payload
    assert "--schema" in payload["_schema_hint"]
    # The plan itself is untouched — only its documentation moved behind a flag.
    assert payload["applicable"] and payload["manual"]


def test_schema_flag_brings_it_back(project, capsys):
    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}", "--schema"],
    )
    assert rc == 0
    assert "manual" in payload["_schema"]
    assert "_schema_hint" not in payload


def test_errors_always_carry_the_schema(project, capsys):
    """An error is exactly where a caller needs to know the shape it is reading."""
    rc, payload = _run(
        capsys,
        ["apply", "--project", str(project), "--scope", f"chunk:{CHUNK_ID}",
         "--select", "no_such_id#9"],
    )
    assert rc == 1
    assert "unknown_ids" in payload["_schema"]
