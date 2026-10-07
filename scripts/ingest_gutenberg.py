#!/usr/bin/env python3
"""
Import a Project Gutenberg HTML book for translation.

Fetches (or reads) a PG HTML file, strips boilerplate, downloads images,
inserts image placeholders, and reports chapter lengths to help you decide
how to chunk the book.

Where the page shows a thumbnail that links to a larger scan of the same
picture (Gutenberg's ``<a href="042_l.gif"><img src="042.jpg"></a>``), the
larger scan is the one imported. ``--inline-images`` keeps the thumbnails.

Usage:
    python scripts/ingest_gutenberg.py URL --output projects/mybook/
    python scripts/ingest_gutenberg.py URL --output projects/mybook/ --no-images
    python scripts/ingest_gutenberg.py local_file.htm --output projects/mybook/

The output source.txt feeds directly into split_book.py.
Image placeholders have the form  [IMAGE:images/filename.jpg]
and survive the chunking / translation pipeline for later re-insertion.
"""

import argparse
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Make the project root importable when run as a standalone script
# (``python scripts/ingest_gutenberg.py``) so ``from src...`` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --- optional imports with helpful error messages ---
try:
    import requests
except ImportError:
    sys.exit("requests is required: pip install requests")

try:
    from bs4 import BeautifulSoup, Comment, NavigableString, Tag
    from bs4.dammit import UnicodeDammit
except ImportError:
    sys.exit("beautifulsoup4 is required: pip install beautifulsoup4")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORDS_PER_CHUNK = 2000

