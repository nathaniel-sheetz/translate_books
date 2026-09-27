#!/usr/bin/env python3
"""
Import an EPUB for translation.

The EPUB counterpart of ingest_gutenberg.py: reads the package (spine, TOC,
guide/landmarks, CSS), drops publisher/edition artifacts (cover, title page,
copyright/ISBN page, table of contents, publisher ads, spacer images), and
writes the same outputs the Gutenberg importer does:

    source.txt       clean text with [IMAGE:images/...] placeholders
    headings.json    the document's heading outline, for the splitter
    images/          illustrations copied out of the EPUB
    footnotes.json   note bodies (only with --footnotes import)

plus ingest_report.json, a record of every drop / join / recase decision so a
surprising result can be traced and overridden (--drop-doc / --keep-doc).

Usage:
    python scripts/ingest_epub.py BOOK.epub --output projects/mybook/
    python scripts/ingest_epub.py BOOK.epub --list          # dry run: show the plan
    python scripts/ingest_epub.py BOOK.epub --output projects/mybook/ \\
        --drop-doc Layout-3.xhtml --keep-doc about.xhtml
"""

from __future__ import annotations

import argparse
import io
import json
import os
import posixpath
import re
import sys
import unicodedata
import urllib.parse
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# Make the project root (for ``src``) and this directory (for the sibling
# ``ingest_gutenberg``) importable however this module is loaded: as a script,
# via ``scripts.ingest_epub``, or through the web UI's spec_from_file_location.
_SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (str(_SCRIPTS_DIR.parent), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bs4 import BeautifulSoup, Comment, NavigableString, Tag  # noqa: E402

from ingest_gutenberg import (  # noqa: E402
    HEADING_TAGS,
    ITALIC_TAGS,
    SKIP_CLASSES,
    Converter,
    _normalize_whitespace,
    build_chapter_report,
    decode_html_bytes,
    print_report,
    suggest_split_pattern,
    write_heading_outline,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPORT_FILENAME = "ingest_report.json"

# Structural semantics (OPF guide types, nav landmarks, epub:type) that mark a
# whole document as a publisher/edition artifact rather than authored text.
DROP_DOC_TYPES = {
    "cover", "title-page", "titlepage", "halftitlepage", "halftitle",
    "copyright-page", "copyright", "toc", "loi", "lot", "colophon", "imprint",
    "imprimatur", "seriespage", "other.also-by", "other.ads",
}

# Nav/NCX labels that name an artifact document.
ARTIFACT_LABEL_RE = re.compile(
    r"^\s*(?:"
    r"cover|title(?:\s+page)?|half[\s-]*title(?:\s+page)?|copyright(?:\s+page)?|"
    r"(?:table\s+of\s+)?contents|list\s+of\s+illustrations|illustrations|"
    r"also\s+by\b.*|other\s+(?:books|titles)\b.*|about\s+the\s+publisher|"
    r"colophon|imprint"
    r")\s*$",
    re.I,
)

# Signals that a short document is a copyright/edition page. STRONG ones are
# rare in prose; a doc needs one strong signal and two signals in total.
_STRONG_COPYRIGHT = [
    ("isbn", re.compile(r"\bISBN\b")),
    ("all-rights-reserved", re.compile(r"all\s+rights\s+reserved", re.I)),
    ("copyright-symbol", re.compile(r"©")),
    ("library-of-congress", re.compile(r"library\s+of\s+congress|cataloging[- ]in[- ]publication", re.I)),
]
_WEAK_COPYRIGHT = [
    ("copyright", re.compile(r"\bcopyright\b", re.I)),
    ("published-by", re.compile(r"\b(?:published|printed)\s+(?:in\s+\d{4}\s+)?by\b|\bthis\s+edition\b|\bprinted\s+in\b", re.I)),
    ("url", re.compile(r"www\.|https?://", re.I)),
]
_BACK_AD_RE = re.compile(
    r"also\s+(?:available|by)|other\s+(?:books|titles)|visit\s+us|www\.|https?://", re.I,
)

# Unheaded authored front matter gets a synthetic heading so the splitter keeps
# it; these are the labels a class/id/epub:type hint can supply.
FRONT_MATTER_HINTS = (
    "dedication", "epigraph", "preface", "foreword", "introduction", "prologue",
    "acknowledgments", "acknowledgements",
)

# Images whose file name marks them as layout filler, not illustrations.
ARTIFACT_IMAGE_RE = re.compile(r"(?:blank|spacer|pixel|transparent|clear)[^/]*$", re.I)
MIN_IMAGE_PX = 16

NOTE_TYPES = {"footnote", "endnote", "rearnote", "note", "doc-footnote", "doc-endnote"}
NOTEREF_TYPES = {"noteref", "doc-noteref"}
PAGEBREAK_TYPES = {"pagebreak", "doc-pagebreak"}

# Block-level tags used when locating headings / leaf blocks in a document.
_BLOCKISH = ["p", "div", "li", "dd", "dt", "td", "th", "blockquote", "figure",
             "figcaption", "section", "article", "header", "footer", "aside",
             "h1", "h2", "h3", "h4", "h5", "h6"]
_LEAF_BLOCKS = ["p", "div", "li", "dd", "dt", "td", "th", "blockquote", "figcaption",
                "h1", "h2", "h3", "h4", "h5", "h6"]

# Marks a spine-document boundary in the converter output; consumed by the
# cross-document paragraph join and stripped before writing. A private-use
# character, so it can never collide with book text.
DOC_SENTINEL = "DOC"

_TERMINAL_RE = re.compile(r"[.!?…:;\"'”’»)\]]_?$")
_IMAGE_BLOCK_RE = re.compile(r"^\[IMAGE:[^\]]+\]$")
_CAPTION_BLOCK_RE = re.compile(r"^\[CAPTION\]")
_WORD_RE = re.compile(r"[^\W\d_]+(?:[’'\-][^\W\d_]+)*")
_SENTENCE_END_RE = re.compile(r"[.!?…]['\"”’»)_]*[\s\"“‘'(«—_]*$")
_OPENERS = " \t\n\"“‘'(«—_"


def _norm(text: str) -> str:
    """Comparison key for labels vs element text: casefolded alphanumerics only."""
    return re.sub(r"[\W_]+", "", (text or "").casefold())


def _types(el) -> set:
    """epub:type tokens plus role, lowercased."""
    if not isinstance(el, Tag):
        return set()
    vals = (el.get("epub:type") or "").split() + (el.get("role") or "").split()
    return {v.lower() for v in vals}


# ---------------------------------------------------------------------------
# Package reading
# ---------------------------------------------------------------------------

@dataclass
class NavEntry:
    doc: str          # zip path of the target document
    fragment: str     # id within the document, or ""
    label: str
    depth: int        # 1 = top level


@dataclass
class SpineDoc:
    index: int
    path: str         # zip path
    idref: str
    linear: bool
    soup: BeautifulSoup
    words: int = 0
    images: list = field(default_factory=list)   # zip paths
    first_text: str = ""
    decision: str = "keep"
    reason: str = ""

    @property
    def name(self) -> str:
        return posixpath.basename(self.path)

    @property
    def body(self) -> Tag:
        return self.soup.body or self.soup


def _resolve(base: str, href: str) -> tuple[str, str]:
    """Resolve *href* relative to zip path *base*. Returns (zip_path, fragment)."""
    href = (href or "").strip()
    path, _, frag = href.partition("#")
    if not path:
        return base, frag
    path = urllib.parse.unquote(path)
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), path)), frag


