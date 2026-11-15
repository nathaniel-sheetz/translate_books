"""The kindle / libre build targets: markup in the generated pages, the edition
line, the house license, «Sobre este libro», and the KDP verify net."""

import json
import logging
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.book_metadata as book_metadata
from src.book_metadata import epub_kwargs, load_book_metadata
from src.epub_builder import (
    KINDLE,
    LIBRE,
    PageMarkup,
    _prepare_page_body,
    _render_inline,
    _select_target_blocks,
    build_epub,
    default_epub_path,
    edition_label,
    page_variables,
)
from src.kdp.verify import read_opf, verify

LICENSE = {
    "name": "CC BY-NC-SA 4.0",
    "url": "https://creativecommons.org/licenses/by-nc-sa/4.0/deed.es",
    "targets": ["libre"],
    "statement": "Esta traducción se publica bajo la licencia [{name}]({url}).",
    "scripture_carveout": "Las citas de la NBLA quedan fuera de esta licencia.",
}
PUBLISH = {
    "license": LICENSE,
    "site": {"url": "https://example.org", "feedback_url": "https://forms.example/r/abc"},
    "edition_label": "Versión {version} ({status})",
}
EDITION = {"version": "1.2", "status": "beta"}
NOTE = (
    "Gracias por leer.\n\n"
    "[KINDLE] Deje una reseña, por favor.\n\n"
    "[LIBRE] Descárguelo en [nuestro sitio]({{SITIO}}) y [cuénteme]({{COMENTARIOS}})."
)


@pytest.fixture(autouse=True)
def _house_defaults(tmp_path, monkeypatch):
    """Pin the house publish settings so a user's own file never leaks in."""
    path = tmp_path / "publish_defaults.json"
    path.write_text(json.dumps(PUBLISH), encoding="utf-8")
    monkeypatch.setattr(book_metadata, "PUBLISH_DEFAULTS_FILE", path)
    monkeypatch.setattr(book_metadata, "PUBLISH_DEFAULTS_EXAMPLE_FILE", tmp_path / "none.json")


def _project(tmp_path: Path, **project_json) -> Path:
    project = tmp_path / "book"
    (project / "chapters").mkdir(parents=True)
    (project / "chapters" / "chapter_01.txt").write_text(
        "CAPÍTULO I\n\nEl bosque\n\nNace un cervatillo.", encoding="utf-8")
    (project / "project.json").write_text(json.dumps({
        "spanish_title": "Sermones cortos para niños", "author": "Autor", **project_json,
    }), encoding="utf-8")
    return project


def _read(epub_path: Path) -> dict:
    with zipfile.ZipFile(epub_path) as zf:
        return {Path(n).name: zf.read(n).decode("utf-8") for n in zf.namelist()
                if n.endswith((".xhtml", ".opf"))}


def _opf(files: dict) -> str:
    return next(text for name, text in files.items() if name.endswith(".opf"))


# --- markup units ------------------------------------------------------------

class TestSelectTargetBlocks:
    def test_keeps_untagged_and_own_target(self):
        body = "Común.\n\n[KINDLE] Solo Kindle.\n\n[libre] Solo libre."
        assert _select_target_blocks(body, KINDLE) == "Común.\n\nSolo Kindle."
        assert _select_target_blocks(body, LIBRE) == "Común.\n\nSolo libre."

    def test_tag_on_its_own_line(self):
        assert _select_target_blocks("[LIBRE]\nLínea.", LIBRE) == "Línea."

    def test_tag_mid_block_is_text(self):
        assert _select_target_blocks("Texto [LIBRE] aquí.", KINDLE) == "Texto [LIBRE] aquí."


class TestRenderInlineLinks:
    TEXT = "Vea [el sitio](https://example.org/a?b=1&c=_x_) hoy."

    def test_libre_is_a_live_link(self):
        out = _render_inline(self.TEXT, LIBRE)
        assert out == 'Vea <a href="https://example.org/a?b=1&amp;c=_x_">el sitio</a> hoy.'

    def test_kindle_keeps_the_text_only(self):
        assert _render_inline(self.TEXT, KINDLE) == "Vea el sitio hoy."

    def test_chapters_leave_the_syntax_alone(self):
        assert "[el sitio](https://" in _render_inline(self.TEXT)

    def test_italics_inside_a_label(self):
        assert _render_inline("[_Bambi_](https://x.org)", LIBRE) == \
            '<a href="https://x.org"><em>Bambi</em></a>'


class TestPrepareBody:
    def test_variables_resolve(self):
        markup = PageMarkup(LIBRE, {"SITIO": "https://example.org", "COMENTARIOS": ""})
        assert _prepare_page_body("[aquí]({{SITIO}})", markup) == "[aquí](https://example.org)"

    def test_blank_variable_degrades_to_text_with_a_warning(self, caplog):
        markup = PageMarkup(LIBRE, {"SITIO": "", "COMENTARIOS": ""})
        with caplog.at_level(logging.WARNING, logger="src.epub_builder"):
            assert _prepare_page_body("[cuénteme]({{COMENTARIOS}}).", markup) == "cuénteme."
        assert "COMENTARIOS" in caplog.text

    def test_no_markup_is_untouched(self):
        assert _prepare_page_body("[LIBRE] x {{SITIO}}", None) == "[LIBRE] x {{SITIO}}"


