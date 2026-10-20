"""
Amazon.com evidence for categories: comparable books and the category tree.

Two halves, kept apart so the parsing is testable without a network:

- **Parsers** (``parse_search``, ``parse_listing``, ``parse_bestsellers``) take
  page HTML and return the few fields a listing decision needs: a comparable
  book's Best Sellers Rank ladder *with browse-node ids*, its reading age and
  publisher; a category's ranked items and where it sits in the tree.
- **``AmazonSession``** drives headless Chromium through Playwright and hands
  page HTML to the parsers. Plain HTTP gets a bot page from the store; a real
  browser that loads the homepage first gets the real one.

No page text leaves this module — callers get the extracted fields only, which
is what keeps a comps run to a few hundred tokens of JSON instead of tens of
thousands per page.

Playwright is an optional dependency (``requirements-kdp.txt``), imported only
when a session opens.
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from bs4 import BeautifulSoup

BASE = "https://www.amazon.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
)
# /gp/bestsellers/digital-text/8407404011/ref=… or /Best-Sellers-…/zgbs/digital-text/8407404011
_NODE_RE = re.compile(r"/(?:gp/bestsellers|zgbs)/([a-z-]+)/(\d+)")
_RANK_RE = re.compile(r"#\s?([\d,]+)\s+in\s+(.+)")
_ASIN_RE = re.compile(r"/dp/([A-Z0-9]{10})")
_INVISIBLE = re.compile(r"[‎‏‪-‮]")

INSTALL_HINT = (
    "Playwright is not installed. It is optional, needed only for comps/category: "
    "pip install -r requirements-kdp.txt && python -m playwright install chromium"
)


class PageError(RuntimeError):
    """One page would not load: a timeout, a network error, or a block."""


class BlockedError(PageError):
    """Amazon served a captcha or an error page instead of the one asked for."""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _INVISIBLE.sub("", text or "")).strip()


def _int(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def node_from_href(href: str) -> tuple[Optional[str], Optional[str]]:
    """``(store, node_id)`` from a best-seller link, else ``(None, None)``."""
    match = _NODE_RE.search(href or "")
    return (match.group(1), match.group(2)) if match else (None, None)


def is_blocked(html: str) -> bool:
    """A captcha form, or the store's "Sorry! Something went wrong" page."""
    return "validateCaptcha" in html or "<title>Sorry! Something went wrong" in html


# ── parsers ─────────────────────────────────────────────────────────────────

