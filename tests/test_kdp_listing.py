"""Tests for src.kdp.listing / report / verify and the kdp_publish CLI — apply to a built EPUB."""

import copy
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from src.epub_builder import build_epub
from src.book_metadata import epub_kwargs
from src.kdp import listing as kdp_listing
from src.kdp import report as kdp_report
from src.kdp.verify import verify

from tests.test_kdp_validate import GOOD

DEFAULTS = {"translator": "Ana Pérez", "publisher": "Editorial Ejemplo",
            "rights": "Traducción © {year} {translator}. La obra original es de dominio público."}
TODAY = date(2026, 10, 19)


@pytest.fixture(autouse=True)
def _tracked_defaults(tmp_path, monkeypatch):
    """The CLI reads the per-user defaults; pin it to the tracked example."""
    import src.kdp

    monkeypatch.setattr(src.kdp, "DEFAULTS_FILE", tmp_path / "no_private_defaults.json")


def _listing():
    listing = copy.deepcopy(GOOD)
    listing["epub"] = {
        "subjects": ["Nature stories", "Deer -- Fiction"],
        "original_year": 1923,
        "edition_notice": {"heading": "Créditos", "body": "Publicado originalmente en 1923.\n\nTraducción: Ana Pérez."},
    }
    return listing


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "book"
    (project / "chapters").mkdir(parents=True)
    (project / "chapters" / "chapter_01.txt").write_text(
        "CAPÍTULO I\n\nEl bosque\n\nNace un cervatillo.", encoding="utf-8")
    (project / "project.json").write_text(json.dumps({"title": "Old", "translator": "A. P."}),
                                          encoding="utf-8")
    return project


def test_planned_writes():
    plan = kdp_listing.planned_writes(Path("."), _listing(), DEFAULTS, today=TODAY)
    fields = plan["project_json"]
    assert fields["spanish_title"] == "Bambi"
    assert fields["subtitle"] == "Una vida en el bosque"
    assert fields["translator"] == "Ana Pérez"
    assert fields["publisher"] == "Editorial Ejemplo"
    assert fields["rights"] == "Traducción © 2026 Ana Pérez. La obra original es de dominio público."
    assert fields["description"] == "En lo más hondo del bosque nace un cervatillo."
    assert fields["publication_date"] == "2026-10-19"
    assert fields["front_matter"] == {"title_page": True}
    assert plan["edition_notice"]["heading"] == "Créditos"


def test_apply_refuses_on_errors(tmp_path):
    project = _project(tmp_path)
    bad = _listing()
    bad["keywords"] = bad["keywords"][:3]
    result = kdp_listing.apply(project, bad, DEFAULTS, today=TODAY)
    assert result["status"] == "refused"
    assert json.loads((project / "project.json").read_text(encoding="utf-8"))["title"] == "Old"


def test_apply_dry_run_writes_nothing(tmp_path):
    project = _project(tmp_path)
    result = kdp_listing.apply(project, _listing(), DEFAULTS, dry_run=True, today=TODAY)
    assert result["status"] == "dry_run"
    changed = {c["field"] for c in result["changes"]}
    assert {"spanish_title", "translator", "subjects", "body"} <= changed
    assert not (project / "kdp_listing.json").exists()
    assert not (project / "edition_notice.json").exists()


def test_apply_then_build_then_verify(tmp_path):
    project = _project(tmp_path)
    result = kdp_listing.apply(project, _listing(), DEFAULTS, today=TODAY)
    assert result["status"] == "applied"
    saved = json.loads((project / "project.json").read_text(encoding="utf-8"))
    assert saved["title"] == "Old"  # untouched: the loader prefers spanish_title
    assert saved["translator"] == "Ana Pérez"
    record = json.loads((project / "kdp_listing.json").read_text(encoding="utf-8"))
    assert record["approved_at"] == "2026-10-19"
    assert [r["field"] for r in record["kdp_form"]][:3] == ["Language", "Book Title", "Subtitle"]
    assert record["kdp_form"][1]["value"] == "Bambi (Traducido)"

    kwargs = epub_kwargs(project)
    build_epub(project_path=project, **kwargs)
    verdict = verify(project)
    assert verdict["ok"], [c for c in verdict["checks"] if not c["ok"]]
    assert {c["check"] for c in verdict["checks"]} >= {
        "title", "subtitle", "translator_trl", "subjects", "date",
        "title_page_first", "edition_notice", "no_gutenberg_trademark"}


def test_verify_catches_gutenberg_in_the_body(tmp_path):
    project = _project(tmp_path)
    (project / "chapters" / "chapter_01.txt").write_text(
        "CAPÍTULO I\n\nTexto del Project Gutenberg.", encoding="utf-8")
    build_epub(project_path=project, title="Bambi", author="Felix Salten")
    checks = {c["check"]: c for c in verify(project)["checks"]}
    assert not checks["no_gutenberg_trademark"]["ok"]
    assert "chapter_01.xhtml" in checks["no_gutenberg_trademark"]["detail"]


def test_verify_allows_johannes_gutenberg(tmp_path):
    project = _project(tmp_path)
    (project / "chapters" / "chapter_01.txt").write_text(
        "CAPÍTULO I\n\nLa imprenta se debe a Gutenberg, orfebre de Maguncia.", encoding="utf-8")
    build_epub(project_path=project, title="Bambi", author="Felix Salten")
    checks = {c["check"]: c for c in verify(project)["checks"]}
    assert checks["no_gutenberg_trademark"]["ok"], checks["no_gutenberg_trademark"]["detail"]


def test_verify_without_an_epub(tmp_path):
    assert verify(tmp_path)["ok"] is False


