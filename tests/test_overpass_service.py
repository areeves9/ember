"""Tests for OverpassService and facility proximity enrichment.

Covers:
- Overpass response parsing (facilities + roads)
- Facility type classification from OSM tags
- Nearest facility/road finding
- Caching (24h TTL)
- Graceful degradation when Overpass unavailable
- Integration with FIRMS clustering pipeline
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("FIRMS_MAP_KEY", "test_key")

from ember.services.overpass import (
    OverpassService,
    _classify_facility,
    _overpass_cache,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def clear_caches():
    """Clear Overpass cache before and after each test."""
    _overpass_cache.clear()
    yield
    _overpass_cache.clear()


@pytest.fixture
def service():
    return OverpassService()


SAMPLE_OVERPASS_RESPONSE = {
    "elements": [
        {
            "type": "node",
            "id": 1,
            "lat": 35.5,
            "lon": -102.3,
            "tags": {
                "man_made": "petroleum_well",
                "name": "Permian Well #42",
            },
        },
        {
            "type": "way",
            "id": 2,
            "center": {"lat": 35.6, "lon": -102.2},
            "tags": {"industrial": "gas", "name": "ADNOC Habshan Complex"},
        },
        {
            "type": "way",
            "id": 3,
            "center": {"lat": 35.55, "lon": -102.25},
            "tags": {"highway": "primary"},
        },
        {
            "type": "way",
            "id": 4,
            "center": {"lat": 35.7, "lon": -102.1},
            "tags": {"military": "range"},
        },
        {
            "type": "node",
            "id": 5,
            "lat": 35.45,
            "lon": -102.35,
            "tags": {"power": "plant", "name": "West Texas Solar"},
        },
    ]
}


# ============================================================================
# Facility type classification
# ============================================================================


class TestFacilityClassification:
    """Test OSM tag to facility type mapping."""

    def test_petroleum_well(self):
        assert _classify_facility({"man_made": "petroleum_well"}) == "oil_well"

    def test_pipeline(self):
        assert _classify_facility({"man_made": "pipeline"}) == "pipeline"

    def test_gas_industrial(self):
        assert _classify_facility({"industrial": "gas"}) == "gas_processing"

    def test_oil_industrial(self):
        assert _classify_facility({"industrial": "oil"}) == "refinery"

    def test_refinery(self):
        assert _classify_facility({"industrial": "refinery"}) == "refinery"

    def test_power_plant(self):
        assert _classify_facility({"power": "plant"}) == "power_plant"

    def test_military(self):
        assert _classify_facility({"military": "range"}) == "military"

    def test_waste_disposal(self):
        assert _classify_facility({"amenity": "waste_disposal"}) == "waste_disposal"

    def test_generic_industrial(self):
        assert _classify_facility({"landuse": "industrial"}) == "industrial_generic"

    def test_unrecognized_tags(self):
        assert _classify_facility({"shop": "bakery"}) is None

    def test_empty_tags(self):
        assert _classify_facility({}) is None


# ============================================================================
# Response parsing
# ============================================================================


class TestOverpassParsing:
    """Test Overpass JSON response parsing."""

    def test_separates_facilities_and_roads(self, service):
        result = service._parse_overpass_response(SAMPLE_OVERPASS_RESPONSE)

        assert len(result["facilities"]) == 4  # well, gas, military, power
        assert len(result["roads"]) == 1  # highway

    def test_facility_has_type_and_coords(self, service):
        result = service._parse_overpass_response(SAMPLE_OVERPASS_RESPONSE)
        well = next(f for f in result["facilities"] if f["type"] == "oil_well")

        assert well["lat"] == 35.5
        assert well["lon"] == -102.3

    def test_facility_name_extracted(self, service):
        result = service._parse_overpass_response(SAMPLE_OVERPASS_RESPONSE)
        well = next(f for f in result["facilities"] if f["type"] == "oil_well")

        assert well["name"] == "Permian Well #42"

    def test_way_uses_center_coords(self, service):
        result = service._parse_overpass_response(SAMPLE_OVERPASS_RESPONSE)
        gas = next(f for f in result["facilities"] if f["type"] == "gas_processing")

        assert gas["lat"] == 35.6
        assert gas["lon"] == -102.2

    def test_skips_elements_without_coords(self, service):
        data = {"elements": [{"type": "way", "id": 99, "tags": {"highway": "primary"}}]}
        result = service._parse_overpass_response(data)

        assert result["facilities"] == []
        assert result["roads"] == []


# ============================================================================
# Nearest finding
# ============================================================================


class TestNearestFinding:
    """Test nearest facility and road finding."""

    def test_find_nearest_facilities_returns_closest(self, service):
        facilities = [
            {"lat": 35.5, "lon": -102.3, "type": "oil_well", "name": "Well A"},
            {"lat": 36.0, "lon": -101.0, "type": "refinery", "name": "Refinery B"},
        ]

        result = service.find_nearest_facilities(35.51, -102.31, facilities)

        assert result["nearest"] is not None
        assert result["nearest"]["type"] == "oil_well"
        assert result["nearest"]["name"] == "Well A"
        assert result["nearest"]["km"] < 2.0

    def test_nearby_facilities_sorted_by_distance(self, service):
        facilities = [
            {"lat": 35.7, "lon": -102.1, "type": "refinery", "name": "Far"},
            {"lat": 35.5, "lon": -102.3, "type": "oil_well", "name": "Close"},
            {"lat": 35.6, "lon": -102.2, "type": "gas_processing", "name": "Mid"},
        ]

        result = service.find_nearest_facilities(35.5, -102.3, facilities)

        assert len(result["nearby"]) == 3
        assert result["nearby"][0]["name"] == "Close"
        assert result["nearby"][0]["distance_km"] < result["nearby"][1]["distance_km"]

    def test_nearby_capped_at_top_n(self, service):
        facilities = [
            {"lat": 35.5 + i * 0.01, "lon": -102.3, "type": "oil_well"} for i in range(10)
        ]

        result = service.find_nearest_facilities(35.5, -102.3, facilities, top_n=3)

        assert len(result["nearby"]) == 3

    def test_no_facility_in_range(self, service):
        facilities = [
            {"lat": 50.0, "lon": -50.0, "type": "refinery"},
        ]

        result = service.find_nearest_facilities(35.5, -102.3, facilities, max_km=50.0)
        assert result["nearest"] is None
        assert result["nearby"] == []

    def test_empty_facilities_list(self, service):
        result = service.find_nearest_facilities(35.5, -102.3, [])
        assert result["nearest"] is None
        assert result["nearby"] == []

    def test_find_nearest_road(self, service):
        roads = [
            {"lat": 35.55, "lon": -102.25},
            {"lat": 36.0, "lon": -101.0},
        ]

        result = service.find_nearest_road(35.5, -102.3, roads)
        assert result is not None
        assert result < 10.0

    def test_no_road_in_range(self, service):
        roads = [{"lat": 50.0, "lon": -50.0}]

        result = service.find_nearest_road(35.5, -102.3, roads, max_km=50.0)
        assert result is None

    def test_empty_roads_list(self, service):
        assert service.find_nearest_road(35.5, -102.3, []) is None


# ============================================================================
# Caching
# ============================================================================


class TestOverpassCaching:
    """Test Overpass result caching."""

    @patch("ember.services.overpass.httpx.AsyncClient")
    async def test_second_call_hits_cache(self, mock_client_class, service):
        """Same bbox query hits cache on second call."""
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = SAMPLE_OVERPASS_RESPONSE
        mock_resp.raise_for_status = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_class.return_value.__aenter__.return_value = mock_client

        await service.query_infrastructure(35.0, 36.0, -103.0, -101.0)
        call_count = mock_client.post.call_count

        await service.query_infrastructure(35.0, 36.0, -103.0, -101.0)
        assert mock_client.post.call_count == call_count


# ============================================================================
# Graceful degradation
# ============================================================================


class TestOverpassGracefulDegradation:
    """Test behavior when Overpass is unavailable."""

    @patch("ember.services.overpass.httpx.AsyncClient")
    async def test_overpass_failure_returns_empty(self, mock_client_class, service):
        """Overpass timeout returns empty lists, not exception."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("Overpass timeout"))
        mock_client_class.return_value.__aenter__.return_value = mock_client

        result = await service.query_infrastructure(35.0, 36.0, -103.0, -101.0)

        assert result == {"facilities": [], "roads": []}


