from __future__ import annotations

import json

import pytest

from src.book_review import evidence as ev
from src.book_review import progress, schema, synthesize
from src.book_review.apply import apply, parse_selection
from src.book_review.signals import run_all
from tests.test_book_review.conftest import Book, issue, pair, term


@pytest.fixture
def book(tmp_path, monkeypatch):
    import src.book_review.corpus as corpus_mod

    b = Book(tmp_path)
    monkeypatch.setattr(corpus_mod, "_repo_root", lambda: b.repo)
    found = issue("glossary-porte-cochere", "El glosario fija «la cochera cubierta».",
                  "bajo el porte-cochere", "bajo la cochera cubierta", category="STYLE_GUIDE",
                  finding_key="pc1")
    (b.glossary([term("porte cochere", "la cochera cubierta", context="carriage porch")])
     .style("FORMS OF ADDRESS\nThe owl keeps a familiar tú with Bambi.\n\nUNITS\nKeep miles.")
     .address_map([pair("Bambi", "the owl", "usted", "tú")])
     .chunk("chapter_01_chunk_000", "First paragraph.\n\nThey stopped under the porte-cochere.",
            "Primer párrafo.\n\nSe detuvieron bajo el porte-cochere.", shipped=[])
     .judge("chapter_01_chunk_000", "editorial", [found]))
    return b


def draft_payload(**overrides):
    rec = {
        "title": "Rekey 'porte cochere' to 'porte-cochere'",
        "rationale": "The key never matches the hyphenated source.",
        "evidence": ["D1:porte cochere", "editorial:chapter_01_chunk_000:kept:0", "made-up"],
        "confidence": "high",
        "priority": 1,
        "patches": [{"type": "glossary.rekey", "english": "porte cochere",
                     "new_english": "porte-cochere"}],
        "follow_ups": [{"type": "retranslate", "chapters": ["chapter_01"]}],
    }
    rec.update(overrides)
    return {"recommendations": [rec], "no_action": [{"signal_id": "D3:x", "reason": "fine"}]}


def write_drafts(project_dir, payload):
    manifest = json.loads(synthesize._manifest_path(project_dir).read_text(encoding="utf-8"))
    for job in manifest["jobs"]:
        theme_payload = payload if job["theme"] == "glossary" else {"recommendations": []}
        open(job["draft_path"], "w", encoding="utf-8").write(json.dumps(theme_payload))


# ---------------------------------------------------------------------------
# evidence


def test_english_for_finds_the_same_paragraph(book):
    corpus = book.load()
    chunk = corpus.chunks["chapter_01_chunk_000"]
    assert ev.english_for(chunk, "bajo el porte-cochere") == "They stopped under the porte-cochere."
    assert ev.english_for(chunk, "nowhere") is None


def test_tier_a_item_carries_findings_english_and_the_glossary_entry(book):
    corpus = book.load()
    built = ev.build(corpus, run_all(corpus))
    glossary = next(t for t in built["themes"] if t["theme"] == "glossary")
    item = glossary["jobs"][0]["items"][0]
    assert item["tier"] == "A"
    assert item["findings"][0]["english"] == "They stopped under the porte-cochere."
    assert item["artifacts"]["glossary"][0]["context"] == "carriage porch"


def test_budget_demotes_and_records(book):
    corpus = book.load()
    built = ev.build(corpus, run_all(corpus), job_cap=50, book_cap=60)
    assert built["omitted"], "a tiny budget must demote something"
    assert {o["to"] for o in built["omitted"]} <= {"B", "C"}


# ---------------------------------------------------------------------------
# schema


def check(book, patch):
    corpus = book.load()
    return schema.check_patch(patch, corpus, {f.id for f in corpus.findings},
                              {c.chapter_id for c in corpus.chunks.values()})


def test_rekey_to_a_key_that_never_occurs_is_rejected(book):
    clean, why = check(book, {"type": "glossary.rekey", "english": "porte cochere",
                              "new_english": "porte cochère"})
    assert clean is None and "never occurs" in why


def test_rekey_is_case_insensitive_on_the_existing_key(book):
    clean, why = check(book, {"type": "glossary.rekey", "english": "Porte Cochere",
                              "new_english": "porte-cochere"})
    assert clean == {"type": "glossary.rekey", "english": "porte cochere", "new_english": "porte-cochere"}


def test_style_before_must_be_verbatim_once(book):
    assert check(book, {"type": "style.replace_text", "before": "not there", "after": "x"})[0] is None
    clean, _ = check(book, {"type": "style.replace_text",
                            "before": "The owl keeps a familiar tú with Bambi.",
                            "after": "Bambi says usted to the owl; the owl answers tú."})
    assert clean is not None


