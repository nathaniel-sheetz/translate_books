"""Tests for src.kdp.listing / report / verify and the kdp_publish CLI.

The listing is the store's half: the edition is applied first by
``src.site_texts.apply`` (the book-metadata skill), and the listing is checked
against it and writes only ``kdp_listing.json``.
"""

import copy
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from src import site_texts
from src.epub_builder import build_epub
from src.book_metadata import epub_kwargs
from src.kdp import listing as kdp_listing
from src.kdp import report as kdp_report
from src.kdp.verify import verify

from tests.test_kdp_validate import GOOD

DEFAULTS = {"translator": "Ana Pérez", "publisher": "Editorial Ejemplo",
            "rights": "Traducción © {year} {translator}. La obra original es de dominio público."}
TODAY = date(2026, 10, 19)
EDITION = {
    "edition": {
        "title": "Bambi", "subtitle": "Una vida en el bosque", "author": "Felix Salten",
        "translator": "Ana Pérez", "original_year": 1923,
        "subjects": ["Nature stories", "Deer -- Fiction"],
        "version": "1.0", "status": "beta",
        "notice": {"heading": "Créditos",
                   "body": "Publicado originalmente en 1923.\n\nTraducción: Ana Pérez."},
    },
    "description": "En lo más hondo del bosque nace un cervatillo.",
}
CONTEXT = {"defaults": DEFAULTS, "endnotes": None, "scripture_notice": "",
           "last_chapter": None, "draft": {}}


@pytest.fixture(autouse=True)
def _tracked_defaults(tmp_path, monkeypatch):
    """The CLI reads the per-user defaults; pin it to the tracked example."""
    import src.kdp

    monkeypatch.setattr(src.kdp, "DEFAULTS_FILE", tmp_path / "no_private_defaults.json")


def _listing():
    return copy.deepcopy(GOOD)


def _bare_project(tmp_path: Path) -> Path:
    """A translated book the edition has not been applied to."""
    project = tmp_path / "book"
    (project / "chapters").mkdir(parents=True)
    (project / "chapters" / "chapter_01.txt").write_text(
        "CAPÍTULO I\n\nEl bosque\n\nNace un cervatillo.", encoding="utf-8")
    (project / "project.json").write_text(json.dumps({"title": "Old"}), encoding="utf-8")
    return project


def _project(tmp_path: Path) -> Path:
    """The same book after ``book_metadata.py apply``: the state a listing starts from."""
    project = _bare_project(tmp_path)
    result = site_texts.apply(project, EDITION, context=CONTEXT, today=TODAY)
    assert result["applied"], result
    return project


def test_book_state_reads_the_applied_edition(tmp_path):
    bare = kdp_listing.book_state(_bare_project(tmp_path / "before"))
    assert bare["edition_applied"] is False
    assert bare["title"] == "Old" and bare["endnotes"] is None

    state = kdp_listing.book_state(_project(tmp_path / "after"))
    assert state["edition_applied"] is True
    assert (state["title"], state["subtitle"], state["translator"]) == (
        "Bambi", "Una vida en el bosque", "Ana Pérez")
    assert state["description"] == "En lo más hondo del bosque nace un cervatillo."
    assert state["edition"] == {"version": "1.0", "status": "beta"}


def test_apply_refuses_on_errors(tmp_path):
    project = _project(tmp_path)
    bad = _listing()
    bad["keywords"] = bad["keywords"][:3]
    result = kdp_listing.apply(project, bad, DEFAULTS, today=TODAY)
    assert result["status"] == "refused"
    assert not (project / "kdp_listing.json").exists()


def test_apply_refuses_before_the_edition_is_applied(tmp_path):
    project = _bare_project(tmp_path)
    result = kdp_listing.apply(project, _listing(), DEFAULTS, today=TODAY)
    assert result["status"] == "refused"
    codes = [i["code"] for i in result["check"]["errors"]]
    assert codes == ["edition_not_applied"]
    assert "book-metadata" in result["check"]["errors"][0]["detail"]


def test_apply_refuses_a_listing_that_names_the_book_differently(tmp_path):
    project = _project(tmp_path)
    listing = _listing()
    listing["title"] = "Bambi, el cervatillo"
    result = kdp_listing.apply(project, listing, DEFAULTS, today=TODAY)
    assert result["status"] == "refused"
    issue = result["check"]["errors"][0]
    assert issue["code"] == "edition_mismatch" and issue["field"] == "title"