def test_report(tmp_path):
    project = _project(tmp_path)
    kdp = project / ".harness" / "kdp"
    kdp.mkdir(parents=True)
    (kdp / "categories.json").write_text(json.dumps({"categories": [
        {"node": "1", "sample": [{"category_rank": 1, "overall": 16410},
                                 {"category_rank": 20, "overall": None}]}]}), encoding="utf-8")
    (kdp / "comps.json").write_text(json.dumps({"listings": [
        {"asin": "B1", "title": "Comp", "language": "Spanish",
         "bsr": {"overall": 5000, "categories": [{"node": "1", "name": "Infantil", "store": "digital-text", "rank": 3}]}},
        {"asin": "B2", "title": "Port", "language": "Portuguese",
         "bsr": {"overall": 9, "categories": [{"node": "9", "name": "Outro", "store": "digital-text"}]}},
    ]}), encoding="utf-8")
    path = kdp_report.write(project, _listing(), DEFAULTS, now=datetime(2026, 10, 19, 9, 30))
    assert path.name == "kdp_20261019_093000.md"
    text = path.read_text(encoding="utf-8")
    assert "# KDP listing — Bambi (Traducido)" in text
    assert "#1 → overall #16,410, #20 → overall ?" in text
    assert "Infantil (node 1): 1" in text and "Outro" not in text
    assert "descriptor_not_english" in text
    assert "Publicado originalmente en 1923." in text


def test_cli_check_and_apply(tmp_path, capsys):
    from scripts import kdp_publish

    project = _project(tmp_path)
    kdp = project / ".harness" / "kdp"
    kdp.mkdir(parents=True)
    (kdp / "listing.json").write_text(json.dumps(_listing(), ensure_ascii=False), encoding="utf-8")
    assert kdp_publish.main(["check", "--project", str(project)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["counts"]["keywords"] == 7
    assert (kdp / "last_output.json").exists()

    assert kdp_publish.main(["apply", "--project", str(project), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"


def test_cli_missing_listing(tmp_path):
    from scripts import kdp_publish

    project = _project(tmp_path)
    with pytest.raises(SystemExit) as exc:
        kdp_publish.main(["check", "--project", str(project)])
    assert "no listing" in str(exc.value)


def test_owned_copyright_form_and_report(tmp_path):
    from tests.test_kdp_validate import OWNED

    project = _project(tmp_path)
    form = {r["field"]: r["value"] for r in kdp_listing.kdp_form(OWNED)}
    assert form["Publishing Rights"].startswith("I own the copyright")
    assert form["Book Title"] == "Bambi"
    kdp = project / ".harness" / "kdp"
    kdp.mkdir(parents=True)
    (kdp / "facts.json").write_text(json.dumps({"authorship": {
        "words_with_draft": 1000, "changed_words": 13, "changed_fraction": 0.013,
        "reader_saves_typed": 4, "reader_saves_from_finding": 2, "model_fixes": 9}}),
        encoding="utf-8")
    text = kdp_report.render(project, OWNED, DEFAULTS, now=datetime(2026, 10, 19))
    assert "## Copyright basis" in text
    assert "13 of 1,000 words changed (1.3%)" in text
    assert "4 typed, 2 from a checker's finding; 9 applied by model judges" in text
    assert "Guía Charlotte Mason" in text
    result = kdp_listing.apply(project, OWNED, DEFAULTS, today=TODAY)
    record = json.loads((project / "kdp_listing.json").read_text(encoding="utf-8"))
    assert result["status"] == "applied" and record["publishing_rights"] == "own_copyright"


def test_apply_refuses_an_unreadable_project_json(tmp_path):
    project = _project(tmp_path)
    broken = '{"title": "Old", "chapter_manifest": [],}'
    (project / "project.json").write_text(broken, encoding="utf-8")
    for dry_run in (True, False):
        result = kdp_listing.apply(project, _listing(), DEFAULTS, dry_run=dry_run, today=TODAY)
        assert result["status"] == "refused" and "project.json" in result["reason"]
    assert (project / "project.json").read_text(encoding="utf-8") == broken
    assert not (project / "kdp_listing.json").exists()


def test_apply_merges_front_matter(tmp_path):
    project = _project(tmp_path)
    (project / "project.json").write_text(json.dumps(
        {"title": "Old", "front_matter": {"translator_label": "Versión de", "title_page": False}}),
        encoding="utf-8")
    kdp_listing.apply(project, _listing(), DEFAULTS, today=TODAY)
    written = json.loads((project / "project.json").read_text(encoding="utf-8"))
    assert written["front_matter"] == {"translator_label": "Versión de", "title_page": True}


def test_dry_run_names_a_heading_only_notice_change(tmp_path):
    project = _project(tmp_path)
    notice = _listing()["epub"]["edition_notice"]
    (project / "edition_notice.json").write_text(
        json.dumps({"heading": "Otro", "body": notice["body"]}), encoding="utf-8")
    result = kdp_listing.apply(project, _listing(), DEFAULTS, dry_run=True, today=TODAY)
    notice_changes = [c for c in result["changes"] if c["file"] == "edition_notice.json"]
    assert notice_changes == [{"file": "edition_notice.json", "field": "heading",
                               "before": "Otro", "after": "Créditos"}]


def test_kdp_form_reading_age_with_one_bound():
    listing = _listing()
    listing["primary_audience"] = {"reading_age_min": None, "reading_age_max": 14}
    rows = {r["field"]: r["value"] for r in kdp_listing.kdp_form(listing, DEFAULTS)}
    assert rows["Reading age"] == "–14"