def _xml(data: bytes) -> BeautifulSoup:
    return BeautifulSoup(data, "xml")


class EpubPackage:
    """The parts of an EPUB container the importer needs, read once."""

    def __init__(self, epub_path: Path):
        self.epub_path = Path(epub_path)
        self.zf = zipfile.ZipFile(self.epub_path)
        self._names = set(self.zf.namelist())

        container = _xml(self.zf.read("META-INF/container.xml"))
        rootfile = container.find("rootfile")
        if rootfile is None or not rootfile.get("full-path"):
            raise ValueError(f"{epub_path}: META-INF/container.xml names no rootfile")
        self.opf_path = rootfile["full-path"]
        opf = _xml(self.zf.read(self.opf_path))

        # Metadata
        def dc(name):
            el = opf.find(name)
            return el.get_text(strip=True) if el else ""
        self.metadata = {
            "title": dc("title"),
            "creator": dc("creator"),
            "publisher": dc("publisher"),
            "language": dc("language"),
        }

        # Manifest
        self.manifest: dict[str, dict] = {}
        self.by_path: dict[str, dict] = {}
        for item in opf.find_all("item"):
            href = item.get("href")
            if not href:
                continue
            path, _ = _resolve(self.opf_path, href)
            rec = {
                "id": item.get("id", ""),
                "path": path,
                "media_type": item.get("media-type", ""),
                "properties": (item.get("properties") or "").split(),
            }
            self.manifest[rec["id"]] = rec
            self.by_path[path] = rec

        # Spine
        spine = opf.find("spine")
        self.spine: list[tuple[str, bool]] = []
        ncx_id = spine.get("toc") if spine else None
        for ref in (spine.find_all("itemref") if spine else []):
            self.spine.append((ref.get("idref", ""), ref.get("linear", "yes") != "no"))

        # Guide (EPUB2)
        self.guide: list[tuple[str, str]] = []
        for ref in opf.find_all("reference"):
            if ref.get("href"):
                self.guide.append(((ref.get("type") or "").lower(), _resolve(self.opf_path, ref["href"])[0]))

        # Cover image
        self.cover_image = None
        for rec in self.manifest.values():
            if "cover-image" in rec["properties"]:
                self.cover_image = rec["path"]
        if self.cover_image is None:
            meta = opf.find("meta", attrs={"name": "cover"})
            if meta is not None:
                ref = meta.get("content", "")
                rec = self.manifest.get(ref)
                if rec is None:  # some tools put the href, not the id
                    rec = next((r for r in self.manifest.values()
                                if posixpath.basename(r["path"]) == ref), None)
                if rec and rec["media_type"].startswith("image/"):
                    self.cover_image = rec["path"]

        # Navigation
        self.nav_path = next((r["path"] for r in self.manifest.values() if "nav" in r["properties"]), None)
        ncx_rec = self.manifest.get(ncx_id) if ncx_id else None
        if ncx_rec is None:
            ncx_rec = next((r for r in self.manifest.values()
                            if r["media_type"] == "application/x-dtbncx+xml"), None)
        self.ncx_path = ncx_rec["path"] if ncx_rec else None
        self.landmarks: list[tuple[str, str]] = []
        self.nav_entries = self._read_nav()

        self.css_paths = [r["path"] for r in self.manifest.values() if r["media_type"] == "text/css"]

    # ------------------------------------------------------------------
    def read(self, path: str) -> bytes:
        return self.zf.read(path)

    def exists(self, path: str) -> bool:
        return path in self._names

    def spine_docs(self) -> list[SpineDoc]:
        docs = []
        for i, (idref, linear) in enumerate(self.spine):
            rec = self.manifest.get(idref)
            if rec is None or not self.exists(rec["path"]):
                continue
            soup = BeautifulSoup(decode_html_bytes(self.read(rec["path"])), "html.parser")
            docs.append(SpineDoc(index=i, path=rec["path"], idref=idref, linear=linear, soup=soup))
        return docs

    # ------------------------------------------------------------------
    def _read_nav(self) -> list[NavEntry]:
        """Merge the EPUB3 nav TOC and the EPUB2 NCX into one entry list.

        Both often exist and disagree in precision -- here the nav links only
        to files while the NCX targets the exact heading paragraph -- so an
        entry with a fragment wins over a fragmentless one for the same label.
        """
        entries: list[NavEntry] = []

        if self.ncx_path and self.exists(self.ncx_path):
            ncx = _xml(self.read(self.ncx_path))

            def walk_ncx(parent, depth):
                for pt in parent.find_all("navPoint", recursive=False):
                    label_el = pt.find("navLabel")
                    content = pt.find("content", recursive=False)
                    if content is not None and content.get("src"):
                        doc, frag = _resolve(self.ncx_path, content["src"])
                        label = label_el.get_text(" ", strip=True) if label_el else ""
                        entries.append(NavEntry(doc, frag, label, depth))
                    walk_ncx(pt, depth + 1)

            nav_map = ncx.find("navMap")
            if nav_map is not None:
                walk_ncx(nav_map, 1)

        if self.nav_path and self.exists(self.nav_path):
            nav_soup = BeautifulSoup(decode_html_bytes(self.read(self.nav_path)), "html.parser")
            toc_nav = None
            for nav in nav_soup.find_all("nav"):
                t = _types(nav)
                if "toc" in t or "doc-toc" in t:
                    toc_nav = nav
                elif "landmarks" in t:
                    for a in nav.find_all("a", href=True):
                        for lt in _types(a):
                            self.landmarks.append((lt, _resolve(self.nav_path, a["href"])[0]))
            if toc_nav is None:
                toc_nav = nav_soup.find("nav")

            def walk_ol(ol, depth):
                for li in ol.find_all("li", recursive=False):
                    a = li.find("a", href=True)
                    if a is not None:
                        doc, frag = _resolve(self.nav_path, a["href"])
                        entries.append(NavEntry(doc, frag, a.get_text(" ", strip=True), depth))
                    for sub in li.find_all("ol", recursive=False):
                        walk_ol(sub, depth + 1)

            if toc_nav is not None:
                top = toc_nav.find("ol")
                if top is not None:
                    walk_ol(top, 1)

        # Dedupe on (doc, label), preferring an entry that carries a fragment.
        best: dict[tuple[str, str], NavEntry] = {}
        order: list[tuple[str, str]] = []
        for e in entries:
            key = (e.doc, _norm(e.label))
            if key not in best:
                best[key] = e
                order.append(key)
            elif e.fragment and not best[key].fragment:
                best[key] = e
        return [best[k] for k in order]


# ---------------------------------------------------------------------------
# CSS: which classes render italic
# ---------------------------------------------------------------------------

_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_FONT_STYLE_RE = re.compile(r"font-style\s*:\s*(italic|oblique|normal)", re.I)
_FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*([^;]+)", re.I)
_FONT_SRC_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)", re.I)
_ITALIC_NAME_RE = re.compile(r"italic|oblique", re.I)
_SIMPLE_SEL_RE = re.compile(r"^([a-zA-Z][\w-]*|\*)?((?:\.[\w-]+)+)$")


def _first_family(body: str) -> str | None:
    m = _FONT_FAMILY_RE.search(body)
    if not m:
        return None
    return m.group(1).split(",")[0].strip().strip("'\"").strip().casefold() or None


