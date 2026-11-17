"""Tests for what src.site_texts gained with the edition: applying it, chapter
references, metadata_draft.md kept in step, the fuller status, and the new CLI commands."""

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from src import site_texts

from tests.test_site_texts import GOOD, _epub, _project, _site

DEFAULTS = {"translator": "Ana Pérez", "publisher": "Editorial Ejemplo",
            "rights": "Traducción © {year} {translator}. La obra original es de dominio público.",
            "edition_notice": {"heading": "Créditos"}}
EDITION = {
    "title": "Mi libro", "subtitle": "Cuentos del huerto", "author": "Ana Autora",
    "translator": "Ana Pérez", "original_year": 1890, "version": "1.0", "status": "beta",
    "notice": {"body": "Publicado originalmente en 1890.\n\nEsta edición incluye 3 notas del traductor."},
}


def _context(**over):
    return {"defaults": DEFAULTS, "endnotes": 3, "scripture_notice": "",
            "last_chapter": 40, "draft": {}, **over}


def test_a_footnote_added_after_the_build_makes_the_epub_stale(tmp_path):
    import os

    project_dir = _project(tmp_path)
    epub = _epub(project_dir, 3)
    os.utime(epub, (1_000_000, 1_000_000))
    assert "annotations.jsonl" not in site_texts._newer_than(epub, project_dir)
    (project_dir / "annotations.jsonl").write_text('{"type": "footnote"}\n', encoding="utf-8")
    assert "annotations.jsonl" in site_texts._newer_than(epub, project_dir)


# ── the edition ─────────────────────────────────────────────────────────────

def test_apply_writes_the_edition_to_the_book(tmp_path):
    project_dir = _project(tmp_path, publisher="Otra Casa")
    texts = {**GOOD, "edition": EDITION}

    dry = site_texts.apply(project_dir, texts, dry_run=True, context=_context(),
                           today=date(2026, 11, 15))
    actions = {(c["file"], c["field"]): c["action"] for c in dry["changes"]}
    assert actions[("project.json", "subtitle")] == "set"
    assert actions[("project.json", "spanish_title")] == "unchanged"
    assert actions[("edition_notice.json", "body")] == "set"
    assert ("project.json", "publisher") not in actions  # on disk already: no fall-back
    assert not (project_dir / "edition_notice.json").exists()

    result = site_texts.apply(project_dir, texts, context=_context(), today=date(2026, 11, 15))
    assert result["applied"] and "rebuild" in result["next"]
    project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    assert project["subtitle"] == "Cuentos del huerto" and project["publisher"] == "Otra Casa"
    assert project["translator"] == "Ana Pérez" and project["original_year"] == 1890
    assert project["rights"].startswith("Traducción © 2026 Ana Pérez.")
    assert project["publication_date"] == "2026-11-15"
    assert project["edition"] == {"version": "1.0", "status": "beta"}
    assert project["front_matter"] == {"title_page": True}
    assert project["site_slug"] == "mi-libro"  # a key apply does not own
    notice = json.loads((project_dir / "edition_notice.json").read_text(encoding="utf-8"))
    assert notice == {"heading": "Créditos", "body": EDITION["notice"]["body"]}

    again = site_texts.apply(project_dir, texts, context=_context(), today=date(2027, 1, 1))
    assert {c["action"] for c in again["changes"]} == {"unchanged"}


def test_apply_refuses_an_edition_whose_notice_miscounts(tmp_path):
    project_dir = _project(tmp_path)
    result = site_texts.apply(project_dir, {"edition": EDITION}, context=_context(endnotes=4))
    assert result["applied"] is False
    assert any("edition.notice.body: says 3 notas" in e for e in result["errors"])
    assert not (project_dir / "edition_notice.json").exists()


