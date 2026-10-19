"""Tests for src.kdp.amazon parsers and src.kdp.suggest, offline against trimmed pages."""

from pathlib import Path

import pytest

from src.kdp import amazon, suggest

FIXTURES = Path(__file__).parent / "fixtures" / "kdp"


def _html(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_node_from_href():
    assert amazon.node_from_href("/gp/bestsellers/digital-text/8407404011/ref=pd") == \
        ("digital-text", "8407404011")
    assert amazon.node_from_href("/Best-Sellers/zgbs/books/3045") == ("books", "3045")
    assert amazon.node_from_href("/dp/B0000") == (None, None)


def test_is_blocked():
    assert amazon.is_blocked('<form action="/errors/validateCaptcha">')
    assert amazon.is_blocked("<title>Sorry! Something went wrong!</title>")
    assert not amazon.is_blocked("<title>Amazon.com</title>")


def test_parse_search():
    results = amazon.parse_search(_html("search.html"))
    assert results[0]["asin"] == "B0CN1Y5FRY"
    assert results[0]["title"].startswith("Bambi, una vida en el bosque")
    assert results[0]["stars"] == "5.0" and results[0]["ratings"] == 4
    assert all(r["asin"] for r in results)


def test_parse_listing():
    listing = amazon.parse_listing(_html("listing.html"), asin="B0B1CB8853")
    assert listing["title"] == "CUENTOS PARA NIÑOS de 2 años (Spanish Edition)"
    assert listing["breadcrumb"][:3] == ["Kindle Store", "Kindle eBooks", "Children's eBooks"]
    assert listing["reading_age"] == "2 - 6 years"
    assert listing["language"] == "Spanish"
    assert listing["ratings"] == 225 and listing["stars"] == "4.1"
    assert listing["price"] == "$2.99"
    bsr = listing["bsr"]
    assert bsr["overall"] == 458900
    assert bsr["categories"][0] == {"rank": 323, "name": "Children's in Spanish",
                                    "node": "8407404011", "store": "digital-text"}
    assert bsr["categories"][-1]["store"] == "books"


def test_parse_listing_without_rank():
    assert amazon.parse_listing("<html><body></body></html>")["bsr"] == \
        {"overall": None, "categories": []}


def test_parse_bestsellers_node():
    page = amazon.parse_bestsellers(_html("bestsellers_node.html"))
    assert page["name"] == "Children's in Spanish"
    assert page["tree_label"] == "Infantil"
    assert page["path"] == ["Kindle Store", "Kindle eBooks", "Foreign Languages", "Spanish", "Infantil"]
    assert {"name": "Spanish", "node": "7735161011"} in page["ancestors"]
    sibling_names = [s["name"] for s in page["siblings"]]
    assert "Juvenil" in sibling_names and "Infantil" not in sibling_names
    assert sibling_names[0] == "Acción y Aventura"  # siblings before the selected node survive
    assert page["children"] == []
    assert [i["rank"] for i in page["items"]] == [1, 2, 3]
    assert page["items"][1] == {"rank": 2, "asin": "B0CCDL4XCR",
                                "title": "Harry Potter y la piedra filosofal (Spanish Edition)"}


def test_session_without_playwright(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _no_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_playwright)
    with pytest.raises(RuntimeError, match="requirements-kdp.txt"):
        with amazon.AmazonSession():
            pass


# ── suggest ────────────────────────────────────────────────────────────────

def test_matches_seed():
    assert suggest.matches_seed("libros clásicos para niños", "libros clasicos para")
    assert not suggest.matches_seed("bamboo pajamas", "bambi")


def test_expand():
    prefixes = suggest.expand(["bambi"])
    assert prefixes[0] == ("bambi", "bambi")
    assert prefixes[1] == ("bambi a", "bambi") and len(prefixes) == 27


def test_collect_aggregates_and_filters():
    replies = {
        "bambi": (["bambi", "bambi 2", "bamboo socks"], "digital-text"),
        "bambi e": (["bambi en español", "bambi"], "aps"),
        "cuentos": (["cuentos de animales"], "aps"),
    }

    def fake(prefix, **_):
        if prefix == "boom":
            raise OSError("down")
        return replies.get(prefix, ([], "digital-text"))

    result = suggest.collect(["bambi", "cuentos", "boom"], delay=0, fetch=fake)
    by_phrase = {p["phrase"]: p for p in result["phrases"]}
    assert "bamboo socks" not in by_phrase and result["dropped_off_seed"] == 1
    assert by_phrase["bambi"]["hits"] == 1 and by_phrase["bambi"]["kindle"]
    assert not by_phrase["cuentos de animales"]["kindle"]
    assert result["errors"] == [{"prefix": "boom", "error": "down"}]

    replies["bambi a"] = replies.pop("bambi e")
    expanded = suggest.collect(["bambi"], do_expand=True, delay=0, fetch=fake)
    assert expanded["prefixes_queried"] == 27
    assert {p["phrase"]: p["hits"] for p in expanded["phrases"]}["bambi"] == 2
