"""
Heuristic full-text feature detection for the style-guide wizard.

This module scans the entire source text of a translation project (not just
the small LLM sample) and emits a deterministic ``FeatureManifest`` describing
which structural / content features are present (dialogue, verse, footnotes,
scripture references, archaic language, etc.).

The wizard uses the manifest to decide which conditional questions to surface
and embeds a compact summary into the LLM-generated-questions prompt so the
LLM can lean on full-text signals.

Design choices (per plan):
- Heuristics only — no LLM in detection.
- Additive only — manifest never hides fixed questions, only triggers new
  conditional ones.
- One scan per project, cached to ``{project_dir}/text_features.json``;
  re-runs when source mtime is newer than the manifest or ``force=True``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from src.chunker import (
    _is_dialogue,
    _is_scene_break,
    ATTRIBUTION_RE,
    DIALOGUE_STARTERS,
)
from src.utils.text_utils import extract_paragraphs, count_words, normalize_newlines

logger = logging.getLogger(__name__)


MAX_EVIDENCE_LEN = 160
MAX_EVIDENCE_PER_FEATURE = 3


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class FeatureResult:
    """Result of a single feature detector."""

    name: str
    present: bool
    count: int
    confidence: float
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FeatureManifest:
    """Full manifest of feature detection results for a project."""

    features: dict[str, FeatureResult]
    generated_at: str
    source_mtime: Optional[float] = None
    total_paragraphs: int = 0
    total_words: int = 0

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "source_mtime": self.source_mtime,
            "total_paragraphs": self.total_paragraphs,
            "total_words": self.total_words,
            "features": {k: v.to_dict() for k, v in self.features.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FeatureManifest":
        feats = {
            k: FeatureResult(**v) for k, v in data.get("features", {}).items()
        }
        return cls(
            features=feats,
            generated_at=data.get("generated_at", ""),
            source_mtime=data.get("source_mtime"),
            total_paragraphs=data.get("total_paragraphs", 0),
            total_words=data.get("total_words", 0),
        )

    def get(self, name: str) -> FeatureResult:
        if name in self.features:
            return self.features[name]
        return FeatureResult(name=name, present=False, count=0, confidence=0.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _excerpt(text: str, max_len: int = MAX_EVIDENCE_LEN) -> str:
    """Trim text to a short evidence excerpt."""
    s = " ".join(text.split())
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s


def _add_evidence(bucket: list[str], snippet: str) -> None:
    if len(bucket) >= MAX_EVIDENCE_PER_FEATURE:
        return
    snippet = _excerpt(snippet)
    if snippet and snippet not in bucket:
        bucket.append(snippet)


# ---------------------------------------------------------------------------
# Individual detectors
# ---------------------------------------------------------------------------


def detect_dialogue(paragraphs: list[str], full_text: str) -> FeatureResult:
    """Detect dialogue using the chunker's existing rules."""
    evidence: list[str] = []
    count = 0
    for p in paragraphs:
        if _is_dialogue(p):
            count += 1
            _add_evidence(evidence, p)
    # raya (em-dash) at start of paragraph is also dialogue in Spanish
    for p in paragraphs:
        if p.lstrip().startswith("—") or p.lstrip().startswith("–"):
            count += 1
            _add_evidence(evidence, p)
    ratio = (count / len(paragraphs)) if paragraphs else 0.0
    present = count >= 5 and ratio > 0.01
    confidence = min(1.0, ratio * 5) if present else 0.0
    return FeatureResult("dialogue", present, count, confidence, evidence)


_VERSE_LINE_MAX = 60
_VERSE_RUN_MIN = 4


def detect_verse(paragraphs: list[str], full_text: str) -> FeatureResult:
    """Detect runs of short, non-terminal lines that look like verse."""
    evidence: list[str] = []
    total_verse_lines = 0
    current_run: list[str] = []
    runs_found = 0

    def flush(run: list[str]) -> int:
        nonlocal runs_found
        if len(run) >= _VERSE_RUN_MIN:
            runs_found += 1
            _add_evidence(evidence, " / ".join(run[:4]))
            return len(run)
        return 0

    lines = normalize_newlines(full_text).split("\n")
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            total_verse_lines += flush(current_run)
            current_run = []
            continue
        if _is_scene_break(stripped):
            total_verse_lines += flush(current_run)
            current_run = []
            continue
        # heading-ish line — treat as run-breaker
        if re.match(r"^(chapter|chap\.|capítulo|cap\.|part|libro)\b", stripped, re.I):
            total_verse_lines += flush(current_run)
            current_run = []
            continue
        is_short = len(stripped) <= _VERSE_LINE_MAX
        is_non_terminal = not stripped.endswith((".", "!", "?", ":"))
        # word count constraint stops one-word noise lines from inflating
        word_n = len(stripped.split())
        if is_short and is_non_terminal and 1 < word_n <= 12:
            current_run.append(stripped)
        else:
            total_verse_lines += flush(current_run)
            current_run = []
    total_verse_lines += flush(current_run)

    present = runs_found >= 1 and total_verse_lines >= _VERSE_RUN_MIN
    confidence = min(1.0, runs_found / 3) if present else 0.0
    return FeatureResult("verse", present, total_verse_lines, confidence, evidence)


