# Implementation Plan: Add Missing Ember Weather Endpoints

## Overview

Add missing Open-Meteo weather endpoints to Ember API to unblock the final 4 weather function migrations in MCP Hub.

**Project**: Ember (Backend-for-Frontend proxy service)
**Scope**: Add 2 new endpoints + enhance 1 existing endpoint
**Impact**: Unblocks 4 MCP Hub weather functions from completing Ember migration
**Estimated Time**: 4-6 hours
**Difficulty**: Medium (requires understanding Open-Meteo API variations)

---

## Background

### Current State

**Ember has 2 weather endpoints** (`src/ember/routers/weather.py`):
- `/weather/current` - Current conditions (uses `/v1/forecast` endpoint)
- `/weather/forecast` - Daily forecast (uses `/v1/forecast` endpoint, daily data only)

**MCP Hub blocked functions** (4 functions in `servers/weather/core/tools/weather_tools.py`):
1. `get_precipitation_history()` (lines 705-775) - Needs **daily historical** data
2. `get_historical_weather()` (lines 778-885) - Needs **daily historical** data
3. `get_hourly_forecast()` (lines 1031-1114) - Needs **hourly forecast** data
4. `get_hourly_weather_history()` (lines 1117-1240) - Needs **hourly historical** data

### Open-Meteo API Endpoints

Open-Meteo provides 2 main weather endpoints:

1. **`/v1/forecast`** - Current conditions + future forecast (daily OR hourly)
   - Supports `daily=` parameter for daily aggregates
   - Supports `hourly=` parameter for hourly data
   - Forecast range: Up to 16 days ahead
   - URL: `https://api.open-meteo.com/v1/forecast`

2. **`/v1/archive`** - Historical weather data (1940-present)
   - Supports `daily=` parameter for daily aggregates
   - Supports `hourly=` parameter for hourly data
   - Date range: Custom `start_date` and `end_date`
   - URL: `https://api.open-meteo.com/v1/archive`

**Key Insight**: The `/v1/forecast` endpoint ALREADY supports hourly data via the `hourly=` parameter. We just need to expose it through Ember.

---

## Required Ember Endpoints

### Endpoint #1: `/weather/historical` (NEW)
- **Purpose**: Get daily historical weather data
- **Open-Meteo Backend**: `/v1/archive` with `daily=` parameters
- **Unblocks**: `get_precipitation_history()`, `get_historical_weather()`
- **Priority**: HIGH

### Endpoint #2: Enhance `/weather/forecast` (MODIFY EXISTING)
- **Purpose**: Add hourly forecast support to existing daily forecast endpoint
- **Open-Meteo Backend**: `/v1/forecast` with `hourly=` parameters
- **Unblocks**: `get_hourly_forecast()`
- **Priority**: MEDIUM (easier than adding new endpoint)

### Endpoint #3: `/weather/historical/hourly` (NEW)
- **Purpose**: Get hourly historical weather data
- **Open-Meteo Backend**: `/v1/archive` with `hourly=` parameters
- **Unblocks**: `get_hourly_weather_history()`
- **Priority**: MEDIUM

---

## Phase 1: Enhance Existing `/weather/forecast` Endpoint

**Goal**: Add `hourly` parameter support to the existing daily forecast endpoint.

**Current Function** (`src/ember/services/openmeteo.py`, lines 129-213):
```python
async def get_forecast(self, lat: float, lon: float, days: int = 3) -> dict[str, Any]:
    """Get weather forecast for a location."""
    # Currently only supports daily forecasts
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": [
            "temperature_2m_max",
            "temperature_2m_min",
            # ... other daily vars
        ],
        "forecast_days": days,
        "timezone": "auto",
    }
    # ... makes request to /v1/forecast
```

### 1.1 Update Service Method

**File**: `src/ember/services/openmeteo.py`

**Add new method** (after existing `get_forecast`):

