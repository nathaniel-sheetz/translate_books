"""Load everything a book review reads, once, read-only.

The detectors in :mod:`src.book_review.signals` ask questions that cut across
every judge and every chapter, so they read from one in-memory view of the book
rather than walking ``evaluations/`` each.

Three joins are made here so no detector has to make them:

- **Findings to human marks.** ``_feedback.jsonl`` is keyed by
  :func:`web_ui.evaluations.issue_key`, the same key the dashboard wrote the
  mark against. Editorial findings carry a stable ``finding_key``; dialogue and
  address findings fall back to a hash of the message, which a re-judge rewords,
  so an old mark on those can fail to join. A finding with no mark is reported as
  unmarked, never guessed.
- **Findings to the prompt the translator actually got.** Each chunk's
  ``last_llm_log`` names the logged translation prompt in ``prompts/history/``.
  Its GLOSSARY TERMS block is the authoritative answer to "did this entry reach
  this chunk?" -- better than recomputing the filter, which may have changed
  since. A chunk without a readable log answers ``None`` and the detectors fall
  back to recomputing.
- **Findings to a theme.** Editorial rule ids are invented per run
  (``glossary-perrito-llanero``, ``forms-of-address-usted``), so grouping on the
  raw id splits one problem into a dozen singletons. :func:`theme_of` folds them
  into a small fixed vocabulary.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The LLM judges a review reads. The coded evaluators are triage's job.
JUDGES: tuple[str, ...] = ("dialogue", "address", "editorial")

# Ordered: the first theme whose pattern matches the rule (and the head of the
# message) wins. Address is first because "tu"/"usted" leak into many rule ids.
_THEMES: tuple[tuple[str, re.Pattern], ...] = tuple(
    (name, re.compile(pattern))
    for name, pattern in (
        ("address", r"address|usted|tuteo|tutea|\btu\b|-tu\b|\btu-|trato|tratamiento|voseo"),
        ("glossary", r"glossar|glosario|\block\b|-lock\b|fixed-form|canon"),
        ("caps_italics", r"caps|italic|cursiva|versal|emphasis|énfasis|mayúscula"),
        ("dialogue_punct", r"raya|dialog|inciso|guillemet|comilla|quote|turn|narration"),
        ("units", r"\bunit|metric|currency|monetar"),
        ("interjection", r"interjec|religio|oath"),
        ("names", r"name|epithet|honorific|title|nombre"),
        ("register", r"dialect|iberian|peninsular|regist|slang|vosotros|archai|anachron|leísmo|leismo"),
        ("agreement", r"gender|agreement|concord|pronoun|antecedent|clitic"),
        ("calque", r"calque|calco|false-friend|falso-amigo|anglic"),
    )
)

# "Polly→Nancy", "Tío Paul -> Jules" inside an address finding's message.
_PAIR_RE = re.compile(
    r"([A-ZÁÉÍÓÚÑ][\w.'’ ]{0,40}?)\s*(?:→|->)\s*([A-ZÁÉÍÓÚÑ][\w.'’ ]{0,40}?)\s*[:,;.)(]"
)
_EXPECTED_RE = re.compile(r"expected\s+(tú|tu|usted|ustedes)\b", re.IGNORECASE)
_BRACKET_RULE_RE = re.compile(r"^\[([^\]]+)\]")
_CHAPTER_RE = re.compile(r"(chapter_\d+)")


@dataclass
class Finding:
    """One judge finding, in the shape every detector reads."""

    id: str
    judge: str
    chunk_id: str
    chapter_id: str
    #: ``kept`` (an issue), ``retracted`` (pass 2 dropped it) or
    #: ``filtered:<reason>`` (pass 1's own gates dropped it).
    status: str
    rule: str
    theme: str
    category: Optional[str]
    severity: Optional[str]
    message: str
    location: str
    suggestion: str
    issue_key: Optional[str]
    #: The feedback type standing on this finding, or ``None``.
    mark: Optional[str] = None
    executed_at: Optional[str] = None
    #: Address findings only: the ``(speaker, addressee)`` the judge named.
    pair: Optional[tuple[str, str]] = None
    #: Address findings only: ``"tú"`` or ``"usted"``.
    expected_form: Optional[str] = None

    def text(self) -> str:
        """Message, excerpt and suggestion joined, for term matching."""
        return f"{self.message}\n{self.location}\n{self.suggestion}"


@dataclass
class ChunkInfo:
    id: str
    chapter_id: str
    source_text: str
    translated_text: str
    translated_at: Optional[str]
    last_llm_log: Optional[str]
    #: Lower-cased English keys of the glossary block in the logged prompt, or
    #: ``None`` when no readable log exists for this chunk.
    shipped_glossary: Optional[set[str]] = None


@dataclass
class BookCorpus:
    project_dir: Path
    chunks: dict[str, ChunkInfo]
    findings: list[Finding]
    glossary: list[dict[str, Any]]
    style_text: str
    style_meta: dict[str, Any]
    address_map: Optional[dict[str, Any]]
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return self.project_dir.name

    def chunks_in_order(self) -> list[ChunkInfo]:
        return [self.chunks[k] for k in sorted(self.chunks)]


def chapter_of(chunk_id: str) -> str:
    match = _CHAPTER_RE.search(chunk_id or "")
    return match.group(1) if match else (chunk_id or "")


def theme_of(rule: str, category: Optional[str], message: str) -> str:
    """Fold a free-form rule id into a fixed theme; the category is the fallback.

    The message is read only when there is no rule id at all: prose that merely
    quotes «usted» or «parecía» would otherwise pull a calque into ``address``.
    """
    probe = rule if rule and rule != "(none)" else message[:80]
    probe = probe.lower()
    for name, pattern in _THEMES:
        if pattern.search(probe):
            return name
    return (category or "other").lower()


def _rule_of(issue: dict[str, Any]) -> str:
    explicit = issue.get("rule_id") or issue.get("rule")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    match = _BRACKET_RULE_RE.match(issue.get("message") or "")
    return match.group(1).strip() if match else "(none)"


def _expected_form(rule: str, message: str) -> Optional[str]:
    probe = f"{rule} {message}".lower()
    if "usted-expected" in probe:
        return "usted"
    if "tu-expected" in probe:
        return "tú"
    match = _EXPECTED_RE.search(message or "")
    if not match:
        return None
    return "usted" if match.group(1).lower().startswith("usted") else "tú"


def _pair_of(message: str) -> Optional[tuple[str, str]]:
    match = _PAIR_RE.search(message or "")
    if not match:
        return None
    return match.group(1).strip(), match.group(2).strip()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Unreadable %s: %s", path, exc)
        return None


_GLOSSARY_HEADER = "GLOSSARY TERMS"
_GLOSSARY_LINE_RE = re.compile(r"^- (.+?) → ")


def parse_shipped_glossary(prompt: str) -> set[str]:
    """English keys of the ``GLOSSARY TERMS`` block of a rendered translation prompt.

    The block runs from its header to the next ``====`` banner, one
    ``- english → spanish`` line per entry (``format_glossary_for_prompt``).
    """
    start = prompt.find(_GLOSSARY_HEADER)
    if start < 0:
        return set()
    body = prompt[start + len(_GLOSSARY_HEADER):]
    # Skip the banner that closes the header itself, then stop at the next one.
    body = re.sub(r"^\s*=+\s*", "", body, count=1)
    end = re.search(r"^=+\s*$", body, re.MULTILINE)
    if end:
        body = body[: end.start()]
    keys: set[str] = set()
    for line in body.splitlines():
        match = _GLOSSARY_LINE_RE.match(line.strip())
        if match:
            keys.add(match.group(1).strip().lower())
    return keys


def _shipped_from_log(log_path: Optional[str], repo_root: Path) -> Optional[set[str]]:
    if not log_path:
        return None
    path = Path(log_path)
    if not path.is_absolute():
        path = repo_root / path
    doc = _read_json(path)
    if not isinstance(doc, dict) or not isinstance(doc.get("prompt"), str):
        return None
    return parse_shipped_glossary(doc["prompt"])


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_chunks(project_dir: Path, repo_root: Path) -> dict[str, ChunkInfo]:
    out: dict[str, ChunkInfo] = {}
    for path in sorted((project_dir / "chunks").glob("*.json")):
        doc = _read_json(path)
        if not isinstance(doc, dict):
            continue
        chunk_id = doc.get("id") or path.stem
        log = doc.get("last_llm_log")
        out[chunk_id] = ChunkInfo(
            id=chunk_id,
            chapter_id=doc.get("chapter_id") or chapter_of(chunk_id),
            source_text=doc.get("source_text") or "",
            translated_text=doc.get("translated_text") or "",
            translated_at=doc.get("translated_at"),
            last_llm_log=log,
            shipped_glossary=_shipped_from_log(log, repo_root),
        )
    return out


def _marks_by_chunk(project_dir: Path):
    from web_ui.evaluations import build_dismissed, load_all_feedback_by_chunk

    return {
        chunk_id: build_dismissed(records)
        for chunk_id, records in load_all_feedback_by_chunk(project_dir).items()
    }


def _normalize_issue(
    judge: str, chunk_id: str, index: int, issue: dict[str, Any], status: str,
    executed_at: Optional[str],
) -> Finding:
    from web_ui.evaluations import issue_key

    rule = _rule_of(issue)
    message = str(issue.get("message") or issue.get("reason") or "")
    location = issue.get("location") if issue.get("location") is not None else issue.get("excerpt")
    if isinstance(location, dict):
        location = location.get("raw")
    key = issue_key(judge, issue) if status == "kept" else issue.get("finding_key")
    finding = Finding(
        id=f"{judge}:{chunk_id}:{status.split(':')[0]}:{index}",
        judge=judge,
        chunk_id=chunk_id,
        chapter_id=chapter_of(chunk_id),
        status=status,
        rule=rule,
        theme=theme_of(rule, issue.get("category"), message),
        category=issue.get("category"),
        severity=issue.get("severity"),
        message=message,
        location=str(location or ""),
        suggestion=str(issue.get("suggestion") or ""),
        issue_key=key,
        executed_at=executed_at,
    )
    if judge == "address":
        finding.pair = _pair_of(message)
        finding.expected_form = _expected_form(rule, message)
    return finding


def _load_findings(project_dir: Path) -> list[Finding]:
    from web_ui.evaluations import feedback_mark

    marks = _marks_by_chunk(project_dir)
    findings: list[Finding] = []
    for path in sorted((project_dir / "evaluations").glob("chapter_*.json")):
        doc = _read_json(path)
        if not isinstance(doc, dict):
            continue
        chunk_id = doc.get("chunk_id") or path.stem
        by_key, by_index = marks.get(chunk_id, ({}, {}))
        for judge in JUDGES:
            result = (doc.get("judges") or {}).get(judge)
            if not isinstance(result, dict):
                continue
            executed_at = result.get("executed_at")
            for i, issue in enumerate(result.get("issues") or []):
                if not isinstance(issue, dict):
                    continue
                finding = _normalize_issue(judge, chunk_id, i, issue, "kept", executed_at)
                record = feedback_mark(by_key, by_index, judge, i, issue)
                finding.mark = record.get("feedback_type") if record else None
                findings.append(finding)
            metadata = result.get("metadata") or {}
            for i, item in enumerate(metadata.get("retracted") or []):
                if isinstance(item, dict):
                    findings.append(
                        _normalize_issue(judge, chunk_id, i, item, "retracted", executed_at)
                    )
            for i, item in enumerate(metadata.get("filtered") or []):
                if isinstance(item, dict):
                    reason = item.get("drop_reason") or "unknown"
                    findings.append(_normalize_issue(
                        judge, chunk_id, i, item, f"filtered:{reason}", executed_at,
                    ))
    return findings


def _load_history(project_dir: Path) -> list[dict[str, Any]]:
    path = project_dir / "decisions_history.jsonl"
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_book(project_dir: Path, *, repo_root: Optional[Path] = None) -> BookCorpus:
    """Read one book's chunks, judge findings, marks and artifacts."""
    project_dir = Path(project_dir)
    repo_root = Path(repo_root) if repo_root else _repo_root()

    glossary_doc = _read_json(project_dir / "glossary.json")
    if isinstance(glossary_doc, dict):
        terms = glossary_doc.get("terms") or []
    elif isinstance(glossary_doc, list):
        terms = glossary_doc
    else:
        terms = []
    style_doc = _read_json(project_dir / "style.json")
    address_doc = _read_json(project_dir / "address_map.json")

    return BookCorpus(
        project_dir=project_dir,
        chunks=_load_chunks(project_dir, repo_root),
        findings=_load_findings(project_dir),
        glossary=[t for t in terms if isinstance(t, dict) and t.get("english")],
        style_text=(style_doc or {}).get("content") or "" if isinstance(style_doc, dict) else "",
        style_meta=style_doc if isinstance(style_doc, dict) else {},
        address_map=address_doc if isinstance(address_doc, dict) else None,
        history=_load_history(project_dir),
    )
