from __future__ import annotations

import contextlib

from src.book_review import judging


def test_run_adjudicates_pass_2_when_editorial_itself_is_current(tmp_path, monkeypatch):
    from src.harness import locks
    from src.judges import sweep

    plan = {"judges": ["dialogue", "editorial"], "address_map": False,
            "owed": {"dialogue": ["chapter_01"]}, "owed_chunks": {"dialogue": 2},
            "pending_pass2_chunks": 3, "nothing_to_do": False}
    monkeypatch.setattr(judging, "plan", lambda project_dir, judges=None: plan)
    monkeypatch.setattr(judging, "_profile", lambda *a: type("P", (), {"cli": "claude"})())
    monkeypatch.setattr(judging, "_gate", lambda *a: None)
    monkeypatch.setattr(judging, "_context", lambda project_dir, names: ({}, None))
    monkeypatch.setattr(locks, "project_lock", lambda *a, **k: contextlib.nullcontext())
    swept, adjudicated = [], []

    def fake_sweep(project_dir, names, scopes, emit, **kwargs):
        swept.append(names)
        return {"ran": 2, "error_count": 0, "errors": []}

    def fake_adjudicate(project_dir, scopes, emit, **kwargs):
        adjudicated.append(scopes)
        return {"adjudicated": 3}, ["one failed"]

    monkeypatch.setattr(sweep, "run_headless_sweep", fake_sweep)
    monkeypatch.setattr(sweep, "adjudicate_editorial", fake_adjudicate)

    out = judging.run(tmp_path, emit=lambda *a, **k: None)
    assert out["status"] == "ok" and swept == [["dialogue"]] and adjudicated == [["book"]]
    assert out["summary"]["ran"] == 2 and out["summary"]["adjudication"] == {"adjudicated": 3}
    assert out["summary"]["error_count"] == 1


def _stub_run(monkeypatch, plan):
    from src.harness import locks

    monkeypatch.setattr(judging, "plan", lambda project_dir, judges=None: plan)
    monkeypatch.setattr(judging, "_profile", lambda *a: type("P", (), {"cli": "claude"})())
    monkeypatch.setattr(judging, "_gate", lambda *a: None)
    monkeypatch.setattr(judging, "_context", lambda project_dir, names: ({}, None))
    monkeypatch.setattr(locks, "project_lock", lambda *a, **k: contextlib.nullcontext())


def test_run_passes_concurrency_to_both_waves(tmp_path, monkeypatch):
    from src.judges import editorial_wave, subagent

    _stub_run(monkeypatch, {"judges": ["editorial"], "address_map": False,
                            "owed": {"editorial": ["chapter_01"]},
                            "owed_chunks": {"editorial": 1}, "pending_pass2_chunks": 0,
                            "nothing_to_do": False})
    widths = {}
    monkeypatch.setattr(subagent, "prepare", lambda *a, **k: {"manifest": [{}]})
    monkeypatch.setattr(subagent, "commit", lambda *a, **k: {"counts": {"committed": 1}})
    monkeypatch.setattr(editorial_wave, "prepare",
                        lambda *a, **k: {"counts": {"chunks": 1}})
    monkeypatch.setattr(editorial_wave, "commit", lambda *a, **k: {"committed": 1})

    def fake_pass1(project_dir, **kwargs):
        widths["pass1"] = kwargs.get("concurrency")
        return {"wrote": ["x"]}

    def fake_pass2(project_dir, **kwargs):
        widths["pass2"] = kwargs.get("concurrency")
        return {}

    monkeypatch.setattr(subagent, "fanout", fake_pass1)
    monkeypatch.setattr(editorial_wave, "fanout", fake_pass2)

    out = judging.run(tmp_path, emit=lambda *a, **k: None, concurrency=10)
    assert out["status"] == "ok" and widths == {"pass1": 10, "pass2": 10}

    judging.run(tmp_path, emit=lambda *a, **k: None)
    assert widths == {"pass1": None, "pass2": None}  # each manifest's batch_size (5)


def test_run_refuses_a_width_below_one_before_taking_the_lock(tmp_path, monkeypatch):
    from src.harness import locks

    def no_lock(*a, **k):
        raise AssertionError("took the lock")

    monkeypatch.setattr(judging, "plan", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("planned")))
    monkeypatch.setattr(locks, "project_lock", no_lock)
    out = judging.run(tmp_path, emit=lambda *a, **k: None, concurrency=0)
    assert out["status"] == "error" and "at least 1" in out["error"]


def test_cli_judge_accepts_concurrency():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "book_review.py"
    spec = importlib.util.spec_from_file_location("book_review_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    parser = cli.build_parser()
    assert parser.parse_args(["judge", "--project", "x", "--concurrency", "10"]).concurrency == 10
    assert parser.parse_args(["run", "--project", "x", "--concurrency", "8"]).concurrency == 8
