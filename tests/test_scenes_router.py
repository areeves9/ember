#!/usr/bin/env python3
"""Integration tests for the scenes router and truecolor-cog convenience endpoint."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from ember.auth import verify_token
from ember.main import app
from ember.services.stac import Scene

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
def mock_scene():
    return Scene(
        id="S2A_11SLT_20260315_0_L2A",
        datetime="2026-03-15T18:32:15Z",
        cloud_cover=8.2,
        bbox=(-118.6, 33.9, -117.9, 34.6),
        assets={
            "B02": "s3://sentinel-cogs/path/B02.tif",
            "B03": "s3://sentinel-cogs/path/B03.tif",
            "B04": "s3://sentinel-cogs/path/B04.tif",
            "B08": "s3://sentinel-cogs/path/B08.tif",
            "B11": "s3://sentinel-cogs/path/B11.tif",
            "B12": "s3://sentinel-cogs/path/B12.tif",
        },
    )


@pytest.fixture
def mock_truecolor_result():
    return {
        "status": "success",
        "scene_id": "S2A_11SLT_20260315_0_L2A",
        "bbox": [-118.5, 34.0, -118.0, 34.5],
        "bands": ["B04", "B03", "B02"],
        "raster": {
            "format": "image/png",
            "encoding": "base64",
            "data": "iVBORw0KGgo=",
            "width": 512,
            "height": 512,
        },
        "source": "Sentinel-2 L2A (AWS COG)",
    }


@pytest.fixture
def mock_index_result():
    return {
        "status": "success",
        "scene_id": "S2A_11SLT_20260315_0_L2A",
        "index": "NDVI",
        "bbox": [-118.5, 34.0, -118.0, 34.5],
        "bands_used": ["B08", "B04"],
        "stats": {"min": -0.1, "max": 0.8, "mean": 0.45},
        "raster": {
            "format": "geotiff",
            "encoding": "base64",
            "data": "base64data",
            "width": 512,
            "height": 512,
        },
        "source": "Sentinel-2 L2A (AWS COG)",
    }


# =============================================================================
# GET /scenes/search
# =============================================================================


class TestSearchScenes:
    def test_returns_scenes(self, client, mock_scene):
        with patch(
            "ember.routers.scenes.stac_service.search_scenes",
            new_callable=AsyncMock,
            return_value=[mock_scene],
        ):
            resp = client.get(
                "/api/v1/scenes/search",
                params={
                    "min_lon": -118.5,
                    "min_lat": 34.0,
                    "max_lon": -118.0,
                    "max_lat": 34.5,
                    "start_date": "2026-03-01",
                    "end_date": "2026-03-31",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["count"] == 1
        assert data["scenes"][0]["id"] == "S2A_11SLT_20260315_0_L2A"

    def test_validates_bbox_ordering(self, client):
        resp = client.get(
            "/api/v1/scenes/search",
            params={
                "min_lon": -118.0,
                "min_lat": 35.0,  # min > max
                "max_lon": -118.5,
                "max_lat": 34.0,
                "start_date": "2026-03-01",
                "end_date": "2026-03-31",
            },
        )
        assert resp.status_code == 400

    def test_validates_date_ordering(self, client):
        resp = client.get(
            "/api/v1/scenes/search",
            params={
                "min_lon": -118.5,
                "min_lat": 34.0,
                "max_lon": -118.0,
                "max_lat": 34.5,
                "start_date": "2026-04-01",
                "end_date": "2026-03-01",
            },
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        "missing_param",
        ["min_lon", "min_lat", "max_lon", "max_lat", "start_date", "end_date"],
    )
    def test_requires_all_params(self, client, missing_param):
        params = {
            "min_lon": -118.5,
            "min_lat": 34.0,
            "max_lon": -118.0,
            "max_lat": 34.5,
            "start_date": "2026-03-01",
            "end_date": "2026-03-31",
        }
        del params[missing_param]
        resp = client.get("/api/v1/scenes/search", params=params)
        assert resp.status_code == 422  # FastAPI validation error


# =============================================================================
# GET /scenes/{scene_id}/bands
# =============================================================================


class TestGetSceneBands:
    def test_returns_truecolor(self, client, mock_scene, mock_truecolor_result):
        with (
            patch(
                "ember.routers.scenes.stac_service.get_scene",
                new_callable=AsyncMock,
                return_value=mock_scene,
            ),
            patch(
                "ember.routers.scenes.sentinel_cog_service.get_truecolor",
                new_callable=AsyncMock,
                return_value=mock_truecolor_result,
            ),
        ):
            resp = client.get(
                "/api/v1/scenes/S2A_11SLT_20260315_0_L2A/bands",
                params={
                    "min_lon": -118.5,
                    "min_lat": 34.0,
                    "max_lon": -118.0,
                    "max_lat": 34.5,
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["datetime"] == "2026-03-15T18:32:15Z"
        assert data["cloud_cover"] == 8.2

    def test_returns_404_for_unknown_scene(self, client):
        with patch(
            "ember.routers.scenes.stac_service.get_scene",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.get(
                "/api/v1/scenes/NONEXISTENT/bands",
                params={
                    "min_lon": -118.5,
                    "min_lat": 34.0,
                    "max_lon": -118.0,
                    "max_lat": 34.5,
                },
            )

        assert resp.status_code == 404

    def test_validates_format(self, client, mock_scene):
        with patch(
            "ember.routers.scenes.stac_service.get_scene",
            new_callable=AsyncMock,
            return_value=mock_scene,
        ):
            resp = client.get(
                "/api/v1/scenes/S2A_TEST/bands",
                params={
                    "min_lon": -118.5,
                    "min_lat": 34.0,
                    "max_lon": -118.0,
                    "max_lat": 34.5,
                    "format": "invalid",
                },
            )
        assert resp.status_code == 400


# =============================================================================
# GET /scenes/{scene_id}/index
# =============================================================================


class TestGetSceneIndex:
    def test_returns_ndvi(self, client, mock_scene, mock_index_result):
        with (
            patch(
                "ember.routers.scenes.stac_service.get_scene",
                new_callable=AsyncMock,
                return_value=mock_scene,
            ),
            patch(
                "ember.routers.scenes.sentinel_cog_service.compute_index",
                new_callable=AsyncMock,
                return_value=mock_index_result,
            ),
        ):
            resp = client.get(
                "/api/v1/scenes/S2A_11SLT_20260315_0_L2A/index",
                params={
                    "index": "ndvi",
                    "min_lon": -118.5,
                    "min_lat": 34.0,
                    "max_lon": -118.0,
                    "max_lat": 34.5,
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["index"] == "NDVI"
        assert "stats" in data

    def test_rejects_unknown_index(self, client, mock_scene):
        with patch(
            "ember.routers.scenes.stac_service.get_scene",
            new_callable=AsyncMock,
            return_value=mock_scene,
        ):
            resp = client.get(
                "/api/v1/scenes/S2A_TEST/index",
                params={
                    "index": "fake_index",
                    "min_lon": -118.5,
                    "min_lat": 34.0,
                    "max_lon": -118.0,
                    "max_lat": 34.5,
                },
            )
        assert resp.status_code == 400

    def test_validates_format(self, client, mock_scene):
        with patch(
            "ember.routers.scenes.stac_service.get_scene",
            new_callable=AsyncMock,
            return_value=mock_scene,
        ):
            resp = client.get(
                "/api/v1/scenes/S2A_TEST/index",
                params={
                    "index": "ndvi",
                    "min_lon": -118.5,
                    "min_lat": 34.0,
                    "max_lon": -118.0,
                    "max_lat": 34.5,
                    "format": "bad",
                },
            )
        assert resp.status_code == 400


# =============================================================================
# GET /imagery/truecolor-cog
# =============================================================================


class TestTruecolorCog:
    def test_returns_image(self, client, mock_scene, mock_truecolor_result):
        with (
            patch(
                "ember.routers.imagery.stac_service.search_coverage",
                new_callable=AsyncMock,
                return_value=[mock_scene],
            ),
            patch(
                "ember.routers.imagery.sentinel_cog_service.get_truecolor",
                new_callable=AsyncMock,
                return_value=mock_truecolor_result,
            ),
        ):
            resp = client.get(
                "/api/v1/imagery/truecolor-cog",
                params={
                    "min_lon": -118.5,
                    "min_lat": 34.0,
                    "max_lon": -118.0,
                    "max_lat": 34.5,
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["datetime"] == "2026-03-15T18:32:15Z"

    def test_returns_404_when_no_scenes(self, client):
        with patch(
            "ember.routers.imagery.stac_service.search_coverage",
            new_callable=AsyncMock,
            return_value=[],
        ):
            resp = client.get(
                "/api/v1/imagery/truecolor-cog",
                params={
                    "min_lon": -118.5,
                    "min_lat": 34.0,
                    "max_lon": -118.0,
                    "max_lat": 34.5,
                },
            )
        assert resp.status_code == 404

    def test_validates_bbox_ordering(self, client):
        resp = client.get(
            "/api/v1/imagery/truecolor-cog",
            params={
                "min_lon": -118.0,
                "min_lat": 35.0,
                "max_lon": -118.5,
                "max_lat": 34.0,
            },
        )
        assert resp.status_code == 400

    def test_validates_format(self, client):
        resp = client.get(
            "/api/v1/imagery/truecolor-cog",
            params={
                "min_lon": -118.5,
                "min_lat": 34.0,
                "max_lon": -118.0,
                "max_lat": 34.5,
                "format": "bmp",
            },
        )
        assert resp.status_code == 400


# =============================================================================
# GET /imagery/ndvi-cog
# =============================================================================


class TestNdviCog:
    def test_returns_ndvi_with_interpretation(self, client, mock_scene, mock_index_result):
        with (
            patch(
                "ember.routers.imagery.stac_service.search_coverage",
                new_callable=AsyncMock,
                return_value=[mock_scene],
            ),
            patch(
                "ember.routers.imagery.sentinel_cog_service.compute_index",
                new_callable=AsyncMock,
                return_value=mock_index_result,
            ),
        ):
            resp = client.get(
                "/api/v1/imagery/ndvi-cog",
                params={
                    "min_lon": -118.5,
                    "min_lat": 34.0,
                    "max_lon": -118.0,
                    "max_lat": 34.5,
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert "ndvi" in data
        assert "mean" in data["ndvi"]
        assert "vegetation_status" in data["ndvi"]
        assert data["datetime"] == "2026-03-15T18:32:15Z"
        assert data["cloud_cover"] == 8.2
        assert "date_range" in data

    def test_vegetation_status_thresholds(self, client, mock_scene):
        """Verify interpretation matches Copernicus thresholds."""
        for mean, expected_status in [
            (0.05, "Bare/Barren"),
            (0.15, "Sparse Vegetation"),
            (0.3, "Moderate Vegetation"),
            (0.5, "Healthy Vegetation"),
            (0.7, "Dense Vegetation"),
        ]:
            index_result = {
                "status": "success",
                "scene_id": mock_scene.id,
                "index": "NDVI",
                "bbox": [-118.5, 34.0, -118.0, 34.5],
                "bands_used": ["B08", "B04"],
                "stats": {"min": mean - 0.1, "max": mean + 0.1, "mean": mean},
                "source": "Sentinel-2 L2A (AWS COG)",
            }
            with (
                patch(
                    "ember.routers.imagery.stac_service.search_coverage",
                    new_callable=AsyncMock,
                    return_value=[mock_scene],
                ),
                patch(
                    "ember.routers.imagery.sentinel_cog_service.compute_index",
                    new_callable=AsyncMock,
                    return_value=index_result,
                ),
            ):
                resp = client.get(
                    "/api/v1/imagery/ndvi-cog",
                    params={
                        "min_lon": -118.5,
                        "min_lat": 34.0,
                        "max_lon": -118.0,
                        "max_lat": 34.5,
                    },
                )
            assert resp.json()["ndvi"]["vegetation_status"] == expected_status

    def test_returns_404_when_no_scenes(self, client):
        with patch(
            "ember.routers.imagery.stac_service.search_coverage",
            new_callable=AsyncMock,
            return_value=[],
        ):
            resp = client.get(
                "/api/v1/imagery/ndvi-cog",
                params={
                    "min_lon": -118.5,
                    "min_lat": 34.0,
                    "max_lon": -118.0,
                    "max_lat": 34.5,
                },
            )
        assert resp.status_code == 404

    def test_validates_format(self, client):
        resp = client.get(
            "/api/v1/imagery/ndvi-cog",
            params={
                "min_lon": -118.5,
                "min_lat": 34.0,
                "max_lon": -118.0,
                "max_lat": 34.5,
                "format": "png",
            },
        )
        assert resp.status_code == 400


# =============================================================================
# GET /imagery/ndmi-cog
# =============================================================================


class TestNdmiCog:
    def test_returns_ndmi_with_interpretation(self, client, mock_scene, mock_index_result):
        ndmi_result = {**mock_index_result, "index": "NDMI", "bands_used": ["B08", "B11"]}
        with (
            patch(
                "ember.routers.imagery.stac_service.search_coverage",
                new_callable=AsyncMock,
                return_value=[mock_scene],
            ),
            patch(
                "ember.routers.imagery.sentinel_cog_service.compute_index",
                new_callable=AsyncMock,
                return_value=ndmi_result,
            ),
        ):
            resp = client.get(
                "/api/v1/imagery/ndmi-cog",
                params={
                    "min_lon": -118.5,
                    "min_lat": 34.0,
                    "max_lon": -118.0,
                    "max_lat": 34.5,
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert "ndmi" in data
        assert "moisture_status" in data["ndmi"]
        assert "fire_risk" in data["ndmi"]
        assert data["datetime"] == "2026-03-15T18:32:15Z"

    def test_fire_risk_thresholds(self, client, mock_scene):
        """Verify fire risk interpretation matches Copernicus thresholds."""
        for mean, expected_risk in [
            (-0.2, "High"),
            (0.0, "Moderate"),
            (0.2, "Low"),
        ]:
            index_result = {
                "status": "success",
                "scene_id": mock_scene.id,
                "index": "NDMI",
                "bbox": [-118.5, 34.0, -118.0, 34.5],
                "bands_used": ["B08", "B11"],
                "stats": {"min": mean - 0.1, "max": mean + 0.1, "mean": mean},
                "source": "Sentinel-2 L2A (AWS COG)",
            }
            with (
                patch(
                    "ember.routers.imagery.stac_service.search_coverage",
                    new_callable=AsyncMock,
                    return_value=[mock_scene],
                ),
                patch(
                    "ember.routers.imagery.sentinel_cog_service.compute_index",
                    new_callable=AsyncMock,
                    return_value=index_result,
                ),
            ):
                resp = client.get(
                    "/api/v1/imagery/ndmi-cog",
                    params={
                        "min_lon": -118.5,
                        "min_lat": 34.0,
                        "max_lon": -118.0,
                        "max_lat": 34.5,
                    },
                )
            assert resp.json()["ndmi"]["fire_risk"] == expected_risk

    def test_returns_404_when_no_scenes(self, client):
        with patch(
            "ember.routers.imagery.stac_service.search_coverage",
            new_callable=AsyncMock,
            return_value=[],
        ):
            resp = client.get(
                "/api/v1/imagery/ndmi-cog",
                params={
                    "min_lon": -118.5,
                    "min_lat": 34.0,
                    "max_lon": -118.0,
                    "max_lat": 34.5,
                },
            )
        assert resp.status_code == 404