# Tags whose text content should be walked normally
BLOCK_TAGS = {"p", "div", "blockquote", "li", "td", "th", "dd", "dt"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_TAGS = {"script", "style", "head", "nav", "aside", "footer", "header"}
ITALIC_TAGS = {"i", "em"}

# CSS classes whose elements should be silently dropped
SKIP_CLASSES = {"pagenum", "page-number", "pageno", "toc", "footnote", "endnote"}

# CSS classes marking an element as an image caption. Emitted as a [CAPTION]
# block so the EPUB builder can pair it with its image instead of rendering it
# as body prose.
#
# Deliberately excludes container classes like "illustration" and "figcenter":
# in Gutenberg those wrap BOTH the <img> and its caption, so treating one as a
# caption would swallow the image. The `not node.find("img")` guard in _walk
# enforces that regardless of what lands in this set.
CAPTION_CLASSES = {"caption", "figcaption", "imgcaption", "ill-caption", "illustration-caption"}

# PG boilerplate markers (case-insensitive substrings)
PG_START_MARKERS = [
    "start of the project gutenberg",
    "start of this project gutenberg",
]
PG_END_MARKERS = [
    "end of the project gutenberg",
    "end of this project gutenberg",
    "end of project gutenberg",
]

USER_AGENT = (
    "Mozilla/5.0 (compatible; book-translation-tool/1.0; "
    "+https://github.com/example/translate-books)"
)

# A link whose target is itself an image file. Gutenberg wraps each displayed
# thumbnail in one, pointing at the full-size scan of the same picture.
IMAGE_LINK_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def decode_html_bytes(raw: bytes) -> str:
    """
    Decode HTML bytes using the document's own encoding rather than assuming UTF-8.

    Used for both local files and URL responses so the same document decodes
    identically either way. UnicodeDammit tries, in order: a BOM, the charset
    declared in a <meta> tag, then statistical detection. Honoring the declared
    charset matters -- detection alone is unreliable on short or mostly-ASCII
    documents, and a Gutenberg Australia file declaring windows-1252 decodes as
    UTF-8 with every em-dash (0x97) and accent replaced by U+FFFD, silently
    corrupting the headings that drive the chapter split.
    """
    return UnicodeDammit(raw, is_html=True).unicode_markup


def fetch_html(source: str) -> tuple[str, str]:
    """
    Return (html_text, base_url).
    source may be a URL or a local file path.
    """
    path = Path(source)
    if path.exists():
        html = decode_html_bytes(path.read_bytes())
        base_url = path.parent.as_uri() + "/"
        return html, base_url

    # Treat as URL — strip fragment before fetching
    parsed = urllib.parse.urlparse(source)
    clean_url = urllib.parse.urlunparse(parsed._replace(fragment=""))
    base_url = clean_url.rsplit("/", 1)[0] + "/"

    resp = requests.get(clean_url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return decode_html_bytes(resp.content), base_url


def fetch_bytes(url: str, timeout: int = 20, *, base_url: str = "") -> bytes:
    """Return the bytes at ``url``. Raises on any failure.

    ``file://`` is read from disk, so a book ingested from a saved page (whose
    base URL is its folder) finds the images saved beside it. Only for such a
    page: one fetched over the network that names a local file is refused.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        if urllib.parse.urlparse(base_url).scheme != "file":
            raise ValueError("a page fetched over the network may not name a local file")
        return Path(urllib.request.url2pathname(parsed.path)).read_bytes()
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    resp.raise_for_status()
    return resp.content


# ---------------------------------------------------------------------------
# Linked larger scans
# ---------------------------------------------------------------------------

def linked_image(anchor: Tag, base_url: str) -> dict | None:
    """Describe ``anchor`` if it is a thumbnail linking to a larger image.

    Returns ``{url, name, inline, inline_names, alt}`` -- the link's target, and
    the image(s) displayed inside it -- or ``None`` for any other link. Two
    displayed images under one link are the halves of a picture the page split
    to wrap text around; the target is the picture whole.

    A link that also carries visible text is left alone: its text has to
    survive, and the ordinary walk keeps both.
    """
    href = anchor.get("href", "")
    if not href or anchor.get_text(strip=True):
        return None
    url = urllib.parse.urljoin(base_url, href)
    name = Path(urllib.parse.urlparse(url).path).name
    if Path(name).suffix.lower() not in IMAGE_LINK_SUFFIXES:
        return None
    images = [img for img in anchor.find_all("img") if img.get("src")]
    inline = [urllib.parse.urljoin(base_url, img["src"]) for img in images]
    if not inline or url in inline:
        return None
    return {
        "url": url,
        "name": name,
        "inline": inline,
        "inline_names": [Path(urllib.parse.urlparse(u).path).name for u in inline],
        "alt": next((img.get("alt") for img in images if img.get("alt")), ""),
    }


def linked_images(root: Tag, base_url: str) -> list[dict]:
    """Every thumbnail-to-larger-image link under ``root``, in document order.

    The map a backfill needs for a book ingested before the larger scans were
    preferred: which displayed file each larger one stands behind.
    """
    found = (linked_image(anchor, base_url) for anchor in root.find_all("a"))
    return [link for link in found if link is not None]


def _pixel_size(data: bytes) -> tuple[int, int] | None:
    """``(width, height)`` of image bytes, or ``None`` if they are not an image.

    Raises ImportError without Pillow, so the caller can tell "cannot check"
    from "checked, and it is not a picture".
    """
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            return image.size
    except Exception:  # noqa: BLE001 - any undecodable payload is one answer
        return None


# ---------------------------------------------------------------------------
# Boilerplate detection
# ---------------------------------------------------------------------------

def _contains_marker(text: str, markers: list[str]) -> bool:
    t = text.lower()
    return any(m in t for m in markers)


def find_book_body(soup: BeautifulSoup) -> Tag:
    """
    Return the subtree that contains just the book content, with PG
    boilerplate nodes removed in place.

    Handles two PG HTML formats:
    - New (cache/epub/): uses <section class="pg-boilerplate"> for header/footer
    - Old (files/): uses *** START/END OF THE PROJECT GUTENBERG *** text markers
    """
    body = soup.find("body") or soup

    # New PG format: remove sections with class "pg-boilerplate"
    for section in soup.find_all(True, class_="pg-boilerplate"):
        section.decompose()

    # Old PG format: text-marker based stripping
    all_elements = list(body.children)
    start_idx = None
    end_idx = None

    for i, el in enumerate(all_elements):
        text = el.get_text() if hasattr(el, "get_text") else str(el)
        if start_idx is None and _contains_marker(text, PG_START_MARKERS):
            start_idx = i
        if end_idx is None and _contains_marker(text, PG_END_MARKERS):
            end_idx = i
            break

    # Remove end marker and everything after it
    if end_idx is not None:
        for el in all_elements[end_idx:]:
            if hasattr(el, "decompose"):
                el.decompose()

    # Remove start marker and everything before it
    if start_idx is not None:
        for el in all_elements[: start_idx + 1]:
            if hasattr(el, "decompose"):
                el.decompose()

    return body


# ---------------------------------------------------------------------------
# HTML → text conversion
# ---------------------------------------------------------------------------

def _word_count(text: str) -> int:
    return len(text.split())


class Converter:
    """
    Walk the BeautifulSoup tree and produce clean plain text.
    Tracks chapters (via heading tags) and downloads images.
    """

    def __init__(self, base_url: str, images_dir: Path, download_images: bool,
                 prefer_linked_images: bool = True):
        self.base_url = base_url
        self.images_dir = images_dir
        self.download_images = download_images
        self.prefer_linked_images = prefer_linked_images

        self.parts: list[str] = []
        self.chapters: list[dict] = []       # {heading, level, word_offset}
        self._word_total = 0
        self._images_downloaded = 0
        self._images_skipped = 0
        self._images_linked = 0
        self._images_unlike: list[str] = []

    # ------------------------------------------------------------------
    def convert(self, root: Tag) -> str:
        self._walk(root)
        text = "".join(self.parts)
        text = _normalize_whitespace(text)
        return text

    # ------------------------------------------------------------------
    def _walk(self, node):
        # Drop HTML comments (BeautifulSoup exposes them as NavigableString subclasses)
        if isinstance(node, Comment):
            return

        if isinstance(node, NavigableString):
            s = str(node)
            stripped = s.strip()
            if not stripped:
                # Whitespace-only text node: emit a single separating space
                # so adjacent emitting siblings (e.g. two <i> tags) don't
                # collide into "_a__b_".
                if s and self.parts:
                    prev = self.parts[-1]
                    if prev and not prev[-1].isspace():
                        self.parts.append(" ")
                return
            # Collapse internal whitespace to single spaces
            normalized = re.sub(r"\s+", " ", stripped)
            # Preserve a boundary space if the original had leading/trailing whitespace —
            # this prevents word-merging when an inline tag (e.g. pagenum span) is skipped
            # and the surrounding text nodes lose their shared whitespace.
            # Only add a leading space if the previous output doesn't already end with whitespace.
            prev = self.parts[-1] if self.parts else ""
            if s[0].isspace() and prev and not prev[-1].isspace():
                normalized = " " + normalized
            if s[-1].isspace():
                normalized = normalized + " "
            self.parts.append(normalized)
            return

        if not isinstance(node, Tag):
            return

        tag = node.name.lower() if node.name else ""

        # Skip non-content tags and elements by CSS class (e.g. page number spans)
        classes = set(node.get("class") or [])
        if self._should_skip(node, tag, classes):
            return

        level = self._heading_level(node, tag)
        if level is not None:
            text = self._heading_text(node)
            if text:
                self._flush_heading(level, text)
            return

        if tag == "img":
            self._handle_image(node)
            return

        # A thumbnail wrapped in a link to its larger scan: import the scan.
        # Declined (so the walk carries on to the <img> inside) whenever the
        # link is not one, or its target cannot be had.
        if tag == "a" and self.prefer_linked_images and self._handle_linked_image(node):
            return

        # Anchor-only elements used as jump targets (no visible text)
        if tag == "a" and not node.get_text(strip=True) and not node.find("img"):
            return

        # Spacer divs (e.g. <div style="height: 4em;">)
        if tag == "div" and node.get("style") and not node.get_text(strip=True):
            return

        if tag == "br":
            self.parts.append("\n")
            return

        if tag == "hr":
            self.parts.append("\n\n---\n\n")
            return

        # Image caption -- <figcaption>, or a block element carrying a caption
        # class. Must be checked BEFORE the BLOCK_TAGS branch below, which would
        # otherwise swallow <p class="caption"> into an ordinary paragraph and
        # discard the class. The find("img") guard keeps a container element
        # (e.g. <div class="caption"> wrapping both image and text) from
        # swallowing its own image.
        if (tag == "figcaption" or (tag in BLOCK_TAGS and classes & CAPTION_CLASSES)) \
                and not node.find("img"):
            self._handle_caption(node)
            return

        if tag in BLOCK_TAGS or tag in ("body", "article", "section", "main"):
            self.parts.append("\n\n")
            for child in node.children:
                self._walk(child)
            self.parts.append("\n\n")
            return

        if self._is_italic(node, tag, classes):
            # Render inner content into a temporary buffer, then wrap the
            # joined text with underscore markers so downstream stages
            # (chunker, LLM, EPUB builder) can carry italics through.
            saved = self.parts
            self.parts = []
            for child in node.children:
                self._walk(child)
            # Collapse any newlines from <br> inside the italic span so the
            # EM_RE in epub_builder (which rejects [^_\n]) can match correctly.
            inner = re.sub(r"\s+", " ", "".join(self.parts)).strip()
            self.parts = saved
            if inner:
                # Ensure the italic marker is not immediately preceded by an
                # alphanumeric char or a closing `_`, both of which would fool
                # the lookbehind in EM_RE into refusing the match.
                # Punctuation such as an opening quote is fine: “_word_” matches.
                if self.parts and self.parts[-1] and (
                    self.parts[-1][-1].isalnum() or self.parts[-1][-1] == "_"
                ):
                    self.parts.append(" ")
                self.parts.append(f"_{inner}_")
            return

        # Inline tags and anything else — just recurse
        for child in node.children:
            self._walk(child)

    # ------------------------------------------------------------------
    # Overridable predicates. The EPUB importer (scripts/ingest_epub.py)
    # subclasses Converter and swaps these for CSS- and nav-aware versions;
    # the defaults below are the Gutenberg behavior.
    def _should_skip(self, node: Tag, tag: str, classes: set) -> bool:
        return tag in SKIP_TAGS or bool(classes & SKIP_CLASSES)

    def _heading_level(self, node: Tag, tag: str) -> int | None:
        if tag in HEADING_TAGS:
            return int(tag[1])
        return None

    def _heading_text(self, node: Tag) -> str:
        text = node.get_text(separator=" ", strip=True)
        # `separator` only joins *separate* text nodes -- it does not collapse
        # whitespace *within* one. A hand-typeset "staircase" title
        # (<h2>The GRASSHOPPER\nand\nthe MEASURING\nWORM</h2>) would keep its
        # embedded newlines, which later paragraph handling reads as blank-line
        # breaks, shattering the heading into fragments that leak into the
        # neighboring chapters. Body text already gets this treatment in the
        # NavigableString branch of _walk; the heading path returns early and
        # would otherwise skip it.
        return re.sub(r"\s+", " ", text).strip()

    def _is_italic(self, node: Tag, tag: str, classes: set) -> bool:
        return tag in ITALIC_TAGS

    # ------------------------------------------------------------------
    def _flush_heading(self, level: int, text: str):
        # Record chapter info before emitting. `level` is the h-tag depth: it is
        # what lets the splitter anchor on the document's own outline instead of
        # re-guessing chapter boundaries by regexing the flattened text.
        current_words = _word_count("".join(self.parts))
        self.chapters.append({
            "heading": text,
            "level": level,
            "word_offset": current_words,
        })
        self.parts.append(f"\n\n{text}\n\n")

    # ------------------------------------------------------------------
    def _handle_caption(self, node: Tag):
        # Render inner content into a temporary buffer (same technique as the
        # italic branch), then emit it as its own block prefixed with the
        # [CAPTION] marker. Internal whitespace is collapsed so a <br> inside
        # the caption cannot split it into two blocks -- the marker only counts
        # at the start of a block.
        saved = self.parts
        self.parts = []
        for child in node.children:
            self._walk(child)
        inner = re.sub(r"\s+", " ", "".join(self.parts)).strip()
        self.parts = saved
        if inner:
            self.parts.append(f"\n\n[CAPTION] {inner}\n\n")

    # ------------------------------------------------------------------
    def _handle_image(self, img: Tag):
        src = img.get("src", "")
        alt = img.get("alt", "")
        if not src:
            return

        # Resolve to absolute URL
        abs_url = urllib.parse.urljoin(self.base_url, src)
        filename = Path(urllib.parse.urlparse(abs_url).path).name
        if not filename:
            filename = "image.jpg"

        local_rel = f"images/{filename}"

        if self.download_images:
            dest = self.images_dir / filename
            if not dest.exists():
                try:
                    dest.write_bytes(fetch_bytes(abs_url, base_url=self.base_url))
                    self._images_downloaded += 1
                except Exception as exc:
                    print(f"  Warning: could not download {abs_url}: {exc}", file=sys.stderr)
                    self._images_skipped += 1
            else:
                self._images_downloaded += 1  # already present

        self._emit_image(local_rel, alt)

    def _handle_linked_image(self, anchor: Tag) -> bool:
        """Import the larger image ``anchor`` links to. False = not handled."""
        link = linked_image(anchor, self.base_url)
        if link is None:
            return False

        if self.download_images:
            dest = self.images_dir / link["name"]
            if not dest.exists():
                try:
                    data = fetch_bytes(link["url"], base_url=self.base_url)
                except Exception as exc:
                    print(
                        f"  Warning: larger scan {link['url']} unavailable ({exc}); "
                        "using the image the page displays",
                        file=sys.stderr,
                    )
                    return False
                if not self._is_the_larger_picture(data, link):
                    return False
                dest.write_bytes(data)
            self._images_downloaded += 1
        # With --no-images nothing can be checked, so the link is taken at its
        # word: the placeholder names the file a later fetch will look for.
        self._images_linked += 1
        self._emit_image(f"images/{link['name']}", link["alt"])
        return True

    def _is_the_larger_picture(self, data: bytes, link: dict) -> bool:
        """Whether a link's target is worth taking over what the page displays."""
        try:
            large = _pixel_size(data)
        except ImportError:
            return True  # no Pillow to measure with: trust the link
        if large is None:
            return False
        if len(link["inline"]) != 1:
            return True  # split halves: there is no one file to measure against
        try:
            thumbnail = fetch_bytes(link["inline"][0], base_url=self.base_url)
        except Exception:  # noqa: BLE001 - a thumbnail we cannot fetch loses
            return True
        small = _pixel_size(thumbnail)
        if small is None:
            return True
        if large[0] * large[1] <= small[0] * small[1]:
            return False
        # A link can point at the wrong plate. Putting one picture under
        # another's caption is worse than keeping a small one, so a scan that
        # does not look like its thumbnail is left for ``image_pass.py backfill``,
        # which can search the page's other scans for the one that does.
        from src.image_pass.inventory import SAME_PICTURE, picture_similarity

        score = picture_similarity(thumbnail, data)
        if score is not None and score < SAME_PICTURE:
            self._images_unlike.append(link["inline_names"][0])
            print(
                f"  Warning: {link['name']} does not look like {link['inline_names'][0]} "
                f"(similarity {score:.2f}); keeping the image the page displays",
                file=sys.stderr,
            )
            return False
        return True

    def _emit_image(self, local_rel: str, alt: str):
        placeholder = f"[IMAGE:{local_rel}]"
        if alt:
            placeholder = f"[IMAGE:{local_rel}:{alt}]"
        # Blank lines, not single newlines: the placeholder must be its own
        # blank-line-separated block for _render_body_blocks to recognize it
        # (it uses fullmatch). With single newlines a neighbouring <figcaption>
        # -- or any text emitted adjacent to the image -- glues onto the same
        # block, the fullmatch fails, and the raw token renders as escaped body
        # text. _normalize_whitespace collapses any excess back to one blank line.
        self.parts.append(f"\n\n{placeholder}\n\n")


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------

def _normalize_whitespace(text: str) -> str:
    """Collapse runs of 3+ blank lines to 2, and deduplicate consecutive --- dividers."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"(---\n\n){2,}", "---\n\n", text)
    return text.strip() + "\n"


# ---------------------------------------------------------------------------
# Chapter report
# ---------------------------------------------------------------------------

def build_chapter_report(chapters: list[dict], total_words: int) -> list[dict]:
    """
    Given a list of {heading, word_offset} dicts (in order), compute the word
    count for each chapter and return enriched dicts.
    """
    result = []
    for i, ch in enumerate(chapters):
        if i + 1 < len(chapters):
            words = chapters[i + 1]["word_offset"] - ch["word_offset"]
        else:
            words = total_words - ch["word_offset"]
        result.append({
            "number": i + 1,
            "heading": ch["heading"],
            "words": max(0, words),
            "chunks": max(1, round(words / WORDS_PER_CHUNK)),
        })
    return result


HEADING_OUTLINE_FILENAME = "headings.json"


def write_heading_outline(output_dir: Path, chapters: list[dict]) -> int:
    """
    Persist the document's heading outline next to ``source.txt`` and return
    how many headings were written.

    This is the structure the HTML already carries and that every text-only
    splitter has to re-guess: ``[{level, text}, ...]`` in document order. The
    splitter's ``headings`` pattern anchors on it (see
    ``src.book_splitter.load_heading_outline``), so a chapter boundary comes
    from the markup that declared it rather than from a regex over flattened
    prose.

    Headings are stored as *text*, not character offsets: offsets computed
    during the walk do not survive ``_normalize_whitespace``, and hand-editing
    ``source.txt`` is a supported workflow. Locating by text is robust to both
    and self-validating -- a heading that cannot be found is reported instead
    of silently shifting every boundary after it.
    """
    outline = [
        {"level": ch.get("level", 0), "text": ch["heading"]}
        for ch in chapters
        if ch.get("heading")
    ]
    path = Path(output_dir) / HEADING_OUTLINE_FILENAME
    # Write-then-rename: a plain write_text that dies partway (full disk,
    # interrupted ingest) leaves a truncated file, which the splitter can only
    # report as "broken sidecar" after the fact. os.replace is atomic on both
    # POSIX and Windows, so the reader sees either the old outline or the new one.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"version": 1, "headings": outline}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return len(outline)


def suggest_split_pattern(chapters: list[dict]) -> str | None:
    """
    Inspect heading text to suggest a --pattern value for split_book.py.
    Uses pattern definitions from split_patterns.json.
    """
    from src.book_splitter import load_split_patterns

    data = load_split_patterns()
    patterns = data["patterns"]
    detection_order = data.get("detection_order", list(patterns.keys()))

    for pattern_name in detection_order:
        defn = patterns.get(pattern_name)
        if not defn:
            continue
        detect_regex = defn.get("detect_regex")
        if not detect_regex:
            continue

        compiled = re.compile(detect_regex, re.I)
        min_ratio = defn.get("detect_min_ratio")

        if min_ratio is not None:
            hits = sum(1 for c in chapters if compiled.match(c["heading"].strip()))
            if hits > len(chapters) * min_ratio:
                return pattern_name
        else:
            hits = sum(1 for c in chapters if compiled.search(c["heading"]))
            if hits > 0:
                return pattern_name

    return None


def print_report(
    source: str,
    output_dir: Path,
    chapters: list[dict],
    total_words: int,
    images_downloaded: int,
    images_skipped: int,
    footnotes_count: int = 0,
    footnotes_mode: str = "drop",
    banner: str = "PROJECT GUTENBERG IMPORT",
    split_hint: list[str] | None = None,
    images_linked: int = 0,
    images_unlike: list[str] | None = None,
):
    print()
    print(f"=== {banner} ===")
    print(f"Source : {source}")
    if images_downloaded or images_skipped:
        img_msg = f"{images_downloaded} downloaded"
        if images_linked:
            img_msg += f" ({images_linked} as the larger scan the page links to)"
        if images_skipped:
            img_msg += f", {images_skipped} failed"
        print(f"Images : {img_msg} -> {output_dir / 'images'}/")
    if images_unlike:
        print(f"         {len(images_unlike)} kept as displayed because the scan each links to "
              f"looks like a different picture: {', '.join(images_unlike)}")
        print("         (scripts/image_pass.py backfill can look for the right scan)")
    if footnotes_count:
        if footnotes_mode == "import":
            print(f"Footnotes : {footnotes_count} imported -> {output_dir / 'footnotes.json'}")
            print("            (translate them with scripts/translate_footnotes.py)")
        else:
            print(f"Footnotes : {footnotes_count} detected and dropped")
            print("            (re-run with --footnotes import to keep them as reader footnotes)")

    if not chapters:
        print(f"\nNo chapter headings detected. Total words: {total_words:,}")
        print(f"Output saved: {output_dir / 'source.txt'}")
        return

    enriched = build_chapter_report(chapters, total_words)
    total_chunks = sum(c["chunks"] for c in enriched)

    print()
    print(f" {'#':>3}  {'Heading':<38}  {'Words':>6}  {'Est. chunks':>11}")
    print(f" {'-'*3}  {'-'*38}  {'-'*6}  {'-'*11}")
    for c in enriched:
        heading = c["heading"][:38]
        print(f" {c['number']:>3}  {heading:<38}  {c['words']:>6,}  {c['chunks']:>11}")
    print(f" {'':>3}  {'TOTAL':<38}  {total_words:>6,}  {total_chunks:>11}")
    print(f"\n* Estimated at ~{WORDS_PER_CHUNK:,} words/chunk (default)")

    pattern = suggest_split_pattern(chapters)
    rel_source = output_dir / "source.txt"
    rel_chapters = output_dir / "chapters/"
    print()
    if split_hint:
        for line in split_hint:
            print(line)
    elif pattern == "roman":
        print(f"Heading pattern: \"Chapter I / II / III ...\" -> --pattern roman")
        print("Suggested split command:")
        print(f"  python scripts/split_book.py {rel_source} \\")
        print(f"      --output {rel_chapters} --pattern roman")
    elif pattern == "numeric":
        print(f"Heading pattern: \"Chapter 1 / 2 / 3 ...\" -> --pattern numeric")
        print("Suggested split command:")
        print(f"  python scripts/split_book.py {rel_source} \\")
        print(f"      --output {rel_chapters} --pattern numeric")
    elif pattern == "bare_roman":
        print("Heading pattern: bare Roman numerals (I, II, III ...)")
        print("Suggested split command:")
        print(f"  python scripts/split_book.py {rel_source} \\")
        print(f"      --output {rel_chapters} \\")
        print(f"      --pattern custom --custom-regex \"^[IVXLCDM]+$\"")
    else:
        print("Could not auto-detect heading pattern.")
        print("Run split_book.py with --pattern custom --custom-regex <your pattern>")

    print(f"\nOutput saved: {output_dir / 'source.txt'}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Import a Project Gutenberg HTML book for translation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/ingest_gutenberg.py \\
      https://www.gutenberg.org/files/41350/41350-h/41350-h.htm \\
      --output projects/mybook/

  python scripts/ingest_gutenberg.py local_book.htm --output projects/mybook/

  # Skip image downloading (placeholders still inserted)
  python scripts/ingest_gutenberg.py URL --output projects/mybook/ --no-images
""",
    )
    parser.add_argument("source", help="Gutenberg HTML URL or local .htm/.html file path")
    parser.add_argument("--output", required=True, help="Output directory (created if needed)")
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Insert placeholders but do not download image files",
    )
    parser.add_argument(
        "--inline-images",
        action="store_true",
        help="Import the images the page displays even where each links to a "
             "larger scan (default: import the larger scan)",
    )
    parser.add_argument(
        "--footnotes",
        choices=["import", "drop"],
        default="drop",
        help="What to do with Gutenberg footnotes. 'import' captures them as "
             "[FOOTNOTE:N] tokens + footnotes.json for translation into reader "
             "footnotes; 'drop' (default) removes them cleanly. Either way the "
             "count is reported.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir = output_dir / "images"

    download_images = not args.no_images
    if download_images:
        images_dir.mkdir(exist_ok=True)

    print(f"Fetching {args.source} ...")
    html, base_url = fetch_html(args.source)

    print("Parsing HTML ...")
    soup = BeautifulSoup(html, "html.parser")
    body = find_book_body(soup)

    # Footnotes: always detected; imported as survivable tokens (+ sidecar) or
    # dropped cleanly, per --footnotes. Must run before the text conversion,
    # which would otherwise flatten the linkage away.
    from src.footnote_import import (
        find_footnotes,
        apply_import,
        apply_drop,
        records_from_matches,
        write_footnotes_sidecar,
    )
    fn_matches = find_footnotes(body)
    if fn_matches:
        if args.footnotes == "import":
            apply_import(fn_matches)
            write_footnotes_sidecar(output_dir, records_from_matches(fn_matches))
        else:
            apply_drop(fn_matches)

    converter = Converter(
        base_url=base_url,
        images_dir=images_dir,
        download_images=download_images,
        prefer_linked_images=not args.inline_images,
    )
    text = converter.convert(body)
    total_words = _word_count(text)

    out_path = output_dir / "source.txt"
    out_path.write_text(text, encoding="utf-8")
    write_heading_outline(output_dir, converter.chapters)

    print_report(
        source=args.source,
        output_dir=output_dir,
        chapters=converter.chapters,
        total_words=total_words,
        images_downloaded=converter._images_downloaded,
        images_skipped=converter._images_skipped,
        images_linked=converter._images_linked,
        images_unlike=converter._images_unlike,
        footnotes_count=len(fn_matches),
        footnotes_mode=args.footnotes,
    )


if __name__ == "__main__":
    main()
