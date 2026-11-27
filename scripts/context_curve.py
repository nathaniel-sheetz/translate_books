"""How the conversation's context grows across a translate-harness run.

Claude Code keeps a transcript of every session as JSONL. Each assistant turn
carries its ``usage`` (input + cache read + cache write is the context the turn
was sent with), each Bash call carries the command, and each compaction leaves a
``compact_boundary`` row with its trigger and its token counts before and after.
That is everything needed to see which harness beats grow the context, where the
user compacted, and what each compaction saved. Nothing new has to be logged.

A **beat** is a run of consecutive harness / book_review / run_judges commands
with the same verb. Its span runs from its first command to the next beat's
first command, so the conversation that follows a command (an approval, a
revision) counts toward the beat that started it.

Read-only. The transcripts are private: this prints to stdout, and ``--json``
writes only where it is told (``logs/`` is gitignored).

    python -X utf8 scripts/context_curve.py <session.jsonl> [...]
    python -X utf8 scripts/context_curve.py --all [--json logs/context_curve.json]
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# `harness.py <verb> [<action>]`, `book_review.py <verb>`, `run_judges.py <verb>`.
_BEAT_RE = re.compile(
    r"(harness|book_review|run_judges)\.py[\"']?\s+([a-z][\w-]*)(?:\s+([a-z][\w-]*))?"
)
# Second words that are an action of the verb, not a flag value or a path.
_ACTIONS = {
    "prepare", "commit", "precheck", "skip", "prepare-questions", "status",
    "estimate", "signals", "apply", "fanout", "board", "judge", "show",
}
# Words that follow `harness.py` in a path or an import, not a command line.
_NOT_VERBS = {"src", "scripts", "py", "and", "or", "the", "to", "is", "in"}
_SHELL_TOOLS = {"Bash", "PowerShell"}
IDLE_GAP_S = 60 * 60  # the prompt cache's TTL; past it the next turn rewrites the context
RAMP_TURNS = 10       # turns after a compaction over which the rebuild is measured

# Beats grouped into pipeline phases. A verb not listed here (status, log-event,
# config-set, runs, ...) is neutral: it belongs to whichever phase it falls in.
_PHASE_PREFIXES = (
    ("address-map", "address-map"),
    ("style-guide", "style-guide"), ("style-rule", "style-guide"),
    ("glossary", "glossary"),
    ("difficulty", "chunk"), ("chunk-edit", "review"), ("chunk", "chunk"),
    ("translate-", "translate"), ("retranslate", "translate"),
    ("book_review:", "review"), ("run_judges:", "review"), ("sentence-edit", "review"),
    ("align", "review"), ("findings", "review"), ("combine", "review"),
    ("epub", "finish"), ("footnotes", "finish"), ("figures", "finish"),
    ("quotes", "finish"), ("captions", "finish"), ("site-export", "finish"),
    ("setup", "setup"), ("split", "setup"), ("group", "setup"), ("precedent", "setup"),
    ("collection-", "setup"),
)
PHASES = ("setup", "address-map", "style-guide", "glossary", "chunk", "translate",
          "canary-review", "review", "finish")


def phase_of(label: str) -> str | None:
    for prefix, phase in _PHASE_PREFIXES:
        if label.startswith(prefix):
            return phase
    return None


def transcript_dir(cwd: Path = REPO) -> Path:
    """Claude Code's transcript folder for a working directory."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    return Path.home() / ".claude" / "projects" / slug


def _ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _lookup_key(tool: str | None, inp: dict) -> str | None:
    """What a look-up call fetched, so the same look-up after a compaction
    counts as a re-read. Most reading in a harness run goes through Bash
    (`cat`, a `python -c` probe, `status`), so shell commands count too."""
    if tool == "Read" and inp.get("file_path"):
        return "read:" + str(inp["file_path"]).replace("\\", "/").lower()
    if tool in _SHELL_TOOLS and inp.get("command"):
        return "sh:" + " ".join(str(inp["command"]).split())
    if tool in ("Grep", "Glob"):
        return f"{tool}:{inp.get('pattern')}:{inp.get('path')}:{inp.get('glob')}"
    return None


def _beat_label(command: str) -> str | None:
    m = _BEAT_RE.search(command)
    if not m:
        return None
    script, verb, action = m.groups()
    if verb in _NOT_VERBS:
        return None
    label = verb if script == "harness" else f"{script}:{verb}"
    if action in _ACTIONS:
        label += f" {action}"
    return label


