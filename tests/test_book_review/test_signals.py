from __future__ import annotations

import json

import pytest

from src.book_review.corpus import parse_shipped_glossary, theme_of
from src.book_review.signals import (
    address_style_conflicts,
    dead_glossary_keys,
    false_positive_clusters,
    loose_variants,
    recurring_rules,
)
from tests.test_book_review.conftest import Book, issue, logged_prompt, pair, term


@pytest.fixture
def book(tmp_path):
    return Book(tmp_path)


def by_key(signals):
    return {s.id: s for s in signals}


# ---------------------------------------------------------------------------
# corpus


def test_parse_shipped_glossary_reads_only_the_glossary_block():
    prompt = logged_prompt([("porte-cochere", "la cochera"), ("Nancy", "Nancy")])
    prompt += "\n- not → a glossary line\n"
    assert parse_shipped_glossary(prompt) == {"porte-cochere", "nancy"}


def test_theme_reads_the_rule_not_the_message():
    # "sustituto" contains "tut"; a message quoting «usted» must not make a calque an address finding
    assert theme_of("calque-substitute-for", "NATURALNESS", "«Para que usted haga» calca") == "calque"
    assert theme_of("forms-of-address-usted", "STYLE_GUIDE", "") == "address"
    assert theme_of("(none)", None, "Glosario: usa X") == "glossary"


def test_marks_join_by_issue_key(book):
    found = issue("glossary-x", "El glosario fija «x».", "y", "x", finding_key="k1")
    book.chunk("chapter_01_chunk_000", "text").judge("chapter_01_chunk_000", "editorial", [found])
    book.mark("chapter_01_chunk_000", "editorial", found, "applied")
    [finding] = book.load().findings
    assert finding.mark == "applied"


def test_retracted_and_filtered_findings_are_read_with_their_status(book):
    book.chunk("chapter_01_chunk_000", "text").judge(
        "chapter_01_chunk_000", "editorial", [],
        retracted=[{"rule": "calque-x", "excerpt": "a", "reason": "fine"}],
        filtered=[{"rule": "calque-y", "excerpt": "b", "message": "m", "drop_reason": "over_budget"}],
    )
    statuses = sorted(f.status for f in book.load().findings)
    assert statuses == ["filtered:over_budget", "retracted"]


# ---------------------------------------------------------------------------
# D1 dead glossary keys


def glossary_finding(spanish: str, wrong: str) -> dict:
    return issue("glossary-entry", f"El glosario fija «{spanish}».", wrong, spanish, category="STYLE_GUIDE")


@pytest.mark.parametrize("key, source, reason", [
    ("porte cochere", "under the porte-cochere it stood", "punctuation"),
    ("'Lias", "and ’Lias ran home", "punctuation"),
    ("stalk (mushroom)", "the stalk of the mushroom", "parenthetical"),
    ("the old stag / the old Prince", "then the old stag spoke", "slash"),
    ("party", "two parties met", "plural_ies"),
])
def test_dead_key_reasons(book, key, source, reason):
    book.glossary([term(key, "algo")]).chunk("chapter_01_chunk_000", source, shipped=[])
    [signal] = dead_glossary_keys(book.load())
    assert signal.metrics["state"] == "dead"
    assert signal.metrics["reasons"] == {reason: 1}


def test_blind_spot_counts_findings_on_entries_the_prompt_lacked(book):
    found = glossary_finding("la cochera cubierta", "el porte-cochere")
    (book.glossary([term("porte cochere", "la cochera cubierta")])
     .chunk("chapter_01_chunk_000", "under the porte-cochere", shipped=[])
     .judge("chapter_01_chunk_000", "editorial", [found]))
    [signal] = dead_glossary_keys(book.load())
    assert signal.metrics["blind_spot_findings"] == 1
    assert signal.tier_hint == "A"


def test_shipped_entry_is_not_reported(book):
    (book.glossary([term("porte-cochere", "la cochera")])
     .chunk("chapter_01_chunk_000", "under the porte-cochere", shipped=[("porte-cochere", "la cochera")]))
    assert dead_glossary_keys(book.load()) == []


