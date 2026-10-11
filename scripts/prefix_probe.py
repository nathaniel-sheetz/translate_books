"""Measure what a change does to the fixed prefix of a headless job.

Every headless job is billed a fixed number of input tokens before its prompt
is counted. This runs a few identical jobs per named arm, through the same
launcher the waves use (so the subscription guard applies), and prints two
things for each arm: the median overhead from the usage log, and what the
prefix was made of, read back from the conversation each CLI leaves on disk.

Arms:
    C0  Cursor wave job under the operator's home (HEADLESS_NEUTRAL_HOME=0)
    C1  the same again; run it after moving a folder out of ~/.claude/skills
    C2  Cursor wave job under a neutral home (HEADLESS_NEUTRAL_HOME=1)
    A0  Claude wave job with no trimming flag
    A1  Claude wave job with --strict-mcp-config
    A2  Claude wave job with ENABLE_CLAUDEAI_MCP_SERVERS=false
    A3  Claude retranslate call, the CLI's default system prompt, no flag
    A4  Claude retranslate call, the short system prompt and --strict-mcp-config
    G0  Cursor retranslate call under the operator's home
    G1  Cursor retranslate call under a neutral home

Every arm sets its own switches, so an arm means the same thing whatever the
launcher's defaults are on the day it is run.

Usage:
    python scripts/prefix_probe.py C0 C2
    python scripts/prefix_probe.py A0 --flag=--safe-mode
    python scripts/prefix_probe.py G0 G1 --project projects/pollyanna
    python scripts/prefix_probe.py --list
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src import llm_mechanisms  # noqa: E402
from src.harness import headless  # noqa: E402
from src.llm_mechanisms import MechanismError  # noqa: E402
from src.retranslator import retranslate_sentence  # noqa: E402
from src.utils.file_io import load_glossary  # noqa: E402

NEUTRAL_HOME = {"HEADLESS_NEUTRAL_HOME": "1"}
OPERATOR_HOME = {"HEADLESS_NEUTRAL_HOME": "0"}

# A wave-shaped job: a fixed preamble plus a short body, as a judge wave sends.
PREAMBLE = (
    "You check one Spanish sentence against its English source. Reply with a "
    "single line: OK if the Spanish says what the English says, or one short "
    "sentence naming the difference. No preface, no markdown.\n"
)
BODY = (
    "English: The old houses along the river had stood empty since the flood.\n"
    "Spanish: Los caserones viejos junto al río llevaban vacíos desde la riada.\n"
)

# A retranslate-shaped call: what the reader sends for one sentence.
SOURCE = "She was quite sure, now, that the day would be a glad one after all."
CONTEXT = (
    "Pollyanna drew a long breath and looked about her. "
    "She was quite sure, now, that the day would be a glad one after all. "
    "Even the bare little attic room seemed friendlier in the morning light."
)

CURSOR_BLOCKS = (
    "agent_skills", "rules", "available_subagent_types", "available_subagent_models",
    "dynamic_tools", "user_info", "agent_transcripts", "system_reminder",
)


@dataclass(frozen=True)
class Arm:
    cli: str
    shape: str  # "wave" or "retranslate"
    note: str
    env: dict[str, str] = field(default_factory=dict)
    flags: tuple[str, ...] = ()
    system_prompt: bool = True


ARMS: dict[str, Arm] = {
    "C0": Arm("cursor", "wave", "Cursor wave job, operator's home", env=OPERATOR_HOME),
    "C1": Arm("cursor", "wave", "Cursor wave job, operator's home, after a skills-folder change on disk",
              env=OPERATOR_HOME),
    "C2": Arm("cursor", "wave", "Cursor wave job, neutral home", env=NEUTRAL_HOME),
    "A0": Arm("claude", "wave", "Claude wave job, no trim"),
    "A1": Arm("claude", "wave", "Claude wave job, --strict-mcp-config", flags=("--strict-mcp-config",)),
    "A2": Arm("claude", "wave", "Claude wave job, connectors off by env",
              env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}),
    "A3": Arm("claude", "retranslate", "Claude retranslate, default system prompt, no trim", system_prompt=False),
    "A4": Arm("claude", "retranslate", "Claude retranslate, short system prompt, --strict-mcp-config",
              flags=("--strict-mcp-config",)),
    "G0": Arm("cursor", "retranslate", "Cursor retranslate, operator's home", env=OPERATOR_HOME),
    "G1": Arm("cursor", "retranslate", "Cursor retranslate, neutral home", env=NEUTRAL_HOME),
}


def _billed(row: dict[str, Any]) -> int:
    return sum(int(row.get(k) or 0) for k in ("input", "cache_creation", "cache_read"))


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def _open_readonly(path: str) -> sqlite3.Connection | None:
    uri = "file:" + path.replace(os.sep, "/")
    for suffix in ("?mode=ro", "?mode=ro&immutable=1"):
        try:
            con = sqlite3.connect(uri + suffix, uri=True)
            con.execute("select count(*) from blobs").fetchone()
            return con
        except sqlite3.Error:
            continue
    return None


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _cursor_store(path: str) -> dict[str, Any] | None:
    """What one Cursor job's prefix held. Reads the ``blobs`` table only.

    The ``meta`` table carries the store's encryption key and is never opened.
    """
    con = _open_readonly(path)
    if con is None:
        return None
    system_chars, context = 0, ""
    try:
        for (data,) in con.execute("select data from blobs"):
            try:
                obj = json.loads(bytes(data).decode("utf-8"))
            except (ValueError, TypeError):
                continue
            if not isinstance(obj, dict):
                continue
            text = _text_of(obj.get("content"))
            if obj.get("role") == "system":
                system_chars = max(system_chars, len(text))
            elif obj.get("role") == "user" and "<user_info" in text and len(text) > len(context):
                context = text
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not context:
        return None
    blocks = {}
    for tag in CURSOR_BLOCKS:
        found = re.search(rf"<{tag}\b.*?</{tag}>", context, re.S)
        blocks[tag] = len(found.group(0)) if found else 0
    return {
        "skills": len(re.findall(r"<agent_skill\b", context)),
        "skill_roots": len(re.findall(r"^\$\d+ = ", context, re.M)),
        "user_rules": len(re.findall(r"<user_rule>", context)),
        "context_chars": len(context),
        "system_chars": system_chars,
        "blocks": blocks,
    }


def _cursor_readout(started: float) -> list[dict[str, Any]]:
    roots = [headless._slot_root(), Path.home() / ".cursor"]
    paths: list[str] = []
    for root in roots:
        paths += glob.glob(str(root / "slot-*" / "chats" / "*" / "*" / "store.db"))
        paths += glob.glob(str(root / "chats" / "*" / "*" / "store.db"))
    fresh = sorted(p for p in set(paths) if os.path.getmtime(p) >= started)
    return [found for found in (_cursor_store(p) for p in fresh) if found]


def _home_readout() -> dict[str, Any]:
    """How deep the neutral homes run, and what the CLI put in them."""
    longest, files, top = 0, 0, set()
    for home in glob.glob(str(headless._slot_root() / "slot-*" / headless._NEUTRAL_HOME_NAME)):
        top.update(os.listdir(home))
        for dirpath, _dirnames, filenames in os.walk(home):
            longest = max(longest, len(dirpath))
            for name in filenames:
                files += 1
                longest = max(longest, len(os.path.join(dirpath, name)))
    return {"longest_path": longest, "files": files, "top_level": sorted(top)}


def _claude_readout(started: float) -> list[dict[str, Any]]:
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(headless.neutral_claude_cwd()))
    base = Path.home() / ".claude" / "projects"
    folder = base / slug
    if not folder.is_dir():
        matches = glob.glob(str(base / "*claude-headless-empty*"))
        if not matches:
            return []
        folder = Path(matches[0])
    out = []
    for path in sorted(folder.glob("*.jsonl")):
        if path.stat().st_mtime < started:
            continue
        tools, tool_chars, system_chars, mcp_chars, attachments = 0, 0, 0, 0, {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("type") != "attachment":
                continue
            att = obj.get("attachment") or {}
            kind = str(att.get("type"))
            if kind == "prompt_snapshot":
                listed = att.get("tools") or []
                tools, tool_chars = len(listed), len(json.dumps(listed)) if listed else 0
                parts = att.get("systemPrompt")
                if isinstance(parts, list):
                    system_chars = sum(len(p) if isinstance(p, str) else len(json.dumps(p)) for p in parts)
                elif parts:
                    system_chars = len(str(parts))
                continue
            if kind == "mcp_instructions_delta":
                mcp_chars = len(json.dumps(att))
            attachments[kind] = len(json.dumps(att))
        out.append({
            "tools": tools,
            "tool_chars": tool_chars,
            "system_chars": system_chars,
            "mcp_instruction_chars": mcp_chars,
            "attachments": attachments,
        })
    return out


def _run_wave(arm: Arm, arm_dir: Path, args: argparse.Namespace) -> str | None:
    preamble = arm_dir / "preamble.txt"
    preamble.write_text(PREAMBLE, encoding="utf-8")
    jobs = [
        {
            "id": f"{arm_dir.name}-{index}",
            "input_text": BODY,
            "system_prompt_file": str(preamble),
            "output_path": str(arm_dir / f"out-{index}.txt"),
        }
        for index in range(args.jobs)
    ]
    claude = arm.cli == "claude"
    result = headless.run_headless_wave(
        jobs,
        model=args.claude_model if claude else args.cursor_model,
        concurrency=args.concurrency,
        cli=arm.cli,
        usage_log=arm_dir / "usage.jsonl",
        extra_flags=["--effort", args.effort] if claude else [],
        effort=args.effort if claude else None,
        warm_first=False,
        cache="off",
    )
    if result.get("error"):
        return str(result["error"])
    for failed in result.get("failed") or []:
        print(f"    failed {failed.get('id')}: {failed.get('error')}")
    usage = result.get("usage") or {}
    for key in ("slots_seeded", "neutral_home"):
        if key in usage:
            print(f"    {key}: {usage[key]}")
    return None


def _run_retranslate(arm: Arm, arm_dir: Path, args: argparse.Namespace) -> str | None:
    project = Path(args.project)
    glossary_path = project / "glossary.json"
    claude = arm.cli == "claude"
    saved_prompt = llm_mechanisms.HEADLESS_SYSTEM_PROMPT
    if not arm.system_prompt:
        llm_mechanisms.HEADLESS_SYSTEM_PROMPT = ""
    try:
        for index in range(args.jobs):
            try:
                result = retranslate_sentence(
                    SOURCE,
                    style_json_path=project / "style.json",
                    glossary=load_glossary(glossary_path) if glossary_path.exists() else None,
                    provider=args.claude_provider if claude else args.cursor_provider,
                    model=args.claude_model if claude else args.cursor_model,
                    context_text=CONTEXT,
                    project_dir=arm_dir,
                )
            except MechanismError as exc:
                print(f"    call {index} failed: {exc}")
                continue
            print(f"    call {index}: {result.new_translation!r}")
    finally:
        llm_mechanisms.HEADLESS_SYSTEM_PROMPT = saved_prompt
    return None


def run_arm(name: str, args: argparse.Namespace, out_root: Path) -> dict[str, Any]:
    arm = ARMS[name]
    arm_dir = out_root / name
    arm_dir.mkdir(parents=True, exist_ok=True)
    env = {**arm.env, **dict(pair.split("=", 1) for pair in args.env)}
    flags = [*arm.flags, *args.flag]
    print(f"\n== {name}: {arm.note}" + (f"  env={env}" if env else "") + (f"  flags={flags}" if flags else ""))

    saved = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    # The arm's flags replace the launcher's own list for its length: a
    # retranslate call takes no argv, and a baseline arm must run without the
    # trim the launcher now adds by default.
    saved_trim = headless._CLAUDE_TRIM_FLAGS
    headless._CLAUDE_TRIM_FLAGS = tuple(flags)
    started = time.time()
    try:
        if arm.shape == "wave":
            error = _run_wave(arm, arm_dir, args)
            log = arm_dir / "usage.jsonl"
        else:
            error = _run_retranslate(arm, arm_dir, args)
            log = arm_dir / ".harness" / "retranslate" / "usage.jsonl"
    finally:
        headless._CLAUDE_TRIM_FLAGS = saved_trim
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    summary: dict[str, Any] = {"arm": name, "note": arm.note, "env": env, "flags": flags}
    if error:
        print(f"    refused before any job ran: {error}")
        summary["error"] = error
    rows = [r for r in _read_rows(log) if r.get("rc") == 0 and _billed(r)]
    overheads = [_billed(r) - int(r.get("prompt_sent") or 0) for r in rows]
    for row, overhead in zip(rows, overheads):
        print(
            f"    billed {_billed(row):6d}  sent {int(row.get('prompt_sent') or 0):5d}  "
            f"overhead {overhead:6d}  output {int(row.get('output') or 0):5d}  "
            f"wall {row.get('wall_s')}s  flags {row.get('flags')}"
            + ("  neutral_home" if row.get("neutral_home") else "")
        )
    summary["jobs_ok"] = len(rows)
    summary["overheads"] = overheads
    summary["median_overhead"] = int(statistics.median(overheads)) if overheads else None
    print(f"    median overhead: {summary['median_overhead']} over {len(rows)} of {args.jobs} jobs")

    if arm.cli == "cursor":
        stores = _cursor_readout(started)
        summary["stores"] = stores
        for store in stores:
            blocks = "  ".join(f"{tag} {chars}" for tag, chars in store["blocks"].items() if chars)
            print(
                f"    store: {store['skills']} skills from {store['skill_roots']} roots, "
                f"{store['user_rules']} rules, context {store['context_chars']} chars, "
                f"system {store['system_chars']} chars"
            )
            print(f"           {blocks}")
        if env.get("HEADLESS_NEUTRAL_HOME") == "1":
            summary["home"] = _home_readout()
            print(f"    neutral homes: {summary['home']}")
    else:
        transcripts = _claude_readout(started)
        summary["transcripts"] = transcripts
        for item in transcripts:
            print(
                f"    transcript: {item['tools']} tools ({item['tool_chars']} chars), "
                f"system prompt {item['system_chars']} chars, "
                f"MCP instructions {item['mcp_instruction_chars']} chars"
            )
    (arm_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("arms", nargs="*", help="arm names, run in the order given")
    parser.add_argument("--list", action="store_true", help="list the arms and exit")
    parser.add_argument("--jobs", type=int, default=3, help="jobs per arm (default 3)")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="workers for the wave arms (default 1; raise it to exercise the slots)")
    parser.add_argument("--flag", action="append", default=[], help="extra Claude argv for every arm run")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                        help="extra environment for every arm run")
    parser.add_argument("--project", default="projects/pollyanna",
                        help="book whose style guide and glossary the retranslate arms use")
    parser.add_argument("--out", help="output folder (default comparisons/prefix-probe/<date>)")
    parser.add_argument("--effort", default="medium", help="Claude effort for wave arms")
    parser.add_argument("--claude-model", default="claude-sonnet-5-5")
    parser.add_argument("--cursor-model", default="grok-4.7-medium")
    parser.add_argument("--claude-provider", default="claude-headless")
    parser.add_argument("--cursor-provider", default="cursor-headless")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    if args.list or not args.arms:
        for name, arm in ARMS.items():
            print(f"{name}  {arm.note}")
        return 0
    unknown = [name for name in args.arms if name not in ARMS]
    if unknown:
        parser.error(f"unknown arm(s): {', '.join(unknown)}")
    if any("=" not in pair for pair in args.env):
        parser.error("--env takes KEY=VALUE")

    out_root = Path(args.out) if args.out else _REPO_ROOT / "comparisons" / "prefix-probe" / time.strftime("%Y-%m-%d")
    summaries = [run_arm(name, args, out_root) for name in args.arms]

    print("\narm   jobs  median overhead")
    for summary in summaries:
        print(f"{summary['arm']:4s}  {summary['jobs_ok']:4d}  {summary['median_overhead']}")
    print(f"\nwritten to {out_root}")
    return 0 if all(s["jobs_ok"] for s in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
