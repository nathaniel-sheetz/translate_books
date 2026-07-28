"""Target building and the eligibility gates that run before any LLM call."""

from __future__ import annotations

from src.annotations import store
from src.annotations.targets import (
    MANUAL_MULTI_ANCHOR,
    SKIP_ALREADY_REVIEWED,
    SKIP_IMPORTED,
    SKIP_ORPHANED,
    build_targets,
)

from tests.test_annotations.conftest import write_annotations


def _ann(es_idx=0, **kw):
    base = {
        "project_id": "testbook",
        "chapter_id": "chapter_01",
        "es_idx": es_idx,
        "type": "word_choice",
        "content": "",
        "timestamp": "2026-01-01T00:00:00",
    }
    base.update(kw)
    return base


def test_gutenberg_imports_are_skipped(project):
    write_annotations(
        project,
        [_ann(type="footnote", content="[x] body", origin="gutenberg", sub_id="gb1")],
    )
    targets, skipped = build_targets(project)
    assert targets == []
    assert [s.reason for s in skipped] == [SKIP_IMPORTED]


def test_orphaned_es_idx_is_skipped(project):
    write_annotations(project, [_ann(es_idx=999, content="stranded", sub_id="u1")])
    targets, skipped = build_targets(project)
    assert targets == []
    assert [s.reason for s in skipped] == [SKIP_ORPHANED]


def test_already_reviewed_is_skipped_when_content_still_matches(project):
    content = "poyo\n— IA: correcto pero regional."
    write_annotations(
        project,
        [
            _ann(
                es_idx=1,
                content=content,
                sub_id="u1",
                ai_review={"written_content": content, "original_content": "poyo"},
            )
        ],
    )
    targets, skipped = build_targets(project)
    assert targets == []
    assert [s.reason for s in skipped] == [SKIP_ALREADY_REVIEWED]


def test_editing_a_reviewed_note_reopens_it(project):
    """A reader edit drops the sidecar, so the annotation becomes eligible again.

    POST /api/annotation rebuilds records from a fixed key set, which is what
    makes this the real behavior rather than a contrivance.
    """
    write_annotations(
        project,
        [
            _ann(
                es_idx=1,
                content="poyo\n— IA: correcto.",
                sub_id="u1",
                ai_review={"written_content": "poyo\n— IA: correcto."},
            ),
            # The reader's edit: same key, no sidecar, different text.
            _ann(
                es_idx=1,
                content="poyo — pero ¿y marco?",
                sub_id="u1",
                timestamp="2026-02-01T00:00:00",
            ),
        ],
    )
    targets, skipped = build_targets(project)
    assert [t.content for t in targets] == ["poyo — pero ¿y marco?"]
    assert skipped == []


def test_stale_sidecar_does_not_skip(project):
    """A sidecar whose written_content no longer matches must not gate the note."""
    write_annotations(
        project,
        [
            _ann(
                es_idx=1,
                content="edited by hand",
                sub_id="u1",
                ai_review={"written_content": "something else"},
            )
        ],
    )
    targets, _ = build_targets(project)
    assert len(targets) == 1


def test_multi_anchor_footnote_is_reviewed_but_withheld(project):
    write_annotations(
        project,
        [_ann(es_idx=2, type="footnote", content="[Sancerre]; [Esaú,]", sub_id="u1")],
    )
    targets, skipped = build_targets(project)
    assert skipped == []
    assert len(targets) == 1
    assert targets[0].manual_reason == MANUAL_MULTI_ANCHOR
    assert targets[0].is_writable is False


def test_single_anchor_footnote_is_writable(project):
    write_annotations(
        project, [_ann(es_idx=2, type="footnote", content="[Sancerre]", sub_id="u1")]
    )
    targets, _ = build_targets(project)
    assert targets[0].manual_reason is None
    assert targets[0].is_writable is True


def test_hint_in_sentence_distinguishes_questioned_from_proposed(project):
    """A bare word present in the sentence is questioned; absent, it's proposed."""
    write_annotations(
        project,
        [
            _ann(es_idx=1, content="poyo", sub_id="u1"),   # "El poyo estaba frío."
            _ann(es_idx=1, content="banco", sub_id="u2"),  # not in that sentence
        ],
    )
    targets, _ = build_targets(project)
    by_content = {t.content: t for t in targets}
    assert by_content["poyo"].hint_in_sentence is True
    assert by_content["banco"].hint_in_sentence is False


def test_hint_matching_is_accent_and_case_folded(project):
    write_annotations(project, [_ann(es_idx=0, content="OSTIoN", sub_id="u1")])
    targets, _ = build_targets(project)
    assert targets[0].hint_in_sentence is True


def test_context_and_provenance_are_attached(project):
    write_annotations(project, [_ann(es_idx=1, content="x", sub_id="u1")])
    targets, _ = build_targets(project)
    t = targets[0]
    assert t.es_sentence == "El poyo estaba frío."
    assert t.en_sentence == "The stone bench was cold."
    assert t.context_before == ["Comimos ostión en el puerto."]
    assert t.context_after[0] == "Nos fuimos a Sancerre."
    assert t.chunk_id == "chapter_01_chunk_000"


def test_glossary_hits_match_either_side(project):
    write_annotations(project, [_ann(es_idx=3, content="[ostra]", sub_id="u1")])
    targets, _ = build_targets(project)
    assert [g["spanish"] for g in targets[0].glossary_hits] == ["ostión"]


def test_type_and_chapter_filters(project):
    write_annotations(
        project,
        [
            _ann(es_idx=0, type="word_choice", sub_id="u1"),
            _ann(es_idx=1, type="footnote", content="[poyo]", sub_id="u2"),
        ],
    )
    targets, _ = build_targets(project, types=["footnote"])
    assert [t.ann_type for t in targets] == ["footnote"]
    targets, _ = build_targets(project, chapters=["chapter_99"])
    assert targets == []


def test_concordance_runs_only_for_the_types_that_need_it(project):
    write_annotations(
        project,
        [
            _ann(es_idx=0, type="inconsistency", content="[ostión]", sub_id="u1"),
            _ann(es_idx=2, type="footnote", content="[Sancerre]", sub_id="u2"),
        ],
    )
    targets, _ = build_targets(project)
    by_type = {t.ann_type: t for t in targets}
    assert by_type["inconsistency"].concordance
    assert by_type["footnote"].concordance == []


def test_target_key_matches_the_store(project):
    write_annotations(project, [_ann(es_idx=1, sub_id="u7")])
    targets, _ = build_targets(project)
    assert targets[0].key == "chapter_01__1__u7"
    assert targets[0].key == store.target_key(targets[0].record)