```python
async def get_hourly_forecast(
    self, lat: float, lon: float, hours: int = 24
) -> dict[str, Any]:
    """
    Get hourly weather forecast for a location.

    Args:
        lat: Latitude
        lon: Longitude
        hours: Number of forecast hours (1-384, up to 16 days)

    Returns:
        Dict with hourly forecast
    """
    # Clamp to valid range (Open-Meteo supports up to 16 days = 384 hours)
    hours = max(1, min(384, hours))

    # Convert hours to days for forecast_days parameter (ceiling division)
    forecast_days = (hours + 23) // 24

    # Check cache
    cache_key = f"weather:hourly_forecast:{lat:.2f},{lon:.2f}:{hours}h"
    cached = _weather_cache.get(cache_key)
    if cached and (time() - cached["timestamp"] < _WEATHER_CACHE_TTL):
        return cached["data"]

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": [
            "temperature_2m",
            "relative_humidity_2m",
            "precipitation",
            "wind_speed_10m",
            "wind_direction_10m",
            "wind_gusts_10m",
            "apparent_temperature",
        ],
        "forecast_days": forecast_days,
        "timezone": "auto",
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(
            OPENMETEO_BASE_URL,  # Uses /v1/forecast
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()

    data = response.json()
    hourly = data.get("hourly", {})

    # Build hourly forecast list (limit to requested hours)
    timestamps = hourly.get("time", [])[:hours]
    temps = hourly.get("temperature_2m", [])[:hours]
    humidities = hourly.get("relative_humidity_2m", [])[:hours]
    precips = hourly.get("precipitation", [])[:hours]
    wind_speeds = hourly.get("wind_speed_10m", [])[:hours]
    wind_dirs = hourly.get("wind_direction_10m", [])[:hours]
    wind_gusts = hourly.get("wind_gusts_10m", [])[:hours]
    apparent_temps = hourly.get("apparent_temperature", [])[:hours]

    forecast = []
    for i, timestamp in enumerate(timestamps):
        forecast.append({
            "timestamp": timestamp,
            "temperature_c": temps[i] if i < len(temps) else None,
            "humidity_pct": humidities[i] if i < len(humidities) else None,
            "precipitation_mm": precips[i] if i < len(precips) else None,
            "wind_speed_kmh": wind_speeds[i] if i < len(wind_speeds) else None,
            "wind_direction_deg": wind_dirs[i] if i < len(wind_dirs) else None,
            "wind_gusts_kmh": wind_gusts[i] if i < len(wind_gusts) else None,
            "feels_like_c": apparent_temps[i] if i < len(apparent_temps) else None,
        })

    result = {
        "status": "success",
        "latitude": lat,
        "longitude": lon,
        "timezone": data.get("timezone"),
        "hourly_forecast": forecast,
        "forecast_hours": len(forecast),
    }

    # Store in cache (1 hour TTL - hourly data updates frequently)
    if len(_weather_cache) >= _WEATHER_CACHE_MAX_SIZE:
        _weather_cache.clear()
    _weather_cache[cache_key] = {"timestamp": time(), "data": result}

    return result
```

### 1.2 Add Router Endpoint

**File**: `src/ember/routers/weather.py`

**Add new route** (after existing `/forecast`):

```python
@router.get("/forecast/hourly")
async def get_hourly_forecast(
    lat: Annotated[float, Query(ge=-90, le=90, description="Latitude")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Longitude")],
    hours: Annotated[
        int,
        Query(ge=1, le=384, description="Forecast hours (max 384 = 16 days)")
    ] = 24,
    _user: dict = require_auth,
):
    """
    Get hourly weather forecast for a location.

    Returns hourly forecast with temperature, precipitation, wind, and humidity.
    Maximum forecast range is 384 hours (16 days).
    """
    try:
        result = await openmeteo_service.get_hourly_forecast(lat, lon, hours)
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Weather API error: {str(e)}")
```

### 1.3 Testing

**Manual API test**:
```bash
# Test hourly forecast endpoint
curl -H "Authorization: Bearer $EMBER_TOKEN" \
  "http://localhost:8000/api/v1/weather/forecast/hourly?lat=45.0&lon=-122.0&hours=48"

# Expected response:
{
  "status": "success",
  "latitude": 45.0,
  "longitude": -122.0,
  "timezone": "America/Los_Angeles",
  "hourly_forecast": [
    {
      "timestamp": "2026-02-16T00:00",
      "temperature_c": 12.5,
      "humidity_pct": 75,
      "precipitation_mm": 0.2,
      "wind_speed_kmh": 15.3,
      "wind_direction_deg": 180,
      "wind_gusts_kmh": 25.1,
      "feels_like_c": 10.2
    },
    // ... 47 more hours
  ],
  "forecast_hours": 48
}
```

