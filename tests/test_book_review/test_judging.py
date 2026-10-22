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
    assert widths == {"pass1": 10, "pass2": 10}  # book-review's default, not the manifests' 5


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


def test_run_resumes_from_drafts_on_both_passes(tmp_path, monkeypatch):
    from src.judges import sweep

    _stub_run(monkeypatch, {"judges": ["dialogue"], "address_map": False,
                            "owed": {"dialogue": ["chapter_01"]},
                            "owed_chunks": {"dialogue": 1}, "pending_pass2_chunks": 2,
                            "nothing_to_do": False})
    seen = {}

    def fake_sweep(project_dir, names, scopes, emit, **kwargs):
        seen["pass1"] = kwargs.get("keep_drafts")
        return {"ran": 1, "error_count": 0, "errors": []}

    def fake_adjudicate(project_dir, scopes, emit, **kwargs):
        seen["pass2"] = kwargs.get("keep_drafts")
        return {}, []

    monkeypatch.setattr(sweep, "run_headless_sweep", fake_sweep)
    monkeypatch.setattr(sweep, "adjudicate_editorial", fake_adjudicate)
    judging.run(tmp_path, emit=lambda *a, **k: None)
    assert seen == {"pass1": "if_unchanged", "pass2": "if_unchanged"}


def _seed_walls(project_dir, walls, *, cli="cursor", model="grok-4.7-medium"):
    from src.harness import usage
    from src.judges.subagent import usage_log_path

    log = usage_log_path(project_dir)
    log.parent.mkdir(parents=True, exist_ok=True)
    for wall in walls:
        usage.append_usage(log, {"rc": 0, "cli": cli, "model": model, "wall_s": wall})


def test_project_wall_quotes_width_and_the_background_cap(tmp_path):
    prof = type("P", (), {"cli": "cursor", "worker_model": "grok-4.7-medium"})()
    assert judging._project_wall(tmp_path, prof, pass1_jobs=10, pass2_jobs=0, width=10,
                                 per_judge={}) is None

    _seed_walls(tmp_path, [480.0] * 8 + [900.0] * 2)
    # Pollyanna's shape: 111 pass-1 jobs at ~8 min. At 5 wide it misses the cap.
    narrow = judging._project_wall(tmp_path, prof, pass1_jobs=111, pass2_jobs=35, width=5,
                                   per_judge={"editorial": 37})
    assert narrow["pass1_s"] == round(111 * 480 / 5 + 900)
    assert narrow["pass2_max_s"] == round(35 * 480 / 5 + 900)
    assert narrow["per_judge_s"] == {"editorial": round(37 * 480 / 5 + 900)}
    assert narrow["fits_background_cap"] is False and narrow["background_cap_s"] == 7200

    wide = judging._project_wall(tmp_path, prof, pass1_jobs=40, pass2_jobs=10, width=10,
                                 per_judge={})
    assert wide["total_s"] == round(40 * 48 + 900) + round(10 * 48 + 900)
    assert wide["fits_background_cap"] is True


def test_estimate_reports_the_default_width_and_its_source(tmp_path, monkeypatch):
    monkeypatch.setattr(judging, "plan", lambda project_dir, judges=None: {
        "judges": ["dialogue"], "address_map": False, "owed": {}, "owed_chunks": {},
        "pending_pass2_chunks": 0, "nothing_to_do": True})
    monkeypatch.setattr(judging, "_profile", lambda *a: type(
        "P", (), {"to_payload": lambda self: {}})())
    out = judging.estimate(tmp_path)
    assert out["concurrency"] == 10 and out["concurrency_source"] == "default"
    out = judging.estimate(tmp_path, concurrency=4)
    assert out["concurrency"] == 4 and out["concurrency_source"] == "--concurrency"


def test_estimate_and_run_return_json_when_the_lock_is_busy(tmp_path, monkeypatch):
    from src.harness import locks

    plan = {"judges": ["dialogue"], "address_map": False,
            "owed": {"dialogue": ["chapter_01"]}, "owed_chunks": {"dialogue": 1},
            "pending_pass2_chunks": 0, "nothing_to_do": False}
    monkeypatch.setattr(judging, "plan", lambda project_dir, judges=None: plan)
    monkeypatch.setattr(judging, "_profile", lambda *a: type(
        "P", (), {"to_payload": lambda self: {}, "cli": "claude", "worker_model": "x"})())
    monkeypatch.setattr(judging, "_gate", lambda *a: None)

    class Busy:
        def __enter__(self):
            raise locks.LockBusy(tmp_path / ".lock", {"kind": "book-review-judges", "pid": 9})

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(locks, "project_lock", lambda *a, **k: Busy())
    estimated = judging.estimate(tmp_path)
    assert estimated["status"] == "error" and "book-review-judges" in estimated["error"]
    ran = judging.run(tmp_path)
    assert ran["status"] == "error" and "book-review-judges" in ran["error"] and ran["plan"] == plan
