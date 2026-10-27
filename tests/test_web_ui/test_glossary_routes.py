"""Tests for glossary edit-UI routes added in feat/glossary-edit-ui.

Covers:
  GET  /api/setup/<project_id>/glossary  (setup_load_glossary)
  POST /api/setup/<project_id>/glossary  (setup_save_glossary — merge + replace modes)

Conventions follow test_translator_note_routes.py and test_project_align_route.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web_ui.app import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Create a minimal project directory and redirect app's projects dir."""
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "proj1"
    proj_dir.mkdir(parents=True)

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


def _write_glossary(proj_dir: Path, terms: list[dict]) -> None:
    """Write a minimal glossary.json into proj_dir."""
    data = {
        "terms": terms,
        "version": "1.0",
        "updated_at": "2026-01-01T00:00:00",
    }
    (proj_dir / "glossary.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _minimal_term(english: str = "magic", spanish: str = "magia",
                  typ: str = "concept") -> dict:
    return {
        "english": english,
        "spanish": spanish,
        "type": typ,
        "context": "Use consistently",
        "alternatives": ["hechiceria"],
    }


# ===========================================================================
# GET /api/setup/<project_id>/glossary — setup_load_glossary
# ===========================================================================

class TestLoadGlossary:

    # --- input validation ---

    def test_bad_project_id_returns_400(self, client):
        rv = client.get("/api/setup/.../glossary")
        assert rv.status_code == 400
        assert "error" in rv.get_json()

    def test_path_traversal_blocked(self, client):
        rv = client.get("/api/setup/../glossary")
        # Flask may strip the path before routing; either 400 or 404 is acceptable.
        assert rv.status_code in (400, 404)

    # --- happy paths ---

    def test_no_glossary_file_returns_empty_list(self, client, project):
        rv = client.get("/api/setup/proj1/glossary")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data == {"terms": []}

    def test_returns_terms_from_existing_file(self, client, project):
        _write_glossary(project, [_minimal_term()])
        rv = client.get("/api/setup/proj1/glossary")
        assert rv.status_code == 200
        terms = rv.get_json()["terms"]
        assert len(terms) == 1
        t = terms[0]
        assert t["english"] == "magic"
        assert t["spanish"] == "magia"
        assert t["type"] == "concept"       # enum .value serialised as string
        assert t["context"] == "Use consistently"
        assert isinstance(t["alternatives"], list)

    def test_returns_multiple_terms(self, client, project):
        _write_glossary(project, [
            _minimal_term("magic", "magia", "concept"),
            _minimal_term("London", "Londres", "place"),
        ])
        rv = client.get("/api/setup/proj1/glossary")
        assert rv.status_code == 200
        terms = rv.get_json()["terms"]
        assert len(terms) == 2

    def test_term_without_context_defaults_to_empty_string(self, client, project):
        data = {
            "terms": [{"english": "cat", "spanish": "gato", "type": "other"}],
            "version": "1.0",
            "updated_at": "2026-01-01T00:00:00",
        }
        (project / "glossary.json").write_text(json.dumps(data), encoding="utf-8")
        rv = client.get("/api/setup/proj1/glossary")
        assert rv.status_code == 200
        term = rv.get_json()["terms"][0]
        assert term["context"] == ""

    def test_term_without_alternatives_defaults_to_empty_list(self, client, project):
        data = {
            "terms": [{"english": "cat", "spanish": "gato", "type": "other", "context": ""}],
            "version": "1.0",
            "updated_at": "2026-01-01T00:00:00",
        }
        (project / "glossary.json").write_text(json.dumps(data), encoding="utf-8")
        rv = client.get("/api/setup/proj1/glossary")
        assert rv.status_code == 200
        term = rv.get_json()["terms"][0]
        assert term["alternatives"] == []


# ===========================================================================
# POST /api/setup/<project_id>/glossary — setup_save_glossary
# ===========================================================================