---

## Phase 2: Add `/weather/historical` Endpoint

**Goal**: Add daily historical weather data endpoint using Open-Meteo `/v1/archive`.

### 2.1 Update Service Constants

**File**: `src/ember/services/openmeteo.py`

**Add constant** (after line 10):

```python
OPENMETEO_BASE_URL = "https://api.open-meteo.com/v1/forecast"
OPENMETEO_ARCHIVE_URL = "https://api.open-meteo.com/v1/archive"  # ADD THIS
```

### 2.2 Add Service Method

**File**: `src/ember/services/openmeteo.py`

**Add new method** (after `get_hourly_forecast`):

```python
async def get_historical_weather(
    self, lat: float, lon: float, start_date: str, end_date: str
) -> dict[str, Any]:
    """
    Get historical daily weather data for a date range.

    Args:
        lat: Latitude
        lon: Longitude
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format

    Returns:
        Dict with daily historical weather data
    """
    # Validate date format (basic check)
    from datetime import datetime
    try:
        datetime.strptime(start_date, "%Y-%m-%d")
        datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError("Dates must be in YYYY-MM-DD format")

    # Check cache (historical data never changes, use permanent cache)
    cache_key = f"weather:historical:{lat:.2f},{lon:.2f}:{start_date}:{end_date}"
    cached = _weather_cache.get(cache_key)
    if cached:  # No TTL check - historical data is permanent
        return cached["data"]

    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "daily": [
            "temperature_2m_mean",
            "temperature_2m_max",
            "temperature_2m_min",
            "relative_humidity_2m_mean",
            "precipitation_sum",
            "rain_sum",
            "wind_speed_10m_max",
        ],
        "timezone": "UTC",
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(
            OPENMETEO_ARCHIVE_URL,  # Uses /v1/archive
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()

    data = response.json()
    daily = data.get("daily", {})

    # Build daily records list
    dates = daily.get("time", [])
    temp_means = daily.get("temperature_2m_mean", [])
    temp_maxs = daily.get("temperature_2m_max", [])
    temp_mins = daily.get("temperature_2m_min", [])
    humidities = daily.get("relative_humidity_2m_mean", [])
    precips = daily.get("precipitation_sum", [])
    rains = daily.get("rain_sum", [])
    winds = daily.get("wind_speed_10m_max", [])

    daily_records = []
    for i, date in enumerate(dates):
        daily_records.append({
            "date": date,
            "temperature_mean_c": temp_means[i] if i < len(temp_means) else None,
            "temperature_max_c": temp_maxs[i] if i < len(temp_maxs) else None,
            "temperature_min_c": temp_mins[i] if i < len(temp_mins) else None,
            "humidity_pct": humidities[i] if i < len(humidities) else None,
            "precipitation_sum_mm": precips[i] if i < len(precips) else None,
            "rain_sum_mm": rains[i] if i < len(rains) else None,
            "wind_speed_max_kmh": winds[i] if i < len(winds) else None,
        })

    result = {
        "status": "success",
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "timezone": data.get("timezone"),
        "daily": daily_records,
        "days_count": len(daily_records),
    }

    # Store in cache permanently (historical data never changes)
    if len(_weather_cache) >= _WEATHER_CACHE_MAX_SIZE:
        _weather_cache.clear()
    _weather_cache[cache_key] = {"data": result}  # No timestamp - permanent

    return result
```

### 2.3 Add Router Endpoint

**File**: `src/ember/routers/weather.py`

**Add new route** (after `/forecast/hourly`):

```python
@router.get("/historical")
async def get_historical_weather(
    lat: Annotated[float, Query(ge=-90, le=90, description="Latitude")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Longitude")],
    start_date: Annotated[
        str,
        Query(
            pattern=r"^\d{4}-\d{2}-\d{2}$",
            description="Start date (YYYY-MM-DD)",
        ),
    ],
    end_date: Annotated[
        str,
        Query(
            pattern=r"^\d{4}-\d{2}-\d{2}$",
            description="End date (YYYY-MM-DD)",
        ),
    ],
    _user: dict = require_auth,
):
    """
    Get historical daily weather data for a date range.

    Returns daily weather records from 1940 to present.
    Data includes temperature, humidity, precipitation, and wind.
    """
    try:
        result = await openmeteo_service.get_historical_weather(
            lat, lon, start_date, end_date
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Weather API error: {str(e)}")
```

