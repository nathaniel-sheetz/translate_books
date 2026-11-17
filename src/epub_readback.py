"""Read a built EPUB back: its pages, its endnotes, and the note counts its pages state.

A book states how many notes it carries in several places written at different
times: the credits page (``edition_notice.json``), «Sobre este libro»
(``about_book.json``), the site's «Sobre esta edición» and the store
description. The built EPUB is the one place the real number can be read, so
every check that compares a stated count goes through this module:

- ``count_endnotes`` / ``endnotes`` read ``endnotes.xhtml``;
- ``stated_note_counts`` finds the counts a text states («29 notas», «una nota
  del traductor», «no lleva notas»);
- ``memo_like`` names a note that reads like a reader's memo saved as a note.

The wording is Spanish because the books are.
"""

from __future__ import annotations

import re
import zipfile
from html import unescape
from pathlib import Path
from typing import Any, Optional

ENDNOTES_PAGE = "endnotes.xhtml"
ABOUT_BOOK_PAGE = "about_book.xhtml"
COPYRIGHT_PAGE = "copyright.xhtml"

_ENDNOTE_ANCHOR_RE = re.compile(r'<a id="en-\d+"')
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(
    r"<h(?P<level>[23])[^>]*>(?P<heading>.*?)</h(?P=level)>"
    r'|<p class="endnote">\s*<a id="en-(?P<number>\d+)"[^>]*>.*?</a>(?P<text>.*?)</p>',
    re.DOTALL)

# «29 notas», and the English a copyright basis is written in: "28 editorial notes".
_DIGIT_COUNT_RE = re.compile(
    r"(?<![\w.,])(\d+)\s+(?:(?:editorial|translator['’]?s?|editor['’]?s?)\s+)?"
    r"(?:notas?|(?:foot|end)?notes?)\b", re.IGNORECASE)
# «5 notas» with no qualifier before it; «del traductor» after it makes it the total.
_BARE_SPANISH_RE = re.compile(r"\d+\s+notas?", re.IGNORECASE)
_DEL_TRADUCTOR_RE = re.compile(r"\s+del\s+traductor\b", re.IGNORECASE)
# «una nota» alone is ordinary prose («una nota lo corrige»); only the phrase the
# house sentence uses counts the edition's notes.
_ONE_NOTE_RE = re.compile(r"\buna\s+nota\s+del\s+traductor\b", re.IGNORECASE)
_NO_NOTES_RE = re.compile(r"\bno\s+lleva\s+notas\b", re.IGNORECASE)
_NUMBER_WORD = (
    r"dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce|trece|catorce|quince"
    r"|dieci\w+|veinte|veinti\w+|treinta|cuarenta|cincuenta|sesenta|setenta|ochenta|noventa"
    r"|cien|ciento")
_WORD_COUNT_RE = re.compile(
    rf"\b((?:{_NUMBER_WORD})(?:\s+y\s+\w+)?)\s+notas\b", re.IGNORECASE)

# The markup of the generated pages (copyright page, «Sobre este libro»), as
# src.epub_builder renders it: a tag at the start of a paragraph, a link to
# http(s)/mailto or to a variable, and the two variables spelled exactly.
_TARGET_TAG_RE = re.compile(r"^\[(LIBRE|KINDLE)\][ \t]*\n?", re.IGNORECASE)
_ANY_TARGET_TAG_RE = re.compile(r"\[(LIBRE|KINDLE)\]", re.IGNORECASE)
_ANY_VAR_RE = re.compile(r"\{\{[^{}\n]*\}\}")
PAGE_VARS = ("{{SITIO}}", "{{COMENTARIOS}}")
_ANY_LINK_RE = re.compile(r"\[[^\[\]\n]+\]\(([^)\n]*)\)")
_GOOD_URL_RE = re.compile(r"(?:https?://|mailto:|\{\{(?:SITIO|COMENTARIOS)\}\})[^\s()]*")
# Markdown these pages do not render: it would show as typed.
_LITERAL_LINE_RE = re.compile(r"^\s*(?:[-*+]\s+\S|\d+[.)]\s+\S|#{1,6}\s+\S|>\s*\S)")

#: Above this length a note is taken for a gloss whatever it says.
MEMO_MAX_CHARS = 60
_SENTENCE_END = (".", "!", "»", '"', "”", ")", "…")
# What a reader's memo to themselves looks like when it was saved as a note.
_MEMO_WORD_RE = re.compile(
    r"\b(?:missing|line break|citation|todo|fixme|typo|check this|explicar|revisar|falta)\b",
    re.IGNORECASE)
