"""Unit tests for satellite pass prediction service."""

from time import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import ember.services.satellite as sat_module
from ember.exceptions import ExternalAPIError
from ember.services.satellite import (
    _NORAD_NAMES,
    _TLE_CACHE_MAX_SIZE,
    _TLE_CACHE_TTL,
    SATELLITE_REGISTRY,
    SatelliteService,
    _azimuth_to_compass,
    _freshness_cache,
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

# Deterministic pass dict used to decouple field-structure tests from skyfield propagation
_DETERMINISTIC_PASS = {
    "satellite": "Suomi NPP",
    "norad_id": 37849,
    "source_key": "VIIRS_SNPP_NRT",
    "instrument": "VIIRS",
    "aos": "2026-03-29T10:00:00Z",
    "tca": "2026-03-29T10:05:00Z",
    "los": "2026-03-29T10:10:00Z",
    "max_elevation_deg": 67.3,
    "direction": "NW",
    "swath_km": 3060.0,
    "time_until_s": 3735,
    "solar_elevation_deg": 35.2,
    "is_daytime_pass": True,
    "quality_score": 89,
}


@pytest.fixture(autouse=True)
def clear_tle_cache():
    """Reset TLE cache, freshness cache, and cooldown state before each test."""
    _tle_cache.clear()
    _freshness_cache.clear()
    sat_module._celestrak_last_failure = 0.0
    yield
    _tle_cache.clear()
    _freshness_cache.clear()
    sat_module._celestrak_last_failure = 0.0


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


@pytest.fixture
def make_pass():
    """Factory fixture: returns a minimal pass dict keyed by TCA string."""

    def _factory(tca: str) -> dict:
        return {
            "satellite": "Suomi NPP",
            "norad_id": 37849,
            "tca": tca,
            "aos": tca,
            "los": tca,
        }

    return _factory


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

    async def test_retry_succeeds_on_second_attempt(self, service, mock_celestrak_response):
        """If first fetch attempt fails but second succeeds, return fresh data."""
        mock_client = AsyncMock()
        mock_client.get.side_effect = [
            httpx.ConnectError("transient failure"),
            mock_celestrak_response,
        ]

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await service._fetch_tle(SAMPLE_NORAD_ID)

        assert result["tle_stale"] is False
        assert result["tle_line1"] == SAMPLE_TLE_LINE1
        assert mock_client.get.call_count == 2

    async def test_cooldown_skips_fetch_with_stale_cache(self, service):
        """During cooldown, stale cache is returned without a network call."""
        _seed_cache(SAMPLE_NORAD_ID, age_seconds=_TLE_CACHE_TTL + 100)
        # Simulate a recent failure
        sat_module._celestrak_last_failure = time() - 60  # 1 min ago, within 5 min cooldown

        with patch("httpx.AsyncClient") as mock_client_cls:
            result = await service._fetch_tle(SAMPLE_NORAD_ID)

            mock_client_cls.assert_not_called()
            assert result["tle_stale"] is True
            assert result["name"] == SAMPLE_TLE_NAME

    async def test_cooldown_resets_on_success(self, service, mock_celestrak_response):
        """A successful fetch should reset the cooldown timer."""
        sat_module._celestrak_last_failure = time() - 400  # cooldown expired

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_celestrak_response

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await service._fetch_tle(SAMPLE_NORAD_ID)

        assert sat_module._celestrak_last_failure == 0.0

    async def test_failure_sets_cooldown(self, service):
        """After both retry attempts fail, the cooldown timer should be set."""
        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("connection refused")

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ExternalAPIError):
                await service._fetch_tle(SAMPLE_NORAD_ID)

        assert sat_module._celestrak_last_failure > 0


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
        """VIIRS source should return passes with all expected fields.

        _compute_passes is mocked so the assertion is not contingent on skyfield
        producing passes for a particular time window.
        """
        _seed_cache(SAMPLE_NORAD_ID, age_seconds=100)

        with patch.object(service, "_compute_passes", return_value=[_DETERMINISTIC_PASS]):
            result = await service.get_passes("VIIRS_SNPP_NRT", 34.05, -118.25, hours=24)

        assert result["source"] == "VIIRS_SNPP_NRT"
        assert result["is_geostationary"] is False
        assert isinstance(result["passes"], list)
        assert result["pass_count"] == len(result["passes"])
        assert len(result["passes"]) == 1

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


