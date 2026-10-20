"""
KDP's listing rules, checked before anything is written.

Sources (KDP Help, read 2026-10-19): "Public Domain Content" (G200743940),
"Make Your Book More Discoverable with Keywords" (G201298500), "Choose
Categories" (G200652170), "Book Description" (G201189630) and the Content
Guidelines' AI section (G200672390).

An **error** is a rule KDP states and enforces (or a defect that would ship in
the book): ``apply`` refuses while one stands. A **warning** is a rule KDP
states as advice, or a house check the operator should look at; it goes into
the report and the hand-off checklist.

The public-domain rules (the title descriptor, the opening bullet summary)
apply only when the listing answers Publishing Rights "public_domain". An
"own_copyright" listing must instead state its ``copyright_basis``, and is
warned about how KDP and US copyright law read that claim.
"""

from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser
from typing import Any

from src.kdp.gutenberg import names_trademark

DESCRIPTION_MAX = 4000
#: Per bullet: KDP's own example has four bullets that run ~180 characters together.
OPENING_SUMMARY_MAX = 80
#: The line that may introduce the bullets ("Esta edición traducida incluye:").
LEAD_IN_MAX = 120
KEYWORD_SLOTS = 7
KEYWORD_MAX = 50
CATEGORY_SLOTS = 3
TITLE_SUBTITLE_MAX = 200

#: KDP's Publishing Rights answers.
PUBLIC_DOMAIN = "public_domain"
OWN_COPYRIGHT = "own_copyright"
RIGHTS_MODES = (PUBLIC_DOMAIN, OWN_COPYRIGHT)

ALLOWED_TAGS = {"b", "i", "u", "em", "strong", "br", "p", "h4", "h5", "h6", "ul", "ol", "li"}
ENGLISH_DESCRIPTORS = ("(Translated)", "(Annotated)", "(Illustrated)")
_DESCRIPTOR_RE = re.compile(
    r"\((?:Translated|Annotated|Illustrated|Traducid[oa]\b[^)]*|Traducci[oó]n\b[^)]*)\)", re.I
)

PROGRAM_NAMES = ("kindle unlimited", "kdp select", "prime reading", "kindle select")
# Subjective, time-sensitive or price claims, in both languages.
CLAIM_WORDS = (
    "best", "bestseller", "best seller", "free", "on sale", "available now",
    "mejor", "mejores", "más vendido", "gratis", "gratuito", "oferta", "rebaja",
    "ya disponible",
)
# "new" is a claim in "nueva edición" but a name in "Nueva York": warn, don't refuse.
SOFT_CLAIM_WORDS = ("new", "nuevo", "nueva", "nuevos", "nuevas")
_STOP = {
    "de", "la", "el", "en", "y", "e", "para", "los", "las", "del", "con", "una", "un",
    "por", "al", "a", "o", "u", "su", "sus", "the", "of", "and", "in", "for", "to",
}
_URL_RE = re.compile(r"(https?://|www\.|\b[\w-]+\.(?:com|net|org|mx|es)\b)", re.I)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
# Nine or more digits, but not a thousands-grouped count ("10.000.000 de copias").
_PHONE_RE = re.compile(r"(?!\d{1,3}(?:[.,]\d{3})+\b)\+?(?:\d[\s().-]*){8,}\d")
# Block tags end a run of text: "bosque.</p><p>Es" must not read as "bosque.Es".
_BLOCK_TAGS = {"p", "br", "li", "ul", "ol", "h4", "h5", "h6"}
_PRICE_RE = re.compile(r"[$€]\s?\d|\b\d+(?:[.,]\d\d)?\s?(?:USD|dólares|pesos)\b", re.I)
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]")


