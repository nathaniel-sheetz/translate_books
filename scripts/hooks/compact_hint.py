"""Claude Code hook: hint when a translate-harness session should compact.

    stop        the Stop hook. Prints {"systemMessage": ...} at a phase boundary
                when the context is past COMPACT_HINT_MIN_TOKENS (default 150000)
    precompact  the PreCompact hook. Logs the compaction to logs/harness_runs.jsonl

Reads the hook's JSON payload on stdin. It never fails a turn: any error is
swallowed, and it prints nothing. The logic is in src/harness/compact_hint.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "stop"
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        sys.path.insert(0, str(REPO))
        from src.harness import compact_hint

        if mode == "precompact":
            compact_hint.on_precompact(payload)
            return 0
        msg = compact_hint.on_stop(payload)
        if msg:
            sys.stdout.write(json.dumps({"systemMessage": msg}, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - a hook must never get in the way of a turn
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
