"""Tests for src/audit/context.py — finding a reader edit and the text before it."""

from __future__ import annotations

from src.audit import context as ctx

EN_CURLY = (
    "CHAPTER I\n\n"
    "“It was in the year 79. Vesuvius was then a peaceful mountain.\n\n"
    "“The old volcano, which seemed forever lulled, suddenly awakened.”\n\n"
    "Everyone listened. Nobody said a word for a while."
)
ES = (
    "»Fue en el año 79. El Vesubio era entonces una montaña apacible.\n\n"
    "»El viejo volcán, que parecía adormecido para siempre, despertó de pronto.\n\n"
    "Todos escuchaban. Nadie dijo nada durante un rato."
)
EN_STRAIGHT = (
    '"I cannot come today. The road is closed by the snow.\n\n'
    '"I will come tomorrow, if the weather improves."\n\n'
    '"Very well," she answered.'
)


def test_paragraphs_split_on_whitespace_only_lines():
    assert ctx.paragraphs("Uno.\n \nDos.\n\n\nTres.") == ["Uno.", "Dos.", "Tres."]


def test_tail_keeps_the_paragraph_opening():
    text = "»Fue en el año 79. " + "x" * 600 + " final."
    cut = ctx.tail(text)
    assert cut.startswith("»Fue en el año 79.")
    assert cut.endswith(" final.")
    assert " … " in cut
    assert len(cut) < len(text)


def test_tail_leaves_short_text_alone():
    assert ctx.tail("corto") == "corto"


def test_curly_quote_left_open():
    assert ctx.quote_left_open("“It was in the year 79.")
    assert not ctx.quote_left_open("“It was,” he said.")


def test_straight_quote_left_open_by_parity():
    assert ctx.quote_left_open('"It was in the year 79.')
    assert ctx.quote_left_open('"Yes," he said, "and then.')
    assert not ctx.quote_left_open('"Yes," he said.')
    assert not ctx.quote_left_open("No quotes at all.")


def test_continuation_in_curly_english():
    found = ctx.locate(EN_CURLY, ["“The old volcano, which seemed forever lulled, suddenly awakened.”"])
    assert found["starts_paragraph"] is True
    assert found["quote_continues"] is True
    assert found["before"] == "“It was in the year 79. Vesuvius was then a peaceful mountain."


def test_a_closed_quotation_does_not_continue():
    found = ctx.locate(EN_CURLY, ["Everyone listened."])
    assert found["starts_paragraph"] is True
    assert found["quote_continues"] is False


def test_continuation_in_straight_english():
    assert ctx.locate(EN_STRAIGHT, ['"I will come tomorrow, if the weather improves."'])["quote_continues"] is True
    assert ctx.locate(EN_STRAIGHT, ['"Very well," she answered.'])["quote_continues"] is False


def test_a_paragraph_opening_the_chunk_cannot_tell():
    found = ctx.locate(ES, ["»Fue en el año 79. El Vesubio era entonces una montaña apacible."])
    assert found["starts_paragraph"] is True
    assert found["quote_continues"] is None
    assert found["before"] == ""


def test_a_sentence_inside_a_paragraph_reads_its_own_opening():
    found = ctx.locate(ES, ["Nadie dijo nada durante un rato."])
    assert found["starts_paragraph"] is False
    assert found["quote_continues"] is False
    assert found["before"] == "Todos escuchaban."


def test_a_short_candidate_that_repeats_falls_through_to_the_next():
    found = ctx.locate(ES, ["»El", "Todos escuchaban."])  # "El" is in two paragraphs
    assert found["starts_paragraph"] is True
    assert found["before"].startswith("»El viejo volcán")


def test_a_short_sentence_found_once_is_trusted():
    found = ctx.locate("Le preguntó qué quería.\n\n¿El jamón?\n\nNo contestó.", ["¿El jamón?"])
    assert found["starts_paragraph"] is True
    assert found["before"] == "Le preguntó qué quería."


def test_an_edit_that_split_a_paragraph_is_found_by_its_first_line():
    text = "Los centinelas se inclinaron, pero todo lo que dijeron fue:\n\n—Nuestras órdenes son terminantes."
    after = "Los centinelas se inclinaron, pero todo lo que dijeron fue: \n\n—Nuestras órdenes son terminantes."
    found = ctx.locate(text, [after])
    assert found["starts_paragraph"] is True
    assert found["quote_continues"] is None


def test_nothing_found_is_none():
    assert ctx.locate(ES, ["Esta frase no aparece en el texto."]) is None


