"""Regression tests for per-project chunking parameter persistence.

The Stage 3 (Chunk) tab on the dashboard remembers the user's last
successful chunk parameters by writing them to ``projects/<id>/project.json``
under the ``chunking_config`` key (global defaults) and, for per-chapter
tuning, a sparse ``chapter_chunking`` map. The form pre-fills from those
values on the next load via ``/api/project/<id>/status``.

These tests cover:

1. ``/api/project/<id>/status`` returns ``chunking_config: None`` for a
   fresh project (so the JS falls back to its HTML default values).
2. A successful ``POST /api/project/<id>/chunk-all`` persists the global
   default (with Advanced min/max ratios) to ``project.json``, plus any
   per-chapter target overrides, and a subsequent status call surfaces them.
3. A successful ``POST /api/project/<id>/chapters/<chapter_id>/rechunk``
   upserts (or clears) that chapter's ``chapter_chunking`` entry without
   disturbing the global ``chunking_config``.
4. min/max chunk bounds are derived from the target via the ratios
   (default 0.25 / 1.5), reproducing the historical 500 / 3000 at target 2000.

Conventions follow ``test_dashboard_workflow_improvements.py`` — Flask
test client + ``monkeypatch`` on ``_get_projects_dir``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web_ui.app import app, _derive_chunk_bounds, _resolve_chunking


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Minimal project dir with one chapter long enough to chunk."""
    projects_dir = tmp_path / "projects"
    proj_dir = projects_dir / "proj1"
    (proj_dir / "chapters").mkdir(parents=True)

    # ~1200 words across several paragraphs — enough for the chunker to
    # actually run without us having to tune extreme parameters.
    paragraph = ("word " * 200).strip()
    body = "\n\n".join([paragraph] * 6)
    (proj_dir / "chapters" / "chapter_001.txt").write_text(body, encoding="utf-8")

    import web_ui.app as app_module
    monkeypatch.setattr(app_module, "_get_projects_dir", lambda: projects_dir)
    return proj_dir


def _read_project_json(proj_dir: Path) -> dict:
    p = proj_dir / "project.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


# ===========================================================================
# Tests
# ===========================================================================

class TestChunkingConfigInStatus:
    def test_status_returns_none_for_fresh_project(self, client, project):
        rv = client.get(f"/api/project/{project.name}/status")
        assert rv.status_code == 200
        data = rv.get_json()
        assert "chunking_config" in data
        assert data["chunking_config"] is None

    def test_status_backfills_ratios_for_legacy_config(self, client, project):
        # A project chunked before per-chapter tuning has no ratios; status
        # backfills the Advanced defaults so the GUI always has them.
        (project / "project.json").write_text(
            json.dumps({
                "title": "Proj 1",
                "chunking_config": {
                    "target_size": 1500,
                    "min_chunk_size": 400,
                    "max_chunk_size": 2500,
                    "overlap_paragraphs": 1,
                    "min_overlap_words": 50,
                },
            }),
            encoding="utf-8",
        )
        rv = client.get(f"/api/project/{project.name}/status")
        assert rv.status_code == 200
        cc = rv.get_json()["chunking_config"]
        assert cc["target_size"] == 1500
        assert cc["min_ratio"] == 0.25
        assert cc["max_ratio"] == 1.5


