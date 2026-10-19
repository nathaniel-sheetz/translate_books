"""Tests for src.kdp.validate — KDP's listing rules."""

import copy

import pytest

from src.kdp import gutenberg
from src.kdp.validate import check, description_plain, parse_description

GOOD = {
    "title": "Bambi",
    "descriptor": "(Traducido)",
    "subtitle": "Una vida en el bosque",
    "author": "Felix Salten",
    "contributors": [{"role": "Translator", "name": "Ana Pérez"}],
    "description_html": (
        "<ul><li>Traducción nueva al español</li><li>Portada nueva</li></ul>"
        "<p>En lo más hondo del bosque nace un cervatillo.</p>"
    ),
    "primary_audience": {"reading_age_min": 10, "reading_age_max": 14},
    "categories": [
        {"path": ["Kindle eBooks", "Foreign Languages", "Spanish", "Infantil"], "node": "1", "store": "digital-text"},
        {"path": ["Kindle eBooks", "Foreign Languages", "Spanish", "Juvenil"], "node": "2", "store": "digital-text"},
        {"path": ["Kindle eBooks", "Children's eBooks", "Classics"], "node": "3", "store": "digital-text"},
    ],
    "keywords": [
        {"phrase": p, "evidence": "autocomplete"} for p in (
            "libros para niños en español", "cuentos de animales para niños",
            "novela de venados", "historias de la naturaleza", "literatura austriaca traducida",
            "lectura en voz alta en familia", "crecer y perder a una madre",
        )
    ],
    "ai_disclosure": {
        "text": {"ai_generated": False},
        "images": {"ai_generated": True, "tool": "Codex"},
        "translations": {"ai_generated": True, "tool": "Claude"},
    },
    "territories": {"mode": "worldwide"},
}


def _codes(result, kind="errors"):
    return [i["code"] for i in result[kind]]


def _with(**changes):
    listing = copy.deepcopy(GOOD)
    listing.update(changes)
    return listing


def test_good_listing_passes():
    result = check(GOOD)
    assert result["ok"], result["errors"]
    assert _codes(result, "warnings") == ["descriptor_not_english"]


def test_english_descriptor_has_no_warning():
    assert "descriptor_not_english" not in _codes(check(_with(descriptor="(Translated)")), "warnings")


def test_missing_descriptor_is_an_error():
    assert "missing_descriptor" in _codes(check(_with(descriptor="")))


def test_descriptor_inside_the_title_counts():
    assert check(_with(title="Bambi (Traducido y anotado)", descriptor=""))["ok"]


def test_missing_translator_and_author():
    codes = _codes(check(_with(contributors=[], author="")))
    assert "missing_translator" in codes and "missing_author" in codes


@pytest.mark.parametrize("html, code", [
    ("<p>Sin viñetas primero.</p><ul><li>Traducción</li></ul>", "no_opening_summary"),
    ("<ul><li>" + "x" * 81 + "</li></ul><p>Texto.</p>", "opening_summary_too_long"),
    ("<ul><li>Nuevo</li></ul><p>" + "a" * 4000 + "</p>", "description_too_long"),
    ("<ul><li>Nuevo</li></ul><div>Texto</div>", "description_bad_tag"),
    ("<ul><li>Nuevo</li></ul><p>Visita www.ejemplo.com</p>", "description_url"),
    ("<ul><li>Nuevo</li></ul><p>Escribe a a@b.com</p>", "description_email"),
    ("<ul><li>Nuevo</li></ul><p>Solo $0.99 hoy</p>", "description_price"),
    ("<ul><li>Nuevo</li></ul><p>Un libro 🦌 precioso</p>", "description_emoji"),
    ("<ul><li>Nuevo</li></ul><p>Del Proyecto Gutenberg</p>", "gutenberg_mention"),
])
def test_description_errors(html, code):
    assert code in _codes(check(_with(description_html=html)))


def test_empty_paragraph_before_the_list_is_tolerated():
    assert check(_with(description_html="<p></p><ul><li>Nuevo</li></ul><p>Texto.</p>"))["ok"]