def test_apply_judges_required_fields_by_the_project_on_disk(tmp_path):
    project_dir = _project(tmp_path)  # has a title and an author, no translator
    stale = _context(project={"translator": "Someone the caller remembered"})
    refused = site_texts.apply(project_dir, {"edition": {"version": "1.0"}}, context=stale)
    assert any("edition.translator: missing" in e for e in refused["errors"])


# ── chapter references ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text, cited", [
    ("**La muerte del abuelo (cap. 31).** Léalo antes.", [31]),
    ("Una tormenta (cap. 12) y la matanza (caps. 19 y 22).", [12, 19, 22]),
    ("En los sermones V y XIV recuerda la muerte; véase el sermón X.", [5, 14, 10]),
    ("El capítulo 3 y la lección 12.", [3, 12]),
    ("Es capaz de asustar; son 24 notas y 15 sermones.", []),
])
def test_chapters_cited(text, cited):
    assert site_texts.chapters_cited(text) == cited


def test_check_refuses_a_chapter_past_the_last_one():
    texts = {"parent_notes": "**La muerte del abuelo (cap. 31).** Léalo antes.",
             "about_book": {"body": "El capítulo 41 es triste."}}
    report = site_texts.check(texts, endnotes=0, context={"last_chapter": 40})
    assert not any("parent_notes" in e for e in report["errors"])
    assert any("about_book.body: cites chapter 41" in e and "40" in e for e in report["errors"])
    # A book whose manifest numbers nothing is not checked.
    assert not site_texts.check(texts, endnotes=0, context={"last_chapter": None})["errors"]


def test_chapter_map_gives_the_number_the_epub_shows():
    project = {"chapter_manifest": [
        {"id": "chapter_01", "kind": "front_matter", "label": "Preface"},
        {"id": "chapter_02", "kind": "chapter", "number": 1},
        {"id": "chapter_03", "kind": "chapter", "number": 2}]}
    assert site_texts.chapter_map(project) == {
        "files": {"chapter_01": "front_matter: Preface", "chapter_02": "1", "chapter_03": "2"},
        "last_number": 2}
    assert site_texts.chapter_map({})["last_number"] is None


# ── metadata_draft.md ───────────────────────────────────────────────────────

def test_render_draft_starts_a_draft_with_one_marked_block_per_text(tmp_path):
    project_dir = _project(tmp_path)
    texts = {**GOOD, "edition": EDITION}

    result = site_texts.render_draft(project_dir, texts, title="Mi libro", today="2026-11-15")

    assert result["action"] == "created" and result["missing_markers"] == []
    draft = (project_dir / "metadata_draft.md").read_text(encoding="utf-8")
    assert draft.startswith("# Metadata draft: Mi libro")
    assert "## What the record shows" in draft and "## Decisions" in draft
    blocks = site_texts.read_draft(project_dir / "metadata_draft.md")
    assert set(blocks) == {"edition", "description", "resumen", "forms", "adaptations",
                           "parent_notes", "about_book"}
    assert blocks["adaptations"] == GOOD["adaptations"] and blocks["forms"] == "II"
    assert blocks["about_book"] == GOOD["about_book"]["body"]
    assert json.loads(blocks["edition"]) == EDITION


def test_render_draft_rewrites_only_the_marked_blocks(tmp_path):
    project_dir = _project(tmp_path)
    draft = project_dir / "metadata_draft.md"
    draft.write_text(
        "# Draft\n\nMy header, with the evidence.\n\n## Adaptations\n\n"
        "<!-- text: adaptations -->\n```markdown\n- Texto viejo.\n```\n\n"
        "Alternative hooks:\n\n```markdown\n**Otro gancho**\n```\n\n"
        "## Parent notes\n\n```markdown\nUn bloque sin marca.\n```\n\n## Decisions\n\nMine.\n",
        encoding="utf-8")

    result = site_texts.render_draft(project_dir, GOOD)

    assert result["action"] == "updated" and result["blocks"] == {"adaptations": "updated"}
    assert "parent_notes" in result["missing_markers"] and "text: KEY" in result["note"]
    text = draft.read_text(encoding="utf-8")
    assert GOOD["adaptations"] in text and "- Texto viejo." not in text
    assert "My header, with the evidence." in text and "**Otro gancho**" in text
    assert "Un bloque sin marca." in text and text.rstrip().endswith("Mine.")
    assert site_texts.render_draft(project_dir, GOOD)["action"] == "unchanged"


