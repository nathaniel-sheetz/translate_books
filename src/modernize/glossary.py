"""The glossary beat for a modernize book.

A translation book's candidates come from ``scripts/extract_glossary_candidates.py``,
which reads English: stopwords, an English dictionary, literary-English
frequency. On an old Spanish source every ordinary word would look like a
candidate. A modernize book drafts its candidates from what the coded
dictionary checker flags in the spelling-fixed source instead: the source is
already in the target language, so what the checker would flag there is what
it will flag in the modernized text, and what an entry has to cover.

That list holds more than names. It also holds the source's OCR and accent
slips, which no entry should cover, so the drafting prompt sorts every
candidate into an entry or a defect (:data:`DEFECTS_DRAFT`).

The glossary's word test (:meth:`src.models.Glossary.matches_word`) ignores
accents. An entry «Zacarías» also silences a bare «Zacarias» that the source
carries by mistake, so :func:`accent_slip_warnings` names every flagged form
that differs from an entry only by accents.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Callable, Iterable, Optional

CANDIDATES_FILE = "glossary_candidates.json"
DEFECTS_DRAFT = "glossary_defects_draft.json"
DEFECT_KINDS = ("ocr", "accent", "modernizer", "other")
_W = r"[^\W\d_]"
_EXCERPTS = 2
_WINDOW = 70


def fold(s: str) -> str:
    """Lowercase, accents removed: the comparison ``matches_word`` makes."""
    nfd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").casefold()


def _excerpts(text: str, form: str, limit: int = _EXCERPTS) -> list[str]:
    out: list[str] = []
    pattern = re.compile(rf"(?<!{_W}){re.escape(form)}(?!{_W})")
    for m in pattern.finditer(text):
        start, end = max(0, m.start() - _WINDOW), min(len(text), m.end() + _WINDOW)
        # Widen to whole words so an excerpt never opens or closes mid-word.
        while start > 0 and not text[start - 1].isspace():
            start -= 1
        while end < len(text) and not text[end].isspace():
            end += 1
        out.append(re.sub(r"\s+", " ", text[start:end]).strip())
        if len(out) >= limit:
            break
    return out


def candidates(
    text: str,
    *,
    max_candidates: int = 500,
    classify: Optional[Callable[[str], dict]] = None,
) -> list[dict]:
    """Glossary candidates for a modernize book, most frequent first.

    ``text`` is the spelling-fixed source. ``classify`` is
    :meth:`DictionaryEvaluator.classify_words` unless a test passes its own.
    Forms that differ only by case are one candidate («DROSSELMAYER» is
    «Drosselmayer»), named by its most frequent form, with capitals preferred
    to all caps. Forms that differ by accents stay separate and each is
    marked with the others in ``accent_pair``.
    """
    if classify is None:
        from src.evaluators.dictionary_eval import DictionaryEvaluator
        classify = DictionaryEvaluator().classify_words
    flagged = classify(text)

    groups: dict[str, dict] = {}
    for word, info in flagged.items():
        g = groups.setdefault(word.casefold(), {"forms": {}, "kinds": set()})
        g["forms"][word] = info["count"]
        g["kinds"].add(info["kind"])

    by_fold: dict[str, list[str]] = {}
    for key in groups:
        by_fold.setdefault(fold(key), []).append(key)

    out: list[dict] = []
    for key, g in groups.items():
        forms = g["forms"]
        term = max(forms, key=lambda f: (forms[f], not f.isupper(), f))
        pair = sorted(
            max(groups[k]["forms"], key=groups[k]["forms"].get)
            for k in by_fold[fold(key)] if k != key
        )
        frequency = sum(forms.values())
        out.append({
            "term": term,
            "type_guess": "other",
            "frequency": frequency,
            "score": float(frequency),
            "context_sentence": "",
            "detection_reasons": [f"dictionary_{k}" for k in sorted(g["kinds"])],
            "forms": dict(sorted(forms.items())),
            "accent_pair": [f"{p} ×{sum(groups[p.casefold()]['forms'].values())}" for p in pair],
            "contexts": [("excerpt", e) for e in _excerpts(text, term)],
        })
    out.sort(key=lambda c: (-c["frequency"], c["term"].casefold()))
    return out[:max_candidates]


def format_candidates(cands: Iterable[dict]) -> str:
    """The numbered candidate list the modernize glossary prompt shows."""
    lines: list[str] = []
    for i, c in enumerate(cands, 1):
        kinds = ", ".join(r.removeprefix("dictionary_") for r in c.get("detection_reasons") or [])
        head = f"{i}. {c['term']}  [×{c.get('frequency', '?')}; flagged as {kinds or '?'}]"
        lines.append(head)
        other_case = [f for f in (c.get("forms") or {}) if f != c["term"]]
        if other_case:
            lines.append(f"   also written: {', '.join(other_case)}")
        if c.get("accent_pair"):
            lines.append(f"   SAME WORD, OTHER ACCENTS: {', '.join(c['accent_pair'])}")
        for label, snippet in c.get("contexts") or []:
            lines.append(f'   {label}: "{snippet}"')
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def save_candidates(hdir: Path, cands: list[dict]) -> Path:
    """Keep the candidates for commit's accent-slip check."""
    path = hdir / CANDIDATES_FILE
    rows = [{"term": c["term"], "frequency": c["frequency"], "forms": c.get("forms") or {}}
            for c in cands]
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_candidates(hdir: Path) -> list[dict]:
    path = hdir / CANDIDATES_FILE
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return rows if isinstance(rows, list) else []


def accent_slip_warnings(terms: Iterable, cands: list[dict]) -> list[str]:
    """``REVIEW:`` lines for flagged forms an entry would hide by accents alone.

    ``terms`` are committed :class:`GlossaryTerm` objects. A flagged form whose
    fold equals one of an entry's forms (key, rendering, alternatives,
    aliases) but which is spelled differently is a slip the entry would
    silence in every finding from here on.
    """
    lines: list[str] = []
    for t in terms:
        forms = [t.english, t.spanish, *(t.alternatives or []), *(t.aliases or [])]
        exact = {f.casefold() for f in forms if f}
        folds = {fold(f) for f in forms if f}
        for c in cands:
            for form, count in (c.get("forms") or {}).items():
                if fold(form) in folds and form.casefold() not in exact:
                    lines.append(
                        f"REVIEW: «{form}» (×{count} in the source) differs from the entry "
                        f"«{t.english}» only by accents. Glossary matches ignore accents, so "
                        f"this entry hides it from the dictionary checker. Fix it in the "
                        f"source (spelling pass or OCR fixes) unless both spellings are right."
                    )
    return lines


def load_defects(hdir: Path) -> tuple[list[dict], list[str]]:
    """The drafted defect list and any problems with it (never raises)."""
    path = hdir / DEFECTS_DRAFT
    if not path.exists():
        return [], []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [], [f"{DEFECTS_DRAFT} is unreadable ({exc}); re-draft it as a JSON array."]
    if not isinstance(rows, list):
        return [], [f"{DEFECTS_DRAFT} is not a JSON array; re-draft it."]
    defects: list[dict] = []
    problems: list[str] = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict) or not str(r.get("form") or "").strip():
            problems.append(f"{DEFECTS_DRAFT} entry {i} has no 'form'; it was left out.")
            continue
        kind = str(r.get("kind") or "other").strip().lower()
        defects.append({
            "form": str(r["form"]).strip(),
            "fix": str(r.get("fix") or "").strip(),
            "kind": kind if kind in DEFECT_KINDS else "other",
            "why": str(r.get("why") or "").strip(),
        })
    return defects, problems
