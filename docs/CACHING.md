# Ember Caching Strategy

Ember uses simple in-memory caching to reduce external API calls and improve response times. Each service maintains its own cache with appropriate TTLs based on data freshness requirements.

## Cache Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Ember Services                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────┐│
│  │  Terrain    │  │   Fires     │  │  Weather    │  │ Geocode ││
│  │  Service    │  │  Service    │  │  Service    │  │ Service ││
│  ├─────────────┤  ├─────────────┤  ├─────────────┤  ├─────────┤│
│  │ TTL: 30min  │  │ TTL: 10min  │  │ TTL: 5min   │  │TTL: 24h ││
│  │ Max: 1000   │  │ Max: 100    │  │ Max: 500    │  │Max: 1000││
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └────┬────┘│
│         │                │                │               │     │
│         ▼                ▼                ▼               ▼     │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                    In-Memory Dict Cache                      ││
│  │         { cache_key: { timestamp, data } }                   ││
│  └─────────────────────────────────────────────────────────────┘│
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Namespace Convention

Cache keys follow a structured format for clarity and collision avoidance:

```
{service}:{operation}:{parameters}
```

### Terrain Cache

**Location:** `src/ember/services/terrain.py`

| Key Pattern | Example | TTL |
|-------------|---------|-----|
| `{lat:.4f},{lon:.4f}:{sorted_layers}` | `34.3500,-118.1000:aspect,elevation,fuel,slope` | 30 min |

**Rationale:** LANDFIRE data is static (updated annually). 4 decimal precision (~11m) balances cache hits with accuracy.

```python
cache_key = f"{lat:.4f},{lon:.4f}:{','.join(sorted(layers))}"
```

### Fires Cache (FIRMS)

**Location:** `src/ember/services/firms.py`

| Key Pattern | Example | TTL |
|-------------|---------|-----|
| `fires:{bbox}:{source}:{days}:{radius}` | `fires:34.00,35.00,-119.00,-118.00:VIIRS_SNPP_NRT:2:1.0` | 10 min |

**Rationale:** NASA FIRMS updates every ~10 minutes. Bbox rounded to 2 decimals for reasonable grouping.

```python
cache_key = f"fires:{min_lat:.2f},{max_lat:.2f},{min_lon:.2f},{max_lon:.2f}:{source}:{days_back}:{cluster_radius_km}"
```

### Weather Cache (Open-Meteo)

**Location:** `src/ember/services/openmeteo.py`

| Key Pattern | Example | TTL |
|-------------|---------|-----|
| `weather:current:{lat},{lon}` | `weather:current:34.35,-118.10` | 5 min |
| `weather:forecast:{lat},{lon}:{days}` | `weather:forecast:34.35,-118.10:3` | 5 min |

**Rationale:** Weather changes frequently but not per-second. 2 decimal precision (~1km) groups nearby requests.

```python
cache_key = f"weather:current:{lat:.2f},{lon:.2f}"
cache_key = f"weather:forecast:{lat:.2f},{lon:.2f}:{days}"
```

### Geocode Cache (Nominatim)

**Location:** `src/ember/services/nominatim.py`

| Key Pattern | Example | TTL |
|-------------|---------|-----|
| `geocode:{address}:{country}` | `geocode:los angeles, ca:us` | 24 hr |
| `reverse:{lat},{lon}:{zoom}` | `reverse:34.3500,-118.1000:18` | 24 hr |

**Rationale:** Addresses rarely change. Long TTL reduces load on public Nominatim servers.

```python
cache_key = f"geocode:{address.lower()}:{country or ''}"
cache_key = f"reverse:{lat:.4f},{lon:.4f}:{zoom}"
```

## Cache Configuration

| Service | TTL | Max Size | Eviction |
|---------|-----|----------|----------|
| Terrain | 1800s (30 min) | 1000 entries | Clear all when full |
| Fires | 600s (10 min) | 100 entries | Clear all when full |
| Weather | 300s (5 min) | 500 entries | Clear all when full |
| Geocode | 86400s (24 hr) | 1000 entries | Clear all when full |

## Full-Extent Raster Caching (ORQ-140)

COG-backed imagery endpoints accept an optional bbox. When bbox is **omitted**,
Ember reads a downsampled overview of the full native extent (rio-tiler's
`Reader.preview(max_size=...)`) and returns the result with a browser-cache
`Cache-Control` header, since the response is far more reusable than a
viewport-scoped crop.

| Endpoint | No-Bbox Cache Key | Server TTL | `Cache-Control` TTL |
|----------|-------------------|------------|---------------------|
| `/terrain?format=raster` | `raster:{layer}:full:{max_size}` | 24 h | `public, max-age=86400` |
| `/imagery/truecolor-cog` | `{format}:preview:{scene_id}:B04,B03,B02:{max_size}` | 24 h | `public, max-age=21600` |
| `/imagery/ndvi-cog` | `idx:ndvi:{format}:preview:{scene_id}:B08,B04:{max_size}` | 24 h | `public, max-age=21600` |
| `/imagery/ndmi-cog` | `idx:ndmi:{format}:preview:{scene_id}:B08,B11:{max_size}` | 24 h | `public, max-age=21600` |