# «Sal 116:11», «1 Jn 4:8», «Mateo 6»: a bare Scripture reference is a real note.
_BARE_REFERENCE_RE = re.compile(r"^(?:[1-3]\s?)?[A-ZÁÉÍÓÚ][\wáéíóúñ]*\.?\s+\d+(?:[:.,;\s–-]+\d+)*$")


def newest_epub(project_dir: Path) -> Optional[Path]:
    """The most recently written ``*.epub`` in the project folder: the Kindle build."""
    epubs = sorted(Path(project_dir).glob("*.epub"), key=lambda p: p.stat().st_mtime)
    return epubs[-1] if epubs else None


def libre_epub(project_dir: Path) -> Optional[Path]:
    """The most recently written free-library build, ``libre/*.epub``."""
    epubs = sorted((Path(project_dir) / "libre").glob("*.epub"), key=lambda p: p.stat().st_mtime)
    return epubs[-1] if epubs else None


def plain(xhtml: str) -> str:
    """A page's text with the tags gone and the entities resolved."""
    return " ".join(unescape(_TAG_RE.sub(" ", xhtml)).split())


def page_texts(epub_path: Path) -> dict[str, str]:
    """``{file name: xhtml}`` for every XHTML page in the EPUB, keyed without its folder."""
    out: dict[str, str] = {}
    with zipfile.ZipFile(epub_path) as zf:
        for name in zf.namelist():
            if name.endswith((".xhtml", ".html")):
                out[name.rsplit("/", 1)[-1]] = zf.read(name).decode("utf-8", "replace")
    return out


def count_endnotes_in(xhtml: str) -> int:
    return len(_ENDNOTE_ANCHOR_RE.findall(xhtml))


def count_endnotes(epub_path: Path) -> int:
    """How many notes the built EPUB carries (0 when it has no notes page)."""
    return count_endnotes_in(page_texts(epub_path).get(ENDNOTES_PAGE, ""))


def parse_endnotes(xhtml: str) -> list[dict[str, Any]]:
    """Every note on a notes page: ``{number, chapter, text}``, in book order.

    ``chapter`` is the heading the note sits under, as the EPUB shows it. In a
    collection the work's title (``<h2>``) comes before the chapter (``<h3>``):
    ``"Obra · Capítulo"``.
    """
    notes: list[dict[str, Any]] = []
    work = chapter = ""
    for match in _BLOCK_RE.finditer(xhtml):
        if match.group("heading") is not None:
            heading = plain(match.group("heading"))
            if match.group("level") == "2":
                work, chapter = heading, ""
            else:
                chapter = heading
            continue
        where = f"{work} · {chapter}" if work and chapter else (chapter or work)
        notes.append({"number": int(match.group("number")), "chapter": where,
                      "text": plain(match.group("text"))})
    return notes


def endnotes(epub_path: Path) -> list[dict[str, Any]]:
    return parse_endnotes(page_texts(epub_path).get(ENDNOTES_PAGE, ""))


def memo_like(text: str) -> str:
    """``"memo"``, ``"short"`` or ``""`` for one note's text.

    ``memo`` is a note that reads like a reader's memo to themselves («missing
    line break», «citation?», «explicar? lo que cubre»): a question, or a word
    no gloss uses. ``short`` is a brief note that does not end like a sentence
    («Roma»), which may be a finished gloss or an unfinished one: worth a look,
    not a defect by itself. A bare Scripture reference is neither.
    """
    text = text.strip()
    if len(text) >= MEMO_MAX_CHARS:
        return ""
    if "?" in text or _MEMO_WORD_RE.search(text):
        return "memo"
    if text.endswith(_SENTENCE_END) or _BARE_REFERENCE_RE.match(text):
        return ""
    return "short"


def stated_note_counts(text: str) -> dict[str, Any]:
    """The note counts ``text`` states.

    ``counts`` are the numbers given in digits, plus 1 for «una nota del
    traductor»; ``bare`` are those of ``counts`` written «N notas» with nothing
    saying they are the edition's notes, which may count a subset («5 notas
    ponen al día la ciencia»); ``says_none`` is «no lleva notas»; ``in_words`` lists counts
    spelled out («veintinueve notas»), which cannot be compared and so are
    reported instead.
    """
    counts: list[int] = []
    bare: list[int] = []
    for match in _DIGIT_COUNT_RE.finditer(text):
        n = int(match.group(1))
        counts.append(n)
        if _BARE_SPANISH_RE.fullmatch(match.group(0)) and not _DEL_TRADUCTOR_RE.match(
                text, match.end()):
            bare.append(n)
    counts += [1] * len(_ONE_NOTE_RE.findall(text))
    return {
        "counts": counts,
        "bare": bare,
        "says_none": bool(_NO_NOTES_RE.search(text)),
        "in_words": [m.group(0) for m in _WORD_COUNT_RE.finditer(text)],
    }