def _fold(text: str) -> str:
    """Lowercase, strip accents — the comparison form for words."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9ñ]+", _fold(text)) if w not in _STOP and len(w) > 2}


class _Tags(HTMLParser):
    """Collect tag names, the first list's items, and the text before that list."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.opening_list_items: list[str] = []
        self.lead_in: list[str] = []
        self._state = "start"  # start -> in_list -> done
        self._depth = 0
        self._item: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        self.tags.append(tag)
        if tag in _BLOCK_TAGS:
            self.text.append(" ")
        if self._state == "start" and tag in ("ul", "ol"):
            self._state = "in_list"
        if self._state == "in_list":
            if tag in ("ul", "ol"):
                self._depth += 1
            elif tag == "li":
                self._item = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.text.append(" ")
        if self._state != "in_list":
            return
        if tag == "li":
            self.opening_list_items.append(" ".join("".join(self._item).split()))
        elif tag in ("ul", "ol"):
            self._depth -= 1
            if self._depth == 0:
                self._state = "done"

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._state == "start":
            self.lead_in.append(data)
        elif self._state == "in_list":
            self._item.append(data)


def parse_description(html: str) -> dict[str, Any]:
    """Tags, text, and the opening summary: the first list and the text before it.

    ``opening`` is "list" when the markup opens with the list, "lead_in" when a
    short line ending in a colon introduces it (KDP's own example: "This fully
    annotated edition … includes:"), "late" when the colon line is a paragraph
    or more, and "none" when no list follows text that introduces it.
    """
    parser = _Tags()
    parser.feed(html or "")
    parser.close()
    lead_in = " ".join("".join(parser.lead_in).split())
    if not parser.opening_list_items:
        opening = "none"
    elif not lead_in:
        opening = "list"
    elif lead_in.endswith(":"):
        opening = "lead_in" if len(lead_in) <= LEAD_IN_MAX else "late"
    else:
        opening = "none"
    return {
        "tags": parser.tags,
        "opening": opening,
        "lead_in": lead_in,
        "opening_list_items": parser.opening_list_items if opening != "none" else [],
        "text": " ".join("".join(parser.text).split()),
    }


def description_plain(html: str, *, drop_opening_list: bool = True) -> str:
    """The description as plain paragraphs, for ``dc:description``.

    The opening summary (its lead-in line and bullet list) is KDP's
    differentiation notice — compliance for the store page, noise in a reader's
    book info — so it is dropped by default.
    """
    html = html or ""
    if drop_opening_list and parse_description(html)["opening"] in ("list", "lead_in"):
        html = re.sub(r"^.*?<(ul|ol)\b.*?</\1\s*>", "", html, count=1, flags=re.I | re.S)
    text = re.sub(r"(?i)<\s*br\s*/?>", "\n", html)
    text = re.sub(r"(?i)</\s*(p|li|h4|h5|h6|ul|ol)\s*>", "\n", text)
    text = re.sub(r"(?i)<\s*li\s*>", "• ", text)
    text = re.sub(r"<[^>]+>", "", text)
    from html import unescape

    lines = [" ".join(line.split()) for line in unescape(text).splitlines()]
    return "\n".join(line for line in lines if line)


def _issue(out: list, code: str, detail: str, field: str) -> None:
    out.append({"code": code, "field": field, "detail": detail})


def rights_mode_of(listing: dict[str, Any], defaults: dict[str, Any] | None = None) -> str:
    """KDP's Publishing Rights answer: the listing's, else the house default."""
    mode = str(listing.get("publishing_rights")
               or (defaults or {}).get("publishing_rights") or PUBLIC_DOMAIN).strip()
    return mode if mode in RIGHTS_MODES else PUBLIC_DOMAIN


