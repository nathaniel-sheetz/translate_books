"""The decisions scan: taxonomy hold-out, prepare, fanout (stubbed) and commit."""

from __future__ import annotations

import json

import pytest

from src.decisions import decisions_dir
from src.decisions import scan as dscan
from src.decisions import taxonomy as dtax

TAXONOMY = {
    "version": 1,
    "categories": [
        {
            "id": "names",
            "name": "Which names to translate",
            "description": "Names that are ordinary words.",
            "detection_cues": ["names that are ordinary English words"],
            "house_default": "Places: translate.",
            "examples": [
                {"project": "book-a", "text": "Translated Thunder as Trueno."},
                {"project": "other-book", "text": "Kept Rex."},
            ],
        },
        {
            "id": "address-pairs",
            "name": "tú / usted",
            "description": "Who says tú or usted to whom.",
            "detection_cues": ["servants speaking to children"],
            "examples": [{"project": "book-a", "text": "The gardener says usted."}],
        },
    ],
}

CHAPTER_01 = (
    "Old Thunder stood by the gate.\n\n"
    "“Good morning, Miss Polly,” said Hobbs, the hired man.\n\n"
    "Polly ran to the barn."
)
CHAPTER_02 = "Thunder ate his oats. Hobbs whistled."


@pytest.fixture
def taxonomy_file(tmp_path, monkeypatch):
    path = tmp_path / "decisions_taxonomy.json"
    path.write_text(json.dumps(TAXONOMY), encoding="utf-8")
    monkeypatch.setattr(dtax, "TAXONOMY_FILE", path)
    monkeypatch.setattr(dtax, "TAXONOMY_EXAMPLE_FILE", tmp_path / "missing.json")
    return path


@pytest.fixture
def project(tmp_path, taxonomy_file):
    project_dir = tmp_path / "projects" / "book-a"
    chapters = project_dir / "chapters"
    chapters.mkdir(parents=True)
    (chapters / "chapter_01.txt").write_text(CHAPTER_01, encoding="utf-8")
    (chapters / "chapter_02.txt").write_text(CHAPTER_02, encoding="utf-8")
    return project_dir


def _draft(project_dir, job_id, payload):
    path = decisions_dir(project_dir) / f"{job_id}.scan.draft.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# --- taxonomy ---------------------------------------------------------------

def test_load_taxonomy_holds_out_the_books_own_examples(taxonomy_file):
    tax = dtax.load_taxonomy(exclude_project="book-a")
    assert tax["held_out_examples"] == 2
    texts = [e["text"] for c in tax["categories"] for e in c["examples"]]
    assert texts == ["Kept Rex."]


def test_format_taxonomy_omits_project_tags(taxonomy_file):
    text = dtax.format_taxonomy(dtax.load_taxonomy())
    assert "### names" in text and "house_default: Places: translate." in text
    assert "book-a" not in text


def test_load_taxonomy_refuses_an_empty_file(tmp_path):
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"categories": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        dtax.load_taxonomy(path=path)


# --- helpers ----------------------------------------------------------------

def test_split_windows_keeps_short_text_whole():
    assert dscan.split_windows("one two three", max_words=10) == ["one two three"]


def test_split_windows_breaks_on_paragraphs():
    text = "\n\n".join(" ".join(["w"] * 30) for _ in range(4))
    windows = dscan.split_windows(text, max_words=70)
    assert [len(w.split()) for w in windows] == [60, 60]


def test_subject_key_folds_case_articles_and_arrows():
    assert dscan.subject_key("The Hobbs → the children") == dscan.subject_key("hobbs -> The children")
    assert dscan.subject_key("Pony Penning") == "pony penning"


def test_validate_candidate_matches_quotes_across_curly_quotes():
    norm = dscan.normalize_for_match(CHAPTER_01)
    row, reason = dscan.validate_candidate(
        {
            "category": "address-pairs",
            "subject": "Hobbs -> Polly",
            "question": "usted or tú?",
            "quotes": ['"Good morning, Miss Polly," said Hobbs', "invented line"],
        },
        chapter_text_norm=norm,
        categories={"names", "address-pairs"},
    )
    assert reason is None
    assert row["quotes"] == ['"Good morning, Miss Polly," said Hobbs']
    assert row["quotes_dropped"] == 1


def test_validate_candidate_refuses_unfound_quotes_and_unknown_categories():
    norm = dscan.normalize_for_match(CHAPTER_01)
    base = {"subject": "x", "question": "q?", "quotes": ["Old Thunder stood"]}
    _row, reason = dscan.validate_candidate(
        {**base, "category": "made-up"}, chapter_text_norm=norm, categories={"names"}
    )
    assert reason == dscan.UNUSABLE_CATEGORY
    _row, reason = dscan.validate_candidate(
        {**base, "category": "names", "quotes": ["not in the text"]},
        chapter_text_norm=norm,
        categories={"names"},
    )
    assert reason == dscan.UNUSABLE_QUOTE
    _row, reason = dscan.validate_candidate(
        {**base, "category": "other"}, chapter_text_norm=norm, categories={"names"}
    )
    assert reason is None