# ===========================================================================
# Past Passes & Detection Correlation (ORQ-94)
# ===========================================================================


class TestGetPastPasses:
    """Test backward pass prediction and detection correlation."""

    async def test_past_passes_returns_passes_sorted_descending(self, service):
        """Past passes should be sorted by AOS descending (most recent first)."""
        _seed_cache(SAMPLE_NORAD_ID, age_seconds=100)
        service._compute_sun_angle = MagicMock(return_value=25.0)

        result = await service.get_past_passes("VIIRS_SNPP_NRT", 34.05, -118.25, hours=48)

        assert result["source"] == "VIIRS_SNPP_NRT"
        assert result["is_geostationary"] is False
        assert "lookback_hours" in result
        assert result["lookback_hours"] == 48

        if len(result["passes"]) > 1:
            aos_times = [p["aos"] for p in result["passes"]]
            assert aos_times == sorted(aos_times, reverse=True)

    async def test_past_passes_unknown_source_raises(self, service):
        with pytest.raises(ValueError, match="Unknown source"):
            await service.get_past_passes("INVALID", 34.0, -118.0)

    @pytest.mark.parametrize("source", ["GOES16_NRT", "GOES17_NRT", "GOES18_NRT"])
    async def test_past_passes_geostationary_returns_static(self, service, source):
        result = await service.get_past_passes(source, 34.0, -118.0)
        assert result["is_geostationary"] is True

    async def test_past_passes_modis_includes_both_satellites(self, service):
        _seed_cache(25994, age_seconds=100)
        _seed_cache(27424, age_seconds=100)
        service._compute_sun_angle = MagicMock(return_value=20.0)

        result = await service.get_past_passes("MODIS_NRT", 34.05, -118.25, hours=48)

        if result["passes"]:
            norad_ids = {p["norad_id"] for p in result["passes"]}
            assert norad_ids.issubset({25994, 27424})

    async def test_past_passes_with_detection_time_includes_correlation(self, service):
        """When detection_time is provided, response should include correlation."""
        _seed_cache(SAMPLE_NORAD_ID, age_seconds=100)
        service._compute_sun_angle = MagicMock(return_value=30.0)

        result = await service.get_past_passes(
            "VIIRS_SNPP_NRT",
            34.05,
            -118.25,
            hours=48,
            detection_time="2026-03-28T12:00:00Z",
        )

        if result["passes"]:
            assert "detection_correlation" in result
            correlation = result["detection_correlation"]
            assert "match_confidence" in correlation

    async def test_past_passes_without_detection_time_no_correlation(self, service):
        """Without detection_time, no correlation field should be present."""
        _seed_cache(SAMPLE_NORAD_ID, age_seconds=100)
        service._compute_sun_angle = MagicMock(return_value=15.0)

        result = await service.get_past_passes("VIIRS_SNPP_NRT", 34.05, -118.25, hours=48)

        assert "detection_correlation" not in result


class TestDetectionCorrelation:
    """Test detection time correlation logic."""

    @pytest.mark.parametrize(
        "detection_time,expected_confidence,expected_diff_s",
        [
            ("2026-03-28T12:03:00Z", "exact", 180),      # 3 min — within 5 min threshold
            ("2026-03-28T12:20:00Z", "likely", None),     # 20 min — within 30 min threshold
            ("2026-03-28T13:30:00Z", "uncertain", None),  # 90 min — within 2 hr threshold
            ("2026-03-28T15:00:00Z", "no_match", None),   # 3 hr — beyond all thresholds
        ],
    )
    def test_match_confidence_thresholds(
        self, make_pass, detection_time, expected_confidence, expected_diff_s
    ):
        passes = [make_pass("2026-03-28T12:00:00Z")]
        result = SatelliteService._correlate_detection(passes, detection_time)
        assert result["match_confidence"] == expected_confidence
        if expected_diff_s is not None:
            assert result["tca_diff_s"] == expected_diff_s

    def test_picks_closest_pass(self, make_pass):
        passes = [
            make_pass("2026-03-28T10:00:00Z"),
            make_pass("2026-03-28T12:00:00Z"),
            make_pass("2026-03-28T14:00:00Z"),
        ]
        result = SatelliteService._correlate_detection(passes, "2026-03-28T12:02:00Z")
        assert result["match_confidence"] == "exact"
        assert result["matched_pass"]["tca"] == "2026-03-28T12:00:00Z"

    def test_invalid_detection_time_returns_error(self, make_pass):
        passes = [make_pass("2026-03-28T12:00:00Z")]
        result = SatelliteService._correlate_detection(passes, "not-a-date")
        assert result["match_confidence"] == "error"


