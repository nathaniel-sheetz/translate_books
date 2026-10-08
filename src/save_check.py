"""
Save-time check, rule layer: warn about an obvious slip in a sentence the
translator has just saved.

It looks only at what the edit introduced -- a word that was not in the
sentence before, a punctuation mark the edit left unbalanced -- which is what
keeps it quiet. The same dictionary test run over whole chunks is right about
one time in ten; restricted to the words an edit typed, and with the
let-throughs in :func:`spelling_hits`, it flagged under 1% of 1,696 clean saves
and more than half of its warnings were real slips
(``docs/design/local-inference-save-check.md``).

Nothing here blocks a save. :func:`check_write` returns hits, the caller logs
them with :func:`append_warning`, and the reader shows them on the sentence
until it is edited again, dismissed, or the word goes on the book's ignore list.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Optional

from src.models import Glossary, IgnoredTerms

logger = logging.getLogger(__name__)

LOG_NAME = "save_checks.jsonl"

RULES = (
    "spelling",
    "unbalanced",
    "comma_before_paren",
    "period_before_raya",
    "raya_closes_guillemet",
    "closing_mark_dropped",
    "doubled_mark",
    "space_before_mark",
    "no_space_after_period",
    "repeated_word",
)

_LETTERS = "A-Za-zÁÉÍÓÚÜÑáéíóúüñ"
_WORD = re.compile(rf"[{_LETTERS}]+(?:['’][{_LETTERS}]+)?")
# « » is left out: a speech that runs on opens its next paragraph with a lone ».
_PAIRS = (("¿", "?"), ("¡", "!"), ("(", ")"), ("‹", "›"), ("[", "]"))
_TAG = re.compile(r"\[(?:CAPTION|IMAGE:[^\]]*)\]")
_DIALECT = re.compile(r"['’]\w+|\w+['’]")
_GLOSSARY_TOKEN = re.compile(r"[^\W\d_]+")
_ACCENTS = str.maketrans("áéíóúÁÉÍÓÚ", "aeiouAEIOU")


def _words(text: str) -> list[str]:
    return _WORD.findall(text)


def introduced_words(before: str, after: str) -> list[str]:
    """Words the edit typed: in the saved sentence, absent (as spelt) before it."""
    before, after = _TAG.sub(" ", before), _TAG.sub(" ", after)
    after = _DIALECT.sub(" ", after)  # written dialect: 'ntequilla, pa'
    seen = set(_words(before))
    return [w for w in dict.fromkeys(_words(after)) if w not in seen and len(w) > 2]


class Speller:
    """The production dictionary checker's word test, applied to single words."""

    def __init__(self) -> None:
        from src.evaluators.dictionary_eval import DictionaryEvaluator

        self.ev = DictionaryEvaluator()

    def is_spanish(self, word: str) -> bool:
        return self.ev._is_special_case(word) or self.ev._check_spanish_word(word)

    def unknown(self, word: str, english: str) -> bool:
        if "'" in word or "’" in word:
            return False  # written dialect
        if self.is_spanish(word):
            return False
        # A name or a word kept from the source is spelt as the English has it.
        if re.search(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])", english, re.I):
            return False
        return True


_speller: Optional[Speller] = None
_speller_failed = False
_speller_lock = threading.Lock()


def default_speller() -> Optional[Speller]:
    """The shared :class:`Speller`, or ``None`` where Enchant is not installed."""
    global _speller, _speller_failed
    if _speller is None and not _speller_failed:
        with _speller_lock:
            if _speller is None and not _speller_failed:
                try:
                    _speller = Speller()
                except Exception as e:
                    _speller_failed = True
                    logger.warning("Save check runs without its spelling rule: %s", e)
    return _speller


def glossary_words(glossary: Optional[Glossary]) -> frozenset[str]:
    """Lower-cased tokens of every Spanish form the glossary lists.

    Matched exactly, accents included. :meth:`Glossary.matches_word` folds
    accents, which would let ``Diaz`` through against a glossary ``Díaz`` -- and
    a dropped accent on a name is one of the slips this check exists to catch.
    """
    if glossary is None:
        return frozenset()
    out: set[str] = set()
    for term in glossary.terms:
        for form in [term.spanish, *term.alternatives]:
            out.update(t.lower() for t in _GLOSSARY_TOKEN.findall(form or ""))
    return frozenset(out)


def _mid_sentence_capital(word: str, text: str) -> bool:
    """True when ``word`` is capitalised and does not open a sentence in ``text``."""
    if not word[0].isupper():
        return False
    m = re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text)
    if not m:
        return False
    lead = text[:m.start()].rstrip(' —«¿¡"“_(*')
    return bool(lead) and lead[-1] not in ".!?…:»”\n"


