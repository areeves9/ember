# Satellite Pass Prediction

Ember predicts when fire-detection satellites will pass over a given location, giving Nova users a countdown to the next fresh observation.

## Overview

```
┌──────────────────────┐     ┌──────────────┐     ┌─────────────────────┐
│  GET /satellite/     │ ──► │  skyfield    │ ──► │  CelesTrak          │
│  next-pass?lat&lon   │     │  SGP4 prop.  │     │  TLE API            │
└──────────────────────┘     └──────────────┘     └─────────────────────┘
                                    │
                                    │ pass predictions
                                    ▼
                              ┌──────────────┐
                              │ AOS/TCA/LOS  │
                              │ + sun angle  │
                              │ + quality    │
                              └──────────────┘
```

## How It Works

1. **User taps a fire marker** in Nova's tactical mode
2. Nova calls `GET /api/v1/satellite/next-pass?lat=34.1&lon=-109.8&source=VIIRS_SNPP_NRT`
3. Ember looks up the satellite's NORAD ID from the registry
4. Fetches the Two-Line Element (TLE) set from CelesTrak (or serves from 24h cache)
5. Uses skyfield's SGP4 propagator to compute when the satellite rises above the horizon
6. Enriches each pass with sun angle (daytime vs nighttime) and a quality score
7. Returns the passes sorted by acquisition of signal (AOS) time
8. Nova renders "Next VIIRS pass: 1h 2m" in the Fire Inspection Card

## Endpoints

### `GET /api/v1/satellite/next-pass`

**Auth required.** Returns pass predictions for a location.

| Parameter | Type | Required | Default | Description |
|:----------|:-----|:---------|:--------|:------------|
| `lat` | float | yes | — | Observer latitude (-90 to 90) |
| `lon` | float | yes | — | Observer longitude (-180 to 180) |
| `source` | string | no | all | FIRMS source key. If omitted, returns all polar-orbiting |
| `hours_ahead` | int | no | 24 | Prediction window (1-72 hours) |
| `min_elevation` | float | no | 10.0 | Minimum peak elevation in degrees (0-90) |

**Response (polar-orbiting):**

```json
{
  "location": {"lat": 34.14, "lon": -109.79},
  "generated_at": "2026-03-29T07:39:00Z",
  "source": "VIIRS_SNPP_NRT",
  "satellite": "Suomi NPP",
  "is_geostationary": false,
  "tle_stale": false,
  "prediction_window_hours": 24,
  "pass_count": 4,
  "passes": [
    {
      "satellite": "Suomi NPP",
      "norad_id": 37849,
      "source_key": "VIIRS_SNPP_NRT",
      "instrument": "VIIRS",
      "aos": "2026-03-29T08:42:15Z",
      "tca": "2026-03-29T08:47:30Z",
      "los": "2026-03-29T08:53:02Z",
      "max_elevation_deg": 67.3,
      "direction": "NW",
      "swath_km": 3060.0,
      "time_until_s": 3735,
      "solar_elevation_deg": 35.2,
      "is_daytime_pass": true,
      "quality_score": 89
    }
  ]
}
```

**Response (geostationary):**

```json
{
  "location": {"lat": 34.14, "lon": -109.79},
  "generated_at": "2026-03-29T07:39:00Z",
  "source": "GOES16_NRT",
  "satellite": "GOES-16 East",
  "is_geostationary": true,
  "refresh_minutes": 15,
  "instrument": "ABI",
  "message": "GOES-16 East is geostationary — continuous coverage, refreshes every 15 min"
}
```

### `GET /api/v1/satellite/sources`

**No auth required.** Lists all available satellite sources.

```json
{
  "sources": [
    {
      "id": "VIIRS_SNPP_NRT",
      "name": "Suomi NPP",
      "instrument": "VIIRS",
      "is_geostationary": false,
      "norad_ids": [37849],
      "swath_km": 3060.0
    },
    {
      "id": "GOES16_NRT",
      "name": "GOES-16 East",
      "instrument": "ABI",
      "is_geostationary": true,
      "refresh_minutes": 15
    }
  ]
}
```

## Satellite Registry

### Polar-Orbiting (pass prediction computed)

| Source Key | Satellite | Instrument | NORAD ID | Swath | Orbit |
|:-----------|:----------|:-----------|:---------|:------|:------|
| `VIIRS_SNPP_NRT` | Suomi NPP | VIIRS | 37849 | 3060 km | ~101 min, sun-sync |
| `VIIRS_NOAA20_NRT` | NOAA-20 | VIIRS | 43013 | 3060 km | ~101 min, sun-sync |
| `VIIRS_NOAA21_NRT` | NOAA-21 | VIIRS | 54234 | 3060 km | ~101 min, sun-sync |
| `MODIS_NRT` | Terra + Aqua | MODIS | 25994, 27424 | 2330 km | ~99 min, sun-sync |

MODIS maps to two satellites. When queried, passes from both are computed and merged.

### Geostationary (static refresh info)

| Source Key | Satellite | Refresh |
|:-----------|:----------|:--------|
| `GOES16_NRT` | GOES-16 East | 15 min |
| `GOES17_NRT` | GOES-17 West | 15 min |
| `GOES18_NRT` | GOES-18 West | 15 min |

No pass prediction needed — they see the same area continuously.

## Pass Fields