def _italic_font_families(css_texts: list[str]) -> set[str]:
    """Families whose every ``@font-face`` is an italic face.

    A face is italic by its ``font-style``, or, when it declares none, by an
    italic/oblique font file name. A family with both a normal and an italic
    face (the standard two-rule pattern) is not italic on its own.
    """
    faces: dict[str, list[bool]] = defaultdict(list)
    for css in css_texts:
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        for sel_text, body in _CSS_RULE_RE.findall(css):
            if sel_text.strip().lower() != "@font-face":
                continue
            family = _first_family(body)
            if not family:
                continue
            m = _FONT_STYLE_RE.search(body)
            if m:
                italic = m.group(1).lower() != "normal"
            else:
                italic = any(_ITALIC_NAME_RE.search(u.rsplit("/", 1)[-1])
                             for u in _FONT_SRC_RE.findall(body))
            faces[family].append(italic)
    return {f for f, flags in faces.items() if all(flags)}


def parse_italic_classes(css_texts: list[str]) -> dict[str, set]:
    """Map class name -> tags it italicizes ("*" = any), from simple selectors.

    Only the last compound of each selector is considered and only when it is a
    plain ``tag.class`` / ``.class``; that covers the generated stylesheets
    publishers ship (InDesign, Sigil, Calibre). A class is italic by
    ``font-style`` or by pointing at an italic font family
    (``font-family: CambriaItalic``, or a family whose ``@font-face`` rules are
    all italic faces). The two properties cascade separately, each last rule
    winning, so ``font-style: normal`` switches off a ``font-style`` italic but
    a later non-italic ``font-family`` does not.
    """
    italic_families = _italic_font_families(css_texts)
    style_state: dict[tuple[str, str], bool] = {}
    family_state: dict[tuple[str, str], bool] = {}
    for css in css_texts:
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        for sel_text, body in _CSS_RULE_RE.findall(css):
            if sel_text.strip().startswith("@"):
                continue
            m = _FONT_STYLE_RE.search(body)
            style = None if not m else m.group(1).lower() != "normal"
            family_name = _first_family(body)
            family = None if family_name is None else bool(
                _ITALIC_NAME_RE.search(family_name) or family_name in italic_families)
            if style is None and family is None:
                continue
            for sel in sel_text.split(","):
                last = re.split(r"[\s>+~]+", sel.strip())[-1]
                sm = _SIMPLE_SEL_RE.match(last)
                if not sm:
                    continue
                tag = (sm.group(1) or "*").lower()
                for cls in sm.group(2).strip(".").split("."):
                    if style is not None:
                        style_state[(cls, tag)] = style
                    if family is not None:
                        family_state[(cls, tag)] = family
    result: dict[str, set] = defaultdict(set)
    for key in style_state.keys() | family_state.keys():
        if style_state.get(key) or family_state.get(key):
            result[key[0]].add(key[1])
    return dict(result)


# ---------------------------------------------------------------------------
# Document analysis + classification
# ---------------------------------------------------------------------------

def _doc_images(doc: SpineDoc) -> list[Tag]:
    imgs = list(doc.body.find_all("img"))
    imgs += [i for i in doc.body.find_all("image")]
    return imgs


def _image_src(el: Tag) -> str:
    if el.name == "img":
        return el.get("src", "")
    return el.get("xlink:href") or el.get("href") or ""


def _analyze(doc: SpineDoc) -> None:
    """Fill words / images / first_text and pre-resolve every image source."""
    text = doc.body.get_text(" ", strip=True)
    doc.words = len(text.split())
    doc.first_text = re.sub(r"\s+", " ", text)[:70]
    for el in _doc_images(doc):
        src = _image_src(el)
        if src and not src.startswith("data:"):
            path, _ = _resolve(doc.path, src)
            el["data-ingest-src"] = path
            doc.images.append(path)


def _copyright_signals(text: str, publisher: str) -> tuple[list[str], bool]:
    found, strong = [], False
    for name, rx in _STRONG_COPYRIGHT:
        if rx.search(text):
            found.append(name)
            strong = True
    for name, rx in _WEAK_COPYRIGHT:
        if rx.search(text):
            found.append(name)
    if publisher and len(publisher) >= 4 and publisher.casefold() in text.casefold():
        found.append("publisher-name")
    return found, strong


def _is_toc_like(doc: SpineDoc, spine_paths: set) -> tuple[bool, int]:
    blocks = [b for b in doc.body.find_all(_LEAF_BLOCKS)
              if b.get_text(strip=True) and not b.find(_BLOCKISH)]
    if not blocks:
        return False, 0
    linked = 0
    for b in blocks:
        for a in b.find_all("a", href=True):
            target, _ = _resolve(doc.path, a["href"])
            if target in spine_paths and target != doc.path:
                linked += 1
                break
    return (linked >= 3 and linked / len(blocks) >= 0.6), linked


def _matches_override(doc: SpineDoc, names: set) -> bool:
    return bool(names & {doc.path, doc.name, doc.idref, posixpath.splitext(doc.name)[0]})


def classify_docs(pkg: EpubPackage, docs: list[SpineDoc], *, drop_docs=(), keep_docs=()) -> None:
    """Decide keep/drop for every spine document, recording the reason.

    Explicit semantics (linear=no, the nav doc, guide/landmark/epub:type
    artifact types, artifact TOC labels) are trusted outright. Content
    heuristics (copyright signals, link-dense TOC pages, title pages, publisher
    ads) are applied only to documents that hold no content heading, so a
    short chapter that happens to mention a copyright can never be dropped.
    ``drop_docs`` / ``keep_docs`` override everything.
    """
    drop_docs, keep_docs = set(drop_docs), set(keep_docs)
    spine_paths = {d.path for d in docs}
    for doc in docs:
        _analyze(doc)
    guide_types = defaultdict(set)
    for t, p in pkg.guide + pkg.landmarks:
        guide_types[p].add(t)

    content_docs = set()
    artifact_label = {}
    for e in pkg.nav_entries:
        if ARTIFACT_LABEL_RE.match(e.label):
            artifact_label.setdefault(e.doc, e.label)
        else:
            content_docs.add(e.doc)
    content_idx = [d.index for d in docs if d.path in content_docs]
    if not content_idx:
        content_idx = [d.index for d in docs if d.words >= 150]
    first_content = min(content_idx) if content_idx else 0
    last_content = max(content_idx) if content_idx else len(docs)

    title_key = _norm(pkg.metadata.get("title", ""))
    publisher = pkg.metadata.get("publisher", "")

    for doc in docs:
        text = doc.body.get_text(" ", strip=True)
        body_types = _types(doc.body) | set().union(
            *(_types(s) for s in doc.body.find_all(["section", "div"], recursive=False)))
        reason = ""

        if not doc.linear:
            reason = "spine linear=no"
        elif doc.path == pkg.nav_path:
            reason = "navigation document"
        elif guide_types.get(doc.path, set()) & DROP_DOC_TYPES:
            reason = "guide/landmark: " + ",".join(sorted(guide_types[doc.path] & DROP_DOC_TYPES))
        elif body_types & DROP_DOC_TYPES:
            reason = "epub:type " + ",".join(sorted(body_types & DROP_DOC_TYPES))
        elif doc.path in artifact_label and doc.path not in content_docs:
            reason = f"TOC label '{artifact_label[doc.path]}'"
        elif doc.path not in content_docs:
            signals, strong = _copyright_signals(text, publisher)
            toc_like, links = _is_toc_like(doc, spine_paths)
            if strong and len(signals) >= 2 and doc.words < 600:
                reason = "copyright page (" + ", ".join(signals) + ")"
            elif toc_like:
                reason = f"table of contents ({links} links)"
            elif (doc.index < first_content and title_key and doc.words <= 50
                  and title_key in _norm(text)):
                reason = "title page"
            elif (doc.index > last_content and doc.words < 800
                  and (_BACK_AD_RE.search(text)
                       or (publisher and publisher.casefold() in text.casefold()))):
                reason = "publisher back matter"
            elif doc.words == 0 and not doc.images:
                reason = "empty"

        if reason:
            doc.decision, doc.reason = "drop", reason
        if _matches_override(doc, keep_docs):
            doc.decision, doc.reason = "keep", "--keep-doc" + (f" (was: {reason})" if reason else "")
        if _matches_override(doc, drop_docs):
            doc.decision, doc.reason = "drop", "--drop-doc"