def page_markup_issues(field: str, body: str) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` for markup in a generated page's body the build would print as typed.

    The copyright page and «Sobre este libro» go through one renderer, which
    understands only a [LIBRE]/[KINDLE] tag opening a paragraph, ``[text](url)``
    with an http(s)/mailto URL or a variable, and ``{{SITIO}}``/``{{COMENTARIOS}}``
    spelled exactly. Anything else reaches the reader literally.
    """
    errors: list[str] = []
    warnings: list[str] = []
    if "**" in body:
        errors.append(f"{field}: bold (**…**) is not rendered on this page; it would show as typed")
    for block in (b.strip() for b in re.split(r"\n\s*\n", body)):
        if not block:
            continue
        tagged = _TARGET_TAG_RE.match(block)
        rest = block[tagged.end():] if tagged else block
        head = rest[:40].replace("\n", " ")
        if _ANY_TARGET_TAG_RE.search(rest):
            errors.append(f"{field}: a [LIBRE]/[KINDLE] tag only works at the start of a paragraph "
                          f"(«{head}…»)")
        for line in rest.split("\n"):
            if _LITERAL_LINE_RE.match(line):
                errors.append(f"{field}: lists, subheadings and block quotes are not rendered on "
                              f"this page («{line.strip()[:40]}…»)")
                break
        has_link = False
        for match in _ANY_LINK_RE.finditer(rest):
            url = match.group(1).strip()
            if url and not _GOOD_URL_RE.fullmatch(url):
                errors.append(f"{field}: the link ({url}) is not https://, mailto: or a variable; "
                              f"the build would print it as typed")
            has_link = True
        has_link = has_link or any(v in PAGE_VARS for v in _ANY_VAR_RE.findall(rest))
        if has_link and not (tagged and tagged.group(1).upper() == "LIBRE"):
            warnings.append(f"{field}: a link outside a [LIBRE] paragraph shows in the Kindle build "
                            f"as bare text («{head}…»)")
    for var in _ANY_VAR_RE.findall(body):
        if var not in PAGE_VARS:
            errors.append(f"{field}: unknown variable {var} (known: {{{{SITIO}}}}, {{{{COMENTARIOS}}}}, "
                          "in capitals and without spaces)")
    return errors, warnings


def note_count_issues(field: str, text: str, endnotes_count: Optional[int],
                      *, expect_count: bool = True) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` for the counts ``text`` states against the built EPUB.

    ``endnotes_count`` is ``None`` when there is no EPUB to read. With
    ``expect_count``, a text that gives no count for a book that has notes gets
    a warning.
    """
    errors: list[str] = []
    warnings: list[str] = []
    stated = stated_note_counts(text)
    for phrase in stated["in_words"]:
        warnings.append(f"{field}: «{phrase}» gives the count in words; write it in digits so "
                        "it can be checked against the built EPUB")
    if endnotes_count is None:
        if stated["counts"] or stated["says_none"]:
            warnings.append(f"{field}: states a note count, but there is no built EPUB to check "
                            "it against")
        return errors, warnings
    bare = list(stated["bare"])
    for n in stated["counts"]:
        if n == endnotes_count:
            if n in bare:
                bare.remove(n)
            continue
        if n in bare and n < endnotes_count:
            # «5 notas ponen al día la ciencia»: a part of the notes, or a stale total.
            bare.remove(n)
            warnings.append(f"{field}: says {n} nota{'s' if n != 1 else ''} and the built EPUB "
                            f"has {endnotes_count}; fine if it counts some of them, wrong if it "
                            "means all (the total is written «N notas del traductor»)")
            continue
        errors.append(f"{field}: says {n} nota{'s' if n != 1 else ''}, the built EPUB has "
                      f"{endnotes_count}")
    if stated["says_none"] and endnotes_count:
        errors.append(f"{field}: says the edition has no notes, the built EPUB has {endnotes_count}")
    if expect_count and endnotes_count and not (
            stated["counts"] or stated["says_none"] or stated["in_words"]):
        warnings.append(f"{field}: the built EPUB has {endnotes_count} notes and the text gives "
                        "no count")
    return errors, warnings