def _near_known_word(word: str, vocabulary: Mapping[str, int], speller: Speller) -> bool:
    """Is an accent variant or an adjacent-letter swap of ``word`` a known word?"""
    base = word.translate(_ACCENTS)
    candidates = {base}
    for i, ch in enumerate(base):
        if ch.lower() in "aeiou":
            accented = "áéíóú"["aeiou".index(ch.lower())]
            candidates.add(base[:i] + (accented.upper() if ch.isupper() else accented) + base[i + 1:])
    for i in range(len(word) - 1):
        candidates.add(word[:i] + word[i + 1] + word[i] + word[i + 2:])
    for candidate in candidates:
        if candidate.lower() == word.lower():
            continue
        if vocabulary.get(candidate.lower(), 0) > 0 or speller.ev._check_spanish_word(candidate):
            return True
    return False


def spelling_hits(
    en: str,
    before: str,
    after: str,
    vocabulary: Mapping[str, int],
    glossary: frozenset[str] = frozenset(),
    ignored: Optional[IgnoredTerms] = None,
    speller: Optional[Speller] = None,
) -> list[str]:
    """Introduced words that look misspelt.

    A word is let through when the book already uses it in any letter case,
    when the dictionary or the English sentence has it, when the glossary lists
    it as a Spanish form, or when it is on the book's ignore list. A
    capitalised word in mid-sentence is taken for a name unless it is one
    accent or one swapped pair of letters away from a known word (``Diaz``,
    ``Comapñía``).
    """
    if speller is None:
        return []
    out = []
    for word in introduced_words(before, after):
        if vocabulary.get(word.lower(), 0) > 0:
            continue
        if not speller.unknown(word, en):
            continue
        if word.lower() in glossary:
            continue
        if ignored is not None and ignored.matches("dictionary", word):
            continue
        if _mid_sentence_capital(word, after) and not _near_known_word(word, vocabulary, speller):
            continue
        out.append(word)
    return out


_NEW_PATTERN_RULES = (
    (r",\s*\(", "comma_before_paren", ",("),
    (r"\.\s*—\s*[A-ZÁÉÍÓÚÑ¿¡]", "period_before_raya", ".—"),
    (r"([,;:])\1", "doubled_mark", None),
    (r"\s[,;:.](?!\.)", "space_before_mark", None),
    (r"(?<![\s.…])\.[A-Za-zÁÉÍÓÚÑáéíóúñ]{2}", "no_space_after_period", None),
)
_REPEATED = re.compile(r"\b(\w{2,})\s+\1\b", re.I)


def punctuation_hits(before: str, after: str) -> list[dict]:
    """Punctuation the edit broke, as ``{rule, text}``; ``text`` is the mark."""
    hits = []
    for opener, closer in _PAIRS:
        if after.count(opener) != after.count(closer) and before.count(opener) == before.count(closer):
            hits.append({"rule": "unbalanced", "text": opener + closer})
    if after.startswith("—") and after.endswith("»") and not (before.startswith("—") and before.endswith("»")):
        hits.append({"rule": "raya_closes_guillemet", "text": "— »"})
    for pattern, rule, mark in _NEW_PATTERN_RULES:
        found = re.search(pattern, after)
        if found and not re.search(pattern, before):
            hits.append({"rule": rule, "text": mark or found.group().strip()})
    if re.search(r"[.;:!?»…]$", before) and re.search(rf"[{_LETTERS}]$", after):
        # Not a sentence, so no closing mark is owed: a one- or two-word label
        # such as a Bible reference ("—Heb." -> "—He"), or a heading in capitals.
        stem = before.rstrip()
        if len(_words(stem)) > 2 and stem.upper() != stem:
            hits.append({"rule": "closing_mark_dropped", "text": stem[-1]})
    repeated = _REPEATED.search(after)
    if repeated and not _REPEATED.search(before):
        hits.append({"rule": "repeated_word", "text": repeated.group()})
    return hits


def check_write(
    en: str,
    before: str,
    after: str,
    vocabulary: Mapping[str, int],
    glossary: frozenset[str] = frozenset(),
    ignored: Optional[IgnoredTerms] = None,
    disabled_rules: Iterable[str] = (),
    speller: Optional[Speller] = None,
) -> list[dict]:
    """Every rule hit for one saved sentence, as ``{rule, text}``.

    ``vocabulary`` maps a lower-cased word to how often the rest of the book
    uses it. ``speller`` is ``None`` where Enchant is missing; the punctuation
    rules still run.
    """
    off = set(disabled_rules)
    hits = []
    if "spelling" not in off:
        for word in spelling_hits(en or "", before, after, vocabulary, glossary, ignored, speller):
            hits.append({"rule": "spelling", "text": word})
    hits.extend(h for h in punctuation_hits(before, after) if h["rule"] not in off)
    return hits


# --- the book's vocabulary --------------------------------------------------

