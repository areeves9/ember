#!/usr/bin/env python3
"""Unit tests for STAC scene discovery service."""

from unittest.mock import MagicMock, patch

import pytest

from ember.services.stac import (
    Scene,
    SceneQuery,
    STACService,
    _intersecting_mgrs_tiles,
    _item_to_scene,
    _scene_cache,
    _search_cache,
    _search_cache_key,
    pick_best_per_tile,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def stac_service():
    """Provide a fresh STACService instance."""
    return STACService()


@pytest.fixture
def sample_query():
    """Standard test query (Los Angeles area, March 2026)."""
    return SceneQuery(
        bbox=(-118.5, 34.0, -118.0, 34.5),
        start_date="2026-03-01",
        end_date="2026-03-31",
        max_cloud_cover=20.0,
        limit=5,
    )


@pytest.fixture
def mock_stac_item():
    """Create a mock pystac Item."""
    item = MagicMock()
    item.id = "S2A_11SLT_20260315_0_L2A"
    item.bbox = [-118.6, 33.9, -117.9, 34.6]
    item.properties = {
        "datetime": "2026-03-15T18:32:15Z",
        "eo:cloud_cover": 8.2,
    }
    # Earth Search uses common names, not band IDs
    item.assets = {
        "blue": MagicMock(href="s3://sentinel-cogs/path/B02.tif"),
        "green": MagicMock(href="s3://sentinel-cogs/path/B03.tif"),
        "red": MagicMock(href="s3://sentinel-cogs/path/B04.tif"),
        "nir": MagicMock(href="s3://sentinel-cogs/path/B08.tif"),
        "swir16": MagicMock(href="s3://sentinel-cogs/path/B11.tif"),
        "swir22": MagicMock(href="s3://sentinel-cogs/path/B12.tif"),
    }
    return item


@pytest.fixture(autouse=True)
def clear_caches():
    """Clear all caches before each test."""
    _search_cache.clear()
    _scene_cache.clear()
    yield
    _search_cache.clear()
    _scene_cache.clear()


# =============================================================================
# item_to_scene conversion
# =============================================================================


class TestItemToScene:
    def test_converts_item_to_scene(self, mock_stac_item):
        scene = _item_to_scene(mock_stac_item)
        assert scene.id == "S2A_11SLT_20260315_0_L2A"
        assert scene.datetime == "2026-03-15T18:32:15Z"
        assert scene.cloud_cover == 8.2
        assert scene.bbox == (-118.6, 33.9, -117.9, 34.6)
        assert "B04" in scene.assets
        assert scene.assets["B04"] == "s3://sentinel-cogs/path/B04.tif"

    def test_handles_missing_bands(self):
        item = MagicMock()
        item.id = "test"
        item.bbox = [0, 0, 1, 1]
        item.properties = {"datetime": "2026-01-01", "eo:cloud_cover": 0}
        item.assets = {}  # No bands
        scene = _item_to_scene(item)
        assert scene.assets == {}

    def test_extracts_mgrs_tile(self, mock_stac_item):
        scene = _item_to_scene(mock_stac_item)
        assert scene.mgrs_tile == "11SLT"


# =============================================================================
# pick_best_per_tile
# =============================================================================


class TestPickBestPerTile:
    def _scene(
        self,
        scene_id: str,
        tile: str,
        dt: str = "2026-03-15T00:00:00Z",
        cloud: float = 5.0,
    ) -> Scene:
        return Scene(
            id=scene_id,
            datetime=dt,
            cloud_cover=cloud,
            bbox=(0, 0, 1, 1),
            mgrs_tile=tile,
        )

    def test_one_scene_per_tile(self):
        scenes = [self._scene("A", "11SLT"), self._scene("B", "11SLU")]
        result = pick_best_per_tile(scenes)
        assert len(result) == 2

    def test_picks_most_recent_per_tile(self):
        """Most recent scene wins per tile, regardless of cloud cover."""
        scenes = [
            # Older but clearer — should LOSE to A2 (newer) under recency ranking.
            self._scene("A1", "11SLT", dt="2026-03-01T00:00:00Z", cloud=5.0),
            self._scene("A2", "11SLT", dt="2026-03-15T00:00:00Z", cloud=80.0),
            self._scene("B1", "11SLU", dt="2026-03-10T00:00:00Z", cloud=10.0),
        ]
        result = pick_best_per_tile(scenes)
        assert len(result) == 2
        ids = {s.id for s in result}
        assert "A2" in ids, "A2 is newer than A1 — recency wins, even at 80% cloud"
        assert "B1" in ids

    def test_cloud_cover_is_not_a_tiebreaker(self):
        """When two scenes for a tile have equal datetimes, first-seen wins."""
        scenes = [
            self._scene("A1", "11SLT", dt="2026-03-15T00:00:00Z", cloud=80.0),
            self._scene("A2", "11SLT", dt="2026-03-15T00:00:00Z", cloud=5.0),
        ]
        result = pick_best_per_tile(scenes)
        assert len(result) == 1
        assert result[0].id == "A1", "Equal datetimes — first scene retained, cloud cover ignored"

    def test_empty_list(self):
        assert pick_best_per_tile([]) == []


# =============================================================================
# Cache key generation
# =============================================================================


class TestSearchCacheKey:
    def test_same_query_produces_same_key(self, sample_query):
        key1 = _search_cache_key(sample_query)
        key2 = _search_cache_key(sample_query)
        assert key1 == key2

    def test_rounded_bbox_matches(self):
        q1 = SceneQuery(
            bbox=(-118.500, 34.000, -118.000, 34.500),
            start_date="2026-03-01",
            end_date="2026-03-31",
        )
        q2 = SceneQuery(
            bbox=(-118.5001, 34.0004, -118.0002, 34.4999),
            start_date="2026-03-01",
            end_date="2026-03-31",
        )
        assert _search_cache_key(q1) == _search_cache_key(q2)

    def test_different_dates_produce_different_key(self):
        q1 = SceneQuery(
            bbox=(-118.5, 34.0, -118.0, 34.5), start_date="2026-03-01", end_date="2026-03-31"
        )
        q2 = SceneQuery(
            bbox=(-118.5, 34.0, -118.0, 34.5), start_date="2026-04-01", end_date="2026-04-30"
        )
        assert _search_cache_key(q1) != _search_cache_key(q2)

    def test_different_cloud_cover_produces_different_key(self):
        q1 = SceneQuery(
            bbox=(-118.5, 34.0, -118.0, 34.5),
            start_date="2026-03-01",
            end_date="2026-03-31",
            max_cloud_cover=10.0,
        )
        q2 = SceneQuery(
            bbox=(-118.5, 34.0, -118.0, 34.5),
            start_date="2026-03-01",
            end_date="2026-03-31",
            max_cloud_cover=50.0,
        )
        assert _search_cache_key(q1) != _search_cache_key(q2)


# =============================================================================
# STACService.search_scenes
# =============================================================================


class TestSearchScenes:
    @pytest.mark.asyncio
    async def test_returns_scenes(self, stac_service, sample_query, mock_stac_item):
        mock_search = MagicMock()
        mock_search.items.return_value = [mock_stac_item]

        mock_client = MagicMock()
        mock_client.search.return_value = mock_search

        with patch.object(stac_service, "_get_client", return_value=mock_client):
            scenes = await stac_service.search_scenes(sample_query)

        assert len(scenes) == 1
        assert scenes[0].id == "S2A_11SLT_20260315_0_L2A"
        assert scenes[0].cloud_cover == 8.2

    @pytest.mark.asyncio
    async def test_caches_search_results(self, stac_service, sample_query, mock_stac_item):
        mock_search = MagicMock()
        mock_search.items.return_value = [mock_stac_item]

        mock_client = MagicMock()
        mock_client.search.return_value = mock_search

        with patch.object(stac_service, "_get_client", return_value=mock_client):
            # First call
            await stac_service.search_scenes(sample_query)
            # Second call should be cached
            await stac_service.search_scenes(sample_query)

        # STAC client should only be called once
        assert mock_client.search.call_count == 1

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_scenes(self, stac_service, sample_query):
        mock_search = MagicMock()
        mock_search.items.return_value = []

        mock_client = MagicMock()
        mock_client.search.return_value = mock_search

        with patch.object(stac_service, "_get_client", return_value=mock_client):
            scenes = await stac_service.search_scenes(sample_query)

        assert scenes == []


# =============================================================================
# STACService.get_scene
# =============================================================================


class TestGetScene:
    @pytest.mark.asyncio
    async def test_returns_scene_by_id(self, stac_service, mock_stac_item):
        mock_search = MagicMock()
        mock_search.items.return_value = [mock_stac_item]

        mock_client = MagicMock()
        mock_client.search.return_value = mock_search

        with patch.object(stac_service, "_get_client", return_value=mock_client):
            scene = await stac_service.get_scene("S2A_11SLT_20260315_0_L2A")

        assert scene is not None
        assert scene.id == "S2A_11SLT_20260315_0_L2A"

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_scene(self, stac_service):
        mock_search = MagicMock()
        mock_search.items.return_value = []

        mock_client = MagicMock()
        mock_client.search.return_value = mock_search

        with patch.object(stac_service, "_get_client", return_value=mock_client):
            scene = await stac_service.get_scene("NONEXISTENT")

        assert scene is None

    @pytest.mark.asyncio
    async def test_uses_scene_cache(self, stac_service, mock_stac_item):
        mock_search = MagicMock()
        mock_search.items.return_value = [mock_stac_item]

        mock_client = MagicMock()
        mock_client.search.return_value = mock_search

        with patch.object(stac_service, "_get_client", return_value=mock_client):
            # First call populates cache
            await stac_service.get_scene("S2A_11SLT_20260315_0_L2A")
            # Second call should hit cache
            await stac_service.get_scene("S2A_11SLT_20260315_0_L2A")

        assert mock_client.search.call_count == 1

    @pytest.mark.asyncio
    async def test_handles_stac_api_error(self, stac_service):
        mock_client = MagicMock()
        mock_client.search.side_effect = Exception("Connection refused")

        with patch.object(stac_service, "_get_client", return_value=mock_client):
            scene = await stac_service.get_scene("S2A_BROKEN")

        assert scene is None


# =============================================================================
# STACService.search_coverage — progressive window escalation
# =============================================================================


def _make_scene(tile: str, cloud: float = 5.0, scene_suffix: str = "") -> Scene:
    """Build a minimal Scene for a given MGRS tile ID."""
    sid = f"S2A_{tile}_20260315_0_L2A{scene_suffix}"
    return Scene(
        id=sid,
        datetime="2026-03-15T18:32:15Z",
        cloud_cover=cloud,
        bbox=(-118.6, 33.9, -117.9, 34.6),
        mgrs_tile=tile,
        assets={
            "B04": f"s3://sentinel-cogs/{tile}/B04.tif",
            "B03": f"s3://sentinel-cogs/{tile}/B03.tif",
            "B02": f"s3://sentinel-cogs/{tile}/B02.tif",
        },
    )


class TestSearchCoverageProgressive:
    """Progressive window escalation in search_coverage.

    All tests mock search_scenes (the per-window STAC call boundary) and
    patch settings.sentinel_search_windows to control the window list.
    _intersecting_mgrs_tiles is patched at the class level to return a fixed
    two-tile set {"11SLT", "11SLU"} so tests are deterministic regardless of
    the real MGRS geometry of BASE_QUERY's bbox.

    Escalation is driven by having mock_search_scenes withhold a tile's scene
    in early windows — no pick_best_per_tile patching is needed.
    """

    BASE_QUERY = SceneQuery(
        bbox=(-118.5, 34.0, -118.0, 34.5),
        start_date="2026-03-01",
        end_date="2026-04-20",
        max_cloud_cover=20.0,
    )
    # Fixed tile set used across all tests in this class.
    REQUIRED_TILES = {"11SLT", "11SLU"}

    @pytest.mark.asyncio
    async def test_first_window_suffices(self, stac_service):
        """30-day window covers all required tiles → 60- and 90-day queries never fire."""
        tile_a = _make_scene("11SLT")
        tile_b = _make_scene("11SLU")
        call_count = 0

        async def mock_search_scenes(query: SceneQuery) -> list[Scene]:
            nonlocal call_count
            call_count += 1
            return [tile_a, tile_b]

        with (
            patch("ember.config.settings.sentinel_search_windows", [30, 60, 90]),
            patch("ember.services.stac._intersecting_mgrs_tiles", return_value=self.REQUIRED_TILES),
        ):
            stac_service.search_scenes = mock_search_scenes
            result = await stac_service.search_coverage(self.BASE_QUERY)

        assert call_count == 1, "Should exit after first window when all tiles are covered"
        tile_ids = {s.mgrs_tile for s in result}
        assert tile_ids == {"11SLT", "11SLU"}

    @pytest.mark.asyncio
    async def test_escalates_to_second_window(self, stac_service):
        """30-day window returns only tile A → 60-day window adds tile B → exits.

        Window 1 only returns 11SLT.  Window 2 returns both tiles, so the
        accumulated best now covers 11SLU and the loop breaks.
        """
        tile_a = _make_scene("11SLT", cloud=5.0)
        tile_b = _make_scene("11SLU", cloud=8.0)
        call_count = 0

        async def mock_search_scenes(query: SceneQuery) -> list[Scene]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [tile_a]  # window 1: only tile A
            return [tile_a, tile_b]  # window 2+: both tiles visible

        with (
            patch("ember.config.settings.sentinel_search_windows", [30, 60, 90]),
            patch("ember.services.stac._intersecting_mgrs_tiles", return_value=self.REQUIRED_TILES),
        ):
            stac_service.search_scenes = mock_search_scenes
            result = await stac_service.search_coverage(self.BASE_QUERY)

        assert call_count == 2, "Should exit after second window"
        tile_ids = {s.mgrs_tile for s in result}
        assert "11SLT" in tile_ids
        assert "11SLU" in tile_ids

    @pytest.mark.asyncio
    async def test_escalates_to_third_window(self, stac_service):
        """Windows 1 and 2 return only tile A → window 3 adds tile B → exits."""
        tile_a = _make_scene("11SLT")
        tile_b = _make_scene("11SLU")
        call_count = 0

        async def mock_search_scenes(query: SceneQuery) -> list[Scene]:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return [tile_a]  # windows 1-2: only tile A
            return [tile_a, tile_b]  # window 3: both tiles

        with (
            patch("ember.config.settings.sentinel_search_windows", [30, 60, 90]),
            patch("ember.services.stac._intersecting_mgrs_tiles", return_value=self.REQUIRED_TILES),
        ):
            stac_service.search_scenes = mock_search_scenes
            result = await stac_service.search_coverage(self.BASE_QUERY)

        assert call_count == 3, "Should exit after third window"
        tile_ids = {s.mgrs_tile for s in result}
        assert "11SLT" in tile_ids
        assert "11SLU" in tile_ids

    @pytest.mark.asyncio
    async def test_exhaustion_all_tiles_covered(self, stac_service, caplog):
        """Single-tile required set covered in window 1 → break immediately, log 'sufficient'."""
        import logging

        tile_a = _make_scene("11SLT")
        call_count = 0

        async def mock_search_scenes(query: SceneQuery) -> list[Scene]:
            nonlocal call_count
            call_count += 1
            return [tile_a]

        with (
            patch("ember.config.settings.sentinel_search_windows", [30, 60, 90]),
            patch("ember.services.stac._intersecting_mgrs_tiles", return_value={"11SLT"}),
        ):
            stac_service.search_scenes = mock_search_scenes
            with caplog.at_level(logging.INFO, logger="ember.services.stac"):
                result = await stac_service.search_coverage(self.BASE_QUERY)

        assert call_count == 1, "Single tile covered in window 1 → break immediately"
        assert any("sufficient" in r.message for r in caplog.records)
        assert len(result) == 1
        assert result[0].mgrs_tile == "11SLT"

    @pytest.mark.asyncio
    async def test_window_queries_ignore_base_cloud_cover(self, stac_service):
        """Window queries always use max_cloud_cover=100.0 regardless of the base query.

        ADR ORQ-141: progressive coverage searches accept any cloud cover so
        that a cloudy scene fills a tile gap rather than leaving it black.
        """
        tile_a = _make_scene("11SLT")
        tile_b = _make_scene("11SLU")
        captured_queries: list[SceneQuery] = []

        async def mock_search_scenes(query: SceneQuery) -> list[Scene]:
            captured_queries.append(query)
            return [tile_a, tile_b]

        base_query = SceneQuery(
            bbox=(-118.5, 34.0, -118.0, 34.5),
            start_date="2026-03-01",
            end_date="2026-04-20",
            max_cloud_cover=20.0,  # Caller's preference — must NOT propagate to windows
        )

        with (
            patch("ember.config.settings.sentinel_search_windows", [30, 60, 90]),
            patch("ember.services.stac._intersecting_mgrs_tiles", return_value=self.REQUIRED_TILES),
        ):
            stac_service.search_scenes = mock_search_scenes
            await stac_service.search_coverage(base_query)

        assert len(captured_queries) >= 1
        for q in captured_queries:
            assert q.max_cloud_cover == 100.0, (
                f"Window query should use max_cloud_cover=100.0, got {q.max_cloud_cover}"
            )

    @pytest.mark.asyncio
    async def test_fetch_limit_scales_with_required_tile_count(self, stac_service):
        """A 30-tile bbox should request limit=60 from STAC, not the floor of 20.

        Hardcoded limit=20 was the cap that systematically excluded tiles in
        large bboxes — the limit must scale with the required tile count so
        every tile has at least one slot in the result set.
        """
        captured_queries: list[SceneQuery] = []

        async def mock_search_scenes(query: SceneQuery) -> list[Scene]:
            captured_queries.append(query)
            # Return one scene per required tile so the loop exits in window 1.
            return [_make_scene(t) for t in self.REQUIRED_TILES]

        large_tile_set = {f"99X{i:02d}" for i in range(30)}

        with (
            patch("ember.config.settings.sentinel_search_windows", [30, 60, 90]),
            patch("ember.services.stac._intersecting_mgrs_tiles", return_value=large_tile_set),
        ):
            stac_service.search_scenes = mock_search_scenes
            await stac_service.search_coverage(self.BASE_QUERY)

        assert captured_queries, "Expected at least one window query"
        # 30 required tiles × 2 = 60.
        assert captured_queries[0].limit == 60, (
            f"Expected limit=60 for 30-tile set, got {captured_queries[0].limit}"
        )

    @pytest.mark.asyncio
    async def test_fetch_limit_floor_is_twenty_for_small_bboxes(self, stac_service):
        """A single-tile bbox should still get limit=20 (the floor), not 2."""
        captured_queries: list[SceneQuery] = []

        async def mock_search_scenes(query: SceneQuery) -> list[Scene]:
            captured_queries.append(query)
            return [_make_scene("11SLT")]

        with (
            patch("ember.config.settings.sentinel_search_windows", [30, 60, 90]),
            patch("ember.services.stac._intersecting_mgrs_tiles", return_value={"11SLT"}),
        ):
            stac_service.search_scenes = mock_search_scenes
            await stac_service.search_coverage(self.BASE_QUERY)

        assert captured_queries[0].limit == 20

    @pytest.mark.asyncio
    async def test_exhaustion_logs_warning_for_residual_tiles(self, stac_service, caplog):
        """All three windows never return a scene for tile B → warning names 11SLU."""
        import logging

        tile_a = _make_scene("11SLT")
        call_count = 0

        async def mock_search_scenes(query: SceneQuery) -> list[Scene]:
            nonlocal call_count
            call_count += 1
            return [tile_a]  # 11SLU never appears in any window

        with (
            patch("ember.config.settings.sentinel_search_windows", [30, 60, 90]),
            patch("ember.services.stac._intersecting_mgrs_tiles", return_value=self.REQUIRED_TILES),
        ):
            stac_service.search_scenes = mock_search_scenes
            with caplog.at_level(logging.WARNING, logger="ember.services.stac"):
                result = await stac_service.search_coverage(self.BASE_QUERY)

        assert call_count == 3, "All three windows should be tried"
        tile_ids = {s.mgrs_tile for s in result}
        assert "11SLT" in tile_ids
        assert "11SLU" not in tile_ids
        assert any("11SLU" in r.message and "no scene found" in r.message for r in caplog.records)


# =============================================================================
# _intersecting_mgrs_tiles helper
# =============================================================================


class TestIntersectingMGRSTiles:
    """Unit tests for the bbox→MGRS-tile-set helper."""

    def test_la_bbox_contains_known_tiles(self):
        """LA area bbox should include at least 11SLT and 11SLU.

        Confirmed via mgrs.MGRS().toMGRS(lat, lon, MGRSPrecision=0):
          corner (34.0, -118.5) → 11SLT
          corner (34.5, -118.5) → 11SLU
          corner (34.0, -118.0) → 11SMT
          corner (34.5, -118.0) → 11SMU
        """
        tiles = _intersecting_mgrs_tiles((-118.5, 34.0, -118.0, 34.5))
        assert "11SLT" in tiles
        assert "11SLU" in tiles
        assert "11SMT" in tiles
        assert "11SMU" in tiles

    def test_point_bbox_returns_single_tile(self):
        """A zero-size bbox (single point) should return exactly one tile."""
        tiles = _intersecting_mgrs_tiles((-118.25, 34.05, -118.25, 34.05))
        assert len(tiles) == 1
        assert "11SLT" in tiles

    def test_narrow_bbox_returns_at_least_one_tile(self):
        """A bbox narrower than the sampling step still returns tiles for each corner."""
        # 0.1° × 0.1° bbox — smaller than the 0.4° step — must still return a tile.
        tiles = _intersecting_mgrs_tiles((-118.3, 34.1, -118.2, 34.2))
        assert len(tiles) >= 1

    def test_returns_uppercase_five_char_ids(self):
        """All returned tile IDs should be uppercase and exactly 5 characters."""
        tiles = _intersecting_mgrs_tiles((-118.5, 34.0, -118.0, 34.5))
        for tile in tiles:
            assert len(tile) == 5, f"Expected 5-char tile ID, got {tile!r}"
            assert tile == tile.upper(), f"Expected uppercase, got {tile!r}"