_FOOTNOTE_BRACKET_RE = re.compile(r"\[(\d{1,3})\]")
_FOOTNOTE_SUPER_RE = re.compile(r"(?<=[A-Za-zñáéíóúÁÉÍÓÚ])(\d{1,3})(?=[\s.,;:!?])")
_FOOTNOTE_NUMBERED_RE = re.compile(r"^\s*(\d{1,3})\.\s+\S")
_FOOTNOTE_ASTERISK_RE = re.compile(r"(?<!\w)\*+(?=\s|$)")


def detect_footnotes(paragraphs: list[str], full_text: str) -> FeatureResult:
    evidence: list[str] = []
    bracket_hits = _FOOTNOTE_BRACKET_RE.findall(full_text)
    asterisk_hits = _FOOTNOTE_ASTERISK_RE.findall(full_text)

    # density of "1. text" lines in the back third (typical endnote location)
    third = max(1, len(paragraphs) // 3)
    back = paragraphs[-third:]
    numbered_back = sum(1 for p in back if _FOOTNOTE_NUMBERED_RE.match(p))

    super_hits = _FOOTNOTE_SUPER_RE.findall(full_text)

    count = len(bracket_hits) + numbered_back + len(super_hits)
    if bracket_hits:
        for p in paragraphs:
            if _FOOTNOTE_BRACKET_RE.search(p):
                _add_evidence(evidence, p)
                if len(evidence) >= MAX_EVIDENCE_PER_FEATURE:
                    break
    elif numbered_back >= 3:
        for p in back:
            if _FOOTNOTE_NUMBERED_RE.match(p):
                _add_evidence(evidence, p)
                if len(evidence) >= MAX_EVIDENCE_PER_FEATURE:
                    break

    present = bool(bracket_hits) or numbered_back >= 3 or len(asterisk_hits) >= 4
    confidence = min(1.0, count / 10) if present else 0.0
    return FeatureResult("footnotes", present, count, confidence, evidence)


_EPIGRAPH_DASH = ("—", "–", "--")
_CHAPTER_HEADING_RE = re.compile(
    r"^(chapter|chap\.|capítulo|cap\.|part|libro|book)\b[\s\dIVXLCM\.\-:]*",
    re.I,
)


def detect_epigraphs(paragraphs: list[str], full_text: str) -> FeatureResult:
    evidence: list[str] = []
    count = 0
    for i, p in enumerate(paragraphs[:-1]):
        if _CHAPTER_HEADING_RE.match(p.strip()):
            nxt = paragraphs[i + 1].strip()
            if 0 < len(nxt) < 300 and any(d in nxt for d in _EPIGRAPH_DASH):
                count += 1
                _add_evidence(evidence, nxt)
    present = count >= 1
    confidence = min(1.0, count / 3) if present else 0.0
    return FeatureResult("epigraphs", present, count, confidence, evidence)


_SALUTATIONS = re.compile(
    r"\b(Dear\s+[A-Z][a-z]+|My\s+dear\s+[A-Z][a-z]+|Mi\s+querid[oa]|"
    r"Estimad[oa]|Querid[oa]\s+[A-Z][a-z]+)\b",
    re.IGNORECASE,
)
_VALEDICTIONS = re.compile(
    r"\b(Sincerely|Yours\s+truly|Yours\s+ever|Faithfully|Atentamente|"
    r"Cordialmente|Un\s+abrazo|Suy[oa]|Tuy[oa])\b",
    re.IGNORECASE,
)


def detect_letters(paragraphs: list[str], full_text: str) -> FeatureResult:
    evidence: list[str] = []
    pairs = 0
    sal_idx = -10
    for i, p in enumerate(paragraphs):
        if _SALUTATIONS.search(p):
            sal_idx = i
            _add_evidence(evidence, p)
        if _VALEDICTIONS.search(p) and 0 <= i - sal_idx <= 60:
            pairs += 1
            sal_idx = -10
    present = pairs >= 1
    confidence = min(1.0, pairs / 3) if present else 0.0
    return FeatureResult("letters", present, pairs, confidence, evidence)


_BIBLE_BOOKS = (
    "Genesis|Exodus|Leviticus|Numbers|Deuteronomy|Joshua|Judges|Ruth|Samuel|"
    "Kings|Chronicles|Ezra|Nehemiah|Esther|Job|Psalms?|Proverbs|Ecclesiastes|"
    "Isaiah|Jeremiah|Lamentations|Ezekiel|Daniel|Hosea|Joel|Amos|Obadiah|Jonah|"
    "Micah|Nahum|Habakkuk|Zephaniah|Haggai|Zechariah|Malachi|"
    "Matthew|Mark|Luke|John|Acts|Romans|Corinthians|Galatians|Ephesians|"
    "Philippians|Colossians|Thessalonians|Timothy|Titus|Philemon|Hebrews|"
    "James|Peter|Jude|Revelation|"
    "Génesis|Éxodo|Levítico|Números|Deuteronomio|Josué|Jueces|Rut|"
    "Reyes|Crónicas|Esdras|Nehemías|Ester|Salmos?|Proverbios|Eclesiastés|"
    "Isaías|Jeremías|Lamentaciones|Ezequiel|Oseas|Abdías|Jonás|Miqueas|"
    "Nahúm|Habacuc|Sofonías|Hageo|Zacarías|Malaquías|"
    "Mateo|Marcos|Lucas|Juan|Hechos|Romanos|Corintios|Gálatas|Efesios|"
    "Filipenses|Colosenses|Tesalonicenses|Timoteo|Tito|Filemón|Hebreos|"
    "Santiago|Pedro|Judas|Apocalipsis"
)
_SCRIPTURE_RE = re.compile(
    r"\b(?:[1-3]\s?)?(?:" + _BIBLE_BOOKS + r")\.?\s+\d{1,3}[:.]\d{1,3}(?:[-–]\d{1,3})?\b"
)


def detect_scripture_references(paragraphs: list[str], full_text: str) -> FeatureResult:
    evidence: list[str] = []
    matches = list(_SCRIPTURE_RE.finditer(full_text))
    count = len(matches)
    for m in matches[:MAX_EVIDENCE_PER_FEATURE]:
        start = max(0, m.start() - 30)
        end = min(len(full_text), m.end() + 30)
        _add_evidence(evidence, full_text[start:end])
    present = count >= 2
    confidence = min(1.0, count / 8) if present else 0.0
    return FeatureResult(
        "scripture_references", present, count, confidence, evidence
    )


_ARCHAIC_EN = {
    "thou", "thee", "thy", "thine", "ye", "hast", "hath", "doth",
    "wast", "wert", "art", "shalt", "wilt", "verily", "behold",
}
_ARCHAIC_ES = {
    "vos", "vuestra", "vuestro", "vuestras", "vuestros", "heos", "habéis",
    "merced", "mercedes", "señoría", "vuesa",
}


def detect_archaic_language(paragraphs: list[str], full_text: str) -> FeatureResult:
    tokens = re.findall(r"\b[\w']+\b", full_text.lower())
    total = len(tokens) or 1
    archaic_set = _ARCHAIC_EN | _ARCHAIC_ES
    hits = [t for t in tokens if t in archaic_set]
    count = len(hits)
    rate_per_10k = count / total * 10000
    evidence: list[str] = []
    if hits:
        for p in paragraphs:
            low = p.lower()
            if any(re.search(r"\b" + re.escape(w) + r"\b", low) for w in archaic_set):
                _add_evidence(evidence, p)
                if len(evidence) >= MAX_EVIDENCE_PER_FEATURE:
                    break
    present = rate_per_10k >= 5 and count >= 3
    confidence = min(1.0, rate_per_10k / 20) if present else 0.0
    return FeatureResult("archaic_language", present, count, confidence, evidence)


_ITALIC_RE = re.compile(r"(?:_([^_\n]{4,80})_|\*([^*\n]{4,80})\*)")


def detect_foreign_passages(paragraphs: list[str], full_text: str) -> FeatureResult:
    evidence: list[str] = []
    italics = [
        (m.group(1) or m.group(2) or "").strip()
        for m in _ITALIC_RE.finditer(full_text)
    ]
    distinct = []
    for s in italics:
        if len(s.split()) >= 2 and s not in distinct:
            distinct.append(s)
    count = len(distinct)
    for s in distinct[:MAX_EVIDENCE_PER_FEATURE]:
        _add_evidence(evidence, s)
    # Conservative: require ≥3 distinct italic passages of ≥2 words.
    present = count >= 3
    confidence = min(1.0, count / 6) if present else 0.0
    return FeatureResult("foreign_passages", present, count, confidence, evidence)


_LIST_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)]|[a-zA-Z][.)])\s+\S")


