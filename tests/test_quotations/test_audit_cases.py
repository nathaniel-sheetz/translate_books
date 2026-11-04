"""The 2026-10-03 short-sermons audit's cases, on a stand-in index.

The repo is public and the NBLA never enters it, so the verses here are the
public-domain Reina-Valera 1909, or made up where the 1909 lacks what a case
needs (it prints "Jehová", never "SEÑOR"). They carry the index's markup: a
leading ``\\n`` marks a poetry line start.
"""

from src.quotations.conventions import locate, render_quotation
from src.quotations.index import BibleIndex
from src.quotations.refs import parse_ref

VERSES = {
    # Psalm 23:1, made up: a divine name in capitals.
    "19:023:001": "\nEl SEÑOR guarda mi senda;\nNada me faltará.",
    # Psalm 23:2, Reina-Valera 1909: two poetry lines.
    "19:023:002": "\nEn lugares de delicados pastos me hará yacer:\nJunto á aguas de reposo me pastoreará.",
    # John 10:11, Reina-Valera 1909: prose, with a name added to test proper words.
    "43:010:011": "Yo soy el buen pastor de Jerusalén: el buen pastor su vida da por las ovejas.",
}
INDEX = BibleIndex({"version": "nbla", "verses": VERSES, "proper_words": ["jerusalén"],
                    "smallcaps_resolved": True})


def _verses(ref: str):
    return [(k, INDEX.get(k)) for k in parse_ref(ref).verse_keys()]


def test_inline_quotation_lowers_the_second_line_start():
    loc = locate(_verses("Psalm 23:2"),
                 "En lugares de delicados pastos me hará yacer: Junto á aguas de reposo me pastoreará.")
    assert loc is not None
    out = render_quotation([loc], inline=True, sentence_initial=True, proper=INDEX.proper)
    assert "yacer: junto á aguas de reposo" in out


def test_senor_stays_in_capitals():
    loc = locate(_verses("Psalm 23:1"), "El SEÑOR guarda mi senda;")
    out = render_quotation([loc], inline=True, sentence_initial=True, proper=INDEX.proper)
    assert out.startswith("El SEÑOR guarda mi senda")


def test_common_nouns_stay_lower_case_and_names_keep_their_capital():
    loc = locate(_verses("John 10:11"), "Yo soy el buen pastor de Jerusalén:")
    assert loc is not None
    out = render_quotation([loc], inline=True, sentence_initial=True, proper=INDEX.proper)
    assert "buen pastor de Jerusalén" in out


def test_john_21_15_17_range():
    ref = parse_ref("John 21:15–17")
    assert ref.spanish() == "Jn 21:15–17"
    assert ref.verse_keys() == ["43:021:015", "43:021:016", "43:021:017"]
