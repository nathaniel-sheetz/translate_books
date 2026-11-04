"""
The per-user Bible index: an EPUB read into ``{"BB:CCC:VVV": text}``.

Ported from ``add_nbla.py`` ``build_index`` / ``clean_text`` with three changes
the quotation pass needs:

- **Poetry line starts are kept.** Each verse text carries a ``\\n`` before every
  word that begins a poetry line (the NBLA's ``sl`` / ``slf`` / ``sll`` /
  ``sls`` paragraphs), including a leading ``\\n`` when the verse itself opens a
  line. :mod:`src.quotations.conventions` lowercases those capitals when a
  quotation runs inline, and needs to know which capitals are line starts and
  which are names. :func:`plain` strips the markers.
- **Small capitals are read as lower case.** The NBLA sets Old Testament
  quotations in the New Testament in small caps
  (``<span>D</span><span class="sm1">ICE EL</span>``, ``red-sm`` in the words of
  Jesus); a plain tag strip turned
  that into "DICE EL SEÑOR". Lower-casing the ``sm1`` runs recovers the real
  sentence case ("Dice el Señor"), names included. The divine name ``SEÑOR`` /
  ``DIOS`` is a bare span and stays in capitals.
- **Only text paragraphs are read.** Headings, Psalm titles, speaker labels and
  the glossary lines between verses are dropped by class rather than by a list
  of heading classes, and ``ver3`` markers (missed by the old regex) count.

The index is gitignored under ``prompts/bible/`` and records the SHA-256 of the
EPUB it came from. :func:`load_index` refuses a missing index and one whose EPUB
has since changed. There is no tracked fallback.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.quotations import INDEX_DIR

LINE = "\n"

#: Paragraph classes that hold verse text in the NBLA EPUB.
PROSE_CLASSES = frozenset({"calibre8", "pf", "pf1", "pf2", "psec", "lt"})
POETRY_CLASSES = frozenset({"sl", "slf", "sll", "sls"})
TEXT_CLASSES = PROSE_CLASSES | POETRY_CLASSES

_PARA_RE = re.compile(r'<p class="([^"]+)"[^>]*>(.*?)</p>', re.DOTALL)
# The number can carry a page anchor (``<span …>​<a id="GBS…"></a>117</span>``).
_VERSE_RE = re.compile(r'<span class="ver\d?" id="v(\d{2})(\d{3})(\d{3})">.*?</span>')
_ENREF_RE = re.compile(r'<a class="enref"[^>]*>[^<]*</a>')
_SUP_RE = re.compile(r"<sup[^>]*>[^<]*</sup>")
# Small caps: ``sm1``, ``sm``, ``sm-b``, and ``red-sm`` for the words of Jesus.
_SMALLCAPS_RE = re.compile(r'<span class="(?:red-)?sm(?:\d|-b)?">([^<]*)</span>')
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v ]+")


class IndexError_(Exception):
    """The index is missing, stale, or not the version asked for."""


def epub_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def clean_fragment(fragment: str) -> str:
    """One paragraph fragment as plain text: notes out, small caps lowered."""
    # Empty page anchors sit anywhere, small-caps runs included.
    s = re.sub(r"<a [^>]*></a>", "", fragment)
    s = _ENREF_RE.sub("", s)
    s = _SUP_RE.sub("", s)
    s = _SMALLCAPS_RE.sub(lambda m: m.group(1).lower(), s)
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    # "dijo*": the NBLA's mark for a present tense rendered as past. A study
    # aid, not wording; a book quoting the verse leaves it out.
    s = re.sub(r"(?<=\w)\*", "", s)
    return _WS_RE.sub(" ", s)


def _verses_in_document(doc: str) -> dict[str, str]:
    """``{key: text-with-line-markers}`` for one XHTML document."""
    out: dict[str, list[str]] = {}
    current: Optional[str] = None
    for para in _PARA_RE.finditer(doc):
        cls, inner = para.group(1), para.group(2)
        if cls not in TEXT_CLASSES:
            continue
        poetry = cls in POETRY_CLASSES
        pos = 0
        pending_line = poetry
        markers = list(_VERSE_RE.finditer(inner))
        bounds = [(m.start(), m.end(), f"{m.group(1)}:{m.group(2)}:{m.group(3)}") for m in markers]
        segments: list[tuple[Optional[str], str]] = []
        for start, end, key in bounds:
            segments.append((None, inner[pos:start]))
            segments.append((key, ""))
            pos = end
        segments.append((None, inner[pos:]))
        for key, fragment in segments:
            if key is not None:
                current = key
                out.setdefault(current, [])
                continue
            text = clean_fragment(fragment)
            if current is None or not text.strip():
                continue
            parts = out[current]
            if pending_line:
                parts.append(LINE + text.lstrip())
                pending_line = False
            elif parts and not parts[-1].endswith((" ", LINE)) and not text.startswith(" "):
                # A new prose paragraph inside the same verse.
                parts.append(" " + text)
            else:
                parts.append(text)
    return {k: _tidy("".join(v)) for k, v in out.items()}


def _tidy(text: str) -> str:
    text = re.sub(r" *\n *", LINE, text)
    text = re.sub(r" {2,}", " ", text)
    return text.rstrip()


def plain(text: str) -> str:
    """Verse text without line markers, whitespace collapsed."""
    return re.sub(r"\s+", " ", text).strip()


def proper_words(verses: dict[str, str], *, ratio: float = 0.9, min_count: int = 3) -> list[str]:
    """Words the NBLA capitalises wherever they fall: names, mostly.

    Counted only away from sentence and line starts. A word capitalised at
    least ``ratio`` of the time there is treated as a name, so "Jerusalén",
    "Israel" and "Dios" qualify while "Mi" and "Tu" (reverential only when God
    speaks) do not. The conventions keep these capitalised.
    """
    caps: Counter[str] = Counter()
    lower: Counter[str] = Counter()
    word_re = re.compile(r"(?<![\wÁÉÍÓÚÑÜáéíóúñü])([A-Za-zÁÉÍÓÚÑÜáéíóúñü]+)")
    for text in verses.values():
        for line in text.split(LINE):
            prev = ""
            for match in word_re.finditer(line):
                word = match.group(1)
                before = line[: match.start()].rstrip()
                at_start = not before or before[-1] in ".!?¿¡«“\"'(—:;" or not prev
                prev = word
                if at_start or word.isupper():
                    continue
                if word[0].isupper():
                    caps[word.lower()] += 1
                else:
                    lower[word.lower()] += 1
    out = []
    for word, n in caps.items():
        if n >= min_count and n >= ratio * (n + lower.get(word, 0)):
            out.append(word)
    return sorted(out)


def build_index(epub: Path, *, version: str) -> dict[str, Any]:
    """Read ``epub`` into an index document (not written; see :func:`write_index`)."""
    epub = Path(epub)
    verses: dict[str, str] = {}
    title = ""
    with zipfile.ZipFile(epub) as zf:
        for name in sorted(zf.namelist()):
            if name.endswith(".opf"):
                opf = zf.read(name).decode("utf-8", errors="replace")
                found = re.search(r"<dc:title[^>]*>([^<]*)</dc:title>", opf)
                title = html.unescape(found.group(1)).strip() if found else title
        for name in sorted(zf.namelist()):
            if not name.endswith((".xhtml", ".html", ".htm")):
                continue
            doc = zf.read(name).decode("utf-8", errors="replace")
            if 'id="v' not in doc:
                continue
            for key, text in _verses_in_document(doc).items():
                if key not in verses and text.strip():
                    verses[key] = text
    return {
        "version": version,
        "title": title,
        "epub_path": str(epub.resolve()),
        "epub_sha256": epub_sha256(epub),
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "verse_count": len(verses),
        # Small caps were read from markup, so capitals left in the text are the
        # NBLA's own ("YO SOY EL QUE SOY", the inscription on the cross).
        "smallcaps_resolved": True,
        "proper_words": proper_words(verses),
        "verses": verses,
    }


def index_path(version: str) -> Path:
    return INDEX_DIR / f"{version.lower()}.json"


def write_index(doc: dict[str, Any], *, path: Optional[Path] = None) -> Path:
    target = Path(path) if path else index_path(doc["version"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return target


class BibleIndex:
    """A loaded index: verse lookup plus what the conventions need."""

    def __init__(self, doc: dict[str, Any]):
        self.doc = doc
        self.version: str = doc.get("version", "")
        self.verses: dict[str, str] = doc.get("verses") or {}
        self.proper: frozenset[str] = frozenset(doc.get("proper_words") or ())

    def get(self, key: str) -> Optional[str]:
        return self.verses.get(key)

    def chapter_keys(self, book: int, chapter: int) -> list[str]:
        prefix = f"{book:02d}:{chapter:03d}:"
        return sorted(k for k in self.verses if k.startswith(prefix))


def load_index(
    version: str = "nbla", *, path: Optional[Path] = None, check_epub: bool = True
) -> BibleIndex:
    """The index for ``version``, or :class:`IndexError_` with the fix in the message.

    A changed EPUB is an error. An EPUB no longer at its recorded path is not:
    the index is still what that EPUB said, and refusing would force the user
    to keep a copy in one place forever.
    """
    target = Path(path) if path else index_path(version)
    if not target.exists():
        raise IndexError_(
            f"no {version.upper()} index at {target}. Build it from your own copy: "
            f"`python -X utf8 scripts/quotation_pass.py index --epub <path-to-epub> "
            f"--version {version}`"
        )
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IndexError_(f"unreadable index {target}: {exc}") from exc
    if str(doc.get("version", "")).lower() != version.lower():
        raise IndexError_(
            f"{target} is a {doc.get('version')!r} index, not {version!r}"
        )
    if not doc.get("verses"):
        raise IndexError_(f"{target} holds no verses; rebuild it with `index --epub`")
    if check_epub:
        epub = Path(doc.get("epub_path") or "")
        if doc.get("epub_path") and epub.exists():
            if epub_sha256(epub) != doc.get("epub_sha256"):
                raise IndexError_(
                    f"the EPUB at {epub} has changed since the {version.upper()} index "
                    f"was built; rebuild it with `index --epub \"{epub}\" --version {version}`"
                )
    return BibleIndex(doc)