### 2.4 Testing

**Manual API test**:
```bash
# Test historical weather endpoint
curl -H "Authorization: Bearer $EMBER_TOKEN" \
  "http://localhost:8000/api/v1/weather/historical?lat=45.0&lon=-122.0&start_date=2024-01-01&end_date=2024-01-07"

# Expected response:
{
  "status": "success",
  "latitude": 45.0,
  "longitude": -122.0,
  "start_date": "2024-01-01",
  "end_date": "2024-01-07",
  "timezone": "UTC",
  "daily": [
    {
      "date": "2024-01-01",
      "temperature_mean_c": 8.5,
      "temperature_max_c": 12.0,
      "temperature_min_c": 5.0,
      "humidity_pct": 80,
      "precipitation_sum_mm": 5.2,
      "rain_sum_mm": 5.2,
      "wind_speed_max_kmh": 25.3
    },
    // ... 6 more days
  ],
  "days_count": 7
}
```

---

## Phase 3: Add `/weather/historical/hourly` Endpoint

**Goal**: Add hourly historical weather data endpoint using Open-Meteo `/v1/archive` with hourly parameters.

### 3.1 Add Service Method

**File**: `src/ember/services/openmeteo.py`

**Add new method** (after `get_historical_weather`):

```python
async def get_hourly_historical_weather(
    self, lat: float, lon: float, start_date: str, end_date: str
) -> dict[str, Any]:
    """
    Get historical hourly weather data for a date range.

    Args:
        lat: Latitude
        lon: Longitude
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format

    Returns:
        Dict with hourly historical weather data
    """
    # Validate date format (basic check)
    from datetime import datetime
    try:
        datetime.strptime(start_date, "%Y-%m-%d")
        datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError("Dates must be in YYYY-MM-DD format")

    # Check cache (historical data never changes, use permanent cache)
    cache_key = f"weather:historical:hourly:{lat:.2f},{lon:.2f}:{start_date}:{end_date}"
    cached = _weather_cache.get(cache_key)
    if cached:  # No TTL check - historical data is permanent
        return cached["data"]

    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "hourly": [
            "temperature_2m",
            "relative_humidity_2m",
            "precipitation",
            "wind_speed_10m",
            "wind_direction_10m",
            "wind_gusts_10m",
            "apparent_temperature",
        ],
        "timezone": "UTC",
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(
            OPENMETEO_ARCHIVE_URL,  # Uses /v1/archive
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()

    data = response.json()
    hourly = data.get("hourly", {})

    # Build hourly records list
    timestamps = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    humidities = hourly.get("relative_humidity_2m", [])
    precips = hourly.get("precipitation", [])
    wind_speeds = hourly.get("wind_speed_10m", [])
    wind_dirs = hourly.get("wind_direction_10m", [])
    wind_gusts = hourly.get("wind_gusts_10m", [])
    apparent_temps = hourly.get("apparent_temperature", [])

    hourly_records = []
    for i, timestamp in enumerate(timestamps):
        hourly_records.append({
            "timestamp": timestamp,
            "temperature_c": temps[i] if i < len(temps) else None,
            "humidity_pct": humidities[i] if i < len(humidities) else None,
            "precipitation_mm": precips[i] if i < len(precips) else None,
            "wind_speed_kmh": wind_speeds[i] if i < len(wind_speeds) else None,
            "wind_direction_deg": wind_dirs[i] if i < len(wind_dirs) else None,
            "wind_gusts_kmh": wind_gusts[i] if i < len(wind_gusts) else None,
            "feels_like_c": apparent_temps[i] if i < len(apparent_temps) else None,
        })

    result = {
        "status": "success",
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date,
        "end_date": end_date,
        "timezone": data.get("timezone"),
        "hourly_history": hourly_records,
        "total_hours": len(hourly_records),
    }

    # Store in cache permanently (historical data never changes)
    if len(_weather_cache) >= _WEATHER_CACHE_MAX_SIZE:
        _weather_cache.clear()
    _weather_cache[cache_key] = {"data": result}  # No timestamp - permanent

    return result
```

### 3.2 Add Router Endpoint

**File**: `src/ember/routers/weather.py`

**Add new route** (after `/historical`):