# ---------------------------------------------------------------------------
# Headings
# ---------------------------------------------------------------------------

def _heading_level_of(el: Tag) -> int | None:
    if not isinstance(el, Tag):
        return None
    if el.get("data-ingest-level"):
        return int(el["data-ingest-level"])
    if el.name in HEADING_TAGS and el.get_text(strip=True):
        return int(el.name[1])
    return None


def _find_by_text(doc: SpineDoc, label: str) -> Tag | None:
    key = _norm(label)
    if not key:
        return None
    for el in doc.body.find_all(_LEAF_BLOCKS):
        if el.find(_BLOCKISH):
            continue
        if _norm(el.get_text()) == key:
            return el
    return None


def _resolve_heading_element(doc: SpineDoc, entry: NavEntry) -> Tag | None:
    """The block element a nav entry points at, or None if it can't be pinned."""
    key = _norm(entry.label)
    el = None
    if entry.fragment:
        el = doc.body.find(id=entry.fragment) or doc.body.find(attrs={"name": entry.fragment})
    if el is None:
        return _find_by_text(doc, entry.label)
    if el.name in HEADING_TAGS:
        return el
    # A fragment on a container (<section id=ch1>): look inside for the title.
    if el.name in _BLOCKISH and el.find(_BLOCKISH):
        for cand in el.find_all(_LEAF_BLOCKS):
            if cand.name in HEADING_TAGS or _norm(cand.get_text()) == key:
                return cand
        return _find_by_text(doc, entry.label)
    # An inline/empty anchor: the heading is the enclosing block, or -- for an
    # anchor parked just before the heading -- the next block.
    block = el if el.name in _LEAF_BLOCKS else el.find_parent(_LEAF_BLOCKS)
    if block is None or block is doc.body:
        block = el.find_next(_LEAF_BLOCKS)
    if block is None:
        return _find_by_text(doc, entry.label)
    text = block.get_text(" ", strip=True)
    if _norm(text) == key or len(text.split()) <= 25:
        return block
    return _find_by_text(doc, entry.label)


def _leaf_items(node: Tag):
    """Headings, leaf text blocks, images and loose text, in document order."""
    for child in node.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            if child.strip():
                yield child
            continue
        if not isinstance(child, Tag):
            continue
        if _heading_level_of(child) is not None:
            yield child
        elif child.name in ("img", "image"):
            yield child
        elif child.name in _LEAF_BLOCKS and not child.find(_BLOCKISH):
            if child.get_text(strip=True) or child.find(["img", "image"]):
                yield child
        else:
            yield from _leaf_items(child)


def mark_headings(pkg: EpubPackage, kept: list[SpineDoc]) -> dict:
    """Tag heading elements (data-ingest-level) and subtitles in the kept docs.

    Returns a summary: {"marked": n, "unresolved": [...], "subtitles": n}.
    """
    by_path = {d.path: d for d in kept}
    unresolved = []
    marked = 0
    for e in pkg.nav_entries:
        if ARTIFACT_LABEL_RE.match(e.label):
            continue
        doc = by_path.get(e.doc)
        if doc is None:
            continue
        el = _resolve_heading_element(doc, e)
        if el is None:
            unresolved.append(e.label)
            continue
        if el.name not in HEADING_TAGS and not el.get("data-ingest-level"):
            el["data-ingest-level"] = str(e.depth)
            marked += 1

    # A heading that directly follows a shallower heading, with nothing in
    # between, is that heading's subtitle ("BUCEPHALUS" / "CHARGER OF
    # ALEXANDER THE GREAT"). It stays in the text but not in the outline:
    # recorded as its own level it would tie the title level, and the splitter
    # breaks ties toward the deeper level -- splitting on subtitles and
    # stranding every title at the end of the previous chapter.
    subtitles = 0
    for doc in kept:
        prev = None
        for item in _leaf_items(doc.body):
            lvl = _heading_level_of(item) if isinstance(item, Tag) else None
            prev_lvl = _heading_level_of(prev) if isinstance(prev, Tag) else None
            if (lvl is not None and prev_lvl is not None and prev_lvl < lvl
                    and not prev.get("data-ingest-subtitle")
                    and len(item.get_text(" ", strip=True).split()) <= 20):
                item["data-ingest-subtitle"] = "1"
                subtitles += 1
            prev = item
    return {"marked": marked, "unresolved": unresolved, "subtitles": subtitles}


def _doc_headings(doc: SpineDoc) -> list[Tag]:
    return [el for el in doc.body.find_all(True)
            if _heading_level_of(el) is not None and not el.get("data-ingest-subtitle")]


def _front_matter_label(doc: SpineDoc) -> str:
    hints = " ".join(
        [" ".join(_types(el)) for el in [doc.body] + doc.body.find_all(True)]
        + [" ".join(el.get("class") or []) + " " + (el.get("id") or "")
           for el in [doc.body] + doc.body.find_all(True)]
        + [doc.name]
    ).lower()
    for hint in FRONT_MATTER_HINTS:
        if hint in hints:
            return "Acknowledgments" if hint.startswith("acknowledg") else hint.capitalize()
    return "Epigraph" if doc.words < 150 else "Preface"


def add_front_matter_headings(kept: list[SpineDoc], soup_factory: BeautifulSoup) -> list[dict]:
    """Give unheaded authored text before the first chapter a heading.

    Without one the splitter has nowhere to put it and drops it as untitled
    pre-chapter text. The label is a guess from semantics/class names and is
    reported so it can be corrected.
    """
    heading_docs = [i for i, d in enumerate(kept) if _doc_headings(d)]
    if not heading_docs:
        return []
    levels = [_heading_level_of(h) for d in kept for h in _doc_headings(d)]
    chapter_level = min(levels) if levels else 1
    added = []
    for d in kept[: heading_docs[0]]:
        if d.words == 0:
            continue
        label = _front_matter_label(d)
        tag = soup_factory.new_tag("p")
        tag["data-ingest-level"] = str(chapter_level)
        tag["data-ingest-synthetic"] = "1"
        tag.string = label
        d.body.insert(0, tag)
        added.append({"doc": d.name, "label": label, "level": chapter_level})
    return added


# ---------------------------------------------------------------------------
# Merge kept documents into one tree
# ---------------------------------------------------------------------------