def test_pull_draft_takes_the_users_wording_verbatim(tmp_path):
    project_dir = _project(tmp_path)
    site_texts.render_draft(project_dir, GOOD, title="Mi libro", today="2026-11-15")
    draft = project_dir / "metadata_draft.md"
    edited = draft.read_text(encoding="utf-8").replace(
        "Es un libro sereno.", "Es un libro sereno,  casi todo huerto.").replace(
        "<!-- text: forms -->\n```text\nII\n```", "<!-- text: forms -->\n```text\nI, II\n```")
    draft.write_text(edited, encoding="utf-8")

    pulled = site_texts.pull_draft(project_dir, GOOD)

    assert sorted(pulled["changed"]) == ["forms", "parent_notes"] and pulled["errors"] == []
    assert pulled["texts"]["parent_notes"].startswith("Es un libro sereno,  casi todo huerto.")
    assert pulled["texts"]["forms"] == ["I", "II"]
    assert pulled["texts"]["about_book"] == GOOD["about_book"]  # heading kept, body unchanged


def test_pull_draft_reports_an_edition_block_that_is_not_json(tmp_path):
    project_dir = _project(tmp_path)
    (project_dir / "metadata_draft.md").write_text(
        "<!-- text: edition -->\n```json\n{\"title\": \"Mi libro\",}\n```\n", encoding="utf-8")
    pulled = site_texts.pull_draft(project_dir, {})
    assert pulled["changed"] == [] and "edition" in pulled["errors"][0]


def test_check_warns_when_the_draft_and_the_texts_differ(tmp_path):
    project_dir = _project(tmp_path)
    site_texts.render_draft(project_dir, GOOD, title="Mi libro", today="2026-11-15")
    in_step = {"draft": site_texts.read_draft(project_dir / "metadata_draft.md")}
    assert not site_texts.check(GOOD, endnotes=3, context=in_step)["warnings"]

    changed = {**GOOD, "resumen": "Otro resumen."}
    warnings = site_texts.check(changed, endnotes=3, context=in_step)["warnings"]
    assert [w for w in warnings if w.startswith("draft_out_of_sync: resumen")]


# ── status, context and the new commands ────────────────────────────────────

def test_status_shows_the_edition_and_what_to_ask(tmp_path, monkeypatch):
    monkeypatch.setattr(site_texts, "site_repo_dir", lambda: "")
    project_dir = _project(
        tmp_path, chapter_manifest=[{"id": "chapter_01", "kind": "chapter", "number": 1}])

    bare = site_texts.status(project_dir)
    assert bare["edition"]["title"] == "Mi libro" and bare["edition"]["version"] == ""
    assert len(bare["ask"]) == 2 and bare["chapters"]["last_number"] == 1

    data = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    data.update({"edition": {"version": "1.0", "status": "beta"}, "original_year": 1890})
    (project_dir / "project.json").write_text(json.dumps(data), encoding="utf-8")
    assert site_texts.status(project_dir)["ask"] == []


