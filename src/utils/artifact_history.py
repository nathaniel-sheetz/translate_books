"""
Change log for a book's three decision artifacts.

Every save of ``glossary.json``, ``style.json`` or ``address_map.json`` appends
one row to ``<project>/decisions_history.jsonl`` holding the before and after
content and a compact diff. Saves used to overwrite with no trace, so the
question "when did this book's choices change, and why" had no answer.

A row is skipped when nothing but a timestamp changed. Writes that bypass the
save functions (an agent hand-editing ``style.json``) are caught later by
``check_external_edits``, which compares each file with the last row logged for
it. Logging is best-effort: a failure here is warned about and never fails the
save it rides on.
"""

import difflib
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

HISTORY_FILENAME = "decisions_history.jsonl"

# Only these filenames are logged. A save to any other path (a comparison
# copy, a temp file) is not a book's live decision artifact.
ARTIFACT_FILES = {
    "glossary.json": "glossary",
    "style.json": "style",
    "address_map.json": "address_map",
}

# Bumped on every save whether or not anything changed, so they never count.
_VOLATILE_KEYS = ("updated_at", "created_at", "version")


def artifact_kind(path: Path) -> Optional[str]:
    return ARTIFACT_FILES.get(Path(path).name)


def history_path(project_dir: Path) -> Path:
    return Path(project_dir) / HISTORY_FILENAME


def _stable(data: Optional[dict]) -> Optional[dict]:
    if data is None:
        return None
    return {k: v for k, v in data.items() if k not in _VOLATILE_KEYS}


def content_hash(data: Optional[dict]) -> Optional[str]:
    """Hash of the content that matters, ignoring timestamps and version."""
    if data is None:
        return None
    blob = json.dumps(_stable(data), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def read_json(path: Path) -> Optional[dict]:
    """The file's JSON, or None when it is missing or unreadable."""
    try:
        with Path(path).open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ── diffs ───────────────────────────────────────────────────────────────────

def _text_diff(before: str, after: str) -> list[str]:
    return list(difflib.unified_diff(
        (before or "").splitlines(), (after or "").splitlines(),
        "before", "after", lineterm="", n=1,
    ))


def _glossary_diff(before: dict, after: dict) -> dict:
    def index(data: dict) -> dict:
        return {
            str(t.get("english", "")).strip().lower(): t
            for t in (data or {}).get("terms", []) if isinstance(t, dict)
        }

    old, new = index(before), index(after)
    changed = []
    for key in sorted(old.keys() & new.keys()):
        for field in ("spanish", "type", "alternatives", "context"):
            if old[key].get(field) != new[key].get(field):
                changed.append({
                    "english": new[key].get("english"),
                    "field": field,
                    "before": old[key].get(field),
                    "after": new[key].get(field),
                })
    return {
        "added": [new[k] for k in sorted(new.keys() - old.keys())],
        "removed": [old[k] for k in sorted(old.keys() - new.keys())],
        "changed": changed,
    }


def _style_diff(before: dict, after: dict) -> dict:
    out = {}
    for field in ("content", "light_content"):
        lines = _text_diff((before or {}).get(field) or "", (after or {}).get(field) or "")
        if lines:
            out[field] = lines
    return out


def _address_map_diff(before: dict, after: dict) -> dict:
    def index(data: dict) -> dict:
        return {
            f"{p.get('a', '')} <-> {p.get('b', '')}": p
            for p in (data or {}).get("pairs", []) if isinstance(p, dict)
        }

    old, new = index(before), index(after)
    out: dict[str, Any] = {
        "pairs_added": sorted(new.keys() - old.keys()),
        "pairs_removed": sorted(old.keys() - new.keys()),
        "pairs_changed": [
            {"pair": k, "before": old[k], "after": new[k]}
            for k in sorted(old.keys() & new.keys()) if old[k] != new[k]
        ],
    }
    for field in ("global_rules", "style_guide_summary", "content"):
        b, a = (before or {}).get(field), (after or {}).get(field)
        if b == a:
            continue
        if isinstance(a, str) or isinstance(b, str):
            out[field] = _text_diff(b or "", a or "")
        else:
            out[field] = {"before": b, "after": a}
    return out


_DIFFERS = {
    "glossary": _glossary_diff,
    "style": _style_diff,
    "address_map": _address_map_diff,
}


def diff_artifact(kind: str, before: Optional[dict], after: Optional[dict]) -> dict:
    return _DIFFERS[kind](before or {}, after or {})


# ── log ─────────────────────────────────────────────────────────────────────

def read_history(project_dir: Path) -> list[dict]:
    path = history_path(project_dir)
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _last_rows(project_dir: Path) -> dict[str, dict]:
    last: dict[str, dict] = {}
    for row in read_history(project_dir):
        if row.get("artifact"):
            last[row["artifact"]] = row
    return last


def _append(project_dir: Path, row: dict) -> None:
    path = history_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def record_change(
    output_path: Path,
    before: Optional[dict],
    after: Optional[dict],
    *,
    writer: Optional[str] = None,
    reason: Optional[str] = None,
) -> Optional[dict]:
    """Append a history row for a save that changed ``output_path``.

    ``before`` is the file's JSON as read just before the save (None for a new
    file). Returns the row written, or None when the path is not an artifact or
    nothing changed. Never raises.
    """
    try:
        kind = artifact_kind(output_path)
        if kind is None:
            return None
        before_hash, after_hash = content_hash(before), content_hash(after)
        if before_hash == after_hash:
            return None
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "artifact": kind,
            "writer": writer or "unknown",
            "reason": reason,
            "before_hash": before_hash,
            "after_hash": after_hash,
            "diff": diff_artifact(kind, before, after),
            "before": before,
            "after": after,
        }
        _append(Path(output_path).parent, row)
        return row
    except Exception as exc:  # never fail the save over its log
        _log.warning("Could not log the change to %s: %s", output_path, exc)
        return None


def check_external_edits(project_dir: Path) -> list[dict]:
    """Log any artifact that changed on disk since its last history row.

    An artifact with no row yet gets a ``writer: "baseline"`` row, which seeds
    the log for books that predate it. Returns the rows written. Never raises.
    """
    written: list[dict] = []
    try:
        project_dir = Path(project_dir)
        last = _last_rows(project_dir)
        for filename, kind in ARTIFACT_FILES.items():
            path = project_dir / filename
            if not path.exists():
                continue
            current = read_json(path)
            if current is None:
                continue
            prev = last.get(kind)
            if prev is None:
                writer, before = "baseline", None
            elif prev.get("after_hash") != content_hash(current):
                writer, before = "external", prev.get("after")
            else:
                continue
            row = record_change(path, before, current, writer=writer)
            if row:
                written.append(row)
    except Exception as exc:
        _log.warning("Could not check %s for outside edits: %s", project_dir, exc)
    return written