def test_address_direction_follows_a_swapped_pair(book):
    clean, _ = check(book, {"type": "address.set_direction", "a": "the owl", "b": "Bambi",
                            "direction": "b_to_a", "form": "tu"})
    assert clean["a"] == "Bambi" and clean["direction"] == "a_to_b" and clean["form"] == "tú"


def test_dismiss_needs_known_kept_findings(book):
    assert check(book, {"type": "judge.dismiss", "finding_ids": ["nope"]})[0] is None


def test_recommendation_drops_unknown_evidence_and_rejects_patchless(book):
    corpus = book.load()
    known = {"D1:porte cochere"} | {f.id for f in corpus.findings}
    rec, problems = schema.check_recommendation(draft_payload()["recommendations"][0],
                                                "glossary", corpus, known)
    assert rec["evidence"] == ["D1:porte cochere", "editorial:chapter_01_chunk_000:kept:0"]
    assert any("made-up" in p for p in problems)
    rec, _ = schema.check_recommendation(
        {"title": "Nothing", "patches": [{"type": "style.replace_text", "before": "zzz", "after": ""}]},
        "rules", corpus, known)
    assert rec is None


def test_reconcile_marks_two_recommendations_on_one_key():
    recs = [{"id": "R-1", "patches": [{"type": "glossary.edit", "english": "X", "spanish": "a"}]},
            {"id": "R-2", "patches": [{"type": "glossary.remove", "english": "x"}]}]
    schema.reconcile(recs)
    assert recs[0]["conflicts_with"] == ["R-2"] and recs[1]["conflicts_with"] == ["R-1"]


# ---------------------------------------------------------------------------
# prepare -> fanout -> commit -> apply


def test_prepare_writes_jobs_inside_the_stage_dir(book):
    out = synthesize.prepare(book.dir, cli="claude")
    assert out["status"] == "ok" and out["jobs"] >= 1
    manifest = json.loads(synthesize._manifest_path(book.dir).read_text(encoding="utf-8"))
    root = synthesize.synthesis_dir(book.dir).resolve()
    for job in manifest["jobs"]:
        assert open(job["body_path"], encoding="utf-8").read().startswith("EVIDENCE (JSON):")
        assert root in __import__("pathlib").Path(job["draft_path"]).resolve().parents
    preamble = open(manifest["preamble_path"], encoding="utf-8").read()
    assert "The owl keeps a familiar tú with Bambi." in preamble


def test_fanout_runs_the_jobs_with_a_fake_runner(book):
    synthesize.prepare(book.dir, cli="claude")

    def runner(cmd, *, input_text, cwd, **kwargs):
        assert input_text.startswith("EVIDENCE (JSON):")
        return 0, json.dumps({"recommendations": []}), ""

    out = synthesize.fanout(book.dir, runner=runner, concurrency=1)
    assert out["counts"]["wrote"] >= 1 and not out["failed"]


def test_commit_validates_and_writes_the_report(book):
    synthesize.prepare(book.dir, cli="claude")
    write_drafts(book.dir, draft_payload())
    out = synthesize.commit(book.dir, today="2026-10-20")
    assert out["recommendations"] == 1 and out["by_priority"][1] == 1
    doc = json.loads(synthesize.recommendations_path(book.dir).read_text(encoding="utf-8"))
    rec = doc["recommendations"][0]
    assert rec["id"].startswith("R-") and rec["targets"][0] and "precondition" not in rec
    report = open(out["report_path"], encoding="utf-8").read()
    assert "rekey `porte cochere` → `porte-cochere`" in report
    assert progress.load_state(book.dir)["kind"] == "synthesis"


def test_apply_dry_run_writes_nothing_then_yes_saves_with_history(book):
    synthesize.prepare(book.dir, cli="claude")
    write_drafts(book.dir, draft_payload())
    synthesize.commit(book.dir)
    rid = json.loads(synthesize.recommendations_path(book.dir).read_text(encoding="utf-8"))[
        "recommendations"][0]["id"]
    before = (book.dir / "glossary.json").read_text(encoding="utf-8")

    dry = apply(book.dir, [rid])
    assert dry["status"] == "dry_run" and "porte-cochere" in dry["diffs"]["glossary"]
    assert (book.dir / "glossary.json").read_text(encoding="utf-8") == before

    done = apply(book.dir, [rid], yes=True)
    assert done["status"] == "ok" and done["saved"] == ["glossary"]
    keys = [t["english"] for t in json.loads((book.dir / "glossary.json").read_text(encoding="utf-8"))["terms"]]
    assert keys == ["porte-cochere"]
    history = [json.loads(l) for l in (book.dir / "decisions_history.jsonl").read_text(encoding="utf-8").splitlines()]
    assert history[-1]["writer"] == "book_review" and history[-1]["reason"] == rid
    offer = next(a for a in done["actions"] if a["type"] == "retranslate")
    assert "retranslate" in offer["command"] and "chapter_01_chunk_000" in offer["command"]


