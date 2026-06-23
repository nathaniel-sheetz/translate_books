"""Tests for the harness run log (timeline + beats) and its run_id plumbing.

Covers four seams:
  * src/utils/run_logger.py  — the append-only JSONL sink (best-effort).
  * src/harness/state.py     — new_run_id / ensure_run_id (mint + persist).
  * src/harness/flow.py      — setup stamps a run_id; log_event records a beat.
  * scripts/harness.py       — _log_command emits one command event per run.

The autouse `_isolate_run_log` fixture in conftest redirects writes to a
per-test tmp file, so these never touch the real logs/harness_runs.jsonl.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.harness import flow, state
from src.utils import run_logger


def _read_events() -> list[dict]:
    """Parse the (isolated) run log into a list of event dicts."""
    path = run_logger._RUNS_PATH
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _make_project(tmp_path: Path, name: str = "fixturebook") -> Path:
    """A minimal project dir that resolve_project_dir accepts (has source.txt)."""
    proj = tmp_path / name
    proj.mkdir()
    (proj / "source.txt").write_text("content", encoding="utf-8")
    return proj


# ── run_logger sink ─────────────────────────────────────────────────────────

class TestLogRunEvent:
    def test_appends_one_line_with_core_fields(self):
        run_logger.log_run_event(run_id="book_20260622_1130", project="book",
                                  event="command", cmd="glossary commit", status="ok")
        events = _read_events()
        assert len(events) == 1
        e = events[0]
        assert e["run_id"] == "book_20260622_1130"
        assert e["project"] == "book"
        assert e["event"] == "command"
        assert e["cmd"] == "glossary commit"
        assert e["status"] == "ok"
        assert "ts" in e  # timestamp always stamped

    def test_appends_not_overwrites(self):
        run_logger.log_run_event(run_id="r", project="p", event="command", cmd="setup")
        run_logger.log_run_event(run_id="r", project="p", event="approval", beat="glossary")
        events = _read_events()
        assert [e["event"] for e in events] == ["command", "approval"]

    def test_extra_fields_passthrough(self):
        run_logger.log_run_event(run_id="r", project="p", event="command",
                                 counts={"committed": 6, "failed": 0})
        assert _read_events()[0]["counts"] == {"committed": 6, "failed": 0}

    def test_best_effort_never_raises(self, tmp_path, monkeypatch):
        # Point the log at a path whose parent is a *file*, so mkdir fails.
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setattr(run_logger, "_RUNS_PATH", blocker / "runs.jsonl")
        # Must not raise, and must not have written anything.
        run_logger.log_run_event(run_id="r", project="p", event="command")
        assert not (blocker / "runs.jsonl").exists()


# ── run id minting ──────────────────────────────────────────────────────────

class TestRunId:
    def test_new_run_id_uses_slug_prefix(self, tmp_path):
        proj = _make_project(tmp_path, "kittens-and-cats")
        rid = state.new_run_id(proj)
        assert rid.startswith("kittens-and-cats_")

    def test_ensure_run_id_mints_and_persists(self, tmp_path):
        proj = _make_project(tmp_path)
        rid = state.ensure_run_id(proj)
        assert rid
        assert state.load_config(proj)["run_id"] == rid

    def test_ensure_run_id_is_stable(self, tmp_path):
        proj = _make_project(tmp_path)
        first = state.ensure_run_id(proj)
        second = state.ensure_run_id(proj)
        assert first == second

    def test_run_id_survives_save_config(self, tmp_path):
        """run_id is not a DEFAULTS key, but save_config must still preserve it."""
        proj = _make_project(tmp_path)
        cfg = state.load_config(proj)
        cfg["run_id"] = "manual_id"
        state.save_config(proj, cfg)
        assert state.load_config(proj)["run_id"] == "manual_id"


# ── flow.setup stamps a run_id ──────────────────────────────────────────────

def _book_source() -> str:
    body = ("Old Thomas walked through the quiet village past the old well and the "
            "great oak tree. ") * 20
    return f"CHAPTER I\n\n{body.strip()}\n\nCHAPTER II\n\n{body.strip()}\n"


def test_setup_stamps_run_id(tmp_path):
    proj = tmp_path / "newbook"
    proj.mkdir()
    (proj / "source.txt").write_text(_book_source(), encoding="utf-8")

    flow.setup(str(proj), url="", target_language="Spanish", locale="mx")

    rid = state.load_config(proj).get("run_id")
    assert rid and rid.startswith("newbook_")


# ── flow.log_event ──────────────────────────────────────────────────────────

class TestLogEventFlow:
    def test_records_beat_with_run_id(self, tmp_path):
        proj = _make_project(tmp_path, "mybook")
        result = flow.log_event(str(proj), event="approval",
                                data='{"beat":"glossary","decision":"approved_first_pass"}')
        assert result["logged"] is True
        assert result["event"] == "approval"
        events = _read_events()
        assert len(events) == 1
        e = events[0]
        assert e["event"] == "approval"
        assert e["beat"] == "glossary"
        assert e["decision"] == "approved_first_pass"
        assert e["project"] == "mybook"
        assert e["run_id"] == result["run_id"]

    def test_no_data_is_fine(self, tmp_path):
        proj = _make_project(tmp_path)
        flow.log_event(str(proj), event="note", data=None)
        assert _read_events()[0]["event"] == "note"

    def test_bad_json_raises_valueerror(self, tmp_path):
        proj = _make_project(tmp_path)
        with pytest.raises(ValueError):
            flow.log_event(str(proj), event="approval", data="{not json}")

    def test_non_object_data_raises(self, tmp_path):
        proj = _make_project(tmp_path)
        with pytest.raises(ValueError):
            flow.log_event(str(proj), event="approval", data='["a","b"]')


# ── scripts/harness.py:_log_command ─────────────────────────────────────────

class TestLogCommand:
    def _import_cli(self):
        import scripts.harness as harness_cli
        return harness_cli

    def test_command_event_with_whitelist_fields(self, tmp_path):
        cli = self._import_cli()
        proj = _make_project(tmp_path)
        args = SimpleNamespace(command="glossary", action="commit", project=str(proj))
        cli._log_command(args, status="ok", duration=2.1234,
                         result={"term_count": 48, "glossary_path": "x", "terms": [1, 2, 3]})
        e = _read_events()[0]
        assert e["event"] == "command"
        assert e["cmd"] == "glossary commit"
        assert e["status"] == "ok"
        assert e["dur_s"] == 2.123
        assert e["term_count"] == 48        # whitelisted summary key
        assert "terms" not in e             # bulk payload excluded
        assert "glossary_path" not in e
        assert e["run_id"].startswith(proj.name + "_")

    def test_command_label_without_action(self, tmp_path):
        cli = self._import_cli()
        proj = _make_project(tmp_path)
        args = SimpleNamespace(command="difficulty", action=None, project=str(proj))
        cli._log_command(args, status="ok", duration=0.5,
                         result={"book_difficulty": 0.42, "suggested_target_size": 1500})
        e = _read_events()[0]
        assert e["cmd"] == "difficulty"
        assert e["book_difficulty"] == 0.42

    def test_log_event_command_is_not_double_logged(self, tmp_path):
        cli = self._import_cli()
        proj = _make_project(tmp_path)
        args = SimpleNamespace(command="log-event", action=None, project=str(proj))
        cli._log_command(args, status="ok", duration=0.1, result={"logged": True})
        assert _read_events() == []

    def test_error_status_logged_without_result(self, tmp_path):
        cli = self._import_cli()
        proj = _make_project(tmp_path)
        args = SimpleNamespace(command="glossary", action="commit", project=str(proj))
        cli._log_command(args, status="validation_error", duration=0.3)
        e = _read_events()[0]
        assert e["status"] == "validation_error"
        assert e["cmd"] == "glossary commit"
