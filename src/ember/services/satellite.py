"""
Satellite pass prediction service using TLE data and skyfield SGP4 propagation.

Fetches Two-Line Element sets (TLEs) from CelesTrak for fire-detection satellites
(VIIRS on Suomi NPP/NOAA-20/NOAA-21, MODIS on Terra/Aqua) and computes upcoming
pass predictions over a given location using SGP4 orbital propagation.

Each pass includes timing (AOS/TCA/LOS), geometry (elevation, direction), and
quality metadata (solar elevation, daytime flag, composite quality score) to help
Nova users understand when fresh satellite imagery will be available and how useful
each observation will be.

Geostationary satellites (GOES-16/17/18) are handled separately — they provide
continuous coverage with fixed refresh intervals and require no pass prediction.

TLEs are cached in memory with a 24-hour TTL and refreshed lazily on request.
If CelesTrak is unreachable, stale cached TLEs are served with a staleness flag.
"""

from dataclasses import dataclass
from datetime import timedelta
from time import time
from typing import Any

import httpx
from skyfield.api import EarthSatellite, wgs84
from skyfield.api import load as sf_load

from ember.config import settings
from ember.exceptions import ExternalAPIError
from ember.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Skyfield timescale (builtin=True avoids network download)
# ---------------------------------------------------------------------------
_ts = sf_load.timescale(builtin=True)

# ---------------------------------------------------------------------------
# Satellite registry — maps Nova source keys to orbital metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SatelliteInfo:
    """Static metadata for a fire-detection satellite.

    Attributes:
        name: Human-readable satellite name (e.g. "Suomi NPP").
        norad_ids: NORAD catalog IDs for orbital lookup. Most sources map to one
            satellite; MODIS maps to two (Terra + Aqua). Empty for geostationary.
        instrument: Sensor name (e.g. "VIIRS", "MODIS", "ABI").
        swath_km: Ground swath width of the instrument in kilometers.
        is_geostationary: True for GOES satellites (no pass prediction needed).
        refresh_minutes: Data refresh interval for geostationary satellites.
    """

    name: str
    norad_ids: tuple[int, ...]
    instrument: str
    swath_km: float
    is_geostationary: bool = False
    refresh_minutes: int | None = None


SATELLITE_REGISTRY: dict[str, SatelliteInfo] = {
    "VIIRS_SNPP_NRT": SatelliteInfo(
        name="Suomi NPP",
        norad_ids=(37849,),
        instrument="VIIRS",
        swath_km=3060.0,
    ),
    "VIIRS_NOAA20_NRT": SatelliteInfo(
        name="NOAA-20",
        norad_ids=(43013,),
        instrument="VIIRS",
        swath_km=3060.0,
    ),
    "VIIRS_NOAA21_NRT": SatelliteInfo(
        name="NOAA-21",
        norad_ids=(54234,),
        instrument="VIIRS",
        swath_km=3060.0,
    ),
    "MODIS_NRT": SatelliteInfo(
        name="MODIS (Terra+Aqua)",
        norad_ids=(25994, 27424),
        instrument="MODIS",
        swath_km=2330.0,
    ),
    "GOES16_NRT": SatelliteInfo(
        name="GOES-16 East",
        norad_ids=(),
        instrument="ABI",
        swath_km=0.0,
        is_geostationary=True,
        refresh_minutes=15,
    ),
    "GOES17_NRT": SatelliteInfo(
        name="GOES-17 West",
        norad_ids=(),
        instrument="ABI",
        swath_km=0.0,
        is_geostationary=True,
        refresh_minutes=15,
    ),
    "GOES18_NRT": SatelliteInfo(
        name="GOES-18 West",
        norad_ids=(),
        instrument="ABI",
        swath_km=0.0,
        is_geostationary=True,
        refresh_minutes=15,
    ),
}

# NORAD ID → friendly satellite name (for MODIS dual-satellite labeling)
_NORAD_NAMES: dict[int, str] = {
    37849: "Suomi NPP",
    43013: "NOAA-20",
    54234: "NOAA-21",
    25994: "Terra",
    27424: "Aqua",
}

