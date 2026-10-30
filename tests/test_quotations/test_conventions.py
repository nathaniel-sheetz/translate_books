"""The conventions, on Reina-Valera 1909 verses and made-up strings.

No NBLA text in a tracked test: the verses below are Reina-Valera 1909 (public
domain) or invented. ``test_nbla_local.py`` runs the real NBLA cases and skips
when there is no index.
"""

from src.quotations.conventions import (
    fills_paragraph,
    is_sentence_initial,
    locate,
    lower_line_starts,
    render_quotation,
    sentence_case_caps,
    set_first_capital,
    splice,
    split_wrappers,
    strip_unbalanced_quotes,
)

# Reina-Valera 1909, Psalm 23:1-2, with the line markers the index writes.
PS23 = [
    ("19:023:001", "\nJehová es mi pastor;\nNada me faltará."),
    ("19:023:002", "\nEn lugares de delicados pastos me hará yacer:\nJunto á aguas de reposo me pastoreará."),
]
# Reina-Valera 1909, John 10:11 (prose, no markers).
JN10 = [("43:010:011", "Yo soy el buen pastor: el buen pastor su vida da por las ovejas.")]

PROPER = frozenset({"jerusalén", "israel", "dios", "jehová"})


def test_locate_finds_a_piece_across_verses_with_its_line_starts():
    loc = locate(PS23, "Nada me faltará. En lugares de delicados pastos")
    assert loc is not None
    assert loc.text == "Nada me faltará. En lugares de delicados pastos"
    # "Nada" opens a line (offset 0); "En" begins verse 2's first line.
    assert loc.line_starts == [0, 17]
    assert loc.verse_keys == ["19:023:001", "19:023:002"]


def test_locate_is_verbatim():
    assert locate(PS23, "Nada me faltara") is None  # accent dropped
    assert locate(PS23, "nada me faltará") is not None  # first-letter case only
    assert locate(PS23, "Nada  me\nfaltará") is not None  # whitespace only


def test_inline_lowers_line_starts_but_not_after_a_full_stop():
    loc = locate(PS23, "Jehová es mi pastor; Nada me faltará. En lugares de delicados pastos me hará yacer: Junto á aguas de reposo me pastoreará.")
    out = render_quotation([loc], inline=True, sentence_initial=True, proper=PROPER)
    assert out == (
        "Jehová es mi pastor; nada me faltará. En lugares de delicados pastos "
        "me hará yacer: junto á aguas de reposo me pastoreará."
    )


def test_block_quotation_keeps_line_capitals():
    loc = locate(PS23, "Jehová es mi pastor; Nada me faltará.")
    out = render_quotation([loc], inline=False, sentence_initial=True, proper=PROPER)
    assert out == "Jehová es mi pastor; Nada me faltará."


def test_line_start_lowering_never_touches_names_reverentials_or_divine_name():
    text = "Él es Rey; Jerusalén se alegra; Tu nombre; SEÑOR de todos; Mas yo canto"
    starts = [text.index(w) for w in ("Jerusalén", "Tu", "SEÑOR", "Mas")]
    out = lower_line_starts(text, starts, PROPER)
    assert out == "Él es Rey; Jerusalén se alegra; Tu nombre; SEÑOR de todos; mas yo canto"


def test_capitals_off_line_starts_are_never_touched():
    loc = locate([("01:001:001", "\nVino a Israel,\nY a Jerusalén con Su gloria.")], "Vino a Israel, Y a Jerusalén con Su gloria.")
    out = render_quotation([loc], inline=True, sentence_initial=True, proper=PROPER)
    assert out == "Vino a Israel, y a Jerusalén con Su gloria."


def test_first_capital_follows_the_sentence():
    assert set_first_capital("yo soy el buen pastor", sentence_initial=True) == "Yo soy el buen pastor"
    assert set_first_capital("Corazón engañoso", sentence_initial=False) == "corazón engañoso"
    assert set_first_capital("Su nombre", sentence_initial=False) == "Su nombre"
    assert set_first_capital("SEÑOR, oye", sentence_initial=False) == "SEÑOR, oye"
    assert set_first_capital("¿Quién midió?", sentence_initial=True) == "¿Quién midió?"