def merge_docs(kept: list[SpineDoc]) -> BeautifulSoup:
    """Concatenate the kept bodies into one soup, one <section> per document.

    ids are prefixed per document and every internal href is rewritten to the
    prefixed form -- including cross-file links (notes.xhtml#n1) -- so the
    footnote detector, which only follows "#frag" links, sees one document.
    """
    index = {d.path: n for n, d in enumerate(kept)}
    merged = BeautifulSoup("<html><body></body></html>", "html.parser")
    for n, doc in enumerate(kept):
        for el in doc.body.find_all(True):
            if el.get("id"):
                el["id"] = f"d{n}-{el['id']}"
            if el.name == "a" and el.get("name"):
                el["name"] = f"d{n}-{el['name']}"
        for a in doc.body.find_all(href=True):
            href = a["href"]
            if re.match(r"^[a-z][a-z0-9+.-]*:", href, re.I):
                continue  # external (http:, mailto:, ...)
            path, frag = _resolve(doc.path, href)
            if path in index and frag:
                a["href"] = f"#d{index[path]}-{frag}"
        section = merged.new_tag("section")
        section["data-ingest-doc"] = doc.path
        for child in list(doc.body.contents):
            section.append(child.extract())
        merged.body.append(section)
    return merged


# ---------------------------------------------------------------------------
# Footnotes
# ---------------------------------------------------------------------------

def find_epub_footnotes(root: Tag):
    """The shared Gutenberg detector plus EPUB3 noteref -> footnote/endnote pairs.

    Back-linked notes are found by ``find_footnotes``; EPUB3 asides frequently
    carry no back-link, so ``epub:type="noteref"`` links whose target sits in a
    note-typed element are added. Numbering is document order of the refs.
    """
    from src.footnote_import import FootnoteMatch, _note_text, find_footnotes

    matches = find_footnotes(root)
    seen_refs = {id(m.ref) for m in matches}
    seen_defs = {id(m.def_block) for m in matches}
    for a in root.find_all("a", href=True):
        if id(a) in seen_refs or not (_types(a) & NOTEREF_TYPES):
            continue
        href = a["href"]
        if not href.startswith("#"):
            continue
        target = root.find(id=href[1:])
        block = target
        for _ in range(5):
            if block is None or _types(block) & NOTE_TYPES:
                break
            block = block.parent
        if block is None or not (_types(block) & NOTE_TYPES) or id(block) in seen_defs:
            continue
        marker = a.get_text(strip=True)
        body = _note_text(block, marker, None)
        body = re.sub(r"\s*[↩↑⤴︎]+\s*$", "", body).strip()
        matches.append(FootnoteMatch(
            number=0, ref_marker=marker, source_body=body, detected="epub-noteref",
            ref=a, landing=None, def_block=block,
        ))
        seen_refs.add(id(a))
        seen_defs.add(id(block))

    order = {id(t): i for i, t in enumerate(root.find_all(True))}
    matches.sort(key=lambda m: order.get(id(m.ref), 0))
    for i, m in enumerate(matches, 1):
        m.number = i
    return matches


def _has_body_text(sec: Tag) -> bool:
    """True when a section holds text or an image outside its headings."""
    if sec.find(["img", "image"]):
        return True
    for s in sec.find_all(string=True):
        if isinstance(s, Comment) or not s.strip():
            continue
        if not any(_heading_level_of(p) is not None for p in s.parents if isinstance(p, Tag)):
            return True
    return False


def drop_emptied_sections(merged: BeautifulSoup, had_text: set) -> list[str]:
    """Remove documents that footnote extraction left holding only a heading
    (a Notes page). A document that never had body text -- a part-title page
    -- is not in *had_text* and is left alone."""
    emptied = []
    for sec in merged.find_all("section", attrs={"data-ingest-doc": True}):
        if sec["data-ingest-doc"] in had_text and not _has_body_text(sec):
            emptied.append(posixpath.basename(sec["data-ingest-doc"]))
            sec.decompose()
    return emptied


# ---------------------------------------------------------------------------
# Converter
# ---------------------------------------------------------------------------

class EpubConverter(Converter):
    """Converter with CSS-aware italics, nav-driven headings and zip images."""

    def __init__(self, pkg: EpubPackage, images_dir: Path | None, extract_images: bool,
                 italic_classes: dict[str, set], image_names: dict[str, str]):
        super().__init__(base_url="", images_dir=images_dir, download_images=extract_images)
        self.pkg = pkg
        self.italic_classes = italic_classes
        self.image_names = image_names
        self.subtitles: list[str] = []
        self.dropped_images: list[str] = []
        self._image_ok: dict[str, bool] = {}

    # -- walk ------------------------------------------------------------
    def _walk(self, node):
        if isinstance(node, Tag):
            if node.get("data-ingest-doc") is not None:
                self.parts.append(f"\n\n{DOC_SENTINEL}\n\n")
                for child in node.children:
                    self._walk(child)
                return
            if node.get("data-ingest-subtitle"):
                for img in node.find_all(["img", "image"]):
                    self._handle_image(img)
                text = self._heading_text(node)
                if text:
                    self.subtitles.append(text)
                    self.parts.append(f"\n\n{text}\n\n")
                return
            tag = (node.name or "").lower()
            if tag == "image":
                self._handle_image(node)
                return
            classes = set(node.get("class") or [])
            if self._heading_level(node, tag) is not None:
                # A heading that also carries an image: keep the image.
                for img in node.find_all(["img", "image"]):
                    self._handle_image(img)
            elif self._is_italic(node, tag, classes) and self._midword(node):
                # Italic that starts or ends inside a word (a styled fragment
                # of a small-caps lead-in, say) would split the word around
                # "_" markers; emit it as plain text instead.
                for child in node.children:
                    self._walk(child)
                return
        super()._walk(node)

    def _midword(self, node: Tag) -> bool:
        prev = next((p[-1] for p in reversed(self.parts) if p), "")
        nxt = node.next_sibling
        nxt_text = (nxt if isinstance(nxt, NavigableString) else
                    nxt.get_text() if isinstance(nxt, Tag) else "") or ""
        return bool(prev and prev.isalnum()) or bool(nxt_text[:1].isalnum())

    # -- predicates --------------------------------------------------------
    def _should_skip(self, node: Tag, tag: str, classes: set) -> bool:
        # Unlike Gutenberg HTML, EPUB chapters wrap their titles in <header>,
        # so header/footer/aside are walked; only note asides are skipped.
        if tag in ("script", "style", "head", "nav"):
            return True
        types = _types(node)
        if types & (PAGEBREAK_TYPES | NOTEREF_TYPES):
            return True
        if tag == "aside" and types & NOTE_TYPES:
            return True
        return bool(classes & SKIP_CLASSES)

    def _heading_level(self, node: Tag, tag: str) -> int | None:
        if node.get("data-ingest-subtitle"):
            return None
        if node.get("data-ingest-level"):
            return int(node["data-ingest-level"])
        if tag in HEADING_TAGS and self._heading_text(node):
            return int(tag[1])
        return None

    def _heading_text(self, node: Tag) -> str:
        # Text nodes are joined WITHOUT a separator: InDesign parks empty
        # anchors mid-word ("B<a id=..></a>UCEPHALUS"), and the Gutenberg
        # get_text(" ") would split the word. <br> and nested blocks still
        # separate words.
        parts: list[str] = []

        def walk(n):
            for child in n.children:
                if isinstance(child, Comment):
                    continue
                if isinstance(child, NavigableString):
                    parts.append(str(child))
                elif isinstance(child, Tag):
                    name = (child.name or "").lower()
                    if self._should_skip(child, name, set(child.get("class") or [])):
                        continue
                    if name == "br":
                        parts.append(" ")
                    elif name in _LEAF_BLOCKS:
                        parts.append(" ")
                        walk(child)
                        parts.append(" ")
                    else:
                        walk(child)

        walk(node)
        return re.sub(r"\s+", " ", "".join(parts)).strip()

    def _is_italic(self, node: Tag, tag: str, classes: set) -> bool:
        if tag in ITALIC_TAGS:
            return True
        if tag in _BLOCKISH:
            return False  # whole-paragraph italic styling is not carried
        style = node.get("style") or ""
        if re.search(r"font-style\s*:\s*(italic|oblique)", style, re.I):
            return True
        for cls in classes:
            tags = self.italic_classes.get(cls)
            if tags and ("*" in tags or tag in tags):
                return True
        return False

    # -- images ------------------------------------------------------------
    def _usable_image(self, src: str) -> bool:
        if src in self._image_ok:
            return self._image_ok[src]
        ok = self.pkg.exists(src) and not ARTIFACT_IMAGE_RE.search(src)
        if ok:
            try:
                from PIL import Image
                with Image.open(io.BytesIO(self.pkg.read(src))) as im:
                    ok = min(im.size) >= MIN_IMAGE_PX
            except Exception:
                pass  # no Pillow / unreadable: keep it
        self._image_ok[src] = ok
        return ok

    def _handle_image(self, img: Tag):
        src = img.get("data-ingest-src")
        if not src:
            return
        if not self._usable_image(src):
            if src not in self.dropped_images:
                self.dropped_images.append(src)
            return
        filename = self.image_names[src]
        if self.download_images and self.images_dir is not None:
            dest = self.images_dir / filename
            if not dest.exists():
                dest.write_bytes(self.pkg.read(src))
            self._images_downloaded += 1
        alt = (img.get("alt") or "").strip()
        placeholder = f"[IMAGE:images/{filename}:{alt}]" if alt else f"[IMAGE:images/{filename}]"
        self.parts.append(f"\n\n{placeholder}\n\n")