class TestChunkAllPersistsConfig:
    def test_chunk_all_writes_default_with_ratios(self, client, project):
        rv = client.post(
            f"/api/project/{project.name}/chunk-all",
            json={
                "default": {
                    "target_size": 1200,
                    "min_ratio": 0.25,
                    "max_ratio": 1.5,
                    "overlap_paragraphs": 1,
                    "min_overlap_words": 25,
                },
                "chapters": {},
            },
        )
        assert rv.status_code == 200, rv.get_json()
        assert rv.get_json().get("ok") is True

        cc = _read_project_json(project).get("chunking_config")
        assert cc == {
            "target_size": 1200,
            # Derived from target × ratios.
            "min_chunk_size": 300,
            "max_chunk_size": 1800,
            "min_ratio": 0.25,
            "max_ratio": 1.5,
            "overlap_paragraphs": 1,
            "min_overlap_words": 25,
        }

    def test_default_bounds_reproduce_legacy_500_3000_at_target_2000(self, client, project):
        client.post(
            f"/api/project/{project.name}/chunk-all",
            json={"default": {"target_size": 2000}, "chapters": {}},
        )
        cc = _read_project_json(project)["chunking_config"]
        assert cc["min_chunk_size"] == 500
        assert cc["max_chunk_size"] == 3000

    def test_flat_payload_is_backward_compatible(self, client, project):
        # A legacy flat payload (no default/chapters wrapper) is treated as
        # the global default.
        rv = client.post(
            f"/api/project/{project.name}/chunk-all",
            json={"target_size": 1000},
        )
        assert rv.status_code == 200, rv.get_json()
        cc = _read_project_json(project)["chunking_config"]
        assert cc["target_size"] == 1000
        assert cc["min_chunk_size"] == 250
        assert cc["max_chunk_size"] == 1500

    def test_non_dict_default_treated_as_empty(self, client, project):
        # A non-dict "default" value (e.g. a string or number) must not cause
        # an AttributeError; it should be coerced to {} and use fallback defaults.
        rv = client.post(
            f"/api/project/{project.name}/chunk-all",
            json={"default": "bad-value", "chapters": {}},
        )
        assert rv.status_code == 200, rv.get_json()

    def test_non_dict_chapters_treated_as_empty(self, client, project):
        rv = client.post(
            f"/api/project/{project.name}/chunk-all",
            json={"default": {"target_size": 2000}, "chapters": "not-a-dict"},
        )
        assert rv.status_code == 200, rv.get_json()

    def test_per_chapter_override_persisted_in_chapter_chunking(self, client, project):
        client.post(
            f"/api/project/{project.name}/chunk-all",
            json={
                "default": {"target_size": 2000},
                "chapters": {"chapter_001": {"target_size": 800}},
            },
        )
        cfg = _read_project_json(project)
        assert cfg["chapter_chunking"]["chapter_001"]["target_size"] == 800

    def test_blank_per_chapter_target_not_persisted(self, client, project):
        client.post(
            f"/api/project/{project.name}/chunk-all",
            json={
                "default": {"target_size": 2000},
                "chapters": {"chapter_001": {"target_size": None}},
            },
        )
        cfg = _read_project_json(project)
        # Sparse: a blank target leaves no chapter_chunking entry at all.
        assert not cfg.get("chapter_chunking")

    def test_chunk_all_surfaces_override_in_status(self, client, project):
        client.post(
            f"/api/project/{project.name}/chunk-all",
            json={
                "default": {"target_size": 2000},
                "chapters": {"chapter_001": {"target_size": 800}},
            },
        )
        rv = client.get(f"/api/project/{project.name}/status")
        assert rv.status_code == 200
        chapters = rv.get_json()["chapters"]
        ch = next(c for c in chapters if c["id"] == "chapter_001")
        assert ch["chunk_target_override"] == 800

    def test_chunk_all_preserves_other_project_json_keys(self, client, project):
        (project / "project.json").write_text(
            json.dumps({"title": "My Book", "gutenberg_url": "http://x"}),
            encoding="utf-8",
        )
        client.post(
            f"/api/project/{project.name}/chunk-all",
            json={"default": {"target_size": 1200}, "chapters": {}},
        )
        cfg = _read_project_json(project)
        assert cfg["title"] == "My Book"
        assert cfg["gutenberg_url"] == "http://x"
        assert cfg["chunking_config"]["target_size"] == 1200