def test_all_caps_run_goes_to_sentence_case_divine_name_kept():
    out = sentence_case_caps("ESTOY ALEGRE Y CONFIADO ante el SEÑOR.", PROPER)
    assert out == "Estoy alegre y confiado ante el SEÑOR."
    assert sentence_case_caps("Oh Señor DIOS, Tú eres grande") == "Oh Señor DIOS, Tú eres grande"
    assert sentence_case_caps("ASÍ DICE EL SEÑOR A ISRAEL", PROPER) == "Así dice el SEÑOR a Israel"


def test_unbalanced_speech_marks_are_dropped():
    assert strip_unbalanced_quotes("no oyen el canto de los extraños»") == "no oyen el canto de los extraños"
    assert strip_unbalanced_quotes("dijo: «Sea el día». Y hubo día") == "dijo: «Sea el día». Y hubo día"
    assert strip_unbalanced_quotes("“Padre nuestro") == "Padre nuestro"


def test_splice_keeps_wrappers_and_the_books_end_punctuation():
    assert splice("«Pero al forastero no escucharán».", "Pero al viajero no escucharán,") == (
        "«Pero al viajero no escucharán»."
    )
    # Epigraph italics.
    assert splice("_Yo soy el buen Pastor._", "Yo soy el buen pastor;") == "_Yo soy el buen pastor._"
    # The NBLA's own question stays a question, whatever the book had.
    assert splice("«Quién midió las aguas.»", "¿Quién midió las aguas?") == "«¿Quién midió las aguas?»"
    # A fragment of a question that opened before the quoted words is a statement.
    assert splice("«Ustedes no caminan solos.»", "ustedes no caminan solos?") == (
        "«ustedes no caminan solos.»"
    )
    # A "¡" the NBLA does not have goes.
    assert splice("«¡Pidan, y se les dará!»", "Pidan, y se les dará.") == "«Pidan, y se les dará»"


def test_split_wrappers():
    assert split_wrappers("«Yo soy el pastor».") == ("«", "Yo soy el pastor", "».")
    assert split_wrappers("_Texto._") == ("_", "Texto", "._")


def test_is_sentence_initial():
    assert is_sentence_initial("")
    assert is_sentence_initial("Jesús dijo: «")
    assert is_sentence_initial("Lo dijo. ")
    assert not is_sentence_initial("y su «")
    assert not is_sentence_initial("porque ")


def test_fills_paragraph_is_block_vs_inline():
    assert fills_paragraph("_Yo soy el buen pastor._—Jn 10:11", "_Yo soy el buen pastor._")
    assert fills_paragraph("«Yo soy el buen pastor». (Juan 10:11)", "«Yo soy el buen pastor».")
    assert not fills_paragraph("Jesús dijo: «Yo soy el buen pastor».", "«Yo soy el buen pastor»")


def test_two_pieces_join_and_the_second_reads_as_a_line_start():
    a = locate(PS23, "Jehová es mi pastor;")
    b = locate(JN10, "El buen pastor su vida da por las ovejas.")
    assert b is not None  # first letter swapped from "el"
    out = render_quotation([a, b], inline=True, sentence_initial=True, proper=PROPER)
    assert out == "Jehová es mi pastor; el buen pastor su vida da por las ovejas."


# ── fixes found on the first short-sermons mapping run (made-up verses) ──────

GEN = [("01:001:003", "Entonces dijo Dios: «Sea el día». Y hubo día.")]
PS_A = [
    ("19:023:004", "\nTu luz y Tu palabra me guardan."),
    ("19:023:005", "\nHas llenado mi mesa de pan;\nMi casa está en paz."),
]


def test_contiguous_pieces_keep_the_nbla_punctuation_between_them():
    a = locate(GEN, "Sea el día")
    b = locate(GEN, "Y hubo día.")
    out = render_quotation([a, b], inline=True, sentence_initial=True, old="Sea el día. Y hubo día.")
    assert out == "Sea el día. Y hubo día."