def test_page_variables_feedback_prefill():
    variables = page_variables(PUBLISH, "sermones", EDITION)
    assert variables["SITIO"] == "https://example.org"
    assert variables["COMENTARIOS"] == "https://forms.example/r/abc?libro=sermones&edicion=beta"
    publish = {"site": {"feedback_url": "https://f.example/r?x=1"}}
    assert page_variables(publish, "s", None)["COMENTARIOS"] == "https://f.example/r?x=1&libro=s"


@pytest.mark.parametrize("edition, expected", [
    ({"version": "1.2", "status": "beta"}, "Versión 1.2 (beta)"),
    ({"version": "2.0", "status": ""}, "Versión 2.0"),
    (None, ""),
    ({"version": "", "status": "beta"}, ""),
])
def test_edition_label(edition, expected):
    assert edition_label(edition) == expected


# --- whole builds ------------------------------------------------------------

def test_kindle_default_build(tmp_path):
    """The default target: no live link, no [LIBRE] text, the review ask kept,
    no license, and the file where it always was."""
    project = _project(tmp_path)
    out = build_epub(project_path=project, title="Sermones", author="Autor",
                     translator_note_body=NOTE, publish=PUBLISH,
                     edition_notice_body="Créditos de la edición.", rights="Traducción © 2026.")
    assert out == project / "Sermones.epub"
    files = _read(out)
    note = files["translator_note.xhtml"]
    assert "Deje una reseña" in note and "Descárguelo" not in note
    assert "href=\"http" not in "".join(files.values())
    assert "Licencia" not in files["copyright.xhtml"] and "Licencia" not in _opf(files)


def test_libre_build(tmp_path):
    project = _project(tmp_path)
    (project / "quotations.json").write_text(json.dumps({"rows": [{"ref": "Jn 3:16"}],
                                                         "within_limits": True}), encoding="utf-8")
    out = build_epub(project_path=project, title="Sermones", author="Autor", target=LIBRE,
                     translator_note_body=NOTE, publish=PUBLISH, edition=EDITION,
                     site_slug="sermones", edition_notice_body="Créditos de la edición.",
                     rights="Traducción © 2026.")
    assert out == project / "libre" / "sermones.epub"
    files = _read(out)
    note = files["translator_note.xhtml"]
    assert "Deje una reseña" not in note
    assert '<a href="https://example.org">nuestro sitio</a>' in note
    assert "libro=sermones&amp;edicion=beta" in note
    assert "Versión 1.2 (beta)" in files["titlepage.xhtml"]  # title page forced on
    notice = files["copyright.xhtml"]
    assert notice.index("Créditos de la edición.") < notice.index("CC BY-NC-SA 4.0")
    assert 'href="https://creativecommons.org/licenses/by-nc-sa/4.0/deed.es"' in notice
    assert "Las citas de la NBLA quedan fuera" in notice
    assert "Traducción © 2026. Licencia: CC BY-NC-SA 4.0." in _opf(files)


def test_libre_without_quotations_has_no_carveout_and_makes_its_own_copyright_page(tmp_path):
    project = _project(tmp_path)
    out = build_epub(project_path=project, title="T", author="A", target=LIBRE,
                     publish=PUBLISH, edition=EDITION)
    notice = _read(out)["copyright.xhtml"]
    assert "CC BY-NC-SA 4.0" in notice and "NBLA" not in notice


def test_libre_needs_an_edition(tmp_path):
    with pytest.raises(ValueError, match="edition"):
        build_epub(project_path=_project(tmp_path), title="T", author="A", target=LIBRE)


def test_libre_refuses_quotations_over_the_limit(tmp_path):
    project = _project(tmp_path)
    (project / "quotations.json").write_text(json.dumps({"rows": [{}], "within_limits": False}),
                                             encoding="utf-8")
    with pytest.raises(ValueError, match="permission-to-quote"):
        build_epub(project_path=project, title="T", author="A", target=LIBRE, edition=EDITION)


def test_unknown_target(tmp_path):
    with pytest.raises(ValueError, match="unknown build target"):
        build_epub(project_path=_project(tmp_path), title="T", author="A", target="kobo")


def test_license_listed_for_kindle_is_plain_text_there(tmp_path):
    publish = {**PUBLISH, "license": {**LICENSE, "targets": ["libre", "kindle"]}}
    out = build_epub(project_path=_project(tmp_path), title="T", author="A", publish=publish)
    notice = _read(out)["copyright.xhtml"]
    assert "licencia CC BY-NC-SA 4.0." in notice and "<a " not in notice