**Rationale:**
- Full-extent reads are keyed only by layer + overview size (no viewport), so
  cache hit rate is naturally high — different users get the same bytes.
- LANDFIRE data updates annually → long (24 h) `Cache-Control`.
- Sentinel-2 NRT scenes publish every 3-5 days per MGRS tile → 6 h
  `Cache-Control` is a safe buffer within a single session without staling
  against new acquisitions.
- Bbox-scoped responses deliberately do **not** carry long `Cache-Control`
  because they vary per-viewport and per-user.

**Knobs:** `OVERVIEW_MAX_SIZE` (default `1200`) caps the output dimension.
`SENTINEL_DEFAULT_REGION` (default `-125,24,-66,50` — CONUS) seeds the STAC
search when bbox is omitted on Sentinel-2 endpoints.

## Bbox-Scoped Raster Caching (ORQ-141)

When bbox params **are** provided, the response is viewport-specific, so the
server TTL stays short. A 1 h browser cache is enough to cover the pan-away /
come-back case without adding full round-trips for every small viewport move.

| Endpoint | Path | `Cache-Control` TTL |
|----------|------|---------------------|
| `/imagery/ndvi-cog` (bbox) | bbox crop path | `public, max-age=3600` |
| `/imagery/ndmi-cog` (bbox) | bbox crop path | `public, max-age=3600` |
| `/imagery/truecolor-cog` (bbox) | bbox crop path | `public, max-age=3600` |
| `/terrain?format=raster` (bbox) | bbox raster path | `public, max-age=3600` |

**Rationale:** Bbox bytes are viewport-specific, so the 6 h / 24 h full-extent
values would over-cache stale crops. One hour covers a typical user session
(pan, zoom, come back) without meaningfully delaying freshness.

**Knob:** `BBOX_CACHE_SECONDS` (default `3600`) controls the TTL. Full-extent
responses keep their own per-endpoint values (`FULL_EXTENT_CACHE_SECONDS`) and
are not affected by this setting.

## Implementation Pattern

All caches follow the same pattern:

```python
from time import time

# Module-level cache
_cache: dict[str, dict] = {}
_CACHE_TTL = 300  # seconds
_CACHE_MAX_SIZE = 500

async def get_data(lat: float, lon: float) -> dict:
    # 1. Check cache
    cache_key = f"service:{lat:.2f},{lon:.2f}"
    cached = _cache.get(cache_key)
    if cached and (time() - cached["timestamp"] < _CACHE_TTL):
        return cached["data"]

    # 2. Fetch from external API
    result = await fetch_external_api(lat, lon)

    # 3. Store in cache (with simple eviction)
    if len(_cache) >= _CACHE_MAX_SIZE:
        _cache.clear()
    _cache[cache_key] = {"timestamp": time(), "data": result}

    return result
```

## Precision Guidelines

| Decimal Places | Approximate Precision | Use Case |
|----------------|----------------------|----------|
| 2 decimals | ~1.1 km | Weather, fire bbox |
| 4 decimals | ~11 m | Terrain, reverse geocode |
| 6 decimals | ~0.1 m | Not needed for caching |

## Performance Impact

| Endpoint | Uncached | Cached | Improvement |
|----------|----------|--------|-------------|
| `/terrain` | 500-1000ms | 10-50ms | 10-20x |
| `/fires` | 1000-3000ms | 10-50ms | 20-60x |
| `/weather` | 200-500ms | 10-50ms | 4-10x |
| `/geocode` | 300-800ms | 10-50ms | 6-16x |

## Future Considerations

### Redis Migration

The current in-memory cache can be migrated to Redis for:
- Shared cache across multiple Ember instances
- Persistence across restarts
- More sophisticated eviction (LRU instead of clear-all)

Key format is already Redis-compatible:
```
terrain:34.3500,-118.1000:aspect,elevation,fuel,slope
fires:34.00,35.00,-119.00,-118.00:VIIRS_SNPP_NRT:2:1.0
weather:current:34.35,-118.10
geocode:los angeles, ca:us
```

### Cache Invalidation

Currently caches expire via TTL only. Future options:
- Manual invalidation endpoint (`POST /cache/clear`)
- Webhook-triggered invalidation (e.g., new FIRMS data available)
- Tag-based invalidation (clear all `weather:*` keys)

## Monitoring

To add cache hit/miss metrics:

```python
import logging
logger = logging.getLogger(__name__)

# In cache check
if cached and (time() - cached["timestamp"] < _CACHE_TTL):
    logger.debug(f"Cache HIT: {cache_key}")
    return cached["data"]

logger.debug(f"Cache MISS: {cache_key}")
```

View cache stats in logs:
```bash
docker compose logs ember | grep -E "Cache (HIT|MISS)"
```
