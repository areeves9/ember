# Ember

Direct API proxy for wildfire data - feeds Nova frontend.

Built with Python 3.12+, FastAPI, and async/await patterns.

## Endpoints

- `/api/v1/fires` - NASA FIRMS fire detections
- `/api/v1/geocode` - Nominatim geocoding
- `/api/v1/weather` - Open-Meteo weather
- `/api/v1/fuel` - LANDFIRE fuel models
- `/api/v1/vegetation` - Copernicus NDVI/NDMI
- `/api/v1/terrain` - LANDFIRE terrain layers (elevation, slope, aspect, canopy)

## Setup

```bash
# Install uv if needed: https://docs.astral.sh/uv/
cp .env.example .env
# Fill in required values (FIRMS_MAP_KEY, SUPABASE_URL, etc.)
uv sync --frozen
uv run python entrypoint.py
```

## Docker

```bash
docker compose up --build
```

## Testing

```bash
pytest tests/
```