# ---------------------------------------------------------------------------
# TLE cache — keyed by NORAD ID, same pattern as firms.py
# ---------------------------------------------------------------------------
_tle_cache: dict[int, dict] = {}
_TLE_CACHE_TTL = 86400  # 24 hours
_TLE_CACHE_MAX_SIZE = 20

CELESTRAK_URL = "https://celestrak.org/NORAD/elements/gp.php"

# ---------------------------------------------------------------------------
# CelesTrak fetch cooldown — avoid hammering a down service
# ---------------------------------------------------------------------------
_celestrak_last_failure: float = 0.0
_CELESTRAK_COOLDOWN = 300  # 5 minutes — skip fetch attempts after a failure

# ---------------------------------------------------------------------------
# Compass direction helpers
# ---------------------------------------------------------------------------
_COMPASS_DIRECTIONS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def _azimuth_to_compass(azimuth_deg: float) -> str:
    """Convert azimuth in degrees (0-360, 0=North) to 8-point compass direction."""
    idx = round(azimuth_deg / 45) % 8
    return _COMPASS_DIRECTIONS[idx]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SatelliteService:
    """Satellite overpass prediction service.

    Manages TLE (Two-Line Element) fetching from CelesTrak, in-memory caching
    with 24h TTL, and SGP4 orbital propagation via skyfield to predict when
    fire-detection satellites will pass over a given location.

    Also computes solar elevation at observation time and a composite quality
    score (0-100) for each pass, helping users assess data utility before the
    satellite arrives.

    On initialization, attempts to load the JPL DE421 planetary ephemeris
    (~17MB, downloaded once and cached to disk by skyfield) for sun angle
    calculations. If unavailable, the service still functions but sun-related
    fields return None.
    """

    def __init__(self):
        self.timeout = settings.http_timeout

        # Lazy-load JPL ephemeris for sun angle computation
        self._ephemeris_available = False
        self._sun = None
        self._earth = None
        try:
            eph = sf_load("de421.bsp")
            self._sun = eph["sun"]
            self._earth = eph["earth"]
            self._ephemeris_available = True
        except Exception:
            logger.warning(
                "Could not load JPL ephemeris (de421.bsp) — "
                "sun angle and quality score will be unavailable"
            )

    async def get_passes(
        self,
        source: str,
        lat: float,
        lon: float,
        hours: int = 24,
        min_elevation: float = 10.0,
    ) -> dict[str, Any]:
        """
        Get upcoming satellite passes for a FIRMS source over a location.

        For polar-orbiting satellites, computes pass predictions using SGP4
        propagation. For geostationary satellites (GOES), returns static
        refresh interval info without any orbital computation.

        MODIS sources return merged passes from both Terra and Aqua satellites,
        sorted by acquisition of signal (AOS) time.

        Args:
            source: FIRMS source key (e.g. "VIIRS_SNPP_NRT"). Must match a key
                in SATELLITE_REGISTRY.
            lat: Observer latitude in degrees (-90 to 90).
            lon: Observer longitude in degrees (-180 to 180).
            hours: Prediction window in hours ahead from now (default 24, max 72).
            min_elevation: Minimum peak elevation angle in degrees to include a
                pass (default 10). Passes below this threshold have poor sensor
                coverage of the observer location.

        Returns:
            Dict containing source metadata, staleness flag, and a list of pass
            dicts each with AOS/TCA/LOS times, elevation, direction, sun angle,
            and quality score.

        Raises:
            ValueError: If source key is not in SATELLITE_REGISTRY.
            ExternalAPIError: If TLE fetch fails and no cached data is available.
        """
        info = SATELLITE_REGISTRY.get(source)
        if not info:
            valid = [k for k, v in SATELLITE_REGISTRY.items() if not v.is_geostationary]
            raise ValueError(f"Unknown source '{source}'. Valid polar-orbiting sources: {valid}")

        if info.is_geostationary:
            return self._geostationary_info(source, info)

        all_passes: list[dict] = []
        tle_stale = False

        for norad_id in info.norad_ids:
            tle_data = await self._fetch_tle(norad_id)
            if tle_data["tle_stale"]:
                tle_stale = True

            passes = self._compute_passes(
                tle_line1=tle_data["tle_line1"],
                tle_line2=tle_data["tle_line2"],
                name=tle_data["name"],
                lat=lat,
                lon=lon,
                hours=hours,
                min_elevation=min_elevation,
                swath_km=info.swath_km,
                norad_id=norad_id,
                source_key=source,
                instrument=info.instrument,
            )
            all_passes.extend(passes)

        # Sort by AOS (earliest first)
        all_passes.sort(key=lambda p: p["aos"])

        return {
            "source": source,
            "satellite": info.name,
            "is_geostationary": False,
            "tle_stale": tle_stale,
            "prediction_window_hours": hours,
            "pass_count": len(all_passes),
            "passes": all_passes,
        }

    async def get_past_passes(
        self,
        source: str,
        lat: float,
        lon: float,
        hours: int = 48,
        min_elevation: float = 10.0,
        detection_time: str | None = None,
    ) -> dict[str, Any]:
        """
        Get past satellite passes over a location, optionally correlating with a
        FIRMS detection timestamp.

        Computes passes that already occurred within the lookback window. When
        detection_time is provided, finds the pass whose TCA is closest to
        that timestamp and assigns a match confidence level.

        Args:
            source: FIRMS source key (e.g. "VIIRS_SNPP_NRT").
            lat: Observer latitude in degrees (-90 to 90).
            lon: Observer longitude in degrees (-180 to 180).
            hours: Lookback window in hours (default 48, max 168).
            min_elevation: Minimum peak elevation in degrees (default 10).
            detection_time: ISO-8601 timestamp of a FIRMS detection to correlate.

        Returns:
            Dict with past passes sorted by AOS descending (most recent first),
            and optional detection correlation result.
        """
        info = SATELLITE_REGISTRY.get(source)
        if not info:
            valid = [k for k, v in SATELLITE_REGISTRY.items() if not v.is_geostationary]
            raise ValueError(f"Unknown source '{source}'. Valid polar-orbiting sources: {valid}")

        if info.is_geostationary:
            return self._geostationary_info(source, info)

        all_passes: list[dict] = []
        tle_stale = False

        for norad_id in info.norad_ids:
            tle_data = await self._fetch_tle(norad_id)
            if tle_data["tle_stale"]:
                tle_stale = True

            passes = self._compute_passes(
                tle_line1=tle_data["tle_line1"],
                tle_line2=tle_data["tle_line2"],
                name=tle_data["name"],
                lat=lat,
                lon=lon,
                hours=hours,
                min_elevation=min_elevation,
                swath_km=info.swath_km,
                norad_id=norad_id,
                source_key=source,
                instrument=info.instrument,
                backward=True,
            )
            all_passes.extend(passes)

        # Sort by AOS descending (most recent first)
        all_passes.sort(key=lambda p: p["aos"], reverse=True)

        result: dict[str, Any] = {
            "source": source,
            "satellite": info.name,
            "is_geostationary": False,
            "tle_stale": tle_stale,
            "lookback_hours": hours,
            "pass_count": len(all_passes),
            "passes": all_passes,
        }

        # Detection correlation
        if detection_time and all_passes:
            correlation = self._correlate_detection(all_passes, detection_time)
            result["detection_correlation"] = correlation

        return result

    @staticmethod
    def _correlate_detection(passes: list[dict], detection_time: str) -> dict[str, Any]:
        """
        Find the satellite pass whose TCA is closest to a FIRMS detection timestamp.

        Assigns a match confidence based on the time difference:
        - exact: TCA within +/-5 minutes
        - likely: TCA within +/-30 minutes
        - uncertain: TCA within +/-2 hours
        - no_match: no pass found within 2 hours
        """
        from datetime import datetime as dt

        try:
            det_dt = dt.fromisoformat(detection_time.replace("Z", "+00:00"))
        except ValueError:
            return {
                "match_confidence": "error",
                "message": "Invalid detection_time format",
            }

        best_pass = None
        best_diff_s = float("inf")

        for p in passes:
            tca_dt = dt.fromisoformat(p["tca"].replace("Z", "+00:00"))
            diff_s = abs((tca_dt - det_dt).total_seconds())
            if diff_s < best_diff_s:
                best_diff_s = diff_s
                best_pass = p

        if best_pass is None:
            return {"match_confidence": "no_match"}

        # Classify confidence
        if best_diff_s <= 300:  # 5 minutes
            confidence = "exact"
        elif best_diff_s <= 1800:  # 30 minutes
            confidence = "likely"
        elif best_diff_s <= 7200:  # 2 hours
            confidence = "uncertain"
        else:
            return {
                "match_confidence": "no_match",
                "nearest_tca_diff_s": round(best_diff_s),
            }

        return {
            "match_confidence": confidence,
            "matched_pass": best_pass,
            "tca_diff_s": round(best_diff_s),
        }

    async def _fetch_tle(self, norad_id: int) -> dict:
        """
        Fetch a Two-Line Element set from CelesTrak for a given NORAD catalog ID.

        Uses a 24-hour in-memory cache with cache hit/miss logging. On cache miss,
        fetches from CelesTrak's GP API with a single retry on transient failures.

        A cooldown mechanism prevents hammering CelesTrak when it's down — after a
        failure, subsequent fetch attempts within 5 minutes are skipped and stale
        cache is returned instead.

        Args:
            norad_id: NORAD catalog number (e.g. 37849 for Suomi NPP).

        Returns:
            Dict with keys: name, tle_line1, tle_line2, tle_stale (bool).

        Raises:
            ExternalAPIError: If fetch fails and no cached TLE is available.
        """
        global _celestrak_last_failure

        cached = _tle_cache.get(norad_id)
        if cached and (time() - cached["timestamp"] < _TLE_CACHE_TTL):
            logger.debug("TLE cache HIT for NORAD %d", norad_id)
            return {**cached["data"], "tle_stale": False}

        logger.debug("TLE cache MISS for NORAD %d", norad_id)

        # Cooldown: if CelesTrak failed recently, skip the fetch and use stale cache
        if _celestrak_last_failure and (time() - _celestrak_last_failure < _CELESTRAK_COOLDOWN):
            if cached:
                logger.debug(
                    "CelesTrak cooldown active, using stale TLE for NORAD %d",
                    norad_id,
                )
                return {**cached["data"], "tle_stale": True}

        url = CELESTRAK_URL
        params = {"CATNR": norad_id, "FORMAT": "TLE"}

        # Retry once on transient failure
        last_exc = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(url, params=params, timeout=self.timeout)
                    response.raise_for_status()

                lines = [
                    line.strip() for line in response.text.strip().splitlines() if line.strip()
                ]
                if len(lines) < 3:
                    raise ValueError(f"Expected 3 TLE lines, got {len(lines)}")

                tle_data = {
                    "name": lines[0],
                    "tle_line1": lines[1],
                    "tle_line2": lines[2],
                }

                # Cache eviction
                if len(_tle_cache) >= _TLE_CACHE_MAX_SIZE:
                    _tle_cache.clear()

                _tle_cache[norad_id] = {"timestamp": time(), "data": tle_data}
                _celestrak_last_failure = 0.0  # reset cooldown on success
                logger.info("TLE fetched for NORAD %d (%s)", norad_id, tle_data["name"])
                return {**tle_data, "tle_stale": False}

            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                if attempt == 0:
                    logger.debug(
                        "CelesTrak fetch attempt 1 failed for NORAD %d: %s, retrying",
                        norad_id,
                        exc,
                    )
                    continue

        # Both attempts failed
        _celestrak_last_failure = time()

        if cached:
            logger.warning(
                "CelesTrak fetch failed for NORAD %d after 2 attempts, "
                "using stale TLE (age: %ds): %s",
                norad_id,
                round(time() - cached["timestamp"]),
                last_exc,
            )
            return {**cached["data"], "tle_stale": True}

        logger.error(
            "CelesTrak fetch failed for NORAD %d with no cached data: %s",
            norad_id,
            last_exc,
        )
        raise ExternalAPIError(
            f"Failed to fetch TLE for NORAD {norad_id} and no cached data available",
            details={"norad_id": norad_id, "error": str(last_exc)},
        ) from last_exc

    def _compute_passes(
        self,
        tle_line1: str,
        tle_line2: str,
        name: str,
        lat: float,
        lon: float,
        hours: int,
        min_elevation: float,
        swath_km: float,
        norad_id: int,
        source_key: str,
        instrument: str,
        backward: bool = False,
    ) -> list[dict]:
        """
        Compute pass predictions for a single satellite using skyfield SGP4 propagation.

        Creates an EarthSatellite from TLE lines, builds an observer position, and
        uses skyfield's find_events() to locate rise/culminate/set event sequences.
        Each complete pass (rise->culminate->set) is evaluated against the minimum
        elevation threshold, then enriched with sun angle and quality scoring.

        When backward=True, computes passes in the past (now - hours) instead of
        the future. The time_until_s field becomes negative for past passes.

        Incomplete passes at window boundaries (e.g. satellite already risen at t0)
        are skipped to avoid returning partial data.

        Returns:
            List of pass dicts sorted chronologically, each containing satellite
            identity, timing, geometry, and quality metadata.
        """
        satellite = EarthSatellite(tle_line1, tle_line2, name, _ts)
        observer = wgs84.latlon(lat, lon)

        now = _ts.now()
        if backward:
            t0 = _ts.utc(now.utc_datetime() - timedelta(hours=hours))
            t1 = now
        else:
            t0 = now
            t1 = _ts.utc(now.utc_datetime() + timedelta(hours=hours))

        # Find all rise/culminate/set events (altitude_degrees=0 to get full passes)
        times, events = satellite.find_events(observer, t0, t1, altitude_degrees=0.0)

        # Group events into passes: rise=0, culminate=1, set=2
        passes: list[dict] = []
        i = 0
        while i < len(events):
            # Find a complete rise → culminate → set sequence
            if events[i] != 0:
                i += 1
                continue

            # Need at least 3 more events for a complete pass
            if i + 2 >= len(events):
                break

            if events[i] == 0 and events[i + 1] == 1 and events[i + 2] == 2:
                aos_time = times[i]
                tca_time = times[i + 1]
                los_time = times[i + 2]

                # Compute max elevation at culmination
                difference = satellite - observer
                topocentric = difference.at(tca_time)
                alt, az, _ = topocentric.altaz()
                max_elev = float(alt.degrees)

                if max_elev >= min_elevation:
                    # Compute direction from azimuth at AOS
                    topo_aos = difference.at(aos_time)
                    _, az_aos, _ = topo_aos.altaz()
                    direction = _azimuth_to_compass(float(az_aos.degrees))

                    # Sun angle at TCA
                    solar_elev = self._compute_sun_angle(lat, lon, tca_time)
                    is_daytime = bool(solar_elev > 0) if solar_elev is not None else None
                    quality = self._compute_quality_score(max_elev, solar_elev)

                    # Time until AOS
                    now_utc = _ts.now().utc_datetime()
                    aos_utc = aos_time.utc_datetime()
                    time_until = max(0, (aos_utc - now_utc).total_seconds())

                    sat_name = _NORAD_NAMES.get(norad_id, name)

                    passes.append(
                        {
                            "satellite": sat_name,
                            "norad_id": norad_id,
                            "source_key": source_key,
                            "instrument": instrument,
                            "aos": aos_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "tca": tca_time.utc_datetime().strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "los": los_time.utc_datetime().strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "max_elevation_deg": round(max_elev, 1),
                            "direction": direction,
                            "swath_km": swath_km,
                            "time_until_s": round(time_until),
                            "solar_elevation_deg": (
                                round(solar_elev, 1) if solar_elev is not None else None
                            ),
                            "is_daytime_pass": is_daytime,
                            "quality_score": quality,
                        }
                    )

                i += 3
            else:
                i += 1

        return passes

    def _compute_sun_angle(self, lat: float, lon: float, tca_time) -> float | None:
        """
        Compute solar elevation angle at an observer location at a specific time.

        Uses the JPL DE421 ephemeris to determine where the Sun is relative to
        the observer's horizon. Positive values mean the Sun is above the horizon
        (daytime); negative values indicate night or twilight.

        Args:
            lat: Observer latitude in degrees.
            lon: Observer longitude in degrees.
            tca_time: Skyfield Time object for the moment to evaluate.

        Returns:
            Solar elevation in degrees, or None if ephemeris is unavailable.
        """
        if not self._ephemeris_available:
            return None

        observer_pos = (self._earth + wgs84.latlon(lat, lon)).at(tca_time)
        sun_alt, _, _ = observer_pos.observe(self._sun).apparent().altaz()
        return float(sun_alt.degrees)

    @staticmethod
    def _compute_quality_score(
        max_elevation_deg: float, solar_elevation_deg: float | None
    ) -> int | None:
        """
        Compute a composite pass quality score from 0 to 100.

        Combines three factors that determine how useful a satellite observation
        will be for fire detection at the observer's location:

        - Elevation factor (0-40 pts): Higher max elevation means the satellite
          passes more directly overhead, yielding better spatial resolution and
          ensuring the location falls within the sensor swath.
        - Sun angle factor (0-30 pts): Daytime passes (sun > 0°) score highest
          because VIIRS/MODIS can use both thermal and visible bands. Civil
          twilight (-6° to 0°) is intermediate. Nighttime (< -6°) is thermal-only
          but still valuable for fire detection.
        - Swath factor (0-30 pts): Correlates with elevation — high-elevation
          passes place the observer near the center of the sensor swath where
          spatial resolution is best.

        Args:
            max_elevation_deg: Peak elevation angle of the pass in degrees.
            solar_elevation_deg: Sun elevation at observer at TCA time, or None.

        Returns:
            Integer quality score (0-100), or None if solar data is unavailable.
        """
        if solar_elevation_deg is None:
            return None

        # Elevation factor (0-40)
        elevation_factor = min(max_elevation_deg / 90.0 * 40.0, 40.0)

        # Sun angle factor (0-30)
        if solar_elevation_deg > 0:
            sun_factor = 30.0  # daytime
        elif solar_elevation_deg > -6:
            sun_factor = 15.0  # civil twilight
        else:
            sun_factor = 10.0  # night (thermal-only)

        # Swath factor (0-30) — high elevation = centered in swath
        if max_elevation_deg > 60:
            swath_factor = 30.0
        elif max_elevation_deg > 30:
            swath_factor = 20.0
        else:
            swath_factor = 10.0

        return round(elevation_factor + sun_factor + swath_factor)

    @staticmethod
    def _geostationary_info(source: str, info: SatelliteInfo) -> dict[str, Any]:
        """
        Return static refresh info for geostationary sources.

        GOES satellites are in geostationary orbit (~36,000 km) and provide
        continuous coverage of their hemisphere. No pass prediction is needed —
        they see the same area all the time with a fixed refresh interval.
        """
        return {
            "source": source,
            "satellite": info.name,
            "is_geostationary": True,
            "refresh_minutes": info.refresh_minutes,
            "instrument": info.instrument,
            "message": (
                f"{info.name} is geostationary — continuous coverage, "
                f"refreshes every {info.refresh_minutes} min"
            ),
        }


satellite_service = SatelliteService()
