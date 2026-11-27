"""Tell the operator when a translate-harness session is at a good point to compact.

The agent cannot run ``/compact`` and cannot see its own context size, so this
runs as a Claude Code hook instead. At the end of each turn (a ``Stop`` hook),
it reads the last turn's context from the transcript's tail. If the context is
past a threshold and the book sits at a phase boundary, it shows the user one
line with a ready-to-paste ``/compact``. Everything the next phase needs is on
disk at those boundaries, and resuming is ``status`` + ``last_output.json``.

The boundaries come from ``scripts/context_curve.py`` over the past harness
sessions (2026-11-25, 15 book sessions):

- the pre-translate beats leave every session over 150k by the glossary's end
  (median 206k), and the drafting often loops back to the style guide and the
  address map after the glossary, so the cut goes at the translate gate, once
  chunks exist (median 185k with about 60 turns still to come);
- the canary review adds about 50k, and the rest of the book follows it;
- the full review is the largest phase (median +63k, p90 +134k), and two of the
  auto compactions landed inside it, so the cut goes before it;
- a session that is already large when ``setup`` runs (one began at 192k).

The floor after a compaction is about 76k, so a hint below 150k is not worth
the minute it takes. Each boundary fires once per book
(``.harness/compact_hints.json``). A ``PreCompact`` hook logs every compaction
to the run log, so ``harness.py runs`` shows whether a hint was taken.

Everything here is best-effort: a hook that raises or prints garbage would get
in the way of every turn, so failures return ``None`` and print nothing.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

DEFAULT_MIN_TOKENS = 150_000
MIN_TOKENS_ENV = "COMPACT_HINT_MIN_TOKENS"
TAIL_BYTES = 1_500_000
HINTS_FILE = "compact_hints.json"

# `python scripts/harness.py <verb> ... --project <slug>`, also book_review.py.
_CMD_RE = re.compile(r"(harness|book_review)\.py[\"']?\s+([a-z][\w-]*)")
_PROJECT_RE = re.compile(r"--project[= ]+[\"']?([^\s\"']+)")

# Commands that leave a fresh project where it was: before the drafting beats.
_SETUP_COMMANDS = {"setup", "split", "split-preview", "group", "precedent", "status",
                   "config-set", "config-get", "log-event"}

# Commands that end a translate wave: the book review has not started yet.
_END_OF_TRANSLATION_COMMANDS = {"translate", "translate-commit", "status"}

# Claude Code puts "Stop says:" before every line of a hook's message, so the
# message is one line: why now, then the /compact to paste, last so it is easy
# to select. `reason` finishes "Good time to compact (197k in context): ...".
_BOUNDARIES: dict[str, dict[str, str]] = {
    "before_drafting": {
        "reason": "the address map, style guide and glossary will add about 100k, "
                  "so start them light",
        "resume": "about to draft the address map, style guide and glossary",
    },
    "translate_gate": {
        "reason": "the style guide, glossary and chunks are saved, so the drafting "
                  "talk can go",
        "resume": "at the translate gate; picking the backend is next",
    },
    "canary_reviewed": {
        "reason": "the canary review and its fixes are saved",
        "resume": "past the canary; translating the rest of the book is next",
    },
    "before_book_review": {
        "reason": "the book review is next and it is the biggest phase, so an automatic "
                  "compaction would land in the middle of a fix",
        "resume": "fully translated; the book review is next",
    },
}


def min_tokens() -> int:
    try:
        return int(os.environ.get(MIN_TOKENS_ENV, DEFAULT_MIN_TOKENS))
    except ValueError:
        return DEFAULT_MIN_TOKENS


# ── the transcript's tail ────────────────────────────────────────────────────

def _tail_entries(transcript_path: str | Path, tail_bytes: int = TAIL_BYTES) -> list[dict]:
    """The JSON rows at the end of a transcript, oldest first."""
    path = Path(transcript_path)
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - tail_bytes))
        raw = f.read()
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if size > tail_bytes and lines:
        lines = lines[1:]  # the first line is cut
    rows = []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def last_context(rows: list[dict]) -> int:
    """The context the last assistant turn was sent with, or 0."""
    for e in reversed(rows):
        if e.get("type") == "system" and e.get("subtype") == "compact_boundary":
            return 0  # compacted since the last turn: nothing to hint
        if e.get("type") != "assistant":
            continue
        msg = e.get("message")
        u = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(u, dict):
            continue
        ctx = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
               + u.get("cache_creation_input_tokens", 0))
        if ctx:
            return int(ctx)
    return 0


def last_harness_command(rows: list[dict]) -> Optional[tuple[str, str]]:
    """``(verb, project)`` of the last harness / book_review command that named a
    project, or ``None``. Read from the transcript, so it is this session's book."""
    for e in reversed(rows):
        if e.get("type") != "assistant":
            continue
        msg = e.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for b in reversed(content):
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            if b.get("name") not in ("Bash", "PowerShell"):
                continue
            command = str((b.get("input") or {}).get("command", ""))
            cmds = list(_CMD_RE.finditer(command))
            proj = _PROJECT_RE.search(command)
            if cmds and proj:
                return cmds[-1].group(2), proj.group(1)
    return None