```python
@router.get("/historical/hourly")
async def get_hourly_historical_weather(
    lat: Annotated[float, Query(ge=-90, le=90, description="Latitude")],
    lon: Annotated[float, Query(ge=-180, le=180, description="Longitude")],
    start_date: Annotated[
        str,
        Query(
            pattern=r"^\d{4}-\d{2}-\d{2}$",
            description="Start date (YYYY-MM-DD)",
        ),
    ],
    end_date: Annotated[
        str,
        Query(
            pattern=r"^\d{4}-\d{2}-\d{2}$",
            description="End date (YYYY-MM-DD)",
        ),
    ],
    _user: dict = require_auth,
):
    """
    Get historical hourly weather data for a date range.

    Returns hourly weather records from 1940 to present.
    Data includes temperature, humidity, precipitation, and wind for each hour.
    """
    try:
        result = await openmeteo_service.get_hourly_historical_weather(
            lat, lon, start_date, end_date
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Weather API error: {str(e)}")
```

### 3.3 Testing

**Manual API test**:
```bash
# Test hourly historical weather endpoint
curl -H "Authorization: Bearer $EMBER_TOKEN" \
  "http://localhost:8000/api/v1/weather/historical/hourly?lat=45.0&lon=-122.0&start_date=2024-01-01&end_date=2024-01-03"

# Expected response:
{
  "status": "success",
  "latitude": 45.0,
  "longitude": -122.0,
  "start_date": "2024-01-01",
  "end_date": "2024-01-03",
  "timezone": "UTC",
  "hourly_history": [
    {
      "timestamp": "2024-01-01T00:00",
      "temperature_c": 8.5,
      "humidity_pct": 80,
      "precipitation_mm": 0.5,
      "wind_speed_kmh": 15.3,
      "wind_direction_deg": 180,
      "wind_gusts_kmh": 25.1,
      "feels_like_c": 6.2
    },
    // ... 71 more hours (3 days * 24 hours)
  ],
  "total_hours": 72
}
```

---

## Phase 4: Add `ember_client` Wrapper Functions in MCP Hub

**Goal**: Add corresponding wrapper functions in MCP Hub's `shared/ember_client.py` to call the new Ember endpoints.

**File**: `/Users/onyxbook/Code/python-projects/mcp-hub/shared/ember_client.py`

**Add 3 new functions** (after existing weather functions):

```python
async def get_hourly_forecast(
    lat: float,
    lon: float,
    hours: int = 24,
) -> dict[str, Any]:
    """
    Get hourly weather forecast via Ember API.

    Args:
        lat: Latitude in decimal degrees
        lon: Longitude in decimal degrees
        hours: Number of hours to forecast (1-384, default 24)

    Returns:
        dict: Hourly forecast data from Open-Meteo via Ember
    """
    params = {
        "lat": lat,
        "lon": lon,
        "hours": hours,
    }

    response = await _make_request(
        method="GET",
        path="/weather/forecast/hourly",
        params=params,
    )
    return response


async def get_historical_weather(
    lat: float,
    lon: float,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """
    Get historical daily weather data via Ember API.

    Args:
        lat: Latitude in decimal degrees
        lon: Longitude in decimal degrees
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format

    Returns:
        dict: Daily historical weather data from Open-Meteo via Ember
    """
    params = {
        "lat": lat,
        "lon": lon,
        "start_date": start_date,
        "end_date": end_date,
    }

    response = await _make_request(
        method="GET",
        path="/weather/historical",
        params=params,
    )
    return response


async def get_hourly_historical_weather(
    lat: float,
    lon: float,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """
    Get historical hourly weather data via Ember API.

    Args:
        lat: Latitude in decimal degrees
        lon: Longitude in decimal degrees
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format

    Returns:
        dict: Hourly historical weather data from Open-Meteo via Ember
    """
    params = {
        "lat": lat,
        "lon": lon,
        "start_date": start_date,
        "end_date": end_date,
    }

    response = await _make_request(
        method="GET",
        path="/weather/historical/hourly",
        params=params,
    )
    return response
```

**Export in `__all__`** (update line ~20):

```python
__all__ = [
    # ... existing exports
    "get_weather_forecast",
    "get_current_weather",
    "get_hourly_forecast",           # ADD
    "get_historical_weather",        # ADD
    "get_hourly_historical_weather", # ADD
]
```