def _copyright_checks(
    listing: dict[str, Any], facts: dict[str, Any] | None, errors: list, warnings: list,
) -> None:
    """An owned-copyright claim on a translation of a public-domain work.

    KDP's public-domain page files a translation under "differentiated public
    domain"; claiming copyright instead is a position the operator takes and
    may have to defend. The listing must say what the claim rests on, and the
    report sets the measured human share of the text beside it.
    """
    basis = listing.get("copyright_basis") or {}
    if not str(basis.get("statement") or "").strip():
        _issue(errors, "missing_copyright_basis",
               "An owned-copyright listing of a translated public-domain work must state what "
               "the copyright covers (copyright_basis.statement): the human translation work, "
               "and any original content the book adds.", "copyright_basis")
    _issue(warnings, "own_copyright_kdp_framing",
           "KDP's public-domain page (G200743940) lists a translation as differentiated "
           "public domain, which bars KDP Select and the 70% royalty. This listing claims "
           "copyright instead; KDP may ask for the basis.", "publishing_rights")
    translations = (listing.get("ai_disclosure") or {}).get("translations") or {}
    if translations.get("ai_generated"):
        share = ((facts or {}).get("authorship") or {}).get("changed_fraction")
        measured = (f" The final text differs from the model's drafts in {share:.1%} of words."
                    if isinstance(share, (int, float)) else "")
        _issue(warnings, "own_copyright_ai_translation",
               "The translation is disclosed as AI-generated. US copyright covers only the "
               "human contribution (Copyright Office, Jan 2025), so the claim rests on human "
               "revision and original content." + measured, "copyright_basis")
    if not basis.get("original_content"):
        _issue(warnings, "own_copyright_no_original_content",
               "No original content listed (introduction, study questions, a guide). Content "
               "the translator wrote is the part of the claim that does not depend on how much "
               "of the translation is human.", "copyright_basis")


