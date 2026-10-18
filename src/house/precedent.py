"""
The precedent index: what a book's siblings already decided.

Derived, never authored. Each sibling in a group (:mod:`src.house.groups`)
contributes three things, limited to what the new book's source can use:

- **style answers** from its ``.harness/style_answers.json``, normalised to
  option ids, so "3 of 4 horse books: keep imperial units" is a count;
- **address pairs** from its ``address_map.json`` for characters whose names
  occur in the new source, plus its ``global_rules`` (series siblings only:
  two books in a domain do not share a cast);
- **glossary entries** whose English occurs in the new source, plus a
  concordance over its committed ``translated_text``: how often each rendering
  appears where the English word does (stall → casilla 32 of 33).

The index is cached at ``projects/<slug>/.harness/precedent.json``. It is a
snapshot taken when the book's drafting starts: a later change to a sibling
does not rewrite this book's drafts, and ``scripts/house_check.py`` reports the
drift instead.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.house import book_slug, project_dir_for, read_body
from src.house.groups import SERIES, siblings
from src.house.match import count_word, word_occurs

logger = logging.getLogger(__name__)

CACHE_NAME = "precedent.json"

#: Bumped when the snapshot's shape or matching changes, so an older cache is rebuilt.
SCHEMA = 2

#: Leading articles stripped before renderings are compared or counted, so a
#: glossary's "el venado" and another's "venado" are the same rendering.
_ARTICLE = re.compile(r"^(?:el|la|los|las|un|una|unos|unas)\s+", re.I)


def bare(rendering: str) -> str:
    return _ARTICLE.sub("", (rendering or "").strip())


def _norm(rendering: str) -> str:
    return bare(rendering).casefold()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def cache_path(project_dir: Path) -> Path:
    return Path(project_dir) / ".harness" / CACHE_NAME


# ---------------------------------------------------------------------------
# style answers
# ---------------------------------------------------------------------------

def _question_defs(sibling_dir: Path) -> dict[str, dict]:
    """The sibling's own question set (follow-ups included), else the shipped config."""
    defs: dict[str, dict] = {}
    own = _read_json(sibling_dir / ".harness" / "style_questions.json")
    if isinstance(own, list):
        defs.update({q["id"]: q for q in own if isinstance(q, dict) and q.get("id")})
    try:
        from src.style_guide_wizard import load_question_config

        config = load_question_config()
        for q in [*config.get("fixed", []), *config.get("conditional", [])]:
            defs.setdefault(q["id"], q)
    except Exception as exc:  # noqa: BLE001 - precedent is a nicety
        logger.warning("Could not load the style question config: %s", exc)
    return defs


def normalize_answer(question: Optional[dict], answer: Any) -> tuple[str, str]:
    """``(answer key, label)``: the option id when the answer names an option, else the text."""
    if question is None:
        text = str(answer).strip()
        return text, text
    from src.style_guide_wizard import option_ids, resolve_answer

    label, _effect, matched = resolve_answer(question, answer)
    if matched:
        labels = [o.get("label", "") for o in question.get("options", [])]
        if label in labels:
            return option_ids(question)[labels.index(label)], label
    return str(label).strip(), str(label).strip()


def _sibling_style(sibling_dir: Path) -> dict[str, dict[str, str]]:
    answers = _read_json(sibling_dir / ".harness" / "style_answers.json")
    if not isinstance(answers, dict):
        return {}
    defs = _question_defs(sibling_dir)
    out: dict[str, dict[str, str]] = {}
    for qid, raw in answers.items():
        key, label = normalize_answer(defs.get(qid), raw)
        if key:
            out[qid] = {"answer": key, "label": label}
    return out


# ---------------------------------------------------------------------------
# address pairs
# ---------------------------------------------------------------------------

#: Words that open a name without identifying the character ("Chief Collig",
#: "Sra. Hardy"): the name's own word is the one after them.
_TITLES = frozenset(
    w.casefold() for w in (
        "Sr.", "Sra.", "Srta.", "Dr.", "Dra.", "Mr.", "Mrs.", "Miss", "Chief", "Jefe",
        "Detective", "Officer", "Oficial", "Captain", "Capitán", "Aunt", "Uncle", "Tía",
        "Tío", "Señor", "Señora", "Señorita", "Don", "Doña", "Doctor", "Professor",
    )
)


def _name_forms(name: str, spanish_to_english: dict[str, list[str]]) -> list[str]:
    """The full name, its English glossary forms, and its identifying word.

    The identifying word is the first word after any title: "Frank" for Frank
    Hardy, "Collig" for Chief Collig.
    """
    forms = [name, *spanish_to_english.get(name.casefold(), [])]
    words = [w for w in name.split() if w.casefold() not in _TITLES]
    if words and len(name.split()) > 1:
        forms.append(words[0])
    return [f.strip() for f in forms if len(f.strip()) > 2]