def detect_lists(paragraphs: list[str], full_text: str) -> FeatureResult:
    evidence: list[str] = []
    lines = normalize_newlines(full_text).split("\n")
    runs = 0
    current = 0
    longest = 0
    for line in lines:
        if _LIST_LINE_RE.match(line):
            current += 1
            if current >= 3 and current > longest:
                longest = current
                _add_evidence(evidence, line.strip())
        else:
            if current >= 3:
                runs += 1
            current = 0
    if current >= 3:
        runs += 1
    present = runs >= 1
    confidence = min(1.0, runs / 3) if present else 0.0
    return FeatureResult("lists", present, runs, confidence, evidence)


def detect_block_quotes(paragraphs: list[str], full_text: str) -> FeatureResult:
    evidence: list[str] = []
    count = 0
    lines = normalize_newlines(full_text).split("\n")
    for line in lines:
        # >2 leading spaces and 60+ chars of content suggests a block quote
        if line.startswith(("    ", "\t")) and len(line.strip()) >= 60:
            count += 1
            _add_evidence(evidence, line.strip())
    # also: paragraphs introduced by ":" then a long quote
    for i in range(len(paragraphs) - 1):
        if paragraphs[i].rstrip().endswith(":"):
            nxt = paragraphs[i + 1].strip()
            if len(nxt) >= 120 and (nxt.startswith('"') or nxt.startswith("\u201c")
                                    or nxt.startswith("«") or nxt.startswith("—")):
                count += 1
                _add_evidence(evidence, nxt)
    present = count >= 3
    confidence = min(1.0, count / 6) if present else 0.0
    return FeatureResult("block_quotes", present, count, confidence, evidence)


