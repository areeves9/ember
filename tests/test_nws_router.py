"""Router-level tests for GET /api/v1/nws/fire-weather-alerts."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from ember.auth import verify_token
from ember.main import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_DEV_USER = {"sub": "dev-user", "email": "dev@localhost", "auth_type": "dev"}

_ALERTS_RESULT = {
    "status": "success",
    "generated_at": "2026-03-31T14:00:00Z",
    "alerts": [
        {
            "event": "Red Flag Warning",
            "severity": "Extreme",
            "urgency": "Immediate",
            "headline": "Red Flag Warning issued by NWS Pendleton OR",
            "description": "Critical fire weather conditions expected.",
            "instruction": "A Red Flag Warning means critical fire weather conditions...",
            "onset": "2026-03-29T17:00:00Z",
            "expires": "2026-03-31T03:00:00Z",
            "issuing_office": "NWS Pendleton OR",
            "affected_zones": ["ORZ046"],
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[-120.0, 45.0], [-119.0, 45.0], [-119.0, 46.0], [-120.0, 45.0]]],
            },
            "parameters": {
                "wind_speed_mph": "25-35",
                "wind_gusts_mph": "50",
                "min_relative_humidity_pct": "8-14",
            },
        }
    ],
    "summary": {
        "red_flag_warnings": 1,
        "fire_weather_watches": 0,
        "total_alerts": 1,
    },
    "source": "NOAA/NWS",
    "retrieved_at": "2026-03-31T14:00:00Z",
}

_EMPTY_RESULT = {
    "status": "success",
    "generated_at": "2026-03-31T14:00:00Z",
    "alerts": [],
    "summary": {
        "red_flag_warnings": 0,
        "fire_weather_watches": 0,
        "total_alerts": 0,
    },
    "source": "NOAA/NWS",
    "retrieved_at": "2026-03-31T14:00:00Z",
}


@pytest.fixture
def client():
    app = create_app()
    app.dependency_overrides[verify_token] = lambda: _DEV_USER
    return TestClient(app)


@pytest.fixture
def mock_nws_alerts():
    with patch(
        "ember.routers.nws.nws_service.get_fire_weather_alerts",
        new_callable=AsyncMock,
        return_value=_ALERTS_RESULT.copy(),
    ) as mock:
        yield mock


@pytest.fixture
def mock_nws_empty():
    with patch(
        "ember.routers.nws.nws_service.get_fire_weather_alerts",
        new_callable=AsyncMock,
        return_value=_EMPTY_RESULT.copy(),
    ) as mock:
        yield mock


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestFireWeatherAlertsValidation:
    def test_no_params_returns_400(self, client):
        response = client.get("/api/v1/nws/fire-weather-alerts")
        assert response.status_code == 400
        assert "Must provide" in response.json()["detail"]

    def test_lat_without_lon_returns_400(self, client):
        response = client.get("/api/v1/nws/fire-weather-alerts?lat=45.0")
        assert response.status_code == 400
        assert "Both lat and lon" in response.json()["detail"]

    def test_lon_without_lat_returns_400(self, client):
        response = client.get("/api/v1/nws/fire-weather-alerts?lon=-120.0")
        assert response.status_code == 400
        assert "Both lat and lon" in response.json()["detail"]

    def test_lat_out_of_range_returns_422(self, client):
        response = client.get("/api/v1/nws/fire-weather-alerts?lat=91&lon=-120")
        assert response.status_code == 422

    def test_lon_out_of_range_returns_422(self, client):
        response = client.get("/api/v1/nws/fire-weather-alerts?lat=45&lon=181")
        assert response.status_code == 422

    def test_state_too_long_returns_422(self, client):
        response = client.get("/api/v1/nws/fire-weather-alerts?state=ORE")
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Success tests
# ---------------------------------------------------------------------------


class TestFireWeatherAlertsSuccess:
    def test_point_query_returns_200(self, client, mock_nws_alerts):
        response = client.get("/api/v1/nws/fire-weather-alerts?lat=45.5&lon=-120.5")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["summary"]["total_alerts"] == 1
        assert data["alerts"][0]["event"] == "Red Flag Warning"

    def test_state_query_returns_200(self, client, mock_nws_alerts):
        response = client.get("/api/v1/nws/fire-weather-alerts?state=OR")
        assert response.status_code == 200
        assert response.json()["status"] == "success"

    def test_empty_alerts_returns_200(self, client, mock_nws_empty):
        response = client.get("/api/v1/nws/fire-weather-alerts?lat=45.5&lon=-120.5")
        assert response.status_code == 200
        data = response.json()
        assert data["summary"]["total_alerts"] == 0
        assert data["alerts"] == []

    def test_service_called_with_point_params(self, client, mock_nws_alerts):
        client.get("/api/v1/nws/fire-weather-alerts?lat=45.5&lon=-120.5")
        mock_nws_alerts.assert_called_once_with(lat=45.5, lon=-120.5, state=None)

    def test_service_called_with_state_param(self, client, mock_nws_alerts):
        client.get("/api/v1/nws/fire-weather-alerts?state=OR")
        mock_nws_alerts.assert_called_once_with(lat=None, lon=None, state="OR")

    def test_response_includes_source_and_retrieved_at(self, client, mock_nws_alerts):
        data = client.get("/api/v1/nws/fire-weather-alerts?lat=45.5&lon=-120.5").json()
        assert data["source"] == "NOAA/NWS"
        assert "retrieved_at" in data

    def test_alert_has_geometry(self, client, mock_nws_alerts):
        data = client.get("/api/v1/nws/fire-weather-alerts?lat=45.5&lon=-120.5").json()
        alert = data["alerts"][0]
        assert alert["geometry"]["type"] == "Polygon"
        assert "coordinates" in alert["geometry"]


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------


class TestFireWeatherAlertsErrors:
    def test_service_exception_returns_502(self, client):
        with patch(
            "ember.routers.nws.nws_service.get_fire_weather_alerts",
            new_callable=AsyncMock,
            side_effect=RuntimeError("NWS API timeout"),
        ):
            response = client.get("/api/v1/nws/fire-weather-alerts?lat=45.5&lon=-120.5")
        assert response.status_code == 502
        assert "NWS API error" in response.json()["detail"]

    def test_value_error_returns_400(self, client):
        with patch(
            "ember.routers.nws.nws_service.get_fire_weather_alerts",
            new_callable=AsyncMock,
            side_effect=ValueError("Must provide lat/lon or state"),
        ):
            response = client.get("/api/v1/nws/fire-weather-alerts?lat=45.5&lon=-120.5")
        assert response.status_code == 400
