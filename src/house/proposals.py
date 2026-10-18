"""
House-rule proposals: where the decisions stage's "every book would answer
this the same way" lands, and how one becomes a house rule.

``consolidate-commit`` appends each proposal to
``prompts/proposals/house_rules.jsonl`` (gitignored), one row per proposal per
book, keyed by a stable id derived from the rule text so the same proposal from
two books is one proposal seen twice. The file is append-only: adopting or
declining appends a status row, and the latest row for an id wins.

Adopting writes the rule into the operator's ``house_style_rules.json`` with
the scope (``every_book`` or ``group:<id>``) and ``triggers`` chosen at the
gate. Books already drafted are not re-settled: ``scripts/house_check.py``
lists where they depart from the new rule.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any, Optional

from src.house import EVERY_BOOK, PROMPTS_DIR

logger = logging.getLogger(__name__)

PROPOSALS_FILE = PROMPTS_DIR / "proposals" / "house_rules.jsonl"

OPEN = "open"
ADOPTED = "adopted"
DECLINED = "declined"


def proposal_id(rule: str) -> str:
    """A stable id for a proposal: the same rule text from two books is one proposal."""
    key = re.sub(r"\W+", " ", rule.casefold()).strip()
    return "p-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def _rows() -> list[dict[str, Any]]:
    if not PROPOSALS_FILE.exists():
        return []
    rows = []
    for line in PROPOSALS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("id"):
            rows.append(row)
    return rows


def _append(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    PROPOSALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with PROPOSALS_FILE.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_proposals(*, status: Optional[str] = None) -> list[dict[str, Any]]:
    """One entry per proposal: ``{id, rule, category, books, subjects, status, ...}``."""
    merged: dict[str, dict[str, Any]] = {}
    for row in _rows():
        entry = merged.setdefault(row["id"], {
            "id": row["id"], "rule": "", "category": "", "books": [], "subjects": [],
            "why": "", "status": OPEN,
        })
        if row.get("kind") == "status":
            entry["status"] = row.get("status") or entry["status"]
            entry.update({k: row[k] for k in ("scope", "rule_id", "reason") if row.get(k)})
            continue
        entry["rule"] = entry["rule"] or str(row.get("rule") or "")
        entry["category"] = entry["category"] or str(row.get("category") or "")
        entry["why"] = entry["why"] or str(row.get("why") or "")
        if row.get("project") and row["project"] not in entry["books"]:
            entry["books"].append(row["project"])
        entry["subjects"] += [s for s in row.get("subjects") or [] if s not in entry["subjects"]]
    out = list(merged.values())
    return [p for p in out if status is None or p["status"] == status]


def record_proposals(project: str, categories: list[dict[str, Any]]) -> list[str]:
    """Append this book's consolidate proposals; returns the ids newly seen for it.

    A proposal this book already recorded is not appended again, so re-running
    ``consolidate-commit`` does not inflate the count of books behind it.
    """
    seen = {(r["id"], r.get("project")) for r in _rows() if r.get("kind") != "status"}
    ts = datetime.now().isoformat(timespec="seconds")
    new_rows = []
    for cat in categories:
        for p in cat.get("proposals") or []:
            rule = str(p.get("rule") or "").strip()
            if not rule:
                continue
            pid = proposal_id(rule)
            if (pid, project) in seen:
                continue
            seen.add((pid, project))
            new_rows.append({
                "id": pid, "kind": "proposal", "ts": ts, "project": project,
                "category": cat.get("category") or "", "rule": rule,
                "why": str(p.get("why") or ""), "subjects": list(p.get("subjects") or []),
            })
    _append(new_rows)
    return [r["id"] for r in new_rows]


def _slug_id(rule: str) -> str:
    words = re.findall(r"[a-z0-9]+", rule.casefold())
    return "-".join(words[:5]) or "house-rule"


def adopt(
    pid: str,
    *,
    scope: str = EVERY_BOOK,
    triggers: Optional[list[str]] = None,
    rule_id: Optional[str] = None,
    note: str = "",
    enforce: Optional[str] = None,
    rule_text: Optional[str] = None,
) -> dict[str, Any]:
    """Write proposal ``pid`` into the house rules; returns the rule as written.

    ``rule_text`` replaces the proposal's wording, for a proposal that is right
    in direction but not in detail. The rule as written is also the answer it
    gives the ``decisions:<category>`` it settles.

    Raises ``ValueError`` for an unknown or already-decided proposal, a bad
    scope, or a rule id the house file already uses (ids are the key findings
    aggregate on, so one is never silently reused).
    """
    from src.judges import context

    proposal = next((p for p in load_proposals() if p["id"] == pid), None)
    if proposal is None:
        raise ValueError(f"no proposal {pid!r}")
    if proposal["status"] != OPEN:
        raise ValueError(f"proposal {pid} is already {proposal['status']}")
    if scope != EVERY_BOOK:
        if not scope.startswith("group:"):
            raise ValueError(f"scope must be {EVERY_BOOK!r} or 'group:<id>', not {scope!r}")
        from src.house.groups import load_groups

        group_id = scope[len("group:"):].strip()
        known = load_groups()
        if group_id not in known:
            raise ValueError(
                f"no group {group_id!r} (known: {', '.join(sorted(known)) or '(none yet)'})"
            )
        scope = f"group:{group_id}"
    if enforce is not None and enforce not in context.ENFORCE_MODES:
        raise ValueError(f"enforce must be one of {context.ENFORCE_MODES}")

    path = context.HOUSE_STYLE_RULES_FILE
    source = path if path.exists() else context.HOUSE_STYLE_RULES_EXAMPLE_FILE
    doc = json.loads(source.read_text(encoding="utf-8")) if source.exists() else {}
    if not isinstance(doc, dict):
        raise ValueError(f"{source}: expected a JSON object")
    rules = doc.setdefault("every_book", [])
    text = (rule_text or proposal["rule"]).strip()
    rule_id = (rule_id or _slug_id(text)).strip()
    taken = {str(r.get("id")) for r in rules + list(doc.get("where_the_guide_agrees") or [])}
    if rule_id in taken:
        raise ValueError(f"house rule id {rule_id!r} is already used; pass another --rule-id")

    rule: dict[str, Any] = {"id": rule_id, "rule": text}
    if note:
        rule["note"] = note
    if scope != EVERY_BOOK:
        rule["scope"] = scope
    if enforce and enforce != context.ENFORCE_PROMPT:
        rule["enforce"] = enforce
    if triggers:
        rule["triggers"] = list(triggers)
    if proposal["category"]:
        rule["settles"] = {f"decisions:{proposal['category']}": text}
    rule["origin"] = f"decisions proposal {pid} ({', '.join(proposal['books'])})"
    rules.append(rule)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _append([{
        "id": pid, "kind": "status", "status": ADOPTED, "scope": scope, "rule_id": rule_id,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }])
    return rule


def decline(pid: str, *, reason: str = "") -> dict[str, Any]:
    proposal = next((p for p in load_proposals() if p["id"] == pid), None)
    if proposal is None:
        raise ValueError(f"no proposal {pid!r}")
    if proposal["status"] != OPEN:
        raise ValueError(f"proposal {pid} is already {proposal['status']}")
    _append([{
        "id": pid, "kind": "status", "status": DECLINED, "reason": reason,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }])
    return {**proposal, "status": DECLINED}