class TestRechunkPersistsOverride:
    def _initial_chunk(self, client, project):
        """Produce real chunk files so /rechunk has something to reconstruct from."""
        rv = client.post(
            f"/api/project/{project.name}/chunk-all",
            json={
                "default": {
                    "target_size": 1500,
                    "overlap_paragraphs": 0,
                    "min_overlap_words": 0,
                },
                "chapters": {},
            },
        )
        assert rv.status_code == 200, rv.get_json()

    def test_rechunk_upserts_chapter_override(self, client, project):
        self._initial_chunk(client, project)

        rv = client.post(
            f"/api/project/{project.name}/chapters/chapter_001/rechunk",
            json={"target_size": 900},
        )
        assert rv.status_code == 200, rv.get_json()

        cfg = _read_project_json(project)
        assert cfg["chapter_chunking"]["chapter_001"]["target_size"] == 900
        # Global default untouched by a single-chapter rechunk.
        assert cfg["chunking_config"]["target_size"] == 1500

    def test_rechunk_blank_target_clears_override(self, client, project):
        self._initial_chunk(client, project)
        # First set an override.
        client.post(
            f"/api/project/{project.name}/chapters/chapter_001/rechunk",
            json={"target_size": 900},
        )
        assert _read_project_json(project)["chapter_chunking"]["chapter_001"]["target_size"] == 900

        # Now clear it (blank ⇒ null).
        rv = client.post(
            f"/api/project/{project.name}/chapters/chapter_001/rechunk",
            json={"target_size": None},
        )
        assert rv.status_code == 200, rv.get_json()
        cfg = _read_project_json(project)
        assert not cfg.get("chapter_chunking")


# ===========================================================================
# Unit tests for _derive_chunk_bounds
# ===========================================================================

class TestDeriveChunkBounds:
    """Unit tests for the pure helper that converts (target, ratios) -> bounds."""

    def test_default_ratios_at_2000_reproduce_legacy(self):
        mn, mx = _derive_chunk_bounds(2000, 0.25, 1.5)
        assert mn == 500
        assert mx == 3000

    def test_custom_ratios(self):
        mn, mx = _derive_chunk_bounds(1000, 0.3, 2.0)
        assert mn == 300
        assert mx == 2000

    def test_min_floor_clamp(self):
        # 100 * 0.1 = 10, below the 50-floor
        mn, mx = _derive_chunk_bounds(100, 0.1, 2.0)
        assert mn == 50

    def test_max_floor_clamp(self):
        # 10 * 1.5 = 15, below the 100-floor
        mn, mx = _derive_chunk_bounds(10, 0.25, 1.5)
        assert mx == 100

    def test_max_strictly_greater_than_min(self):
        # Pathological: identical ratios force max > min
        mn, mx = _derive_chunk_bounds(200, 1.0, 1.0)
        assert mx > mn

    def test_rounding(self):
        # 1000 * 0.333 rounds to 333
        mn, mx = _derive_chunk_bounds(1000, 0.333, 1.666)
        assert mn == 333
        assert mx == 1666


# ===========================================================================
# Unit tests for _resolve_chunking
# ===========================================================================

class TestResolveChunking:
    """Unit tests for _resolve_chunking, which builds a ChunkingConfig from
    a global default dict and an optional per-chapter override."""

    def test_no_override_uses_default_target(self):
        cfg, weights = _resolve_chunking({"target_size": 1800}, None)
        assert cfg.target_size == 1800
        assert weights is None

    def test_override_target_overrides_default(self):
        cfg, weights = _resolve_chunking({"target_size": 1800}, {"target_size": 900})
        assert cfg.target_size == 900

    def test_min_max_derived_from_default_ratios_even_when_override_target(self):
        # override target=800, default ratios 0.25/1.5 -> min=200, max=1200
        cfg, _ = _resolve_chunking(
            {"target_size": 2000, "min_ratio": 0.25, "max_ratio": 1.5},
            {"target_size": 800},
        )
        assert cfg.min_chunk_size == 200
        assert cfg.max_chunk_size == 1200

    def test_overlap_inherited_from_default(self):
        cfg, _ = _resolve_chunking(
            {"target_size": 2000, "overlap_paragraphs": 3, "min_overlap_words": 75},
            {"target_size": 800},
        )
        assert cfg.overlap_paragraphs == 3
        assert cfg.min_overlap_words == 75

    def test_non_dict_override_treated_as_empty(self):
        # A non-dict override (e.g. None or a bare string) should be coerced
        # to {} and the default target used instead.
        cfg, _ = _resolve_chunking({"target_size": 1500}, "not-a-dict")
        assert cfg.target_size == 1500

    def test_empty_default_uses_fallback_2000(self):
        cfg, _ = _resolve_chunking({}, None)
        assert cfg.target_size == 2000

    def test_ratios_stored_on_config(self):
        cfg, _ = _resolve_chunking({"target_size": 2000, "min_ratio": 0.3, "max_ratio": 2.0}, None)
        assert cfg.min_ratio == 0.3
        assert cfg.max_ratio == 2.0

    def test_weights_always_none_in_phase1(self):
        # Phase 1 never returns para_weights; that is reserved for Phase 2.
        _, weights = _resolve_chunking({"target_size": 2000}, {"target_size": 900})
        assert weights is None

    def test_max_ratio_not_greater_than_min_ratio_raises(self):
        with pytest.raises(ValueError, match="max_ratio"):
            _resolve_chunking({"target_size": 2000, "min_ratio": 1.0, "max_ratio": 0.5}, None)

    def test_equal_ratios_raise(self):
        with pytest.raises(ValueError, match="max_ratio"):
            _resolve_chunking({"target_size": 2000, "min_ratio": 0.5, "max_ratio": 0.5}, None)

    def test_infinite_ratio_raises(self):
        with pytest.raises(ValueError, match="finite"):
            _resolve_chunking({"target_size": 2000, "min_ratio": 0.25, "max_ratio": float("inf")}, None)

    def test_nan_ratio_raises(self):
        with pytest.raises(ValueError, match="finite"):
            _resolve_chunking({"target_size": 2000, "min_ratio": float("nan"), "max_ratio": 1.5}, None)


