"""Tests for src.edition: the edition block's rules and what applying it writes."""

from datetime import date

import pytest

from src import edition

TODAY = date(2026, 11, 15)
DEFAULTS = {
    "translator": "Ana Pérez", "publisher": "Editorial Ejemplo",
    "rights": "Traducción © {year} {translator}. La obra original es de dominio público.",
    "edition_notice": {"heading": "Créditos"},
}
BLOCK = {
    "title": "Bambi", "subtitle": "Una vida en el bosque", "author": "Felix Salten",
    "translator": "Ana Pérez", "source_title": "Bambi: Eine Lebensgeschichte aus dem Walde",
    "original_year": 1923, "subjects": ["Deer -- Fiction"], "version": "1.0", "status": "beta",
    "notice": {"heading": "Créditos",
               "body": "Publicado originalmente en 1923.\n\nEsta edición incluye 3 notas del traductor."},
}


def _check(block=None, **kwargs):
    texts = {"edition": BLOCK if block is None else block}
    texts.update(kwargs.pop("texts", {}))
    return edition.check(texts, today=TODAY, **kwargs)


# ── markdown_plain ─────────────────────────────────────────────────────────

def test_markdown_plain_keeps_the_words_and_one_paragraph_per_line():
    text = ("**¿Por qué el río nunca se acaba?**\n\nCada tarde, la abuela _Inés_ cuenta algo\n"
            "verdadero. Más en [el sitio](https://example.org).\n\n- Publicado en 1890.")
    assert edition.markdown_plain(text) == (
        "¿Por qué el río nunca se acaba?\n"
        "Cada tarde, la abuela Inés cuenta algo verdadero. Más en el sitio.\n"
        "Publicado en 1890.")


def test_markdown_plain_leaves_an_underscore_inside_a_word():
    assert edition.markdown_plain("el archivo libro_toml") == "el archivo libro_toml"


# ── check ──────────────────────────────────────────────────────────────────

def test_check_accepts_a_good_block():
    assert _check(endnotes=3) == ([], [])


def test_check_says_nothing_without_a_block_or_description():
    assert edition.check({"forms": ["II"]}) == ([], [])


@pytest.mark.parametrize("key", ["title", "author", "translator"])
def test_check_refuses_a_blank_required_field(key):
    errors, _ = _check({**BLOCK, key: " "})
    assert any(f"edition.{key}: blank" in e for e in errors)


def test_check_judges_a_missing_field_by_project_json():
    block = {k: v for k, v in BLOCK.items() if k != "translator"}
    errors, _ = _check(block)
    assert any("edition.translator: missing" in e for e in errors)
    assert _check(block, project={"translator": "Ana Pérez"})[0] == []
    # A book whose only title is project.json "title" still has one.
    untitled = {k: v for k, v in BLOCK.items() if k != "title"}
    assert _check(untitled, project={"title": "Bambi"})[0] == []


def test_check_refuses_the_gutenberg_name_anywhere_in_the_book():
    errors, _ = _check({**BLOCK, "notice": {"body": "Texto fuente: Project Gutenberg."}})
    assert any("edition.notice.body: names Project Gutenberg" in e for e in errors)
    errors, _ = _check({**BLOCK, "subtitle": "Edición de Project Gutenberg"})
    assert any("edition.subtitle" in e for e in errors)
    errors, _ = edition.check({"description": "Tomado del Proyecto Gutenberg."})
    assert any(e.startswith("description: names Project Gutenberg") for e in errors)


def test_check_warns_on_what_a_store_description_may_not_carry():
    _, warnings = edition.check({"description": "Visite www.ejemplo.com hoy."})
    assert any("a URL" in w for w in warnings)
    assert edition.check({"description": "Publicado en 1890 y leído medio siglo."}) == ([], [])


def test_check_requires_the_scripture_notice_word_for_word():
    notice = "Escrituras tomadas de la NBLA, Copyright © 2005 por The Lockman Foundation."
    errors, _ = _check(scripture_notice=notice)
    assert any("notice must be on this page" in e for e in errors)
    with_it = {**BLOCK, "notice": {"body": BLOCK["notice"]["body"] + "\n\n" + notice}}
    assert _check(with_it, scripture_notice=notice, endnotes=3)[0] == []


def test_check_compares_the_notices_count_with_the_built_epub():
    errors, _ = _check(endnotes=4)
    assert any("edition.notice.body: says 3 notas, the built EPUB has 4" in e for e in errors)
    one = {**BLOCK, "notice": {"body": "Esta edición incluye una nota del traductor."}}
    assert any("says 1 nota," in e for e in _check(one, endnotes=2)[0])
    assert _check(one, endnotes=1)[0] == []
    # Nothing built yet: a warning, since nothing can be compared.
    errors, warnings = _check(endnotes=None)
    assert not errors and any("no built EPUB" in w for w in warnings)


@pytest.mark.parametrize("body, needle", [
    ("**Créditos**\n\nTexto.", "bold"),
    ("Cambios:\n- uno\n- dos", "lists"),
    ("Texto [KINDLE] en medio.", "start of a paragraph"),
    ("[LIBRE] Ver [aquí](www.x.org).", "not https://"),
    ("[LIBRE] Ver {{ SITIO }}.", "unknown variable"),
])
def test_check_refuses_markup_the_copyright_page_cannot_render(body, needle):
    # The copyright page goes through the same renderer as «Sobre este libro».
    errors, _ = _check({**BLOCK, "notice": {"body": body}})
    assert any(e.startswith("edition.notice.body:") and needle in e for e in errors), errors


