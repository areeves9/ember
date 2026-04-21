"""Tests for full-extent raster overviews (ORQ-140).

Covers the bbox-optional contract on COG-backed imagery endpoints:
- `/imagery/truecolor-cog`, `/imagery/ndvi-cog`, `/imagery/ndmi-cog`
- `/terrain?format=raster`

Contract:
- All bbox params present → existing bbox crop behavior (unchanged)
- All bbox params absent → full-extent preview via pyramid overviews
- Partial bbox → 400 ValidationError
- Full-extent responses carry `Cache-Control: public, max-age=<ttl>`
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

os.environ.setdefault("LANDFIRE_S3_PREFIX", "s3://test-landfire/")

from ember.auth import verify_token  # noqa: E402
from ember.main import app  # noqa: E402
from ember.routers.imagery import _validate_all_or_none_bbox  # noqa: E402
from ember.services.stac import Scene  # noqa: E402

_DEV_USER = {"sub": "dev-user", "email": "dev@localhost", "auth_type": "dev"}


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def client():
    app.dependency_overrides[verify_token] = lambda: _DEV_USER
    yield TestClient(app)
    app.dependency_overrides.pop(verify_token, None)


@pytest.fixture
def fake_scene():
    return Scene(
        id="S2B_10SEH_20260415_0_L2A",
        datetime="2026-04-15T18:30:00Z",
        cloud_cover=3.2,
        bbox=(-122.0, 37.0, -121.0, 38.0),
        mgrs_tile="10SEH",
        assets={
            "B02": "https://example/B02.tif",
            "B03": "https://example/B03.tif",
            "B04": "https://example/B04.tif",
            "B08": "https://example/B08.tif",
            "B11": "https://example/B11.tif",
        },
    )


def _index_result(scene: Scene, index: str, stats: dict) -> dict:
    return {
        "status": "success",
        "scene_id": scene.id,
        "index": index,
        "bbox": list(scene.bbox),
        "bands_used": ["B08", "B04"] if index == "NDVI" else ["B08", "B11"],
        "stats": stats,
        "source": "Sentinel-2 L2A (AWS COG)",
    }


def _truecolor_result(scene: Scene) -> dict:
    return {
        "status": "success",
        "scene_id": scene.id,
        "bbox": list(scene.bbox),
        "bands": ["B04", "B03", "B02"],
        "raster": {
            "format": "image/png",
            "encoding": "base64",
            "data": "fake",
            "width": 1200,
            "height": 1200,
        },
        "source": "Sentinel-2 L2A (AWS COG)",
    }


@pytest.fixture
def fake_terrain_service():
    """MagicMock terrain service with two registered layers and async full-extent path."""
    svc = MagicMock()
    svc.available_layers = ["fuel", "elevation"]
    svc.query_terrain_full_extent_raster = AsyncMock(
        return_value={
            "status": "success",
            "layer": "fuel",
            "bbox": [-125.0, 24.0, -66.0, 50.0],
            "raster": {
                "format": "geotiff",
                "encoding": "base64",
                "data": "fake",
                "width": 1200,
                "height": 793,
            },
            "stats": {"min": 91.0, "max": 204.0, "mean": 165.0},
        }
    )
    return svc


# =============================================================================
# _validate_all_or_none_bbox unit tests
# =============================================================================


class TestBboxValidation:
    def test_all_four_present_returns_tuple(self):
        bbox = _validate_all_or_none_bbox(34.0, 35.0, -118.0, -117.0)
        assert bbox == (-118.0, 34.0, -117.0, 35.0)

    def test_all_four_absent_returns_none(self):
        assert _validate_all_or_none_bbox(None, None, None, None) is None

    @pytest.mark.parametrize(
        "args,missing_name",
        [
            ((34.0, None, -118.0, -117.0), "max_lat"),
            ((None, 35.0, -118.0, -117.0), "min_lat"),
            ((34.0, 35.0, None, -117.0), "min_lon"),
            ((34.0, 35.0, -118.0, None), "max_lon"),
        ],
    )
    def test_partial_bbox_raises_400(self, args, missing_name):
        with pytest.raises(HTTPException) as exc:
            _validate_all_or_none_bbox(*args)
        assert exc.value.status_code == 400
        assert missing_name in exc.value.detail
        assert "Incomplete bbox" in exc.value.detail

    @pytest.mark.parametrize(
        "args,err_fragment",
        [
            ((35.0, 34.0, -118.0, -117.0), "min_lat must be less than max_lat"),
            ((34.0, 35.0, -117.0, -118.0), "min_lon must be less than max_lon"),
        ],
    )
    def test_inverted_axis_raises_400(self, args, err_fragment):
        with pytest.raises(HTTPException) as exc:
            _validate_all_or_none_bbox(*args)
        assert exc.value.status_code == 400
        assert err_fragment in exc.value.detail


# =============================================================================
# Router-level: /imagery/*-cog partial bbox → 400 (parametrized over endpoint)
# =============================================================================


COG_ENDPOINTS = ["ndvi-cog", "ndmi-cog", "truecolor-cog"]


class TestImageryPartialBbox:
    @pytest.mark.parametrize("endpoint", COG_ENDPOINTS)
    @pytest.mark.parametrize(
        "qs",
        [
            "min_lat=34.0&max_lat=35.0&min_lon=-118.0",  # missing max_lon
            "min_lat=34.0&max_lon=-117.0",  # two missing
            "min_lat=34.0",  # one provided
        ],
    )
    def test_partial_bbox_returns_400(self, client, endpoint, qs):
        r = client.get(f"/api/v1/imagery/{endpoint}?{qs}")
        assert r.status_code == 400
        assert "Incomplete bbox" in r.json()["detail"]


# =============================================================================
# Router-level: /imagery/*-cog no-bbox full-extent path
# =============================================================================


# Parametrizing across all three Sentinel-2 COG endpoints:
#   - (endpoint path, service attr mocked, canned-result factory)
IMAGERY_FULL_EXTENT_CASES = [
    (
        "ndvi-cog",
        "ember.routers.imagery.sentinel_cog_service.compute_index",
        lambda scene: _index_result(scene, "NDVI", {"min": 0.1, "max": 0.7, "mean": 0.42}),
    ),
    (
        "ndmi-cog",
        "ember.routers.imagery.sentinel_cog_service.compute_index",
        lambda scene: _index_result(scene, "NDMI", {"min": -0.3, "max": 0.4, "mean": 0.05}),
    ),
    (
        "truecolor-cog",
        "ember.routers.imagery.sentinel_cog_service.get_truecolor",
        lambda scene: _truecolor_result(scene),
    ),
]


class TestImageryFullExtent:
    @pytest.mark.parametrize("endpoint,service_target,result_factory", IMAGERY_FULL_EXTENT_CASES)
    def test_no_bbox_calls_service_with_bbox_none(
        self, client, fake_scene, endpoint, service_target, result_factory
    ):
        """All three COG endpoints share the no-bbox contract:
        service is called with bbox=None, no stitched scenes arg, single scene selected,
        and the response carries Cache-Control headers.
        """
        canned = result_factory(fake_scene)
        with (
            patch(
                "ember.routers.imagery.stac_service.search_scenes",
                new=AsyncMock(return_value=[fake_scene]),
            ),
            patch(service_target, new=AsyncMock(return_value=canned)) as mock_svc,
        ):
            r = client.get(f"/api/v1/imagery/{endpoint}")

        assert r.status_code == 200
        mock_svc.assert_awaited_once()
        call_kwargs = mock_svc.await_args.kwargs
        assert call_kwargs["bbox"] is None
        assert call_kwargs["scene_id"] == fake_scene.id
        # Full-extent path never passes the stitched `scenes=` kwarg
        assert call_kwargs.get("scenes") is None

        body = r.json()
        assert body["scenes_used"] == 1
        assert body["scene_id"] == fake_scene.id
        assert r.headers.get("Cache-Control", "").startswith("public, max-age=")

    @pytest.mark.parametrize("endpoint", COG_ENDPOINTS)
    def test_no_scenes_found_returns_404(self, client, endpoint):
        with patch(
            "ember.routers.imagery.stac_service.search_scenes",
            new=AsyncMock(return_value=[]),
        ):
            r = client.get(f"/api/v1/imagery/{endpoint}")
        assert r.status_code == 404
        assert "No Sentinel-2 scenes found" in r.json()["detail"]


# =============================================================================
# Router-level: /imagery/*-cog bbox path still works (regression)
# =============================================================================


class TestImageryBboxStillWorks:
    def test_ndvi_cog_with_bbox_uses_stitched_path(self, client, fake_scene):
        canned = _index_result(fake_scene, "NDVI", {"min": 0.0, "max": 0.6, "mean": 0.3})
        with (
            patch(
                "ember.routers.imagery.stac_service.search_coverage",
                new=AsyncMock(return_value=[fake_scene]),
            ),
            patch(
                "ember.routers.imagery.sentinel_cog_service.compute_index",
                new=AsyncMock(return_value=canned),
            ) as mock_idx,
        ):
            r = client.get(
                "/api/v1/imagery/ndvi-cog?min_lat=34.0&max_lat=35.0&min_lon=-118.0&max_lon=-117.0"
            )

        assert r.status_code == 200
        call_kwargs = mock_idx.await_args.kwargs
        assert call_kwargs["bbox"] == (-118.0, 34.0, -117.0, 35.0)
        assert call_kwargs["scenes"] is not None
        # bbox responses should NOT carry the 6h full-extent Cache-Control
        assert not r.headers.get("Cache-Control", "").startswith("public, max-age=21600")


# =============================================================================
# Router-level: /terrain?format=raster
# =============================================================================


class TestTerrainFullExtent:
    def test_no_bbox_full_extent_happy_path(self, client, fake_terrain_service):
        with patch(
            "ember.routers.terrain.get_terrain_service", return_value=fake_terrain_service
        ):
            r = client.get("/api/v1/terrain?format=raster&layers=fuel")

        assert r.status_code == 200
        fake_terrain_service.query_terrain_full_extent_raster.assert_awaited_once()
        assert r.headers["Cache-Control"] == "public, max-age=86400"
        body = r.json()
        assert body["layer"] == "fuel"

    def test_no_bbox_forwards_max_size(self, client, fake_terrain_service):
        with patch(
            "ember.routers.terrain.get_terrain_service", return_value=fake_terrain_service
        ):
            r = client.get("/api/v1/terrain?format=raster&layers=fuel&max_size=800")

        assert r.status_code == 200
        call_kwargs = fake_terrain_service.query_terrain_full_extent_raster.await_args.kwargs
        assert call_kwargs["max_size"] == 800

    def test_no_bbox_requires_layers_param(self, client, fake_terrain_service):
        with patch(
            "ember.routers.terrain.get_terrain_service", return_value=fake_terrain_service
        ):
            r = client.get("/api/v1/terrain?format=raster")

        assert r.status_code == 400
        assert "Raster format requires exactly one layer" in r.json()["detail"]

    def test_no_bbox_multiple_layers_rejected(self, client, fake_terrain_service):
        with patch(
            "ember.routers.terrain.get_terrain_service", return_value=fake_terrain_service
        ):
            r = client.get("/api/v1/terrain?format=raster&layers=fuel,elevation")

        assert r.status_code == 400
        assert "exactly one layer" in r.json()["detail"]

    def test_no_bbox_unknown_layer_400(self, client, fake_terrain_service):
        with patch(
            "ember.routers.terrain.get_terrain_service", return_value=fake_terrain_service
        ):
            r = client.get("/api/v1/terrain?format=raster&layers=unobtainium")

        assert r.status_code == 400
        assert "Unknown layer" in r.json()["detail"]

    def test_no_bbox_service_not_configured_returns_503(self, client):
        with patch("ember.routers.terrain.get_terrain_service", return_value=None):
            r = client.get("/api/v1/terrain?format=raster&layers=fuel")

        assert r.status_code == 503
        assert "Terrain service not configured" in r.json()["detail"]

    @pytest.mark.parametrize(
        "qs",
        [
            "format=raster&layers=fuel&min_lat=34.0",  # 1 of 4
            "format=raster&layers=fuel&min_lat=34.0&max_lat=35.0",  # 2 of 4
            "format=raster&layers=fuel&min_lat=34.0&max_lat=35.0&min_lon=-118.0",  # 3 of 4
        ],
    )
    def test_partial_bbox_still_rejects(self, client, fake_terrain_service, qs):
        """Partial bbox on terrain bypasses full-extent path and returns 400."""
        with patch(
            "ember.routers.terrain.get_terrain_service", return_value=fake_terrain_service
        ):
            r = client.get(f"/api/v1/terrain?{qs}")

        assert r.status_code == 400
        assert "Incomplete bbox" in r.json()["detail"]


# =============================================================================
# ORQ-141: bbox-scoped raster responses carry Cache-Control: public, max-age=3600
# =============================================================================

BBOX_QS = "min_lat=34.0&max_lat=35.0&min_lon=-118.0&max_lon=-117.0"

IMAGERY_BBOX_CASES = [
    (
        "ndvi-cog",
        "ember.routers.imagery.stac_service.search_coverage",
        "ember.routers.imagery.sentinel_cog_service.compute_index",
        lambda scene: _index_result(scene, "NDVI", {"min": 0.1, "max": 0.7, "mean": 0.42}),
    ),
    (
        "ndmi-cog",
        "ember.routers.imagery.stac_service.search_coverage",
        "ember.routers.imagery.sentinel_cog_service.compute_index",
        lambda scene: _index_result(scene, "NDMI", {"min": -0.3, "max": 0.4, "mean": 0.05}),
    ),
    (
        "truecolor-cog",
        "ember.routers.imagery.stac_service.search_coverage",
        "ember.routers.imagery.sentinel_cog_service.get_truecolor",
        lambda scene: _truecolor_result(scene),
    ),
]


class TestImageryBboxCacheHeader:
    """ORQ-141 Phase 1: bbox-scoped imagery responses carry 1h Cache-Control."""

    @pytest.mark.parametrize("endpoint,stac_target,svc_target,result_factory", IMAGERY_BBOX_CASES)
    def test_bbox_response_has_1h_cache_control(
        self, client, fake_scene, endpoint, stac_target, svc_target, result_factory
    ):
        canned = result_factory(fake_scene)
        with (
            patch(stac_target, new=AsyncMock(return_value=[fake_scene])),
            patch(svc_target, new=AsyncMock(return_value=canned)),
        ):
            r = client.get(f"/api/v1/imagery/{endpoint}?{BBOX_QS}")

        assert r.status_code == 200
        assert r.headers.get("Cache-Control") == "public, max-age=3600"

    @pytest.mark.parametrize("endpoint,stac_target,svc_target,result_factory", IMAGERY_BBOX_CASES)
    def test_full_extent_still_has_6h_cache_control(
        self, client, fake_scene, endpoint, stac_target, svc_target, result_factory
    ):
        """Regression: full-extent path must still return max-age=21600."""
        canned = result_factory(fake_scene)
        with (
            patch(
                "ember.routers.imagery.stac_service.search_scenes",
                new=AsyncMock(return_value=[fake_scene]),
            ),
            patch(svc_target, new=AsyncMock(return_value=canned)),
        ):
            r = client.get(f"/api/v1/imagery/{endpoint}")

        assert r.status_code == 200
        assert r.headers.get("Cache-Control") == "public, max-age=21600"


class TestTerrainBboxCacheHeader:
    """ORQ-141 Phase 1: bbox-scoped terrain raster response carries 1h Cache-Control."""

    @pytest.fixture
    def fake_terrain_service_with_bbox(self):
        """Terrain service mock with both full-extent and bbox-raster async methods."""
        svc = MagicMock()
        svc.available_layers = ["fuel", "elevation"]
        svc.query_terrain_full_extent_raster = AsyncMock(
            return_value={
                "status": "success",
                "layer": "fuel",
                "bbox": [-125.0, 24.0, -66.0, 50.0],
                "raster": {"format": "geotiff", "encoding": "base64", "data": "fake"},
                "stats": {"min": 91.0, "max": 204.0, "mean": 165.0},
            }
        )
        svc.query_terrain_bbox_raster = AsyncMock(
            return_value={
                "status": "success",
                "layer": "fuel",
                "bbox": [-118.0, 34.0, -117.0, 35.0],
                "raster": {"format": "geotiff", "encoding": "base64", "data": "fake"},
                "stats": {"min": 91.0, "max": 180.0, "mean": 140.0},
            }
        )
        return svc

    def test_bbox_raster_has_1h_cache_control(self, client, fake_terrain_service_with_bbox):
        with patch(
            "ember.routers.terrain.get_terrain_service",
            return_value=fake_terrain_service_with_bbox,
        ):
            r = client.get(
                "/api/v1/terrain?format=raster&layers=fuel"
                "&min_lat=34.0&max_lat=35.0&min_lon=-118.0&max_lon=-117.0"
            )

        assert r.status_code == 200
        fake_terrain_service_with_bbox.query_terrain_bbox_raster.assert_awaited_once()
        assert r.headers.get("Cache-Control") == "public, max-age=3600"

    def test_full_extent_terrain_still_has_24h_cache_control(
        self, client, fake_terrain_service_with_bbox
    ):
        """Regression: full-extent terrain must still return max-age=86400."""
        with patch(
            "ember.routers.terrain.get_terrain_service",
            return_value=fake_terrain_service_with_bbox,
        ):
            r = client.get("/api/v1/terrain?format=raster&layers=fuel")

        assert r.status_code == 200
        fake_terrain_service_with_bbox.query_terrain_full_extent_raster.assert_awaited_once()
        assert r.headers.get("Cache-Control") == "public, max-age=86400"