def _present(name: str, source: str, spanish_to_english: dict[str, list[str]]) -> bool:
    """Whether a character's name occurs in ``source``, as a whole capitalised word.

    Case-sensitive on purpose: a name matched like a glossary term would find
    "Red" Hawkins in every "red".
    """
    return any(
        re.search(r"(?<![^\W\d_])" + re.escape(f) + r"(?![^\W\d_])", source)
        for f in _name_forms(name, spanish_to_english)
    )


def _sibling_address(
    sibling_dir: Path, source: str, glossary: list[dict]
) -> tuple[list[dict], str]:
    amap = _read_json(sibling_dir / "address_map.json")
    if not isinstance(amap, dict):
        return [], ""
    spanish_to_english: dict[str, list[str]] = {}
    for term in glossary:
        spanish_to_english.setdefault(bare(term.get("spanish", "")).casefold(), []).append(
            term.get("english", "")
        )
    pairs = [
        p for p in amap.get("pairs") or []
        if isinstance(p, dict)
        and _present(str(p.get("a") or ""), source, spanish_to_english)
        and _present(str(p.get("b") or ""), source, spanish_to_english)
    ]
    return pairs, str(amap.get("global_rules") or "").strip()


def default_form(pair: dict, direction: str) -> Optional[str]:
    """A pair's ``when="default"`` form in one direction, or ``None``."""
    for rule in (pair.get("directions") or {}).get(direction) or []:
        if isinstance(rule, dict) and (rule.get("when") or "default") == "default":
            return rule.get("form")
    return None


def pair_key(a: str, b: str) -> tuple[str, str]:
    return (a.strip().casefold(), b.strip().casefold())


def _oriented(pair: dict) -> tuple[tuple[str, str], dict[str, Optional[str]]]:
    """An order-free key for a pair and its default forms keyed by speaker → hearer."""
    a, b = str(pair.get("a") or ""), str(pair.get("b") or "")
    forms = {
        f"{a} → {b}": default_form(pair, "a_to_b"),
        f"{b} → {a}": default_form(pair, "b_to_a"),
    }
    return tuple(sorted(pair_key(a, b))), forms  # type: ignore[return-value]


def address_divergence(
    pairs: list[dict], against: list[dict], *, label: str = "from"
) -> list[dict[str, Any]]:
    """Directions where ``pairs`` and ``against`` give different default forms.

    Pairs match whatever order their two names are in. Each row is
    ``{pair, direction, form, other_form, other}`` where ``other`` is the
    ``against`` pair's ``label`` field (the sibling it came from).
    """
    index: dict[tuple[str, str], list[tuple[dict, dict]]] = {}
    for p in against:
        key, forms = _oriented(p)
        index.setdefault(key, []).append((p, forms))
    rows = []
    for p in pairs:
        key, forms = _oriented(p)
        for other, other_forms in index.get(key, []):
            for direction, form in forms.items():
                theirs = next(
                    (f for d, f in other_forms.items() if d.casefold() == direction.casefold()),
                    None,
                )
                if form and theirs and form != theirs:
                    rows.append({
                        "pair": f"{p.get('a')} / {p.get('b')}",
                        "direction": direction,
                        "form": form,
                        "other_form": theirs,
                        "other": other.get(label),
                    })
    return rows


def series_pairs(precedent: dict) -> tuple[list[dict], list[dict[str, Any]]]:
    """``(pairs, divergence)``: one pair per character pair, and where siblings disagree.

    The kept pair is the first sibling's (siblings are ordered series first,
    then by slug); the divergence lists every later sibling that differs.
    """
    kept: dict[tuple[str, str], dict] = {}
    divergence: list[dict[str, Any]] = []
    for p in precedent.get("address", {}).get("pairs") or []:
        key, _ = _oriented(p)
        if key not in kept:
            kept[key] = p
            continue
        for row in address_divergence([p], [kept[key]]):
            divergence.append({**row, "pair_from": p.get("from")})
    return list(kept.values()), divergence


# ---------------------------------------------------------------------------
# glossary and concordance
# ---------------------------------------------------------------------------

def _sibling_chunks(sibling_dir: Path) -> list[tuple[str, str]]:
    """``(source, translation)`` for every committed chunk of a sibling."""
    rows: list[tuple[str, str]] = []
    for path in sorted((sibling_dir / "chunks").glob("*.json")):
        doc = _read_json(path)
        if isinstance(doc, dict) and doc.get("translated_text"):
            rows.append((str(doc.get("source_text") or ""), str(doc["translated_text"])))
    return rows