@dataclass
class Turn:
    ts: datetime | None
    ctx: int
    cache_write: int


@dataclass
class Beat:
    label: str
    start: int            # index into Session.turns of the first command's turn
    end: int = -1         # index of the last turn in the span
    commands: int = 1


@dataclass
class Compaction:
    ts: datetime | None
    trigger: str
    pre: int
    post_summary: int
    duration_s: float
    turn: int             # turns before it
    floor: int = 0        # context of the first turn after it
    # Files read before the compaction and read again after it (path -> tokens).
    rereads: dict[str, int] = field(default_factory=dict)

    @property
    def reread_files(self) -> int:
        return len(self.rereads)

    @property
    def reread_tokens(self) -> int:
        return sum(self.rereads.values())


@dataclass
class Session:
    path: Path
    turns: list[Turn] = field(default_factory=list)
    beats: list[Beat] = field(default_factory=list)
    compactions: list[Compaction] = field(default_factory=list)
    idle_gaps: list[dict] = field(default_factory=list)
    user_prompts: int = 0

    @property
    def harness(self) -> bool:
        return bool(self.beats)


def parse(path: Path) -> Session:
    s = Session(path)
    seen_msg: set[str] = set()
    read_path_by_tool: dict[str, str] = {}
    read_earlier: set[str] = set()   # files read before the latest compaction
    read_since: set[str] = set()     # files read since it
    last_ts: datetime | None = None
    pending_gap: dict | None = None

    with path.open(encoding="utf-8") as f:
        lines = f.readlines()
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        kind = e.get("type")
        ts = _ts(e.get("timestamp"))

        if kind == "system" and e.get("subtype") == "compact_boundary":
            meta = e.get("compactMetadata") or {}
            read_earlier |= read_since
            read_since = set()
            s.compactions.append(Compaction(
                ts=ts, trigger=meta.get("trigger", "?"),
                pre=int(meta.get("preTokens") or 0),
                post_summary=int(meta.get("postTokens") or 0),
                duration_s=round((meta.get("durationMs") or 0) / 1000, 1),
                turn=len(s.turns),
            ))
            continue

        if ts and last_ts and (ts - last_ts).total_seconds() > IDLE_GAP_S:
            pending_gap = {"at": ts.isoformat(timespec="minutes"),
                           "idle_min": round((ts - last_ts).total_seconds() / 60)}
        if ts:
            last_ts = ts

        msg = e.get("message")
        if not isinstance(msg, dict):
            msg = {}
        if kind == "user":
            content = msg.get("content")
            if isinstance(content, str):
                if not content.startswith("<") and not e.get("isCompactSummary"):
                    s.user_prompts += 1
            elif isinstance(content, list):
                for b in content:
                    if b.get("type") != "tool_result":
                        continue
                    p = read_path_by_tool.pop(b.get("tool_use_id"), None)
                    if p is None:
                        continue
                    if s.compactions and p in read_earlier and p not in read_since:
                        tokens = len(json.dumps(b.get("content"), ensure_ascii=False)) // 4
                        s.compactions[-1].rereads[p] = tokens
                    read_since.add(p)
            continue

        if kind != "assistant":
            continue
        mid = msg.get("id")
        u = msg.get("usage") or {}
        if mid and mid not in seen_msg:
            seen_msg.add(mid)
            ctx = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                   + u.get("cache_creation_input_tokens", 0))
            if ctx:
                s.turns.append(Turn(ts, ctx, u.get("cache_creation_input_tokens", 0)))
                if pending_gap:
                    pending_gap["ctx"] = ctx
                    pending_gap["cache_write"] = s.turns[-1].cache_write
                    s.idle_gaps.append(pending_gap)
                    pending_gap = None
                if s.compactions and not s.compactions[-1].floor:
                    s.compactions[-1].floor = ctx
        for b in msg.get("content") or []:
            if b.get("type") != "tool_use":
                continue
            inp = b.get("input") or {}
            key = _lookup_key(b.get("name"), inp)
            if key:
                read_path_by_tool[b.get("id")] = key
            if b.get("name") in _SHELL_TOOLS:
                label = _beat_label(str(inp.get("command", "")))
                if label and s.turns:
                    i = len(s.turns) - 1
                    if s.beats and s.beats[-1].label == label:
                        s.beats[-1].commands += 1
                    else:
                        if s.beats:
                            s.beats[-1].end = max(s.beats[-1].start, i - 1)
                        s.beats.append(Beat(label, i))
    if s.beats:
        s.beats[-1].end = len(s.turns) - 1
    return s