def test_logged_prompt_overrides_recomputation(book):
    # The key matches the text as written, but the prompt the chunk was translated
    # with did not carry it: the entry came later.
    (book.glossary([term("bay", "castaño")])
     .chunk("chapter_01_chunk_000", "the bay gelding", shipped=[]))
    [signal] = dead_glossary_keys(book.load())
    assert signal.metrics["state"] == "added_after_translation"


def test_without_a_log_the_filter_is_recomputed(book):
    book.glossary([term("bay", "castaño")]).chunk("chapter_01_chunk_000", "the bay gelding")
    assert dead_glossary_keys(book.load()) == []


def test_head_noun_needs_a_corroborating_finding(book):
    book.glossary([term("prairie dog", "perrito llanero")])
    book.chunk("chapter_01_chunk_000", "the dogs barked", shipped=[])
    assert [s.id for s in dead_glossary_keys(book.load())] == ["D1:absent"]

    found = glossary_finding("perrito llanero", "perros de la pradera")
    book.judge("chapter_01_chunk_000", "editorial", [found])
    [signal] = dead_glossary_keys(book.load())
    assert signal.metrics["reasons"] == {"head_noun": 1}
    assert signal.metrics["blind_spot_findings"] == 1


def test_sibling_entry_with_same_spanish_covers_the_chunk(book):
    found = glossary_finding("el juego de alegrarse", "el juego")
    (book.glossary([term("the game", "el juego de alegrarse"), term("glad game", "el juego de alegrarse")])
     .chunk("chapter_01_chunk_000", "the game was fun", shipped=[("the game", "el juego de alegrarse")])
     .judge("chapter_01_chunk_000", "editorial", [found]))
    # "glad game" is simply absent; what matters is that no finding is pinned on it.
    assert [s.id for s in dead_glossary_keys(book.load())] == ["D1:absent"]


def test_finding_belongs_to_the_entry_it_quotes_longest(book):
    found = glossary_finding("el juego de alegrarse", "el juego")
    (book.glossary([term("being glad", "alegrarse"), term("the game", "el juego de alegrarse")])
     .chunk("chapter_01_chunk_000", "the game of being glad", shipped=[("the game", "el juego de alegrarse"), ("being glad", "alegrarse")])
     .judge("chapter_01_chunk_000", "editorial", [found]))
    assert dead_glossary_keys(book.load()) == []


def test_absent_keys_are_one_low_signal(book):
    book.glossary([term("Geneva", "Ginebra")]).chunk("chapter_01_chunk_000", "nothing here", shipped=[])
    [signal] = dead_glossary_keys(book.load())
    assert signal.id == "D1:absent"
    assert signal.metrics["keys"] == ["Geneva"]


def test_loose_variants_order_strong_to_weak():
    reasons = [r for _, r in loose_variants("prairie dog")]
    assert reasons[0] == "punctuation" and reasons[-1] == "head_noun"


# ---------------------------------------------------------------------------
# D3 address map vs style guide


def test_style_clause_against_map(book):
    (book.style("FORMS OF ADDRESS\nThe owl keeps a familiar tú with Bambi at any age.")
     .address_map([pair("Bambi", "the owl", "usted", "tú")]))
    signals = address_style_conflicts(book.load())
    [signal] = [s for s in signals if s.id.startswith("D3:text")]
    assert signal.metrics["map_forms"] == {"Bambi→the owl": ["usted"]}


def test_coordinated_subject_is_not_a_pair(book):
    (book.style("Paul and Maureen use usted with their grandparents.")
     .address_map([pair("Paul", "Maureen", "tú", "tú")]))
    assert address_style_conflicts(book.load()) == []


