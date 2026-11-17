"""
Read a built EPUB back and confirm the listing's metadata landed.

The build is several hops from ``apply`` (project.json -> book_metadata ->
build_epub / build_epub_from_chunks -> ebooklib), and any one of them can drop
a field quietly. This opens the zip, reads the OPF and the spine, and checks
each thing ``apply`` asked for against what is actually in the file.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Optional

from src import epub_readback
from src.book_metadata import load_book_metadata
from src.kdp.gutenberg import names_trademark
from src.kdp.validate import _DESCRIPTOR_RE

_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
}

_EXTERNAL_LINK_RE = re.compile(r'<a\b[^>]*\bhref="(?:https?:|mailto:)', re.IGNORECASE)
# A tag, any {{…}} variable (spaced or misspelled ones too), or a Markdown link
# the renderer left as typed because its URL was not http(s)/mailto.
_LEFTOVER_MARKUP_RE = re.compile(
    r'\[(?:LIBRE|KINDLE)\]|\{\{[^{}\n]*\}\}|\[[^\[\]\n<>]+\]\([^)\s<>]*\)', re.IGNORECASE)


KINDLE, LIBRE = "kindle", "libre"

#: The generated pages whose stated note counts must match the notes page.
_COUNTED_PAGES = (epub_readback.COPYRIGHT_PAGE, epub_readback.ABOUT_BOOK_PAGE)

newest_epub = epub_readback.newest_epub


def read_opf(epub_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(epub_path) as zf:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        rootfile = container.find(".//c:rootfile", _NS).get("full-path")
        opf = ET.fromstring(zf.read(rootfile))
        base = str(Path(rootfile).parent).replace("\\", "/")
        base = "" if base == "." else base + "/"
        manifest = {
            item.get("id"): item.get("href")
            for item in opf.findall("opf:manifest/opf:item", _NS)
        }
        spine = [manifest.get(ref.get("idref"), "") for ref in opf.findall("opf:spine/opf:itemref", _NS)]
        texts = {}
        for href in manifest.values():
            if href and href.endswith((".xhtml", ".html")):
                try:
                    texts[href] = zf.read(base + href).decode("utf-8", "replace")
                except KeyError:
                    continue
    meta = opf.find("opf:metadata", _NS)

    def _all(tag: str) -> list[ET.Element]:
        return meta.findall(f"dc:{tag}", _NS) if meta is not None else []

    refines = {}
    for m in meta.findall("opf:meta", _NS) if meta is not None else []:
        if m.get("refines") and m.get("property"):
            refines.setdefault(m.get("refines").lstrip("#"), {})[m.get("property")] = (m.text or "").strip()
    return {
        "titles": [(t.get("id"), (t.text or "").strip()) for t in _all("title")],
        "creators": [(c.text or "").strip() for c in _all("creator")],
        "contributors": [((c.text or "").strip(), refines.get(c.get("id") or "", {}).get("role"))
                         for c in _all("contributor")],
        "description": " ".join((d.text or "").strip() for d in _all("description")),
        "subjects": [(s.text or "").strip() for s in _all("subject")],
        "dates": [(d.text or "").strip() for d in _all("date")],
        "publisher": " ".join((p.text or "").strip() for p in _all("publisher")),
        "rights": " ".join((r.text or "").strip() for r in _all("rights")),
        "language": " ".join((lang.text or "").strip() for lang in _all("language")),
        "refines": refines,
        "spine": spine,
        "texts": texts,
    }


def _authors_landed(project_dir: Path, author: str, creators: list[str]) -> bool:
    """A book's author is one ``dc:creator``; a collection carries one per member author.

    A collection's volume author may be a joined "A y B" or a label of its own
    ("Varios autores"), so it is checked against the members instead.
    """
    from src import collection as collection_mod

    if collection_mod.is_collection_dir(project_dir):
        expected = collection_mod.member_authors(collection_mod.load_collection(project_dir))
        return bool(creators) and all(a in creators for a in expected)
    return author in creators


def _note_checks(texts: dict[str, str]) -> list[tuple[str, bool, str]]:
    """The notes page against the pages that count it, and against memo-like notes."""
    by_name = {href.rsplit("/", 1)[-1]: text for href, text in texts.items()}
    notes_page = by_name.get(epub_readback.ENDNOTES_PAGE, "")
    notes = epub_readback.parse_endnotes(notes_page)
    total = epub_readback.count_endnotes_in(notes_page)
    wrong: list[str] = []
    for name in _COUNTED_PAGES:
        if name not in by_name:
            continue
        errors, _warnings = epub_readback.note_count_issues(
            name, epub_readback.plain(by_name[name]), total, expect_count=False)
        wrong.extend(errors)
    kinds: dict[str, list[str]] = {"memo": [], "short": []}
    for note in notes:
        kind = epub_readback.memo_like(note["text"])
        if kind:
            kinds[kind].append(f"{note['number']} («{note['text']}»)")
    # Only a memo fails the build; a short note is named so someone looks at it.
    parts = []
    if kinds["memo"]:
        parts.append(f"read like memos, not notes: {', '.join(kinds['memo'])}")
    if kinds["short"]:
        parts.append(f"short and without a final period, worth a look: {', '.join(kinds['short'])}")
    return [
        ("note_counts_agree", not wrong,
         "; ".join(wrong) if wrong else f"{total} notes, and no page says otherwise"),
        ("footnotes_look_like_glosses", not kinds["memo"],
         "; ".join(parts) if parts else f"{len(notes)} notes read"),
    ]


def verify(project_dir: Path, epub_path: Optional[Path] = None, *,
           target: str = KINDLE) -> dict[str, Any]:
    """Check a built EPUB against ``project.json``.

    ``target`` is the build being checked. ``kindle`` (the default, what goes to
    KDP) may carry no live link; ``libre`` (the free-library build) carries
    them by design, so that one check is left out. With no ``epub_path`` the
    newest build of that target is read.
    """
    project_dir = Path(project_dir)
    if target not in (KINDLE, LIBRE):
        return {"ok": False, "epub": None, "checks": [], "target": target,
                "error": f"unknown target {target!r} (expected {KINDLE} or {LIBRE})"}
    if epub_path is None:
        epub_path = (epub_readback.libre_epub(project_dir) if target == LIBRE
                     else newest_epub(project_dir))
    epub_path = Path(epub_path) if epub_path else None
    if not epub_path or not epub_path.exists():
        hint = "harness.py epub --target libre" if target == LIBRE else "harness.py epub"
        return {"ok": False, "epub": None, "checks": [], "target": target,
                "error": f"no EPUB found; build one with {hint} first"}
    want = load_book_metadata(project_dir)
    got = read_opf(epub_path)
    checks: list[dict[str, Any]] = []

    def _check(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(passed), "detail": detail})

    main_titles = [t for i, t in got["titles"] if i != "subtitle"]
    main_title = main_titles[0] if main_titles else ""
    _check("title", main_title == want["title"], f"OPF {main_title!r}, project.json {want['title']!r}")
    _check("title_has_no_descriptor", not _DESCRIPTOR_RE.search(main_title),
           "the KDP descriptor belongs in the KDP title field, not the book")
    if want["subtitle"]:
        subtitle = dict(got["titles"]).get("subtitle", "")
        _check("subtitle", subtitle == want["subtitle"]
               and got["refines"].get("subtitle", {}).get("title-type") == "subtitle",
               f"OPF {subtitle!r}")
    _check("author", _authors_landed(project_dir, want["author"], got["creators"]),
           f"OPF {got['creators']}")
    if want["translator"]:
        _check("translator_trl", (want["translator"], "trl") in got["contributors"],
               f"OPF {got['contributors']}")
    for key in ("description", "publisher", "rights"):
        if want[key]:
            _check(key, bool(got[key].strip()), "present" if got[key].strip() else "missing")
    if want["subjects"]:
        missing = [s for s in want["subjects"] if s not in got["subjects"]]
        _check("subjects", not missing, f"missing {missing}" if missing else f"{len(got['subjects'])} present")
    if want["date"]:
        _check("date", want["date"] in got["dates"], f"OPF {got['dates']}")

    spine = got["spine"]
    if (want["front_matter"] or {}).get("title_page"):
        _check("title_page_first", bool(spine) and spine[0] == "titlepage.xhtml", f"spine starts {spine[:3]}")
    if want["edition_notice_body"].strip():
        _check("edition_notice", "copyright.xhtml" in spine[:3], f"spine starts {spine[:3]}")

    # The Kindle build renders [texto](url) as text; a live link here means the
    # libre file was picked up, or markup slipped past the target switch.
    if target == KINDLE:
        linked = sorted(href for href, text in got["texts"].items()
                        if _EXTERNAL_LINK_RE.search(text))
        _check("no_external_links", not linked,
               f"live links in {linked}" if linked else "none")
    leftover = sorted(href for href, text in got["texts"].items() if _LEFTOVER_MARKUP_RE.search(text))
    _check("no_leftover_markup", not leftover,
           f"[LIBRE]/[KINDLE], a {{{{…}}}} variable or a [text](url) link left in {leftover}"
           if leftover else "none")

    named = sorted(href for href, text in got["texts"].items() if names_trademark(text))
    if names_trademark(got["description"] + "\n" + got["rights"]):
        named.append("content.opf")
    _check("no_gutenberg_trademark", not named,
           f"named in {named}" if named else "not named anywhere")

    for name, passed, detail in _note_checks(got["texts"]):
        _check(name, passed, detail)

    return {"ok": all(c["ok"] for c in checks), "epub": str(epub_path), "target": target,
            "checks": checks}
