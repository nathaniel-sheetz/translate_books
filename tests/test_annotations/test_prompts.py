"""Prompt rendering: the cache split, and the per-type framing of the reader's note."""

from __future__ import annotations

from src.annotations import prompts as annprompts
from src.annotations.targets import AnnotationTarget
from src.judges.base import _CACHE_PREFIX_SPLIT_MARKER


def _target(ann_type="word_choice", **kw):
    base = dict(
        key="chapter_01__1__u1",
        chapter_id="chapter_01",
        es_idx=1,
        sub_id="u1",
        ann_type=ann_type,
        content="",
        es_sentence="El poyo estaba frío.",
        en_sentence="The stone bench was cold.",
    )
    base.update(kw)
    return AnnotationTarget(**base)


def _context():
    return {
        "target_language": "Spanish",
        "style_guide": "REGISTER\nPlain.",
        "glossary": "- oyster → ostión",
    }


def test_every_type_has_a_template():
    from src.annotations.store import ANNOTATION_TYPES

    for ann_type in ANNOTATION_TYPES:
        assert ann_type in annprompts.TEMPLATES
        assert annprompts.template_version(ann_type)


def test_prompt_splits_on_the_cache_marker():
    prefix, body = annprompts.build_prompt_parts(_target(), _context())
    assert prefix and body
    assert _CACHE_PREFIX_SPLIT_MARKER not in prefix + body
    assert prefix + body == annprompts.build_prompt(_target(), _context())


def test_preamble_is_target_independent():
    """What makes it cacheable: two annotations of a type share the prefix exactly."""
    a, _ = annprompts.build_prompt_parts(_target(content="poyo"), _context())
    b, _ = annprompts.build_prompt_parts(
        _target(content="otra cosa", es_idx=2, key="chapter_01__2__u2"), _context()
    )
    assert a == b


def test_style_guide_and_glossary_live_above_the_split():
    """The style guide alone misses Sonnet's 1024-token floor; the glossary is
    what carries the preamble over it."""
    prefix, body = annprompts.build_prompt_parts(_target(), _context())
    assert "REGISTER" in prefix
    assert "oyster → ostión" in prefix
    assert "REGISTER" not in body


def test_glossary_hits_are_capped_in_the_prompt_body():
    """Matched hits must not bypass MAX_GLOSSARY_TERMS via limit=len(hits)."""
    hits = [
        {
            "english": f"term-{i}",
            "spanish": f"término-{i}",
            "type": "noun",
            "context": "",
            "alternatives": [],
        }
        for i in range(annprompts.MAX_GLOSSARY_TERMS + 50)
    ]
    rendered = annprompts._format_glossary_hits(hits)
    assert rendered.count("→") == annprompts.MAX_GLOSSARY_TERMS
    assert "further terms not listed" in rendered


def test_word_choice_hint_uses_the_questioned_vs_proposed_framing():
    _, present = annprompts.build_prompt_parts(
        _target(content="poyo", hint="poyo", hint_in_sentence=True), _context()
    )
    assert "the wording the reader is questioning" in present

    _, absent = annprompts.build_prompt_parts(
        _target(content="banco", hint="banco", hint_in_sentence=False), _context()
    )
    assert "a replacement the reader is proposing" in absent


def test_footnote_hint_is_never_framed_as_a_proposed_replacement():
    """Regression: a gloss is *expected* not to appear in the sentence.

    The type-blind framing told footnote workers that an absent hint was a
    proposed replacement, which made them rewrite finished glosses
    ("Himno cristiano por George Washington Doane (1848)") instead of leaving
    them alone.
    """
    _, body = annprompts.build_prompt_parts(
        _target(
            ann_type="footnote",
            content="Himno cristiano por George Washington Doane (1848)",
            hint="Himno cristiano por George Washington Doane (1848)",
            hint_in_sentence=False,
        ),
        _context(),
    )
    assert "a replacement the reader is proposing" not in body
    assert "already-written gloss" in body
    assert "already_resolved" in body


def test_flag_hint_is_not_framed_as_a_replacement_either():
    _, body = annprompts.build_prompt_parts(
        _target(ann_type="flag", content="puntuación", hint="puntuación"), _context()
    )
    assert "a replacement the reader is proposing" not in body


def test_blank_note_is_described_as_blank():
    _, body = annprompts.build_prompt_parts(_target(content=""), _context())
    assert "only the marked span, or nothing at all" in body


def test_multi_anchor_is_announced_to_the_model():
    _, body = annprompts.build_prompt_parts(
        _target(ann_type="footnote", anchors=["a", "b", "c"]), _context()
    )
    assert "Marked spans (3)" in body


def test_no_anchor_says_the_note_covers_the_sentence():
    _, body = annprompts.build_prompt_parts(_target(anchors=[]), _context())
    assert "points at the sentence as a whole" in body


def test_footnote_template_names_short_glosses_as_resolved():
    """The second half of the same regression, in the template itself."""
    template = annprompts.load_template(annprompts.template_for("footnote"))
    assert "A short gloss is not an unfinished one" in template
    assert "Pequeño sin nombre" in template


def test_unknown_type_falls_back_to_the_flag_template():
    assert annprompts.template_for("something_new") == annprompts.TEMPLATES["flag"]


def test_context_defaults_to_spanish_without_a_harness_config(tmp_path):
    project = tmp_path / "bare"
    project.mkdir()
    context = annprompts.build_context(project)
    assert context["target_language"] == "Spanish"
    assert "no style guide" in context["style_guide"]