---

## Phase 5: Integration Testing

**Goal**: Test all 3 new Ember endpoints end-to-end.

### 5.1 Test Each Endpoint Individually

```bash
# Navigate to Ember project
cd ~/Code/python-projects/ember

# Start Ember service (if not running)
DEBUG=true ENVIRONMENT=development uvicorn ember.main:app --reload --port 8000

# In another terminal, get M2M token
export EMBER_TOKEN=$(curl -s -X POST "https://${AUTH0_DOMAIN}/oauth/token" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "'${AUTH0_CLIENT_ID}'",
    "client_secret": "'${AUTH0_CLIENT_SECRET}'",
    "audience": "https://api.ember.local",
    "grant_type": "client_credentials"
  }' | jq -r '.access_token')

# Test 1: Hourly forecast
curl -s -H "Authorization: Bearer $EMBER_TOKEN" \
  "http://localhost:8000/api/v1/weather/forecast/hourly?lat=45.0&lon=-122.0&hours=48" | jq

# Test 2: Historical daily
curl -s -H "Authorization: Bearer $EMBER_TOKEN" \
  "http://localhost:8000/api/v1/weather/historical?lat=45.0&lon=-122.0&start_date=2024-01-01&end_date=2024-01-07" | jq

# Test 3: Historical hourly
curl -s -H "Authorization: Bearer $EMBER_TOKEN" \
  "http://localhost:8000/api/v1/weather/historical/hourly?lat=45.0&lon=-122.0&start_date=2024-01-01&end_date=2024-01-03" | jq
```

### 5.2 Test MCP Hub `ember_client` Functions

```bash
# Navigate to MCP Hub
cd ~/Code/python-projects/mcp-hub

# Create test script
cat > /tmp/test_ember_weather.py << 'EOF'
import asyncio
from shared import ember_client

async def test_new_endpoints():
    print("Testing hourly forecast...")
    result1 = await ember_client.get_hourly_forecast(45.0, -122.0, hours=24)
    print(f"✅ Hourly forecast: {result1['forecast_hours']} hours")

    print("\nTesting historical weather...")
    result2 = await ember_client.get_historical_weather(
        45.0, -122.0, "2024-01-01", "2024-01-07"
    )
    print(f"✅ Historical weather: {result2['days_count']} days")

    print("\nTesting hourly historical...")
    result3 = await ember_client.get_hourly_historical_weather(
        45.0, -122.0, "2024-01-01", "2024-01-03"
    )
    print(f"✅ Hourly historical: {result3['total_hours']} hours")

asyncio.run(test_new_endpoints())
EOF

# Run test
python /tmp/test_ember_weather.py
```

### 5.3 Verify All 3 Endpoints Work

**Success criteria**:
- ✅ Hourly forecast returns 24-48 hours of data
- ✅ Historical daily returns 7 days of data
- ✅ Historical hourly returns 72 hours of data (3 days * 24)
- ✅ All responses include proper status, coordinates, timezone
- ✅ No authentication errors (401)
- ✅ No server errors (500)

---

## Phase 6: Error Handling & Edge Cases

### 6.1 Invalid Date Formats

**Test**:
```bash
curl -H "Authorization: Bearer $EMBER_TOKEN" \
  "http://localhost:8000/api/v1/weather/historical?lat=45.0&lon=-122.0&start_date=2024/01/01&end_date=2024-01-07"

# Expected: 400 Bad Request with validation error
```

### 6.2 Out of Range Coordinates

**Test**:
```bash
curl -H "Authorization: Bearer $EMBER_TOKEN" \
  "http://localhost:8000/api/v1/weather/forecast/hourly?lat=95.0&lon=-122.0&hours=24"

# Expected: 422 Unprocessable Entity (FastAPI validation)
```

### 6.3 Future Dates in Historical Endpoint

**Test**:
```bash
curl -H "Authorization: Bearer $EMBER_TOKEN" \
  "http://localhost:8000/api/v1/weather/historical?lat=45.0&lon=-122.0&start_date=2030-01-01&end_date=2030-01-07"

# Expected: Open-Meteo may return empty data or error (handle gracefully)
```

### 6.4 Large Date Ranges