def _span_ctx(s: Session, b: Beat) -> tuple[int, int, int]:
    """(entry, peak, delta) for a beat's span. A compaction inside the span
    resets the context, so the delta is the growth summed across the pieces."""
    turns = s.turns[b.start:b.end + 1]
    if not turns:
        return 0, 0, 0
    delta, prev = 0, turns[0].ctx
    for t in turns[1:]:
        if t.ctx >= prev:
            delta += t.ctx - prev
        prev = t.ctx
    return turns[0].ctx, max(t.ctx for t in turns), delta


def _wall_min(s: Session, b: Beat) -> float | None:
    a, z = s.turns[b.start].ts, s.turns[b.end].ts
    if a and z:
        return round((z - a).total_seconds() / 60, 1)
    return None


def _k(n: int | float) -> str:
    return f"{n / 1000:.0f}k"


def _growth(turns: list[Turn]) -> int:
    """Context added across turns, ignoring the drop a compaction causes."""
    total = 0
    for a, b in zip(turns, turns[1:]):
        if b.ctx >= a.ctx:
            total += b.ctx - a.ctx
    return total


def _phase_spans(s: Session) -> list[dict]:
    """Contiguous runs of beats in one phase, neutral beats absorbed."""
    spans: list[dict] = []
    for b in s.beats:
        ph = phase_of(b.label)
        if spans and (ph is None or ph == spans[-1]["phase"]):
            spans[-1]["end"] = b.end
            continue
        if ph is None:
            continue
        spans.append({"phase": ph, "start": b.start, "end": b.end})
    # A review with more translation after it is the canary on the first wave.
    for i, sp in enumerate(spans):
        if sp["phase"] == "review" and any(x["phase"] == "translate" for x in spans[i + 1:]):
            sp["phase"] = "canary-review"
    comp_turns = [c.turn for c in s.compactions]
    out = []
    for sp in spans:
        turns = s.turns[sp["start"]:sp["end"] + 1]
        exit_turn = sp["end"] + 1
        nxt = next((t for t in comp_turns if t >= exit_turn), len(s.turns))
        out.append({
            "phase": sp["phase"],
            "entry": turns[0].ctx, "exit": turns[-1].ctx, "grew": _growth(turns),
            "turns": len(turns),
            "compacted_inside": any(sp["start"] < t <= sp["end"] for t in comp_turns),
            # Turns from the end of this phase to the next compaction or the end.
            "turns_until_compaction": nxt - exit_turn,
        })
    return out


def session_report(s: Session) -> dict:
    beats = []
    for b in s.beats:
        entry, peak, delta = _span_ctx(s, b)
        beats.append({"label": b.label, "commands": b.commands, "entry": entry,
                      "peak": peak, "delta": delta, "turns": b.end - b.start + 1,
                      "wall_min": _wall_min(s, b)})
    comps = []
    for c in s.compactions:
        nxt = next((x.turn for x in s.compactions if x.turn > c.turn), len(s.turns))
        carried = max(c.pre - c.floor, 0) if c.floor else 0
        comps.append({
            "at": c.ts.isoformat(timespec="minutes") if c.ts else None,
            "trigger": c.trigger, "pre": c.pre, "summary": c.post_summary,
            "floor": c.floor, "duration_s": c.duration_s,
            "turns_after": nxt - c.turn,
            # Context not re-sent on every turn until the next compaction or the end.
            "saved_ctx_tokens": carried * (nxt - c.turn),
            "reread_files": c.reread_files, "reread_tokens": c.reread_tokens,
            "ramp": (s.turns[min(c.turn + RAMP_TURNS, len(s.turns) - 1)].ctx - c.floor
                     if c.floor and c.turn < len(s.turns) else 0),
            "last_beat_before": next((b.label for b in reversed(s.beats) if b.start < c.turn), None),
            "first_beat_after": next((b.label for b in s.beats if b.start >= c.turn), None),
        })
    return {
        "session": s.path.stem,
        "start": s.turns[0].ts.isoformat(timespec="minutes") if s.turns and s.turns[0].ts else None,
        "assistant_turns": len(s.turns),
        "user_prompts": s.user_prompts,
        "peak_ctx": max((t.ctx for t in s.turns), default=0),
        "first_ctx": s.turns[0].ctx if s.turns else 0,
        "ctx_at_first_beat": s.turns[s.beats[0].start].ctx if s.beats else None,
        "beats": beats,
        "phases": _phase_spans(s),
        "compactions": comps,
        "idle_gaps": s.idle_gaps,
    }


