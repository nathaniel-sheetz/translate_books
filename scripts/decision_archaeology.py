#!/usr/bin/env python3
"""
Mine every book's history for the decisions that were revisited.

Read-only over the projects, backups, logs and transcripts; writes only under
``--out`` (default ``comparisons/decision-archaeology/``):

  events.jsonl     one row per mined event (see EVENT KINDS below)
  versions.json    per project/artifact: every distinct version found, by date
  summary.md       counts per project and kind, plus judge false-positive rates

Sources
  * versions of style.json / glossary.json / address_map.json from the dated
    backup zips and folders, .backburner forks, .bak copies, the harness drafts
    and the live files, diffed in date order (so a change made and later
    reverted still shows)
  * harness approval events (logs/harness_runs.jsonl)
  * questionnaire answers that overrode the suggested default
  * judge findings the user marked false_positive (evaluations/_feedback.jsonl)
  * glossary-consistency rulings (consistency/verdicts.jsonl)
  * reader annotations of type word_choice / inconsistency
  * Edit/Write tool calls on the three artifacts in Claude Code transcripts
  * the user's own prompts that discuss glossary / style / address choices

The backups to read are listed in decision_archaeology_sources.json at the repo
root (gitignored; copy decision_archaeology_sources.example.json). Without it,
only the live projects, logs and transcripts are read.

Usage:
  python scripts/decision_archaeology.py
  python scripts/decision_archaeology.py --projects midnight five-little-peppers
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.annotations.store import load_active  # noqa: E402
from src.style_guide_wizard import resolve_answer  # noqa: E402
from src.utils.artifact_history import content_hash, diff_artifact  # noqa: E402

HOME = Path.home()
ARTIFACTS = {"style.json": "style", "glossary.json": "glossary", "address_map.json": "address_map"}

#: Where this machine keeps its backups. Per-user and gitignored, because the
#: list is a map of the operator's drives; copy the ``.example`` twin to start.
#: Unlike the prompt files there is no fallback to the example: its paths are
#: placeholders, so without this file no backups are read and the summary says so.
SOURCES_FILE = REPO / "decision_archaeology_sources.json"

# Fork and archive copies map back to the book they came from.
_FORK_SUFFIXES = re.compile(r"(\.bak-[\w-]+|-archive|-original|-ab)$")

# Claude Code names a project's transcript folder after its path, with every
# character that is not a letter or digit turned into a dash.
DEFAULT_TRANSCRIPTS = HOME / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(REPO))
PROMPT_HISTORY = HOME / ".claude" / "history.jsonl"


def _resolve_path(raw: str) -> Path:
    """``~`` expands to the home folder; a relative path is taken from the repo root."""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else REPO / path


def load_sources(path: Path) -> tuple[list[tuple[str, list[Path]]],
                                      list[tuple[str, Path, Optional[str]]], Path]:
    """The backup zips, backup folders and transcript folder named in ``path``.

    ``zip_backups``: dated snapshots of the whole repo, each with every place a
    copy of it lives (copies on two drives hash-dedupe away; the first that
    exists is read). ``folder_backups``: ``projects`` folders, each with a date,
    or ``null`` to date every file by its mtime. ``transcripts_dir`` overrides
    the folder derived from the repo path.
    """
    if not path.exists():
        return [], [], DEFAULT_TRANSCRIPTS
    cfg = json.loads(path.read_text(encoding="utf-8"))
    zips = [(z["date"], [_resolve_path(p) for p in z["paths"]]) for z in cfg.get("zip_backups", [])]
    folders = [(f["label"], _resolve_path(f["path"]), f.get("date"))
               for f in cfg.get("folder_backups", [])]
    transcripts = cfg.get("transcripts_dir")
    return zips, folders, _resolve_path(transcripts) if transcripts else DEFAULT_TRANSCRIPTS
_PROMPT_KEYWORDS = re.compile(
    r"\b(glossary|glosario|style guide|style\.json|address map|address_map|usted|tú|tu\b|vosotros|"
    r"rendering|translate .* as|instead of|prefer|should be|false positive|judge)\b", re.I)

EVENT_KINDS = (
    "version_change", "approval_edit", "approval_event", "questionnaire_override",
    "followup_question", "judge_fp_cluster", "judge_fp_rate", "consistency_ruling",
    "annotation_cluster", "transcript_edit", "user_prompt",
)


def base_project(name: str) -> str:
    return _FORK_SUFFIXES.sub("", name)


def _date(ts: Any) -> Optional[str]:
    if not ts:
        return None
    s = str(ts)
    m = re.match(r"\d{4}-\d{2}-\d{2}", s)
    if m:
        return m.group(0)
    try:  # epoch millis (history.jsonl)
        return datetime.fromtimestamp(float(s) / (1000 if float(s) > 1e11 else 1)).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()


def _load_json_bytes(raw: bytes) -> Any:
    for enc in ("utf-8", "utf-8-sig", "cp1252"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, ValueError):
            continue
    return None


def _read_json(path: Path) -> Any:
    try:
        return _load_json_bytes(path.read_bytes())
    except OSError:
        return None


def _read_jsonl(path: Path) -> Iterable[dict]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def _clip(text: Any, n: int = 400) -> str:
    if text is None:
        return ""
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


# ── live projects ────────────────────────────────────────────────────────────

def live_project_dirs() -> dict[str, Path]:
    """slug -> dir for every live project, including nested groups like .backburner."""
    out: dict[str, Path] = {}
    root = REPO / "projects"
    for p in sorted(root.rglob("pipeline_state.json")):
        d = p.parent
        if "archive" in d.parts or "_recovery_backup" in d.parts or ".harness" in d.parts:
            continue
        out.setdefault(d.name, d)
    for p in sorted(root.glob("*/")):
        if (p / "style.json").exists() or (p / "glossary.json").exists():
            out.setdefault(p.name, p)
    return out


# ── versions ─────────────────────────────────────────────────────────────────

def _drop_nulls(value: Any) -> Any:
    """Drop None-valued keys, so a draft that omits a field and the saved model
    that writes it as null compare equal."""
    if isinstance(value, dict):
        return {k: _drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_nulls(v) for v in value]
    return value


def _normalize_draft(kind: str, raw: Any) -> Optional[dict]:
    """Turn a harness draft into the shape of the artifact it becomes."""
    if raw is None:
        return None
    if kind == "address_map":
        return _drop_nulls(raw) if isinstance(raw, dict) else None
    if kind == "glossary":
        if isinstance(raw, dict) and "terms" in raw:
            raw = raw["terms"]
        if not isinstance(raw, list):
            return None
        terms = []
        for t in raw:
            if not isinstance(t, dict) or not t.get("english"):
                continue
            terms.append({
                "english": t.get("english"),
                "spanish": t.get("spanish", t.get("translation")),
                "type": t.get("type"),
                "context": t.get("context"),
                "alternatives": t.get("alternatives") or [],
            })
        return {"terms": terms}
    if kind == "style":
        return {"content": raw} if isinstance(raw, str) else raw
    return raw if isinstance(raw, dict) else None


def _normalize_artifact(kind: str, data: Any) -> Optional[dict]:
    if not isinstance(data, dict):
        return None
    if kind == "glossary":
        out = _normalize_draft("glossary", data.get("terms", []))
        if out is not None and data.get("updated_at"):
            out["updated_at"] = data["updated_at"]
        return out
    if kind == "address_map":
        return _drop_nulls(data)
    return data


class Versions:
    def __init__(self) -> None:
        # (project, kind) -> list of {date, source, data, hash}
        self.by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)

    def add(self, project: str, kind: str, date: Optional[str], source: str,
            data: Optional[dict], *, draft: bool = False, fork_of: Optional[str] = None) -> None:
        if not data:
            return
        seen = date or "9999-99-99"  # when the copy was taken (live = latest)
        if source.startswith("live"):
            seen = "9999-99-99"
        if not draft:
            # The artifact's own save date beats the backup's: a zip taken on
            # 08-09 may hold a map last saved on 07-26. A hand-edit can leave
            # updated_at untouched, so same-date copies then order by `seen`.
            date = _date(data.get("updated_at")) or date
        h = content_hash(data)
        self.by_key[(project, kind)].append({
            "date": date or "0000-00-00", "seen": seen, "source": source, "data": data, "hash": h,
            "draft": draft, "fork": fork_of,
        })

    def collect_zips(self, wanted: Optional[set[str]],
                     zip_backups: list[tuple[str, list[Path]]]) -> list[str]:
        seen_dates, notes = set(), []
        pat = re.compile(r"(?:^|/)projects/(.+?)/(style|glossary|address_map)\.json$")
        for date, candidates in zip_backups:
            path = next((p for p in candidates if p.exists()), None)
            if path is None:
                notes.append(f"zip {date}: not found")
                continue
            if date in seen_dates:
                continue
            seen_dates.add(date)
            n = 0
            try:
                zf = zipfile.ZipFile(path)
            except zipfile.BadZipFile:
                notes.append(f"zip {date} ({path}): unreadable, skipped")
                continue
            with zf:
                for name in zf.namelist():
                    m = pat.search(name)
                    if not m or any(s in name for s in ("/archive/", "/.harness/", "_recovery_backup/")):
                        continue
                    proj = Path(m.group(1)).name
                    base = base_project(proj)
                    if wanted and base not in wanted:
                        continue
                    kind = ARTIFACTS[m.group(2) + ".json"]
                    data = _normalize_artifact(kind, _load_json_bytes(zf.read(name)))
                    self.add(base, kind, date, f"zip {date}", data,
                             fork_of=proj if proj != base else None)
                    n += 1
            notes.append(f"zip {date} ({path}): {n} artifact files")
        return notes

    def collect_folders(self, wanted: Optional[set[str]],
                        folder_backups: list[tuple[str, Path, Optional[str]]]) -> list[str]:
        notes = []
        for label, root, date in folder_backups:
            if not root.exists():
                notes.append(f"{label}: not found")
                continue
            n = 0
            for fname, kind in ARTIFACTS.items():
                for path in root.rglob(fname):
                    rel = path.relative_to(root)
                    if any(part in ("archive", ".harness", "_recovery_backup") for part in rel.parts):
                        continue
                    proj = path.parent.name
                    base = base_project(proj)
                    if wanted and base not in wanted:
                        continue
                    data = _normalize_artifact(kind, _read_json(path))
                    self.add(base, kind, date or _mtime(path), label, data,
                             fork_of=proj if proj != base else None)
                    n += 1
            notes.append(f"{label} ({root}): {n} artifact files")
        return notes

    def collect_live(self, projects: dict[str, Path], wanted: Optional[set[str]]) -> list[str]:
        n = 0
        for slug, d in projects.items():
            base = base_project(slug)
            if wanted and base not in wanted:
                continue
            fork = slug if slug != base else None
            for fname, kind in ARTIFACTS.items():
                path = d / fname
                if path.exists():
                    data = _normalize_artifact(kind, _read_json(path))
                    date = _date((data or {}).get("updated_at")) or _mtime(path)
                    self.add(base, kind, date, "live" + (f" ({slug})" if fork else ""), data, fork_of=fork)
                    n += 1
            bak = d / "address_map.json.bak-number"
            if bak.exists():
                self.add(base, "address_map", _mtime(bak), "address_map.json.bak-number",
                         _normalize_artifact("address_map", _read_json(bak)))
            # Harness drafts: what the agent proposed before the user's edits.
            h = d / ".harness"
            drafts = {
                "style": h / "style_guide_draft.txt",
                "glossary": h / "glossary_draft.json",
                "address_map": h / "address_map_draft.json",
            }
            for kind, path in drafts.items():
                if not path.exists():
                    continue
                raw = path.read_text(encoding="utf-8", errors="replace") if kind == "style" \
                    else _read_json(path)
                self.add(base, kind, _mtime(path), "harness draft" + (f" ({slug})" if fork else ""),
                         _normalize_draft(kind, raw), draft=True, fork_of=fork)
        return [f"live projects: {n} artifact files"]

    def chains(self) -> dict[tuple[str, str, str], list[dict]]:
        """Distinct versions per (project, kind, fork) in date order.

        A fork (``.backburner/x-archive``) is its own line, so its versions never
        interleave with the live book's. Drafts sort before a same-day commit.
        """
        grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for (project, kind), rows in self.by_key.items():
            for r in rows:
                grouped[(project, kind, r.get("fork") or "")].append(r)
        out = {}
        for key, rows in grouped.items():
            rows = sorted(rows, key=lambda r: (r["date"], not r["draft"], r["seen"], r["source"]))
            chain: list[dict] = []
            for r in rows:
                if chain and chain[-1]["hash"] == r["hash"] and chain[-1]["draft"] == r["draft"]:
                    chain[-1].setdefault("also_in", []).append(r["source"])
                    continue
                chain.append(r)
            out[key] = chain
        return out


def _style_hunks(lines: list[str]) -> list[dict]:
    hunks, cur = [], None
    for line in lines:
        if line.startswith("@@"):
            cur = {"removed": [], "added": []}
            hunks.append(cur)
        elif cur is not None and line.startswith("-") and not line.startswith("---"):
            cur["removed"].append(line[1:])
        elif cur is not None and line.startswith("+") and not line.startswith("+++"):
            cur["added"].append(line[1:])
    return [h for h in hunks if h["removed"] or h["added"]]


def _diff_events(base: dict, kind: str, prev: dict, cur: dict,
                 renderings: dict[str, list]) -> list[dict]:
    events = []
    diff = diff_artifact(kind, prev["data"], cur["data"])
    if kind == "glossary":
        for c in diff["changed"]:
            if c["field"] == "context":
                continue
            events.append({**base, "subject": c["english"], "change": c["field"],
                           "before": c["before"], "after": c["after"]})
            if c["field"] == "spanish" and base["kind"] == "version_change":
                renderings[c["english"]] += [c["before"], c["after"]]
        if diff["added"]:
            events.append({**base, "subject": "terms added", "change": "added",
                           "after": [f'{t["english"]} → {t["spanish"]}' for t in diff["added"]]})
        if diff["removed"]:
            events.append({**base, "subject": "terms removed", "change": "removed",
                           "before": [f'{t["english"]} → {t["spanish"]}' for t in diff["removed"]]})
    elif kind == "style":
        for field, lines in diff.items():
            for h in _style_hunks(lines):
                events.append({**base, "subject": field, "change": "edited",
                               "before": _clip("\n".join(h["removed"]), 900),
                               "after": _clip("\n".join(h["added"]), 900)})
    else:
        for p in diff.get("pairs_added", []):
            events.append({**base, "subject": p, "change": "pair added"})
        for p in diff.get("pairs_removed", []):
            events.append({**base, "subject": p, "change": "pair removed"})
        for c in diff.get("pairs_changed", []):
            events.append({**base, "subject": c["pair"], "change": "pair changed",
                           "before": _clip(c["before"].get("directions"), 600),
                           "after": _clip(c["after"].get("directions"), 600)})
        for field in ("global_rules", "style_guide_summary", "content"):
            if field not in diff:
                continue
            val = diff[field]
            if isinstance(val, list):
                for h in _style_hunks(val):
                    events.append({**base, "subject": field, "change": "edited",
                                   "before": _clip("\n".join(h["removed"]), 900),
                                   "after": _clip("\n".join(h["added"]), 900)})
            else:
                events.append({**base, "subject": field, "change": "edited",
                               "before": _clip(val.get("before"), 900),
                               "after": _clip(val.get("after"), 900)})
    return events


def version_events(chains: dict) -> list[dict]:
    """Two kinds of diff per line of versions.

    * version_change: each committed version against the previous committed one.
    * approval_edit: each harness draft against the first commit on or after it,
      i.e. what the user changed between the agent's proposal and the artifact.
    """
    events = []
    for (project, kind, fork), chain in chains.items():
        commits = [v for v in chain if not v["draft"]]
        drafts = [v for v in chain if v["draft"]]
        renderings: dict[str, list] = defaultdict(list)
        for prev, cur in zip(commits, commits[1:]):
            base = {"project": project, "artifact": kind, "kind": "version_change",
                    "date": cur["date"], "from": f'{prev["source"]} {prev["date"]}',
                    "to": f'{cur["source"]} {cur["date"]}', "fork": fork or None}
            events += _diff_events(base, kind, prev, cur, renderings)
        for d in drafts:
            after = next((c for c in commits if c["date"] >= d["date"]), None)
            if after is None:
                continue
            if kind == "style":  # a draft holds only `content`; the commit adds `light_content`
                after = {**after, "data": {k: (after["data"] or {}).get(k) for k in (d["data"] or {})}}
            if content_hash(after["data"]) == d["hash"]:
                continue
            base = {"project": project, "artifact": kind, "kind": "approval_edit",
                    "date": after["date"], "from": f'harness draft {d["date"]}',
                    "to": f'{after["source"]} {after["date"]}', "fork": fork or None}
            events += _diff_events(base, kind, d, after, renderings)
        for term, seq in renderings.items():
            vals = [v for i, v in enumerate(seq) if i == 0 or v != seq[i - 1]]
            if len(vals) != len(set(map(str, vals))):
                events.append({"project": project, "artifact": kind, "kind": "version_change",
                               "subject": term, "change": "flip-flop", "after": vals,
                               "date": chain[-1]["date"], "fork": fork or None})
    return events


# ── harness log, questionnaire ───────────────────────────────────────────────

def approval_events(wanted: Optional[set[str]]) -> list[dict]:
    events = []
    for row in _read_jsonl(REPO / "logs" / "harness_runs.jsonl"):
        if row.get("event") != "approval":
            continue
        proj = base_project(str(row.get("project") or ""))
        if wanted and proj not in wanted:
            continue
        extra = {k: v for k, v in row.items()
                 if k not in ("beat", "decision", "redrafts", "ts", "run_id", "project", "event")}
        if row.get("decision") == "approved_first_pass" and not extra:
            continue
        events.append({"project": proj, "artifact": row.get("beat"), "kind": "approval_event",
                       "date": _date(row.get("ts")), "subject": row.get("decision"),
                       "redrafts": row.get("redrafts"), "detail": extra or None})
    return events


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def questionnaire_events(projects: dict[str, Path], wanted: Optional[set[str]]) -> list[dict]:
    events = []
    for slug, d in projects.items():
        proj = base_project(slug)
        if proj != slug:  # a fork copy repeats the live book's logs
            continue
        if wanted and proj not in wanted:
            continue
        h = d / ".harness"
        answers = _read_json(h / "style_answers.json") or {}
        if not isinstance(answers, dict):
            continue
        date = _mtime(h / "style_answers.json") if (h / "style_answers.json").exists() else None
        questions = []
        for fname in ("style_questions.json", "style_followups.json"):
            q = _read_json(h / fname)
            if isinstance(q, dict):
                q = q.get("questions", [])
            for item in q or []:
                if isinstance(item, dict):
                    questions.append((fname, item))
        followup_ids = {q.get("id") for f, q in questions if f == "style_followups.json"}
        for fname, q in questions:
            qid = q.get("id")
            if qid not in answers:
                continue
            opts = q.get("options") or []
            labels = [o.get("label", "") if isinstance(o, dict) else str(o) for o in opts]
            if fname == "style_questions.json" and qid in followup_ids:
                continue  # the follow-up pass reports it
            ans = answers[qid]
            chosen = None
            if opts and all(isinstance(o, dict) for o in opts):
                label, _, matched = resolve_answer(q, ans)
                chosen = label if matched else None
            if chosen is None:
                a = _slug(str(ans))
                chosen = next((lbl for lbl in labels if a and (_slug(lbl).startswith(a[:30])
                               or a.startswith(_slug(lbl)[:30]))), None)
            default = q.get("default")
            default_label = labels[default] if isinstance(default, int) and 0 <= default < len(labels) \
                else (default if isinstance(default, str) else None)
            overrode = bool(default_label) and _slug(str(chosen or ans)) != _slug(default_label)
            if fname == "style_followups.json":
                events.append({"project": proj, "artifact": "style", "kind": "followup_question",
                               "date": date, "subject": qid, "question": _clip(q.get("question"), 500),
                               "context": _clip(q.get("context"), 500), "options": labels,
                               "answer": chosen or ans, "default": default_label,
                               "overrode_default": overrode})
            elif not overrode:
                continue
            elif chosen:
                events.append({"project": proj, "artifact": "style", "kind": "questionnaire_override",
                               "date": date, "subject": qid, "question": _clip(q.get("question"), 300),
                               "default": default_label, "answer": chosen})
            elif isinstance(ans, str) and labels:
                # Free-text answer: the user wrote their own option.
                events.append({"project": proj, "artifact": "style", "kind": "questionnaire_override",
                               "date": date, "subject": qid, "question": _clip(q.get("question"), 300),
                               "default": default_label, "answer": _clip(ans, 500), "free_text": True})
    return events


# ── judges, consistency, annotations ─────────────────────────────────────────

_RULE = re.compile(r"^\[([^\]]+)\]")
_JUDGE_EVALS = ("address", "dialogue", "editorial", "glossary", "blacklist")


def _issue_key(eval_name: str, issue: dict) -> str:
    """Same derivation as web_ui.evaluations.issue_key, without its imports."""
    explicit = issue.get("finding_key")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    location = issue.get("location")
    if isinstance(location, dict):
        location = location.get("raw")
    parts = [eval_name or "", str(issue.get("severity") or ""),
             str(issue.get("message") or ""), str(location or "")]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


class _Findings:
    """Judge findings per chunk, looked up by key or by (eval, index)."""

    def __init__(self, eval_dir: Path) -> None:
        self.eval_dir = eval_dir
        self.cache: dict[str, dict] = {}

    def _load(self, chunk_id: str) -> dict:
        if chunk_id not in self.cache:
            data = _read_json(self.eval_dir / f"{chunk_id}.json") or {}
            by_key, by_index = {}, {}
            for name, judge in (data.get("judges") or {}).items():
                for i, issue in enumerate((judge or {}).get("issues") or []):
                    if isinstance(issue, dict):
                        by_key[_issue_key(name, issue)] = issue
                        by_index[(name, i)] = issue
            self.cache[chunk_id] = {"key": by_key, "index": by_index}
        return self.cache[chunk_id]

    def message(self, row: dict) -> str:
        if row.get("message"):
            return row["message"]
        found = self._load(str(row.get("chunk_id")))
        issue = (found["key"].get(row.get("issue_key"))
                 or found["index"].get((row.get("eval_name"), row.get("issue_index"))))
        if not issue:
            return ""
        text = issue.get("message") or ""
        if issue.get("rule_id") and not text.startswith("["):
            text = f'[{issue["rule_id"]}] {text}'
        if issue.get("suggestion"):
            text += f' (suggested: {issue["suggestion"]})'
        return text


def judge_events(projects: dict[str, Path], wanted: Optional[set[str]]) -> list[dict]:
    events = []
    for slug, d in projects.items():
        proj = base_project(slug)
        if proj != slug:  # a fork copy repeats the live book's logs
            continue
        if wanted and proj not in wanted:
            continue
        fb = d / "evaluations" / "_feedback.jsonl"
        if not fb.exists():
            continue
        last: dict[str, dict] = {}
        for row in _read_jsonl(fb):
            key = row.get("issue_key") or f'{row.get("chunk_id")}#{row.get("eval_name")}#{row.get("issue_index")}'
            last[key] = row
        findings = _Findings(d / "evaluations")
        counts: dict[str, Counter] = defaultdict(Counter)
        fps: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in last.values():
            ev = row.get("eval_name")
            if ev not in _JUDGE_EVALS:
                continue
            ft = row.get("feedback_type")
            counts[ev][ft] += 1
            if ft == "false_positive":
                msg = findings.message(row)
                m = _RULE.match(msg)
                fps[(ev, m.group(1) if m else "-")].append({**row, "message": msg})
        for ev, c in counts.items():
            labelled = c["false_positive"] + c["resolved"]
            if labelled:
                events.append({"project": proj, "artifact": "judge", "kind": "judge_fp_rate",
                               "subject": ev, "false_positive": c["false_positive"],
                               "resolved": c["resolved"], "applied": c["applied"],
                               "fp_rate": round(c["false_positive"] / labelled, 2)})
        for (ev, rule), rows in fps.items():
            rows.sort(key=lambda r: r.get("ts") or "")
            events.append({"project": proj, "artifact": "judge", "kind": "judge_fp_cluster",
                           "subject": f"{ev}:{rule}", "count": len(rows),
                           "date": _date(rows[-1].get("ts")),
                           "chunks": sorted({r.get("chunk_id") for r in rows})[:12],
                           "examples": [_clip(r.get("message"), 350) for r in rows[:6]],
                           "notes": [_clip(r.get("note"), 300) for r in rows if r.get("note")][:6]})
    return events


def consistency_events(projects: dict[str, Path], wanted: Optional[set[str]]) -> list[dict]:
    events = []
    for slug, d in projects.items():
        proj = base_project(slug)
        if proj != slug:  # a fork copy repeats the live book's logs
            continue
        if wanted and proj not in wanted:
            continue
        last: dict[str, dict] = {}
        for row in _read_jsonl(d / "consistency" / "verdicts.jsonl"):
            last[row.get("key")] = row
        for row in last.values():
            if not row.get("verdict"):
                continue
            comp = row.get("competitor") or {}
            exp = row.get("expected") or {}
            events.append({"project": proj, "artifact": "glossary", "kind": "consistency_ruling",
                           "date": _date(row.get("ts")), "subject": row.get("english"),
                           "detector": row.get("detector"), "verdict": row.get("verdict"),
                           "preferred": row.get("preferred"), "note": _clip(row.get("note"), 300),
                           "expected": exp.get("text"), "competitor": comp.get("text"),
                           "competitor_count": comp.get("count")})
    return events


_ANNOT_TYPES = ("word_choice", "inconsistency")


def annotation_events(projects: dict[str, Path], wanted: Optional[set[str]]) -> list[dict]:
    events = []
    for slug, d in projects.items():
        proj = base_project(slug)
        if proj != slug:  # a fork copy repeats the live book's logs
            continue
        if wanted and proj not in wanted:
            continue
        groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in load_active(d, types=_ANNOT_TYPES):
            content = (row.get("content") or "").split("— IA:")[0].strip()
            m = re.match(r"\[([^\]]+)\]", content)
            subject = (m.group(1) if m else content[:60]).strip().lower()
            groups[(row["type"], subject)].append({**row, "_content": content})
        for (typ, subject), rows in groups.items():
            rows.sort(key=lambda r: str(r.get("timestamp") or ""))
            events.append({"project": proj, "artifact": "translation", "kind": "annotation_cluster",
                           "subject": subject, "annotation_type": typ, "count": len(rows),
                           "date": _date(rows[-1].get("timestamp")),
                           "examples": [{"note": _clip(r["_content"], 250),
                                         "es": _clip(r.get("es_text"), 250)} for r in rows[:4]],
                           "ai_reply": _clip(((rows[0].get("ai_review") or {}).get("written_content")), 300)
                           or None})
    return events


# ── transcripts and prompts ──────────────────────────────────────────────────

_ARTIFACT_PATH = re.compile(
    r"projects[\\/](?:[^\\/]+[\\/])*?([^\\/]+)[\\/](?:\.harness[\\/])?"
    r"(style\.json|glossary\.json|address_map\.json|style_rules\.json|style_guide_draft\.txt|"
    r"glossary_draft\.json|address_map_draft\.json)$")


def transcript_events(wanted: Optional[set[str]], transcripts: Path) -> list[dict]:
    events = []
    if not transcripts.exists():
        return events
    for path in sorted(transcripts.rglob("*.jsonl")):
        session = path.stem[:8]
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"tool_use"' not in line or "projects" not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                content = ((row.get("message") or {}).get("content")) or []
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict) or item.get("type") != "tool_use":
                        continue
                    if item.get("name") not in ("Edit", "Write", "MultiEdit"):
                        continue
                    inp = item.get("input") or {}
                    m = _ARTIFACT_PATH.search(str(inp.get("file_path", "")))
                    if not m:
                        continue
                    proj = base_project(m.group(1))
                    if wanted and proj not in wanted:
                        continue
                    ev = {"project": proj, "artifact": m.group(2), "kind": "transcript_edit",
                          "date": _date(row.get("timestamp")), "session": session,
                          "tool": item.get("name")}
                    if item.get("name") == "Edit":
                        ev.update(before=_clip(inp.get("old_string"), 700),
                                  after=_clip(inp.get("new_string"), 700))
                    elif item.get("name") == "MultiEdit":
                        ev.update(edits=[{"before": _clip(e.get("old_string"), 300),
                                          "after": _clip(e.get("new_string"), 300)}
                                         for e in inp.get("edits", [])][:8])
                    else:
                        ev.update(after=_clip(inp.get("content"), 300))
                    events.append(ev)
    return events


def prompt_events(project_names: set[str], wanted: Optional[set[str]]) -> list[dict]:
    events = []
    names = sorted(project_names, key=len, reverse=True)
    for row in _read_jsonl(PROMPT_HISTORY):
        if REPO.name not in str(row.get("project", "")):
            continue
        text = row.get("display") or ""
        if len(text) < 25 or not _PROMPT_KEYWORDS.search(text):
            continue
        low = text.lower()
        proj = next((n for n in names if n in low or n.replace("-", " ") in low), None)
        if wanted and proj not in wanted:
            continue
        events.append({"project": proj or "(unspecified)", "artifact": "prompt", "kind": "user_prompt",
                       "date": _date(row.get("timestamp")), "text": _clip(text, 1200)})
    return events


# ── report ───────────────────────────────────────────────────────────────────

def write_summary(out: Path, events: list[dict], chains: dict, notes: list[str]) -> None:
    by_proj: dict[str, Counter] = defaultdict(Counter)
    for e in events:
        by_proj[e["project"]][e["kind"]] += 1
    lines = ["# Decision archaeology — summary", "",
             f"Generated {datetime.now().isoformat(timespec='seconds')}. {len(events)} events.", "",
             "## Sources read", ""] + [f"- {n}" for n in notes] + ["", "## Versions found", "",
             "| project | style | glossary | address_map |", "|---|---:|---:|---:|"]
    for p, fork in sorted({(p, f) for p, _, f in chains}):
        cells = [str(len(chains.get((p, k, fork), []))) for k in ("style", "glossary", "address_map")]
        label = f"{p} (fork {fork})" if fork else p
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    kinds = [k for k in EVENT_KINDS if any(c[k] for c in by_proj.values())]
    lines += ["", "## Events per project", "",
              "| project | " + " | ".join(kinds) + " |", "|---|" + "---:|" * len(kinds)]
    for p in sorted(by_proj):
        lines.append(f"| {p} | " + " | ".join(str(by_proj[p][k]) for k in kinds) + " |")
    rates = [e for e in events if e["kind"] == "judge_fp_rate"]
    if rates:
        lines += ["", "## Judge false-positive rates (labelled findings only)", "",
                  "| project | judge | FP | resolved | rate |", "|---|---|---:|---:|---:|"]
        for e in sorted(rates, key=lambda e: (-e["fp_rate"], e["project"])):
            lines.append(f'| {e["project"]} | {e["subject"]} | {e["false_positive"]} | '
                         f'{e["resolved"]} | {e["fp_rate"]} |')
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(REPO / "comparisons" / "decision-archaeology"))
    ap.add_argument("--projects", nargs="*", default=None, help="Limit to these book slugs")
    ap.add_argument("--skip-transcripts", action="store_true",
                    help="Skip the transcript scan (the slowest source)")
    ap.add_argument("--sources", default=str(SOURCES_FILE),
                    help="JSON naming the backup zips and folders to read "
                         "(see decision_archaeology_sources.example.json)")
    args = ap.parse_args()
    wanted = set(args.projects) if args.projects else None
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    sources = Path(args.sources)
    zip_backups, folder_backups, transcripts = load_sources(sources)

    projects = live_project_dirs()
    versions = Versions()
    notes = [] if sources.exists() else [f"{sources.name} not found: no backups read"]
    notes += versions.collect_zips(wanted, zip_backups)
    notes += versions.collect_folders(wanted, folder_backups)
    notes += versions.collect_live(projects, wanted)
    chains = versions.chains()

    events: list[dict] = []
    events += version_events(chains)
    events += approval_events(wanted)
    events += questionnaire_events(projects, wanted)
    events += judge_events(projects, wanted)
    events += consistency_events(projects, wanted)
    events += annotation_events(projects, wanted)
    if not args.skip_transcripts:
        events += transcript_events(wanted, transcripts)
    all_names = {base_project(s) for s in projects} | {p for p, _, _ in chains}
    events += prompt_events(all_names, wanted)

    # Recalled events the user added by hand survive a re-run.
    events_path = out / "events.jsonl"
    recalled = [e for e in _read_jsonl(events_path) if e.get("source") == "user_recall"]
    events.sort(key=lambda e: (e["project"], e.get("date") or "", e["kind"]))
    with events_path.open("w", encoding="utf-8") as f:
        for e in events + recalled:
            f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")

    versions_out = {
        f"{p}/{k}" + (f" (fork {fk})" if fk else ""): [{"date": v["date"], "source": v["source"], "also_in": v.get("also_in", []),
                      "draft": v["draft"], "fork": v.get("fork"), "hash": v["hash"]} for v in chain]
        for (p, k, fk), chain in sorted(chains.items())
    }
    (out / "versions.json").write_text(json.dumps(versions_out, indent=2, ensure_ascii=False),
                                       encoding="utf-8")
    write_summary(out, events, chains, notes)
    print(json.dumps({"events": len(events), "recalled_kept": len(recalled),
                      "projects": len({e["project"] for e in events}),
                      "by_kind": Counter(e["kind"] for e in events), "out": str(out)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