_DRAMATIC_SPEAKER_RE = re.compile(r"^([A-ZÁÉÍÓÚÑ]{2,}[A-ZÁÉÍÓÚÑ\s]{0,30}):\s+\S")
_STAGE_DIRECTION_RE = re.compile(r"\[(Enter|Exit|Exeunt|Aside|Entra|Sale|Salen)\b", re.I)


def detect_dramatic_format(paragraphs: list[str], full_text: str) -> FeatureResult:
    evidence: list[str] = []
    speaker_hits = 0
    for p in paragraphs:
        first = p.strip().split("\n", 1)[0]
        if _DRAMATIC_SPEAKER_RE.match(first):
            speaker_hits += 1
            _add_evidence(evidence, first)
    stage_hits = len(_STAGE_DIRECTION_RE.findall(full_text))
    count = speaker_hits + stage_hits
    present = speaker_hits >= 3 or stage_hits >= 2
    confidence = min(1.0, count / 8) if present else 0.0
    return FeatureResult("dramatic_format", present, count, confidence, evidence)


_IMPERIAL_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s?(miles?|feet|foot|inches|inch|lbs?|pounds?|ounces?|°F|yards?|gallons?)\b",
    re.IGNORECASE,
)


def detect_measurements_imperial(paragraphs: list[str], full_text: str) -> FeatureResult:
    evidence: list[str] = []
    matches = list(_IMPERIAL_RE.finditer(full_text))
    count = len(matches)
    for m in matches[:MAX_EVIDENCE_PER_FEATURE]:
        start = max(0, m.start() - 30)
        end = min(len(full_text), m.end() + 30)
        _add_evidence(evidence, full_text[start:end])
    present = count >= 2
    confidence = min(1.0, count / 6) if present else 0.0
    return FeatureResult(
        "measurements_imperial", present, count, confidence, evidence
    )


_CURRENCY_RE = re.compile(
    r"(\$\d|\£\d|\bshillings?\b|\bpence\b|\bpesos?\b|\breales?\b|"
    # Guard against false positives like "guinea pig(s)" / "guinea hen(s)"
    # (animals, not currency).
    r"\bmaravedíes?\b|\bduros?\b|\bdoubloons?\b|\bcrowns?\b|\bguineas?\b(?!\s+(?:pigs?|hens?|fowls?|cocks?)\b))",
    re.IGNORECASE,
)


def detect_currency_period(paragraphs: list[str], full_text: str) -> FeatureResult:
    evidence: list[str] = []
    matches = list(_CURRENCY_RE.finditer(full_text))
    count = len(matches)
    for m in matches[:MAX_EVIDENCE_PER_FEATURE]:
        start = max(0, m.start() - 30)
        end = min(len(full_text), m.end() + 30)
        _add_evidence(evidence, full_text[start:end])
    present = count >= 2
    confidence = min(1.0, count / 6) if present else 0.0
    return FeatureResult(
        "currency_period", present, count, confidence, evidence
    )