def test_a_one_line_lead_in_before_the_bullets_is_allowed():
    # KDP's own example opens "This fully annotated edition of … includes:".
    html = ("<p>Esta edición traducida incluye:</p><ul><li>Traducción original al español "
            "latino por Ana M. Pérez</li><li>Texto íntegro (sin abreviar)</li></ul>"
            "<p>Sara Crewe parece una princesita.</p>")
    result = check(_with(description_html=html))
    assert result["ok"], result["errors"]
    assert "opening_summary_late" not in _codes(result, "warnings")


def test_the_80_character_limit_is_per_bullet():
    # Together 90 characters, each under 80: passes.
    html = "<ul><li>" + "a" * 60 + "</li><li>" + "b" * 30 + "</li></ul><p>Texto.</p>"
    assert check(_with(description_html=html))["ok"]


def test_a_paragraph_long_lead_in_is_a_warning():
    html = "<p>" + "Hace siglos. " * 12 + "Esta edición incluye:</p><ul><li>Traducción</li></ul>"
    result = check(_with(description_html=html))
    assert result["ok"]
    assert "opening_summary_late" in _codes(result, "warnings")


def test_spanish_noun_descriptor_passes():
    assert "missing_descriptor" not in _codes(check(_with(descriptor="(Traducción)")))


def test_keyword_count_and_length():
    listing = _with(keywords=GOOD["keywords"][:6])
    assert "keyword_count" in _codes(check(listing))
    listing = copy.deepcopy(GOOD)
    listing["keywords"][0]["phrase"] = "x" * 51
    assert "keyword_too_long" in _codes(check(listing))


@pytest.mark.parametrize("phrase, code", [
    ('"bambi"', "keyword_quotes"),
    ("<b>bambi</b>", "keyword_html"),
    ("libros kindle unlimited", "keyword_program_name"),
    ("el mejor libro de animales", "keyword_claim"),
    ("libros gratis en español", "keyword_claim"),
    ("proyecto gutenberg", "gutenberg_mention"),
])
def test_keyword_errors(phrase, code):
    listing = copy.deepcopy(GOOD)
    listing["keywords"][0]["phrase"] = phrase
    assert code in _codes(check(listing))


@pytest.mark.parametrize("phrase, code", [
    ("bambi felix salten", "keyword_repeats_metadata"),
    ("infantil juvenil", "keyword_repeats_category"),
    ("ebook de animales", "keyword_stoplist"),
    ("historias naturaleza de la", "keyword_duplicate"),
    ("cuentos de nueva york", "keyword_maybe_claim"),
])
def test_keyword_warnings(phrase, code):
    listing = copy.deepcopy(GOOD)
    listing["keywords"][0]["phrase"] = phrase
    result = check(listing, {"keyword_stoplist": ["ebook"]})
    assert code in _codes(result, "warnings")
    assert result["ok"]


def test_plain_string_keywords_are_accepted_but_cite_no_evidence():
    listing = _with(keywords=[k["phrase"] for k in GOOD["keywords"]])
    result = check(listing)
    assert result["ok"]
    assert "keyword_no_evidence" not in _codes(result, "warnings")


def test_categories():
    assert "category_count" in _codes(check(_with(categories=GOOD["categories"][:2])))
    dup = copy.deepcopy(GOOD)
    dup["categories"][2]["node"] = "1"
    assert "category_duplicate" in _codes(check(dup))
    books = copy.deepcopy(GOOD)
    books["categories"][2]["store"] = "books"
    assert "category_not_kindle" in _codes(check(books), "warnings")


def test_reading_age():
    assert "reading_age_inverted" in _codes(
        check(_with(primary_audience={"reading_age_min": 14, "reading_age_max": 10})))
    assert "reading_age_range" in _codes(
        check(_with(primary_audience={"reading_age_min": 10, "reading_age_max": 21})), "warnings")


def test_ai_disclosure_incomplete():
    listing = _with(ai_disclosure={"translations": {"ai_generated": True}})
    warnings = check(listing)["warnings"]
    fields = {w["field"] for w in warnings if w["code"] == "ai_disclosure_incomplete"}
    assert fields == {"ai_disclosure.text", "ai_disclosure.images", "ai_disclosure.translations"}