# ===========================================================================
# Unit tests for ChunkingConfig new fields
# ===========================================================================

class TestChunkingConfigNewFields:
    """Validate that min_ratio and max_ratio fields behave correctly."""

    def test_defaults(self):
        from src.models import ChunkingConfig
        cfg = ChunkingConfig()
        assert cfg.min_ratio == 0.25
        assert cfg.max_ratio == 1.5

    def test_custom_values(self):
        from src.models import ChunkingConfig
        cfg = ChunkingConfig(min_ratio=0.5, max_ratio=2.0)
        assert cfg.min_ratio == 0.5
        assert cfg.max_ratio == 2.0

    def test_zero_min_ratio_rejected(self):
        from src.models import ChunkingConfig
        with pytest.raises(Exception):
            ChunkingConfig(min_ratio=0.0)

    def test_zero_max_ratio_rejected(self):
        from src.models import ChunkingConfig
        with pytest.raises(Exception):
            ChunkingConfig(max_ratio=0.0)


# ===========================================================================
# Edge-case: _persist_chapter_chunking upsert preserves existing extra keys
# ===========================================================================

class TestPersistChapterChunkingUpsert:
    """Verify that upsert keeps pre-existing keys on a chapter entry."""

    def test_upsert_preserves_existing_keys(self, client, project):
        # Seed a chapter_chunking entry with a hypothetical Phase-2 'weights' key.
        (project / "project.json").write_text(
            json.dumps({
                "chapter_chunking": {
                    "chapter_001": {"target_size": 700, "weights": [1.0, 2.0]},
                },
            }),
            encoding="utf-8",
        )
        # Chunk-all with an override that updates target_size only.
        client.post(
            f"/api/project/{project.name}/chunk-all",
            json={
                "default": {"target_size": 2000},
                "chapters": {"chapter_001": {"target_size": 900}},
            },
        )
        cfg = _read_project_json(project)
        entry = cfg["chapter_chunking"]["chapter_001"]
        assert entry["target_size"] == 900
        # Existing Phase-2 key must survive the upsert.
        assert entry.get("weights") == [1.0, 2.0]

    def test_rechunk_override_then_chunk_all_clears_override(self, client, project):
        # chunk-all with target=2000 (no per-chapter override) should clear any
        # chapter_chunking entry whose override was set to None.
        client.post(
            f"/api/project/{project.name}/chunk-all",
            json={
                "default": {"target_size": 2000},
                "chapters": {"chapter_001": {"target_size": None}},
            },
        )
        cfg = _read_project_json(project)
        assert not cfg.get("chapter_chunking")