class TestSaveGlossaryMergeMode:
    """Default mode="merge": new terms appended, existing preserved."""

    # --- input validation ---

    def test_bad_project_id_returns_400(self, client):
        rv = client.post(
            "/api/setup/.../glossary",
            json={"terms": [_minimal_term()], "mode": "merge"},
        )
        assert rv.status_code == 400

    def test_invalid_mode_returns_400(self, client, project):
        rv = client.post(
            "/api/setup/proj1/glossary",
            json={"terms": [_minimal_term()], "mode": "upsert"},
        )
        assert rv.status_code == 400
        assert rv.get_json()["error"] == "Invalid mode"

    def test_merge_with_empty_terms_returns_400(self, client, project):
        rv = client.post(
            "/api/setup/proj1/glossary",
            json={"terms": [], "mode": "merge"},
        )
        assert rv.status_code == 400
        assert "No terms provided" in rv.get_json()["error"]

    def test_merge_without_mode_field_defaults_to_merge_rejects_empty(
        self, client, project
    ):
        """Omitting 'mode' defaults to 'merge', which requires non-empty terms."""
        rv = client.post(
            "/api/setup/proj1/glossary",
            json={"terms": []},
        )
        assert rv.status_code == 400

    # --- happy path: no existing glossary ---

    def test_merge_creates_new_glossary_when_none_exists(self, client, project):
        rv = client.post(
            "/api/setup/proj1/glossary",
            json={"terms": [_minimal_term()], "mode": "merge"},
        )
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert data["total"] == 1
        assert data["new"] == 1
        assert (project / "glossary.json").exists()

    def test_merge_persists_term_to_disk(self, client, project):
        rv = client.post(
            "/api/setup/proj1/glossary",
            json={"terms": [_minimal_term("magic", "magia")], "mode": "merge"},
        )
        assert rv.status_code == 200
        on_disk = json.loads((project / "glossary.json").read_text(encoding="utf-8"))
        assert any(t["english"] == "magic" for t in on_disk["terms"])

    # --- happy path: with existing glossary ---

    def test_merge_appends_only_new_terms(self, client, project):
        _write_glossary(project, [_minimal_term("magic", "magia")])
        rv = client.post(
            "/api/setup/proj1/glossary",
            json={"terms": [_minimal_term("dragon", "dragon")], "mode": "merge"},
        )
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["total"] == 2
        assert data["new"] == 1

    def test_merge_skips_duplicate_terms_by_english_lower(self, client, project):
        _write_glossary(project, [_minimal_term("magic", "magia")])
        # Submit the same English term (different case to test lowercasing)
        rv = client.post(
            "/api/setup/proj1/glossary",
            json={"terms": [_minimal_term("Magic", "magia")], "mode": "merge"},
        )
        assert rv.status_code == 200
        data = rv.get_json()
        # "Magic".lower() == "magic" → duplicate, not appended
        assert data["new"] == 0
        assert data["total"] == 1

    def test_merge_default_mode_used_when_mode_absent(self, client, project):
        """No 'mode' key → defaults to merge → appends new terms."""
        rv = client.post(
            "/api/setup/proj1/glossary",
            json={"terms": [_minimal_term()]},
        )
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert data["new"] == 1

    def test_merge_moves_updated_at(self, client, project):
        """Merge wrote the *loaded* timestamp straight back, so the reader's
        staleness warning could never fire for the commonest glossary edit."""
        _write_glossary(project, [_minimal_term("magic", "magia")])
        before = json.loads(
            (project / "glossary.json").read_text(encoding="utf-8"))["updated_at"]
        rv = client.post(
            "/api/setup/proj1/glossary",
            json={"terms": [_minimal_term("dragon", "dragon")], "mode": "merge"},
        )
        assert rv.status_code == 200
        after = json.loads(
            (project / "glossary.json").read_text(encoding="utf-8"))["updated_at"]
        assert after > before


class TestSaveGlossaryReplaceMode:
    """mode="replace": entire glossary is replaced with submitted list."""

    def test_replace_creates_glossary_from_empty(self, client, project):
        rv = client.post(
            "/api/setup/proj1/glossary",
            json={"terms": [_minimal_term()], "mode": "replace"},
        )
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["ok"] is True
        assert data["total"] == 1
        assert data["mode"] == "replace"

    def test_replace_overwrites_existing_terms(self, client, project):
        _write_glossary(project, [
            _minimal_term("magic", "magia"),
            _minimal_term("dragon", "dragon"),
        ])
        rv = client.post(
            "/api/setup/proj1/glossary",
            json={"terms": [_minimal_term("castle", "castillo")], "mode": "replace"},
        )
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["total"] == 1  # old 2 terms removed, 1 new term

        on_disk = json.loads((project / "glossary.json").read_text(encoding="utf-8"))
        english_terms = [t["english"] for t in on_disk["terms"]]
        assert english_terms == ["castle"]
        assert "magic" not in english_terms
        assert "dragon" not in english_terms

    def test_replace_with_empty_list_is_rejected(self, client, project):
        _write_glossary(project, [_minimal_term("magic", "magia")])
        rv = client.post(
            "/api/setup/proj1/glossary",
            json={"terms": [], "mode": "replace"},
        )
        assert rv.status_code == 400
        assert "empty" in rv.get_json()["error"].lower()

    def test_replace_returns_correct_mode_in_response(self, client, project):
        rv = client.post(
            "/api/setup/proj1/glossary",
            json={"terms": [_minimal_term()], "mode": "replace"},
        )
        assert rv.get_json()["mode"] == "replace"

    def test_replace_response_has_no_new_key(self, client, project):
        """Replace mode response does not include the 'new' counter (merge-only field)."""
        rv = client.post(
            "/api/setup/proj1/glossary",
            json={"terms": [_minimal_term()], "mode": "replace"},
        )
        data = rv.get_json()
        assert "new" not in data


# ===========================================================================
# Round-trip: GET after POST
# ===========================================================================

