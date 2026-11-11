"""Tests for the [QUOTE] block marker: helpers, ingest, guard, builder, chunker,
evaluators and the pre-translation backfill.

The ingest fixtures are cut from real Gutenberg markup: home-geography
(pg12228) and stormy-misty-s-foal (pg67298).
"""

from bs4 import BeautifulSoup

from scripts.ingest_gutenberg import Converter
from src.chunker import _is_dialogue, _is_subchapter_heading, _score_split_points
from src.epub_builder import _render_body_blocks, detect_chapter_heading
from src.evaluators.paragraph_eval import ParagraphEvaluator
from src.harness_guard import guard_translation_draft
from src.models import Chunk, ChunkMetadata
from src import quotes as quotes_mod
from src.utils.text_utils import (
    blank_quote_markers,
    is_quote_block,
    quote_block_count,
    quote_instruction,
    quote_marker_ranges,
    quote_run_count,
    strip_block_markers,
    strip_quote_marker,
)


def _convert(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("body") or soup
    return Converter(base_url="", images_dir=None, download_images=False).convert(root)


def _blocks(text: str) -> list[str]:
    return [b.strip() for b in text.split("\n\n") if b.strip()]


def _chunk(source_text: str, translated_text: str | None = None) -> Chunk:
    return Chunk(
        id="ch01_chunk_001",
        chapter_id="chapter_01",
        position=1,
        source_text=source_text,
        translated_text=translated_text,
        metadata=ChunkMetadata(
            char_start=0, char_end=max(1, len(source_text)),
            overlap_start=0, overlap_end=0, paragraph_count=1,
            word_count=len(source_text.split()),
        ),
    )


# --------------------------------------------------------------------------- #
# text_utils helpers
# --------------------------------------------------------------------------- #

class TestHelpers:
    def test_only_a_block_leading_marker_counts(self):
        assert is_quote_block("[QUOTE] Querida Misty:")
        assert is_quote_block("  [QUOTE] indented")
        assert not is_quote_block("He typed [QUOTE] here.")

    def test_strip(self):
        assert strip_quote_marker("[QUOTE] «Querida Misty:") == "«Querida Misty:"
        assert strip_quote_marker("Plain.") == "Plain."
        assert strip_block_markers("[QUOTE] —Hola.") == "—Hola."

    def test_runs_not_blocks(self):
        text = "[QUOTE] A.\n\n[QUOTE] B.\n\nBody.\n\n[QUOTE] C."
        assert quote_block_count(text) == 3
        assert quote_run_count(text) == 2

    def test_merging_lines_inside_a_run_keeps_the_run_count(self):
        src = "[QUOTE] Oh, give me a home\n\n[QUOTE] Where the buffalo roam,"
        out = "[QUOTE] Oh, dame un hogar\ndonde el búfalo vaga,"
        assert quote_run_count(src) == quote_run_count(out) == 1

    def test_blank_preserves_offsets(self):
        text = "Body.\n\n[QUOTE] El ganso."
        blanked = blank_quote_markers(text)
        assert len(blanked) == len(text)
        assert "QUOTE" not in blanked
        assert quote_marker_ranges(text) == [(7, 15)]

    def test_strip_block_markers_either_order(self):
        assert strip_block_markers("[CAPTION] [QUOTE] x") == "x"
        assert strip_block_markers("[QUOTE] [CAPTION] x") == "x"
        assert strip_block_markers("[QUOTE]\n—x") == "—x"

    def test_blank_an_indented_marker(self):
        text = "  [QUOTE] x"
        blanked = blank_quote_markers(text)
        assert len(blanked) == len(text)
        assert "QUOTE" not in blanked and blanked.endswith("x")

    def test_instruction_gating(self):
        assert quote_instruction("No quotes here.") == ""
        assert "[QUOTE]" in quote_instruction("[QUOTE] A stanza.")
        assert quote_instruction("", always_include=True)


# --------------------------------------------------------------------------- #
# Gutenberg ingest
# --------------------------------------------------------------------------- #

class TestIngest:
    def test_stanza_lines_join_into_one_block(self):
        # pg67298: one <div class="verse"> per line. Before the marker, every
        # line came out as its own paragraph.
        html = (
            '<body><p>A cowboy\'s voice was throbbing:</p>'
            '<div class="poetry"><div class="stanza">'
            '<div class="verse">"Oh, give me a home</div>'
            '<div class="verse">Where the buffalo roam,</div>'
            '<div class="verse">And the deer and the antelope ..."</div>'
            '</div></div><p>The word "play" never came.</p></body>'
        )
        blocks = _blocks(_convert(html))
        assert blocks == [
            "A cowboy's voice was throbbing:",
            '[QUOTE] "Oh, give me a home\nWhere the buffalo roam,\n'
            'And the deer and the antelope ..."',
            'The word "play" never came.',
        ]

    def test_two_stanzas_stay_two_blocks_of_one_run(self):
        html = (
            '<body><div class="poetry">'
            '<div class="stanza"><div class="verse">One a</div><div class="verse">One b</div></div>'
            '<div class="stanza"><div class="verse">Two a</div><div class="verse">Two b</div></div>'
            '</div></body>'
        )
        text = _convert(html)
        assert _blocks(text) == ["[QUOTE] One a\nOne b", "[QUOTE] Two a\nTwo b"]
        assert quote_run_count(text) == 1

    def test_verse_div_of_paragraphs_becomes_one_stanza(self):
        # pg12228 "In the heart of a seed": div.verse > p per couplet, no <br>.
        html = (
            '<body><div class="verse">'
            '<p>In the heart of a seed buried deep, so deep, A\ndear little plant lay fast asleep.</p>'
            '<p>"Wake!" said the sunshine, "and creep to the light."</p>'
            '</div></body>'
        )
        assert _blocks(_convert(html)) == [
            "[QUOTE] In the heart of a seed buried deep, so deep, A dear little "
            'plant lay fast asleep.\n"Wake!" said the sunshine, "and creep to the light."'
        ]

    def test_p_stanzas_with_br_keep_their_stanza_break(self):
        html = '<body><div class="poem"><p>a1<br/>a2</p><p>b1<br/>b2</p></div></body>'
        assert _blocks(_convert(html)) == ["[QUOTE] a1\na2", "[QUOTE] b1\nb2"]

    def test_verse_lines_with_trailing_br_stay_one_stanza(self):
        html = (
            '<body><div class="stanza"><div class="verse">a<br/></div>'
            '<div class="verse">b<br/></div></div></body>'
        )
        assert _blocks(_convert(html)) == ["[QUOTE] a\nb"]

    def test_span_br_stanzas_stay_two_stanzas(self):
        html = (
            '<body><div class="poem">'
            '<div class="stanza"><span>a</span><br/><span>b</span><br/></div>'
            '<div class="stanza"><span>c</span><br/><span>d</span><br/></div>'
            '</div></body>'
        )
        assert _blocks(_convert(html)) == ["[QUOTE] a\nb", "[QUOTE] c\nd"]

    def test_container_with_a_nav_heading_is_not_a_quote(self):
        # The EPUB importer marks nav-resolved headings on ordinary elements.
        html = (
            '<body><blockquote><p data-ingest-level="1">Chapter One</p>'
            '<p>A short chapter wrapped in a blockquote.</p></blockquote></body>'
        )
        assert "[QUOTE]" not in _convert(html)

    def test_br_lines_inside_a_poem_paragraph(self):
        html = '<body><p class="poem">"Light of spirit, by my charms,<br/>Light of body, every part,</p></body>'
        assert _blocks(_convert(html)) == ['[QUOTE] "Light of spirit, by my charms,\nLight of body, every part,']

    def test_letter_keeps_its_paragraphs_and_italics(self):
        # pg67298 div.blockquot: a letter Grandma reads aloud.
        html = (
            '<body><div class="blockquot">'
            '<p>"<i>Dear Paul and Maureen,</i></p>'
            '<p>"<i>I am sorry the storm came.</i>"</p>'
            '</div></body>'
        )
        assert _blocks(_convert(html)) == [
            '[QUOTE] "_Dear Paul and Maureen,_',
            '[QUOTE] "_I am sorry the storm came._"',
        ]

    def test_rule_inside_a_container_splits_the_letters(self):
        html = (
            '<body><div class="blockquot">'
            '<p><i>Here is a check.</i></p><hr class="tb"/>'
            '<p><i>We had a lemonade stand.</i></p>'
            '</div></body>'
        )
        text = _convert(html)
        assert _blocks(text) == ["[QUOTE] _Here is a check._", "---", "[QUOTE] _We had a lemonade stand._"]
        assert quote_run_count(text) == 2

    def test_unclassed_br_verse_is_left_alone(self):
        # pg12228's other 48 stanzas; pg67298's dedication page has the same shape.
        html = '<body><p>Watch the pretty snowflakes fall,<br/>Some are large and some are small;</p></body>'
        assert "[QUOTE]" not in _convert(html)

    def test_container_with_a_heading_is_not_a_quote(self):
        html = '<body><blockquote><h2>CHAPTER I</h2><p>Text.</p></blockquote></body>'
        assert "[QUOTE]" not in _convert(html)

    def test_caption_inside_a_poem_stays_a_caption(self):
        html = (
            '<body><div class="poem"><p class="caption">A lark.</p>'
            '<p>Sing, sing,<br/>Throat-strong.</p></div></body>'
        )
        assert _blocks(_convert(html)) == ["[CAPTION] A lark.", "[QUOTE] Sing, sing,\nThroat-strong."]


# --------------------------------------------------------------------------- #
# harness_guard parity
# --------------------------------------------------------------------------- #

_SRC = (
    "Grandpa recited in a whisper:\n\n"
    "[QUOTE] \"A goose washin' in the horse trough\n\n"
    "[QUOTE] Means tomorrow we'll be bad off.\"\n\n"
    "Paul laughed at the old rhyme and went back out to the barn."
)


class TestGuard:
    def test_joined_lines_pass(self):
        out = (
            "El abuelo recitó en voz baja:\n\n"
            "[QUOTE] «Si el ganso se lava en el bebedero\nmañana nos irá muy mal».\n\n"
            "Paul se rio de la vieja rima y volvió al establo."
        )
        problems = guard_translation_draft(_chunk(_SRC), out)
        assert not any("quote-marker-parity" in p for p in problems)

    def test_dropped_passage_fails(self):
        out = (
            "El abuelo recitó en voz baja:\n\n"
            "«Si el ganso se lava en el bebedero\nmañana nos irá muy mal».\n\n"
            "Paul se rio de la vieja rima y volvió al establo."
        )
        problems = guard_translation_draft(_chunk(_SRC), out)
        assert any("quote-marker-parity" in p and "source has 1" in p for p in problems)

    def test_added_passage_fails(self):
        src = "The sun rose over the quiet village and the children ran out to play."
        out = "[QUOTE] El sol salió sobre el pueblo tranquilo y los niños salieron a jugar."
        problems = guard_translation_draft(_chunk(src), out)
        assert any("quote-marker-parity" in p for p in problems)

    def test_bare_marker_block_fails(self):
        out = (
            "El abuelo recitó en voz baja:\n\n[QUOTE]\n\n"
            "«Si el ganso se lava en el bebedero\nmañana nos irá muy mal».\n\n"
            "Paul se rio de la vieja rima y volvió al establo."
        )
        problems = guard_translation_draft(_chunk(_SRC), out)
        assert any("quote-marker-shape" in p and "stands alone" in p for p in problems)

    def test_marker_on_an_inner_line_fails(self):
        out = (
            "El abuelo recitó en voz baja:\n\n"
            "[QUOTE] «Si el ganso se lava en el bebedero\n[QUOTE] mañana nos irá muy mal».\n\n"
            "Paul se rio de la vieja rima y volvió al establo."
        )
        problems = guard_translation_draft(_chunk(_SRC), out)
        assert any("quote-marker-shape" in p and "inside a paragraph" in p for p in problems)

    def test_one_leading_marker_has_no_shape_problem(self):
        out = (
            "El abuelo recitó en voz baja:\n\n"
            "[QUOTE] «Si el ganso se lava en el bebedero\nmañana nos irá muy mal».\n\n"
            "Paul se rio de la vieja rima y volvió al establo."
        )
        assert not any("quote-marker-shape" in p for p in guard_translation_draft(_chunk(_SRC), out))


# --------------------------------------------------------------------------- #
# EPUB builder
# --------------------------------------------------------------------------- #

class TestBuilder:
    def test_prose_run_is_one_blockquote(self):
        out = _render_body_blocks("Escuchen:\n\n[QUOTE] «_Querida Misty:_\n\n[QUOTE] «_Adoro los caballos._»\n\nFin.")
        assert out[0] == "<p>Escuchen:</p>"
        assert out[1] == (
            '<blockquote class="quote">\n<p>«<em>Querida Misty:</em></p>\n'
            '<p>«<em>Adoro los caballos.</em>»</p>\n</blockquote>'
        )
        assert out[2] == "<p>Fin.</p>"

    def test_verse_keeps_its_lines_inside_the_quote(self):
        out = _render_body_blocks("[QUOTE] Oh, dame un hogar\ndonde el búfalo vaga,")
        assert out == [
            '<blockquote class="quote">\n<div class="verse">\n'
            '  <p class="verse-line">Oh, dame un hogar</p>\n'
            '  <p class="verse-line">donde el búfalo vaga,</p>\n</div>\n</blockquote>'
        ]

    def test_rule_splits_two_quotes(self):
        out = _render_body_blocks("[QUOTE] Uno.\n\n---\n\n[QUOTE] Dos.")
        assert [o.split("\n")[0] for o in out] == ['<blockquote class="quote">', "<hr/>", '<blockquote class="quote">']

    def test_image_line_inside_a_quote_renders_as_an_image(self):
        out = _render_body_blocks("[QUOTE] Mira:\n[IMAGE:images/x.png]")
        assert '<img src="images/x.png"' in out[0]
        assert "[IMAGE:" not in out[0]

    def test_text_opening_on_a_quote_has_no_heading_line(self):
        from src.epub_builder import _first_nonempty_line
        assert _first_nonempty_line("[QUOTE] Luz de espíritu\nluz de cuerpo") == ""
        assert _first_nonempty_line("[IMAGE:images/a.png]\nPrólogo\n\nTexto.") == "Prólogo"

    def test_quote_is_never_a_subtitle(self):
        _heading, subtitle, body = detect_chapter_heading(
            "Capítulo 1\n\n[QUOTE] Luz de espíritu\n\nTexto.", chapter_number=1
        )
        assert subtitle == ""
        assert body.startswith("[QUOTE] Luz de espíritu")


# --------------------------------------------------------------------------- #
# Chunker and paragraph evaluator
# --------------------------------------------------------------------------- #

class TestChunker:
    def test_dialogue_seen_through_the_marker(self):
        assert _is_dialogue('[QUOTE] "_Dear little Misty,_')

    def test_all_caps_quote_is_not_a_heading(self):
        assert not _is_subchapter_heading("[QUOTE] JESUS SAVIOUR PILOT ME")

    def test_split_inside_a_run_is_penalized(self):
        paras = ["Narration ends here.", "[QUOTE] Stanza one", "[QUOTE] Stanza two", "Narration again."]
        scores = _score_split_points(paras)
        assert scores[1] < scores[0]
        assert scores[1] < scores[2]


class TestParagraphEval:
    def test_merged_quote_lines_are_not_a_dropped_paragraph(self):
        chunk = _chunk(_SRC, translated_text=(
            "El abuelo recitó en voz baja:\n\n"
            "[QUOTE] «Si el ganso se lava en el bebedero\nmañana nos irá muy mal».\n\n"
            "Paul se rio de la vieja rima y volvió al establo."
        ))
        result = ParagraphEvaluator().evaluate(chunk, {})
        assert result.metadata["unexplained_delta"] == 0

    def test_raya_counted_through_the_marker(self):
        chunk = _chunk("A.\n\nB.", translated_text="[QUOTE] —Uno.\n\n—Dos.\n\n—Tres.")
        result = ParagraphEvaluator().evaluate(chunk, {})
        assert result.metadata["dialogue_paragraphs"] == 3


# --------------------------------------------------------------------------- #
# Pre-translation backfill (src/quotes.py)
# --------------------------------------------------------------------------- #

_PLAIN = (
    "The owl came out.\n\n"
    "    “I will sing a song. I’m the Owl.”\n\n"
    "    “Sing about the night.”\n\n"
    "Then the lark spoke.\n\n"
    "If the lake should disappear, they must find the hole.\n"
)


class TestBackfill:
    def test_indented_blocks_group_into_passages(self):
        found = quotes_mod.scan_text(_PLAIN)
        assert [(c.reason, c.passage) for c in found] == [("indented", 1), ("indented", 1)]

    def test_match_adds_an_unindented_block(self):
        found = quotes_mod.scan_text(_PLAIN, ["if the LAKE should disappear"])
        assert found[-1].reason.startswith("match:")
        assert found[-1].passage == 2

    def test_mark_replaces_the_indent(self):
        found = quotes_mod.scan_text(_PLAIN, ["lake should disappear"])
        out = quotes_mod.mark_text(_PLAIN, found)
        assert _blocks(out) == [
            "The owl came out.",
            "[QUOTE] “I will sing a song. I’m the Owl.”",
            "[QUOTE] “Sing about the night.”",
            "Then the lark spoke.",
            "[QUOTE] If the lake should disappear, they must find the hole.",
        ]
        # Idempotent: a marked block is no longer a candidate.
        assert quotes_mod.scan_text(out, ["lake should disappear"]) == []

    def test_apply_refuses_a_translated_book(self, tmp_path):
        import json
        import pytest
        (tmp_path / "source.txt").write_text(_PLAIN, encoding="utf-8")
        (tmp_path / "chunks").mkdir()
        (tmp_path / "chunks" / "chapter_01_chunk_000.json").write_text(
            json.dumps({"translated_text": "Ya traducido."}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="pre-translation only"):
            quotes_mod.apply_marks(tmp_path, quotes_mod.scan_project(tmp_path))
        assert (tmp_path / "source.txt").read_text(encoding="utf-8") == _PLAIN

    def test_apply_writes_source_and_backup(self, tmp_path):
        (tmp_path / "source.txt").write_text(_PLAIN, encoding="utf-8")
        report = quotes_mod.apply_marks(tmp_path, quotes_mod.scan_project(tmp_path))
        assert report == {"marked": 2, "passages": 1, "backup": "source.txt.bak-quotes"}
        assert (tmp_path / "source.txt.bak-quotes").read_text(encoding="utf-8") == _PLAIN
        assert quote_block_count((tmp_path / "source.txt").read_text(encoding="utf-8")) == 2

    def test_indented_headings_are_not_candidates(self):
        text = (
            "Intro.\n\n    CHAPTER I\n\nBody text.\n\n    The owl sang low,\n\n"
            "    The lark sang high.\n\n    II.\n\n    THE GIANT\n\nMore."
        )
        assert [c.text for c in quotes_mod.scan_text(text)] == [
            "The owl sang low,", "The lark sang high.",
        ]
        # A named --match still reaches a heading-shaped block.
        found = quotes_mod.scan_text(text, ["the giant"])
        assert found[-1].text == "THE GIANT" and found[-1].reason.startswith("match:")

    def test_harness_quotes_without_source_fails_clearly(self, tmp_path, monkeypatch):
        import pytest
        from src.harness import flow
        from src.harness_guard import HarnessValidationError
        monkeypatch.setattr(flow.state, "resolve_project_dir", lambda p: tmp_path)
        with pytest.raises(HarnessValidationError, match="no source.txt"):
            flow.quotes(str(tmp_path))


# --------------------------------------------------------------------------- #
# Audit context
# --------------------------------------------------------------------------- #

class TestAuditContext:
    def test_locate_sees_through_the_marker(self):
        from src.audit.context import locate
        text = "Narración.\n\n[QUOTE] «Si el ganso se lava en el bebedero\nmañana nos irá muy mal»."
        hit = locate(text, ["[QUOTE] «Si el ganso se lava en el bebedero"])
        assert hit is not None
        assert hit["starts_paragraph"] is True