# ---------------------------------------------------------------------------
# Epicene animal speakers (EN→ES gender-vs-sex hazard)
# ---------------------------------------------------------------------------
#
# English carries an animal character's biological sex via pronouns ("he",
# "she"), kinship terms ("father spider", "the mother giraffe"), and
# honorifics ("Mr. Swallow"). Many Spanish animal nouns are *epicene* — they
# have one fixed grammatical gender regardless of biological sex (la
# golondrina, el tiburón, la jirafa, el mosquito, la hormiga). A naive
# translation will therefore silently flip the character's apparent sex
# (Mr. Swallow → "ella"). This detector flags such books so the wizard can
# ask how the translation should preserve sex.
#
# Lexicon: English lemma -> (Spanish lemma, fixed Spanish gender M/F).
# Only nouns whose Spanish equivalent is *truly epicene* (no separate
# masculine/feminine form). Pairs with distinct genders (oso/osa,
# perro/perra, lobo/loba, gato/gata, conejo/coneja, león/leona) are
# excluded — they don't have the gender-vs-sex hazard.

_EPICENE_ANIMALS: dict[str, tuple[str, str]] = {
    # Feminine epicene
    "swallow": ("golondrina", "F"),
    "swallows": ("golondrinas", "F"),
    "giraffe": ("jirafa", "F"),
    "giraffes": ("jirafas", "F"),
    "ant": ("hormiga", "F"),
    "ants": ("hormigas", "F"),
    "frog": ("rana", "F"),
    "frogs": ("ranas", "F"),
    "spider": ("araña", "F"),
    "spiders": ("arañas", "F"),
    "whale": ("ballena", "F"),
    "whales": ("ballenas", "F"),
    "turtle": ("tortuga", "F"),
    "turtles": ("tortugas", "F"),
    "tortoise": ("tortuga", "F"),
    "tortoises": ("tortugas", "F"),
    "eagle": ("águila", "F"),
    "eagles": ("águilas", "F"),
    "hare": ("liebre", "F"),
    "hares": ("liebres", "F"),
    "owl": ("lechuza", "F"),
    "owls": ("lechuzas", "F"),
    "bee": ("abeja", "F"),
    "bees": ("abejas", "F"),
    "butterfly": ("mariposa", "F"),
    "butterflies": ("mariposas", "F"),
    "snake": ("serpiente", "F"),
    "snakes": ("serpientes", "F"),
    "panther": ("pantera", "F"),
    "panthers": ("panteras", "F"),
    "ladybug": ("mariquita", "F"),
    "ladybugs": ("mariquitas", "F"),
    "caterpillar": ("oruga", "F"),
    "caterpillars": ("orugas", "F"),
    "seal": ("foca", "F"),
    "seals": ("focas", "F"),
    "weasel": ("comadreja", "F"),
    "weasels": ("comadrejas", "F"),
    "squirrel": ("ardilla", "F"),
    "squirrels": ("ardillas", "F"),
    "otter": ("nutria", "F"),
    "otters": ("nutrias", "F"),
    "magpie": ("urraca", "F"),
    "magpies": ("urracas", "F"),
    "pigeon": ("paloma", "F"),
    "pigeons": ("palomas", "F"),
    "dove": ("paloma", "F"),
    "doves": ("palomas", "F"),
    "stork": ("cigüeña", "F"),
    "storks": ("cigüeñas", "F"),
    "zebra": ("cebra", "F"),
    "zebras": ("cebras", "F"),
    "llama": ("llama", "F"),
    "llamas": ("llamas", "F"),
    "gazelle": ("gacela", "F"),
    "gazelles": ("gacelas", "F"),
    "hyena": ("hiena", "F"),
    "hyenas": ("hienas", "F"),
    "flea": ("pulga", "F"),
    "fleas": ("pulgas", "F"),
    "heron": ("garza", "F"),
    "herons": ("garzas", "F"),
    # Masculine epicene
    "shark": ("tiburón", "M"),
    "sharks": ("tiburones", "M"),
    "mosquito": ("mosquito", "M"),
    "mosquitos": ("mosquitos", "M"),
    "mosquitoes": ("mosquitos", "M"),
    "mouse": ("ratón", "M"),
    "mice": ("ratones", "M"),
    "toad": ("sapo", "M"),
    "toads": ("sapos", "M"),
    "octopus": ("pulpo", "M"),
    "octopuses": ("pulpos", "M"),
    "crocodile": ("cocodrilo", "M"),
    "crocodiles": ("cocodrilos", "M"),
    "alligator": ("caimán", "M"),
    "alligators": ("caimanes", "M"),
    "hippopotamus": ("hipopótamo", "M"),
    "hippopotamuses": ("hipopótamos", "M"),
    "hippo": ("hipopótamo", "M"),
    "hippos": ("hipopótamos", "M"),
    "rhinoceros": ("rinoceronte", "M"),
    "rhino": ("rinoceronte", "M"),
    "rhinos": ("rinocerontes", "M"),
    "penguin": ("pingüino", "M"),
    "penguins": ("pingüinos", "M"),
    "crab": ("cangrejo", "M"),
    "crabs": ("cangrejos", "M"),
    "worm": ("gusano", "M"),
    "worms": ("gusanos", "M"),
    "snail": ("caracol", "M"),
    "snails": ("caracoles", "M"),
    "dolphin": ("delfín", "M"),
    "dolphins": ("delfines", "M"),
    "mole": ("topo", "M"),
    "moles": ("topos", "M"),
    "hedgehog": ("erizo", "M"),
    "hedgehogs": ("erizos", "M"),
    "raven": ("cuervo", "M"),
    "ravens": ("cuervos", "M"),
    "crow": ("cuervo", "M"),
    "crows": ("cuervos", "M"),
    "sparrow": ("gorrión", "M"),
    "sparrows": ("gorriones", "M"),
    "canary": ("canario", "M"),
    "canaries": ("canarios", "M"),
    "parrot": ("loro", "M"),
    "parrots": ("loros", "M"),
}