class TestGlossaryRoundTrip:

    def test_get_returns_saved_terms_after_replace(self, client, project):
        client.post(
            "/api/setup/proj1/glossary",
            json={"terms": [_minimal_term("sky", "cielo", "other")], "mode": "replace"},
        )
        rv = client.get("/api/setup/proj1/glossary")
        assert rv.status_code == 200
        terms = rv.get_json()["terms"]
        assert len(terms) == 1
        assert terms[0]["english"] == "sky"
        assert terms[0]["spanish"] == "cielo"

    def test_get_reflects_merged_terms(self, client, project):
        client.post(
            "/api/setup/proj1/glossary",
            json={"terms": [_minimal_term("magic", "magia")], "mode": "merge"},
        )
        client.post(
            "/api/setup/proj1/glossary",
            json={"terms": [_minimal_term("dragon", "dragon")], "mode": "merge"},
        )
        rv = client.get("/api/setup/proj1/glossary")
        terms = rv.get_json()["terms"]
        english_set = {t["english"] for t in terms}
        assert english_set == {"magic", "dragon"}


# ---------------------------------------------------------------------------
# note / aliases, unmatched keys, chapter-scoped book rules
# ---------------------------------------------------------------------------

def test_note_and_aliases_round_trip_and_unmatched_keys_are_named(client, project):
    (project / "source.txt").write_text("They rode past the dog town at dusk.", encoding="utf-8")
    terms = [
        {"english": "prairie dog", "spanish": "perrito de la pradera", "type": "other",
         "note": "the rodent, not a dog", "aliases": ["dog town"]},
        {"english": "porcupine", "spanish": "puercoespín", "type": "other"},
    ]
    resp = client.post("/api/setup/proj1/glossary", json={"terms": terms, "mode": "replace"})
    assert resp.get_json()["unmatched"] == ["porcupine"]

    rows = client.get("/api/setup/proj1/glossary").get_json()["terms"]
    assert rows[0]["note"] == "the rodent, not a dog" and rows[0]["aliases"] == ["dog town"]
    assert rows[1]["note"] == "" and rows[1]["aliases"] == []


def test_style_rule_routes_add_list_reject_and_remove(client, project):
    (project / "chapters").mkdir()
    for n in (1, 2, 3):
        (project / "chapters" / f"chapter_0{n}.txt").write_text("x", encoding="utf-8")

    resp = client.post("/api/setup/proj1/style-rules", json={
        "id": "late", "rule": "Adults now.", "since": "chapter_02", "triggers": "",
    })
    assert resp.status_code == 200 and resp.get_json()["rule"]["window"] == "chapter_02 onward"

    listed = client.get("/api/setup/proj1/style-rules").get_json()
    assert [r["id"] for r in listed["rules"]] == ["late"]
    assert [c["id"] for c in listed["chapters"]] == ["chapter_01", "chapter_02", "chapter_03"]

    bad = client.post("/api/setup/proj1/style-rules", json={"id": "x", "rule": "X.", "since": "chapter_09"})
    assert bad.status_code == 400 and "not a chapter" in bad.get_json()["error"]

    assert client.delete("/api/setup/proj1/style-rules/late").get_json()["removed"]
    assert client.get("/api/setup/proj1/style-rules").get_json()["rules"] == []


def test_style_rule_route_splits_triggers_by_line_and_keeps_omitted_fields(client, project):
    (project / "chapters").mkdir()
    (project / "chapters" / "chapter_01.txt").write_text("x", encoding="utf-8")

    resp = client.post("/api/setup/proj1/style-rules", json={
        "id": "dear", "rule": "Render it as ¡Ay, Dios!", "note": "an exclamation",
        "triggers": "re:(?i)\\boh,? dear\\b\ngoodness\n",
    })
    assert resp.status_code == 200
    assert resp.get_json()["rule"]["triggers"] == ["re:(?i)\\boh,? dear\\b", "goodness"]

    # A body without "note" keeps it; an empty one clears it.
    client.post("/api/setup/proj1/style-rules", json={"id": "dear", "rule": "Render it as ¡Ay!"})
    [rule] = client.get("/api/setup/proj1/style-rules").get_json()["rules"]
    assert rule["note"] == "an exclamation" and len(rule["triggers"]) == 2
    client.post("/api/setup/proj1/style-rules", json={"id": "dear", "rule": "Ay.", "note": ""})
    [rule] = client.get("/api/setup/proj1/style-rules").get_json()["rules"]
    assert "note" not in rule


def test_style_rule_routes_reject_a_missing_project_and_a_bad_body(client, project):
    assert client.get("/api/setup/nosuch/style-rules").status_code == 404
    assert client.post("/api/setup/nosuch/style-rules", json={"id": "x", "rule": "X."}).status_code == 404
    assert client.delete("/api/setup/nosuch/style-rules/x").status_code == 404
    assert client.post("/api/setup/proj1/style-rules", json=["not", "an", "object"]).status_code == 400
    assert client.post("/api/setup/proj1/style-rules",
                       json={"id": "x", "rule": "X.", "triggers": 5}).status_code == 400