def sibling_chunks(
    slug: str, sibling_dir: Path, memo: Optional[dict[str, list]] = None
) -> list[tuple[str, str]]:
    """:func:`_sibling_chunks`, read once per ``memo`` (keyed by slug)."""
    if memo is None:
        return _sibling_chunks(sibling_dir)
    if slug not in memo:
        memo[slug] = _sibling_chunks(sibling_dir)
    return memo[slug]


def concordance(
    english: str, forms: list[str], chunks: list[tuple[str, str]]
) -> Optional[dict[str, Any]]:
    """How often each Spanish form appears where ``english`` occurs, or ``None``.

    Counted chunk by chunk: the English word's occurrences in each chunk that
    has it, and each form's occurrences in that chunk's translation. A form is
    counted bare (no article) with its plural, so "las casillas" counts as
    casilla. ``None`` when the English never occurs in the committed text.

    ``example`` is the first English sentence that uses the word, so a reader
    can see which sense the sibling meant (a boat's wheel, not a car's).
    """
    english_n = 0
    example = ""
    counts: Counter = Counter()
    for source, translation in chunks:
        n = count_word(english, source)
        if not n:
            continue
        english_n += n
        if not example:
            example = example_sentence(english, source)
        for form in forms:
            counts[form] += count_word(form, translation)
    if not english_n:
        return None
    return {
        "english": english_n,
        "forms": {f: counts[f] for f in forms if counts[f]},
        "example": example,
    }


_EXAMPLE_MAX = 90