# Sort lemmas by length so longer words match first inside the alternation.
_ANIMAL_LEMMAS = sorted(_EPICENE_ANIMALS.keys(), key=len, reverse=True)
_ANIMAL_ALT = "|".join(re.escape(w) for w in _ANIMAL_LEMMAS)

# Animal mention with optional preceding article / honorific / kinship /
# adjective so we can spot proper-name and prefix-borne sex cues.
_ANIMAL_MENTION_RE = re.compile(
    r"(?:(?P<prefix>"
    r"the|a|an|this|that|my|your|his|her|their|our|its|"
    r"Mr\.?|Mrs\.?|Ms\.?|Miss|Sir|Madam|Master|Doctor|Dr\.?|Lord|Lady|"
    r"Father|Mother|Mama|Mamma|Mommy|Mom|Papa|Daddy|Dad|"
    r"Grandfather|Grandmother|Grandma|Grandpa|"
    r"Uncle|Aunt|Brother|Sister|"
    r"old|young|little|big|wise|kind|"
    r"male|female|"
    r"King|Queen|Prince|Princess|Boy|Girl"
    r")\s+)?"
    r"(?P<animal>" + _ANIMAL_ALT + r")\b",
    re.IGNORECASE,
)

_SPEECH_VERBS = (
    "said|replied|asked|whispered|shouted|cried|exclaimed|murmured|grumbled|"
    "sighed|laughed|chuckled|shrieked|hissed|growled|roared|squeaked|"
    "answered|added|insisted|continued|retorted|muttered|called|begged|"
    "thought|wondered|decided|remembered|smiled|frowned|nodded|agreed"
)
# Animal+verb in either order (attribution before or after the animal).
_SPEECH_RE = re.compile(
    r"\b(?:" + _SPEECH_VERBS + r")\s+(?:the\s+)?(?:" + _ANIMAL_ALT + r")\b|"
    r"\b(?:the\s+)?(?:" + _ANIMAL_ALT + r")\s+\w*\s*(?:" + _SPEECH_VERBS + r")\b",
    re.IGNORECASE,
)

_MASC_CUE_RE = re.compile(
    r"\b(?:he|him|his|himself|"
    r"Mr\.?|Sir|Master|Lord|"
    r"father|papa|daddy|dad|grandfather|grandpa|uncle|brother|son|"
    r"husband|king|prince|boy|gentleman|"
    r"male|bull|buck|jack|tom)\b",
    re.IGNORECASE,
)
_FEM_CUE_RE = re.compile(
    r"\b(?:she|her|hers|herself|"
    r"Mrs\.?|Ms\.?|Miss|Madam|Ma'am|Lady|"
    r"mother|mama|mamma|mommy|mom|grandmother|grandma|aunt|sister|daughter|"
    r"wife|queen|princess|girl|"
    r"female|cow|doe|jenny|hen)\b",
    re.IGNORECASE,
)

# Local windows around an animal mention, in characters.
_EPICENE_SPEECH_WIN = 200  # for speech-verb / dialogue context
_EPICENE_CUE_WIN = 120     # for sex-cue pronouns and kinship