def assign_image_names(paths) -> dict[str, str]:
    """Flat, unique file names for images/ (the EPUB builder stores them flat).

    A basename shared by two directories gets its parent folder prefixed, and
    an in-book image called cover.* is renamed so the EPUB builder's cover
    auto-detection (images/cover.jpg) never picks up the source cover.
    """
    names: dict[str, str] = {}
    used: set[str] = set()
    for path in paths:
        if path in names:
            continue
        base = posixpath.basename(path)
        if posixpath.splitext(base)[0].lower() == "cover":
            base = "source_" + base
        name = base
        if name.lower() in used:
            parent = posixpath.basename(posixpath.dirname(path)) or "img"
            name = f"{parent}_{base}"
            n = 2
            while name.lower() in used:
                name = f"{parent}_{n}_{base}"
                n += 1
        used.add(name.lower())
        names[path] = name
    return names


# ---------------------------------------------------------------------------
# Text post-processing
# ---------------------------------------------------------------------------

_INVISIBLE_RE = re.compile("[­​‌‍⁠﻿]")
_LIGATURE_RE = re.compile("[ﬀ-ﬆ]")


def normalize_chars(text: str) -> str:
    """Strip soft hyphens / zero-width characters and trailing line spaces, and
    expand ligature glyphs."""
    text = _INVISIBLE_RE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return _LIGATURE_RE.sub(lambda m: unicodedata.normalize("NFKC", m.group(0)), text)


def _is_prose(block: str, headings: set) -> bool:
    return bool(block) and block != DOC_SENTINEL and block not in headings \
        and not _IMAGE_BLOCK_RE.match(block) and not _CAPTION_BLOCK_RE.match(block) \
        and block != "---"


def join_split_paragraphs(text: str, headings: set) -> tuple[str, list[dict]]:
    """Rejoin paragraphs a page/file boundary cut in two.

    At each document boundary: when the last block before it is prose with no
    terminal punctuation, and the next prose block (skipping illustration and
    caption blocks) starts lowercase, the two are one paragraph. Images that sat
    between them move after the rejoined paragraph. Verse (a block with line
    breaks) is never joined.
    """
    blocks = text.split("\n\n")
    out: list[str] = []
    joins = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b == DOC_SENTINEL and out:
            prev_i = len(out) - 1
            while prev_i >= 0 and out[prev_i] == DOC_SENTINEL:
                prev_i -= 1
            prev = out[prev_i] if prev_i >= 0 else ""
            if _is_prose(prev, headings) and "\n" not in prev and not _TERMINAL_RE.search(prev):
                k = i + 1
                between = []
                while k < len(blocks) and not _is_prose(blocks[k], headings) \
                        and blocks[k] not in headings:
                    if blocks[k] != DOC_SENTINEL:
                        between.append(blocks[k])
                    k += 1
                nxt = blocks[k] if k < len(blocks) else ""
                first = nxt.lstrip("_")[:1]
                if _is_prose(nxt, headings) and first.islower():
                    sep = "" if prev.endswith("-") else " "
                    out[prev_i] = prev + sep + nxt
                    out.extend(between)
                    joins.append({"end": prev[-50:], "start": nxt[:50], "images_moved": len(between)})
                    i = k + 1
                    continue
        out.append(b)
        i += 1
    return "\n\n".join(out), joins


def anchor_boundary_images(text: str, headings: set) -> tuple[str, list[dict]]:
    """Keep an image-only page that falls between two chapters with the first.

    The splitter pulls an image sitting directly above a heading into that
    heading's chapter -- right for a Gutenberg ornament above a title, wrong
    for a full-page plate the print book placed after a chapter's last page,
    which then lands one chapter late. When the image group is its own document
    (sentinels on both sides) and the next block starts a new document with a
    heading, the group moves above the previous chapter's last paragraph.
    """
    blocks = text.split("\n\n")
    moved = []
    h = 0
    while h < len(blocks):
        if blocks[h] not in headings:
            h += 1
            continue
        j = h - 1
        if j < 0 or blocks[j] != DOC_SENTINEL:
            h += 1
            continue
        while j >= 0 and blocks[j] == DOC_SENTINEL:
            j -= 1
        end = j  # last block of the candidate image group
        while j >= 0 and (_IMAGE_BLOCK_RE.match(blocks[j]) or _CAPTION_BLOCK_RE.match(blocks[j])):
            j -= 1
        start = j + 1
        if start > end or not _IMAGE_BLOCK_RE.match(blocks[start]) or j < 0 or blocks[j] != DOC_SENTINEL:
            h += 1
            continue
        k = j
        while k >= 0 and blocks[k] == DOC_SENTINEL:
            k -= 1
        if k < 0 or not _is_prose(blocks[k], headings):
            h += 1
            continue
        group = blocks[start:end + 1]
        del blocks[start:end + 1]
        blocks[k:k] = group
        moved.append({"images": [b for b in group if _IMAGE_BLOCK_RE.match(b)],
                      "before_heading": blocks[h]})
        h += 1
    return "\n\n".join(blocks), moved


