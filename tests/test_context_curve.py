"""scripts/context_curve.py: beats, phases and compactions from a transcript."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import context_curve as cc  # noqa: E402


def _turn(mid: str, ts: str, ctx: int, command: str | None = None) -> dict:
    content = ([{"type": "tool_use", "id": f"t{mid}", "name": "Bash",
                 "input": {"command": command}}] if command else [])
    return {"type": "assistant", "timestamp": ts,
            "message": {"id": mid, "content": content,
                        "usage": {"input_tokens": 0, "cache_read_input_tokens": ctx,
                                  "cache_creation_input_tokens": 0}}}


def _write(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


H = "python scripts/harness.py"


def test_beats_phases_and_compaction(tmp_path):
    rows = [
        _turn("1", "2026-11-24T00:00:00Z", 80_000, f"{H} setup --project p"),
        _turn("2", "2026-11-24T00:01:00Z", 90_000, f"{H} glossary prepare --project p"),
        _turn("3", "2026-11-24T00:02:00Z", 180_000),
        _turn("4", "2026-11-24T00:03:00Z", 190_000, f"{H} log-event --project p --event x"),
        _turn("5", "2026-11-24T00:04:00Z", 200_000, f"{H} chunk --project p"),
        {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-11-24T00:05:00Z",
         "compactMetadata": {"trigger": "manual", "preTokens": 205_000, "postTokens": 20_000,
                             "durationMs": 60_000}},
        # more than the cache TTL later
        _turn("6", "2026-11-24T02:00:00Z", 78_000, f"{H} translate-prepare --project p"),
        _turn("7", "2026-11-24T02:01:00Z", 85_000),
    ]
    r = cc.session_report(cc.parse(_write(tmp_path, rows)))

    assert [b["label"] for b in r["beats"]] == [
        "setup", "glossary prepare", "log-event", "chunk", "translate-prepare"]
    glossary = r["beats"][1]
    assert glossary["entry"] == 90_000 and glossary["delta"] == 90_000

    phases = [(p["phase"], p["exit"]) for p in r["phases"]]
    # log-event is neutral: it folds into the glossary phase
    assert phases == [("setup", 80_000), ("glossary", 190_000), ("chunk", 200_000),
                      ("translate", 85_000)]

    (c,) = r["compactions"]
    assert c["trigger"] == "manual" and c["pre"] == 205_000 and c["floor"] == 78_000
    assert c["last_beat_before"] == "chunk" and c["first_beat_after"] == "translate-prepare"
    assert c["turns_after"] == 2 and c["saved_ctx_tokens"] == (205_000 - 78_000) * 2
    assert r["idle_gaps"] and r["idle_gaps"][0]["idle_min"] == 116  # from the last turn at 00:04


def test_canary_review_is_split_from_the_final_review():
    s = cc.Session(Path("x"))
    s.turns = [cc.Turn(None, 100_000 + i * 1000, 0) for i in range(4)]
    s.beats = [cc.Beat("translate-commit", 0, 0), cc.Beat("book_review:judge", 1, 1),
               cc.Beat("translate-prepare", 2, 2), cc.Beat("book_review:signals", 3, 3)]
    assert [p["phase"] for p in cc._phase_spans(s)] == [
        "translate", "canary-review", "translate", "review"]


def test_rows_that_are_not_objects_are_skipped(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join([
        json.dumps(_turn("1", "2026-11-24T00:00:00Z", 80_000, f"{H} setup --project p")),
        "[1]", "null", "{broken", json.dumps({"type": "assistant", "message": "a string"}),
        json.dumps(_turn("2", "2026-11-24T00:01:00Z", 95_000, f"{H} chunk --project p")),
    ]), encoding="utf-8")
    r = cc.session_report(cc.parse(p))
    assert [b["label"] for b in r["beats"]] == ["setup", "chunk"]


def test_paths_and_imports_are_not_beats():
    assert cc._beat_label("cat scripts/harness.py src/x") is None
    assert cc._beat_label("python scripts/harness.py style-guide commit --project p") == "style-guide commit"
    assert cc._beat_label("python scripts/book_review.py judge --yes") == "book_review:judge"
