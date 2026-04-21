#!/usr/bin/env python3
"""Unit tests for STAC scene discovery service."""

from unittest.mock import MagicMock, patch

import pytest

from ember.services.stac import (
    Scene,
    SceneQuery,
    STACService,
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
    def _scene(self, scene_id: str, tile: str, cloud: float) -> Scene:
        return Scene(
            id=scene_id,
            datetime="2026-03-15",
            cloud_cover=cloud,
            bbox=(0, 0, 1, 1),
            mgrs_tile=tile,
        )

    def test_one_scene_per_tile(self):
        scenes = [self._scene("A", "11SLT", 5.0), self._scene("B", "11SLU", 10.0)]
        result = pick_best_per_tile(scenes)
        assert len(result) == 2

    def test_picks_clearest_per_tile(self):
        scenes = [
            self._scene("A1", "11SLT", 20.0),
            self._scene("A2", "11SLT", 5.0),
            self._scene("B1", "11SLU", 10.0),
        ]
        result = pick_best_per_tile(scenes)
        assert len(result) == 2
        ids = {s.id for s in result}
        assert "A2" in ids  # 5% cloud, not A1 at 20%
        assert "B1" in ids

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

    Option-B tile-discovery contract: the required tile set is fixed after
    the first window fires — it equals the MGRS tile IDs present in that
    window's STAC results.  Escalation only fires when some tile in the
    required set is still uncovered after pick_best_per_tile runs on the
    accumulated scenes.  Because pick_best_per_tile always assigns every
    tile it receives, tests that exercise multi-window escalation must patch
    pick_best_per_tile to simulate an unresolvable gap.
    """

    BASE_QUERY = SceneQuery(
        bbox=(-118.5, 34.0, -118.0, 34.5),
        start_date="2026-03-01",
        end_date="2026-04-20",
        max_cloud_cover=20.0,
    )

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

        with patch("ember.config.settings.sentinel_search_windows", [30, 60, 90]):
            stac_service.search_scenes = mock_search_scenes
            result = await stac_service.search_coverage(self.BASE_QUERY)

        assert call_count == 1, "Should exit after first window when all tiles are covered"
        tile_ids = {s.mgrs_tile for s in result}
        assert tile_ids == {"11SLT", "11SLU"}

    @pytest.mark.asyncio
    async def test_escalates_to_second_window(self, stac_service):
        """30-day has tile B uncovered → 60-day fills it → 90-day never fires.

        pick_best_per_tile is patched to drop 11SLU on the first call only,
        simulating a gap that a wider window can fill.  On subsequent calls
        the patch stops dropping so the 60-day window resolves 11SLU.
        """
        from ember.services import stac as stac_module

        tile_a = _make_scene("11SLT", cloud=5.0)
        tile_b = _make_scene("11SLU", cloud=8.0)
        call_count = 0
        pick_call_count = 0

        async def mock_search_scenes(query: SceneQuery) -> list[Scene]:
            nonlocal call_count
            call_count += 1
            return [tile_a, tile_b]  # both tiles always visible

        def patched_pick_best(scenes: list[Scene]) -> list[Scene]:
            """Drop 11SLU only on the first pick call (window 1); allow it from window 2."""
            nonlocal pick_call_count
            pick_call_count += 1
            base = pick_best_per_tile(scenes)
            if pick_call_count == 1:
                return [s for s in base if s.mgrs_tile != "11SLU"]
            return base

        with (
            patch("ember.config.settings.sentinel_search_windows", [30, 60, 90]),
            patch.object(stac_module, "pick_best_per_tile", side_effect=patched_pick_best),
        ):
            stac_service.search_scenes = mock_search_scenes
            result = await stac_service.search_coverage(self.BASE_QUERY)

        assert call_count == 2, "Should exit after second window"
        tile_ids = {s.mgrs_tile for s in result}
        assert "11SLT" in tile_ids
        assert "11SLU" in tile_ids

    @pytest.mark.asyncio
    async def test_escalates_to_third_window(self, stac_service):
        """30- and 60-day leave tile B uncovered → 90-day fills it → loop exits.

        pick_best_per_tile is patched to drop 11SLU for the first two calls,
        simulating a tile that only becomes assignable at the widest window.
        """
        from ember.services import stac as stac_module

        tile_a = _make_scene("11SLT")
        tile_b = _make_scene("11SLU")
        call_count = 0
        pick_call_count = 0

        async def mock_search_scenes(query: SceneQuery) -> list[Scene]:
            nonlocal call_count
            call_count += 1
            return [tile_a, tile_b]

        def patched_pick_best(scenes: list[Scene]) -> list[Scene]:
            nonlocal pick_call_count
            pick_call_count += 1
            base = pick_best_per_tile(scenes)
            if pick_call_count <= 2:
                return [s for s in base if s.mgrs_tile != "11SLU"]
            return base

        with (
            patch("ember.config.settings.sentinel_search_windows", [30, 60, 90]),
            patch.object(stac_module, "pick_best_per_tile", side_effect=patched_pick_best),
        ):
            stac_service.search_scenes = mock_search_scenes
            result = await stac_service.search_coverage(self.BASE_QUERY)

        assert call_count == 3, "Should exit after third window"
        tile_ids = {s.mgrs_tile for s in result}
        assert "11SLT" in tile_ids
        assert "11SLU" in tile_ids

    @pytest.mark.asyncio
    async def test_exhaustion_all_tiles_covered(self, stac_service, caplog):
        """When all windows cover the same single tile, loop exhausts and logs info.

        Under Option B, with a single tile always returned, window 1 sets
        required = {11SLT}, covered = {11SLT}, uncovered = {} → breaks after
        window 1.  This verifies the common happy path: one tile, first window.
        """
        import logging

        tile_a = _make_scene("11SLT")
        call_count = 0

        async def mock_search_scenes(query: SceneQuery) -> list[Scene]:
            nonlocal call_count
            call_count += 1
            return [tile_a]

        with patch("ember.config.settings.sentinel_search_windows", [30, 60, 90]):
            stac_service.search_scenes = mock_search_scenes
            with caplog.at_level(logging.INFO, logger="ember.services.stac"):
                result = await stac_service.search_coverage(self.BASE_QUERY)

        assert call_count == 1, "Single tile covered in window 1 → break immediately"
        assert any("sufficient" in r.message for r in caplog.records)
        assert len(result) == 1
        assert result[0].mgrs_tile == "11SLT"

    @pytest.mark.asyncio
    async def test_exhaustion_logs_warning_for_residual_tiles(self, stac_service, caplog):
        """All three windows leave tile B uncovered → warning logged, partial result returned.

        pick_best_per_tile is patched to always drop 11SLU, simulating an
        unresolvable gap.  The function should exhaust all windows, log a
        warning naming 11SLU, and return the partial result containing 11SLT.
        """
        import logging

        from ember.services import stac as stac_module

        tile_a = _make_scene("11SLT")
        tile_b = _make_scene("11SLU")
        call_count = 0

        async def mock_search_scenes(query: SceneQuery) -> list[Scene]:
            nonlocal call_count
            call_count += 1
            return [tile_a, tile_b]

        def dropping_pick_best(scenes: list[Scene]) -> list[Scene]:
            """Always omit 11SLU to simulate an unresolvable gap."""
            return [s for s in pick_best_per_tile(scenes) if s.mgrs_tile != "11SLU"]

        with (
            patch("ember.config.settings.sentinel_search_windows", [30, 60, 90]),
            patch.object(stac_module, "pick_best_per_tile", side_effect=dropping_pick_best),
        ):
            stac_service.search_scenes = mock_search_scenes
            with caplog.at_level(logging.WARNING, logger="ember.services.stac"):
                result = await stac_service.search_coverage(self.BASE_QUERY)

        assert call_count == 3, "All three windows should be tried"
        tile_ids = {s.mgrs_tile for s in result}
        assert "11SLT" in tile_ids
        assert "11SLU" not in tile_ids  # dropped by patched function
        assert any(
            "11SLU" in r.message and "no scene found" in r.message for r in caplog.records
        )