def _file_counts(path: Path, from_alignment: bool) -> Counter:
    counts: Counter = Counter()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return counts
    if from_alignment:
        texts = [a.get("es") or "" for a in data.get("alignments", []) if isinstance(a, dict)]
    else:
        texts = [data.get("translated_text") or ""]
    for text in texts:
        for word in _words(_TAG.sub(" ", text)):
            counts[word.lower()] += 1
    return counts


class BookVocabulary:
    """How often each word, lower-cased, occurs in a book's reading text.

    Read from the chapter alignments, which carry Saves that are still pending,
    and from the chunks of any chapter not yet aligned. A file is re-read only
    when its mtime moves, so a Save costs one chapter.
    """

    def __init__(self) -> None:
        self._books: dict[Path, tuple[dict, Counter]] = {}
        self._lock = threading.Lock()

    def counts(self, project_dir: Path) -> Counter:
        project_dir = Path(project_dir)
        aligned = {p.stem: p for p in (project_dir / "alignments").glob("*.json")}
        sources = {p: True for p in aligned.values()}
        for p in (project_dir / "chunks").glob("*_chunk_*.json"):
            if p.stem.rsplit("_chunk_", 1)[0] not in aligned:
                sources[p] = False
        with self._lock:
            files, total = self._books.setdefault(project_dir, ({}, Counter()))
            for path in list(files):
                if path not in sources:
                    total.subtract(files.pop(path)[1])
            for path, from_alignment in sources.items():
                try:
                    stamp = path.stat().st_mtime_ns
                except OSError:
                    continue
                known = files.get(path)
                if known is not None and known[0] == stamp:
                    continue
                if known is not None:
                    total.subtract(known[1])
                counts = _file_counts(path, from_alignment)
                total.update(counts)
                files[path] = (stamp, counts)
            return total


book_vocabulary = BookVocabulary()


# --- the warning log --------------------------------------------------------

def append_warning(project_dir: Path, **fields) -> str:
    """Record a warning in ``save_checks.jsonl`` and return its id."""
    warning_id = uuid.uuid4().hex[:12]
    _append(project_dir, {"kind": "warning", "id": warning_id,
                          "timestamp": datetime.now().isoformat(), **fields})
    return warning_id


def append_outcome(project_dir: Path, warning_id: str, outcome: str, **fields) -> None:
    """Record what the translator did with a warning: ``dismissed`` or ``ignored``."""
    _append(project_dir, {"kind": "outcome", "id": warning_id, "outcome": outcome,
                          "timestamp": datetime.now().isoformat(), **fields})


def _append(project_dir: Path, record: dict) -> None:
    with open(Path(project_dir) / LOG_NAME, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_log(project_dir: Path) -> tuple[list[dict], dict[str, dict]]:
    """``(warnings, outcome by warning id)`` from a book's log, oldest first."""
    warnings: list[dict] = []
    outcomes: dict[str, dict] = {}
    path = Path(project_dir) / LOG_NAME
    if not path.exists():
        return warnings, outcomes
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("kind") == "warning":
            warnings.append(record)
        elif record.get("kind") == "outcome":
            outcomes[record.get("id")] = record
    return warnings, outcomes


def standing_row(warning: dict, rows: list[dict]) -> Optional[dict]:
    """The alignment row that still carries a warning's sentence, if any."""
    saved = (warning.get("es_after") or "").strip()
    same = [r for r in rows if (r.get("es") or "").strip() == saved]
    if same:
        return next((r for r in same if r.get("es_idx") == warning.get("es_idx")), same[0])
    # A replaced span can be re-aligned into several sentences: the warning
    # follows the one that holds the flagged word.
    words = [h["text"] for h in warning.get("hits", []) if h.get("rule") == "spelling"]
    for row in rows:
        es = (row.get("es") or "").strip()
        if es and es in saved and any(
            re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", es) for w in words
        ):
            return row
    return None


def open_warnings(
    project_dir: Path,
    chapter_id: str,
    rows: list[dict],
    ignored: Optional[IgnoredTerms] = None,
) -> list[dict]:
    """Warnings a chapter should still show, each with its current ``es_idx``.

    A warning stays open while its saved sentence still stands in ``rows`` (the
    chapter's alignment), it has not been dismissed, and at least one of its
    hits is not on the ignore list. It is matched on text, so a realign moves it
    and the next edit of the sentence closes it. One per sentence, the latest.
    """
    warnings, outcomes = load_log(project_dir)
    by_idx: dict = {}
    for warning in warnings:
        if warning.get("chapter_id") != chapter_id or warning.get("id") in outcomes:
            continue
        row = standing_row(warning, rows)
        if row is None:
            continue
        hits = [
            h for h in warning.get("hits", [])
            if not (h.get("rule") == "spelling" and ignored is not None
                    and ignored.matches("dictionary", h.get("text")))
        ]
        if hits:
            by_idx[row.get("es_idx")] = {"id": warning["id"], "es_idx": row.get("es_idx"), "hits": hits}
    return list(by_idx.values())