def test_apply_dry_run_writes_nothing(tmp_path):
    project = _project(tmp_path)
    result = kdp_listing.apply(project, _listing(), DEFAULTS, dry_run=True, today=TODAY)
    assert result["status"] == "dry_run"
    assert {"title", "keywords", "categories", "kdp_select"} <= {c["field"] for c in result["changes"]}
    assert all(c["file"] == "kdp_listing.json" for c in result["changes"])
    assert not (project / "kdp_listing.json").exists()


def test_apply_writes_only_the_listing_then_build_then_verify(tmp_path):
    project = _project(tmp_path)
    book_before = (project / "project.json").read_bytes()
    notice_before = (project / "edition_notice.json").read_bytes()

    result = kdp_listing.apply(project, _listing(), DEFAULTS, today=TODAY)

    assert result["status"] == "applied"
    assert [Path(p).name for p in result["written"]] == ["kdp_listing.json"]
    assert (project / "project.json").read_bytes() == book_before
    assert (project / "edition_notice.json").read_bytes() == notice_before
    record = json.loads((project / "kdp_listing.json").read_text(encoding="utf-8"))
    assert record["approved_at"] == "2026-10-19" and record["kdp_select"] is False
    assert [r["field"] for r in record["kdp_form"]][:3] == ["Language", "Book Title", "Subtitle"]
    assert record["kdp_form"][1]["value"] == "Bambi (Traducido)"

    kwargs = epub_kwargs(project)
    build_epub(project_path=project, **kwargs)
    verdict = verify(project)
    assert verdict["ok"], [c for c in verdict["checks"] if not c["ok"]]
    assert {c["check"] for c in verdict["checks"]} >= {
        "title", "subtitle", "translator_trl", "subjects", "date", "title_page_first",
        "edition_notice", "no_gutenberg_trademark", "note_counts_agree",
        "footnotes_look_like_glosses"}


def test_an_old_epub_block_is_ignored_with_a_warning(tmp_path):
    project = _project(tmp_path)
    listing = _listing()
    listing["epub"] = {"edition_notice": {"heading": "Otro", "body": "Otro texto."},
                       "original_year": 1900}
    result = kdp_listing.apply(project, listing, DEFAULTS, today=TODAY)
    assert result["status"] == "applied"
    assert "epub_block_ignored" in [w["code"] for w in result["warnings"]]
    assert json.loads((project / "project.json").read_text(encoding="utf-8"))["original_year"] == 1923
    assert "epub" not in json.loads((project / "kdp_listing.json").read_text(encoding="utf-8"))


def test_mark_published_records_the_asin_and_apply_keeps_it(tmp_path):
    project = _project(tmp_path)
    assert kdp_listing.mark_published(project, "B0EXAMPLE1")["status"] == "refused"  # nothing applied
    kdp_listing.apply(project, _listing(), DEFAULTS, today=TODAY)

    assert kdp_listing.mark_published(project, "not-an-asin")["status"] == "refused"
    done = kdp_listing.mark_published(project, "b0example1", when="2026-10-20")
    assert done["status"] == "ok" and done["after"] == {
        "asin": "B0EXAMPLE1", "published_at": "2026-10-20"}

    again = kdp_listing.apply(project, _listing(), DEFAULTS, today=date(2026, 11, 1))
    assert again["changes"] == []
    record = json.loads((project / "kdp_listing.json").read_text(encoding="utf-8"))
    assert record["asin"] == "B0EXAMPLE1" and record["published_at"] == "2026-10-20"
    assert record["approved_at"] == "2026-11-01"


def test_verify_catches_gutenberg_in_the_body(tmp_path):
    project = _bare_project(tmp_path)
    (project / "chapters" / "chapter_01.txt").write_text(
        "CAPÍTULO I\n\nTexto del Project Gutenberg.", encoding="utf-8")
    build_epub(project_path=project, title="Bambi", author="Felix Salten")
    checks = {c["check"]: c for c in verify(project)["checks"]}
    assert not checks["no_gutenberg_trademark"]["ok"]
    assert "chapter_01.xhtml" in checks["no_gutenberg_trademark"]["detail"]


def test_verify_allows_johannes_gutenberg(tmp_path):
    project = _bare_project(tmp_path)
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
    # The site is read live, not from facts.json: a libre build puts the book on it.
    (project / "libre").mkdir()
    (project / "libre" / "bambi.epub").write_bytes(b"")
    (kdp / "facts.json").write_text(json.dumps({
        "site": {"on_site": False},
        "existing_listing": {"file": True, "asin": "B0EXAMPLE1", "published_at": "2026-05-01"},
    }), encoding="utf-8")
    path = kdp_report.write(project, _listing(), DEFAULTS, now=datetime(2026, 10, 19, 9, 30))
    assert path.name == "kdp_20261019_093000.md"
    text = path.read_text(encoding="utf-8")
    assert "# KDP listing — Bambi (Traducido)" in text
    assert "#1 → overall #16,410, #20 → overall ?" in text
    assert "Infantil (node 1): 1" in text and "Outro" not in text
    assert "descriptor_not_english" in text
    assert "`kdp_listing.json` only" in text and "Publicado originalmente" not in text
    assert "**KDP Select:** No. The book is on the site" in text
    assert "already for sale as B0EXAMPLE1" in text
    assert "**Edition:** 1.0 beta" in text


