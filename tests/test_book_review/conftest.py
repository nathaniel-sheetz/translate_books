"""A small book on disk with judge findings, marks and a logged translation prompt."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def write_json(path: Path, doc: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def term(english: str, spanish: str, **extra) -> dict:
    return {"english": english, "spanish": spanish, "type": "other", **extra}


def pair(a: str, b: str, ab: str, ba: str) -> dict:
    return {"a": a, "b": b, "directions": {
        "a_to_b": [{"form": ab, "when": "default"}],
        "b_to_a": [{"form": ba, "when": "default"}],
    }}


def issue(rule: str, message: str, location: str = "", suggestion: str = "", *,
          category: Optional[str] = None, finding_key: Optional[str] = None) -> dict:
    out = {"severity": "error", "message": f"[{rule}] {message}", "location": location,
           "suggestion": suggestion, "rule_id": rule, "category": category}
    if finding_key:
        out["finding_key"] = finding_key
    return out


def logged_prompt(keys: list[tuple[str, str]]) -> str:
    """A translation prompt whose GLOSSARY TERMS block carries these entries."""
    lines = "\n".join(f"- {en} → {es}" for en, es in keys)
    return (
        "=" * 80 + "\nGLOSSARY TERMS\n" + "=" * 80 + "\n\n"
        "Use these translations:\n\nOTHER TERMS:\n" + lines + "\n\n"
        + "=" * 80 + "\nSTYLE GUIDE\n" + "=" * 80 + "\nBe nice.\n"
    )


class Book:
    """Builder for ``projects/<slug>/`` under a tmp dir."""

    def __init__(self, root: Path, slug: str = "book"):
        self.dir = root / slug
        self.dir.mkdir(parents=True, exist_ok=True)
        self.repo = root / "repo"
        (self.repo / "prompts" / "history").mkdir(parents=True, exist_ok=True)
        self._feedback: list[dict] = []

    def glossary(self, terms: list[dict]) -> "Book":
        write_json(self.dir / "glossary.json", {"terms": terms, "version": "1.0"})
        return self

    def style(self, content: str, *, updated_at: Optional[str] = None) -> "Book":
        doc = {"content": content, "version": "1.0"}
        if updated_at:
            doc["updated_at"] = updated_at
        write_json(self.dir / "style.json", doc)
        return self

    def address_map(self, pairs: list[dict], global_rules: str = "", content: str = "map") -> "Book":
        write_json(self.dir / "address_map.json", {
            "content": content, "pairs": pairs, "global_rules": global_rules,
        })
        return self

    def chunk(self, chunk_id: str, source: str, translation: str = "texto",
              *, shipped: Optional[list[tuple[str, str]]] = None) -> "Book":
        log = None
        if shipped is not None:
            log_path = self.repo / "prompts" / "history" / f"{chunk_id}.json"
            write_json(log_path, {"metadata": {}, "prompt": logged_prompt(shipped), "response": ""})
            log = str(log_path)
        write_json(self.dir / "chunks" / f"{chunk_id}.json", {
            "id": chunk_id, "chapter_id": chunk_id.split("_chunk")[0], "position": 0,
            "source_text": source, "translated_text": translation,
            "translated_at": "2026-07-01T00:00:00", "last_llm_log": log,
            "status": "translated",
            "metadata": {"char_start": 0, "char_end": max(1, len(source)), "overlap_start": 0,
                         "overlap_end": 0, "paragraph_count": 1, "word_count": len(source.split())},
        })
        return self

    def judge(self, chunk_id: str, judge: str, issues: list[dict], *,
              retracted: Optional[list[dict]] = None, filtered: Optional[list[dict]] = None,
              prompt_version: Optional[str] = None) -> "Book":
        path = self.dir / "evaluations" / f"{chunk_id}.json"
        doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "chunk_id": chunk_id, "judges": {},
        }
        metadata: dict[str, Any] = {}
        if retracted:
            metadata["retracted"] = retracted
        if filtered:
            metadata["filtered"] = filtered
        if prompt_version:
            metadata["prompt_version"] = prompt_version
        doc["judges"][judge] = {
            "eval_name": judge, "issues": issues, "metadata": metadata,
            "executed_at": "2026-09-01T00:00:00",
        }
        write_json(path, doc)
        return self

    def mark(self, chunk_id: str, judge: str, issue_doc: dict, feedback_type: str) -> "Book":
        from web_ui.evaluations import issue_key

        self._feedback.append({
            "ts": "2026-09-02T00:00:00", "chunk_id": chunk_id, "eval_name": judge,
            "issue_index": 0, "issue_key": issue_key(judge, issue_doc),
            "feedback_type": feedback_type,
        })
        path = self.dir / "evaluations" / "_feedback.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in self._feedback) + "\n",
                        encoding="utf-8")
        return self

    def load(self):
        from src.book_review.corpus import load_book

        return load_book(self.dir, repo_root=self.repo)