def test_status_says_when_the_review_read_another_source(tmp_path, monkeypatch):
    monkeypatch.setattr(site_texts, "site_repo_dir", lambda: "")
    project_dir = _project(tmp_path)
    (project_dir / "source.txt").write_text("The edited source.", encoding="utf-8")
    _epub(project_dir, 0)
    same = hashlib.sha256(b"The edited source.").hexdigest()
    review = project_dir / "content-review.md"

    review.write_text(f"# Review\n\n- Source SHA-256 at review: `{same}`.\n", encoding="utf-8")
    matching = site_texts.status(project_dir)
    assert matching["reviews"][0]["matches_source"] is True and matching["needs"] == []

    review.write_text(
        "# Review\n\n- [line 9](C:/elsewhere/out/source.txt:9): a quote.\n"
        f"- Source SHA-256 at review: `{'0' * 64}`.\n", encoding="utf-8")
    other = site_texts.status(project_dir)
    assert other["reviews"][0]["matches_source"] is False
    assert other["reviews"][0]["reviewed_source"] == "C:/elsewhere/out/source.txt"
    assert len(other["needs"]) == 1 and "source-diff --against" in other["needs"][0]


def test_current_texts_reads_what_is_written(tmp_path, monkeypatch):
    site_dir = _site(tmp_path)
    (site_dir / "libros" / "mi-libro" / "descripcion.md").write_text(
        "**Un gancho.**\n", encoding="utf-8")
    monkeypatch.setattr(site_texts, "site_repo_dir", lambda: str(site_dir))
    project_dir = _project(tmp_path, adaptations="- Se queda.", description="Un gancho.")
    (project_dir / "about_book.json").write_text(
        json.dumps({"heading": "Sobre este libro", "body": "Texto."}), encoding="utf-8")

    now = site_texts.current_texts(project_dir)

    assert now["adaptations"] == "- Se queda." and now["about_book"]["body"] == "Texto."
    assert now["description"] == "**Un gancho.**" and now["resumen"] == "Viejo resumen."
    assert now["project_description"] == "Un gancho." and now["edition"]["title"] == "Mi libro"


def test_cli_notes_changes_draft_and_verify(tmp_path, capsys, monkeypatch):
    from scripts import book_metadata as cli

    monkeypatch.setattr(site_texts, "site_repo_dir", lambda: "")
    project_dir = _project(tmp_path)
    _epub(project_dir, 3)
    work = site_texts.work_dir(project_dir)
    work.mkdir(parents=True)
    (work / site_texts.TEXTS_FILE).write_text(json.dumps(GOOD, ensure_ascii=False), encoding="utf-8")

    assert cli.main(["status", "--project", str(project_dir), "--no-facts"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert Path(status["current"]["path"]).exists() and "facts" not in status
    assert (work / "last_output.status.json").exists()

    assert cli.main(["notes", "--project", str(project_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert cli.main(["changes", "--project", str(project_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == []

    assert cli.main(["draft", "--project", str(project_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "created"
    draft = project_dir / "metadata_draft.md"
    draft.write_text(draft.read_text(encoding="utf-8").replace(
        "Es un libro sereno.", "Es un libro muy sereno."), encoding="utf-8")
    assert cli.main(["draft", "--project", str(project_dir), "--pull"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == ["parent_notes"]
    saved = json.loads((work / site_texts.TEXTS_FILE).read_text(encoding="utf-8"))
    assert saved["parent_notes"].startswith("Es un libro muy sereno.")


def test_cli_verify_skips_a_build_that_does_not_exist(tmp_path, capsys):
    from scripts import book_metadata as cli

    project_dir = tmp_path / "sin-epub"
    project_dir.mkdir()
    (project_dir / "project.json").write_text("{}", encoding="utf-8")
    (project_dir / "source.txt").write_text("x", encoding="utf-8")

    assert cli.main(["verify", "--project", str(project_dir)]) == 1
    nothing = json.loads(capsys.readouterr().out)
    assert nothing["ok"] is False and set(nothing["targets"]) == {"kindle", "libre"}
    assert all("skipped" in t for t in nothing["targets"].values())

    # Asked for by name, a missing build is an error, not a skip.
    assert cli.main(["verify", "--project", str(project_dir), "--target", "libre"]) == 1
    asked = json.loads(capsys.readouterr().out)
    assert "error" in asked["targets"]["libre"] and "skipped" not in asked["targets"]["libre"]