# ===========================================================================
# Composite Freshness (ORQ-95)
# ===========================================================================


class TestStalenessClassification:
    """Test staleness classification thresholds."""

    @pytest.mark.parametrize(
        "gap_hours,expected",
        [
            (0.5, "fresh"),
            (2.9, "fresh"),
            (3.0, "moderate"),
            (7.9, "moderate"),
            (8.0, "stale"),
            (15.9, "stale"),
            (16.0, "very_stale"),
            (48.0, "very_stale"),
            (None, "unknown"),
        ],
    )
    def test_staleness_thresholds(self, gap_hours, expected):
        assert SatelliteService._classify_staleness(gap_hours) == expected


class TestCompositeFreshness:
    """Test composite freshness computation across all satellites."""

    async def test_freshness_returns_required_fields(self, service):
        """Freshness response should have all expected top-level fields."""
        # Seed TLEs for all polar-orbiting satellites
        for norad_id in [37849, 43013, 54234, 25994, 27424]:
            _seed_cache(norad_id, age_seconds=100)
        service._compute_sun_angle = MagicMock(return_value=20.0)

        result = await service.get_composite_freshness(34.05, -118.25)

        assert "most_recent_pass" in result
        assert "next_pass" in result
        assert "current_gap_hours" in result
        assert "staleness" in result
        assert "all_satellites" in result
        assert isinstance(result["all_satellites"], list)
        assert len(result["all_satellites"]) == 5  # 5 polar orbiters

    async def test_freshness_staleness_is_valid(self, service):
        """Staleness should be one of the defined classifications."""
        for norad_id in [37849, 43013, 54234, 25994, 27424]:
            _seed_cache(norad_id, age_seconds=100)
        service._compute_sun_angle = MagicMock(return_value=15.0)

        result = await service.get_composite_freshness(34.05, -118.25)

        assert result["staleness"] in (
            "fresh",
            "moderate",
            "stale",
            "very_stale",
            "unknown",
        )

    async def test_freshness_satellites_sorted_by_recency(self, service):
        """Per-satellite list should be sorted by most recent first."""
        for norad_id in [37849, 43013, 54234, 25994, 27424]:
            _seed_cache(norad_id, age_seconds=100)
        service._compute_sun_angle = MagicMock(return_value=25.0)

        result = await service.get_composite_freshness(34.05, -118.25)

        hours = [
            s["last_pass_hours_ago"]
            for s in result["all_satellites"]
            if s["last_pass_hours_ago"] is not None
        ]
        assert hours == sorted(hours)

    async def test_freshness_cache_hit(self, service):
        """Second call to same location should hit the freshness cache, not recompute."""
        for norad_id in [37849, 43013, 54234, 25994, 27424]:
            _seed_cache(norad_id, age_seconds=100)
        service._compute_sun_angle = MagicMock(return_value=30.0)

        result1 = await service.get_composite_freshness(34.05, -118.25)
        assert len(_freshness_cache) == 1

        with patch.object(service, "get_past_passes", wraps=service.get_past_passes) as spy:
            result2 = await service.get_composite_freshness(34.05, -118.25)
            spy.assert_not_called()

        assert result1 == result2

    async def test_freshness_most_recent_pass_has_required_fields(self, service):
        """Most recent pass summary should have satellite, tca, hours_ago."""
        for norad_id in [37849, 43013, 54234, 25994, 27424]:
            _seed_cache(norad_id, age_seconds=100)
        service._compute_sun_angle = MagicMock(return_value=20.0)

        result = await service.get_composite_freshness(34.05, -118.25)

        if result["most_recent_pass"] is not None:
            mrp = result["most_recent_pass"]
            assert "satellite" in mrp
            assert "source_key" in mrp
            assert "tca" in mrp
            assert "hours_ago" in mrp
            assert "quality_score" in mrp