def parse_search(html: str) -> list[dict[str, Any]]:
    """Organic and sponsored results on a Kindle search page, in page order."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for result in soup.select('div[data-component-type="s-search-result"][data-asin]'):
        asin = result.get("data-asin") or ""
        if not asin:
            continue
        h2 = result.select_one("h2")
        stars = result.select_one('[aria-label*="out of 5 stars"]')
        ratings = next(
            (a.get("aria-label") for a in result.select("a[aria-label]")
             if "rating" in (a.get("aria-label") or "") and "out of" not in a.get("aria-label")),
            None,
        )
        out.append({
            "asin": asin,
            "title": _clean(h2.get_text(" ")) if h2 else "",
            "sponsored": bool(result.find(string=re.compile(r"^\s*Sponsored\s*$"))),
            "stars": _clean(stars.get("aria-label")).split(" out")[0] if stars else None,
            "ratings": _int(ratings) if ratings else None,
        })
    return out


def _detail_pairs(soup: BeautifulSoup) -> dict[str, str]:
    pairs = {}
    for li in soup.select("#detailBullets_feature_div li"):
        text = _clean(li.get_text(" "))
        label, sep, value = text.partition(" : ")
        if sep and label and not label.startswith("Best Sellers Rank"):
            pairs[label.strip()] = value.strip()
    for item in soup.select('[id^="rpi-attribute-"]'):
        key = item.get("id", "").removeprefix("rpi-attribute-")
        label = item.select_one(".rpi-attribute-label")
        value = item.select_one(".rpi-attribute-value")
        if label and value:
            pairs.setdefault(_clean(label.get_text(" ")), _clean(value.get_text(" ")))
        elif key:
            pairs.setdefault(key, _clean(item.get_text(" ")))
    return pairs


def parse_bsr(soup: BeautifulSoup) -> dict[str, Any]:
    """The Best Sellers Rank block: the overall Kindle Store rank and each category rung."""
    anchor = soup.find(string=re.compile("Best Sellers Rank"))
    if anchor is None:
        return {"overall": None, "categories": []}
    container = anchor.find_parent("li") or anchor.find_parent("tr")
    if container is None:
        return {"overall": None, "categories": []}
    text = _clean(container.get_text(" "))
    overall_match = re.search(r"#\s?([\d,]+)\s+in\s+Kindle Store", text)
    categories = []
    for link in container.select("a[href]"):
        store, node = node_from_href(link.get("href", ""))
        name = _clean(link.get_text(" "))
        if not node or name.startswith("See Top"):
            continue
        # The rank is the "#N in" text immediately before this link.
        before = _clean("".join(
            s for s in link.find_all_previous(string=True, limit=3)[::-1]
        ))
        rank_match = re.search(r"#\s?([\d,]+)\s+in\s*$", before)
        categories.append({
            "rank": _int(rank_match.group(1)) if rank_match else None,
            "name": name,
            "node": node,
            "store": store,
        })
    return {
        "overall": _int(overall_match.group(1)) if overall_match else None,
        "categories": categories,
    }


def _price(soup: BeautifulSoup) -> Optional[str]:
    for selector in ("#kindle-price", "#tmm-grid-swatch-KINDLE .slot-price",
                     "#tmmSwatches .a-color-price", ".a-price .a-offscreen"):
        node = soup.select_one(selector)
        text = _clean(node.get_text(" ")) if node else ""
        if text.startswith("$"):
            return text
    return None


def parse_listing(html: str, asin: Optional[str] = None) -> dict[str, Any]:
    """One product page, reduced to what a category and audience decision uses."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.select_one("#productTitle")
    byline = soup.select_one("#bylineInfo")
    crumbs = soup.select_one("#wayfinding-breadcrumbs_feature_div")
    stars = soup.select_one("#acrPopover")
    reviews = soup.select_one("#acrCustomerReviewText")
    pairs = _detail_pairs(soup)
    return {
        "asin": asin or pairs.get("ASIN"),
        "title": _clean(title.get_text(" ")) if title else "",
        "byline": _clean(byline.get_text(" ")) if byline else "",
        "breadcrumb": [_clean(a.get_text(" ")) for a in crumbs.select("a")] if crumbs else [],
        "publisher": pairs.get("Publisher"),
        "publication_date": pairs.get("Publication date"),
        "language": pairs.get("Language"),
        "print_length": pairs.get("Print length"),
        "reading_age": pairs.get("Reading age"),
        "grade_level": pairs.get("Grade level"),
        "series": pairs.get("Part of series"),
        "stars": _clean(stars.get("title")).split(" out")[0] if stars and stars.get("title") else None,
        "ratings": _int(reviews.get_text()) if reviews else None,
        "price": _price(soup),
        "bsr": parse_bsr(soup),
    }