def check(
    listing: dict[str, Any],
    defaults: dict[str, Any] | None = None,
    *,
    facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Every error and warning for ``listing``. ``ok`` is true when there are no errors.

    ``facts`` (``facts.json``) adds the cross-checks that need the book: a
    worldwide sale against the source edition's rights flags.
    """
    defaults = defaults or {}
    errors: list[dict] = []
    warnings: list[dict] = []

    title = str(listing.get("title") or "").strip()
    descriptor = str(listing.get("descriptor") or "").strip()
    subtitle = str(listing.get("subtitle") or "").strip()
    author = str(listing.get("author") or "").strip()
    contributors = listing.get("contributors") or []
    translator = next(
        (str(c.get("name") or "").strip() for c in contributors
         if str(c.get("role") or "").lower() in ("translator", "traductor")),
        "",
    )

    rights_mode = rights_mode_of(listing, defaults)
    public_domain = rights_mode == PUBLIC_DOMAIN

    # ── title ──
    if not title:
        _issue(errors, "missing_title", "No title.", "title")
    title_field = f"{title} {descriptor}".strip()
    if not public_domain:
        pass  # The descriptor is a public-domain rule; an owned work carries none.
    elif not _DESCRIPTOR_RE.search(title_field):
        _issue(errors, "missing_descriptor",
               "A differentiated public-domain title must carry (Translated), (Annotated) or "
               "(Illustrated) in the title field (KDP G200743940).", "descriptor")
    elif not any(d in title_field for d in ENGLISH_DESCRIPTORS):
        _issue(warnings, "descriptor_not_english",
               f"{descriptor!r} is the house's Spanish rendering; KDP's wording is English. "
               "Watch the first review for a rejection.", "descriptor")
    if len(title_field) + len(subtitle) > TITLE_SUBTITLE_MAX:
        _issue(warnings, "title_subtitle_long",
               f"Title field + subtitle is {len(title_field) + len(subtitle)} characters "
               f"(KDP's limit is {TITLE_SUBTITLE_MAX}).", "subtitle")

    # ── contributors ──
    if not author:
        _issue(errors, "missing_author", "No author.", "author")
    if not translator:
        _issue(errors, "missing_translator",
               "A translation must credit the translator in the contributor field.", "contributors")

    # ── description ──
    html = str(listing.get("description_html") or "")
    parsed = parse_description(html)
    if not html.strip():
        _issue(errors, "missing_description", "No description.", "description_html")
    if len(html) > DESCRIPTION_MAX:
        _issue(errors, "description_too_long",
               f"{len(html)} characters including tags (limit {DESCRIPTION_MAX}).",
               "description_html")
    bad_tags = sorted({t for t in parsed["tags"] if t not in ALLOWED_TAGS})
    if bad_tags:
        _issue(errors, "description_bad_tag",
               f"Tags KDP does not render: {', '.join(bad_tags)}.", "description_html")
    if html.strip() and public_domain:
        if parsed["opening"] == "none":
            _issue(errors, "no_opening_summary",
                   "The description must open with a bulleted summary of what is original "
                   "in this edition, optionally after one short line ending in a colon "
                   "(KDP G200743940).", "description_html")
        elif parsed["opening"] == "late":
            _issue(warnings, "opening_summary_late",
                   f"The bullets come after {len(parsed['lead_in'])} characters of text; KDP "
                   "wants them at the beginning. Cut the lead-in to one line "
                   f"(≤ {LEAD_IN_MAX}).", "description_html")
        for item in parsed["opening_list_items"]:
            if len(item) > OPENING_SUMMARY_MAX:
                _issue(errors, "opening_summary_too_long",
                       f"Opening bullet {item!r} is {len(item)} characters "
                       f"(limit {OPENING_SUMMARY_MAX} per bullet).", "description_html")
    text = parsed["text"]
    # An epub.description override goes straight to dc:description: same rules.
    epub_description = str((listing.get("epub") or {}).get("description") or "")
    for field, value in (("description_html", text), ("epub.description", epub_description)):
        for code, pattern, what in (
            ("description_url", _URL_RE, "a URL or website"),
            ("description_email", _EMAIL_RE, "an email address"),
            ("description_phone", _PHONE_RE, "a phone number"),
            ("description_price", _PRICE_RE, "a price"),
            ("description_emoji", _EMOJI_RE, "an emoji"),
        ):
            match = pattern.search(value)
            if match:
                _issue(errors, code, f"The description contains {what} ({match.group(0)!r}).",
                       field)

    # ── keywords ──
    keyword_pairs = [
        (str((k.get("phrase") if isinstance(k, dict) else k) or "").strip(), k)
        for k in listing.get("keywords") or []
    ]
    keyword_pairs = [(phrase, entry) for phrase, entry in keyword_pairs if phrase]
    keywords = [phrase for phrase, _ in keyword_pairs]
    if len(keywords) != KEYWORD_SLOTS:
        _issue(errors, "keyword_count",
               f"{len(keywords)} keywords; fill all {KEYWORD_SLOTS} slots.", "keywords")
    metadata_words = _words(" ".join([title, subtitle, author, translator]))
    category_words = set()
    for cat in listing.get("categories") or []:
        path = cat.get("path") or []
        category_words |= _words(" ".join(path if isinstance(path, list) else [str(path)]))
        category_words |= _words(str(cat.get("name") or ""))
    stoplist = {_fold(w) for w in defaults.get("keyword_stoplist") or []}
    seen: dict[frozenset, str] = {}
    for i, phrase in enumerate(keywords, 1):
        field = f"keywords[{i}]"
        folded = _fold(phrase)
        if len(phrase) > KEYWORD_MAX:
            _issue(errors, "keyword_too_long", f"{phrase!r} is {len(phrase)} characters "
                   f"(limit {KEYWORD_MAX}).", field)
        if re.search(r"[\"“”«»]", phrase):
            _issue(errors, "keyword_quotes", f"{phrase!r} contains quotation marks.", field)
        if re.search(r"<[^>]+>", phrase):
            _issue(errors, "keyword_html", f"{phrase!r} contains HTML.", field)
        for name in PROGRAM_NAMES:
            if name in folded:
                _issue(errors, "keyword_program_name", f"{phrase!r} names an Amazon program.", field)
        for claim in CLAIM_WORDS:
            if re.search(rf"\b{re.escape(_fold(claim))}\b", folded):
                _issue(errors, "keyword_claim",
                       f"{phrase!r} makes a quality, time or price claim ({claim!r}).", field)
                break
        for claim in SOFT_CLAIM_WORDS:
            if re.search(rf"\b{claim}\b", folded):
                _issue(warnings, "keyword_maybe_claim",
                       f"{phrase!r} says {claim!r}: fine in a name (Nueva York), not as a "
                       "time claim (KDP bars \"new\").", field)
                break
        if "gutenberg" in folded:
            _issue(errors, "gutenberg_mention", f"{phrase!r} names Project Gutenberg.", field)
        words = _words(phrase)
        if words and words <= metadata_words:
            _issue(warnings, "keyword_repeats_metadata",
                   f"{phrase!r} only repeats the title or contributors, which Amazon already "
                   "indexes.", field)
        overlap = words & category_words
        if overlap and words <= category_words:
            _issue(warnings, "keyword_repeats_category",
                   f"{phrase!r} only repeats words from a chosen category.", field)
        hit = sorted(w for w in words if w in stoplist)
        if hit:
            _issue(warnings, "keyword_stoplist",
                   f"{phrase!r} spends characters on {', '.join(hit)}.", field)
        key = frozenset(words)
        if key and key in seen:
            _issue(warnings, "keyword_duplicate",
                   f"{phrase!r} has the same words as {seen[key]!r}.", field)
        seen.setdefault(key, phrase)
        entry = keyword_pairs[i - 1][1]
        if isinstance(entry, dict) and not entry.get("evidence"):
            _issue(warnings, "keyword_no_evidence",
                   f"{phrase!r} cites no search evidence.", field)

    # ── categories ──
    categories = listing.get("categories") or []
    if len(categories) != CATEGORY_SLOTS:
        _issue(errors, "category_count",
               f"{len(categories)} categories; KDP takes {CATEGORY_SLOTS}.", "categories")
    keys = [str(c.get("node") or "/".join(c.get("path") or []) or c.get("name")) for c in categories]
    if len(set(keys)) != len(keys):
        _issue(errors, "category_duplicate", "The same category is chosen twice.", "categories")
    for i, cat in enumerate(categories, 1):
        if cat.get("store") and cat["store"] != "digital-text":
            _issue(warnings, "category_not_kindle",
                   f"{cat.get('name')!r} is a {cat['store']} (print) node, not a Kindle one.",
                   f"categories[{i}]")

    # ── audience ──
    audience = listing.get("primary_audience") or {}
    lo, hi = audience.get("reading_age_min"), audience.get("reading_age_max")
    if lo is not None and hi is not None and lo > hi:
        _issue(errors, "reading_age_inverted", f"Reading age {lo}–{hi} runs backwards.",
               "primary_audience")
    for value in (lo, hi):
        if value is not None and not (0 <= value <= 18):
            _issue(warnings, "reading_age_range",
                   f"Reading age {value} is outside KDP's 0–18 children's range; leave it blank "
                   "for an adult book.", "primary_audience")

    # ── AI disclosure ──
    disclosure = listing.get("ai_disclosure") or {}
    for part in ("text", "images", "translations"):
        entry = disclosure.get(part)
        if not isinstance(entry, dict) or "ai_generated" not in entry:
            _issue(warnings, "ai_disclosure_incomplete",
                   f"No answer for AI-generated {part} (KDP asks for text, images and "
                   "translations separately).", f"ai_disclosure.{part}")
        elif entry.get("ai_generated") and not entry.get("tool"):
            _issue(warnings, "ai_disclosure_incomplete",
                   f"AI-generated {part} names no tool.", f"ai_disclosure.{part}")

    # ── an owned-copyright claim ──
    if not public_domain:
        _copyright_checks(listing, facts, errors, warnings)

    # ── territories against the source edition's rights ──
    flags = [f for f in (facts or {}).get("rights_flags") or []
             if f.get("code") in ("life_plus_70", "mexico_life_plus_100", "us_term")]
    territories = listing.get("territories") or {}
    if flags and territories.get("mode", "worldwide") == "worldwide":
        _issue(warnings, "territories_vs_rights",
               f"Selling worldwide, but facts.json carries {len(flags)} rights flag(s) "
               f"({', '.join(sorted({f['code'] for f in flags}))}). Choose individual "
               "territories or record why worldwide is safe.", "territories")

    # ── the book side: nothing may name Project Gutenberg ──
    epub = listing.get("epub") or {}
    notice = (epub.get("edition_notice") or {}).get("body") or ""
    for field, value in (("title", title), ("subtitle", subtitle),
                         ("description_html", text), ("epub.description", epub_description),
                         ("epub.edition_notice", notice),
                         ("epub.rights", str(epub.get("rights") or ""))):
        if names_trademark(value):
            _issue(errors, "gutenberg_mention",
                   "Project Gutenberg's trademark license bars its name on a commercial "
                   "derivative.", field)

    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "rights_mode": rights_mode,
            "counts": {"description_chars": len(html), "keywords": len(keywords),
                       "categories": len(categories)}}
