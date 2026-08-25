"""The feedback endpoint resolves the finding's content key server-side.

Deriving the key here rather than in the browser is what let all three marking
surfaces (dashboard card, reader Review Mode, mobile bottom sheet) stay
unchanged: they already send ``eval_name`` + ``issue_index``, and the server
turns that into a hash of the finding itself.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import web_ui.app as app_module  # noqa: E402
from web_ui.app import app  # noqa: E402
from web_ui.evaluations import issue_key  # noqa: E402


CODED_ISSUE = {
    "severity": "warning",
    "message": "'Bothon': Unknown word (found 1 time(s))",
    "location": "Character position 7551",
    "suggestion": "Possible misspelling.",
}
JUDGE_ISSUE = {
    "severity": "error",
    "message": "[wrong-form-tu-expected] Aunt Polly to Nancy.",
    "location": "espero que usted se encargue",
    "suggestion": "espero que te encargues",
}


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def project(tmp_path, monkeypatch):
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "test-project"
    (proj_dir / "evaluations").mkdir(parents=True)

    evaluation = {
        "chunk_id": "chapter_01_chunk_000",
        "results": [
            {
                "eval_name": "dictionary",
                "issues": [{"severity": "info", "message": "other"}, CODED_ISSUE],
            }
        ],
        "judges": {"address": {"eval_name": "address", "issues": [JUDGE_ISSUE]}},
    }
    (proj_dir / "evaluations" / "chapter_01_chunk_000.json").write_text(
        json.dumps(evaluation), encoding="utf-8"
    )
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


def post(client, **payload):
    return client.post(
        "/api/project/test-project/evaluations/chapter_01_chunk_000/feedback",
        json=payload,
    )


def only_record(project_dir):
    path = project_dir / "evaluations" / "_feedback.jsonl"
    return json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])


def test_key_resolved_for_a_coded_evaluator(client, project):
    resp = post(
        client, eval_name="dictionary", issue_index=1, feedback_type="false_positive"
    )
    assert resp.status_code == 200

    record = only_record(project)
    assert record["issue_key"] == issue_key("dictionary", CODED_ISSUE)
    assert record["issue_index"] == 1


def test_key_resolved_for_a_judge(client, project):
    """Judge issues live under ``judges{}``, not ``results[]``."""
    resp = post(client, eval_name="address", issue_index=0, feedback_type="resolved")
    assert resp.status_code == 200
    assert only_record(project)["issue_key"] == issue_key("address", JUDGE_ISSUE)


def test_out_of_range_index_still_records_the_mark(client, project):
    """A mark must never be lost because its key could not be derived -- it
    falls back to positional matching like every pre-existing record."""
    resp = post(
        client, eval_name="dictionary", issue_index=99, feedback_type="false_positive"
    )
    assert resp.status_code == 200

    record = only_record(project)
    assert record["issue_key"] is None
    assert record["issue_index"] == 99


def test_negative_index_does_not_key_the_last_finding(client, project):
    """Python indexing makes -1 resolve to the *end* of the list, so a negative
    index silently stamped the mark with a finding the user never touched."""
    resp = post(
        client, eval_name="dictionary", issue_index=-1, feedback_type="false_positive"
    )
    assert resp.status_code == 200

    record = only_record(project)
    assert record["issue_key"] is None
    assert record["issue_key"] != issue_key("dictionary", CODED_ISSUE)


def test_large_negative_index_does_not_500(client, project):
    """``_resolve_issue_key`` runs outside the route's try/except, so an
    IndexError there took down the request and lost the mark entirely."""
    resp = post(
        client, eval_name="dictionary", issue_index=-5, feedback_type="resolved"
    )
    assert resp.status_code == 200
    assert only_record(project)["issue_index"] == -5


def test_unknown_evaluator_still_records_the_mark(client, project):
    resp = post(client, eval_name="nosuch", issue_index=0, feedback_type="resolved")
    assert resp.status_code == 200
    assert only_record(project)["issue_key"] is None


def test_bad_feedback_type_is_still_rejected(client, project):
    resp = post(client, eval_name="dictionary", issue_index=1, feedback_type="bogus")
    assert resp.status_code == 400
    assert not (project / "evaluations" / "_feedback.jsonl").exists()


def test_resolved_is_accepted_from_the_dashboard_card(client, project):
    """The dashboard offered only three labels; 'resolved' now reaches the same
    endpoint the reader uses."""
    resp = post(client, eval_name="dictionary", issue_index=1, feedback_type="resolved")
    assert resp.status_code == 200
    assert only_record(project)["feedback_type"] == "resolved"