# ── the boundary ─────────────────────────────────────────────────────────────

def boundary(st: dict, last_command: Optional[str]) -> Optional[str]:
    """The compaction boundary a ``harness.py status`` payload is at, or ``None``.

    ``last_command`` is the last harness verb this session ran. It tells the
    canary's end (the agent moved on to ``translate-prepare`` for the rest of the
    book) from its middle (the review is recorded at ``signals``, before the
    fixes are made).
    """
    stage = st.get("stage")
    artifacts = st.get("artifacts") or {}
    review = st.get("book_review") or {}
    if stage == "pre-chunk":
        if (artifacts.get("source") and not artifacts.get("style_guide")
                and not artifacts.get("glossary") and not artifacts.get("address_map")
                and last_command in _SETUP_COMMANDS):
            return "before_drafting"
        return None
    if stage == "untranslated":
        return "translate_gate"
    if stage == "partial":
        if review.get("last_review") and not review.get("due") and last_command == "translate-prepare":
            return "canary_reviewed"
        return None
    # `due` stays set until the review records at signals, so a book_review verb
    # (judge, prepare, apply...) means the review is already under way.
    if (stage == "fully-translated" and review.get("due")
            and last_command in _END_OF_TRANSLATION_COMMANDS):
        return "before_book_review"
    return None


def message(name: str, *, project: str, ctx: int) -> str:
    b = _BOUNDARIES[name]
    paste = (f"/compact Translating {project} with translate-harness, {b['resume']}. "
             f"First run status and read .harness/last_output.json.")
    return f"Good time to compact ({ctx // 1000}k in context): {b['reason']}. Paste: {paste}"


# ── fire once per book ───────────────────────────────────────────────────────

def _hints_path(project_dir: Path) -> Path:
    return Path(project_dir) / ".harness" / HINTS_FILE


def shown(project_dir: Path) -> dict[str, Any]:
    try:
        doc = json.loads(_hints_path(project_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def mark_shown(project_dir: Path, name: str, *, ctx: int, session_id: Optional[str]) -> None:
    doc = shown(project_dir)
    doc[name] = {"at": datetime.now().isoformat(timespec="seconds"), "ctx": ctx,
                 "session_id": session_id}
    path = _hints_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


# ── the two hooks ────────────────────────────────────────────────────────────

def _resolve(project: str) -> tuple[Path, dict]:
    from src.harness import flow, state
    return state.resolve_project_dir(project), flow.status(project)


def on_stop(payload: dict) -> Optional[str]:
    """The ``Stop`` hook: the message to show, or ``None``."""
    transcript = payload.get("transcript_path")
    if not transcript:
        return None
    rows = _tail_entries(transcript)
    ctx = last_context(rows)
    if ctx < min_tokens():
        return None  # the common case, decided before anything heavy is imported
    found = last_harness_command(rows)
    if not found:
        return None
    verb, project = found
    from src.harness import state
    already = shown(state.resolve_project_dir(project))
    if set(_BOUNDARIES) <= set(already):
        return None  # every boundary has fired: skip the status call (about 1.3 s)
    project_dir, st = _resolve(project)
    name = boundary(st, verb)
    if not name or name in already:
        return None
    mark_shown(project_dir, name, ctx=ctx, session_id=payload.get("session_id"))
    _log(project_dir, "compact_hint", boundary=name, ctx=ctx,
         session_id=payload.get("session_id"), last_command=verb)
    return message(name, project=project_dir.name, ctx=ctx)


def on_precompact(payload: dict) -> None:
    """The ``PreCompact`` hook: log the compaction to the book's run log."""
    transcript = payload.get("transcript_path")
    if not transcript:
        return
    rows = _tail_entries(transcript)
    found = last_harness_command(rows)
    if not found:
        return
    verb, project = found
    project_dir, st = _resolve(project)
    _log(project_dir, "compaction", trigger=payload.get("trigger"), ctx=last_context(rows),
         session_id=payload.get("session_id"), last_command=verb, stage=st.get("stage"),
         boundary=boundary(st, verb))


def _log(project_dir: Path, event: str, **fields) -> None:
    """Best-effort: a bad config or log write must never cost the hint itself."""
    try:
        from src.harness import state
        from src.utils.run_logger import log_run_event
        log_run_event(run_id=state.load_config(project_dir).get("run_id"),
                      project=project_dir.name, event=event, **fields)
    except Exception:
        pass