def test_global_rule_exception_against_pair(book):
    (book.style("")
     .address_map([pair("John Pendleton", "Pollyanna", "tú", "usted")],
                  global_rules="tú DOWN the ladder; UNLESS (John Pendleton ceremoniously uses usted "
                               "even with the child Pollyanna)"))
    [signal] = address_style_conflicts(book.load())
    assert signal.metrics["source"] == "address map global_rules"
    assert signal.metrics["map_forms"] == {"John Pendleton→Pollyanna": ["tú"]}


def test_judges_pulling_one_pair_apart(book):
    book.style("").address_map([pair("Aunt Polly", "Nancy", "tú", "usted")])
    address = issue("wrong-form-tu-expected", "Private kitchen, Miss Polly→Nancy: expected tú, found usted.")
    editorial = issue("forms-of-address-usted", "La señorita Polly tutea a Nancy; la guía exige usted.",
                      "cuando te hablo", "cuando le hablo", category="STYLE_GUIDE")
    (book.chunk("chapter_01_chunk_000", "text")
     .judge("chapter_01_chunk_000", "address", [address])
     .judge("chapter_01_chunk_000", "editorial", [editorial]))
    ids = by_key(address_style_conflicts(book.load()))
    assert "D3:judges:Aunt Polly→Nancy" in ids


def test_address_judge_against_its_own_pair(book):
    book.style("").address_map([pair("Pollyanna", "Nancy", "tú", "usted")])
    address = issue("wrong-form-usted-expected", "Station, Pollyanna→Nancy: expected usted (child to adult stranger), found tú.")
    book.chunk("chapter_03_chunk_000", "text").judge("chapter_03_chunk_000", "address", [address])
    ids = by_key(address_style_conflicts(book.load()))
    assert ids["D3:map:Pollyanna→Nancy"].metrics["map_forms"] == ["tú"]


def test_time_scoped_pair_is_not_checked_against_the_judge(book):
    scoped = {"a": "Pollyanna", "b": "Dr. Chilton", "directions": {
        "a_to_b": [{"form": "usted", "until": "chapter_31"}, {"form": "tú", "since": "chapter_31"}],
        "b_to_a": [{"form": "usted", "when": "default"}],
    }}
    book.style("").address_map([scoped])
    address = issue("wrong-form-tu-expected", "Reunion, Pollyanna→Dr. Chilton: expected tú, found usted.")
    book.chunk("chapter_31_chunk_000", "text").judge("chapter_31_chunk_000", "address", [address])
    assert address_style_conflicts(book.load()) == []


# ---------------------------------------------------------------------------
# D5 recurring rules, D7 false-positive clusters


def spread(book, judge, make, n, mark=None):
    for i in range(1, n + 1):
        chunk_id = f"chapter_{i:02d}_chunk_000"
        found = make(i)
        book.chunk(chunk_id, "text").judge(chunk_id, judge, [found])
        if mark:
            book.mark(chunk_id, judge, found, mark)


def test_accepted_style_rule_everywhere_is_resisted(book):
    spread(book, "editorial", lambda i: issue("glad-game-lock", f"Forma fija {i}", f"el juego {i}",
                                              "el juego de alegrarse", category="STYLE_GUIDE",
                                              finding_key=f"g{i}"), 4, mark="applied")
    signals = by_key(recurring_rules(book.load()))
    assert signals["D5:editorial:rule:glad-game-lock"].metrics["label"] == "resisted"


def test_dismissed_rule_everywhere_is_disputed_and_clusters(book):
    spread(book, "editorial", lambda i: issue("glossary-heb", f"Abreviatura {i}", "He 11:6", "Heb. 11:6",
                                              category="STYLE_GUIDE", finding_key=f"h{i}"), 4,
           mark="false_positive")
    corpus = book.load()
    assert by_key(recurring_rules(corpus))["D5:editorial:rule:glossary-heb"].metrics["label"] == "disputed"
    [cluster] = false_positive_clusters(corpus)
    assert cluster.metrics["label"] == "glossary_stale"


def test_rare_rule_is_not_recurring(book):
    spread(book, "editorial", lambda i: issue("calque-x", f"m{i}", finding_key=f"c{i}"), 2)
    assert recurring_rules(book.load()) == []