def test_worldwide_sale_against_rights_flags():
    facts = {"rights_flags": [{"code": "life_plus_70", "detail": "x"}]}
    assert "territories_vs_rights" in _codes(check(GOOD, facts=facts), "warnings")
    individual = _with(territories={"mode": "individual", "exclude": ["DE"]})
    assert "territories_vs_rights" not in _codes(check(individual, facts=facts), "warnings")


def test_gutenberg_in_the_edition_notice():
    listing = _with(epub={"edition_notice": {"body": "Texto fuente: Project Gutenberg."}})
    assert "gutenberg_mention" in _codes(check(listing))


def test_johannes_gutenberg_in_the_edition_notice():
    listing = _with(epub={"edition_notice": {"body": "Gutenberg inventó la imprenta."}})
    assert "gutenberg_mention" not in _codes(check(listing))


@pytest.mark.parametrize("text, named", [
    ("Texto del Project Gutenberg.", True),
    ("Del Proyecto Gutenberg", True),
    ("Projet-Gutenberg", True),
    ("https://www.gutenberg.org/ebooks/1342", True),
    ("The Project Gutenberg-tm License", True),
    ("Gutenberg™ trademark", True),
    ("This Gutenberg eBook", True),
    ("su invención se debe a Gutenberg.", False),
    ("Johannes Gutenberg, orfebre e impresor alemán", False),
])
def test_names_trademark(text, named):
    assert gutenberg.names_trademark(text) is named


def test_parse_description_opening_list():
    parsed = parse_description("<ul><li>Uno</li><li>Dos</li></ul><p>Tres</p>")
    assert parsed["opening_list_items"] == ["Uno", "Dos"]
    assert parse_description("<p>Antes</p><ul><li>Uno</li></ul>")["opening_list_items"] == []


def test_description_plain_drops_the_kdp_summary():
    html = "<ul><li>A</li></ul><p>Uno &amp; dos.</p><p>Tres<br>cuatro</p><ul><li>C</li></ul>"
    assert description_plain(html) == "Uno & dos.\nTres\ncuatro\n• C"
    assert description_plain(html, drop_opening_list=False).startswith("• A")


def test_description_plain_drops_the_lead_in_with_the_summary():
    html = "<p>Esta edición incluye:</p><ul><li>A</li></ul><p>La historia.</p>"
    assert description_plain(html) == "La historia."
    # Prose that merely precedes a list is not a summary, so nothing is dropped.
    html = "<p>La historia.</p><ul><li>A</li></ul>"
    assert description_plain(html) == "La historia.\n• A"


OWNED = {
    **copy.deepcopy(GOOD),
    "publishing_rights": "own_copyright",
    "descriptor": "",
    "description_html": "<p><b>Guillermo Tell y la manzana.</b></p><p>Historias famosas.</p>",
    "copyright_basis": {
        "statement": "Traducción revisada por el traductor y guía de narración original.",
        "original_content": ["Guía Charlotte Mason: preguntas de narración por capítulo"],
    },
}


def test_owned_copyright_drops_the_public_domain_rules():
    result = check(OWNED)
    assert result["ok"], result["errors"]
    assert result["rights_mode"] == "own_copyright"
    codes = _codes(result, "warnings")
    assert "own_copyright_kdp_framing" in codes
    assert "own_copyright_ai_translation" in codes
    assert "own_copyright_no_original_content" not in codes
    assert "descriptor_not_english" not in codes


def test_owned_copyright_needs_a_basis():
    listing = copy.deepcopy(OWNED)
    listing["copyright_basis"] = {}
    result = check(listing)
    assert "missing_copyright_basis" in _codes(result)
    assert "own_copyright_no_original_content" in _codes(result, "warnings")


def test_owned_copyright_quotes_the_measured_human_share():
    facts = {"authorship": {"changed_fraction": 0.013}}
    detail = next(w["detail"] for w in check(OWNED, facts=facts)["warnings"]
                  if w["code"] == "own_copyright_ai_translation")
    assert "1.3% of words" in detail


def test_rights_mode_falls_back_to_the_house_default():
    listing = copy.deepcopy(OWNED)
    del listing["publishing_rights"]
    assert check(listing, {"publishing_rights": "own_copyright"})["ok"]
    assert "missing_descriptor" in _codes(check(listing))
    assert "missing_descriptor" in _codes(check(_with(publishing_rights="nonsense", descriptor="")))