def test_skipped_words_take_the_books_ellipsis_or_separator():
    a = locate(PS23, "Jehová es mi pastor")
    b = locate(PS23, "Junto á aguas de reposo me pastoreará.")
    assert render_quotation([a, b], inline=True, sentence_initial=True,
                            old="Jehová es mi pastor… junto a aguas") == (
        "Jehová es mi pastor… junto á aguas de reposo me pastoreará."
    )
    assert render_quotation([a, b], inline=True, sentence_initial=True,
                            old="Jehová es mi pastor; junto a aguas") == (
        "Jehová es mi pastor; junto á aguas de reposo me pastoreará."
    )


def test_line_initial_pronoun_follows_the_verses_own_usage():
    loc = locate(PS_A, "Has llenado mi mesa de pan; Mi casa está en paz.")
    out = render_quotation([loc], inline=True, sentence_initial=True)
    assert out == "Has llenado mi mesa de pan; mi casa está en paz."
    loc = locate(PS_A, "Tu luz y Tu palabra me guardan. Has llenado")
    assert render_quotation([loc], inline=True, sentence_initial=True).startswith("Tu luz y Tu palabra")


def test_sentence_start_looks_through_a_parenthetical_reference():
    assert is_sentence_initial("…a quien todo le es conocido.» (He 4:13) «")
    assert not is_sentence_initial("y dijo que (según Juan) «")


def test_no_doubled_opening_marks():
    assert splice("¿Acaso soy Yo un Rey de un día?", "¿acaso soy Yo un Rey solo de un día?") == (
        "¿acaso soy Yo un Rey solo de un día?"
    )
    # A question only at the end gets its "¿" there, not at the start.
    assert splice("«El siervo sirve a su señor. ¿Dónde está Mi pan?»",
                  "El siervo sirve a su señor. Pues si Yo soy amo, ¿dónde está Mi pan?") == (
        "«El siervo sirve a su señor. Pues si Yo soy amo, ¿dónde está Mi pan?»"
    )


def test_caps_fallback_is_skipped_for_an_index_that_read_small_caps():
    loc = locate([("02:003:014", "Y dijo Dios: «YO SOY EL QUE SOY»")], "YO SOY EL QUE SOY")
    assert render_quotation([loc], inline=True, sentence_initial=True, fix_caps=False) == "YO SOY EL QUE SOY"


def test_block_quote_follows_the_books_own_line_capitals():
    loc = locate(PS23, "Jehová es mi pastor; Nada me faltará.")
    assert render_quotation([loc], inline=False, sentence_initial=True,
                            old="Jehová es mi pastor; nada me faltará.") == "Jehová es mi pastor; nada me faltará."
    assert render_quotation([loc], inline=False, sentence_initial=True,
                            old="Jehová es mi pastor; Nada me faltará.") == "Jehová es mi pastor; Nada me faltará."


def test_ellipsis_join_replaces_the_pieces_end_punctuation():
    a = locate(PS23, "Jehová es mi pastor; Nada me faltará.")
    b = locate(PS23, "Junto á aguas de reposo me pastoreará.")
    out = render_quotation([a, b], inline=True, sentence_initial=True, old="Jehová… junto")
    assert out == "Jehová es mi pastor; nada me faltará… junto á aguas de reposo me pastoreará."


def test_a_capital_after_a_speech_mark_is_lowered_mid_sentence():
    verses = [("24:023:023", "«¿Soy Yo un Rey de un día», declara el SEÑOR, «Y no un Rey de siempre?")]
    a = locate(verses, "¿Soy Yo un Rey de un día")
    b = locate(verses, ", declara el SEÑOR, ")
    c = locate(verses, "Y no un Rey de siempre?")
    out = render_quotation([a, b, c], inline=True, sentence_initial=True, old="x")
    assert out == "¿Soy Yo un Rey de un día, declara el SEÑOR, y no un Rey de siempre?"
