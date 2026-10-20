"""
What Project Gutenberg's catalog record says about the source book.

The ingest keeps only the body text, so the catalog facts a listing needs —
Library of Congress subject headings and class, Gutenberg's bookshelves, the
source edition's year, and the birth/death years of everyone credited — are
fetched once from ``gutenberg.org/ebooks/<id>.rdf`` and cached under
``.harness/kdp/``.

The death years matter for rights, not just for the copyright page: KDP sells
worldwide by default, and a source edition whose translator or illustrator died
within the last seventy years is public domain in the USA but not in a
life-plus-seventy territory. ``rights_flags`` turns that into named warnings.

Nothing here ever reaches the published book: Project Gutenberg's trademark
license bars its name on a commercial derivative.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Any, Optional

RDF_URL = "https://www.gutenberg.org/ebooks/{id}.rdf"

_NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dcterms": "http://purl.org/dc/terms/",
    "pgterms": "http://www.gutenberg.org/2009/pgterms/",
    "dcam": "http://purl.org/dc/dcam/",
    "marcrel": "http://id.loc.gov/vocabulary/relators/",
}

# /ebooks/72577, /cache/epub/72577/pg72577-images.html, /files/3048/3048-h/3048-h.htm,
# /epub/72577/... — the id is the first number after one of those path segments.
_ID_RE = re.compile(r"gutenberg\.org/(?:ebooks|cache/epub|files|epub)/(\d+)")

# The trademark, not the man: "Project/Proyecto Gutenberg", the domain, and the
# license's "Gutenberg-tm" / "Gutenberg eBook" forms. A bare "Gutenberg" is
# usually Johannes, in a chapter on printing, and must not fail a book.
_TRADEMARK_RE = re.compile(
    r"\b(?:project|proyecto|projet|projekt|progetto)[\s\-]+gutenberg"
    r"|gutenberg\.org"
    r"|\bgutenberg[\s\-]*(?:™|(?:tm|e-?books?|e-?texts?)\b)",
    re.I)

# MARC relator codes Gutenberg uses, to the role a listing cares about.
_ROLES = {
    "creator": "author",
    "aut": "author",
    "trl": "translator",
    "ill": "illustrator",
    "edt": "editor",
    "aui": "introduction",
    "com": "compiler",
}

#: The term of copyright in life-plus-seventy territories.
LIFE_PLUS = 70
#: Mexico's term since 2003 — the readership these translations are written for.
MEXICO_LIFE_PLUS = 100
#: US public domain covers works published before 1 January of (this year - 95).
US_TERM_YEARS = 95


def names_trademark(text: str) -> bool:
    """Whether ``text`` names Project Gutenberg (the trademark), not the printer."""
    return bool(_TRADEMARK_RE.search(text or ""))


def gutenberg_id(url: Optional[str]) -> Optional[int]:
    """The ebook number in a Gutenberg URL, else ``None``."""
    if not url:
        return None
    match = _ID_RE.search(str(url))
    return int(match.group(1)) if match else None


def _text(node: Optional[ET.Element]) -> str:
    return (node.text or "").strip() if node is not None else ""


def _int(value: str) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_rdf(xml_text: str) -> dict[str, Any]:
    """The fields a listing needs from one Gutenberg RDF record."""
    root = ET.fromstring(xml_text)
    ebook = root.find("pgterms:ebook", _NS)
    if ebook is None:
        raise ValueError("no pgterms:ebook in RDF record")

    def _values(tag: str, vocab: Optional[str] = None) -> list[str]:
        out = []
        for desc in ebook.findall(f"{tag}/rdf:Description", _NS):
            member = desc.find("dcam:memberOf", _NS)
            resource = member.get(f"{{{_NS['rdf']}}}resource", "") if member is not None else ""
            if vocab and not resource.endswith(vocab):
                continue
            value = _text(desc.find("rdf:value", _NS))
            if value:
                out.append(value)
        return out

    agents = []
    for child in ebook:
        ns, _, local = child.tag.partition("}")
        if ns.lstrip("{") == _NS["dcterms"] and local == "creator":
            role = "author"
        elif ns.lstrip("{") == _NS["marcrel"]:
            role = _ROLES.get(local, local)
        else:
            continue
        agent = child.find("pgterms:agent", _NS)
        if agent is None:
            continue
        agents.append({
            "role": role,
            "name": _text(agent.find("pgterms:name", _NS)),
            "birth": _int(_text(agent.find("pgterms:birthdate", _NS))),
            "death": _int(_text(agent.find("pgterms:deathdate", _NS))),
        })

    title = _text(ebook.find("dcterms:title", _NS))
    # MARC subfield markers leak into some titles: "Bambi : $b A life in the woods".
    title = re.sub(r"\s*\$[a-z]\s*", " ", title).replace(" :", ":").strip()
    reading_ease = _text(ebook.find("pgterms:marc908", _NS))
    summary = _text(ebook.find("pgterms:marc520", _NS))
    summary = summary.replace("(This is an automatically generated summary.)", "").strip()

    return {
        "title": title,
        "agents": agents,
        "lcsh": _values("dcterms:subject", "/LCSH"),
        "lcc": _values("dcterms:subject", "/LCC"),
        "bookshelves": [b.removeprefix("Category: ") for b in _values("pgterms:bookshelf")],
        "language": _values("dcterms:language"),
        "source_edition_year": _int(_text(ebook.find("pgterms:marc906", _NS))),
        "imprint": _text(ebook.find("pgterms:marc260", _NS)),
        "summary": summary,
        "reading_ease": reading_ease,
        "rights": _text(ebook.find("dcterms:rights", _NS)),
        "downloads": _int(_text(ebook.find("pgterms:downloads", _NS))),
    }


def fetch_rdf(book_id: int, cache_dir: Path, *, timeout: float = 30.0) -> tuple[Optional[dict], Optional[str]]:
    """``(record, error)`` for ``book_id``, reading ``cache_dir/gutenberg_<id>.rdf`` first."""
    cache = Path(cache_dir) / f"gutenberg_{book_id}.rdf"
    xml_text = None
    if cache.exists():
        xml_text = cache.read_text(encoding="utf-8")
    else:
        try:
            import requests

            resp = requests.get(RDF_URL.format(id=book_id), timeout=timeout)
            resp.raise_for_status()
            xml_text = resp.text
        except Exception as exc:  # noqa: BLE001 - catalog facts are advisory
            return None, f"could not fetch Gutenberg RDF for #{book_id}: {exc}"
    try:
        record = parse_rdf(xml_text)
    except (ET.ParseError, ValueError) as exc:
        # Drop a bad cache (an HTML interstitial, a truncated file) so the next call refetches.
        cache.unlink(missing_ok=True)
        return None, f"unparseable Gutenberg RDF for #{book_id}: {exc}"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(xml_text, encoding="utf-8")
    return record, None


def rights_flags(record: Optional[dict], *, today: Optional[date] = None) -> list[dict[str, str]]:
    """Named warnings about where the *source edition* may not be public domain.

    - ``life_plus_70``: someone credited on the source edition died less than
      seventy full years ago, so the text is still in copyright in
      life-plus-seventy territories (most of Europe).
    - ``mexico_life_plus_100``: past seventy years but inside Mexico's hundred.
    - ``us_term``: the source edition's year is too recent for US public domain.
    - ``unknown_death``: a credited person has no death year on record.
    """
    if not record:
        return []
    year = (today or date.today()).year
    flags = []
    for agent in record.get("agents") or []:
        death = agent.get("death")
        who = f"{agent.get('name') or '?'} ({agent.get('role')})"
        if death is None:
            flags.append({
                "code": "unknown_death",
                "detail": f"{who} has no death year on record; check the term by hand.",
            })
        elif death + LIFE_PLUS >= year:
            flags.append({
                "code": "life_plus_70",
                "detail": (
                    f"{who} died {death}: the source edition stays in copyright in "
                    f"life+70 territories until 1 Jan {death + LIFE_PLUS + 1}. "
                    "Restrict KDP territories, or confirm."
                ),
            })
        elif death + MEXICO_LIFE_PLUS >= year:
            flags.append({
                "code": "mexico_life_plus_100",
                "detail": (
                    f"{who} died {death}: Mexico's term is life+100 for works not already "
                    "public domain when it was extended, so this may be in copyright there "
                    f"until 1 Jan {death + MEXICO_LIFE_PLUS + 1}. Confirm before selling "
                    "in the Amazon.com.mx territory."
                ),
            })
    edition_year = record.get("source_edition_year")
    if edition_year and edition_year >= year - US_TERM_YEARS:
        flags.append({
            "code": "us_term",
            "detail": (
                f"Source edition dated {edition_year}: US public domain covers works "
                f"published before {year - US_TERM_YEARS}. Confirm the copyright was not renewed."
            ),
        })
    return flags