| Field | Description |
|:------|:------------|
| `aos` | **Acquisition of Signal** — satellite rises above horizon |
| `tca` | **Time of Closest Approach** — satellite at peak elevation |
| `los` | **Loss of Signal** — satellite drops below horizon |
| `max_elevation_deg` | Peak elevation angle (0° = horizon, 90° = overhead). Higher = better data |
| `direction` | 8-point compass direction the satellite rises from |
| `swath_km` | Instrument's ground imaging width in km |
| `time_until_s` | Seconds from now until AOS |
| `solar_elevation_deg` | Sun angle at observer location at TCA. Positive = daytime |
| `is_daytime_pass` | `true` if sun is above horizon. Daytime = thermal + visible bands |
| `quality_score` | 0-100 composite (elevation 0-40 + sun angle 0-30 + swath position 0-30) |

## TLE Caching

**Location:** `src/ember/services/satellite.py`

| Property | Value |
|:---------|:------|
| Cache key | NORAD catalog ID (int) |
| TTL | 86400s (24 hours) |
| Max size | 20 entries |
| Eviction | Clear all when full |
| Staleness | Served with `tle_stale: true` when CelesTrak is down |

TLE cache keys differ from other Ember services — they're keyed by NORAD ID rather than
coordinate strings, since TLEs are per-satellite (not per-request location).

### Cache Key Examples

```
37849 → Suomi NPP TLE
43013 → NOAA-20 TLE
54234 → NOAA-21 TLE
25994 → Terra TLE
27424 → Aqua TLE
```

## Error Handling & Resilience

### CelesTrak Fetch Failures

```
Request → Cache fresh?
            ├── YES → return cached (tle_stale: false)
            └── NO → CelesTrak in cooldown?
                      ├── YES + stale cache → return stale (tle_stale: true)
                      └── NO → fetch from CelesTrak
                                ├── SUCCESS → cache + return (tle_stale: false)
                                └── FAIL → retry once
                                            ├── SUCCESS → cache + return
                                            └── FAIL → set cooldown
                                                        ├── stale cache → return (tle_stale: true)
                                                        └── no cache → ExternalAPIError (502)
```

### Cooldown Mechanism

After a CelesTrak failure, a 5-minute cooldown prevents repeated timeouts on subsequent requests:

- **Cooldown active + stale cache:** Return stale data immediately (no network call)
- **Cooldown active + no cache:** Attempt the fetch anyway (last resort)
- **Cooldown resets** on any successful fetch

### Retry

One automatic retry on transient failures (connection errors, timeouts, HTTP 5xx). No backoff delay between attempts since the retry is immediate and bounded to a single extra attempt.

### Ephemeris Unavailability

If `de421.bsp` (JPL planetary ephemeris, ~17MB) is not available:

- Service still works — pass predictions are computed normally
- `solar_elevation_deg`, `is_daytime_pass`, and `quality_score` return `null`
- Warning logged once at startup

In Docker, the ephemeris is pre-downloaded at build time (`Dockerfile`), so this only affects local development on first run.

## Logging

| Level | Event |
|:------|:------|
| `DEBUG` | Cache HIT for NORAD ID |
| `DEBUG` | Cache MISS for NORAD ID |
| `DEBUG` | Cooldown active, using stale TLE |
| `DEBUG` | First fetch attempt failed, retrying |
| `INFO` | TLE successfully fetched (NORAD ID + satellite name) |
| `WARNING` | Fetch failed after retries, serving stale TLE (with age in seconds) |
| `WARNING` | Ephemeris unavailable at startup |
| `ERROR` | Fetch failed with no cached data available |

View satellite-specific logs:

```bash
docker compose logs ember | grep -i "tle\|celestrak\|norad\|satellite\|ephemeris"
```

## Quality Score Breakdown

The quality score (0-100) combines three factors:

| Factor | Range | Criteria |
|:-------|:------|:---------|
| **Elevation** | 0-40 | `min(elevation / 90 * 40, 40)`. 90° overhead = 40 pts |
| **Sun angle** | 0-30 | Daytime (sun > 0°) = 30, twilight (-6° to 0°) = 15, night (< -6°) = 10 |
| **Swath position** | 0-30 | Elevation > 60° = 30, 30-60° = 20, < 30° = 10 |

**Examples:**

| Scenario | Elevation | Sun | Score |
|:---------|:----------|:----|:------|
| Daytime, directly overhead | 90° | 45° | 100 |
| Daytime, low pass | 15° | 30° | 47 |
| Night, high pass | 80° | -20° | 76 |
| Night, low pass | 10° | -15° | 24 |

## Dependencies

| Package | Purpose |
|:--------|:--------|
| `skyfield>=1.45` | SGP4 orbital propagation, pass prediction, sun angle |
| `sgp4` | Low-level SGP4 implementation (installed by skyfield) |
| `jplephem` | JPL ephemeris reader (installed by skyfield) |

### External Services

| Service | URL | Purpose | Failure mode |
|:--------|:----|:--------|:-------------|
| CelesTrak | `celestrak.org/NORAD/elements/gp.php` | TLE source | Stale cache fallback + cooldown |
| JPL (indirect) | via `de421.bsp` | Planetary positions | Sun angle returns null |

## Files

| File | Purpose |
|:-----|:--------|
| `src/ember/services/satellite.py` | Service: TLE fetching, caching, SGP4 propagation, sun angle |
| `src/ember/routers/satellite.py` | Router: `/next-pass` and `/sources` endpoints |
| `tests/test_satellite.py` | 48 unit tests covering all service logic |
