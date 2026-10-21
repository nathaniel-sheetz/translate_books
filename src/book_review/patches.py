"""Pure patch functions: apply one validated patch to an artifact document in memory.

Shared by :mod:`src.book_review.schema`, which replays a recommendation's
patches in order so each is validated against the result of the ones before it
(a rekey followed by a variant of the new key), and by
:mod:`src.book_review.apply`, which applies them for real.
"""

from __future__ import annotations

import copy
from typing import Optional


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