def example_sentence(word: str, text: str) -> str:
    """The sentence around ``word``'s first whole-word use, at most ~90 characters."""
    m = re.search(r"(?<![^\W\d_])" + re.escape(word) + r"(?:e?s)?(?![^\W\d_])", text, re.IGNORECASE)
    if not m:
        return ""
    start = max(text.rfind(c, 0, m.start()) for c in ".!?\n") + 1
    ends = [i for i in (text.find(c, m.end()) for c in ".!?\n") if i != -1]
    end = min(ends) + 1 if ends else len(text)
    sentence = " ".join(text[start:end].split()).strip(' "“”')
    if len(sentence) <= _EXAMPLE_MAX:
        return sentence
    # Too long: a window centred on the word.
    pos = sentence.lower().find(m.group(0).lower())
    lo = max(0, pos - _EXAMPLE_MAX // 2)
    window = sentence[lo:lo + _EXAMPLE_MAX].strip()
    return ("…" if lo else "") + window + ("…" if lo + _EXAMPLE_MAX < len(sentence) else "")


def example_of(entry: dict[str, Any]) -> str:
    """The first sibling example sentence in an index entry's concordance, or ``""``."""
    for c in (entry.get("concordance") or {}).values():
        if c.get("example"):
            return c["example"]
    return ""


def committed_uses(entry: dict[str, Any]) -> Optional[int]:
    """Uses of any rendering in the siblings' committed text, or ``None`` when
    no sibling's committed text has the English word (nothing to judge by)."""
    concord = entry.get("concordance") or {}
    if not concord:
        return None
    return sum(n for c in concord.values() for n in c["forms"].values())


# ---------------------------------------------------------------------------
# build / load
# ---------------------------------------------------------------------------

def build_precedent(
    project_dir: Path, *, write: bool = True, chunk_memo: Optional[dict[str, list]] = None
) -> dict[str, Any]:
    """Build the index from the book's siblings, and cache it unless ``write`` is off.

    ``chunk_memo`` (see :func:`sibling_chunks`) lets a caller that reads the
    siblings' chunks again afterwards read them once.
    """
    from src.house.lexicon import entries_for
    from src.house.groups import group_ids_of

    project_dir = Path(project_dir)
    slug = book_slug(project_dir)
    source = read_body(project_dir) or ""
    sibs = siblings(slug)

    style: dict[str, list[dict[str, str]]] = {}
    pairs: list[dict[str, Any]] = []
    global_rules: list[dict[str, str]] = []
    glossary: dict[str, dict[str, Any]] = {}
    chunks_by_sibling: dict[str, list[tuple[str, str]]] = {}

    for sib in sibs:
        sdir = project_dir_for(sib["slug"])
        if sdir is None:
            continue
        for qid, ans in _sibling_style(sdir).items():
            style.setdefault(qid, []).append({**ans, "slug": sib["slug"], "kind": sib["kind"]})

        doc = _read_json(sdir / "glossary.json")
        terms = [t for t in (doc or {}).get("terms") or [] if isinstance(t, dict)] if isinstance(doc, dict) else []
        for term in terms:
            english = str(term.get("english") or "").strip()
            spanish = str(term.get("spanish") or "").strip()
            if not english or not spanish or not word_occurs(english, source):
                continue
            # A domain shares vocabulary, not a cast or a map: its characters'
            # and places' names are not precedent for this book.
            if sib["kind"] != SERIES and str(term.get("type") or "") in ("character", "place"):
                continue
            entry = glossary.setdefault(english.casefold(), {
                "english": english, "type": term.get("type") or "other", "renderings": [],
            })
            entry["renderings"].append({
                "spanish": spanish,
                "alternatives": [a for a in term.get("alternatives") or [] if a],
                "slug": sib["slug"],
                "kind": sib["kind"],
            })

        if sib["kind"] == SERIES:
            sib_pairs, rules = _sibling_address(sdir, source, terms)
            pairs += [{**p, "from": sib["slug"]} for p in sib_pairs]
            if rules:
                global_rules.append({"from": sib["slug"], "text": rules})

        chunks_by_sibling[sib["slug"]] = sibling_chunks(sib["slug"], sdir, chunk_memo)

    # Concordance for every term a sibling glossary or the lexicon brings to
    # this book, over every sibling's committed text.
    lexicon = entries_for(group_ids_of(slug), source)
    for entry in lexicon:
        if entry["spanish"]:
            glossary.setdefault(entry["english"].casefold(), {
                "english": entry["english"], "type": entry["type_guess"], "renderings": [],
            })
    for key, entry in glossary.items():
        forms = sorted({
            bare(f)
            for r in entry["renderings"]
            for f in [r["spanish"], *r["alternatives"]]
            if bare(f)
        } | {bare(e["spanish"]) for e in lexicon if e["spanish"] and e["english"].casefold() == key})
        entry["concordance"] = {
            s: c for s, rows in chunks_by_sibling.items()
            if (c := concordance(entry["english"], forms, rows))
        }
        entry["divergent"] = len({_norm(r["spanish"]) for r in entry["renderings"]}) > 1

    doc = {
        "schema": SCHEMA,
        "project": slug,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "siblings": sibs,
        "style": style,
        "address": {"pairs": pairs, "global_rules": global_rules},
        "glossary": dict(sorted(glossary.items())),
    }
    if write:
        path = cache_path(project_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return doc


def cached_precedent(project_dir: Path) -> Optional[dict[str, Any]]:
    """The book's precedent snapshot, or ``None`` when it has none (or an old schema)."""
    cached = _read_json(cache_path(project_dir))
    return cached if isinstance(cached, dict) and cached.get("schema") == SCHEMA else None


def load_precedent(project_dir: Path, *, rebuild: bool = False) -> dict[str, Any]:
    """The cached snapshot, built on first use (or when ``rebuild``)."""
    if not rebuild:
        cached = cached_precedent(project_dir)
        if cached is not None:
            return cached
    return build_precedent(project_dir)


# ---------------------------------------------------------------------------
# summaries the drafting steps read
# ---------------------------------------------------------------------------

def style_votes(precedent: dict, qid: str, kind: Optional[str] = None) -> list[dict[str, Any]]:
    """``[{answer, label, slugs}]`` for one question, most-chosen first."""
    rows = [r for r in precedent.get("style", {}).get(qid, []) if kind is None or r["kind"] == kind]
    grouped: dict[str, dict[str, Any]] = {}
    for r in rows:
        g = grouped.setdefault(r["answer"], {"answer": r["answer"], "label": r["label"], "slugs": []})
        g["slugs"].append(r["slug"])
    return sorted(grouped.values(), key=lambda g: (-len(g["slugs"]), g["answer"]))


def leading_rendering(entry: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The rendering most siblings chose, weighted by committed concordance on a tie."""
    if not entry.get("renderings"):
        return None
    tally: dict[str, dict[str, Any]] = {}
    for r in entry["renderings"]:
        g = tally.setdefault(_norm(r["spanish"]), {
            "spanish": bare(r["spanish"]), "alternatives": [], "slugs": [],
        })
        g["slugs"].append(r["slug"])
        g["alternatives"] += [a for a in r["alternatives"] if a not in g["alternatives"]]
    uses = Counter()
    for counts in (entry.get("concordance") or {}).values():
        for form, n in counts["forms"].items():
            uses[_norm(form)] += n
    return max(tally.values(), key=lambda g: (len(g["slugs"]), uses[_norm(g["spanish"])]))


def format_concordance(entry: dict[str, Any]) -> str:
    """``stormy-misty-s-foal: stall ×33 → casilla ×32`` per sibling, joined by '; '.

    Counts are occurrences in the chunks that contain the English word, so a
    form can outnumber it (the translation repeats a noun the English left as
    "it"); read them as evidence of use, not as a ratio.
    """
    parts = []
    for slug, c in (entry.get("concordance") or {}).items():
        forms = ", ".join(
            f"{f} ×{n}" for f, n in sorted(c["forms"].items(), key=lambda x: -x[1])
        )
        parts.append(f"{slug}: {entry['english']} ×{c['english']} → {forms or 'none of these forms'}")
    return "; ".join(parts)
