"""Tests for src.site_texts and the book_metadata CLI: check, apply, site, status."""

import json
import zipfile
from pathlib import Path

import pytest

from src import site_texts

GOOD = {
    "forms": ["II"],
    "adaptations": ("- Esta edición traduce, sin abreviar, la edición inglesa de 1890.\n"
                    "- Se corrigen sin marcarlos algunos descuidos evidentes.\n"
                    "- Al final del libro hay **3 notas del traductor**."),
    "parent_notes": "Es un libro sereno.\n\n**La muerte del abuelo (cap. 31).** Léalo antes.",
    "about_book": {
        "heading": "Sobre este libro",
        "body": ("Esta edición traduce, sin abreviar, la edición inglesa de 1890.\n\n"
                 "Al final del libro hay 3 notas del traductor.\n\n"
                 "[LIBRE] En [nuestro sitio]({{SITIO}}) encontrará una guía para los padres."),
    },
    "description": "**¿Por qué el río nunca se acaba?**\n\nCada tarde, la abuela Inés cuenta algo verdadero.",
    "resumen": 'La abuela Inés cuenta historias "verdaderas" del huerto.',
}


def _project(tmp_path: Path, **extra) -> Path:
    project_dir = tmp_path / "projects" / "mi-libro"
    project_dir.mkdir(parents=True)
    config = {"title": "My Book", "spanish_title": "Mi libro", "author": "Ana Autora",
              "site_slug": "mi-libro", **extra}
    (project_dir / "project.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return project_dir


def _epub(project_dir: Path, notes: int, *, about: bool = False) -> Path:
    path = project_dir / "mi-libro.epub"
    body = "".join(f'<p><a id="en-{n}"></a>{n}. Nota.</p>' for n in range(1, notes + 1))
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("EPUB/chapter_01.xhtml", "<p>Texto.</p>")
        if notes:
            zf.writestr("EPUB/endnotes.xhtml", f"<html><body>{body}</body></html>")
        if about:
            zf.writestr("EPUB/about_book.xhtml", "<p>Sobre este libro.</p>")
    return path


def _site(tmp_path: Path, *, with_book: bool = True) -> Path:
    site_dir = tmp_path / "sitio"
    book_dir = site_dir / "libros" / "mi-libro"
    book_dir.mkdir(parents=True)
    if with_book:
        (book_dir / "libro.toml").write_text(
            'titulo = "Mi libro"\nresumen = "Viejo resumen."  # una frase\nedicion = "beta"\n',
            encoding="utf-8")
    return site_dir


# ── check ───────────────────────────────────────────────────────────────────

def test_check_accepts_a_good_draft():
    report = site_texts.check(GOOD, endnotes=3)
    assert report == {"errors": [], "warnings": []}


def test_check_refuses_an_empty_draft_and_warns_on_unknown_keys():
    assert site_texts.check({}, endnotes=None)["errors"]
    report = site_texts.check({"forms": ["II"], "parents": "x"}, endnotes=None)
    assert not report["errors"]
    assert any("parents" in w for w in report["warnings"])


def test_check_refuses_unknown_forms():
    report = site_texts.check({"forms": ["II", "VII"]}, endnotes=None)
    assert any("VII" in e for e in report["errors"])


@pytest.mark.parametrize("body, needle", [
    ("Hay **3 notas** al final.", "bold"),
    ("Cambios:\n- uno\n- dos", "lists"),
    ("## Para los padres\n\nTexto.", "lists"),
    ("Vea el sitio. [LIBRE] Solo aquí.", "start of a paragraph"),
    ("Visite {{TIENDA}}.", "unknown variable"),
    ("Texto tomado de Project Gutenberg.", "Gutenberg"),
])
def test_check_refuses_markup_the_epub_page_cannot_render(body, needle):
    report = site_texts.check({"about_book": {"body": body}}, endnotes=None)
    assert any(needle in e for e in report["errors"]), report


def test_check_warns_on_a_link_outside_a_libre_paragraph():
    body = "En [nuestro sitio]({{SITIO}}) hay una guía."
    report = site_texts.check({"about_book": {"body": body}}, endnotes=None)
    assert not report["errors"]
    assert any("[LIBRE]" in w for w in report["warnings"])


def test_check_compares_note_counts_with_the_built_epub():
    wrong = site_texts.check(GOOD, endnotes=5)
    assert any("adaptations" in e and "5" in e for e in wrong["errors"])
    assert any("about_book.body" in e for e in wrong["errors"])

    none = {"adaptations": "- Esta edición no lleva notas."}
    assert not site_texts.check(none, endnotes=0)["errors"]
    assert site_texts.check(none, endnotes=4)["errors"]

    silent = site_texts.check({"adaptations": "- Sin abreviar."}, endnotes=4)
    assert not silent["errors"] and any("gives no count" in w for w in silent["warnings"])

    unbuilt = site_texts.check(GOOD, endnotes=None)
    assert not unbuilt["errors"] and any("no built EPUB" in w for w in unbuilt["warnings"])


def test_check_keeps_epub_markup_out_of_the_site_texts():
    report = site_texts.check({"parent_notes": "[LIBRE] Vea {{SITIO}}."}, endnotes=None)
    assert any("parent_notes" in e for e in report["errors"])


def test_check_resumen_is_one_line_and_description_opens_with_a_hook():
    report = site_texts.check({"resumen": "Una.\nDos.", "description": "Sin gancho."}, endnotes=None)
    assert any("resumen" in e for e in report["errors"])
    assert any("bold hook" in w for w in report["warnings"])


# ── apply ───────────────────────────────────────────────────────────────────

def test_apply_dry_run_reports_and_writes_nothing(tmp_path):
    project_dir = _project(tmp_path, adaptations="- Texto anterior.")
    before = (project_dir / "project.json").read_text(encoding="utf-8")

    result = site_texts.apply(project_dir, GOOD, endnotes=3, dry_run=True)

    assert result["applied"] is False and result["dry_run"] is True
    actions = {(c["file"], c["field"]): c["action"] for c in result["changes"]}
    assert actions[("project.json", "adaptations")] == "replaced"
    assert actions[("project.json", "forms")] == "set"
    assert actions[("about_book.json", "body")] == "set"
    assert (project_dir / "project.json").read_text(encoding="utf-8") == before
    assert not (project_dir / "about_book.json").exists()


def test_apply_writes_both_files_and_keeps_other_keys(tmp_path):
    project_dir = _project(tmp_path)

    result = site_texts.apply(project_dir, GOOD, endnotes=3)

    assert result["applied"] is True and result["site_only"] == ["description", "resumen"]
    project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert project["forms"] == ["II"]
    assert project["adaptations"] == GOOD["adaptations"]
    assert project["parent_notes"] == GOOD["parent_notes"]
    assert project["spanish_title"] == "Mi libro"
    assert "description" not in project and "resumen" not in project
    about = json.loads((project_dir / "about_book.json").read_text(encoding="utf-8"))
    assert about == GOOD["about_book"]

    again = site_texts.apply(project_dir, GOOD, endnotes=3)
    assert {c["action"] for c in again["changes"]} == {"unchanged"}


def test_apply_leaves_absent_keys_alone_and_removes_blank_ones(tmp_path):
    project_dir = _project(tmp_path, forms=["I"], adaptations="- Se queda.", parent_notes="Se va.")

    site_texts.apply(project_dir, {"parent_notes": "  "}, endnotes=None)

    project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert project["forms"] == ["I"] and project["adaptations"] == "- Se queda."
    assert "parent_notes" not in project
    assert not (project_dir / "about_book.json").exists()


def test_apply_refuses_while_check_has_errors(tmp_path):
    project_dir = _project(tmp_path)

    result = site_texts.apply(project_dir, GOOD, endnotes=9)

    assert result["applied"] is False and result["errors"] and result["changes"] == []
    assert "forms" not in json.loads((project_dir / "project.json").read_text(encoding="utf-8"))


# ── site ────────────────────────────────────────────────────────────────────

def test_site_writes_the_book_folder(tmp_path):
    project_dir = _project(tmp_path)
    site_dir = _site(tmp_path)
    site_texts.apply(project_dir, GOOD, endnotes=3)
    book_dir = site_dir / "libros" / "mi-libro"

    dry = site_texts.site(project_dir, GOOD, site_dir, dry_run=True)
    assert dry["written"] is False
    assert not (book_dir / "descripcion.md").exists()

    result = site_texts.site(project_dir, GOOD, site_dir)

    actions = {f["file"]: f["action"] for f in result["files"]}
    assert actions == {"adaptaciones.md": "created", "padres.md": "created",
                       "descripcion.md": "created", "libro.toml": "updated"}
    assert (book_dir / "descripcion.md").read_text(encoding="utf-8") == GOOD["description"] + "\n"
    assert (book_dir / "padres.md").read_text(encoding="utf-8") == GOOD["parent_notes"] + "\n"

    import tomllib
    toml_text = (book_dir / "libro.toml").read_text(encoding="utf-8")
    libro = tomllib.loads(toml_text)
    assert libro["resumen"] == GOOD["resumen"]
    assert libro["titulo"] == "Mi libro" and libro["edicion"] == "beta"
    assert "# una frase" in toml_text

    again = site_texts.site(project_dir, GOOD, site_dir)
    assert {f["action"] for f in again["files"]} == {"unchanged"}


def test_site_stops_when_the_book_has_no_folder(tmp_path):
    project_dir = _project(tmp_path)
    site_dir = _site(tmp_path, with_book=False)

    result = site_texts.site(project_dir, GOOD, site_dir)

    assert result["written"] is False and "libro.toml" in result["error"]


def test_site_skips_a_resumen_it_cannot_place(tmp_path):
    project_dir = _project(tmp_path)
    site_dir = _site(tmp_path)
    toml_path = site_dir / "libros" / "mi-libro" / "libro.toml"
    toml_path.write_text('titulo = "Mi libro"\n', encoding="utf-8")

    result = site_texts.site(project_dir, {"resumen": "Una frase."}, site_dir)

    row = next(f for f in result["files"] if f["file"] == "libro.toml")
    assert row["action"] == "skipped"
    assert toml_path.read_text(encoding="utf-8") == 'titulo = "Mi libro"\n'


# ── status and the CLI ──────────────────────────────────────────────────────

def test_read_epub_counts_notes_and_finds_the_about_page(tmp_path):
    project_dir = _project(tmp_path)
    assert site_texts.read_epub(_epub(project_dir, 7, about=True)) == {
        "endnotes": 7, "has_about_book": True}
    assert site_texts.read_epub(_epub(project_dir, 0)) == {"endnotes": 0, "has_about_book": False}


def test_status_names_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(site_texts, "GUIDE_FILE", tmp_path / "no_private_guide.md")
    monkeypatch.setattr(site_texts, "site_repo_dir", lambda: "")
    project_dir = _project(tmp_path, forms=["II"])

    bare = site_texts.status(project_dir)
    assert bare["guide"]["kind"] == "example" and Path(bare["guide"]["path"]).exists()
    assert bare["epub"] is None and bare["reviews"] == []
    assert len(bare["needs"]) == 2
    assert bare["current"]["forms"] == ["II"]
    assert bare["site"]["site_slug"] == "mi-libro" and bare["site"]["repo_dir"] is None

    (project_dir / "family-review.md").write_text("# Review", encoding="utf-8")
    _epub(project_dir, 4)
    ready = site_texts.status(project_dir)
    assert [r["file"] for r in ready["reviews"]] == ["family-review.md"]
    assert ready["epub"]["endnotes"] == 4 and ready["needs"] == []


def test_cli_check_and_apply(tmp_path, capsys):
    from scripts import book_metadata as cli

    project_dir = _project(tmp_path)
    _epub(project_dir, 3)
    texts = site_texts.work_dir(project_dir) / site_texts.TEXTS_FILE
    texts.parent.mkdir(parents=True)
    texts.write_text(json.dumps(GOOD, ensure_ascii=False), encoding="utf-8")

    assert cli.main(["check", "--project", str(project_dir)]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert checked["ok"] is True and checked["endnotes"] == 3

    assert cli.main(["apply", "--project", str(project_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is True
    assert (project_dir / "about_book.json").exists()
    assert (site_texts.work_dir(project_dir) / "last_output.json").exists()

    texts.write_text(json.dumps({**GOOD, "forms": ["IX"]}), encoding="utf-8")
    assert cli.main(["check", "--project", str(project_dir)]) == 1