def test_edition_on_kindle_title_page_when_set(tmp_path):
    out = build_epub(project_path=_project(tmp_path), title="T", author="A",
                     front_matter={"title_page": True}, edition=EDITION, publish=PUBLISH)
    assert '<p class="titlepage-edition">Versión 1.2 (beta)</p>' in _read(out)["titlepage.xhtml"]


def test_about_book_opens_the_toc_before_chapter_one(tmp_path):
    out = build_epub(project_path=_project(tmp_path), title="T", author="A",
                     about_book_body="Un libro de 1848.")
    files = _read(out)
    spine = read_opf(out)["spine"]
    assert spine.index("nav.xhtml") + 1 == spine.index("about_book.xhtml") \
        == spine.index("chapter_01.xhtml") - 1
    nav = files["nav.xhtml"]
    assert nav.index("about_book.xhtml") < nav.index("chapter_01.xhtml")
    assert "<h1>Sobre este libro</h1>" in files["about_book.xhtml"]


def test_target_only_pages_are_dropped_when_blank_in_that_target(tmp_path):
    out = build_epub(project_path=_project(tmp_path), title="T", author="A",
                     about_book_body="[LIBRE] Solo libre.",
                     translator_note_body="[LIBRE] Solo libre.",
                     edition_notice_body="[LIBRE] Solo libre.")
    files = _read(out)
    for name in ("about_book.xhtml", "translator_note.xhtml", "copyright.xhtml"):
        assert name not in files


def test_default_epub_path(tmp_path):
    assert default_epub_path(tmp_path, "Bambi: Una vida") == tmp_path / "Bambi- Una vida.epub"
    assert default_epub_path(tmp_path, "x", target=LIBRE, site_slug="bambi") == \
        tmp_path / "libre" / "bambi.epub"


# --- metadata ----------------------------------------------------------------

def test_metadata_reads_edition_about_slug_and_publish(tmp_path):
    project = _project(tmp_path, edition={"version": "1.0", "status": "beta"})
    (project / "about_book.json").write_text(json.dumps({"heading": "", "body": "Sobre."}),
                                             encoding="utf-8")
    meta = load_book_metadata(project)
    assert meta["edition"] == {"version": "1.0", "status": "beta"}
    assert meta["about_book_body"] == "Sobre."
    assert meta["site_slug"] == "sermones-cortos-para-ninos"
    assert meta["publish"]["license"]["name"] == "CC BY-NC-SA 4.0"
    assert meta["publish"]["site"]["url"] == "https://example.org"


def test_book_license_overrides_and_opts_out(tmp_path):
    own = load_book_metadata(_project(tmp_path, license={"name": "CC BY 4.0", "url": ""}))
    assert own["publish"]["license"]["name"] == "CC BY 4.0"
    assert own["publish"]["license"]["targets"] == ["libre"]  # the house field stays
    out = load_book_metadata(_project(tmp_path / "b", license=None))
    assert out["publish"]["license"] is None


def test_site_slug_from_project_json(tmp_path):
    assert load_book_metadata(_project(tmp_path, site_slug="sermones"))["site_slug"] == "sermones"


def test_epub_kwargs_build_libre_end_to_end(tmp_path):
    project = _project(tmp_path, edition={"version": "1.0", "status": "beta"},
                       front_matter={"title_page": True})
    (project / "translator_note.json").write_text(json.dumps({"heading": "", "body": NOTE}),
                                                  encoding="utf-8")
    out = build_epub(project_path=project, target=LIBRE, **epub_kwargs(project))
    assert out == project / "libre" / "sermones-cortos-para-ninos.epub"
    assert "CC BY-NC-SA 4.0" in _read(out)["copyright.xhtml"]


# --- the KDP verify net ------------------------------------------------------

def test_verify_passes_kindle_and_rejects_libre(tmp_path):
    project = _project(tmp_path, edition={"version": "1.0", "status": "beta"},
                       front_matter={"title_page": True})
    (project / "translator_note.json").write_text(json.dumps({"heading": "", "body": NOTE}),
                                                  encoding="utf-8")
    kindle = build_epub(project_path=project, **epub_kwargs(project))
    checks = {c["check"]: c for c in verify(project, kindle)["checks"]}
    assert checks["no_external_links"]["ok"] and checks["no_leftover_markup"]["ok"]

    libre = build_epub(project_path=project, target=LIBRE, **epub_kwargs(project))
    checks = {c["check"]: c for c in verify(project, libre)["checks"]}
    assert not checks["no_external_links"]["ok"]
    assert "translator_note.xhtml" in checks["no_external_links"]["detail"]


def test_verify_catches_leftover_markup(tmp_path):
    project = _project(tmp_path)
    (project / "chapters" / "chapter_01.txt").write_text(
        "CAPÍTULO I\n\n[LIBRE] un párrafo mal puesto.", encoding="utf-8")
    out = build_epub(project_path=project, **epub_kwargs(project))
    checks = {c["check"]: c for c in verify(project, out)["checks"]}
    assert not checks["no_leftover_markup"]["ok"]
    assert "chapter_01.xhtml" in checks["no_leftover_markup"]["detail"]