def test_apply_refuses_when_the_artifact_changed(book):
    synthesize.prepare(book.dir, cli="claude")
    write_drafts(book.dir, draft_payload())
    synthesize.commit(book.dir)
    rid = json.loads(synthesize.recommendations_path(book.dir).read_text(encoding="utf-8"))[
        "recommendations"][0]["id"]
    book.glossary([term("porte cochere", "otra cosa")])
    out = apply(book.dir, [rid], yes=True)
    assert out["status"] == "error" and "changed since the review" in out["error"]


def test_dismiss_writes_keyed_feedback(book):
    synthesize.prepare(book.dir, cli="claude")
    fid = "editorial:chapter_01_chunk_000:kept:0"
    write_drafts(book.dir, draft_payload(
        title="Dismiss the porte-cochere finding", follow_ups=[],
        patches=[{"type": "judge.dismiss", "finding_ids": [fid]}]))
    synthesize.commit(book.dir)
    rid = json.loads(synthesize.recommendations_path(book.dir).read_text(encoding="utf-8"))[
        "recommendations"][0]["id"]
    out = apply(book.dir, [rid], yes=True)
    assert out["actions"][0]["status"] == "done" and out["actions"][0]["marked"] == 1
    [finding] = [f for f in book.load().findings if f.id == fid]
    assert finding.mark == "false_positive"



def committed(book, **overrides):
    """Prepare, write one glossary-theme draft and commit it; the recommendation's id."""
    synthesize.prepare(book.dir, cli="claude")
    write_drafts(book.dir, draft_payload(**overrides))
    synthesize.commit(book.dir)
    return json.loads(synthesize.recommendations_path(book.dir).read_text(encoding="utf-8"))[
        "recommendations"][0]["id"]


def test_dismiss_skips_a_finding_whose_chunk_was_rejudged(book):
    fid = "editorial:chapter_01_chunk_000:kept:0"
    rid = committed(book, title="Dismiss the porte-cochere finding", follow_ups=[],
                    patches=[{"type": "judge.dismiss", "finding_ids": [fid]}])
    other = issue("units", "Keep miles.", "diez kilómetros", "diez millas", finding_key="u1")
    book.judge("chapter_01_chunk_000", "editorial", [other])
    out = apply(book.dir, [rid], yes=True)
    action = out["actions"][0]
    assert action["marked"] == 0 and action["skipped_changed"] == [fid]
    assert not (book.dir / "evaluations" / "_feedback.jsonl").exists()


def test_apply_twice_is_refused_and_appends_once(book):
    rid = committed(book, title="Note the porch", follow_ups=[],
                    patches=[{"type": "style.append", "text": "PORCH\nSay cochera."}])
    assert apply(book.dir, [rid], yes=True)["status"] == "ok"
    after_first = (book.dir / "style.json").read_text(encoding="utf-8")
    again = apply(book.dir, [rid], yes=True)
    assert again["status"] == "error" and "already applied" in again["error"]
    assert (book.dir / "style.json").read_text(encoding="utf-8") == after_first


def test_apply_refuses_bad_change_numbers_without_logging(book):
    rid = committed(book)
    out = apply(book.dir, [f"{rid}.9"], yes=True)
    assert out["status"] == "error" and "no change numbered [9]" in out["error"]
    bad = apply(book.dir, [f"{rid}.abc"], yes=True)
    assert bad["status"] == "error" and "not a selection" in bad["error"]
    assert not (book.dir / ".harness" / "book_review" / "applied.jsonl").exists()


def test_apply_validates_every_artifact_before_saving_any(book, monkeypatch):
    import src.book_review.apply as apply_mod

    rid = committed(book, patches=[
        {"type": "glossary.rekey", "english": "porte cochere", "new_english": "porte-cochere"},
        {"type": "style.append", "text": "PORCH\nSay cochera."},
    ])
    real = apply_mod._validate

    def failing(name, doc):
        if name == "style":
            raise ValueError("bad style guide")
        return real(name, doc)

    monkeypatch.setattr(apply_mod, "_validate", failing)
    before = (book.dir / "glossary.json").read_text(encoding="utf-8")
    out = apply(book.dir, [rid], yes=True)
    assert out["status"] == "error" and "bad style guide" in out["error"]
    assert (book.dir / "glossary.json").read_text(encoding="utf-8") == before