def _casing_dictionary(blocks: list[str], exclude: set) -> dict[str, Counter]:
    """How each word is cased mid-sentence across the body text."""
    variants: dict[str, Counter] = defaultdict(Counter)
    for block in blocks:
        if block in exclude or not _is_prose(block, exclude):
            continue
        for m in _WORD_RE.finditer(block):
            word = m.group(0)
            prefix = block[max(0, m.start() - 12): m.start()]
            if (m.start() <= 12 and not block[: m.start()].strip(_OPENERS)) \
                    or _SENTENCE_END_RE.search(prefix):
                continue  # sentence-initial: capitalized by position, not by name
            variants[word.casefold()][word] += 1
    return variants


def _recase_word(core: str, first: bool, variants: dict[str, Counter]) -> str:
    key = core.casefold()
    seen = variants.get(key)
    if seen:
        mixed = Counter({w: c for w, c in seen.items() if not (w.isupper() and len(w) > 1)})
        if mixed:
            word = mixed.most_common(1)[0][0]
        else:
            return core  # only ever written in capitals: an acronym, keep it
    elif "-" in core:
        parts = core.split("-")
        return "-".join(_recase_word(p, first and n == 0, variants) for n, p in enumerate(parts))
    elif re.search(r"[’']", core):
        head, sep, tail = re.split(r"([’'])", core, maxsplit=1)
        return _recase_word(head, first, variants) + sep + tail.lower()
    else:
        word = core.lower()
    if first:
        word = word[:1].upper() + word[1:]
    return word


def recase_lead_ins(text: str, headings: list[str], subtitles: set) -> tuple[str, list[dict]]:
    """Recase an ALL-CAPS drop-cap/small-caps lead-in using the book's own casing.

    Only the first prose block after each heading is considered, and only its
    leading run of all-caps words ("THE black" / "JOAN OF ARC stood"). Each word
    takes its dominant mid-sentence casing elsewhere in the book, so proper
    nouns survive ("Joan of Arc"); words only ever written in capitals stay.
    """
    blocks = text.split("\n\n")
    heading_set = set(headings) | subtitles
    variants = _casing_dictionary(blocks, heading_set)
    changes = []
    expect_lead = False
    for n, block in enumerate(blocks):
        if block in heading_set:
            expect_lead = True
            continue
        if not expect_lead or not _is_prose(block, heading_set):
            continue
        expect_lead = False
        tokens = list(re.finditer(r"\S+", block))
        run = []
        for m in tokens:
            core = re.sub(r"^[\W_]+|[\W_]+$", "", m.group(0))
            if not core or not any(c.isalpha() for c in core):
                break
            if any(c.islower() for c in core):
                break
            run.append((m, core))
        letters = sum(sum(c.isalpha() for c in core) for _, core in run)
        if not run or letters < 2:
            continue
        pieces, last = [], 0
        for idx, (m, core) in enumerate(run):
            tok = m.group(0)
            lead = tok[: tok.index(core)]
            trail = tok[tok.index(core) + len(core):]
            pieces.append(block[last:m.start()])
            pieces.append(lead + _recase_word(core, idx == 0, variants) + trail)
            last = m.end()
        new_block = "".join(pieces) + block[last:]
        if new_block != block:
            changes.append({
                "from": block[: run[-1][0].end()],
                "to": new_block[: len("".join(pieces))],
            })
            blocks[n] = new_block
    return "\n\n".join(blocks), changes


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

@dataclass
class EpubIngestResult:
    text: str
    chapters: list
    subtitles: list
    metadata: dict
    docs: list
    synthetic_headings: list
    heading_summary: dict
    images_extracted: int
    images_dropped: list
    joins: list
    anchored_images: list
    recased: list
    footnotes_count: int
    footnotes_mode: str
    emptied_docs: list
    cover: str | None

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def report_dict(self, source: str) -> dict:
        return {
            "version": 1,
            "source": source,
            "format": "epub",
            "metadata": self.metadata,
            "docs": self.docs,
            "headings": [{"level": c["level"], "text": c["heading"]} for c in self.chapters],
            "subtitles": self.subtitles,
            "synthetic_headings": self.synthetic_headings,
            "unresolved_toc_entries": self.heading_summary.get("unresolved", []),
            "images": {"extracted": self.images_extracted, "dropped": self.images_dropped,
                       "cover": self.cover},
            "paragraph_joins": self.joins,
            "images_kept_with_previous_chapter": self.anchored_images,
            "recased_lead_ins": self.recased,
            "footnotes": {"count": self.footnotes_count, "mode": self.footnotes_mode},
            "emptied_docs": self.emptied_docs,
        }


def plan_epub(epub_path, *, drop_docs=(), keep_docs=()) -> tuple[EpubPackage, list[SpineDoc]]:
    pkg = EpubPackage(Path(epub_path))
    docs = pkg.spine_docs()
    classify_docs(pkg, docs, drop_docs=drop_docs, keep_docs=keep_docs)
    return pkg, docs