**Test**:
```bash
# Request 1 year of hourly data (8760 hours)
curl -H "Authorization: Bearer $EMBER_TOKEN" \
  "http://localhost:8000/api/v1/weather/historical/hourly?lat=45.0&lon=-122.0&start_date=2023-01-01&end_date=2023-12-31"

# Watch for:
# - Response time (should be < 5 seconds)
# - Response size (should be < 10MB)
# - Memory usage (should not spike)
```

**Consider**: Add max date range validation (e.g., 90 days for hourly historical).

---

## Phase 7: Documentation & Commit

### 7.1 Update Ember API Documentation

**File**: `src/ember/README.md` (or `docs/API.md`)

**Add new endpoints** to API documentation:

```markdown
### Weather Endpoints

#### GET /api/v1/weather/forecast/hourly
Get hourly weather forecast (up to 16 days / 384 hours).

**Parameters:**
- `lat` (float): Latitude (-90 to 90)
- `lon` (float): Longitude (-180 to 180)
- `hours` (int): Forecast hours (1-384, default 24)

**Response:** Hourly forecast with temperature, wind, precipitation, humidity.

---

#### GET /api/v1/weather/historical
Get historical daily weather data (1940-present).

**Parameters:**
- `lat` (float): Latitude (-90 to 90)
- `lon` (float): Longitude (-180 to 180)
- `start_date` (string): Start date (YYYY-MM-DD)
- `end_date` (string): End date (YYYY-MM-DD)

**Response:** Daily historical weather records.

---

#### GET /api/v1/weather/historical/hourly
Get historical hourly weather data (1940-present).

**Parameters:**
- `lat` (float): Latitude (-90 to 90)
- `lon` (float): Longitude (-180 to 180)
- `start_date` (string): Start date (YYYY-MM-DD)
- `end_date` (string): End date (YYYY-MM-DD)

**Response:** Hourly historical weather records.
```

### 7.2 Commit Strategy

**Commit 1: Add hourly forecast endpoint**
```bash
git add src/ember/services/openmeteo.py src/ember/routers/weather.py
git commit -m "feat: Add hourly weather forecast endpoint

- Add get_hourly_forecast() service method
- Add /weather/forecast/hourly route
- Supports up to 384 hours (16 days) of forecast
- Uses Open-Meteo /v1/forecast with hourly parameters
- Cache TTL: 1 hour

Unblocks: MCP Hub get_hourly_forecast() migration"
```

**Commit 2: Add historical weather endpoints**
```bash
git add src/ember/services/openmeteo.py src/ember/routers/weather.py
git commit -m "feat: Add historical weather endpoints (daily & hourly)

- Add get_historical_weather() service method
- Add get_hourly_historical_weather() service method
- Add /weather/historical route (daily data)
- Add /weather/historical/hourly route (hourly data)
- Uses Open-Meteo /v1/archive endpoint
- Permanent cache (historical data never changes)

Unblocks: MCP Hub historical weather function migrations"
```

**Commit 3: Add ember_client wrappers in MCP Hub**
```bash
cd ~/Code/python-projects/mcp-hub
git add shared/ember_client.py
git commit -m "feat: Add ember_client wrappers for new weather endpoints

- Add get_hourly_forecast()
- Add get_historical_weather()
- Add get_hourly_historical_weather()

Enables: MCP Hub weather tools to use new Ember endpoints"
```

---

## Phase 8: Next Steps (MCP Hub Function Refactoring)

**After Ember endpoints are deployed**, refactor the 4 blocked MCP Hub functions:

### 8.1 Function Migration Order

1. **`get_hourly_forecast()`** (easiest)
   - Replace `httpx.AsyncClient` with `ember_client.get_hourly_forecast()`
   - Response format already compatible (just key name changes)
   - Estimated: 30 minutes

2. **`get_precipitation_history()`** (medium)
   - Replace with `ember_client.get_historical_weather()`
   - Extract only precipitation fields from daily records
   - Estimated: 45 minutes

3. **`get_historical_weather()`** (medium)
   - Direct replacement with `ember_client.get_historical_weather()`
   - Response format nearly identical
   - Estimated: 45 minutes

4. **`get_hourly_weather_history()`** (medium)
   - Replace with `ember_client.get_hourly_historical_weather()`
   - Response format compatible
   - Estimated: 45 minutes

**Total estimated time for MCP Hub refactoring**: 2.5-3 hours