def test_cli_check_and_apply(tmp_path, capsys):
    from scripts import kdp_publish

    project = _project(tmp_path)
    kdp = project / ".harness" / "kdp"
    kdp.mkdir(parents=True)
    (kdp / "listing.json").write_text(json.dumps(_listing(), ensure_ascii=False), encoding="utf-8")
    assert kdp_publish.main(["check", "--project", str(project)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["counts"]["keywords"] == 7
    assert (kdp / "last_output.json").exists() and (kdp / "last_output.check.json").exists()

    assert kdp_publish.main(["apply", "--project", str(project), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "dry_run"
    assert json.loads((kdp / "last_output.check.json").read_text(encoding="utf-8"))["ok"]

    assert kdp_publish.main(["apply", "--project", str(project)]) == 0
    capsys.readouterr()
    assert kdp_publish.main(["published", "--project", str(project), "--asin", "B0EXAMPLE1"]) == 0
    assert json.loads(capsys.readouterr().out)["after"]["asin"] == "B0EXAMPLE1"


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
    assert form["Book Title"] == "Bambi" and form["KDP Select"] == "No"
    kdp = project / ".harness" / "kdp"
    kdp.mkdir(parents=True)
    (kdp / "facts.json").write_text(json.dumps({"authorship": {
        "words_with_draft": 1000, "changed_words": 13, "changed_fraction": 0.013,
        "reader_saves_typed": 4, "reader_saves_from_finding": 2, "model_fixes": 9}}),
        encoding="utf-8")
    (project / "metadata_draft.md").write_text(
        "# Draft\n\n## 5. Texts\n\nno\n\n## What the record shows\n\n"
        "| Finding | Where | Evidence |\n|---|---|---|\n| Smith → Díaz | cap. 2 | corrections |\n\n"
        "## Decisions\n\nnot this\n", encoding="utf-8")
    text = kdp_report.render(project, OWNED, DEFAULTS, now=datetime(2026, 10, 19))
    assert "## Copyright basis" in text
    assert "13 of 1,000 words changed (1.3%)" in text
    assert "4 typed, 2 from a checker's finding; 9 applied by model judges" in text
    assert "Guía Charlotte Mason" in text
    assert "| Smith → Díaz | cap. 2 | corrections |" in text and "not this" not in text
    result = kdp_listing.apply(project, OWNED, DEFAULTS, today=TODAY)
    record = json.loads((project / "kdp_listing.json").read_text(encoding="utf-8"))
    assert result["status"] == "applied" and record["publishing_rights"] == "own_copyright"
    assert "description_diverges" in [w["code"] for w in result["warnings"]]


def test_apply_refuses_an_unreadable_listing_record(tmp_path):
    project = _project(tmp_path)
    broken = '{"title": "Bambi",}'
    (project / "kdp_listing.json").write_text(broken, encoding="utf-8")
    for dry_run in (True, False):
        result = kdp_listing.apply(project, _listing(), DEFAULTS, dry_run=dry_run, today=TODAY)
        assert result["status"] == "refused" and "kdp_listing.json" in result["reason"]
    assert (project / "kdp_listing.json").read_text(encoding="utf-8") == broken


def test_kdp_form_reading_age_with_one_bound():
    listing = _listing()
    listing["primary_audience"] = {"reading_age_min": None, "reading_age_max": 14}
    rows = {r["field"]: r["value"] for r in kdp_listing.kdp_form(listing, DEFAULTS)}
    assert rows["Reading age"] == "–14"


def test_kdp_form_answers_select_from_the_site():
    listing = _listing()
    listing["kdp_select"] = True
    off_site = {r["field"]: r["value"] for r in kdp_listing.kdp_form(listing, DEFAULTS)}
    assert off_site["KDP Select"] == "Yes"
    on_site = {r["field"]: r["value"] for r in kdp_listing.kdp_form(
        listing, DEFAULTS, facts={"site": {"on_site": True}})}
    assert on_site["KDP Select"].startswith("No. The book is on the site")
