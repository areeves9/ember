# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Ember is a wildfire data API proxy that integrates with NASA FIRMS, OpenMeteo, Copernicus, LANDFIRE, and Nominatim. It serves as the backend for the Nova frontend. Built with Python 3.12+, FastAPI, and async/await patterns.

## Commands

```bash
# Development
uv run python entrypoint.py          # Start server (port 8001)
uv sync --frozen                     # Install all dependencies (dev + prod)
uv sync --frozen --no-dev            # Install prod dependencies only

# Linting & Formatting
ruff check src/                      # Check for lint errors
ruff check src/ --fix                # Auto-fix lint errors
ruff format src/                     # Format code

# Testing (pytest-asyncio with asyncio_mode="auto")
pytest tests/                        # Run all tests
pytest tests/test_file.py            # Run single test file
pytest tests/test_file.py::TestClass::test_name  # Run specific test

# Docker
docker compose up --build            # Start with Docker
```

## Architecture

### Service Layer Pattern
Each external API has a dedicated service class in `src/ember/services/`. Services handle caching, error handling, and data transformation. Singleton instances are exported at module level:

```python
class SomeService:
    def __init__(self): ...
    async def get_data(self): ...

service_instance = SomeService()
```

### Router Pattern
Routers in `src/ember/routers/` use prefix + tags. All routes require authentication by default:

```python
router = APIRouter(prefix="/fires", tags=["fires"])

@router.get("")
async def handler(..., _user: dict = require_auth):
    ...
```

### Caching
In-memory caching with TTL-based expiration. Cache keys follow `namespace:param1:param2:...` format. TTLs vary by service (5min weather → 24hr geocode). See `docs/CACHING.md` for details.

### Exception Hierarchy
Custom exceptions in `src/ember/exceptions.py` map to HTTP status codes:
- `ExternalAPIError` (502), `AuthenticationError` (401), `AuthorizationError` (403)
- `ValidationError` (400), `NotFoundError` (404), `RateLimitError` (429)

### Configuration
Pydantic Settings loaded from `.env`. Access via `get_settings()` singleton. Environment detection available via `settings.is_development`.

### Authentication
JWT-based via Supabase. Supports ES256 (JWKS) + HS256 fallback. Dev mode auto-authenticates without config.

### Logging
Use `get_logger(__name__)`. Three formatters: `DevFormatter` (colorized), `StructuredFormatter` (JSON for prod), plain text.

## Key Directories

- `src/ember/services/` - Business logic and external API clients
- `src/ember/routers/` - API endpoints (fires, fuel, geocode, weather, vegetation, terrain)
- `src/ember/api/` - Middleware and error handlers
- `docs/` - Architecture documentation (CACHING.md, COG_PIPELINE.md)
- `scripts/` - Data sync scripts
- `tests/` - Pytest tests (use `asyncio_mode="auto"`, no need for `@pytest.mark.asyncio` decorator)

## Geospatial Notes

- Cloud Optimized GeoTIFFs (COG) stored in S3 for LANDFIRE data
- DBSCAN clustering for fire detection aggregation (runs in thread pool)
- GeoJSON output for map rendering
- Geodesic area calculations use WGS84 ellipsoid
