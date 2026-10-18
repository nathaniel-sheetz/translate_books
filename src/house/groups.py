"""
Book groups: which books share characters (a series) or vocabulary (a domain).

``prompts/book_groups.json`` (gitignored; ``book_groups.example.json`` is the
tracked twin) holds ``{"groups": {id: {kind, about, members: [slug, ...]}}}``.

- A **series** shares characters, so a sibling pre-fills names, address pairs,
  style answers and vocabulary.
- A **domain** (the horse books) shares vocabulary and style answers, but not
  address pairs: two horse books do not share a cast.

A group's id is also a scope: a house rule or lexicon entry with
``scope: "group:horses"`` applies only to the horse books.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from src.house import PROMPTS_DIR

logger = logging.getLogger(__name__)

GROUPS_FILE = PROMPTS_DIR / "book_groups.json"
GROUPS_EXAMPLE_FILE = PROMPTS_DIR / "book_groups.example.json"

SERIES = "series"
DOMAIN = "domain"
KINDS = (SERIES, DOMAIN)


def groups_path() -> Path:
    """The operator's groups file if present, else the tracked example."""
    return GROUPS_FILE if GROUPS_FILE.exists() else GROUPS_EXAMPLE_FILE


def load_groups(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """``{group_id: {kind, about, members}}``, or ``{}`` when there is no usable file.

    A malformed file is logged and read as no groups: a book with no siblings
    still translates, it just gets no precedent.
    """
    path = Path(path) if path else groups_path()
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable %s: %s", path, exc)
        return {}
    raw = doc.get("groups") if isinstance(doc, dict) else None
    if not isinstance(raw, dict):
        logger.warning("Ignoring %s: expected a 'groups' object", path)
        return {}
    groups: dict[str, dict[str, Any]] = {}
    for gid, group in raw.items():
        if not isinstance(group, dict):
            continue
        kind = str(group.get("kind") or DOMAIN).strip()
        groups[str(gid)] = {
            "kind": kind if kind in KINDS else DOMAIN,
            "about": str(group.get("about") or "").strip(),
            "members": [str(m) for m in group.get("members") or [] if str(m).strip()],
        }
    return groups


def groups_of(slug: Optional[str], groups: Optional[dict] = None) -> dict[str, dict[str, Any]]:
    """The groups ``slug`` belongs to."""
    if not slug:
        return {}
    groups = load_groups() if groups is None else groups
    return {gid: g for gid, g in groups.items() if slug in g["members"]}


def group_ids_of(slug: Optional[str], groups: Optional[dict] = None) -> set[str]:
    return set(groups_of(slug, groups))


def siblings(slug: Optional[str], groups: Optional[dict] = None) -> list[dict[str, str]]:
    """Every other member of every group ``slug`` is in, as ``{slug, kind, group}``.

    A book in both a series and a domain with the same sibling is listed once,
    under the series: a series sibling contributes everything a domain one does
    and the address pairs besides.
    """
    found: dict[str, dict[str, str]] = {}
    for gid, group in groups_of(slug, groups).items():
        for member in group["members"]:
            if member == slug:
                continue
            prior = found.get(member)
            if prior is None or (group["kind"] == SERIES and prior["kind"] != SERIES):
                found[member] = {"slug": member, "kind": group["kind"], "group": gid}
    return sorted(found.values(), key=lambda s: (s["kind"] != SERIES, s["slug"]))


def _write(doc: dict[str, Any]) -> Path:
    GROUPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    GROUPS_FILE.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return GROUPS_FILE


def _editable_doc() -> dict[str, Any]:
    """The operator's file as a dict, seeded from the example on first write.

    Seeding from the example rather than from nothing keeps a clone's first
    ``group join`` from silently discarding the example's groups the operator
    has been reading all along.

    Raises ``ValueError`` when the operator's file exists but does not parse:
    writing over it would replace every group with the one being edited.
    """
    about = ""
    if GROUPS_FILE.exists():
        try:
            loaded = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"{GROUPS_FILE} is unreadable ({exc}); fix it by hand first") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"{GROUPS_FILE} is not a JSON object; fix it by hand first")
        about = str(loaded.get("about") or "")
    doc: dict[str, Any] = {"version": 1}
    if about:
        doc["about"] = about
    doc["groups"] = load_groups()
    return doc


def join_group(
    slug: str, group_id: str, *, kind: Optional[str] = None, about: Optional[str] = None
) -> dict[str, Any]:
    """Add ``slug`` to ``group_id``, creating the group when ``kind`` is given.

    Raises ``ValueError`` for an unknown group with no ``kind``, so a typo in a
    group id does not quietly found a new group of one.
    """
    group_id = group_id.strip()
    if not group_id:
        raise ValueError("group id is empty")
    doc = _editable_doc()
    groups = doc["groups"]
    group = groups.get(group_id)
    if group is None:
        if kind not in KINDS:
            known = ", ".join(sorted(groups)) or "(none yet)"
            raise ValueError(
                f"no group {group_id!r} (known: {known}); pass --kind series|domain to create it"
            )
        group = groups[group_id] = {"kind": kind, "about": (about or "").strip(), "members": []}
    elif about:
        group["about"] = about.strip()
    if slug not in group["members"]:
        group["members"].append(slug)
    _write(doc)
    return {"group": group_id, **group}


def leave_group(slug: str, group_id: str) -> dict[str, Any]:
    doc = _editable_doc()
    group = doc["groups"].get(group_id)
    if group is None:
        raise ValueError(f"no group {group_id!r}")
    group["members"] = [m for m in group["members"] if m != slug]
    _write(doc)
    return {"group": group_id, **group}


def describe(slug: Optional[str] = None) -> dict[str, Any]:
    """Every group, and which ones ``slug`` is in, for setup and the gates."""
    groups = load_groups()
    return {
        "path": str(groups_path()),
        "available": [
            {"id": gid, "kind": g["kind"], "about": g["about"], "members": len(g["members"])}
            for gid, g in sorted(groups.items())
        ],
        "joined": sorted(group_ids_of(slug, groups)) if slug else [],
    }