def detect_epicene_animal_speakers(
    paragraphs: list[str], full_text: str
) -> FeatureResult:
    """Detect animal characters whose English source establishes a sex but
    whose Spanish equivalents are epicene (one fixed grammatical gender).

    Triggers when (a) an English animal noun maps to a Spanish epicene noun,
    (b) the animal appears in a speaking/anthropomorphized context, and
    (c) English sex cues (pronouns, kinship terms, honorifics) appear in the
    local window. Confidence is boosted when the cue's sex *conflicts* with
    the Spanish noun's grammatical gender — the smoking-gun case where a
    naive translation will silently flip the character's apparent sex.
    """
    evidence: list[str] = []
    speaking_animals: set[str] = set()
    cue_pairs: set[tuple[str, str]] = set()
    mismatch_pairs: set[tuple[str, str]] = set()

    if not full_text:
        return FeatureResult(
            "epicene_animal_speakers", False, 0, 0.0, evidence
        )

    n = len(full_text)
    for m in _ANIMAL_MENTION_RE.finditer(full_text):
        animal = m.group("animal").lower()
        if animal not in _EPICENE_ANIMALS:
            continue
        _, gender = _EPICENE_ANIMALS[animal]
        prefix = (m.group("prefix") or "").strip()

        # Speech / dialogue context within ±200 chars
        ws = max(0, m.start() - _EPICENE_SPEECH_WIN)
        we = min(n, m.end() + _EPICENE_SPEECH_WIN)
        speech_window = full_text[ws:we]

        has_speech = bool(_SPEECH_RE.search(speech_window))
        has_dialogue = (
            '"' in speech_window
            or "\u201c" in speech_window
            or "\u201d" in speech_window
            or "—" in speech_window
            or "«" in speech_window
        )

        # Animal capitalized mid-sentence -> proper-name signal
        raw = m.group("animal")
        before = full_text[max(0, m.start("animal") - 80):m.start("animal")].rstrip()
        is_proper = (
            raw[0].isupper()
            and bool(before)
            and not before.endswith((".", "!", "?", "\n"))
        )

        # A non-pronoun prefix (Mr./Father/old/wise/...) is itself an
        # anthropomorphism cue.
        prefix_anthropomorphic = bool(prefix) and prefix.lower() not in {
            "the", "a", "an", "this", "that",
            "my", "your", "his", "her", "their", "our", "its",
        }

        if not (has_speech or has_dialogue or is_proper or prefix_anthropomorphic):
            continue

        speaking_animals.add(animal)

        # Sex cue detection. The prefix wins (most direct attribution),
        # otherwise look in a tighter ±120-char window.
        cue_sex: Optional[str] = None
        if prefix:
            if _MASC_CUE_RE.search(prefix):
                cue_sex = "M"
            elif _FEM_CUE_RE.search(prefix):
                cue_sex = "F"

        if cue_sex is None:
            cs = max(0, m.start() - _EPICENE_CUE_WIN)
            ce = min(n, m.end() + _EPICENE_CUE_WIN)
            cue_window = full_text[cs:ce]
            masc = bool(_MASC_CUE_RE.search(cue_window))
            fem = bool(_FEM_CUE_RE.search(cue_window))
            if masc and not fem:
                cue_sex = "M"
            elif fem and not masc:
                cue_sex = "F"
            # If both appear, treat as ambiguous -> skip cue assignment.

        if cue_sex in ("M", "F"):
            cue_pairs.add((animal, cue_sex))
            if (cue_sex == "M" and gender == "F") or (cue_sex == "F" and gender == "M"):
                mismatch_pairs.add((animal, cue_sex))
                _add_evidence(evidence, full_text[ws:we])
            else:
                _add_evidence(evidence, full_text[ws:we])

    count = len(cue_pairs)
    base = (
        0.05 * min(len(speaking_animals), 5)
        + 0.15 * min(len(cue_pairs), 5)
        + 0.40 * min(len(mismatch_pairs), 3)
    )
    confidence = min(1.0, base)
    present = count >= 1 and len(speaking_animals) >= 1

    return FeatureResult(
        "epicene_animal_speakers", present, count, confidence, evidence
    )


_TRANSLATOR_NOTE_RE = re.compile(
    r"\[\s*(?:N\.\s*del\s*T\.?|Translator['\u2019]?s?\s+note|Nota\s+del\s+traductor)",
    re.IGNORECASE,
)


def detect_translator_notes(paragraphs: list[str], full_text: str) -> FeatureResult:
    evidence: list[str] = []
    matches = list(_TRANSLATOR_NOTE_RE.finditer(full_text))
    count = len(matches)
    for m in matches[:MAX_EVIDENCE_PER_FEATURE]:
        start = max(0, m.start() - 30)
        end = min(len(full_text), m.end() + 60)
        _add_evidence(evidence, full_text[start:end])
    present = count >= 1
    confidence = min(1.0, count / 3) if present else 0.0
    return FeatureResult(
        "translator_notes", present, count, confidence, evidence
    )


# ---------------------------------------------------------------------------
# Detector registry & manifest construction
# ---------------------------------------------------------------------------


DetectorFn = Callable[[list[str], str], FeatureResult]

DETECTORS: dict[str, DetectorFn] = {
    "dialogue": detect_dialogue,
    "verse": detect_verse,
    "footnotes": detect_footnotes,
    "epigraphs": detect_epigraphs,
    "letters": detect_letters,
    "scripture_references": detect_scripture_references,
    "archaic_language": detect_archaic_language,
    "foreign_passages": detect_foreign_passages,
    "lists": detect_lists,
    "block_quotes": detect_block_quotes,
    "dramatic_format": detect_dramatic_format,
    "measurements_imperial": detect_measurements_imperial,
    "currency_period": detect_currency_period,
    "translator_notes": detect_translator_notes,
    "epicene_animal_speakers": detect_epicene_animal_speakers,
}


