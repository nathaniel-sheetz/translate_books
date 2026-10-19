"""
What Amazon shoppers actually type: the Kindle-store search autocomplete.

``completion.amazon.com`` answers the search box's dropdown with the phrases
real shoppers complete a prefix to, in the store and locale asked for
(``alias=digital-text``, ``lop=es_US``). A phrase that comes back is evidence
of search demand that no amount of model intuition supplies, which is why the
seven keywords are drawn from here rather than drafted.

A plain HTTPS GET with no cookies works, unlike the store's search pages; it
is still throttled (``delay``) to stay a polite handful of requests per book.

The Kindle scope is a request, not a guarantee. When the Kindle index has
nothing for a prefix the endpoint falls back to all departments (``alias:
"aps"`` in the reply), and even a Kindle-aliased reply fuzzes a prefix it
cannot complete ("bambi a" -> "bamboo pajamas"). So a phrase is kept only if it
contains every word of the seed that produced it (``matches_seed``), and each
kept phrase records whether a Kindle-aliased reply carried it (``kindle``) —
weak evidence, but a phrase seen only in all-department fallbacks is shopper
language, not Kindle demand. The search box's own parameters (``_WIDGET``)
widen the replies, so they are sent too.
"""

from __future__ import annotations

import json
import re
import string
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

ENDPOINT = "https://completion.amazon.com/api/2017/suggestions"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
)


_WIDGET = {
    "suggestion-type": "KEYWORD", "plain-mid": "1", "client-info": "amazon-search-ui",
    "site-variant": "desktop", "version": "3", "page-type": "Search",
}


def _fetch(prefix: str, *, mid: str, lop: str, timeout: float) -> tuple[list[str], str]:
    """``(phrases, alias the reply was served from)``."""
    import requests

    resp = requests.get(
        ENDPOINT,
        params={"mid": mid, "alias": "digital-text", "prefix": prefix, "limit": 11, "lop": lop,
                **_WIDGET},
        headers={"User-Agent": _UA},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    values = [s.get("value", "") for s in data.get("suggestions") or [] if s.get("value")]
    return values, str(data.get("alias") or "")


def expand(seeds: Iterable[str]) -> list[tuple[str, str]]:
    """``(prefix, seed)``: each seed, then the seed + each letter a–z (the long tail)."""
    out = []
    for seed in seeds:
        seed = seed.strip()
        if not seed:
            continue
        out.append((seed, seed))
        out.extend((f"{seed} {letter}", seed) for letter in string.ascii_lowercase)
    return out


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


def matches_seed(phrase: str, seed: str) -> bool:
    """Every word of ``seed`` occurs in ``phrase`` (accents and case ignored)."""
    words = set(re.findall(r"\w+", _fold(phrase)))
    return all(w in words for w in re.findall(r"\w+", _fold(seed)))


def collect(
    seeds: Iterable[str],
    *,
    mid: str = "ATVPDKIKX0DER",
    lop: str = "es_US",
    do_expand: bool = False,
    delay: float = 0.4,
    timeout: float = 15.0,
    fetch: Optional[Callable[..., tuple[list[str], str]]] = None,
) -> dict[str, Any]:
    """Query every prefix and aggregate by phrase.

    Each phrase records the prefixes that surfaced it and its best (lowest)
    dropdown position: a phrase several seeds reach, near the top, is the
    strongest demand signal this source gives.
    """
    fetch = fetch or _fetch
    seeds = [s.strip() for s in seeds if s and s.strip()]
    prefixes = expand(seeds) if do_expand else [(s, s) for s in seeds]
    phrases: dict[str, dict[str, Any]] = {}
    errors = []
    kindle_replies = 0
    dropped = 0
    for i, (prefix, seed) in enumerate(prefixes):
        if i and delay:
            time.sleep(delay)
        try:
            values, alias = fetch(prefix, mid=mid, lop=lop, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - one bad prefix must not sink the run
            errors.append({"prefix": prefix, "error": str(exc)})
            continue
        if alias == "digital-text":
            kindle_replies += 1
        for position, value in enumerate(values, 1):
            if not matches_seed(value, seed):
                dropped += 1
                continue
            entry = phrases.setdefault(value, {"phrase": value, "hits": 0, "best_position": position,
                                               "kindle": False, "prefixes": []})
            entry["hits"] += 1
            entry["kindle"] = entry["kindle"] or alias == "digital-text"
            entry["best_position"] = min(entry["best_position"], position)
            if len(entry["prefixes"]) < 5:
                entry["prefixes"].append(prefix)
    ranked = sorted(phrases.values(), key=lambda e: (-e["hits"], e["best_position"], e["phrase"]))
    return {
        "marketplace_id": mid,
        "locale": lop,
        "seeds": seeds,
        "prefixes_queried": len(prefixes),
        "kindle_scoped_replies": kindle_replies,
        "dropped_off_seed": dropped,
        "phrases": ranked,
        "errors": errors,
    }


def write(result: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