# --- prepare ----------------------------------------------------------------

def test_prepare_renders_one_job_per_chapter_without_the_held_out_examples(project):
    out = dscan.scan_prepare(project)
    assert out["status"] == "ok", out
    assert out["jobs"] == 2
    assert out["held_out_examples"] == 2
    preamble = (decisions_dir(project) / "preamble.scan.txt").read_text(encoding="utf-8")
    assert "Kept Rex." in preamble
    assert "Translated Thunder" not in preamble
    assert "{{" not in preamble
    body = (decisions_dir(project) / "chapter_01.scan.body.txt").read_text(encoding="utf-8")
    assert body.startswith("CHAPTER: chapter_01")
    assert "Old Thunder stood by the gate." in body


def test_prepare_uses_house_rules_but_not_the_books_own_rules(project):
    (project / "style_rules.json").write_text(
        json.dumps({"rules": [{"id": "book-secret", "rule": "Thunder is Trueno."}]}),
        encoding="utf-8",
    )
    dscan.scan_prepare(project)
    preamble = (decisions_dir(project) / "preamble.scan.txt").read_text(encoding="utf-8")
    assert "book-secret" not in preamble


def test_prepare_lists_prior_rulings(project):
    decisions_dir(project).mkdir(parents=True)
    dscan.rulings_path(project).write_text(
        json.dumps({"category": "names", "subject": "Thunder", "choice": "Trueno"}) + "\n",
        encoding="utf-8",
    )
    out = dscan.scan_prepare(project)
    assert out["rulings"] == 1
    preamble = (decisions_dir(project) / "preamble.scan.txt").read_text(encoding="utf-8")
    assert "[names] Thunder: Trueno" in preamble


def test_prepare_rejects_unknown_chapters(project):
    out = dscan.scan_prepare(project, chapters=["chapter_09"])
    assert out["status"] == "error"


# --- fanout (stubbed runner, nothing spawned) -------------------------------

def test_fanout_needs_a_manifest(project):
    assert "scan-prepare" in dscan.scan_fanout(project)["error"]


def test_fanout_sends_the_body_and_skips_existing_drafts(project):
    dscan.scan_prepare(project)
    _draft(project, "chapter_02", {"chapter_id": "chapter_02", "candidates": []})
    seen = []

    def runner(cmd, *, input_text, cwd, **kwargs):
        seen.append({"cmd": cmd, "input_text": input_text})
        return 0, json.dumps({"chapter_id": "chapter_01", "candidates": []}), ""

    out = dscan.scan_fanout(project, runner=runner, concurrency=1)
    assert out["counts"]["wrote"] == 1
    assert out["skipped"] == ["chapter_02"]
    assert "--system-prompt-file" in " ".join(seen[0]["cmd"])
    assert seen[0]["input_text"].startswith("CHAPTER: chapter_01")


def test_fanout_rejects_unknown_job_ids(project):
    dscan.scan_prepare(project)
    assert "not in the manifest" in dscan.scan_fanout(project, job_ids=["nope"])["error"]


# --- commit -----------------------------------------------------------------

def test_commit_merges_subjects_across_chapters_and_reports_volume(project):
    dscan.scan_prepare(project)
    thunder = {
        "category": "names",
        "subject": "Thunder",
        "question": "Translate the horse's name?",
        "quotes": ["Old Thunder stood by the gate."],
        "options": [{"label": "Trueno", "effect": "translate descriptive names"}],
        "why_hard": "an ordinary word",
        "house_default": False,
    }
    _draft(project, "chapter_01", {
        "chapter_id": "chapter_01",
        "candidates": [
            thunder,
            {**thunder, "category": "address-pairs", "subject": "Hobbs -> Polly",
             "quotes": ["“Good morning, Miss Polly,” said Hobbs"]},
            {**thunder, "subject": "ghost", "quotes": ["never written"]},
        ],
    })
    _draft(project, "chapter_02", {
        "chapter_id": "chapter_02",
        "candidates": [{**thunder, "subject": "the thunder", "quotes": ["Thunder ate his oats."]}],
    })

    out = dscan.scan_commit(project)
    assert out["status"] == "ok", out
    assert out["counts"]["usable"] == 3
    assert out["unusable_by_reason"] == {dscan.UNUSABLE_QUOTE: 1}
    assert out["volume"]["subjects"] == 2
    assert out["by_category"]["names"] == {"candidates": 2, "subjects": 1}
    report = (decisions_dir(project) / "candidates.md").read_text(encoding="utf-8")
    assert "### Thunder — 2 chapter(s)" in report


def test_commit_reports_missing_and_bad_drafts(project):
    dscan.scan_prepare(project)
    _draft(project, "chapter_01", {"chapter_id": "chapter_09", "candidates": []})
    out = dscan.scan_commit(project)
    assert out["status"] == "error"
    assert out["missing"] == ["chapter_02"]
    assert "chapter mismatch" in out["failed"][0]["problem"]