def _load_full_source_text(project_dir: Path) -> tuple[str, Optional[float]]:
    """Load the entire source text for a project.

    Returns ``(text, mtime)``. See
    ``src.utils.source_text.load_clean_source_text`` for the priority order
    (chapters → chunks → source.txt). The mtime is used for cache
    invalidation of the feature manifest.
    """
    from src.utils.source_text import load_clean_source_text

    text, mtime, _ = load_clean_source_text(project_dir)
    return text, mtime


def build_manifest(full_text: str) -> FeatureManifest:
    """Run every registered detector against ``full_text``.

    Public helper used by tests and by ``detect_all_features``.
    """
    paragraphs = extract_paragraphs(full_text) if full_text else []
    total_words = count_words(full_text) if full_text else 0
    results: dict[str, FeatureResult] = {}
    for name, fn in DETECTORS.items():
        try:
            results[name] = fn(paragraphs, full_text)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Detector {name} failed: {exc}")
            results[name] = FeatureResult(name, False, 0, 0.0, [])
    return FeatureManifest(
        features=results,
        generated_at=datetime.now(timezone.utc).isoformat(),
        total_paragraphs=len(paragraphs),
        total_words=total_words,
    )


def manifest_path(project_dir: Path) -> Path:
    return Path(project_dir) / "text_features.json"


def _save_manifest(manifest: FeatureManifest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest.to_dict(), fh, ensure_ascii=False, indent=2)


def _load_manifest(path: Path) -> Optional[FeatureManifest]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return FeatureManifest.from_dict(json.load(fh))
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        logger.warning(f"Failed to load manifest at {path}: {exc}")
        return None


def detect_all_features(
    project_dir: Path,
    *,
    force: bool = False,
    text: Optional[str] = None,
) -> FeatureManifest:
    """Detect features for a project, with on-disk caching.

    Args:
        project_dir: Project directory containing ``chunks/`` or ``source.txt``.
        force: If True, re-run detection even if a fresh manifest exists.
        text: Pre-loaded source text. When provided, bypasses file loading
            (useful for tests). Cache will not be written in this case.
    """
    project_dir = Path(project_dir)
    cache_path = manifest_path(project_dir)

    if text is not None:
        return build_manifest(text)

    full_text, source_mtime = _load_full_source_text(project_dir)

    if not force:
        cached = _load_manifest(cache_path)
        if cached is not None:
            cached_mtime = cached.source_mtime or 0.0
            if source_mtime is None or cached_mtime >= source_mtime:
                logger.info(f"Using cached feature manifest at {cache_path}")
                return cached

    if not full_text:
        logger.warning(f"No source text found in {project_dir}; manifest is empty")
        manifest = FeatureManifest(features={}, generated_at=datetime.now(timezone.utc).isoformat())
        return manifest

    logger.info(f"Running heuristic feature detection on {project_dir}")
    manifest = build_manifest(full_text)
    manifest.source_mtime = source_mtime
    _save_manifest(manifest, cache_path)
    return manifest


# ---------------------------------------------------------------------------
# Conditional question filtering
# ---------------------------------------------------------------------------


def matches_requires(requires: dict, manifest: FeatureManifest) -> bool:
    """Evaluate a ``requires`` predicate against a manifest.

    Predicate shape::

        {"feature": "dialogue", "min_count": 5}

    Optional keys:
        - ``min_confidence``: float 0-1
        - ``min_count``: integer
        - ``present``: bool (default True)
    """
    if not requires:
        return True
    feature = requires.get("feature")
    if not feature:
        return True
    result = manifest.get(feature)
    if requires.get("present", True) and not result.present:
        return False
    if "min_count" in requires and result.count < requires["min_count"]:
        return False
    if "min_confidence" in requires and result.confidence < requires["min_confidence"]:
        return False
    return True


def filter_conditional_questions(
    conditional_questions: list[dict], manifest: FeatureManifest
) -> list[dict]:
    """Return only conditional questions whose ``requires`` predicate matches."""
    out = []
    for q in conditional_questions:
        if matches_requires(q.get("requires", {}), manifest):
            out.append(q)
    return out


def manifest_summary(manifest: FeatureManifest) -> str:
    """Compact one-line-per-feature summary for embedding in LLM prompts."""
    lines = [
        f"Total: {manifest.total_paragraphs} paragraphs, {manifest.total_words} words",
    ]
    for name, result in manifest.features.items():
        marker = "✓" if result.present else "·"
        lines.append(
            f"  {marker} {name}: count={result.count}, confidence={result.confidence:.2f}"
        )
    return "\n".join(lines)
