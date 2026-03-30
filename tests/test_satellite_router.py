"""Router-level tests for GET /api/v1/satellite/track."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from ember.auth import verify_token
from ember.main import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_DEV_USER = {"sub": "dev-user", "email": "dev@localhost", "auth_type": "dev"}

_TRACK_RESULT = {
    "source": "VIIRS_SNPP_NRT",
    "satellite": "Suomi NPP",
    "is_geostationary": False,
    "tle_stale": False,
    "hours_behind": 6,
    "hours_ahead": 6,
    "interval_s": 30,
    "geojson": {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[-120.1, 45.0], [-119.8, 46.0]]},
                "properties": {
                    "track_type": "past",
                    "satellite": "Suomi NPP",
                    "source_key": "VIIRS_SNPP_NRT",
                    "norad_id": 37849,
                    "instrument": "VIIRS",
                    "start_time": "2026-03-30T04:00:00Z",
                    "end_time": "2026-03-30T16:00:00Z",
                },
            },
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[-118.0, 47.0], [-117.5, 48.0]]},
                "properties": {
                    "track_type": "future",
                    "satellite": "Suomi NPP",
                    "source_key": "VIIRS_SNPP_NRT",
                    "norad_id": 37849,
                    "instrument": "VIIRS",
                    "start_time": "2026-03-30T04:00:00Z",
                    "end_time": "2026-03-30T16:00:00Z",
                },
            },
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-118.5, 46.5]},
                "properties": {
                    "track_type": "current_position",
                    "satellite": "Suomi NPP",
                    "source_key": "VIIRS_SNPP_NRT",
                    "norad_id": 37849,
                    "instrument": "VIIRS",
                    "altitude_km": 828.4,
                    "time": "2026-03-30T10:00:00Z",
                    "start_time": "2026-03-30T04:00:00Z",
                    "end_time": "2026-03-30T16:00:00Z",
                },
            },
        ],
    },
}

_GEOSTATIONARY_RESULT = {
    "source": "GOES16_NRT",
    "satellite": "GOES-16 East",
    "is_geostationary": True,
    "refresh_minutes": 15,
    "instrument": "ABI",
    "message": "GOES-16 East is geostationary — continuous coverage, refreshes every 15 min",
    "geojson": {"type": "FeatureCollection", "features": []},
}


@pytest.fixture
def client():
    """TestClient with auth bypassed via dependency override on verify_token."""
    app = create_app()
    app.dependency_overrides[verify_token] = lambda: _DEV_USER
    return TestClient(app)


@pytest.fixture
def unauthenticated_client():
    """TestClient with no auth override — tests 401 enforcement."""
    app = create_app()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def mock_ground_track():
    """Patch satellite_service.get_ground_track with a deterministic result."""
    with patch(
        "ember.routers.satellite.satellite_service.get_ground_track",
        new_callable=AsyncMock,
        return_value=_TRACK_RESULT.copy(),
    ) as mock:
        yield mock


@pytest.fixture
def mock_geostationary_track():
    """Patch satellite_service.get_ground_track to return a geostationary result."""
    with patch(
        "ember.routers.satellite.satellite_service.get_ground_track",
        new_callable=AsyncMock,
        return_value=_GEOSTATIONARY_RESULT.copy(),
    ) as mock:
        yield mock


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestGroundTrackValidation:
    """FastAPI query-param validation for GET /api/v1/satellite/track."""

    def test_missing_source_returns_422(self, client):
        """Required `source` param absent → 422 Unprocessable Entity."""
        response = client.get("/api/v1/satellite/track")
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "param,value",
        [
            ("hours_behind", 0),    # below ge=1
            ("hours_behind", 25),   # above le=24
            ("hours_ahead", 0),     # below ge=1
            ("hours_ahead", 25),    # above le=24
            ("interval_s", 9),      # below ge=10
            ("interval_s", 301),    # above le=300
        ],
    )
    def test_out_of_range_param_returns_422(self, client, param, value):
        """Out-of-range numeric params → 422 before hitting the service."""
        response = client.get(
            f"/api/v1/satellite/track?source=VIIRS_SNPP_NRT&{param}={value}"
        )
        assert response.status_code == 422

    def test_invalid_source_returns_400(self, client):
        """Unknown source key → service raises ValueError → router returns 400."""
        response = client.get("/api/v1/satellite/track?source=INVALID_SAT")
        assert response.status_code == 400
        assert "Unknown source" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Authentication tests
# ---------------------------------------------------------------------------


class TestGroundTrackAuth:
    """Auth enforcement for GET /api/v1/satellite/track."""

    def test_unauthenticated_request_returns_401(self, unauthenticated_client):
        """No Authorization header → 401 when auth is configured."""
        with patch("ember.auth.settings") as mock_settings:
            mock_settings.is_development = False
            mock_settings.supabase_url = "https://test.supabase.co"
            mock_settings.supabase_jwt_secret = ""
            mock_settings.auth0_domain = ""
            response = unauthenticated_client.get(
                "/api/v1/satellite/track?source=VIIRS_SNPP_NRT"
            )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Success tests
# ---------------------------------------------------------------------------


class TestGroundTrackSuccess:
    """Successful response structure for GET /api/v1/satellite/track."""

    def test_response_includes_generated_at(self, client, mock_ground_track):
        """Router must inject generated_at timestamp."""
        response = client.get("/api/v1/satellite/track?source=VIIRS_SNPP_NRT")
        assert response.status_code == 200
        assert "generated_at" in response.json()

    def test_response_metadata_fields(self, client, mock_ground_track):
        """Top-level response must include source, tle_stale, hours_behind/ahead, interval_s, geojson."""
        data = client.get("/api/v1/satellite/track?source=VIIRS_SNPP_NRT").json()
        assert data["source"] == "VIIRS_SNPP_NRT"
        assert "tle_stale" in data
        assert "hours_behind" in data
        assert "hours_ahead" in data
        assert "interval_s" in data
        assert data["geojson"]["type"] == "FeatureCollection"
        assert isinstance(data["geojson"]["features"], list)

    def test_service_called_with_correct_params(self, client, mock_ground_track):
        """Router must forward query params to service correctly."""
        client.get(
            "/api/v1/satellite/track?source=VIIRS_SNPP_NRT&hours_behind=3&hours_ahead=9&interval_s=60"
        )
        mock_ground_track.assert_called_once_with(
            source="VIIRS_SNPP_NRT",
            hours_behind=3,
            hours_ahead=9,
            interval_s=60,
        )

    def test_geostationary_source_returns_200_with_empty_geojson(
        self, client, mock_geostationary_track
    ):
        """GOES sources return 200 with empty features — not a 4xx error."""
        response = client.get("/api/v1/satellite/track?source=GOES16_NRT")
        assert response.status_code == 200
        data = response.json()
        assert data["is_geostationary"] is True
        assert data["geojson"]["features"] == []


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestGroundTrackErrorHandling:
    """Error propagation from service layer to HTTP responses."""

    def test_service_exception_returns_502(self, client):
        """Unexpected service errors → 502 Bad Gateway."""
        with patch(
            "ember.routers.satellite.satellite_service.get_ground_track",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Skyfield propagation failed"),
        ):
            response = client.get("/api/v1/satellite/track?source=VIIRS_SNPP_NRT")
        assert response.status_code == 502
        assert "Ground track error" in response.json()["detail"]

    def test_value_error_returns_400(self, client):
        """Service ValueError (e.g. unknown source) → 400 with detail."""
        with patch(
            "ember.routers.satellite.satellite_service.get_ground_track",
            new_callable=AsyncMock,
            side_effect=ValueError("Unknown source 'BAD'. Valid: VIIRS_SNPP_NRT"),
        ):
            response = client.get("/api/v1/satellite/track?source=BAD")
        assert response.status_code == 400
        assert "Unknown source" in response.json()["detail"]
