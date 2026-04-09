#!/usr/bin/env python3
"""Unit tests for STAC scene discovery service."""

from unittest.mock import MagicMock, patch

import pytest

from ember.services.stac import (
    SceneQuery,
    STACService,
    _item_to_scene,
    _scene_cache,
    _search_cache,
    _search_cache_key,
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