### 8.2 Create Follow-Up Plan

After completing this Ember work, create a separate implementation plan:

**`WEATHER_TOOLS_FINAL_REFACTORING_PLAN.md`**
- Step-by-step for refactoring the 4 blocked functions
- Test updates for each function
- Verification checklist
- Target: Junior developer or devstral-2

---

## Summary

### Files to Modify in Ember

| File | Changes | Lines Added |
|------|---------|-------------|
| `src/ember/services/openmeteo.py` | Add 3 service methods + 1 constant | ~200 |
| `src/ember/routers/weather.py` | Add 3 route handlers | ~80 |

### Files to Modify in MCP Hub

| File | Changes | Lines Added |
|------|---------|-------------|
| `shared/ember_client.py` | Add 3 wrapper functions | ~80 |

### Testing Checklist

- [ ] `/weather/forecast/hourly` returns valid hourly forecast
- [ ] `/weather/historical` returns valid daily historical data
- [ ] `/weather/historical/hourly` returns valid hourly historical data
- [ ] All endpoints handle invalid coordinates (422)
- [ ] All endpoints handle invalid dates (400)
- [ ] Authentication required (401 without token)
- [ ] Cache works (second request faster than first)
- [ ] `ember_client` wrappers work from MCP Hub
- [ ] No memory leaks with large date ranges
- [ ] Response times acceptable (< 5 seconds typical)

### Success Criteria

✅ All 3 Ember endpoints deployed and tested
✅ All endpoints properly authenticated (Auth0 M2M)
✅ Response formats match Open-Meteo documentation
✅ Cache behavior appropriate (TTL for forecasts, permanent for historical)
✅ MCP Hub `ember_client` wrappers tested and working
✅ Documentation updated
✅ Code committed with clear messages
✅ Unblocks 4 MCP Hub weather function migrations

---

## References

**Open-Meteo API Documentation:**
- [Weather Forecast API](https://open-meteo.com/en/docs) - `/v1/forecast` endpoint
- [Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api) - `/v1/archive` endpoint
- [API Features](https://open-meteo.com/en/features) - Performance and capabilities
- [Historical Forecast API](https://open-meteo.com/en/docs/historical-forecast-api) - Alternative historical approach

**Related MCP Hub Plans:**
- `WEATHER_TOOLS_REFACTORING_PLAN.md` - 5 completed weather function migrations
- `GEOCODING_TOOLS_REFACTORING_PLAN.md` - Geocoding refactoring guide

**Related PRs:**
- PR #34 - Active fires refactoring
- PR #36 - Terrain tools refactoring
- PR #37 - 5 weather tools refactoring (merged)

---

## Appendix: Open-Meteo API Details

### Forecast Endpoint (`/v1/forecast`)

**URL**: `https://api.open-meteo.com/v1/forecast`

**Parameters:**
- `latitude`, `longitude` - Location
- `hourly` - Comma-separated list of hourly variables
- `daily` - Comma-separated list of daily variables
- `forecast_days` - Number of forecast days (1-16)
- `timezone` - Timezone for timestamps (default: GMT)

**Hourly Variables** (used by MCP Hub):
- `temperature_2m`, `relative_humidity_2m`, `precipitation`
- `wind_speed_10m`, `wind_direction_10m`, `wind_gusts_10m`
- `apparent_temperature`

### Archive Endpoint (`/v1/archive`)

**URL**: `https://api.open-meteo.com/v1/archive`

**Parameters:**
- `latitude`, `longitude` - Location
- `start_date`, `end_date` - Date range (YYYY-MM-DD)
- `hourly` - Comma-separated list of hourly variables
- `daily` - Comma-separated list of daily variables
- `timezone` - Timezone for timestamps (default: GMT)

**Daily Variables** (used by MCP Hub):
- `temperature_2m_mean`, `temperature_2m_max`, `temperature_2m_min`
- `relative_humidity_2m_mean`, `precipitation_sum`, `rain_sum`
- `wind_speed_10m_max`

**Historical Coverage**: 1940-present, 9-10km resolution, reanalysis data

---

**Plan created**: 2026-02-16
**Target completion**: TBD
**Dependencies**: Auth0 M2M credentials, Ember service deployed
**Next plan**: `WEATHER_TOOLS_FINAL_REFACTORING_PLAN.md` (after this completes)