def parse_bestsellers(html: str) -> dict[str, Any]:
    """A category's best-seller page: its ranked items and its place in the tree."""
    soup = BeautifulSoup(html, "html.parser")
    heading = next(
        (_clean(h.get_text(" ")) for h in soup.select("h1")
         if _clean(h.get_text(" ")).startswith("Best Sellers in")),
        "",
    )
    items = []
    for grid in soup.select("#gridItemRoot"):
        badge = grid.select_one(".zg-bdg-text")
        link = grid.select_one('a[href*="/dp/"]')
        match = _ASIN_RE.search(link.get("href", "")) if link else None
        image = grid.select_one("img[alt]")
        items.append({
            "rank": _int(badge.get_text()) if badge else None,
            "asin": match.group(1) if match else None,
            "title": _clean(image.get("alt")) if image else "",
        })

    # The left-hand tree is flat markup; nesting shows only as DOM depth. One
    # pass in document order: shallower links before the selected node are its
    # ancestors (the last one seen at each depth), same-depth links are its
    # siblings, deeper links after it are its children.
    before: list[tuple[int, dict]] = []
    after: list[tuple[int, dict]] = []
    current = None
    selected_depth = None
    for el in soup.select('[role="group"] a[href], [class*="zg-selected"]'):
        depth = len(list(el.parents))
        if el.name != "a":
            current = _clean(el.get_text(" ")).replace("(Current)", "").strip()
            selected_depth = depth
            continue
        _store, node = node_from_href(el.get("href", ""))
        entry = {"name": _clean(el.get_text(" ")), "node": node}
        (before if selected_depth is None else after).append((depth, entry))
    ancestors: list[dict] = []
    siblings: list[dict] = []
    children: list[dict] = []
    if selected_depth is not None:
        by_depth: dict[int, dict] = {}
        for depth, entry in before:
            if depth < selected_depth:
                by_depth[depth] = entry
                for deeper in [d for d in by_depth if d > depth]:
                    del by_depth[deeper]
            elif depth == selected_depth:
                siblings.append(entry)
        ancestors = [by_depth[d] for d in sorted(by_depth)]
        siblings += [e for d, e in after if d == selected_depth]
        children = [e for d, e in after if d > selected_depth]

    def _strip(entries: list[dict]) -> list[dict]:
        return [e for e in entries
                if e["name"] and e["name"] not in ("Any Department", "Amazon Best Sellers")]

    return {
        "name": heading.removeprefix("Best Sellers in").strip(),
        "tree_label": current,
        "path": [e["name"] for e in _strip(ancestors)] + ([current] if current else []),
        "ancestors": _strip(ancestors),
        "siblings": _strip(siblings),
        "children": _strip(children),
        "items": items,
    }


# ── browser ─────────────────────────────────────────────────────────────────

class AmazonSession:
    """One headless Chromium context; warm it on the homepage, then fetch pages.

    Use as a context manager. ``fetch`` returns page HTML, retrying once (after
    a fresh homepage visit) when the store serves a captcha or error page, and
    raises ``BlockedError`` if it does so twice, or ``PageError`` when the page
    does not load at all. A browser that will not start raises ``RuntimeError``.
    """

    def __init__(self, *, delay: float = 1.5, timeout_ms: int = 45000, headless: bool = True):
        self.delay = delay
        self.timeout_ms = timeout_ms
        self.headless = headless
        self.pages_loaded = 0
        self._pw = self._browser = self._page = None

    def __enter__(self) -> "AmazonSession":
        try:
            from playwright.sync_api import Error as PlaywrightError, sync_playwright
        except ImportError as exc:
            raise RuntimeError(INSTALL_HINT) from exc
        self._pw_error = PlaywrightError
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=self.headless)
            context = self._browser.new_context(
                user_agent=_UA, locale="en-US", viewport={"width": 1366, "height": 900},
            )
            self._page = context.new_page()
            self._warm()
        except BaseException as exc:
            # __exit__ never runs when __enter__ raises: close what started.
            self.__exit__(None, None, None)
            if isinstance(exc, PlaywrightError):
                raise RuntimeError(f"the browser session would not start: {exc}") from exc
            raise
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _goto(self, url: str) -> str:
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            time.sleep(self.delay)
            return self._page.content()
        except self._pw_error as exc:
            raise PageError(f"{url} did not load: {exc}") from exc

    def _warm(self) -> None:
        self._goto(BASE + "/")

    def fetch(self, path_or_url: str) -> str:
        url = path_or_url if path_or_url.startswith("http") else BASE + path_or_url
        for attempt in range(2):
            html = self._goto(url)
            self.pages_loaded += 1
            if not is_blocked(html):
                return html
            if attempt == 0:
                self._warm()
        raise BlockedError(f"Amazon blocked {url} twice (captcha or error page)")

    def search(self, query: str) -> list[dict[str, Any]]:
        from urllib.parse import quote_plus

        return parse_search(self.fetch(f"/s?k={quote_plus(query)}&i=digital-text"))

    def listing(self, asin: str) -> dict[str, Any]:
        return parse_listing(self.fetch(f"/dp/{asin}"), asin=asin)

    def bestsellers(self, node: str, store: str = "digital-text") -> dict[str, Any]:
        return parse_bestsellers(self.fetch(f"/gp/bestsellers/{store}/{node}"))
