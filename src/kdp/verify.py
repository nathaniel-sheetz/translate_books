"""
Read a built EPUB back and confirm the listing's metadata landed.

The build is several hops from ``apply`` (project.json -> book_metadata ->
build_epub / build_epub_from_chunks -> ebooklib), and any one of them can drop
a field quietly. This opens the zip, reads the OPF and the spine, and checks
each thing ``apply`` asked for against what is actually in the file.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Optional

from src.book_metadata import load_book_metadata
from src.kdp.gutenberg import names_trademark
from src.kdp.validate import _DESCRIPTOR_RE

_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
}


def newest_epub(project_dir: Path) -> Optional[Path]:
    """The most recently written ``*.epub`` in the project folder (what the web UI serves)."""
    epubs = sorted(Path(project_dir).glob("*.epub"), key=lambda p: p.stat().st_mtime)
    return epubs[-1] if epubs else None


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


def verify(project_dir: Path, epub_path: Optional[Path] = None) -> dict[str, Any]:
    project_dir = Path(project_dir)
    epub_path = Path(epub_path) if epub_path else newest_epub(project_dir)
    if not epub_path or not epub_path.exists():
        return {"ok": False, "epub": None, "checks": [],
                "error": "no EPUB found; build one with harness.py epub first"}
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
    _check("author", want["author"] in got["creators"], f"OPF {got['creators']}")
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

    named = sorted(href for href, text in got["texts"].items() if names_trademark(text))
    if names_trademark(got["description"] + "\n" + got["rights"]):
        named.append("content.opf")
    _check("no_gutenberg_trademark", not named,
           f"named in {named}" if named else "not named anywhere")

    return {"ok": all(c["ok"] for c in checks), "epub": str(epub_path), "checks": checks}