# ---------------------------------------------------------------------------
# CLI


def test_signals_cli_writes_report_and_json(book, monkeypatch, capsys):
    import scripts.book_review as cli
    import src.book_review.corpus as corpus_mod

    found = glossary_finding("la cochera cubierta", "el porte-cochere")
    (book.glossary([term("porte cochere", "la cochera cubierta")]).style("")
     .chunk("chapter_01_chunk_000", "under the porte-cochere", shipped=[])
     .judge("chapter_01_chunk_000", "editorial", [found]))
    monkeypatch.setattr(corpus_mod, "_repo_root", lambda: book.repo)
    assert cli.main(["signals", "--project", str(book.dir)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["headline"][0].startswith("Glossary entry 'porte cochere'")
    report = open(out["report_path"], encoding="utf-8").read()
    assert "never reached the translator" in report and "porte-cochere" in report
    assert json.loads(open(out["signals_path"], encoding="utf-8").read())["findings"] == 1


# ---------------------------------------------------------------------------
# D2 glossary sense, D4 rule drift, D6 opposite directions

from src.book_review.signals import glossary_sense, opposite_directions, rule_drift  # noqa: E402


def test_judge_replacing_a_shipped_rendering_is_a_sense_signal(book):
    found = issue("wrong-lexeme", "Balanzas nombra el instrumento de pesar, no las escamas.",
                  "Dos pobres balanzas", "Dos pobres escamas", category="NATURALNESS")
    (book.glossary([term("scales", "balanza", context="Weighing instrument, not escamas")])
     .chunk("chapter_60_chunk_000", "Two poor scales serve it for calyx.", shipped=[("scales", "balanza")])
     .judge("chapter_60_chunk_000", "editorial", [found]))
    [signal] = glossary_sense(book.load())
    assert signal.metrics["sense_note"] is True
    assert "Two poor scales" in signal.metrics["concordance"][0]


def test_dialogue_findings_on_chunks_translated_without_the_rules(book):
    found = issue("narration-separation", "Intro narration on its own line.", "a", "b")
    book.chunk("chapter_01_chunk_000", "text", shipped=[]).judge("chapter_01_chunk_000", "dialogue", [found])
    [signal] = [s for s in rule_drift(book.load(), current_dialogue="abc") if s.id.startswith("D4:dialogue")]
    assert signal.id == "D4:dialogue:none"
    assert signal.metrics["chunks"] == 1


def test_judge_prompt_version_split(book):
    for n, version in ((1, "aaaa1111"), (2, "bbbb2222")):
        chunk_id = f"chapter_{n:02d}_chunk_000"
        book.chunk(chunk_id, "text").judge(chunk_id, "dialogue", [issue("x", f"m{n}")],
                                           prompt_version=version)
    ids = by_key(rule_drift(book.load(), current_dialogue=""))
    assert set(ids["D4:judge-prompt:dialogue"].metrics["versions"]) == {"aaaa1111", "bbbb2222"}


def test_style_guide_changed_after_translation(book):
    book.style("Keep it plain.", updated_at="2026-08-01T00:00:00")
    book.chunk("chapter_01_chunk_000", "text").judge(
        "chapter_01_chunk_000", "editorial", [issue("calque-x", "m", finding_key="c1")])
    ids = by_key(rule_drift(book.load(), current_dialogue=""))
    assert ids["D4:artifact:style guide"].metrics["chunks_before"] == 1


def test_suggestions_that_undo_each_other(book):
    book.chunk("chapter_01_chunk_000", "text").judge(
        "chapter_01_chunk_000", "editorial", [issue("a", "m", "lo sabe", "lo sabes", finding_key="k1")])
    book.chunk("chapter_02_chunk_000", "text").judge(
        "chapter_02_chunk_000", "editorial", [issue("b", "m", "lo sabes", "lo sabe", finding_key="k2")])
    [signal] = opposite_directions(book.load())
    assert signal.metrics["forward"] == 1 and signal.metrics["backward"] == 1