def test_edit_context_uses_the_text_not_the_index():
    chunk = {"source_text": EN_CURLY, "translated_text": ES}
    row = {
        "en": "“The old volcano, which seemed forever lulled, suddenly awakened.”",
        "es_before": "El viejo volcán, que parecía adormecido para siempre, despertó de pronto.",
        "es_after": "»El viejo volcán, que parecía adormecido para siempre, despertó de pronto.",
        "es_idx": 99,  # a realign moved it; never read
    }
    assert ctx.edit_context(chunk, row) == {
        "starts_paragraph": True,
        "quote_continues": True,
        "context_before_en": "“It was in the year 79. Vesuvius was then a peaceful mountain.",
        "context_before_es": "»Fue en el año 79. El Vesubio era entonces una montaña apacible.",
        "en_found": True,
        "es_found": True,
    }


def test_edit_context_falls_back_to_the_before_text():
    chunk = {"source_text": EN_CURLY, "translated_text": ES}
    row = {
        "en": "Everyone listened. Nobody said a word for a while.",
        "es_before": "Todos escuchaban. Nadie dijo nada durante un rato.",
        "es_after": "Todos escucharon atentos. Nadie dijo nada en un buen rato.",
    }
    out = ctx.edit_context(chunk, row)
    assert out["es_found"] is True
    assert out["context_before_es"].startswith("»El viejo volcán")


def test_edit_context_reports_what_it_could_not_find():
    chunk = {"source_text": EN_CURLY, "translated_text": ES}
    row = {
        "en": "A sentence that is not in this chunk.",
        "es_before": "Una frase que no está en este fragmento.",
        "es_after": "Otra frase que no está en este fragmento.",
    }
    out = ctx.edit_context(chunk, row)
    assert out["en_found"] is False and out["es_found"] is False
    assert out["starts_paragraph"] is False
    assert out["quote_continues"] is None
    assert out["context_before_en"] == "" and out["context_before_es"] == ""


def test_image_and_caption_paragraphs_are_not_the_paragraph_before():
    text = (
        "»Los estratos rojos anuncian lluvia.\n\n[IMAGE:images/i186.jpg]\n\n[CAPTION] Estratos\n\n"
        "»Finalmente, damos el nombre de «nimbos» a una masa de nubes oscuras."
    )
    found = ctx.locate(text, ["»Finalmente, damos el nombre de «nimbos» a una masa de nubes oscuras."])
    assert found["before"] == "»Los estratos rojos anuncian lluvia."
    assert found["skipped"] == 2


def test_an_unmarked_english_caption_is_skipped_only_where_the_spanish_had_one():
    en = (
        "“They are followed by rain or wind.\n\nStratus\n\n"
        "“Finally, we give the name ‘nimbus’ to a mass of dark clouds."
    )
    es = (
        "»Los estratos rojos anuncian lluvia.\n\n[CAPTION] Estratos\n\n"
        "»Finalmente, damos el nombre de «nimbos» a una masa de nubes oscuras."
    )
    row = {
        "en": "“Finally, we give the name ‘nimbus’ to a mass of dark clouds.",
        "es_before": "—Finalmente, damos el nombre de «nimbos» a una masa de nubes oscuras.",
        "es_after": "»Finalmente, damos el nombre de «nimbos» a una masa de nubes oscuras.",
    }
    out = ctx.edit_context({"source_text": en, "translated_text": es}, row)
    assert out["quote_continues"] is True
    assert out["context_before_en"] == "“They are followed by rain or wind."
    # Without the Spanish caption as evidence, the short line is taken as prose.
    assert ctx.locate(en, [row["en"]])["quote_continues"] is False


def test_a_paragraph_the_spanish_split_off_inside_an_open_quotation_continues():
    en = "“Jacques knocked it down,” continued Uncle Paul, “and crushed it. The worthy man took the entrails for poison."
    es = "—Jacques la tumbó —continuó el tío Paul— y la aplastó.\n\n»El buen hombre tomó por veneno las entrañas."
    row = {
        "en": "The worthy man took the entrails for poison.",
        "es_before": "El buen hombre tomó por veneno las entrañas.",
        "es_after": "»El buen hombre tomó por veneno las entrañas.",
    }
    out = ctx.edit_context({"source_text": en, "translated_text": es}, row)
    assert out["starts_paragraph"] is True
    assert out["quote_continues"] is True


def test_narration_the_spanish_split_off_does_not_continue():
    en = "“Jacques knocked it down,” said Uncle Paul. The worthy man took the entrails for poison."
    es = "—Jacques la tumbó —dijo el tío Paul.\n\nEl buen hombre tomó por veneno las entrañas."
    row = {
        "en": "The worthy man took the entrails for poison.",
        "es_before": "—El buen hombre tomó por veneno las entrañas.",
        "es_after": "El buen hombre tomó por veneno las entrañas.",
    }
    out = ctx.edit_context({"source_text": en, "translated_text": es}, row)
    assert out["starts_paragraph"] is True
    assert out["quote_continues"] is False