def print_session(r: dict) -> None:
    print(f"## {r['session']}  ({r['start']})")
    print(f"{r['assistant_turns']} turns, {r['user_prompts']} prompts, peak {_k(r['peak_ctx'])}, "
          f"first turn {_k(r['first_ctx'])}, at first harness command "
          f"{_k(r['ctx_at_first_beat'] or 0)}\n")
    print("| beat | cmds | entry | peak | +grew | turns | min |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for b in r["beats"]:
        print(f"| {b['label']} | {b['commands']} | {_k(b['entry'])} | {_k(b['peak'])} | "
              f"{_k(b['delta'])} | {b['turns']} | {b['wall_min'] if b['wall_min'] is not None else ''} |")
    print("\n| phase | entry | exit | +grew | turns | compacted inside | turns to next compaction |")
    print("|---|---:|---:|---:|---:|---|---:|")
    for p in r["phases"]:
        print(f"| {p['phase']} | {_k(p['entry'])} | {_k(p['exit'])} | {_k(p['grew'])} | {p['turns']} | "
              f"{'yes' if p['compacted_inside'] else ''} | {p['turns_until_compaction']} |")
    if r["compactions"]:
        print(f"\n| compaction | trigger | pre | floor after | s | turns after | saved | "
              f"repeat look-ups | +{RAMP_TURNS} turns |")
        print("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for c in r["compactions"]:
            print(f"| {c['at']} after `{c['last_beat_before']}` | {c['trigger']} | {_k(c['pre'])} | "
                  f"{_k(c['floor'])} | {c['duration_s']} | {c['turns_after']} | "
                  f"{c['saved_ctx_tokens'] / 1e6:.1f}M | {c['reread_files']} / {_k(c['reread_tokens'])} | "
                  f"{_k(c['ramp'])} |")
    if r["idle_gaps"]:
        gaps = ", ".join(f"{g['idle_min']} min → {_k(g.get('cache_write', 0))} rewrite"
                         for g in r["idle_gaps"])
        print(f"\nIdle gaps past the cache TTL: {gaps}")
    print()


def _pct(values: list[float], q: float) -> float:
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


def aggregate(reports: list[dict]) -> dict:
    by_label: dict[str, list[dict]] = {}
    for r in reports:
        for b in r["beats"]:
            by_label.setdefault(b["label"], []).append(b)
    beats = []
    for label, rows in by_label.items():
        d = [x["delta"] for x in rows]
        beats.append({
            "label": label, "n": len(rows),
            "median_delta": statistics.median(d), "p90_delta": _pct(d, 0.9),
            "median_entry": statistics.median(x["entry"] for x in rows),
            "median_turns": statistics.median(x["turns"] for x in rows),
        })
    beats.sort(key=lambda x: -x["median_delta"] * x["n"])
    comps = [c for r in reports for c in r["compactions"]]
    floors = [c["floor"] for c in comps if c["floor"]]
    floor = statistics.median(floors) if floors else 0

    # Phases, over the sessions that ran a book (a glossary or a translate phase).
    books = [r for r in reports
             if any(p["phase"] in ("glossary", "translate") for p in r["phases"])]
    phases = []
    for ph in PHASES:
        rows = [(r, [p for p in r["phases"] if p["phase"] == ph]) for r in books]
        rows = [(r, ps) for r, ps in rows if ps]
        if not rows:
            continue
        grew = [sum(p["grew"] for p in ps) for _, ps in rows]
        last = [ps[-1] for _, ps in rows]  # where the phase was left for good
        value = [max(p["exit"] - floor, 0) * p["turns_until_compaction"] for p in last]
        phases.append({
            "phase": ph, "n": len(rows),
            "median_grew": statistics.median(grew), "p90_grew": _pct(grew, 0.9),
            "median_exit": statistics.median(p["exit"] for p in last),
            "exit_over_150k": sum(p["exit"] >= 150_000 for p in last),
            "median_turns_after": statistics.median(p["turns_until_compaction"] for p in last),
            "median_value": statistics.median(value),
            "compactions_inside": sum(p["compacted_inside"] for _, ps in rows for p in ps),
        })
    starts = [r["ctx_at_first_beat"] for r in reports if r["ctx_at_first_beat"]]
    firsts = [r["first_ctx"] for r in reports if r["first_ctx"]]
    return {
        "sessions": len(reports),
        "first_turn_median": statistics.median(firsts) if firsts else None,
        "ctx_at_first_beat_median": statistics.median(starts) if starts else None,
        "post_compact_floor_median": statistics.median(floors) if floors else None,
        "compactions": {
            "n": len(comps),
            "manual": sum(c["trigger"] == "manual" for c in comps),
            "auto": sum(c["trigger"] == "auto" for c in comps),
            "median_pre": statistics.median(c["pre"] for c in comps) if comps else None,
            "median_duration_s": statistics.median(c["duration_s"] for c in comps) if comps else None,
            "median_reread_tokens": statistics.median(c["reread_tokens"] for c in comps) if comps else None,
            "median_ramp": statistics.median(c["ramp"] for c in comps) if comps else None,
            "after_beat": _count(c["last_beat_before"] for c in comps),
            "auto_after_beat": _count(c["last_beat_before"] for c in comps if c["trigger"] == "auto"),
        },
        "book_sessions": len(books),
        "phases": phases,
        "beats": beats,
    }


def _count(items) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in items:
        out[str(x)] = out.get(str(x), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def print_aggregate(a: dict) -> None:
    c = a["compactions"]
    print(f"# {a['sessions']} harness sessions\n")
    print(f"- first turn median {_k(a['first_turn_median'] or 0)}; "
          f"at the first harness command {_k(a['ctx_at_first_beat_median'] or 0)}; "
          f"floor after a compaction {_k(a['post_compact_floor_median'] or 0)}")
    if c["n"]:
        print(f"- {c['n']} compactions ({c['manual']} manual, {c['auto']} auto), median at "
              f"{_k(c['median_pre'])}, {c['median_duration_s']} s, re-read after "
              f"{_k(c['median_reread_tokens'])}")
        print(f"- context added in the {RAMP_TURNS} turns after a compaction: median "
              f"{_k(c['median_ramp'])}; exact repeat look-ups {_k(c['median_reread_tokens'])}")
        print("- last beat before a compaction: "
              + ", ".join(f"`{k}` ×{v}" for k, v in c["after_beat"].items()))
        print("- ... before an auto compaction: "
              + ", ".join(f"`{k}` ×{v}" for k, v in c["auto_after_beat"].items()))
    print(f"\n## Phases ({a['book_sessions']} sessions that ran a book)\n")
    print("value = (context at the phase's end − floor) × turns until the next compaction: "
          "the context a compact at that point would have stopped re-sending.\n")
    print("| phase | n | median +grew | p90 +grew | median exit | exits ≥150k | "
          "median turns after | median value | compactions inside |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for p in a["phases"]:
        print(f"| {p['phase']} | {p['n']} | {_k(p['median_grew'])} | {_k(p['p90_grew'])} | "
              f"{_k(p['median_exit'])} | {p['exit_over_150k']} | {p['median_turns_after']:g} | "
              f"{p['median_value'] / 1e6:.1f}M | {p['compactions_inside']} |")
    print("\n## Beats\n")
    print("| beat | n | median +grew | p90 +grew | median entry | median turns |")
    print("|---|---:|---:|---:|---:|---:|")
    for b in a["beats"]:
        print(f"| {b['label']} | {b['n']} | {_k(b['median_delta'])} | {_k(b['p90_delta'])} | "
              f"{_k(b['median_entry'])} | {b['median_turns']:g} |")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="*", type=Path, help="session transcripts (.jsonl)")
    ap.add_argument("--all", action="store_true",
                    help="every session in this repo's transcript folder that ran a harness command")
    ap.add_argument("--dir", type=Path, default=None, help="transcript folder (default: this repo's)")
    ap.add_argument("--sessions", action="store_true",
                    help="with --all, also print each session's table")
    ap.add_argument("--json", type=Path, default=None, help="write the full report here")
    args = ap.parse_args(argv)

    paths = list(args.paths)
    if args.all:
        paths += sorted((args.dir or transcript_dir()).glob("*.jsonl"),
                        key=lambda p: p.stat().st_mtime)
    if not paths:
        ap.error("give transcript paths or --all")

    reports = []
    for p in paths:
        s = parse(p)
        if s.harness:
            reports.append(session_report(s))
    if not reports:
        print("no session ran a harness command", file=sys.stderr)
        return 1

    if not args.all or args.sessions:
        for r in reports:
            print_session(r)
    agg = aggregate(reports) if len(reports) > 1 else None
    if agg:
        print_aggregate(agg)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"sessions": reports, "aggregate": agg},
                                        ensure_ascii=False, indent=1, default=str),
                             encoding="utf-8")
        print(f"\nwrote {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
