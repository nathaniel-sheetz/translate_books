"""The change log written beside a book's style guide, glossary and address map."""

import json
from datetime import datetime

from src.models import AddressMap, Glossary, GlossaryTerm, StyleGuide
from src.utils import artifact_history
from src.utils.file_io import save_address_map, save_glossary, save_style_guide


def _glossary(*terms):
    return Glossary(terms=[GlossaryTerm(english=e, spanish=s, type="other") for e, s in terms])


def _style(content):
    now = datetime.now()
    return StyleGuide(content=content, version="1.0", created_at=now, updated_at=now)


def _address_map(form):
    return AddressMap.model_validate({
        "content": "Adults use usted.",
        "pairs": [{
            "a": "Tom", "b": "Mr. Moss",
            "directions": {"a_to_b": [{"form": form, "when": "default"}]},
        }],
        "global_rules": "Adults who are not intimates use usted.",
    })


def test_glossary_save_logs_added_removed_and_changed_terms(tmp_path):
    path = tmp_path / "glossary.json"
    save_glossary(_glossary(("spruce", "pícea"), ("elk", "alce")), path, writer="test")
    save_glossary(_glossary(("spruce", "abeto"), ("bay", "bayo")), path,
                  writer="test", reason="user prefers abeto")

    rows = artifact_history.read_history(tmp_path)
    assert [r["artifact"] for r in rows] == ["glossary", "glossary"]
    assert rows[0]["before"] is None

    diff = rows[1]["diff"]
    assert [t["english"] for t in diff["added"]] == ["bay"]
    assert [t["english"] for t in diff["removed"]] == ["elk"]
    assert diff["changed"] == [
        {"english": "spruce", "field": "spanish", "before": "pícea", "after": "abeto"}
    ]
    assert rows[1]["reason"] == "user prefers abeto"
    assert rows[1]["writer"] == "test"


def test_resave_with_only_new_timestamps_logs_nothing(tmp_path):
    path = tmp_path / "style.json"
    save_style_guide(_style("Use tú among children."), path)
    save_style_guide(_style("Use tú among children."), path)
    assert len(artifact_history.read_history(tmp_path)) == 1


def test_style_save_logs_a_text_diff(tmp_path):
    path = tmp_path / "style.json"
    save_style_guide(_style("Line one.\nUse rió."), path)
    save_style_guide(_style("Line one.\nUse rio."), path)
    diff = artifact_history.read_history(tmp_path)[-1]["diff"]
    assert "-Use rió." in diff["content"]
    assert "+Use rio." in diff["content"]


def test_address_map_save_logs_changed_pair(tmp_path):
    path = tmp_path / "address_map.json"
    save_address_map(_address_map("tú"), path)
    save_address_map(_address_map("usted"), path)
    diff = artifact_history.read_history(tmp_path)[-1]["diff"]
    assert [c["pair"] for c in diff["pairs_changed"]] == ["Tom <-> Mr. Moss"]
    assert diff["pairs_added"] == [] and diff["pairs_removed"] == []


def test_non_artifact_filenames_are_not_logged(tmp_path):
    save_glossary(_glossary(("elk", "alce")), tmp_path / "glossary_copy.json")
    assert not artifact_history.history_path(tmp_path).exists()


def test_hand_edit_is_caught_as_external(tmp_path):
    path = tmp_path / "style.json"
    save_style_guide(_style("Use tú."), path)

    data = json.loads(path.read_text(encoding="utf-8"))
    data["content"] = "Use usted."
    path.write_text(json.dumps(data), encoding="utf-8")

    rows = artifact_history.check_external_edits(tmp_path)
    assert [r["writer"] for r in rows] == ["external"]
    assert rows[0]["before"]["content"] == "Use tú."
    # Already logged: a second check finds nothing new.
    assert artifact_history.check_external_edits(tmp_path) == []


def test_a_hand_edit_before_a_save_gets_its_own_row(tmp_path):
    path = tmp_path / "style.json"
    save_style_guide(_style("Use tú."), path, writer="harness")

    data = json.loads(path.read_text(encoding="utf-8"))
    data["content"] = "Use usted."
    path.write_text(json.dumps(data), encoding="utf-8")

    save_style_guide(_style("Use vos."), path, writer="web_ui:style-guide")
    rows = artifact_history.read_history(tmp_path)
    assert [r["writer"] for r in rows] == ["harness", "external", "web_ui:style-guide"]
    assert (rows[1]["before"]["content"], rows[1]["after"]["content"]) == ("Use tú.", "Use usted.")
    assert rows[2]["before"]["content"] == "Use usted."


def test_first_save_over_an_untracked_file_seeds_a_baseline(tmp_path):
    path = tmp_path / "glossary.json"
    path.write_text(json.dumps(_glossary(("elk", "alce")).model_dump(mode="json")),
                    encoding="utf-8")
    save_glossary(_glossary(("elk", "alce"), ("bay", "bayo")), path, writer="test")

    rows = artifact_history.read_history(tmp_path)
    assert [r["writer"] for r in rows] == ["baseline", "test"]
    assert [t["english"] for t in rows[1]["diff"]["added"]] == ["bay"]
    assert rows[1]["diff"]["removed"] == []


def test_a_corrupt_byte_in_the_log_keeps_the_other_rows(tmp_path):
    save_style_guide(_style("Use tú."), tmp_path / "style.json")
    with artifact_history.history_path(tmp_path).open("ab") as f:
        f.write(b"\xff\xfe torn line\n")
    assert [r["artifact"] for r in artifact_history.read_history(tmp_path)] == ["style"]
    assert artifact_history.check_external_edits(tmp_path) == []


def test_web_ui_reason_must_be_a_string():
    from web_ui.app import _change_reason

    assert _change_reason({"reason": {"x": 1}}) is None
    assert _change_reason({"reason": "   "}) is None
    assert _change_reason({"reason": "  user prefers abeto "}) == "user prefers abeto"


def test_untracked_artifacts_get_a_baseline_row(tmp_path):
    (tmp_path / "glossary.json").write_text(
        json.dumps(_glossary(("elk", "alce")).model_dump(mode="json")), encoding="utf-8")
    rows = artifact_history.check_external_edits(tmp_path)
    assert [(r["artifact"], r["writer"]) for r in rows] == [("glossary", "baseline")]
    assert artifact_history.check_external_edits(tmp_path) == []


def test_a_broken_log_never_fails_the_save(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(artifact_history, "_append", boom)
    path = tmp_path / "glossary.json"
    save_glossary(_glossary(("elk", "alce")), path)
    assert path.exists()
