"""Tests for the per-book ignore-list routes.

Covers:
  GET    /api/project/<project_id>/ignored-terms
  POST   /api/project/<project_id>/ignored-terms
  DELETE /api/project/<project_id>/ignored-terms

The reader can only add; removal exists solely here, on the dashboard side.
That is a UI affordance rather than a boundary -- the app has no authentication
-- so what these tests pin is the *shape*: add is idempotent, remove is a real
delete, and neither needs a re-evaluation to take effect, because the ignore
list filters at read time.

Conventions follow test_glossary_routes.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web_ui.app import app


URL = "/api/project/proj1/ignored-terms"


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def project(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "proj1"
    proj_dir.mkdir(parents=True)

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


def _write_evaluation(proj_dir: Path, issues: list[dict]) -> None:
    ev = proj_dir / "evaluations"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "chapter_01_chunk_000.json").write_text(
        json.dumps({"chunk_id": "chapter_01_chunk_000", "normalized_issues": issues}),
        encoding="utf-8",
    )


def _dict_issue(word, position=10):
    return {
        "eval_name": "dictionary",
        "issue_index": 0,
        "severity": "warning",
        "message": "'" + word + "': Unknown word (found 1 time(s))",
        "location": {
            "raw": "Character position " + str(position),
            "side": "target",
            "char_start": position,
            "char_end": position + len(word),
        },
        "term": word,
        "rule_id": None,
    }


def _write_feedback(proj_dir: Path, issues: list[dict]) -> None:
    """Dismiss the given findings, keyed the way the reader writes them."""
    from web_ui.evaluations import issue_key

    ev = proj_dir / "evaluations"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "_feedback.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "chunk_id": "chapter_01_chunk_000",
                    "eval_name": i["eval_name"],
                    "issue_key": issue_key(i["eval_name"], i),
                    "feedback_type": "resolved",
                }
            )
            + "\n"
            for i in issues
        ),
        encoding="utf-8",
    )


def _stored(proj_dir: Path) -> dict:
    return json.loads((proj_dir / "ignored_terms.json").read_text(encoding="utf-8"))


class TestGet:
    def test_absent_file_is_an_empty_list_not_an_error(self, client, project):
        r = client.get(URL)
        assert r.status_code == 200
        assert r.get_json() == {"ok": True, "terms": []}

    def test_rows_carry_the_hides_count(self, client, project):
        _write_evaluation(project, [_dict_issue("Deum", 10), _dict_issue("Deum", 40)])
        client.post(URL, json={"term": "Deum", "eval_name": "dictionary"})
        rows = client.get(URL).get_json()["terms"]
        assert len(rows) == 1
        assert rows[0]["term"] == "Deum"
        assert rows[0]["hides"] == 2
        assert rows[0]["dismissed"] == 0

    def test_hides_excludes_hand_dismissed_findings_and_reports_them_apart(
        self, client, project
    ):
        """``hides`` promises what a removal restores, so a dismissal is not in it.

        The panel needs the second number anyway: without it a term you had
        already dismissed everywhere reads as dormant, and the row invites a
        removal that would leave nothing suppressing it once an edit
        invalidates the dismissal's key.
        """
        first = _dict_issue("Mirandy", 10)
        _write_evaluation(project, [first, _dict_issue("Mirandy", 40)])
        _write_feedback(project, [first])
        client.post(URL, json={"term": "Mirandy", "eval_name": "dictionary"})
        row = client.get(URL).get_json()["terms"][0]
        assert row["hides"] == 1
        assert row["dismissed"] == 1

    def test_flags_a_term_the_glossary_already_covers(self, client, project):
        """A redundant entry -- the glossary suppresses it before the evaluator
        ever produces a finding -- so the panel says so rather than showing a
        silent zero."""
        (project / "glossary.json").write_text(
            json.dumps({
                "terms": [{"english": "Richard", "spanish": "Ricardo",
                           "type": "character", "alternatives": []}],
                "version": "1.0",
            }),
            encoding="utf-8",
        )
        client.post(URL, json={"term": "Ricardo", "eval_name": "dictionary"})
        client.post(URL, json={"term": "Pecquigny", "eval_name": "dictionary"})
        rows = {r["term"]: r for r in client.get(URL).get_json()["terms"]}
        assert rows["Ricardo"]["in_glossary"] is True
        assert rows["Pecquigny"]["in_glossary"] is False

    def test_bad_project_id_is_rejected(self, client, project):
        # A traversal id answers 404 from Werkzeug's URL normalization before
        # the view is entered, so asserting `in (400, 404)` on one would pass
        # with `_safe_id` deleted from all three routes. Use an id that routes
        # cleanly and can only be stopped by the guard, and pin the 400.
        assert client.get("/api/project/a$b/ignored-terms").status_code == 400
        assert client.post(
            "/api/project/a$b/ignored-terms",
            json={"term": "x", "eval_name": "dictionary"},
        ).status_code == 400
        assert client.delete(
            "/api/project/a$b/ignored-terms",
            json={"term": "x", "eval_name": "dictionary"},
        ).status_code == 400

    def test_missing_project_is_404(self, client, project):
        assert client.get("/api/project/nope/ignored-terms").status_code == 404


class TestAdd:
    def test_writes_the_sidecar(self, client, project):
        r = client.post(URL, json={
            "term": "Pecquigny",
            "eval_name": "dictionary",
            "added_from": "chapter_04_chunk_001",
            "note": "French place name",
        })
        assert r.get_json() == {"ok": True, "added": True, "total": 1}
        entry = _stored(project)["terms"][0]
        assert entry["term"] == "Pecquigny"
        assert entry["added_from"] == "chapter_04_chunk_001"
        assert entry["note"] == "French place name"
        assert entry["added_at"]

    def test_idempotent_across_case(self, client, project):
        client.post(URL, json={"term": "Pecquigny", "eval_name": "dictionary"})
        r = client.post(URL, json={"term": "PECQUIGNY", "eval_name": "dictionary"})
        assert r.get_json() == {"ok": True, "added": False, "total": 1}
        assert len(_stored(project)["terms"]) == 1

    def test_grammar_requires_a_rule_id(self, client, project):
        """A word-only grammar ignore would silence every rule on that token."""
        r = client.post(URL, json={"term": "el", "eval_name": "grammar"})
        assert r.status_code == 400
        assert "rule_id" in r.get_json()["error"]
        assert not (project / "ignored_terms.json").exists()

    def test_grammar_with_a_rule_id_is_accepted(self, client, project):
        r = client.post(URL, json={
            "term": "aun", "eval_name": "grammar", "rule_id": "AUN",
        })
        assert r.get_json()["added"] is True
        assert _stored(project)["terms"][0]["rule_id"] == "AUN"

    def test_same_word_under_two_rules_are_separate_entries(self, client, project):
        client.post(URL, json={"term": "aun", "eval_name": "grammar", "rule_id": "AUN"})
        client.post(URL, json={"term": "aun", "eval_name": "grammar", "rule_id": "AUN2"})
        assert len(_stored(project)["terms"]) == 2

    def test_rule_id_is_dropped_for_non_grammar(self, client, project):
        """Spelling is keyed on the word alone; a stray rule would split the key."""
        client.post(URL, json={
            "term": "Deum", "eval_name": "dictionary", "rule_id": "SOMETHING",
        })
        assert _stored(project)["terms"][0]["rule_id"] is None

    def test_rejects_an_unignorable_evaluator(self, client, project):
        r = client.post(URL, json={"term": "x", "eval_name": "address"})
        assert r.status_code == 400

    def test_rejects_a_blank_term(self, client, project):
        assert client.post(URL, json={"term": "   ", "eval_name": "dictionary"}).status_code == 400

    def test_rejects_an_empty_body(self, client, project):
        assert client.post(URL, json={}).status_code == 400


class TestRemove:
    def test_removes_and_reports_the_count(self, client, project):
        client.post(URL, json={"term": "Deum", "eval_name": "dictionary"})
        r = client.delete(URL, json={"term": "Deum", "eval_name": "dictionary"})
        assert r.get_json() == {"ok": True, "removed": 1, "total": 0}
        assert _stored(project)["terms"] == []

    def test_removal_is_case_insensitive(self, client, project):
        client.post(URL, json={"term": "Pecquigny", "eval_name": "dictionary"})
        r = client.delete(URL, json={"term": "pecquigny", "eval_name": "dictionary"})
        assert r.get_json()["removed"] == 1

    def test_removes_only_the_named_rule(self, client, project):
        client.post(URL, json={"term": "aun", "eval_name": "grammar", "rule_id": "AUN"})
        client.post(URL, json={"term": "aun", "eval_name": "grammar", "rule_id": "AUN2"})
        client.delete(URL, json={"term": "aun", "eval_name": "grammar", "rule_id": "AUN"})
        remaining = _stored(project)["terms"]
        assert [t["rule_id"] for t in remaining] == ["AUN2"]

    def test_removing_something_absent_is_not_an_error(self, client, project):
        r = client.delete(URL, json={"term": "nope", "eval_name": "dictionary"})
        assert r.status_code == 200
        assert r.get_json()["removed"] == 0

    def test_requires_term_and_eval_name(self, client, project):
        assert client.delete(URL, json={"term": "x"}).status_code == 400
        assert client.delete(URL, json={"eval_name": "dictionary"}).status_code == 400


class TestUnreadableList:
    """An unreadable file is a conflict, not an empty list.

    The read path deliberately degrades to "nothing is ignored" so a malformed
    file cannot blank the review queue. Reusing that loader on the write path
    was destructive: every add and remove rewrites the file wholesale, so a
    single tap would have replaced the reviewer's whole list with the one entry
    in hand and reported success.
    """

    def test_add_refuses_and_leaves_the_file_untouched(self, client, project):
        path = project / "ignored_terms.json"
        path.write_text('{"version": 1, "terms": [', encoding="utf-8")
        before = path.read_bytes()

        r = client.post(URL, json={"term": "Deum", "eval_name": "dictionary"})

        assert r.status_code == 409
        assert r.get_json()["error"]
        assert path.read_bytes() == before, "the unreadable list must survive"

    def test_remove_refuses_rather_than_reporting_a_false_no_op(
        self, client, project
    ):
        path = project / "ignored_terms.json"
        path.write_text("not json at all", encoding="utf-8")

        r = client.delete(URL, json={"term": "Deum", "eval_name": "dictionary"})

        assert r.status_code == 409
        assert "removed" not in r.get_json()

    def test_an_unknown_schema_version_is_a_conflict_not_a_wipe(
        self, client, project
    ):
        path = project / "ignored_terms.json"
        path.write_text(
            json.dumps({"version": 2, "terms": [], "future_field": "kept"}),
            encoding="utf-8",
        )
        before = path.read_bytes()

        r = client.post(URL, json={"term": "Deum", "eval_name": "dictionary"})

        assert r.status_code == 409
        assert path.read_bytes() == before


class TestGhostEntries:
    """`identity()` fills the rule slot for grammar only, like `matches()`.

    Keyed unconditionally, a non-grammar entry carrying a ``rule_id`` -- from a
    hand-edited file, a restored backup, or a future client -- was suppressed by
    `matches` but unreachable by the removal route, which nulls the rule slot
    for everything but grammar. It could never be cleared through the UI.
    """

    def test_a_dictionary_entry_carrying_a_rule_id_is_still_removable(
        self, client, project
    ):
        (project / "ignored_terms.json").write_text(
            json.dumps({
                "version": 1,
                "terms": [{
                    "term": "Deum",
                    "eval_name": "dictionary",
                    "rule_id": "MORFOLOGIK",
                }],
            }),
            encoding="utf-8",
        )

        r = client.delete(URL, json={"term": "Deum", "eval_name": "dictionary"})

        assert r.get_json() == {"ok": True, "removed": 1, "total": 0}
        assert _stored(project)["terms"] == []

    def test_it_is_counted_as_hiding_what_it_actually_hides(
        self, client, project
    ):
        _write_evaluation(project, [_dict_issue("Deum", 10)])
        (project / "ignored_terms.json").write_text(
            json.dumps({
                "version": 1,
                "terms": [{
                    "term": "Deum",
                    "eval_name": "dictionary",
                    "rule_id": "MORFOLOGIK",
                }],
            }),
            encoding="utf-8",
        )

        # Was `hides: 0`, which the dashboard renders as "safe to remove" for an
        # entry that is in fact suppressing a live finding.
        assert client.get(URL).get_json()["terms"][0]["hides"] == 1


class TestPersistenceFailure:
    """Every route on this blueprint answers in JSON, including when it fails."""

    def test_a_failed_save_is_a_json_500_not_an_html_traceback(
        self, client, project, monkeypatch
    ):
        import web_ui.app as app_module

        def boom(*_a, **_k):
            raise OSError("No space left on device")

        monkeypatch.setattr(app_module, "save_ignored_terms", boom)

        r = client.post(URL, json={"term": "Deum", "eval_name": "dictionary"})

        assert r.status_code == 500
        assert "No space left on device" in r.get_json()["error"]

    def test_the_same_on_remove(self, client, project, monkeypatch):
        import web_ui.app as app_module

        client.post(URL, json={"term": "Deum", "eval_name": "dictionary"})

        def boom(*_a, **_k):
            raise OSError("read-only file system")

        monkeypatch.setattr(app_module, "save_ignored_terms", boom)

        r = client.delete(URL, json={"term": "Deum", "eval_name": "dictionary"})

        assert r.status_code == 500
        assert r.get_json()["error"]


class TestEvalCardFlag:
    """The per-chunk card marks ignored findings rather than hiding them.

    It is the surface where the reviewer looks at what the checker actually
    found, so dropping the row would leave the Review stage's lower count with
    no visible explanation.
    """

    def test_findings_are_flagged_not_filtered(self, client, project):
        _write_evaluation(project, [_dict_issue("Deum", 10)])
        client.post(URL, json={"term": "Deum", "eval_name": "dictionary"})

        payload = client.get(
            "/api/project/proj1/evaluations/chapter_01_chunk_000"
        ).get_json()

        issues = payload["normalized_issues"]
        assert len(issues) == 1, "the finding must still be listed"
        assert issues[0]["ignored"] is True

    def test_an_unignored_finding_is_not_flagged(self, client, project):
        _write_evaluation(project, [_dict_issue("Deum", 10)])
        client.post(URL, json={"term": "Pecquigny", "eval_name": "dictionary"})

        payload = client.get(
            "/api/project/proj1/evaluations/chapter_01_chunk_000"
        ).get_json()

        assert payload["normalized_issues"][0]["ignored"] is False


class TestNoRerunNeeded:
    """The whole point of filtering at read time rather than in the evaluator."""

    def test_add_then_remove_restores_the_count_without_re_evaluating(
        self, client, project
    ):
        _write_evaluation(project, [_dict_issue("Deum", 10), _dict_issue("Deum", 40)])
        before = json.loads(
            (project / "evaluations" / "chapter_01_chunk_000.json").read_text(
                encoding="utf-8"
            )
        )

        client.post(URL, json={"term": "Deum", "eval_name": "dictionary"})
        assert client.get(URL).get_json()["terms"][0]["hides"] == 2

        client.delete(URL, json={"term": "Deum", "eval_name": "dictionary"})
        assert client.get(URL).get_json()["terms"] == []

        after = json.loads(
            (project / "evaluations" / "chapter_01_chunk_000.json").read_text(
                encoding="utf-8"
            )
        )
        assert before == after, "the stored evaluation must never be rewritten"
