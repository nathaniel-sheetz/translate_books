"""The translate-harness compaction hint (src/harness/compact_hint.py) and its hook."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.harness import compact_hint as ch

REPO = Path(__file__).resolve().parents[1]


def _assistant(ctx: int, command: str | None = None, mid: str = "m") -> dict:
    content = []
    if command:
        content.append({"type": "tool_use", "id": f"t-{mid}", "name": "Bash",
                        "input": {"command": command}})
    return {"type": "assistant", "timestamp": "2026-11-24T00:00:00Z",
            "message": {"id": mid, "content": content,
                        "usage": {"input_tokens": 10, "cache_read_input_tokens": ctx - 10,
                                  "cache_creation_input_tokens": 0}}}


def _transcript(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _status(stage: str, *, review: dict | None = None, **artifacts) -> dict:
    arts = {"source": True, "style_guide": True, "glossary": True, "address_map": False}
    arts.update(artifacts)
    st = {"stage": stage, "artifacts": arts}
    if review is not None:
        st["book_review"] = review
    return st


# ── boundary ────────────────────────────────────────────────────────────────

def test_fresh_project_after_setup_is_before_drafting():
    st = _status("pre-chunk", style_guide=False, glossary=False)
    assert ch.boundary(st, "setup") == "before_drafting"


def test_drafting_in_progress_is_not_a_boundary():
    st = _status("pre-chunk", style_guide=False, glossary=False)
    assert ch.boundary(st, "address-map") is None
    # a style guide on disk means the drafting has started
    assert ch.boundary(_status("pre-chunk", glossary=False), "setup") is None
    # so does an address map
    st = _status("pre-chunk", style_guide=False, glossary=False, address_map=True)
    assert ch.boundary(st, "status") is None


def test_chunked_and_untranslated_is_the_translate_gate():
    assert ch.boundary(_status("untranslated"), "chunk") == "translate_gate"


def test_canary_fires_only_when_moving_on_to_the_rest():
    reviewed = {"due": None, "last_review": {"kind": "signals"}}
    assert ch.boundary(_status("partial", review=reviewed), "translate-prepare") == "canary_reviewed"
    # signals records the review before the fixes are made
    assert ch.boundary(_status("partial", review=reviewed), "signals") is None
    assert ch.boundary(_status("partial", review=reviewed), "sentence-edit") is None
    # canary still due
    due = {"due": "canary: ...", "last_review": None}
    assert ch.boundary(_status("partial", review=due), "translate-prepare") is None


def test_translated_book_with_review_due():
    due = {"due": "the book is translated and has never been reviewed", "last_review": None}
    assert ch.boundary(_status("fully-translated", review=due), "translate-commit") == "before_book_review"
    done = {"due": None, "last_review": {"kind": "signals"}}
    assert ch.boundary(_status("fully-translated", review=done), "epub") is None


def test_message_carries_the_context_and_a_paste_line():
    msg = ch.message("translate_gate", project="my-book", ctx=353_400)
    assert "353k" in msg
    assert "\n" not in msg  # Claude Code labels every line "Stop says:"
    assert "/compact Translating my-book with translate-harness, at the translate gate" in msg
    assert "run status" in msg
    for name in ch._BOUNDARIES:
        assert "\n" not in ch.message(name, project="my-book", ctx=200_000)


# ── the transcript's tail ───────────────────────────────────────────────────

def test_last_context_and_command(tmp_path):
    p = _transcript(tmp_path, [
        _assistant(90_000, "python scripts/harness.py setup --project projects/a", "1"),
        _assistant(200_000, "python scripts/harness.py chunk --project projects/b --size 600", "2"),
        _assistant(210_000, "ls", "3"),
    ])
    rows = ch._tail_entries(p)
    assert ch.last_context(rows) == 210_000
    assert ch.last_harness_command(rows) == ("chunk", "projects/b")


def test_book_review_commands_count(tmp_path):
    p = _transcript(tmp_path, [_assistant(
        200_000, 'python -X utf8 scripts/book_review.py judge --project "projects/c" --yes', "1")])
    assert ch.last_harness_command(ch._tail_entries(p)) == ("judge", "projects/c")


def test_a_compaction_after_the_last_turn_means_no_context(tmp_path):
    p = _transcript(tmp_path, [
        _assistant(300_000, "python scripts/harness.py chunk --project x", "1"),
        {"type": "system", "subtype": "compact_boundary"},
    ])
    assert ch.last_context(ch._tail_entries(p)) == 0


def test_tail_drops_the_cut_first_line(tmp_path):
    rows = [_assistant(100_000 + i, None, str(i)) for i in range(50)]
    p = _transcript(tmp_path, rows)
    tail = ch._tail_entries(p, tail_bytes=600)
    assert tail and all(r["type"] == "assistant" for r in tail)
    assert ch.last_context(tail) == 100_049


# ── on_stop ─────────────────────────────────────────────────────────────────

@pytest.fixture
def book(tmp_path, monkeypatch):
    project_dir = tmp_path / "book"
    (project_dir / ".harness").mkdir(parents=True)
    logged: list[tuple[str, dict]] = []
    status = {"value": _status("untranslated")}
    monkeypatch.setattr(ch, "_resolve", lambda project: (project_dir, status["value"]))
    monkeypatch.setattr(ch, "_log", lambda pd, event, **f: logged.append((event, f)))
    monkeypatch.delenv(ch.MIN_TOKENS_ENV, raising=False)
    return {"dir": project_dir, "logged": logged, "status": status, "tmp": tmp_path}


def _stop(book, ctx: int, verb: str = "chunk") -> str | None:
    p = _transcript(book["tmp"], [_assistant(
        ctx, f"python scripts/harness.py {verb} --project {book['dir']}", "1")])
    return ch.on_stop({"transcript_path": str(p), "session_id": "s1"})


def test_on_stop_fires_once_at_a_boundary(book):
    msg = _stop(book, 353_000)
    assert msg and "353k" in msg
    assert book["logged"] == [("compact_hint", {"boundary": "translate_gate", "ctx": 353_000,
                                                "session_id": "s1", "last_command": "chunk"})]
    assert "translate_gate" in ch.shown(book["dir"])
    assert _stop(book, 360_000) is None  # once per book
    assert len(book["logged"]) == 1


def test_on_stop_is_silent_below_the_threshold(book, monkeypatch):
    assert _stop(book, 120_000) is None
    assert not book["logged"]
    monkeypatch.setenv(ch.MIN_TOKENS_ENV, "100000")
    assert _stop(book, 120_000) is not None


def test_on_stop_is_silent_off_a_boundary(book):
    book["status"]["value"] = _status("partial", review={"due": None, "last_review": {}})
    assert _stop(book, 300_000, verb="sentence-edit") is None
    assert not ch.shown(book["dir"])


def test_on_stop_needs_a_harness_command(book):
    p = _transcript(book["tmp"], [_assistant(300_000, "git status", "1")])
    assert ch.on_stop({"transcript_path": str(p)}) is None


def test_on_precompact_logs_the_compaction(book):
    p = _transcript(book["tmp"], [_assistant(
        246_000, f"python scripts/harness.py chunk --project {book['dir']}", "1")])
    ch.on_precompact({"transcript_path": str(p), "session_id": "s1", "trigger": "auto"})
    event, fields = book["logged"][0]
    assert event == "compaction"
    assert fields["trigger"] == "auto" and fields["ctx"] == 246_000
    assert fields["stage"] == "untranslated" and fields["boundary"] == "translate_gate"


# ── the hook script ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("stdin", ["", "not json", '{"transcript_path": "/no/such/file"}'])
@pytest.mark.parametrize("mode", ["stop", "precompact"])
def test_hook_never_fails_and_prints_nothing(stdin, mode):
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "hooks" / "compact_hint.py"), mode],
                       input=stdin, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0
    assert r.stdout == ""
