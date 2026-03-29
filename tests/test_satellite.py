"""Unit tests for satellite pass prediction service."""

from time import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ember.exceptions import ExternalAPIError
from ember.services.satellite import (
    _NORAD_NAMES,
    _TLE_CACHE_MAX_SIZE,
    _TLE_CACHE_TTL,
    SATELLITE_REGISTRY,
    SatelliteService,
    _azimuth_to_compass,
    _tle_cache,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Real TLE for Suomi NPP (NORAD 37849) — used for deterministic skyfield tests
SAMPLE_TLE_NAME = "SUOMI NPP"
SAMPLE_TLE_LINE1 = "1 37849U 11061A   26088.50000000  .00000040  00000-0  30000-4 0  9990"
SAMPLE_TLE_LINE2 = "2 37849  98.7200 120.0000 0001000  90.0000 270.0000 14.19552000600000"
SAMPLE_NORAD_ID = 37849

SAMPLE_TLE_RESPONSE = f"{SAMPLE_TLE_NAME}\n{SAMPLE_TLE_LINE1}\n{SAMPLE_TLE_LINE2}\n"


@pytest.fixture(autouse=True)
def clear_tle_cache():
    """Reset TLE cache before each test to prevent bleed between tests."""
    _tle_cache.clear()
    yield
    _tle_cache.clear()


@pytest.fixture
def service():
    """Create a SatelliteService with mocked ephemeris to avoid de421.bsp dependency."""
    with patch("ember.services.satellite.sf_load") as mock_load:
        mock_eph = MagicMock()
        mock_load.return_value = mock_eph
        mock_load.timescale.return_value = MagicMock()

        svc = SatelliteService()
        # Ensure ephemeris is marked available and sun angle returns a fixed value
        svc._ephemeris_available = True
        return svc


@pytest.fixture
def service_no_ephemeris():
    """Create a SatelliteService without ephemeris (sun angle unavailable)."""
    with patch("ember.services.satellite.sf_load") as mock_load:
        mock_load.side_effect = Exception("no ephemeris")
        mock_load.timescale.return_value = MagicMock()

        svc = SatelliteService()
        svc._ephemeris_available = False
        return svc


@pytest.fixture
def mock_celestrak_response():
    """Mock a successful CelesTrak TLE response."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = SAMPLE_TLE_RESPONSE
    mock_response.raise_for_status = MagicMock()
    return mock_response


def _seed_cache(norad_id, age_seconds=0):
    """Helper to seed the TLE cache with a known entry."""
    _tle_cache[norad_id] = {
        "timestamp": time() - age_seconds,
        "data": {
            "name": SAMPLE_TLE_NAME,
            "tle_line1": SAMPLE_TLE_LINE1,
            "tle_line2": SAMPLE_TLE_LINE2,
        },
    }


# ===========================================================================
# Registry & Static Logic
# ===========================================================================


class TestSatelliteRegistry:
    """Verify satellite registry contains expected entries."""

    EXPECTED_POLAR_SOURCES = [
        "VIIRS_SNPP_NRT",
        "VIIRS_NOAA20_NRT",
        "VIIRS_NOAA21_NRT",
        "MODIS_NRT",
    ]
    EXPECTED_GEO_SOURCES = ["GOES16_NRT", "GOES17_NRT", "GOES18_NRT"]

    @pytest.mark.parametrize("source", EXPECTED_POLAR_SOURCES)
    def test_polar_source_exists_and_has_norad_ids(self, source):
        info = SATELLITE_REGISTRY[source]
        assert not info.is_geostationary
        assert len(info.norad_ids) >= 1
        assert info.swath_km > 0
        assert info.instrument in ("VIIRS", "MODIS")

    @pytest.mark.parametrize("source", EXPECTED_GEO_SOURCES)
    def test_geostationary_source_has_no_norad_ids(self, source):
        info = SATELLITE_REGISTRY[source]
        assert info.is_geostationary
        assert len(info.norad_ids) == 0
        assert info.refresh_minutes is not None
        assert info.instrument == "ABI"

    def test_modis_has_two_satellites(self):
        info = SATELLITE_REGISTRY["MODIS_NRT"]
        assert len(info.norad_ids) == 2
        assert 25994 in info.norad_ids  # Terra
        assert 27424 in info.norad_ids  # Aqua

    def test_norad_names_cover_all_polar_ids(self):
        all_norad_ids = set()
        for info in SATELLITE_REGISTRY.values():
            all_norad_ids.update(info.norad_ids)
        for norad_id in all_norad_ids:
            assert norad_id in _NORAD_NAMES


class TestAzimuthToCompass:
    """Test compass direction conversion from azimuth degrees."""

    @pytest.mark.parametrize(
        "azimuth,expected",
        [
            (0, "N"),
            (45, "NE"),
            (90, "E"),
            (135, "SE"),
            (180, "S"),
            (225, "SW"),
            (270, "W"),
            (315, "NW"),
            (360, "N"),  # wrap-around
            (22, "N"),  # within N bucket
            (23, "NE"),  # boundary rounds to NE
            (359, "N"),  # just before 0
        ],
    )
    def test_azimuth_directions(self, azimuth, expected):
        assert _azimuth_to_compass(azimuth) == expected


class TestQualityScore:
    """Test composite quality score computation."""

    @pytest.mark.parametrize(
        "elevation,solar_elev,min_score,max_score",
        [
            # Daytime, high elevation — best case
            (90.0, 45.0, 90, 100),
            # Daytime, low elevation
            (10.0, 30.0, 40, 55),
            # Night, high elevation
            (90.0, -20.0, 70, 85),
            # Civil twilight, medium elevation
            (45.0, -3.0, 45, 60),
            # Night, low elevation — worst case
            (10.0, -20.0, 20, 35),
        ],
    )
    def test_quality_score_ranges(self, elevation, solar_elev, min_score, max_score):
        score = SatelliteService._compute_quality_score(elevation, solar_elev)
        assert score is not None
        assert min_score <= score <= max_score

    def test_quality_score_none_when_no_solar_data(self):
        assert SatelliteService._compute_quality_score(45.0, None) is None

    def test_quality_score_is_integer(self):
        score = SatelliteService._compute_quality_score(60.0, 30.0)
        assert isinstance(score, int)


class TestGeostationaryInfo:
    """Test static geostationary response construction."""

    @pytest.mark.parametrize("source", ["GOES16_NRT", "GOES17_NRT", "GOES18_NRT"])
    def test_geostationary_response_structure(self, source):
        info = SATELLITE_REGISTRY[source]
        result = SatelliteService._geostationary_info(source, info)

        assert result["source"] == source
        assert result["is_geostationary"] is True
        assert result["refresh_minutes"] == 15
        assert result["instrument"] == "ABI"
        assert result["satellite"] == info.name
        assert "message" in result


# ===========================================================================
# TLE Fetching & Caching
# ===========================================================================


class TestTLEFetching:
    """Test TLE fetch, caching, and fallback behavior."""

    async def test_cache_hit_skips_network_call(self, service):
        """Fresh cache entry should be returned without making an HTTP request."""
        _seed_cache(SAMPLE_NORAD_ID, age_seconds=100)  # well within TTL

        with patch("httpx.AsyncClient") as mock_client_cls:
            result = await service._fetch_tle(SAMPLE_NORAD_ID)

            mock_client_cls.assert_not_called()
            assert result["tle_stale"] is False
            assert result["name"] == SAMPLE_TLE_NAME

    async def test_cache_miss_fetches_from_celestrak(self, service, mock_celestrak_response):
        """Empty cache should trigger a CelesTrak fetch and populate cache."""
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_celestrak_response

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await service._fetch_tle(SAMPLE_NORAD_ID)

        assert result["tle_stale"] is False
        assert result["tle_line1"] == SAMPLE_TLE_LINE1
        assert result["tle_line2"] == SAMPLE_TLE_LINE2
        assert SAMPLE_NORAD_ID in _tle_cache

    async def test_stale_cache_returned_on_network_failure(self, service):
        """When CelesTrak is down and stale cache exists, return it with tle_stale=True."""
        _seed_cache(SAMPLE_NORAD_ID, age_seconds=_TLE_CACHE_TTL + 100)  # expired

        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("connection refused")

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await service._fetch_tle(SAMPLE_NORAD_ID)

        assert result["tle_stale"] is True
        assert result["name"] == SAMPLE_TLE_NAME

    async def test_no_cache_and_network_failure_raises(self, service):
        """When CelesTrak is down and no cache exists, raise ExternalAPIError."""
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("connection refused")

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ExternalAPIError) as exc_info:
                await service._fetch_tle(SAMPLE_NORAD_ID)

            assert "NORAD 37849" in str(exc_info.value.message)

    async def test_malformed_tle_response_treated_as_failure(self, service):
        """Response with fewer than 3 lines should be treated as a fetch failure."""
        mock_response = MagicMock()
        mock_response.text = "SUOMI NPP\n1 37849U ...\n"  # only 2 lines
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ExternalAPIError):
                await service._fetch_tle(SAMPLE_NORAD_ID)

    async def test_cache_eviction_on_max_size(self, service, mock_celestrak_response):
        """Cache should clear when max size is reached."""
        # Fill cache to max
        for i in range(_TLE_CACHE_MAX_SIZE):
            _tle_cache[i] = {"timestamp": time(), "data": {"name": f"SAT-{i}"}}

        assert len(_tle_cache) == _TLE_CACHE_MAX_SIZE

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_celestrak_response

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await service._fetch_tle(SAMPLE_NORAD_ID)

        # Cache should have been cleared then repopulated with new entry
        assert len(_tle_cache) == 1
        assert SAMPLE_NORAD_ID in _tle_cache


# ===========================================================================
# Pass Prediction (get_passes integration)
# ===========================================================================


class TestGetPasses:
    """Test the main get_passes method with mocked TLE fetching."""

    PASS_REQUIRED_FIELDS = {
        "satellite",
        "norad_id",
        "source_key",
        "instrument",
        "aos",
        "tca",
        "los",
        "max_elevation_deg",
        "direction",
        "swath_km",
        "time_until_s",
        "solar_elevation_deg",
        "is_daytime_pass",
        "quality_score",
    }

    async def test_unknown_source_raises_value_error(self, service):
        with pytest.raises(ValueError, match="Unknown source"):
            await service.get_passes("INVALID_SOURCE", 34.0, -118.0)

    @pytest.mark.parametrize("source", ["GOES16_NRT", "GOES17_NRT", "GOES18_NRT"])
    async def test_geostationary_returns_static_info(self, service, source):
        result = await service.get_passes(source, 34.0, -118.0)
        assert result["is_geostationary"] is True
        assert result["refresh_minutes"] == 15
        assert "passes" not in result

    async def test_polar_source_returns_passes_with_required_fields(self, service):
        """VIIRS source should return passes with all expected fields."""
        _seed_cache(SAMPLE_NORAD_ID, age_seconds=100)
        # Mock sun angle to return a fixed value
        service._compute_sun_angle = MagicMock(return_value=35.0)

        result = await service.get_passes("VIIRS_SNPP_NRT", 34.05, -118.25, hours=24)

        assert result["source"] == "VIIRS_SNPP_NRT"
        assert result["is_geostationary"] is False
        assert isinstance(result["passes"], list)
        assert result["pass_count"] == len(result["passes"])

        if result["passes"]:
            first_pass = result["passes"][0]
            assert self.PASS_REQUIRED_FIELDS.issubset(first_pass.keys())
            assert first_pass["source_key"] == "VIIRS_SNPP_NRT"
            assert first_pass["instrument"] == "VIIRS"
            assert first_pass["norad_id"] == SAMPLE_NORAD_ID
            assert first_pass["swath_km"] == 3060.0

    async def test_modis_returns_passes_from_both_satellites(self, service):
        """MODIS source should fetch TLEs for both Terra and Aqua."""
        _seed_cache(25994, age_seconds=100)  # Terra
        _seed_cache(27424, age_seconds=100)  # Aqua
        service._compute_sun_angle = MagicMock(return_value=20.0)

        result = await service.get_passes("MODIS_NRT", 34.05, -118.25, hours=24)

        assert result["source"] == "MODIS_NRT"
        if result["passes"]:
            norad_ids_in_result = {p["norad_id"] for p in result["passes"]}
            # Should have passes from at least one of the two satellites
            assert norad_ids_in_result.issubset({25994, 27424})

    async def test_passes_sorted_by_aos(self, service):
        """All returned passes should be sorted by AOS ascending."""
        _seed_cache(SAMPLE_NORAD_ID, age_seconds=100)
        service._compute_sun_angle = MagicMock(return_value=10.0)

        result = await service.get_passes("VIIRS_SNPP_NRT", 34.05, -118.25, hours=48)

        if len(result["passes"]) > 1:
            aos_times = [p["aos"] for p in result["passes"]]
            assert aos_times == sorted(aos_times)

    async def test_high_min_elevation_filters_passes(self, service):
        """Higher min_elevation should return fewer or equal passes."""
        _seed_cache(SAMPLE_NORAD_ID, age_seconds=100)
        service._compute_sun_angle = MagicMock(return_value=25.0)

        result_low = await service.get_passes(
            "VIIRS_SNPP_NRT", 34.05, -118.25, hours=48, min_elevation=5.0
        )
        result_high = await service.get_passes(
            "VIIRS_SNPP_NRT", 34.05, -118.25, hours=48, min_elevation=60.0
        )

        assert result_high["pass_count"] <= result_low["pass_count"]

    async def test_tle_stale_flag_propagates(self, service):
        """If TLE is stale, the response should reflect it."""
        _seed_cache(SAMPLE_NORAD_ID, age_seconds=_TLE_CACHE_TTL + 100)

        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("down")

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            service._compute_sun_angle = MagicMock(return_value=15.0)
            result = await service.get_passes("VIIRS_SNPP_NRT", 34.05, -118.25)

        assert result["tle_stale"] is True


class TestSunAngle:
    """Test sun angle computation and graceful degradation."""

    def test_no_ephemeris_returns_none(self, service_no_ephemeris):
        result = service_no_ephemeris._compute_sun_angle(34.0, -118.0, MagicMock())
        assert result is None

    async def test_passes_without_ephemeris_have_none_sun_fields(self, service_no_ephemeris):
        """When ephemeris is unavailable, sun-related fields should be None."""
        _seed_cache(SAMPLE_NORAD_ID, age_seconds=100)

        result = await service_no_ephemeris.get_passes("VIIRS_SNPP_NRT", 34.05, -118.25)

        for p in result["passes"]:
            assert p["solar_elevation_deg"] is None
            assert p["is_daytime_pass"] is None
            assert p["quality_score"] is None