def ingest_epub(
    epub_path,
    output_dir,
    *,
    footnotes: str = "drop",
    recase: bool = True,
    extract_images: bool = True,
    boundary_images: str = "previous",
    drop_docs=(),
    keep_docs=(),
    write: bool = True,
) -> EpubIngestResult:
    """Convert *epub_path* into ``source.txt`` + sidecars under *output_dir*."""
    output_dir = Path(output_dir)
    pkg, docs = plan_epub(epub_path, drop_docs=drop_docs, keep_docs=keep_docs)
    kept = [d for d in docs if d.decision == "keep"]

    heading_summary = mark_headings(pkg, kept)
    synthetic = add_front_matter_headings(kept, kept[0].soup if kept else BeautifulSoup("", "html.parser"))

    css = []
    for p in pkg.css_paths:
        if pkg.exists(p):
            css.append(decode_html_bytes(pkg.read(p)))
    italic_classes = parse_italic_classes(css)

    image_names = assign_image_names(p for d in kept for p in d.images)
    merged = merge_docs(kept)
    root = merged.body

    from src.footnote_import import apply_drop, apply_import, records_from_matches, write_footnotes_sidecar
    had_text = {sec["data-ingest-doc"] for sec in root.find_all("section", attrs={"data-ingest-doc": True})
                if _has_body_text(sec)}
    fn_matches = find_epub_footnotes(root)
    if fn_matches:
        if footnotes == "import":
            apply_import(fn_matches)
            if write:
                output_dir.mkdir(parents=True, exist_ok=True)
                write_footnotes_sidecar(output_dir, records_from_matches(fn_matches))
        else:
            apply_drop(fn_matches)
    emptied = drop_emptied_sections(merged, had_text) if fn_matches else []

    images_dir = output_dir / "images"
    do_images = extract_images and write
    if write:
        output_dir.mkdir(parents=True, exist_ok=True)
        if do_images:
            images_dir.mkdir(exist_ok=True)

    conv = EpubConverter(pkg, images_dir if do_images else None, do_images, italic_classes, image_names)
    text = conv.convert(root)
    text = normalize_chars(text)

    headings = [c["heading"] for c in conv.chapters]
    heading_set = set(headings) | set(conv.subtitles)
    text, joins = join_split_paragraphs(text, heading_set)
    anchored: list = []
    if boundary_images == "previous":
        text, anchored = anchor_boundary_images(text, set(headings))
    text = "\n\n".join(b for b in text.split("\n\n") if b != DOC_SENTINEL)
    recased: list = []
    if recase:
        text, recased = recase_lead_ins(text, headings, set(conv.subtitles))
    text = _normalize_whitespace(text)

    cover = None
    if pkg.cover_image and pkg.exists(pkg.cover_image):
        cover = "images/source_cover" + (posixpath.splitext(pkg.cover_image)[1] or ".jpg")
        if do_images:
            (output_dir / cover).write_bytes(pkg.read(pkg.cover_image))

    dropped_images = [posixpath.basename(p) for p in conv.dropped_images]
    for d in docs:
        if d.decision == "drop":
            dropped_images += [f"{posixpath.basename(p)} (in dropped {d.name})"
                               for p in d.images if p != pkg.cover_image]

    result = EpubIngestResult(
        text=text,
        chapters=conv.chapters,
        subtitles=conv.subtitles,
        metadata=pkg.metadata,
        docs=[{"doc": d.name, "words": d.words, "images": len(d.images),
               "decision": d.decision, "reason": d.reason, "first_text": d.first_text}
              for d in docs],
        synthetic_headings=synthetic,
        heading_summary=heading_summary,
        images_extracted=conv._images_downloaded,
        images_dropped=dropped_images,
        joins=joins,
        anchored_images=anchored,
        recased=recased,
        footnotes_count=len(fn_matches),
        footnotes_mode=footnotes,
        emptied_docs=emptied,
        cover=cover,
    )

    if write:
        (output_dir / "source.txt").write_text(text, encoding="utf-8")
        write_heading_outline(output_dir, conv.chapters)
        report_path = output_dir / REPORT_FILENAME
        tmp = report_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result.report_dict(str(epub_path)), indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, report_path)
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_plan(pkg: EpubPackage, docs: list[SpineDoc]) -> None:
    md = pkg.metadata
    print(f"Title    : {md.get('title') or '?'}")
    print(f"Author   : {md.get('creator') or '?'}")
    print(f"Publisher: {md.get('publisher') or '?'}")
    print()
    print(f" {'#':>3}  {'Document':<24} {'Words':>6} {'Img':>3}  {'Keep':<4}  Reason / first text")
    print(f" {'-'*3}  {'-'*24} {'-'*6} {'-'*3}  {'-'*4}  {'-'*40}")
    for d in docs:
        mark = "yes" if d.decision == "keep" else "DROP"
        info = d.reason if d.decision == "drop" else d.first_text
        print(f" {d.index:>3}  {d.name[:24]:<24} {d.words:>6} {len(d.images):>3}  {mark:<4}  {info[:60]}")
    content = [e for e in pkg.nav_entries if not ARTIFACT_LABEL_RE.match(e.label)]
    print(f"\nTOC entries: {len(pkg.nav_entries)} ({len(content)} content, "
          f"depths {sorted({e.depth for e in content}) or '-'})")


def print_extras(result: EpubIngestResult, output_dir: Path) -> None:
    dropped = [d for d in result.docs if d["decision"] == "drop"]
    if dropped:
        print(f"\nDropped documents ({len(dropped)}):")
        for d in dropped:
            print(f"  - {d['doc']}: {d['reason']}")
    if result.emptied_docs:
        print(f"Emptied by footnote extraction: {', '.join(result.emptied_docs)}")
    if result.images_dropped:
        print(f"Dropped images ({len(result.images_dropped)}): {', '.join(result.images_dropped[:8])}"
              + (" ..." if len(result.images_dropped) > 8 else ""))
    if result.cover:
        print(f"Source cover saved as {output_dir / result.cover} (not auto-used by the EPUB builder)")
    if result.subtitles:
        print(f"Subtitles kept as the first body line (not split on): {len(result.subtitles)}")
    for s in result.synthetic_headings:
        print(f"Added heading '{s['label']}' (level {s['level']}) to unheaded front matter in {s['doc']}")
    unresolved = result.heading_summary.get("unresolved") or []
    if unresolved:
        print(f"TOC entries not found in the text ({len(unresolved)}): {', '.join(unresolved[:6])}")
    if result.joins:
        print(f"Paragraphs rejoined across page/file breaks: {len(result.joins)}")
    if result.anchored_images:
        print(f"Between-chapter plates kept with the preceding chapter: {len(result.anchored_images)} "
              "(--boundary-images next to attach them to the following one)")
    if result.recased:
        print(f"Lead-ins recased: {len(result.recased)}  e.g. "
              + "; ".join(f"{c['from']!r} -> {c['to']!r}" for c in result.recased[:3]))
    print(f"Full decision record: {output_dir / REPORT_FILENAME}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Import an EPUB for translation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/ingest_epub.py book.epub --list
  python scripts/ingest_epub.py book.epub --output projects/mybook/
  python scripts/ingest_epub.py book.epub --output projects/mybook/ --drop-doc praise.xhtml
""",
    )
    parser.add_argument("source", help="Path to the .epub file")
    parser.add_argument("--output", help="Output directory (created if needed)")
    parser.add_argument("--list", action="store_true",
                        help="Dry run: show each spine document's keep/drop decision and exit")
    parser.add_argument("--no-images", action="store_true",
                        help="Insert placeholders but do not extract image files")
    parser.add_argument("--footnotes", choices=["import", "drop"], default="drop",
                        help="'import' keeps notes as [FOOTNOTE:N] tokens + footnotes.json; "
                             "'drop' (default) removes them cleanly")
    parser.add_argument("--no-recase", action="store_true",
                        help="Leave all-caps chapter lead-ins ('THE black yearling') as-is")
    parser.add_argument("--boundary-images", choices=["previous", "next"], default="previous",
                        help="An image-only page between two chapters belongs to the "
                             "chapter before it (default, matches print order) or the "
                             "one after it (a plate facing the chapter opening)")
    parser.add_argument("--drop-doc", action="append", default=[], metavar="DOC",
                        help="Force-drop a spine document (file name, path or idref); repeatable")
    parser.add_argument("--keep-doc", action="append", default=[], metavar="DOC",
                        help="Force-keep a spine document the heuristics dropped; repeatable")
    args = parser.parse_args(argv)
    if not args.list and not args.output:
        parser.error("--output is required (or use --list for a dry run)")
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.list:
        pkg, docs = plan_epub(args.source, drop_docs=args.drop_doc, keep_docs=args.keep_doc)
        print_plan(pkg, docs)
        return

    output_dir = Path(args.output)
    print(f"Reading {args.source} ...")
    result = ingest_epub(
        args.source,
        output_dir,
        footnotes=args.footnotes,
        recase=not args.no_recase,
        extract_images=not args.no_images,
        boundary_images=args.boundary_images,
        drop_docs=args.drop_doc,
        keep_docs=args.keep_doc,
    )
    print_report(
        source=args.source,
        output_dir=output_dir,
        chapters=result.chapters,
        total_words=result.word_count,
        images_downloaded=result.images_extracted,
        images_skipped=0,
        footnotes_count=result.footnotes_count,
        footnotes_mode=result.footnotes_mode,
        banner="EPUB IMPORT",
        split_hint=[
            "Headings come from the EPUB's own table of contents (headings.json),",
            "which the outline-aware split uses automatically. To split:",
            f"  python scripts/harness.py setup --project {output_dir}",
            "(or ingest + split in one step: harness.py setup --epub BOOK.epub --title ...)",
        ],
    )
    print_extras(result, output_dir)


if __name__ == "__main__":
    main()
