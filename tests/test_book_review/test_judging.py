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
