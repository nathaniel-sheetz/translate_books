"""Tests for src.epub_readback and the note checks of src.kdp.verify."""

import json
import zipfile
from pathlib import Path

import pytest

from src import epub_readback
from src.book_metadata import epub_kwargs
from src.epub_builder import build_epub
from src.kdp.verify import verify

NOTES_PAGE = (
    "<html><body><h1>Notas</h1>\n"
    "<h2>Sermón II</h2>\n"
    '<p class="endnote"><a id="en-1" href="chapter_03.xhtml#enref-1">1.</a> Versos de «Praise» '
    "(<em>Alabanza</em>), de Isaac Watts.</p>\n"
    '<p class="endnote"><a id="en-2" href="chapter_03.xhtml#enref-2">2.</a> Mateo 6:5.</p>\n'
    "<h2>Sermón III</h2>\n"
    '<p class="endnote"><a id="en-3" href="chapter_04.xhtml#enref-3">3.</a> 1 Samuel 17:34–37</p>\n'
    "</body></html>")


def _epub(path: Path, pages: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in pages.items():
            zf.writestr(f"EPUB/{name}", text)
    return path


# ── epub_readback ──────────────────────────────────────────────────────────

def test_parse_endnotes_puts_each_note_under_the_heading_before_it():
    notes = epub_readback.parse_endnotes(NOTES_PAGE)
    assert [(n["number"], n["chapter"]) for n in notes] == [
        (1, "Sermón II"), (2, "Sermón II"), (3, "Sermón III")]
    assert notes[0]["text"] == "Versos de «Praise» ( Alabanza ), de Isaac Watts."
    assert epub_readback.count_endnotes_in(NOTES_PAGE) == 3


def test_parse_endnotes_names_the_work_and_the_chapter_in_a_collection():
    page = ('<h2>Sermones cortos</h2><h3>Sermón I</h3>'
            '<p class="endnote"><a id="en-1" href="a.xhtml#enref-1">1.</a> Nota.</p>'
            '<h2>Pensamientos</h2>'
            '<p class="endnote"><a id="en-2" href="b.xhtml#enref-2">2.</a> Otra.</p>')
    assert [n["chapter"] for n in epub_readback.parse_endnotes(page)] == [
        "Sermones cortos · Sermón I", "Pensamientos"]


def test_newest_epub_and_libre_epub_are_kept_apart(tmp_path):
    assert epub_readback.newest_epub(tmp_path) is None and epub_readback.libre_epub(tmp_path) is None
    kindle = _epub(tmp_path / "libro.epub", {"endnotes.xhtml": NOTES_PAGE})
    libre = _epub(tmp_path / "libre" / "libro.epub", {"chapter_01.xhtml": "<p>x</p>"})
    assert epub_readback.newest_epub(tmp_path) == kindle
    assert epub_readback.libre_epub(tmp_path) == libre
    assert epub_readback.count_endnotes(kindle) == 3 and epub_readback.count_endnotes(libre) == 0


@pytest.mark.parametrize("text, expected", [
    ("Al final hay **29 notas del traductor**.", [29]),
    ("Esta edición incluye una nota del traductor y una portada nueva.", [1]),
    ("Esta edición incluye 1 nota del traductor.", [1]),
    ("Una nota lo corrige; otra nota explica la moneda.", []),
    ("Publicado en 1848. Sin notas en esta página.", []),
    ("Las 3 notas más importantes y las otras 21 notas.", [3, 21]),
    ("Copyright is claimed in the 28 editorial notes and in the translator's note.", [28]),
    ("It carries 12 endnotes; see note 4 and the 1848 edition.", [12]),
])
def test_stated_note_counts(text, expected):
    assert epub_readback.stated_note_counts(text)["counts"] == expected


def test_stated_note_counts_sees_none_and_words():
    assert epub_readback.stated_note_counts("Esta edición no lleva notas.")["says_none"]
    stated = epub_readback.stated_note_counts("Hay veintinueve notas y treinta y dos notas.")
    assert stated["counts"] == [] and stated["in_words"] == [
        "veintinueve notas", "treinta y dos notas"]


@pytest.mark.parametrize("text, kind", [
    ("missing line break", "memo"),
    ("citation?", "memo"),
    ("explicar? lo que cubre", "memo"),
    ("Roma", "short"),
    ("manjar francés", "short"),
    ("Sal 116:11", ""),
    ("1 Jn 4:8", ""),
    ("Mateo 6:5.", ""),
    ("Dicho de Edmund Burke (1729–1797) en un discurso en Bristol, en 1780, sin punto final", ""),
])
def test_memo_like(text, kind):
    assert epub_readback.memo_like(text) == kind


@pytest.mark.parametrize("text, kind", [
    # A bare «N notas» below the total may count some of them.
    ("Al final hay 24 notas del traductor; 5 notas ponen al día la ciencia.", "warning"),
    # Above the total it cannot be a subset.
    ("Hay 30 notas al final.", "error"),
    # «del traductor» makes it the total, which must match.
    ("Hay 5 notas del traductor.", "error"),
    ("Copyright is claimed in the 5 editorial notes.", "error"),
    ("Hay 24 notas al final.", "clean"),
])
def test_note_count_issues_tells_a_subset_from_a_wrong_total(text, kind):
    errors, warnings = epub_readback.note_count_issues("page", text, 24)
    if kind == "error":
        assert errors, (errors, warnings)
    elif kind == "warning":
        assert not errors and any("fine if it counts some of them" in w for w in warnings)
    else:
        assert (errors, warnings) == ([], [])


def test_note_count_issues_can_leave_a_silent_text_alone():
    errors, warnings = epub_readback.note_count_issues("page", "Sin cifra.", 4, expect_count=False)
    assert (errors, warnings) == ([], [])
    _, warnings = epub_readback.note_count_issues("text", "Sin cifra.", 4)
    assert any("gives no count" in w for w in warnings)


# ── verify ─────────────────────────────────────────────────────────────────

def _built(tmp_path: Path, *, notice: str = "", about: str = "") -> Path:
    """A real build with one footnote, so the notes page comes from the builder."""
    project = tmp_path / "book"
    (project / "chapters").mkdir(parents=True)
    (project / "chapters" / "chapter_01.txt").write_text(
        "CAPÍTULO I\n\nEl bosque\n\nNace un cervatillo.", encoding="utf-8")
    config = {"spanish_title": "Bambi", "author": "Felix Salten", "translator": "Ana Pérez",
              "front_matter": {"title_page": True}}
    (project / "project.json").write_text(json.dumps(config), encoding="utf-8")
    if notice:
        (project / "edition_notice.json").write_text(
            json.dumps({"heading": "Créditos", "body": notice}), encoding="utf-8")
    if about:
        (project / "about_book.json").write_text(
            json.dumps({"heading": "Sobre este libro", "body": about}), encoding="utf-8")
    build_epub(project_path=project, **epub_kwargs(project))
    return project


def _inject_notes(project: Path, page: str) -> None:
    """Add a notes page to the built EPUB (the builder writes one only from the note store)."""
    epub = epub_readback.newest_epub(project)
    with zipfile.ZipFile(epub) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
        opf_name = next(n for n in members if n.endswith(".opf"))
    opf = members[opf_name].decode("utf-8")
    opf = opf.replace("</manifest>",
                      '<item id="endnotes" href="endnotes.xhtml" media-type="application/xhtml+xml"/>'
                      "</manifest>")
    members[opf_name] = opf.encode("utf-8")
    folder = opf_name.rsplit("/", 1)[0] + "/" if "/" in opf_name else ""
    members[folder + "endnotes.xhtml"] = page.encode("utf-8")
    with zipfile.ZipFile(epub, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def _checks(result: dict) -> dict:
    return {c["check"]: c for c in result["checks"]}


def test_verify_passes_when_the_pages_state_the_real_count(tmp_path):
    project = _built(tmp_path, notice="Esta edición incluye 3 notas del traductor.",
                     about="Al final del libro hay 3 notas del traductor.")
    _inject_notes(project, NOTES_PAGE)
    checks = _checks(verify(project))
    assert checks["note_counts_agree"]["ok"], checks["note_counts_agree"]["detail"]
    assert checks["footnotes_look_like_glosses"]["ok"]
    # Note 3 has no final period, but a bare Scripture reference is a finished note.
    assert "worth a look" not in checks["footnotes_look_like_glosses"]["detail"]


def test_verify_fails_a_page_that_states_a_stale_count(tmp_path):
    project = _built(tmp_path, notice="Esta edición incluye 28 notas del traductor.",
                     about="Al final del libro hay 3 notas del traductor.")
    _inject_notes(project, NOTES_PAGE)
    result = verify(project)
    check = _checks(result)["note_counts_agree"]
    assert not result["ok"] and not check["ok"]
    assert "copyright.xhtml: says 28 notas, the built EPUB has 3" in check["detail"]
    assert "about_book.xhtml" not in check["detail"]


def test_verify_fails_a_count_stated_by_a_book_with_no_notes(tmp_path):
    project = _built(tmp_path, notice="Esta edición incluye una nota del traductor.")
    check = _checks(verify(project))["note_counts_agree"]
    assert not check["ok"] and "says 1 nota, the built EPUB has 0" in check["detail"]


def test_verify_fails_a_memo_and_only_names_a_short_note(tmp_path):
    project = _built(tmp_path)
    page = NOTES_PAGE.replace("Mateo 6:5.", "missing line break").replace(
        "1 Samuel 17:34–37", "Roma")
    _inject_notes(project, page)
    check = _checks(verify(project))["footnotes_look_like_glosses"]
    assert not check["ok"]
    assert "read like memos, not notes: 2 («missing line break»)" in check["detail"]
    assert "worth a look: 3 («Roma»)" in check["detail"]

    _inject_notes(project, NOTES_PAGE.replace("1 Samuel 17:34–37", "Roma"))
    short_only = _checks(verify(project))["footnotes_look_like_glosses"]
    assert short_only["ok"] and "worth a look: 3 («Roma»)" in short_only["detail"]


def test_verify_checks_the_libre_build_without_the_link_rule(tmp_path):
    project = _built(tmp_path)
    missing = verify(project, target="libre")
    assert missing["ok"] is False and "--target libre" in missing["error"]

    kindle = epub_readback.newest_epub(project)
    (project / "libre").mkdir()
    libre = project / "libre" / "bambi.epub"
    with zipfile.ZipFile(kindle) as src, zipfile.ZipFile(libre, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if name.endswith("chapter_01.xhtml"):
                data = data.replace(b"Nace un cervatillo.",
                                    b'Nace un <a href="https://example.org">cervatillo</a>.')
            dst.writestr(name, data)

    result = verify(project, target="libre")
    assert result["target"] == "libre" and result["epub"] == str(libre)
    assert "no_external_links" not in _checks(result)
    assert result["ok"], [c for c in result["checks"] if not c["ok"]]
    # The same file read as a Kindle build is refused for its live link.
    as_kindle = _checks(verify(project, libre, target="kindle"))
    assert not as_kindle["no_external_links"]["ok"]
    assert verify(project, target="print")["ok"] is False
