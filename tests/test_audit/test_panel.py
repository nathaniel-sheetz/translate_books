"""Tests for src/audit/panel.py — prepare and commit of the Phase 0 reader-edit audit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.audit import panel
from src.judges.llm_io import JudgeParseError

EN = (
    "“It was in the year 79. Vesuvius was then a peaceful mountain.\n\n"
    "“The old volcano, which seemed forever lulled, suddenly awakened.”\n\n"
    "Everyone listened in silence for a long while."
)
ES = (
    "»Fue en el año 79. El Vesubio era entonces una montaña apacible.\n\n"
    "»El viejo volcán, que parecía adormecido para siempre, despertó de pronto.\n\n"
    "Todos escucharon en silencio durante un largo rato."
)
MODELS = ["model-a", "model-b", "model-c"]


def _row(audit_id, before, after, en, project="book"):
    return {
        "audit_id": audit_id, "project_id": project, "chapter_id": "ch01",
        "chunk_id": "ch01_chunk_000", "es_idx": 0, "en": en,
        "es_before": before, "es_after": after, "verified_by": "self", "status": "applied",
    }


ROWS = [
    _row(
        "a1",
        "El viejo volcán, que parecía adormecido para siempre, despertó de pronto.",
        "»El viejo volcán, que parecía adormecido para siempre, despertó de pronto.",
        "“The old volcano, which seemed forever lulled, suddenly awakened.”",
    ),
    _row(
        "a2",
        "Todos escucharon en silencio durante un largo tiempo.",
        "Todos escucharon en silencio durante un largo rato.",
        "Everyone listened in silence for a long while.",
    ),
    _row(
        "a3",
        "Una frase que no está en el capítulo.",
        "Otra frase que no está en el capítulo.",
        "A sentence that is not in the chapter.",
    ),
    _row("a4", "Sin cambio en esta frase.", "Sin cambio en esta frase.", "No change."),
]


def _book(root: Path, slug: str, group: str | None = None) -> Path:
    book = root / group / slug if group else root / slug
    (book / "chunks").mkdir(parents=True)
    (book / "corrections_applied.jsonl").write_text("", encoding="utf-8")
    chunk = {"id": "ch01_chunk_000", "source_text": EN, "translated_text": ES}
    (book / "chunks" / "ch01_chunk_000.json").write_text(json.dumps(chunk, ensure_ascii=False), encoding="utf-8")
    return book


def _input(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return path


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "projects"
    _book(root, "book")
    return root, _input(tmp_path / "input.jsonl", ROWS), tmp_path / "run"


def _prepare(setup, **kw):
    root, input_path, run_dir = setup
    kw.setdefault("models", MODELS)
    return panel.prepare(input_path, run_dir, projects_root=root, **kw)


def _manifest(run_dir: Path) -> dict:
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def _items(body: str) -> list[dict]:
    start = body.index("[", body.index("Items ("))
    end = body.rindex("]", 0, body.index("Return a JSON array")) + 1
    return json.loads(body[start:end])


def _draft(run_dir: Path, model: str, job_id: str, text: str) -> Path:
    path = run_dir / "drafts" / panel.model_slug(model) / f"{job_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _answers(votes: dict[str, tuple[str, str]]) -> str:
    return json.dumps([
        {"id": audit_id, "verdict": verdict, "defect_class": cls, "reason": "because",
         "native_rewrite": "Otra cosa." if verdict == "regression" else None}
        for audit_id, (verdict, cls) in votes.items()
    ])


# --- prepare -----------------------------------------------------------------

def test_prepare_batches_rows_in_audit_id_order(setup):
    out = _prepare(setup, rows_per_job=2)
    assert out["status"] == "ok"
    assert out["rows"] == 3  # the unchanged row is dropped
    assert out["jobs"] == 2
    manifest = _manifest(setup[2])
    assert [job["audit_ids"] for job in manifest["jobs"]] == [["a1", "a2"], ["a3"]]
    assert manifest["rows"]["a1"]["job_id"] == "job-001"
    assert manifest["rows"]["a1"]["quote_continues"] is True


def test_prepare_renders_items_in_the_prompt_shape(setup):
    _prepare(setup, rows_per_job=2)
    run_dir = setup[2]
    body = (run_dir / _manifest(run_dir)["jobs"][0]["body_path"]).read_text(encoding="utf-8")
    assert body.startswith("Items (2):\n")
    items = _items(body)
    assert list(items[0]) == [
        "id", "en", "before", "after", "starts_paragraph", "quote_continues",
        "context_before_en", "context_before_es",
    ]
    assert items[0]["starts_paragraph"] is True
    assert items[0]["context_before_es"].startswith("»Fue en el año 79.")


def test_the_preamble_carries_the_rules_and_no_items(setup):
    _prepare(setup)
    preamble = (setup[2] / "preamble.txt").read_text(encoding="utf-8")
    assert "DIALOGUE FORMATTING" in preamble
    assert "quote_continues is read from the English source" in preamble
    assert "{{" not in preamble
    assert "Items (" not in preamble


def test_prepare_reports_rows_without_context(setup):
    out = _prepare(setup)
    assert out["context_missing"] == {
        "count": 1,
        "rows": [{
            "audit_id": "a3", "project_id": "book", "chunk_id": "ch01_chunk_000",
            "reason": "sentence_not_found", "en_found": False, "es_found": False,
        }],
    }


def test_prepare_excludes_then_limits(setup):
    _prepare(setup, exclude_ids=["a1"], limit=1)
    manifest = _manifest(setup[2])
    assert list(manifest["rows"]) == ["a2"]
    assert manifest["filters"] == {"projects": None, "limit": 1, "excluded": 1}


def test_prepare_resolves_a_grouped_book(tmp_path):
    root = tmp_path / "projects"
    _book(root, "grouped", group=".published")
    input_path = _input(tmp_path / "input.jsonl", [dict(ROWS[0], project_id="grouped")])
    out = panel.prepare(input_path, tmp_path / "run", models=MODELS, projects_root=root)
    assert out["status"] == "ok"
    assert out["context_missing"]["count"] == 0


def test_prepare_refuses_a_slug_naming_two_books(tmp_path):
    root = tmp_path / "projects"
    _book(root, "twin")
    _book(root, "twin", group=".published")
    input_path = _input(tmp_path / "input.jsonl", [dict(ROWS[0], project_id="twin")])
    out = panel.prepare(input_path, tmp_path / "run", models=MODELS, projects_root=root)
    assert out["status"] == "error"
    assert out["shared"] == {"twin": [".published/twin", "twin"]}


def test_prepare_still_renders_a_row_whose_book_is_gone(tmp_path):
    input_path = _input(tmp_path / "input.jsonl", [dict(ROWS[0], project_id="ghost")])
    out = panel.prepare(input_path, tmp_path / "run", models=MODELS, projects_root=tmp_path / "projects")
    assert out["rows"] == 1
    assert out["context_missing"]["rows"][0]["reason"] == "book_not_found"


def test_prepare_never_writes_over_a_run(setup):
    assert _prepare(setup)["status"] == "ok"
    out = _prepare(setup)
    assert out["status"] == "error"
    assert "not an empty directory" in out["error"]


def test_prepare_rejects_a_line_that_is_not_an_audit_row(setup):
    setup[1].write_text('{"no_id": true}\n', encoding="utf-8")
    out = _prepare(setup)
    assert out["status"] == "error"
    assert "audit_id" in out["error"]


def test_prepare_refuses_models_sharing_a_directory(setup):
    out = _prepare(setup, models=["grok[a]", "grok-a"])
    assert out["status"] == "error"


# --- parse / consensus --------------------------------------------------------

def test_parse_draft_accepts_a_fenced_array():
    raw = "```json\n" + json.dumps([
        {"id": "a1", "verdict": "Taste", "defect_class": "none", "reason": "r", "native_rewrite": ""},
    ]) + "\n```"
    assert panel.parse_draft(raw, ["a1"]) == [
        {"id": "a1", "verdict": "taste", "defect_class": "none", "reason": "r", "native_rewrite": None},
    ]


@pytest.mark.parametrize("payload, message", [
    ({"id": "a1", "verdict": "taste", "defect_class": "none"}, "expected a JSON array"),
    ([{"id": "a1", "verdict": "better", "defect_class": "none"}], "verdict"),
    ([{"id": "a1", "verdict": "taste", "defect_class": "style"}], "defect_class"),
    ([{"id": "a9", "verdict": "taste", "defect_class": "none"}], "do not match"),
    ([{"id": "a1", "verdict": "taste", "defect_class": "none"}] * 2, "more than once"),
])
def test_parse_draft_rejects(payload, message):
    with pytest.raises(JudgeParseError, match=message):
        panel.parse_draft(json.dumps(payload), ["a1"])


def test_consensus_rules():
    def vote(verdict, cls="none"):
        return {"verdict": verdict, "defect_class": cls}

    pair = ["a", "b"]
    assert panel.consensus({"a": vote("improvement", "typo"), "b": vote("improvement", "typo")}, pair) == "silver"
    assert panel.consensus({"a": vote("improvement", "grammar"), "b": vote("improvement", "typo")}, pair) == "split"
    assert panel.consensus({"a": vote("taste"), "b": vote("taste")}, pair) == "taste"
    assert panel.consensus({"a": vote("taste"), "b": vote("regression", "meaning")}, pair) == "regression_queue"
    assert panel.consensus({"a": vote("taste"), "b": vote("improvement", "typo")}, pair) == "split"
    assert panel.consensus({"a": vote("taste")}, pair) is None


# --- commit ---------------------------------------------------------------------

def test_commit_joins_votes_into_buckets_and_m6(setup):
    _prepare(setup, rows_per_job=3)
    run_dir = setup[2]
    votes = {
        "model-a": {"a1": ("improvement", "punctuation"), "a2": ("regression", "naturalness"), "a3": ("taste", "none")},
        "model-b": {"a1": ("improvement", "punctuation"), "a2": ("regression", "word_choice"), "a3": ("taste", "none")},
        "model-c": {"a1": ("improvement", "punctuation"), "a2": ("regression", "naturalness"), "a3": ("improvement", "grammar")},
    }
    for model, answers in votes.items():
        _draft(run_dir, model, "job-001", _answers(answers))

    out = panel.commit(run_dir)
    assert out["status"] == "ok"
    assert out["complete"] == 3
    assert out["buckets"] == {"silver": 1, "taste": 0, "regression_queue": 1, "split": 1}
    assert out["m6"] == {
        "unanimous_regression": 1, "any_regression": 1, "of": 3, "share": 0.3333, "any_share": 0.3333,
    }
    assert out["by_model"]["model-c"] == {
        "improvement": 2, "taste": 0, "regression": 1, "judged": 3, "failed": 0, "missing": 0,
    }
    assert {"models": ["model-a", "model-b"], "same": 3, "of": 3} in out["agreement"]
    assert {"models": ["model-a", "model-c"], "same": 2, "of": 3} in out["agreement"]
    assert out["failed"] == [] and out["missing"] == []

    results = [json.loads(line) for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["consensus"] for r in results] == ["silver", "regression_queue", "split"]
    assert results[1]["votes"]["model-b"]["native_rewrite"] == "Otra cosa."

    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "**M6, unanimous regression:** 1 of 3 (33.3%)" in report
    assert "### `a2` · book · ch01_chunk_000" in report
    assert "### `a1`" not in report


def test_commit_sets_a_bad_draft_aside_and_lists_what_is_missing(setup):
    _prepare(setup, rows_per_job=3)
    run_dir = setup[2]
    bad = _draft(run_dir, "model-a", "job-001", '[{"id": "a1", "verdict": "better", "defect_class": "none"}]')

    out = panel.commit(run_dir)
    assert [f["model"] for f in out["failed"]] == ["model-a"]
    assert not bad.exists()
    assert bad.with_name("job-001.rejected.json").exists()
    assert {m["model"] for m in out["missing"]} == {"model-b", "model-c"}
    assert out["complete"] == 0
    assert out["m6"]["share"] is None


def test_commit_needs_a_manifest(tmp_path):
    out = panel.commit(tmp_path)
    assert out["status"] == "error"
    assert "prepare" in out["error"]


# --- fanout (stubbed runner, nothing spawned) ----------------------------------

def _answering_runner(seen=None, verdict="improvement", defect_class="grammar"):
    def runner(cmd, *, input_text, cwd, **kwargs):
        if seen is not None:
            seen.append({"cmd": cmd, "input_text": input_text})
        ids = [item["id"] for item in _items(input_text)]
        return 0, _answers({audit_id: (verdict, defect_class) for audit_id in ids}), ""
    return runner


def test_fanout_folds_the_preamble_into_each_cursor_job(setup):
    _prepare(setup, rows_per_job=2)
    run_dir = setup[2]
    seen = []
    out = panel.fanout(run_dir, model="model-a", runner=_answering_runner(seen), concurrency=1)
    assert out["counts"] == {"wrote": 2, "failed": 0, "skipped": 0, "todo": 2}
    assert len(seen) == 2
    assert all(s["input_text"].startswith("# Reader-edit audit") for s in seen)
    assert "--system-prompt-file" not in " ".join(seen[0]["cmd"])
    draft = (run_dir / "drafts" / "model-a" / "job-001.json").read_text(encoding="utf-8")
    assert [v["id"] for v in json.loads(draft)] == ["a1", "a2"]


def test_fanout_resumes_past_written_drafts(setup):
    _prepare(setup, rows_per_job=2)
    panel.fanout(setup[2], model="model-a", runner=_answering_runner(), concurrency=1)
    seen = []
    out = panel.fanout(setup[2], model="model-a", runner=_answering_runner(seen), concurrency=1)
    assert out["skipped"] == ["job-001", "job-002"]
    assert seen == []


def test_fanout_refuses_a_model_off_the_panel(setup):
    _prepare(setup)
    out = panel.fanout(setup[2], model="someone-else", runner=_answering_runner())
    assert "not on this run's panel" in out["error"]


def test_fanout_refuses_unknown_job_ids(setup):
    _prepare(setup)
    out = panel.fanout(setup[2], model="model-a", job_ids=["job-999"], runner=_answering_runner())
    assert "job ids not in the manifest" in out["error"]


def test_prepare_fanout_commit_through_the_cli(setup, capsys):
    import scripts.panel_audit as cli

    root, input_path, run_dir = setup
    ids_file = input_path.with_name("ids.txt")
    ids_file.write_text("# rows the prompt was tuned on\na3\n", encoding="utf-8")
    models = [arg for model in MODELS for arg in ("--model", model)]
    assert cli.main([
        "prepare", "--input", str(input_path), "--run", str(run_dir),
        "--projects-root", str(root), "--exclude-ids-file", str(ids_file), *models,
    ]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == 2

    for model in MODELS:
        panel.fanout(run_dir, model=model, runner=_answering_runner(), concurrency=1)
    assert cli.main(["commit", "--run", str(run_dir)]) == 0
    committed = json.loads(capsys.readouterr().out)
    assert committed["buckets"]["silver"] == 2
    assert committed["missing"] == []


def test_cli_fanout_without_a_run_fails(tmp_path, capsys):
    import scripts.panel_audit as cli

    assert cli.main(["fanout", "--run", str(tmp_path / "nope")]) == 1
    assert "prepare" in json.loads(capsys.readouterr().out)["error"]


# --- successive saves ---------------------------------------------------------------

def _save(audit_id, before, after, timestamp, chunk="ch01_chunk_000"):
    return {**_row(audit_id, before, after, "When you squeeze a sponge."), "chunk_id": chunk, "timestamp": timestamp}


def test_successive_saves_on_a_sentence_are_one_net_edit():
    rows = [
        _save("c2", "Cuando aprietan una esponja, haces que salga agua.",
              "Cuando aprietan una esponja, hacen que salga agua.", "2026-07-02T10:00:00"),
        _save("c1", "Cuando aprietas una esponja, haces que salga agua.",
              "Cuando aprietan una esponja, haces que salga agua.", "2026-07-01T10:00:00"),
        _save("x1", "Otra frase distinta del capítulo.", "Otra frase diferente del capítulo.", "2026-07-01T11:00:00"),
    ]
    chains = panel.collapse_saves(rows)
    assert sorted([row["audit_id"] for row in chain] for chain in chains) == [["c1", "c2"], ["x1"]]
    net = panel.net_edit(next(chain for chain in chains if len(chain) == 2))
    assert net["audit_id"] == "c2"
    assert net["es_before"] == "Cuando aprietas una esponja, haces que salga agua."
    assert net["es_after"] == "Cuando aprietan una esponja, hacen que salga agua."
    assert net["chain"] == ["c1", "c2"]


def test_a_save_in_another_chunk_does_not_continue_the_chain():
    rows = [
        _save("c1", "Frase uno del capítulo aquí.", "Frase dos del capítulo aquí.", "2026-07-01"),
        _save("c2", "Frase dos del capítulo aquí.", "Frase tres del capítulo aquí.", "2026-07-02", chunk="ch02_chunk_000"),
    ]
    assert len(panel.collapse_saves(rows)) == 2


def test_a_revert_is_one_chain_not_a_loop():
    rows = [
        _save("r2", "Texto B de la frase editada.", "Texto A de la frase original.", "2026-07-02"),
        _save("r1", "Texto A de la frase original.", "Texto B de la frase editada.", "2026-07-01"),
    ]
    assert [[row["audit_id"] for row in chain] for chain in panel.collapse_saves(rows)] == [["r1", "r2"]]


def _chained_input(tmp_path):
    before, after = ROWS[1]["es_before"], ROWS[1]["es_after"]
    middle = "Todos escucharon en silencio durante mucho tiempo."
    return _input(tmp_path / "input.jsonl", [
        dict(ROWS[0], timestamp="2026-07-01T09:00:00"),
        dict(ROWS[1], audit_id="b1", es_before=before, es_after=middle, timestamp="2026-07-01T10:00:00"),
        dict(ROWS[1], audit_id="b2", es_before=middle, es_after=after, timestamp="2026-07-02T10:00:00"),
        dict(ROWS[2], audit_id="r1", timestamp="2026-07-01T10:00:00"),
        dict(ROWS[2], audit_id="r2", es_before=ROWS[2]["es_after"], es_after=ROWS[2]["es_before"],
             timestamp="2026-07-02T10:00:00"),
    ])


def test_prepare_audits_the_net_edit_and_drops_a_revert(tmp_path):
    root = tmp_path / "projects"
    _book(root, "book")
    out = panel.prepare(_chained_input(tmp_path), tmp_path / "run", models=MODELS, projects_root=root)
    assert out["rows"] == 2
    assert out["collapsed"] == {"chains": 2, "saves_merged": 2, "reverted": 1}
    rows = _manifest(tmp_path / "run")["rows"]
    assert list(rows) == ["a1", "b2"]
    assert rows["b2"]["es_before"] == ROWS[1]["es_before"]
    assert rows["b2"]["es_after"] == ROWS[1]["es_after"]
    assert rows["b2"]["chain"] == ["b1", "b2"]
    assert rows["a1"]["chain"] == ["a1"]


def test_excluding_any_save_excludes_its_net_edit(tmp_path):
    root = tmp_path / "projects"
    _book(root, "book")
    out = panel.prepare(
        _chained_input(tmp_path), tmp_path / "run", models=MODELS, projects_root=root, exclude_ids=["b1"],
    )
    assert out["rows"] == 1
    assert list(_manifest(tmp_path / "run")["rows"]) == ["a1"]