def test_check_reports_a_count_written_in_words():
    words = {**BLOCK, "notice": {"body": "Al final hay veintinueve notas del traductor."}}
    errors, warnings = _check(words, endnotes=29)
    assert not errors and any("in words" in w for w in warnings)


def test_check_types():
    errors, _ = _check({**BLOCK, "original_year": "1923", "subjects": "Deer",
                        "publication_date": "19/10/2026", "version": 1})
    assert any("original_year: must be a whole number" in e for e in errors)
    assert any("subjects: must be a list" in e for e in errors)
    assert any("publication_date" in e and "YYYY-MM-DD" in e for e in errors)
    assert any("version: must be a string" in e for e in errors)
    assert any("not a plausible year" in e for e in _check({**BLOCK, "original_year": 3024})[0])
    assert edition.check({"edition": "Bambi"})[0] == ["edition: must be an object"]


def test_check_warns_when_no_version_is_on_record():
    block = {k: v for k, v in BLOCK.items() if k not in ("version", "status")}
    _, warnings = _check(block, endnotes=3)
    assert any("edition.version" in w and "libre build refuses" in w for w in warnings)
    _, warnings = _check(block, project={"edition": {"version": "1.0"}}, endnotes=3)
    assert not any("edition.version" in w for w in warnings)


def test_check_warns_on_an_unknown_key():
    _, warnings = _check({**BLOCK, "isbn": "x"}, endnotes=3)
    assert any("edition.isbn: not a known key" in w for w in warnings)


# ── planned ────────────────────────────────────────────────────────────────

def test_planned_writes_the_block_and_the_house_fallbacks():
    plan = edition.planned(
        {"edition": BLOCK, "description": "**Un gancho.**\n\nUn párrafo."}, {}, DEFAULTS, today=TODAY)
    fields = plan["project_json"]
    assert fields["spanish_title"] == "Bambi" and fields["subtitle"] == "Una vida en el bosque"
    assert fields["translator"] == "Ana Pérez" and fields["publisher"] == "Editorial Ejemplo"
    assert fields["rights"] == "Traducción © 2026 Ana Pérez. La obra original es de dominio público."
    assert fields["publication_date"] == "2026-11-15"
    assert fields["original_year"] == 1923 and fields["subjects"] == ["Deer -- Fiction"]
    assert fields["edition"] == {"version": "1.0", "status": "beta"}
    assert fields["front_matter"] == {"title_page": True}
    assert fields["description"] == "Un gancho.\nUn párrafo."
    assert plan["remove"] == []
    assert plan["edition_notice"] == {"heading": "Créditos", "body": BLOCK["notice"]["body"]}


def test_planned_never_replaces_what_is_on_disk_with_a_fallback():
    project = {"publisher": "Otra Casa", "rights": "© 2025 Otra Casa.",
               "publication_date": "2025-01-01", "translator": "A. P.",
               "front_matter": {"translator_label": "Versión de", "title_page": False},
               "edition": {"version": "0.9", "status": "beta"}}
    block = {"title": "Bambi", "status": "revisada"}
    plan = edition.planned({"edition": block}, project, DEFAULTS, today=TODAY)
    assert plan["project_json"] == {
        "spanish_title": "Bambi",
        "edition": {"version": "0.9", "status": "revisada"},
    }
    assert "front_matter" not in plan["project_json"]
    assert plan["edition_notice"] is None


def test_planned_keeps_a_title_page_turned_off_and_honours_an_explicit_one():
    project = {"front_matter": {"title_page": False}}
    plan = edition.planned({"edition": {"status": "revisada"}}, project, {}, today=TODAY)
    assert "front_matter" not in plan["project_json"]
    plan = edition.planned({"edition": {"title_page": True}}, project, {}, today=TODAY)
    assert plan["project_json"]["front_matter"] == {"title_page": True}


def test_planned_removes_what_is_blanked_and_keeps_what_is_left_out():
    project = {"subtitle": "Viejo", "source_title": "Old", "subjects": ["x"]}
    plan = edition.planned({"edition": {"subtitle": "", "subjects": []}}, project, {}, today=TODAY)
    assert sorted(plan["remove"]) == ["subjects", "subtitle"]
    assert "source_title" not in plan["project_json"]


def test_planned_with_only_a_description_touches_nothing_else():
    plan = edition.planned({"description": "Un párrafo."}, {"title": "Old"}, DEFAULTS, today=TODAY)
    assert plan == {"project_json": {"description": "Un párrafo."}, "remove": [],
                    "edition_notice": None}


def test_current_reads_the_edition_back_in_the_blocks_shape():
    project = {"title": "Old", "author": "Felix Salten", "original_year": 1923,
               "edition": {"version": "1.0", "status": "beta"}}
    now = edition.current(project, {"heading": "Créditos", "body": "Texto."})
    assert now["title"] == "Old" and now["author"] == "Felix Salten" and now["subtitle"] == ""
    assert now["version"] == "1.0" and now["notice"] == {"heading": "Créditos", "body": "Texto."}