# ============================================================================
# Integration with FIRMS clustering
# ============================================================================


class TestFacilityEnrichmentIntegration:
    """Test facility proximity enrichment in the FIRMS pipeline.

    Mocks the overpass_service singleton at the import site in firms.py
    so only one httpx patch is needed (for FIRMS). Follows the same
    httpx mock pattern as test_satellite.py.
    """

    async def test_clusters_get_facility_properties(self):
        """Clusters include facility proximity properties after enrichment."""
        from ember.services.firms import FirmsService, _baseline_cache, _fires_cache

        _fires_cache.clear()
        _baseline_cache.clear()

        firms_csv = (
            "latitude,longitude,bright_ti4,frp,confidence,"
            "acq_date,acq_time,satellite,daynight\n"
            "35.5,-102.3,350,500,nominal,2026-04-06,1420,N,D\n"
        )

        firms_resp = MagicMock()
        firms_resp.text = firms_csv
        firms_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = firms_resp

        # Parse sample Overpass response for the mock
        parsed_infra = OverpassService._parse_overpass_response(SAMPLE_OVERPASS_RESPONSE)

        mock_overpass = MagicMock()
        mock_overpass.query_infrastructure = AsyncMock(return_value=parsed_infra)
        mock_overpass.find_nearest_facilities = OverpassService.find_nearest_facilities
        mock_overpass.find_nearest_road = OverpassService.find_nearest_road

        with (
            patch("httpx.AsyncClient") as mock_cls,
            patch("ember.services.firms.overpass_service", mock_overpass),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            svc = FirmsService()
            svc.api_key = "test_key"
            result = await svc.get_fires(min_lat=35.0, max_lat=36.0, min_lon=-103.0, max_lon=-101.0)

        features = result["geojson"]["features"]
        marker = next(f for f in features if f["properties"]["layer"] == "marker")
        props = marker["properties"]

        assert "nearest_facility_km" in props
        assert "nearest_facility_type" in props
        assert "nearest_facility_name" in props
        assert "nearest_road_km" in props

        assert props["nearest_facility_type"] == "oil_well"
        assert props["nearest_facility_name"] == "Permian Well #42"
        assert props["nearest_facility_km"] is not None
        assert props["nearest_facility_km"] < 1.0

        assert "nearby_facilities" in props
        assert isinstance(props["nearby_facilities"], list)
        assert len(props["nearby_facilities"]) > 0
        assert props["nearby_facilities"][0]["type"] == "oil_well"

        _fires_cache.clear()
        _baseline_cache.clear()

    async def test_overpass_failure_doesnt_break_clustering(self):
        """Overpass failure still produces clusters with null facility fields."""
        from ember.services.firms import FirmsService, _baseline_cache, _fires_cache

        _fires_cache.clear()
        _baseline_cache.clear()

        firms_csv = (
            "latitude,longitude,bright_ti4,frp,confidence,"
            "acq_date,acq_time,satellite,daynight\n"
            "35.5,-102.3,350,500,nominal,2026-04-06,1420,N,D\n"
        )

        firms_resp = MagicMock()
        firms_resp.text = firms_csv
        firms_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = firms_resp

        mock_overpass = MagicMock()
        mock_overpass.query_infrastructure = AsyncMock(side_effect=Exception("Overpass down"))

        with (
            patch("httpx.AsyncClient") as mock_cls,
            patch("ember.services.firms.overpass_service", mock_overpass),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            svc = FirmsService()
            svc.api_key = "test_key"
            result = await svc.get_fires(min_lat=35.0, max_lat=36.0, min_lon=-103.0, max_lon=-101.0)

        assert result["cluster_count"] >= 1
        features = result["geojson"]["features"]
        marker = next(f for f in features if f["properties"]["layer"] == "marker")
        props = marker["properties"]

        assert "nearest_facility_km" in props
        assert props["nearest_facility_km"] is None
        assert props["nearest_facility_type"] is None
        assert props["nearest_road_km"] is None
        assert props["nearby_facilities"] == []

        _fires_cache.clear()
        _baseline_cache.clear()