def test_apply_stamps_updated_at(book):
    book.style("FORMS OF ADDRESS\nThe owl keeps a familiar tú with Bambi.\n\nUNITS\nKeep miles.",
               updated_at="2025-01-01T00:00:00")
    rid = committed(book, follow_ups=[],
                    patches=[{"type": "style.append", "text": "PORCH\nSay cochera."}])
    assert apply(book.dir, [rid], yes=True)["status"] == "ok"
    style = json.loads((book.dir / "style.json").read_text(encoding="utf-8"))
    assert style["updated_at"] > "2025-01-02"


def test_prepare_with_no_themes_drops_the_old_manifest(book, monkeypatch):
    committed(book)
    monkeypatch.setattr(synthesize.ev, "build", lambda corpus, signals: {"themes": []})
    out = synthesize.prepare(book.dir, cli="claude")
    assert out["jobs"] == 0 and not synthesize._manifest_path(book.dir).exists()
    assert synthesize.commit(book.dir)["status"] == "error"

def test_parse_selection():
    assert parse_selection(["R-1,R-2.3", "R-2.1"]) == {"R-1": None, "R-2": {1, 3}}


def test_later_patches_are_validated_against_earlier_ones(book):
    corpus = book.load()
    raw = {"title": "Rekey then add a variant", "patches": [
        {"type": "glossary.rekey", "english": "porte cochere", "new_english": "porte-cochere"},
        {"type": "glossary.add_variant", "english": "porte-cochere", "new_english": "porte"},
    ]}
    rec, problems = schema.check_recommendation(raw, "glossary", corpus, set())
    assert [p["type"] for p in rec["patches"]] == ["glossary.rekey", "glossary.add_variant"], problems


def test_address_signals_from_any_detector_share_one_job():
    from src.book_review.evidence import _theme_of_signal

    assert _theme_of_signal({"detector": "recurring_rules", "metrics": {
        "level": "rule", "name": "forms-of-address-usted", "judge": "editorial"}}) == "address_style"
    assert _theme_of_signal({"detector": "rule_drift", "metrics": {
        "kind": "artifact_after_translation", "artifact": "address map"}}) == "address_style"
    assert _theme_of_signal({"detector": "false_positive_clusters", "metrics": {
        "theme": "address", "label": "judge_miscalibrated"}}) == "address_style"
    assert _theme_of_signal({"detector": "recurring_rules", "metrics": {
        "level": "rule", "name": "calque-syntax", "judge": "editorial"}}) == "rules"


def test_budget_shrinks_generic_clusters_before_conflicts(book):
    corpus = book.load()
    signals = {"signals": [
        {"id": "D3:x", "detector": "address_style_conflicts", "title": "t", "score": 1.0,
         "finding_ids": [], "artifact_refs": [], "metrics": {}, "tier_hint": "A"},
        {"id": "D5:y", "detector": "recurring_rules", "title": "t", "score": 50.0,
         "finding_ids": ["editorial:chapter_01_chunk_000:kept:0"] * 40, "artifact_refs": [],
         "metrics": {"name": "calque-x", "level": "rule"}, "tier_hint": "A"},
    ]}
    ev.MIN_DEMOTE_TOKENS, saved = 0, ev.MIN_DEMOTE_TOKENS
    try:
        built = ev.build(corpus, signals, job_cap=200, book_cap=10_000)
    finally:
        ev.MIN_DEMOTE_TOKENS = saved
    assert built["omitted"][0]["signal_id"] == "D5:y"


def test_add_pair_validates_and_applies(book):
    from src.book_review.patches import apply_address

    clean, why = check(book, {"type": "address.add_pair", "a": "la ardilla", "b": "la madre",
                              "a_to_b": "tu", "b_to_a": "tú"})
    assert clean and clean["a_to_b"] == "tú", why
    assert check(book, {"type": "address.add_pair", "a": "Bambi", "b": "the owl",
                        "a_to_b": "tú", "b_to_a": "tú"})[0] is None
    doc = {"pairs": []}
    apply_address(doc, clean)
    assert doc["pairs"][0]["directions"]["b_to_a"][0]["form"] == "tú"


def test_overlapping_dismissals_are_conflicts():
    recs = [{"id": "R-1", "patches": [{"type": "judge.dismiss", "finding_ids": ["f1", "f2"]}]},
            {"id": "R-2", "patches": [{"type": "judge.dismiss", "finding_ids": ["f2"]}]}]
    schema.reconcile(recs)
    assert recs[0]["conflicts_with"] == ["R-2"]
