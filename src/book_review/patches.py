"""Pure patch functions: apply one validated patch to an artifact document in memory.

Shared by :mod:`src.book_review.schema`, which replays a recommendation's
patches in order so each is validated against the result of the ones before it
(a rekey followed by a variant of the new key), and by
:mod:`src.book_review.apply`, which applies them for real.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Optional

#: The artifact each patch family edits, as :func:`apply_patch` names them.
ARTIFACT_OF = {"glossary": "glossary", "style": "style", "address": "address_map"}


def artifact_of(patch_type: str) -> Optional[str]:
    return ARTIFACT_OF.get(patch_type.split(".", 1)[0])


def find_entry(terms: list[dict], english: str) -> Optional[dict]:
    wanted = english.lower()
    return next((t for t in terms if str(t.get("english") or "").lower() == wanted), None)


def apply_glossary(doc: dict, patch: dict) -> None:
    terms = doc.setdefault("terms", [])
    kind = patch["type"]
    if kind == "glossary.add":
        terms.append({"english": patch["english"], "spanish": patch["spanish"], "type": "other",
                      "alternatives": patch.get("alternatives") or [],
                      **({"context": patch["context"]} if patch.get("context") else {})})
        return
    entry = find_entry(terms, patch["english"])
    if entry is None:
        raise ValueError(f"glossary entry {patch['english']!r} is gone")
    if kind == "glossary.rekey":
        entry["english"] = patch["new_english"]
    elif kind == "glossary.add_variant":
        twin = copy.deepcopy(entry)
        twin["english"] = patch["new_english"]
        terms.insert(terms.index(entry) + 1, twin)
    elif kind == "glossary.edit":
        for field in ("spanish", "alternatives", "context"):
            if field in patch:
                entry[field] = patch[field]
    elif kind == "glossary.split_sense":
        at = terms.index(entry)
        terms.pop(at)
        for offset, e in enumerate(patch["entries"]):
            terms.insert(at + offset, {"english": e["english"], "spanish": e["spanish"],
                                       "type": entry.get("type") or "other", "alternatives": [],
                                       **({"context": e["context"]} if e.get("context") else {})})
    elif kind == "glossary.remove":
        terms.remove(entry)


def apply_style(doc: dict, patch: dict) -> None:
    content = doc.get("content") or ""
    if patch["type"] == "style.replace_text":
        if content.count(patch["before"]) != 1:
            raise ValueError("the style guide text to replace is no longer there exactly once")
        doc["content"] = content.replace(patch["before"], patch["after"], 1)
    else:
        doc["content"] = content.rstrip() + "\n\n" + patch["text"]


def apply_address(doc: dict, patch: dict) -> None:
    if patch["type"] == "address.replace_global_rules_text":
        rules = doc.get("global_rules") or ""
        if rules.count(patch["before"]) != 1:
            raise ValueError("the global-rules text to replace is no longer there exactly once")
        doc["global_rules"] = rules.replace(patch["before"], patch["after"], 1)
        return
    if patch["type"] == "address.add_pair":
        def segment(form):
            seg = {"form": form, "when": "default"}
            if patch.get("notes"):
                seg["notes"] = patch["notes"]
            return [seg]
        doc.setdefault("pairs", []).append({
            "a": patch["a"], "b": patch["b"],
            **({"relationship": patch["relationship"]} if patch.get("relationship") else {}),
            "directions": {"a_to_b": segment(patch["a_to_b"]), "b_to_a": segment(patch["b_to_a"])},
        })
        return
    for pair in doc.get("pairs") or []:
        if pair.get("a") == patch["a"] and pair.get("b") == patch["b"]:
            segment = {"form": patch["form"], "when": "default"}
            if patch.get("notes"):
                segment["notes"] = patch["notes"]
            pair.setdefault("directions", {})[patch["direction"]] = [segment]
            return
    raise ValueError(f"address pair {patch['a']}/{patch['b']} is gone")


def apply_patch(docs: dict[str, Any], patch: dict) -> None:
    """Apply one patch to ``docs`` (``{"glossary": ..., "style": ..., "address_map": ...}``).

    Patches that edit no artifact (dismissals, follow-ups, proposals) are no-ops here.
    """
    name = artifact_of(patch["type"])
    if name is None:
        return
    if docs.get(name) is None:
        raise ValueError(f"the book has no {name.replace('_', ' ')}")
    {"glossary": apply_glossary, "style": apply_style,
     "address_map": apply_address}[name](docs[name], patch)


# --- freshness -----------------------------------------------------------------
#
# A recommendation is checked against the book again before it is picked or
# applied. A patch that quotes the text it replaces checks itself: replaying it
# fails once that text is gone. A patch that edits a glossary entry or an
# address pair by name would silently overwrite a later change to it, so commit
# stamps the state of that target and it must still match. Edits elsewhere in
# the same file leave the recommendation pickable.

def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def target_state(docs: dict[str, Any], patch: dict) -> Optional[str]:
    """The state of what ``patch`` edits by name, hashed; None when replaying it is the check."""
    kind = patch["type"]
    if kind.startswith("glossary."):
        terms = (docs.get("glossary") or {}).get("terms") or []
        keys = [patch["english"], patch.get("new_english")]
        keys += [e["english"] for e in patch.get("entries") or []]
        return _digest([find_entry(terms, k) for k in keys if k])
    if kind in ("address.set_direction", "address.add_pair"):
        wanted = {patch["a"], patch["b"]}
        pairs = (docs.get("address_map") or {}).get("pairs") or []
        return _digest([p for p in pairs if isinstance(p, dict) and {p.get("a"), p.get("b")} == wanted])
    return None


def _target_name(patch: dict) -> str:
    if patch["type"].startswith("glossary."):
        return f"the glossary entry {patch['english']!r}"
    return f"the address pair {patch['a']}/{patch['b']}"


def stamp(docs: dict[str, Any], patches: list[dict]) -> list[Optional[str]]:
    """Each patch's :func:`target_state`, replaying the patches in order on a copy."""
    work = copy.deepcopy(docs)
    out = []
    for patch in patches:
        out.append(target_state(work, patch))
        apply_patch(work, patch)
    return out


def stale_changes(docs: dict[str, Any], patches: list[dict],
                  stamps: list[Optional[str]]) -> list[str]:
    """Why each patch no longer fits the book as it is now; empty when all of them do."""
    work = copy.deepcopy(docs)
    problems = []
    for n, patch in enumerate(patches, 1):
        stamped = stamps[n - 1] if n <= len(stamps) else None
        if stamped is not None and target_state(work, patch) != stamped:
            problems.append(f"change {n}: {_target_name(patch)} changed since the review")
            continue
        try:
            apply_patch(work, patch)
        except ValueError as exc:
            problems.append(f"change {n}: {exc}")
    return problems
